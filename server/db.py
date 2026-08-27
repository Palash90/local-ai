"""Unified SQLite database for local-ai.

All persisted relational state — to-do tasks, theme/combination tracking and
MCP bulk-chat batches — lives in ONE SQLite file so there is a single source of
truth, a single file to back up, and a single connection/lock discipline.

Each feature module (``server.batches_db``, ``server.features.tasks_db``,
``server.features.themes_db``) keeps its own public API but funnels every query
through the shared helpers here: run(), fetch(), fetch_one() and ensure_init().

Existing data in the legacy per-feature DB files (``tasks.db``, ``themes.db``,
``mcp_batches.db``) is migrated into the unified DB once, on first init, and the
legacy files are then renamed to ``*.migrated`` so they are never re-imported.
"""

import os
import sqlite3
import threading

from server.config import APP_DB, FILES_DIR

# Single overridable path; defaults to one file in the shared FILES_DIR.
DB_PATH = os.environ.get("LOCAL_AI_DB", APP_DB)

# One re-entrant lock serializes every connection so cross-feature writes can
# never interleave. RLock (not Lock) lets init() drive the one-time migration
# through the same run()/fetch() helpers without deadlocking.
_db_lock = threading.RLock()
_ready = False
_initializing = False

# Legacy per-feature DB files, migrated once into the unified DB.
_LEGACY_FILES = {
    "tasks": os.path.join(FILES_DIR, "tasks.db"),
    "themes": os.path.join(FILES_DIR, "themes.db"),
    "batches": os.path.join(FILES_DIR, "mcp_batches.db"),
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _create_tables(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            priority TEXT DEFAULT 'medium',
            due_date TEXT,
            session_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            reminder_at TEXT,
            reminded INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_batches (
            batch_id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'PENDING',
            system_prompt TEXT DEFAULT '',
            session_id TEXT DEFAULT '',
            research INTEGER DEFAULT 0,
            cpu INTEGER DEFAULT 0,
            no_tools INTEGER DEFAULT 0,
            created INTEGER NOT NULL,
            started_at INTEGER,
            finished_at INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_batch_items (
            batch_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            prompt TEXT NOT NULL,
            session_id TEXT DEFAULT '',
            task_id TEXT DEFAULT '',
            status TEXT DEFAULT 'queued',
            reply TEXT DEFAULT '',
            error TEXT DEFAULT '',
            collected_at INTEGER,
            submitted_result TEXT,
            submitted_at INTEGER,
            guardrail_blocked INTEGER DEFAULT 0,
            PRIMARY KEY (batch_id, idx)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_tasks (
            task_id TEXT PRIMARY KEY,
            session_id TEXT DEFAULT '',
            message TEXT DEFAULT '',
            status TEXT DEFAULT 'queued',
            reply TEXT DEFAULT '',
            mode TEXT DEFAULT 'gpu',
            research INTEGER DEFAULT 0,
            cpu INTEGER DEFAULT 0,
            no_tools INTEGER DEFAULT 0,
            verification_level TEXT DEFAULT '',
            failure_reason TEXT DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    # Columns may already exist on databases created before these migrations.
    for col, typedef in [
        ("guardrail_blocked", "INTEGER DEFAULT 0"),
        ("verification_level", "TEXT DEFAULT ''"),
        ("failure_reason", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(
                f"ALTER TABLE mcp_batch_items ADD COLUMN {col} {typedef}"
            )
        except sqlite3.OperationalError:
            pass
    # Tracks one-time bookkeeping such as the legacy migration.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _db_meta (key TEXT PRIMARY KEY, value TEXT)"
    )


# ---------------------------------------------------------------------------
# One-time migration of the legacy per-feature DB files
# ---------------------------------------------------------------------------

def _migrate_legacy():
    meta = {r["key"]: r["value"] for r in fetch("SELECT key, value FROM _db_meta")}
    if meta.get("legacy_migrated") == "1":
        return

    for key, path in _LEGACY_FILES.items():
        if not os.path.exists(path) or meta.get(f"legacy_{key}") == "1":
            continue
        try:
            src = sqlite3.connect(path)
            src.row_factory = sqlite3.Row
            if key == "tasks":
                for r in src.execute("SELECT * FROM tasks").fetchall():
                    run(
                        "INSERT OR IGNORE INTO tasks (id, user_id, title, description, "
                        "status, priority, due_date, session_id, created_at, updated_at, "
                        "reminder_at, reminded) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (r["id"], r["user_id"], r["title"], r["description"], r["status"],
                         r["priority"], r["due_date"], r["session_id"], r["created_at"],
                         r["updated_at"], r["reminder_at"], r["reminded"]),
                    )
            elif key == "themes":
                for r in src.execute("SELECT * FROM theme_log").fetchall():
                    run(
                        "INSERT OR IGNORE INTO theme_log (id, scope, user_id, genre, mood, "
                        "role, persona, details, combo_hash, theme, status, created_at, "
                        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (r["id"], r["scope"], r["user_id"], r["genre"], r["mood"], r["role"],
                         r["persona"], r["details"], r["combo_hash"], r["theme"], r["status"],
                         r["created_at"], r["updated_at"]),
                    )
            elif key == "batches":
                for r in src.execute("SELECT * FROM mcp_batches").fetchall():
                    run(
                        "INSERT OR IGNORE INTO mcp_batches (batch_id, status, system_prompt, "
                        "session_id, research, cpu, no_tools, created, started_at, finished_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (r["batch_id"], r["status"], r["system_prompt"], r["session_id"],
                         r["research"], r["cpu"], r["no_tools"], r["created"], r["started_at"],
                         r["finished_at"]),
                    )
                for r in src.execute("SELECT * FROM mcp_batch_items").fetchall():
                    run(
                        "INSERT OR IGNORE INTO mcp_batch_items (batch_id, idx, prompt, "
                        "session_id, task_id, status, reply, error, collected_at, "
                        "submitted_result, submitted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (r["batch_id"], r["idx"], r["prompt"], r["session_id"], r["task_id"],
                         r["status"], r["reply"], r["error"], r["collected_at"],
                         r["submitted_result"], r["submitted_at"]),
                    )
            src.close()
        except sqlite3.OperationalError:
            # Legacy file exists but is missing the expected table — skip it.
            pass
        run("INSERT OR REPLACE INTO _db_meta (key, value) VALUES (?, ?)",
            (f"legacy_{key}", "1"))
        try:
            os.rename(path, path + ".migrated")
        except OSError:
            pass

    run("INSERT OR REPLACE INTO _db_meta (key, value) VALUES (?, ?)",
        ("legacy_migrated", "1"))


# ---------------------------------------------------------------------------
# Init + query helpers (the only public surface the feature modules touch)
# ---------------------------------------------------------------------------

def _do_init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        _create_tables(conn)
        conn.commit()
    finally:
        conn.close()
    _migrate_legacy()


def ensure_init():
    """Idempotently bring the unified DB up (tables + legacy migration)."""
    global _ready, _initializing
    if _ready:
        return
    with _db_lock:
        if _ready:
            return
        if _initializing:
            # Re-entered from run()/fetch() while init is already in progress.
            return
        _initializing = True
        try:
            _do_init()
        finally:
            _initializing = False
            _ready = True


def run(query, params=()):
    """Execute a write query, commit, return rowcount."""
    ensure_init()
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute(query, params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def fetch(query, params=()):
    """Execute a read query, return rows as a list of dicts."""
    ensure_init()
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(query, params).fetchall()]
        finally:
            conn.close()


def fetch_one(query, params=()):
    rows = fetch(query, params)
    return rows[0] if rows else None


def with_connection(func):
    """Run ``func(conn)`` inside the shared lock with a fresh connection.

    Use this for multi-statement transactions (e.g. a SELECT-then-UPDATE that
    must be atomic) where the single-shot run()/fetch() helpers are not enough.
    """
    ensure_init()
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            return func(conn)
        finally:
            conn.close()
