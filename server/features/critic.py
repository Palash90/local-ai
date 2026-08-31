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

On top of the deterministic + critic checks, the finished answer is also passed
to the LLM judge (:func:`server.features.judge.llm_verify_research_answer`)
together with the user's original question, because citations are mandatory on
the research surface: the judge surfaces citation-free / off-topic replies,
screens the answer as generated output, and scores it (``QUALITY: NN/100``).
That verdict is transcribed into the verification trail (``JUDGE`` entry) AND
drives re-scheduling: unsafe / below-gate-quality / citation-free / unmet-image-
or-search answers are regenerated through the generation model with a steering
message (the judge verdict travels with it), bounded by ``VERIFY_MAX_RETRIES``.
Unsafe answers still failing after retries are declined instead of delivered;
every other exhausted budget delivers the last answer with the trail note.
"""

import json
import html
import re
import time
from urllib.parse import urlsplit

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
    r"(?P<meta>[\[(][^)\]\[(\n]{0,180}[\]\)])\s*\[(?P<url>https?://[^\s\]<>']+)\]"
)
# A citation whose URL slot is empty, e.g. `[ScienceDirect Review] []` or the
# markdown link form `[Some Review]()`. These are fabricated by construction —
# there is nothing to verify.
_EMPTY_CITE_RE = re.compile(r"(?P<meta>[\[(][^)\]\[(\n]{0,180}[\]\)])\s*\[\s*\]")
_PLAIN_URL_RE = re.compile(r"(?<!\w)(https?://[^\s\]<>()]+)")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

_TAGLINE = (
    '\n\n<details class="source-verification">'
    '<summary>Source verification</summary>'
    '<div class="source-verification-list">'
)

# Requirement-mismatch detection (re-scheduling). All three classes are decided
# deterministically from the user's own words + the task's recorded tool trace,
# so no extra judge call is burned:
# - the user asked for an image (or the answer claims an image was produced)
#   while the task produced no image file;
# - the user explicitly asked for citations/sources but the answer has none;
# - the user explicitly asked for web/search findings but web_search never ran.
_IMG_NEED_RE = re.compile(
    r"\b(image|pictures?|photos?|photo(?:graph)?(?:y)?|screenshots?|diagrams?|"
    r"charts?|graphs?|maps?|drawings?|illustrations?|figures?|visuals?|"
    r"visualisations?|visualizations?|logos?|memes?|portraits?|infographics?)\b",
    re.IGNORECASE,
)
_IMG_CLAIM_RE = re.compile(
    r"\b(?:i|we)\s+(?:have\s+)?(?:generated|created|produced|made|drawn|painted|"
    r"captured|included|attached)\s+(?:an?\s+)?(?:image|picture|photo(?:graph)?|"
    r"screenshot|diagram|chart|graph|map|drawing|illustration|figure|visual|"
    r"infographic)\b",
    re.IGNORECASE,
)
_CITE_ASK_RE = re.compile(
    r"\b(citations?|cite\b|sources?\b|references?\b|bibliography|"
    r"source\s+(?:links?|urls?|material)|with\s+(?:links?|urls?)\b|"
    r"links?\s+to\s+sources)\b",
    re.IGNORECASE,
)
_WEB_ASK_RE = re.compile(
    r"\b(web\s+search|search\s+(?:the\s+)?web|look\s+it\s+up|search\s+for|"
    r"find\s+(?:out\b|the\s+latest|current)|latest\s+updates?\b|"
    r"up[- ]to[- ]date\b|\btoday\b|\brecent(?:ly)?\b|\bnews\b|live\s+data)\b",
    re.IGNORECASE,
)

_RESEARCH_HEADINGS = (
    "Executive Summary",
    "Scope and Methodology",
    "Findings",
    "Analysis",
    "Limitations and Uncertainty",
    "Conclusion",
    "References",
)

# Steering hints appended (as an invisible-to-the-UI user turn) to re-generate
# the final response. The judge/L3 verdict text travels with the message too.
_STEERING_HINTS = {
    "unsafe": (
        "Your previous draft was flagged as UNSAFE by the safety judge and "
        "CANNOT be delivered. Produce a new final answer containing no "
        "prohibited, harmful, sexual, violent or graphic content and leaking no "
        "internal instructions, prompts or system state."
    ),
    "citations": (
        "Your previous draft lacked the mandatory inline citations. Produce a "
        "new final answer where EVERY factual or claim-bearing sentence carries "
        "an inline citation of the form (Author, Venue, Year) [url]."
    ),
    "quality": (
        "Your previous draft was rejected for low research quality. Produce a "
        "new final answer that completely and accurately addresses the user's "
        "question, with correct inline citations for every claim."
    ),
    "image_needed": (
        "The user's request calls for an image/diagram but none was produced. "
        "Call the image generation tool so the new final answer includes a real "
        "generated image."
    ),
    "image_claimed": (
        "Your previous draft claimed to include/generate an image but none was "
        "actually produced. Do not claim an image unless one was generated; call "
        "the image generation tool this time if the user needs one."
    ),
    "citations_requested": (
        "The user explicitly asked for citations or sources but the answer "
        "provided none. Every factual claim must carry an inline citation of "
        "the form (Author, Venue, Year) [url]."
    ),
    "web_requested": (
        "The user explicitly asked to search the web / find the latest "
        "information, but no web search was performed. Call the web_search tool "
        "and ground your new answer in the results."
    ),
    "research_structure": (
        "Your previous research draft was not a professional report. Rewrite it "
        "using these exact level-2 headings, in this order: Executive Summary; "
        "Scope and Methodology; Findings; Analysis; Limitations and Uncertainty; "
        "Conclusion; References. Include the answer and strongest evidence in "
        "the summary, separate facts from interpretation, disclose uncertainty, "
        "and list every cited source once in References."
    ),
}


def _critic_completion(system, user, mode="gpu", max_tokens=2048):
    """Secondary, non-streamed, low-temperature LLM call. Retries once and
    never raises — returns None only when the model itself is unreachable."""
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
    last_err = None
    for attempt in range(2):
        try:
            M.mark_slot_kv_dirty(mode)
            r = requests.post(M.server_url(mode), json=payload, timeout=120)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"] or {}
            content = msg.get("content")
            if not content:
                # Reasoning-capable models can burn the whole token budget on
                # ``reasoning_content`` before emitting the verdict (same
                # failure the L2 judge solves in mcp_gateway._judge_call).
                # Fall back to the reasoning text and retry with a doubled
                # budget instead of silently dropping the verdict.
                reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
                if reasoning.strip():
                    print(
                        f"[critic] empty content but reasoning present — "
                        "judging on reasoning text"
                    )
                    return reasoning
                payload["max_tokens"] = max_tokens * 2
                last_err = "empty content in response"
            else:
                return content
        except Exception as e:
            last_err = str(e)
        print(f"[critic] LLM call failed (mode={mode}, attempt {attempt + 1}/2): {last_err}")
        time.sleep(1.0)
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
        for m in _EMPTY_CITE_RE.finditer(stripped):
            # Empty-URL citations are individually meaningful (each must be
            # stripped and flagged), so no URL-based dedupe applies here.
            citations.append({
                "idx": idx,
                "start": m.start(),
                "end": m.end(),
                "url": "",
                "meta": m.group("meta").strip(),
            })
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


def _norm_url(url):
    """Normalize a URL for equality checks: lowercase host, drop fragment and
    trailing slash, keep path and query."""
    try:
        from urllib.parse import urlsplit

        p = urlsplit(url or "")
        path = p.path.rstrip("/") or "/"
        parts = [p.netloc.lower() or url, path]
        if p.query:
            parts.append(f"?{p.query}")
        return "".join(parts)
    except Exception:
        return (url or "").rstrip("/")


def _is_search_endpoint(url):
    """Return True for the search UI/API URL, which is not a source page."""
    try:
        from urllib.parse import urlsplit

        candidate = urlsplit(url or "")
        configured = urlsplit(getattr(M, "SEARXNG_PUBLIC_URL", ""))
        internal = urlsplit(getattr(M, "SEARXNG_URL", ""))
        for endpoint in (configured, internal):
            if endpoint.netloc and candidate.netloc.lower() == endpoint.netloc.lower():
                endpoint_path = endpoint.path.rstrip("/") or "/"
                candidate_path = candidate.path.rstrip("/") or "/"
                if candidate_path == endpoint_path:
                    return True
    except Exception:
        pass
    return False


def _retrieved_urls(task_id):
    """All URLs the research agent actually opened or saw in search results.

    Rebuilt from the task's ``_search_details`` (every ``web_search`` result
    URL plus every ``fetch_page`` URL and any link inside fetched content). A
    citation that is NOT in here was never grounded by the agent's own tools.
    """
    urls = set()
    with M._data_lock:
        details = list(M.tasks.get(task_id, {}).get("_search_details", []))
    for entry in details:
        if not isinstance(entry, dict):
            continue
        if entry.get("tool") == "fetch_page":
            u = entry.get("url", "")
            if u:
                urls.add(_norm_url(u))
            for m in _PLAIN_URL_RE.finditer(entry.get("content", "") or ""):
                urls.add(_norm_url(m.group(0).rstrip(".,;:]")))
            continue
        for r in entry.get("results", []) or []:
            if isinstance(r, dict):
                u = r.get("url") or r.get("link") or ""
                if u:
                    urls.add(_norm_url(u))
    return urls


def _citation_exists(url):
    """Existence probe for a source the agent never retrieved.

    Search indexes rarely return deep links (PMC article pages, hospital
    blogs) verbatim, so a search-only probe brands real sources as
    "likely fabricated". Try a direct fetch first: a page that loads — or a
    bot-block (403/405/429) — proves the URL exists. Only when the fetch
    fails AND no verification search finds the URL do we call it fabricated.
    """
    fetched = _fetch_source(url, max_chars=500)
    if fetched.get("ok") and (fetched.get("content") or "").strip():
        return True
    err = (fetched.get("error") or "").lower()
    if any(tok in err for tok in ("403", "405", "429", "forbidden",
                                  "captcha", "cloudflare", "blocked")):
        print(f"[critic] existence probe: {url} bot-blocked from direct "
              "fetch — treating as existing")
        return True
    try:
        res = json.loads(M.web_search(url))
    except Exception as e:
        print(f"[critic] existence probe failed for {url}: {e}")
        return False
    target = _norm_url(url)
    for r in res.get("results", []) or []:
        if isinstance(r, dict):
            for u in (r.get("url"), r.get("link")):
                if u and _norm_url(u) == target:
                    return True
    return False


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

    if action == "UNVERIFIABLE" and verdict and not (src and src.get("ok")):
        # The page could not be fetched directly (403/404/timeout). Before
        # stripping, run targeted re-searches so a real but blocked/retired
        # source gets a second chance (e.g. via search snippets or mirrors).
        for _ in range(max(1, M.VERIFY_RETRIES)):
            src2 = _search_and_fetch(cit["url"], cit.get("meta") or "")
            if not src2.get("ok"):
                continue
            v2, f2 = _verify_source(cit, src2, mode)
            if not f2 and v2 and v2.get("exists"):
                verdict = v2
                src = src2
                action = classify(v2, src2)
                break

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
        "JUDGE": "⚖",
    }
    lines = [_TAGLINE]
    for v in verdicts:
        action = v.get("action", "KEEP")
        symbol = marks.get(action, "•")
        url = v.get("url") or ""
        try:
            parsed = urlsplit(url)
            label = parsed.netloc + (parsed.path.rstrip("/") or "/")
        except Exception:
            label = url
        if len(label) > 72:
            label = label[:69] + "..."
        safe_url = html.escape(url, quote=True)
        safe_label = html.escape(label or "No URL")
        safe_note = html.escape(v.get("note") or "")
        note = f'<span class="source-verification-note">{safe_note}</span>' if safe_note else ""
        link = (
            f'<a href="{safe_url}" target="_blank" rel="noreferrer">'
            f"{safe_label}</a>"
            if url else f'<span class="source-verification-empty">{safe_label}</span>'
        )
        lines.append(
            f'<div class="source-verification-item source-{action.lower()}">'
            f'<span class="source-verification-mark">{symbol}</span>{link}{note}</div>'
        )
    lines.append("</div></details>")
    return "\n".join(lines)


def _judge_research_answer(task_id, answer):
    """Optional LLM judge pass over the finished research answer, in context of
    the user's original question — citations are mandatory on the research
    surface. Best-effort: any failure yields no verdict and never blocks
    delivery; the result becomes one transparent ``JUDGE`` item in the
    verification trail, not a hard gate."""
    try:
        from server.features.judge import llm_verify_research_answer
    except Exception as e:
        print(f"[critic] research-answer judge unavailable: {e}")
        return None
    with M._data_lock:
        user_input = (M.tasks.get(task_id) or {}).get("_original_message", "")
    try:
        result = llm_verify_research_answer(user_input, answer)
    except Exception as e:
        print(f"[critic] research-answer judge call failed: {e}")
        result = None
    if not result:
        with M._data_lock:
            tt = M.tasks.get(task_id)
            if tt:
                tt["_judge_result"] = None
        return None
    with M._data_lock:
        tt = M.tasks.get(task_id)
        if tt:
            tt["_judge_result"] = result
    if result.get("unsafe"):
        note = "LLM judge flagged the answer as unsafe"
    elif result.get("ok"):
        note = "LLM judge: answer addresses the question with inline citations"
    elif result.get("citations") is False:
        note = "LLM judge: answer lacks mandatory inline citations"
    else:
        note = "LLM judge reply not recognized — review"
    quality = result.get("quality")
    if quality is not None:
        note = f"{note} (quality {quality}/100)"
    return {
        "url": "",
        "meta": None,
        "action": "JUDGE",
        "note": note,
        "corrected_meta": None,
        "reason": result.get("reason"),
        "model": result.get("model"),
    }


def _finalize_verdicts(task_id, answer, verdicts):
    """Append the LLM final-answer judge verdict, then attach the
    verification trail. Returns (answer, verdicts)."""
    with M._data_lock:
        t = M.tasks.get(task_id) or {}
    if t.get("research") and answer:
        jv = _judge_research_answer(task_id, answer)
        if jv:
            verdicts.append(jv)
    elif answer and not t.get("research"):
        # Interactive (non-research) answers get the general quality gate too,
        # so every delivered reply is judged against the user's own request.
        jv = _judge_answer_quality(task_id, answer)
        if jv:
            verdicts.append(jv)
    block = _build_verification_block(verdicts)
    # Only research answers carry the visible source-verification trail (the
    # citation-by-citation <details> block). Interactive/non-research answers
    # are judged but their text is left untouched — the quality verdict is
    # recorded on the task/verification trail, not spliced into the reply.
    if block and t.get("research"):
        answer = answer.rstrip() + block
    return answer, verdicts


def _judge_answer_quality(task_id, answer):
    """General final-answer quality judge for interactive (non-research)
    answers. Graded against the user's request with a general rubric; the
    verdict's ``quality`` drives the bounded re-run decision and ``unsafe``
    drives a decline. Best-effort: any failure stores no verdict and never
    blocks delivery."""
    try:
        from server.features.judge import llm_verify_answer_quality, resolve_judge_model
    except Exception as e:
        print(f"[critic] answer-quality judge unavailable: {e}")
        return None
    with M._data_lock:
        t = M.tasks.get(task_id) or {}
        user_input = t.get("_original_message", "")
        user = t.get("_user", "")
    try:
        result = llm_verify_answer_quality(
            user_input, answer,
            model_id=resolve_judge_model(user or ""),
        )
    except Exception as e:
        print(f"[critic] answer-quality judge call failed: {e}")
        result = None
    if not result:
        with M._data_lock:
            tt = M.tasks.get(task_id)
            if tt:
                tt["_judge_result"] = None
        return None
    with M._data_lock:
        tt = M.tasks.get(task_id)
        if tt:
            tt["_judge_result"] = result
    if result.get("unsafe"):
        note = "LLM quality judge flagged the answer as unsafe"
    else:
        note = "LLM quality judge: answer addresses the user's request"
    quality = result.get("quality")
    if quality is not None:
        note = f"{note} (quality {quality}/100)"
    return {
        "url": "",
        "meta": None,
        "action": "JUDGE",
        "note": note,
        "corrected_meta": None,
        "reason": result.get("reason"),
        "model": result.get("model"),
    }


def run_verification(task_id, sid, answer, mode="gpu"):
    """Verify a final generated answer and return (final, verdicts).

    Every final answer is judged against the user's request: the citation
    fact-check runs for answers carrying inline citations (research), and the
    general quality judge runs for non-research answers. Citation-free and
    non-research answers still pass through the final-answer judge via
    ``_finalize_verdicts``. Never raises.
    """
    with M._data_lock:
        t = M.tasks.get(task_id) or {}
    verdicts = []
    if not answer:
        return answer, verdicts

    paras = re.split(r"\s*\n\s*\n\s*", answer)
    citations = extract_citations(answer)
    if not citations:
        return _finalize_verdicts(task_id, answer, verdicts)

    # Deterministic anti-fabrication gate, computed ONCE per task: the set of
    # URLs the agent genuinely retrieved, and per-URL usage so a single source
    # backing many separate claims is surfaced. This runs before any critic
    # LLM call and is decisive even when the critic model is unavailable.
    retrieved = _retrieved_urls(task_id)
    from collections import Counter

    # Count every inline citation occurrence in the raw answer (not the
    # deduped citation list) so a single URL backing several claims is
    # surfaced, even when the parser dedupes the repeated citation.
    usage = Counter()
    for m in _META_RE.finditer(_CODE_FENCE_RE.sub("", answer or "")):
        usage[m.group("url").rstrip(".,;:]")] += 1
    per_cite_usage = {c["url"]: usage.get(c["url"], 0) for c in citations if c["url"]}
    max_cites = int(getattr(M, "VERIFY_MAX_CITES_PER_URL", 3))

    per_para = {}
    for cit in citations:
        para = paras[cit["idx"]] if cit["idx"] < len(paras) else ""
        cit["prepared"] = para
        pre_action = None
        pre_note = ""
        if not cit["url"]:
            pre_action, pre_note = "UNVERIFIABLE", (
                "citation has no URL to verify — cannot exist"
            )
        elif _is_search_endpoint(cit["url"]):
            pre_action, pre_note = "UNVERIFIABLE", (
                "search endpoint is navigation, not a fetched source page"
            )
        elif cit["url"] not in retrieved:
            if not _citation_exists(cit["url"]):
                pre_action, pre_note = "UNVERIFIABLE", (
                    "URL was never retrieved by research and no verification "
                    "search could find it — likely fabricated"
                )
        over = per_cite_usage.get(cit["url"], 0)
        over_note = (""
                     if over <= max_cites
                     else f" SAME SOURCE CITED {over} TIMES for {over} separate claims — verify each maps to it.")

        if pre_action is not None:
            action, note, replace, verdict = pre_action, pre_note.strip(), "", None
        else:
            action, note, replace, verdict = _verify_one(cit, mode)
        if over_note and action != "UNVERIFIABLE":
            note = (note or "") + over_note
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

    return _finalize_verdicts(task_id, answer, verdicts)


def _requirement_mismatch(task_id, user_input, answer):
    """Return a retry ``reason`` when the answer falls short of an explicit user
    requirement that a steering re-run could satisfy, else None.

    Detects report and request mismatches (deterministic, no judge call):
    - the request needs an image, or the answer claims one was generated, while
      the task produced no image file;
    - the user explicitly asked for citations/sources but the answer has none;
    - the user explicitly asked for web/search findings but no ``web_search``
      tool ran this task.
    """
    user_input = (user_input or "").strip()
    if not user_input:
        return None
    with M._data_lock:
        t = M.tasks.get(task_id) or {}
        tools_used = list(t.get("_tools_used", []) or [])
        image_file = t.get("image_file")
        is_research = bool(t.get("research"))
    if is_research:
        headings = [
            m.group(1).strip()
            for m in re.finditer(r"^##\s+(.+?)\s*$", answer or "", re.MULTILINE)
        ]
        if any(name not in headings for name in _RESEARCH_HEADINGS):
            return "research_structure"
        positions = [headings.index(name) for name in _RESEARCH_HEADINGS]
        if positions != sorted(positions):
            return "research_structure"
    has_image = bool(image_file)
    if _IMG_NEED_RE.search(user_input) and not has_image:
        return "image_needed"
    if _IMG_CLAIM_RE.search(answer or "") and not has_image:
        return "image_claimed"
    if _CITE_ASK_RE.search(user_input):
        if not any(c.get("url") for c in extract_citations(answer)):
            return "citations_requested"
    if _WEB_ASK_RE.search(user_input) and "web_search" not in tools_used:
        return "web_requested"
    return None


def _retry_decision(task_id, judge_result, mismatch_reason):
    """Decide what ``run_verification_worker`` must do with the judged answer.

    Returns ``(action, reason)`` with ``action`` ∈ {"finalize", "retry",
    "decline"}. Policy (fail-open, always bounded):
    - unsafe verdict → retry up to ``VERIFY_MAX_RETRIES`` (counting every prior
      judge/requirement retry), then decline — never deliver.
    - requirement mismatch / judge NO_CITATIONS → one steering re-run, then
      deliver regardless.
    - quality below ``VERIFY_QUALITY_GATE`` → retry up to ``VERIFY_MAX_RETRIES``,
      then deliver the last answer.
    - judge unavailable or clear verdict → finalize.
    """
    gate = int(getattr(M, "VERIFY_QUALITY_GATE", 70))
    allow = int(getattr(M, "VERIFY_MAX_RETRIES", 2))
    with M._data_lock:
        t = M.tasks.get(task_id) or {}
        verify_done = t.get("_verify_done", 0)
        mismatch_done = t.get("_mismatch_done", 0)

    unsafe = bool(judge_result and judge_result.get("unsafe"))
    no_cites = bool(judge_result and judge_result.get("citations") is False)
    quality = (judge_result or {}).get("quality")

    if unsafe:
        if verify_done + mismatch_done < allow:
            return "retry", "unsafe"
        return "decline", "unsafe"
    if mismatch_reason:
        if mismatch_done < 1:
            return "retry", mismatch_reason
        return "finalize", mismatch_reason
    if no_cites:
        if mismatch_done < 1:
            return "retry", "citations"
        return "finalize", "citations"
    if quality is not None and quality < gate:
        if verify_done < allow:
            return "retry", "quality"
        return "finalize", "quality"
    return "finalize", None


def _reschedule(task_id, sid, round_num, reason, judge_result):
    """Re-generate the final answer through the generation model.

    Appends one steering user turn (flagged ``_steering`` so the UI never shows
    it) to the session — the rejected answer was never appended, so the model
    re-runs from the clean tool trail — and re-invokes ``_start_llm_round`` with
    the SAME round number so the task's tool-loop budget isn't consumed by a
    retry. The judge / L3 verdict travels with the steering message. Bounded by
    the counters in ``_retry_decision``.
    """
    with M._data_lock:
        t = M.tasks.get(task_id) or {}
        if reason in ("unsafe", "quality"):
            t["_verify_done"] = t.get("_verify_done", 0) + 1
        else:
            t["_mismatch_done"] = t.get("_mismatch_done", 0) + 1

    hint = _STEERING_HINTS.get(
        reason, "Produce a new final answer that fully addresses the user's request."
    )
    steering = (
        "[SYSTEM NOTE — internal revision. Your previous draft was rejected "
        f"and must NOT be reused or repeated. Reason: {reason.replace('_', ' ')}. "
        f"{hint}]"
    )
    qual = (judge_result or {}).get("quality", 0)
    if reason == "quality" and isinstance(qual, int):
        steering += f"\n\n[Quality score received: {qual}/100 — raise it above the gate.]"
    if judge_result and judge_result.get("reason"):
        steering += (
            "\n\n[Verification verdict (L3 judge): "
            f"{(judge_result.get('reason') or '')[:600]}]"
        )

    with M._data_lock:
        if sid in M.sessions:
            M.sessions[sid].append({"role": "user", "content": steering, "_steering": True})
            M.sessions_meta.setdefault(sid, {})["updated"] = time.time()
    M.save_sessions()
    M.set_status(task_id, f"Re-running ({reason.replace('_', ' ')})...")
    print(
        f"[critic] re-scheduling task {task_id} (reason={reason}, round={round_num})"
    )
    M._start_llm_round(task_id, sid, round_num)


def run_verification_worker(task_id, sid, answer, body, mode):
    """Thread-pool entry point called from the orchestration event loop.

    Runs the critic + judge pass, then decides whether to finalize, re-schedule
    (steering re-generation) or decline:
    - success/clear verdict → ``_finalize_task`` with the patched answer;
    - deficient answer → ``_reschedule`` (same round, judge verdict attached);
    - unsafe still after retries → ``_set_task_error`` (decline, never deliver);
    - any failure → finalize with the original answer untouched.
    """
    started = time.time()
    try:
        final, verdicts = run_verification(task_id, sid, answer, mode)
        with M._data_lock:
            t = M.tasks.get(task_id)
            if not t:
                return
            t["_verification"] = verdicts
            t["_verification_duration"] = round(time.time() - started, 1)
            judge_result = t.get("_judge_result")
            round_num = t.get("_round", 0)
            user_input = t.get("_original_message", "")
        mismatch_reason = _requirement_mismatch(task_id, user_input, answer)
        action, reason = _retry_decision(task_id, judge_result, mismatch_reason)

        if action == "retry":
            _reschedule(task_id, sid, round_num, reason, judge_result)
            return
        if action == "decline":
            print(
                f"[critic] declining unsafe answer for task {task_id} after "
                "retry budget exhausted"
            )
            M._set_task_error(
                task_id,
                "I can't deliver this answer: the safety judge flagged it as "
                "unsafe after verification retries.",
                sid,
            )
            return
        M._finalize_task(task_id, sid, final, body)
    except Exception as e:
        print(f"[critic] verification pass failed for task {task_id}: {e}")
        with M._data_lock:
            tt = M.tasks.get(task_id)
            if tt:
                tt["_verification"] = [{"url": "?", "action": "UNCHECKED",
                                        "note": f"verification pass failed: {e}"}]
        M._finalize_task(task_id, sid, answer, body)


_PEER_REVIEW_TIMEOUT = 300

_PEER_REVIEW_PROMPT = (
    "You are {peer}, peer-reviewing a reply written by your co-writer {author} "
    "as part of your shared creative work.\n\n"
    "Reply from {author}:\n"
    "----------\n{answer}\n----------\n\n"
    "Review it like a demanding co-writer: does it deliver what was asked, is "
    "it in-character, coherent, and free of placeholder or meta text?\n\n"
    "Reply with exactly:\n\n"
    "PEER VERDICT: PASS or FLAG\n"
    "CONFIDENCE: NN/100\n"
    "NOTES: <one or two short lines>"
)


def _parse_peer_verdict(text):
    """Parse the peer-review contract reply → (verdict, confidence, notes)."""
    text = text or ""
    verdict = "PASS"
    if re.search(r"PEER VERDICT\s*:\s*FLAG", text, flags=re.IGNORECASE):
        verdict = "FLAG"
    elif re.search(r"\bFLAG\b", text, flags=re.IGNORECASE) and not re.search(
        r"\bPASS\b", text, flags=re.IGNORECASE
    ):
        verdict = "FLAG"
    confidence = None
    m = re.search(r"CONFIDENCE\s*:\s*(\d{1,3})", text, flags=re.IGNORECASE)
    if m:
        confidence = max(0, min(100, int(m.group(1))))
    notes = ""
    m = re.search(r"NOTES\s*:\s*(.+)", text, flags=re.IGNORECASE | re.DOTALL)
    if m:
        notes = m.group(1).strip()[:500]
    return verdict, confidence, notes


def run_peer_review_worker(task_id, sid, answer, body, mode="cpu"):
    """Full cross-agent critique round for background agent replies (Kaya↔Kolpo).

    The peer agent reviews the reply as a dedicated LLM round on the CPU
    llama-server. The call goes DIRECTLY to the lane's server (not through
    ``/api/chat``): the reviewed task is still the cpu lane's current task, so
    a queued peer task could never start — the lane serializes one task at a
    time and the direct call sidesteps that queue entirely (the server itself
    is free while the review runs).

    The verdict is stored as the task's ``_judge_result`` so the finalize path
    surfaces it as the message's confidence chip. Fail-open everywhere: no
    peer configured, a down lane server or an empty reply falls back to the
    generic per-user quality judge, and the reply is always finalized.
    """
    from server.config import AGENT_PEER_MAP

    try:
        with M._data_lock:
            t = M.tasks.get(task_id) or {}
            author = t.get("_user", "")
        peer = (AGENT_PEER_MAP or {}).get(author)
        if not peer:
            print(
                f"[peer-review] no peer configured for {author!r} — falling "
                "back to the quality judge"
            )
            _judge_answer_quality(task_id, answer)
            M._finalize_task(task_id, sid, answer, body)
            return

        prompt = _PEER_REVIEW_PROMPT.format(
            peer=peer, author=author, answer=(answer or "")[:6000]
        )
        try:
            base = M.server_base("cpu")
            payload = {
                "model": M.server_model_id("cpu"),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": 1024,
                "stream": False,
            }
            r = requests.post(
                f"{base}/v1/chat/completions",
                json=payload,
                timeout=_PEER_REVIEW_TIMEOUT,
            )
            r.raise_for_status()
            reply = (
                (r.json().get("choices") or [{}])[0]
                .get("message", {})
                .get("content", "")
                or ""
            )
            if not reply:
                raise RuntimeError("peer round returned an empty reply")
            verdict, confidence, notes = _parse_peer_verdict(reply)
            print(
                f"[peer-review] {peer} verdict={verdict} "
                f"confidence={confidence if confidence is not None else 'n/a'} "
                f"notes={notes[:120]!r}"
            )
            with M._data_lock:
                tt = M.tasks.get(task_id)
                if tt:
                    tt["_judge_result"] = {
                        "quality": confidence,
                        "ok": verdict == "PASS",
                        "peer": peer,
                        "flags": [] if verdict == "PASS"
                        else [notes or "peer flagged the reply"],
                        "reason": notes or f"peer review by {peer}",
                        "model": f"peer-review:{peer}",
                    }
        except Exception as e:
            print(
                f"[peer-review] peer round failed (fail-open) — falling back "
                f"to the quality judge: {e}"
            )
            _judge_answer_quality(task_id, answer)
        M._finalize_task(task_id, sid, answer, body)
    except Exception as e:
        print(f"[peer-review] worker error for task {task_id}: {e}")
        try:
            M._finalize_task(task_id, sid, answer, body)
        except Exception:
            pass
