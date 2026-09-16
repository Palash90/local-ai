"""The event loop, task queue and task-state helpers that drive a chat request."""

import base64
import hashlib
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


def _source_footer(search_details):
    """Build a deterministic citation footer from sources actually retrieved."""
    sources = []
    seen = set()
    retrieved_at = None
    for detail in search_details:
        if not isinstance(detail, dict):
            continue
        retrieved_at = retrieved_at or detail.get("retrieved_at")
        candidates = detail.get("results", [])
        if detail.get("tool") == "fetch_page":
            candidates = [detail]
        for source in candidates:
            if not isinstance(source, dict):
                continue
            url = source.get("url", "")
            if not url or url in seen or "/search" in url:
                continue
            if detail.get("tool") == "fetch_page" and not source.get("content"):
                continue
            if detail.get("tool") != "fetch_page" and not source.get("full_content"):
                continue
            seen.add(url)
            sources.append((source.get("title") or source.get("page_title") or url, url))
            if len(sources) >= 5:
                break
        if len(sources) >= 5:
            break
    if not sources:
        return ""
    stamp = f" (retrieved {retrieved_at})" if retrieved_at else ""
    lines = [f"\n\n---\n**Sources{stamp}:**"]
    lines.extend(f"- [{title}]({url})" for title, url in sources)
    return "\n".join(lines)


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


def _is_openai_lane_server_tool(tc):
    """True if a tool call targets a server-executed lane tool.

    Server set wins on exact name match (collision policy); everything
    else is a client tool returned for the client to run.
    """
    try:
        names = M.OPENAI_LANE_SERVER_TOOL_NAMES
    except Exception:
        names = {"web_search", "fetch_page", "tool_details"}
    try:
        return ((tc.get("function") or {}).get("name") in names)
    except Exception:
        return False


def _openai_lane_server_calls(tool_calls):
    """Partition of an llm_ok round: server-executed calls only."""
    return [tc for tc in (tool_calls or []) if _is_openai_lane_server_tool(tc)]


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
    # Abort any in-flight LLM stream so an errored task's round ends cleanly
    # instead of lingering (or later hitting an unloaded server). Mirrors the
    # user-cancel path in api.py.
    with M._data_lock:
        stream = M._active_streams.pop(task_id, None)
    if stream is not None:
        try:
            stream.close()
            stream.raw.close()
        except Exception:
            pass
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


def _delete_task_music(task_id):
    """Remove the generated music files attached to a (cancelled) task, if any."""
    with M._data_lock:
        t = M.tasks.get(task_id)
        if not t:
            return
        rel = t.get("music_file")
    if not rel:
        return
    for fpath in (
        os.path.join(M.MUSIC_DIR, rel),
        os.path.splitext(os.path.join(M.MUSIC_DIR, rel))[0] + ".mid",
        os.path.splitext(os.path.join(M.MUSIC_DIR, rel))[0] + ".opus",
    ):
        try:
            if os.path.exists(fpath):
                os.remove(fpath)
                print(f"[cancel] Removed music for cancelled task {task_id}: {fpath}")
        except OSError:
            pass


# Fabricated artifact links the model pastes when its tool loop failed —
# e.g. "[Image](/[Image: 9771012342.png])", "[Image of a flute...](https://
# storage.googleapis.com/...)", or "[Image](https://...googleusercontent.com/
# drive/output/...)". Our real images live under /output/ and /uploads/,
# never GCS — a GCS link presented as generated imagery is fabricated. Bare
# internal tokens (music_url, None, empty) as targets are the same lie. None
# of these survive into the stored answer.
_FAKE_ARTIFACT_LINK_RE = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(?:/\[[^\]\n]*\][^\)\n]*"
    r"|(?:music_url|music_file|image_url|_music_\w+|_image_\w+|None|none|"
    r"undefined|null|))\s*\)"
    r"|!?\[[^\]\n]*(?:image|picture|photo|figure|illustration)[^\]\n]*\]"
    r"\(\s*https?://(?:storage\.googleapis\.com|[\w.-]*googleusercontent\.com)/[^)\s]*\)",
    re.IGNORECASE)

_ART_LINK_LINE_RE = re.compile(
    r"(?im)^[ \t]*(?:[-*+][ \t]+)?(?:\*\*)?\s*"
    r"(?:audio|image|music|track|song)(?:\s+link)?\s*:?\s*(?:\*\*)?[ \t]*"
    r"\[[^\]\n]*\]\((/(?:output|music)/[^)\s]+)\)[ \t]*\n?",
)
_ART_LABEL_LINE_RE = re.compile(
    r"(?im)^[ \t]*(?:\*\*)?\s*(?:audio|image|music|track|song)(?:\s+link)?\s*:?\s*"
    r"(?:\*\*)?[ \t]*\n+",
)
# "z_image/..." is an image-model config key, never a served route — a model
# mashing it into a markdown link always 404s. Neutralize regardless of
# attached state (the attached card/player, if any, still renders).
_BOGUS_IMG_LINK_RE = re.compile(
    r"!?\[(?P<alt>[^\]\n]*)\]\(\s*z_image/[^)\s]*\s*\)",
    re.IGNORECASE,
)


def _strip_pasted_artifact_paths(text, image_attached, music_attached):
    """Drop text lines that merely restate an artifact path ("**Image Link:**
    [View](/output/...)") when the UI already attaches that very artifact as
    a card/player. Only lines whose artifact is attached are removed, so a
    reference the UI does not show is never silently deleted."""
    if not text:
        return text

    def _drop(match):
        line = match.group(0)
        if "/output/" in line and image_attached:
            return ""
        if "/music/" in line and music_attached:
            return ""
        return line

    out = _ART_LINK_LINE_RE.sub(_drop, text)
    out = _ART_LABEL_LINE_RE.sub("", out)
    # Drop link-only lines that are just a bogus z_image link; unwrap inline
    # ones to their alt text. A message left with nothing real says so via
    # the caller's "(No response content generated)" fallback.
    lines = []
    for line in out.split("\n"):
        if line.strip() and _BOGUS_IMG_LINK_RE.fullmatch(line.strip()):
            continue
        lines.append(_BOGUS_IMG_LINK_RE.sub(lambda m: m.group("alt"), line))
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _busted_stream_url(rel, stream_url):
    """Append a cache-busting ``?v=<mtime>`` to a music stream URL.

    Content-addressed URLs are immutable-cached for a year; if a sidecar is
    ever re-encoded under the same name, the mtime query busts the stale
    browser cache. Query is stripped server-side. Fail-safe passthrough.
    """
    if stream_url and rel and "?" not in stream_url:
        try:
            _op = os.path.join(M.MUSIC_DIR, rel.rsplit(".", 1)[0] + ".opus")
            return f"{stream_url}?v={int(os.path.getmtime(_op))}"
        except OSError:
            pass
    return stream_url


def _finalize_task(task_id, sid, msg_content, body, attach_image=True):
    """Append the finalized assistant message. With attach_image=False (used
    by the safety-decline path), image fields are withheld: unlike deterministic
    audio renders, model-prompted imagery is never attached to a turn whose
    text failed verification."""
    msg_content = strip_tool_call_text(msg_content or "")
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
        image_file_list = list(t.get("image_files", []) or [])
        music_rel = t.get("music_file")
        music_score = t.get("music_score")
        music_levels = t.get("music_levels")
        music_stream_url = t.get("music_stream_url")
        music_file_list = list(t.get("music_files", []) or [])
        verification = t.get("_verification")
        verification_duration = t.get("_verification_duration")
        judge_result = t.get("_judge_result")
    image_url = f"/output/{image_filename}" if image_filename and attach_image else None
    gen_prompt = gen_prompt if attach_image else None
    image_model = image_model if attach_image else None
    music_url = f"/music/{music_rel}" if music_rel else None
    # Anaphoric reuse ("with the same image", "play that track again"): re-show
    # the earlier artifact on THIS message so the UI re-attaches its card/player
    # even though this task only generated one of them.
    user_text = t.get("_original_message") or ""
    try:
        from server.features.critic import _referenced_artifacts, _prior_artifact
        referenced = _referenced_artifacts(user_text)
        if not image_url and "image" in referenced:
            image_url = _prior_artifact(sid, "_image_url")
        if not music_url and "music" in referenced:
            music_url = _prior_artifact(sid, "_music_url")
        if not music_stream_url and "music" in referenced:
            music_stream_url = _prior_artifact(sid, "_music_stream_url")
    except Exception as e:
        print(f"[finalize] artifact carry-over skipped: {e}")
    if music_stream_url and music_rel and "?" not in music_stream_url:
        # Content-addressed URLs are immutable-cached for a year; if a
        # sidecar is ever re-encoded under the same name, a ?v=<mtime>
        # busts the stale browser cache. Query is stripped server-side.
        music_stream_url = _busted_stream_url(music_rel, music_stream_url)
    # Plural channels: every render in this task, in order. Singular keys
    # above stay last-wins for critic/sessions/L3/shares compatibility.
    images = []
    if attach_image:
        for _entry in image_file_list:
            _rel = (_entry or {}).get("rel")
            if _rel:
                images.append(
                    {
                        "url": f"/output/{_rel}",
                        "prompt": (_entry or {}).get("prompt", ""),
                        "model": (_entry or {}).get("model"),
                    }
                )
    if not images and image_url:
        # Anaphoric carry-over (or legacy single render): mirror the singular.
        images.append(
            {"url": image_url, "prompt": gen_prompt or "", "model": image_model}
        )
    tracks = []
    for _entry in music_file_list:
        _entry = _entry or {}
        _rel = _entry.get("rel")
        _url = _entry.get("url") or (f"/music/{_rel}" if _rel else None)
        if not _url:
            continue
        tracks.append(
            {
                "url": _url,
                "stream_url": _busted_stream_url(_rel, _entry.get("stream_url")),
                "score": _entry.get("score", ""),
                "levels": _entry.get("levels", []),
                "duration_s": _entry.get("duration_s"),
            }
        )
    if not tracks and music_url:
        tracks.append(
            {
                "url": music_url,
                "stream_url": music_stream_url,
                "score": music_score,
                "levels": music_levels,
                "duration_s": None,
            }
        )
    msg_content = _strip_pasted_artifact_paths(
        msg_content, bool(image_url), bool(music_url)
    )
    msg_content = _FAKE_ARTIFACT_LINK_RE.sub("", msg_content or "")
    if image_url:
        print(f"[finalize] image_file='{image_filename}' → image_url='{image_url}' for task {task_id}")  # DEBUG
    timings = body.get("timings", {})
    predicted_per_second = timings.get("predicted_per_second")
    with M._data_lock:
        started_at = t.get("_started_at")
    elapsed_ms = None
    if started_at is not None:
        elapsed_ms = int((time.time() - started_at) * 1000)
    with M._data_lock:
        task_timings = dict(M.tasks.get(task_id, {}).get("_timings", {}))
    if elapsed_ms is not None:
        task_timings["total_ms"] = elapsed_ms
    print(f"[latency] task={task_id} total_ms={elapsed_ms} timings={task_timings}", flush=True)
    if not msg_content:
        msg_content = "(No response content generated)"
    reasoning = body.get("choices", [{}])[0].get("message", {}).get("reasoning_content", "")
    drafts = []
    with M._data_lock:
        for m in M.sessions.get(sid, []):
            if (m.get("role") == "assistant" and m.get("_draft")
                    and m.get("_task_id") == task_id):
                if m.get("_draft_reasoning"):
                    drafts.append(m["_draft_reasoning"])
                if m.get("content"):
                    drafts.append(strip_tool_call_text(m["content"]))
    if drafts:
        reasoning = ("\n\n".join(drafts) + "\n\n" + reasoning).strip()
    source_timestamp = next(
        (d.get("retrieved_at") for d in reversed(search_details)
         if isinstance(d, dict) and d.get("retrieved_at")),
        None,
    )
    if search_details and "**Sources" not in msg_content:
        msg_content = msg_content.rstrip() + _source_footer(search_details)
    msg_entry = {
        "role": "assistant",
        "content": msg_content,
        "_reasoning": reasoning,
        "_tools_used": tools_used,
        "_image_url": image_url,
        "_images": images,
        "_gen_prompt": gen_prompt,
        "_image_model": image_model,
        "_music_url": music_url,
        "_tracks": tracks,
        "_music_stream_url": music_stream_url,
        "_music_score": music_score,
        "_music_levels": music_levels,
        "_search_details": search_details,
        "_artifacts": artifacts,
        "_research": bool(t.get("research")),
        "_elapsed_ms": elapsed_ms,
    }
    if source_timestamp:
        msg_entry["_source_retrieved_at"] = source_timestamp
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
                "_images": images,
                "gen_prompt": gen_prompt,
                "_image_model": image_model,
                "music": music_url,
                "_music_url": music_url,
                "_tracks": tracks,
                "_music_score": music_score,
                "_music_levels": music_levels,
                "_search_details": search_details,
                "_artifacts": artifacts,
                "_elapsed_ms": elapsed_ms,
                "timings": task_timings,
                "reasoning": reasoning,
            }
            if source_timestamp:
                M.tasks[task_id]["source_retrieved_at"] = source_timestamp
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
        # Simple UI turns already skipped the quality judge; skip the (also
        # expensive, CPU-serialized) L3 LLM strict judge too, keeping only the
        # fast deterministic pattern scan. Non-simple / MCP / guardrail lanes
        # always run the LLM judge.
        simple_lane_skip = (mode == "gpu" and not is_mcp_lane
                            and M.is_simple_round_task(t)
                            and not M.answer_claims_artifact(reply_text))
        if not blocked and reply_text.strip() and not simple_lane_skip:
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
            M._delete_task_music(task_id)
            continue

        if ev_type == "start":
            sid = data["sid"]
            user_message = data["message"]
            image_b64 = data.get("image")
            audio_b64 = data.get("audio")
            user = data.get("user", "")
            client_ts = data.get("client_timestamp")
            # Preemption resume: a parked research task carries its next
            # round here (read before the dict rewrite below drops it).
            parked_round = t.get("_parked_round")
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
                    "client_tools": list(data.get("client_tools") or []),
                    "client_tool_choice": data.get("client_tool_choice") or "none",
                    "_started_at": t.get("_started_at"),
                    "skip_ensure_llama": bool(data.get("skip_ensure_llama")),
                }
                if data.get("openai_lane"):
                    print(f"[openai] processing OpenAI lane request for task {task_id}")
            # (The owning lane's _current_task_ids[mode] was already set by
            # _queue_worker before this "start" event was posted.)
            if isinstance(user_message, str) and user_message.strip().lower().startswith("make music"):
                # TEMPORARY dummy shortcut (no LLM): render a random arrangement
                # synchronously. Replaced soon by a real LLM tool call.
                M._prepare_session(task_id, sid, user_message, image_b64, audio_b64, client_ts)
                M._render_make_music(task_id, sid, user_message, user)
                continue
            if parked_round is not None:
                # Preemption resume: the session already holds the user
                # message plus every round so far — skip _prepare_session
                # (same rationale as _resumed below) and continue from the
                # parked round instead of restarting at 0. Takes precedence
                # over _resumed: a RAM evacuation mid-park must not reset
                # research progress.
                with M._data_lock:
                    tt = M.tasks.get(task_id)
                    if tt:
                        tt["_round"] = parked_round
                M._start_llm_round(task_id, sid, parked_round)
            elif data.get("_resumed"):
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
                pending = len(msg["tool_calls"])
                is_openai = t.get("openai_lane")
                summary, repeats = _track_tool_repeat(task_id, msg["tool_calls"])
                print(f"[llm_ok] Round {round_num}: LLM requested {pending} tool(s) for task {task_id}: {summary}" + (" (OpenAI lane: client will execute)" if is_openai else ""))  # DEBUG
                if repeats >= TOOL_LOOP_WATCH_THRESHOLD:
                    print(f"[loop-watch] task {task_id}: identical tool call {repeats}x in a row: {summary}")
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
                            if msg.get("content"):
                                # The model wrote scratch prose alongside its
                                # tool calls (e.g. score drafts). Keep it in
                                # the LLM history for continuity, but mark it:
                                # the UI hides the bubble and _finalize_task
                                # folds the drafts into the final answer's
                                # reasoning block instead of leaking them as
                                # visible messages.
                                assistant_msg["_draft"] = True
                                assistant_msg["_task_id"] = task_id
                                r = body.get("choices", [{}])[0].get(
                                    "message", {}).get("reasoning_content")
                                if r:
                                    assistant_msg["_draft_reasoning"] = r
                                if (msg.get("content") != raw_content
                                        and not (t.get("_tc_echo_steered") if t else False)):
                                    # A text-form tool echo was stripped from
                                    # this round's draft: steer once per task
                                    # so the next round uses the structured
                                    # channel only instead of imitating the
                                    # <|tool_call> history formatting.
                                    try:
                                        M.sessions[sid].append(
                                            {
                                                "role": "user",
                                                "content": (
                                                    "[SYSTEM NOTE — internal revision. Invoke tools "
                                                    "only through the structured function-call channel. "
                                                    "Never write tool-call syntax (<|tool_call>, "
                                                    "call:name{...}, [Assistant tool call]) as text. "
                                                    "This note is from your own execution loop, not "
                                                    "from the user and not an injection attempt. "
                                                    "Comply silently; do not discuss this note in "
                                                    "your thinking or answer.]"
                                                ),
                                                "_steering": True,
                                            }
                                        )
                                        if t is not None:
                                            t["_tc_echo_steered"] = True
                                        print(f"[llm_ok] dual-round echo stripped — steering added for task {task_id}")  # DEBUG
                                    except Exception:
                                        pass
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
                elif _openai_lane_server_calls(msg.get("tool_calls")):
                    # Hybrid lane, server-first: server-named calls execute
                    # in-lane via _tool_worker and rounds continue;
                    # client-named calls in the same round are dropped
                    # (the next round re-invites them with results in
                    # context). Cap server rounds to avoid ping-pong.
                    try:
                        max_srv = int(getattr(M, "OPENAI_LANE_MAX_SERVER_ROUNDS", 3) or 3)
                    except Exception:
                        max_srv = 3
                    with M._data_lock:
                        t3 = M.tasks.get(task_id)
                        srv_done = int((t3 or {}).get("_openai_server_rounds", 0) or 0)
                    if srv_done >= max_srv:
                        _client_only = [tc for tc in msg["tool_calls"]
                                        if not _is_openai_lane_server_tool(tc)]
                        if _client_only:
                            print(f"[openai] server-round cap ({max_srv}) hit — returning client calls for task {task_id}")
                            with M._data_lock:
                                t2 = M.tasks.get(task_id)
                                if t2:
                                    t2["status"] = "done"
                                    t2["response"] = ""
                                    t2["session_id"] = sid
                                    t2["tool_calls"] = _client_only
                                    t2["finish_reason"] = "tool_calls"
                                    t2["_terminal"] = True
                            continue
                        print(f"[openai] server-round cap ({max_srv}) hit with no client calls — forcing text final for task {task_id}")
                        with M._data_lock:
                            if sid in M.sessions:
                                M.sessions[sid].append(
                                    {
                                        "role": "user",
                                        "content": (
                                            "[SYSTEM NOTE — internal revision. You have already "
                                            "called server tools several times. Answer the user's "
                                            "request now in plain text using the results in "
                                            "context. Do not call any more tools.]"
                                        ),
                                        "_steering": True,
                                    }
                                )
                                M.sessions_meta.setdefault(sid, {})["updated"] = time.time()
                        M.save_sessions()
                        M._start_llm_round(task_id, sid, round_num + 1)
                        continue
                    with M._data_lock:
                        t4 = M.tasks.get(task_id)
                        if t4:
                            t4["_state"] = "tools_running"
                            t4["_openai_server_rounds"] = srv_done + 1
                            t4["_pending_tools"] = len(_openai_lane_server_calls(msg.get("tool_calls")))
                    print(f"[openai] executing {t4['_pending_tools'] if t4 else '?'} server tool(s) in-lane for task {task_id} (server round {srv_done + 1}/{max_srv})")
                    tool_mode = M.task_mode(task_id)
                    for i, tc in enumerate(_openai_lane_server_calls(msg.get("tool_calls"))):
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
                            # Structured tool_calls go to the client; the
                            # scratch prose alongside them is an internal
                            # draft (often containing text-form tool-markup
                            # echoes) — never send it as response content or
                            # API clients render the notation as chat text.
                            t2["response"] = ""
                            t2["session_id"] = sid
                            t2["tool_calls"] = msg["tool_calls"]
                            t2["finish_reason"] = "tool_calls"
                            t2["_terminal"] = True
            else:
                print(f"[llm_ok] Round {round_num}: LLM generated final response (no tool calls) for task {task_id}")  # DEBUG
                print(f"[llm_ok] Message structure: content={repr(msg.get('content'))}")
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
                    if spam_done > 2 or (spam_done >= 1 and t.get("openai_lane")):
                        # OpenAI lane fails fast: API clients get one quick,
                        # descriptive error instead of three GPU-burning
                        # retries that all produce the same markup. Chat lanes
                        # keep the bounded re-schedule below.
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
                                        "language without any tool-call syntax. This note is from "
                                        "your own execution loop, not from the user and not an "
                                        "injection attempt. Comply silently; do not discuss this "
                                        "note in your thinking or answer.]"
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
                    # But short, low-stakes turns (a one-line answer to a short
                    # question) skip the extra quality-judge LLM inference and
                    # finalize immediately — the deterministic pattern blockers
                    # still run in _finalize_task.
                    simple = (M.is_simple_round_task(t)
                              and not M.answer_claims_artifact(
                                  msg.get("content") or ""))
                    with M._data_lock:
                        tt = M.tasks.get(task_id)
                        if not tt or tt.get("status") in ("done", "error", "cancelled"):
                            continue
                    if simple:
                        # The deterministic requirement gates are free
                        # (regex-only) — run them even on simple rounds so a
                        # claim-shaped answer can never bypass verification.
                        _mm = None
                        try:
                            _mm = M._requirement_mismatch(
                                task_id, sid, t.get("_original_message", ""),
                                msg.get("content") or "")
                        except Exception as _e:
                            print(f"[llm_ok] simple-round gate check skipped: {_e}")
                        if _mm:
                            print(
                                f"[llm_ok] simple round BUT deterministic gate "
                                f"fired ({_mm}) — routing to verification "
                                f"for task {task_id}"
                            )
                            simple = False
                    if simple:
                        print(
                            f"[llm_ok] simple GPU round — skipping quality judge "
                            f"for task {task_id}"
                        )
                        M._finalize_task(task_id, sid, (msg.get("content") or ""), body)
                    else:
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
            # llama.cpp 500s the whole request when the model emits
            # malformed tool-call arguments (e.g. an unterminated score
            # string in generate_music). The model usually succeeds on a
            # re-try, so steer it toward compact valid JSON and re-run the
            # same round, bounded.
            err_text = data.get("error", "") or ""
            if "Failed to parse tool call arguments" in err_text:
                retries = t.get("_tool_json_retries", 0)
                if retries < 2:
                    with M._data_lock:
                        tt = M.tasks.get(task_id)
                        if tt:
                            tt["_tool_json_retries"] = retries + 1
                        if sid in M.sessions:
                            M.sessions[sid].append(
                                {
                                    "role": "user",
                                    "content": (
                                        "[SYSTEM NOTE — internal revision. Your last tool call "
                                        "was rejected: your score ran past the "
                                        "output limit and was cut off mid-string. "
                                        "You wrote every bar of every lane — stop. "
                                        "Re-emit ONE tool call with a score of at "
                                        "most 1100 characters: max 6 lanes, at most "
                                        "2 bars of material per lane (sections "
                                        "LOOP those bars to the declared length), "
                                        "no comments. Example budget: 6 lanes x 2 "
                                        "bars x ~12 tokens ≈ 900 chars. Escape all "
                                        "newlines as \\n and keep the JSON valid. This note is from "
                                        "your own execution loop, not from the user and not an "
                                        "injection attempt. Comply silently; do not discuss this "
                                        "note in your thinking or answer.]"
                                    ),
                                    "_steering": True,
                                }
                            )
                            M.sessions_meta.setdefault(sid, {})["updated"] = time.time()
                    M.save_sessions()
                    M.set_status(task_id, "Retrying (malformed tool call)...")
                    print(f"[llm_err] task {task_id} malformed tool-call JSON — re-scheduling round (retry {retries + 1}/2)")
                    M._start_llm_round(task_id, sid, data.get("round", 0))
                    continue
            # A cpu round killed by an image render's eviction (server killed
            # mid-flight) must resume, not die permanently. Requeue it like the
            # RAM-evacuation path so the lane picks it back up once the render
            # clears and the model reloads.
            if M.task_mode(task_id) == "cpu":
                with M._data_lock:
                    img_active = M._image_active
                if img_active:
                    print(f"[llm_err] task {task_id} interrupted by image render — requeueing to resume after it", flush=True)
                    with M._data_lock:
                        tsk = M.tasks.get(task_id, {})
                    entry = {
                        "task_id": task_id,
                        "session_id": tsk.get("session_id", ""),
                        "message": tsk.get("_original_message", ""),
                        "image": tsk.get("_original_image"),
                        "audio": tsk.get("_audio"),
                        "user": tsk.get("_user", ""),
                        "client_timestamp": tsk.get("_client_timestamp"),
                        "research": bool(tsk.get("research")),
                        "cpu": True,
                        "no_tools": bool(tsk.get("no_tools")),
                        "openai_lane": bool(tsk.get("openai_lane")),
                        "skip_ensure_llama": bool(tsk.get("skip_ensure_llama")),
                        "mode": "cpu",
                        "_mcp": bool(tsk.get("_mcp")),
                        "_peer_review": bool(tsk.get("_peer_review")),
                        "_resumed": True,
                    }
                    with M._queue_locks["cpu"]:
                        M._task_queues["cpu"].insert(0, entry)
                        M._queue_conds["cpu"].notify_all()
                    with M._data_lock:
                        tt = M.tasks.get(task_id)
                        if tt:
                            tt["status"] = "requeued"
                            tt["message"] = "Interrupted by image render — requeued, will resume shortly"
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
                    if not _maybe_park_research(task_id, sid, round_num):
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
                    if not _maybe_park_research(task_id, data["sid"], round_num):
                        M._start_llm_round(task_id, data["sid"], round_num)
                else:
                    M._set_task_error(task_id, "Max tool rounds exceeded", data["sid"])


# Lane priority for the shared GPU queue: interactive UI users first, then
# the OpenAI lane, then the MCP lane. The CPU lane (kaya/kolpo self-chat)
# is a separate worker and sits below all of these by construction.
LANE_RANK_UI = 0
LANE_RANK_OPENAI = 1
LANE_RANK_MCP = 2


def _lane_rank(item):
    """Return the queue priority rank of a task-queue entry (lower runs first)."""
    if not isinstance(item, dict):
        return LANE_RANK_UI
    if item.get("_mcp"):
        return LANE_RANK_MCP
    if item.get("openai_lane"):
        return LANE_RANK_OPENAI
    return LANE_RANK_UI


def _enqueue_ranked(queue, entry):
    """Insert ``entry`` into a task ``queue`` list behind the last queued
    item of equal-or-higher priority.

    Ordering is stable FIFO within a lane; a higher-priority arrival jumps
    ahead of lower-priority waiters but never ahead of its own lane. Caller
    must hold the lane's queue lock.
    """
    rank = _lane_rank(entry)
    pos = len(queue)
    for i in range(len(queue) - 1, -1, -1):
        if _lane_rank(queue[i]) <= rank:
            pos = i + 1
            break
        pos = i
    queue.insert(pos, entry)


# Queue-head ranks allowed to preempt in-flight research between rounds:
# interactive UI chats and the OpenAI lane. MCP and kaya/kolpo wait for
# research to finish.
PREEMPT_RANKS = (LANE_RANK_UI, LANE_RANK_OPENAI)


def _higher_priority_waiting():
    """True when the GPU queue head outranks in-flight research (UI/OpenAI)."""
    with M._queue_locks["gpu"]:
        q = M._task_queues["gpu"]
        if not q:
            return False
        return _lane_rank(q[0]) in PREEMPT_RANKS


def _maybe_park_research(task_id, sid, round_num):
    """Park an in-flight research task between rounds if a UI/OpenAI chat is
    queued. Returns True when parked (caller must NOT chain the next round);
    the lane worker requeues the task and it auto-resumes later from
    ``_parked_round``. Non-research tasks are never parked."""
    with M._data_lock:
        t = M.tasks.get(task_id, {})
        if not t.get("research"):
            return False
    if not _higher_priority_waiting():
        return False
    with M._data_lock:
        tt = M.tasks.get(task_id)
        if not tt or tt.get("status") in ("done", "error", "cancelled", "requeued"):
            return False
        tt["status"] = "parked"
        tt["_parked_round"] = round_num
        tt["_parked_sid"] = sid
        tt["message"] = "Paused — a higher-priority chat is running, will resume automatically."
    print(f"[preempt] parked research task {task_id} at round {round_num} for higher-priority waiter")
    return True


# Rounds of consecutive identical client tool calls after which a
# [loop-watch] line is logged (logging only — never steers or blocks).
TOOL_LOOP_WATCH_THRESHOLD = 3


def _toolcall_summary(tool_calls, arg_preview_chars=120):
    """Compact one-line summary of structured tool calls for logs.

    Renders ``name(arg-preview)`` per call, e.g.
    ``web_search({"q": "gemma 26b uncen…"})``. Defensive: bare/missing
    ``function`` dicts (seen in the wild) render as ``?`` instead of
    raising. Argument previews are truncated so query text can't flood
    the log.
    """
    parts = []
    for tc in tool_calls or []:
        fn = (tc.get("function") if isinstance(tc, dict) else None) or {}
        name = fn.get("name") or "?"
        args = fn.get("arguments", "")
        if not isinstance(args, str):
            try:
                args = json.dumps(args, sort_keys=True)
            except (TypeError, ValueError):
                args = repr(args)
        args = " ".join(args.split())
        if len(args) > arg_preview_chars:
            args = args[:arg_preview_chars] + "…"
        parts.append(f"{name}({args})")
    return ", ".join(parts) or "?"


def _toolcall_signature(tool_calls):
    """Stable signature of a round's tool calls for repeat detection."""
    norm = []
    for tc in tool_calls or []:
        fn = (tc.get("function") if isinstance(tc, dict) else None) or {}
        args = fn.get("arguments", "")
        if not isinstance(args, str):
            try:
                args = json.dumps(args, sort_keys=True)
            except (TypeError, ValueError):
                args = repr(args)
        norm.append((fn.get("name") or "?", args))
    raw = json.dumps(norm, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _track_tool_repeat(task_id, tool_calls):
    """Update the consecutive-repeat counter for a round's tool calls.

    Returns ``(summary, repeats)`` where ``repeats`` is how many rounds in
    a row produced the identical signature. Callers log ``[loop-watch]``
    when ``repeats`` hits ``TOOL_LOOP_WATCH_THRESHOLD``.
    """
    summary = _toolcall_summary(tool_calls)
    sig = _toolcall_signature(tool_calls)
    with M._data_lock:
        tt = M.tasks.get(task_id)
        if tt is None:
            return summary, 1
        if tt.get("_last_tool_sig") == sig:
            repeats = int(tt.get("_tool_repeat_count") or 1) + 1
        else:
            repeats = 1
        tt["_last_tool_sig"] = sig
        tt["_tool_repeat_count"] = repeats
    return summary, repeats


def _queue_worker(mode):
    """Drain the task queue for ``mode`` ("gpu" for interactive UI users,
    "cpu" for self-chat agents).

    Each lane runs on its own thread with its own lock/condition/queue, so a
    self-chat agent task sitting in the CPU lane can never make an
    interactive UI user in the GPU lane wait in line — they only share
    physical hardware if they both actually need the GPU (chat model load or
    image generation), which is arbitrated separately.

    Within the shared GPU queue, tasks are ordered by lane priority (see
    ``_lane_rank``): interactive UI users first, then the OpenAI lane, then
    the MCP lane. Ordering is stable FIFO within a lane. The CPU lane
    (kaya/kolpo self-chat agents) is a separate worker and is lowest by
    construction.
    """
    queue_lock = M._queue_locks[mode]
    queue_cond = M._queue_conds[mode]
    task_queue = M._task_queues[mode]
    while True:
        item = None
        with queue_lock:
            while not task_queue:
                queue_cond.wait()
            with M._data_lock:
                oh = M._overheated
            # GPU overheating only pauses the GPU lane — the CPU lane runs on
            # the CPU server and is unaffected. RAM pressure affects the whole
            # box, so it pauses both lanes. An active image render pauses every
            # lane: ComfyUI needs the GPU (VRAM) AND the ~9 GB of RAM the cpu
            # lane's model was evicted from, so no new model may load mid-render.
            if (oh and mode == "gpu") or M._ram_evacuating or M._image_active:
                label = "GPU overheating" if oh else ("RAM pressure — restarting servers" if M._ram_evacuating else "image rendering")
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
            item = task_queue.pop(0)
            M._current_task_ids[mode] = item["task_id"]
            with M._data_lock:
                if item["task_id"] in M.tasks:
                    queued_at = M.tasks[item["task_id"]].get("_queued_at")
                    M.tasks[item["task_id"]]["_started_at"] = time.time()
                    if queued_at is not None:
                        M.tasks[item["task_id"]].setdefault("_timings", {})["queue_ms"] = int((time.time() - queued_at) * 1000)
        # Thermal pacing (duty-cycle control): breathe between pickups so
        # sustained load can't outrun chassis cooling. This shared worker is
        # the single site for all task lanes (guardrail judges exempt: their
        # seconds-long bursts don't move package temp, and they carry 240s
        # floors plus fail-closed semantics). In-flight rounds are never
        # touched — pacing applies between pickups only. Logs only when the
        # scaled extension fires, so the 1s floor stays silent.
        if mode != "guardrail":
            try:
                _plat_temp = M.get_platform_temp()
                _pace = M.pace_delay(_plat_temp)
            except Exception:
                _plat_temp, _pace = None, 1.0
            if _pace > 1.0:
                print(f"[thermal] pacing {_pace:.0f}s before {mode} round (platform {_plat_temp:.0f}C)", flush=True)
            time.sleep(_pace)
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
            client_tools=item.get("client_tools"),
            client_tool_choice=item.get("client_tool_choice"),
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
        # A "parked" research task yielded between rounds for a
        # higher-priority waiter: put it back in line (ranked — behind
        # UI/OpenAI waiters, ahead of MCP) and pick up the head instead.
        while True:
            with M._data_lock:
                st = M.tasks.get(item["task_id"], {}).get("status")
            if st in ("done", "error", "cancelled", "requeued"):
                break
            if st == "parked":
                with queue_lock:
                    _enqueue_ranked(task_queue, item)
                    M._current_task_ids[mode] = None
                print(f"[preempt] requeued parked task {item['task_id']}; picking up higher-priority waiter")
                item = None
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
            _enqueue_ranked(M._task_queues[mode], entry)
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
