"""Orchestrator: score text -> MIDI bytes -> WAV file. Returns JSON string."""

import json
import os

MUSIC_DIR_DEFAULT = os.path.expanduser("~/local-ai-files/music")


def _lane_view(s):
    """Human-facing instrument label for a rendered lane. Intentionally
    instrument-name only: richer display words (use/style) leaked into
    model-visible tool results, and the model copied them back as lane
    headers ('unknown lane word comping' loops). Falls back to the bare
    lane name when the header was ambiguous."""
    instr = s.get("instr")
    kit = (s.get("kit") or "").lower()
    if s.get("drum"):
        return {"instrument": kit or "drum kit"}
    if instr:
        return {"instrument": instr.replace("_", " ").lower()}
    if s.get("program") is not None:
        from server.features.music.theory import PROGRAMS
        return {"instrument": next(
            (k.lower() for k, v in PROGRAMS.items()
             if v == s["program"]), s.get("name", "").lower())}
    return {"instrument": s.get("name", "").lower()}


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
    # Content-addressed renders: an identical score+tempo reuses the existing
    # files (with a .json sidecar carrying the full result) instead of paying
    # fluidsynth+opus again — retries and "play it again" asks are instant.
    import hashlib
    _key = hashlib.sha256(f"{tempo}\n{score_text}".encode("utf-8")).hexdigest()[:12]
    _meta_path = os.path.join(outdir, f"gen_{_key}.json")
    if os.path.exists(_meta_path):
        try:
            _cached = json.load(open(_meta_path))
            _op = os.path.splitext(_cached.get("wav_path") or "")[0] + ".opus"
            if not os.path.exists(_op):
                _cached["music_stream_url"] = None
            if (os.path.getsize(_cached["wav_path"]) > 100
                    and os.path.exists(_cached.get("mid_path") or "")):
                _cached["dedup_hit"] = True
                print(f"[music] render cache hit: {_cached.get('music_url')}")
                return json.dumps(_cached)
        except Exception as e:
            print(f"[music] stale cache entry ignored: {e}")
    tag = _key
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
    _res = {"ok": True, "music_url": f"/music/{rel}",
            "music_stream_url": stream_url,
            "mid_path": base + ".mid", "wav_path": base + ".wav",
            "duration_s": round(dur, 2), "notes": n, "engine": engine,
            "soundfonts": soundfonts,
            "tempo": tempo, "levels": levels, "structure": structure,
            "score": score_text, "errors": errors}
    try:
        with open(_meta_path, "w") as _mf:
            json.dump(_res, _mf)
    except Exception as e:
        print(f"[music] cache write skipped: {e}")
    return json.dumps(_res)
