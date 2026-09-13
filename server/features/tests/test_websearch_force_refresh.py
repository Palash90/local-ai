"""Unit tests for web_search(force_refresh=...) cache bypass.

Covers all three read layers (in-memory, persistent sqlite, semantic recall):
a plain call must serve the persistent-cache hit without touching the network,
while force_refresh=True must skip every cache read, hit live SearXNG, and
still write the fresh payload back. No network or real DB is touched.
"""

import json

from server.features.websearch import search

QUERY = "quantum computing tutorial"  # no freshness keywords -> cache eligible

CACHED_PAYLOAD = {
    "results": [
        {
            "title": "Cached result",
            "url": "http://cached.example/",
            "snippet": "stale snippet",
        }
    ],
    "_ttl": 300,
}

LIVE_RESULTS = {
    "results": [
        {
            "title": "Live result",
            "url": "http://live.example/",
            "content": "fresh snippet",
        }
    ]
}


class _StubEntrypoint:
    SEARXNG_URL = "http://127.0.0.1:9/searxng"
    SEARXNG_PUBLIC_URL = "http://127.0.0.1:9/searxng"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _install_common_stubs(monkeypatch, calls):
    """Stub entrypoint, pacing, TTL, enrichment, and relevance gates."""
    monkeypatch.setattr(search, "M", _StubEntrypoint())
    monkeypatch.setattr(search, "_pace_outbound_request", lambda: None)
    monkeypatch.setattr(search, "_llm_ttl", lambda query, default: 60)
    monkeypatch.setattr(
        search, "_enrich_top_results", lambda results: results
    )
    monkeypatch.setattr(search, "scrub_search_results",
                        lambda formatted, query=None: formatted)
    monkeypatch.setattr(
        search.relevance,
        "filter_relevance",
        lambda formatted, query: (formatted, False),
    )

    def fake_get(url, params=None, timeout=None):
        calls["live"] += 1
        return _FakeResponse(LIVE_RESULTS)

    monkeypatch.setattr(search.requests, "get", fake_get)


def test_cached_hit_serves_without_network(monkeypatch):
    calls = {"live": 0, "search_get": 0, "search_put": 0}
    _install_common_stubs(monkeypatch, calls)
    monkeypatch.setattr(
        search.page_cache, "search_get",
        lambda norm_query: (calls.__setitem__("search_get",
                                              calls["search_get"] + 1),
                            dict(CACHED_PAYLOAD))[1],
    )
    monkeypatch.setattr(
        search.page_cache, "search_put",
        lambda *a, **k: calls.__setitem__("search_put",
                                          calls["search_put"] + 1),
    )

    data = json.loads(search.web_search(QUERY))
    assert data["results"][0]["url"] == "http://cached.example/"
    assert calls["search_get"] == 1
    assert calls["live"] == 0


def test_force_refresh_bypasses_all_caches(monkeypatch):
    calls = {"live": 0, "search_get": 0, "search_put": 0}
    _install_common_stubs(monkeypatch, calls)

    def boom(norm_query):
        calls["search_get"] += 1
        raise AssertionError("persistent cache must not be read")

    monkeypatch.setattr(search.page_cache, "search_get", boom)
    monkeypatch.setattr(
        search.page_cache,
        "page_semantic",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("semantic recall must not run")),
    )
    monkeypatch.setattr(
        search.page_cache, "search_put",
        lambda *a, **k: calls.__setitem__("search_put",
                                          calls["search_put"] + 1),
    )

    data = json.loads(search.web_search(QUERY, force_refresh=True))
    assert calls["live"] >= 1
    assert calls["search_get"] == 0
    urls = [r["url"] for r in data["results"]]
    assert "http://live.example/" in urls
    assert "http://cached.example/" not in urls
    # Fresh payload is still written back for the next identical query.
    assert calls["search_put"] == 1
