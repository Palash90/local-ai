"""OpenAI-compatible API endpoints.

Provides ``/v1/chat/completions`` and ``/v1/models`` on the same port as the
chat UI (3001).  Authentication uses a static ``OPENAI_API_KEY`` from the
environment, sent as ``Authorization: Bearer <key>``.

Chat completions are routed through the existing chat pipeline (system prompt,
tool calling, multi-round orchestration, session management) rather than being
proxied raw to llama-server.
"""

import json
import threading
import time
import uuid

import requests

from server.config import (
    LLAMA_BASE,
    LLAMA_BASE_CPU,
    MODEL_ID,
    MODEL_ID_CPU,
    OPENAI_API_KEY,
)
from server.features.state import M


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _require_api_key(handler):
    """Return the caller identity dict or send a 401 and return None."""
    if not OPENAI_API_KEY:
        handler.send_json(
            {"error": {"message": "Server has no OPENAI_API_KEY configured", "type": "server_error"}},
            status=500,
        )
        return None
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        handler.send_json(
            {"error": {"message": "Missing Authorization header", "type": "invalid_request_error"}},
            status=401,
        )
        return None
    token = auth[len("Bearer "):].strip()
    if not __import__("hmac").compare_digest(token, OPENAI_API_KEY):
        handler.send_json(
            {"error": {"message": "Invalid API key", "type": "invalid_request_error"}},
            status=401,
        )
        return None
    return {"key": token}


# ---------------------------------------------------------------------------
# GET /v1
# ---------------------------------------------------------------------------

def handle_v1_root(handler):
    """Return a basic API info response for GET /v1/."""
    if _require_api_key(handler) is None:
        return
    handler.send_json({
        "object": "list",
        "data": [{"id": "chat", "object": "model"}],
    })


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------

def handle_list_models(handler):
    """Return the list of available models in OpenAI format."""
    if _require_api_key(handler) is None:
        return

    models = []

    # Try to get the live model list from the GPU llama-server
    try:
        r = requests.get(f"{LLAMA_BASE}/v1/models", timeout=5)
        if r.status_code == 200:
            data = r.json()
            for m in data.get("data", []):
                models.append({
                    "id": m.get("id", MODEL_ID or "unknown"),
                    "object": "model",
                    "created": m.get("created", int(time.time())),
                    "owned_by": "local-ai-gpu",
                    "permission": [],
                })
    except Exception:
        pass

    # Fallback: if llama-server didn't return anything, use the configured model ID
    if not models and MODEL_ID:
        models.append({
            "id": MODEL_ID,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "local-ai-gpu",
            "permission": [],
        })

    # Include the CPU model if it differs from the GPU model
    if MODEL_ID_CPU and MODEL_ID_CPU != MODEL_ID:
        models.append({
            "id": MODEL_ID_CPU,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "local-ai-cpu",
            "permission": [],
        })

    handler.send_json({"object": "list", "data": models})


def handle_retrieve_model(handler, model_id):
    """Return a single model's details."""
    if _require_api_key(handler) is None:
        return

    # Check live models first
    try:
        r = requests.get(f"{LLAMA_BASE}/v1/models", timeout=5)
        if r.status_code == 200:
            for m in r.json().get("data", []):
                if m.get("id") == model_id:
                    handler.send_json({
                        "id": m["id"],
                        "object": "model",
                        "created": m.get("created", int(time.time())),
                        "owned_by": "local-ai-gpu",
                        "permission": [],
                    })
                    return
    except Exception:
        pass

    # Check configured models
    if model_id in (MODEL_ID, MODEL_ID_CPU):
        handler.send_json({
            "id": model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "local-ai-gpu" if model_id == MODEL_ID else "local-ai-cpu",
            "permission": [],
        })
        return

    handler.send_json(
        {"error": {"message": f"Model '{model_id}' not found", "type": "invalid_request_error"}},
        status=404,
    )


# ---------------------------------------------------------------------------
# POST /v1/chat/completions
# ---------------------------------------------------------------------------

_API_USER = "_openai_api"

# Dedicated session prefix for API calls so they never collide with browser
# sessions.  Each ``model`` field value maps to its own session so different
# models maintain separate conversation histories.
_api_sessions = {}
_api_sessions_lock = threading.Lock()


def _get_api_session(sid):
    """Return (or create) the internal session list for an API session id."""
    with M._data_lock:
        if sid not in M.sessions:
            M.sessions[sid] = []
            M.sessions_meta[sid] = {
                "name": "API Chat",
                "created": time.time(),
                "updated": time.time(),
                "user_id": _API_USER,
            }
        return M.sessions[sid]


def _messages_to_session(messages):
    """Convert an OpenAI messages array into session-format entries.

    The messages may include ``system``, ``user``, ``assistant`` and ``tool``
    roles.  Images embedded as ``image_url`` content parts are forwarded as-is
    (llama-server supports multimodal when an mmproj is loaded).
    """
    entries = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        entry = {"role": role}

        if role == "tool":
            entry["tool_call_id"] = m.get("tool_call_id", "")
            entry["content"] = content or ""
        elif role == "assistant":
            if content:
                entry["content"] = content
            if m.get("tool_calls"):
                entry["tool_calls"] = m["tool_calls"]
        elif role == "system":
            entry["content"] = content or ""
        else:
            # user message — may be a string or an array of content parts
            entry["content"] = content or ""

        entries.append(entry)
    return entries


def _poll_task(task_id, timeout_s=600, status_callback=None):
    """Block until the task reaches a terminal state, returning the task dict.

    If status_callback is provided, it's called with (status, message) on each change.
    """
    deadline = time.time() + timeout_s
    last_status = None
    while time.time() < deadline:
        with M._data_lock:
            t = M.tasks.get(task_id, {})
        status = t.get("status", "unknown")
        message = t.get("message", "")

        if status != last_status:
            print(f"[poll] Task {task_id} status changed: {last_status} → {status}, message={message}")
            if status_callback:
                try:
                    status_callback(status, message)
                except Exception as e:
                    print(f"[poll] Status callback error: {e}")
            last_status = status

        if status in ("done", "error", "cancelled"):
            return t
        time.sleep(0.3)
    return {"status": "error", "error": "Timeout waiting for response"}


def handle_chat_completions(handler):
    """Submit a chat completion through the existing pipeline.

    The request is routed through the system prompt, tool calling loop, and
    session management just like the browser UI.  The final response is
    returned in OpenAI format.

    Supports ``stream: true`` (SSE) and ``stream: false`` (single JSON).
    Streaming wraps the final response as SSE chunks — intermediate tool
    rounds are executed silently.
    """
    if _require_api_key(handler) is None:
        return

    length = int(handler.headers.get("Content-Length", 0))
    try:
        body = json.loads(handler.rfile.read(length)) if length else {}
    except (json.JSONDecodeError, ValueError) as e:
        handler.send_json(
            {"error": {"message": f"Invalid JSON: {e}", "type": "invalid_request_error"}},
            status=400,
        )
        return

    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        handler.send_json(
            {"error": {"message": "'messages' is required and must be a non-empty array", "type": "invalid_request_error"}},
            status=400,
        )
        return

    stream = body.get("stream", True)
    model = body.get("model") or MODEL_ID or "local-model"

    print(f"[openai_api] Received request: stream={stream}, model={model}, messages={len(messages)}")

    # ── Build the user message for the pipeline ─────────────────────────
    # The last user message becomes the "new" message submitted to the queue.
    # All preceding messages are injected into the session as history so the
    # pipeline (and tools) see the full conversation.
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break

    if last_user_idx is None:
        handler.send_json(
            {"error": {"message": "No user message found in messages array", "type": "invalid_request_error"}},
            status=400,
        )
        return

    # Extract the text content from the last user message (may be multimodal)
    last_user = messages[last_user_idx]
    user_content = last_user.get("content", "")
    user_image = None
    if isinstance(user_content, list):
        # Multimodal: extract text and image
        text_parts = []
        for part in user_content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    img_url = part.get("image_url", {})
                    user_image = img_url.get("url") if isinstance(img_url, dict) else img_url
        user_text = "\n".join(text_parts) if text_parts else str(user_content)
    else:
        user_text = str(user_content)

    # ── Create an API session and populate history ──────────────────────
    session_id = f"api_{uuid.uuid4().hex[:12]}"

    # Create the session entry so the pipeline can find it
    with _api_sessions_lock:
        _api_sessions[session_id] = True

    with M._data_lock:
        M.sessions[session_id] = []
        M.sessions_meta[session_id] = {
            "name": "API Chat",
            "created": time.time(),
            "updated": time.time(),
            "user_id": _API_USER,
        }

    # Inject all preceding messages as conversation history
    history_entries = _messages_to_session(messages[:last_user_idx])
    if history_entries:
        with M._data_lock:
            M.sessions[session_id] = history_entries

    # ── Determine lane mode ─────────────────────────────────────────────
    # API users go to the GPU lane like interactive UI users.
    mode = "gpu"

    # ── Queue the task ──────────────────────────────────────────────────
    task_id = str(uuid.uuid4())
    entry = {
        "task_id": task_id,
        "session_id": session_id,
        "message": user_text,
        "image": user_image,
        "audio": None,
        "user": _API_USER,
        "client_timestamp": None,
        "research": False,
        "cpu": False,
        "no_tools": True,
        "mode": mode,
        "skip_ensure_llama": True,
    }

    with M._data_lock:
        M.tasks[task_id] = {
            "status": "queued",
            "message": "Waiting in line...",
            "session_id": session_id,
            "mode": mode,
            "_user": _API_USER,
        }

    with M._queue_locks[mode]:
        if len(M._task_queues[mode]) >= M.MAX_QUEUE_SIZE:
            handler.send_json(
                {"error": {"message": "Server busy", "type": "server_error"}},
                status=503,
            )
            return
        M._task_queues[mode].append(entry)
        M._queue_conds[mode].notify()

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    # ── For streaming, send headers first so we can stream status updates ─
    if stream:
        print(f"[openai_api] Task {task_id} starting stream (headers sent)")
        # Disable Nagle's algorithm (TCP_NODELAY) to force immediate transmission
        try:
            handler.connection.setsockopt(1, 6, 1)  # IPPROTO_TCP=1, TCP_NODELAY=6
        except Exception as e:
            print(f"[openai_api] Warning: Could not set TCP_NODELAY: {e}")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()

        def stream_status(status, message):
            """Stream status updates as they change."""
            status_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": f"[{message}]"}, "finish_reason": None}],
            }
            try:
                data = f"data: {json.dumps(status_chunk)}\n\n".encode("utf-8")
                # Write directly to socket and force flush
                handler.connection.sendall(data)
                # Force TCP buffer drain: set SO_SNDBUF to 0 temporarily
                import socket as socket_module
                try:
                    orig_sndbuf = handler.connection.getsockopt(socket_module.SOL_SOCKET, socket_module.SO_SNDBUF)
                    handler.connection.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_SNDBUF, 1)
                    handler.connection.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_SNDBUF, orig_sndbuf)
                except Exception:
                    pass
            except Exception as e:
                print(f"[openai_api] Status stream error: {e}")
    else:
        stream_status = None

    # ── Wait for the pipeline to finish ─────────────────────────────────
    print(f"[openai_api] Task {task_id} polling for response...")
    result = _poll_task(task_id, timeout_s=600, status_callback=stream_status)

    status = result.get("status", "error")
    print(f"[openai_api] Task {task_id} completed with status={status}")
    if status == "error":
        err_msg = result.get("error", "Unknown error")
        print(f"[openai_api] Task {task_id} error: {err_msg}")
        handler.send_json(
            {"error": {"message": str(err_msg), "type": "server_error"}},
            status=500,
        )
        return

    response_text = result.get("response", "")
    reasoning = result.get("reasoning", "")
    usage = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if key in result:
            usage[key] = result[key]

    print(f"[openai_api] Task {task_id} response_len={len(response_text)}, response_repr={repr(response_text)}, reasoning_len={len(reasoning)}, has_tools={'_tools_used' in result}")

    # ── Stream or return the response ───────────────────────────────────
    if not stream:
        response_json = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_text,
                },
                "finish_reason": "stop",
            }],
            "usage": usage,
        }
        print(f"[openai_api] Task {task_id} BEFORE send_json, response ready")
        handler.send_json(response_json)
        print(f"[openai_api] Task {task_id} AFTER send_json, response sent")
        return

    # ── Streaming: wrap the final response as SSE chunks ────────────────
    print(f"[openai_api] Task {task_id} sending final response stream, response_text={len(response_text)} chars")

    try:
        import socket as socket_module

        # First chunk: role
        role_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        print(f"[openai_api] Task {task_id} writing role chunk")
        handler.connection.sendall(f"data: {json.dumps(role_chunk)}\n\n".encode("utf-8"))
        # Force flush by toggling SO_SNDBUF
        try:
            orig_sndbuf = handler.connection.getsockopt(socket_module.SOL_SOCKET, socket_module.SO_SNDBUF)
            handler.connection.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_SNDBUF, 1)
            handler.connection.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_SNDBUF, orig_sndbuf)
        except Exception:
            pass

        # Content chunks: split into ~20 char pieces for a smooth stream feel
        chunk_size = 20
        for i in range(0, len(response_text), chunk_size):
            piece = response_text[i:i + chunk_size]
            content_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
            }
            handler.connection.sendall(f"data: {json.dumps(content_chunk)}\n\n".encode("utf-8"))

        # Final chunk
        finish_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": usage,
        }
        print(f"[openai_api] Task {task_id} writing finish chunk")
        handler.connection.sendall(f"data: {json.dumps(finish_chunk)}\n\n".encode("utf-8"))
        handler.connection.sendall(b"data: [DONE]\n\n")
        # Final flush
        try:
            orig_sndbuf = handler.connection.getsockopt(socket_module.SOL_SOCKET, socket_module.SO_SNDBUF)
            handler.connection.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_SNDBUF, 1)
            handler.connection.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_SNDBUF, orig_sndbuf)
        except Exception:
            pass
        print(f"[openai_api] Task {task_id} stream complete")
    except (BrokenPipeError, ConnectionResetError) as e:
        print(f"[openai_api] Task {task_id} stream write error: {e}")
