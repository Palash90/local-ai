"""SQLite-backed persistence for MCP single-message chat tasks.

Unlike the in-memory ``tasks`` dict (which is lost on restart), the
``mcp_tasks`` table gives every ``send_chat_message`` call a durable
record so the MCP client can poll for results across restarts and
operators can inspect the full lifecycle from the database.

Storage is the unified local-ai database (server/db.py).
"""

import time

import server.db as db


def _ensure_db():
    db.ensure_init()


def mcp_task_insert(task_id, session_id, message, mode="gpu",
                     research=False, cpu=False, no_tools=False):
    """Write the initial record for an MCP chat task."""
    _ensure_db()
    now = int(time.time())
    print(f"[mcp_db] inserting task {task_id}: session={session_id}, research={research}, cpu={cpu}, no_tools={no_tools}")
    result = db.run(
        "INSERT INTO mcp_tasks"
        " (task_id, session_id, message, status, mode, research, cpu, no_tools,"
        "  created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            session_id,
            message,
            "queued",
            mode,
            int(research),
            int(cpu),
            int(no_tools),
            now,
            now,
        ),
    )
    print(f"[mcp_db] insert completed for task {task_id}, result={result}")


def mcp_task_update(task_id, **fields):
    """Update arbitrary fields on an MCP task record."""
    allowed = {
        "status", "reply", "verification_level", "failure_reason",
        "session_id", "l3_judged_at",
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return 0
    fields["updated_at"] = int(time.time())
    set_clause = ", ".join(f"{k}=?" for k in fields)
    return db.run(
        f"UPDATE mcp_tasks SET {set_clause} WHERE task_id=?",
        (*fields.values(), task_id),
    )


def mcp_task_get(task_id):
    """Fetch a single MCP task record."""
    _ensure_db()
    return db.fetch_one("SELECT * FROM mcp_tasks WHERE task_id=?", (task_id,))


def mcp_task_list(limit=50, status=None):
    """List recent MCP tasks, newest first."""
    _ensure_db()
    if status:
        result = db.fetch(
            "SELECT * FROM mcp_tasks WHERE status=? "
            "ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )
        if result:
            print(f"[mcp_db] found {len(result)} tasks with status={status}")
        return result
    result = db.fetch(
        "SELECT * FROM mcp_tasks ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return result


def mcp_task_delete(task_id):
    """Remove an MCP task record."""
    return db.run("DELETE FROM mcp_tasks WHERE task_id=?", (task_id,))
