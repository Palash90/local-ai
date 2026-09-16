"""Sampling router runs on the CPU lane (never the task's own lane)."""

import types

import server.features.llm as llm


def _fake_m(monkeypatch):
    calls = {}

    class Resp:
        def json(self):
            return {"choices": [{"message": {"content": "  CHAT  "}}]}

    def fake_post(url, json=None, timeout=None):
        calls["url"] = url
        calls["body"] = json
        return Resp()

    fake_m = types.SimpleNamespace(
        server_url=lambda mode: f"http://127.0.0.1:lane-{mode}",
        server_model_id=lambda mode: f"model-{mode}",
        SAMPLING_ROUTER_PROMPT="classify",
        SAMPLING_BUCKETS={"chat": {"temperature": 1.0}},
        SAMPLING_ROUTER_TIMEOUT=5,
        SAMPLING_ROUTER_MAX_TOKENS=256,
    )
    monkeypatch.setattr(llm, "M", fake_m)
    monkeypatch.setattr(llm, "_last_user_text", lambda messages: "hello there")
    monkeypatch.setattr(llm.requests, "post", fake_post)
    return calls


def test_router_posts_to_cpu_lane_not_task_lane(monkeypatch):
    calls = _fake_m(monkeypatch)
    out = llm._route_sampling("gpu", [{"role": "user", "content": "hello there"}])
    assert calls["url"] == "http://127.0.0.1:lane-cpu"
    assert calls["body"]["model"] == "model-cpu"
    assert out == {"temperature": 1.0}


def test_router_failure_still_falls_back_to_defaults(monkeypatch):
    _fake_m(monkeypatch)

    def boom(url, json=None, timeout=None):
        raise ConnectionError("cpu lane down")

    monkeypatch.setattr(llm.requests, "post", boom)
    assert llm._route_sampling("gpu", [{"role": "user", "content": "hi"}]) == {}
