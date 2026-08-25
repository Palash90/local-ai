"""SQLite-backed to-do tasks used by the ``manage_tasks`` tool and /api/tasks.

Storage is the unified local-ai database (server/db.py): the ``tasks`` table
lives alongside theme_log and the MCP batch tables in one file.
"""

import json
import uuid
from datetime import datetime

import server.db as db


def _init_tasks_db():
    """Ensure the unified DB (and thus the tasks table) exists."""
    db.ensure_init()


# Thin wrappers kept for back-compat with callers that imported the helpers.
def _db_run(query, params=()):
    return db.run(query, params)


def _db_fetch(query, params=()):
    return db.fetch(query, params)


def _db_fetch_one(query, params=()):
    return db.fetch_one(query, params)


def task_create(
    user_id,
    title,
    description="",
    priority="medium",
    due_date=None,
    session_id=None,
    reminder_at=None,
):
    tid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    db.run(
        "INSERT INTO tasks (id, user_id, title, description, status, priority, due_date, session_id, created_at, updated_at, reminder_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            tid,
            user_id,
            title,
            description,
            "pending",
            priority,
            due_date,
            session_id,
            now,
            now,
            reminder_at,
        ),
    )
    return db.fetch_one("SELECT * FROM tasks WHERE id=?", (tid,))


def task_update(tid, user_id, **kwargs):
    fields = {k: v for k, v in kwargs.items() if v is not None}
    if not fields:
        return None
    fields["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [tid, user_id]
    db.run(f"UPDATE tasks SET {set_clause} WHERE id=? AND user_id=?", vals)
    return db.fetch_one("SELECT * FROM tasks WHERE id=?", (tid,))


def task_complete(tid, user_id):
    now = datetime.now().isoformat()
    db.run(
        "UPDATE tasks SET status='completed', updated_at=? WHERE id=? AND user_id=?",
        (now, tid, user_id),
    )
    return db.fetch_one("SELECT * FROM tasks WHERE id=?", (tid,))


def task_delete(tid, user_id):
    return db.run("DELETE FROM tasks WHERE id=? AND user_id=?", (tid, user_id))


def task_list(user_id, status=None):
    if status:
        return db.fetch(
            "SELECT * FROM tasks WHERE user_id=? AND status=? ORDER BY due_date IS NULL, due_date ASC, created_at DESC",
            (user_id, status),
        )
    return db.fetch(
        "SELECT * FROM tasks WHERE user_id=? ORDER BY due_date IS NULL, due_date ASC, created_at DESC",
        (user_id,),
    )


def task_get(tid, user_id):
    return db.fetch_one(
        "SELECT * FROM tasks WHERE id=? AND user_id=?", (tid, user_id)
    )


def handle_task_tool(user_id, args):
    op = args.get("operation", "")
    if op == "create":
        if not args.get("title"):
            return json.dumps({"ok": False, "error": "Missing required argument: title"})
        t = task_create(
            user_id,
            args["title"],
            args.get("description", ""),
            args.get("priority", "medium"),
            args.get("due_date"),
            args.get("session_id"),
            args.get("reminder_at"),
        )
        return json.dumps({"ok": True, "task": t})
    elif op in ("update", "complete", "delete", "get"):
        tid = args.get("task_id")
        if not tid:
            return json.dumps(
                {"ok": False, "error": f"Missing required argument: task_id"}
            )
        if op == "update":
            t = task_update(
                tid,
                user_id,
                title=args.get("title"),
                description=args.get("description"),
                priority=args.get("priority"),
                status=args.get("status"),
                due_date=args.get("due_date"),
                reminder_at=args.get("reminder_at"),
            )
            if t:
                return json.dumps({"ok": True, "task": t})
            return json.dumps({"ok": False, "error": "Task not found"})
        elif op == "complete":
            t = task_complete(tid, user_id)
            if t:
                return json.dumps({"ok": True, "task": t})
            return json.dumps({"ok": False, "error": "Task not found"})
        elif op == "delete":
            task_delete(tid, user_id)
            return json.dumps({"ok": True})
        else:
            t = task_get(tid, user_id)
            if t:
                return json.dumps({"ok": True, "task": t})
            return json.dumps({"ok": False, "error": "Task not found"})
    elif op == "list":
        tasks = task_list(user_id, args.get("status"))
        return json.dumps({"ok": True, "tasks": tasks})
    return json.dumps({"ok": False, "error": f"Unknown operation: {op}"})
