"""SQLite-backed queue for MCP bulk-chat batches.

Batches enter the system as PENDING rows; a single gateway worker claims
them one at a time (PENDING → WORKING → COMPLETED|ERROR), so N concurrent
start_chat_batch calls queue up instead of each spawning its own task.
A batch closes as ERROR only when EVERY item failed; partial failures
still count as COMPLETED (failed_indexes shows which items died).
State survives gateway restarts: WORKING rows found at boot are re-queued
and their non-terminal items resume from scratch.
"""

import json
import os
import sqlite3
import sys
import threading
import time

try:
    from server.config import FILES_DIR
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from server.config import FILES_DIR

BATCHES_DB = os.environ.get(
    "MCP_BATCHES_DB", os.path.join(FILES_DIR, "mcp_batches.db")
)

PENDING = "PENDING"
WORKING = "WORKING"
COMPLETED = "COMPLETED"
ERROR = "ERROR"

_batches_db_lock = threading.RLock()
_ready = False


def _ensure_init():
    global _ready
    if not _ready:
        with _batches_db_lock:
            if not _ready:
                init_batches_db()
                _ready = True


def _db_run(query, params=()):
    _ensure_init()
    with _batches_db_lock:
        conn = sqlite3.connect(BATCHES_DB)
        try:
            cur = conn.execute(query, params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def _db_fetch(query, params=()):
    _ensure_init()
    with _batches_db_lock:
        conn = sqlite3.connect(BATCHES_DB)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(query, params).fetchall()]
        finally:
            conn.close()


def init_batches_db():
    os.makedirs(os.path.dirname(BATCHES_DB), exist_ok=True)
    with _batches_db_lock:
        conn = sqlite3.connect(BATCHES_DB)
        try:
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
                    PRIMARY KEY (batch_id, idx)
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def batch_insert(batch_id, system_prompt, session_id, research, cpu, no_tools, prompts, item_session_ids=None):
    _ensure_init()
    if item_session_ids is None:
        item_session_ids = [""] * len(prompts)
    now = int(time.time())
    with _batches_db_lock:
        conn = sqlite3.connect(BATCHES_DB)
        try:
            conn.execute(
                "INSERT INTO mcp_batches (batch_id, status, system_prompt, session_id,"
                " research, cpu, no_tools, created) VALUES (?,?,?,?,?,?,?,?)",
                (
                    batch_id,
                    PENDING,
                    system_prompt,
                    session_id,
                    int(research),
                    int(cpu),
                    int(no_tools),
                    now,
                ),
            )
            conn.executemany(
                "INSERT INTO mcp_batch_items (batch_id, idx, prompt, session_id) VALUES (?,?,?,?)",
                [(batch_id, i, p, s) for i, (p, s) in enumerate(zip(prompts, item_session_ids))],
            )
            conn.commit()
        finally:
            conn.close()


def batch_get(batch_id):
    rows = _db_fetch("SELECT * FROM mcp_batches WHERE batch_id=?", (batch_id,))
    if not rows:
        return None
    b = rows[0]
    items = []
    for r in _db_fetch(
        "SELECT * FROM mcp_batch_items WHERE batch_id=? ORDER BY idx", (batch_id,)
    ):
        it = {
            "index": r["idx"],
            "prompt": r["prompt"],
            "session_id": r["session_id"],
            "task_id": r["task_id"],
            "status": r["status"],
            "reply": r["reply"],
            "error": r["error"],
        }
        if r["collected_at"] is not None:
            it["collected_at"] = r["collected_at"]
        if r["submitted_result"] is not None:
            try:
                result = json.loads(r["submitted_result"])
            except (ValueError, TypeError):
                result = r["submitted_result"]
            it["submitted"] = {"result": result, "submitted_at": r["submitted_at"]}
        items.append(it)
    return {
        "batch_id": b["batch_id"],
        "status": b["status"],
        "created": b["created"],
        "started_at": b["started_at"],
        "finished_at": b["finished_at"],
        "system_prompt": b["system_prompt"],
        "session_id": b["session_id"],
        "research": bool(b["research"]),
        "cpu": bool(b["cpu"]),
        "no_tools": bool(b["no_tools"]),
        "items": items,
    }


def claim_next_pending():
    """Atomically promote the oldest PENDING batch to WORKING; return its id.
    Ties on the same-second created timestamp fall back to insertion order."""
    _ensure_init()
    with _batches_db_lock:
        conn = sqlite3.connect(BATCHES_DB)
        try:
            row = conn.execute(
                "SELECT batch_id FROM mcp_batches WHERE status=? "
                "ORDER BY created ASC, rowid ASC LIMIT 1",
                (PENDING,),
            ).fetchone()
            if not row:
                return ""
            changed = conn.execute(
                "UPDATE mcp_batches SET status=?, started_at=strftime('%s','now') "
                "WHERE batch_id=? AND status=?",
                (WORKING, row[0], PENDING),
            ).rowcount
            conn.commit()
            return row[0] if changed else ""
        finally:
            conn.close()


def queue_position(batch_id):
    """How many PENDING batches are ahead of this one."""
    rows = _db_fetch(
        "SELECT created, rowid AS rid FROM mcp_batches WHERE batch_id=?",
        (batch_id,),
    )
    if not rows:
        return -1
    ahead = _db_fetch(
        "SELECT COUNT(*) AS n FROM mcp_batches "
        "WHERE status=? AND (created < ? OR (created = ? AND rowid < ?))",
        (PENDING, rows[0]["created"], rows[0]["created"], rows[0]["rid"]),
    )
    return ahead[0]["n"] if ahead else -1


def item_update(batch_id, idx, **fields):
    allowed = {
        "session_id",
        "task_id",
        "status",
        "reply",
        "error",
        "collected_at",
        "submitted_result",
        "submitted_at",
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return 0
    set_clause = ", ".join(f"{k}=?" for k in fields)
    return _db_run(
        f"UPDATE mcp_batch_items SET {set_clause} WHERE batch_id=? AND idx=?",
        (*fields.values(), batch_id, idx),
    )


def finish_batch(batch_id):
    """Close out a WORKING batch: ERROR when every item failed, otherwise
    COMPLETED (partial failures are reported via the items themselves)."""
    rows = _db_fetch(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done "
        "FROM mcp_batch_items WHERE batch_id=?",
        (batch_id,),
    )
    total = rows[0]["total"] if rows else 0
    done = rows[0]["done"] or 0 if rows else 0
    final = ERROR if total and done == 0 else COMPLETED
    return _db_run(
        "UPDATE mcp_batches SET status=?, finished_at=strftime('%s','now') "
        "WHERE batch_id=? AND status='WORKING'",
        (final, batch_id),
    )


def fail_open_items(batch_id, error):
    """Force every non-terminal item of a batch into error state."""
    return _db_run(
        "UPDATE mcp_batch_items SET status='error', error=? "
        "WHERE batch_id=? AND status IN ('queued','running')",
        (str(error)[:300], batch_id),
    )


def requeue_stuck_batches():
    """Boot-time recovery: WORKING batches from a crashed run go back to
    PENDING and their mid-flight items reset to queued for a clean rerun."""
    _db_run(
        "UPDATE mcp_batch_items SET status='queued' "
        "WHERE status='running' AND batch_id IN "
        "(SELECT batch_id FROM mcp_batches WHERE status=?)",
        (WORKING,),
    )
    return _db_run(
        "UPDATE mcp_batches SET status=?, started_at=NULL WHERE status=?",
        (PENDING, WORKING),
    )


def prune_batches(keep=50):
    ids = [
        r["batch_id"]
        for r in _db_fetch(
            "SELECT batch_id FROM mcp_batches ORDER BY created DESC LIMIT -1 OFFSET ?",
            (keep,),
        )
    ]
    for bid in ids:
        _db_run("DELETE FROM mcp_batch_items WHERE batch_id=?", (bid,))
        _db_run("DELETE FROM mcp_batches WHERE batch_id=?", (bid,))
    return len(ids)


def pending_count():
    rows = _db_fetch(
        "SELECT COUNT(*) AS n FROM mcp_batches WHERE status=?", (PENDING,)
    )
    return rows[0]["n"] if rows else 0
