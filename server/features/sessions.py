"""Conversation session persistence and per-request session preparation."""

import glob
import json
import os
import time
from datetime import datetime

from server.features.state import M


def _session_file(user):
    return os.path.join(M.SESSIONS_DIR, f"sessions_{M._safe_username(user)}.json")


def _session_meta_from(sdata):
    return {
        "name": sdata.get("name", "Chat"),
        "created": sdata.get("created", time.time()),
        "updated": sdata.get("updated", time.time()),
        "user_id": sdata.get("user_id", ""),
        "system_prompts": sdata.get("system_prompts", []),
        "context_tokens": sdata.get("context_tokens", {}),
    }


def _load_extra_prompts(items):
    """Normalize a list of extra system prompt sources into [{name, content}].

    Each item may be a {name, content} dict or a server-side file path string.
    """
    blocks = []
    for it in items or []:
        if isinstance(it, dict):
            content = it.get("content") or ""
            if not content.strip():
                continue
            blocks.append(
                {"name": it.get("name") or "System Prompt", "content": content}
            )
        elif isinstance(it, str):
            p = os.path.abspath(os.path.expanduser(it))
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        blocks.append(
                            {"name": os.path.basename(it), "content": f.read()}
                        )
                except OSError:
                    pass
    return blocks


def load_sessions():
    with M._data_lock:
        M.sessions.clear()
        M.sessions_meta.clear()
    for path in glob.glob(os.path.join(M.SESSIONS_DIR, "sessions_*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        with M._data_lock:
            for sid, sdata in data.get("sessions", {}).items():
                M.sessions[sid] = sdata.get("messages", [])
                M.sessions_meta[sid] = _session_meta_from(sdata)
    stale = os.path.join(M.SESSIONS_DIR, "sessions.json")
    if os.path.exists(stale):
        try:
            with open(stale) as f:
                data = json.load(f)
            with M._data_lock:
                for sid, sdata in data.get("sessions", {}).items():
                    if sid not in M.sessions:
                        M.sessions[sid] = sdata.get("messages", [])
                        M.sessions_meta[sid] = _session_meta_from(sdata)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        try:
            os.remove(stale)
        except OSError:
            pass


def save_sessions():
    by_user = {}
    with M._data_lock:
        for sid in M.sessions:
            meta = M.sessions_meta.get(
                sid, {"name": "Chat", "created": time.time(), "updated": time.time()}
            )
            user = meta.get("user_id", "")
            by_user.setdefault(user, {}).setdefault("sessions", {})[sid] = {
                "name": meta["name"],
                "created": meta["created"],
                "updated": meta["updated"],
                "user_id": meta.get("user_id", ""),
                "system_prompts": meta.get("system_prompts", []),
                "context_tokens": meta.get("context_tokens", {}),
                "messages": M.sessions[sid],
            }
    for user, data in by_user.items():
        with open(_session_file(user), "w") as f:
            json.dump(data, f, indent=2)


def _prepare_session(task_id, sid, user_message, image_b64, audio_b64=None, client_ts=None):
    try:
        if client_ts:
            ts = datetime.fromisoformat(client_ts.replace("Z", "+00:00"))
        else:
            ts = datetime.now()
    except Exception:
        ts = datetime.now()
    loc = M.location_str()
    loc_context = f" [User location: {loc}]" if loc else ""
    date_loc_context = f"[Current date: {ts.strftime('%Y-%m-%d %A %H:%M')}]{loc_context}"
    user = ""
    extra_prompts = []
    context_tokens = {}
    with M._data_lock:
        t = M.tasks.get(task_id)
        if t:
            user = t.get("_user", "")
        meta = M.sessions_meta.get(sid, {})
        extra_prompts = meta.get("system_prompts", [])
        context_tokens = meta.get("context_tokens", {})
    user_context = M.read_user_context(user) if user else ""
    context_block = f"\n\n## User Context\n{user_context}" if user_context else ""
    full_sys_content = f"{M.SYS_CONTENT}\n\n{date_loc_context}{context_block}"
    for blk in extra_prompts:
        full_sys_content += f"\n\n## {blk.get('name', 'System Prompt')}\n{blk.get('content', '')}"
    full_sys_content = full_sys_content.replace(
        "%current_time%", ts.strftime("%Y-%m-%d %A %H:%M")
    )
    if loc:
        full_sys_content = full_sys_content.replace("%current_location%", loc)
    else:
        full_sys_content = full_sys_content.replace(
            "Currently the server is hosted on %current_location%.", ""
        )
        full_sys_content = full_sys_content.replace("%current_location%", "not available")
    for token, value in context_tokens.items():
        full_sys_content = full_sys_content.replace(token, value)
    if user_context:
        print(
            f"[context] Injected {len(user_context)} chars of context for user '{user}'"
        )
    with M._data_lock:
        if sid not in M.sessions or not M.sessions[sid]:
            M.sessions[sid] = [{"role": "system", "content": full_sys_content}]
        elif M.sessions[sid][0].get("role") == "system":
            M.sessions[sid][0]["content"] = full_sys_content
        else:
            M.sessions[sid].insert(0, {"role": "system", "content": full_sys_content})
        if sid not in M.sessions_meta:
            M.sessions_meta[sid] = {
                "name": user_message[:50],
                "created": time.time(),
                "updated": time.time(),
            }
        content = []
        if image_b64:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                }
            )
        if audio_b64:
            content.append({"type": "text", "text": "\U0001F3A4 Audio message"})
        content.append(
            {
                "type": "text",
                "text": user_message,
            }
        )
        M.sessions[sid].append(
            {
                "role": "user",
                "content": content,
                "_timestamp": datetime.now().isoformat(),
            }
        )
        if M.sessions_meta[sid]["name"] in ("New Chat", ""):
            M.sessions_meta[sid]["name"] = user_message[:50] + (
                "..." if len(user_message) > 50 else ""
            )
        M.sessions_meta[sid]["updated"] = time.time()
    M.save_sessions()
    mode = M.task_mode(task_id)
    with M._data_lock:
        ms = M._cpu_model_status if mode == "cpu" else M.model_status
    if ms != "chat_loaded":
        M.load_llama_model(mode)
