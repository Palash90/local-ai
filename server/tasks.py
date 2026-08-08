"""SQLite-backed user task management (create/update/complete/delete/list)
and the `task` tool handler. Extracted from chat-webui.py.

Every function takes `db_path` explicitly (rather than importing a fixed
TASKS_DB) so callers — including tests that point at a scratch DB — control
which database is used.
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime

_tasks_db_lock = threading.Lock()


def init_tasks_db(db_path):
    with _tasks_db_lock:
        conn = sqlite3.connect(db_path)
        conn.execute("""
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
        """)
        conn.commit()
        conn.close()


def db_run(db_path, query, params=()):
    with _tasks_db_lock:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(query, params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def db_fetch(db_path, query, params=()):
    with _tasks_db_lock:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(query, params)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def db_fetch_one(db_path, query, params=()):
    rows = db_fetch(db_path, query, params)
    return rows[0] if rows else None


def task_create(db_path, user_id, title, description="", priority="medium", due_date=None, session_id=None, reminder_at=None):
    tid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    db_run(
        db_path,
        "INSERT INTO tasks (id, user_id, title, description, status, priority, due_date, session_id, created_at, updated_at, reminder_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (tid, user_id, title, description, "pending", priority, due_date, session_id, now, now, reminder_at),
    )
    return db_fetch_one(db_path, "SELECT * FROM tasks WHERE id=?", (tid,))


def task_update(db_path, tid, user_id, **kwargs):
    fields = {k: v for k, v in kwargs.items() if v is not None}
    if not fields:
        return None
    fields["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [tid, user_id]
    db_run(db_path, f"UPDATE tasks SET {set_clause} WHERE id=? AND user_id=?", vals)
    return db_fetch_one(db_path, "SELECT * FROM tasks WHERE id=?", (tid,))


def task_complete(db_path, tid, user_id):
    now = datetime.now().isoformat()
    db_run(db_path, "UPDATE tasks SET status='completed', updated_at=? WHERE id=? AND user_id=?", (now, tid, user_id))
    return db_fetch_one(db_path, "SELECT * FROM tasks WHERE id=?", (tid,))


def task_delete(db_path, tid, user_id):
    return db_run(db_path, "DELETE FROM tasks WHERE id=? AND user_id=?", (tid, user_id))


def task_list(db_path, user_id, status=None):
    if status:
        return db_fetch(db_path, "SELECT * FROM tasks WHERE user_id=? AND status=? ORDER BY due_date IS NULL, due_date ASC, created_at DESC", (user_id, status))
    return db_fetch(db_path, "SELECT * FROM tasks WHERE user_id=? ORDER BY due_date IS NULL, due_date ASC, created_at DESC", (user_id,))


def task_get(db_path, tid, user_id):
    return db_fetch_one(db_path, "SELECT * FROM tasks WHERE id=? AND user_id=?", (tid, user_id))


def due_reminders(db_path, now_iso):
    """Tasks whose reminder has fired but hasn't been announced yet."""
    return db_fetch(
        db_path,
        "SELECT * FROM tasks WHERE reminder_at IS NOT NULL AND reminder_at <= ? AND reminded=0 AND status NOT IN ('completed','cancelled')",
        (now_iso,),
    )


def mark_reminded(db_path, tid):
    db_run(db_path, "UPDATE tasks SET reminded=1 WHERE id=?", (tid,))


def _not_found():
    return json.dumps({"ok": False, "error": "Task not found"})


def handle_task_tool(db_path, user_id, args):
    op = args.get("operation", "")
    if op == "create":
        if not args.get("title"):
            return json.dumps({"ok": False, "error": "Missing required argument: title"})
        t = task_create(db_path, user_id, args["title"], args.get("description", ""), args.get("priority", "medium"), args.get("due_date"), args.get("session_id"), args.get("reminder_at"))
        return json.dumps({"ok": True, "task": t})
    elif op in ("update", "complete", "delete", "get"):
        tid = args.get("task_id")
        if not tid:
            return json.dumps({"ok": False, "error": "Missing required argument: task_id"})
        if op == "update":
            t = task_update(db_path, tid, user_id, title=args.get("title"), description=args.get("description"), priority=args.get("priority"), status=args.get("status"), due_date=args.get("due_date"), reminder_at=args.get("reminder_at"))
            return json.dumps({"ok": True, "task": t}) if t else _not_found()
        elif op == "complete":
            t = task_complete(db_path, tid, user_id)
            return json.dumps({"ok": True, "task": t}) if t else _not_found()
        elif op == "delete":
            task_delete(db_path, tid, user_id)
            return json.dumps({"ok": True})
        else:
            t = task_get(db_path, tid, user_id)
            return json.dumps({"ok": True, "task": t}) if t else _not_found()
    elif op == "list":
        tasks = task_list(db_path, user_id, args.get("status"))
        return json.dumps({"ok": True, "tasks": tasks})
    return json.dumps({"ok": False, "error": f"Unknown operation: {op}"})