"""Unit tests for the web-search operator sanitizer (A) and retry ordering (B)."""

from server.features.websearch import search
from server.features.websearch.search import _rescoped_query, _sanitize_query


def test_dash_flag_stripped():
    assert (
        _sanitize_query("python3 -X faulthandler explanation")
        == "python3 faulthandler explanation"
    )


def test_em_dash_flag_stripped():
    assert _sanitize_query("python3 \u2014X faulthandler") == "python3 faulthandler"


def test_quoted_operator_preserved():
    assert _sanitize_query('"-X" flag in python') == '"-X" flag in python'


def test_numbers_and_ranges_preserved():
    assert _sanitize_query("temperature -5 degrees") == "temperature -5 degrees"
    assert _sanitize_query("2020-2024 sales report") == "2020-2024 sales report"


def test_normal_query_untouched():
    assert _sanitize_query("normal query here") == "normal query here"


def test_single_operator_not_emptied():
    assert _sanitize_query("-X") == "-X"


def test_retry_ordering_sanitize_before_rescope():
    # A naive rescope of the raw query keeps the exclusion token 'x';
    # sanitize-first must drop it so the retry cannot reproduce the failure.
    raw_scoped = _rescoped_query("python3 -X faulthandler explanation")
    assert "x" in raw_scoped.split()
    retry_q = _rescoped_query(
        _sanitize_query("python3 -X faulthandler explanation")
    )
    assert "x" not in retry_q.split()
    assert "faulthandler" in retry_q


def test_retry_block_present_in_web_search():
    import inspect

    src = inspect.getsource(search.web_search)
    assert "_rescoped_query(_sanitize_query(clean_query))" in src
    assert 'payload["retried"]' in src
