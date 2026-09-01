"""The event loop, task queue and task-state helpers that drive a chat request."""

import base64
import json
import os
import re
import time

from server.features.context import resolve_image_path
from server.features.state import M
from server.features.toolstrip import strip_tool_call_text


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


def _image_bytes_b64(image):
    """Normalize an uploaded image to base64 bytes.

    The UI may pass a ``/uploads/...`` link (uploaded ahead of time) instead of
    raw base64. Image code downstream (edit_image, ComfyUI input writing)
    expects actual bytes, so resolve links to their on-disk contents here.
    """
    if not image:
        return None
    s = str(image)
    if not (s.startswith(("/uploads/", "/output/", "/api/image/")) or re.match(
        r"^https?://", s
    )):
        if s.startswith("data:image/"):
            s = s.split(",", 1)[-1]
        return s
    fpath = resolve_image_path(s)
    if fpath:
        try:
            with open(fpath, "rb") as f:
                return base64.b64encode(f.read()).decode()
        except OSError:
            pass
    return None


def _task_max_rounds(task_id):
    """Tool-loop round budget for a task: 10 for normal chats, 50 when the
    UI's research toggle is on (stored on the task as ``research``)."""
    with M._data_lock:
        t = M.tasks.get(task_id, {})
        if t.get("research"):
            return M.MAX_TOOL_ROUNDS.get("research", 50)
    return M.MAX_TOOL_ROUNDS.get("default", 10)


def _set_task_error(task_id, error, sid=None):
    mode = ""
    is_mcp = False
    with M._data_lock:
        if task_id in M.tasks:
            d = M.tasks[task_id]
            elapsed_ms = None
            if d.get("_started_at") is not None:
                elapsed_ms = int((time.time() - d.get("_started_at")) * 1000)
            mode = d.get("mode", "")
            is_mcp = bool(d.get("_mcp"))
            # Preserve lane/mcp/user/round markers so recovery paths (mcp-db
            # worker bookkeeping, tool-loop, critic reschedule) still work even
            # though the task has errored.
            M.tasks[task_id] = {
                "status": "error",
                "error": str(error),
                "session_id": d.get("session_id", sid),
                "_elapsed_ms": elapsed_ms,
                "mode": mode,
                "_mcp": is_mcp,
                "_user": d.get("_user"),
            }
    if mode == "guardrail" or is_mcp:
        try:
            from server.mcp_tasks_db import mcp_task_update
            mcp_task_update(task_id, status="error", reply=str(error)[:300])
        except Exception:
            pass


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
        artifacts = list(t.get("_artifacts", []))
        image_filename = t.get("image_file")
        gen_prompt = t.get("gen_prompt")
        image_model = t.get("_image_model")
        verification = t.get("_verification")
        verification_duration = t.get("_verification_duration")
        judge_result = t.get("_judge_result")
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
        "_artifacts": artifacts,
        "_research": bool(t.get("research")),
        "_elapsed_ms": elapsed_ms,
    }
    if verification is not None:
        msg_entry["_verification"] = verification
        msg_entry["_verification_duration"] = verification_duration
    confidence = (judge_result or {}).get("quality")
    if isinstance(confidence, int):
        msg_entry["_confidence"] = confidence
    mode = M.task_mode(task_id)
    with M._data_lock:
        if sid in M.sessions:
            M.sessions[sid].append(msg_entry)
            M.sessions_meta.setdefault(sid, {})["updated"] = time.time()

        if mode == "gpu":
            M._last_tps = predicted_per_second
            M._last_llm_use = time.time()
        elif mode == "cpu":
            M._cpu_last_llm_use = time.time()
        elif mode == "guardrail":
            M._guardrail_last_llm_use = time.time()
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
                "_artifacts": artifacts,
                "reasoning": reasoning,
                "_elapsed_ms": elapsed_ms,
            }
            if verification is not None:
                M.tasks[task_id]["_verification"] = verification
                M.tasks[task_id]["_verification_duration"] = verification_duration
    is_mcp_lane = (mode == "guardrail") or bool(t.get("_mcp"))
    # L3 post-processing output judge. Runs for EVERY generated task — including
    # the interactive UI (GPU) lane, which previously skipped it entirely — using
    # the per-user judge so the right model screens each user's reply. The
    # guardrail/MCP lane stays fail-closed (blocked output = marked failed); the
    # UI lane is fail-open (a judge outage must never drop a user's reply, so an
    # unavailable judge lets the answer through with a recorded note).
    task_user = t.get("_user") or ""
    try:
        from server.input_guard import is_strict_output_blocked
        from server.features.judge import mcp_output_judge, resolve_judge_model

        # MCP lane keeps the explicit MCP_USER judge; the UI lane resolves
        # the judge per-user (empty/unknown degrades to the default judge).
        if is_mcp_lane:
            judge_model = resolve_judge_model(os.environ.get("MCP_USER", "") or task_user)
        else:
            judge_model = resolve_judge_model(task_user)

        reply_text = msg_content or ""
        print(f"[L3] verifying output for task {task_id}, len={len(reply_text)}, image_file={image_filename}, lane={'guardrail/MCP' if is_mcp_lane else 'UI'}, judge={judge_model}")
        print(f"[L3] msg_content={reply_text}")

        print(f"[L3] checking strict output blocks")
        blocked = is_strict_output_blocked(reply_text)
        judge_verdict = None
        if not blocked and reply_text.strip():
            # LLM strict judge. On the guardrail/MCP lane it is fail-closed
            # (self-heals by restarting the guardrail server and retrying);
            # on the UI lane a judge outage degrades to a "screened" note so
            # the reply is still delivered.
            if is_mcp_lane:
                judge_verdict = mcp_output_judge(
                    reply_text, model_id=judge_model, fail_closed=True,
                )
            else:
                judge_verdict = mcp_output_judge(
                    reply_text, model_id=judge_model, fail_closed=False,
                    allow_gpu_fallback=(mode == "gpu"),
                )
            blocked = blocked or bool(judge_verdict)
        if blocked:
            print(f"[L3] BLOCKED: strict output filter triggered on text: {reply_text[:500]}")
            if is_mcp_lane:
                from server.mcp_tasks_db import mcp_task_update
                mcp_task_update(task_id, status="done", reply=reply_text,
                              verification_level="LEVEL 3 OUTPUT VERIFICATION FAILED",
                              failure_reason="Output blocked by strict filter")
            else:
                with M._data_lock:
                    tt = M.tasks.get(task_id)
                    if tt:
                        tt["_l3_verdict"] = "BLOCKED"
                        tt.setdefault("_verification", []).append(
                            {"url": "", "meta": None,
                             "action": "BLOCKED",
                             "note": "L3 output judge flagged the reply as blocked"}
                        )
        else:
            print(f"[L3] PASSED: output approved by strict filter")
            if is_mcp_lane:
                from server.mcp_tasks_db import mcp_task_update
                mcp_task_update(task_id, status="done", reply=reply_text,
                              verification_level="LEVEL 3 OUTPUT VERIFICATION PASSED")
    except Exception as e:
        print(f"[L3] error during output verification: {e}")
        # MCP output verification is fail-closed. Do not publish a successful
        # task when L3 could not produce a verdict, but do persist the terminal
        # failure so the MCP client is not left polling a permanent "working"
        # row.
        if is_mcp_lane:
            M._set_task_error(task_id, f"L3 output verification failed: {e}", sid)


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
                    "_original_image": _image_bytes_b64(image_b64),
                    "_audio": audio_b64,
                    "_user": user,
                    "_client_timestamp": client_ts,
                    "mode": t.get("mode"),
                    "_mcp": bool(data.get("_mcp")) or bool(t.get("_mcp")),
                    "_peer_review": bool(data.get("_peer_review"))
                    or bool(t.get("_peer_review")),
                    "research": bool(data.get("research")),
                    "cpu": bool(data.get("cpu")),
                    "no_tools": bool(data.get("no_tools")),
                    "openai_lane": bool(data.get("openai_lane")),
                    "_started_at": t.get("_started_at"),
                    "skip_ensure_llama": bool(data.get("skip_ensure_llama")),
                }
                if data.get("openai_lane"):
                    print(f"[openai] processing OpenAI lane request for task {task_id}")
            # (The owning lane's _current_task_ids[mode] was already set by
            # _queue_worker before this "start" event was posted.)
            if data.get("_resumed"):
                # RAM-evacuation resume: _prepare_session already ran on the
                # first attempt — the user message and everything the model
                # produced since (tool trail, steering turns) are in the
                # session. Only the user message was appended back then;
                # appending it again duplicated the turn in the UI.
                M._start_llm_round(task_id, sid, 0)
            else:
                M._prepare_session(task_id, sid, user_message, image_b64, audio_b64, client_ts)
                M._start_llm_round(task_id, sid, 0)

        elif ev_type == "llm_ok":
            if t.get("_state") != "llm_waiting":
                continue
            sid = data["sid"]
            round_num = data["round"]
            body = data["body"]
            msg = body["choices"][0]["message"]
            raw_content = msg.get("content") or ""
            cleaned_content = strip_tool_call_text(raw_content)
            if cleaned_content != raw_content:
                # The model leaked tool-call markup as plain text (happens on
                # no-tool rounds whose history still shows past tool
                # exchanges). Strip it before judging, storing or streaming.
                msg["content"] = cleaned_content
                print(f"[llm_ok] Stripped inline tool-call markup from content ({len(raw_content)} -> {len(cleaned_content)} chars) for task {task_id}")  # DEBUG
            mode = M.task_mode(task_id)
            with M._data_lock:
                if mode == "cpu":
                    M._cpu_last_llm_use = time.time()
                elif mode == "guardrail":
                    M._guardrail_last_llm_use = time.time()
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
                is_openai = t.get("openai_lane")
                print(f"[llm_ok] Round {round_num}: LLM requested {pending} tool(s) for task {task_id}" + (" (OpenAI lane: client will execute)" if is_openai else ""))  # DEBUG
                with M._data_lock:
                    tt = M.tasks.get(task_id)
                    if tt:
                        if is_openai:
                            tt["_state"] = "client_tool_calls_pending"
                        else:
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
                if not is_openai:
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
                    # OpenAI lane: don't execute tools server-side — hand the
                    # structured tool_calls back to the client so the extension
                    # executes them and (optionally) posts results in a follow-up
                    # request. Finalize the task with the tool_calls attached so
                    # the /v1/chat/completions handler (blocking in _poll_task)
                    # returns promptly instead of waiting out its timeout.
                    print(f"[openai] skipping server-side tool execution; sending tool_calls to client")
                    with M._data_lock:
                        t2 = M.tasks.get(task_id)
                        if t2:
                            t2["status"] = "done"
                            t2["response"] = msg.get("content") or ""
                            t2["session_id"] = sid
                            t2["tool_calls"] = msg["tool_calls"]
                            t2["finish_reason"] = "tool_calls"
                            t2["_terminal"] = True
            else:
                print(f"[llm_ok] Round {round_num}: LLM generated final response (no tool calls) for task {task_id}")  # DEBUG
                print(f"[llm_ok] Message structure: content={repr(msg.get('content'))}, reasoning={repr(msg.get('reasoning_content'))}")
                if raw_content and not cleaned_content:
                    # Content was *pure* tool-call spam. Reject the draft the
                    # same way the critic does — it was never appended to the
                    # session, so the retry re-runs from the clean trail with
                    # a steering note (bounded by _toolspam_done).
                    with M._data_lock:
                        tt = M.tasks.get(task_id)
                        spam_done = (tt.get("_toolspam_done", 0) + 1) if tt else 0
                        if tt:
                            tt["_toolspam_done"] = spam_done
                    if spam_done > 2:
                        M._set_task_error(
                            task_id,
                            "Model repeatedly emitted tool-call markup instead of a reply",
                            sid,
                        )
                        continue
                    with M._data_lock:
                        if sid in M.sessions:
                            M.sessions[sid].append(
                                {
                                    "role": "user",
                                    "content": (
                                        "[SYSTEM NOTE — internal revision. Your previous draft was "
                                        "rejected and must NOT be reused or repeated. Reason: it was "
                                        "raw tool-call markup instead of a reply. Answer in plain "
                                        "language without any tool-call syntax.]"
                                    ),
                                    "_steering": True,
                                }
                            )
                            M.sessions_meta.setdefault(sid, {})["updated"] = time.time()
                    M.save_sessions()
                    M.set_status(task_id, "Re-running (tool-call markup)...")
                    print(f"[llm_ok] content was pure tool-call markup — re-scheduling task {task_id} (round={round_num})")  # DEBUG
                    M._start_llm_round(task_id, sid, round_num)
                    continue
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
                elif mode == "gpu" and not t.get("openai_lane"):
                    # Interactive UI (GPU) answers go through the same final-
                    # answer quality gate + bounded re-run as research (see
                    # critic.run_verification_worker), so every UI reply is
                    # judged against the user's request before it is finalized.
                    M.set_status(task_id, "Evaluating answer...")
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
                elif (
                    mode == "cpu"
                    and t.get("_user") in M._agent_users
                    and not t.get("_peer_review")
                    and not t.get("research")
                ):
                    # Background agent replies (Kaya/Kolpo on the CPU lane) get
                    # a full cross-agent critique round: the peer reviews the
                    # reply as a real chat before the task finalizes. The peer
                    # round itself is flagged _peer_review and skips this
                    # branch (no recursion).
                    M.set_status(task_id, "Peer review...")
                    with M._data_lock:
                        tt = M.tasks.get(task_id)
                        if tt:
                            tt["_state"] = "peer_review_running"
                    M._tool_pools[mode].submit(
                        M.run_peer_review_worker,
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
                if not tt or tt.get("status") in ("done", "error", "requeued"):
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
                if not tt or tt.get("status") in ("done", "error", "requeued"):
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
        if mode in ("cpu", "guardrail"):
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
            if mode in ("cpu", "guardrail") and M._human_priority_active():
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
            openai_lane=item.get("openai_lane"),
            skip_ensure_llama=item.get("skip_ensure_llama"),
            # Carry the MCP lane flag through the queue: the RAM/thermal pause
            # paths below rewrite M.tasks[tid] to a minimal dict (dropping
            # "_mcp"), so the entry itself must remain the source of truth.
            _mcp=item.get("_mcp"),
            # Same for the peer-review recursion guard.
            _peer_review=item.get("_peer_review"),
            # Set when _evacuate_ram requeued an in-flight task: the session
            # already holds this task's user message, so "start" must skip
            # _prepare_session (see the resume branch in _event_loop).
            _resumed=item.get("_resumed"),
        )
        # Wait for this task to finish (status becomes "done", "error",
        # "cancelled" or "requeued") before dequeuing the next item IN THIS
        # LANE. The other lane's worker keeps running independently the whole
        # time. ("requeued" is set only by _evacuate_ram on the CURRENT task,
        # never at enqueue time, so it cannot race with a fresh "queued".)
        while True:
            with M._data_lock:
                st = M.tasks.get(item["task_id"], {}).get("status")
            if st in ("done", "error", "cancelled", "requeued"):
                break
            time.sleep(0.5)
        with queue_lock:
            M._current_task_ids[mode] = None
            queue_cond.notify_all()


MCP_DB_POLL_INTERVAL = 2


def _mcp_db_worker():
    """DB-polling worker for MCP tasks: reads queued tasks from the SQLite
    ``mcp_tasks`` table, claims them, and routes them through the owning
    lane's queue (GPU by default, CPU when requested) so they are scheduled
    exactly like an interactive chat user.

    This runs on its own thread and processes one MCP task at a time; the
    guardrail (L2/L3) LLM judging still happens on the dedicated guardrail
    server regardless of which lane runs the generation.
    """
    from server.mcp_tasks_db import mcp_task_list, mcp_task_update
    MCP_USER = os.environ.get("MCP_USER", "")
    print("[mcp-db] worker started — polling SQLite for queued tasks", flush=True)
    while True:
        rows = mcp_task_list(limit=1, status="queued")
        if not rows:
            time.sleep(MCP_DB_POLL_INTERVAL)
            continue
        row = rows[0]
        task_id = row["task_id"]
        cpu_flagged = bool(row.get("cpu"))
        # MCP chat generation runs on the GPU lane (same server/model as
        # interactive chat users) by default; callers may opt into the CPU lane.
        mode = "cpu" if cpu_flagged else "gpu"
        print(f"[mcp-db] found queued task {task_id}, marking as working ({mode} lane)", flush=True)
        mcp_task_update(task_id, status="working")
        entry = {
            "task_id": task_id,
            "session_id": row["session_id"],
            "message": row["message"],
            "image": None,
            "audio": None,
            "user": MCP_USER,
            "client_timestamp": None,
            "research": bool(row.get("research")),
            "cpu": cpu_flagged,
            "no_tools": bool(row.get("no_tools")),
            "mode": mode,
            "_mcp": True,
        }
        with M._data_lock:
            M.tasks[task_id] = {
                "status": "queued",
                "message": "Waiting in line...",
                "session_id": row["session_id"],
                "mode": mode,
                "_mcp": True,
                "research": bool(row.get("research")),
                "cpu": cpu_flagged,
                "no_tools": bool(row.get("no_tools")),
            }
        # Route through the owning lane's queue so MCP chat tasks are scheduled
        # exactly like an interactive chat user (FIFO ordering, _current_task_ids
        # bookkeeping for idle/RAM/thermal protection) rather than bypassing it.
        print(f"[mcp-db] queuing task {task_id} on {mode} lane", flush=True)
        with M._queue_locks[mode]:
            M._task_queues[mode].append(entry)
            M._queue_conds[mode].notify_all()
        print(f"[mcp-db] waiting for task {task_id} to complete", flush=True)
        while True:
            with M._data_lock:
                st = M.tasks.get(task_id, {}).get("status")
            if st in ("done", "error", "cancelled"):
                print(f"[mcp-db] task {task_id} completed with status={st}", flush=True)
                break
            time.sleep(0.5)
        time.sleep(5)
