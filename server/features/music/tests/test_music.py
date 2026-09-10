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
