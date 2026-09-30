"""Door-B VRAM gate: a dead :8089 must never reboot into occupied VRAM."""

import threading
import types

import server.features.monitoring as mon


def _fake_m(monkeypatch, alive):
    fake_m = types.SimpleNamespace(
        _data_lock=threading.Lock(),
        GUARDRAIL_EXTERNAL=False,
        server_base=lambda mode: "http://localhost:8089" if mode == "26b" else "http://localhost:8081",
        is_llama_alive=lambda base: alive.get(base, False),
    )
    monkeypatch.setattr(mon, "M", fake_m)
    return fake_m


def test_26b_boot_refused_when_vram_held(monkeypatch):
    _fake_m(monkeypatch, {"http://localhost:8089": False})
    monkeypatch.setattr(mon, "_wait_vram_below", lambda thr, tmo: False)
    called = []
    monkeypatch.setattr(mon, "restart_llama_server", lambda mode: called.append(mode))
    assert mon.ensure_llama_server("gpu", "gemma4-26b") is False
    assert called == []  # zero boot attempts -> zero OOMs


def test_26b_boot_proceeds_once_vram_free(monkeypatch):
    _fake_m(monkeypatch, {"http://localhost:8089": False})
    monkeypatch.setattr(mon, "_wait_vram_below", lambda thr, tmo: True)
    called = []
    monkeypatch.setattr(mon, "restart_llama_server", lambda mode: called.append(mode))
    assert mon.ensure_llama_server("gpu", "gemma4-26b") is True
    assert called == ["26b"]


def test_other_lanes_ungated(monkeypatch):
    _fake_m(monkeypatch, {"http://localhost:8081": False})
    called = []
    monkeypatch.setattr(mon, "restart_llama_server", lambda mode: called.append(mode))
    assert mon.ensure_llama_server("gpu") is True
    assert called == ["gpu"]


def test_alive_server_no_restart(monkeypatch):
    _fake_m(monkeypatch, {"http://localhost:8089": True})
    called = []
    monkeypatch.setattr(mon, "restart_llama_server", lambda mode: called.append(mode))
    assert mon.ensure_llama_server("gpu", "gemma4-26b") is True
    assert called == []
