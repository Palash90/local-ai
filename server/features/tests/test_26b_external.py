"""External 26B lane (big-boy): no local lifecycle, health-gated ensure.

Mirrors the GUARDRAIL_EXTERNAL contract: when LLAMA_BASE_26B points off-box
(LLAMA_26B_EXTERNAL auto-detect, or explicit env), this box must never
spawn/kill/unload the :8089 server, never apply the local-VRAM boot gate,
and never evict/reload the remote model around image renders.
"""

import types

import server.config as config
import server.features.images as images
import server.features.llm as llm
import server.features.monitoring as mon


def _fake_m(monkeypatch, mod, **attrs):
    fake_m = types.SimpleNamespace(**attrs)
    monkeypatch.setattr(mod, "M", fake_m)
    return fake_m


# ── config auto-detect ───────────────────────────────────────────────────

def test_is_local_host_loopback_variants():
    assert config._is_local_host("http://localhost:8089") is True
    assert config._is_local_host("http://127.0.0.1:8089") is True
    assert config._is_local_host("http://[::1]:8089") is True


def test_is_local_host_remote():
    assert config._is_local_host("http://big-boy.local:8089") is False
    assert config._is_local_host("http://192.168.29.213:8089") is False


# ── ensure: health-gated, never spawns ───────────────────────────────────

def test_ensure_external_alive_true_no_spawn(monkeypatch):
    _fake_m(monkeypatch, mon,
            LLAMA_26B_EXTERNAL=True,
            server_base=lambda mode: "http://big-boy.local:8089",
            is_llama_alive=lambda base: True)

    def _boom(mode):
        raise AssertionError("must not spawn remote lane")

    monkeypatch.setattr(mon, "restart_llama_server", _boom)
    assert mon.ensure_llama_server("gpu", "gemma4-26b") is True


def test_ensure_external_down_false_no_spawn(monkeypatch):
    _fake_m(monkeypatch, mon,
            LLAMA_26B_EXTERNAL=True,
            server_base=lambda mode: "http://big-boy.local:8089",
            is_llama_alive=lambda base: False)

    def _boom(mode):
        raise AssertionError("must not spawn remote lane")

    monkeypatch.setattr(mon, "restart_llama_server", _boom)

    def _boom_gate(thr, tmo):
        raise AssertionError("local VRAM gate is invalid for remote lane")

    monkeypatch.setattr(mon, "_wait_vram_below", _boom_gate)
    assert mon.ensure_llama_server("gpu", "gemma4-26b") is False


def test_ensure_local_unchanged(monkeypatch):
    _fake_m(monkeypatch, mon,
            LLAMA_26B_EXTERNAL=False,
            server_base=lambda mode: "http://localhost:8089",
            is_llama_alive=lambda base: False)
    monkeypatch.setattr(mon, "_wait_vram_below", lambda thr, tmo: True)
    called = []
    monkeypatch.setattr(mon, "restart_llama_server", lambda mode: called.append(mode))
    assert mon.ensure_llama_server("gpu", "gemma4-26b") is True
    assert called == ["26b"]


# ── restart / kill / unload: no-ops ──────────────────────────────────────

def test_restart_26b_external_noop(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not touch remote lane")

    _fake_m(monkeypatch, mon, LLAMA_26B_EXTERNAL=True,
            kill_llama_server=_boom)
    mon.restart_llama_server("26b")
    mon.kill_llama_server("26b")


def test_unload_26b_external_noop(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not touch remote lane")

    _fake_m(monkeypatch, llm, LLAMA_26B_EXTERNAL=True,
            kill_llama_server=_boom)
    assert llm.unload_llama_model("26b") is True


# ── images: never evict/reload remote 26b ────────────────────────────────

def test_lanes_snapshot_reports_remote_26b_idle(monkeypatch):
    _fake_m(monkeypatch, images,
            LLAMA_26B_EXTERNAL=True,
            server_status=lambda mode: "chat_loaded")
    gpu_was, guard_was, b26_was = images._lanes_loaded_for_reload()
    assert (gpu_was, guard_was) == (True, True)
    assert b26_was is False


def test_lanes_snapshot_local_unchanged(monkeypatch):
    _fake_m(monkeypatch, images,
            LLAMA_26B_EXTERNAL=False,
            server_status=lambda mode: "chat_loaded")
    assert images._lanes_loaded_for_reload() == (True, True, True)


# ── residency: remote 26b needs no local choreography ────────────────────

def test_lane_keep_resident_26b_external(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_26B_EXTERNAL", True)
    assert config.lane_keep_resident("26b") is True
    monkeypatch.setattr(config, "LLAMA_26B_EXTERNAL", False)
    assert config.lane_keep_resident("26b") is False
