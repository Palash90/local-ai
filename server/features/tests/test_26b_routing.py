"""26B serving identity: resolvers route override tasks to :8089.

Covers server_base/url/model_id/status/last_use for both the "26b" mode
string and the task-level model override, plus _lane_model_status.
"""

import threading
import types

import server.features.llm as llm


def _fake_m(monkeypatch):
    fake_m = types.SimpleNamespace(
        LLAMA_BASE="http://localhost:8081",
        LLAMA_URL="http://localhost:8081/v1/chat/completions",
        LLAMA_BASE_CPU="http://localhost:8079",
        LLAMA_URL_CPU="http://localhost:8079/v1/chat/completions",
        LLAMA_BASE_GUARDRAIL="http://localhost:8083",
        LLAMA_URL_GUARDRAIL="http://localhost:8083/v1/chat/completions",
        LLAMA_BASE_EMBED="http://localhost:8084",
        LLAMA_BASE_26B="http://localhost:8089",
        LLAMA_URL_26B="http://localhost:8089/v1/chat/completions",
        MODEL_ID="gemma4-e4b-q4",
        MODEL_ID_CPU="gemma4-e4b-q4",
        MODEL_ID_GUARDRAIL="gemma-4-E2B-it-Q4_K_M",
        MODEL_ID_OPENAI="gemma4-26b",
        _data_lock=threading.Lock(),
        model_status="chat_loaded",
        _cpu_model_status="unloaded",
        _guardrail_model_status="unloaded",
        _26b_model_status="unloaded",
        _last_llm_use=100.0,
        _cpu_last_llm_use=200.0,
        _guardrail_last_llm_use=300.0,
        _26b_last_llm_use=400.0,
    )
    monkeypatch.setattr(llm, "M", fake_m)
    return fake_m


def test_base_url_routing(monkeypatch):
    _fake_m(monkeypatch)
    assert llm.server_base("gpu") == "http://localhost:8081"
    assert llm.server_base("cpu") == "http://localhost:8079"
    # Task-level override selects the 26B server regardless of lane.
    assert llm.server_base("gpu", "gemma4-26b") == "http://localhost:8089"
    assert llm.server_base("cpu", "gemma4-26b") == "http://localhost:8089"
    # Serving identity works as a bare mode string too.
    assert llm.server_base("26b") == "http://localhost:8089"
    assert llm.server_url("gpu", "gemma4-26b") == (
        "http://localhost:8089/v1/chat/completions"
    )
    assert llm.server_url("26b") == "http://localhost:8089/v1/chat/completions"
    assert llm.server_url("gpu") == "http://localhost:8081/v1/chat/completions"


def test_model_id_routing(monkeypatch):
    _fake_m(monkeypatch)
    assert llm.server_model_id("gpu") == "gemma4-e4b-q4"
    assert llm.server_model_id("gpu", "gemma4-26b") == "gemma4-26b"
    assert llm.server_model_id("26b") == "gemma4-26b"


def test_status_clocks_routing(monkeypatch):
    _fake_m(monkeypatch)
    assert llm.server_status("gpu") == "chat_loaded"
    assert llm.server_status("gpu", "gemma4-26b") == "unloaded"
    assert llm.server_status("26b") == "unloaded"
    assert llm.server_last_use("gpu", "gemma4-26b") == 400.0
    assert llm.server_last_use("26b") == 400.0
    assert llm.server_last_use("cpu") == 200.0
    assert llm._lane_model_status("26b") == "unloaded"
    assert llm._lane_model_status("gpu") == "chat_loaded"


def _resp(payload, code=200):
    class R:
        status_code = code

        def json(self):
            return payload

    return R()


def test_is_model_ready_single_model_shape(monkeypatch):
    import types as _t

    fake_req = _t.SimpleNamespace(
        get=lambda *a, **k: _resp(
            {"models": [{"name": "gemma4-26b", "model": "gemma4-26b"}]}
        ),
    )
    monkeypatch.setattr(llm, "requests", fake_req)
    assert llm.is_model_ready("http://x:8089", "gemma4-26b") is True
    assert llm.is_model_ready("http://x:8089", "gemma4-e4b-q4") is False


def test_is_model_ready_router_shape(monkeypatch):
    import types as _t

    def fake_get(url, **k):
        if "up" in url:
            return _resp({"data": [{"id": "gemma4-e4b-q4",
                                    "status": {"value": "loaded"}}]})
        return _resp({"data": [{"id": "gemma4-e4b-q4",
                                "status": {"value": "unloaded"}}]})

    fake_req = _t.SimpleNamespace(get=fake_get)
    monkeypatch.setattr(llm, "requests", fake_req)
    assert llm.is_model_ready("http://up:8081", "gemma4-e4b-q4") is True
    assert llm.is_model_ready("http://down:8081", "gemma4-e4b-q4") is False


def test_is_model_ready_statusless_data_shape(monkeypatch):
    # Regression: single-model :8089 lists the model under "data" (with id
    # AND "models") but with NO status object. The old code matched the
    # "data" entry first and returned False forever while serving.
    import types as _t

    body = {
        "models": [{"name": "gemma4-26b", "model": "gemma4-26b"}],
        "object": "list",
        "data": [{"id": "gemma4-26b", "aliases": ["gemma4-26b"],
                  "object": "model", "owned_by": "llamacpp"}],
    }
    fake_req = _t.SimpleNamespace(get=lambda *a, **k: _resp(body))
    monkeypatch.setattr(llm, "requests", fake_req)
    assert llm.is_model_ready("http://x:8089", "gemma4-26b") is True
    assert llm.is_model_ready("http://x:8089", "gemma4-e4b-q4") is False
