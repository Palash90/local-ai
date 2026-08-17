"""Post-generation self-verification ("critic") pass for research answers.

After the LLM emits its final research answer, ``run_verification`` extracts the
structured ``(Author, Venue, Year) [url]`` inline citations, re-fetches each
source and asks a fresh LLM call ("the critic") to check that (1) the source
exists, (2) the claimed author/venue/year metadata matches the real page, and
(3) the specific claim is actually supported by the source text. The resulting
verdict drives a small, local edit of just that sentence (never a full report
rewrite) and a transparent verification trail is appended to the answer.

The pass is always-on for research tasks (no separate toggle) and bounded only
per-citation: at most ``VERIFY_RETRIES`` extra search/fetch attempts per
citation, so a pathological report can never loop forever while keeping the
"no overall cap" behaviour.
"""

import json
import re
import time

import requests

from server.features.state import M

_VERIFY_SYSTEM = (
    "You are a strict citation fact-checker for a research assistant. Given a "
    "claimed citation (URL plus author/venue/year metadata plus one specific "
    "claim) and the fetched text of the cited web page, determine: "
    "(1) whether the page exists and its topic matches the citation, "
    "(2) whether the cited author, venue and year actually appear on the page, "
    "(3) whether the exact claim is supported by the page text. "
    'Reply with ONLY a JSON object of the form: '
    '{"exists": bool, "url_ok": bool, "author_ok": bool, "venue_ok": bool, '
    '"year_ok": bool, "claim_support": "supports"|"partial"|"unsupported"|"absent", '
    '"corrected_meta": string|null, "reason": string}. '
    '"corrected_meta" must be a comma-separated "Author, Venue, Year" string when '
    'the metadata is wrong AND the page itself reveals the correct values, '
    'otherwise null. "year_ok"/"author_ok"/"venue_ok" must be false only when '
    'the page text contradicts the claim, not when the value is merely absent.'
)

_META_RE = re.compile(
    r"(?P<meta>\([^()\n]{0,180}?\))\s*\[(?P<url>https?://[^\s\]<>']+)\]"
)
_PLAIN_URL_RE = re.compile(r"(?<!\w)(https?://[^\s\]<>()]+)")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

_TAGLINE = "\n\n<details>\n<summary>Source verification</summary>"


def _critic_completion(system, user, mode="gpu", max_tokens=600):
    """Secondary, non-streamed, low-temperature LLM call."""
    payload = {
        "model": M.server_model_id(mode),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stream": False,
    }
    try:
        r = requests.post(M.server_url(mode), json=payload, timeout=120)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[critic] LLM call failed (mode={mode}): {e}")
        return None


def _parse_verdict(text):
    """Best-effort JSON parse of the critic's reply. Returns None on failure."""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except (ValueError, TypeError):
        return None


def extract_citations(answer):
    """Return a list of citation dicts found in the answer.

    Each dict is ``{idx, start, end, url, meta, prepared}`` where ``idx`` is the
    paragraph index in ``answer`` split on blank lines, ``start``/``end`` are
    offsets inside that paragraph, and ``meta`` is the "(Author, Venue, Year)"
    text (or None for a bare URL). Duplicate URLs are ignored.
    """
    citations = []
    seen = set()
    paras = re.split(r"\s*\n\s*\n\s*", answer or "")
    for idx, para in enumerate(paras):
        stripped = _CODE_FENCE_RE.sub("", para)
        structured = []
        for m in _META_RE.finditer(stripped):
            url = m.group("url").rstrip(".,;:]")
            if url in seen:
                continue
            seen.add(url)
            item = {
                "idx": idx,
                "start": m.start(),
                "end": m.end(),
                "url": url,
                "meta": m.group("meta").strip(),
            }
            citations.append(item)
            structured.append(url.rstrip(".,;:]"))
        for m in _PLAIN_URL_RE.finditer(stripped):
            url = m.group(0).rstrip(".,;:]")
            if url in seen or url in structured:
                continue
            seen.add(url)
            citations.append({
                "idx": idx,
                "start": m.start(),
                "end": m.end(),
                "url": url,
                "meta": None,
            })
    return citations


def _fetch_source(url, max_chars=6000):
    """Fetch a single source page; never raises. ``ok=False`` on any failure."""
    try:
        page = json.loads(M.fetch_page(url, max_chars=max_chars, chunk=1))
    except Exception as e:
        return {"ok": False, "url": url, "error": f"fetch failed: {e}"}
    if page.get("error"):
        return {"ok": False, "url": url, "error": page["error"]}
    return {
        "ok": True,
        "url": url,
        "final_url": page.get("url", url),
        "title": page.get("title", ""),
        "content": page.get("content", "") or "",
    }


def _verify_source(cit, src, mode):
    """Single critic call for one citation. Returns (verdict, failed)."""
    if not (src and src.get("ok")):
        return {
            "exists": False,
            "url_ok": False,
            "claim_support": "absent",
            "corrected_meta": None,
            "reason": (src or {}).get("error") or "source could not be fetched",
        }, False
    body = src.get("content", "") or ""
    if not body.strip():
        return {
            "exists": True,
            "url_ok": True,
            "claim_support": "absent",
            "corrected_meta": None,
            "reason": "page content unreachable (likely JS-only or blocked)",
        }, False
    user = (
        f"CLAIMED CITATION:\n"
        f"URL: {cit['url']}\n"
        f"Metadata: {cit.get('meta') or '(none given)'}\n\n"
        f"CLAIM CONTEXT:\n{cit.get('prepared') or cit.get('para') or ''}\n\n"
        f"FETCHED SOURCE (title: {src.get('title', '')}):\n"
        f"{body[:M.VERIFY_FETCH_CHARS]}"
    )
    text = _critic_completion(_VERIFY_SYSTEM, user, mode)
    if text is None:
        return None, True
    verdict = _parse_verdict(text)
    if verdict is None:
        return None, True
    return verdict, False


def classify(verdict, src):
    """Map a critic verdict (+ fetch info) to an action token."""
    if not verdict:
        return "UNCHECKED"
    if verdict.get("exists") is False or verdict.get("url_ok") is False:
        return "UNVERIFIABLE"
    if not (src and src.get("ok")) or not (src.get("content") or "").strip():
        return "AMBIGUOUS"
    support = verdict.get("claim_support", "absent")
    if support in ("unsupported", "absent"):
        return "UNVERIFIABLE"
    if any(verdict.get(f) is False for f in ("author_ok", "venue_ok", "year_ok")):
        return "METADATA_FIX"
    if support == "partial":
        return "AMBIGUOUS"
    return "KEEP"


def _search_and_fetch(url, meta):
    """Targeted re-search for a corrected source; prefers the same host."""
    host = ""
    try:
        from urllib.parse import urlparse

        host = urlparse(url).netloc
    except Exception:
        pass
    query = f"{meta or url} {url}".strip()
    try:
        res = json.loads(M.web_search(query))
    except Exception:
        res = {}
    results = res.get("results", [])
    for r in results:
        u = r.get("url", "")
        r_host = ""
        try:
            from urllib.parse import urlparse

            r_host = urlparse(u).netloc
        except Exception:
            pass
        if (not host or r_host == host) and r.get("full_content"):
            return {"ok": True, "url": u, "title": r.get("page_title", ""), "content": r.get("full_content", "")}
    for r in results:
        if r.get("full_content"):
            return {"ok": True, "url": r.get("url", ""), "title": r.get("page_title", ""), "content": r.get("full_content", "")}
    return {"ok": False, "url": url}


def _verify_one(cit, mode):
    """Full per-citation pipeline. Returns (action, note, replace, verdict)."""
    src = _fetch_source(cit["url"])
    verdict, failed = _verify_source(cit, src, mode)
    if failed:
        return ("UNCHECKED",
                "critic unavailable — not verified",
                None, None)
    action = classify(verdict, src)

    if action == "METADATA_FIX" and not (verdict or {}).get("corrected_meta"):
        best = None
        for _ in range(max(1, M.VERIFY_RETRIES)):
            src2 = _search_and_fetch(cit["url"], cit.get("meta") or "")
            if not src2.get("ok"):
                continue
            v2, f2 = _verify_source(cit, src2, mode)
            if f2 or not v2:
                continue
            if v2.get("corrected_meta"):
                best = v2
                src = src2
                break
        if best:
            verdict = best
        else:
            verdict = {**verdict, "corrected_meta": None}

    if action == "UNVERIFIABLE" and verdict and src and not src.get("ok"):
        # Transient fetch failure? One more direct fetch before stripping.
        src2 = _fetch_source(cit["url"])
        v2, f2 = _verify_source(cit, src2, mode)
        if not f2 and v2 and v2.get("exists"):
            verdict = v2
            src = src2
            action = classify(v2, src2)

    if action == "METADATA_FIX":
        corrected = (verdict or {}).get("corrected_meta")
        claimed_meta = cit.get("meta")
        if corrected and claimed_meta:
            replace = f"({corrected}) [{cit['url']}]"
            note = f"metadata corrected to ({corrected})"
        elif claimed_meta:
            replace = f"(Author, Venue, uncertain) [{cit['url']}]"
            note = "metadata could not be confirmed — marked uncertain"
        else:
            replace = None
            note = "bare source URL without metadata — could not confirm metadata"
    elif action == "UNVERIFIABLE":
        replace = ""
        note = (verdict or {}).get("reason") or "source could not be confirmed — citation removed"
    elif action == "AMBIGUOUS":
        replace = None
        note = "claim only partially supported, or sources conflict — review"
        _maybe = (verdict or {}).get("corrected_meta")
        if _maybe:
            note = f"metadata uncertain ({_maybe}) — review"
    else:  # KEEP / UNCHECKED
        replace = None
        note = "verified"
    return action, note, replace, verdict


def _build_verification_block(verdicts):
    if not verdicts:
        return ""
    marks = {
        "KEEP": "✓",
        "METADATA_FIX": "△",
        "UNVERIFIABLE": "✗",
        "AMBIGUOUS": "⚠",
        "UNCHECKED": "∅",
    }
    lines = [_TAGLINE]
    for v in verdicts:
        action = v.get("action", "KEEP")
        symbol = marks.get(action, "•")
        note = f" — {v['note']}" if v.get("note") else ""
        lines.append(f"{symbol} {v['url']}{note}")
    lines.append("</details>")
    return "\n".join(lines)


def run_verification(task_id, sid, answer, mode="gpu"):
    """Extract citations, verify each, patch the answer, return (final, verdicts).

    Research tasks only; non-research answers and citation-free answers pass
    through untouched. Never raises.
    """
    with M._data_lock:
        t = M.tasks.get(task_id) or {}
    verdicts = []
    if not answer or not t.get("research"):
        return answer, verdicts

    paras = re.split(r"\s*\n\s*\n\s*", answer)
    citations = extract_citations(answer)
    if not citations:
        return answer, verdicts

    per_para = {}
    for cit in citations:
        para = paras[cit["idx"]] if cit["idx"] < len(paras) else ""
        cit["prepared"] = para
        action, note, replace, verdict = _verify_one(cit, mode)
        verdicts.append({
            "url": cit["url"],
            "meta": cit.get("meta"),
            "action": action,
            "note": note,
            "corrected_meta": (verdict or {}).get("corrected_meta") if verdict else None,
            "reason": (verdict or {}).get("reason") if verdict else None,
        })
        if replace is not None:
            per_para.setdefault(cit["idx"], []).append({
                "start": cit["start"],
                "end": cit["end"],
                "replace": replace,
            })

    if per_para:
        for idx, edits in per_para.items():
            para = paras[idx]
            parts = []
            pos = 0
            for e in sorted(edits, key=lambda x: x["start"]):
                parts.append(para[pos:e["start"]])
                parts.append(e["replace"])
                pos = e["end"]
            parts.append(para[pos:])
            paras[idx] = "".join(parts)
        answer = "\n\n".join(paras)

    block = _build_verification_block(verdicts)
    if block:
        answer = answer.rstrip() + block
    return answer, verdicts


def run_verification_worker(task_id, sid, answer, body, mode):
    """Thread-pool entry point called from the orchestration event loop.

    Guarantees the task always finalizes: the (possibly patched) answer on
    success, the original answer untouched on any failure.
    """
    started = time.time()
    try:
        final, verdicts = run_verification(task_id, sid, answer, mode)
        with M._data_lock:
            tt = M.tasks.get(task_id)
            if tt:
                tt["_verification"] = verdicts
                tt["_verification_duration"] = round(time.time() - started, 1)
        M._finalize_task(task_id, sid, final, body)
    except Exception as e:
        print(f"[critic] verification pass failed for task {task_id}: {e}")
        with M._data_lock:
            tt = M.tasks.get(task_id)
            if tt:
                tt["_verification"] = [{"url": "?", "action": "UNCHECKED",
                                        "note": f"verification pass failed: {e}"}]
        M._finalize_task(task_id, sid, answer, body)