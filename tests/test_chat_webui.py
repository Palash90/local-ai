import json
import os
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
