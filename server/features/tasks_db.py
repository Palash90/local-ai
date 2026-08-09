"""SQLite-backed to-do tasks used by the ``manage_tasks`` tool and /api/tasks."""

import json
import sqlite3
import threading
import uuid
from datetime import datetime

from server.features.state import M

_tasks_db_lock = threading.Lock()


def _init_tasks_db():
    with _tasks_db_lock:
        conn = sqlite3.connect(M.TASKS_DB)
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
        conn.commit()
        conn.close()


def _db_run(query, params=()):
    with _tasks_db_lock:
        conn = sqlite3.connect(M.TASKS_DB)
        try:
            cur = conn.execute(query, params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def _db_fetch(query, params=()):
    with _tasks_db_lock:
        conn = sqlite3.connect(M.TASKS_DB)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(query, params)
            rows = [dict(r) for r in cur.fetchall()]
            return rows
        finally:
            conn.close()


def _db_fetch_one(query, params=()):
    rows = M._db_fetch(query, params)
    return rows[0] if rows else None


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
    M._db_run(
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
    return M._db_fetch_one("SELECT * FROM tasks WHERE id=?", (tid,))


def task_update(tid, user_id, **kwargs):
    fields = {k: v for k, v in kwargs.items() if v is not None}
    if not fields:
        return None
    fields["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [tid, user_id]
    M._db_run(f"UPDATE tasks SET {set_clause} WHERE id=? AND user_id=?", vals)
    return M._db_fetch_one("SELECT * FROM tasks WHERE id=?", (tid,))


def task_complete(tid, user_id):
    now = datetime.now().isoformat()
    M._db_run(
        "UPDATE tasks SET status='completed', updated_at=? WHERE id=? AND user_id=?",
        (now, tid, user_id),
    )
    return M._db_fetch_one("SELECT * FROM tasks WHERE id=?", (tid,))


def task_delete(tid, user_id):
    return M._db_run("DELETE FROM tasks WHERE id=? AND user_id=?", (tid, user_id))


def task_list(user_id, status=None):
    if status:
        return M._db_fetch(
            "SELECT * FROM tasks WHERE user_id=? AND status=? ORDER BY due_date IS NULL, due_date ASC, created_at DESC",
            (user_id, status),
        )
    return M._db_fetch(
        "SELECT * FROM tasks WHERE user_id=? ORDER BY due_date IS NULL, due_date ASC, created_at DESC",
        (user_id,),
    )


def task_get(tid, user_id):
    return M._db_fetch_one(
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
