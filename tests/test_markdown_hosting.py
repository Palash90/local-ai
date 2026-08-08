import os

import pytest


def _login(client, username, password):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return r.json()["token"]


def _write_story(mh, folder, filename="story.md", content="# Story Title\n\nBody text.\n"):
    os.makedirs(os.path.join(mh.COLLECTION_RULES[folder]["path"], "s1"), exist_ok=True)
    with open(os.path.join(mh.COLLECTION_RULES[folder]["path"], "s1", filename), "w") as f:
        f.write(content)


class TestLogin:
    def test_success(self, mh_client, mh_make_user):
        mh_make_user({"alice": {"password": "secret"}})
        r = mh_client.post("/api/login", json={"username": "alice", "password": "secret"})
        assert r.status_code == 200
        data = r.json()
        assert data["username"] == "alice"
        assert data["token"]
        assert data["context_file"] == ""

    def test_success_returns_context_file(self, mh_client, mh_make_user, tmp_path):
        ctx = str(tmp_path / "alice.txt")
        mh_make_user({"alice": {"password": "secret", "context_file": ctx}})
        r = mh_client.post("/api/login", json={"username": "alice", "password": "secret"})
        assert r.json()["context_file"] == ctx

    def test_bad_password(self, mh_client, mh_make_user):
        mh_make_user({"alice": {"password": "secret"}})
        r = mh_client.post("/api/login", json={"username": "alice", "password": "wrong"})
        assert r.status_code == 401
        assert "Invalid credentials" in r.json()["detail"]

    def test_unknown_user(self, mh_client, mh_make_user):
        mh_make_user({})
        r = mh_client.post("/api/login", json={"username": "nobody", "password": "x"})
        assert r.status_code == 401

    def test_sets_auth_cookie(self, mh_client, mh_make_user):
        mh_make_user({"alice": {"password": "secret"}})
        r = mh_client.post("/api/login", json={"username": "alice", "password": "secret"})
        assert "X-Auth-Token" in r.cookies

    def test_trims_whitespace(self, mh_client, mh_make_user):
        mh_make_user({"alice": {"password": "secret"}})
        r = mh_client.post("/api/login", json={"username": "  alice  ", "password": "  secret  "})
        assert r.status_code == 200
        assert r.json()["username"] == "alice"


class TestLogout:
    def test_removes_token(self, mh_client, mh_make_user):
        mh_make_user({"alice": {"password": "secret"}})
        token = _login(mh_client, "alice", "secret")
        r = mh_client.post("/api/logout", headers={"X-Auth-Token": token})
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert mh_client.cookies.get("X-Auth-Token") in (None, "")


class TestUserHelpers:
    def test_get_user_password(self, mh, mh_make_user):
        mh_make_user({"alice": {"password": "secret"}})
        assert mh.get_user_password("alice") == "secret"
        assert mh.get_user_password("bob") == ""

    def test_get_user_context_path(self, mh, mh_make_user, tmp_path):
        ctx = str(tmp_path / "c.txt")
        mh_make_user({"alice": {"password": "s", "context_file": ctx}})
        assert mh.get_user_context_path("alice") == ctx
        assert mh.get_user_context_path("bob") == ""

    def test_get_user_role_from_file(self, mh, mh_make_user):
        mh_make_user({"alice": {"password": "s", "role": "admin"}})
        assert mh.get_user_role("alice") == "admin"

    def test_get_user_role_defaults(self, mh, mh_make_user):
        mh_make_user({})
        assert mh.get_user_role("palash") == "premium"
        assert mh.get_user_role("totan") == "premium"
        assert mh.get_user_role("someone_else") == "free"

    def test_user_role_level(self, mh, mh_make_user):
        mh_make_user({
            "free_user": {"password": "s"},
            "prem": {"password": "s", "role": "premium"},
            "adm": {"password": "s", "role": "admin"},
        })
        assert mh.user_role_level(None) == 0
        assert mh.user_role_level("free_user") == 0
        assert mh.user_role_level("prem") == 1
        assert mh.user_role_level("adm") == 2


class TestGetCurrentUser:
    def test_no_token(self, mh):
        class R:
            headers = {}
            cookies = {}

        assert mh.get_current_user(R()) is None

    def test_header_token(self, mh):
        mh._active_tokens["tok"] = "alice"

        class R:
            headers = {"X-Auth-Token": "tok"}
            cookies = {}

        assert mh.get_current_user(R()) == "alice"

    def test_cookie_token(self, mh):
        mh._active_tokens["tok"] = "bob"

        class R:
            headers = {}
            cookies = {"X-Auth-Token": "tok"}

        assert mh.get_current_user(R()) == "bob"

    def test_unknown_token(self, mh):
        class R:
            headers = {"X-Auth-Token": "nope"}
            cookies = {}

        assert mh.get_current_user(R()) is None


class TestEnforceRbac:
    def test_unknown_collection(self, mh):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            mh.enforce_rbac("no_such_collection", None)
        assert ei.value.status_code == 404

    def test_guest_blocked_from_premium(self, mh):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            mh.enforce_rbac("premium_stories", None)
        assert ei.value.status_code == 401

    def test_free_user_blocked_from_premium(self, mh, mh_make_user):
        from fastapi import HTTPException

        mh_make_user({"free": {"password": "s"}})
        with pytest.raises(HTTPException) as ei:
            mh.enforce_rbac("premium_stories", "free")
        assert ei.value.status_code == 403

    def test_free_user_blocked_from_admin(self, mh, mh_make_user):
        from fastapi import HTTPException

        mh_make_user({"free": {"password": "s"}})
        with pytest.raises(HTTPException) as ei:
            mh.enforce_rbac("admin_stories", "free")
        assert ei.value.status_code == 403

    def test_premium_user_allowed_on_premium(self, mh, mh_make_user):
        mh_make_user({"prem": {"password": "s", "role": "premium"}})
        assert mh.enforce_rbac("premium_stories", "prem") is None

    def test_admin_allowed_everywhere(self, mh, mh_make_user):
        mh_make_user({"adm": {"password": "s", "role": "admin"}})
        for name in ("free_stories", "premium_stories", "admin_stories"):
            assert mh.enforce_rbac(name, "adm") is None


class TestPickStoryMd:
    def test_empty_folder(self, mh, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        assert mh.pick_story_md(str(d)) is None

    def test_picks_first_md(self, mh, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        (d / "a.md").write_text("x")
        (d / "b.md").write_text("y")
        assert mh.pick_story_md(str(d)).endswith("a.md")

    def test_prefers_edited(self, mh, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        (d / "a.md").write_text("x")
        (d / "a.edited.md").write_text("edited")
        assert mh.pick_story_md(str(d)).endswith("a.edited.md")


class TestModeration:
    def test_none(self, mh, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        assert mh.story_moderation(str(d)) is None

    def test_returns_parsed_json(self, mh, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        (d / "story.moderation.json").write_text('{"verdict": "GREEN"}')
        assert mh.story_moderation(str(d)) == {"verdict": "GREEN"}

    def test_badge_empty(self, mh):
        assert mh.moderation_badge(None) == ""

    def test_badge_green(self, mh):
        b = mh.moderation_badge({"verdict": "GREEN"})
        assert "GREEN" in b
        assert "#2a7" in b

    def test_badge_red(self, mh):
        b = mh.moderation_badge({"verdict": "RED"})
        assert "RED" in b
        assert "#c44" in b

    def test_badge_other(self, mh):
        b = mh.moderation_badge({"verdict": "MEH"})
        assert "MEH" in b


class TestListCollectionStories:
    def test_flat_story(self, mh):
        root = mh.COLLECTION_RULES["free_stories"]["path"]
        os.makedirs(os.path.join(root, "story1"))
        with open(os.path.join(root, "story1", "story.md"), "w") as f:
            f.write("# hi")
        items = mh.list_collection_stories(root)
        assert "story1" in [sid for _, sid in items]

    def test_genre_story(self, mh):
        root = mh.COLLECTION_RULES["free_stories"]["path"]
        os.makedirs(os.path.join(root, "genre1", "story2"))
        with open(os.path.join(root, "genre1", "story2", "story.md"), "w") as f:
            f.write("# hi")
        items = mh.list_collection_stories(root)
        assert ("genre1", "genre1/story2") in items

    def test_ignores_non_dirs(self, mh):
        root = mh.COLLECTION_RULES["free_stories"]["path"]
        with open(os.path.join(root, "notes.md"), "w") as f:
            f.write("# hi")
        assert mh.list_collection_stories(root) == []


class TestIndex:
    def test_empty_placeholder(self, mh_client, mh_make_user):
        mh_make_user({})
        r = mh_client.get("/")
        assert r.status_code == 200
        assert "No story collections found yet." in r.text

    def test_guest_sees_only_free(self, mh_client, mh, mh_make_user):
        mh_make_user({})
        _write_story(mh, "free_stories")
        _write_story(mh, "premium_stories")
        _write_story(mh, "admin_stories")
        r = mh_client.get("/")
        assert "Free Stories" in r.text
        assert "Premium Stories" not in r.text
        assert "Admin Stories" not in r.text

    def test_premium_sees_free_and_premium(self, mh_client, mh, mh_make_user):
        mh_make_user({"prem": {"password": "s", "role": "premium"}})
        _write_story(mh, "free_stories")
        _write_story(mh, "premium_stories")
        _write_story(mh, "admin_stories")
        _login(mh_client, "prem", "s")
        r = mh_client.get("/")
        assert "Free Stories" in r.text
        assert "Premium Stories" in r.text
        assert "Admin Stories" not in r.text

    def test_admin_sees_all(self, mh_client, mh, mh_make_user):
        mh_make_user({"adm": {"password": "s", "role": "admin"}})
        _write_story(mh, "free_stories")
        _write_story(mh, "premium_stories")
        _write_story(mh, "admin_stories")
        _login(mh_client, "adm", "s")
        r = mh_client.get("/")
        assert "Free Stories" in r.text
        assert "Premium Stories" in r.text
        assert "Admin Stories" in r.text

    def test_logged_in_label(self, mh_client, mh, mh_make_user):
        mh_make_user({"adm": {"password": "s", "role": "admin"}})
        _write_story(mh, "free_stories")
        _login(mh_client, "adm", "s")
        r = mh_client.get("/")
        assert "Logged in as" in r.text
        assert "adm" in r.text


class TestReadStory:
    def test_renders_markdown_and_rewrites_images(self, mh_client, mh, mh_make_user):
        mh_make_user({"free": {"password": "s"}})
        _write_story(
            mh,
            "free_stories",
            content="# Great Title\n\n![pic](img.png)\n",
        )
        r = mh_client.get("/story/free_stories/s1")
        assert r.status_code == 200
        assert "Great Title" in r.text
        assert 'src="/media/free_stories/s1/img.png"' in r.text

    def test_guest_allowed_on_free(self, mh_client, mh, mh_make_user):
        mh_make_user({})
        _write_story(mh, "free_stories")
        r = mh_client.get("/story/free_stories/s1")
        assert r.status_code == 200

    def test_guest_blocked_from_premium(self, mh_client, mh, mh_make_user):
        mh_make_user({})
        _write_story(mh, "premium_stories")
        r = mh_client.get("/story/premium_stories/s1")
        assert r.status_code == 401

    def test_free_user_blocked_from_premium(self, mh_client, mh, mh_make_user):
        mh_make_user({"free": {"password": "s"}})
        _write_story(mh, "premium_stories")
        _login(mh_client, "free", "s")
        r = mh_client.get("/story/premium_stories/s1")
        assert r.status_code == 403

    def test_premium_user_can_read(self, mh_client, mh, mh_make_user):
        mh_make_user({"prem": {"password": "s", "role": "premium"}})
        _write_story(mh, "premium_stories")
        _login(mh_client, "prem", "s")
        r = mh_client.get("/story/premium_stories/s1")
        assert r.status_code == 200

    def test_missing_story(self, mh_client, mh_make_user):
        mh_make_user({})
        r = mh_client.get("/story/free_stories/does_not_exist")
        assert r.status_code == 404

    def test_missing_collection(self, mh_client, mh_make_user):
        mh_make_user({})
        r = mh_client.get("/story/no_such_collection/s1")
        assert r.status_code == 404

    def test_shows_moderation_verdict(self, mh_client, mh, mh_make_user):
        mh_make_user({})
        root = mh.COLLECTION_RULES["free_stories"]["path"]
        os.makedirs(os.path.join(root, "s1"))
        with open(os.path.join(root, "s1", "story.md"), "w") as f:
            f.write("# hi")
        with open(os.path.join(root, "s1", "story.moderation.json"), "w") as f:
            f.write('{"verdict": "GREEN"}')
        r = mh_client.get("/story/free_stories/s1")
        assert "Moderation: GREEN" in r.text


class TestStoryContent:
    def test_returns_html(self, mh_client, mh, mh_make_user):
        mh_make_user({})
        _write_story(mh, "free_stories", content="# Hi There")
        r = mh_client.get("/story/free_stories/s1/content")
        assert r.status_code == 200
        assert "<h1>Hi There</h1>" in r.json()["html"]

    def test_uses_edited_file(self, mh_client, mh, mh_make_user):
        mh_make_user({})
        _write_story(mh, "free_stories", filename="story.md", content="# Old")
        _write_story(
            mh, "free_stories", filename="story.edited.md", content="# New"
        )
        r = mh_client.get("/story/free_stories/s1/content")
        assert "<h1>New</h1>" in r.json()["html"]

    def test_missing_story(self, mh_client, mh_make_user):
        mh_make_user({})
        r = mh_client.get("/story/free_stories/nope/content")
        assert r.status_code == 404

    def test_no_markdown(self, mh_client, mh, mh_make_user):
        mh_make_user({})
        root = mh.COLLECTION_RULES["free_stories"]["path"]
        os.makedirs(os.path.join(root, "s1"))
        r = mh_client.get("/story/free_stories/s1/content")
        assert r.status_code == 404


class TestDeleteStory:
    def test_deletes_folder(self, mh_client, mh, mh_make_user):
        mh_make_user({"free": {"password": "s"}})
        _write_story(mh, "free_stories")
        _login(mh_client, "free", "s")
        r = mh_client.delete("/story/free_stories/s1")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "deleted": "s1"}
        assert not os.path.exists(
            os.path.join(mh.COLLECTION_RULES["free_stories"]["path"], "s1")
        )

    def test_missing_story(self, mh_client, mh_make_user):
        mh_make_user({"free": {"password": "s"}})
        _login(mh_client, "free", "s")
        r = mh_client.delete("/story/free_stories/nope")
        assert r.status_code == 404

    def test_guest_blocked(self, mh_client, mh_make_user):
        mh_make_user({})
        r = mh_client.delete("/story/premium_stories/anything")
        assert r.status_code == 401


class TestServeStoryImage:
    def test_serves_image(self, mh_client, mh, mh_make_user):
        mh_make_user({})
        root = mh.COLLECTION_RULES["free_stories"]["path"]
        os.makedirs(os.path.join(root, "s1"))
        with open(os.path.join(root, "s1", "pic.png"), "wb") as f:
            f.write(b"\x89PNG\x0d\x0a\x1a\x0a fake")
        r = mh_client.get("/media/free_stories/s1/pic.png")
        assert r.status_code == 200
        assert r.content == b"\x89PNG\x0d\x0a\x1a\x0a fake"

    def test_missing_image(self, mh_client, mh_make_user):
        mh_make_user({})
        r = mh_client.get("/media/free_stories/s1/pic.png")
        assert r.status_code == 404

    def test_guest_blocked_from_premium(self, mh_client, mh_make_user):
        mh_make_user({})
        r = mh_client.get("/media/premium_stories/s1/pic.png")
        assert r.status_code == 401
