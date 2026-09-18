"""External guardrail judges: routing, candidates, payload, ensure bypass."""

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


# ── candidates ─────────────────────────────────────────────────────────────

def test_candidates_external_prefers_remote_tag(monkeypatch):
    monkeypatch.setattr(judge, "_guardrail_external", lambda: True)
    monkeypatch.setenv("GUARD_LLM_MODEL", "gemma3:2b")

    def fake_get(url, timeout=None):
        assert url.endswith("/v1/models")
        return _Resp(200, {"data": [{"id": "gemma3:2b"}, {"id": "qwen3:1.7b"}]})

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    out = judge._judge_candidates("http://tablet:11434", forced="gemma-4-E2B-it-Q4_K_M")
    assert out[0] == "gemma3:2b"
    assert "gemma-4-E2B-it-Q4_K_M" in out  # pin still tried, but second
    # Local GGUF chat id must never be sent to the remote endpoint.
    assert judge._chat_model_id() not in out


def test_candidates_local_order_unchanged(monkeypatch):
    monkeypatch.setattr(judge, "_guardrail_external", lambda: False)
    monkeypatch.delenv("GUARD_LLM_MODEL", raising=False)

    def fake_get(url, timeout=None):
        return _Resp(200, {"data": [{"id": "a"}, {"id": "b"}]})

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    out = judge._judge_candidates("http://localhost:8083", forced="pinned")
    assert out[0] == "pinned"


# ── payload ────────────────────────────────────────────────────────────────

def _run_post(monkeypatch, external, content="SAFE"):
    monkeypatch.setattr(judge, "_guardrail_external", lambda: external)
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        seen["payload"] = json
        return _Resp(
            200, {"choices": [{"message": {"content": content}}]}
        )

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    cand, text = judge._judge_post_loop(
        "test", "sys", "some user text", "http://x:11434", 90,
        2000, None, ["m"], "cache-key", None,
    )
    return cand, text, seen


def test_payload_external_is_strict_openai(monkeypatch):
    cand, text, seen = _run_post(monkeypatch, True)
    assert cand == "m"
    assert text == "SAFE"
    assert seen["url"] == "http://x:11434/v1/chat/completions"
    assert "reasoning_budget_tokens" not in seen["payload"]
    assert "cache_prompt" not in seen["payload"]
    assert seen["payload"]["stream"] is False


def test_payload_local_keeps_llama_extras(monkeypatch):
    _, _, seen = _run_post(monkeypatch, False)
    assert "reasoning_budget_tokens" in seen["payload"]
    assert seen["payload"]["cache_prompt"] is False


# ── ensure bypass ──────────────────────────────────────────────────────────

def test_ensure_judge_ready_skips_non_guardrail_base(monkeypatch):
    def _boom(model_id=None):
        raise AssertionError("local ensure must not run for remote base")

    import server.features.monitoring as monitoring

    monkeypatch.setattr(monitoring, "ensure_guardrail_ready", _boom)
    # Base mismatch vs LLAMA_BASE_GUARDRAIL (localhost:8083) → early return.
    assert judge.ensure_judge_ready("http://tablet:11434") is None


# ── verdict parsing is backend-agnostic ────────────────────────────────────

def test_parse_verdict_keywords():
    assert judge._parse_verdict("HARMFUL: instructions for wrongdoing") is True
    assert judge._parse_verdict("SAFE: benign chit-chat") is False
    assert judge._parse_verdict("") is False


# ── render hold ────────────────────────────────────────────────────────────

def _register_entrypoint(monkeypatch, **attrs):
    from server.features import state as _state

    fake_ep = types.SimpleNamespace(**attrs)
    monkeypatch.setattr(_state._Registry, "entrypoint", fake_ep)
    return fake_ep


def test_render_hold_skipped_when_external(monkeypatch):
    _register_entrypoint(monkeypatch, GUARDRAIL_EXTERNAL=True, _image_active=True)

    def _boom(secs):
        raise AssertionError("must not sleep while external")

    import time as _time_mod

    monkeypatch.setattr(_time_mod, "sleep", _boom)
    assert judge.wait_until_render_safe(label="t") is True


def test_render_hold_kept_when_local_idle(monkeypatch):
    _register_entrypoint(monkeypatch, GUARDRAIL_EXTERNAL=False, _image_active=False)
    assert judge.wait_until_render_safe(label="t") is True


def test_render_hold_waits_when_local_render_active(monkeypatch):
    _register_entrypoint(monkeypatch, GUARDRAIL_EXTERNAL=False, _image_active=True)
    # Short timeout: holds briefly, then gives up (callers proceed anyway).
    assert judge.wait_until_render_safe(timeout=1, cooldown=0, label="t") is False


# ── judge_endpoint ─────────────────────────────────────────────────────────
def test_judge_endpoint_local_passthrough(monkeypatch):
    monkeypatch.setattr(judge, "_guardrail_external", lambda: False)
    assert judge.judge_endpoint(default_model="gguf-name") == (
        None, None, "gguf-name",
    )


def test_judge_endpoint_remote_prefers_env_tag(monkeypatch):
    monkeypatch.setattr(judge, "_guardrail_external", lambda: True)
    monkeypatch.setattr("server.config.GUARD_LLM_BASE", "http://tablet:11434")
    monkeypatch.setenv("GUARD_LLM_MODEL", "gemma3:2b")
    assert judge.judge_endpoint(default_model="gguf-name") == (
        "http://tablet:11434",
        "http://tablet:11434/v1/chat/completions",
        "gemma3:2b",
    )


def test_judge_endpoint_remote_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(judge, "_guardrail_external", lambda: True)
    monkeypatch.setattr("server.config.GUARD_LLM_BASE", "http://tablet:11434")
    monkeypatch.delenv("GUARD_LLM_MODEL", raising=False)
    assert judge.judge_endpoint(default_model="gguf-name") == (
        "http://tablet:11434",
        "http://tablet:11434/v1/chat/completions",
        "gguf-name",
    )


# ── remote "user-set judge, else default" ──────────────────────────────────

def _listed_models(monkeypatch, *ids):
    def fake_get(url, timeout=None):
        return _Resp(200, {"data": [{"id": mid} for mid in ids]})

    import requests

    monkeypatch.setattr(requests, "get", fake_get)


def test_candidates_remote_user_pin_first(monkeypatch):
    # kaya-style row holding an Ollama tag: the pin answers first try.
    monkeypatch.setattr(judge, "_guardrail_external", lambda: True)
    monkeypatch.setenv("GUARD_LLM_MODEL", "gemma3:2b")
    _listed_models(monkeypatch, "gemma3:2b", "qwen3:4b")
    out = judge._judge_candidates("http://tablet:11434", forced="qwen3:4b")
    assert out[0] == "qwen3:4b"
    assert out[1] == "gemma3:2b"


def test_candidates_remote_default_skips_gguf_probe(monkeypatch):
    # No user row: the pin IS the local default, so the shared remote tag
    # leads and no 404 is burned on the GGUF id.
    monkeypatch.setattr(judge, "_guardrail_external", lambda: True)
    monkeypatch.setenv("GUARD_LLM_MODEL", "gemma3:2b")
    _listed_models(monkeypatch, "gemma3:2b")
    out = judge._judge_candidates(
        "http://tablet:11434", forced="gemma-4-E2B-it-Q4_K_M"
    )
    assert out[0] == "gemma3:2b"


def test_candidates_remote_warns_without_env_tag(monkeypatch, capsys):
    monkeypatch.setattr(judge, "_guardrail_external", lambda: True)
    monkeypatch.delenv("GUARD_LLM_MODEL", raising=False)
    monkeypatch.setattr(judge, "_warned_no_remote_tag", False)
    _listed_models(monkeypatch, "gemma3:2b")
    out = judge._judge_candidates("http://tablet:11434", forced="qwen3:4b")
    assert out[0] == "qwen3:4b"
    assert "GUARD_LLM_MODEL is empty" in capsys.readouterr().out


def test_post_loop_falls_through_stale_pin(monkeypatch):
    # Stale GGUF pin 404s on Ollama, shared tag answers: stale rows degrade,
    # they don't block.
    monkeypatch.setattr(judge, "_guardrail_external", lambda: True)
    seen = []

    def fake_post(url, json=None, timeout=None):
        seen.append(json["model"])
        if json["model"] == "gemma4-e2b-q4":
            return _Resp(404, {}, text="no such model")
        return _Resp(200, {"choices": [{"message": {"content": "SAFE"}}]})

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    cand, text = judge._judge_post_loop(
        "test", "sys", "some user text", "http://x:11434", 90,
        2000, None, ["gemma4-e2b-q4", "gemma3:2b"], "cache-key", None,
    )
    assert (cand, text) == ("gemma3:2b", "SAFE")
    assert seen == ["gemma4-e2b-q4", "gemma3:2b"]
