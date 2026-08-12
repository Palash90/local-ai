import base64
import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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

    def test_path_parsed(self, self_chat):
        tasks = self_chat._parse_tasks([{"task": "T", "path": "/custom/dir"}])
        assert tasks[0]["path"] == "/custom/dir"

    def test_path_defaults_to_none(self, self_chat):
        tasks = self_chat._parse_tasks([{"task": "T"}])
        assert tasks[0]["path"] is None

    def test_blank_path_defaults_to_none(self, self_chat):
        tasks = self_chat._parse_tasks([{"task": "T", "path": "   "}])
        assert tasks[0]["path"] is None


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
            "tasks": [{"task": "One", "path": "/custom"}],
            "genre_checklists": {"Drama": {"editor": ["Check A"]}},
        }))
        tasks, checklists = self_chat.load_config_file(str(p))
        assert [t["task"] for t in tasks] == ["One"]
        assert tasks[0]["path"] == "/custom"
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

    def test_custom_path_used(self, self_chat, monkeypatch, tmp_path):
        base = tmp_path / "stories"
        custom = tmp_path / "custom_stories"
        monkeypatch.setattr(self_chat, "STORY_BASE_DIR", str(base))
        stories_dir, fname = self_chat.start_story(
            1, "My Task", "My Task", ["image", "text"], "English", ["free"], "Drama", str(custom)
        )
        assert os.path.isfile(fname)
        assert str(custom) in stories_dir
        assert str(custom) in fname
        assert not os.path.exists(base)

    def test_defaults_to_base_dir(self, self_chat, monkeypatch, tmp_path):
        base = tmp_path / "stories"
        monkeypatch.setattr(self_chat, "STORY_BASE_DIR", str(base))
        stories_dir, fname = self_chat.start_story(
            1, "My Task", "My Task", ["text"], "English", ["free"], "Drama"
        )
        assert os.path.isfile(fname)
        assert str(base) in stories_dir
        assert str(base) in fname


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
        assert args[0] == "http://localhost:3001/api/login"
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
        assert args[0] == "http://localhost:3001/api/sessions"
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
        assert args[0] == "http://localhost:3001/api/register-agent"
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
             "mediums": ["image"], "roles": ["free"], "details": "keep it short",
             "checklist": {}, "path": "/custom/dir"},
        ])
        monkeypatch.setattr(self_chat, "TASKS_SOURCE", "/tmp/opencode/tasks.json")
        self_chat.run_dry_run()
        out = capsys.readouterr().out
        assert "DRY RUN — 1 task(s)" in out
        assert "Write a mystery" in out
        assert "ENVIRONMENT" in out
        assert "genre:       Drama" in out
        assert "path:        /custom/dir" in out

    def test_script_enforcement_and_missing_files(self, self_chat, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(self_chat, "TASKS", [
            {"task": "Bengali mystery", "genre": "Mystery", "languages": ["bengali", "English"],
             "mediums": ["audio", "text"], "roles": ["free", "premium"], "details": "x",
             "checklist": {"editor": ["Check A"]}},
        ])
        monkeypatch.setattr(self_chat, "TASKS_SOURCE", "/tmp/opencode/tasks.json")
        monkeypatch.setattr(self_chat, "GENRE_CHECKLISTS_FILE", str(tmp_path / "missing_gc.json"))
        monkeypatch.setattr(self_chat, "SELF_CHAT_PROMPT_FILE", str(tmp_path / "missing_self.txt"))
        unhandled = tmp_path / "editor.txt"
        unhandled.write_text("%unknown% placeholder\n")
        monkeypatch.setattr(self_chat, "EDITOR_PROMPT_FILE", str(unhandled))
        handled = tmp_path / "moderator.txt"
        handled.write_text("%genre% %checklist% rest\n")
        monkeypatch.setattr(self_chat, "MODERATOR_PROMPT_FILE", str(handled))
        self_chat.run_dry_run()
        out = capsys.readouterr().out
        assert "script enforcement active (bengali/hindi)" in out
        assert "no audio tool exists" in out
        assert "MISSING — falling back to empty checklists" in out
        assert "MISSING" in out
        assert "UNHANDLED placeholders" in out
        assert "ok (2 placeholder" in out


class TestLoadGenreChecklists:
    def test_missing_file_returns_empty(self, self_chat, monkeypatch, capsys):
        monkeypatch.setattr(self_chat, "GENRE_CHECKLISTS_FILE", "/nonexistent/genre.json")
        assert self_chat.load_genre_checklists() == {}
        assert "Could not load" in capsys.readouterr().out

    def test_merges_extra(self, self_chat, monkeypatch, tmp_path):
        p = tmp_path / "gc.json"
        p.write_text(json.dumps({"Drama": {"editor": ["A"]}}))
        monkeypatch.setattr(self_chat, "GENRE_CHECKLISTS_FILE", str(p))
        out = self_chat.load_genre_checklists({"Extra": {"editor": ["B"]}})
        assert out["Drama"]["editor"] == ["A"]
        assert out["Extra"]["editor"] == ["B"]


class TestWaitForUserToLeave:
    def test_is_noop(self, self_chat):
        assert self_chat.wait_for_user_to_leave() is None


class TestCallLlm:
    def _post_get(self, self_chat, monkeypatch, post_payload, get_payloads):
        sleeps = []
        monkeypatch.setattr(self_chat.time, "sleep", lambda s: sleeps.append(s))

        def fake_post(*a, **k):
            return FakeResp(post_payload)

        seq = iter(get_payloads)
        monkeypatch.setattr(self_chat.requests, "post", fake_post)
        monkeypatch.setattr(self_chat.requests, "get", lambda *a, **k: FakeResp(next(seq)))
        return sleeps

    def test_success(self, self_chat, monkeypatch):
        self._post_get(self_chat, monkeypatch, {"task_id": "T1"},
                       [{"status": "done", "response": "hello", "image": "/i.png",
                         "_search_details": [{"q": 1}]}])
        out = self_chat.call_llm("tok", "S", "msg", image_b64="B64")
        assert out == {"text": "hello", "image": "/i.png", "searches": [{"q": 1}]}

    def test_polls_until_done(self, self_chat, monkeypatch):
        sleeps = self._post_get(self_chat, monkeypatch, {"task_id": "T1"},
                                [{"status": "working"}, {"status": "done", "response": "x"}])
        out = self_chat.call_llm("tok", "S", "msg")
        assert out["text"] == "x"
        assert sleeps == [10.0]

    def test_error_status_raises(self, self_chat, monkeypatch):
        self._post_get(self_chat, monkeypatch, {"task_id": "T1"}, [{"status": "error", "why": "bad"}])
        with pytest.raises(RuntimeError, match="Task failed"):
            self_chat.call_llm("tok", "S", "msg")

    def test_submit_http_error(self, self_chat, monkeypatch):
        self._post_get(self_chat, monkeypatch, None, [])
        monkeypatch.setattr(self_chat.requests, "post", lambda *a, **k: FakeResp(status=400))
        with pytest.raises(RuntimeError):
            self_chat.call_llm("tok", "S", "msg")

    def test_status_http_error(self, self_chat, monkeypatch):
        monkeypatch.setattr(self_chat.requests, "post", lambda *a, **k: FakeResp({"task_id": "T1"}))
        monkeypatch.setattr(self_chat.requests, "get", lambda *a, **k: FakeResp(status=500))
        with pytest.raises(RuntimeError):
            self_chat.call_llm("tok", "S", "msg")


class TestProposeTitle:
    def test_success(self, self_chat, monkeypatch):
        monkeypatch.setattr(self_chat, "call_llm", lambda *a, **k: {"text": "My Great Title"})
        assert self_chat.propose_title("tok", "S", "Task", "English", "Drama", "story...") == "My Great Title"

    def test_empty_falls_back_to_task(self, self_chat, monkeypatch):
        monkeypatch.setattr(self_chat, "call_llm", lambda *a, **k: {"text": ""})
        assert self_chat.propose_title("tok", "S", "Task X", "English", "Drama", "s") == "Task X"

    def test_exception_falls_back(self, self_chat, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("offline")
        monkeypatch.setattr(self_chat, "call_llm", boom)
        assert self_chat.propose_title("tok", "S", "Task Y", "English", "Drama", "s") == "Task Y"


class TestEmbedStoryImage:
    def _comfy(self, tmp_path, rel="user/pic.png", data=b"imgdata"):
        root = tmp_path / "comfy"
        full = root / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        return root

    def test_success_copies(self, self_chat, monkeypatch, tmp_path):
        root = self._comfy(tmp_path)
        monkeypatch.setattr(self_chat.os.path, "expanduser", lambda p: str(root))
        stories = tmp_path / "stories"
        stories.mkdir()
        name = self_chat.embed_story_image("/output/user/pic.png", str(stories), 1, "Kolpo", 0)
        assert name == "img_r1_Kolpo_0.png"
        assert (stories / name).read_bytes() == b"imgdata"

    def test_missing_file_returns_none(self, self_chat, monkeypatch, tmp_path):
        monkeypatch.setattr(self_chat.os.path, "expanduser", lambda p: str(tmp_path))
        assert self_chat.embed_story_image("/output/user/nope.png", str(tmp_path), 1, "A", 0) is None

    def test_copy_error_returns_none(self, self_chat, monkeypatch, tmp_path):
        root = self._comfy(tmp_path)
        monkeypatch.setattr(self_chat.os.path, "expanduser", lambda p: str(root))

        def boom(src, dst):
            raise OSError("denied")
        monkeypatch.setattr(self_chat.shutil, "copy", boom)
        assert self_chat.embed_story_image("/output/user/pic.png", str(tmp_path), 1, "A", 0) is None


class TestFileToB64:
    def test_encodes(self, self_chat, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"\x00\x01\xff")
        assert self_chat.file_to_b64(str(p)) == base64.b64encode(b"\x00\x01\xff").decode()


class TestApplyTitleNoHeading:
    def test_inserts_heading_when_missing(self, self_chat, tmp_path):
        fname = tmp_path / "plain.md"
        fname.write_text("Body without heading\n")
        new_dir, new_fname = self_chat.apply_title("Fresh", str(tmp_path), str(fname))
        assert new_fname == str(fname)
        with open(new_fname) as f:
            assert f.readline().strip() == "# Fresh"


class TestAppendStoryEntryImage:
    def test_appends_embedded_image(self, self_chat, monkeypatch, tmp_path):
        root = tmp_path / "comfy"
        (root / "user").mkdir(parents=True)
        (root / "user" / "pic.png").write_bytes(b"P")
        monkeypatch.setattr(self_chat.os.path, "expanduser", lambda p: str(root))
        fname = str(tmp_path / "story.md")
        with open(fname, "w") as f:
            f.write("# Title\n")
        entry = {"speaker": "Kolpo", "message": 2, "text": "hi", "image": "/output/user/pic.png", "searches": None}
        self_chat.append_story_entry(entry, fname, {}, str(tmp_path), 1, 0)
        content = open(fname).read()
        assert "![Kolpo](img_r1_Kolpo_0.png)" in content
        assert os.path.isfile(str(tmp_path / "img_r1_Kolpo_0.png"))


class TestRunSingleConversation:
    def _setup(self, self_chat, monkeypatch, tmp_path, script):
        monkeypatch.setattr(self_chat, "STORY_BASE_DIR", str(tmp_path / "stories"))
        monkeypatch.setattr(self_chat, "create_session", lambda token, name, **k: f"sid-{name}")
        monkeypatch.setattr(self_chat, "wait_for_user_to_leave", lambda: None)
        monkeypatch.setattr(self_chat.time, "sleep", lambda s: None)
        monkeypatch.setattr(self_chat.random, "sample", lambda seq, k: list(seq)[:k])
        monkeypatch.setattr(self_chat.random, "choice", lambda seq: seq[0])
        monkeypatch.setattr(self_chat, "image_url_to_b64", lambda url: "b64data" if url else None)
        monkeypatch.setattr(self_chat, "propose_title", lambda *a, **k: "Mocked Title")
        monkeypatch.setattr(self_chat, "run_editor", lambda *a, **k: None)
        moderated = []
        monkeypatch.setattr(self_chat, "run_moderator", lambda *a, **k: moderated.append(1) or {"verdict": "GREEN"})

        calls = []

        def fake_llm(token, session_id, message, image_b64=None):
            calls.append({"token": token, "session": session_id, "message": message, "image": image_b64})
            return script[len(calls) - 1]

        monkeypatch.setattr(self_chat, "call_llm", fake_llm)
        return calls, moderated

    def _run(self, self_chat, mediums=("text",), languages=("English",)):
        return self_chat.run_single_conversation(
            "ta", "tb", 1, "Task", list(mediums), list(languages), ["free"], "Drama", "", {}
        )

    def test_happy_path_with_image_and_searches(self, self_chat, monkeypatch, tmp_path):
        script = [
            {"text": "Plan set.", "image": None, "searches": None},
            {"text": "Scene attached.",
             "image": "/output/user/scene.png",
             "searches": [{"query": "kitten facts", "results": [
                 {"url": "http://k", "title": "Kitten Wiki", "snippet": "All about kittens"}]}, "garbage"]},
            {"text": "Story complete. [END CONVERSATION]", "image": None, "searches": None},
        ]
        calls, moderated = self._setup(self_chat, monkeypatch, tmp_path, script)

        def fake_editor(stories_dir, fname, task, genre, **k):
            ed = fname.replace(".md", ".edited.md")
            with open(fname, encoding="utf-8") as f:
                content = f.read()
            with open(ed, "w", encoding="utf-8") as f:
                f.write(content + "\n\nMinor polish.\n")
            return ed

        monkeypatch.setattr(self_chat, "run_editor", fake_editor)
        transcript, sa, sb, fname = self._run(self_chat)
        assert len(transcript) == 3
        assert moderated == [1]
        assert calls[2]["image"] == "b64data"
        assert "WEB SEARCH REPORTS SHARED" in calls[2]["message"]
        assert "Kitten Wiki" in calls[2]["message"]
        assert os.path.isfile(fname)

    def test_red_auto_verdict(self, self_chat, monkeypatch, tmp_path):
        script = [{"text": "Done. [END CONVERSATION]", "image": None, "searches": None}]
        calls, moderated = self._setup(self_chat, monkeypatch, tmp_path, script)
        transcript, *_ = self._run(self_chat, mediums=("audio", "text"))
        assert moderated == []
        verdicts = []
        for root, _dirs, files in os.walk(tmp_path / "stories"):
            for fn in files:
                if fn.endswith(".moderation.json"):
                    verdicts.append(os.path.join(root, fn))
        assert verdicts
        with open(verdicts[0]) as f:
            assert json.load(f)["verdict"] == "RED"

    def test_empty_reply_retry_succeeds(self, self_chat, monkeypatch, tmp_path):
        script = [
            {"text": "", "image": None, "searches": None},
            {"text": "Retry content", "image": None, "searches": None},
            {"text": "B final. [END CONVERSATION]", "image": None, "searches": None},
        ]
        calls, _ = self._setup(self_chat, monkeypatch, tmp_path, script)
        transcript, *_ = self._run(self_chat)
        assert len(transcript) == 2
        assert "SYSTEM ERROR: Your previous output was empty" in calls[1]["message"]

    def test_double_empty_aborts(self, self_chat, monkeypatch, tmp_path):
        script = [
            {"text": "", "image": None, "searches": None},
            {"text": "", "image": None, "searches": None},
        ]
        calls, _ = self._setup(self_chat, monkeypatch, tmp_path, script)
        transcript, *_ = self._run(self_chat)
        assert transcript == []

    def test_duplicate_retry_and_message_cap(self, self_chat, monkeypatch, tmp_path):
        words = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet "
                 "kilo lima mike november oscar papa quebec romeo sierra tango "
                 "uniform victor whiskey xray yankee zulu").split()
        replies = ["One", "Two", "Two", "Two revised"] + words
        script = [{"text": r, "image": None, "searches": None} for r in replies]
        calls, _ = self._setup(self_chat, monkeypatch, tmp_path, script)
        transcript, *_ = self._run(self_chat)
        assert len(transcript) == 29
        assert "identical to your partner" in calls[3]["message"]

    def test_custom_path_used(self, self_chat, monkeypatch, tmp_path):
        script = [{"text": "Done. [END CONVERSATION]", "image": None, "searches": None}]
        self._setup(self_chat, monkeypatch, tmp_path, script)
        custom = tmp_path / "custom_stories"
        _transcript, _sa, _sb, fname = self_chat.run_single_conversation(
            "ta", "tb", 1, "Task", ["text"], ["English"], ["free"], "Drama", "", {}, str(custom)
        )
        assert os.path.isfile(fname)
        assert str(custom) in fname
        assert not os.path.isdir(str(tmp_path / "stories"))


class TestRunEditor:
    def _stubs(self, self_chat, monkeypatch, tmp_path):
        monkeypatch.setattr(self_chat, "login", lambda u, p: "tok")
        monkeypatch.setattr(self_chat, "register_agent_tokens", lambda *a, **k: None)
        monkeypatch.setattr(self_chat, "delete_session", lambda *a, **k: True)
        prompt = tmp_path / "editor_prompt.txt"
        prompt.write_text("Edit: %genre% %mediums% %language% %details% %checklist%")
        monkeypatch.setattr(self_chat, "EDITOR_PROMPT_FILE", str(prompt))
        monkeypatch.setattr(self_chat, "create_session", lambda *a, **k: "sid")

    def _story(self, tmp_path, with_image=False):
        fname = tmp_path / "story_r1_20260811_123456.md"
        content = "# Story\n\n**Task prompt:** t\n"
        if with_image:
            (tmp_path / "img.png").write_bytes(b"IMG")
            content += "\n![scene](img.png)\n"
        fname.write_text(content)
        return fname

    def test_success_with_image(self, self_chat, monkeypatch, tmp_path):
        self._stubs(self_chat, monkeypatch, tmp_path)
        fname = self._story(tmp_path, with_image=True)
        results = iter([
            {"text": "seen it"},
            {"text": "```markdown\nRevised story\n```"},
        ])
        monkeypatch.setattr(self_chat, "call_llm", lambda *a, **k: next(results))
        out = self_chat.run_editor(str(tmp_path), str(fname), "t", "Drama", mediums=["text"],
                                   language="English", details="d", checklist={"editor": ["C"]})
        assert out == str(fname).replace(".md", ".edited.md")
        assert os.path.isfile(out)
        assert "Revised story" in open(out).read()

    def test_login_failure(self, self_chat, monkeypatch, tmp_path):
        def boom(u, p):
            raise RuntimeError("auth")
        monkeypatch.setattr(self_chat, "login", boom)
        out = self_chat.run_editor(str(tmp_path), "x.md", "t", "Drama")
        assert out is None

    def test_prompt_read_error(self, self_chat, monkeypatch, tmp_path):
        self._stubs(self_chat, monkeypatch, tmp_path)
        monkeypatch.setattr(self_chat, "EDITOR_PROMPT_FILE", str(tmp_path / "missing.txt"))
        assert self_chat.run_editor(str(tmp_path), "x.md", "t", "Drama") is None

    def test_empty_revision(self, self_chat, monkeypatch, tmp_path):
        self._stubs(self_chat, monkeypatch, tmp_path)
        fname = self._story(tmp_path)
        monkeypatch.setattr(self_chat, "call_llm", lambda *a, **k: {"text": "```markdown\n\n```"})
        assert self_chat.run_editor(str(tmp_path), str(fname), "t", "Drama") is None

    def test_phase_exception(self, self_chat, monkeypatch, tmp_path):
        deleted = []
        self._stubs(self_chat, monkeypatch, tmp_path)
        monkeypatch.setattr(self_chat, "delete_session", lambda *a, **k: deleted.append(a) or True)
        fname = self._story(tmp_path)

        def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(self_chat, "call_llm", boom)
        assert self_chat.run_editor(str(tmp_path), str(fname), "t", "Drama") is None
        assert deleted == [("tok", "sid")]


class TestRunModerator:
    def _stubs(self, self_chat, monkeypatch, tmp_path):
        monkeypatch.setattr(self_chat, "login", lambda u, p: "tok")
        monkeypatch.setattr(self_chat, "register_agent_tokens", lambda *a, **k: None)
        monkeypatch.setattr(self_chat, "delete_session", lambda *a, **k: True)
        prompt = tmp_path / "moderator_prompt.txt"
        prompt.write_text("Moderate: %genre% %checklist%")
        monkeypatch.setattr(self_chat, "MODERATOR_PROMPT_FILE", str(prompt))
        monkeypatch.setattr(self_chat, "create_session", lambda *a, **k: "sid")

    def _story(self, tmp_path, with_image=False):
        fname = tmp_path / "story_r2_20260811_123456.md"
        content = "# Story\n\n**Task prompt:** t\n"
        if with_image:
            (tmp_path / "img.png").write_bytes(b"IMG")
            content += "\n![scene](img.png)\n"
        fname.write_text(content)
        return fname

    def test_green_with_image(self, self_chat, monkeypatch, tmp_path):
        self._stubs(self_chat, monkeypatch, tmp_path)
        fname = self._story(tmp_path, with_image=True)
        results = iter([
            {"text": "seen it"},
            {"text": "VERDICT: GREEN\nREASONS: all good"},
        ])
        monkeypatch.setattr(self_chat, "call_llm", lambda *a, **k: next(results))
        data = self_chat.run_moderator(str(tmp_path), str(fname), "t", "Drama")
        assert data["verdict"] == "GREEN"
        verdict_path = str(fname).replace(".md", ".moderation.json")
        assert os.path.isfile(verdict_path)
        with open(verdict_path) as f:
            assert json.load(f)["verdict"] == "GREEN"

    def test_red_bare(self, self_chat, monkeypatch, tmp_path):
        self._stubs(self_chat, monkeypatch, tmp_path)
        fname = self._story(tmp_path)
        monkeypatch.setattr(self_chat, "call_llm", lambda *a, **k: {"text": "This is RED overall"})
        data = self_chat.run_moderator(str(tmp_path), str(fname), "t", "Drama")
        assert data["verdict"] == "RED"

    def test_green_bare(self, self_chat, monkeypatch, tmp_path):
        self._stubs(self_chat, monkeypatch, tmp_path)
        fname = self._story(tmp_path)
        monkeypatch.setattr(self_chat, "call_llm", lambda *a, **k: {"text": "Overall this is GREEN"})
        data = self_chat.run_moderator(str(tmp_path), str(fname), "t", "Drama")
        assert data["verdict"] == "GREEN"

    def test_unknown(self, self_chat, monkeypatch, tmp_path):
        self._stubs(self_chat, monkeypatch, tmp_path)
        fname = self._story(tmp_path)
        monkeypatch.setattr(self_chat, "call_llm", lambda *a, **k: {"text": "maybe fine"})
        data = self_chat.run_moderator(str(tmp_path), str(fname), "t", "Drama")
        assert data["verdict"] == "UNKNOWN"

    def test_login_failure(self, self_chat, monkeypatch, tmp_path):
        def boom(u, p):
            raise RuntimeError("auth")
        monkeypatch.setattr(self_chat, "login", boom)
        assert self_chat.run_moderator(str(tmp_path), "x.md", "t", "Drama") is None

    def test_prompt_read_error(self, self_chat, monkeypatch, tmp_path):
        self._stubs(self_chat, monkeypatch, tmp_path)
        monkeypatch.setattr(self_chat, "MODERATOR_PROMPT_FILE", str(tmp_path / "missing.txt"))
        assert self_chat.run_moderator(str(tmp_path), "x.md", "t", "Drama") is None

    def test_phase_exception(self, self_chat, monkeypatch, tmp_path):
        deleted = []
        self._stubs(self_chat, monkeypatch, tmp_path)
        monkeypatch.setattr(self_chat, "delete_session", lambda *a, **k: deleted.append(a) or True)
        fname = self._story(tmp_path)

        def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(self_chat, "call_llm", boom)
        assert self_chat.run_moderator(str(tmp_path), str(fname), "t", "Drama") is None
        assert deleted == [("tok", "sid")]


class TestRunForever:
    def test_audio_guard_and_rounds(self, self_chat, monkeypatch):
        monkeypatch.setattr(self_chat, "TASKS", [
            {"task": "Audio task", "mediums": ["audio"], "languages": ["English"]},
            {"task": "Normal task", "mediums": ["text"], "languages": ["English"],
             "roles": ["free"], "genre": "Drama", "path": "/custom/dir"},
        ])
        monkeypatch.setattr(self_chat, "login", lambda u, p: f"tok-{u}")
        monkeypatch.setattr(self_chat, "register_agent_tokens", lambda *a, **k: None)
        rounds = []
        monkeypatch.setattr(self_chat, "run_single_conversation",
                            lambda *a, **k: rounds.append(a) or ([{"speaker": "A"}], "sa", "sb", "/x/story.md"))
        deleted = []
        monkeypatch.setattr(self_chat, "delete_session", lambda *a, **k: deleted.append(a) or True)

        def fake_sleep(s):
            fake_sleep.calls += 1
            if fake_sleep.calls >= 2:
                raise KeyboardInterrupt
        fake_sleep.calls = 0
        monkeypatch.setattr(self_chat.time, "sleep", fake_sleep)
        self_chat.run_forever()
        assert len(rounds) == 2
        assert rounds[0][3] == "Normal task"
        assert rounds[0][10] == "/custom/dir"
        assert len(deleted) == 4


class TestModuleScopes:
    def _reload(self, pre):
        name = "self_chat_reload"
        sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "self-chat.py"))
        mod = importlib.util.module_from_spec(spec)
        pre()
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_no_tasks_exits(self, tmp_path, monkeypatch):
        tasks = tmp_path / "tasks.json"
        tasks.write_text(json.dumps([]))

        def pre():
            os.environ["SELF_CHAT_PASSWORD"] = "test-pass"
            monkeypatch.setattr(sys, "argv", ["self-chat.py", "--config", str(tasks)])
            monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
        with pytest.raises(SystemExit) as ei:
            self._reload(pre)
        assert ei.value.code == 1

    def test_dry_run_exits(self, tmp_path, monkeypatch):
        tasks = tmp_path / "tasks.json"
        tasks.write_text(json.dumps([{"task": "One", "languages": ["English"], "mediums": ["text"]}]))

        def pre():
            os.environ["SELF_CHAT_PASSWORD"] = "test-pass"
            monkeypatch.setattr(sys, "argv", ["self-chat.py", "--config", str(tasks), "--dry-run"])
            monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
        with pytest.raises(SystemExit) as ei:
            self._reload(pre)
        assert ei.value.code == 0
