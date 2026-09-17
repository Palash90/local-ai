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
    OPENAI_SERVER_TOOLS,
)
from server.features.state import M
from server.features.orchestration import _enqueue_ranked, _toolcall_summary
from server.features.openai_adapter import (
    stream_tool_calls,
    format_tool_calls_for_response,
)
from server.features.toolstrip import strip_tool_call_text as _strip_tool_call_text


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


def _poll_task(task_id, timeout_s=3600, status_callback=None, keepalive_interval=10):
    """Block until the task reaches a terminal state, returning the task dict.

    If status_callback is provided, it's called with (status, message) whenever
    the status changes AND at least every ``keepalive_interval`` seconds even if
    it hasn't — OpenAI-compatible HTTP clients (e.g. Node's undici, used by
    several VS Code extensions) abort a request if no bytes arrive for ~300s,
    so during long model-load / queueing / reasoning pauses we still need to
    push *something* down the wire to keep the connection alive.
    """
    deadline = time.time() + timeout_s
    last_status = None
    last_ping = 0.0
    while time.time() < deadline:
        with M._data_lock:
            t = M.tasks.get(task_id, {})
        status = t.get("status", "unknown")
        message = t.get("message", "")

        now = time.time()
        if status != last_status or (now - last_ping) >= keepalive_interval:
            if status != last_status:
                print(f"[poll] Task {task_id} status changed: {last_status} → {status}, message={message}")
            if status_callback:
                try:
                    status_callback(status, message)
                except Exception as e:
                    print(f"[poll] Status callback error: {e}")
            last_ping = now
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

    # ── Client-supplied tools (pass-through) ────────────────────────
    # OpenAI clients (e.g. opencode) send their own `tools` and expect
    # structured `tool_calls` back, which they execute themselves. Honor
    # them: without this the model sees tool context in the conversation
    # history but has no structured tool channel, so tool-trained models
    # leak `<|tool_call|>` markup as plain text instead of replying.
    client_tools = body.get("tools") or []
    if not isinstance(client_tools, list):
        handler.send_json(
            {"error": {"message": "'tools' must be an array", "type": "invalid_request_error"}},
            status=400,
        )
        return
    for t in client_tools:
        fn = (t.get("function") if isinstance(t, dict) else None) or {}
        if not isinstance(t, dict) or t.get("type") != "function" or not fn.get("name"):
            handler.send_json(
                {"error": {"message": "Each entry in 'tools' must have type 'function' and a function.name", "type": "invalid_request_error"}},
                status=400,
            )
            return
    client_tool_choice = body.get("tool_choice", "auto" if client_tools else "none")
    if client_tools:
        print(f"[openai_api] Passing through {len(client_tools)} client tool(s), tool_choice={client_tool_choice}")

    # ── Hybrid lane: server-executed tools ──────────────────────────
    # Local-ai's own search/fetch tools ride along so API clients get
    # UI-lane behavior. Default from OPENAI_SERVER_TOOLS env (auto);
    # per-request `server_tools` overrides: false/never disables,
    # true/always merges, "only" drops client tools entirely.
    _st_raw = body.get("server_tools", None)
    if _st_raw is None:
        server_tool_mode = (OPENAI_SERVER_TOOLS or "auto").strip().lower()
    elif _st_raw is True or (isinstance(_st_raw, str) and _st_raw.strip().lower() in ("always", "true", "1", "yes", "on")):
        server_tool_mode = "always"
    elif isinstance(_st_raw, str) and _st_raw.strip().lower() == "only":
        server_tool_mode = "only"
    else:
        server_tool_mode = "never"
    if server_tool_mode not in ("auto", "always", "only", "never"):
        server_tool_mode = "auto"
    server_tools_only = (server_tool_mode == "only")
    if server_tools_only:
        client_tools = []
        client_tool_choice = "none"
    if server_tool_mode in ("auto", "always", "only"):
        print(f"[openai_api] server tools mode={server_tool_mode} (search+fetch will merge on wire)")

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

    # Inject all messages except the last user message as conversation
    # history, preserving order. Messages after the last user (the normal
    # OpenAI tool loop: assistant(tool_calls) -> tool(result)) must be kept
    # — dropping that tail discards every tool result and the model re-asks
    # forever. The last user message itself is submitted as the new prompt
    # via _prepare_session, so it is excluded here to avoid duplication.
    history_entries = _messages_to_session(
        messages[:last_user_idx] + messages[last_user_idx + 1:]
    )
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
        "no_tools": not client_tools and server_tool_mode == "never",
        "client_tools": client_tools,
        "client_tool_choice": client_tool_choice,
        "server_tool_mode": server_tool_mode,
        "openai_lane": True,
        "mode": mode,
        "skip_ensure_llama": True,
    }

    with M._data_lock:
        M.tasks[task_id] = {
            "status": "queued",
            "message": "Waiting in line...",
            "session_id": session_id,
            "_queued_at": time.time(),
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
        _enqueue_ranked(M._task_queues[mode], entry)
        M._queue_conds[mode].notify()

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    # ── Streaming: send headers immediately, then keep the connection alive
    # with periodic SSE pings while the task is queued/processing. Without
    # this, OpenAI-compatible clients (VS Code extensions built on Node's
    # undici, which aborts a request after ~300s of silence) give up long
    # before a queued or slow (model-load, long reasoning) response is ready
    # — even though our server eventually finishes fine.
    if stream:
        print(f"[openai_api] Task {task_id} starting SSE stream")
        handler.close_connection = True
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()

        def write_sse(raw_line):
            """Write one raw SSE line/frame; return False if the client is gone."""
            try:
                handler.wfile.write(raw_line.encode("utf-8"))
                handler.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                print(f"[openai_api] Task {task_id} client disconnected: {e}")
                return False

        # Send the role chunk immediately so clients register the stream as started.
        write_sse(f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n")

        def status_ping(status, message):
            if status == "done":
                return
            # SSE comment line (":" prefix) — ignored by JSON/data parsers but
            # still counts as "bytes received" to reset the client's idle timer.
            write_sse(f": {status} — {message}\n\n")

        result = _poll_task(task_id, timeout_s=3600, status_callback=status_ping)
    else:
        result = _poll_task(task_id, timeout_s=3600)

    status = result.get("status", "error")
    print(f"[openai_api] Task {task_id} completed with status={status}")

    if status == "error":
        err_msg = result.get("error", "Unknown error")
        print(f"[openai_api] Task {task_id} error: {err_msg}")
        if stream:
            write_sse(f"data: {json.dumps({'error': {'message': str(err_msg), 'type': 'server_error'}})}\n\n")
            write_sse("data: [DONE]\n\n")
        else:
            handler.send_json(
                {"error": {"message": str(err_msg), "type": "server_error"}},
                status=500,
            )
        return

    response_text = _strip_tool_call_text(result.get("response", ""))
    usage = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if key in result:
            usage[key] = result[key]

    tool_calls = result.get("tool_calls")
    sid = result.get("session_id")
    if not tool_calls and sid:
        with M._data_lock:
            session = M.sessions.get(sid, [])
            if session:
                for msg in reversed(session):
                    if msg.get("role") == "assistant" and msg.get("tool_calls"):
                        tool_calls = msg.get("tool_calls")
                        break
    if tool_calls:
        # Hybrid lane: server-executed calls (web_search/fetch_page/...)
        # already ran in-lane — never hand them back for the client to
        # re-execute. Filter to client-named calls only.
        try:
            from server.config import OPENAI_LANE_SERVER_TOOL_NAMES as _SRV_NAMES
        except Exception:
            _SRV_NAMES = {"web_search", "fetch_page", "tool_details"}
        tool_calls = [tc for tc in tool_calls
                      if ((tc.get("function") or {}).get("name") not in _SRV_NAMES)]
        if not tool_calls:
            tool_calls = None

    print(f"[openai_api] Task {task_id} response_len={len(response_text)}, tool_calls={len(tool_calls) if tool_calls else 0}" + (f": {_toolcall_summary(tool_calls)}" if tool_calls else ""))

    if not stream:
        message = {"role": "assistant"}
        if response_text:
            message["content"] = response_text
        if tool_calls:
            message["tool_calls"] = format_tool_calls_for_response(tool_calls)
        handler.send_json({
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }],
            "usage": usage,
        })
        return

    if tool_calls:
        if not stream_tool_calls(write_sse, tool_calls, completion_id, created, model):
            return
    else:
        chunk_size = 20
        for i in range(0, len(response_text), chunk_size):
            piece = response_text[i:i + chunk_size]
            if not write_sse(f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {'content': piece}, 'finish_reason': None}]})}\n\n"):
                return

    if not tool_calls:
        write_sse(f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': usage})}\n\n")
    
    write_sse("data: [DONE]\n\n")
    print(f"[openai_api] Task {task_id} stream complete")
