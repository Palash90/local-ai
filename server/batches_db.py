"""SQLite-backed queue for MCP bulk-chat batches.

Batches enter the system as PENDING rows; a single gateway worker claims
them one at a time (PENDING → WORKING → COMPLETED|ERROR), so N concurrent
start_chat_batch calls queue up instead of each spawning its own task.
A batch closes as ERROR only when EVERY item failed; partial failures
still count as COMPLETED (failed_indexes shows which items died).
State survives gateway restarts: WORKING rows found at boot are re-queued
and their non-terminal items resume from scratch.

Storage is the unified local-ai database (server/db.py): the ``mcp_batches``
and ``mcp_batch_items`` tables live alongside tasks and theme_log in one file.
"""

import json
import time

import server.db as db

PENDING = "PENDING"
WORKING = "WORKING"
COMPLETED = "COMPLETED"
ERROR = "ERROR"


def init_batches_db():
    """Ensure the unified DB (and thus the batch tables) exists."""
    db.ensure_init()


def batch_insert(batch_id, system_prompt, session_id, research, cpu, no_tools, prompts, item_session_ids=None):
    if item_session_ids is None:
        item_session_ids = [""] * len(prompts)
    now = int(time.time())

    def _tx(conn):
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

    db.with_connection(_tx)


def batch_get(batch_id):
    b = db.fetch_one("SELECT * FROM mcp_batches WHERE batch_id=?", (batch_id,))
    if not b:
        return None
    items = []
    for r in db.fetch(
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
            "guardrail_blocked": r["guardrail_blocked"],
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
    def _tx(conn):
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

    return db.with_connection(_tx)


def queue_position(batch_id):
    """How many PENDING batches are ahead of this one."""
    rows = db.fetch(
        "SELECT created, rowid AS rid FROM mcp_batches WHERE batch_id=?",
        (batch_id,),
    )
    if not rows:
        return -1
    ahead = db.fetch(
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
        "guardrail_blocked",
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return 0
    set_clause = ", ".join(f"{k}=?" for k in fields)
    return db.run(
        f"UPDATE mcp_batch_items SET {set_clause} WHERE batch_id=? AND idx=?",
        (*fields.values(), batch_id, idx),
    )


def finish_batch(batch_id):
    """Close out a WORKING batch: ERROR when every item failed, otherwise
    COMPLETED (partial failures are reported via the items themselves)."""
    rows = db.fetch(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done "
        "FROM mcp_batch_items WHERE batch_id=?",
        (batch_id,),
    )
    total = rows[0]["total"] if rows else 0
    done = rows[0]["done"] or 0 if rows else 0
    final = ERROR if total and done == 0 else COMPLETED
    return db.run(
        "UPDATE mcp_batches SET status=?, finished_at=strftime('%s','now') "
        "WHERE batch_id=? AND status='WORKING'",
        (final, batch_id),
    )


def fail_open_items(batch_id, error):
    """Force every non-terminal item of a batch into error state."""
    return db.run(
        "UPDATE mcp_batch_items SET status='error', error=? "
        "WHERE batch_id=? AND status IN ('queued','running')",
        (str(error)[:300], batch_id),
    )


def requeue_stuck_batches():
    """Boot-time recovery: WORKING batches from a crashed run go back to
    PENDING and their mid-flight items reset to queued for a clean rerun."""
    db.run(
        "UPDATE mcp_batch_items SET status='queued' "
        "WHERE status='running' AND batch_id IN "
        "(SELECT batch_id FROM mcp_batches WHERE status=?)",
        (WORKING,),
    )
    return db.run(
        "UPDATE mcp_batches SET status=?, started_at=NULL WHERE status=?",
        (PENDING, WORKING),
    )


def prune_batches(keep=50):
    ids = [
        r["batch_id"]
        for r in db.fetch(
            "SELECT batch_id FROM mcp_batches ORDER BY created DESC LIMIT -1 OFFSET ?",
            (keep,),
        )
    ]
    for bid in ids:
        db.run("DELETE FROM mcp_batch_items WHERE batch_id=?", (bid,))
        db.run("DELETE FROM mcp_batches WHERE batch_id=?", (bid,))
    return len(ids)


def pending_count():
    rows = db.fetch(
        "SELECT COUNT(*) AS n FROM mcp_batches WHERE status=?", (PENDING,)
    )
    return rows[0]["n"] if rows else 0
