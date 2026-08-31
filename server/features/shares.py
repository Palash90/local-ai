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
import re
import time
import uuid

from server.config import COMFYUI_OUTPUT, UPLOADS_DIR
from server.features.state import M

# Image/upload references embedded anywhere inside a message snapshot
# ("[IMAGE: /output/x.png]", "[FILE: /uploads/y.pdf]", image_url parts, ...).
_IMAGE_REF_RE = re.compile(r"/(?:uploads|output)/[A-Za-z0-9._\-/]+")


def message_image_refs(obj):
    """Every uploads/|output/ reference found anywhere in a message structure.

    Returns paths relative to the files root, e.g. {"uploads/a.pdf",
    "output/img.png"}. Used both to scope what a public share may serve and
    to decide which artifacts are safe to purge on unshare.
    """
    refs = set()

    def walk(node):
        if isinstance(node, str):
            for match in _IMAGE_REF_RE.findall(node):
                refs.add(match.strip("/"))
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(obj)
    return refs


def _ref_file_path(ref):
    """Absolute path for an "uploads/..." or "output/..." ref, or None."""
    if ref.startswith("uploads/"):
        root, rest = UPLOADS_DIR, ref[len("uploads/"):]
    elif ref.startswith("output/"):
        root, rest = COMFYUI_OUTPUT, ref[len("output/"):]
    else:
        return None
    real_root = os.path.realpath(root)
    fpath = os.path.realpath(os.path.join(real_root, rest))
    if fpath != real_root and not fpath.startswith(real_root + os.sep):
        return None
    return fpath


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
    "_confidence",
    "_timestamp",
)


def _snapshot_message(msg):
    if msg.get("_steering"):
        return None
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


def revoke_share(token, user, purge=False):
    """Remove a share. Only the owner may revoke.

    With ``purge`` the artifacts referenced ONLY by this share's snapshot are
    deleted as well — used when the underlying chat is already gone and the
    user wants the files gone with it. A ref survives if any other active
    share or any still-existing session message references it.

    Returns ``(ok, info)`` where info carries ``session_exists`` (was the
    originating chat still present?) and ``purged`` (refs actually deleted).
    """
    with M._data_lock:
        rec = M.shares.get(token)
        if not rec or rec.get("owner") != user:
            return False, {}
        session_exists = rec.get("session_id") in M.sessions_meta
        own_refs = message_image_refs(rec.get("message", {}))
        del M.shares[token]
        # Refs still needed elsewhere: other shares or live conversations.
        keep = set()
        for other in M.shares.values():
            keep |= message_image_refs(other.get("message", {}))
        for msgs in M.sessions.values():
            for msg in msgs:
                keep |= message_image_refs(msg)
    save_shares()

    purged = []
    if purge:
        for ref in sorted(own_refs - keep):
            fpath = _ref_file_path(ref)
            if fpath and os.path.exists(fpath):
                try:
                    os.remove(fpath)
                    purged.append(ref)
                except OSError:
                    pass
    return True, {"session_exists": session_exists, "purged": purged}


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
                    "session_exists": rec.get("session_id") in M.sessions_meta,
                    "msg_index": rec.get("msg_index"),
                    "created": rec.get("created"),
                    "preview": preview,
                }
            )
    return sorted(out, key=lambda s: s.get("created") or 0, reverse=True)
