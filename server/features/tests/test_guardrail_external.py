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
