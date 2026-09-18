"""MCP L2 verify routes via the remote judge endpoint when external."""

import asyncio
import json
import types

import httpx

import server.mcp_gateway as mcp_gateway
import server.features.monitoring as monitoring
from server.features import state as _state


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def _register_entrypoint(monkeypatch, **attrs):
    fake_ep = types.SimpleNamespace(**attrs)
    monkeypatch.setattr(_state._Registry, "entrypoint", fake_ep)
    return fake_ep


def _fake_http(monkeypatch, seen, content="SAFE: benign"):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, timeout=None):
            seen["url"] = url
            seen["payload"] = json
            return _Resp(200, {"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        monitoring, "ensure_guardrail_ready", lambda model_id=None: True
    )


def test_l2_routes_remote_when_external(monkeypatch):
    _register_entrypoint(
        monkeypatch,
        GUARDRAIL_EXTERNAL=True,
        _image_active=False,
        server_model_id=lambda mode: "gguf-judge-name",
        server_base=lambda mode: "http://localhost:8083",
        server_url=lambda mode: "http://localhost:8083/v1/chat/completions",
    )
    monkeypatch.setattr("server.config.GUARD_LLM_BASE", "http://127.0.0.1:9")
    monkeypatch.setenv("GUARD_LLM_MODEL", "test-tag")
    seen = {}
    _fake_http(monkeypatch, seen)

    passed, reason = asyncio.run(
        mcp_gateway._run_llm_verify("hello", "judge-system")
    )
    assert passed is True, reason
    assert seen["url"] == "http://127.0.0.1:9/v1/chat/completions"
    assert seen["payload"]["model"] == "test-tag"


def test_l2_stays_local_by_default(monkeypatch):
    _register_entrypoint(
        monkeypatch,
        GUARDRAIL_EXTERNAL=False,
        _image_active=False,
        server_model_id=lambda mode: "gguf-judge-name",
        server_base=lambda mode: "http://localhost:8083",
        server_url=lambda mode: "http://localhost:8083/v1/chat/completions",
    )
    monkeypatch.delenv("GUARD_LLM_MODEL", raising=False)
    seen = {}
    _fake_http(monkeypatch, seen)

    passed, reason = asyncio.run(
        mcp_gateway._run_llm_verify("hello", "judge-system")
    )
    assert passed is True, reason
    assert seen["url"] == "http://localhost:8083/v1/chat/completions"
    assert seen["payload"]["model"] == "gguf-judge-name"


def test_l2_remote_failure_is_fail_closed(monkeypatch):
    _register_entrypoint(
        monkeypatch,
        GUARDRAIL_EXTERNAL=True,
        _image_active=False,
        server_model_id=lambda mode: "gguf-judge-name",
        server_base=lambda mode: "http://localhost:8083",
        server_url=lambda mode: "http://localhost:8083/v1/chat/completions",
    )
    monkeypatch.setattr("server.config.GUARD_LLM_BASE", "http://127.0.0.1:9")
    monkeypatch.setenv("GUARD_LLM_MODEL", "test-tag")
    monkeypatch.setattr(
        monitoring, "ensure_guardrail_ready", lambda model_id=None: True
    )

    class _DeadClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, timeout=None):
            raise ConnectionError("tablet down")

    monkeypatch.setattr(httpx, "AsyncClient", _DeadClient)

    passed, reason = asyncio.run(
        mcp_gateway._run_llm_verify("hello", "judge-system")
    )
    assert passed is False
    assert "unavailable" in reason
