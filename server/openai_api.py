"""OpenAI-compatible API endpoints.

Provides ``/v1/chat/completions`` and ``/v1/models`` on the same port as the
chat UI (3001).  Authentication uses a static ``OPENAI_API_KEY`` from the
environment, sent as ``Authorization: Bearer <key>``.

The handler functions receive the ``Handler`` instance from
:class:`server.api.Handler` and use its ``send_json``, ``_safe_write``,
``send_response``, ``send_header``, ``end_headers`` helpers for output.
"""

import hmac
import json
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
    if not hmac.compare_digest(token, OPENAI_API_KEY):
        handler.send_json(
            {"error": {"message": "Invalid API key", "type": "invalid_request_error"}},
            status=401,
        )
        return None
    return {"key": token}


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

def handle_chat_completions(handler):
    """Proxy a chat completion request to llama-server with full SSE streaming.

    Supports:
    * ``stream: true`` (default) — SSE delta chunks matching the OpenAI spec.
    * ``stream: false`` — single JSON response.
    * Vision messages (``image_url`` content parts) forwarded to multimodal
      llama-server builds.
    * Tool calling responses forwarded as-is (llama-server emits OpenAI-
      compatible ``tool_calls`` deltas).
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

    # Build the llama-server payload.  We forward most fields directly so that
    # llama-server features (temperature, top_p, tools, etc.) work unchanged.
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    # Forward optional OpenAI parameters that llama-server understands
    for key in (
        "temperature", "top_p", "top_k", "min_p", "max_tokens",
        "stop", "tools", "tool_choice", "frequency_penalty",
        "presence_penalty", "seed", "logprobs", "top_logprobs",
        "reasoning_budget",
    ):
        if key in body:
            payload[key] = body[key]

    # Use the GPU server by default
    target_base = LLAMA_BASE

    # If the requested model matches the CPU model, route there
    if MODEL_ID_CPU and model == MODEL_ID_CPU:
        target_base = LLAMA_BASE_CPU

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    try:
        r = requests.post(
            f"{target_base}/v1/chat/completions",
            json=payload,
            stream=True,
            timeout=600,
        )
    except requests.ConnectionError:
        handler.send_json(
            {"error": {"message": "llama-server is not running", "type": "server_error"}},
            status=503,
        )
        return
    except requests.Timeout:
        handler.send_json(
            {"error": {"message": "llama-server timed out", "type": "server_error"}},
            status=504,
        )
        return

    if r.status_code != 200:
        err_text = r.text[:500] if r.text else f"HTTP {r.status_code}"
        handler.send_json(
            {"error": {"message": f"llama-server error: {err_text}", "type": "server_error"}},
            status=r.status_code,
        )
        return

    if not stream:
        # ── Non-streaming response ──────────────────────────────────────
        try:
            data = r.json()
        except Exception:
            handler.send_json(
                {"error": {"message": "Invalid response from llama-server", "type": "server_error"}},
                status=500,
            )
            return
        # Ensure OpenAI-compatible envelope
        data.setdefault("id", completion_id)
        data.setdefault("object", "chat.completion")
        data.setdefault("created", created)
        data.setdefault("model", model)
        # Normalize choices if llama-server returned a slightly different shape
        for choice in data.get("choices", []):
            choice.setdefault("index", 0)
            choice.setdefault("finish_reason", "stop")
        handler.send_json(data)
        return

    # ── Streaming SSE response ───────────────────────────────────────────
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()

    r.encoding = "utf-8"
    try:
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            # llama-server sends "data: {...}" or "data: [DONE]"
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    handler._safe_write(b"data: [DONE]\n\n")
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                # Ensure required envelope fields
                chunk.setdefault("id", completion_id)
                chunk.setdefault("object", "chat.completion.chunk")
                chunk.setdefault("created", created)
                chunk.setdefault("model", model)
                # Ensure choices array has index
                for choice in chunk.get("choices", []):
                    choice.setdefault("index", 0)
                wire = f"data: {json.dumps(chunk)}\n\n"
                handler._safe_write(wire.encode("utf-8"))
            else:
                # Pass through any non-data lines (e.g. comments) unchanged
                handler._safe_write((line + "\n\n").encode("utf-8"))
    except (BrokenPipeError, ConnectionResetError):
        # Client disconnected — stop streaming
        pass
    finally:
        r.close()
