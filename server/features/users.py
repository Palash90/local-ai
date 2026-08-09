"""Authentication and per-user context files.

Mirrors the original chat-webui.py helpers: password lookup against the shared
``users.json`` (with a short cache), context file path resolution, context
read/append, and auth-token validation for the HTTP layer.
"""

import json
import os
import re
import time
from datetime import datetime

from server.features.state import M


def _safe_username(user):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", user or "")
    return safe or "unknown"


def load_users():
    now = time.time()
    if M._users_cache is not None and now - M._users_cache_time < 30:
        return M._users_cache
    try:
        with open(M.USERS_FILE) as f:
            data = json.load(f)
        M._users_cache = data.get("users", {})
        M._users_cache_time = now
    except (FileNotFoundError, json.JSONDecodeError):
        M._users_cache = {}
        M._users_cache_time = now
    return M._users_cache


def get_user_password(username):
    users = M.load_users()
    u = users.get(username)
    return u.get("password", "") if u else ""


def get_user_context_path(username):
    users = M.load_users()
    u = users.get(username)
    if u and u.get("context_file"):
        return os.path.join(u["context_file"])
    return ""


def read_user_context(username):
    path = get_user_context_path(username)
    print("Context path", path, "for", username)
    if path and os.path.exists(path):
        try:
            print("Reading", path)
            with open(path) as f:
                context = f.read()
                print(context)
                return context
        except:
            return ""
    return ""


def write_user_context(username, content):
    path = get_user_context_path(username)
    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existing = read_user_context(username)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"[{timestamp}] {content}"
        new_content = (existing.strip() + "\n\n" + entry) if existing.strip() else entry
        with open(path, "w") as f:
            f.write(new_content)


def get_current_user(headers):
    token = headers.get("X-Auth-Token", "")
    if not token:
        return None
    with M._tokens_lock:
        entry = M._active_tokens.get(token)
        if not entry:
            return None
        entry["last_seen"] = time.time()
        return entry["user"]
