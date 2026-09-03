r"""Persistent page + search-result cache (SQLite + sqlite-vec).

Stores every ``fetch_page`` result and every ``web_search`` response in a SQLite
database **outside the repo** (``~/local-ai-files/page_cache.db`` by default,
override with ``LOCAL_AI_PAGE_CACHE``) so repeated fetches become instant cache
hits instead of new outbound requests — the same goal as the web_search pacing,
but eliminating the request entirely on re-fetch.

Two layers:

* **Keyed cache** — pages by canonical URL, searches by normalized query, each
  with a TTL. Time-sensitive queries (``today``, ``news``, ``\d{4}`` ...) get a
  short TTL; everything else a long one.
* **Vector layer** — each cached page keeps a cosine-embedding of its
  title+text in a ``vec0`` virtual table so past fetches can be recalled
  semantically (``page_semantic``). A pluggable embedding provider defaults to
  the local nomic embedding llama-server (``http://localhost:8084``); if no
  provider/model is available the cache degrades to the keyed layer only
  (vectors are simply not written or queried).

Every operation opens a short-lived connection under a module lock; pages/searches
are only ever served from the DB if they were validated by the calling path
(the SSRF guard + successful fetch) before being written, so serving a cached
entry never re-opens a safety hole.
"""

import json
import os
import re
import sqlite3
import threading
import time

from server.config import FILES_DIR

CACHE_DIR = os.environ.get("LOCAL_AI_PAGE_CACHE")
if not CACHE_DIR:
    from server.config import PAGE_CACHE_DB

    CACHE_DIR = PAGE_CACHE_DB

EMBED_URL = os.environ.get("LOCAL_AI_EMBED_URL", "http://localhost:8084").rstrip("/")
EMBED_MODEL = os.environ.get("LOCAL_AI_EMBED_MODEL", "nomic-embed-text-v1.5.Q8_0")
EMBED_DIM = int(os.environ.get("LOCAL_AI_EMBED_DIM", "768"))
# Page text handed to the embedder is truncated to this many characters
# BEFORE the request. nomic-embed-text-v1.5 has a native 2048-token window and
# the server rejects longer inputs; 3000 chars stays inside it even for the
# densest real-world text (~1.5 chars/token for CJK/code), while ordinary
# prose (~4 chars/token) keeps ~750 tokens of the article body — plenty of
# retrieval signal for semantic recall.
EMBED_BUDGET_CHARS = 3000

SEARCH_FRESH_TTL = 300
SEARCH_STALE_TTL = 30 * 24 * 3600
PAGE_TTL = 30 * 24 * 3600

_FRESH_QUERY_RE = re.compile(
    r"\b(?:today|tonight|yesterday|now|current(?:ly)?|latest|breaking|"
    r"recent(?:ly)?|news|headlines?|live|updates?|this\s+(?:week|month|year)|"
    r"\d{4})\b",
    re.IGNORECASE,
)


def regex_ttl(query):
    """Regex-only TTL estimate (seconds): short for time-sensitive queries.

    Serves as the default/fallback when the LLM TTL classifier is unavailable.
    """
    return SEARCH_FRESH_TTL if _FRESH_QUERY_RE.search(query or "") else SEARCH_STALE_TTL

_EMBED_BUDGET_URL = EMBED_URL + "/embedding"
_lock = threading.RLock()
_VEC_TABLE = f"page_vec_{EMBED_DIM}"


def _canon(url):
    """Normalize a URL into the cache key: strip the fragment only.

    The path/query keep their exact casing/layout — two URLs that differ there
    fetch different content. The SSRF guard and fetch already ran before a row
    is ever written, so the key needs no path rewriting.
    """
    if not url:
        return None
    try:
        return url.split("#", 1)[0]
    except (TypeError, AttributeError):
        return None


def _db_path():
    if CACHE_DIR:
        return CACHE_DIR
    return os.path.join(FILES_DIR, "page_cache.db")


def _connect():
    conn = sqlite3.connect(_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.enable_load_extension(True)
        import sqlite_vec  # noqa: PLC0415

        sqlite_vec.load(conn)
    except Exception as e:
        print(f"[page_cache] sqlite-vec unavailable; vector layer disabled ({e})")
    _create_tables(conn)
    return conn


def _create_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pages (
            url TEXT PRIMARY KEY,
            final_url TEXT NOT NULL,
            title TEXT DEFAULT '',
            text TEXT NOT NULL,
            doc_type TEXT DEFAULT 'web',
            page_images TEXT DEFAULT '[]',
            fetched_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS searches (
            norm_query TEXT PRIMARY KEY,
            query TEXT NOT NULL,
            payload TEXT NOT NULL,
            fresh INTEGER NOT NULL,
            fetched_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {_VEC_TABLE}
        USING vec0(embedding float[{EMBED_DIM}] distance_metric=cosine,
                   url text primary key)
        """
    )


def _request_embeddings(texts):
    """Embed ``texts`` via the local llama.cpp ``/embedding`` endpoint.

    llama-server's ``--embedding`` mode answers ``POST /embedding`` with
    ``[{"index":0,"embedding":[[...]]}]`` — index `i` corresponds to the i-th
    item of a batched ``input`` array. We accept one text at a time to keep the
    parse simple and robust. Returns a list of vectors (one per text) or None
    on any failure so callers degrade to the keyed cache.
    """
    import requests

    vectors = []
    for text in texts:
        try:
            r = requests.post(
                _EMBED_BUDGET_URL,
                json={"model": EMBED_MODEL, "input": [text]},
                timeout=2,
            )
            r.raise_for_status()
            rows = r.json()
            embedding = rows[0]["embedding"][0]
            if not isinstance(embedding, list) or not embedding:
                print(
                    f"[page_cache] unexpected embedding shape for '{EMBED_MODEL}': "
                    f"{type(embedding)}"
                )
                return None
            vectors.append(embedding)
        except Exception as e:
            print(f"[page_cache] embeddings unavailable: {e}")
            return None
    return vectors


def embed_texts(texts):
    """Public embedding hook; returns a list of vectors or None on failure.

    Kept separate so callers (or tests) can inject a stub provider.
    """
    fn = _request_embeddings
    if not callable(fn):
        return None
    return fn(texts)


def _store_vec(conn, url, title, text):
    fn = _request_embeddings
    if not callable(fn):
        return
    vecs = fn([f"{title} :: {text}"[:EMBED_BUDGET_CHARS]])
    if not vecs:
        return
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO {_VEC_TABLE}(url, embedding) VALUES (?, ?)",
            (url, json.dumps(vecs[0])),
        )
    except Exception as e:
        print(f"[page_cache] vector store failed: {e}")


def _purge_expired(conn, now):
    """Drop expired rows so the DB doesn't grow without bound."""
    conn.execute("DELETE FROM pages WHERE expires_at <= ?", (now,))
    conn.execute("DELETE FROM searches WHERE expires_at <= ?", (now,))


def page_put(url, final_url, title, text, doc_type="web", page_images=None, ttl=PAGE_TTL):
    """Store a successfully fetched page under its canonical request URL."""
    key = _canon(url)
    if not key:
        return
    now = time.time()
    images = json.dumps(page_images or [])
    with _lock:
        conn = _connect()
        try:
            _purge_expired(conn, now)
            conn.execute(
                """
                INSERT OR REPLACE INTO pages
                    (url, final_url, title, text, doc_type, page_images,
                     fetched_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (key, final_url, title, text, doc_type, images, now, now + ttl),
            )
            _store_vec(conn, key, title, text)
            conn.commit()
        finally:
            conn.close()


def page_get(url):
    """Return the stored page row for ``url`` if the entry is fresh, else None."""
    key = _canon(url)
    if not key:
        return None
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM pages WHERE url = ? AND expires_at > ?",
                (key, now),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    return {
        "url": row["url"],
        "final_url": row["final_url"],
        "title": row["title"],
        "text": row["text"],
        "doc_type": row["doc_type"],
        "page_images": json.loads(row["page_images"] or "[]"),
        "fetched_at": row["fetched_at"],
    }


def _backfill_vecs(conn, limit=4):
    """Re-vectorize cached pages whose embedding is missing.

    A transient embed-server outage (RAM starvation, restart race) used to
    leave a page cached but semantically blind for its whole TTL — the store
    failure degraded gracefully but was never retried. Heal lazily instead of
    with a startup pass: the vector layer's only consumer (``page_semantic``)
    backfills a bounded batch of orphans before querying, so coverage recovers
    as semantic recall is actually used. Returns the number healed.
    """
    try:
        rows = conn.execute(
            f"SELECT p.url, p.title, p.text FROM pages p "
            f"LEFT JOIN {_VEC_TABLE} v ON v.url = p.url "
            "WHERE v.url IS NULL LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception as e:
        print(f"[page_cache] vector backfill lookup failed: {e}")
        return 0
    if not rows:
        return 0
    fixed = 0
    for r in rows:
        try:
            vecs = _request_embeddings(
                [f"{r['title']} :: {r['text']}"[:EMBED_BUDGET_CHARS]]
            )
            if not vecs:
                break  # provider down — stop, don't hammer it
            conn.execute(
                f"INSERT OR REPLACE INTO {_VEC_TABLE}(url, embedding) VALUES (?, ?)",
                (r["url"], json.dumps(vecs[0])),
            )
            fixed += 1
        except Exception as e:
            print(f"[page_cache] vector backfill failed for {r['url']}: {e}")
            break
    if fixed:
        conn.commit()
        print(
            f"[page_cache] backfilled {fixed}/{len(rows)} missing page vector(s)"
        )
    return fixed


def page_semantic(query, k=5, min_score=0.6):
    """Return cached pages whose embedding is close to ``query``.

    Cosine distance in vec0 maps to similarity as ``1 - distance``; results
    below ``min_score`` are dropped. Empty when the vector layer is off or no
    provider/model is available.
    """
    if not query:
        return []
    vecs = _request_embeddings([query])
    if not vecs:
        return []
    with _lock:
        conn = _connect()
        try:
            # Heal pages cached during an embed outage before querying.
            _backfill_vecs(conn)
            rows = conn.execute(
                f"SELECT url, distance FROM {_VEC_TABLE} "
                "WHERE embedding MATCH ? AND k = ?",
                (json.dumps(vecs[0]), k),
            ).fetchall()
            hits = []
            for r in rows:
                score = 1.0 - r["distance"]
                if score < min_score:
                    continue
                p = conn.execute(
                    "SELECT title, text FROM pages WHERE url = ?", (r["url"],)
                ).fetchone()
                if not p:
                    continue
                tail = conn.execute(
                    f"SELECT url FROM {_VEC_TABLE} WHERE url = ?", (r["url"],)
                ).fetchone()
                hits.append(
                    {
                        "url": r["url"],
                        "title": p["title"],
                        "snippet": p["text"][:280] if p["text"] else "",
                        "score": round(score, 4),
                        "in_vec": bool(tail),
                    }
                )
            return hits
        except Exception as e:
            print(f"[page_cache] semantic query failed: {e}")
            return []
        finally:
            conn.close()


def search_get(norm_query):
    """Return a cached web_search payload for ``norm_query`` if unexpired."""
    if not norm_query:
        return None
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT payload FROM searches "
                "WHERE norm_query = ? AND expires_at > ?",
                (norm_query, now),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    payload = dict(payload)
    payload["cached_result"] = True
    return payload


def search_put(norm_query, query, payload, ttl=None):
    """Persist a web_search response under its normalized query key.

    ``ttl`` is the seconds until this result is re-fetched; when omitted it
    falls back to the regex heuristic (short for time-sensitive queries, long
    otherwise). ``ttl`` is clamped to at least 60s so an accidental report of a
    short-lived topic can't cause a hot loop.
    """
    if not norm_query:
        return
    if ttl is None:
        ttl = regex_ttl(query)
    ttl = max(60, int(ttl or 0))
    now = time.time()
    stored = dict(payload)
    stored["_ttl"] = ttl
    with _lock:
        conn = _connect()
        try:
            _purge_expired(conn, now)
            conn.execute(
                """
                INSERT OR REPLACE INTO searches
                    (norm_query, query, payload, fresh, fetched_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (norm_query, query, json.dumps(stored), 0,
                 now, now + ttl),
            )
            conn.commit()
        finally:
            conn.close()
