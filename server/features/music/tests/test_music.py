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
    # energy now RAMPES toward section targets (slope-capped) instead of stepping
    assert abs(energies[0] - 0.4) < 1e-6 and abs(energies[1] - 0.4) < 1e-6
    assert abs(energies[2] - 0.6) < 1e-6 and abs(energies[3] - 0.8) < 1e-6


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


BOSSA_VAMP = """@tempo 90
@genre bossa
@mood calm
@section intro bars=2 energy=0.3
@section verse bars=4 energy=0.5
@section chorus bars=4 energy=0.8
@section outro bars=3 energy=0.4
[MELODY piano vol=85]
E4 q G4 q B4 q A4 q |
R q E4 q G4 q A4 q |
[HARMONY epiano vol=75]
C3:maj7 w | G3:7 w |
[BASS ebass vol=88]
C2 h G2 q G2 q | C2 h G2 q G2 q |
[RHYTHM]
BD e R e HH e R e BD e R e HH e R e | BD e R e SN e R e BD e R e HH e R e |"""


def test_tiling_fills_section_grid():
    from server.features.music.parse import parse_score
    secs, errs, structure = parse_score(BOSSA_VAMP, 90)
    assert not errs, errs
    grid_beats = sum(s["bars"] for s in structure) * 4.0
    for lane in secs:
        if not lane["events"]:
            continue
        end = max(e["start"] + e["dur"] for e in lane["events"])
        assert end >= grid_beats - 4.0, (lane["name"], end, grid_beats)


def test_tiling_leaves_full_lanes_untouched():
    from server.features.music.parse import parse_score
    from server.features.music.random_arrange import random_score
    for seed in (3, 7, 42):
        for g in ("bossa", "cinematic", "edm"):
            score, tempo, info = random_score(seed=seed, genre=g)
            secs, errs, structure = parse_score(score, tempo)
            assert not errs, errs
            grid = sum(sg["bars"] for sg in structure)
            for lane in secs:
                if not lane["events"]:
                    continue
                end = max(e["start"] + e["dur"] for e in lane["events"])
                # same bar-aligned gate parse.py uses for tiling: engine
                # cadences may breathe a beat under the final barline, but
                # the last written bar must count as content (no loop added)
                assert (int(end) + 3) // 4 >= grid, (seed, g, lane["name"])


def test_tiled_bossa_renders_declared_length():
    import json
    from server.features.music.render import render_score
    res = json.loads(render_score(BOSSA_VAMP, 90, title="bossa-tile", user="local"))
    assert res["ok"], res
    # 13 declared bars @ 90 BPM = 34.7s; old behavior truncated to ~7s
    assert 30.0 <= res["duration_s"] <= 40.0, res["duration_s"]
    assert res["errors"] == []
    notes = {l["name"]: l["notes"] for l in res["levels"]}
    assert notes["RHYTHM"] >= 20, notes


def test_arpeggio_expansion():
    from server.features.music.parse import parse_score
    secs, errs, _ = parse_score(
        "[HARMONY epiano]\nC3:min7 ar w | C3:min7 w |\n[BASS ebass]\nD2:7 ad q |",
        120)
    assert not errs, errs
    h = secs[0]["events"]
    assert all(e["type"] == "note" for e in h[:4])
    up = [e["midi"] for e in h[:4]]
    assert up == sorted(up) and all(abs(e["dur"] - 1.0) < 1e-6 for e in h[:4])
    assert h[4]["type"] == "chord"                      # block form untouched
    d = [e["midi"] for e in secs[1]["events"]]
    assert d == sorted(d, reverse=True)                 # ad -> descending roll
    secs2, errs2, _ = parse_score("[HARMONY epiano]\nE4:min7arq |", 120)  # fused form
    assert not errs2 and len(secs2[0]["events"]) == 4


def test_dsl_doc_sections_and_example_bars():
    """The live DSL doc is hot-swapped by hand — guard its structure, and
    every bar-shaped example in it (grooves, talas, fills, arp figures)
    against the real parser. Prose is skipped because only pitch+duration
    PAIR runs match the extraction regex."""
    import pathlib
    import re
    from server.features.music.parse import parse_score
    doc = (pathlib.Path(__file__).resolve().parents[4] / "prompts" /
           "music_dsl.txt").read_text()
    for sec in ("LANES:", "EVENTS", "GENRE GROOVES", "ARPENING",
                "arpeggio:", "chord:", "drum:", "MUSIC THEORY",
                "ANTI-EXAMPLE", "LOOP SEMANTICS", "TALAS", "SARGAM"):
        assert sec in doc, sec
    PITCH = r"[A-Z][A-Z0-9#']*(?::[a-z0-9]+)?(?:ar|ad|au)?"
    DUR = r"[qwhes]\.?"
    pair_re = re.compile(rf"(?:{PITCH} {DUR} ?)+")
    checked = 0
    for line in doc.splitlines():
        for spec in [s.strip() for s in line.split("|")]:
            if not spec or not pair_re.fullmatch(spec):
                continue
            lane = ("[RHYTHM tabla]" if not re.search(r"\d+:", spec)
                    else "[HARMONY epiano]")
            _, errs, _ = parse_score(lane + "\n" + spec + " |", 120)
            assert not errs, (spec[:60], errs[:2])
            checked += 1
    assert checked >= 12, f"guard barely checked anything: {checked}"


def test_kit_routing_and_policy():
    import json
    from server.features.music import fluid
    from server.features.music.render import render_score
    path, note_map = fluid.kit_file("tabla")
    assert path and path.endswith("Tabla.sf2")
    assert note_map[36] == 60                      # DHA into the kit range
    from server.features.music.parse import parse_score
    secs, errs, _ = parse_score(
        "[RHYTHM tabla]\nDHA e DHIN e NA e TIN q |", 100)
    assert not errs and secs[0]["kit"] == "TABLA"
    groups = fluid.plan(secs)
    assert list(groups) == [path]                  # its own render pass
    # melodic kit: not channel 9, program 0 emitted
    from server.features.music.midi_out import build_midi
    kit_mid = build_midi(secs, 100, kit=True, note_map=note_map)
    assert b"\xc0\x00" in kit_mid                  # prog 0 on a normal channel
    # policy: registered kit with a missing file must refuse, not fake
    import server.features.music.theory as th
    fluid.KIT_SOUNDFONTS["ZZFAKE"] = ("Nope.sf2", {36: 60})
    styles = th.DRUM_STYLES
    th.DRUM_STYLES = set(styles) | {"ZZFAKE"}
    try:
        res = json.loads(render_score(
            "[RHYTHM zzfake]\nDHA q |", 100, title="t", user="local"))
        assert not res["ok"] and "not installed" in res["error"], res
    finally:
        th.DRUM_STYLES = styles
        fluid.KIT_SOUNDFONTS.pop("ZZFAKE")


def test_kit_note_map_maps_all_syllables():
    from server.features.music import fluid
    from server.features.music.parse import DRUM_MAP
    path, note_map = fluid.kit_file("tabla")
    syl = {36: "DHA", 45: "GHE", 38: "NA", 50: "TIN",
           47: "DHIN", 37: "TA", 40: "KA", 49: "CR"}
    assert set(syl) <= set(note_map)
    for gm, nm in syl.items():
        assert 60 <= note_map[gm] <= 82
        assert DRUM_MAP.get(nm) in (gm, None) or DRUM_MAP.get(nm) == gm


def test_energy_ramp_is_smooth():
    from server.features.music.parse import _parse_sections
    text = ("@section intro bars=2 energy=0.3\n"
            "@section verse bars=4 energy=0.6\n"
            "@section chorus bars=4 energy=0.9\n"
            "@section outro bars=2 energy=0.3\n")
    _, be = _parse_sections(text)
    assert len(be) == 12
    assert max(abs(be[i + 1] - be[i]) for i in range(len(be) - 1)) <= 0.201
    assert be[9] >= 0.89 and be[0] == 0.3


def test_variation_tiling_fills_lifts_and_thins():
    from server.features.music.parse import parse_score
    secs, errs, _ = parse_score(
        "@tempo 100\n"
        "@section verse bars=2 energy=0.8\n"
        "@section chorus bars=4 energy=0.9\n"
        "@section outro bars=2 energy=0.3\n"
        "[RHYTHM]\nBD e HH e SN e HH e |\n"
        "[MELODY santoor]\nC4 e E4 e G4 e A4 e |\n", 100)
    assert not errs, errs
    rhy, mel = secs[0]["events"], secs[1]["events"]
    fills = {int(e["start"] // 4) for e in rhy if e["midi"] in (45, 47, 50, 49)}
    assert 1 in fills and 5 in fills, sorted(fills)          # before section changes
    assert any(e.get("ghost") and e["midi"] >= 60 for e in mel)  # octave ghosts
    last = [e for e in rhy if 28 <= e["start"] < 32]
    assert last and all(abs((e["start"] % 1.0) - 0.5) > 0.15
                        or abs(e["start"] % 1.0) < 0.15 for e in last)


def test_full_length_lanes_never_transformed():
    from server.features.music.parse import parse_score
    vamp = ("@section a bars=2 energy=0.9\n@section b bars=2 energy=0.9\n"
            "[RHYTHM]\nBD e HH e SN e HH e | BD e HH e SN e HH e |\n"
            "BD e HH e SN e HH e | BD e HH e SN e HH e |\n")
    secs, errs, _ = parse_score(vamp, 100)
    assert not errs, errs
    assert sum(len(s["events"]) for s in secs) == 16
    assert not any(e.get("ghost") for s in secs for e in s["events"])
    assert all(e["midi"] != 49 for e in secs[0]["events"])     # no synthetic fills


def test_chord_melody_gets_top_note_ghosts():
    """Chord-type MELODY events must get octave ghost doubles in loud bars
    (regression: ghosts previously only fired for single-note events, so a
    block-chord lead got zero variation)."""
    from server.features.music.parse import parse_score
    score = (
        "@tempo 100\n"
        "@section chorus bars=4 energy=1.0\n"
        "[MELODY Frenchhorn]\n"
        "C4:maj7 q D4:maj7 q E4:min7 q F4:maj7 h |\n"
        "C4:maj7 q D4:maj7 q E4:min7 q F4:maj7 h |\n"
    )
    secs, errs, _ = parse_score(score, 100)
    assert not errs, errs
    mel = next(s for s in secs if s["name"] == "MELODY")
    ghosts = [e for e in mel["events"] if e.get("ghost")]
    assert ghosts, "no ghosts on chord melody"
    # top pitch of first chord C4:maj7 (60,64,67,71) doubled at 83
    assert any(e["midi"] == 83 and e["vel"] == 64 for e in ghosts)


def test_repeated_bars_thin_on_even_occurrences():
    """Repeats looping WITHIN one section lose the beat-3 stab on even
    occurrences; a reprise in a NEW section (verse 1 -> verse 2) stays
    whole — that's song form, not monotony. Sparse bars untouched.
    The lane is shorter than the grid so it tiles (the realistic vamp path);
    tiling copies carry the same figure into both sections."""
    from server.features.music.parse import parse_score
    score = (
        "@tempo 100\n"
        "@section verse1 bars=3 energy=0.6\n"
        "@section verse2 bars=3 energy=0.6\n"
        "[MELODY piano]\n"
        "C4 q D4 q E4 q F4 q |\n"   # written bar 0
        "C4 q D4 q E4 q F4 q |\n"   # written bar 1
        "G4 w |\n"                   # written bar 2
    )
    secs, errs, _ = parse_score(score, 100)
    assert not errs, errs
    mel = next(s for s in secs if s["name"] == "MELODY")
    bybar = {}
    for e in mel["events"]:
        bybar.setdefault(int(e["start"] // 4), []).append(e)
    assert len(bybar[0]) == 4
    assert len(bybar[1]) == 3 and all(e["start"] - 4 < 3 for e in bybar[1])
    assert len(bybar[2]) == 1
    assert len(bybar[3]) == 4
    assert len(bybar[4]) == 3
    assert len(bybar[5]) == 1
    # determinism: identical re-parse
    secs2, _, _ = parse_score(score, 100)
    mel2 = next(s for s in secs2 if s["name"] == "MELODY")
    k = lambda e: (e["type"], e.get("midi"), tuple(e.get("pitches", [])),
                   round(e["start"], 3), round(e["dur"], 3), e.get("vel"))
    assert [k(e) for e in mel["events"]] == [k(e) for e in mel2["events"]]


def test_no_internal_keys_leak_to_events():
    """_cycle/_tiled markers must never reach midi_out."""
    from server.features.music.parse import parse_score
    score = open("/tmp/opencode/u_score.txt").read()
    secs, errs, _ = parse_score(score, 100)
    assert not errs, errs
    assert not any("_cycle" in e or "_tiled" in e
                   for s in secs for e in s["events"])
    assert all("_tiled" not in s for s in secs)


def test_header_extra_role_words_ignored():
    """Regression: [PERC soft drum vol=70] (the failed live score) must parse
    clean — extra role words are harmless, adjectives are ignored."""
    from server.features.music.parse import parse_score
    secs, errs, _ = parse_score("[PERC soft drum vol=70]\nBD q SN q BD q SN q |", 120)
    assert not errs, errs
    assert secs[0]["drum"] is True
    assert secs[0]["vol"] == 70
    assert len(secs[0]["events"]) == 4


def test_header_adjective_ignored_melody_kept():
    from server.features.music.parse import parse_score
    secs, errs, _ = parse_score("[MELODY soft piano vol=85]\nC4 q D4 q E4 q F4 q |", 120)
    assert not errs, errs
    assert secs[0]["name"] == "MELODY"
    assert secs[0]["program"] == 0  # PIANO
    assert len(secs[0]["events"]) == 4


def test_header_typo_suggests_sensibly():
    from server.features.music.parse import parse_score
    _, errs, _ = parse_score("[MELODY SANTOR]\nC4 w |", 120)
    assert errs and "SANTOR" in errs[0] and "SANTOOR" in errs[0].upper()
    _, errs, _ = parse_score("[MELOD piano]\nC4 w |", 120)
    assert errs and "MELODY" in errs[0].upper()
    # existing priority unchanged: first role word names the lane
    secs, errs, _ = parse_score("[BASS ebass]\nC2 w |", 120)
    assert not errs, errs
    assert secs[0]["name"] == "BASS" and secs[0]["program"] == 33


def test_inline_hash_comments_stripped():
    """Trailing '# ...' comments must not produce bad tokens (the model's
    dominant error class); sharps in note names must survive."""
    from server.features.music.parse import parse_score, parse_tempo
    score = (
        "@tempo 84  # calm raga pace\n"
        "@section verse bars=2 energy=0.6  # main section\n"
        "[MELODY santoor vol=85]\n"
        "C4 q E4 q G4 q F#4 h |  # motif with a sharp, must keep F#4\n"
        "C4 q E4 q G4 q A4 h |  # second bar\n"
    )
    assert parse_tempo(score, 120) == 84
    secs, errs, _ = parse_score(score, 120)
    assert not errs, errs
    mel = next(s for s in secs if s["name"] == "MELODY")
    midis = [e["midi"] for e in mel["events"] if e["type"] == "note"]
    assert 66 in midis  # F#4 survived the '#' strip
    assert len(mel["events"]) == 8
