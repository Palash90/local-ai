"""Never-evict residency flags + external-guardrail lifecycle guards."""

import threading
import types

import server.features.monitoring as monitoring


def _fake_m(monkeypatch, **attrs):
    fake_m = types.SimpleNamespace(**attrs)
    monkeypatch.setattr(monitoring, "M", fake_m)
    return fake_m


# ── idle-unload suppression ────────────────────────────────────────────────

def test_idle_unload_suppressed_when_lane_kept(monkeypatch):
    _fake_m(monkeypatch, lane_keep_resident=lambda mode: True)
    assert monitoring._idle_unload_suppressed("gpu") is True
    assert monitoring._idle_unload_suppressed("cpu") is True
    assert monitoring._idle_unload_suppressed("guardrail") is True


def test_idle_unload_not_suppressed_by_default(monkeypatch):
    _fake_m(monkeypatch, lane_keep_resident=lambda mode: False)
    assert monitoring._idle_unload_suppressed("gpu") is False
    assert monitoring._idle_unload_suppressed("cpu") is False
    assert monitoring._idle_unload_suppressed("guardrail") is False


def test_idle_unload_suppressed_fails_safe_without_entrypoint(monkeypatch):
    class Bad:
        def __getattr__(self, name):
            raise RuntimeError("no entrypoint")

    monkeypatch.setattr(monitoring, "M", Bad())
    assert monitoring._idle_unload_suppressed("gpu") is False


# ── image-render CPU eviction ──────────────────────────────────────────────

def test_evict_cpu_skipped_when_kept_resident(monkeypatch):
    calls = []
    _fake_m(
        monkeypatch,
        lane_keep_resident=lambda mode: True,
        unload_llama_model=lambda *a, **k: calls.append((a, k)),
    )

    def _boom():
        raise AssertionError("_free_ram_mb must not be consulted")

    monkeypatch.setattr(monitoring, "_free_ram_mb", _boom)
    monitoring.evict_cpu_model_for_image()
    assert calls == []


# ── verify-kill escalation ─────────────────────────────────────────────────

def _fast_clock(monkeypatch):
    now = [1000.0]

    def _time():
        return now[0]

    def _sleep(secs):
        now[0] += secs

    import time as _time_mod

    monkeypatch.setattr(_time_mod, "time", _time)
    monkeypatch.setattr(_time_mod, "sleep", _sleep)


def test_verify_kill_escalation_fires_without_keep_flag(monkeypatch):
    kills = []
    _fake_m(
        monkeypatch,
        lane_keep_resident=lambda mode: False,
        kill_llama_server=lambda mode: kills.append(mode),
        _data_lock=threading.Lock(),
        _cpu_model_status="chat_loaded",
    )
    monkeypatch.setattr(monitoring, "_free_ram_mb", lambda: 1500)
    _fast_clock(monkeypatch)
    monitoring._verify_cpu_unload(1000, context="idle")
    assert kills == ["cpu"]


def test_verify_kill_escalation_suppressed_with_keep_flag(monkeypatch):
    kills = []
    _fake_m(
        monkeypatch,
        lane_keep_resident=lambda mode: True,
        kill_llama_server=lambda mode: kills.append(mode),
        _data_lock=threading.Lock(),
        _cpu_model_status="chat_loaded",
    )
    monkeypatch.setattr(monitoring, "_free_ram_mb", lambda: 1500)
    _fast_clock(monkeypatch)
    monitoring._verify_cpu_unload(1000, context="idle")
    assert kills == []


# ── external guardrail: no local lifecycle ─────────────────────────────────

def test_kill_guardrail_noop_when_external(monkeypatch):
    _fake_m(monkeypatch, GUARDRAIL_EXTERNAL=True)
    # Would raise AttributeError on subprocess if it got that far; the
    # guard returns before any process call.
    monitoring.kill_llama_server("guardrail")


def test_restart_guardrail_noop_when_external(monkeypatch):
    _fake_m(monkeypatch, GUARDRAIL_EXTERNAL=True)
    monitoring.restart_llama_server("guardrail")


def test_ensure_llama_server_guardrail_noop_when_external(monkeypatch):
    _fake_m(monkeypatch, GUARDRAIL_EXTERNAL=True)
    monitoring.ensure_llama_server("guardrail")


def test_ensure_guardrail_ready_external_pings_only(monkeypatch):
    _fake_m(monkeypatch, GUARDRAIL_EXTERNAL=True)
    monkeypatch.setattr(monitoring, "_external_judge_ping", lambda: True)
    assert monitoring.ensure_guardrail_ready() is True


def test_ensure_guardrail_ready_external_unreachable(monkeypatch):
    _fake_m(monkeypatch, GUARDRAIL_EXTERNAL=True)
    monkeypatch.setattr(monitoring, "_external_judge_ping", lambda: False)
    assert monitoring.ensure_guardrail_ready() is False
