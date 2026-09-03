"""Web-search orchestration, in-memory caches, and enrichment."""

import concurrent.futures
import json
import os
import re
import threading
import time
from datetime import datetime
from urllib.parse import urlencode

import requests

from server.mcp_client import mcp_manager
from server.features.state import M
from server.features.urlclassify import scrub_search_results
from server.features.websearch import fetch_page
from server.features.websearch import relevance
from server.features.websearch import vector_store as page_cache
from server.features.websearch.relevance import _query_tokens

_PLACE_HINTS = relevance._PLACE_HINTS

WEB_SEARCH_RESULT_LIMIT = 10
WEB_SEARCH_ENRICH_TOP = 6
WEB_SEARCH_ENRICH_CHARS = 6000
WEB_SEARCH_ENRICH_TIMEOUT = 25
# Search-result cache and outbound pacing for web_search. Non-time-sensitive
# results are reused for a day for exact and near-duplicate queries;
# time-sensitive queries are only reused within a short window. Outbound
# fetches are paced globally so bursts from the research agents cannot get
# the upstream engines rate-limited or CAPTCHA'd. Cache reads take only
# _CACHE_LOCK and never wait on pacing or on an in-flight fetch.
SEARCH_CACHE_MAX = 256
SEARCH_SIMILARITY_THRESHOLD = 0.7
# Keep requests to the local SearXNG instance spaced out. SearXNG fans one
# request out to several upstream engines, so a short interval here can still
# trigger upstream 429/403 responses when multiple queries arrive together.
# Override for a trusted/private deployment with WEB_SEARCH_MIN_INTERVAL.
SEARCH_MIN_INTERVAL = max(
    1.0, float(os.environ.get("WEB_SEARCH_MIN_INTERVAL", "20"))
)
SEARCH_INFLIGHT_WAIT = 30
_CACHE_LOCK = threading.Lock()
_PACE_LOCK = threading.Lock()
_SEARCH_LAST_FETCH = 0.0
_SEARCH_CACHE = {}
_IN_FLIGHT = {}


def _search_cache_get(norm_query, query, now):
    """Lock-free cache lookup; caller must hold _CACHE_LOCK."""
    ttl = page_cache.regex_ttl(query)
    entry = _SEARCH_CACHE.get(norm_query)
    if entry:
        # Prefer the per-entry TTL fixed when the answer was cached (which may
        # have come from the LLM classifier); fall back to the regex default.
        age = now - entry[0]
        if age <= (entry[2] if len(entry) > 2 else ttl):
            payload = dict(entry[1])
            payload["cached_result"] = True
            return payload
    tokens = set(norm_query.split())
    if not tokens:
        return None
    best_key, best_score = None, 0.0
    for key, (cached_at, _, store_ttl) in _SEARCH_CACHE.items():
        if now - cached_at > store_ttl:
            continue
        cand = set(key.split())
        if not cand:
            continue
        score = len(tokens & cand) / min(len(tokens), len(cand))
        if score > best_score:
            best_key, best_score = key, score
    if best_score >= SEARCH_SIMILARITY_THRESHOLD:
        payload = dict(_SEARCH_CACHE[best_key][1])
        payload["cached_result"] = True
        return payload
    return None


def _search_cache_store(norm_query, payload, ttl=None):
    snapshot = json.loads(json.dumps(payload))
    if ttl is None:
        ttl = page_cache.regex_ttl(norm_query)
    with _CACHE_LOCK:
        _SEARCH_CACHE[norm_query] = (time.monotonic(), snapshot, ttl)
        while len(_SEARCH_CACHE) > SEARCH_CACHE_MAX:
            oldest = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
            del _SEARCH_CACHE[oldest]


def _finish_inflight(norm_query, event):
    event.set()
    with _CACHE_LOCK:
        if _IN_FLIGHT.get(norm_query) is event:
            del _IN_FLIGHT[norm_query]


def _pace_outbound_request():
    """Space outbound fetches at least SEARCH_MIN_INTERVAL apart.

    Holds _PACE_LOCK while sleeping so concurrent fetchers queue up behind
    the pacing decision; cache readers are never blocked by this lock.
    """
    global _SEARCH_LAST_FETCH
    with _PACE_LOCK:
        wait = SEARCH_MIN_INTERVAL - (time.monotonic() - _SEARCH_LAST_FETCH)
        if wait > 0:
            time.sleep(wait)
        _SEARCH_LAST_FETCH = time.monotonic()


# Semantic web_search reuse. When a query has no exact keyed-cache match we ask
# the vector layer for previously-fetched pages whose meaning resembles the
# query, then reuse them as the result set. Only high-confidence hits are used;
# below this score the query degrades to a live SearXNG fetch.
_SEMANTIC_HIT_MIN_SCORE = 0.60
_SEMANTIC_HIT_K = 5
# Semantic page recall is unsafe for live or location-sensitive requests: a
# cached London traffic page is semantically close to Kolkata traffic while
# being factually useless.
_SEMANTIC_RECALL_UNSAFE_RE = re.compile(
    r"\b(?:traffic|weather|nearby|local|live|current(?:ly)?|now|latest|"
    r"breaking|today|tonight|news|updates?)\b",
    re.IGNORECASE,
)

# TTL the LLM is allowed to return, in seconds.
_LLM_TTL_MIN = 60
_LLM_TTL_MAX = 30 * 24 * 3600
# Few-shot prompt for the raw /completion endpoint. The chat models here think
# unconditionally (channel templates) and burn the token budget before the
# JSON ever appears, so the classifier uses base-style completion instead:
# examples teach the judgment, a GBNF grammar guarantees the shape, and the
# shared prefix stays prompt-cached (cache_reuse) for ~0.5s classifications.
_LLM_TTL_FEWSHOT = (
    "Classify how long a web-search answer stays valid.\n"
    'Query: latest breaking news headlines -> {"ttl_seconds": 300}\n'
    'Query: how to bake sourdough bread -> {"ttl_seconds": 2592000}\n'
    'Query: nvidia stock price right now -> {"ttl_seconds": 60}\n'
    'Query: python asyncio tutorial -> {"ttl_seconds": 2592000}\n'
    'Query: premier league results from yesterday -> {"ttl_seconds": 3600}\n'
)
_LLM_TTL_GRAMMAR = (
    'root ::= "{" [ ]*"\\\"ttl_seconds\\\""[ ]*":"[ ]*[0-9]+ "}"'
)
_LLM_TTL_TIMEOUT = 10
# Lanes tried in order for the TTL classification. The GPU lane serves every
# interactive chat (web_search's only caller), the CPU/guardrail lanes serve
# agent runs. All three sit behind lazy routers that would happily START a
# multi-GB model load for our little request, so each lane is gated on
# ``is_model_ready`` first: an unloaded lane is skipped, never poked.
_LLM_TTL_LANES = ("gpu", "guardrail", "cpu")


def _llm_ttl(query, default_ttl):
    """Ask the LLM how long this query's answer stays fresh.

    Returns seconds for the cache. On any failure (no loaded lane, malformed
    reply) falls back to ``default_ttl`` so a classifier hiccup never blocks a
    search. Uses grammar-constrained raw completion: one tiny decode pass,
    immune to thinking-mode token burn. Only lanes with an already-loaded
    model are used — this call must never itself trigger a model load.
    """
    payload = {
        "prompt": _LLM_TTL_FEWSHOT + f"Query: {query} ->",
        "n_predict": 16,
        "temperature": 0.1,
        "grammar": _LLM_TTL_GRAMMAR,
        "cache_reuse": 256,
    }
    r = None
    for lane in _LLM_TTL_LANES:
        url = M.server_url(lane)
        base = url[: -len("/v1/chat/completions")] if url.endswith(
            "/v1/chat/completions"
        ) else url
        model_id = M.server_model_id(lane)
        if not M.is_model_ready(base, model_id):
            continue
        try:
            r = requests.post(
                f"{base}/completion",
                json=dict(payload, model=model_id),
                timeout=_LLM_TTL_TIMEOUT,
            )
            if r.status_code == 200:
                break
            print(f"[ttl] {lane} lane HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:
            print(f"[ttl] {lane} lane unavailable: {e}")
    else:
        print("[ttl] no loaded LLM lane, using regex default")
        return default_ttl
    try:
        content = r.json().get("content") or ""
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end == -1:
            print(f"[ttl] classifier non-JSON: {content[:80]!r}")
            return default_ttl
        raw = int(json.loads(content[start : end + 1]).get("ttl_seconds") or 0)
        if raw <= 0:
            print(f"[ttl] classifier returned non-positive TTL: {raw!r}")
            return default_ttl
    except Exception as e:
        print(f"[ttl] classifier failed, using default: {e}")
        return default_ttl
    ttl = max(_LLM_TTL_MIN, min(_LLM_TTL_MAX, raw))
    print(f"[ttl] LLM TTL for {query!r} -> {ttl}s")
    return ttl


def _semantic_search_hit(query, min_score=_SEMANTIC_HIT_MIN_SCORE,
                         k=_SEMANTIC_HIT_K):
    """Return a web_search payload built from vector-cached pages, or None.

    ``page_semantic`` returns pages already fetched/stored (with real nomic
    embeddings). If the top hits clear ``min_score`` we can answer the query
    from them instead of the network.
    """
    if _SEMANTIC_RECALL_UNSAFE_RE.search(query or ""):
        return None
    hits = page_cache.page_semantic(query, k=k, min_score=min_score)
    if not hits:
        return None
    results = []
    for h in hits:
        results.append(
            {
                "title": h["title"] or h["url"],
                "url": h["url"],
                "snippet": h.get("snippet") or "",
                "semantic_score": h["score"],
            }
        )
    if not results:
        return None
    payload = {
        "results": results,
        "search_date": datetime.now().strftime("%Y-%m-%d %A"),
        "query": query,
        "semantic_recall": True,
    }
    return payload


_CATEGORY_RE = (
    (re.compile(r"news|updates?\b|today|latest|breaking|announ[ce]|unveil",
                re.I), "news"),
    (re.compile(r"python|javascript|typescript|github|gitlab|git\b|docker|"
                r"kubernetes|linux|unix|apache|nginx|devops|cloud|"
                r"programming|developer|open.?source|bug\b|debug|compile|"
                r"deploy|server|database|sql\b|terminal|command.?line|script|"
                r"django|flask|react|angular|node\b|rails|laravel|spring\b|"
                r"tensorflow|pytorch|\bAI\b|\bLLM\b|model\s+weights|error",
                re.I), "it"),
    (re.compile(r"arxiv|paper|research|study|journal|scientific|physics|"
                r"mathematics?|mathematical|chemistry|chemical|biology|"
                r"genome|quantum|neuron|astronom|cosmolog|doi\b|experiment|"
                r"hypothesis|theorem|calculus|peer.?review", re.I), "science"),
)


def _pick_categories(query):
    """Best-guess SearXNG categories for a query.

    Returns ``general`` plus any specific category the query clearly matches
    (news/it/science), so a search never gets locked to a single narrow engine
    pool — a bare ``categories=it`` only searches github/stackoverflow and can
    return empty results for queries that merely mention an ``AI``-style token.
    """
    cats = ["general"]
    matched = [cat for rx, cat in _CATEGORY_RE if rx.search(query or "")]
    cats += [cat for cat in ("news", "it", "science") if cat in matched]
    return ",".join(cats)


# ---------------------------------------------------------------------------
# Query re-scoping for the search backend and the diagnostics fallback fetch.
# ---------------------------------------------------------------------------
_COUNTRY_LANG = [
    (("united states", "usa", "america"), "en-US"),
    (("us",), "en-US"),
    (("india", "bengaluru", "bangalore", "mumbai", "delhi", "kolkata", "chennai", "hyderabad", "pune"), "en-IN"),
    (("united kingdom", "uk", "britain", "london"), "en-GB"),
    (("australia", "sydney", "melbourne"), "en-AU"),
    (("canada", "toronto"), "en-CA"),
]


def _region_language(query, location=""):
    """Best-guess SearXNG ``language`` code from a query's geo tokens + location."""
    q = " ".join(_query_tokens(query))
    q = q.replace("-", " ")
    if q:
        for keys, lang in _COUNTRY_LANG:
            for k in keys:
                if re.search(rf"(^| ){re.escape(k)}( |$)", q):
                    return lang
    loc = (location or "").lower()
    if "india" in loc or "bengaluru" in loc or "bangalore" in loc:
        return "en-IN"
    return ""


def _rescoped_query(query):
    """Drop role-filler words so the backend/fallback searches real content.

    E.g. "overview of quantum computing fundamentals and applications" →
    "quantum computing", which the local SearXNG can actually answer instead
    of returning dictionary pages for the word "overview".
    """
    toks = _query_tokens(query)
    return " ".join(sorted(toks, key=str.lower)) if toks else (query or "")


def _apply_location_scoping(clean_query, current_location, params):
    """Fold location/geo into SearXNG params (language hint, place qualifier)."""
    lang = _region_language(clean_query, current_location)
    if lang:
        params["language"] = lang
    # If the user gave no explicit place and we know where they are AND the
    # query is location-sensitive, qualify the search with their area so the
    # engine returns local results instead of global keyword-only junk.
    place = (current_location or "").strip()
    if place and not _has_explicit_place(clean_query):
        if re.search(r"weather|traffic|news|nearby|restaurants?|caf[eé]s?|"
                     r"events?|local|today|forecast|store|shop|market", clean_query, re.I):
            params["q"] = f"{clean_query} {place}"


def _has_explicit_place(query):
    """True when the query already names a city/country/place."""
    return any(re.search(rf"(^| ){re.escape(place)}( |$)",
                         query.lower())
               for place in _PLACE_HINTS)


_PLACE_HINTS = [
    "bengaluru", "bangalore", "mumbai", "delhi", "kolkata", "chennai",
    "hyderabad", "pune", "india", "london", "paris", "new york", "tokyo",
    "boston", "seattle", "sydney", "melbourne", "toronto", "us", "usa",
    "united states", "america", "uk", "singapore", "bangkok", "dubai",
]


def web_search(query, current_time=None, current_location=None):
    ts = datetime.now()
    clean_query = (query or "").strip()
    norm_query = " ".join(re.findall(r"[a-z0-9]+", clean_query.lower()))
    # Live and location-sensitive searches must not reuse an older result set,
    # including one that may have been contaminated by semantic recall.
    allow_cached_results = not _SEMANTIC_RECALL_UNSAFE_RE.search(clean_query)
    params = {"q": clean_query, "format": "json"}
    _apply_location_scoping(clean_query, current_location, params)
    cats = _pick_categories(clean_query)
    if cats:
        params["categories"] = cats
    print(f"[web_search] categories={cats!r} for {clean_query!r}")
    # The diagnostics fallback never repeats a query the engine already failed:
    # it re-runs the ROLE-FILLER-STRIPPED content words (plus geo scope) so a
    # "quantum computing overview" dead-end becomes a real "quantum computing"
    # search instead of dictionary pages for the word "overview". It is still
    # explicitly non-citable.
    fallback_params = dict(params)
    fallback_params["q"] = _rescoped_query(clean_query)
    _apply_location_scoping(fallback_params["q"], current_location, fallback_params)
    fallback_url = f"{M.SEARXNG_PUBLIC_URL}?{urlencode(fallback_params)}"

    def _respond(results, error=None, low_confidence=False):
        payload = {
            "results": results,
            "search_date": ts.strftime("%Y-%m-%d %A"),
            "retrieved_at": ts.astimezone().isoformat(timespec="seconds"),
            "query": query,
            "fallback_fetch_url": fallback_url,
            "fallback_fetch_note": (
                "Diagnostics-only re-run of the query with role-filler words "
                "stripped (e.g. 'overview'/'fundamentals' removed) plus the "
                "geo/language scope, returned as raw JSON. NEVER cite this URL "
                "as a source; cite result URLs only. Prefer issuing a fresh, "
                "well-scoped web_search instead of fetching this, and only use "
                "it to confirm whether the engine has better results."
            ),
        }
        if low_confidence:
            # Do not expose the raw fallback endpoint for failed searches. The
            # model may fetch it despite the note, bypassing result screening
            # and receiving unrelated engine output (as happened for GurPithe).
            payload.pop("fallback_fetch_url", None)
            payload.pop("fallback_fetch_note", None)
            payload["low_confidence"] = True
            payload["low_confidence_note"] = (
                "This search FAILED to find any result credibly matching the "
                "query (fewer than two results survived relevance screening; "
                "the set may even be empty). Treat every listed URL as "
                "UNRELIABLE and off-topic: do NOT fetch any of them, do NOT "
                "summarize them, and do NOT build an answer around them (e.g. "
                "do not pivot to Real Madrid for a traffic question just "
                "because it is the only returned link). State the sub-answer "
                "as UNSUPPORTED and issue a fresh web_search with a clearer, "
                "better-scoped query instead of improvising or latching onto "
                "an irrelevant source."
            )
        if error:
            payload["error"] = error
        return payload

    owns_slot = False
    with _CACHE_LOCK:
        hit = (
            _search_cache_get(norm_query, clean_query, time.monotonic())
            if allow_cached_results
            else None
        )
        # A failed/low-confidence search must not poison the cache for its TTL.
        # Treat it as a miss so the next request can try SearXNG again.
        if hit is not None and (
            not hit.get("results") or hit.get("low_confidence")
        ):
            hit = None
        if hit is None and allow_cached_results:
            inflight = _IN_FLIGHT.get(norm_query)
            if inflight is None:
                inflight = _IN_FLIGHT[norm_query] = threading.Event()
                owns_slot = True
    if hit is not None:
        print("Web-search cache hit")
        return json.dumps(_screen_cached_payload(hit, clean_query))
    if hit is None and allow_cached_results:
        # Persistent (cross-restart) cache: a query answered before this
        # process started should never cost another SearXNG request.
        hit = page_cache.search_get(norm_query)
        if hit is not None and hit.get("results") and not hit.get("low_confidence"):
            _search_cache_store(norm_query, hit, ttl=hit.get("_ttl"))
            print("Web-search cache hit (persistent)")
            return json.dumps(_screen_cached_payload(hit, clean_query))
    if hit is None and allow_cached_results:
        # Semantic recall: this query shares no exact key with a cached search,
        # but we may have already fetched pages (via fetch_page or enrichment)
        # whose meaning matches the query. Reuse those instead of another
        # SearXNG call — the whole point of the vector layer.
        hit = _semantic_search_hit(clean_query)
        if hit is not None:
            if owns_slot:
                _finish_inflight(norm_query, inflight)
            _search_cache_store(norm_query, hit)
            print("Web-search cache hit (semantic)")
            return json.dumps(_screen_cached_payload(hit, clean_query))
    if not owns_slot and allow_cached_results:
        # An identical fetch is already running: wait for its result
        # instead of duplicating the request.
        if inflight.wait(SEARCH_INFLIGHT_WAIT):
            with _CACHE_LOCK:
                hit = _search_cache_get(
                    norm_query, clean_query, time.monotonic()
                )
            if hit is not None:
                print("Web-search cache hit (in-flight wait)")
                return json.dumps(_screen_cached_payload(hit, clean_query))
        # Timed out or the fetch failed without caching: fall through and
        # fetch this query ourselves (paced like any other).

    _pace_outbound_request()
    print("Performing web search", M.SEARXNG_URL)
    try:
        r = requests.get(M.SEARXNG_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"Web-search failed: {e}")
        _finish_inflight(norm_query, inflight)
        return json.dumps(_respond([], error=str(e)))

    results = data.get("results", [])[:WEB_SEARCH_RESULT_LIMIT]
    formatted = []
    for x in results:
        formatted.append(
            {
                "title": x.get("title", ""),
                "url": x.get("url", ""),
                "snippet": x.get("content", "") or x.get("snippet", ""),
            }
        )
    # Scrub ads/promo and orphan landing pages BEFORE this result set is handed
    # to the LLM (it is also what gets recorded as _search_details and later
    # re-read by the story pipeline). The model should never see a shopping,
    # download, quote, or company-profile URL as a possible source.
    formatted = scrub_search_results(formatted, query=query)
    # Relevance gate (A): drop keyword-only junk (Real-Madrid-for-traffic,
    # dictionary-for-"overview") before the LLM ever sees it, and flag the
    # set as low-confidence when few credible results survive so the model
    # reports UNSUPPORTED instead of improvising.
    if not formatted:
        low_confidence = False
    else:
        formatted, low_confidence = relevance.filter_relevance(formatted, query)
    payload = _respond(formatted, low_confidence=low_confidence)
    # Ask the LLM how long this answer stays fresh so the next identical query
    # re-fetches at the right time ("breaking news" -> seconds, "how to" -> days),
    # rather than a fixed regex heuristic. Falls back to the regex default if
    # the classifier call fails or the LLM is unreachable. Computed once and
    # used for both the in-memory and persistent stores.
    ttl = _llm_ttl(query, page_cache.regex_ttl(query))
    # Cache the raw results before enrichment so waiters can pick them up,
    # then re-store the enriched version once page fetching completes.
    _search_cache_store(norm_query, payload, ttl=ttl)
    _finish_inflight(norm_query, inflight)
    enriched = _enrich_top_results(formatted)
    payload["results"] = enriched
    _search_cache_store(norm_query, payload, ttl=ttl)
    page_cache.search_put(norm_query, query, payload, ttl=ttl)
    return json.dumps(payload)


def _enrich_top_results(results):
    """Attach the full page text to the top results.

    The top ``WEB_SEARCH_ENRICH_TOP`` results are fetched concurrently (their
    body is stored as ``full_content``) so the LLM does not have to make a
    separate ``fetch_page`` call for every promising link. Fetch failures are
    recorded as ``fetch_error`` and never break the search response.
    """
    targets = results[:WEB_SEARCH_ENRICH_TOP]
    if not targets:
        return results

    def _one(entry):
        url = entry.get("url", "")
        if not url.lower().startswith(("http://", "https://")):
            return
        try:
            page = json.loads(fetch_page(url, max_chars=WEB_SEARCH_ENRICH_CHARS))
            if page.get("content"):
                entry["full_content"] = page["content"]
                entry["page_title"] = page.get("title", "")
            elif page.get("error"):
                entry["fetch_error"] = page["error"]
        except Exception as e:
            entry["fetch_error"] = str(e)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(targets))
    futs = [executor.submit(_one, entry) for entry in targets]
    concurrent.futures.wait(futs, timeout=WEB_SEARCH_ENRICH_TIMEOUT)
    executor.shutdown(wait=False)
    return results


_semantic_relevance = relevance._semantic_relevance
_screen_cached_payload = relevance._screen_cached_payload
