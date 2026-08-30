"""Input guardrail for MCP gateway chat inputs.

Two independent layers:

1. ``is_jailbreak_attempt`` — literal substring filter over inbound user
   text. Flagged inputs are declined locally: nothing is sent upstream, no
   task is created, no session history is touched.
2. ``wrap_user_message`` — everything that passes is forwarded wrapped in
   the SAFETY DIRECTIVES / CRITICAL DIRECTIVE frame with explicit
   ``<user_input> / </user_input>`` XML boundaries, so instructions
   embedded inside user text stay user text and the model is told to answer
    boundary-violating content with a fixed refusal instead of complying.

Layer 1 is intentionally naive (cheap, deterministic substring matching);
novel jailbreak phrasings simply fall through to layer 2.

All pattern/prompt files live in a configurable ``SURFACE_ATTACKS_DIR``
(outside this repo).  When ``SURFACE_ATTACKS_KEY`` is set the loader reads
``.enc`` files and decrypts them in memory with Fernet; otherwise it reads
plain ``.txt`` files directly (dev convenience).
"""

import logging
import os
import re
import unicodedata
from pathlib import Path

from server.features.judge import sanitize_judge_model

log = logging.getLogger(__name__)

GUARDRAIL_DECLINE = "I cannot fulfill this request."
MODEL_REFUSAL = "Request declined."
HARMFUL_DECLINE = (
    "I can't provide instructions for creating weapons, explosives, "
    "incendiaries, or illegal/harmful substances. If you're researching a "
    "legitimate topic (history, safety, policy), I can discuss it in general terms."
)


def _normalize(text: str) -> str:
    """Lowercase and strip diacritics so ASCII patterns match accented text
    (e.g. French "bombe à essence" → "bombe a essence")."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(
        c for c in decomposed if not unicodedata.combining(c)
    ).lower()


# ── Encrypted file loader ────────────────────────────────────────────────────
# Lazy initialisation: env vars are read on first call, not at import time,
# so dotenv / systemd EnvironmentFile has time to populate os.environ before
# we touch it.

_fernet = None
_surface_dir = None
_patterns_cache: dict[str, list[str]] = {}
_prompts_cache: dict[str, str] = {}


def _ensure_fernet():
    """Initialise Fernet + surface dir once, on first use."""
    global _fernet, _surface_dir
    if _surface_dir is not None:
        return  # already initialised

    _surface_dir = Path(os.environ.get(
        "SURFACE_ATTACKS_DIR",
        Path(__file__).resolve().parent.parent / "prompts" / "surface_attacks",
    ))

    key = os.environ.get("SURFACE_ATTACKS_KEY", "").strip()
    if key:
        try:
            from cryptography.fernet import Fernet
            _fernet = Fernet(key.encode() if isinstance(key, str) else key)
            log.info("[guardrail] Fernet decryption enabled for %s", _surface_dir)
        except Exception as exc:
            log.warning("[guardrail] bad SURFACE_ATTACKS_KEY, falling back to plaintext: %s", exc)
            _fernet = None
    else:
        log.info("[guardrail] SURFACE_ATTACKS_KEY not set — reading plaintext .txt from %s", _surface_dir)


def _load_raw(name: str) -> bytes:
    """Read a file from ``SURFACE_ATTACKS_DIR``.

    Tries ``<name>.enc`` first (decrypted in memory via Fernet), then falls
    back to ``<name>`` (plaintext).  Raises ``FileNotFoundError`` if neither
    exists.
    """
    _ensure_fernet()
    enc = _surface_dir / f"{name}.enc"
    plain = _surface_dir / name

    if _fernet and enc.exists():
        return _fernet.decrypt(enc.read_bytes())

    if plain.exists():
        return plain.read_bytes()

    raise FileNotFoundError(
        f"[guardrail] Neither {enc} nor {plain} found. "
        f"Set SURFACE_ATTACKS_DIR and (optionally) SURFACE_ATTACKS_KEY."
    )


def _get_patterns(name: str) -> list[str]:
    """Load (and cache) a pattern list — one pattern per line."""
    if name not in _patterns_cache:
        text = _load_raw(name).decode("utf-8")
        _patterns_cache[name] = [line.strip() for line in text.splitlines() if line.strip()]
    return _patterns_cache[name]


def _get_prompt(name: str) -> str:
    """Load (and cache) a prompt text file, stripping trailing whitespace."""
    if name not in _prompts_cache:
        _prompts_cache[name] = _load_raw(name).decode("utf-8").rstrip("\n")
    return _prompts_cache[name]


# ── Lazy accessors (used by mcp_gateway + internal functions) ────────────────

def _injection_patterns():
    return _get_patterns("injection_patterns.txt")

def _harmful_request_patterns():
    return _get_patterns("harmful_request_patterns.txt")

def _harmful_output_patterns():
    return _get_patterns("harmful_output_patterns.txt")

def _strict_output_patterns():
    return _get_patterns("strict_output_patterns.txt")

def _judge_system():
    return _get_prompt("judge_input.txt")

def _judge_output_system():
    return _get_prompt("judge_output.txt")

def _strict_judge_system():
    return _get_prompt("judge_strict.txt")

def _safety_frame():
    return _get_prompt("safety_frame.txt")


def is_jailbreak_attempt(message: str) -> bool:
    msg_lower = (message or "").lower()
    return any(pattern in msg_lower for pattern in _injection_patterns())


# ── Harmful-content guardrail ────────────────────────────────────────────────
# Layer 1 (above) blocks known *jailbreak* phrasings. This layer blocks
# explicit requests to manufacture weapons / explosives / illegal harmful
# substances, and — crucially — scans the MODEL'S OWN OUTPUT, because the
# advisory safety frame alone is not reliably obeyed by local LLMs (e.g. they
# will still walk through building a gasoline bomb).
#
# Patterns are intentionally high-signal to limit false positives; they are a
# first line of defence, not a substitute for real content moderation. Tune the
# lists below as needed.


def is_harmful_request(message: str) -> bool:
    """True if the inbound user text is an explicit harmful-manufacture request."""
    msg = _normalize(message)
    return any(pattern in msg for pattern in _harmful_request_patterns())


def is_harmful_content(text: str) -> bool:
    """True if generated text looks like harmful manufacture instructions.

    Scans the model's output for high-signal harmful how-to phrasing. Best
    effort: substring matching, so novel phrasings may slip through, but it
    reliably catches the common compliance cases (e.g. gasoline-bomb walkthroughs).
    """
    txt = _normalize(text)
    return any(pattern in txt for pattern in _harmful_output_patterns())


def is_strict_output_blocked(text: str) -> bool:
    """True if generated text matches strict MCP output prohibition patterns.

    Broader than ``is_harmful_content``: covers sexual, violent, gore, death,
    suicide, murder, illegal content, AND prompt/input/system-prompt leaking.
    Intentionally aggressive — false positives are acceptable because the
    downstream LLM judge is the final arbiter.
    """
    txt = _normalize(text)
    return any(pattern in txt for pattern in _strict_output_patterns())


# ── LLM safety judge ────────────────────────────────────────────────────────
# Pattern matching (above) is fast but language-bound and easy to dodge with
# novel phrasings. The judge pre-calls the inference engine with a strict
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

# ── LLM safety judge ────────────────────────────────────────────────────────
# Pattern matching (above) is fast but language-bound and easy to dodge with
# novel phrasings. The judge pre-calls the inference engine with a strict
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


def _run_judge(label, system_prompt, text, base_url, timeout, fail_closed,
               max_chars=2000, model_id=None):
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
        # Must cover a COLD model load (~25s) plus thinking-model inference;
        # a tight timeout here would fail-closed-block benign traffic after
        # every idle unload. Mirrors SAMPLING_ROUTER_TIMEOUT=90 in config.py.
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
    try:
        import requests
    except Exception as e:
        print(f"[guardrail][{label}] requests unavailable: {e}")
        return fail_closed

    model_id = sanitize_judge_model(model_id, base_url)

    cache_key = (base_url, (model_id or "").strip())
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
                    {"role": "user", "content": text[:max_chars]},
                ],
                "temperature": 0,
                "max_tokens": max_tokens,
                "cache_prompt": False,
                "stream": False,
            }
            try:
                r = requests.post(
                    f"{base_url.rstrip('/')}/v1/chat/completions",
                    json=payload,
                    timeout=timeout,
                )
            except Exception as e:
                # Endpoint itself down — other model ids won't help.
                print(
                    f"[guardrail][{label}] call failed: {e} — treating as "
                    f"{'HARMFUL (fail-closed)' if fail_closed else 'SAFE (fail-open)'}"
                )
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
                verdict = _parse_verdict(content)
                raw = repr(content.strip())
                if not content.strip():
                    reasoning = msg.get("reasoning_content") or ""
                    raw += f" reasoning_tail={reasoning[-120:]!r}"
                print(
                    f"[guardrail][{label}] model={cand} "
                    f"verdict={'HARMFUL' if verdict else 'SAFE'} raw={raw}"
                )
                _JUDGE_MODEL_CACHE[cache_key] = [cand] + [
                    c for c in candidates if c != cand
                ]
                return verdict
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
        f"[guardrail][{label}] all {len(candidates)} judge model(s) failed "
        f"({last_err}) — treating as "
        f"{'HARMFUL (fail-closed)' if fail_closed else 'SAFE (fail-open)'}"
    )
    return fail_closed


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
    base_url = os.environ.get("GUARD_LLM_BASE", "http://localhost:8083")
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


def mcp_output_judge(text, timeout=None, fail_closed=True, model_id=None):
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
    if timeout is None or timeout < _JUDGE_MIN_TIMEOUT:
        try:
            timeout = int(os.environ.get("GUARD_LLM_TIMEOUT", "90"))
        except ValueError:
            timeout = _JUDGE_MIN_TIMEOUT
    text = (text or "").strip()
    if not text:
        return False
    print(
        f"[guardrail][strict-output-judge] -> {base_url} "
        f"fail_closed={fail_closed} model={model_id or 'auto'} text={text!r}"
    )
    try:
        import requests as _requests
    except Exception as e:
        print(f"[guardrail][strict-output-judge] requests unavailable: {e}")
        return fail_closed

    model_id = sanitize_judge_model(model_id, base_url)

    cache_key = (base_url, (model_id or "").strip())
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
                    {"role": "system", "content": _strict_judge_system()},
                    {"role": "user", "content": text[:6000]},
                ],
                "temperature": 0,
                "max_tokens": max_tokens,
                "cache_prompt": False,
                "stream": False,
            }
            try:
                r = _requests.post(
                    f"{base_url.rstrip('/')}/v1/chat/completions",
                    json=payload,
                    timeout=timeout,
                )
            except Exception as e:
                print(
                    f"[guardrail][strict-output-judge] call failed: {e} — "
                    f"BLOCKED (fail-closed)"
                )
                last_err = f"connection error: {e}"
                conn_failed = True
                break
            if r.status_code == 200:
                msg = r.json().get("choices", [{}])[0].get("message", {}) or {}
                content = msg.get("content") or ""
                if not content.strip():
                    # Thinking models may exhaust the budget mid-reasoning, leaving
                    # content empty. Judge on the reasoning text so a harmless
                    # reply isn't falsely BLOCKED (fail-closed) on a truncated
                    # response.
                    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
                    if reasoning:
                        content = reasoning
                verdict = _parse_strict_verdict(content)
                raw = repr(content.strip())
                if not content.strip():
                    reasoning = msg.get("reasoning_content") or ""
                    raw += f" reasoning_tail={reasoning[-120:]!r}"
                print(
                    f"[guardrail][strict-output-judge] model={cand} "
                    f"verdict={'BLOCKED' if verdict else 'SAFE'} raw={raw}"
                )
                _JUDGE_MODEL_CACHE[cache_key] = [cand] + [
                    c for c in candidates if c != cand
                ]
                return verdict
            last_err = f"HTTP {r.status_code}: {r.text[:150]}"
            print(f"[guardrail][strict-output-judge] model={cand} rejected — {last_err}")
        if conn_failed and attempts == 1:
            print(
                "[guardrail][strict-output-judge] judge connection failed — "
                "ensuring guardrail server & retrying once",
                flush=True,
            )
            ensure_judge_ready(base_url, model_id=model_id)
            _JUDGE_MODEL_CACHE.pop(cache_key, None)
            continue
        break

    _JUDGE_MODEL_CACHE.pop(cache_key, None)
    print(
        f"[guardrail][strict-output-judge] all {len(candidates)} judge model(s) "
        f"failed ({last_err}) — BLOCKED (fail-closed)"
    )
    return fail_closed


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


def wrap_user_message(user_message: str) -> str:
    wrapped = _safety_frame().format(
        decline=GUARDRAIL_DECLINE,
        refusal=MODEL_REFUSAL,
        user_message=user_message if isinstance(user_message, str) else "",
    )
    # Normalise the legacy bracket boundaries to XML tags so the model gets an
    # unambiguous structural boundary around untrusted user text.
    return (
        wrapped.replace("[USER INPUT START]", "<user_input>")
        .replace("[USER INPUT END]", "</user_input>")
        .strip()
    )
