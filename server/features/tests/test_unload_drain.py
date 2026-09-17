"""Unload drain protection: refuse mid-inference kills, retry, then force."""

import threading
import types

import server.features.llm as llm
import server.features.images as images


class _Resp:
    def __init__(self, status_code=200, text='{"success":true}'):
        self.status_code = status_code
        self.text = text


def _fake_llm_m(monkeypatch, **attrs):
    base = dict(
        _model_transition_lock=threading.Lock(),
        _data_lock=threading.Lock(),
        _chat_generating_lock=threading.Lock(),
        _chat_generating_by_lane={},
    )
    base.update(attrs)
    fake_m = types.SimpleNamespace(**base)
    monkeypatch.setattr(llm, "M", fake_m)
    return fake_m


def test_unload_refuses_while_lane_busy(monkeypatch):
    _fake_llm_m(
        monkeypatch,
        server_status=lambda mode: "chat_loaded",
        _chat_generating_by_lane={"gpu": 1},
    )

    def _boom(*a, **k):
        raise AssertionError("unload POST must not fire while busy")

    monkeypatch.setattr(
        llm, "requests", types.SimpleNamespace(post=_boom)
    )
    assert llm.unload_llama_model("gpu") is False


def test_unload_force_bypasses_busy_check(monkeypatch):
    posts = []
    _fake_llm_m(
        monkeypatch,
        server_status=lambda mode: "chat_loaded",
        _chat_generating_by_lane={"gpu": 1},
        model_status="chat_loaded",
        server_base=lambda mode: "http://x:8081",
        server_model_id=lambda mode: "m",
    )
    monkeypatch.setattr(
        llm, "requests", types.SimpleNamespace(post=lambda *a, **k: (posts.append(k), _Resp())[1])
    )
    monkeypatch.setattr(llm, "save_slot_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(llm, "_wait_vram_freed", lambda *a, **k: True)
    assert llm.unload_llama_model("gpu", force=True) is True
    assert len(posts) == 1


def test_unload_idle_lane_still_unloads(monkeypatch):
    posts = []
    _fake_llm_m(
        monkeypatch,
        server_status=lambda mode: "chat_loaded",
        _chat_generating_by_lane={"gpu": 0},
        model_status="chat_loaded",
        server_base=lambda mode: "http://x:8081",
        server_model_id=lambda mode: "m",
    )
    monkeypatch.setattr(
        llm, "requests", types.SimpleNamespace(post=lambda *a, **k: (posts.append(k), _Resp())[1])
    )
    monkeypatch.setattr(llm, "save_slot_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(llm, "_wait_vram_freed", lambda *a, **k: True)
    assert llm.unload_llama_model("gpu") is True
    assert len(posts) == 1


def _fake_images_m(monkeypatch, unloads, counts):
    calls = []

    def _unload(mode, force=False):
        calls.append(force)
        return unloads.pop(0)

    fake_m = types.SimpleNamespace(
        unload_llama_model=_unload,
        lane_generating_count=lambda mode: counts.pop(0),
    )
    monkeypatch.setattr(images, "M", fake_m)
    return calls


def test_render_unload_retries_until_drained(monkeypatch):
    import time as _time_mod

    monkeypatch.setattr(_time_mod, "sleep", lambda s: None)
    calls = _fake_images_m(monkeypatch, [False, True], [1])
    assert images._unload_lane_for_render("gpu", "image") is True
    # Drain retries stay non-forced; force is reserved for budget expiry.
    assert calls == [False, False]


def test_render_unload_forces_after_budget(monkeypatch):
    import time as _time_mod

    monkeypatch.setattr(_time_mod, "sleep", lambda s: None)
    calls = _fake_images_m(monkeypatch, [False, False], [1])
    assert (
        images._unload_lane_for_render("gpu", "image", drain_budget=0) is False
    )
    assert calls == [False, True]
