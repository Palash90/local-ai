"""load_llama_model: VRAM wait + single retry on GPU/guardrail loads (mocked)."""

import threading
import types

import pytest

import server.features.llm as llm


class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {}


def _install(monkeypatch, post_script, ready=True):
    """Fake M + HTTP; return (calls dict, fake_m.

    ready may be a bool or a per-is_model_ready-call script list.
    """
    calls = {"post": 0, "ready_calls": 0, "vram_wait": [], "restored": [], "sleeps": []}
    status = {"mode": "unloaded", "cpu": "unloaded", "guardrail": "unloaded"}

    def fake_post(url, json=None, timeout=None, **k):
        calls["post"] += 1
        code, text = post_script[min(calls["post"] - 1, len(post_script) - 1)]
        return _Resp(code, text)

    def fake_ready(base, mid):
        calls["ready_calls"] += 1
        if isinstance(ready, list):
            return ready[min(calls["ready_calls"] - 1, len(ready) - 1)]
        return ready

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(
        llm, "_wait_image_active_clear", lambda *a, **k: None)
    monkeypatch.setattr(
        llm, "_wait_vram_freed",
        lambda threshold_mb=500, timeout=30: calls["vram_wait"].append(
            (threshold_mb, timeout)) or True)
    monkeypatch.setattr("time.sleep", lambda s: calls["sleeps"].append(s))
    fake_m = types.SimpleNamespace(
        server_model_id=lambda mode: "mid-" + mode,
        server_base=lambda mode: "http://x/",
        is_model_ready=fake_ready,
        _model_transition_lock=threading.Lock(),
        _data_lock=threading.Lock(),
        model_status="unloaded",
        _cpu_model_status="unloaded",
        _guardrail_model_status="unloaded",
        _guardrail_loaded_model="",
        _cpu_last_llm_use=0,
        _guardrail_last_llm_use=0,
        _last_llm_use=0,
        restore_slot_checkpoint=lambda mode: calls["restored"].append(mode),
        GUARDRAIL_EXTERNAL=False,
    )
    monkeypatch.setattr(llm, "M", fake_m)
    return calls, fake_m


def test_gpu_load_waits_vram_then_succeeds(monkeypatch):
    calls, fake_m = _install(monkeypatch, [(200, "ok")], ready=True)
    assert llm.load_llama_model("gpu") is True
    assert calls["vram_wait"] == [(500, 60)]
    assert calls["post"] == 1
    assert fake_m.model_status == "chat_loaded"
    assert calls["restored"] == ["gpu"]


def test_gpu_load_retries_once_after_500(monkeypatch):
    # 500 then not-ready: fallback can't save attempt 1, retry fires.
    calls, fake_m = _install(
        monkeypatch, [(500, "boom"), (200, "ok")],
        ready=[False, False, True])
    assert llm.load_llama_model("gpu") is True
    assert calls["post"] == 2
    # Retry path sleeps 10s then re-waits (shorter budget).
    assert 10 in calls["sleeps"]
    assert (500, 30) in calls["vram_wait"]
    assert fake_m.model_status == "chat_loaded"


def test_gpu_load_gives_up_after_two_failures(monkeypatch):
    calls, fake_m = _install(
        monkeypatch, [(500, "boom"), (500, "boom")], ready=False)
    assert llm.load_llama_model("gpu") is False
    assert calls["post"] == 2
    assert fake_m.model_status == "unloaded"


def test_cpu_load_never_waits_vram(monkeypatch):
    calls, fake_m = _install(monkeypatch, [(200, "ok")], ready=True)
    assert llm.load_llama_model("cpu") is True
    assert calls["vram_wait"] == []
    assert fake_m._cpu_model_status == "chat_loaded"
