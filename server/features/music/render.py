"""Orchestrator: score text -> MIDI bytes -> WAV file. Returns JSON string."""

import json
import os
import uuid

MUSIC_DIR_DEFAULT = os.path.expanduser("~/local-ai-files/music")


def _music_dir():
    try:
        from server.features.state import M
        d = getattr(M, "MUSIC_DIR", None)
        if d:
            return d
    except Exception:
        pass
    return MUSIC_DIR_DEFAULT


def render_score(score_text, tempo=120, title="music", user="local"):
    import wave
    from server.features.music.parse import parse_score, parse_tempo
    from server.features.music.midi_out import build_midi
    from server.features.music.synth import render_pcm

    tempo = parse_tempo(score_text, tempo)
    sections, errors, structure = parse_score(score_text, tempo)
    n = sum(len(s["events"]) for s in sections)
    if not sections or n == 0:
        return json.dumps({"ok": False, "error": "no notes parsed",
                           "errors": errors})
    safe_user = "".join(c if c.isalnum() or c in "-_" else "_" for c in (user or "local")) or "local"
    try:
        from server.features.users import _safe_username
        safe_user = _safe_username(user)
    except Exception:
        pass
    outdir = os.path.join(_music_dir(), safe_user)
    os.makedirs(outdir, exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    base = os.path.join(outdir, f"gen_{tag}")
    try:
        with open(base + ".mid", "wb") as f:
            f.write(build_midi(sections, tempo))
        engine = "fluidsynth"
        from server.features.music import fluid
        if not fluid.render_midi_to_wav(base + ".mid", base + ".wav"):
            engine = "numpy"
            pcm, sr = render_pcm(sections, tempo)
            with wave.open(base + ".wav", "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(pcm.tobytes())
        # Cut dead silence past the last sounding note so the reported duration
        # matches what you hear (a short reverb tail is kept).
        from server.features.music.synth import trim_wav_silence
        try:
            dur = trim_wav_silence(base + ".wav")
        except Exception:
            with wave.open(base + ".wav", "rb") as w:
                dur = w.getnframes() / float(w.getframerate())
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e), "errors": errors})
    rel = f"{safe_user}/gen_{tag}.wav"
    levels = [
        {"name": s.get("name"), "program": s.get("program"),
         "drum": bool(s.get("drum")), "vol": s.get("vol", 100),
         "notes": len(s["events"])}
        for s in sections
    ]
    return json.dumps({"ok": True, "music_url": f"/music/{rel}",
                       "mid_path": base + ".mid", "wav_path": base + ".wav",
                       "duration_s": round(dur, 2), "notes": n, "engine": engine,
                       "tempo": tempo, "levels": levels, "structure": structure,
                       "score": score_text, "errors": errors})
