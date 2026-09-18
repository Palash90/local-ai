"""Image choreography gates: residency snapshot + conditional recycle."""

import types

import server.features.images as images


def _fake_m(monkeypatch, status=None, headroom=4000, recycled=None):
    if status is None:
        status = {}
    if recycled is None:
        recycled = []
    fake_m = types.SimpleNamespace(
        server_status=lambda mode: status.get(mode, "unloaded"),
        IMAGE_RENDER_RAM_HEADROOM_MB=headroom,
        recycle_comfyui=lambda wait=False: recycled.append(wait),
    )
    monkeypatch.setattr(images, "M", fake_m)
    return recycled


def test_residency_snapshot_reports_loaded_lanes(monkeypatch):
    _fake_m(monkeypatch, {"gpu": "chat_loaded", "guardrail": "unloaded"})
    assert images._lanes_loaded_for_reload() == (True, False)


def test_residency_snapshot_defaults_to_reload_on_error(monkeypatch):
    class Bad:
        def __getattr__(self, name):
            raise RuntimeError("no entrypoint")

    monkeypatch.setattr(images, "M", Bad())
    # Fail-safe: a skipped reload breaks the next chat; a redundant one
    # costs seconds (idle loop re-unloads later).
    assert images._lanes_loaded_for_reload() == (True, True)


def test_recycle_skipped_when_headroom_ample(monkeypatch):
    recycled = _fake_m(monkeypatch, headroom=4000)
    monkeypatch.setattr(images, "_free_ram_mb", lambda: 9000)
    images._recycle_after_render("generate_image")
    assert recycled == []


def test_recycle_runs_when_ram_tight(monkeypatch):
    recycled = _fake_m(monkeypatch, headroom=4000)
    monkeypatch.setattr(images, "_free_ram_mb", lambda: 1200)
    images._recycle_after_render("edit_image")
    assert recycled == [True]


def test_recycle_runs_when_ram_unknown(monkeypatch):
    recycled = _fake_m(monkeypatch, headroom=4000)
    monkeypatch.setattr(images, "_free_ram_mb", lambda: None)
    images._recycle_after_render("generate_image")
    assert recycled == [True]


def _fake_unload_m(monkeypatch, keep):
    unloaded = []

    def _keep(mode):
        return keep.get(mode, False)

    fake_m = types.SimpleNamespace(
        lane_keep_resident=_keep,
        # Composed unload path routes via _unload_lane_for_render, which
        # passes force= (drain-retry). Accept and ignore extra kwargs.
        unload_llama_model=lambda mode, *a, **k: unloaded.append(mode) or True,
        server_status=lambda mode: "chat_loaded",
    )
    monkeypatch.setattr(images, "M", fake_m)
    return unloaded


def test_maybe_unload_skips_kept_lane(monkeypatch):
    unloaded = _fake_unload_m(monkeypatch, {"gpu": True, "guardrail": True})
    assert images._maybe_unload_lane("gpu", "image") is False
    assert images._maybe_unload_lane("guardrail", "image") is False
    assert unloaded == []


def test_maybe_unload_unloads_unkept_lane(monkeypatch):
    unloaded = _fake_unload_m(monkeypatch, {"gpu": False, "guardrail": False})
    assert images._maybe_unload_lane("gpu", "image") is True
    assert images._maybe_unload_lane("guardrail", "image") is True
    assert unloaded == ["gpu", "guardrail"]
