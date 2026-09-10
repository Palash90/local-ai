import json
import os


def test_pitch_map():
    from server.features.music.theory import note_to_midi
    assert note_to_midi("C4") == 60
    assert note_to_midi("A4") == 69


def test_demo_renders_wav(tmp_path=None):
    from server.features.music.render import render_score
    score = "[PIANO]\nC4 q E4 q G4 q C5 h | G4 h R q"
    res = json.loads(render_score(score, 120))
    assert res["ok"] is True, res
    assert os.path.exists(res["wav_path"])
    assert os.path.exists(res["mid_path"])
    with open(res["wav_path"], "rb") as f:
        assert f.read(4) == b"RIFF"
    assert res["duration_s"] > 1.0


def test_bad_token_reports_error():
    from server.features.music.parse import parse_score
    _, errors, _structure = parse_score("[PIANO]\nZZZ q")
    assert errors


def test_section_energy_timeline():
    from server.features.music.parse import parse_score
    score = ("@section verse bars=2 energy=0.4\n"
             "@section chorus bars=2 energy=1.0\n"
             "[PIANO]\nC4 w | E4 w | G4 w | C5 w |")
    secs, errs, structure = parse_score(score)
    assert not errs, errs
    assert [s["name"] for s in structure] == ["verse", "chorus"]
    energies = [e["energy"] for e in secs[0]["events"]]
    assert energies == [0.4, 0.4, 1.0, 1.0]


def test_flat_accidentals_roundtrip():
    from server.features.music.theory import note_to_midi
    assert note_to_midi("Bb4") == 70
    assert note_to_midi("F#4") == 66


def test_random_arrange_ends_on_tonic_cadence():
    from server.features.music.random_arrange import random_score
    from server.features.music.parse import parse_score
    score, tempo, info = random_score(seed=3)
    secs, errs, _ = parse_score(score)
    assert not errs, errs
    # the closing harmony should be the tonic chord (degree 0) -> consonant end
    harm = next((s for s in secs if s["name"] == "HARMONY"), None)
    assert harm is not None
    last = harm["events"][-1]
    assert last["type"] == "chord"
    tonic_pc = info["tonic_pc"]
    root_pc = last["pitches"][0] % 12
    assert root_pc == tonic_pc, (root_pc, tonic_pc)


def test_trailing_silence_trimmed():
    """Reported duration must match audible length, not a long dead tail."""
    import wave
    import numpy as np
    from server.features.music.random_arrange import random_score
    from server.features.music.render import render_score
    score, tempo, info = random_score(seed=4, mood="calm")
    res = json.loads(render_score(score, tempo, user="local"))
    assert res["ok"], res
    with wave.open(res["wav_path"], "rb") as w:
        n, sr, ch = w.getnframes(), w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(n), dtype=np.int16).reshape(-1, ch).astype(np.int32)
    peak = int(abs(data).max()) or 1
    active = np.where(abs(data).max(axis=1) > max(24, peak * 0.0025))[0]
    last_audible = active[-1] / sr if len(active) else 0
    silent_tail = (n / sr) - last_audible
    # the kept reverb tail is ~0.8s; anything past a couple seconds is a bug
    assert silent_tail < 2.0, silent_tail
    assert abs(res["duration_s"] - last_audible) < 1.5


def test_build_midi_lane_filter():
    from server.features.music.parse import parse_score
    from server.features.music.midi_out import build_midi
    secs, errs, _ = parse_score("[PIANO]\nC4 w | E4 w |\n[STRINGS]\nG4 w | B4 w |")
    assert not errs
    full = build_midi(secs, 120)
    only0 = build_midi(secs, 120, lanes={0})
    assert b"\xc0" in full and b"\xc1" in full   # program change on ch0 + ch1
    assert b"\xc0" in only0 and b"\xc1" not in only0
    assert len(only0) < len(full)


def test_soundfont_map_env(monkeypatch=None):
    import tempfile
    from server.features.music import fluid
    d = tempfile.mkdtemp()
    other = os.path.join(d, "Other.sf2")
    open(other, "wb").close()
    old = os.environ.get("FLUID_SOUNDFONT_MAP")
    os.environ["FLUID_SOUNDFONT_MAP"] = json.dumps({"0": other})
    fluid._map_cache = None
    try:
        m = fluid.voice_soundfont_map()
        assert m.get(0) == other
        secs = [{"program": 0, "drum": False}, {"program": 60, "drum": False}]
        groups = fluid.plan(secs)
        assert groups[other] == [0]
        assert groups[fluid.soundfont_path()] == [1]
    finally:
        if old is None:
            del os.environ["FLUID_SOUNDFONT_MAP"]
        else:
            os.environ["FLUID_SOUNDFONT_MAP"] = old
        fluid._map_cache = None


def test_mix_wavs_sum_and_normalize(tmpdir=None):
    import wave
    import numpy as np
    import tempfile
    from server.features.music import fluid
    d = tempfile.mkdtemp()
    a = np.full((100, 2), 30000, dtype=np.int16)
    b = np.zeros((60, 2), dtype=np.int16)
    b[:] = 20000
    paths = []
    for i, arr in enumerate((a, b)):
        p = os.path.join(d, f"{i}.wav")
        with wave.open(p, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(arr.tobytes())
        paths.append(p)
    out = os.path.join(d, "mix.wav")
    assert fluid._mix_wavs(paths, out)
    with wave.open(out, "rb") as w:
        assert w.getnframes() == 100
        mix = np.frombuffer(w.readframes(100), dtype=np.int16).reshape(-1, 2)
    # 30000+20000 would clip -> whole mix scaled by 32700/50000
    assert mix[:60].max() <= 32700 and mix[:60].max() > 25000
    assert abs(int(mix[80, 0]) - 19620) <= 1  # 30000 * 32700/50000


def test_multi_soundfont_render_matches_single():
    """Full score via N groups (same sf twice) sounds like the single pass."""
    from server.features.music import fluid
    if not fluid.available():
        return
    from server.features.music.random_arrange import random_score
    from server.features.music.render import render_score
    base = fluid.soundfont_path()
    old = os.environ.get("FLUID_SOUNDFONT_MAP")
    # route keys to a copy -> forces 2 render passes + mix
    import shutil
    import tempfile
    d = tempfile.mkdtemp()
    dup = os.path.join(d, "Base-dup.sf2")
    shutil.copyfile(base, dup)
    os.environ["FLUID_SOUNDFONT_MAP"] = json.dumps({"0": dup})
    fluid._map_cache = None
    try:
        score, tempo, info = random_score(seed=5, mood="calm")
        # route whichever program actually exists (but not all) to the copy
        from server.features.music.parse import parse_score
        secs, _, _ = parse_score(score, tempo)
        programs = sorted({s["program"] for s in secs if not s.get("drum")})
        assert len(programs) >= 2, programs
        os.environ["FLUID_SOUNDFONT_MAP"] = json.dumps({str(programs[0]): dup})
        fluid._map_cache = None
        res = json.loads(render_score(score, tempo, user="local"))
        assert res["ok"], res
        assert len(res["soundfonts"]) == 2, res["soundfonts"]
        assert res["engine"] == "fluidsynth"
        assert res["duration_s"] > 1.0
    finally:
        if old is None:
            del os.environ["FLUID_SOUNDFONT_MAP"]
        else:
            os.environ["FLUID_SOUNDFONT_MAP"] = old
        fluid._map_cache = None
        shutil.rmtree(d, ignore_errors=True)
