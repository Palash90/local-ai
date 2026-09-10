"""Pluck-ish numpy synth + WAV tail trimming."""

import numpy as np

SR = 44100
MAX_SECONDS = 300


def trim_wav_silence(wav_path, keep_s=0.8, thresh_frac=0.0025):
    """Trim trailing dead silence so duration matches the audible content.

    FluidSynth renders up to the last MIDI event (plus our release-tail event),
    which can leave many seconds of pure silence at the end -> the player's
    scrub bar overhangs the actual music and seeking past the end is silent.
    This finds the last frame that is still sounding (above a small fraction of
    peak) and cuts everything after it plus a short ``keep_s`` reverb tail.

    Returns the trimmed duration in seconds.
    """
    import wave

    with wave.open(wav_path, "rb") as w:
        n = w.getnframes()
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(n)
    if sw != 2:
        return n / float(sr)
    data = np.frombuffer(raw, dtype=np.int16)
    if n == 0:
        return 0.0
    frames = data.reshape(-1, ch)
    peak = int(np.abs(frames).max()) or 1
    thresh = max(24, int(peak * thresh_frac))
    active = np.where(np.abs(frames.astype(np.int32)).max(axis=1) > thresh)[0]
    if len(active) == 0:
        end = n
    else:
        end = min(n, int(active[-1]) + int(keep_s * sr))
    trimmed = data[: end * ch]
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(sw)
        w.setframerate(sr)
        w.writeframes(trimmed.tobytes())
    return end / float(sr)


def _freq(midi):
    return 440.0 * (2.0 ** ((midi - 69) / 12.0))


def render_pcm(sections, tempo=120):
    beat_s = 60.0 / tempo
    total_beats = max((e["start"] + e["dur"] for s in sections for e in s["events"]), default=0)
    total_s = min(total_beats * beat_s + 0.5, MAX_SECONDS)
    out = np.zeros(int(total_s * SR), dtype=np.float64)
    for sec in sections:
        gain = max(0.0, min(1.0, sec.get("vol", 100) / 100.0))
        drum = bool(sec.get("drum"))
        for e in sec["events"]:
            if e["type"] == "rest" or gain == 0.0:
                continue
            mids = e.get("pitches") or ([e["midi"]] if "midi" in e else [])
            t0 = int(e["start"] * beat_s * SR)
            n = int(e["dur"] * beat_s * SR)
            if t0 >= len(out) or n <= 0:
                continue
            n = min(n, len(out) - t0)
            t = np.arange(n) / SR
            for p in mids:
                if drum:
                    # percussive: short filtered noise burst + low body thump
                    body = min(int(0.12 * SR), n)
                    tt = np.arange(body) / SR
                    burst = np.random.uniform(-1, 1, body) * np.exp(-tt * 45)
                    thump = np.sin(2 * np.pi * max(50.0, p * 1.6) * tt) * np.exp(-tt * 22)
                    out[t0:t0 + body] += (0.5 * burst + 0.7 * thump) * gain
                    continue
                decay = np.exp(-3.0 * t / max(e["dur"] * beat_s, 0.05))
                f = _freq(p)
                wave = (np.sin(2 * np.pi * f * t) + 0.3 * np.sin(4 * np.pi * f * t)
                        + 0.15 * np.sin(6 * np.pi * f * t)) / 1.45
                # simple attack ramp to avoid clicks
                a = min(int(0.005 * SR), n)
                env = decay.copy()
                env[:a] *= np.linspace(0, 1, a)
                out[t0:t0 + n] += wave * env * gain / max(len(mids), 1)
    peak = np.max(np.abs(out))
    if peak > 0:
        out = out / peak * 0.89
    return (out * 32767).astype(np.int16), SR
