"""Unit tests for the Pensieve package (no repo state, no network)."""

import pytest

from server.features.pensieve import distill, memory
from server.features.pensieve.retrieval import memory_read


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "PENSIEVE_DB", str(tmp_path / "pensieve_test.db"))
    yield


def _big_estimate(_messages):
    return 10 ** 9


def _small_estimate(_messages):
    return 100


def _tiny_budget():
    return 1000


def test_block_splitting_atomic_tool_chain():
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "Fetch two things."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {}}, {"function": {}}],
        },
        {"role": "tool", "content": "result a"},
        {"role": "tool", "content": "result b"},
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "Next."},
    ]
    blocks = distill.build_blocks(messages)
    assert [b["kind"] for b in blocks] == [
        "system",
        "normal",
        "normal",
        "normal",
        "normal",
    ]
    # user + tool-calling assistant + BOTH tool results stay one block
    assert blocks[2]["msgs"] == [2, 3, 4]
    assert blocks[3]["msgs"] == [5]
    assert blocks[4]["msgs"] == [6]


def test_system_message_never_archived():
    messages = [
        {"role": "system", "content": "You are a helpful pipeline."},
        {"role": "user", "content": "Old topic."},
        {"role": "assistant", "content": "Old reply."},
    ]
    out = distill.distill_and_store_messages(
        "s1",
        messages,
        mode="gpu",
        estimate_fn=_big_estimate,
        budget_fn=_tiny_budget,
        keep_recent=0,
    )
    assert out[0] == {"role": "system", "content": "You are a helpful pipeline."}
    assert out[1] == {"role": "system", "content": "[#1: Old topic. (1 messages)]"}
    assert out[2] == {"role": "system", "content": "[#2: Old reply. (1 messages)]"}


def test_older_archived_recent_kept_inline():
    messages = [
        {"role": "user", "content": "Question one about gallium."},
        {"role": "assistant", "content": "Answer one."},
        {"role": "user", "content": "Question two about bismuth."},
        {"role": "assistant", "content": "Answer two."},
    ]
    out = distill.distill_and_store_messages(
        "s2",
        messages,
        mode="gpu",
        estimate_fn=_big_estimate,
        budget_fn=_tiny_budget,
        keep_recent=2,
    )
    assert len(out) == 4
    assert out[0] == {"role": "system", "content": "[#1: Question one about gallium. (1 messages)]"}
    assert out[1] == {"role": "system", "content": "[#2: Answer one. (1 messages)]"}
    assert out[2] == messages[2]
    assert out[3] == messages[3]
    assert memory.count_units("s2") == 2


def test_below_watermark_archives_nothing():
    messages = [
        {"role": "user", "content": "tiny"},
        {"role": "assistant", "content": "tiny"},
    ]
    out = distill.distill_and_store_messages(
        "s3",
        messages,
        mode="gpu",
        estimate_fn=_small_estimate,
        budget_fn=_tiny_budget,
        keep_recent=0,
    )
    assert out is messages
    assert memory.count_units("s3") == 0


def test_non_archivable_mode_unchanged():
    messages = [{"role": "user", "content": "x"}]
    out = distill.distill_and_store_messages(
        "s4", messages, mode="guardrail", estimate_fn=_big_estimate, budget_fn=_tiny_budget
    )
    assert out is messages


def test_kill_switch_disabled(monkeypatch):
    monkeypatch.setattr(distill, "RELEVANCE_DISTILL", False)
    messages = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]
    out = distill.distill_and_store_messages(
        "s5",
        messages,
        mode="gpu",
        estimate_fn=_big_estimate,
        budget_fn=_tiny_budget,
        keep_recent=0,
    )
    assert out is messages
    assert memory.count_units("s5") == 0


def test_tool_chain_never_split_partial():
    messages = [
        {"role": "user", "content": "Research it."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "web_search"}, "arguments": "{}"}],
        },
        {"role": "tool", "content": '{"results": ["..."]}'},
        {"role": "assistant", "content": "Summary."},
    ]
    out = distill.distill_and_store_messages(
        "s6",
        messages,
        mode="gpu",
        estimate_fn=_big_estimate,
        budget_fn=_tiny_budget,
        keep_recent=1,
    )
    # the fused assistant+tool block must be archived whole, never alone
    roles = [m.get("role") for m in out]
    assert "tool" not in roles
    fused = memory.fetch_block("s6", 2)
    assert [m.get("role") for m in memory.deserialize(fused["raw"])] == [
        "assistant",
        "tool",
    ]


def test_marker_format_and_topic_truncation():
    messages = [
        {"role": "user", "content": "What is the capital of Bhutan? Please explain fully."},
        {"role": "assistant", "content": "Thimphu."},
        {"role": "user", "content": "Current."},
        {"role": "assistant", "content": "Still Thimphu."},
    ]
    out = distill.distill_and_store_messages(
        "s7",
        messages,
        mode="gpu",
        estimate_fn=_big_estimate,
        budget_fn=_tiny_budget,
        keep_recent=1,
    )
    assert out[0]["content"].startswith(
        "[#1: What is the capital of Bhutan? Please explain fully."
    )
    assert "(1 messages)" in out[0]["content"]


def test_memory_read_by_ids_and_missing():
    messages = [
        {"role": "user", "content": "Alpha topic."},
        {"role": "assistant", "content": "Alpha answer."},
        {"role": "user", "content": "Beta topic."},
        {"role": "assistant", "content": "Beta answer."},
    ]
    distill.distill_and_store_messages(
        "s8",
        messages,
        mode="gpu",
        estimate_fn=_big_estimate,
        budget_fn=_tiny_budget,
        keep_recent=1,
    )
    body = memory_read("s8", memory_ids=[1, 2, 999])
    assert "### [#1] Alpha topic." in body
    assert "### [#2] Alpha answer." in body
    assert "could not find" in body and "999" in body


def test_memory_read_keyword_query():
    messages = [
        {"role": "user", "content": "Tell me about aurora borealis."},
        {"role": "assistant", "content": "It glows green."},
        {"role": "user", "content": "Tell me about komodo dragons."},
        {"role": "assistant", "content": "They live in Indonesia."},
    ]
    distill.distill_and_store_messages(
        "s9",
        messages,
        mode="gpu",
        estimate_fn=_big_estimate,
        budget_fn=_tiny_budget,
        keep_recent=1,
    )
    body = memory_read("s9", query="aurora borealis")
    assert "aurora" in body and "Aurora" not in body
    no_hit = memory_read("s9", query="zebras")
    assert "No archived blocks matched" in no_hit


def test_memory_read_both_prefers_ids():
    messages = [{"role": "user", "content": "Gamma."}, {"role": "assistant", "content": "A."}]
    distill.distill_and_store_messages(
        "s10",
        messages,
        mode="gpu",
        estimate_fn=_big_estimate,
        budget_fn=_tiny_budget,
        keep_recent=0,
    )
    body = memory_read("s10", memory_ids=[1], query="gamma")
    assert "### [#1]" in body


def test_memory_read_no_args_guidance():
    out = memory_read("nobody", memory_ids=None, query="")
    assert "requires either memory_ids" in out


def test_sid_isolation():
    msgs = [{"role": "user", "content": "Isolation A."}, {"role": "assistant", "content": "A."}]
    distill.distill_and_store_messages(
        "ida",
        msgs,
        mode="gpu",
        estimate_fn=_big_estimate,
        budget_fn=_tiny_budget,
        keep_recent=0,
    )
    msgs2 = [{"role": "user", "content": "Isolation B."}, {"role": "assistant", "content": "B."}]
    distill.distill_and_store_messages(
        "idb",
        msgs2,
        mode="gpu",
        estimate_fn=_big_estimate,
        budget_fn=_tiny_budget,
        keep_recent=0,
    )
    assert "No archived blocks matched 'Isolation B'" in memory_read("ida", query="Isolation B")
    assert "Isolation B" in memory_read("idb", query="Isolation B")


def test_trim_old_caps_at_max():
    for i in range(5):
        messages = [
            {"role": "user", "content": f"Msg {i} alpha."},
            {"role": "assistant", "content": f"reply to msg {i}"},
        ]
        distill.distill_and_store_messages(
            "cap",
            messages,
            mode="gpu",
            estimate_fn=_big_estimate,
            budget_fn=_tiny_budget,
            keep_recent=0,
            max_units=3,
        )
    assert memory.count_units("cap") == 3
    top = memory.keyword_search("cap", "msg", limit=10)
    ids = sorted(b["block_id"] for b in top)
    assert ids == [8, 9, 10]  # oldest dropped, newest 3 remain


def test_purge_session_clears():
    messages = [{"role": "user", "content": "Purge me."}, {"role": "assistant", "content": "r"}]
    distill.distill_and_store_messages(
        "gone",
        messages,
        mode="gpu",
        estimate_fn=_big_estimate,
        budget_fn=_tiny_budget,
        keep_recent=0,
    )
    assert memory.count_units("gone") == 2
    memory.purge_session("gone")
    assert memory.count_units("gone") == 0