import base64
import builtins
import io
import json
import os
import threading
import time

import pytest


class TestSafeUsername:
    def test_normal(self, chat_webui):
        assert chat_webui._safe_username("alice") == "alice"

    def test_sanitizes(self, chat_webui):
        assert chat_webui._safe_username("a b/c") == "a_b_c"

    def test_empty_becomes_unknown(self, chat_webui):
        assert chat_webui._safe_username("") == "unknown"
        assert chat_webui._safe_username(None) == "unknown"

    def test_unicode(self, chat_webui):
        assert chat_webui._safe_username("আমি") == "___"


class TestTextTokens:
    def test_empty(self, chat_webui):
        assert chat_webui._text_tokens("") == 0
        assert chat_webui._text_tokens(None) == 0

    def test_ascii(self, chat_webui):
        assert chat_webui._text_tokens("hello world") == 2  # 11 chars / 4

    def test_non_ascii_dominant(self, chat_webui):
        # >15% non-ASCII -> divisor 2
        text = "বাংলা বাংলা বাংলা বাংলা"
        assert chat_webui._text_tokens(text) == int(len(text) / 2)


class TestEstimateTokens:
    def test_empty_messages_without_tools(self, chat_webui):
        assert chat_webui.estimate_tokens([], include_tools=False) == 1

    def test_with_tools_cost(self, chat_webui):
        assert chat_webui.estimate_tokens([], include_tools=True) == chat_webui.TOOLS_TOKEN_COST

    def test_text_message(self, chat_webui):
        msgs = [{"role": "user", "content": "hello"}]
        assert chat_webui.estimate_tokens(msgs) > chat_webui.PER_MESSAGE_OVERHEAD

    def test_multimodal_content(self, chat_webui):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,xx"}},
            {"type": "audio_url", "audio_url": {"url": "data:audio/webm;base64,xx"}},
        ]}]
        total = chat_webui.estimate_tokens(msgs, include_tools=False)
        assert total >= chat_webui.IMAGE_TOKEN_COST + chat_webui.AUDIO_TOKEN_COST + chat_webui.PER_MESSAGE_OVERHEAD

    def test_tool_calls_counted(self, chat_webui):
        msgs = [{"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "web_search", "arguments": "{\"query\":\"x\"}"}}
        ]}]
        with_tools = chat_webui.estimate_tokens(msgs)
        no_tools = chat_webui.estimate_tokens(msgs, include_tools=False)
        assert with_tools > no_tools


class TestTrimMessagesForContext:
    def test_returns_messages_when_small(self, chat_webui):
        msgs = [{"role": "user", "content": "hi"}]
        assert chat_webui.trim_messages_for_context(msgs) == msgs

    def test_keeps_system_message(self, chat_webui):
        sys_msg = {"role": "system", "content": "you are x"}
        filler = [{"role": "user", "content": "a" * 5000} for _ in range(10)]
        msgs = [sys_msg] + filler
        out = chat_webui.trim_messages_for_context(msgs)
        assert out[0] == sys_msg
        assert chat_webui.estimate_tokens(out) <= chat_webui.MAX_INPUT_TOKENS

    def test_never_returns_empty(self, chat_webui):
        msgs = [{"role": "user", "content": "a" * 100000}]
        out = chat_webui.trim_messages_for_context(msgs)
        assert len(out) >= 1


class TestCompactMessagesCopy:
    def test_short_list_returned_as_is(self, chat_webui, monkeypatch):
        msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        monkeypatch.setattr(chat_webui, "_summarize_with_llm", lambda *a: None)
        out = chat_webui.compact_messages_copy(msgs)
        assert out == msgs

    def test_long_list_gets_summary(self, chat_webui, monkeypatch):
        monkeypatch.setattr(
            chat_webui, "_summarize_with_llm",
            lambda text: "SUMMARY OF CONVO",
        )
        msgs = [{"role": "user", "content": "msg-%d" % i} for i in range(20)]
        out = chat_webui.compact_messages_copy(msgs)
        # summary system message + recent messages
        assert out[0]["role"] == "system"
        assert "SUMMARY OF CONVO" in out[0]["content"]
        assert len(out) <= 7
        # original list untouched (it is a copy)
        assert len(msgs) == 20

    def test_summary_failure_keeps_original(self, chat_webui, monkeypatch):
        monkeypatch.setattr(chat_webui, "_summarize_with_llm", lambda text: None)
        msgs = [{"role": "user", "content": "msg-%d" % i} for i in range(20)]
        out = chat_webui.compact_messages_copy(msgs)
        assert out == msgs


class TestPrepareContextForLLM:
    def test_below_threshold_uses_trim(self, chat_webui):
        sid = "s1"
        msgs = [{"role": "user", "content": "hello"}]
        with chat_webui._effective_contexts_lock:
            chat_webui._effective_contexts.pop(sid, None)
        out = chat_webui.prepare_context_for_llm(sid, msgs)
        assert out == msgs
        with chat_webui._effective_contexts_lock:
            assert sid not in chat_webui._effective_contexts

    def test_above_threshold_compresses(self, chat_webui, monkeypatch):
        monkeypatch.setattr(
            chat_webui, "_summarize_with_llm", lambda text: "compressed summary"
        )
        sid = "s2"
        big = [{"role": "user", "content": "x" * 5000} for _ in range(30)]
        with chat_webui._effective_contexts_lock:
            chat_webui._effective_contexts.pop(sid, None)
        out = chat_webui.prepare_context_for_llm(sid, big)
        assert out[0]["role"] == "system"
        with chat_webui._effective_contexts_lock:
            assert sid in chat_webui._effective_contexts


class TestEffectiveTokenEstimate:
    def test_uses_cached_compressed(self, chat_webui):
        sid = "s3"
        cached = [{"role": "system", "content": "short"}]
        with chat_webui._effective_contexts_lock:
            chat_webui._effective_contexts[sid] = cached
        assert chat_webui.effective_token_estimate(sid, [{"role": "user", "content": "x" * 1000}]) == chat_webui.estimate_tokens(cached)

    def test_falls_back_to_messages(self, chat_webui):
        sid = "s4"
        with chat_webui._effective_contexts_lock:
            chat_webui._effective_contexts.pop(sid, None)
        msgs = [{"role": "user", "content": "hi"}]
        assert chat_webui.effective_token_estimate(sid, msgs) == chat_webui.estimate_tokens(msgs)


class TestContextTokenReport:
    def test_report_shape(self, chat_webui):
        sid = "s5"
        with chat_webui._effective_contexts_lock:
            chat_webui._effective_contexts.pop(sid, None)
        report = chat_webui.context_token_report(sid, [{"role": "user", "content": "hi"}])
        assert set(report) == {"token_estimate", "raw_token_estimate", "context_compressed"}
        assert report["context_compressed"] is False


class TestTaskFunctions:
    def test_crud_cycle(self, chat_webui, temp_paths):
        t = chat_webui.task_create("u1", "Buy milk", "2%", priority="high")
        assert t["title"] == "Buy milk"
        assert t["user_id"] == "u1"
        assert t["status"] == "pending"
        assert t["priority"] == "high"

        t2 = chat_webui.task_update(t["id"], "u1", status="completed")
        assert t2["status"] == "completed"

        assert chat_webui.task_get(t["id"], "u1")["id"] == t["id"]
        assert chat_webui.task_get(t["id"], "other") is None

        listed = chat_webui.task_list("u1")
        assert len(listed) == 1
        assert chat_webui.task_list("u1", status="completed")[0]["id"] == t["id"]
        assert chat_webui.task_list("u1", status="pending") == []

        assert chat_webui.task_delete(t["id"], "u1") == 1
        assert chat_webui.task_get(t["id"], "u1") is None

    def test_task_update_skips_none_values(self, chat_webui, temp_paths):
        t = chat_webui.task_create("u1", "T")
        t2 = chat_webui.task_update(t["id"], "u1", title="U", due_date=None)
        assert t2["title"] == "U"

    def test_task_complete_sets_status(self, chat_webui, temp_paths):
        t = chat_webui.task_create("u1", "T")
        t2 = chat_webui.task_complete(t["id"], "u1")
        assert t2["status"] == "completed"

    def test_task_update_other_user_noop(self, chat_webui, temp_paths):
        t = chat_webui.task_create("u1", "T")
        chat_webui.task_update(t["id"], "u2", status="completed")
        assert chat_webui.task_get(t["id"], "u1")["status"] == "pending"


class TestHandleTaskTool:
    def test_create(self, chat_webui, temp_paths):
        out = json.loads(chat_webui.handle_task_tool("u1", {"operation": "create", "title": "X"}))
        assert out["ok"] is True
        assert out["task"]["title"] == "X"

    def test_create_missing_title(self, chat_webui, temp_paths):
        out = json.loads(chat_webui.handle_task_tool("u1", {"operation": "create"}))
        assert out["ok"] is False

    def test_list(self, chat_webui, temp_paths):
        chat_webui.task_create("u1", "X")
        out = json.loads(chat_webui.handle_task_tool("u1", {"operation": "list"}))
        assert out["ok"] is True
        assert len(out["tasks"]) == 1

    def test_complete(self, chat_webui, temp_paths):
        t = chat_webui.task_create("u1", "X")
        out = json.loads(chat_webui.handle_task_tool("u1", {"operation": "complete", "task_id": t["id"]}))
        assert out["task"]["status"] == "completed"

    def test_complete_missing_task(self, chat_webui, temp_paths):
        out = json.loads(chat_webui.handle_task_tool("u1", {"operation": "complete", "task_id": "nope"}))
        assert out["ok"] is False

    def test_delete(self, chat_webui, temp_paths):
        t = chat_webui.task_create("u1", "X")
        out = json.loads(chat_webui.handle_task_tool("u1", {"operation": "delete", "task_id": t["id"]}))
        assert out["ok"] is True
        assert chat_webui.task_get(t["id"], "u1") is None

    def test_unknown_operation(self, chat_webui, temp_paths):
        out = json.loads(chat_webui.handle_task_tool("u1", {"operation": "frobnicate"}))
        assert out["ok"] is False


class TestUsers:
    def test_password_lookup(self, chat_webui, make_user):
        make_user({"alice": "secret"})
        assert chat_webui.get_user_password("alice") == "secret"
        assert chat_webui.get_user_password("bob") == ""

    def test_context_path(self, chat_webui, make_user, tmp_path):
        ctx = str(tmp_path / "ctx" / "alice.txt")
        make_user({"alice": "secret"}, context_files={"alice": ctx})
        assert chat_webui.get_user_context_path("alice") == ctx
        assert chat_webui.get_user_context_path("bob") == ""

    def test_read_missing_context(self, chat_webui, make_user):
        make_user({"alice": "secret"})
        assert chat_webui.read_user_context("alice") == ""

    def test_write_and_read_context(self, chat_webui, make_user, tmp_path):
        ctx = str(tmp_path / "ctx" / "alice.txt")
        make_user({"alice": "secret"}, context_files={"alice": ctx})
        chat_webui.write_user_context("alice", "Loves tea")
        context = chat_webui.read_user_context("alice")
        assert "Loves tea" in context

    def test_write_appends(self, chat_webui, make_user, tmp_path):
        ctx = str(tmp_path / "ctx" / "alice.txt")
        make_user({"alice": "secret"}, context_files={"alice": ctx})
        chat_webui.write_user_context("alice", "Fact one")
        chat_webui.write_user_context("alice", "Fact two")
        context = chat_webui.read_user_context("alice")
        assert "Fact one" in context
        assert "Fact two" in context

    def test_load_users_caches(self, chat_webui, make_user, monkeypatch):
        make_user({"alice": "secret"})
        assert chat_webui.get_user_password("alice") == "secret"
        # rewrite the file; cache must still serve the old value within 30s
        with open(chat_webui.USERS_FILE, "w") as f:
            json.dump({"users": {"alice": {"password": "changed"}}}, f)
        assert chat_webui.get_user_password("alice") == "secret"
        # after cache expiry it reloads
        chat_webui._users_cache_time = 0
        assert chat_webui.get_user_password("alice") == "changed"


class TestGetCurrentUser:
    def test_no_token(self, chat_webui):
        assert chat_webui.get_current_user({}) is None

    def test_unknown_token(self, chat_webui):
        assert chat_webui.get_current_user({"X-Auth-Token": "nope"}) is None

    def test_valid_token_updates_last_seen(self, chat_webui):
        chat_webui._active_tokens["t"] = {"user": "alice", "last_seen": 0}
        assert chat_webui.get_current_user({"X-Auth-Token": "t"}) == "alice"
        assert chat_webui._active_tokens["t"]["last_seen"] > 0


class TestSessionsPersistence:
    def test_save_and_load_roundtrip(self, chat_webui, temp_paths):
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.sessions["s1"] = [{"role": "user", "content": "hi"}]
        chat_webui.sessions_meta["s1"] = {
            "name": "My Chat", "created": 1, "updated": 2,
            "user_id": "alice", "system_prompts": [],
        }
        chat_webui.save_sessions()
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.load_sessions()
        assert chat_webui.sessions["s1"][0]["content"] == "hi"
        assert chat_webui.sessions_meta["s1"]["name"] == "My Chat"
        assert chat_webui.sessions_meta["s1"]["user_id"] == "alice"

    def test_session_file_is_per_user(self, chat_webui, temp_paths):
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.sessions["a"] = []
        chat_webui.sessions_meta["a"] = {"name": "A", "user_id": "alice", "created": 1, "updated": 2, "system_prompts": []}
        chat_webui.sessions["b"] = []
        chat_webui.sessions_meta["b"] = {"name": "B", "user_id": "bob", "created": 1, "updated": 2, "system_prompts": []}
        chat_webui.save_sessions()
        assert os.path.exists(os.path.join(chat_webui.SESSIONS_DIR, "sessions_alice.json"))
        assert os.path.exists(os.path.join(chat_webui.SESSIONS_DIR, "sessions_bob.json"))


class TestMiscHelpers:
    def test_load_extra_prompts_dict(self, chat_webui):
        out = chat_webui._load_extra_prompts([{"name": "X", "content": "hello"}])
        assert out == [{"name": "X", "content": "hello"}]

    def test_load_extra_prompts_skips_empty(self, chat_webui):
        assert chat_webui._load_extra_prompts([{"name": "X", "content": "   "}]) == []

    def test_load_extra_prompts_file(self, chat_webui, tmp_path):
        p = tmp_path / "extra.md"
        p.write_text("# Extra")
        out = chat_webui._load_extra_prompts([str(p)])
        assert out[0]["content"] == "# Extra"
        assert out[0]["name"] == "extra.md"

    def test_image_url_rel(self, chat_webui):
        assert chat_webui._image_url_rel("/output/user/img.png") == "user/img.png"
        assert chat_webui._image_url_rel("https://x/output/a.png") == "a.png"
        assert chat_webui._image_url_rel("img.png") == "img.png"

    def test_output_rel(self, chat_webui, tmp_path):
        assert chat_webui._output_rel("relative/path.png") == "relative/path.png"
        abs_path = os.path.join(chat_webui.COMFYUI_OUTPUT, "user", "x.png")
        rel = chat_webui._output_rel(abs_path)
        assert rel == os.path.join("user", "x.png")

    def test_model_status_snapshot(self, chat_webui):
        chat_webui.model_status = "chat_loaded"
        chat_webui._last_tps = 5.5
        snap = chat_webui.model_status_snapshot()
        assert snap["model"] == "chat_loaded"
        assert snap["predicted_per_second"] == 5.5

    def test_location_roundtrip(self, chat_webui):
        assert chat_webui.location_str() is None
        chat_webui.set_client_location("Kolkata")
        assert chat_webui.location_str() == "Kolkata"

    def test_set_status_marks_working(self, chat_webui):
        chat_webui.tasks["t1"] = {"status": "queued"}
        chat_webui.set_status("t1", "Doing work")
        assert chat_webui.tasks["t1"]["status"] == "working"
        assert chat_webui.tasks["t1"]["message"] == "Doing work"

    def test_set_status_skips_cancelled(self, chat_webui):
        chat_webui.tasks["t1"] = {"status": "cancelled"}
        chat_webui.set_status("t1", "Doing work")
        assert chat_webui.tasks["t1"]["status"] == "cancelled"

    def test_strip_html(self, chat_webui):
        html = "<html><style>.x{}</style><script>bad()</script><p>Hello <b>World</b></p></html>"
        out = chat_webui.strip_html(html)
        assert "Hello World" in out
        assert "<script>" not in out
        assert "bad()" not in out


class TestWebSearch:
    def test_returns_results(self, chat_webui, monkeypatch):
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"results": [
                    {"title": "T", "url": "http://example.com", "content": "snippet"},
                ]}

        calls = {}
        def fake_get(url, params=None, timeout=None):
            calls["params"] = params
            return FakeResp()
        monkeypatch.setattr(chat_webui.requests, "get", fake_get)
        out = json.loads(chat_webui.web_search("  cats  "))
        assert out["results"][0]["title"] == "T"
        # params use the trimmed query; the echoed "query" field keeps raw input
        assert calls["params"]["q"] == "cats"
        assert out["query"] == "  cats  "
        assert "search_date" in out

    def test_error_returns_error_payload(self, chat_webui, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("offline")
        monkeypatch.setattr(chat_webui.requests, "get", boom)
        out = json.loads(chat_webui.web_search("cats"))
        assert out["results"] == []
        assert "offline" in out["error"]


class TestFetchPage:
    def test_empty_url(self, chat_webui):
        out = json.loads(chat_webui.fetch_page(""))
        assert out["error"] == "No URL provided."

    def test_bad_scheme(self, chat_webui):
        out = json.loads(chat_webui.fetch_page("file:///etc/passwd"))
        assert "Only http/https" in out["error"]

    def test_ssrf_private_ip_blocked(self, chat_webui, monkeypatch):
        monkeypatch.setattr(
            "socket.gethostbyname", lambda host: "127.0.0.1"
        )
        out = json.loads(chat_webui.fetch_page("http://internal.local/"))
        assert "private/internal" in out["error"]

    def test_ssrf_link_local_blocked(self, chat_webui, monkeypatch):
        monkeypatch.setattr(
            "socket.gethostbyname", lambda host: "169.254.169.254"
        )
        out = json.loads(chat_webui.fetch_page("http://metadata/"))
        assert "private/internal" in out["error"]

    def test_fetches_and_extracts_text(self, chat_webui, monkeypatch):
        monkeypatch.setattr("socket.gethostbyname", lambda host: "93.184.216.34")
        from bs4 import BeautifulSoup

        class FakeResp:
            url = "http://example.com/"
            encoding = "utf-8"

            @property
            def text(self):
                return "<html><head><title>Example</title></head><body><main><p>Hello world</p></main></body></html>"

            @property
            def headers(self):
                return {"Content-Type": "text/html"}

            def raise_for_status(self):
                pass

        monkeypatch.setattr(chat_webui.requests, "get", lambda *a, **k: FakeResp())
        out = json.loads(chat_webui.fetch_page("http://example.com/"))
        assert out["title"] == "Example"
        assert "Hello world" in out["content"]

    def test_truncates_long_content(self, chat_webui, monkeypatch):
        monkeypatch.setattr("socket.gethostbyname", lambda host: "93.184.216.34")

        class FakeResp:
            url = "http://example.com/"
            encoding = "utf-8"

            @property
            def text(self):
                return "<html><body><main>" + "word " * 5000 + "</main></body></html>"

            @property
            def headers(self):
                return {"Content-Type": "text/html"}

            def raise_for_status(self):
                pass

        monkeypatch.setattr(chat_webui.requests, "get", lambda *a, **k: FakeResp())
        out = json.loads(chat_webui.fetch_page("http://example.com/", max_chars=100))
        assert out["content"].endswith("...[truncated]")


class TestFinalizeTask:
    def test_appends_assistant_message(self, chat_webui, temp_paths):
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.sessions["s1"] = []
        chat_webui.sessions_meta["s1"] = {"name": "N", "user_id": "alice", "created": 1, "updated": 1, "system_prompts": []}
        chat_webui.tasks["t1"] = {"_tools_used": ["web_search"], "session_id": "s1"}
        body = {"choices": [{"message": {"content": "answer", "reasoning_content": "thinking"}}], "timings": {"predicted_per_second": 9.5}}
        chat_webui._finalize_task("t1", "s1", "answer", body)
        msgs = chat_webui.sessions["s1"]
        assert msgs[-1]["role"] == "assistant"
        assert msgs[-1]["content"] == "answer"
        assert msgs[-1]["_reasoning"] == "thinking"
        assert msgs[-1]["_tools_used"] == ["web_search"]
        assert chat_webui.tasks["t1"]["status"] == "done"
        assert chat_webui.tasks["t1"]["predicted_per_second"] == 9.5

    def test_image_url_from_file(self, chat_webui, temp_paths):
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.sessions["s1"] = []
        chat_webui.sessions_meta["s1"] = {"name": "N", "user_id": "alice", "created": 1, "updated": 1, "system_prompts": []}
        chat_webui.tasks["t1"] = {"_tools_used": [], "image_file": "user/gen.png", "session_id": "s1"}
        body = {"choices": [{"message": {"content": "", "reasoning_content": ""}}], "timings": {}}
        chat_webui._finalize_task("t1", "s1", "", body)
        assert chat_webui.tasks["t1"]["image"] == "/output/user/gen.png"


class TestDispatchTool:
    def test_unknown_tool(self, chat_webui):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        tc = {"id": "tc1", "function": {"name": "nope", "arguments": "{}"}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert events and events[0][0][0] == "tool_ok"
        result = json.loads(events[0][1]["result"])
        assert "Unknown tool" in result["error"]

    def test_web_search(self, chat_webui, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        monkeypatch.setattr(chat_webui, "web_search", lambda q, **k: json.dumps({"results": [{"title": "T", "url": "u"}]}))
        chat_webui.tasks["t1"] = {"session_id": "s1"}
        tc = {"id": "tc1", "function": {"name": "web_search", "arguments": json.dumps({"query": "cats"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert events and events[0][0][0] == "tool_ok"
        result = events[0][1]["result"]
        assert "Web search results" in result
        assert chat_webui.tasks["t1"]["_tools_used"] == ["web_search"]
        assert chat_webui.tasks["t1"]["_search_details"][0]["results"]

    def test_generate_image_limit(self, chat_webui):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        chat_webui.tasks["t1"] = {"session_id": "s1", "_tools_used": ["generate_image"]}
        tc = {"id": "tc1", "function": {"name": "generate_image", "arguments": json.dumps({"prompt": "cat", "model": "z_image"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        result = json.loads(events[0][1]["result"])
        assert "limit reached" in result["error"]

    def test_generate_image_success(self, chat_webui, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        monkeypatch.setattr(
            chat_webui, "generate_image",
            lambda **k: json.dumps({"prompt_id": "p1", "file": "/tmp/x.png", "rel": "user/x.png"}),
        )
        chat_webui.tasks["t1"] = {"session_id": "s1"}
        tc = {"id": "tc1", "function": {"name": "generate_image", "arguments": json.dumps({"prompt": "cat", "model": "z_image"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        result = json.loads(events[0][1]["result"])
        assert result["image_url"] == "/output/user/x.png"
        assert chat_webui.tasks["t1"]["image_file"] == "user/x.png"
        assert chat_webui.tasks["t1"]["_tools_used"] == ["generate_image"]

    def test_read_file(self, chat_webui, temp_paths, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        fpath = os.path.join(chat_webui.UPLOADS_DIR, "doc.txt")
        with open(fpath, "w") as f:
            f.write("file contents")
        monkeypatch.setattr(chat_webui, "read_file_text", lambda p: "file contents")
        tc = {"id": "tc1", "function": {"name": "read_file", "arguments": json.dumps({"file_url": "/uploads/doc.txt"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        result = events[0][1]["result"]
        assert "file contents" in result

    def test_read_file_missing(self, chat_webui):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        tc = {"id": "tc1", "function": {"name": "read_file", "arguments": json.dumps({"file_url": "/uploads/nope.txt"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert "File not found" in events[0][1]["result"]

    def test_manage_tasks(self, chat_webui, temp_paths):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        chat_webui.tasks["t1"] = {"session_id": "s1", "_user": "alice"}
        tc = {"id": "tc1", "function": {"name": "manage_tasks", "arguments": json.dumps({"operation": "create", "title": "X"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        result = json.loads(events[0][1]["result"])
        assert result["ok"] is True

    def test_update_user_context(self, chat_webui, temp_paths, make_user, tmp_path):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        ctx = str(tmp_path / "ctx" / "alice.txt")
        make_user({"alice": "secret"}, context_files={"alice": ctx})
        chat_webui.tasks["t1"] = {"session_id": "s1", "_user": "alice"}
        tc = {"id": "tc1", "function": {"name": "update_user_context", "arguments": json.dumps({"content": "Likes cats"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        result = json.loads(events[0][1]["result"])
        assert result["saved"] is True
        assert "Likes cats" in chat_webui.read_user_context("alice")

    def test_edit_image(self, chat_webui, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        monkeypatch.setattr(
            chat_webui, "edit_image",
            lambda **k: json.dumps({"prompt_id": "p", "file": "/tmp/x.png", "rel": "user/e.png"}),
        )
        chat_webui.tasks["t1"] = {"session_id": "s1"}
        tc = {"id": "tc1", "function": {"name": "edit_image", "arguments": json.dumps({"prompt": "make it red", "denoise": 0.5})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        result = json.loads(events[0][1]["result"])
        assert result["image_url"] == "/output/user/e.png"


# ---------------------------------------------------------------------------
# Additional branch coverage for task tool, users, sessions, paths
# ---------------------------------------------------------------------------


class TestHandleTaskToolMore:
    def test_update_success(self, chat_webui, temp_paths):
        t = chat_webui.task_create("u1", "T")
        out = json.loads(
            chat_webui.handle_task_tool("u1", {"operation": "update", "task_id": t["id"], "status": "in_progress"})
        )
        assert out["ok"] is True
        assert out["task"]["status"] == "in_progress"

    def test_update_missing_task(self, chat_webui, temp_paths):
        out = json.loads(chat_webui.handle_task_tool("u1", {"operation": "update", "task_id": "nope"}))
        assert out["ok"] is False

    def test_missing_task_id(self, chat_webui, temp_paths):
        for op in ("update", "complete", "delete", "get"):
            out = json.loads(chat_webui.handle_task_tool("u1", {"operation": op}))
            assert out["ok"] is False
            assert "task_id" in out["error"]

    def test_get(self, chat_webui, temp_paths):
        t = chat_webui.task_create("u1", "T")
        out = json.loads(chat_webui.handle_task_tool("u1", {"operation": "get", "task_id": t["id"]}))
        assert out["task"]["id"] == t["id"]
        out = json.loads(chat_webui.handle_task_tool("u1", {"operation": "get", "task_id": "nope"}))
        assert out["ok"] is False

    def test_update_no_fields(self, chat_webui, temp_paths):
        t = chat_webui.task_create("u1", "T")
        assert chat_webui.task_update(t["id"], "u1") is None


class TestLoadUsersErrors:
    def test_invalid_json(self, chat_webui, temp_paths):
        with open(chat_webui.USERS_FILE, "w") as f:
            f.write("{not json")
        chat_webui._users_cache = None
        assert chat_webui.load_users() == {}
        assert chat_webui.get_user_password("x") == ""

    def test_missing_file(self, chat_webui, temp_paths):
        if os.path.exists(chat_webui.USERS_FILE):
            os.remove(chat_webui.USERS_FILE)
        chat_webui._users_cache = None
        assert chat_webui.load_users() == {}


class TestReadUserContextErrors:
    def test_directory_context_returns_empty(self, chat_webui, make_user, tmp_path):
        d = tmp_path / "ctxdir"
        d.mkdir()
        make_user({"alice": "secret"}, context_files={"alice": str(d)})
        assert chat_webui.read_user_context("alice") == ""

    def test_load_extra_prompts_open_error(self, chat_webui, tmp_path, monkeypatch):
        p = tmp_path / "x.md"
        p.write_text("x")
        real_open = open

        def fake_open(path, *a, **k):
            if str(path) == str(p):
                raise OSError("nope")
            return real_open(path, *a, **k)

        monkeypatch.setattr(builtins, "open", fake_open)
        assert chat_webui._load_extra_prompts([str(p)]) == []


class TestLoadSessionsMore:
    def test_invalid_json_file_skipped(self, chat_webui, temp_paths):
        p = os.path.join(chat_webui.SESSIONS_DIR, "sessions_broken.json")
        with open(p, "w") as f:
            f.write("not json")
        chat_webui.load_sessions()

    def test_stale_sessions_json_loaded_and_removed(self, chat_webui, temp_paths):
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        stale = chat_webui.SESSIONS_FILE
        with open(stale, "w") as f:
            json.dump({"sessions": {"stale1": {"messages": [{"role": "user", "content": "old"}], "name": "Old", "user_id": "u"}}}, f)
        chat_webui.load_sessions()
        assert chat_webui.sessions.get("stale1")[0]["content"] == "old"
        assert not os.path.exists(stale)

    def test_stale_invalid_json(self, chat_webui, temp_paths):
        stale = chat_webui.SESSIONS_FILE
        with open(stale, "w") as f:
            f.write("{bad")
        chat_webui.load_sessions()

    def test_stale_remove_error(self, chat_webui, temp_paths, monkeypatch):
        stale = chat_webui.SESSIONS_FILE
        with open(stale, "w") as f:
            json.dump({"sessions": {}}, f)

        def fake_remove(p):
            raise OSError("boom")

        monkeypatch.setattr(chat_webui.os, "remove", fake_remove)
        chat_webui.load_sessions()


class TestPathHelpers:
    def test_task_user(self, chat_webui):
        chat_webui.tasks.pop("t1", None)
        assert chat_webui._task_user("t1") == ""
        chat_webui.tasks["t1"] = {"_user": "alice"}
        assert chat_webui._task_user("t1") == "alice"

    def test_output_dir(self, chat_webui, temp_paths):
        assert chat_webui._output_dir("alice") == os.path.join(chat_webui.COMFYUI_OUTPUT, "alice")

    def test_input_dir(self, chat_webui, temp_paths):
        assert chat_webui._input_dir("alice") == os.path.join(chat_webui.COMFYUI_INPUT, "alice")

    def test_output_rel_valueerror(self, chat_webui, monkeypatch):
        def boom(a, b):
            raise ValueError

        monkeypatch.setattr(chat_webui.os.path, "relpath", boom)
        assert chat_webui._output_rel("/abs/x.png") == "x.png"


class TestEstimateTokensMore:
    def test_multimodal_skips_non_dict_part(self, chat_webui):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}, "junk"]}]
        assert chat_webui.estimate_tokens(msgs, include_tools=False) > 0

    def test_trims_when_over_limit(self, chat_webui):
        msgs = [{"role": "user", "content": "a" * 50000} for _ in range(20)]
        out = chat_webui.trim_messages_for_context(msgs)
        assert chat_webui.estimate_tokens(out) <= chat_webui.MAX_INPUT_TOKENS
        assert len(out) < len(msgs)


class TestCompactMessagesCopyMore:
    def test_keep_messages_zero(self, chat_webui, monkeypatch):
        monkeypatch.setattr(chat_webui, "_summarize_with_llm", lambda text: "S")
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        out = chat_webui.compact_messages_copy(msgs, keep_messages=0)
        assert out[0]["role"] == "system"
        assert out[1]["role"] == "system"
        assert "S" in out[1]["content"]

    def test_list_content_parts(self, chat_webui, monkeypatch):
        monkeypatch.setattr(chat_webui, "_summarize_with_llm", lambda text: text)
        msgs = [{"role": "user", "content": [{"type": "text", "text": "part1"}, {"type": "image_url", "image_url": {"url": "x"}}, "junk"]} for _ in range(15)]
        out = chat_webui.compact_messages_copy(msgs)
        assert "part1" in out[0]["content"]
        assert "image_url" not in out[0]["content"]
        assert "junk" not in out[0]["content"]

    def test_empty_content_returns_original(self, chat_webui, monkeypatch):
        monkeypatch.setattr(chat_webui, "_summarize_with_llm", lambda text: "S")
        msgs = [{"role": "user", "content": ""} for _ in range(15)]
        out = chat_webui.compact_messages_copy(msgs)
        assert out == msgs

    def test_system_message_with_summary(self, chat_webui, monkeypatch):
        monkeypatch.setattr(chat_webui, "_summarize_with_llm", lambda text: "S")
        msgs = [{"role": "system", "content": "sys"}] + [{"role": "user", "content": "m%d" % i} for i in range(15)]
        out = chat_webui.compact_messages_copy(msgs)
        assert out[0]["role"] == "system"
        assert out[1]["role"] == "system"


class TestSummarizeWithLLM:
    def test_success(self, chat_webui, monkeypatch):
        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "summary!"}}]}

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: Resp())
        assert chat_webui._summarize_with_llm("text") == "summary!"

    def test_failure(self, chat_webui, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr(chat_webui.requests, "post", boom)
        assert chat_webui._summarize_with_llm("text") is None


# ---------------------------------------------------------------------------
# Network / system helpers
# ---------------------------------------------------------------------------


class TestIsLlamaAlive:
    def test_alive(self, chat_webui, monkeypatch):
        class Resp:
            status_code = 200

        monkeypatch.setattr(chat_webui.requests, "get", lambda *a, **k: Resp())
        assert chat_webui.is_llama_alive() is True

    def test_dead(self, chat_webui, monkeypatch):
        def boom(*a, **k):
            raise OSError("no")

        monkeypatch.setattr(chat_webui.requests, "get", boom)
        assert chat_webui.is_llama_alive() is False


class TestLlamaModelLifecycle:
    def test_unload_when_already_unloaded(self, chat_webui):
        chat_webui.model_status = "unloaded"
        assert chat_webui.unload_llama_model() is True

    def test_unload_success(self, chat_webui, monkeypatch):
        chat_webui.model_status = "chat_loaded"
        class Resp:
            status_code = 200
            text = "ok"

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: Resp())
        assert chat_webui.unload_llama_model() is True
        assert chat_webui.model_status == "unloaded"

    def test_unload_failure_falls_back_alive(self, chat_webui, monkeypatch):
        chat_webui.model_status = "chat_loaded"
        class Resp:
            status_code = 500
            text = "err"

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: Resp())
        monkeypatch.setattr(chat_webui, "is_llama_alive", lambda: True)
        assert chat_webui.unload_llama_model() is False
        assert chat_webui.model_status == "chat_loaded"

    def test_unload_exception(self, chat_webui, monkeypatch):
        chat_webui.model_status = "chat_loaded"

        def boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr(chat_webui.requests, "post", boom)
        monkeypatch.setattr(chat_webui, "is_llama_alive", lambda: False)
        assert chat_webui.unload_llama_model() is False
        assert chat_webui.model_status == "unloaded"

    def test_load_success(self, chat_webui, monkeypatch):
        chat_webui.model_status = "unloaded"
        class Resp:
            status_code = 200
            text = "ok"

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: Resp())
        monkeypatch.setattr(chat_webui, "is_llama_alive", lambda: True)
        monkeypatch.setattr(chat_webui.time, "sleep", lambda *a, **k: None)
        assert chat_webui.load_llama_model() is True
        assert chat_webui.model_status == "chat_loaded"

    def test_load_polls_then_fails(self, chat_webui, monkeypatch):
        chat_webui.model_status = "unloaded"
        class Resp:
            status_code = 200
            text = "ok"

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: Resp())
        monkeypatch.setattr(chat_webui, "is_llama_alive", lambda: False)
        monkeypatch.setattr(chat_webui.time, "sleep", lambda *a, **k: None)
        assert chat_webui.load_llama_model() is False
        assert chat_webui.model_status == "unloaded"

    def test_load_http_error_fallback_alive(self, chat_webui, monkeypatch):
        chat_webui.model_status = "unloaded"
        class Resp:
            status_code = 503
            text = "busy"

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: Resp())
        monkeypatch.setattr(chat_webui, "is_llama_alive", lambda: True)
        monkeypatch.setattr(chat_webui.time, "sleep", lambda *a, **k: None)
        assert chat_webui.load_llama_model() is True

    def test_load_exception(self, chat_webui, monkeypatch):
        chat_webui.model_status = "unloaded"

        def boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr(chat_webui.requests, "post", boom)
        monkeypatch.setattr(chat_webui, "is_llama_alive", lambda: False)
        monkeypatch.setattr(chat_webui.time, "sleep", lambda *a, **k: None)
        assert chat_webui.load_llama_model() is False


class TestSystemHelpers:
    def test_free_vram_success(self, chat_webui, monkeypatch):
        class Resp:
            status_code = 200

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: Resp())
        monkeypatch.setattr(chat_webui.time, "sleep", lambda *a, **k: None)
        assert chat_webui.free_comfyui_vram() is True

    def test_free_vram_error(self, chat_webui, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr(chat_webui.requests, "post", boom)
        monkeypatch.setattr(chat_webui.time, "sleep", lambda *a, **k: None)
        assert chat_webui.free_comfyui_vram() is False

    def test_get_gpu_temp(self, chat_webui, monkeypatch):
        class Result:
            stdout = "72\n"

        monkeypatch.setattr(chat_webui.subprocess, "run", lambda *a, **k: Result())
        assert chat_webui.get_gpu_temp() == 72

    def test_get_gpu_temp_error(self, chat_webui, monkeypatch):
        def boom(*a, **k):
            raise OSError("no gpu")

        monkeypatch.setattr(chat_webui.subprocess, "run", boom)
        assert chat_webui.get_gpu_temp() is None

    def test_get_ram_usage(self, chat_webui, monkeypatch):
        class Result:
            stdout = "              total        used        free      shared  buff/cache   available\nMem:          15987        5120        2000        100        8867        10700\nSwap:         2048           0        2048\n"

        monkeypatch.setattr(chat_webui.subprocess, "run", lambda *a, **k: Result())
        val = chat_webui.get_ram_usage()
        assert val is not None and 0 <= val <= 100

    def test_get_ram_usage_error(self, chat_webui, monkeypatch):
        def boom(*a, **k):
            raise OSError("no free")

        monkeypatch.setattr(chat_webui.subprocess, "run", boom)
        assert chat_webui.get_ram_usage() is None

    def test_kill_llama_server(self, chat_webui, monkeypatch):
        calls = []
        monkeypatch.setattr(chat_webui.subprocess, "run", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(chat_webui.time, "sleep", lambda *a, **k: None)
        chat_webui.kill_llama_server()
        assert len(calls) == 2

    def test_kill_comfyui(self, chat_webui, monkeypatch):
        calls = []
        monkeypatch.setattr(chat_webui.subprocess, "run", lambda *a, **k: calls.append(a))
        chat_webui.kill_comfyui()
        assert len(calls) == 1


class TestRestartServers:
    def test_restart_healthy(self, chat_webui, monkeypatch, tmp_path):
        killed = []
        monkeypatch.setattr(chat_webui, "kill_llama_server", lambda: killed.append("llama"))
        monkeypatch.setattr(chat_webui, "kill_comfyui", lambda: killed.append("comfy"))
        class Resp:
            status_code = 200

        monkeypatch.setattr(chat_webui.requests, "get", lambda *a, **k: Resp())
        opened = []
        real_open = open

        def fake_open(path, mode):
            opened.append(path)
            return real_open(path, mode)

        monkeypatch.setattr(builtins, "open", fake_open)
        popens = []
        monkeypatch.setattr(chat_webui.subprocess, "Popen", lambda *a, **k: popens.append(a))
        monkeypatch.setattr(chat_webui.time, "sleep", lambda *a, **k: None)
        monkeypatch.setattr(chat_webui.os.path, "expanduser", lambda p: str(tmp_path))
        chat_webui.restart_servers()
        assert len(popens) == 2
        assert len(opened) == 2
        assert killed == ["llama", "comfy"]

    def test_restart_timeout_kills(self, chat_webui, monkeypatch, tmp_path):
        killed = []
        monkeypatch.setattr(chat_webui, "kill_llama_server", lambda: killed.append("llama"))
        monkeypatch.setattr(chat_webui, "kill_comfyui", lambda: None)

        def boom(*a, **k):
            raise OSError("no")

        monkeypatch.setattr(chat_webui.requests, "get", boom)
        real_open = open
        monkeypatch.setattr(builtins, "open", lambda path, mode: real_open(str(tmp_path / os.path.basename(path)), mode))
        monkeypatch.setattr(chat_webui.subprocess, "Popen", lambda *a, **k: None)
        monkeypatch.setattr(chat_webui.time, "sleep", lambda *a, **k: None)
        state = {"t": 1000.0}

        def fake_time():
            state["t"] += 3
            return state["t"]

        monkeypatch.setattr(chat_webui.time, "time", fake_time)
        monkeypatch.setattr(chat_webui.os.path, "expanduser", lambda p: str(tmp_path))
        chat_webui.restart_servers()
        assert killed.count("llama") == 2


class TestEnsureComfyuiRunning:
    def test_already_running(self, chat_webui, monkeypatch):
        class Resp:
            status_code = 200

        monkeypatch.setattr(chat_webui.requests, "get", lambda *a, **k: Resp())
        started = []
        monkeypatch.setattr(chat_webui, "kill_comfyui", lambda: started.append(1))
        monkeypatch.setattr(builtins, "open", lambda path, mode: io.StringIO())
        monkeypatch.setattr(chat_webui.subprocess, "Popen", lambda *a, **k: None)
        monkeypatch.setattr(chat_webui.time, "sleep", lambda *a, **k: None)
        monkeypatch.setattr(chat_webui.os.path, "expanduser", lambda p: "/tmp")
        chat_webui.ensure_comfyui_running()
        assert started == []

    def test_start_healthy_after(self, chat_webui, monkeypatch):
        state = {"i": 0}

        def fake_get(*a, **k):
            state["i"] += 1
            if state["i"] == 1:
                raise OSError("down")
            class Resp:
                status_code = 200
            return Resp()

        monkeypatch.setattr(chat_webui.requests, "get", fake_get)
        started = []
        monkeypatch.setattr(chat_webui, "kill_comfyui", lambda: started.append(1))
        monkeypatch.setattr(builtins, "open", lambda path, mode: io.StringIO())
        monkeypatch.setattr(chat_webui.subprocess, "Popen", lambda *a, **k: None)
        monkeypatch.setattr(chat_webui.time, "sleep", lambda *a, **k: None)
        monkeypatch.setattr(chat_webui.os.path, "expanduser", lambda p: "/tmp")
        chat_webui.ensure_comfyui_running()
        assert started == [1]

    def test_start_on_5xx_then_healthy(self, chat_webui, monkeypatch):
        state = {"i": 0}

        def fake_get(*a, **k):
            state["i"] += 1
            class Resp:
                status_code = 503 if state["i"] == 1 else 200
            return Resp()

        monkeypatch.setattr(chat_webui.requests, "get", fake_get)
        started = []
        monkeypatch.setattr(chat_webui, "kill_comfyui", lambda: started.append(1))
        monkeypatch.setattr(builtins, "open", lambda path, mode: io.StringIO())
        monkeypatch.setattr(chat_webui.subprocess, "Popen", lambda *a, **k: None)
        monkeypatch.setattr(chat_webui.time, "sleep", lambda *a, **k: None)
        monkeypatch.setattr(chat_webui.os.path, "expanduser", lambda p: "/tmp")
        chat_webui.ensure_comfyui_running()
        assert started == [1]

    def test_start_timeout(self, chat_webui, monkeypatch):
        def boom(*a, **k):
            raise OSError("down")

        monkeypatch.setattr(chat_webui.requests, "get", boom)
        monkeypatch.setattr(chat_webui, "kill_comfyui", lambda: None)
        monkeypatch.setattr(builtins, "open", lambda path, mode: io.StringIO())
        monkeypatch.setattr(chat_webui.subprocess, "Popen", lambda *a, **k: None)
        monkeypatch.setattr(chat_webui.time, "sleep", lambda *a, **k: None)
        state = {"t": 1000.0}

        def fake_time():
            state["t"] += 3
            return state["t"]

        monkeypatch.setattr(chat_webui.time, "time", fake_time)
        monkeypatch.setattr(chat_webui.os.path, "expanduser", lambda p: "/tmp")
        chat_webui.ensure_comfyui_running()


# ---------------------------------------------------------------------------
# fetch_page extra paths
# ---------------------------------------------------------------------------


class TestFetchPageMore:
    def test_invalid_dns(self, chat_webui, monkeypatch):
        import socket

        def boom(host):
            raise socket.gaierror("nxdomain")

        monkeypatch.setattr("socket.gethostbyname", boom)
        out = json.loads(chat_webui.fetch_page("http://bad.example/"))
        assert "Invalid URL" in out["error"]

    def test_binary_content_type_skipped(self, chat_webui, monkeypatch):
        monkeypatch.setattr("socket.gethostbyname", lambda host: "93.184.216.34")

        class FakeResp:
            url = "http://x/"

            @property
            def headers(self):
                return {"Content-Type": "application/pdf"}

            def raise_for_status(self):
                pass

            @property
            def text(self):
                return "binary"

        monkeypatch.setattr(chat_webui.requests, "get", lambda *a, **k: FakeResp())
        out = json.loads(chat_webui.fetch_page("http://x/"))
        assert "not readable text content" in out["error"]

    def test_encoding_detected(self, chat_webui, monkeypatch):
        monkeypatch.setattr("socket.gethostbyname", lambda host: "93.184.216.34")

        class FakeResp:
            url = "http://x/"
            encoding = None
            apparent_encoding = "latin-1"

            @property
            def headers(self):
                return {"Content-Type": "text/html"}

            def raise_for_status(self):
                pass

            @property
            def text(self):
                return "<html><body><main>hi</main></body></html>"

        monkeypatch.setattr(chat_webui.requests, "get", lambda *a, **k: FakeResp())
        out = json.loads(chat_webui.fetch_page("http://x/"))
        assert "hi" in out["content"]

    def test_strips_noise_tags(self, chat_webui, monkeypatch):
        monkeypatch.setattr("socket.gethostbyname", lambda host: "93.184.216.34")

        class FakeResp:
            url = "http://x/"
            encoding = "utf-8"

            @property
            def headers(self):
                return {"Content-Type": "text/html"}

            def raise_for_status(self):
                pass

            @property
            def text(self):
                return "<html><body><main><nav>nav</nav><footer>foot</footer><p>text</p></main></body></html>"

        monkeypatch.setattr(chat_webui.requests, "get", lambda *a, **k: FakeResp())
        out = json.loads(chat_webui.fetch_page("http://x/"))
        assert "text" in out["content"]
        assert "nav" not in out["content"]

    def test_fetch_failure(self, chat_webui, monkeypatch):
        monkeypatch.setattr("socket.gethostbyname", lambda host: "93.184.216.34")

        def boom(*a, **k):
            raise RuntimeError("timeout")

        monkeypatch.setattr(chat_webui.requests, "get", boom)
        out = json.loads(chat_webui.fetch_page("http://x/"))
        assert "Failed to fetch page" in out["error"]


# ---------------------------------------------------------------------------
# _finalize_task, _set_task_error, _delete_task_image
# ---------------------------------------------------------------------------


class TestFinalizeTaskMore:
    def test_task_not_found(self, chat_webui):
        chat_webui.tasks.pop("ghost", None)
        chat_webui._finalize_task("ghost", "s1", "x", {"choices": [], "timings": {}})

    def test_no_content_with_reasoning(self, chat_webui, temp_paths):
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.sessions["s1"] = []
        chat_webui.sessions_meta["s1"] = {"name": "N", "user_id": "a", "created": 1, "updated": 1, "system_prompts": []}
        chat_webui.tasks["t1"] = {"_tools_used": [], "session_id": "s1"}
        body = {"choices": [{"message": {"content": "", "reasoning_content": "thinking"}}], "timings": {}}
        chat_webui._finalize_task("t1", "s1", "", body)
        assert chat_webui.tasks["t1"]["response"] == "(No response content generated)"


class TestSetTaskError:
    def test_sets_error(self, chat_webui):
        chat_webui.tasks["t1"] = {"status": "working", "session_id": "s1"}
        chat_webui._set_task_error("t1", "boom", "s1")
        assert chat_webui.tasks["t1"]["status"] == "error"
        assert chat_webui.tasks["t1"]["error"] == "boom"

    def test_sid_fallback(self, chat_webui):
        chat_webui.tasks["t1"] = {"status": "working"}
        chat_webui._set_task_error("t1", "boom", "sX")
        assert chat_webui.tasks["t1"]["session_id"] == "sX"


class TestDeleteTaskImage:
    def test_no_task(self, chat_webui):
        chat_webui.tasks.pop("ghost", None)
        chat_webui._delete_task_image("ghost")

    def test_no_image(self, chat_webui):
        chat_webui.tasks["t1"] = {}
        chat_webui._delete_task_image("t1")

    def test_no_image_file_field(self, chat_webui):
        chat_webui.tasks["t1"] = {"status": "cancelled"}
        chat_webui._delete_task_image("t1")

    def test_removes_relative(self, chat_webui, temp_paths):
        os.makedirs(chat_webui.IMG_PATH, exist_ok=True)
        fpath = os.path.join(chat_webui.IMG_PATH, "sub", "x.png")
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "w") as f:
            f.write("x")
        chat_webui.tasks["t1"] = {"image_file": "sub/x.png"}
        chat_webui._delete_task_image("t1")
        assert not os.path.exists(fpath)

    def test_removes_absolute(self, chat_webui, temp_paths):
        os.makedirs(chat_webui.IMG_PATH, exist_ok=True)
        fpath = os.path.join(chat_webui.IMG_PATH, "abs.png")
        with open(fpath, "w") as f:
            f.write("x")
        chat_webui.tasks["t1"] = {"image_file": fpath}
        chat_webui._delete_task_image("t1")
        assert not os.path.exists(fpath)

    def test_remove_error(self, chat_webui, temp_paths, monkeypatch):
        os.makedirs(chat_webui.IMG_PATH, exist_ok=True)
        fpath = os.path.join(chat_webui.IMG_PATH, "x.png")
        with open(fpath, "w") as f:
            f.write("x")
        chat_webui.tasks["t1"] = {"image_file": "x.png"}

        def fake_remove(p):
            raise OSError("nope")

        monkeypatch.setattr(chat_webui.os, "remove", fake_remove)
        chat_webui._delete_task_image("t1")


# ---------------------------------------------------------------------------
# _dispatch_tool remaining branches
# ---------------------------------------------------------------------------


class TestDispatchToolMore:
    def test_invalid_json_arguments(self, chat_webui):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        chat_webui.tasks["t1"] = {"session_id": "s1"}
        tc = {"id": "tc1", "function": {"name": "manage_tasks", "arguments": "not-json"}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert events and events[0][0][0] == "tool_ok"

    def test_get_user_location_cached(self, chat_webui):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        chat_webui.set_client_location("Kolkata")
        tc = {"id": "l1", "function": {"name": "get_user_location", "arguments": "{}"}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert events[0][1]["result"] == "Kolkata"

    def test_get_user_location_denied(self, chat_webui, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        chat_webui._client_location = None
        class FakeEvent:
            def wait(self, timeout=None):
                return False

        monkeypatch.setattr(chat_webui.threading, "Event", lambda: FakeEvent())
        tc = {"id": "l1", "function": {"name": "get_user_location", "arguments": "{}"}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert events[0][1]["result"] == "User denied location access"

    def test_get_user_location_provided(self, chat_webui, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        chat_webui._client_location = None
        class FakeEvent:
            def wait(self, timeout=None):
                chat_webui._client_location = "Pune"
                return True

        monkeypatch.setattr(chat_webui.threading, "Event", lambda: FakeEvent())
        tc = {"id": "l1", "function": {"name": "get_user_location", "arguments": "{}"}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert events[0][1]["result"] == "Pune"

    def test_read_file_no_text(self, chat_webui, temp_paths, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        fpath = os.path.join(chat_webui.UPLOADS_DIR, "doc.pdf")
        with open(fpath, "wb") as f:
            f.write(b"%PDF-1.4")
        monkeypatch.setattr(chat_webui, "read_file_text", lambda p: "")
        tc = {"id": "tc1", "function": {"name": "read_file", "arguments": json.dumps({"file_url": "/uploads/doc.pdf"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert "Could not extract text" in events[0][1]["result"]

    def test_web_search_exception(self, chat_webui, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))

        def boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr(chat_webui, "web_search", boom)
        chat_webui.tasks["t1"] = {"session_id": "s1"}
        tc = {"id": "tc1", "function": {"name": "web_search", "arguments": json.dumps({"query": "cats"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert chat_webui.tasks["t1"]["_search_details"][0]["error"] == "down"

    def test_web_search_non_json_result(self, chat_webui, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        monkeypatch.setattr(chat_webui, "web_search", lambda *a, **k: "not json")
        chat_webui.tasks["t1"] = {"session_id": "s1"}
        tc = {"id": "tc1", "function": {"name": "web_search", "arguments": json.dumps({"query": "q"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert chat_webui.tasks["t1"].get("_search_details") == []

    def test_fetch_page_tool(self, chat_webui, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        monkeypatch.setattr(
            chat_webui, "fetch_page",
            lambda url: json.dumps({"url": "http://x/", "title": "T", "content": "body"}),
        )
        chat_webui.tasks["t1"] = {"session_id": "s1"}
        tc = {"id": "tc1", "function": {"name": "fetch_page", "arguments": json.dumps({"url": "http://x/"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert "Page content fetched" in events[0][1]["result"]
        assert chat_webui.tasks["t1"]["_search_details"][0]["title"] == "T"

    def test_fetch_page_raises(self, chat_webui, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))

        def boom(url):
            raise RuntimeError("bad page")

        monkeypatch.setattr(chat_webui, "fetch_page", boom)
        chat_webui.tasks["t1"] = {"session_id": "s1"}
        tc = {"id": "tc1", "function": {"name": "fetch_page", "arguments": json.dumps({"url": "http://x/"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert "bad page" in events[0][1]["result"]
        assert chat_webui.tasks["t1"]["_search_details"][0]["error"] == "bad page"

    def test_fetch_page_non_json_result(self, chat_webui, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        monkeypatch.setattr(chat_webui, "fetch_page", lambda url: "not json at all")
        chat_webui.tasks["t1"] = {"session_id": "s1"}
        tc = {"id": "tc1", "function": {"name": "fetch_page", "arguments": json.dumps({"url": "http://x/"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert "Page content fetched" in events[0][1]["result"]
        assert chat_webui.tasks["t1"].get("_search_details") is None

    def test_edit_image_no_file(self, chat_webui, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        monkeypatch.setattr(chat_webui, "edit_image", lambda **k: json.dumps({"error": "nope"}))
        chat_webui.tasks["t1"] = {"session_id": "s1"}
        tc = {"id": "tc1", "function": {"name": "edit_image", "arguments": json.dumps({"prompt": "x"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert json.loads(events[0][1]["result"])["error"] == "nope"

    def test_generate_image_no_file(self, chat_webui, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        monkeypatch.setattr(chat_webui, "generate_image", lambda **k: json.dumps({"error": "timeout"}))
        chat_webui.tasks["t1"] = {"session_id": "s1"}
        tc = {"id": "tc1", "function": {"name": "generate_image", "arguments": json.dumps({"prompt": "cat", "model": "z_image"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert json.loads(events[0][1]["result"])["error"] == "timeout"

    def test_manage_tasks_no_user(self, chat_webui):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        chat_webui.tasks["t1"] = {"session_id": "s1"}
        tc = {"id": "tc1", "function": {"name": "manage_tasks", "arguments": json.dumps({"operation": "list"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        result = json.loads(events[0][1]["result"])
        assert result["error"] == "User not found"

    def test_update_user_context_no_user(self, chat_webui):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        chat_webui.tasks["t1"] = {"session_id": "s1"}
        tc = {"id": "tc1", "function": {"name": "update_user_context", "arguments": json.dumps({"content": "x"})}}
        chat_webui._dispatch_tool("t1", "s1", tc, None, 0, 0)
        assert json.loads(events[0][1]["result"])["saved"] is False


class TestToolWorker:
    def test_dispatch_error(self, chat_webui, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))

        def boom(*a, **k):
            raise RuntimeError("crashed")

        monkeypatch.setattr(chat_webui, "_dispatch_tool", boom)
        tc = {"id": "tc1", "function": {"name": "tool", "arguments": "{}"}}
        chat_webui._tool_worker("t1", "s1", tc, None, 0, 0)
        assert events and events[0][0][0] == "tool_ok"
        assert "crashed" in json.loads(events[0][1]["result"])["error"]

    def test_dispatch_ok(self, chat_webui, monkeypatch):
        called = []

        def fake_dispatch(*a, **k):
            called.append(a)

        monkeypatch.setattr(chat_webui, "_dispatch_tool", fake_dispatch)
        tc = {"id": "tc1", "function": {"name": "tool", "arguments": "{}"}}
        chat_webui._tool_worker("t1", "s1", tc, None, 0, 0)
        assert called


class TestEventPost:
    def test_posts_to_queue(self, chat_webui):
        chat_webui._event_post("llm_ok", "t1", body={})
        ev_type, task_id, data = chat_webui._event_queue.get()
        assert ev_type == "llm_ok"
        assert task_id == "t1"
        assert data == {"body": {}}


# ---------------------------------------------------------------------------
# generate_image / edit_image
# ---------------------------------------------------------------------------


def _noop_sleep(chat_webui, monkeypatch):
    monkeypatch.setattr(chat_webui.time, "sleep", lambda *a, **k: None)


class TestGenerateImage:
    def _setup(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.tasks["t1"] = {"session_id": "s1", "_user": "alice", "status": "working"}
        monkeypatch.setattr(chat_webui, "unload_llama_model", lambda: None)
        monkeypatch.setattr(chat_webui, "ensure_comfyui_running", lambda: None)
        monkeypatch.setattr(chat_webui, "free_comfyui_vram", lambda: True)
        monkeypatch.setattr(chat_webui, "load_llama_model", lambda: True)
        _noop_sleep(chat_webui, monkeypatch)

    def test_comfyui_error(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        class Resp:
            def json(self):
                return {"error": "graph broken"}

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: Resp())
        result = json.loads(chat_webui.generate_image("cat", "t1"))
        assert "ComfyUI" in result["error"]
        assert chat_webui.tasks["t1"]["gen_prompt"] == "cat"

    def test_success(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        class PostResp:
            def json(self):
                return {"prompt_id": "p1"}

        class GetResp:
            def json(self):
                return {"p1": {"outputs": {"9": {"images": [{"filename": "cat.png", "subfolder": ""}]}}}}

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: PostResp())
        monkeypatch.setattr(chat_webui.requests, "get", lambda *a, **k: GetResp())
        result = json.loads(chat_webui.generate_image("cat", "t1"))
        assert result["file"] == os.path.join(chat_webui.COMFYUI_OUTPUT, "cat.png")
        assert chat_webui.tasks["t1"]["image_file"] == "cat.png"

    def test_poll_retries_on_error(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        class PostResp:
            def json(self):
                return {"prompt_id": "p1"}

        state = {"n": 0}

        def fake_get(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("conn error")
            class GetResp:
                def json(self):
                    return {"p1": {"outputs": {"9": {"images": [{"filename": "cat.png", "subfolder": ""}]}}}}
            return GetResp()

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: PostResp())
        monkeypatch.setattr(chat_webui.requests, "get", fake_get)
        result = json.loads(chat_webui.generate_image("cat", "t1"))
        assert result["file"] == os.path.join(chat_webui.COMFYUI_OUTPUT, "cat.png")

    def test_cancelled_deletes_file(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        chat_webui.tasks["t1"]["status"] = "cancelled"
        os.makedirs(chat_webui.COMFYUI_OUTPUT, exist_ok=True)
        fpath = os.path.join(chat_webui.COMFYUI_OUTPUT, "cat.png")
        with open(fpath, "w") as f:
            f.write("img")
        class PostResp:
            def json(self):
                return {"prompt_id": "p1"}

        class GetResp:
            def json(self):
                return {"p1": {"outputs": {"9": {"images": [{"filename": "cat.png", "subfolder": ""}]}}}}

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: PostResp())
        monkeypatch.setattr(chat_webui.requests, "get", lambda *a, **k: GetResp())
        result = json.loads(chat_webui.generate_image("cat", "t1"))
        assert "Cancelled" in result["error"]
        assert not os.path.exists(fpath)

    def test_cancelled_remove_error(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        chat_webui.tasks["t1"]["status"] = "cancelled"
        os.makedirs(chat_webui.COMFYUI_OUTPUT, exist_ok=True)
        fpath = os.path.join(chat_webui.COMFYUI_OUTPUT, "cat.png")
        with open(fpath, "w") as f:
            f.write("img")
        class PostResp:
            def json(self):
                return {"prompt_id": "p1"}

        class GetResp:
            def json(self):
                return {"p1": {"outputs": {"9": {"images": [{"filename": "cat.png", "subfolder": ""}]}}}}

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: PostResp())
        monkeypatch.setattr(chat_webui.requests, "get", lambda *a, **k: GetResp())
        monkeypatch.setattr(chat_webui.os, "remove", lambda p: (_ for _ in ()).throw(OSError("locked")))
        result = json.loads(chat_webui.generate_image("cat", "t1"))
        assert "Cancelled" in result["error"]

    def test_timeout(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        class PostResp:
            def json(self):
                return {"prompt_id": "p1"}

        class GetResp:
            def json(self):
                return {}

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: PostResp())
        monkeypatch.setattr(chat_webui.requests, "get", lambda *a, **k: GetResp())
        result = json.loads(chat_webui.generate_image("cat", "t1"))
        assert "timeout" in result["error"]

    def test_exception(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)

        def boom(*a, **k):
            raise RuntimeError("conn refused")

        monkeypatch.setattr(chat_webui.requests, "post", boom)
        result = json.loads(chat_webui.generate_image("cat", "t1"))
        assert result["error"] == "conn refused"

    def test_sd3_medium_workflow(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        monkeypatch.setattr(chat_webui, "IMAGE_MODELS", {
            "z_image": {"clip1": "c1", "vae": "v", "unet": "u"},
            "sd3_5_medium": {"clip1": "c1", "clip2": "c2", "t5": "t5", "vae": "v", "unet": "u"},
        })
        class PostResp:
            def json(self):
                return {"error": "x"}

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: PostResp())
        result = json.loads(chat_webui.generate_image("cat", "t1", model="sd3_5_medium"))
        assert "ComfyUI" in result["error"]

    def test_unknown_model_workflow_unbound(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        result = json.loads(chat_webui.generate_image("cat", "t1", model="foo"))
        assert "error" in result


class TestEditImage:
    def _setup(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.tasks["t1"] = {"session_id": "s1", "_user": "alice", "status": "working"}
        monkeypatch.setattr(chat_webui, "unload_llama_model", lambda: None)
        monkeypatch.setattr(chat_webui, "ensure_comfyui_running", lambda: None)
        monkeypatch.setattr(chat_webui, "free_comfyui_vram", lambda: True)
        monkeypatch.setattr(chat_webui, "load_llama_model", lambda: True)
        _noop_sleep(chat_webui, monkeypatch)

    def test_no_image(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        chat_webui.sessions.pop("s1", None)
        result = json.loads(chat_webui.edit_image("make red", "t1", "", sid="s1"))
        assert result["error"] == "No image provided for editing."

    def test_no_image_no_sid(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        result = json.loads(chat_webui.edit_image("make red", "t1", ""))
        assert result["error"] == "No image provided for editing."

    def _success_mocks(self, chat_webui, monkeypatch):
        class PostResp:
            def json(self):
                return {"prompt_id": "p1"}

        class GetResp:
            def json(self):
                return {"p1": {"outputs": {"9": {"images": [{"filename": "edited.png", "subfolder": ""}]}}}}

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: PostResp())
        monkeypatch.setattr(chat_webui.requests, "get", lambda *a, **k: GetResp())

    def test_success(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        self._success_mocks(chat_webui, monkeypatch)
        image_b64 = base64.b64encode(b"fake-image-bytes").decode()
        result = json.loads(chat_webui.edit_image("make red", "t1", image_b64))
        assert "file" in result

    def test_poll_retries_on_error(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        class PostResp:
            def json(self):
                return {"prompt_id": "p1"}

        state = {"n": 0}

        def fake_get(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("conn error")
            class GetResp:
                def json(self):
                    return {"p1": {"outputs": {"9": {"images": [{"filename": "edited.png", "subfolder": ""}]}}}}
            return GetResp()

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: PostResp())
        monkeypatch.setattr(chat_webui.requests, "get", fake_get)
        image_b64 = base64.b64encode(b"fake").decode()
        result = json.loads(chat_webui.edit_image("x", "t1", image_b64))
        assert "file" in result

    def test_cancelled(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        chat_webui.tasks["t1"]["status"] = "cancelled"
        os.makedirs(chat_webui.IMG_PATH, exist_ok=True)
        fpath = os.path.join(chat_webui.IMG_PATH, "edited.png")
        with open(fpath, "w") as f:
            f.write("x")
        self._success_mocks(chat_webui, monkeypatch)
        image_b64 = base64.b64encode(b"fake").decode()
        result = json.loads(chat_webui.edit_image("x", "t1", image_b64))
        assert "Cancelled" in result["error"]

    def test_cancelled_remove_error(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        chat_webui.tasks["t1"]["status"] = "cancelled"
        os.makedirs(chat_webui.IMG_PATH, exist_ok=True)
        fpath = os.path.join(chat_webui.IMG_PATH, "edited.png")
        with open(fpath, "w") as f:
            f.write("x")
        self._success_mocks(chat_webui, monkeypatch)
        monkeypatch.setattr(chat_webui.os, "remove", lambda p: (_ for _ in ()).throw(OSError("locked")))
        image_b64 = base64.b64encode(b"fake").decode()
        result = json.loads(chat_webui.edit_image("x", "t1", image_b64))
        assert "Cancelled" in result["error"]

    def test_timeout(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        class PostResp:
            def json(self):
                return {"prompt_id": "p1"}

        class GetResp:
            def json(self):
                return {}

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: PostResp())
        monkeypatch.setattr(chat_webui.requests, "get", lambda *a, **k: GetResp())
        image_b64 = base64.b64encode(b"fake").decode()
        result = json.loads(chat_webui.edit_image("x", "t1", image_b64))
        assert "timeout" in result["error"]

    def test_exception(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)

        def boom(*a, **k):
            raise RuntimeError("conn refused")

        monkeypatch.setattr(chat_webui.requests, "post", boom)
        image_b64 = base64.b64encode(b"fake").decode()
        result = json.loads(chat_webui.edit_image("x", "t1", image_b64))
        assert result["error"] == "conn refused"

    def test_comfyui_error(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        class PostResp:
            def json(self):
                return {"error": "boom"}

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: PostResp())
        image_b64 = base64.b64encode(b"fake").decode()
        result = json.loads(chat_webui.edit_image("x", "t1", image_b64))
        assert "ComfyUI" in result["error"]

    def test_extract_from_session_image(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        self._success_mocks(chat_webui, monkeypatch)
        os.makedirs(chat_webui.IMG_PATH, exist_ok=True)
        img_path = os.path.join(chat_webui.IMG_PATH, "alice.png")
        with open(img_path, "wb") as f:
            f.write(b"png-data")
        chat_webui.sessions.clear()
        chat_webui.sessions["s1"] = [{"role": "assistant", "_image_url": "/output/alice.png"}]
        result = json.loads(chat_webui.edit_image("x", "t1", "", sid="s1"))
        assert "file" in result

    def test_extract_from_message_content(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        self._success_mocks(chat_webui, monkeypatch)
        data_b64 = base64.b64encode(b"user-image").decode()
        chat_webui.sessions.clear()
        chat_webui.sessions["s1"] = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data_b64}"}}]}]
        result = json.loads(chat_webui.edit_image("x", "t1", "", sid="s1"))
        assert "file" in result

    def test_cleanup_error_handled(self, chat_webui, temp_paths, monkeypatch):
        self._setup(chat_webui, temp_paths, monkeypatch)
        class PostResp:
            def json(self):
                return {"error": "boom"}

        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: PostResp())

        def fake_remove(p):
            raise OSError("locked")

        monkeypatch.setattr(chat_webui.os, "remove", fake_remove)
        image_b64 = base64.b64encode(b"fake").decode()
        result = json.loads(chat_webui.edit_image("x", "t1", image_b64))
        assert "ComfyUI" in result["error"]


# ---------------------------------------------------------------------------
# _prepare_session / _start_llm_round
# ---------------------------------------------------------------------------


class TestPrepareSessionFull:
    def test_new_session_full(self, chat_webui, temp_paths, make_user, tmp_path, monkeypatch):
        ctx = str(tmp_path / "ctx" / "alice.txt")
        make_user({"alice": "secret"}, context_files={"alice": ctx})
        chat_webui.write_user_context("alice", "Loves cats")
        chat_webui.tasks["t1"] = {"_user": "alice"}
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.sessions_meta["s1"] = {"name": "New Chat", "system_prompts": [{"name": "Extra", "content": "extra stuff"}], "created": 1, "updated": 1}
        chat_webui.model_status = "unloaded"
        load_calls = []
        monkeypatch.setattr(chat_webui, "load_llama_model", lambda: load_calls.append(1))
        chat_webui.set_client_location("Kolkata")
        chat_webui._prepare_session("t1", "s1", "hello there", "aGVsbG8=", "YXVkaW8=", "2026-08-09T10:00:00Z")
        sys_msg = chat_webui.sessions["s1"][0]
        assert sys_msg["role"] == "system"
        assert "Kolkata" in sys_msg["content"]
        assert "Loves cats" in sys_msg["content"]
        assert "extra stuff" in sys_msg["content"]
        user_msg = chat_webui.sessions["s1"][-1]
        assert [p["type"] for p in user_msg["content"]] == ["image_url", "audio_url", "text"]
        assert chat_webui.sessions_meta["s1"]["name"] == "hello there"
        assert load_calls == [1]

    def test_existing_session_system_update(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.tasks["t1"] = {"_user": ""}
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.sessions["s1"] = [{"role": "system", "content": "old"}, {"role": "user", "content": "q"}]
        chat_webui.sessions_meta["s1"] = {"name": "Existing", "system_prompts": [], "created": 1, "updated": 1}
        chat_webui.model_status = "chat_loaded"
        chat_webui.set_client_location(None)
        chat_webui._prepare_session("t1", "s1", "new q", None, None, "2026-01-01T00:00:00+05:30")
        assert chat_webui.sessions["s1"][0]["role"] == "system"
        assert "old" not in chat_webui.sessions["s1"][0]["content"]
        assert "%current_time%" not in chat_webui.sessions["s1"][0]["content"]

    def test_existing_session_inserts_system(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.tasks["t1"] = {"_user": ""}
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.sessions["s1"] = [{"role": "user", "content": "q"}]
        chat_webui.sessions_meta["s1"] = {"name": "X", "system_prompts": [], "created": 1, "updated": 1}
        chat_webui.model_status = "chat_loaded"
        chat_webui._prepare_session("t1", "s1", "new q", None)
        assert chat_webui.sessions["s1"][0]["role"] == "system"

    def test_name_rename_new_chat(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.tasks["t1"] = {"_user": ""}
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.sessions["s1"] = [{"role": "system", "content": "s"}]
        chat_webui.sessions_meta["s1"] = {"name": "New Chat", "system_prompts": [], "created": 1, "updated": 1}
        chat_webui.model_status = "chat_loaded"
        long_msg = "This is a very long first message" * 3
        chat_webui._prepare_session("t1", "s1", long_msg, None)
        assert chat_webui.sessions_meta["s1"]["name"].endswith("...")

    def test_invalid_timestamp(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.tasks["t1"] = {"_user": ""}
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.model_status = "chat_loaded"
        chat_webui._prepare_session("t1", "s1", "hi", None, None, "garbage")
        assert chat_webui.sessions["s1"][0]["role"] == "system"


class TestStartLLMRound:
    def test_submits_and_sets_state(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.sessions.clear()
        chat_webui.sessions["s1"] = [{"role": "user", "content": "hi"}]
        chat_webui.tasks["t1"] = {"session_id": "s1"}
        submitted = []
        class FakePool:
            def submit(self, fn, *a, **k):
                submitted.append((fn, a, k))

        monkeypatch.setattr(chat_webui, "_llm_pool", FakePool())
        chat_webui.model_status = "chat_loaded"
        chat_webui._start_llm_round("t1", "s1", 0)
        assert chat_webui.tasks["t1"]["_state"] == "llm_waiting"
        assert chat_webui.tasks["t1"]["_round"] == 0
        assert submitted and submitted[0][0] is chat_webui._llm_worker

    def test_loads_model_if_needed(self, chat_webui, monkeypatch):
        chat_webui.tasks["t1"] = {"session_id": "s1"}
        chat_webui.model_status = "unloaded"
        load_calls = []
        monkeypatch.setattr(chat_webui, "load_llama_model", lambda: load_calls.append(1))
        class FakePool:
            def submit(self, fn, *a, **k):
                pass

        monkeypatch.setattr(chat_webui, "_llm_pool", FakePool())
        chat_webui._start_llm_round("t1", "s1", 1)
        assert load_calls

    def test_missing_task_returns(self, chat_webui, monkeypatch):
        chat_webui.tasks.pop("ghost", None)
        class FakePool:
            def submit(self, fn, *a, **k):
                raise AssertionError("should not submit")

        monkeypatch.setattr(chat_webui, "_llm_pool", FakePool())
        chat_webui._start_llm_round("ghost", "s1", 0)


# ---------------------------------------------------------------------------
# _llm_worker streaming
# ---------------------------------------------------------------------------


class TestLLMWorker:
    @staticmethod
    def _stream(lines):
        class Resp:
            encoding = "utf-8"

            def iter_lines(self, decode_unicode=False):
                return iter(lines)

        return Resp()

    def test_final_response(self, chat_webui, temp_paths, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        chat_webui.sessions.clear()
        chat_webui.sessions["s1"] = [{"role": "user", "content": "hi"}]
        chat_webui.tasks["t1"] = {"session_id": "s1", "reasoning": ""}
        lines = [
            'data: {"choices": [{"delta": {"reasoning_content": "think "}}]}',
            'data: {"choices": [{"delta": {"content": "hello"}}]}',
            'data: [DONE]',
        ]
        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: self._stream(lines))
        chat_webui._llm_worker("t1", "s1", 0, chat_webui.sessions["s1"])
        assert events and events[0][0][0] == "llm_ok"
        msg = events[0][1]["body"]["choices"][0]["message"]
        assert msg["content"] == "hello"
        assert msg["reasoning_content"] == "think "
        assert "tool_calls" not in msg

    def test_tool_calls_accumulated(self, chat_webui, temp_paths, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        chat_webui.sessions.clear()
        chat_webui.sessions["s1"] = [{"role": "user", "content": "hi"}]
        chat_webui.tasks["t1"] = {"session_id": "s1", "reasoning": ""}
        lines = [
            "",
            "event: keepalive",
            "data: not json",
            'data: {"foo": 1}',
            'data: {"choices": [{"delta": {"reasoning_content": "think "}}]}',
            'data: {"choices": [{"delta": {"content": "partial"}}]}',
            'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "web_search", "arguments": "{\\"query\\""}}]}}]}',
            'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ":\\"cats\\"}"}}]}}]}',
            'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "web_search"}}]}}]}',
            'data: [DONE]',
            'data: {"choices": [{"delta": {"content": "ignored"}}]}',
        ]
        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: self._stream(lines))
        chat_webui._llm_worker("t1", "s1", 0, chat_webui.sessions["s1"])
        msg = events[0][1]["body"]["choices"][0]["message"]
        assert msg["content"] == "partial"
        assert msg["reasoning_content"] == "think "
        assert msg["tool_calls"][0]["id"] == "call_1"
        assert msg["tool_calls"][0]["function"]["name"] == "web_search"
        assert msg["tool_calls"][0]["function"]["arguments"] == '{"query":"cats"}'
        assert "ignored" not in msg["content"]

    def test_context_compression_triggered(self, chat_webui, temp_paths, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        monkeypatch.setattr(chat_webui, "_summarize_with_llm", lambda text: "compressed")
        chat_webui.sessions.clear()
        chat_webui.sessions["s1"] = [{"role": "user", "content": "x" * 50000} for _ in range(30)]
        chat_webui.tasks["t1"] = {"session_id": "s1", "reasoning": ""}
        lines = ['data: {"choices": [{"delta": {"content": "ok"}}]}', "data: [DONE]"]
        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: self._stream(lines))
        chat_webui._llm_worker("t1", "s1", 0, chat_webui.sessions["s1"])
        assert events and events[0][0][0] == "llm_ok"

    def test_error_handling(self, chat_webui, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))

        def boom(*a, **k):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(chat_webui.requests, "post", boom)
        chat_webui._llm_worker("t1", "s1", 0, [{"role": "user", "content": "x"}])
        assert events[0][0][0] == "llm_err"
        assert events[0][1]["error"] == "connection reset"

    def test_error_image_message(self, chat_webui, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))

        def boom(*a, **k):
            raise RuntimeError("This model does not support image input")

        monkeypatch.setattr(chat_webui.requests, "post", boom)
        chat_webui._llm_worker("t1", "s1", 0, [{"role": "user", "content": "x"}])
        assert "vision-capable" in events[0][1]["error"]

    def test_tool_message_in_context(self, chat_webui, temp_paths, monkeypatch):
        events = []
        chat_webui._event_post = lambda *a, **k: events.append((a, k))
        monkeypatch.setattr(
            chat_webui, "prepare_context_for_llm",
            lambda sid, msgs: [{"role": "user", "content": "q"}, {"role": "tool", "tool_call_id": "c1", "content": "res"}],
        )
        chat_webui.sessions.clear()
        chat_webui.sessions["s1"] = [{"role": "user", "content": "hi"}]
        chat_webui.tasks["t1"] = {"session_id": "s1", "reasoning": ""}
        lines = ['data: {"choices": [{"delta": {"content": "ok"}}]}', "data: [DONE]"]
        monkeypatch.setattr(chat_webui.requests, "post", lambda *a, **k: self._stream(lines))
        chat_webui._llm_worker("t1", "s1", 0, chat_webui.sessions["s1"])
        assert events and events[0][0][0] == "llm_ok"


# ---------------------------------------------------------------------------
# _event_loop
# ---------------------------------------------------------------------------


class TestEventLoop:
    def _run(self, chat_webui, monkeypatch, events):
        class ScriptedQueue:
            def __init__(self):
                self.events = list(events)
                self.i = 0

            def get(self):
                if self.i >= len(self.events):
                    raise StopIteration
                ev = self.events[self.i]
                self.i += 1
                return ev

            def put(self, *a, **k):
                pass

        monkeypatch.setattr(chat_webui, "_event_queue", ScriptedQueue())
        class FakePool:
            def submit(self, fn, *a, **k):
                pass

        monkeypatch.setattr(chat_webui, "_llm_pool", FakePool())
        monkeypatch.setattr(chat_webui, "_tool_pool", FakePool())

        def _target():
            try:
                chat_webui._event_loop()
            except StopIteration:
                pass

        th = threading.Thread(target=_target)
        th.daemon = True
        th.start()
        th.join(timeout=10)
        assert not th.is_alive()

    def test_full_flow(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.model_status = "chat_loaded"
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.tasks["t1"] = {"status": "queued", "session_id": "s1"}
        ev = [
            ("start", "t1", {"sid": "s1", "message": "hello", "image": None, "audio": None, "user": "alice", "client_timestamp": None}),
            ("llm_ok", "t1", {"sid": "s1", "round": 0, "body": {"choices": [{"message": {"content": "partial", "reasoning_content": "r", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]}}]}}),
            ("tool_ok", "t1", {"sid": "s1", "tc_id": "c1", "result": "{}", "round": 0, "tool_index": 0}),
            ("llm_ok", "t1", {"sid": "s1", "round": 1, "body": {"choices": [{"message": {"content": "final answer", "reasoning_content": ""}}]}}),
        ]
        self._run(chat_webui, monkeypatch, ev)
        assert chat_webui.tasks["t1"]["status"] == "done"
        assert chat_webui.sessions["s1"][-1]["content"] == "final answer"
        # assistant message with the tool call was stored too
        assert any(m.get("tool_calls") for m in chat_webui.sessions["s1"])

    def test_llm_err(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.sessions["s1"] = []
        chat_webui.sessions_meta["s1"] = {"name": "N", "user_id": "a", "created": 1, "updated": 1, "system_prompts": []}
        chat_webui.tasks["t1"] = {"status": "working", "_state": "llm_waiting", "session_id": "s1"}
        ev = [("llm_err", "t1", {"sid": "s1", "error": "boom", "round": 0})]
        self._run(chat_webui, monkeypatch, ev)
        assert chat_webui.tasks["t1"]["status"] == "error"

    def test_tool_err(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.sessions["s1"] = []
        chat_webui.sessions_meta["s1"] = {"name": "N", "user_id": "a", "created": 1, "updated": 1, "system_prompts": []}
        chat_webui.tasks["t1"] = {"status": "working", "_state": "tools_running", "_pending_tools": 1, "session_id": "s1"}
        ev = [("tool_err", "t1", {"sid": "s1", "tc_id": "c1", "result": "{}", "round": 0, "error": "x"})]
        self._run(chat_webui, monkeypatch, ev)
        assert chat_webui.tasks["t1"]["_round"] == 1
        assert chat_webui.tasks["t1"]["_state"] == "llm_waiting"

    def test_tool_ok_round_limit(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.sessions["s1"] = []
        chat_webui.sessions_meta["s1"] = {"name": "N", "user_id": "a", "created": 1, "updated": 1, "system_prompts": []}
        chat_webui.tasks["t1"] = {"status": "working", "_state": "tools_running", "_pending_tools": 1, "session_id": "s1"}
        ev = [("tool_ok", "t1", {"sid": "s1", "tc_id": "c1", "result": "{}", "round": 9, "tool_index": 0})]
        self._run(chat_webui, monkeypatch, ev)
        assert chat_webui.tasks["t1"]["status"] == "error"

    def test_llm_ok_wrong_state(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.tasks["t1"] = {"status": "working", "_state": "tools_running", "session_id": "s1"}
        ev = [("llm_ok", "t1", {"sid": "s1", "round": 0, "body": {"choices": [{"message": {}}]}})]
        self._run(chat_webui, monkeypatch, ev)
        assert chat_webui.tasks["t1"]["_state"] == "tools_running"

    def test_llm_err_wrong_state(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.tasks["t1"] = {"status": "working", "_state": "tools_running", "session_id": "s1"}
        ev = [("llm_err", "t1", {"sid": "s1", "error": "boom", "round": 0})]
        self._run(chat_webui, monkeypatch, ev)
        assert chat_webui.tasks["t1"]["status"] == "working"

    def test_tool_ok_done_task(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.tasks["t1"] = {"status": "done", "_pending_tools": 1}
        ev = [("tool_ok", "t1", {"sid": "s1", "tc_id": "c1", "result": "{}", "round": 0, "tool_index": 0})]
        self._run(chat_webui, monkeypatch, ev)

    def test_tool_err_done_task(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.tasks["t1"] = {"status": "done", "_pending_tools": 1}
        ev = [("tool_err", "t1", {"sid": "s1", "tc_id": "c1", "result": "{}", "round": 0, "error": "x"})]
        self._run(chat_webui, monkeypatch, ev)

    def test_tool_err_round_limit(self, chat_webui, temp_paths, monkeypatch):
        chat_webui.sessions.clear()
        chat_webui.sessions_meta.clear()
        chat_webui.sessions["s1"] = []
        chat_webui.sessions_meta["s1"] = {"name": "N", "user_id": "a", "created": 1, "updated": 1, "system_prompts": []}
        chat_webui.tasks["t1"] = {"status": "working", "_state": "tools_running", "_pending_tools": 1, "session_id": "s1"}
        ev = [("tool_err", "t1", {"sid": "s1", "tc_id": "c1", "result": "{}", "round": 10, "error": "x"})]
        self._run(chat_webui, monkeypatch, ev)
        assert chat_webui.tasks["t1"]["status"] == "error"

    def test_cancelled_task(self, chat_webui, temp_paths, monkeypatch):
        os.makedirs(chat_webui.IMG_PATH, exist_ok=True)
        fpath = os.path.join(chat_webui.IMG_PATH, "del.png")
        with open(fpath, "w") as f:
            f.write("x")
        chat_webui.tasks["t1"] = {"status": "cancelled", "image_file": "del.png"}
        ev = [("start", "t1", {"sid": "s1", "message": "x", "image": None, "audio": None, "user": "", "client_timestamp": None})]
        self._run(chat_webui, monkeypatch, ev)
        assert not os.path.exists(fpath)

    def test_unknown_task(self, chat_webui, temp_paths, monkeypatch):
        ev = [("start", "ghost", {"sid": "s1", "message": "x", "image": None, "audio": None, "user": "", "client_timestamp": None})]
        self._run(chat_webui, monkeypatch, ev)


# ---------------------------------------------------------------------------
# _queue_worker
# ---------------------------------------------------------------------------


class TestQueueWorker:
    @staticmethod
    def _fake_cond(chat_webui):
        class FakeCond:
            def __init__(self):
                self.empty_waits = 0

            def wait(self, timeout=None):
                if timeout is None:
                    self.empty_waits += 1
                    if self.empty_waits >= 2:
                        raise StopIteration
                else:
                    chat_webui._overheated = False
                    chat_webui._ram_evacuating = False
                return True

            def notify(self):
                pass

            def notify_all(self):
                pass

        return FakeCond()

    def test_normal_flow(self, chat_webui, temp_paths, monkeypatch):
        posted = []
        monkeypatch.setattr(chat_webui, "_event_post", lambda *a, **k: posted.append((a, k)))
        chat_webui._task_queue[:] = []
        chat_webui.tasks["t1"] = {"status": "working", "session_id": "s1", "_original_message": "hi"}
        chat_webui._task_queue.append({"task_id": "t1", "session_id": "s1", "message": "hi", "user": "alice"})
        monkeypatch.setattr(chat_webui, "_queue_cond", self._fake_cond(chat_webui))

        def fake_sleep(secs):
            with chat_webui._data_lock:
                chat_webui.tasks["t1"]["status"] = "done"

        monkeypatch.setattr(chat_webui.time, "sleep", fake_sleep)
        with pytest.raises(StopIteration):
            chat_webui._queue_worker()
        assert posted and posted[0][0][0] == "start"
        assert posted[0][1]["user"] == "alice"

    def test_overheated_waiting(self, chat_webui, temp_paths, monkeypatch):
        posted = []
        monkeypatch.setattr(chat_webui, "_event_post", lambda *a, **k: posted.append((a, k)))
        chat_webui._task_queue[:] = []
        chat_webui._overheated = True
        chat_webui.tasks["t1"] = {"status": "queued", "session_id": "s1"}
        chat_webui.tasks["t2"] = {"status": "queued", "session_id": "s2"}
        chat_webui._task_queue.append({"task_id": "t1", "session_id": "s1", "message": "a", "user": "u"})
        chat_webui._task_queue.append({"task_id": "t2", "session_id": "s2", "message": "b", "user": "u"})
        monkeypatch.setattr(chat_webui, "_queue_cond", self._fake_cond(chat_webui))

        def fake_sleep(secs):
            with chat_webui._data_lock:
                chat_webui.tasks["t1"]["status"] = "done"
                chat_webui.tasks["t2"]["status"] = "done"

        monkeypatch.setattr(chat_webui.time, "sleep", fake_sleep)
        with pytest.raises(StopIteration):
            chat_webui._queue_worker()
        assert len(posted) == 2
        assert chat_webui.tasks["t1"]["status"] == "done"

    def test_ram_evacuating_waiting(self, chat_webui, temp_paths, monkeypatch):
        posted = []
        monkeypatch.setattr(chat_webui, "_event_post", lambda *a, **k: posted.append((a, k)))
        chat_webui._task_queue[:] = []
        chat_webui._ram_evacuating = True
        chat_webui.tasks["t1"] = {"status": "queued", "session_id": "s1"}
        chat_webui._task_queue.append({"task_id": "t1", "session_id": "s1", "message": "a", "user": "u"})
        monkeypatch.setattr(chat_webui, "_queue_cond", self._fake_cond(chat_webui))

        def fake_sleep(secs):
            with chat_webui._data_lock:
                chat_webui.tasks["t1"]["status"] = "done"

        monkeypatch.setattr(chat_webui.time, "sleep", fake_sleep)
        with pytest.raises(StopIteration):
            chat_webui._queue_worker()
        assert len(posted) == 1


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------


class TestIdleUnloadLoop:
    def test_unloads_after_idle(self, chat_webui, monkeypatch):
        unloaded = []
        monkeypatch.setattr(chat_webui, "unload_llama_model", lambda: unloaded.append(1))
        calls = []

        def fake_sleep(secs):
            calls.append(secs)
            if len(calls) >= 2:
                raise StopIteration

        monkeypatch.setattr(chat_webui.time, "sleep", fake_sleep)
        chat_webui.model_status = "chat_loaded"
        old_llm_use = chat_webui._last_llm_use
        chat_webui._last_llm_use = 0
        chat_webui._task_queue[:] = []
        chat_webui._current_task_id = None
        try:
            with pytest.raises(StopIteration):
                chat_webui._idle_unload_loop()
        finally:
            chat_webui._last_llm_use = old_llm_use
        assert unloaded

    def test_no_unload_when_not_loaded(self, chat_webui, monkeypatch):
        unloaded = []
        monkeypatch.setattr(chat_webui, "unload_llama_model", lambda: unloaded.append(1))
        calls = []

        def fake_sleep(secs):
            calls.append(secs)
            if len(calls) >= 2:
                raise StopIteration

        monkeypatch.setattr(chat_webui.time, "sleep", fake_sleep)
        chat_webui.model_status = "unloaded"
        old_llm_use = chat_webui._last_llm_use
        chat_webui._last_llm_use = 0
        chat_webui._task_queue[:] = []
        chat_webui._current_task_id = None
        try:
            with pytest.raises(StopIteration):
                chat_webui._idle_unload_loop()
        finally:
            chat_webui._last_llm_use = old_llm_use
        assert not unloaded

    def test_no_unload_when_queue_active(self, chat_webui, monkeypatch):
        unloaded = []
        monkeypatch.setattr(chat_webui, "unload_llama_model", lambda: unloaded.append(1))
        calls = []

        def fake_sleep(secs):
            calls.append(secs)
            if len(calls) >= 2:
                raise StopIteration

        monkeypatch.setattr(chat_webui.time, "sleep", fake_sleep)
        chat_webui.model_status = "chat_loaded"
        old_llm_use = chat_webui._last_llm_use
        chat_webui._last_llm_use = 0
        chat_webui._task_queue[:] = []
        chat_webui._current_task_id = "t1"
        try:
            with pytest.raises(StopIteration):
                chat_webui._idle_unload_loop()
        finally:
            chat_webui._last_llm_use = old_llm_use
        assert not unloaded


class TestReminderLoop:
    def test_processes_due(self, chat_webui, monkeypatch):
        db_run = []
        monkeypatch.setattr(chat_webui, "_db_fetch", lambda q, params=(): [{"id": "x", "title": "T", "user_id": "u"}])
        monkeypatch.setattr(chat_webui, "_db_run", lambda q, params=(): db_run.append((q, params)) or 1)

        def fake_sleep(secs):
            raise StopIteration

        monkeypatch.setattr(chat_webui.time, "sleep", fake_sleep)
        with pytest.raises(StopIteration):
            chat_webui._reminder_loop()
        assert db_run and db_run[0][1] == ("x",)

    def test_error_path(self, chat_webui, monkeypatch):
        def boom(q, params=()):
            raise RuntimeError("db down")

        monkeypatch.setattr(chat_webui, "_db_fetch", boom)

        def fake_sleep(secs):
            raise StopIteration

        monkeypatch.setattr(chat_webui.time, "sleep", fake_sleep)
        with pytest.raises(StopIteration):
            chat_webui._reminder_loop()


class TestEvacuateRam:
    def _stubs(self, chat_webui, monkeypatch, ram_values=None):
        killed = []
        monkeypatch.setattr(chat_webui, "kill_llama_server", lambda: killed.append("llama"))
        monkeypatch.setattr(chat_webui, "kill_comfyui", lambda: killed.append("comfy"))
        restarted = []
        monkeypatch.setattr(chat_webui, "restart_servers", lambda: restarted.append(1))
        if ram_values is not None:
            values = iter(list(ram_values))
            monkeypatch.setattr(chat_webui, "get_ram_usage", lambda: next(values))
        monkeypatch.setattr(chat_webui.time, "sleep", lambda *a, **k: None)
        return killed, restarted

    def test_requeues_current_task(self, chat_webui, monkeypatch):
        killed, restarted = self._stubs(chat_webui, monkeypatch, ram_values=[40])
        chat_webui._current_task_id = "t1"
        chat_webui.tasks["t1"] = {"session_id": "s1", "status": "working", "_original_message": "hi", "_original_image": None}
        chat_webui._task_queue[:] = []
        old_flag = chat_webui._ram_evacuating
        try:
            chat_webui._evacuate_ram()
        finally:
            chat_webui._ram_evacuating = old_flag
        assert chat_webui.tasks["t1"]["status"] == "error"
        assert len(chat_webui._task_queue) == 1
        assert chat_webui._task_queue[0]["task_id"] == "t1"
        assert killed == ["llama", "comfy"]
        assert restarted == [1]
        assert chat_webui._ram_evacuating is False

    def test_no_current_task(self, chat_webui, monkeypatch):
        killed, restarted = self._stubs(chat_webui, monkeypatch, ram_values=[40])
        chat_webui._current_task_id = None
        old_flag = chat_webui._ram_evacuating
        try:
            chat_webui._evacuate_ram()
        finally:
            chat_webui._ram_evacuating = old_flag
        assert killed == ["llama", "comfy"]
        assert restarted == [1]

    def test_done_task_not_requeued(self, chat_webui, monkeypatch):
        killed, restarted = self._stubs(chat_webui, monkeypatch, ram_values=[40])
        chat_webui._current_task_id = "t1"
        chat_webui.tasks["t1"] = {"session_id": "s1", "status": "done", "_original_message": "hi"}
        chat_webui._task_queue[:] = []
        old_flag = chat_webui._ram_evacuating
        try:
            chat_webui._evacuate_ram()
        finally:
            chat_webui._ram_evacuating = old_flag
        assert chat_webui._task_queue == []

    def test_ram_wait_none(self, chat_webui, monkeypatch):
        killed, restarted = self._stubs(chat_webui, monkeypatch, ram_values=[None, 40])
        chat_webui._current_task_id = None
        old_flag = chat_webui._ram_evacuating
        try:
            chat_webui._evacuate_ram()
        finally:
            chat_webui._ram_evacuating = old_flag
        assert restarted == [1]


class TestThermalMonitor:
    def _stop_after(self, n):
        calls = []

        def fake_sleep(secs):
            calls.append(secs)
            if len(calls) >= n:
                raise StopIteration

        return fake_sleep

    def test_overheat_and_cooldown(self, chat_webui, monkeypatch):
        unloaded = []
        monkeypatch.setattr(chat_webui, "unload_llama_model", lambda: unloaded.append(1))
        monkeypatch.setattr(chat_webui, "get_ram_usage", lambda: 30)
        temps = iter([90, 60, None])

        def fake_temp():
            try:
                return next(temps)
            except StopIteration:
                return None

        monkeypatch.setattr(chat_webui, "get_gpu_temp", fake_temp)
        calls = []

        def fake_sleep(secs):
            calls.append(secs)
            if len(calls) >= 3:
                raise StopIteration

        monkeypatch.setattr(chat_webui.time, "sleep", fake_sleep)
        chat_webui._overheated = False
        chat_webui.model_status = "chat_loaded"
        chat_webui._current_task_id = None
        chat_webui._ram_evacuating = False
        try:
            with pytest.raises(StopIteration):
                chat_webui._thermal_monitor()
        finally:
            chat_webui._overheated = False
            chat_webui._gpu_temp = None
        assert unloaded == [1]
        assert chat_webui._overheated is False

    def test_image_active_frees_vram(self, chat_webui, monkeypatch):
        freed = []
        monkeypatch.setattr(chat_webui, "free_comfyui_vram", lambda: freed.append(1))
        monkeypatch.setattr(chat_webui, "get_ram_usage", lambda: 30)
        monkeypatch.setattr(chat_webui, "get_gpu_temp", lambda: 90)

        monkeypatch.setattr(chat_webui.time, "sleep", self._stop_after(2))
        chat_webui._overheated = True
        chat_webui.model_status = "image_active"
        chat_webui._current_task_id = None
        chat_webui._ram_evacuating = False
        try:
            with pytest.raises(StopIteration):
                chat_webui._thermal_monitor()
        finally:
            chat_webui._overheated = False
            chat_webui._gpu_temp = None
        assert freed == [1]

    def test_busy_skips_actions(self, chat_webui, monkeypatch):
        unloaded = []
        freed = []
        monkeypatch.setattr(chat_webui, "unload_llama_model", lambda: unloaded.append(1))
        monkeypatch.setattr(chat_webui, "free_comfyui_vram", lambda: freed.append(1))
        monkeypatch.setattr(chat_webui, "get_ram_usage", lambda: 30)
        monkeypatch.setattr(chat_webui, "get_gpu_temp", lambda: 90)

        monkeypatch.setattr(chat_webui.time, "sleep", self._stop_after(2))
        chat_webui._overheated = True
        chat_webui.model_status = "chat_loaded"
        chat_webui._current_task_id = "t1"
        chat_webui._ram_evacuating = False
        try:
            with pytest.raises(StopIteration):
                chat_webui._thermal_monitor()
        finally:
            chat_webui._overheated = False
            chat_webui._gpu_temp = None
        assert unloaded == [] and freed == []

    def test_ram_evacuation_triggered(self, chat_webui, monkeypatch):
        evacuated = []
        monkeypatch.setattr(chat_webui, "_evacuate_ram", lambda: evacuated.append(1))
        monkeypatch.setattr(chat_webui, "get_ram_usage", lambda: 99)
        monkeypatch.setattr(chat_webui, "get_gpu_temp", lambda: 40)

        monkeypatch.setattr(chat_webui.time, "sleep", self._stop_after(2))
        chat_webui._overheated = False
        chat_webui._ram_evacuating = False
        try:
            with pytest.raises(StopIteration):
                chat_webui._thermal_monitor()
        finally:
            chat_webui._overheated = False
            chat_webui._gpu_temp = None
        assert evacuated == [1]

    def test_ram_evacuating_skips(self, chat_webui, monkeypatch):
        evacuated = []
        monkeypatch.setattr(chat_webui, "_evacuate_ram", lambda: evacuated.append(1))
        monkeypatch.setattr(chat_webui, "get_ram_usage", lambda: 99)
        monkeypatch.setattr(chat_webui, "get_gpu_temp", lambda: 40)

        monkeypatch.setattr(chat_webui.time, "sleep", self._stop_after(2))
        chat_webui._overheated = False
        chat_webui._ram_evacuating = True
        try:
            with pytest.raises(StopIteration):
                chat_webui._thermal_monitor()
        finally:
            chat_webui._ram_evacuating = False
        assert evacuated == []

    def test_none_temp_resets_overheat(self, chat_webui, monkeypatch):
        monkeypatch.setattr(chat_webui, "get_ram_usage", lambda: 30)
        monkeypatch.setattr(chat_webui, "get_gpu_temp", lambda: None)

        def fake_sleep(secs):
            raise StopIteration

        monkeypatch.setattr(chat_webui.time, "sleep", fake_sleep)
        chat_webui._overheated = True
        chat_webui._ram_evacuating = False
        try:
            with pytest.raises(StopIteration):
                chat_webui._thermal_monitor()
        finally:
            chat_webui._overheated = False
        assert chat_webui._overheated is False
