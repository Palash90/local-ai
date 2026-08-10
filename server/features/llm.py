"""llama-server lifecycle and the streaming LLM round-trip worker."""

import json
import time

import requests

from server.features.state import M


def is_llama_alive():
    try:
        r = requests.get(f"{M.LLAMA_BASE}/health", timeout=5)
        return r.status_code == 200
    except:
        return False


def unload_llama_model():
    with M._model_transition_lock:
        with M._data_lock:
            if M.model_status == "unloaded":
                return True
            M.model_status = "unloading"

        print("[llama] Requesting model unload from VRAM...")
        try:
            r = requests.post(
                f"{M.LLAMA_BASE}/models/unload", json={"model": M.MODEL_ID}, timeout=30
            )
            if r.status_code == 200:
                print("[llama] Model unloaded")
                with M._data_lock:
                    M.model_status = "unloaded"
                return True
            print(f"[llama] Unload response: {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"[llama] Unload error: {e}")

        # Check real status if unload failed or erred out
        with M._data_lock:
            M.model_status = "chat_loaded" if M.is_llama_alive() else "unloaded"
        return False


def load_llama_model():
    with M._data_lock:
        M.model_status = "loading"
    print(f"[llama] Sending load request for model '{M.MODEL_ID}'...")
    try:
        r = requests.post(
            f"{M.LLAMA_BASE}/models/load", json={"model": M.MODEL_ID}, timeout=180
        )
        if r.status_code in (200, 201):
            for i in range(30):
                if M.is_llama_alive():
                    print(f"[llama] Model ready (attempt {i+1})")
                    with M._data_lock:
                        M.model_status = "chat_loaded"
                        M._last_llm_use = time.time()  # Reset idle timer upon loading
                    return True
                time.sleep(2)
        else:
            print(f"[llama] Load failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        print(f"[llama] Load exception: {e}")

    # Fallback check: verify if the server is alive and responding anyway
    if M.is_llama_alive():
        with M._data_lock:
            M.model_status = "chat_loaded"
            M._last_llm_use = time.time()  # Reset idle timer upon loading
        return True

    with M._data_lock:
        M.model_status = "unloaded"
    return False


def _llm_worker(task_id, sid, round_num, msgs):
    try:
        if M.estimate_tokens(msgs) > M.AUTO_COMPACT_THRESHOLD:
            M.set_status(task_id, "Context is full — compressing older messages...")
        messages = M.prepare_context_for_llm(sid, msgs)
        tool_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
        if tool_msgs:
            print(f"[llm_round] Round {round_num} includes {len(tool_msgs)} tool message(s) with search results")  # DEBUG
        payload = {
            "model": M.MODEL_ID,
            "messages": messages,
            "tools": M.TOOLS,
            "tool_choice": "auto",
            "max_tokens": M.MAX_INPUT_TOKENS,
            #"reasoning_budget": REASONING_BUDGET,
            #"reasoning_effort": "medium",
        }
        payload["stream"] = True
        r = requests.post(M.LLAMA_URL, json=payload, stream=True, timeout=600)
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
    M._ensure_llama_mode_for_task(task_id)
    with M._data_lock:
        ms = M.model_status
    if ms != "chat_loaded":
        M.load_llama_model()
    with M._data_lock:
        t = M.tasks.get(task_id)
        if not t:
            return
        t["_state"] = "llm_waiting"
        t["_round"] = round_num
        messages = list(M.sessions.get(sid, []))
    print(f"[llm_round] Starting round {round_num} for task {task_id} with {len(messages)} raw messages")  # DEBUG
    M.set_status(
        task_id, "Thinking..." if round_num == 0 else f"Thinking (round {round_num})..."
    )
    M._llm_pool.submit(M._llm_worker, task_id, sid, round_num, messages)
