"""GPU-only content judges: routing, fallback, policy.

Content (output) judges must verdict on the resident GPU chat model —
never the CPU guardrail lane, never a remote endpoint. Input (L2) judges
are out of scope here (they stay CPU-or-tablet by design).
"""

import json
import types

import server.features.judge as judge


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def _ep(**over):
    attrs = {
        "server_base": lambda mode: "http://localhost:8081",
        "_image_active": False,
        "mark_slot_kv_dirty": lambda mode: None,
    }
    attrs.update(over)
    return types.SimpleNamespace(**attrs)


def _register(monkeypatch, **over):
    from server.features import state as _state
    ep = _ep(**over)
    monkeypatch.setattr(_state._Registry, "entrypoint", ep)
    return ep


def _no_mark(monkeypatch):
    import server.features.llm as _llm
    monkeypatch.setattr(_llm, "_mark_chat_generating", lambda *a, **k: None)


def _verdict_payload(content="SAFE", reasoning=""):
    msg = {"role": "assistant", "content": content}
    if reasoning:
        msg["reasoning_content"] = reasoning
    return {"choices": [{"message": msg}]}


# ── routing ────────────────────────────────────────────────────────────────

def test_gpu_verdict_posts_to_gpu_with_chat_model(monkeypatch):
    _register(monkeypatch)
    _no_mark(monkeypatch)
    monkeypatch.setattr(judge, "_chat_model_id", lambda: "gemma4-e4b-q4")
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        seen["payload"] = json
        return _Resp(200, _verdict_payload("SAFE"))

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    model, content = judge._gpu_verdict_post("t", "sys", "hello", 90)
    assert model == "gemma4-e4b-q4"
    assert content == "SAFE"
    assert seen["url"] == "http://localhost:8081/v1/chat/completions"
    assert "8083" not in seen["url"] and "11434" not in seen["url"]
    assert seen["payload"]["model"] == "gemma4-e4b-q4"
    assert seen["payload"]["reasoning_budget_tokens"] == 256
    assert seen["payload"]["cache_prompt"] is False


def test_gpu_verdict_reasoning_fallback(monkeypatch):
    _register(monkeypatch)
    _no_mark(monkeypatch)
    monkeypatch.setattr(judge, "_chat_model_id", lambda: "gemma4-e4b-q4")

    def fake_post(url, json=None, timeout=None):
        return _Resp(200, _verdict_payload("", reasoning="HARMFUL plotting"))

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    model, content = judge._gpu_verdict_post("t", "sys", "hello", 90)
    assert model == "gemma4-e4b-q4"
    assert content == "HARMFUL plotting"


def test_gpu_verdict_down_returns_none(monkeypatch):
    _register(monkeypatch)
    _no_mark(monkeypatch)
    monkeypatch.setattr(judge, "_chat_model_id", lambda: "gemma4-e4b-q4")

    def fake_post(url, json=None, timeout=None):
        raise ConnectionError("refused")

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    assert judge._gpu_verdict_post("t", "sys", "hello", 5) == (None, None)


def test_gpu_verdict_retries_transient_500(monkeypatch):
    """A 500 right after generation (slot-drain race: 'proxy error: Failed
    to read connection') gets one breather + retry instead of an instant
    fail-closed BLOCK."""
    _register(monkeypatch)
    _no_mark(monkeypatch)
    monkeypatch.setattr(judge, "_chat_model_id", lambda: "gemma4-e4b-q4")
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            return _Resp(500, {}, text="proxy error: Failed to read connection")
        return _Resp(200, _verdict_payload("SAFE"))

    import requests
    import time as _time_mod
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(_time_mod, "sleep", lambda s: None)
    model, content = judge._gpu_verdict_post("t", "sys", "hello", 90)
    assert (model, content) == ("gemma4-e4b-q4", "SAFE")
    assert len(calls) == 2


def test_gpu_verdict_gives_up_after_second_500(monkeypatch):
    _register(monkeypatch)
    _no_mark(monkeypatch)
    monkeypatch.setattr(judge, "_chat_model_id", lambda: "gemma4-e4b-q4")

    def fake_post(url, json=None, timeout=None):
        return _Resp(500, {}, text="proxy error")

    import requests
    import time as _time_mod
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(_time_mod, "sleep", lambda s: None)
    assert judge._gpu_verdict_post("t", "sys", "hello", 5) == (None, None)


# ── entry points ignore CPU/remote routing ─────────────────────────────────

def test_strict_judge_ignores_remote_base_and_pin(monkeypatch):
    _register(monkeypatch)
    _no_mark(monkeypatch)
    monkeypatch.setattr(judge, "_chat_model_id", lambda: "gemma4-e4b-q4")
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        return _Resp(200, _verdict_payload("SAFE"))

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    out = judge.mcp_output_judge(
        "harmless reply", model_id="gemma-4-E2B-it-Q4_K_M",
    )
    assert out is False
    assert seen["url"] == "http://localhost:8081/v1/chat/completions"


def test_strict_judge_fail_closed_when_gpu_down(monkeypatch):
    _register(monkeypatch)
    _no_mark(monkeypatch)
    monkeypatch.setattr(judge, "_chat_model_id", lambda: "gemma4-e4b-q4")

    def fake_post(url, json=None, timeout=None):
        raise ConnectionError("down")

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    assert judge.mcp_output_judge("anything", timeout=5) is True
    assert judge.mcp_output_judge("anything", timeout=5,
                                  fail_closed=False) is False


def test_quality_judge_ignores_exclusion_and_remote(monkeypatch):
    _register(monkeypatch)
    _no_mark(monkeypatch)
    monkeypatch.setattr(judge, "_chat_model_id", lambda: "gemma4-e4b-q4")
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        return _Resp(200, _verdict_payload("VERDICT: OK\nQUALITY: 90/100"))

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    res = judge.llm_verify_answer_quality(
        "q", "a",
        base_url="http://192.168.29.164:11434",
        model_id="qwen3:4b-q4_K_M",
        allow_gpu_fallback=False,
        exclude_models={"gemma4-e4b-q4"},  # same-model grading is intended
    )
    assert res is not None and res["ok"] is True and res["quality"] == 90
    assert res["model"] == "gemma4-e4b-q4"
    assert seen["url"] == "http://localhost:8081/v1/chat/completions"


def test_research_judge_gpu_only(monkeypatch):
    _register(monkeypatch)
    _no_mark(monkeypatch)
    monkeypatch.setattr(judge, "_chat_model_id", lambda: "gemma4-e4b-q4")
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        return _Resp(200, _verdict_payload("VERDICT: OK\nQUALITY: 82/100"))

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    res = judge.llm_verify_research_answer("q", "a (Doe, X, 2024) [http://x]",
                                           base_url="http://localhost:8083")
    assert res is not None and res["ok"] is True
    assert "8083" not in seen["url"]


def test_output_classify_gpu_only(monkeypatch):
    _register(monkeypatch)
    _no_mark(monkeypatch)
    monkeypatch.setattr(judge, "_chat_model_id", lambda: "gemma4-e4b-q4")
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        return _Resp(200, _verdict_payload("HARMFUL instructions"))

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    assert judge.llm_classify_harmful_output(
        "bad", base_url="http://localhost:8083") is True
    assert seen["url"].startswith("http://localhost:8081/")


# ── render hold (GPU lane must wait out image renders) ─────────────────────

def test_gpu_render_hold_waits_for_image_active(monkeypatch):
    ep = _ep()
    object.__setattr__(ep, "_image_active", True)

    from server.features import state as _state
    monkeypatch.setattr(_state._Registry, "entrypoint", ep)

    import time as _time_mod
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        object.__setattr__(ep, "_image_active", False)

    monkeypatch.setattr(_time_mod, "sleep", fake_sleep)
    assert judge._wait_gpu_render_safe("t", timeout=30, cooldown=0) is True
    assert sleeps  # held at least one cycle


def test_gpu_render_hold_skipped_when_idle(monkeypatch):
    _register(monkeypatch, _image_active=False)

    def _boom(s):
        raise AssertionError("must not sleep when idle")

    import time as _time_mod
    monkeypatch.setattr(_time_mod, "sleep", _boom)
    assert judge._wait_gpu_render_safe("t") is True
