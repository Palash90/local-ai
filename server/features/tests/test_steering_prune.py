"""Regression tests for consumed-steering pruning in context building.

Steering notes ([SYSTEM NOTE ...], _steering=True) guide the immediate
retry round, but they used to persist in LLM context forever — later
rounds misread them as user-typed prompt injections and spent their
reasoning analyzing the note instead of the task.
"""

from server.features.context import _prune_consumed_steering as prune


def _u(text):
    return {"role": "user", "content": text}


def _s(text):
    return {"role": "user", "content": text, "_steering": True}


def _a(text):
    return {"role": "assistant", "content": text}


def test_consumed_steering_dropped_trailing_kept():
    msgs = [_u("draw a cat"), _s("NOTE-1"), _a("here is a cat"), _s("NOTE-2")]
    out = prune(msgs)
    # NOTE-1 was acted on (assistant replied after it) -> dropped.
    # NOTE-2 is trailing (still guiding) -> kept.
    assert [m["content"] for m in out] == ["draw a cat", "here is a cat", "NOTE-2"]


def test_no_steering_returns_same():
    msgs = [_u("hi"), _a("hello")]
    assert prune(msgs) == msgs


def test_stored_history_not_mutated():
    msgs = [_u("draw a cat"), _s("NOTE-1"), _a("done")]
    before = list(msgs)
    prune(msgs)
    assert msgs == before
