"""Session persistence and per-user session-file helpers.

Like server/tasks.py, these are plain functions that take the relevant state
(sessions dir, sessions dict, sessions_meta dict) as parameters instead of
reading module-level globals. chat-webui.py still owns the `sessions` /
`sessions_meta` globals and the locking around them; these helpers just do
the file I/O and data-shaping, so they're easy to test in isolation.
"""
import glob
import json
import os
import re
import time


def safe_username(user):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", user or "")
    return safe or "unknown"


def session_file(sessions_dir, user):
    return os.path.join(sessions_dir, f"sessions_{safe_username(user)}.json")


def load_extra_prompts(items):
    """Normalize a list of extra system prompt sources into [{name, content}].

    Each item may be a {name, content} dict or a server-side file path string.
    """
    blocks = []
    for it in items or []:
        if isinstance(it, dict):
            content = it.get("content") or ""
            if not content.strip():
                continue
            blocks.append({"name": it.get("name") or "System Prompt", "content": content})
        elif isinstance(it, str):
            p = os.path.abspath(os.path.expanduser(it))
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        blocks.append({"name": os.path.basename(it), "content": f.read()})
                except OSError:
                    pass
    return blocks


def load_sessions(sessions_dir):
    """Read every sessions_*.json under sessions_dir (plus a legacy
    sessions.json, which is migrated in and then removed).

    Returns fresh (sessions, sessions_meta) dicts. The caller installs them
    into its own state under whatever locking it uses.
    """
    sessions = {}
    sessions_meta = {}

    for path in glob.glob(os.path.join(sessions_dir, "sessions_*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        for sid, sdata in data.get("sessions", {}).items():
            sessions[sid] = sdata.get("messages", [])
            sessions_meta[sid] = {
                "name": sdata.get("name", "Chat"),
                "created": sdata.get("created", time.time()),
                "updated": sdata.get("updated", time.time()),
                "user_id": sdata.get("user_id", ""),
                "system_prompts": sdata.get("system_prompts", []),
            }

    stale = os.path.join(sessions_dir, "sessions.json")
    if os.path.exists(stale):
        try:
            with open(stale) as f:
                data = json.load(f)
            for sid, sdata in data.get("sessions", {}).items():
                if sid not in sessions:
                    sessions[sid] = sdata.get("messages", [])
                    sessions_meta[sid] = {
                        "name": sdata.get("name", "Chat"),
                        "created": sdata.get("created", time.time()),
                        "updated": sdata.get("updated", time.time()),
                        "user_id": sdata.get("user_id", ""),
                        "system_prompts": sdata.get("system_prompts", []),
                    }
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        try:
            os.remove(stale)
        except OSError:
            pass

    return sessions, sessions_meta


def save_sessions(sessions_dir, sessions, sessions_meta):
    by_user = {}
    for sid in sessions:
        meta = sessions_meta.get(
            sid, {"name": "Chat", "created": time.time(), "updated": time.time()}
        )
        user = meta.get("user_id", "")
        by_user.setdefault(user, {}).setdefault("sessions", {})[sid] = {
            "name": meta["name"],
            "created": meta["created"],
            "updated": meta["updated"],
            "user_id": meta.get("user_id", ""),
            "system_prompts": meta.get("system_prompts", []),
            "messages": sessions[sid],
        }
    for user, data in by_user.items():
        with open(session_file(sessions_dir, user), "w") as f:
            json.dump(data, f, indent=2)