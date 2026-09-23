"""Context compaction: threshold trigger, summary path, fallbacks, no-mutation.

prepare_context_for_llm() runs on EVERY chat round once history is long, yet
had zero tests. These cover the threshold branch, the LLM-summary path (mocked),
the summarizer-failure fallback, trim order, giant-message truncation, and the
core invariant: the stored session is never mutated.
"""

import copy
import threading
import types

import pytest

from server.features import context as C
from server.features import state


def _M(**over):
    base = dict(
        TOOLS_TOKEN_COST=0,
        PER_MESSAGE_OVERHEAD=0,
        IMAGE_TOKEN_COST=0,
        AUDIO_TOKEN_COST=0,
        AUTO_COMPACT_THRESHOLD=100,
        UPLOADS_DIR="/nonexistent",
        IMG_PATH="/nonexistent",
        MODEL_ID="gemma4-e4b-q4",
        MODEL_ID_CPU="gemma4-e4b-q4",
        MODEL_ID_GUARDRAIL="gemma-4-E2B-it-Q4_K_M",
        _effective_contexts={},
        _effective_contexts_lock=threading.Lock(),
        sessions_meta={},
        prompt_token_budget=lambda mode: 10_000,
        _summarize_with_llm=lambda text, mode="gpu": "SUMMARY",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


@pytest.fixture()
def entrypoint(monkeypatch):
    m = _M()
    prev = state._Registry.entrypoint
    state.register_entrypoint(m)
    # Isolate compaction from pensieve distillation.
    monkeypatch.setattr(
        C, "distill_and_store_messages", lambda sid, messages, mode: messages)
    try:
        yield m
    finally:
        state.register_entrypoint(prev)


def _msgs(n, size=10):
    out = [{"role": "system", "content": "sys"}]
    for i in range(n):
        out.append({"role": "user" if i % 2 == 0 else "assistant",
                    "content": f"msg-{i} " + "x" * size})
    return out


def test_below_threshold_no_compaction(entrypoint):
    msgs = _msgs(4)
    before = copy.deepcopy(msgs)
    out = C.prepare_context_for_llm("s1", msgs, mode="gpu")
    assert out == before  # unchanged content
    assert msgs == before  # stored session never mutated
    assert entrypoint.sessions_meta.get("s1", {}).get("compactions", 0) == 0
    assert entrypoint._effective_contexts["s1"] == before


def test_above_threshold_summarizes_and_counts(entrypoint):
    msgs = _msgs(30, size=60)  # well over threshold=100
    before = copy.deepcopy(msgs)
    out = C.prepare_context_for_llm("s2", msgs, mode="gpu")
    assert msgs == before  # stored session never mutated
    # [system, compressed summary, last 6]
    assert out[0]["role"] == "system" and out[0]["content"] == "sys"
    assert out[1]["role"] == "system"
    assert out[1]["content"].startswith("[Compressed context]: SUMMARY")
    assert len(out) == 2 + 6
    assert out[-6:] == before[-6:]
    assert entrypoint.sessions_meta["s2"]["compactions"] == 1
    assert C.estimate_tokens(out) < C.estimate_tokens(msgs)


def test_summarizer_failure_falls_back_without_count(entrypoint):
    entrypoint._summarize_with_llm = lambda text, mode="gpu": None
    msgs = _msgs(30, size=60)
    before = copy.deepcopy(msgs)
    out = C.prepare_context_for_llm("s3", msgs, mode="gpu")
    assert out == before  # full list returned, nothing fabricated
    assert entrypoint.sessions_meta.get("s3", {}).get("compactions", 0) == 0


def test_trim_pops_oldest_keeps_system_first(entrypoint):
    entrypoint.AUTO_COMPACT_THRESHOLD = 10 ** 9  # force trim-only path
    entrypoint.prompt_token_budget = lambda mode: 25
    msgs = ([{"role": "system", "content": "sys"}] +
            [{"role": "user", "content": f"m{i} pad pad pad"} for i in range(10)])
    out = C.prepare_context_for_llm("s4", msgs, mode="gpu")
    assert out[0]["content"] == "sys"
    assert C.estimate_tokens(out) <= 25 + C.estimate_tokens([out[0]])


def test_giant_single_message_gets_truncated_with_marker(entrypoint):
    entrypoint.AUTO_COMPACT_THRESHOLD = 10 ** 9
    entrypoint.prompt_token_budget = lambda mode: 50
    big = "Z" * 5000
    out = C.prepare_context_for_llm(
        "s5", [{"role": "user", "content": big}], mode="gpu")
    assert len(out) == 1
    assert "older tool output truncated" in out[0]["content"]
    assert out[0]["content"].startswith("Z")


def test_compact_copy_short_list_unchanged(entrypoint):
    msgs = _msgs(5)
    out = C.compact_messages_copy(msgs, keep_messages=6, mode="gpu")
    assert out == msgs
