"""tasks_db: CRUD round-trip, user isolation, reminder normalization (tmp DB)."""

import sqlite3
from datetime import datetime

import pytest

import server.db as db
from server.features import tasks_db as T


@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    p = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", p)
    conn = sqlite3.connect(p)
    try:
        db._create_tables(conn)
        conn.commit()
    finally:
        conn.close()
    return p


def test_crud_round_trip(tmpdb):
    row = T.task_create("u1", "Buy milk", priority="high")
    assert row["title"] == "Buy milk" and row["status"] == "pending"
    tid = row["id"]
    assert T.task_get(tid, "u1")["id"] == tid
    assert len(T.task_list("u1")) == 1
    T.task_update(tid, "u1", description="2% milk")
    assert T.task_get(tid, "u1")["description"] == "2% milk"
    T.task_complete(tid, "u1")
    assert T.task_get(tid, "u1")["status"] == "completed"
    assert T.task_list("u1", status="completed")[0]["id"] == tid
    assert T.task_list("u1", status="pending") == []
    T.task_delete(tid, "u1")
    assert T.task_get(tid, "u1") is None


def test_user_isolation(tmpdb):
    row = T.task_create("alice", "secret")
    assert T.task_get(row["id"], "bob") is None
    assert T.task_list("bob") == []
    T.task_complete(row["id"], "bob")  # must not touch alice's row
    assert T.task_get(row["id"], "alice")["status"] == "pending"
    T.task_delete(row["id"], "bob")  # must not delete alice's row
    assert T.task_get(row["id"], "alice") is not None


def test_reminder_normalization():
    assert T.normalize_reminder_at(None) is None
    assert T.normalize_reminder_at("2026-09-13T12:35:00") == "2026-09-13T12:35:00"
    # Zulu input converts to server-local (code is correct; only shape asserted)
    z = T.normalize_reminder_at("2026-09-13T12:35:00Z")
    assert len(z) == 19 and z[10] == "T"
    assert T.normalize_reminder_at(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"
    today = datetime.now().strftime("%Y-%m-%d")
    assert T.normalize_reminder_at("12:35 pm") == f"{today}T12:35:00"
    assert T.normalize_reminder_at("12:35 am") == f"{today}T00:35:00"
    assert T.normalize_reminder_at("9:05").endswith("09:05:00")
    with pytest.raises(ValueError):
        T.normalize_reminder_at("not a time")
    with pytest.raises(ValueError):
        T.normalize_reminder_at("")
    with pytest.raises(ValueError):
        T.normalize_reminder_at("25:99")


def test_reminder_stored_normalized(tmpdb):
    row = T.task_create("u1", "t", reminder_at="2:30 pm")
    assert row["reminder_at"] is not None and "T" in row["reminder_at"]
