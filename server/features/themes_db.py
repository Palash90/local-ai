"""SQLite-backed theme & combination tracker (``track_theme`` tool, /api/themes).

A dedicated ledger that guarantees creative variety across generated content
without polluting per-user contexts or the to-do (``manage_tasks``) system.

Two dimensions are tracked:

* ``scope`` — the user whose content is being generated. Records inside one
  scope are that user's *combination* history: every already-used combination
  of detail fields + mood + genre + role + persona.
* global   — the union of ALL scopes ("vastly keep track of all the users"),
  used to avoid cross-user repetition and to see overall output.

Within a self-chat window every participant (kolpo, kaya, editor, moderator)
shares a single scope (``"self-chat"``) so the theme stays coordinated between
the users of that window while regular per-user chats stay isolated.
"""

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime

from server.features.state import M

_themes_db_lock = threading.Lock()


def _db_run(query, params=()):
    with _themes_db_lock:
        conn = sqlite3.connect(M.THEMES_DB)
        try:
            cur = conn.execute(query, params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def _db_fetch(query, params=()):
    with _themes_db_lock:
        conn = sqlite3.connect(M.THEMES_DB)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(query, params)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def _db_fetch_one(query, params=()):
    rows = _db_fetch(query, params)
    return rows[0] if rows else None


def _init_themes_db():
    _db_run(
        """
        CREATE TABLE IF NOT EXISTS theme_log (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            user_id TEXT DEFAULT '',
            genre TEXT DEFAULT '',
            mood TEXT DEFAULT '',
            role TEXT DEFAULT '',
            persona TEXT DEFAULT '',
            details TEXT DEFAULT '{}',
            combo_hash TEXT NOT NULL,
            theme TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(scope, combo_hash)
        )
        """
    )


def combo_hash(genre, mood, role, persona, details=None, level="round"):
    """Canonical fingerprint of a combination.

    The combination mixes every detail field with mood, genre, role and
    persona, so reusing ANY of them with the same set counts as a duplicate.
    ``level`` separates round-scoped records from per-turn records so they
    never falsely collide.
    """
    payload = {
        "genre": genre or "",
        "mood": mood or "",
        "role": role or "",
        "persona": persona or "",
        "details": details or {},
        "level": level or "round",
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def theme_log_create(
    scope,
    user_id="",
    genre="",
    mood="",
    role="",
    persona="",
    details=None,
    theme="",
    status="active",
    level="round",
):
    if isinstance(details, dict):
        details = json.dumps(details, ensure_ascii=False, sort_keys=True)
    if details in (None, ""):
        details = "{}"
    try:
        details_obj = json.loads(details)
    except (TypeError, ValueError):
        details_obj = {}
    h = combo_hash(genre, mood, role, persona, details_obj, level)

    existing = _db_fetch_one(
        "SELECT * FROM theme_log WHERE scope=? AND combo_hash=?",
        (scope, h),
    )
    if existing:
        updates = {}
        if theme and theme != existing.get("theme"):
            updates["theme"] = theme
        if status and existing.get("status") != status:
            updates["status"] = status
        if updates:
            updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
            set_clause = ", ".join(f"{k}=?" for k in updates)
            _db_run(
                f"UPDATE theme_log SET {set_clause} WHERE id=?",
                tuple(updates.values()) + (existing["id"],),
            )
            existing = _db_fetch_one(
                "SELECT * FROM theme_log WHERE id=?", (existing["id"],)
            )
        return existing, True

    tid = str(uuid.uuid4())
    now = datetime.now().isoformat(timespec="seconds")
    _db_run(
        "INSERT INTO theme_log (id, scope, user_id, genre, mood, role, persona, details, combo_hash, theme, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            tid,
            scope,
            user_id or "",
            genre or "",
            mood or "",
            role or "",
            persona or "",
            details,
            h,
            theme or "",
            status or "active",
            now,
            now,
        ),
    )
    return _db_fetch_one("SELECT * FROM theme_log WHERE id=?", (tid,)), False


def theme_log_complete(tid):
    existing = _db_fetch_one("SELECT * FROM theme_log WHERE id=?", (tid,))
    if not existing:
        return None
    _db_run(
        "UPDATE theme_log SET status='completed', updated_at=? WHERE id=?",
        (datetime.now().isoformat(timespec="seconds"), tid),
    )
    return _db_fetch_one("SELECT * FROM theme_log WHERE id=?", (tid,))


def theme_log_list(scope=None, all_scopes=False, status=None, limit=50):
    where = []
    params = []
    if not all_scopes and scope:
        where.append("scope=?")
        params.append(scope)
    if status:
        where.append("status=?")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 50
    params.append(limit)
    return _db_fetch(
        f"SELECT * FROM theme_log {clause} ORDER BY created_at DESC LIMIT ?",
        params,
    )


def theme_log_check(scope, genre="", mood="", role="", persona="", details=None, level="round"):
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except (TypeError, ValueError):
            details = {}
    h = combo_hash(genre, mood, role, persona, details or {}, level)
    return _db_fetch_one(
        "SELECT * FROM theme_log WHERE scope=? AND combo_hash=?",
        (scope, h),
    )


def theme_log_stats(scope=None, all_scopes=False):
    where = []
    params = []
    if not all_scopes and scope:
        where.append("scope=?")
        params.append(scope)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = _db_fetch(
        f"SELECT scope, status, COUNT(*) AS count FROM theme_log {clause} GROUP BY scope, status ORDER BY scope",
        params,
    )
    totals = {}
    for r in rows:
        totals[r["scope"]] = totals.get(r["scope"], {})
        totals[r["scope"]][r["status"]] = r["count"]
    return {
        "total": sum(r["count"] for r in rows),
        "per_scope": totals,
    }


def handle_theme_tool(user_id, args):
    op = args.get("operation", "")
    scope = (args.get("scope") or "").strip() or None

    if op == "log":
        if not scope:
            return json.dumps({"ok": False, "error": "Missing required argument: scope"})
        record, duplicate = theme_log_create(
            scope,
            user_id=user_id,
            genre=args.get("genre"),
            mood=args.get("mood"),
            role=args.get("role"),
            persona=args.get("persona"),
            details=args.get("details"),
            theme=args.get("theme"),
            status=args.get("status"),
            level=args.get("level", "round"),
        )
        return json.dumps({"ok": True, "duplicate": duplicate, "theme": record})
    elif op == "complete":
        tid = args.get("theme_id")
        if not tid:
            return json.dumps({"ok": False, "error": "Missing required argument: theme_id"})
        record = theme_log_complete(tid)
        if record:
            return json.dumps({"ok": True, "theme": record})
        return json.dumps({"ok": False, "error": "Theme not found"})
    elif op == "check":
        if not scope:
            return json.dumps({"ok": False, "error": "Missing required argument: scope"})
        record = theme_log_check(
            scope,
            genre=args.get("genre"),
            mood=args.get("mood"),
            role=args.get("role"),
            persona=args.get("persona"),
            details=args.get("details"),
            level=args.get("level", "round"),
        )
        return json.dumps(
            {
                "ok": True,
                "used": bool(record),
                "theme": record,
            }
        )
    elif op == "list":
        records = theme_log_list(
            scope=scope,
            all_scopes=bool(args.get("global")),
            status=args.get("status"),
            limit=args.get("limit", 50),
        )
        return json.dumps({"ok": True, "themes": records})
    elif op == "stats":
        return json.dumps(
            {"ok": True, **theme_log_stats(scope=scope, all_scopes=bool(args.get("global")))}
        )
    return json.dumps({"ok": False, "error": f"Unknown operation: {op}"})
