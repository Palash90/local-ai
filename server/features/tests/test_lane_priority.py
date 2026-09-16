"""Lane priority: GPU queue ordering UI > OpenAI > MCP, FIFO within a lane."""

from server.features import orchestration
from server.features.orchestration import (
    LANE_RANK_MCP,
    LANE_RANK_OPENAI,
    LANE_RANK_UI,
    _enqueue_ranked,
    _lane_rank,
)


def _entry(task_id, **flags):
    d = {"task_id": task_id}
    d.update(flags)
    return d


def test_lane_rank_mapping():
    assert _lane_rank(_entry("a")) == LANE_RANK_UI
    assert _lane_rank(_entry("b", openai_lane=True)) == LANE_RANK_OPENAI
    assert _lane_rank(_entry("c", _mcp=True)) == LANE_RANK_MCP
    # MCP flag wins when both are set (should not happen in practice).
    assert _lane_rank(_entry("d", openai_lane=True, _mcp=True)) == LANE_RANK_MCP
    assert _lane_rank(None) == LANE_RANK_UI


def test_enqueue_ranked_orders_lanes():
    queue = []
    _enqueue_ranked(queue, _entry("mcp1", _mcp=True))
    _enqueue_ranked(queue, _entry("oai1", openai_lane=True))
    _enqueue_ranked(queue, _entry("ui1"))
    assert [e["task_id"] for e in queue] == ["ui1", "oai1", "mcp1"]


def test_enqueue_ranked_fifo_within_lane():
    queue = []
    _enqueue_ranked(queue, _entry("oai1", openai_lane=True))
    _enqueue_ranked(queue, _entry("mcp1", _mcp=True))
    _enqueue_ranked(queue, _entry("oai2", openai_lane=True))
    _enqueue_ranked(queue, _entry("mcp2", _mcp=True))
    _enqueue_ranked(queue, _entry("ui1"))
    assert [e["task_id"] for e in queue] == ["ui1", "oai1", "oai2", "mcp1", "mcp2"]


def test_enqueue_ranked_empty_and_single():
    queue = []
    _enqueue_ranked(queue, _entry("only", _mcp=True))
    assert [e["task_id"] for e in queue] == ["only"]


def test_no_human_priority_residue():
    assert not hasattr(orchestration, "_human_priority_active")
