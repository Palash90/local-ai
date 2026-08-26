"""Per-user context files and HTTP identity resolution.

Identity comes from :mod:`server.auth` — Authentik is the single identity
provider (there is no users.json anymore). This module keeps the names the
chat engine calls: password lookup is gone, context paths are derived from the
username, and ``get_current_user``/``get_current_identity`` resolve the
request's identity through the shared auth layer.
"""

import os
import re
import time
from datetime import datetime

from server.auth import get_current_user as auth_get_current_user
from server.auth import get_identity as auth_get_identity
from server.config import CONTEXTS_DIR
from server.features.state import M


def _safe_username(user):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", user or "")
    return safe or "unknown"


def _mark_seen(username):
    """Record a username as recently active (heartbeat for active-user display)."""
    if not username:
        return
    with M._user_last_seen_lock:
        M._user_last_seen[username] = time.time()


def get_current_user(headers):
    username = auth_get_current_user(headers)
    _mark_seen(username)
    return username


def get_current_identity(headers):
    identity = auth_get_identity(headers)
    if identity:
        _mark_seen(identity["username"])
    return identity


def active_users(window_seconds=120, exclude_agents=True):
    """Sorted usernames seen within the window, optionally excluding agents."""
    now = time.time()
    with M._user_last_seen_lock:
        users = sorted(
            u for u, ts in M._user_last_seen.items()
            if now - ts <= window_seconds
        )
    if exclude_agents:
        with M._tokens_lock:
            agent_users = set(M._agent_users)
        return [u for u in users if u not in agent_users]
    return users


def get_user_context_path(username):
    """Derive the context file path from the username.

    users.json previously stored an arbitrary per-user ``context_file`` path;
    with Authentik as the sole identity store that field no longer exists, so
    every user's context lives at ``CONTEXTS_DIR/<username>.txt``.
    """
    return os.path.join(CONTEXTS_DIR, _safe_username(username) + ".txt")


def read_user_context(username):
    path = get_user_context_path(username)
    # print("Context path", path, "for", username)  # Disabled for cleaner logs
    if path and os.path.exists(path):
        try:
            # print("Reading", path)  # Disabled for cleaner logs
            with open(path) as f:
                context = f.read()
                # print(context)  # Disabled for cleaner logs
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