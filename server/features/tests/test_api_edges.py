"""Batch-3 edge paths: register-agent / leaving / logout handler routes.

Drives server.api.Handler directly with stub streams (no socket): proves the
edge routes behave without a live stack. Module globals that are normally
wired by the chat-webui entrypoint are stubbed per-test.
"""

import io
import json
import threading

import pytest

import server.api as api


def _handler(path, body=None):
    h = api.Handler.__new__(api.Handler)
    raw = json.dumps(body or {}).encode()
    h.command = "POST"
    h.path = path
    h.requestline = f"POST {path} HTTP/1.1"
    h.request_version = "HTTP/1.1"
    # Handlers size the body read by Content-Length — without it they see {}.
    h.headers = {"Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    h.wfile = io.BytesIO()
    h._headers_buffer = []
    h.log_request = lambda *a, **k: None
    h.log_message = lambda *a, **k: None
    return h


def _responded(h):
    raw = h.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, json.loads(body.decode() or "{}")


@pytest.fixture()
def wired(monkeypatch):
    monkeypatch.setattr(api, "_agent_tokens", set())
    monkeypatch.setattr(api, "_agent_users", set())
    monkeypatch.setattr(api, "_agent_token_by_user", {})
    monkeypatch.setattr(api, "_user_last_seen", {"palash": 1.0})
    monkeypatch.setattr(api, "_tokens_lock", threading.Lock())
    monkeypatch.setattr(api, "identity_from_headers", lambda headers: None)
    yield


def test_register_agent_records_tokens_and_users(wired):
    h = _handler("/api/register-agent",
                 {"tokens": ["tok1"], "usernames": ["agent1"]})
    h.do_POST()
    status, data = _responded(h)
    assert status == 200 and data == {"ok": True}
    assert "tok1" in api._agent_tokens
    assert "agent1" in api._agent_users
    assert api._agent_token_by_user["agent1"] == "tok1"


def test_register_agent_empty_body_ok(wired):
    h = _handler("/api/register-agent", {})
    h.do_POST()
    assert _responded(h)[0] == 200


def test_leaving_clears_heartbeat_by_body_username(wired):
    h = _handler("/api/leaving", {"username": "palash"})
    h.do_POST()
    status, data = _responded(h)
    assert status == 200 and data == {"ok": True}
    assert "palash" not in api._user_last_seen


def test_leaving_falls_back_to_header_identity(wired, monkeypatch):
    monkeypatch.setattr(api, "identity_from_headers",
                        lambda headers: {"username": "palash"})
    h = _handler("/api/leaving", {})
    h.do_POST()
    assert _responded(h)[0] == 200
    assert "palash" not in api._user_last_seen


def test_leaving_unknown_user_still_ok(wired):
    h = _handler("/api/leaving", {"username": "ghost"})
    h.do_POST()
    assert _responded(h) == (200, {"ok": True})


def test_logout_always_ok(wired):
    h = _handler("/api/logout", {})
    h.do_POST()
    assert _responded(h) == (200, {"ok": True})
