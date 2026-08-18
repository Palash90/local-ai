"""The event loop, task queue and task-state helpers that drive a chat request."""

import json
import os
import time

from server.features.state import M


def set_status(task_id, message):
    with M._data_lock:
        if task_id in M.tasks and M.tasks[task_id].get("status") != "cancelled":
            M.tasks[task_id]["status"] = "working"
            M.tasks[task_id]["message"] = message


def location_str():
    if M._client_location:
        return M._client_location
    return None


def set_client_location(value):
    M._client_location = value


def _task_user(task_id):
    with M._data_lock:
        return M.tasks.get(task_id, {}).get("_user", "")


def _task_max_rounds(task_id):
    """Tool-loop round budget for a task: 10 for normal chats, 50 when the
    UI's research toggle is on (stored on the task as ``research``)."""
    with M._data_lock:
        t = M.tasks.get(task_id, {})
        if t.get("research"):
            return M.MAX_TOOL_ROUNDS.get("research", 50)
    return M.MAX_TOOL_ROUNDS.get("default", 10)


def _set_task_error(task_id, error, sid=None):
    with M._data_lock:
        if task_id in M.tasks:
            d = M.tasks[task_id]
            elapsed_ms = None
            if d.get("_started_at") is not None:
                elapsed_ms = int((time.time() - d.get("_started_at")) * 1000)
            M.tasks[task_id] = {
                "status": "error",
                "error": str(error),
                "session_id": d.get("session_id", sid),
                "_elapsed_ms": elapsed_ms,
            }


def _delete_task_image(task_id):
    """Remove the generated image file attached to a (cancelled) task, if any."""
    with M._data_lock:
        t = M.tasks.get(task_id)
        if not t:
            return
        fname = t.get("image_file")
    if not fname:
        return
    fpath = fname if os.path.isabs(fname) else os.path.join(M.IMG_PATH, fname)
    try:
        if os.path.exists(fpath):
            os.remove(fpath)
            print(f"[cancel] Removed image for cancelled task {task_id}: {fpath}")
    except OSError:
        pass


def _finalize_task(task_id, sid, msg_content, body):
    with M._data_lock:
        t = M.tasks.get(task_id)
        if not t:
            return
        tools_used = list(t.get("_tools_used", []))
        search_details = list(t.get("_search_details", []))
        image_filename = t.get("image_file")
        gen_prompt = t.get("gen_prompt")
        image_model = t.get("_image_model")
        verification = t.get("_verification")
        verification_duration = t.get("_verification_duration")
    image_url = f"/output/{image_filename}" if image_filename else None
    if image_url:
        print(f"[finalize] image_file='{image_filename}' → image_url='{image_url}' for task {task_id}")  # DEBUG
    timings = body.get("timings", {})
    predicted_per_second = timings.get("predicted_per_second")
    with M._data_lock:
        started_at = t.get("_started_at")
    elapsed_ms = None
    if started_at is not None:
        elapsed_ms = int((time.time() - started_at) * 1000)
    reasoning = (
        body.get("choices", [{}])[0].get("message", {}).get("reasoning_content", "")
    )
    if not msg_content and reasoning:
        msg_content = "(No response content generated)"
    msg_entry = {
        "role": "assistant",
        "content": msg_content,
        "_reasoning": reasoning,
        "_tools_used": tools_used,
        "_image_url": image_url,
        "_gen_prompt": gen_prompt,
        "_image_model": image_model,
        "_search_details": search_details,
        "_research": bool(t.get("research")),
        "_elapsed_ms": elapsed_ms,
    }
    if verification is not None:
        msg_entry["_verification"] = verification
        msg_entry["_verification_duration"] = verification_duration
    mode = M.task_mode(task_id)
    with M._data_lock:
        if sid in M.sessions:
            M.sessions[sid].append(msg_entry)
            M.sessions_meta.setdefault(sid, {})["updated"] = time.time()
        if mode == "gpu":
            M._last_tps = predicted_per_second
            M._last_llm_use = time.time()  # Reset GPU idle timer when task finishes
        else:
            M._cpu_last_llm_use = time.time()  # Reset CPU idle timer when task finishes
    M.save_sessions()
    with M._data_lock:
        if task_id in M.tasks:
            M.tasks[task_id] = {
                "status": "done",
                "response": msg_content,
                "session_id": sid,
                "session_name": M.sessions_meta.get(sid, {}).get("name", ""),
                **M.context_token_report(sid, M.sessions.get(sid, [])),
                "predicted_per_second": predicted_per_second,
                "tools_used": tools_used,
                "image": image_url,
                "_image_url": image_url,
                "gen_prompt": gen_prompt,
                "_image_model": image_model,
                "_search_details": search_details,
                "reasoning": reasoning,
                "_elapsed_ms": elapsed_ms,
            }
            if verification is not None:
                M.tasks[task_id]["_verification"] = verification
                M.tasks[task_id]["_verification_duration"] = verification_duration


def _event_post(ev_type, task_id, **data):
    M._event_queue.put((ev_type, task_id, data))


def _event_loop():
    while True:
        ev_type, task_id, data = M._event_queue.get()
        t = M.tasks.get(task_id)
        if not t:
            continue
        if t.get("status") == "cancelled":
            M._delete_task_image(task_id)
            continue

        if ev_type == "start":
            sid = data["sid"]
            user_message = data["message"]
            image_b64 = data.get("image")
            audio_b64 = data.get("audio")
            user = data.get("user", "")
            client_ts = data.get("client_timestamp")
            with M._data_lock:
                M.tasks[task_id] = {
                    "status": "working",
                    "message": "Processing task...",
                    "session_id": sid,
                    "_tools_used": [],
                    "_search_details": [],
                    "_original_message": user_message,
                    "_original_image": image_b64,
                    "_audio": audio_b64,
                    "_user": user,
                    "_client_timestamp": client_ts,
                    "mode": t.get("mode"),
                    "research": bool(data.get("research")),
                    "cpu": bool(data.get("cpu")),
                    "no_tools": bool(data.get("no_tools")),
                    "_started_at": t.get("_started_at"),
                }
            # (The owning lane's _current_task_ids[mode] was already set by
            # _queue_worker before this "start" event was posted.)
            M._prepare_session(task_id, sid, user_message, image_b64, audio_b64, client_ts)
            M._start_llm_round(task_id, sid, 0)

        elif ev_type == "llm_ok":
            if t.get("_state") != "llm_waiting":
                continue
            sid = data["sid"]
            round_num = data["round"]
            body = data["body"]
            msg = body["choices"][0]["message"]
            mode = M.task_mode(task_id)
            with M._data_lock:
                if mode == "cpu":
                    M._cpu_last_llm_use = time.time()
                else:
                    M._last_llm_use = time.time()
            if msg.get("tool_calls"):
                with M._data_lock:
                    tt = M.tasks.get(task_id)
                    if tt:
                        tt.setdefault("_tools_used", [])
                        tt.setdefault("_search_details", [])
                        rc = msg.get("reasoning_content", "")
                        if rc:
                            tt["reasoning"] = rc
                pending = len(msg["tool_calls"])
                print(f"[llm_ok] Round {round_num}: LLM requested {pending} tool(s) for task {task_id}")  # DEBUG
                with M._data_lock:
                    tt = M.tasks.get(task_id)
                    if tt:
                        tt["_state"] = "tools_running"
                        tt["_pending_tools"] = pending
                with M._data_lock:
                    if sid in M.sessions:
                        assistant_msg = {"role": "assistant"}
                        if msg.get("content"):
                            assistant_msg["content"] = msg["content"]
                        if msg.get("tool_calls"):
                            assistant_msg["tool_calls"] = msg["tool_calls"]
                        M.sessions[sid].append(assistant_msg)
                        M.sessions_meta.setdefault(sid, {})["updated"] = time.time()
                M.save_sessions()
                tool_mode = M.task_mode(task_id)
                for i, tc in enumerate(msg["tool_calls"]):
                    M._tool_pools[tool_mode].submit(
                        M._tool_worker,
                        task_id,
                        sid,
                        tc,
                        t.get("_original_image"),
                        round_num,
                        i,
                    )
            else:
                print(f"[llm_ok] Round {round_num}: LLM generated final response (no tool calls) for task {task_id}")  # DEBUG
                if t.get("research"):
                    M.set_status(task_id, "Verifying sources...")
                    with M._data_lock:
                        tt = M.tasks.get(task_id)
                        if tt:
                            tt["_state"] = "critic_running"
                    M._tool_pools[mode].submit(
                        M.run_verification_worker,
                        task_id,
                        sid,
                        (msg.get("content") or ""),
                        body,
                        mode,
                    )
                else:
                    M._finalize_task(task_id, sid, (msg.get("content") or ""), body)

        elif ev_type == "llm_err":
            if t.get("_state") != "llm_waiting":
                continue
            M._set_task_error(task_id, data["error"], data.get("sid"))

        elif ev_type == "tool_ok":
            sid = data["sid"]
            tc_id = data["tc_id"]
            result = data["result"]
            with M._data_lock:
                if sid in M.sessions:
                    M.sessions[sid].append(
                        {"role": "tool", "tool_call_id": tc_id, "content": result}
                    )
                    M.sessions_meta.setdefault(sid, {})["updated"] = time.time()
                    print(f"[tool_ok] Appended tool result to session {sid} for task {task_id}")  # DEBUG
                tt = M.tasks.get(task_id)
                if not tt or tt.get("status") in ("done", "error"):
                    continue
                pending = (tt.get("_pending_tools", 0) - 1) if tt else 0
                if tt:
                    tt["_pending_tools"] = pending
            M.save_sessions()
            print(f"[tool_ok] Pending tools left for task {task_id}: {pending}")  # DEBUG
            if pending <= 0:
                round_num = data.get("round", 0) + 1
                print(f"[tool_ok] All tools done for task {task_id}. Starting LLM round {round_num} with search results in context.")  # DEBUG
                with M._data_lock:
                    tt = M.tasks.get(task_id)
                    if tt:
                        tt["_round"] = round_num
                if round_num < M._task_max_rounds(task_id):
                    M._start_llm_round(task_id, sid, round_num)
                else:
                    M._set_task_error(task_id, "Max tool rounds exceeded", sid)

        elif ev_type == "tool_err":
            result = data.get(
                "result", json.dumps({"error": data.get("error", "Tool error")})
            )
            with M._data_lock:
                if data.get("sid") in M.sessions:
                    M.sessions[data["sid"]].append(
                        {
                            "role": "tool",
                            "tool_call_id": data["tc_id"],
                            "content": result,
                        }
                    )
                    M.sessions_meta.setdefault(data["sid"], {})["updated"] = time.time()
                tt = M.tasks.get(task_id)
                if not tt or tt.get("status") in ("done", "error"):
                    continue
                pending = (tt.get("_pending_tools", 0) - 1) if tt else 0
                if tt:
                    tt["_pending_tools"] = pending
            M.save_sessions()
            if pending <= 0:
                round_num = data.get("round", 0) + 1
                with M._data_lock:
                    tt = M.tasks.get(task_id)
                    if tt:
                        tt["_round"] = round_num
                if round_num < M._task_max_rounds(task_id):
                    M._start_llm_round(task_id, data["sid"], round_num)
                else:
                    M._set_task_error(task_id, "Max tool rounds exceeded", data["sid"])


def _human_priority_active():
    '''
    # Removed the following check as self-agent bots will continue on CPU
    # Not needed anymore
    
    with M._queue_locks["gpu"]:
        if M._current_task_ids["gpu"] is not None or M._task_queues["gpu"]:
            return True
    now = time.time()
    with M._tokens_lock:
        for token, entry in M._active_tokens.items():
            if token in M._agent_tokens or entry.get("user") in M._agent_users:
                continue
            if now - entry.get("last_seen", 0) <= M.ACTIVE_WINDOW_SECONDS:
                return True
    '''
    return False


def _queue_worker(mode):
    """Drain the task queue for ``mode`` ("gpu" for interactive UI users,
    "cpu" for self-chat agents).

    Each lane runs on its own thread with its own lock/condition/queue, so a
    self-chat agent task sitting in the CPU lane can never make an
    interactive UI user in the GPU lane wait in line — they only share
    physical hardware if they both actually need the GPU (chat model load or
    image generation), which is arbitrated separately.

    The CPU lane additionally yields to any human presence (see
    ``_human_priority_active``) before starting its *next* task. An
    already-running self-chat round is never interrupted — it runs on its own
    hardware and was already established not to block the GPU lane — this
    only holds the CPU lane from picking up new work while a human is around.
    """
    queue_lock = M._queue_locks[mode]
    queue_cond = M._queue_conds[mode]
    task_queue = M._task_queues[mode]
    while True:
        if mode == "cpu":
            # If a human is currently active in the UI, hold off agent tasks
            while _human_priority_active():
                time.sleep(1.0)
                
        item = None
        with queue_lock:
            while not task_queue:
                queue_cond.wait()
            with M._data_lock:
                oh = M._overheated
            # GPU overheating only pauses the GPU lane — the CPU lane runs on
            # the CPU server and is unaffected. RAM pressure affects the whole
            # box, so it pauses both lanes.
            if (oh and mode == "gpu") or M._ram_evacuating:
                label = "GPU overheating" if oh else "RAM pressure — restarting servers"
                for qitem in task_queue:
                    tid = qitem["task_id"]
                    if tid in M.tasks:
                        M.tasks[tid] = {
                            "status": "waiting",
                            "message": f"Server paused — {label}. Will resume shortly.",
                            "session_id": qitem["session_id"],
                        }
                queue_cond.wait(5)
                continue
            if mode == "cpu" and M._human_priority_active():
                for qitem in task_queue:
                    tid = qitem["task_id"]
                    if tid in M.tasks:
                        M.tasks[tid] = {
                            "status": "waiting",
                            "message": "Yielding to an active user session...",
                            "session_id": qitem["session_id"],
                        }
                queue_cond.wait(5)
                continue
            item = task_queue.pop(0)
            M._current_task_ids[mode] = item["task_id"]
            with M._data_lock:
                if item["task_id"] in M.tasks:
                    M.tasks[item["task_id"]]["_started_at"] = time.time()
        M._event_post(
            "start",
            item["task_id"],
            sid=item["session_id"],
            message=item["message"],
            image=item.get("image"),
            audio=item.get("audio"),
            user=item.get("user", ""),
            client_timestamp=item.get("client_timestamp"),
            research=item.get("research"),
            cpu=item.get("cpu"),
            no_tools=item.get("no_tools"),
        )
        # Wait for this task to finish (status becomes "done", "error" or "cancelled")
        # before dequeuing the next item IN THIS LANE. The other lane's worker
        # keeps running independently the whole time.
        while True:
            with M._data_lock:
                st = M.tasks.get(item["task_id"], {}).get("status")
            if st in ("done", "error", "cancelled"):
                break
            time.sleep(0.5)
        with queue_lock:
            M._current_task_ids[mode] = None
            queue_cond.notify_all()