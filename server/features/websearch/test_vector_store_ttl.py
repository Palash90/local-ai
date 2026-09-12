from server.features.websearch.vector_store import (
    TIMELESS_TTL, SEARCH_FRESH_TTL, SEARCH_STALE_TTL, regex_ttl)


def test_timeless_music_theory_queries():
    for q in ("raga bhupali aarohi avarohi pakad",
              "keherwa tala theka bols",
              "tabla groove pattern",
              "sitar instrument tuning range",
              "maqam hijaz scale degrees"):
        assert regex_ttl(q) == TIMELESS_TTL, q


def test_fresh_still_wins_over_timeless():
    assert regex_ttl("ragas concert tonight tickets") == SEARCH_FRESH_TTL
    assert regex_ttl("tabla news this week") == SEARCH_FRESH_TTL


def test_stale_default_unchanged():
    assert regex_ttl("what is the capital of France") == SEARCH_STALE_TTL
    assert regex_ttl("how to cook pasta") == SEARCH_STALE_TTL
    assert regex_ttl("") == SEARCH_STALE_TTL
