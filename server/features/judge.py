"""Read-only per-user judge model resolution.

The ``user_judges`` table maps a username to the judge model id used for LLM
verification on the guardrail lane. The table is seeded out-of-band and is
never written by the app (no runtime API). Users without a row fall back to
the default guardrail judge (``MODEL_ID_GUARDRAIL``).

A short-TTL cache keeps per-request lookups off the critical path while still
picking up out-of-band table edits within a bounded window.
"""

import threading
import time

import server.db as db
from server.config import MODEL_ID_GUARDRAIL

_CACHE_TTL = 30
_cache = {}
_cache_lock = threading.Lock()

_AVAIL_TTL = 60
_avail_cache = {}
_warned_bad_ids = set()


def _default_judge_model():
    try:
        from server.features.state import M
        return M.server_model_id("guardrail") or MODEL_ID_GUARDRAIL
    except Exception:
        return MODEL_ID_GUARDRAIL


def _guardrail_base():
    try:
        from server.config import LLAMA_BASE_GUARDRAIL
        return str(LLAMA_BASE_GUARDRAIL or "").rstrip("/")
    except ImportError:
        return "http://localhost:8083"


def _model_ids_listed(base_url):
    """Cached set of model ids the server at ``base_url`` advertises, or None
    when the endpoint couldn't be queried (caller should not guess)."""
    now = time.time()
    with _cache_lock:
        hit = _avail_cache.get(base_url)
        if hit and now < hit[0]:
            return hit[1]

    ids = None
    try:
        import requests
        r = requests.get(f"{base_url}/v1/models", timeout=5)
        if r.status_code == 200:
            ids = set(
                (d.get("id") or "").strip()
                for d in (r.json().get("data") or [])
                if (d.get("id") or "").strip()
            )
    except Exception:
        pass

    with _cache_lock:
        _avail_cache[base_url] = (now + _AVAIL_TTL, ids)
    return ids


def sanitize_judge_model(model_id, base_url=None):
    """Return ``model_id`` if the guardrail server can actually serve it, else
    the default judge id.

    A per-user judge row can hold a typo'd/removed model id; pinning it would
    make every judge call burn the full ensure timeout then fail closed. This
    pre-flights the id against ``GET /v1/models`` (cached ~60s) and silently
    falls back to the default judge so verification keeps working. Only acts
    when ``base_url`` is the guardrail server; external judge endpoints keep
    their id untouched. Never raises.
    """
    model_id = (model_id or "").strip()
    base_url = (base_url or "").rstrip("/")
    if not model_id or base_url != _guardrail_base():
        return model_id

    listed = _model_ids_listed(base_url)
    if listed is None:
        # Endpoint unreachable — don't guess; the normal ensure/retry path
        # (and its bounded wait) handles the outage.
        return model_id
    if model_id in listed:
        return model_id

    default = _default_judge_model()
    if model_id == default:
        return model_id
    if model_id not in _warned_bad_ids:
        _warned_bad_ids.add(model_id)
        print(
            f"[guardrail][judge] user judge '{model_id}' not available on "
            f"server — falling back to default judge '{default}'",
            flush=True,
        )
    return default


def resolve_judge_model(username):
    """Return the judge ``model_id`` for ``username``, or the default judge.

    ``username`` may be empty/None (MCP batch lane, internal tasks) — those
    resolve to the default judge. Never raises; a DB failure degrades to the
    default judge so verification keeps working.
    """
    if not username:
        return _default_judge_model()

    now = time.time()
    with _cache_lock:
        hit = _cache.get(username)
        if hit and now < hit[0]:
            return hit[1]

    model_id = ""
    try:
        row = db.fetch_one(
            "SELECT model_id FROM user_judges WHERE username=?", (username,)
        )
        if row:
            model_id = (row.get("model_id") or "").strip()
    except Exception:
        model_id = ""

    if not model_id:
        model_id = _default_judge_model()

    with _cache_lock:
        _cache[username] = (now + _CACHE_TTL, model_id)
    return model_id