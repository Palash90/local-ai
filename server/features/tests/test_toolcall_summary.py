"""Client tool-call log summaries + loop-watch repeat detection."""

import threading
from types import SimpleNamespace

import pytest

from server.features import state as _state
from server.features.orchestration import (
    TOOL_LOOP_WATCH_THRESHOLD,
    _toolcall_signature,
    _toolcall_summary,
    _track_tool_repeat,
)


@pytest.fixture
def fake_entrypoint():
    ep = SimpleNamespace(tasks={}, _data_lock=threading.Lock())
    _state.register_entrypoint(ep)
    try:
        yield ep
    finally:
        _state.register_entrypoint(None)


def _tc(name, args):
    return {"id": "x", "type": "function",
            "function": {"name": name, "arguments": args}}


def test_summary_names_and_args():
    out = _toolcall_summary([_tc("web_search", '{"q": "gemma 26b"}')])
    assert out == 'web_search({"q": "gemma 26b"})'


def test_summary_truncates_long_args():
    out = _toolcall_summary([_tc("web_search", '{"q": "' + "x" * 200 + '"}')])
    assert len(out) <= len("web_search()") + 121
    assert out.endswith("…)")


def test_summary_defensive_shapes():
    assert _toolcall_summary([]) == "?"
    assert _toolcall_summary([{"function": {}}]) == "?()"
    assert _toolcall_summary([{}]) == "?()"
    assert _toolcall_summary(None) == "?"
    # Non-string arguments (dict) are JSON-encoded, not crashed on.
    out = _toolcall_summary([_tc("f", {"q": "hi"})])
    assert out.startswith("f(") and '"q"' in out


def test_signature_stable_and_sensitive():
    a = [_tc("web_search", '{"q": "x"}')]
    b = [_tc("web_search", '{"q": "x"}')]
    c = [_tc("web_search", '{"q": "y"}')]
    assert _toolcall_signature(a) == _toolcall_signature(b)
    assert _toolcall_signature(a) != _toolcall_signature(c)


def test_repeat_counter_tracks_and_resets(fake_entrypoint):
    ep = fake_entrypoint
    with ep._data_lock:
        ep.tasks["t1"] = {"status": "working"}
    a = [_tc("web_search", '{"q": "x"}')]
    b = [_tc("web_search", '{"q": "y"}')]
    _, r1 = _track_tool_repeat("t1", a)
    _, r2 = _track_tool_repeat("t1", a)
    _, r3 = _track_tool_repeat("t1", a)
    assert (r1, r2, r3) == (1, 2, 3)
    assert r3 >= TOOL_LOOP_WATCH_THRESHOLD
    _, r4 = _track_tool_repeat("t1", b)
    assert r4 == 1


def test_repeat_missing_task_is_safe(fake_entrypoint):
    summary, repeats = _track_tool_repeat("nope", [_tc("f", "{}")])
    assert repeats == 1
    assert summary.startswith("f(")
