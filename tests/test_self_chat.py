import base64
import json
import os

import pytest


class FakeResp:
    def __init__(self, payload=None, status=200):
        self._payload = payload or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class TestModuleLoad:
    def test_import_success(self, self_chat):
        assert self_chat.PASSWORD == "test-pass"
        assert self_chat.keep_sessions is False
        assert self_chat.args.dry_run is False

    def test_tasks_loaded_from_default_file(self, self_chat):
        assert isinstance(self_chat.TASKS, list)
        assert len(self_chat.TASKS) > 0
        assert isinstance(self_chat.GENRE_CHECKLISTS, dict)
        assert self_chat.STARTING_CONVERSATION


class TestParseTasks:
    def test_basic(self, self_chat):
        items = [{"task": "Write a story", "genre": "Drama", "details": "d"}]
        tasks = self_chat._parse_tasks(items)
        assert tasks[0]["task"] == "Write a story"
        assert tasks[0]["genre"] == "Drama"
        assert tasks[0]["details"] == "d"
        assert tasks[0]["languages"] == ["English"]
        assert tasks[0]["mediums"] == ["image", "text"]
        assert tasks[0]["roles"] == ["free"]
        assert tasks[0]["checklist"] == {}

    def test_skips_empty_tasks(self, self_chat):
        tasks = self_chat._parse_tasks([{"task": "  "}, {"task": None}, {"task": "real"}])
        assert [t["task"] for t in tasks] == ["real"]

    def test_string_fields_split(self, self_chat):
        tasks = self_chat._parse_tasks([
            {"task": "T", "languages": "bengali, english", "mediums": "image,audio", "roles": "free,premium"}
        ])
        assert tasks[0]["languages"] == ["bengali", "english"]
        assert tasks[0]["mediums"] == ["image", "audio"]
        assert tasks[0]["roles"] == ["free", "premium"]

    def test_genre_defaults_to_general(self, self_chat):
        tasks = self_chat._parse_tasks([{"task": "T"}])
        assert tasks[0]["genre"] == "General"


class TestLoadConfigFile:
    def test_missing_file(self, self_chat):
        assert self_chat.load_config_file("/nonexistent/tasks.json") == ([], {})

    def test_invalid_json_exits(self, self_chat, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not valid json")
        with pytest.raises(SystemExit) as ei:
            self_chat.load_config_file(str(p))
        assert ei.value.code == 1

    def test_dict_with_tasks_and_checklists(self, self_chat, tmp_path):
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps({
            "tasks": [{"task": "One"}],
            "genre_checklists": {"Drama": {"editor": ["Check A"]}},
        }))
        tasks, checklists = self_chat.load_config_file(str(p))
        assert [t["task"] for t in tasks] == ["One"]
        assert checklists == {"Drama": {"editor": ["Check A"]}}

    def test_plain_list(self, self_chat, tmp_path):
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps([{"task": "One"}, {"task": "Two"}]))
        tasks, checklists = self_chat.load_config_file(str(p))
        assert [t["task"] for t in tasks] == ["One", "Two"]
        assert checklists == {}


class TestLoadTasks:
    def test_default_file(self, self_chat, monkeypatch, tmp_path):
        p = tmp_path / "tasks.json"
        p.write_text(json.dumps([{"task": "Alpha"}, {"task": "Beta"}]))
        monkeypatch.setattr(self_chat, "DEFAULT_TASKS_FILE", str(p))
        monkeypatch.setattr(self_chat.args, "config", "")
        monkeypatch.setattr(self_chat.args, "defaults", False)
        tasks, source, checklists = self_chat.load_tasks()
        assert [t["task"] for t in tasks] == ["Alpha", "Beta"]
        assert source == str(p)
        assert checklists == {}

    def test_config_plus_defaults_dedup(self, self_chat, monkeypatch, tmp_path):
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps([{"task": "Shared"}, {"task": "OnlyConfig"}]))
        defaults = tmp_path / "defaults.json"
        defaults.write_text(json.dumps([{"task": "Shared"}, {"task": "OnlyDefault"}]))
        monkeypatch.setattr(self_chat, "DEFAULT_TASKS_FILE", str(defaults))
        monkeypatch.setattr(self_chat.args, "config", str(cfg))
        monkeypatch.setattr(self_chat.args, "defaults", True)
        tasks, source, _ = self_chat.load_tasks()
        names = [t["task"] for t in tasks]
        assert names == ["Shared", "OnlyConfig", "OnlyDefault"]
        assert "defaults" in source


class TestChecklistFor:
    def _setup(self, self_chat, monkeypatch):
        monkeypatch.setattr(self_chat, "GENRE_CHECKLISTS", {
            "Drama": {"editor": ["Genre editor check"], "moderator": ["Genre mod check"]},
            "default": {"editor": ["Default editor check"]},
        })

    def test_task_checklist_wins(self, self_chat, monkeypatch):
        self._setup(self_chat, monkeypatch)
        out = self_chat.checklist_for("Drama", "editor", {"editor": ["Task check"]})
        assert out == "- Task check"

    def test_genre_checklist(self, self_chat, monkeypatch):
        self._setup(self_chat, monkeypatch)
        out = self_chat.checklist_for("Drama", "moderator")
        assert out == "- Genre mod check"

    def test_default_checklist(self, self_chat, monkeypatch):
        self._setup(self_chat, monkeypatch)
        out = self_chat.checklist_for("UnknownGenre", "editor")
        assert out == "- Default editor check"

    def test_no_checklist_fallback(self, self_chat, monkeypatch):
        self._setup(self_chat, monkeypatch)
        out = self_chat.checklist_for("UnknownGenre", "moderator")
        assert "No genre-specific checks" in out


class TestCheckLanguageScript:
    def test_unmapped_language_passes(self, self_chat):
        assert self_chat.check_language_script("any text at all", "English") is True
        assert self_chat.check_language_script("any", "esoteric") is True

    def test_empty_body_fails(self, self_chat):
        assert self_chat.check_language_script("", "bengali") is False

    def test_bengali_text_passes(self, self_chat):
        text = "আমি বাংলায় একটি সুন্দর গল্প লিখছি।"
        assert self_chat.check_language_script(text, "bengali") is True

    def test_wrong_script_fails(self, self_chat):
        text = "This is written in English for testing."
        assert self_chat.check_language_script(text, "bengali") is False

    def test_ignores_small_blocks(self, self_chat):
        text = "<small>English header</small> বাংলা বাংলা বাংলা বাংলা বাংলা"
        assert self_chat.check_language_script(text, "bengali") is True


class TestVerifyTaskFulfillment:
    ORIGINAL = (
        "**Task prompt:** Write a mystery\n\n"
        "**Genre:** Mystery\n\n"
        "**Mediums:** image , text\n\n"
        "**Language(s):** English\n\n"
        "## Citations & References\n"
    )

    def test_all_good(self, self_chat):
        check = (
            self.ORIGINAL
            + "\nThe detective walked in. ![scene](img1.png)\n"
        )
        problems = self_chat.verify_task_fulfillment(
            self.ORIGINAL, check, ["image", "text"], "English"
        )
        assert problems == []

    def test_audio_declared(self, self_chat):
        problems = self_chat.verify_task_fulfillment(
            self.ORIGINAL, self.ORIGINAL, ["audio", "text"], "English"
        )
        assert any("audio" in p for p in problems)

    def test_image_declared_but_missing(self, self_chat):
        problems = self_chat.verify_task_fulfillment(
            self.ORIGINAL, self.ORIGINAL, ["image", "text"], "English"
        )
        assert any("no image is embedded" in p for p in problems)

    def test_dropped_header_field(self, self_chat):
        check = self.ORIGINAL.replace("**Genre:** Mystery", "Genre removed")
        problems = self_chat.verify_task_fulfillment(
            self.ORIGINAL, check, ["text"], "English"
        )
        assert any("Genre" in p and "header" in p for p in problems)

    def test_dropped_citations_section(self, self_chat):
        check = self.ORIGINAL.replace("## Citations & References", "No citations here")
        problems = self_chat.verify_task_fulfillment(
            self.ORIGINAL, check, ["text"], "English"
        )
        assert any("Citations & References" in p for p in problems)

    def test_wrong_language(self, self_chat):
        check = self.ORIGINAL + "\nAll English prose here.\n"
        problems = self_chat.verify_task_fulfillment(
            self.ORIGINAL, check, ["text"], "bengali"
        )
        assert any("language" in p.lower() for p in problems)

    def test_prohibited_name(self, self_chat):
        check = self.ORIGINAL + "\nKaya walked into the room.\n"
        problems = self_chat.verify_task_fulfillment(
            self.ORIGINAL, check, ["text"], "English"
        )
        assert any("Prohibited name" in p for p in problems)

    def test_editor_flag(self, self_chat):
        check = self.ORIGINAL + "\n<!-- EDITOR FLAG: chapter 3 is missing -->\n"
        problems = self_chat.verify_task_fulfillment(
            self.ORIGINAL, check, ["text"], "English"
        )
        assert any("chapter 3 is missing" in p for p in problems)


class TestIsDuplicate:
    def test_no_previous(self, self_chat):
        assert self_chat.is_duplicate("text", "") is False

    def test_identical(self, self_chat):
        assert self_chat.is_duplicate("Same text", "Same text") is True

    def test_different(self, self_chat):
        assert self_chat.is_duplicate("Alpha story", "Beta story") is False


class TestSlugify:
    def test_basic(self, self_chat):
        assert self_chat.slugify("Hello World") == "Hello-World"

    def test_illegal_chars_removed(self, self_chat):
        assert self_chat.slugify("a/b\\c:d") == "abcd"

    def test_truncates(self, self_chat):
        assert len(self_chat.slugify("x" * 200)) <= 60

    def test_empty_falls_back(self, self_chat):
        assert self_chat.slugify("///:::") == "story"
        assert self_chat.slugify("") == "story"


class TestSanitizeTitle:
    def test_strips_quotes(self, self_chat):
        assert self_chat.sanitize_title('"The Great Escape"') == "The Great Escape"

    def test_strips_prefix(self, self_chat):
        assert self_chat.sanitize_title("Title: The Story") == "The Story"

    def test_strips_numbering(self, self_chat):
        assert self_chat.sanitize_title("1. First Option") == "First Option"

    def test_strips_trailing_punct(self, self_chat):
        assert self_chat.sanitize_title("A Title!") == "A Title"

    def test_empty(self, self_chat):
        assert self_chat.sanitize_title("") is None
        assert self_chat.sanitize_title(None) is None

    def test_whitespace_only_crashes(self, self_chat):
        with pytest.raises(IndexError):
            self_chat.sanitize_title("   ")

    def test_truncates(self, self_chat):
        assert len(self_chat.sanitize_title("y" * 200)) <= 80


class TestCleanSpeakerText:
    def test_strips_name_prefix(self, self_chat):
        assert self_chat.clean_speaker_text("Kolpo", "Kolpo: Hello there") == "Hello there"

    def test_strips_speaker_prefix(self, self_chat):
        assert self_chat.clean_speaker_text("Kaya", "kaya: Hi") == "Hi"

    def test_removes_next_turn(self, self_chat):
        out = self_chat.clean_speaker_text("Kolpo", "Text [NEXT TURN: A does x] more")
        assert "NEXT TURN" not in out
        assert "Text" in out

    def test_removes_action_tags(self, self_chat):
        out = self_chat.clean_speaker_text("Kolpo", "Do stuff [ACTION: run] now")
        assert "ACTION" not in out

    def test_removes_end_conversation(self, self_chat):
        assert self_chat.clean_speaker_text("Kolpo", "Bye [END CONVERSATION]") == "Bye"


class TestScrubAgentNames:
    def test_vocative_removed(self, self_chat):
        out = self_chat.scrub_agent_names("Hi Kaya, let's begin")
        assert "Kaya" not in out
        assert "Hi" in out

    def test_bare_mention_removed(self, self_chat):
        out = self_chat.scrub_agent_names("I told Kolpo everything")
        assert "Kolpo" not in out

    def test_protects_image_alt(self, self_chat):
        out = self_chat.scrub_agent_names("Kaya: scene ![Kaya](img.png)")
        assert "![Kaya](img.png)" in out

    def test_protects_small_blocks(self, self_chat):
        text = "Some text <small>_Round 1 · Kaya Turn 2_</small>"
        out = self_chat.scrub_agent_names(text)
        assert "<small>_Round 1 · Kaya Turn 2_</small>" in out


class TestNormalizeMarkdownLines:
    def test_reinserts_structural_breaks(self, self_chat):
        text = "Intro <small style=x>h</small>## Chapter 1 ![img](a.png)"
        out = self_chat.normalize_markdown_lines(text)
        assert "\n\n<small " in out
        assert "\n\n## Chapter 1" in out
        assert "\n\n![img](a.png)" in out

    def test_trailing_newline(self, self_chat):
        out = self_chat.normalize_markdown_lines("Just a line")
        assert out.endswith("\n")


class TestStripModelCitations:
    def test_removes_citations_block(self, self_chat):
        text = "Some story\n\n## Citations & References\n\n1. [x](url)"
        assert self_chat.strip_model_citations(text) == "Some story"

    def test_handles_hash_variants(self, self_chat):
        text = "### Citations & References\n\n1. [x](url)"
        assert self_chat.strip_model_citations(text) == ""

    def test_bare_references_heading_kept(self, self_chat):
        text = "### References\n\n1. [x](url)"
        assert self_chat.strip_model_citations(text) == "### References\n\n1. [x](url)"

    def test_keeps_text_without_citations(self, self_chat):
        assert self_chat.strip_model_citations("Just a story.") == "Just a story."


class TestExtractMarkdownFence:
    def test_markdown_fence(self, self_chat):
        text = "prefix\n```markdown\n# Hi\n```\nsuffix"
        assert self_chat.extract_markdown_fence(text) == "# Hi"

    def test_md_fence(self, self_chat):
        assert self_chat.extract_markdown_fence("```md\nhello\n```") == "hello"

    def test_no_fence(self, self_chat):
        assert self_chat.extract_markdown_fence("  plain text  ") == "plain text"


class TestCollectCitations:
    def test_collects_unique(self, self_chat):
        searches = [
            {"query": "q1", "results": [
                {"url": "http://a", "title": "T1"},
                {"url": "http://a", "title": "T1 dup"},
                {"url": ""},
            ]},
            "not a dict",
            {"query": "q2", "results": [{"url": "http://b", "title": "T2"}]},
        ]
        citations = {}
        self_chat.collect_citations(citations, searches)
        assert citations["http://a"] == ("T1", "q1")
        assert citations["http://b"] == ("T2", "q2")
        assert len(citations) == 2


class TestStoryImagesInOrder:
    def test_ordered_and_deduped(self, self_chat, tmp_path):
        (tmp_path / "b.png").write_bytes(b"B")
        (tmp_path / "a.png").write_bytes(b"A")
        md = "![first](b.png) ![second](missing.png) ![again](b.png) ![later](a.png)"
        result = self_chat.story_images_in_order(str(tmp_path), md)
        assert [fname for fname, _ in result] == ["b.png", "a.png"]
        assert os.path.isfile(result[0][1])


class TestBuildInput:
    def test_phase1(self, self_chat):
        out = self_chat.build_input("A", 1, "", "bengali", "Task X")
        assert "PHASE 1: ALIGNMENT" in out
        assert "You are responding as Kolpo" in out
        assert "Your partner is Kaya" in out

    def test_phase2(self, self_chat):
        out = self_chat.build_input("B", 3, "", "english", "Task X")
        assert "PHASE 2: DIRECT EXECUTION" in out
        assert "Speak in english" in out

    def test_phase3(self, self_chat):
        out = self_chat.build_input("A", self_chat.MAX_MESSAGES_PER_AGENT - 2, "", "english", "Task X")
        assert "PHASE 3: FINALIZATION" in out
        assert "[END CONVERSATION]" in out

    def test_phase2_mid_conversation(self, self_chat):
        mid = max(3, self_chat.MAX_MESSAGES_PER_AGENT // 2)
        out = self_chat.build_input("A", mid, "", "english", "Task X")
        assert "PHASE 2: DIRECT EXECUTION" in out

    def test_phase3_boundary(self, self_chat):
        boundary = self_chat.MAX_MESSAGES_PER_AGENT - 3
        out = self_chat.build_input("A", boundary, "", "english", "Task X")
        assert "PHASE 2: DIRECT EXECUTION" in out

    def test_incoming_included(self, self_chat):
        out = self_chat.build_input("A", 3, "previous reply", "english", "Task X")
        assert "previous reply" in out


class TestStartStory:
    def test_creates_folder_and_header(self, self_chat, monkeypatch, tmp_path):
        base = tmp_path / "stories"
        monkeypatch.setattr(self_chat, "STORY_BASE_DIR", str(base))
        stories_dir, fname = self_chat.start_story(
            1, "My Task", "My Task", ["image", "text"], "English", ["free"], "Drama"
        )
        assert os.path.isfile(fname)
        with open(fname) as f:
            content = f.read()
        assert content.startswith("# My Task")
        assert "**Task prompt:** My Task" in content
        assert "**Genre:** Drama" in content
        assert "**For roles:** free" in content
        assert "**Mediums:** image , text" in content
        assert os.path.isdir(stories_dir)


class TestApplyTitle:
    def test_renames_folder(self, self_chat, monkeypatch, tmp_path):
        base = tmp_path / "stories"
        monkeypatch.setattr(self_chat, "STORY_BASE_DIR", str(base))
        stories_dir, fname = self_chat.start_story(
            2, "Old Title", "Old Title", ["text"], "English", ["free"], "Drama"
        )
        old_dir = stories_dir
        new_dir, new_fname = self_chat.apply_title("Brand New Title", stories_dir, fname)
        assert new_dir != old_dir
        assert os.path.isdir(new_dir)
        assert os.path.exists(new_fname)
        with open(new_fname) as f:
            assert f.readline().strip() == "# Brand New Title"

    def test_no_timestamp_keeps_place(self, self_chat, monkeypatch, tmp_path):
        base = tmp_path / "stories"
        monkeypatch.setattr(self_chat, "STORY_BASE_DIR", str(base))
        fname = tmp_path / "plain.md"
        fname.write_text("# Old\n")
        new_dir, new_fname = self_chat.apply_title("New", str(tmp_path), str(fname))
        assert new_fname == str(fname)
        with open(new_fname) as f:
            assert f.readline().strip() == "# New"


class TestAppendStoryEntry:
    def test_appends_turn_and_collects_citations(self, self_chat, tmp_path):
        fname = str(tmp_path / "story.md")
        with open(fname, "w") as f:
            f.write("# Title\n")
        citations = {}
        entry = {
            "speaker": "Kolpo",
            "message": 1,
            "text": "Kolpo: Hello world\n[END CONVERSATION]",
            "image": None,
            "searches": [{"query": "q", "results": [{"url": "http://a", "title": "A"}]}],
        }
        self_chat.append_story_entry(entry, fname, citations, str(tmp_path), 1, 0)
        with open(fname) as f:
            content = f.read()
        assert "_Round 1 · Kolpo Turn 1_" in content
        assert "Hello world" in content
        assert "[END CONVERSATION]" not in content
        assert citations["http://a"] == ("A", "q")


class TestFinalizeStory:
    def test_appends_citations(self, self_chat, tmp_path):
        fname = str(tmp_path / "story.md")
        fname = fname.replace(".md", "_20260808_123456.md")
        with open(fname, "w") as f:
            f.write("# Story\n")
        self_chat.finalize_story(fname, {"http://a": ("Title A", "query")})
        with open(fname) as f:
            content = f.read()
        assert "## Citations & References" in content
        assert "[Title A](http://a)" in content
        assert "*(source: query)*" in content

    def test_no_citations_no_change(self, self_chat, tmp_path):
        fname = str(tmp_path / "story.md")
        with open(fname, "w") as f:
            f.write("# Story\n")
        self_chat.finalize_story(fname, {})
        with open(fname) as f:
            assert f.read() == "# Story\n"


class TestSaveTranscript:
    def test_writes_json(self, self_chat, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        fname = self_chat.save_transcript([{"speaker": "A"}], 3)
        assert os.path.isfile(fname)
        assert fname.startswith("conv_r3_")
        with open(fname) as f:
            assert json.load(f) == [{"speaker": "A"}]


class TestImageUrlToB64:
    def test_empty(self, self_chat):
        assert self_chat.image_url_to_b64("") is None
        assert self_chat.image_url_to_b64(None) is None

    def test_missing_file(self, self_chat):
        assert self_chat.image_url_to_b64("/output/user/does_not_exist.png") is None

    def test_encodes_existing_file(self, self_chat, monkeypatch, tmp_path):
        img_dir = tmp_path / "out"
        os.makedirs(img_dir / "user", exist_ok=True)
        raw = b"\x89PNG fake"
        with open(img_dir / "user" / "x.png", "wb") as f:
            f.write(raw)
        monkeypatch.setattr(self_chat.os.path, "expanduser", lambda p: str(img_dir))
        assert self_chat.image_url_to_b64("/output/user/x.png") == base64.b64encode(raw).decode()


class TestLoginAndSessions:
    def test_login(self, self_chat, monkeypatch):
        calls = {}

        def fake_post(*a, **k):
            calls["post"] = (a, k)
            return FakeResp({"token": "T"})

        monkeypatch.setattr(self_chat.requests, "post", fake_post)
        token = self_chat.login("alice", "secret")
        assert token == "T"
        args, kwargs = calls["post"]
        assert args[0] == "http://localhost/api/login"
        assert kwargs["json"] == {"username": "alice", "password": "secret"}

    def test_create_session(self, self_chat, monkeypatch):
        calls = {}

        def fake_post(*a, **k):
            calls["post"] = (a, k)
            return FakeResp({"session_id": "S"})

        monkeypatch.setattr(self_chat.requests, "post", fake_post)
        sid = self_chat.create_session("tok", "Chat", system_prompts=[{"name": "N", "content": "c"}], context_tokens={"%genre%": "adult"})
        assert sid == "S"
        args, kwargs = calls["post"]
        assert args[0] == "http://localhost/api/sessions"
        assert kwargs["headers"] == {"X-Auth-Token": "tok"}
        assert kwargs["json"]["name"] == "Chat"
        assert kwargs["json"]["system_prompts"] == [{"name": "N", "content": "c"}]
        assert kwargs["json"]["context_tokens"] == {"%genre%": "adult"}

    def test_create_session_no_context_tokens(self, self_chat, monkeypatch):
        calls = {}

        def fake_post(*a, **k):
            calls["post"] = (a, k)
            return FakeResp({"session_id": "S"})

        monkeypatch.setattr(self_chat.requests, "post", fake_post)
        self_chat.create_session("tok", "Chat")
        args, kwargs = calls["post"]
        assert "context_tokens" not in kwargs["json"]

    def test_delete_session_success(self, self_chat, monkeypatch):
        monkeypatch.setattr(self_chat.requests, "delete", lambda *a, **k: FakeResp(status=200))
        assert self_chat.delete_session("tok", "S") is True

    def test_delete_session_failure(self, self_chat, monkeypatch):
        monkeypatch.setattr(self_chat.requests, "delete", lambda *a, **k: FakeResp(status=404))
        assert self_chat.delete_session("tok", "S") is False


class TestActiveUsers:
    def test_returns_users(self, self_chat, monkeypatch):
        monkeypatch.setattr(self_chat.requests, "get", lambda *a, **k: FakeResp({"users": ["alice"]}))
        assert self_chat.active_real_users() == ["alice"]

    def test_error_returns_empty(self, self_chat, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("offline")
        monkeypatch.setattr(self_chat.requests, "get", boom)
        assert self_chat.active_real_users() == []


class TestRegisterAgentTokens:
    def test_posts_tokens(self, self_chat, monkeypatch):
        calls = {}
        monkeypatch.setattr(self_chat.requests, "post", lambda *a, **k: calls.setdefault("post", (a, k)) or FakeResp({"ok": True}))
        self_chat.register_agent_tokens(["t1"], ["kolpo"])
        args, kwargs = calls["post"]
        assert args[0] == "http://localhost/api/register-agent"
        assert kwargs["json"] == {"tokens": ["t1"], "usernames": ["kolpo"]}

    def test_error_is_swallowed(self, self_chat, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("offline")
        monkeypatch.setattr(self_chat.requests, "post", boom)
        self_chat.register_agent_tokens(["t1"])  # must not raise


class TestDryRun:
    def test_prints_plan(self, self_chat, monkeypatch, capsys):
        monkeypatch.setattr(self_chat, "TASKS", [
            {"task": "Write a mystery", "genre": "Drama", "languages": ["English"],
             "mediums": ["image"], "roles": ["free"], "details": "keep it short", "checklist": {}},
        ])
        monkeypatch.setattr(self_chat, "TASKS_SOURCE", "/tmp/opencode/tasks.json")
        self_chat.run_dry_run()
        out = capsys.readouterr().out
        assert "DRY RUN — 1 task(s)" in out
        assert "Write a mystery" in out
        assert "ENVIRONMENT" in out
        assert "genre:       Drama" in out
