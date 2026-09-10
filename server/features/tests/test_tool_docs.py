import json
import os


def _enabled(tool_docs, monkeypatch):
    """Cache tests exercise the mechanism, independent of the temporary
    calibration switch that keeps generate_music out of the warm cache."""
    monkeypatch.setattr(tool_docs, "WARM_DISABLED", set())


def _music_entry():
    from server.config import TOOLS_DETAILED
    return json.dumps(
        next(t for t in TOOLS_DETAILED if t["function"]["name"] == "generate_music"),
        sort_keys=True,
    )


def test_warm_fresh_roundtrip(tmp_path, monkeypatch):
    from server.features import tool_docs
    _enabled(tool_docs, monkeypatch)
    assert tool_docs.warm("alice", ["generate_music"], cache_dir=str(tmp_path))
    f = tmp_path / "alice.json"
    assert f.exists()
    docs = tool_docs.fresh("alice", cache_dir=str(tmp_path))
    assert "generate_music" in docs
    assert json.loads(docs["generate_music"])["function"]["name"] == "generate_music"
    assert json.loads(docs["generate_music"]) == json.loads(_music_entry())


def test_stale_hash_dropped(tmp_path, monkeypatch):
    from server.features import tool_docs
    _enabled(tool_docs, monkeypatch)
    tool_docs.warm("bob", ["generate_music"], cache_dir=str(tmp_path))
    monkeypatch.setattr(tool_docs, "_detail", lambda name: '{"edited": true}')
    assert tool_docs.fresh("bob", cache_dir=str(tmp_path)) == {}
    # stale entry is pruned from the persisted file too
    stored = json.loads((tmp_path / "bob.json").read_text())
    assert stored == {}


def test_docs_block_keyword_gate(tmp_path, monkeypatch):
    from server.features import tool_docs
    _enabled(tool_docs, monkeypatch)
    tool_docs.warm("carol", ["generate_music"], cache_dir=str(tmp_path))
    b = tool_docs.docs_block("carol", "please compose a calm ambient song", [],
                             cache_dir=str(tmp_path))
    assert "preloaded_tools" in b and "generate_music" in b
    assert tool_docs.docs_block("carol", "what is the capital of France?", [],
                                cache_dir=str(tmp_path)) == ""
    assert tool_docs.docs_block("", "compose a song", [],
                                cache_dir=str(tmp_path)) == ""


def test_docs_block_skips_history(tmp_path, monkeypatch):
    from server.features import tool_docs
    _enabled(tool_docs, monkeypatch)
    tool_docs.warm("dave", ["generate_music"], cache_dir=str(tmp_path))
    entry = json.loads(_music_entry())
    history = [{"role": "tool", "content": json.dumps([entry])}]
    assert tool_docs.docs_in_history(history) == {"generate_music"}
    assert tool_docs.docs_block("dave", "compose me a jingle tune", history,
                                cache_dir=str(tmp_path)) == ""
    # compacted history (docs evicted) -> docs come back
    assert "generate_music" in tool_docs.docs_block(
        "dave", "compose me a jingle tune", [], cache_dir=str(tmp_path))


def test_max_entries_cap(tmp_path, monkeypatch):
    from server.features import tool_docs
    _enabled(tool_docs, monkeypatch)
    tool_docs.warm("erin", ["generate_music", "generate_image", "edit_image",
                            "web_search", "fetch_page"], cache_dir=str(tmp_path))
    stored = json.loads((tmp_path / "erin.json").read_text())
    assert len(stored) == 5  # under cap: everything kept
    tool_docs.warm("erin", ["read_file", "read_image", "update_user_context"],
                   cache_dir=str(tmp_path))
    stored = json.loads((tmp_path / "erin.json").read_text())
    assert len(stored) <= tool_docs.MAX_ENTRIES
    # the just-warmed batch survives eviction
    assert {"read_file", "read_image", "update_user_context"} <= set(stored)


def test_user_context_memo(tmp_path, monkeypatch):
    from server.features import users
    monkeypatch.setattr(users, "CONTEXTS_DIR", str(tmp_path))
    p = tmp_path / "frank.txt"
    p.write_text("likes jazz")
    assert users.read_user_context("frank") == "likes jazz"
    assert users.read_user_context("frank") == "likes jazz"  # memo hit
    p.write_text("likes ragas")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert users.read_user_context("frank") == "likes ragas"
    p.unlink()
    assert users.read_user_context("frank") == ""


def test_sys_content_hot_reload(tmp_path, monkeypatch):
    from server import config
    f = tmp_path / "sys_prompt.txt"
    f.write_text("<system_prompt>\nv1 %model_list%\n</system_prompt>")
    monkeypatch.setattr(config, "PROMPT_PATH", str(f))
    monkeypatch.setattr(config, "_SYS_STATE", {"mtime": None, "content": None})
    assert "v1" in config.get_sys_content()
    f.write_text("<system_prompt>\nv2\n</system_prompt>")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert "v2" in config.get_sys_content() and "v1" not in config.get_sys_content()


def test_generate_music_warm_disabled_during_calibration(tmp_path):
    from server.features import tool_docs
    assert "generate_music" in tool_docs.WARM_DISABLED
    assert tool_docs.warm("cal", ["generate_music", "web_search"],
                          cache_dir=str(tmp_path)) == ["web_search"]
    docs = tool_docs.fresh("cal", cache_dir=str(tmp_path))
    assert set(docs) == {"web_search"}
