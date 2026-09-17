"""Multi-attachment finalize: _images/_tracks arrays, singular last-wins."""

import threading
import types

import server.features.orchestration as orch


def _fake_m(monkeypatch, tasks, sessions=None, music_dir="/nonexistent"):
    if sessions is None:
        sessions = {"s1": []}
    else:
        sessions.setdefault("s1", [])
    fake_m = types.SimpleNamespace(
        _data_lock=threading.Lock(),
        tasks=tasks,
        sessions=sessions,
        sessions_meta={},
        MUSIC_DIR=music_dir,
        task_mode=lambda tid: "gpu",
        context_token_report=lambda sid, msgs: {},
        save_sessions=lambda: None,
    )
    monkeypatch.setattr(orch, "M", fake_m)
    return fake_m, sessions


def _body():
    return {"timings": {}, "choices": [{"message": {}}]}


def test_two_images_emit_ordered_array_and_singular_last(monkeypatch):
    tasks = {
        "t1": {
            "_tools_used": ["generate_image", "generate_image"],
            "_original_message": "draw two cats",
            "image_file": "u/b.png",
            "gen_prompt": "p2",
            "_image_model": "z_image",
            "image_files": [
                {"rel": "u/a.png", "prompt": "p1", "model": "z_image"},
                {"rel": "u/b.png", "prompt": "p2", "model": "z_image"},
            ],
        }
    }
    _, sessions = _fake_m(monkeypatch, tasks)
    orch._finalize_task("t1", "s1", "here they are", _body())
    msg = sessions["s1"][0]
    assert msg["_images"] == [
        {"url": "/output/u/a.png", "prompt": "p1", "model": "z_image"},
        {"url": "/output/u/b.png", "prompt": "p2", "model": "z_image"},
    ]
    assert msg["_image_url"] == "/output/u/b.png"
    assert tasks["t1"]["_images"] == msg["_images"]


def test_single_legacy_image_mirrors_singular(monkeypatch):
    tasks = {
        "t1": {
            "_tools_used": ["generate_image"],
            "_original_message": "draw one",
            "image_file": "u/a.png",
            "gen_prompt": "p1",
            "_image_model": "z_image",
        }
    }
    _, sessions = _fake_m(monkeypatch, tasks)
    orch._finalize_task("t1", "s1", "done", _body())
    msg = sessions["s1"][0]
    assert msg["_images"] == [
        {"url": "/output/u/a.png", "prompt": "p1", "model": "z_image"}
    ]
    assert msg["_image_url"] == "/output/u/a.png"


def test_safety_decline_withholds_all_images(monkeypatch):
    tasks = {
        "t1": {
            "_tools_used": ["generate_image", "generate_image"],
            "_original_message": "draw two",
            "image_file": "u/b.png",
            "gen_prompt": "p2",
            "_image_model": "z_image",
            "image_files": [
                {"rel": "u/a.png", "prompt": "p1", "model": "z_image"},
                {"rel": "u/b.png", "prompt": "p2", "model": "z_image"},
            ],
        }
    }
    _, sessions = _fake_m(monkeypatch, tasks)
    orch._finalize_task("t1", "s1", "declined", _body(), attach_image=False)
    msg = sessions["s1"][0]
    assert msg["_images"] == []
    assert msg["_image_url"] is None


def test_two_tracks_emit_ordered_array_and_singular_last(monkeypatch):
    tasks = {
        "t1": {
            "_tools_used": ["generate_music", "generate_music"],
            "_original_message": "two tunes",
            "music_file": "u/b.wav",
            "music_score": "score-b",
            "music_url": "/music/u/b.wav",
            "music_stream_url": "/music/u/b.opus",
            "music_levels": [1, 2],
            "music_files": [
                {
                    "rel": "u/a.wav",
                    "url": "/music/u/a.wav",
                    "stream_url": "/music/u/a.opus",
                    "score": "score-a",
                    "levels": [3],
                    "duration_s": 10,
                },
                {
                    "rel": "u/b.wav",
                    "url": "/music/u/b.wav",
                    "stream_url": "/music/u/b.opus",
                    "score": "score-b",
                    "levels": [1, 2],
                    "duration_s": 20,
                },
            ],
        }
    }
    _, sessions = _fake_m(monkeypatch, tasks)
    orch._finalize_task("t1", "s1", "enjoy", _body())
    msg = sessions["s1"][0]
    assert [t["url"] for t in msg["_tracks"]] == [
        "/music/u/a.wav",
        "/music/u/b.wav",
    ]
    assert msg["_tracks"][0]["score"] == "score-a"
    assert msg["_tracks"][1]["duration_s"] == 20
    assert msg["_music_url"] == "/music/u/b.wav"
    assert tasks["t1"]["_tracks"] == msg["_tracks"]


def test_stream_url_cache_bust(tmp_path, monkeypatch):
    (tmp_path / "u.opus").write_bytes(b"x")
    tasks = {
        "t1": {
            "_tools_used": ["generate_music"],
            "_original_message": "tune",
            "music_file": "u.wav",
            "music_url": "/music/u.wav",
            "music_stream_url": "/music/u.opus",
            "music_files": [
                {
                    "rel": "u.wav",
                    "url": "/music/u.wav",
                    "stream_url": "/music/u.opus",
                    "score": "",
                    "levels": [],
                    "duration_s": 5,
                }
            ],
        }
    }
    _, sessions = _fake_m(monkeypatch, tasks, music_dir=str(tmp_path))
    orch._finalize_task("t1", "s1", "enjoy", _body())
    msg = sessions["s1"][0]
    assert msg["_tracks"][0]["stream_url"].startswith("/music/u.opus?v=")
    assert msg["_music_stream_url"].startswith("/music/u.opus?v=")


def test_no_attachments_empty_arrays(monkeypatch):
    tasks = {"t1": {"_tools_used": [], "_original_message": "hi"}}
    _, sessions = _fake_m(monkeypatch, tasks)
    orch._finalize_task("t1", "s1", "hello", _body())
    msg = sessions["s1"][0]
    assert msg["_images"] == []
    assert msg["_tracks"] == []
    assert msg["_image_url"] is None
    assert msg["_music_url"] is None


def test_image_worker_appends_each_render(monkeypatch):
    import json as _json

    import server.features.images as images

    calls = {"n": 0}

    def fake_gen(prompt="", task_id="", **kw):
        calls["n"] += 1
        rel = f"u/img{calls['n']}.png"
        return _json.dumps({"file": f"/x/{rel}", "rel": rel})

    fake_m = types.SimpleNamespace(
        _data_lock=threading.Lock(),
        tasks={"t1": {"status": "working"}},
        generate_image=fake_gen,
    )
    monkeypatch.setattr(images, "M", fake_m)
    for prompt in ("a", "b"):
        images._run_generate_image(
            "t1",
            {"prompt": prompt, "model": "z_image", "aspect_ratio": "landscape"},
        )
    t = fake_m.tasks["t1"]
    assert [e["rel"] for e in t["image_files"]] == ["u/img1.png", "u/img2.png"]
    assert [e["prompt"] for e in t["image_files"]] == ["a", "b"]
    assert t["image_file"] == "u/img2.png"
