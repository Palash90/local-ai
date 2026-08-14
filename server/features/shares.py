"""Public message sharing: create, fetch and revoke single-message shares.

A share is a capability URL: whoever knows the unguessable token can read the
message without logging in. The message content is snapshotted at share time so
later edits to or deletion of the session never change or break the share.

Storage is a simple JSON file (``SHARES_FILE``) shaped as::

    {
        "<token>": {
            "session_id": "...",
            "msg_index": 3,
            "owner": "alice",
            "created": 1234567890.0,
            "message": { "role": "assistant", "content": "...", ... }
        }
    }
"""

import copy
import json
import os
import time
import uuid

from server.features.state import M


def load_shares():
    """Load share records from disk into the shared ``shares`` container."""
    with M._data_lock:
        M.shares.clear()
    path = M.SHARES_FILE
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    with M._data_lock:
        for token, rec in (data or {}).items():
            M.shares[token] = rec


def save_shares():
    """Persist the in-memory ``shares`` container to disk."""
    with M._data_lock:
        data = copy.deepcopy(dict(M.shares))
    path = M.SHARES_FILE
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# Message fields worth carrying into a public snapshot. Everything else is
# internal bookkeeping that the public page must never see.
_SNAPSHOT_KEYS = (
    "role",
    "content",
    "_image_url",
    "_image_model",
    "_gen_prompt",
    "_reasoning",
    "_tools_used",
    "_search_details",
    "_elapsed_ms",
    "_timestamp",
)


def _snapshot_message(msg):
    snap = {}
    for key in _SNAPSHOT_KEYS:
        if key in msg:
            snap[key] = copy.deepcopy(msg[key])
    return snap


def _share_url(token):
    """Public URL for a share.

    Uses ``SHARE_BASE_URL`` (a portless origin, see config) when set, otherwise
    falls back to a site-relative path. A ported origin like ``:3001`` is
    deliberately avoided because WhatsApp stops auto-linking URLs at the ":" of
    a port, which truncates every share link to a dead short URL.
    """
    base = M.SHARE_BASE_URL
    return f"{base}/s/{token}" if base else f"/s/{token}"


def create_share(user, session_id, msg_index):
    """Create a share for a single assistant message.

    Validates session ownership and that the target message is an assistant
    (bot) message, then snapshots it. Returns ``(token, url)``.

    Raises ``ValueError`` with a user-facing message on any validation failure.
    """
    try:
        msg_index = int(msg_index)
    except (TypeError, ValueError):
        raise ValueError("Invalid message index")

    with M._data_lock:
        meta = M.sessions_meta.get(session_id)
        if not meta:
            raise ValueError("Session not found")
        if meta.get("user_id", "") != user:
            raise ValueError("Not your session")
        msgs = M.sessions.get(session_id)
        if not msgs or msg_index < 0 or msg_index >= len(msgs):
            raise ValueError("Message not found")
        msg = msgs[msg_index]
        if msg.get("role") != "assistant":
            raise ValueError("Only assistant messages can be shared")

    token = uuid.uuid4().hex
    with M._data_lock:
        M.shares[token] = {
            "session_id": session_id,
            "msg_index": msg_index,
            "owner": user,
            "created": time.time(),
            "message": _snapshot_message(msg),
        }
    save_shares()
    return token, _share_url(token)


def get_share(token):
    """Return the share record for a token, or ``None`` if missing/revoked."""
    if not token:
        return None
    with M._data_lock:
        return M.shares.get(token)


def revoke_share(token, user):
    """Remove a share. Only the owner may revoke.

    Returns ``True`` on success, ``False`` if the share does not exist or the
    user is not its owner.
    """
    with M._data_lock:
        rec = M.shares.get(token)
        if not rec or rec.get("owner") != user:
            return False
        del M.shares[token]
    save_shares()
    return True


def list_shares(user):
    """Return the caller's share records (with a plain-text preview)."""
    out = []
    with M._data_lock:
        for token, rec in M.shares.items():
            if rec.get("owner") != user:
                continue
            content = rec.get("message", {}).get("content", "")
            preview = ""
            if isinstance(content, str):
                preview = content[:120]
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        preview = (part.get("text") or "")[:120]
                        break
            out.append(
                {
                    "token": token,
                    "url": _share_url(token),
                    "session_id": rec.get("session_id"),
                    "msg_index": rec.get("msg_index"),
                    "created": rec.get("created"),
                    "preview": preview,
                }
            )
    return sorted(out, key=lambda s: s.get("created") or 0, reverse=True)
