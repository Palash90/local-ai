"""OpenAI lane guards: context cap boundary + fail-open estimate."""

import types

import server.openai_api as oai


def _msgs(n_chars):
    return [{"role": "user", "content": "x" * n_chars}]


def _fake_m(monkeypatch, per_char=0.25, limit=24000, fail=False):
    import server.config as _cfg
    monkeypatch.setattr(_cfg, "OPENAI_MAX_CONTEXT_TOKENS", limit)
    # _openai_context_check reads the module-level imported name.
    monkeypatch.setattr(oai, "OPENAI_MAX_CONTEXT_TOKENS", limit)

    def _est(msgs):
        if fail:
            raise RuntimeError("estimator down")
        total = 0
        for m in msgs or []:
            c = m.get("content", "")
            total += int(len(c) * per_char) if isinstance(c, str) else 0
        return total

    fake_m = types.SimpleNamespace(estimate_tokens=_est)
    monkeypatch.setattr(oai, "M", fake_m)


def test_cap_admits_under_limit(monkeypatch):
    _fake_m(monkeypatch, per_char=0.25, limit=24000)
    ok, est, lim = oai._openai_context_check(_msgs(1000))
    assert ok is True and lim == 24000 and est == 250


def test_cap_rejects_over_limit(monkeypatch):
    _fake_m(monkeypatch, per_char=0.25, limit=24000)
    ok, est, lim = oai._openai_context_check(_msgs(100000))
    assert ok is False and est == 25000 and lim == 24000


def test_cap_fail_open_on_estimator_error(monkeypatch):
    _fake_m(monkeypatch, fail=True)
    ok, est, lim = oai._openai_context_check(_msgs(100000))
    assert ok is True and est == 0


def _hist(*entries):
    return list(entries)


def _tc(cid, url):
    import json as _j
    return {"id": cid, "type": "function",
            "function": {"name": "webfetch",
                         "arguments": _j.dumps({"url": url})}}


def test_loop_breaker_flags_404_once():
    hist = _hist(
        {"role": "assistant", "tool_calls": [_tc("c1", "https://x.example/dead")]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "StatusCode: non 2xx status code (404 GET https://x.example/dead)"},
    )
    assert oai._failed_fetch_urls(hist) == ["https://x.example/dead"]


def test_loop_breaker_flags_403_and_420():
    hist = _hist(
        {"role": "assistant", "tool_calls": [_tc("c1", "https://a.example/p")]},
        {"role": "tool", "tool_call_id": "c1", "content": "403 Forbidden"},
        {"role": "assistant", "tool_calls": [_tc("c2", "https://b.example/q")]},
        {"role": "tool", "tool_call_id": "c2",
         "content": "420 Enhance Your Calm, slow down"},
    )
    assert oai._failed_fetch_urls(hist) == [
        "https://a.example/p", "https://b.example/q"]


def test_loop_breaker_ignores_successful_fetches():
    hist = _hist(
        {"role": "assistant", "tool_calls": [_tc("c1", "https://x.example/ok")]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "long page body " * 200},
    )
    assert oai._failed_fetch_urls(hist) == []


def test_loop_breaker_dedupes_repeated_failures():
    hist = _hist(
        {"role": "assistant", "tool_calls": [_tc("c1", "https://x.example/dead")]},
        {"role": "tool", "tool_call_id": "c1", "content": "404 Not Found"},
        {"role": "assistant", "tool_calls": [_tc("c2", "https://x.example/dead")]},
        {"role": "tool", "tool_call_id": "c2", "content": "404 Not Found"},
    )
    assert oai._failed_fetch_urls(hist) == ["https://x.example/dead"]


def _dead_hist():
    return _hist(
        {"role": "assistant", "tool_calls": [_tc("c1", "https://x.example/dead")]},
        {"role": "tool", "tool_call_id": "c1", "content": "404 Not Found"},
    )


def test_tool_call_url_json_args():
    assert oai._tool_call_url(_tc("c1", "https://x.example/a")) == "https://x.example/a"


def test_tool_call_url_raw_string_args():
    tc = {"id": "c9", "type": "function",
          "function": {"name": "webfetch",
                       "arguments": "fetch https://y.example/b now"}}
    assert oai._tool_call_url(tc) == "https://y.example/b"


def test_tool_call_url_missing():
    assert oai._tool_call_url({}) == ""
    assert oai._tool_call_url({"id": "c1", "function": {"name": "x", "arguments": "{}"}}) == ""
    assert oai._tool_call_url(None) == ""


def test_strip_dead_keeps_live_calls():
    hist = _dead_hist()
    calls = [_tc("c2", "https://x.example/dead"),
             _tc("c3", "https://ok.example/fine")]
    live, dropped = oai._strip_dead_tool_calls(calls, hist)
    assert [c["id"] for c in live] == ["c3"]
    assert dropped == ["https://x.example/dead"]


def test_strip_dead_all_dead_empties():
    hist = _dead_hist()
    live, dropped = oai._strip_dead_tool_calls(
        [_tc("c2", "https://x.example/dead")], hist)
    assert live == [] and dropped == ["https://x.example/dead"]


def test_strip_dead_no_history_passthrough():
    calls = [_tc("c1", "https://x.example/a")]
    live, dropped = oai._strip_dead_tool_calls(calls, [])
    assert [c["id"] for c in live] == ["c1"] and dropped == []
