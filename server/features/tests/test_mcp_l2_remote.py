"""MCP L2 verify is pinned to the guardrail lane — never remote, never CPU."""

import asyncio
import json
import types

import httpx

import server.mcp_gateway as mcp_gateway
import server.features.monitoring as monitoring


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def _register_entrypoint(monkeypatch, **attrs):
    from server.features import state as _state
    fake_ep = types.SimpleNamespace(**attrs)
    monkeypatch.setattr(_state._Registry, "entrypoint", fake_ep)
    return fake_ep


def _guardrail_ep(**over):
    attrs = {
        "_image_active": False,
        "server_model_id": lambda mode: "gemma-4-E2B-it-Q4_K_M" if mode == "guardrail" else "other",
        "server_base": lambda mode: "http://localhost:8083" if mode == "guardrail" else "http://localhost:8079",
        "server_url": lambda mode: "http://localhost:8083/v1/chat/completions" if mode == "guardrail" else "http://localhost:8079/v1/chat/completions",
    }
    attrs.update(over)
    return attrs


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


def test_l2_ignores_remote_when_external(monkeypatch):
    _register_entrypoint(monkeypatch, GUARDRAIL_EXTERNAL=True, **_guardrail_ep())
    monkeypatch.setattr("server.config.GUARD_LLM_BASE", "http://127.0.0.1:9")
    monkeypatch.setenv("GUARD_LLM_MODEL", "test-tag")
    seen = {}
    _fake_http(monkeypatch, seen)

    passed, reason = asyncio.run(
        mcp_gateway._run_llm_verify("hello", "judge-system")
    )
    assert passed is True, reason
    assert seen["url"] == "http://localhost:8083/v1/chat/completions"
    assert "127.0.0.1:9" not in seen["url"]
    assert "8079" not in seen["url"]
    assert seen["payload"]["model"] == "gemma-4-E2B-it-Q4_K_M"


def test_l2_uses_guardrail_lane_by_default(monkeypatch):
    _register_entrypoint(monkeypatch, GUARDRAIL_EXTERNAL=False, **_guardrail_ep())
    monkeypatch.delenv("GUARD_LLM_MODEL", raising=False)
    seen = {}
    _fake_http(monkeypatch, seen)

    passed, reason = asyncio.run(
        mcp_gateway._run_llm_verify("hello", "judge-system")
    )
    assert passed is True, reason
    assert seen["url"] == "http://localhost:8083/v1/chat/completions"
    assert seen["payload"]["model"] == "gemma-4-E2B-it-Q4_K_M"


def test_l2_guardrail_failure_is_fail_closed(monkeypatch):
    _register_entrypoint(monkeypatch, GUARDRAIL_EXTERNAL=False, **_guardrail_ep())
    monkeypatch.setattr(
        monitoring, "ensure_guardrail_ready", lambda model_id=None: True
    )

    class _DeadClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, timeout=None):
            raise ConnectionError("guardrail down")

    monkeypatch.setattr(httpx, "AsyncClient", _DeadClient)

    passed, reason = asyncio.run(
        mcp_gateway._run_llm_verify("hello", "judge-system")
    )
    assert passed is False
    assert "unavailable" in reason


def test_l2_overall_deadline_is_fail_closed(monkeypatch):
    import asyncio as _asyncio
    _register_entrypoint(monkeypatch, GUARDRAIL_EXTERNAL=False, **_guardrail_ep())
    monkeypatch.setattr(
        monitoring, "ensure_guardrail_ready", lambda model_id=None: True
    )

    class _HungClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, timeout=None):
            await _asyncio.sleep(300)
            raise AssertionError("must never get here")

    monkeypatch.setattr(httpx, "AsyncClient", _HungClient)
    async def _bounded():
        return await _asyncio.wait_for(
            mcp_gateway._run_llm_verify_inner("hello", "judge-system"),
            timeout=0.2,
        )
    import pytest as _pytest
    with _pytest.raises(_asyncio.TimeoutError):
        _asyncio.run(_bounded())


def test_l2_wrapper_timeout_is_fail_closed(monkeypatch):
    import asyncio as _asyncio
    _register_entrypoint(monkeypatch, GUARDRAIL_EXTERNAL=False, **_guardrail_ep())

    async def _hung_inner(*a, **k):
        await _asyncio.sleep(300)
        return True, ""

    monkeypatch.setattr(mcp_gateway, "_run_llm_verify_inner", _hung_inner)
    real_wait_for = _asyncio.wait_for

    async def _short_wait_for(coro, timeout=None):
        return await real_wait_for(coro, timeout=0.2)

    monkeypatch.setattr(_asyncio, "wait_for", _short_wait_for)
    passed, reason = _asyncio.run(
        mcp_gateway._run_llm_verify("hello", "judge-system"))
    assert passed is False
    assert "timed out" in reason
