"""llama-server lifecycle and the streaming LLM round-trip worker.

Two llama-server processes run concurrently:

* the **GPU** server on ``LLAMA_BASE`` (8081) serving interactive chat UI
  users, and
* the **CPU** server on ``LLAMA_BASE_CPU`` (8079) serving automated self-chat
  agents.

Every function below takes a ``mode`` (``"gpu"`` or ``"cpu"``) so loads,
unloads and completions always hit the right server without ever stopping the
other one.
"""

import json
import time

import requests

from server.features.state import M


def _consult_worker(*args, **kwargs):
    # Implement worker call or redirect to your task handler
    pass


def consult_expert_model(prompt: str, mode: str = "cpu", **kwargs):
    """
    Executes a prompt against the expert/agent model pool.
    """
    from server.features.state import _llm_pools, _human_priority_active
    import time

    # Pause CPU execution if a human user is active
    if mode == "cpu":
        while _human_priority_active():
            time.sleep(1.0)

    # Submit task to the designated LLM thread pool
    pool = _llm_pools.get(mode, _llm_pools["cpu"])
    
    # Add your model invocation / API request logic here
    # future = pool.submit(your_llm_call_function, prompt, **kwargs)
    # return future.result()


def task_mode(task_id):
    """Return the llama-server mode a task must run on.

    Tasks posted by agent users (self-chat: editor, moderator, ...) run on the
    server selected by ``SELF_CHAT_MODE`` (``"cpu"`` or ``"gpu"``); tasks from
    interactive users always use the GPU server.
    """
    with M._data_lock:
        t = M.tasks.get(task_id)
        if not t:
            return "gpu"
        user = t.get("_user", "")
    return M.SELF_CHAT_MODE if user in M._agent_users else "gpu"


def server_base(mode):
    """Base URL of the llama-server for ``mode`` (defaults to the GPU server)."""
    return M.LLAMA_BASE_CPU if mode == "cpu" else M.LLAMA_BASE


def server_url(mode):
    """Chat-completions URL of the llama-server for ``mode``."""
    return M.LLAMA_URL_CPU if mode == "cpu" else M.LLAMA_URL


def server_model_id(mode):
    """Model filename the llama-server for ``mode`` should load."""
    if mode == "cpu":
        return M.MODEL_ID_CPU or M.MODEL_ID
    return M.MODEL_ID


def server_status(mode):
    """Model status of the llama-server for ``mode`` ("unloaded", "loading",
    "chat_loaded", "unloading", ...)."""
    with M._data_lock:
        return M._cpu_model_status if mode == "cpu" else M.model_status


def server_last_use(mode):
    """Idle timestamp of the llama-server for ``mode``."""
    return M._cpu_last_llm_use if mode == "cpu" else M._last_llm_use


def active_model_id(mode="gpu"):
    """Backwards-compatible model filename lookup for ``mode``."""
    return server_model_id(mode)


def is_llama_alive(base=None):
    """True when the llama-server at ``base`` answers /health.

    Defaults to the GPU server so existing callers keep working.
    """
    if base is None:
        base = M.LLAMA_BASE
    try:
        r = requests.get(f"{base}/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def unload_llama_model(mode="gpu"):
    """Unload the model from the llama-server for ``mode``."""
    with M._model_transition_lock:
        with M._data_lock:
            if (M._cpu_model_status if mode == "cpu" else M.model_status) == "unloaded":
                return True
            if mode == "cpu":
                M._cpu_model_status = "unloading"
            else:
                M.model_status = "unloading"

        print(f"[llama] Requesting {mode} model unload from VRAM/RAM...")
        try:
            r = requests.post(
                f"{M.server_base(mode)}/models/unload",
                json={"model": M.server_model_id(mode)},
                timeout=30,
            )
            if r.status_code == 200:
                print(f"[llama] {mode} model unloaded")
                with M._data_lock:
                    if mode == "cpu":
                        M._cpu_model_status = "unloaded"
                    else:
                        M.model_status = "unloaded"
                return True
            print(f"[llama] Unload response: {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"[llama] Unload error: {e}")

        # Check real status if unload failed or erred out
        alive = M.is_llama_alive(M.server_base(mode))
        with M._data_lock:
            if mode == "cpu":
                M._cpu_model_status = "chat_loaded" if alive else "unloaded"
            else:
                M.model_status = "chat_loaded" if alive else "unloaded"
        return False


def load_llama_model(mode="gpu"):
    """Load the model on the llama-server for ``mode`` and wait for it to be
    ready, tracking the per-server model status and idle timestamp."""
    with M._data_lock:
        if mode == "cpu":
            M._cpu_model_status = "loading"
        else:
            M.model_status = "loading"
    model_id = M.server_model_id(mode)
    base = M.server_base(mode)
    print(f"[llama] Sending load request for model '{model_id}' to {base}...")
    try:
        r = requests.post(
            f"{base}/models/load", json={"model": model_id}, timeout=180
        )
        if r.status_code in (200, 201):
            for i in range(30):
                if M.is_llama_alive(base):
                    print(f"[llama] {mode} model ready (attempt {i+1})")
                    with M._data_lock:
                        if mode == "cpu":
                            M._cpu_model_status = "chat_loaded"
                            M._cpu_last_llm_use = time.time()
                        else:
                            M.model_status = "chat_loaded"
                            M._last_llm_use = time.time()  # Reset idle timer upon loading
                    return True
                time.sleep(2)
        else:
            print(f"[llama] Load failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        print(f"[llama] Load exception: {e}")

    # Fallback check: verify if the server is alive and responding anyway
    if M.is_llama_alive(base):
        with M._data_lock:
            if mode == "cpu":
                M._cpu_model_status = "chat_loaded"
                M._cpu_last_llm_use = time.time()
            else:
                M.model_status = "chat_loaded"
                M._last_llm_use = time.time()  # Reset idle timer upon loading
        return True

    with M._data_lock:
        if mode == "cpu":
            M._cpu_model_status = "unloaded"
        else:
            M.model_status = "unloaded"
    return False


def _llm_worker(task_id, sid, round_num, msgs, mode="gpu"):
    try:
        if M.estimate_tokens(msgs) > M.AUTO_COMPACT_THRESHOLD:
            M.set_status(task_id, "Context is full — compressing older messages...")
        messages = M.prepare_context_for_llm(sid, msgs, mode)
        tool_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
        if tool_msgs:
            print(f"[llm_round] Round {round_num} includes {len(tool_msgs)} tool message(s) with search results")  # DEBUG
        payload = {
            "model": M.server_model_id(mode),
            "messages": messages,
            "tools": M.TOOLS,
            "tool_choice": "auto",
            "max_tokens": M.MAX_INPUT_TOKENS,
            #"reasoning_budget": REASONING_BUDGET,
            #"reasoning_effort": "medium",
        }
        payload["stream"] = True
        r = requests.post(M.server_url(mode), json=payload, stream=True, timeout=600)
        if r.status_code != 200:
            err_body = r.text[:500] if r.text else f"HTTP {r.status_code}"
            raise RuntimeError(f"LLM server returned {r.status_code}: {err_body}")
        r.encoding = "utf-8"
        reasoning_buf = ""
        content_buf = ""
        tool_calls_map = {}
        with M._data_lock:
            prev_reasoning = M.tasks.get(task_id, {}).get("reasoning", "")
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            rc = delta.get("reasoning_content")
            if rc:
                reasoning_buf += rc
                with M._data_lock:
                    if task_id in M.tasks:
                        M.tasks[task_id]["reasoning"] = prev_reasoning + reasoning_buf
            c = delta.get("content")
            if c:
                content_buf += c
            tc_list = delta.get("tool_calls")
            if tc_list:
                for tc in tc_list:
                    idx = tc.get("index", 0)
                    if idx not in tool_calls_map:
                        fn = tc.get("function", {})
                        tool_calls_map[idx] = {
                            "index": idx,
                            "id": tc.get("id", ""),
                            "type": tc.get("type", "function"),
                            "function": {
                                "name": fn.get("name", ""),
                                "arguments": fn.get("arguments", ""),
                            },
                        }
                    else:
                        existing = tool_calls_map[idx]
                        if tc.get("id"):
                            existing["id"] = tc["id"]
                        fn = tc.get("function")
                        if fn:
                            if fn.get("name"):
                                existing["function"]["name"] = fn["name"]
                            if fn.get("arguments"):
                                existing["function"]["arguments"] += fn["arguments"]
        print(f"[llm_round] Round {round_num} done: reasoning_buf={len(reasoning_buf)} chars, content_buf={len(content_buf)} chars, tool_calls={len(tool_calls_map)}")  # DEBUG
        msg = {
            "role": "assistant",
            "content": content_buf,
            "reasoning_content": prev_reasoning + reasoning_buf,
        }
        if tool_calls_map:
            msg["tool_calls"] = list(tool_calls_map.values())
        body = {"choices": [{"message": msg}]}
        if "choices" in body:
            M._event_post("llm_ok", task_id, body=body, round=round_num, sid=sid)
        else:
            M._event_post(
                "llm_err",
                task_id,
                error="Unexpected response",
                round=round_num,
                sid=sid,
            )
    except Exception as e:
        err_text = str(e)
        if "image" in err_text.lower() or "vision" in err_text.lower():
            err_text = "The current model does not support image input. Please use a vision-capable model or send text-only messages."
        M._event_post("llm_err", task_id, error=err_text, round=round_num, sid=sid)


def _start_llm_round(task_id, sid, round_num):
    mode = M.task_mode(task_id)
    M.ensure_llama_server(mode)
    with M._data_lock:
        ms = M._cpu_model_status if mode == "cpu" else M.model_status
    if ms != "chat_loaded":
        M.load_llama_model(mode)
    with M._data_lock:
        t = M.tasks.get(task_id)
        if not t:
            return
        t["_state"] = "llm_waiting"
        t["_round"] = round_num
        messages = list(M.sessions.get(sid, []))
    print(f"[llm_round] Starting round {round_num} for task {task_id} on {mode} server with {len(messages)} raw messages")  # DEBUG
    M.set_status(
        task_id, "Thinking..." if round_num == 0 else f"Thinking (round {round_num})..."
    )
    pool = M._llm_pools.get(mode, M._llm_pools["cpu"])
    pool.submit(M._llm_worker, task_id, sid, round_num, messages, mode)
