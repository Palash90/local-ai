import os
import sys
import threading
import time
import types

import pytest


@pytest.fixture
def api_env(handler, make_user, chat_webui, monkeypatch, tmp_path):
    """Standard auth'd API environment with two users and a scratch upload dir."""
    import server.api as api

    monkeypatch.setattr(api, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(api, "IMG_PATH", str(tmp_path / "output"))
    monkeypatch.setattr(api, "COMFYUI_OUTPUT", str(tmp_path / "comfy_output"))
    os.makedirs(api.UPLOADS_DIR, exist_ok=True)
    os.makedirs(api.IMG_PATH, exist_ok=True)

    make_user(
        {"alice": "secret", "bob": "secret"},
        context_files={"alice": str(tmp_path / "ctx" / "alice.txt")},
    )
    chat_webui.ACTIVE_WINDOW_SECONDS = 120

    def login(username, password="secret"):
        r = handler("/api/login", method="POST", data={"username": username, "password": password})
        assert r.status == 200, r.json
        return r.json["token"]

    token_a = login("alice")
    token_b = login("bob")
    auth_a = {"X-Auth-Token": token_a}
    auth_b = {"X-Auth-Token": token_b}
    return {
        "handler": handler,
        "chat": chat_webui,
        "api": api,
        "token_a": token_a,
        "token_b": token_b,
        "auth_a": auth_a,
        "auth_b": auth_b,
        "tmp_path": tmp_path,
    }


def _create_session(env, auth):
    r = env["handler"]("/api/sessions", method="POST", data={}, headers=auth)
    assert r.status == 200
    return r.json["session_id"]


class TestOptions:
    def test_cors_headers(self, api_env):
        r = api_env["handler"]("/api/login", method="OPTIONS")
        assert r.status == 200
        assert ("Access-Control-Allow-Origin", "*") in r.sent_headers
        assert any(k == "Access-Control-Allow-Methods" for k, _ in r.sent_headers)


class TestCheckAuth:
    def test_no_token(self, api_env):
        r = api_env["handler"]("/api/check-auth")
        assert r.status == 200
        assert r.json == {"authenticated": False}

    def test_valid_token(self, api_env):
        r = api_env["handler"]("/api/check-auth", headers=api_env["auth_a"])
        assert r.status == 200
        assert r.json == {"authenticated": True, "username": "alice"}

    def test_bad_token(self, api_env):
        r = api_env["handler"]("/api/check-auth", headers={"X-Auth-Token": "nope"})
        assert r.json == {"authenticated": False}


class TestLoginLogout:
    def test_login_success(self, api_env):
        r = api_env["handler"]("/api/login", method="POST", data={"username": "alice", "password": "secret"})
        assert r.status == 200
        body = r.json
        assert body["token"]
        assert body["username"] == "alice"
        assert body["context_file"].endswith("alice.txt")

    def test_login_wrong_password(self, api_env):
        r = api_env["handler"]("/api/login", method="POST", data={"username": "alice", "password": "wrong"})
        assert r.status == 401
        assert r.json["error"] == "Invalid credentials"

    def test_login_unknown_user(self, api_env):
        r = api_env["handler"]("/api/login", method="POST", data={"username": "ghost", "password": "x"})
        assert r.status == 401

    def test_login_trims_whitespace(self, api_env):
        r = api_env["handler"]("/api/login", method="POST", data={"username": "  alice  ", "password": "  secret  "})
        assert r.status == 200
        assert r.json["username"] == "alice"

    def test_logout_removes_token(self, api_env):
        env = api_env
        auth = env["auth_a"]
        r = env["handler"]("/api/logout", method="POST", headers=auth)
        assert r.status == 200
        assert r.json == {"ok": True}
        r2 = env["handler"]("/api/check-auth", headers=auth)
        assert r2.json == {"authenticated": False}


class TestActiveUsers:
    def _clear(self, env):
        env["chat"]._active_tokens.clear()
        env["chat"]._agent_users.clear()
        env["chat"]._agent_tokens.clear()

    def test_empty(self, api_env):
        env = api_env
        self._clear(env)
        r = env["handler"]("/api/active-users")
        assert r.status == 200
        assert r.json == {"users": []}

    def test_lists_recently_seen_users(self, api_env):
        env = api_env
        self._clear(env)
        env["chat"]._active_tokens["t1"] = {"user": "alice", "last_seen": time.time()}
        r = env["handler"]("/api/active-users")
        assert "alice" in r.json["users"]

    def test_ignores_stale_users(self, api_env):
        env = api_env
        self._clear(env)
        env["chat"]._active_tokens["t1"] = {"user": "alice", "last_seen": time.time() - 10000}
        r = env["handler"]("/api/active-users")
        assert r.json["users"] == []

    def test_excludes_agent_users(self, api_env):
        env = api_env
        self._clear(env)
        env["chat"]._active_tokens["t1"] = {"user": "alice", "last_seen": time.time()}
        env["chat"]._agent_users.add("alice")
        r = env["handler"]("/api/active-users")
        assert r.json["users"] == []

    def test_excludes_agent_tokens(self, api_env):
        env = api_env
        self._clear(env)
        env["chat"]._active_tokens["t1"] = {"user": "alice", "last_seen": time.time()}
        env["chat"]._agent_tokens.add("t1")
        r = env["handler"]("/api/active-users")
        assert r.json["users"] == []

    def test_sorted_and_deduped(self, api_env):
        env = api_env
        self._clear(env)
        now = time.time()
        env["chat"]._active_tokens["t1"] = {"user": "bob", "last_seen": now}
        env["chat"]._active_tokens["t2"] = {"user": "alice", "last_seen": now}
        env["chat"]._active_tokens["t3"] = {"user": "bob", "last_seen": now}
        r = env["handler"]("/api/active-users")
        assert r.json["users"] == ["alice", "bob"]


class TestModelStatus:
    def test_reports_state(self, api_env):
        env = api_env
        env["chat"].model_status = "chat_loaded"
        env["chat"]._last_tps = 12.3
        env["chat"]._overheated = False
        r = env["handler"]("/api/model-status", headers=env["auth_a"])
        assert r.status == 200
        body = r.json
        assert body["model"] == "chat_loaded"
        assert body["predicted_per_second"] == 12.3
        assert body["overheated"] is False
        assert body["max_context"] == env["chat"].MAX_INPUT_TOKENS
        assert body["reminder_count"] == 0


class TestUserContext:
    def test_get_requires_auth(self, api_env):
        r = api_env["handler"]("/api/user-context")
        assert r.status == 401

    def test_get_missing_context_file(self, api_env):
        env = api_env
        r = env["handler"]("/api/user-context", headers=env["auth_a"])
        assert r.status == 200
        assert r.json["context"] == ""
        assert r.json["username"] == "alice"

    def test_write_then_read(self, api_env):
        env = api_env
        r = env["handler"]("/api/user-context", method="POST",
                           data={"action": "write", "context": "Likes cats"},
                           headers=env["auth_a"])
        assert r.status == 200
        assert r.json["status"] == "ok"
        r2 = env["handler"]("/api/user-context", headers=env["auth_a"])
        assert "Likes cats" in r2.json["context"]

    def test_overwrite(self, api_env):
        env = api_env
        env["handler"]("/api/user-context", method="POST",
                       data={"action": "write", "context": "old"},
                       headers=env["auth_a"])
        env["handler"]("/api/user-context", method="POST",
                       data={"action": "overwrite", "context": "new"},
                       headers=env["auth_a"])
        r = env["handler"]("/api/user-context", headers=env["auth_a"])
        assert r.json["context"].strip() == "new"


class TestSessions:
    def test_get_requires_auth(self, api_env):
        r = api_env["handler"]("/api/sessions")
        assert r.status == 401
        assert r.json == []

    def test_create_and_list(self, api_env):
        env = api_env
        sid = _create_session(env, env["auth_a"])
        assert sid
        r = env["handler"]("/api/sessions", headers=env["auth_a"])
        assert r.status == 200
        assert any(s["session_id"] == sid for s in r.json)

    def test_create_with_context_tokens(self, api_env):
        env = api_env
        r = env["handler"]("/api/sessions", method="POST",
                           data={"system_prompts": [{"name": "D", "content": "g is %genre%"}],
                                 "context_tokens": {"%genre%": "adult"}},
                           headers=env["auth_a"])
        assert r.status == 200
        sid = r.json["session_id"]
        meta = env["chat"].sessions_meta[sid]
        assert meta["context_tokens"] == {"%genre%": "adult"}
        assert meta["system_prompts"] == [{"name": "D", "content": "g is %genre%"}]

    def test_sessions_scoped_to_user(self, api_env):
        env = api_env
        _create_session(env, env["auth_a"])
        r = env["handler"]("/api/sessions", headers=env["auth_b"])
        assert all(s["session_id"] not in [x["session_id"] for x in env["handler"]("/api/sessions", headers=env["auth_a"]).json] or True for s in r.json)
        assert len(r.json) == 0

    def test_session_messages_empty(self, api_env):
        env = api_env
        sid = _create_session(env, env["auth_a"])
        r = env["handler"]("/api/sessions/%s/messages" % sid, headers=env["auth_a"])
        assert r.status == 200
        assert r.json["messages"] == []
        assert "token_estimate" in r.json

    def test_session_messages_other_user_404(self, api_env):
        env = api_env
        sid = _create_session(env, env["auth_a"])
        r = env["handler"]("/api/sessions/%s/messages" % sid, headers=env["auth_b"])
        assert r.status == 404

    def test_rename_session(self, api_env):
        env = api_env
        sid = _create_session(env, env["auth_a"])
        r = env["handler"]("/api/sessions/%s" % sid, method="PUT",
                           data={"name": "Renamed"}, headers=env["auth_a"])
        assert r.status == 200
        list_r = env["handler"]("/api/sessions", headers=env["auth_a"])
        assert any(s["session_id"] == sid and s["name"] == "Renamed" for s in list_r.json)

    def test_delete_session(self, api_env):
        env = api_env
        sid = _create_session(env, env["auth_a"])
        r = env["handler"]("/api/sessions/%s" % sid, method="DELETE", headers=env["auth_a"])
        assert r.status == 200
        assert r.json == {"status": "deleted"}
        list_r = env["handler"]("/api/sessions", headers=env["auth_a"])
        assert not any(s["session_id"] == sid for s in list_r.json)

    def test_delete_other_users_session_404(self, api_env):
        env = api_env
        sid = _create_session(env, env["auth_a"])
        r = env["handler"]("/api/sessions/%s" % sid, method="DELETE", headers=env["auth_b"])
        assert r.status == 404


class TestChat:
    def test_requires_auth(self, api_env):
        r = api_env["handler"]("/api/chat", method="POST", data={"message": "hi"})
        assert r.status == 401

    def test_unknown_session(self, api_env):
        env = api_env
        r = env["handler"]("/api/chat", method="POST",
                           data={"message": "hi", "session_id": "missing"},
                           headers=env["auth_a"])
        assert r.status == 404
        assert r.json["error"] == "Session not found"

    def test_enqueues_task(self, api_env):
        env = api_env
        sid = _create_session(env, env["auth_a"])
        r = env["handler"]("/api/chat", method="POST",
                           data={"message": "hello", "session_id": sid},
                           headers=env["auth_a"])
        assert r.status == 200
        task_id = r.json["task_id"]
        assert task_id
        assert any(t["task_id"] == task_id for t in env["chat"]._task_queues["gpu"])
        st = env["handler"]("/api/status/%s" % task_id)
        assert st.json["status"] == "queued"

    def test_queue_full_returns_503(self, api_env):
        env = api_env
        sid = _create_session(env, env["auth_a"])
        for _ in range(env["chat"].MAX_QUEUE_SIZE):
            env["chat"]._task_queues["gpu"].append({"task_id": "filler-%d" % _, "session_id": sid})
        r = env["handler"]("/api/chat", method="POST",
                           data={"message": "hello", "session_id": sid},
                           headers=env["auth_a"])
        assert r.status == 503
        assert r.json["error"] == "Server busy"


class TestExtractFile:
    def test_saves_upload(self, api_env):
        env = api_env
        import base64
        raw = base64.b64encode(b"hello world").decode()
        r = env["handler"]("/api/extract-file", method="POST",
                           data={"name": "notes.txt", "data": raw})
        assert r.status == 200
        body = r.json
        assert body["name"] == "notes.txt"
        assert body["url"].startswith("/uploads/")
        stored = os.path.join(env["api"].UPLOADS_DIR, os.path.basename(body["url"]))
        with open(stored, "rb") as f:
            assert f.read() == b"hello world"

    def test_sanitizes_extension(self, api_env):
        env = api_env
        import base64
        raw = base64.b64encode(b"x").decode()
        r = env["handler"]("/api/extract-file", method="POST",
                           data={"name": "../../evil.png", "data": raw})
        fname = os.path.basename(r.json["url"])
        assert fname == os.path.basename(r.json["url"])
        assert ".." not in fname
        assert fname.endswith(".png")


class TestTasksApi:
    def test_requires_auth(self, api_env):
        r = api_env["handler"]("/api/tasks")
        assert r.status == 401

    def test_create_and_list(self, api_env):
        env = api_env
        r = env["handler"]("/api/tasks", method="POST",
                           data={"title": "Buy milk", "priority": "high"},
                           headers=env["auth_a"])
        assert r.status == 200
        task = r.json["task"]
        assert task["title"] == "Buy milk"
        assert task["priority"] == "high"
        assert task["user_id"] == "alice"
        list_r = env["handler"]("/api/tasks", headers=env["auth_a"])
        assert any(t["id"] == task["id"] for t in list_r.json["tasks"])

    def test_tasks_scoped_to_user(self, api_env):
        env = api_env
        env["handler"]("/api/tasks", method="POST", data={"title": "A"},
                       headers=env["auth_a"])
        r = env["handler"]("/api/tasks", headers=env["auth_b"])
        assert r.json["tasks"] == []

    def test_update_task(self, api_env):
        env = api_env
        task = env["handler"]("/api/tasks", method="POST", data={"title": "A"},
                              headers=env["auth_a"]).json["task"]
        r = env["handler"]("/api/tasks/%s" % task["id"], method="PUT",
                           data={"status": "completed"}, headers=env["auth_a"])
        assert r.status == 200
        assert r.json["task"]["status"] == "completed"

    def test_update_other_users_task_does_not_apply(self, api_env):
        env = api_env
        task = env["handler"]("/api/tasks", method="POST", data={"title": "A"},
                              headers=env["auth_a"]).json["task"]
        r = env["handler"]("/api/tasks/%s" % task["id"], method="PUT",
                           data={"status": "completed"}, headers=env["auth_b"])
        # the stored record must be untouched for the wrong user
        t = env["chat"].task_get(task["id"], "alice")
        assert t["status"] == "pending"

    def test_delete_task(self, api_env):
        env = api_env
        task = env["handler"]("/api/tasks", method="POST", data={"title": "A"},
                              headers=env["auth_a"]).json["task"]
        r = env["handler"]("/api/tasks/%s" % task["id"], method="DELETE",
                           headers=env["auth_a"])
        assert r.status == 200
        list_r = env["handler"]("/api/tasks", headers=env["auth_a"])
        assert list_r.json["tasks"] == []


class TestLocation:
    def test_denied(self, api_env):
        env = api_env
        r = env["handler"]("/api/location", method="POST", data={"denied": True})
        assert r.status == 200
        assert env["chat"].location_str() is None

    def test_sets_location(self, api_env, monkeypatch):
        env = api_env
        import server.api as api

        fake_resp = types.SimpleNamespace(
            json=lambda: {"display_name": "Kolkata, India"}
        )
        monkeypatch.setattr(api.requests, "get", lambda *a, **k: fake_resp)
        r = env["handler"]("/api/location", method="POST",
                           data={"latitude": 22.57, "longitude": 88.36})
        assert r.status == 200
        assert env["chat"].location_str() == "Kolkata, India"

    def test_location_fallback_on_error(self, api_env, monkeypatch):
        env = api_env
        import server.api as api

        def boom(*a, **k):
            raise RuntimeError("offline")
        monkeypatch.setattr(api.requests, "get", boom)
        r = env["handler"]("/api/location", method="POST",
                           data={"latitude": 22.57, "longitude": 88.36})
        assert r.status == 200
        assert "22.5700, 88.3600" in env["chat"].location_str()


class TestTTS:
    def test_requires_auth(self, api_env):
        r = api_env["handler"]("/api/tts", method="POST", data={"text": "hi"})
        assert r.status == 401

    def test_empty_text(self, api_env):
        env = api_env
        r = env["handler"]("/api/tts", method="POST", data={"text": ""}, headers=env["auth_a"])
        assert r.status == 400

    def test_piper_wav(self, api_env, monkeypatch):
        env = api_env

        class FakeArray:
            def __mul__(self, o):
                return self

            def clip(self, lo, hi):
                return self

            def astype(self, dtype):
                return self

            def tobytes(self):
                return b"\x00\x00" * 40

        class FakeVoice:
            @classmethod
            def load(cls, onnx_path, config_path=None):
                return cls()

            def synthesize(self, text):
                yield types.SimpleNamespace(audio_float_array=FakeArray())

        fake_piper = types.SimpleNamespace(PiperVoice=FakeVoice)
        monkeypatch.setitem(sys.modules, "piper", fake_piper)
        r = env["handler"]("/api/tts", method="POST",
                           data={"text": "hello world"}, headers=env["auth_a"])
        assert r.status == 200
        body = r.json
        assert body["type"] == "audio/wav"
        assert body["audio"]

    def test_edge_mp3(self, api_env, monkeypatch):
        env = api_env

        class FakeComm:
            def __init__(self, text, voice):
                self.text = text
                self.voice = voice

            async def stream(self):
                yield {"type": "audio", "data": b"\xff\xf3\x00\x01"}
                yield {"type": "text", "data": "ignored"}

        fake_edge = types.SimpleNamespace(Communicate=FakeComm)
        monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)
        r = env["handler"]("/api/tts", method="POST",
                           data={"text": "[kn] ನಮಸ್ಕಾರ"}, headers=env["auth_a"])
        assert r.status == 200
        body = r.json
        assert body["type"] == "audio/mpeg"
        assert body["audio"]

    def test_error_returns_500(self, api_env, monkeypatch):
        env = api_env

        class FakeVoice:
            @classmethod
            def load(cls, onnx_path, config_path=None):
                raise RuntimeError("voice file missing")

        fake_piper = types.SimpleNamespace(PiperVoice=FakeVoice)
        monkeypatch.setitem(sys.modules, "piper", fake_piper)
        r = env["handler"]("/api/tts", method="POST",
                           data={"text": "[bn] কিছু"}, headers=env["auth_a"])
        assert r.status == 500
        assert "error" in r.json


class TestRegisterAgent:
    def test_registers_tokens_and_users(self, api_env):
        env = api_env
        r = env["handler"]("/api/register-agent", method="POST",
                           data={"tokens": ["t1", "t2"], "usernames": ["kolpo"]})
        assert r.status == 200
        assert {"t1", "t2"} <= env["chat"]._agent_tokens
        assert "kolpo" in env["chat"]._agent_users


class TestServing:
    def test_index_html(self, api_env):
        r = api_env["handler"]("/")
        assert r.status == 200
        assert any(k == "Content-Type" and "text/html" in v for k, v in r.sent_headers)

    def test_spa_fallback_for_unknown_path(self, api_env):
        r = api_env["handler"]("/some/client/route")
        assert r.status == 200
        assert any(k == "Content-Type" and "text/html" in v for k, v in r.sent_headers)

    def test_missing_static_file_404(self, api_env):
        r = api_env["handler"]("/assets/does-not-exist.js")
        assert r.status == 404

    def test_output_image(self, api_env):
        env = api_env
        os.makedirs(env["api"].COMFYUI_OUTPUT, exist_ok=True)
        with open(os.path.join(env["api"].COMFYUI_OUTPUT, "gen.png"), "wb") as f:
            f.write(b"\x89PNG\r\n")
        r = env["handler"]("/output/gen.png")
        assert r.status == 200
        assert ("Content-Type", "image/png") in r.sent_headers
        assert r.wfile.getvalue() == b"\x89PNG\r\n"

    def test_output_missing_404(self, api_env):
        r = api_env["handler"]("/output/nope.png")
        assert r.status == 404

    def test_output_traversal_blocked(self, api_env):
        env = api_env
        r = env["handler"]("/output/../../../etc/passwd")
        assert r.status == 404

    def test_uploads_image(self, api_env):
        env = api_env
        with open(os.path.join(env["api"].UPLOADS_DIR, "doc.pdf"), "wb") as f:
            f.write(b"%PDF")
        r = env["handler"]("/uploads/doc.pdf")
        assert r.status == 200
        assert ("Content-Disposition", "inline") in r.sent_headers

    def test_uploads_traversal_blocked(self, api_env):
        env = api_env
        r = env["handler"]("/uploads/../../etc/passwd")
        assert r.status == 404


class TestIndexMissing:
    def test_missing_index_falls_back(self, api_env, monkeypatch):
        import server.api as api
        monkeypatch.setattr(api, "__file__", "/tmp/nope/package/api.py")
        assert "index.html missing" in api.read_index_html()


class TestModelStatusReminder:
    def test_reminder_db_error_returns_zero(self, api_env, monkeypatch):
        env = api_env
        import server.api as api

        def boom(*a, **k):
            raise RuntimeError("db locked")
        monkeypatch.setattr(api, "_db_fetch", boom)
        r = env["handler"]("/api/model-status", headers=env["auth_a"])
        assert r.status == 200
        assert r.json["reminder_count"] == 0

    def test_reminder_count_for_user(self, api_env):
        env = api_env
        import server.api as api
        task = env["handler"]("/api/tasks", method="POST",
                              data={"title": "R", "reminder_at": "2000-01-01T00:00:00"},
                              headers=env["auth_a"]).json["task"]
        r = env["handler"]("/api/model-status", headers=env["auth_a"])
        assert r.json["reminder_count"] == 1


class TestSessionMessagesUnauthorized:
    def test_messages_requires_auth(self, api_env):
        r = api_env["handler"]("/api/sessions/s1/messages")
        assert r.status == 401

    def test_messages_missing_session_404(self, api_env):
        env = api_env
        sid = _create_session(env, env["auth_a"])
        with env["chat"]._data_lock:
            env["chat"].sessions.pop(sid, None)
        r = env["handler"]("/api/sessions/%s/messages" % sid, headers=env["auth_a"])
        assert r.status == 404


class TestStaticServing:
    def test_serves_dist_asset(self, api_env):
        env = api_env
        import server.api as api
        import os
        dist_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(api.__file__))), "dist")
        os.makedirs(dist_dir, exist_ok=True)
        asset = os.path.join(dist_dir, "test-asset-xyz.js")
        with open(asset, "w") as f:
            f.write("console.log('hi')")
        try:
            r = env["handler"]("/test-asset-xyz.js")
            assert r.status == 200
            assert r.wfile.getvalue() == b"console.log('hi')"
        finally:
            os.remove(asset)


class TestDeleteSessionDeep:
    def test_cancels_inflight_and_removes_files(self, api_env):
        env = api_env
        sid = _create_session(env, env["auth_a"])
        with env["chat"]._data_lock:
            env["chat"].sessions[sid] = [
                {"role": "assistant", "_image_url": "/output/user/gen.png"},
                {"role": "user", "content": "See [FILE: /uploads/doc.pdf] and [FILE: /uploads/other.txt]"},
                {"role": "user", "content": [{"type": "text", "text": "plain [FILE: /uploads/multi.txt]"}]},
            ]
            env["chat"].tasks["t9"] = {"session_id": sid, "status": "working"}
        img_dir = os.path.join(env["api"].IMG_PATH, "user")
        os.makedirs(img_dir, exist_ok=True)
        for name in ["doc.pdf", "other.txt", "multi.txt"]:
            with open(os.path.join(env["api"].UPLOADS_DIR, name), "w") as f:
                f.write("x")
        with open(os.path.join(img_dir, "gen.png"), "w") as f:
            f.write("img")
        with env["chat"]._effective_contexts_lock:
            env["chat"]._effective_contexts[sid] = [{"role": "system", "content": "c"}]
        r = env["handler"]("/api/sessions/%s" % sid, method="DELETE", headers=env["auth_a"])
        assert r.status == 200
        assert env["chat"].tasks["t9"]["status"] == "cancelled"
        assert not os.path.exists(os.path.join(env["api"].UPLOADS_DIR, "doc.pdf"))
        assert not os.path.exists(os.path.join(env["api"].UPLOADS_DIR, "multi.txt"))
        assert not os.path.exists(os.path.join(img_dir, "gen.png"))
        with env["chat"]._effective_contexts_lock:
            assert sid not in env["chat"]._effective_contexts

    def test_delete_missing_session_404(self, api_env):
        env = api_env
        sid = _create_session(env, env["auth_a"])
        with env["chat"]._data_lock:
            env["chat"].sessions.pop(sid, None)
        r = env["handler"]("/api/sessions/%s" % sid, method="DELETE", headers=env["auth_a"])
        assert r.status == 404


class TestDeleteUnauthorized:
    def test_delete_session_requires_auth(self, api_env):
        r = api_env["handler"]("/api/sessions/s1", method="DELETE")
        assert r.status == 401

    def test_delete_task_requires_auth(self, api_env):
        r = api_env["handler"]("/api/tasks/t1", method="DELETE")
        assert r.status == 401

    def test_delete_task_missing_404(self, api_env):
        env = api_env
        r = env["handler"]("/api/tasks/nope", method="DELETE", headers=env["auth_a"])
        assert r.status == 404

    def test_delete_unknown_path_404(self, api_env):
        r = api_env["handler"]("/api/nope", method="DELETE")
        assert r.status == 404


class TestPutUnauthorized:
    def test_put_session_requires_auth(self, api_env):
        r = api_env["handler"]("/api/sessions/s1", method="PUT", data={"name": "x"})
        assert r.status == 401

    def test_put_session_missing_404(self, api_env):
        env = api_env
        r = env["handler"]("/api/sessions/nope", method="PUT", data={"name": "x"}, headers=env["auth_a"])
        assert r.status == 404

    def test_put_task_requires_auth(self, api_env):
        r = api_env["handler"]("/api/tasks/t1", method="PUT", data={"title": "x"})
        assert r.status == 401

    def test_put_task_missing_404(self, api_env):
        env = api_env
        r = env["handler"]("/api/tasks/nope", method="PUT", data={"title": "x"}, headers=env["auth_a"])
        assert r.status == 404

    def test_put_unknown_path_404(self, api_env):
        r = api_env["handler"]("/api/nope", method="PUT", data={})
        assert r.status == 404


class TestPostUserContext:
    def test_write_requires_auth(self, api_env):
        r = api_env["handler"]("/api/user-context", method="POST", data={"action": "write", "context": "x"})
        assert r.status == 401

    def test_read_action_default(self, api_env):
        env = api_env
        r = env["handler"]("/api/user-context", method="POST", data={"action": "other"}, headers=env["auth_a"])
        assert r.status == 200
        assert r.json["context"] == ""
        assert r.json["username"] == "alice"


class TestTTSVoiceBranch:
    def test_voice_present_uses_en(self, api_env, monkeypatch):
        import sys
        import types
        env = api_env

        class FakeArray:
            def __mul__(self, o):
                return self

            def clip(self, lo, hi):
                return self

            def astype(self, dtype):
                return self

            def tobytes(self):
                return b"\x00\x00" * 40

        class FakeVoice:
            @classmethod
            def load(cls, onnx_path, config_path=None):
                return cls()

            def synthesize(self, text):
                yield types.SimpleNamespace(audio_float_array=FakeArray())

        monkeypatch.setitem(sys.modules, "piper", types.SimpleNamespace(PiperVoice=FakeVoice))
        r = env["handler"]("/api/tts", method="POST",
                           data={"text": "hello there", "voice": "bn-IN-TanishaaNeural"},
                           headers=env["auth_a"])
        assert r.status == 200
        assert r.json["type"] == "audio/wav"


class TestPostSessions:
    def test_create_requires_auth(self, api_env):
        r = api_env["handler"]("/api/sessions", method="POST", data={})
        assert r.status == 401

    def test_create_with_bad_json_body(self, api_env):
        env = api_env
        r = env["handler"]("/api/sessions", method="POST", body=b"{not json", headers=env["auth_a"])
        assert r.status == 200
        assert r.json["session_id"]


class TestLocationEvents:
    def test_denied_sets_event(self, api_env):
        env = api_env
        ev = threading.Event()
        with env["chat"]._data_lock:
            env["chat"]._location_events["t1"] = ev
        r = env["handler"]("/api/location", method="POST", data={"denied": True, "task_id": "t1"})
        assert r.status == 200
        assert ev.is_set()

    def test_allow_sets_event(self, api_env, monkeypatch):
        env = api_env
        import server.api as api
        ev = threading.Event()
        with env["chat"]._data_lock:
            env["chat"]._location_events["t2"] = ev
        fake_resp = types.SimpleNamespace(json=lambda: {"display_name": "Dhaka"})
        monkeypatch.setattr(api.requests, "get", lambda *a, **k: fake_resp)
        r = env["handler"]("/api/location", method="POST",
                           data={"latitude": 23.8, "longitude": 90.4, "task_id": "t2"})
        assert r.status == 200
        assert ev.is_set()


class TestPostFallback:
    def test_unknown_post_404(self, api_env):
        r = api_env["handler"]("/api/does-not-exist", method="POST", data={})
        assert r.status == 404

    def test_post_tasks_requires_auth(self, api_env):
        r = api_env["handler"]("/api/tasks", method="POST", data={"title": "x"})
        assert r.status == 401


class TestLogMessage:
    def test_log_message_noop(self, api_env):
        import server.api
        h = server.api.Handler.__new__(server.api.Handler)
        h.log_message("x")



def test_debug_env(api_env):
    import os
    env = api_env
    print("USERS_FILE:", env["chat"].USERS_FILE)
    print("users cache:", env["chat"]._users_cache)
    print("cw.get_user_password:", repr(env["chat"].get_user_password("alice")))
    print("api.get_user_password:", repr(env["api"].get_user_password("alice")))
    with open(env["chat"].USERS_FILE) as f:
        print("users.json:", f.read())
