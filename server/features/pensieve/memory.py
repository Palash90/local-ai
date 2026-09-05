"""Archived-conversation store for Pensieve.

Deterministic, id-based archive of conversation blocks that were distilled out
of a session's context window. Each block (a fused ``user -> assistant
[tool_calls] + tool results`` chain, never split apart) is stored as one row
keyed by a **per-session sequential** ``block_id``. The model recalls blocks via
``memory_read(memory_ids=[...])`` (direct primary-key lookups, < 1 ms) or a
plain ``LIKE`` keyword search on ``query``.

No embeddings, no vector math, no extra model loads — kept deliberately
lightweight so lookups are instant and the artifact is trivial to unit test.
If richer semantic recall is ever wanted, swap ``keyword_search`` for an
embed-based retriever without touching the public API.

DB lives outside the repo (``~/local-ai-files/pensieve.db`` by default,
override with ``LOCAL_AI_PENSIEVE_DB``), mirroring ``page_cache.db``.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime

PENSIEVE_DB = os.environ.get("LOCAL_AI_PENSIEVE_DB") or os.path.join(
    os.path.expanduser("~/local-ai-files"), "pensieve.db"
)

_lock = threading.RLock()


def _connect():
    conn = sqlite3.connect(PENSIEVE_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _create_tables(conn)
    return conn


def _create_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sid TEXT NOT NULL,
            block_id INTEGER NOT NULL,
            topic TEXT NOT NULL,
            raw TEXT NOT NULL,
            n_msgs INTEGER NOT NULL,
            created_ts TEXT NOT NULL,
            UNIQUE(sid, block_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_memory_sid ON session_memory(sid)"
    )


def store_unit(sid, topic, raw, n_msgs):
    """Persist one archive block and return its per-session ``block_id``."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "SELECT COALESCE(MAX(block_id), 0) + 1 AS nxt FROM session_memory WHERE sid = ?",
                (sid,),
            )
            next_id = cur.fetchone()["nxt"]
            conn.execute(
                "INSERT INTO session_memory (sid, block_id, topic, raw, n_msgs, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    next_id,
                    topic,
                    raw,
                    n_msgs,
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            return next_id
        finally:
            conn.close()


def fetch_block(sid, block_id):
    """Return one archive block as a dict (or None)."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT block_id, topic, raw, n_msgs, created_ts "
                "FROM session_memory WHERE sid = ? AND block_id = ?",
                (sid, block_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def fetch_blocks(sid, block_ids):
    """Fetch blocks preserving ``block_ids`` order; missing ids are reported."""
    found, missing = [], []
    for bid in block_ids:
        block = fetch_block(sid, bid)
        if block is None:
            missing.append(bid)
        else:
            found.append(block)
    return found, missing


def keyword_search(sid, query, limit=5):
    """Deterministic zero-embed lookup: every whitespace-separated term of
    ``query`` must appear (case-insensitive) in the block's ``topic`` or raw
    text. Newest first, bounded by ``limit``."""
    terms = [t for t in (query or "").lower().split() if t]
    if not terms:
        return []
    like = "".join(f" AND (lower(topic) LIKE ? OR lower(raw) LIKE ?)" for _ in terms)
    params = []
    for t in terms:
        params.extend([f"%{t}%", f"%{t}%"])
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                f"SELECT block_id, topic, raw, n_msgs, created_ts "
                f"FROM session_memory WHERE sid = ?{like} "
                f"ORDER BY block_id DESC LIMIT ?",
                (sid, *params, int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def count_units(sid):
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM session_memory WHERE sid = ?", (sid,)
            ).fetchone()
            return row["n"]
        finally:
            conn.close()


def trim_old(sid, max_units=200):
    """Drop the oldest blocks beyond ``max_units`` for a session."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM session_memory WHERE sid = ? AND block_id NOT IN ("
                "SELECT block_id FROM session_memory WHERE sid = ? "
                "ORDER BY block_id DESC LIMIT ?)",
                (sid, sid, int(max_units)),
            )
            conn.commit()
        finally:
            conn.close()


def purge_session(sid):
    """Delete every archived block for a session (session reset/delete)."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM session_memory WHERE sid = ?", (sid,))
            conn.commit()
        finally:
            conn.close()


def _serialize(raw_msgs):
    return json.dumps(raw_msgs, ensure_ascii=False)


def deserialize(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return []