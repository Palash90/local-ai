"""Per-user judge model resolution + the LLM safety judge.

Single home for ALL judge machinery:

- per-user resolution (:func:`resolve_judge_model`, read-only over the
  ``user_judges`` table) and pre-flight sanitizing (:func:`sanitize_judge_model`)
- judge prompts (``_judge_system`` / ``_judge_output_system`` /
  ``_strict_judge_system``), loaded via :mod:`server.features.surface_loader`
- verdict parsing (``_parse_verdict`` / ``_parse_strict_verdict``)
- candidate selection (``_judge_candidates``) and the shared POST plumbing
  (:func:`_run_judge`)
- public entry points: :func:`llm_classify_harmful`,
  :func:`llm_classify_harmful_output`, :func:`mcp_output_judge`

The guardrail llama-server *process lifecycle* (``ensure_guardrail_ready``)
stays in :mod:`server.features.monitoring`; the async L2 gateway judge stays in
``server/mcp_gateway.py``; both consume this module.

Resolution priority: per-user row -> ``GUARD_LLM_MODEL``-style fallbacks are
handled by ``_judge_candidates`` -> configured default (``MODEL_ID_GUARDRAIL``).
A short-TTL cache keeps per-request lookups off the critical path while still
picking up out-of-band table edits within a bounded window.
"""

import os
import re
import threading
import time

import server.db as db
from server.config import MODEL_ID_GUARDRAIL
from server.features.surface_loader import _get_prompt

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


# ── LLM safety judge ────────────────────────────────────────────────────────
# Pattern matching (input_guard) is fast but language-bound and easy to dodge
# with novel phrasings. The judge pre-calls the inference engine with a strict
# classify-only prompt and asks for a single HARMFUL/SAFE verdict BEFORE the
# real request is ever sent for generation. This catches non-English prompts
# (e.g. French/Spanish bomb requests) and paraphrases that patterns miss.
#
# Configure via GUARD_LLM_BASE (defaults to the local llama-server). If unset
# or the call fails, the judge is a no-op and the pattern layer remains the
# only defence.

# Verdict parsing: local models routinely ignore the 'exactly one word'
# instruction and reply with phrases like "Not harmful", "Safe — no harmful
# content", or "I cannot classify this". Substring-matching "HARMFUL" turned
# every one of those into a block (massive false positives), so parse the
# verdict properly instead: only an affirmative, unnegated harmful token may
# block; anything ambiguous parses as SAFE.
_VERDICT_NEGATIONS = {"NOT", "NO", "NEITHER", "NEVER", "ISNT", "ISN'T",
                      "CANNOT", "CANT", "CAN'T"}
_VERDICT_HARMFUL = {"HARMFUL", "UNSAFE"}
_VERDICT_SAFE = {"SAFE", "BENIGN", "HARMLESS", "NONE", "OKAY", "OK"}


def _parse_verdict(content):
    """Interpret a free-form judge reply as a boolean HARMFUL verdict."""
    words = re.findall(r"[A-Za-z']+", (content or "").upper())
    negated = False
    for w in words[:10]:
        if w in _VERDICT_NEGATIONS:
            negated = not negated
        elif w in _VERDICT_HARMFUL:
            return not negated
        elif w in _VERDICT_SAFE:
            return negated
    return False


_JUDGE_MODEL_CACHE = {}
_JUDGE_MIN_TIMEOUT = 90


def _judge_system():
    return _get_prompt("judge_input.txt")


def _judge_output_system():
    return _get_prompt("judge_output.txt")


def _strict_judge_system():
    return _get_prompt("judge_strict.txt")


def _chat_model_id():
    """Model id the chat pipeline itself uses (from server/config.py)."""
    try:
        from server.config import MODEL_ID
        return MODEL_ID or ""
    except ImportError:
        pass
    try:
        from config import MODEL_ID
        return MODEL_ID or ""
    except ImportError:
        return ""


def _judge_candidates(base_url, forced=None):
    """Ordered list of model ids to try for judging on ``base_url``.

    Constraints discovered the hard way:
    - Newer llama.cpp builds REJECT completions without a 'model' field
      (HTTP 400 'model name is missing').
    - The :8081 server runs in --models-dir (multi-model) mode where each
      completion LOADS its named model on demand — and loads DO NOT evict
      other models first. Judging with an arbitrary/different model than
      whatever is resident caused VRAM exhaustion (CUDA OOM) that took down
      the chat model itself.

    So the order below never allocates when avoidable:
      1. ``forced`` (per-user/per-call judge model id, if provided)
      2. GUARD_LLM_MODEL env override
      3. a model ALREADY LOADED on the endpoint (zero VRAM churn)
      4. the chat model id (warms exactly what generation will use next)
      5. whatever else the endpoint lists
    The winner is cached per endpoint+model; failures fall through to the next
    candidate and clear the cache so it is re-probed later.
    """
    ids, loaded = [], []
    try:
        import requests
        r = requests.get(f"{base_url.rstrip('/')}/v1/models", timeout=5)
        if r.status_code == 200:
            for d in r.json().get("data") or []:
                mid = d.get("id", "") or ""
                if not mid:
                    continue
                ids.append(mid)
                if (d.get("status") or {}).get("value") == "loaded":
                    loaded.append(mid)
    except Exception:
        pass

    out = []
    requested = (forced or "").strip()
    if requested:
        out.append(requested)
    forced = os.environ.get("GUARD_LLM_MODEL", "").strip()
    if forced:
        out.append(forced)
    out.extend(m for m in loaded if m not in out)
    chat = _chat_model_id()
    if chat and chat not in out:
        out.append(chat)
    out.extend(m for m in ids if m not in out)
    return out


def _judge_max_tokens():
    """Token budget for a judge call. Thinking models spend their budget on
    reasoning BEFORE emitting the verdict word, so a tiny cap yields an empty
    content field and a meaningless SAFE. 2048 gives the reasoning headroom to
    still emit the verdict; tune via GUARD_LLM_MAX_TOKENS."""
    try:
        return int(os.environ.get("GUARD_LLM_MAX_TOKENS", "2048"))
    except ValueError:
        return 2048


def ensure_judge_ready(base_url, model_id=None):
    """Bring the guardrail LLM judge server up with the requested model loaded.

    Only acts when ``base_url`` points at the guardrail server (the default
    for all judge calls); other endpoints are left untouched. The real work is
    done by :func:`server.features.monitoring.ensure_guardrail_ready`, which
    restarts the process if down, loads the model, and waits until it serves.
    ``model_id`` may be a per-user judge; defaults to the configured judge.
    Best-effort: failures are logged, never raised.
    """
    try:
        from server.config import LLAMA_BASE_GUARDRAIL
    except ImportError:
        LLAMA_BASE_GUARDRAIL = "http://localhost:8083"
    if str(base_url or "").rstrip("/") != str(LLAMA_BASE_GUARDRAIL).rstrip("/"):
        return
    try:
        from server.features.monitoring import ensure_guardrail_ready
        ensure_guardrail_ready(model_id=model_id)
    except Exception as e:
        print(f"[guardrail][judge] ensure_judge_ready failed: {e}")


_RENDER_WAIT_TIMEOUT = 600   # matches llm._wait_image_active_clear
_RENDER_COOLDOWN = 30        # let ComfyUI /free + post-render VRAM settle


def wait_until_render_safe(timeout=_RENDER_WAIT_TIMEOUT, cooldown=_RENDER_COOLDOWN,
                           label=None):
    """Hold judge calls while an image render owns the machine (RAM safety).

    ComfyUI renders pull multi-GB weights into system RAM; a judge model
    loading in that same window pushes the box over RAM_EVAC_THRESHOLD and
    triggers an emergency evacuation that kills the render mid-flight (and
    everything else). The image path unloads the judge at render start, so a
    judge call arriving during ``M._image_active`` is always early — waiting
    it out is safe: background lanes (self-chat agents, MCP batching) don't
    care about a short delay, and UI tasks surface explicit status notes
    ("Image Gen", "Evaluating answer...") while they wait. The response
    judges run right after the render anyway.

    After the render clears, sleep a short cooldown so ComfyUI's /free and
    the post-render VRAM settle before judge weights land in RAM. Returns
    True when the render window cleared, False on timeout (callers proceed
    anyway — same semantics as ``llm._wait_image_active_clear``).
    """
    try:
        from server.features.state import M
    except Exception:
        return True
    tag = f"[judge][{label}]" if label else "[judge]"
    deadline = time.time() + timeout
    waited = False
    while time.time() < deadline:
        if not getattr(M, "_image_active", False):
            break
        waited = True
        time.sleep(1)
    if not waited:
        return True
    print(f"{tag} image render active — holding judge call until it finishes", flush=True)
    time.sleep(min(cooldown, max(0.0, deadline - time.time())))
    return time.time() < deadline


# ── GPU fallback for UI-lane post-generation judges ─────────────────────────
# The guardrail judge server is CPU-only and shared by every lane: MCP batch
# verification (L2/L3) and the Kaya/Kolpo agent output judges each keep their
# own judge model resident there. When such a foreign judge is parked on the
# CPU server, a UI user's judge call must first evict it and then wait out a
# cold CPU load plus slow CPU inference (and the other lane swaps its judge
# right back) — while the next UI query sits in the GPU lane queue behind
# "Evaluating answer...". UI-lane judge calls therefore verify on the GPU
# server with the user's configured chat model instead: VRAM is cleared of
# foreign llama models and idle ComfyUI weights, the chat model is loaded,
# and the judge POST runs where it finishes in seconds. The MCP/agent lanes
# never fall back — their judges belong on the CPU server.

_RESIDENT_READY_STATES = ("loaded", "ready")


def _guardrail_resident_judge():
    """Model id currently resident on the guardrail (CPU) judge server, or "".

    Prefers the lane's own bookkeeping (``_guardrail_loaded_model``, kept in
    sync by the load/unload/ensure paths) and falls back to probing
    ``GET /models`` for a model the server reports as loaded/ready (covers an
    auto-load at boot before any bookkeeping ran). Never raises.
    """
    try:
        from server.features.state import M
    except Exception:
        return ""
    with M._data_lock:
        resident = (M._guardrail_loaded_model or "").strip()
    if resident:
        return resident
    try:
        import requests
        r = requests.get(f"{M.server_base('guardrail')}/models", timeout=5)
        if r.status_code == 200:
            for m in r.json().get("data", []):
                if (m.get("status") or {}).get("value") in _RESIDENT_READY_STATES:
                    return (m.get("id") or "").strip()
    except Exception:
        pass
    return ""


def _evict_gpu_foreign_models(base, chat):
    """Unload every non-chat model resident on the GPU llama-server.

    The GPU server runs in --models-dir mode where loads DO NOT evict other
    models first (see :func:`_judge_candidates`): loading the chat model next
    to a foreign resident OOMs the small card. Best-effort; never raises.
    """
    try:
        import requests
        from server.features.llm import _wait_model_unloaded
        r = requests.get(f"{base.rstrip('/')}/models", timeout=5)
        if r.status_code != 200:
            return
        for m in r.json().get("data", []):
            mid = (m.get("id") or "").strip()
            if not mid or mid == chat:
                continue
            if (m.get("status") or {}).get("value") not in _RESIDENT_READY_STATES:
                continue
            print(
                f"[guardrail][gpu-fallback] unloading foreign GPU model "
                f"'{mid}' before the chat model load",
                flush=True,
            )
            try:
                requests.post(
                    f"{base.rstrip('/')}/models/unload",
                    json={"model": mid},
                    timeout=60,
                )
                _wait_model_unloaded(base, mid, timeout=60)
            except Exception as e:
                print(f"[guardrail][gpu-fallback] unload of '{mid}' failed: {e}")
    except Exception as e:
        print(f"[guardrail][gpu-fallback] GPU model probe failed: {e}")


def _gpu_judge_fallback_base(requested_model_id, label):
    """GPU base URL when a UI judge should verify there instead of on the CPU
    guardrail judge, else None.

    Trigger (callers gate this on the task's lane being the interactive GPU
    lane): the guardrail server currently has a judge resident that is NOT
    the requested one — the MCP verify model or a Kaya/Kolpo agent judge.
    Serving the UI judge from there means evicting that lane's model and
    waiting out a cold CPU load plus slow CPU inference, so verify on the GPU
    instead: clear VRAM (foreign llama models, idle ComfyUI weights), load
    the user's configured chat model, and judge there.

    Best-effort and fail-safe: any prep failure returns None and the caller
    stays on the normal guardrail path. Nothing is loaded or unloaded while
    an image render is active.
    """
    try:
        from server.features.state import M
        from server.features.llm import _is_vram_occupied
    except Exception:
        return None
    requested = (requested_model_id or "").strip()
    chat = _chat_model_id()
    if not chat:
        return None
    with M._data_lock:
        if M._image_active:
            return None
    resident = _guardrail_resident_judge()
    if not resident or resident == requested:
        return None
    base = M.server_base("gpu")
    if M.is_model_ready(base, chat):
        return base
    with M._chat_generating_lock:
        if M._chat_generating > 0:
            return None
    try:
        M.ensure_llama_server("gpu")
        _evict_gpu_foreign_models(base, chat)
        if _is_vram_occupied():
            M.free_comfyui_vram()
        if not M.load_llama_model("gpu"):
            print(
                f"[guardrail][{label}] GPU chat model not ready — "
                "staying on the CPU judge",
                flush=True,
            )
            return None
    except Exception as e:
        print(f"[guardrail][{label}] GPU judge fallback prep failed: {e}")
        return None
    return base


def _judge_completion(label, system_prompt, user_content, base_url, timeout,
                      max_chars=2000, model_id=None, allow_gpu_fallback=False):
    """Lowest-level judge POST plumbing shared by every judge entry point.

    Resolves the pinned/explicit and candidate model ids (cached, see
    :func:`_judge_candidates`), brings the guardrail server + model up when
    the judge lives there (:func:`ensure_judge_ready`), POSTs the classify
    prompt with a bounded timeout, retries once after a connection failure,
    and caches whichever model answered. Returns ``(model_id_used, content)``
    or ``(None, None)``. ``content`` falls back to the raw reasoning text when
    a thinking model exhausts its budget before emitting ``content``.

    ``allow_gpu_fallback`` (UI-lane callers only) redirects the whole call to
    the GPU server with the user's configured chat model when the CPU judge
    is parked on a foreign judge model — see :func:`_gpu_judge_fallback_base`.
    """
    if timeout is None or timeout < _JUDGE_MIN_TIMEOUT:
        # Must cover a COLD model load (~25s) plus thinking-model inference;
        # a tight timeout here would fail-closed-block benign traffic after
        # every idle unload. Mirrors SAMPLING_ROUTER_TIMEOUT=90 in config.py.
        try:
            timeout = int(os.environ.get("GUARD_LLM_TIMEOUT", "90"))
        except ValueError:
            timeout = _JUDGE_MIN_TIMEOUT
    user_content = (user_content or "").strip()
    if not user_content:
        return None, None
    # One ComfyUI render + one judge model load can tip the box over the RAM
    # evacuation threshold — never load judge weights mid-render.
    wait_until_render_safe(label=label)
    try:
        import requests
    except Exception as e:
        print(f"[guardrail][{label}] requests unavailable: {e}")
        return None, None

    gpu_base = (
        _gpu_judge_fallback_base(model_id, label) if allow_gpu_fallback else None
    )
    if gpu_base:
        print(
            f"[guardrail][{label}] CPU judge parked on a foreign judge — "
            f"verifying on the GPU server ({gpu_base}) with the chat model",
            flush=True,
        )
        from server.features.llm import _mark_chat_generating
        base_url = gpu_base
        model_id = _chat_model_id()
        candidates = [model_id]
    else:
        model_id = sanitize_judge_model(model_id, base_url)
        candidates = None
    cache_key = (base_url, (model_id or "").strip())
    if candidates is None:
        cached = _JUDGE_MODEL_CACHE.get(cache_key)
        candidates = list(cached) if isinstance(cached, list) else (
            [cached] if cached else None
        ) or _judge_candidates(base_url, forced=model_id)

    if (model_id or "").strip():
        ensure_judge_ready(base_url, model_id=model_id)

    max_tokens = _judge_max_tokens()
    last_err = ""
    attempts = 0
    while True:
        attempts += 1
        conn_failed = False
        for cand in candidates:
            payload = {
                "model": cand,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content[:max_chars]},
                ],
                "temperature": 0,
                "max_tokens": max_tokens,
                "cache_prompt": False,
                "stream": False,
            }
            try:
                if gpu_base:
                    _mark_chat_generating("gpu", True)
                try:
                    r = requests.post(
                        f"{base_url.rstrip('/')}/v1/chat/completions",
                        json=payload,
                        timeout=timeout,
                    )
                finally:
                    if gpu_base:
                        _mark_chat_generating("gpu", False)
            except Exception as e:
                # Endpoint itself down — other model ids won't help.
                print(f"[guardrail][{label}] call failed: {e} — connection error")
                last_err = f"connection error: {e}"
                conn_failed = True
                break
            if r.status_code == 200:
                msg = r.json().get("choices", [{}])[0].get("message", {}) or {}
                content = msg.get("content") or ""
                if not content.strip():
                    # Thinking models may exhaust the budget mid-reasoning, leaving
                    # content empty but a useful reasoning tail. Judge on the
                    # reasoning text so we don't silently return a meaningless SAFE.
                    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
                    if reasoning:
                        content = reasoning
                _JUDGE_MODEL_CACHE[cache_key] = [cand] + [
                    c for c in candidates if c != cand
                ]
                return cand, (content or "").strip()
            last_err = f"HTTP {r.status_code}: {r.text[:150]}"
            print(f"[guardrail][{label}] model={cand} rejected — {last_err}")
        if conn_failed and attempts == 1:
            print(
                f"[guardrail][{label}] judge connection failed — ensuring "
                "guardrail server & retrying once",
                flush=True,
            )
            ensure_judge_ready(base_url, model_id=model_id)
            _JUDGE_MODEL_CACHE.pop(cache_key, None)
            continue
        break

    # Every candidate failed; forget the ordering so it is re-probed later.
    _JUDGE_MODEL_CACHE.pop(cache_key, None)
    print(
        f"[guardrail][{label}] all {len(candidates)} judge model(s) failed ({last_err})"
    )
    return None, None


def _run_judge(label, system_prompt, text, base_url, timeout, fail_closed,
               max_chars=2000, model_id=None, allow_gpu_fallback=False):
    """Shared judge plumbing: POST the classify prompt, log exactly what was
    passed and what came back, apply fail-open/fail-closed policy.

    Tries candidate models in order (see :func:`_judge_candidates`) and
    caches whichever one answers, so steady-state calls hit a single model
    with zero load/unload churn. ``model_id`` pins the judge (e.g. a per-user
    judge) and is tried first; on the guardrail server an unavailable pinned
    judge is (re)loaded via ``ensure_judge_ready`` before the retry. Returns
    True only for an affirmative HARMFUL verdict.
    """
    if timeout is None or timeout < _JUDGE_MIN_TIMEOUT:
        try:
            timeout = int(os.environ.get("GUARD_LLM_TIMEOUT", "90"))
        except ValueError:
            timeout = _JUDGE_MIN_TIMEOUT
    text = (text or "").strip()
    if not text:
        return False
    print(
        f"[guardrail][{label}] -> {base_url} fail_closed={fail_closed} "
        f"model={model_id or 'auto'} text={text!r}"
    )
    cand, content = _judge_completion(
        label, system_prompt, text, base_url, timeout,
        max_chars=max_chars, model_id=model_id,
        allow_gpu_fallback=allow_gpu_fallback,
    )
    if cand is None:
        print(
            f"[guardrail][{label}] judge unavailable — treating as "
            f"{'HARMFUL (fail-closed)' if fail_closed else 'SAFE (fail-open)'}"
        )
        return fail_closed
    verdict = _parse_verdict(content)
    print(
        f"[guardrail][{label}] model={cand} "
        f"verdict={'HARMFUL' if verdict else 'SAFE'} raw={content!r}"
    )
    return verdict


def llm_classify_harmful_output(text, base_url=None, timeout=20, fail_closed=False,
                                model_id=None):
    """Return True if an LLM judge classifies generated ``text`` as harmful
    how-to content.

    This is the output-side counterpart of :func:`llm_classify_harmful`: it is
    run against the model's own reply (the single-message read path and the
    batch worker) so that completions which comply with a harmful request are
    caught even when the prompt itself dodged the input filters. Synchronous;
    ``fail_closed`` mirrors :func:`llm_classify_harmful`. ``model_id`` pins a
    per-user judge.
    """
    base_url = base_url or os.environ.get("GUARD_LLM_BASE", "http://localhost:8083")
    return _run_judge(
        "output-judge", _judge_output_system(), text, base_url, timeout,
        fail_closed, max_chars=4000, model_id=model_id,
    )


# ── Strict MCP output judge ─────────────────────────────────────────────────
# This is the FINAL end-of-pipe gatekeeper for all MCP tool outputs.  It uses
# the broadest possible definition of prohibited content and is ALWAYS
# fail-closed: if the judge model is unreachable or errors, the output is
# BLOCKED.  The goal is zero-tolerance: nothing inappropriate, illegal,
# sexual, violent, graphic, or leaking internal state ever reaches the client.

_VERDICT_BLOCKED = {"BLOCKED", "UNSAFE", "HARMFUL", "REJECTED", "DENIED", "REFUSED"}


def _parse_strict_verdict(content):
    """Interpret a free-form judge reply as a boolean BLOCKED verdict."""
    words = re.findall(r"[A-Za-z']+", (content or "").upper())
    negated = False
    for w in words[:10]:
        if w in _VERDICT_NEGATIONS:
            negated = not negated
        elif w in _VERDICT_BLOCKED:
            return not negated
        elif w in _VERDICT_SAFE:
            return negated
    # Default: if the judge said nothing useful, BLOCK (fail-closed)
    return True


def mcp_output_judge(text, timeout=None, fail_closed=True, model_id=None,
                     allow_gpu_fallback=False):
    """Final end-of-pipe strict judge for ALL MCP outputs.

    Return True if the text must be BLOCKED.  This function is the absolute
    last line of defence: it is called after pattern scans and the existing
    output judge, and covers ALL prohibited categories plus prompt/input
    leaking.  ``fail_closed`` defaults to True — if the judge model is down
    or errors, the output is BLOCKED. ``model_id`` pins a per-user judge.

    The text is truncated to 6000 chars before judging to stay within the
    judge model's context window while still covering the bulk of the output.
    """
    base_url = os.environ.get("GUARD_LLM_BASE", "http://localhost:8083")
    # The guardrail lane is CPU-only and this prompt carries up to 6000 chars
    # of reply text: a cold model load plus thinking-model inference can
    # legitimately run 2-4 minutes (observed: 90s read timeouts right after a
    # RAM-evacuation restart). Floor the L3 window at 240s — GUARD_LLM_TIMEOUT
    # can raise it, never lower it below this floor. A truly DOWN server still
    # fails fast (connection refused), so this only tolerates slow starts.
    if timeout is None or timeout < 240:
        try:
            env_timeout = int(os.environ.get("GUARD_LLM_TIMEOUT", "90"))
        except ValueError:
            env_timeout = _JUDGE_MIN_TIMEOUT
        timeout = max(240, env_timeout)
    text = (text or "").strip()
    if not text:
        return False
    print(
        f"[guardrail][strict-output-judge] -> {base_url} "
        f"fail_closed={fail_closed} model={model_id or 'auto'} text={text!r}"
    )
    cand, content = _judge_completion(
        "strict-output-judge", _strict_judge_system(), text[:6000],
        base_url, timeout, max_chars=6000, model_id=model_id,
        allow_gpu_fallback=allow_gpu_fallback,
    )
    if cand is None:
        print(
            "[guardrail][strict-output-judge] judge unavailable — BLOCKED (fail-closed)"
        )
        return fail_closed
    verdict = _parse_strict_verdict(content)
    print(
        f"[guardrail][strict-output-judge] model={cand} "
        f"verdict={'BLOCKED' if verdict else 'SAFE'} raw={content!r}"
    )
    return verdict


# ── Research-surface answer judge ─────────────────────────────────────────
# The critic (server.features.critic) fact-checks every inline citation that IS
# present. This judge additionally inspects the finished answer TOGETHER WITH
# the user's own question: citations are mandatory on the research surface, so
# a citation-free or off-topic reply must be surfaced, and the answer is also
# screened as generated output (prohibited content / internal-state leaks).

_RES_VERDICT_OK = {"OK", "CITED", "COMPLETE", "ADEQUATE", "ACCEPTED"}
_RES_VERDICT_NO_CITES = {"UNCITED"}
_RES_VERDICT_UNSAFE = {"UNSAFE", "HARMFUL", "BLOCKED", "LEAK", "LEAKED", "PROHIBITED"}
# Compound tokens local models may emit verbatim (underscore-joined), which a
# plain word-token scan would split apart (e.g. "NO_CITATIONS" -> NO/CITATIONS).
_RES_NO_CITES_LITERAL = ("NO_CITATIONS", "MISSING_CITATIONS", "NO_CITE")

_QUALITY_RE = re.compile(
    r"(?:QUALITY|CONFIDENCE|SCORE)\s*[:=]?\s*(\d{1,3}(?:[.,]\d+)?)"
    r"(?:\s*/\s*(\d{1,3}))?",
    re.IGNORECASE,
)


def _parse_quality(content):
    """Extract a 0-100 quality/confidence score from a judge reply, or None.

    Accepts ``QUALITY: 84/100``, ``Quality 84``, ``Confidence: 7/10`` etc. A
    fraction (x/y) is normalized to the 0-100 scale; any value is clamped.
    Returns None (never raises) when no usable score is present.
    """
    m = _QUALITY_RE.search(content or "")
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", "."))
        den = m.group(2)
        if den:
            val = val / float(den) * 100.0
        return max(0, min(100, int(round(val))))
    except (ValueError, TypeError):
        return None


def _parse_research_verdict(content):
    """Interpret a free-form judge reply as a research-verification token:
    OK / NO_CITATIONS / UNSAFE, or None when nothing usable was said."""
    text = (content or "").upper()
    for tok in _RES_NO_CITES_LITERAL:
        if tok in text:
            return "NO_CITATIONS"
    words = re.findall(r"[A-Za-z']+", text)
    negated = False
    for w in words[:12]:
        if w in _VERDICT_NEGATIONS:
            negated = not negated
        elif w in _RES_VERDICT_UNSAFE:
            return "OK" if negated else "UNSAFE"
        elif w in _RES_VERDICT_OK:
            return "OK" if not negated else "UNSAFE"
        elif w in _RES_VERDICT_NO_CITES:
            return "NO_CITATIONS"
    return None


def llm_verify_research_answer(user_input, answer, base_url=None, timeout=None,
                               model_id=None, max_chars=8000,
                               allow_gpu_fallback=False):
    """Context-aware LLM judge for research answers.

    Runs the judge over BOTH the user's original question and the generated
    research answer, because citations are mandatory on the research surface:
    the judge verifies whether the answer (1) directly addresses the question,
    (2) backs each claim with an inline ``(Author, Venue, Year) [url]``
    citation, and (3) is itself safe (no prohibited content, no leaking of
    internal instructions/prompts/system state). Replies with one token:
    OK / NO_CITATIONS / UNSAFE, plus a ``QUALITY: NN/100`` confidence score.

    Synchronous (requests). Returns a dict
    ``{model, ok, citations, unsafe, quality, reason}``, or None when the judge
    is unavailable — fail-open, the research answer is still delivered (the
    deterministic pattern layer and the critic citation pass remain the hard
    gates). ``model_id`` pins a per-user judge. The caller decides whether a
    below-gate ``quality`` or a False ``citations`` triggers a re-run.
    """
    base_url = base_url or os.environ.get("GUARD_LLM_BASE", "http://localhost:8083")
    if timeout is None or timeout < _JUDGE_MIN_TIMEOUT:
        try:
            timeout = int(os.environ.get("GUARD_LLM_TIMEOUT", "90"))
        except ValueError:
            timeout = _JUDGE_MIN_TIMEOUT
    answer = (answer or "").strip()
    if not answer:
        return {"model": None, "ok": True, "citations": True,
                "unsafe": False, "quality": None, "reason": "empty answer"}
    print(
        f"[guardrail][research-verify] -> {base_url} model={model_id or 'auto'} "
        f"user_input={repr((user_input or '')[:200])} answer_len={len(answer)}"
    )
    user_content = (
        f"USER QUESTION:\n{(user_input or '').strip()}\n\n"
        f"MODEL ANSWER:\n{answer}"
    )
    cand, content = _judge_completion(
        "research-verify", _get_prompt("judge_research.txt"), user_content,
        base_url, timeout, max_chars=max_chars, model_id=model_id,
        allow_gpu_fallback=allow_gpu_fallback,
    )
    if cand is None:
        print("[guardrail][research-verify] judge unavailable — fail-open")
        return None
    status = _parse_research_verdict(content)
    quality = _parse_quality(content)
    result = {
        "model": cand,
        "ok": status == "OK",
        "citations": status == "OK",
        "unsafe": status == "UNSAFE",
        "quality": quality,
        "reason": (content or "").strip()[:400],
    }
    if status is None:
        result["citations"] = None
        result["reason"] = f"unrecognized judge reply: {result['reason']}"
    print(
        f"[guardrail][research-verify] model={cand} status={status or 'UNKNOWN'} "
        f"quality={quality} reason={result['reason'][:160]!r}"
    )
    return result


def llm_verify_answer_quality(user_input, answer, base_url=None, timeout=None,
                               model_id=None, max_chars=8000,
                               allow_gpu_fallback=False):
    """General interactive-answer quality judge (the post-generation gate).

    Grades the finished answer against the user's own request with a general
    rubric (complete, accurate, on-topic, helpful; no prohibited content). This
    is the interactive-chat analogue of the research-surface judge and is used
    as a bounded synchronous gate: a below-gate ``quality`` lets the caller
    re-run generation; an ``unsafe`` verdict must never be delivered.

    Synchronous (requests). Returns ``{model, ok, unsafe, quality, reason}``,
    or None when the judge is unavailable — fail-open (the caller still delivers
    with a recorded note rather than dropping the reply). ``model_id`` pins a
    per-user judge.
    """
    base_url = base_url or os.environ.get("GUARD_LLM_BASE", "http://localhost:8083")
    if timeout is None or timeout < _JUDGE_MIN_TIMEOUT:
        try:
            timeout = int(os.environ.get("GUARD_LLM_TIMEOUT", "90"))
        except ValueError:
            timeout = _JUDGE_MIN_TIMEOUT
    answer = (answer or "").strip()
    if not answer:
        return {"model": None, "ok": True, "unsafe": False,
                "quality": None, "reason": "empty answer"}
    print(
        f"[guardrail][quality-judge] -> {base_url} model={model_id or 'auto'} "
        f"user_input={repr((user_input or '')[:200])} answer_len={len(answer)}"
    )
    user_content = (
        f"USER REQUEST:\n{(user_input or '').strip()}\n\n"
        f"MODEL ANSWER:\n{answer}"
    )
    cand, content = _judge_completion(
        "quality-judge", _get_prompt("judge_quality.txt"), user_content,
        base_url, timeout, max_chars=max_chars, model_id=model_id,
        allow_gpu_fallback=allow_gpu_fallback,
    )
    if cand is None:
        print("[guardrail][quality-judge] judge unavailable — fail-open")
        return None
    status = _parse_research_verdict(content)
    quality = _parse_quality(content)
    result = {
        "model": cand,
        "ok": status != "UNSAFE",
        "unsafe": status == "UNSAFE",
        "quality": quality,
        "reason": (content or "").strip()[:400],
    }
    print(
        f"[guardrail][quality-judge] model={cand} status={status or 'UNKNOWN'} "
        f"quality={quality} reason={result['reason'][:160]!r}"
    )
    return result


def llm_classify_harmful(text, base_url=None, timeout=None, fail_closed=False,
                         model_id=None):
    """Return True if an LLM judge classifies ``text`` as a harmful request.

    Synchronous (uses requests). ``fail_closed`` controls behaviour when the
    judge is unreachable or errors: when True the request is treated as
    harmful (blocked) so a missing/unavailable judge can never silently let
    dangerous traffic through; when False it degrades to the pattern layer.
    ``model_id`` pins a per-user judge.
    """
    base_url = base_url or os.environ.get("GUARD_LLM_BASE", "http://localhost:8083")
    return _run_judge(
        "input-judge", _judge_system(), text, base_url, timeout, fail_closed,
        model_id=model_id,
    )