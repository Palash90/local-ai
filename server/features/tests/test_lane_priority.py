"""Lane priority: GPU queue ordering UI > OpenAI > MCP, FIFO within a lane,
plus between-round research preemption."""

import threading
from types import SimpleNamespace

import pytest

from server.features import orchestration
from server.features import state as _state
from server.features.orchestration import (
    LANE_RANK_MCP,
    LANE_RANK_OPENAI,
    LANE_RANK_UI,
    _enqueue_ranked,
    _higher_priority_waiting,
    _lane_rank,
    _maybe_park_research,
)


@pytest.fixture
def fake_entrypoint():
    """Register a stub entrypoint so the M proxy resolves in tests."""
    ep = SimpleNamespace(
        tasks={},
        _data_lock=threading.Lock(),
        _queue_locks={"gpu": threading.Lock()},
        _task_queues={"gpu": []},
    )
    _state.register_entrypoint(ep)
    try:
        yield ep
    finally:
        _state.register_entrypoint(None)


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


def _set_gpu_queue(ep, entries):
    with ep._queue_locks["gpu"]:
        ep._task_queues["gpu"][:] = list(entries)


def _park_setup(ep, task_id, research=True, status="working"):
    with ep._data_lock:
        ep.tasks[task_id] = {"status": status, "research": research,
                             "session_id": "sess", "mode": "gpu"}


def test_no_park_without_waiters(fake_entrypoint):
    ep = fake_entrypoint
    _set_gpu_queue(ep, [])
    _park_setup(ep, "r1")
    assert _maybe_park_research("r1", "sess", 3) is False
    with ep._data_lock:
        assert ep.tasks["r1"]["status"] == "working"


def test_no_park_for_non_research(fake_entrypoint):
    ep = fake_entrypoint
    _set_gpu_queue(ep, [_entry("ui1")])
    _park_setup(ep, "c1", research=False)
    assert _maybe_park_research("c1", "sess", 3) is False


def test_no_park_for_mcp_waiter(fake_entrypoint):
    ep = fake_entrypoint
    _set_gpu_queue(ep, [_entry("mcp1", _mcp=True)])
    _park_setup(ep, "r2")
    assert _higher_priority_waiting() is False
    assert _maybe_park_research("r2", "sess", 3) is False


def test_park_for_ui_waiter(fake_entrypoint):
    ep = fake_entrypoint
    _set_gpu_queue(ep, [_entry("ui1")])
    _park_setup(ep, "r3")
    assert _higher_priority_waiting() is True
    assert _maybe_park_research("r3", "sess", 7) is True
    with ep._data_lock:
        t = ep.tasks["r3"]
        assert t["status"] == "parked"
        assert t["_parked_round"] == 7
        assert t["_parked_sid"] == "sess"


def test_park_for_openai_waiter(fake_entrypoint):
    ep = fake_entrypoint
    _set_gpu_queue(ep, [_entry("oai1", openai_lane=True)])
    _park_setup(ep, "r4")
    assert _maybe_park_research("r4", "sess", 2) is True
    with ep._data_lock:
        assert ep.tasks["r4"]["status"] == "parked"


def test_no_park_when_already_terminal(fake_entrypoint):
    ep = fake_entrypoint
    _set_gpu_queue(ep, [_entry("ui1")])
    _park_setup(ep, "r5", status="done")
    assert _maybe_park_research("r5", "sess", 4) is False


def test_parked_task_requeues_at_own_lane_rank():
    # UI research toggle: requeued entry keeps rank 0, ahead of MCP.
    queue = [_entry("ui1"), _entry("mcp1", _mcp=True)]
    _enqueue_ranked(queue, _entry("r6", research=True))
    assert [e["task_id"] for e in queue] == ["ui1", "r6", "mcp1"]
    # MCP research task: keeps _mcp flag, FIFO within the MCP class.
    queue = [_entry("ui1"), _entry("mcp1", _mcp=True)]
    _enqueue_ranked(queue, _entry("r7", research=True, _mcp=True))
    assert [e["task_id"] for e in queue] == ["ui1", "mcp1", "r7"]
