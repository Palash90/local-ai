"""Orchestrator: score text -> MIDI bytes -> WAV file. Returns JSON string."""

import json
import os
import uuid

MUSIC_DIR_DEFAULT = os.path.expanduser("~/local-ai-files/music")


def _lane_view(s):
    """Human-facing label + style for a rendered lane, derived from what it
    ACTUALLY contains — instrument name, its role in the song, and a style
    inferred from the events (drones hold, harmony chords, fast figures
    move). Falls back to the bare lane name when the header was ambiguous."""
    ev = s.get("events") or []
    role = (s.get("role") or "").upper()
    instr = s.get("instr")
    kit = (s.get("kit") or "").lower()
    if s.get("drum"):
        return {"instrument": kit or "drum kit",
                "use": "percussion", "style": "groove" if kit else "drums"}
    if instr:
        instrument = instr.replace("_", " ").lower()
    elif s.get("program") is not None and not s.get("drum"):
        from server.features.music.theory import PROGRAMS
        instrument = next((k.lower() for k, v in PROGRAMS.items()
                           if v == s["program"]), s.get("name", "").lower())
    else:
        instrument = s.get("name", "").lower()
    sounding = [e for e in ev if e.get("type") != "rest"]
    n = len(sounding)
    chords = sum(1 for e in sounding if e.get("type") == "chord")
    if not sounding:
        style, use = "rests", "space"
    else:
        avg_dur = sum(e.get("dur", 1) for e in sounding) / n
        long_low = all(e.get("dur", 1) >= 3.0 for e in sounding)
        midis = [(e.get("midi") if e.get("midi") is not None
                  else min(e.get("pitches") or [60])) for e in sounding]
        avg_midi = sum(midis) / len(midis)
        if role.startswith("DRONE") or (long_low and chords and n <= 6):
            style, use = "drone", "holds the tonic"
        elif role.startswith("HARMONY") or role.startswith("PAD"):
            style = "chords" if avg_dur >= 1.5 else "comping"
            if n and max(e.get("dur", 0) for e in sounding) < 1.0:
                style = "rolled figures"
            use = "harmony"
        elif role.startswith("BASS"):
            style = "walking" if avg_dur <= 1.0 else "sustained"
            use = "bassline"
        elif role.startswith("RHYTHM") or role.startswith("PERC"):
            style, use = "pattern", "percussion"
        else:  # melody & friends
            fast = sum(1 for e in sounding if e.get("dur", 1) <= 0.75)
            if n and fast / n > 0.5:
                style = "running figures"
            elif avg_dur >= 2.5:
                style = "sustained lines"
            else:
                style = "melody"
            use = "lead" if not role or role.endswith("Y") else role.lower()
            if role and role[-1].isdigit() and len(role) > 6:
                use = "answer voice"
        if avg_midi < 48 and use == "harmony":
            style += ", low register"
    return {"instrument": instrument, "use": use, "style": style}


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
    # No compromise on named kits: a score that asks for tabla must render
    # with real tabla — a rock-kit substitute is a lie, not a fallback.
    from server.features.music import fluid
    for s in sections:
        kname = (s.get("kit") or "").upper()
        if kname in fluid.KIT_SOUNDFONTS and not fluid.kit_file(s["kit"])[0]:
            return json.dumps({
                "ok": False,
                "error": f"{kname.title()} soundfont not installed — place "
                         f"{fluid.KIT_SOUNDFONTS[kname][0]} in "
                         "~/local-ai-files/music/soundfonts/ (a "
                         f"{kname.title()}-style kit is required for this "
                         "score; the GM rock kit will not fake it)",
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
        soundfonts = []
        if fluid.render_midi_to_wav(base + ".mid", base + ".wav",
                                    sections=sections, tempo=tempo):
            soundfonts = sorted(os.path.basename(sf)
                                for sf in fluid.plan(sections))
        else:
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
        # Best-effort Opus sidecar for streaming playback (~22x smaller than
        # the WAV). Absence simply means "play the WAV" downstream.
        stream_url = None
        try:
            from server.features.music import opus as opus_enc
            opus_path = os.path.splitext(base + ".wav")[0] + ".opus"
            opus_enc.encode_wav_to_opus(base + ".wav", opus_path)
            stream_url = f"/music/{safe_user}/gen_{tag}.opus"
        except Exception as e:
            print(f"[opus] stream encode skipped: {e}")
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e), "errors": errors})
    rel = f"{safe_user}/gen_{tag}.wav"
    levels = [
        {"name": s.get("name"), "program": s.get("program"),
         "drum": bool(s.get("drum")), "vol": s.get("vol", 100),
         "notes": len(s["events"]), **_lane_view(s)}
        for s in sections
    ]
    return json.dumps({"ok": True, "music_url": f"/music/{rel}",
                       "music_stream_url": stream_url,
                       "mid_path": base + ".mid", "wav_path": base + ".wav",
                       "duration_s": round(dur, 2), "notes": n, "engine": engine,
                       "soundfonts": soundfonts,
                       "tempo": tempo, "levels": levels, "structure": structure,
                       "score": score_text, "errors": errors})
