"""Lexical, location, semantic, and cached-result relevance filters."""

import re

from server.features.websearch import vector_store as page_cache

WEB_SEARCH_RESULT_LIMIT = 10

_PLACE_HINTS = [
    "bengaluru", "bangalore", "mumbai", "delhi", "kolkata", "chennai",
    "hyderabad", "pune", "india", "london", "paris", "new york", "tokyo",
    "boston", "seattle", "sydney", "melbourne", "toronto", "us", "usa",
    "united states", "america", "uk", "singapore", "bangkok", "dubai",
]

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
# Result relevance gating.
#
# SearXNG (especially the local instance) does naive token matching and will
# happily return keyword-only junk for a multi-word query — the "real-time
# traffic" → Real Madrid and "quantum computing overview" → dictionary-of-the-
# -word-`overview` failures seen in production. Two layers run on every fresh
# search before results reach the LLM:
#   1. lexical: drop results sharing no meaningful content token with the query;
#   2. semantic: score each surviving result against the query with the local
#      nomic embedding server and drop low-similarity hits.
# Both degrade gracefully: if the tokenizer/embed server is unavailable we keep
# the results rather than empty the list, but we still surface ``low_confidence``
# so the LLM is told to treat the set as unreliable instead of improvising.
# ---------------------------------------------------------------------------

# Semantic similarity (cosine 0..1) a result must reach to be considered
# credible. Genuinely relevant hits sit 0.5-0.85; the keyword-only junk cases
# (Real Madrid vs traffic scored ~0.37, dictionary page vs quantum ~0.2) sit
# below it, so 0.40 is a deliberate gap that drops them while keeping real hits.
REL_MIN_SEMANTIC = 0.40
# If fewer than this many results survive BOTH gates we surface the (possibly
# empty) survivor set with ``low_confidence=True`` and a re-search directive so
# the model never fixates on an irrelevant single result (e.g. Real Madrid in
# a Bangalore traffic query).
REL_MIN_CREDIBLE_RESULTS = 2

# Content words that deserve no query token (role-filler / vague-intent words
# that match unrelated dictionary pages, e.g. "overview", "research", "report").
_QUERY_STOPWORDS = set(
    """
    a an the of on in to for and or but about with at by from via
    what who when where why how is are was were be been being do does did
    can could will would should shall may might must have has had
    this that these those it its there here
    overview summary general basics fundamental fundamentals introduction
    detail details comprehensive research report find information info explain
    explainer guide explain write about
    sign signs way ways check checks issue issues symptom symptoms cause causes
    test tests testing identify determine tell dying
    """.split()
)


def _query_tokens(query):
    """Lowercased, stopword-stripped meaningful query terms.

    Hyphenated compounds are kept intact (``real-time`` stays ONE token so the
    query never over-matches the word ``real``, e.g. against "Real Madrid" in a
    traffic search); underscores are collapsed to plain tokens.
    """
    q = (query or "").lower()
    q = q.replace("_", " ")
    toks = set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", q))
    return toks - _QUERY_STOPWORDS


def _result_tokens(entry):
    """Lowercased meaningful tokens from a result's title + snippet."""
    text = " ".join(
        [
            entry.get("title", "") or "",
            entry.get("page_title", "") or "",
            entry.get("snippet", "") or "",
        ]
    ).lower()
    text = text.replace("_", " ")
    return set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text)) or set()


# High-frequency, highly ambiguous English words that routinely cause false
# keyword "matches" (e.g. the standalone "real" from a space-separated
# "real time" query colliding with "Real Madrid"). A result whose ONLY overlap
# with the query is on these weak tokens counts as having NO overlap at all.
_WEAK_TOKENS = {
    "real", "top", "best", "new", "latest", "more", "today", "result",
    "results", "big", "info", "information", "page", "main", "item", "items",
    "list", "article",
}


def _strong_overlap(qtoks, rtoks):
    """True if the token overlap includes at least one non-weak query token."""
    overlap = qtoks & rtoks
    if not overlap:
        return False
    strong = qtoks - _WEAK_TOKENS
    return bool(overlap & strong)


_PLACE_ALIASES = {
    "bengaluru": ("bengaluru", "bangalore"),
    "bangalore": ("bengaluru", "bangalore"),
    "kolkata": ("kolkata", "calcutta"),
    "calcutta": ("kolkata", "calcutta"),
    "mumbai": ("mumbai", "bombay"),
    "bombay": ("mumbai", "bombay"),
    "united states": ("united states", "usa", "us"),
    "usa": ("united states", "usa", "us"),
    "us": ("united states", "usa", "us"),
}


def _requested_places(query):
    """Return explicitly named places in a query, including known aliases."""
    q = (query or "").lower()
    places = []
    for place in _PLACE_HINTS:
        if re.search(rf"(^| ){re.escape(place)}( |$)", q):
            places.extend(_PLACE_ALIASES.get(place, (place,)))
    return set(places)


def _location_matches(query, entry):
    """Reject a result that cannot be about an explicitly requested place."""
    requested = _requested_places(query)
    if not requested:
        return True
    text = " ".join(
        str(entry.get(key, "") or "")
        for key in ("title", "page_title", "snippet")
    ).lower()
    url = str(entry.get("url", "") or "").lower()
    return any(
        re.search(rf"(^|[^a-z]){re.escape(place)}([^a-z]|$)", text)
        or place.replace(" ", "") in url
        for place in requested
    )


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _semantic_relevance(results, query):
    """Score results against the query with the embed server.

    Returns (results, low_confidence). Results below ``REL_MIN_SEMANTIC`` are
    HARD-DROPPED (never kept just so the list is non-empty — a surviving junk
    result like a Real Madrid page makes the model latch onto it). On embed
    failure returns (results, False) untouched so the caller keeps the lexical
    survivors rather than emptying the list over a transient outage.
    """
    if not results:
        return results, False
    texts = [query] + [
        f"{r.get('title') or ''} :: {r.get('snippet') or ''}"[:280] for r in results
    ]
    vecs = page_cache.embed_texts(texts)
    if not vecs or len(vecs) != len(texts):
        return results, False
    qv = vecs[0]
    scored = []
    for r, rv in zip(results, vecs[1:]):
        r["relevance"] = round(_cosine(qv, rv), 3)
        if r["relevance"] >= REL_MIN_SEMANTIC:
            scored.append(r)
    scored.sort(key=lambda r: r.get("relevance", 0.0), reverse=True)
    low_conf = len(scored) < REL_MIN_CREDIBLE_RESULTS
    return scored[:WEB_SEARCH_RESULT_LIMIT], low_conf


def _filter_relevant_results(results, query):
    """Apply lexical + semantic relevance gating; return (results, low_confidence)."""
    if not results:
        return results, False
    qtoks = _query_tokens(query)
    # Location gate is authoritative for explicit place queries. Generic
    # topical similarity must not turn a Kolkata query into a London/TfL hit.
    location_kept = [r for r in results if _location_matches(query, r)]
    if _requested_places(query):
        if not location_kept:
            return [], True
        results = location_kept
    # Lexical gate is AUTHORITATIVE: a result sharing no meaningful token with
    # the query (Real-Madrid-for-"real-time traffic", dictionary-for-"overview")
    # is junk and is hard-dropped — never handed back just to avoid an empty
    # list, because the model will then fixate on the irrelevant page.
    if qtoks:
        kept = [r for r in results if _strong_overlap(qtoks, _result_tokens(r))]
        if kept:
            results = kept
        else:
            return [], True
    # Semantic gate: drop low-similarity survivors; degrade gracefully to the
    # lexical survivors if the embed server is briefly unavailable.
    try:
        results, low_conf = _semantic_relevance(results, query)
    except Exception as e:
        print(f"[web_search] relevance screening failed: {e}")
        results, low_conf = results, False
    return results[:WEB_SEARCH_RESULT_LIMIT], low_conf


def _screen_cached_payload(payload, query):
    """Revalidate cached results because cache keys may predate relevance gates."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return payload
    screened = dict(payload)
    results, low_confidence = _filter_relevant_results(payload["results"], query)
    screened["results"] = results
    if low_confidence:
        screened.pop("fallback_fetch_url", None)
        screened.pop("fallback_fetch_note", None)
        screened["low_confidence"] = True
        screened["low_confidence_note"] = (
            "Cached search results did not contain enough credible sources for "
            "this query. Do not fetch or cite the listed URLs; issue a fresh, "
            "better-scoped web_search instead."
        )
    return screened


# ---------------------------------------------------------------------------
