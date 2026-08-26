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
import os
import time

import requests

from server.features.state import M


# Rotating KV-checkpoint filename per lane (slot 0 is the only slot — both
# servers run with the default --parallel 1). Kept constant so each
# save overwrites the previous snapshot instead of filling the disk.
_SLOT_CHECKPOINT_FILES = {"gpu": "gpu_slot0.kv", "cpu": "cpu_slot0.kv", "guardrail": "guardrail_slot0.kv"}


def slot_checkpoint_file(mode="gpu"):
    """Checkpoint filename (relative to ``LLAMA_SLOT_SAVE_DIR``) for ``mode``."""
    return _SLOT_CHECKPOINT_FILES.get(mode, _SLOT_CHECKPOINT_FILES["gpu"])


def slot_checkpoint_path(mode="gpu"):
    """Absolute path of the KV-checkpoint file for ``mode``."""
    return os.path.join(M.LLAMA_SLOT_SAVE_DIR, slot_checkpoint_file(mode))


def mark_slot_kv_dirty(mode="gpu"):
    """Flag the lane's slot KV as changed by an outgoing completion.

    Called right before any request is sent to ``mode``'s chat-completions
    endpoint — processing a prompt always mutates the server's slot KV. The
    flag decides whether :func:`save_slot_checkpoint` snapshots again on the
    next unload or the on-disk snapshot is already up to date."""
    with M._data_lock:
        M._slot_kv_dirty[mode] = True


def save_slot_checkpoint(mode="gpu"):
    """Snapshot the llama-server's current KV cache to disk.

    Calls ``POST /slots/0?action=save`` so the KV of everything processed so
    far survives an imminent model unload. Skipped when the model is not
    loaded or no completion has run since the last save/restore (the on-disk
    snapshot is already current). Failures (slot busy, media tokens in the
    slot, feature unavailable) never block the caller — they just mean the
    next reload re-prefills from scratch, exactly like before this
    optimization existed.
    """
    with M._data_lock:
        if mode == "cpu":
            ms = M._cpu_model_status
        elif mode == "guardrail":
            ms = M._guardrail_model_status
        else:
            ms = M.model_status
        dirty = M._slot_kv_dirty.get(mode, False)
        cp = M._slot_checkpoints.get(mode)
    if ms != "chat_loaded":
        return False
    if not dirty and cp:
        return True  # snapshot on disk already reflects the current KV

    filename = slot_checkpoint_file(mode)
    try:
        r = requests.post(
            f"{M.server_base(mode)}/slots/0?action=save",
            # "model" is required in router mode: the parent picks the child
            # instance to proxy to from this field (the child itself ignores it).
            json={"filename": filename, "model": M.server_model_id(mode)},
            timeout=180,
        )
        if r.status_code == 200:
            n_tokens = r.json().get("n_tokens", 0)
            with M._data_lock:
                M._slot_checkpoints[mode] = {
                    "file": filename,
                    "model": M.server_model_id(mode),
                    "ts": time.time(),
                    "n_tokens": n_tokens,
                }
                M._slot_kv_dirty[mode] = False
            print(
                f"[llama] {mode} slot KV checkpointed ({n_tokens} tokens) -> {filename}"
            )
            return True
        print(
            f"[llama] {mode} slot KV save failed ({r.status_code}): {r.text[:200]}"
        )
    except Exception as e:
        print(f"[llama] {mode} slot KV save error: {e}")
    return False


def restore_slot_checkpoint(mode="gpu"):
    """Restore the previously saved KV cache into slot 0 of ``mode``'s server.

    Called right after a model load. The restored prefix is only a *cache*:
    the next completion still verifies its prompt against the restored tokens
    and evaluates whatever is new, so a stale snapshot costs time but can
    never produce wrong output.
    """
    with M._data_lock:
        cp = dict(M._slot_checkpoints.get(mode) or {})
    if not cp:
        return False
    filename = cp.get("file") or slot_checkpoint_file(mode)
    if not os.path.exists(os.path.join(M.LLAMA_SLOT_SAVE_DIR, filename)):
        with M._data_lock:
            M._slot_checkpoints.pop(mode, None)
        return False
    if cp.get("model") != M.server_model_id(mode):
        # Snapshot belongs to another model — restoring it would fail (or worse,
        # misload state), drop it silently.
        print(
            f"[llama] Dropping stale {mode} KV checkpoint "
            f"(saved for '{cp.get('model')}', now '{M.server_model_id(mode)}')"
        )
        with M._data_lock:
            M._slot_checkpoints.pop(mode, None)
        return False

    try:
        r = requests.post(
            f"{M.server_base(mode)}/slots/0?action=restore",
            # See save_slot_checkpoint: router mode routes by body "model".
            json={"filename": filename, "model": M.server_model_id(mode)},
            timeout=180,
        )
        if r.status_code == 200:
            n_tokens = r.json().get("n_tokens", cp.get("n_tokens", 0))
            with M._data_lock:
                # The slot now holds exactly the snapshot's KV again.
                M._slot_kv_dirty[mode] = False
            print(
                f"[llama] {mode} slot KV restored from checkpoint ({n_tokens} tokens)"
            )
            return True
        # Unusable snapshot — clear it so we don't retry every load.
        print(
            f"[llama] {mode} slot KV restore failed ({r.status_code}): {r.text[:200]}"
        )
    except Exception as e:
        print(f"[llama] {mode} slot KV restore error: {e}")
    with M._data_lock:
        M._slot_checkpoints.pop(mode, None)
    return False


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
    if mode in ("cpu", "guardrail"):
        while _human_priority_active():
            time.sleep(1.0)

    # Submit task to the designated LLM thread pool
    pool = _llm_pools.get(mode, _llm_pools["cpu"])
    
    # Add your model invocation / API request logic here
    # future = pool.submit(your_llm_call_function, prompt, **kwargs)
    # return future.result()


def task_mode(task_id):
    with M._data_lock:
        t = M.tasks.get(task_id)
        if not t:
            return "gpu"
        user = t.get("_user", "")
        mode = t.get("mode")
        cpu_flagged = bool(t.get("cpu"))
    if cpu_flagged:
        return "cpu"
    if mode == "guardrail":
        return "guardrail"
    if M.FORCE_GPU_LANE:
        return "gpu"
    if user in M._agent_users and mode in ("gpu", "cpu"):
        return mode
    return M.SELF_CHAT_MODE if user in M._agent_users else "gpu"


def server_base(mode):
    if mode == "cpu":
        return M.LLAMA_BASE_CPU
    if mode == "guardrail":
        return M.LLAMA_BASE_GUARDRAIL
    return M.LLAMA_BASE


def server_url(mode):
    if mode == "cpu":
        return M.LLAMA_URL_CPU
    if mode == "guardrail":
        return M.LLAMA_URL_GUARDRAIL
    return M.LLAMA_URL


def server_model_id(mode):
    if mode == "cpu":
        return M.MODEL_ID_CPU or M.MODEL_ID
    if mode == "guardrail":
        return M.MODEL_ID_GUARDRAIL
    return M.MODEL_ID


def server_status(mode):
    with M._data_lock:
        if mode == "cpu":
            return M._cpu_model_status
        if mode == "guardrail":
            return M._guardrail_model_status
        return M.model_status


def server_last_use(mode):
    if mode == "cpu":
        return M._cpu_last_llm_use
    if mode == "guardrail":
        return M._guardrail_last_llm_use
    return M._last_llm_use


def active_model_id(mode="gpu"):
    """Backwards-compatible model filename lookup for ``mode``."""
    return server_model_id(mode)


def is_llama_alive(base=None):
    """True when the llama-server at ``base`` answers /health.

    Defaults to the GPU server so existing callers keep working. In router
    mode /health only reports that the router process is up, not that any
    particular model is actually loaded and serving — use
    :func:`is_model_ready` to check a specific model's state.
    """
    if base is None:
        base = M.LLAMA_BASE
    try:
        r = requests.get(f"{base}/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def is_model_ready(base, model_id):
    """True when ``model_id`` is actually loaded and serving on the router at
    ``base`` (``GET /models`` -> ``status.value == "ready"`` for that id).

    /health only proves the router itself is up; a child instance can fail to
    load (OOM, bad args, ...) and exit while the router stays healthy, which
    previously made load_llama_model report success for a model that was
    never actually ready.
    """
    try:
        r = requests.get(f"{base}/models", timeout=5)
        if r.status_code != 200:
            return False
        for m in r.json().get("data", []):
            if m.get("id") == model_id:
                return m.get("status", {}).get("value") == "ready"
    except Exception:
        pass
    return False


def unload_llama_model(mode="gpu"):
    """Unload the model from the llama-server for ``mode``."""
    with M._model_transition_lock:
        if M.server_status(mode) == "unloaded":
            return True

        print(f"[llama] Requesting {mode} model unload from VRAM/RAM...")
        # Checkpoint the KV cache BEFORE it is destroyed by the unload, so the
        # post-image-gen (or post-idle) reload can restore it instead of
        # re-prefilling the whole conversation. This must happen while the
        # model still reads "chat_loaded" — save_slot_checkpoint skips
        # anything else — hence before the "unloading" transition below.
        
        with M._data_lock:
            if mode == "cpu":
                M._cpu_model_status = "unloading"
            elif mode == "guardrail":
                M._guardrail_model_status = "unloading"
            else:
                M.model_status = "unloading"

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
                    elif mode == "guardrail":
                        M._guardrail_model_status = "unloaded"
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
            elif mode == "guardrail":
                M._guardrail_model_status = "chat_loaded" if alive else "unloaded"
            else:
                M.model_status = "chat_loaded" if alive else "unloaded"
        return False


def load_llama_model(mode="gpu"):
    """Load the model on the llama-server for ``mode`` and wait for it to be
    ready, tracking the per-server model status and idle timestamp.

    When the load follows an unload (image generation, idle release), the KV
    checkpoint saved by :func:`save_slot_checkpoint` is restored so the next
    completion only has to evaluate new tokens."""
    with M._data_lock:
        # Only a fresh load benefits from a restore: if the model is already
        # running, its live KV is newer than any snapshot on disk.
        # Read status directly — server_status() would re-acquire _data_lock
        # (non-reentrant), causing a permanent deadlock.
        if mode == "cpu":
            was_unloaded = M._cpu_model_status == "unloaded"
            M._cpu_model_status = "loading"
        elif mode == "guardrail":
            was_unloaded = M._guardrail_model_status == "unloaded"
            M._guardrail_model_status = "loading"
        else:
            was_unloaded = M.model_status == "unloaded"
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
                if M.is_model_ready(base, model_id):
                    print(f"[llama] {mode} model ready (attempt {i+1})")
                    with M._data_lock:
                        if mode == "cpu":
                            M._cpu_model_status = "chat_loaded"
                            M._cpu_last_llm_use = time.time()
                        elif mode == "guardrail":
                            M._guardrail_model_status = "chat_loaded"
                            M._guardrail_last_llm_use = time.time()
                        else:
                            M.model_status = "chat_loaded"
                            M._last_llm_use = time.time()
                    if was_unloaded:
                        M.restore_slot_checkpoint(mode)
                    return True
                time.sleep(2)
        else:
            print(f"[llama] Load failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        print(f"[llama] Load exception: {e}")

    # Fallback check: the load request may have failed with "already running"
    # (another caller raced us to load the same model) — verify the model is
    # actually ready rather than just assuming so.
    if M.is_model_ready(base, model_id):
        with M._data_lock:
            if mode == "cpu":
                M._cpu_model_status = "chat_loaded"
                M._cpu_last_llm_use = time.time()
            elif mode == "guardrail":
                M._guardrail_model_status = "chat_loaded"
                M._guardrail_last_llm_use = time.time()
            else:
                M.model_status = "chat_loaded"
                M._last_llm_use = time.time()
        if was_unloaded:
            M.restore_slot_checkpoint(mode)
        return True

    with M._data_lock:
        if mode == "cpu":
            M._cpu_model_status = "unloaded"
        elif mode == "guardrail":
            M._guardrail_model_status = "unloaded"
        else:
            M.model_status = "unloaded"
    return False


def _inject_read_image(messages):
    """Attach the bytes of the most recent ``read_image`` result to its tool
    message so the model can actually see the image this round.

    The stored tool result stays a tiny JSON blob (url only); the image bytes
    are embedded only in this round's payload. A copy is returned so the
    stored session is never mutated.
    """
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        m = out[i]
        if m.get("role") != "tool":
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            continue
        url = data.get("image_url") if data.get("ok") is True else None
        if not url:
            continue
        data_url = M._image_to_data_url(url)
        if not data_url:
            continue
        out[i] = {
            **m,
            "content": [
                {"type": "text", "text": f"[Image loaded from {url}]"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
        break
    return out


def _last_user_text(messages):
    """Return the text of the most recent user message (multimodal-safe)."""
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = [
                p.get("text", "")
                for p in c
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            if parts:
                return "\n".join(parts)
    return ""


def _route_sampling(mode, messages):
    """Classify the latest user message and return sampling overrides.

    One tiny greedy call against the same llama-server that will serve the
    real request. Returns {} (= server defaults) on any failure — the router
    can never block generation.
    """
    text = _last_user_text(messages)
    if not text.strip():
        return {}
    try:
        r = requests.post(
            M.server_url(mode),
            json={
                "model": M.server_model_id(mode),
                "messages": [
                    {"role": "system", "content": M.SAMPLING_ROUTER_PROMPT},
                    {"role": "user", "content": text[:4000]},
                ],
                "max_tokens": M.SAMPLING_ROUTER_MAX_TOKENS,
                "temperature": 0.0,
                "top_k": 1,
                "stream": False,
            },
            timeout=M.SAMPLING_ROUTER_TIMEOUT,
        )
        label = (
            r.json()["choices"][0]["message"]["content"].strip().lower()
        )
    except Exception as e:
        print(f"[sampling-router] failed ({e}); using server defaults")
        return {}
    for bucket, params in M.SAMPLING_BUCKETS.items():
        if bucket in label:
            print(f"[sampling-router] {mode}: '{label.strip()}' → {bucket} {params}")
            return dict(params)
    print(f"[sampling-router] unrecognised label '{label}'; using server defaults")
    return {}


def _llm_worker(task_id, sid, round_num, msgs, mode="gpu"):
    try:
        if M.estimate_tokens(msgs) > M.AUTO_COMPACT_THRESHOLD:
            M.set_status(task_id, "Context is full — compressing older messages...")
        messages = M.prepare_context_for_llm(sid, msgs, mode)
        messages = _inject_read_image(messages)
        tool_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
        if tool_msgs:
            print(f"[llm_round] Round {round_num} includes {len(tool_msgs)} tool message(s) with search results")  # DEBUG
        with M._data_lock:
            task_user = M.tasks.get(task_id, {}).get("_user", "")
            task_no_tools = M.tasks.get(task_id, {}).get("no_tools", False)
        tool_free = task_user in M.TOOL_FREE_AGENTS or task_no_tools
        # Agents (Kaya/Kolpo pipeline) get the full tool set; humans never see
        # AGENT_ONLY_TOOLS (track_theme), saving its tokens on every turn.
        if task_user in M._agent_users:
            wire_tools = M.TOOLS
        else:
            wire_tools = M.TOOLS_HUMAN
        # Sampling router: classify once on round 0, reuse for all rounds of
        # this task (stored under an underscore key so it stays private).
        with M._data_lock:
            sampling = M.tasks.get(task_id, {}).get("_sampling")
        if sampling is None:
            sampling = _route_sampling(mode, messages) if round_num == 0 else {}
            with M._data_lock:
                if task_id in M.tasks:
                    M.tasks[task_id]["_sampling"] = sampling
        payload = {
            "model": M.server_model_id(mode),
            "messages": messages,
            "tools": [] if tool_free else wire_tools,
            "tool_choice": "none" if tool_free else "auto",
            "max_tokens": M.MAX_OUTPUT_TOKENS,
            "reasoning_budget_tokens": M.REASONING_BUDGET,
        }
        payload.update(sampling or {})
        payload["stream"] = True
        M.mark_slot_kv_dirty(mode)
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
    with M._data_lock:
        task = M.tasks.get(task_id, {})
    if not task.get("skip_ensure_llama"):
        M.ensure_llama_server(mode)
    with M._data_lock:
        if mode == "cpu":
            ms = M._cpu_model_status
        elif mode == "guardrail":
            ms = M._guardrail_model_status
        else:
            ms = M.model_status
    if ms != "chat_loaded":
        M.load_llama_model(mode)
    with M._data_lock:
        t = M.tasks.get(task_id)
        if not t:
            return
        t["_state"] = "llm_waiting"
        t["_round"] = round_num
        messages = list(M.sessions.get(sid, []))
    print(f"[llm_round] Starting round {round_num} for task {task_id} on {mode} server with {len(messages)} raw messages")
    M.set_status(
        task_id, "Thinking..." if round_num == 0 else f"Thinking (round {round_num})..."
    )
    pool = M._llm_pools.get(mode, M._llm_pools["cpu"])
    pool.submit(M._llm_worker, task_id, sid, round_num, messages, mode)
