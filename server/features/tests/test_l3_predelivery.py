"""L3 pre-delivery gate: verdict before append, refusal substitute, outage note."""

import threading
import types

import server.features.orchestration as orch


def _fake_m(monkeypatch, tasks, sessions=None):
    if sessions is None:
        sessions = {"s1": []}
    else:
        sessions.setdefault("s1", [])
    fake_m = types.SimpleNamespace(
        _data_lock=threading.Lock(),
        tasks=tasks,
        sessions=sessions,
        sessions_meta={},
        task_mode=lambda tid: "gpu",
        server_model_id=lambda mode, override=None: override or "m",
        context_token_report=lambda sid, msgs: {},
        save_sessions=lambda: None,
    )
    monkeypatch.setattr(orch, "M", fake_m)
    return fake_m, sessions


def _body():
    return {"timings": {}, "choices": [{"message": {}}]}


def _task(**over):
    base = {"_tools_used": [], "_original_message": "hi"}
    base.update(over)
    return {"t1": base}


def _mock_l3(monkeypatch, pattern=False, verdict=False):
    import server.input_guard as ig
    import server.features.judge as jd
    monkeypatch.setattr(ig, "is_strict_output_blocked", lambda text: pattern)
    seen = {}
    def fake_judge(text, timeout=None, fail_closed=True, model_id=None,
                   allow_gpu_fallback=False):
        seen["called"] = True
        seen["fail_closed"] = fail_closed
        return verdict
    monkeypatch.setattr(jd, "mcp_output_judge", fake_judge)
    return seen


def test_safe_reply_delivered_verbatim(monkeypatch):
    _, sessions = _fake_m(monkeypatch, _task())
    seen = _mock_l3(monkeypatch, pattern=False, verdict=False)
    orch._finalize_task("t1", "s1", "hello world", _body())
    assert seen.get("called") is True  # gated: no simple-turn skip
    assert sessions["s1"][0]["content"] == "hello world"


def test_blocked_reply_substituted_original_withheld(monkeypatch):
    tasks = _task()
    _, sessions = _fake_m(monkeypatch, tasks)
    _mock_l3(monkeypatch, pattern=False, verdict=True)
    orch._finalize_task("t1", "s1", "SECRET-BAD-CONTENT", _body())
    delivered = sessions["s1"][0]["content"]
    assert delivered == "I can't provide that response."
    assert "SECRET-BAD-CONTENT" not in delivered
    assert "SECRET-BAD-CONTENT" not in tasks["t1"]["response"]
    assert tasks["t1"]["_l3_verdict"] == "BLOCKED"


def test_pattern_block_skips_llm_judge(monkeypatch):
    _, sessions = _fake_m(monkeypatch, _task())
    seen = _mock_l3(monkeypatch, pattern=True, verdict=False)
    orch._finalize_task("t1", "s1", "blocked words here", _body())
    assert seen.get("called") is not True
    assert sessions["s1"][0]["content"] == "I can't provide that response."


def test_judge_outage_delivers_with_unverified_note(monkeypatch):
    tasks = _task()
    _, sessions = _fake_m(monkeypatch, tasks)
    _mock_l3(monkeypatch, pattern=False, verdict=None)
    orch._finalize_task("t1", "s1", "normal answer", _body())
    assert sessions["s1"][0]["content"] == "normal answer"
    assert tasks["t1"]["_l3_verdict"] == "UNVERIFIED"
    notes = [v for v in tasks["t1"].get("_verification", [])
             if v.get("action") == "SCREENED"]
    assert len(notes) == 1


def test_mcp_blocked_keeps_contract(monkeypatch):
    import server.mcp_tasks_db as db
    tasks = _task(_mcp=True)
    _, sessions = _fake_m(monkeypatch, tasks)
    _mock_l3(monkeypatch, pattern=False, verdict=True)
    seen = {}
    monkeypatch.setattr(db, "mcp_task_update",
                        lambda task_id, **kw: seen.update(kw))
    orch._finalize_task("t1", "s1", "mcp secret", _body())
    assert seen.get("verification_level") == "LEVEL 3 OUTPUT VERIFICATION FAILED"
    assert seen.get("reply") == "mcp secret"


def test_verdict_holds_generating_mark_so_evict_refuses(monkeypatch):
    """An in-flight verdict must count as live inference: unload paths
    refuse while lane_generating_count > 0, so evicting mid-L3 is refused
    rather than killing delivery."""
    import server.features.judge as jd
    import server.features.llm as llm_mod
    from server.features import state as _state
    ep = types.SimpleNamespace(
        server_base=lambda mode, override=None: "http://localhost:8081",
        _image_active=False,
        mark_slot_kv_dirty=lambda mode: None,
        _chat_generating_lock=threading.Lock(),
        _chat_generating=0,
        _chat_generating_by_lane={},
    )
    monkeypatch.setattr(_state._Registry, "entrypoint", ep)
    monkeypatch.setattr(jd, "_chat_model_id", lambda: "m")
    observed = {}

    import requests
    def fake_post(url, json=None, timeout=None):
        observed["generating"] = llm_mod.lane_generating_count("gpu")
        return _Resp(200, {"choices": [{"message": {"content": "SAFE"}}]})
    monkeypatch.setattr(requests, "post", fake_post)
    jd._gpu_verdict_post("t", "sys", "hello", 90)
    assert observed.get("generating", 0) > 0
    assert llm_mod.lane_generating_count("gpu") == 0


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "{}"

    def json(self):
        return self._payload


def test_openai_lane_skips_l3_entirely(monkeypatch):
    """OpenAI lane policy: no pattern scan, no LLM verdict, no annotation —
    blockable text is delivered verbatim."""
    import server.input_guard as ig
    import server.features.judge as jd
    calls = []
    monkeypatch.setattr(ig, "is_strict_output_blocked",
                        lambda text: calls.append("pattern") or True)
    monkeypatch.setattr(jd, "mcp_output_judge",
                        lambda *a, **k: calls.append("judge") or True)
    tasks = _task(openai_lane=True)
    _, sessions = _fake_m(monkeypatch, tasks)
    orch._finalize_task("t1", "s1", "blockable words here", _body())
    assert calls == []
    assert sessions["s1"][0]["content"] == "blockable words here"
    assert tasks["t1"]["response"] == "blockable words here"
    assert "_l3_verdict" not in tasks["t1"]
    assert all(v.get("action") != "BLOCKED"
               for v in tasks["t1"].get("_verification", []))


def test_score_only_dump_never_blocks(monkeypatch):
    """A pasted music-score draft (no prose) must not BLOCK: the prose-only
    scan is empty, so neither the pattern filter nor the LLM judge runs."""
    import server.input_guard as ig
    import server.features.judge as jd
    calls = []
    monkeypatch.setattr(ig, "is_strict_output_blocked",
                        lambda text: calls.append(("pattern", text)) or True)
    monkeypatch.setattr(jd, "mcp_output_judge",
                        lambda *a, **k: calls.append(("judge", a[0])) or True)
    _, sessions = _fake_m(monkeypatch, _task())
    score_dump = ('```json\n{"score": "[GENRE: Indian Ambient]\\n@intro\\n'
                  '[ROLE drone instr: Tanpura]\\nC4:16\\n'
                  '[ROLE melody instr: Bansuri]\\nC5:16", "tempo": 50}\n```')
    orch._finalize_task("t1", "s1", score_dump, _body())
    assert calls == []
    assert sessions["s1"][0]["content"] == score_dump


def test_prose_around_score_still_judged(monkeypatch):
    """Prose stays judged even when a score block rides along."""
    seen = {}
    import server.input_guard as ig
    import server.features.judge as jd
    monkeypatch.setattr(ig, "is_strict_output_blocked", lambda text: False)
    def fake_judge(text, timeout=None, fail_closed=True, model_id=None,
                   allow_gpu_fallback=False):
        seen["text"] = text
        return False
    monkeypatch.setattr(jd, "mcp_output_judge", fake_judge)
    _, sessions = _fake_m(monkeypatch, _task())
    orch._finalize_task("t1", "s1",
                        "Here is your track:\n```json\n{\"score\": \"x\"}\n```",
                        _body())
    assert "Here is your track" in seen.get("text", "")
    assert "score" not in seen.get("text", "")


def test_openai_lane_never_cooccurs_with_mcp(monkeypatch):
    """_mcp always wins: if both flags ever co-occur, the task is judged
    fail-closed instead of silently skipping a lane that needed it."""
    import server.mcp_tasks_db as db
    tasks = _task(openai_lane=True, _mcp=True)
    _, sessions = _fake_m(monkeypatch, tasks)
    _mock_l3(monkeypatch, pattern=False, verdict=False)
    seen = {}
    monkeypatch.setattr(db, "mcp_task_update",
                        lambda task_id, **kw: seen.update(kw))
    orch._finalize_task("t1", "s1", "hi", _body())
    assert seen.get("verification_level") == "LEVEL 3 OUTPUT VERIFICATION PASSED"
    assert sessions["s1"][0]["content"] == "hi"
