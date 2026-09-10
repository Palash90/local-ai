"""FluidSynth MIDI->WAV rendering (real sampled instruments).

Uses a user-prefix fluidsynth (no root) when present; render.py falls back to
the numpy synth if the binary/soundfont are unavailable.

Supports *per-voice soundfonts*: each lane's GM program (or drums) can be
routed to a different SF2. Lanes are grouped by soundfont, each group is
rendered as its own MIDI pass, and the resulting WAVs are summed. With one
group this is a single classic render, so behaviour is unchanged unless a map
exists. Map sources (later wins): auto-discovered well-known SF2s in the
soundfont dir, then a JSON ``FLUID_SOUNDFONT_MAP`` env override, e.g.
``FLUID_SOUNDFONT_MAP='{"0": "~/sf/MusyngKite.sf2", "drum": "~/sf/better_drum.sf2"}'``
"""

import fnmatch
import glob
import json
import os
import shutil
import subprocess
import tempfile
from collections import OrderedDict

_PREFIX = os.path.expanduser("~/local-ai-files/music/vendor")
_DEFAULT_BIN = os.path.join(_PREFIX, "usr", "bin", "fluidsynth")
_DEFAULT_LIB = os.path.join(_PREFIX, "usr", "lib", "x86_64-linux-gnu")
_SF_DIR = os.path.expanduser("~/local-ai-files/music/soundfonts")
_DEFAULT_SF = os.path.join(_SF_DIR, "GeneralUser-GS.sf2")

# Files that, when present in the soundfont dir, "win" the listed GM programs
# over the base soundfont. Keys are fnmatch patterns (case-insensitive).
# 0-7 acoustic/electric pianos, 4 EPIANO, 8 CELESTA, 10 MUSICBOX: MusyngKite's
# fortepianos are the usual upgrade pick; everything else stays on the base.
# MuseScore_General (FluidLite/timbre-matters) has the better string section.
_KNOWN_OVERRIDES = [
    ("musyngkite*.sf2", [0, 1, 2, 3, 4, 5, 6, 7, 8, 10]),
    ("musescore_general.sf3", [48, 49, 52, 53, 55]),  # real strings/choir/orch-hit
]

# Named drum kits rendered from their own soundfont (melodic preset, so the
# lane goes out on a normal channel with a note map from GM-kit numbers).
# note_map is interim until the ear-check on music/local/tabla-audition/ lands.
KIT_SOUNDFONTS = {
    # Tabla.sf2 is a chromatic single-hit set: measured by spectrum, key K
    # sounds at K-12 semitones (60 -> C3 ~133 Hz ... 82 -> A#4 ~467 Hz);
    # zones alternate bayan (L, bass) / dayan (R, treble), and keys >82 are
    # fluidsynth pitch-shifted extrapolations, so the register lives in
    # 60..82. Syllables mapped by tabla register:
    #   DHA bass-open(bayan) | GHE bass-closed | DHIN mid-ring | TA mid-closed
    #   KA mid-flat          | NA treble-open  | TIN treble-ring
    "TABLA": ("Tabla.sf2", {
        36: 60,  # DHA
        45: 61,  # GHE
        47: 64,  # DHIN
        37: 67,  # TA
        40: 70,  # KA
        38: 74,  # NA
        50: 78,  # TIN
    }),
}

_map_cache = None


def kit_file(kit):
    """(path_or_None, note_map) for a lane kit name; (None, None) otherwise."""
    entry = KIT_SOUNDFONTS.get((kit or "").upper())
    if not entry:
        return None, None
    path = os.path.join(_SF_DIR, entry[0])
    return (path if os.path.isfile(path) else None), dict(entry[1])


def _candidate():
    return os.environ.get("FLUIDSYNTH_BIN", _DEFAULT_BIN)


def soundfont_path():
    return os.environ.get("FLUID_SOUNDFONT", _DEFAULT_SF)


def voice_soundfont_map():
    """{program_int_or_'drum': sf2_path} for lanes that should NOT use the base SF."""
    global _map_cache
    if _map_cache is not None:
        return _map_cache
    m = {}
    for pattern, programs in _KNOWN_OVERRIDES:
        for path in sorted(glob.glob(os.path.join(_SF_DIR, "*.sf[23]"))):
            if fnmatch.fnmatch(os.path.basename(path).lower(), pattern):
                for p in programs:
                    m[p] = path
                break
    raw = os.environ.get("FLUID_SOUNDFONT_MAP")
    if raw:
        try:
            for k, v in json.loads(raw).items():
                v = os.path.expanduser(str(v))
                if not os.path.isfile(v):
                    continue
                m["drum" if k == "drum" else int(k)] = v
        except Exception:
            pass
    _map_cache = {k: v for k, v in m.items() if os.path.isfile(v)}
    return _map_cache


def soundfont_for_section(sec):
    kpath, _ = kit_file(sec.get("kit"))
    if kpath:
        return kpath
    m = voice_soundfont_map()
    key = "drum" if sec.get("drum") else int(sec.get("program", 0))
    return m.get(key, soundfont_path())


def plan(sections):
    """Ordered {sf_path: [lane_idx]} grouping; one entry unless a map applies."""
    groups = OrderedDict()
    for si, sec in enumerate(sections):
        groups.setdefault(soundfont_for_section(sec), []).append(si)
    return groups


def available():
    binp = _candidate()
    if not os.path.isfile(binp) or not os.access(binp, os.X_OK):
        return False
    if not os.path.isfile(soundfont_path()):
        return False
    return True


def _env():
    env = dict(os.environ)
    lib = env.get("FLUIDSYNTH_LIB", _DEFAULT_LIB)
    if os.path.isdir(lib):
        env["LD_LIBRARY_PATH"] = lib + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    return env


def _fast_render(sf_path, mid_path, out_wav):
    """Run one fluidsynth pass; returns out_wav path or None."""
    cmd = [
        _candidate(), "-ni", "-a", "file", "-r", "44100",
        "--fast-render=" + out_wav, sf_path, mid_path,
    ]
    try:
        subprocess.run(
            cmd, env=_env(), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=180, check=True,
        )
    except Exception:
        return None
    return out_wav if os.path.isfile(out_wav) else None


def _read_wav(path):
    import numpy as np
    import wave
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        nch = w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    arr = data.reshape(-1, nch).astype("float64")
    return arr, sr


def _mix_wavs(paths, out_path):
    """Sum fluidsynth passes (same sr) to one 16-bit WAV, normalizing only if the
    naive mix would clip so per-lane CC7 balance is preserved."""
    import numpy as np
    import wave
    arrays = [_read_wav(p) for p in paths]
    if len(arrays) == 1:
        shutil.copy(paths[0], out_path)
        return True
    sr = arrays[0][1]
    nch = max(a.shape[1] for a, _ in arrays)
    n = max(a.shape[0] for a, _ in arrays)
    mix = np.zeros((n, nch), dtype="float64")
    for arr, _ in arrays:
        if arr.shape[1] == 1 and nch == 2:
            arr = np.repeat(arr, 2, axis=1)
        mix[:arr.shape[0]] += arr
    peak = np.abs(mix).max() if mix.size else 0.0
    if peak > 32700:
        mix *= 32700.0 / peak
    out = np.clip(mix, -32768, 32767).astype("<i2")
    with wave.open(out_path, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(out.tobytes())
    return True


def render_midi_to_wav(mid_path, wav_path, sections=None, tempo=None):
    """Render a MIDI file to WAV via fluidsynth fast-render. Returns bool ok.

    With ``sections`` (+ ``tempo``) given and a per-voice soundfont map active,
    renders one pass per soundfont group from lane-filtered MIDIs and mixes.
    """
    if not available():
        return False
    groups = plan(sections) if sections else OrderedDict()
    base = soundfont_path()
    if len(groups) <= 1 and (not groups or next(iter(groups)) == base):
        tmp = tempfile.mktemp(suffix=".wav")
        ok = _fast_render(soundfont_path(), mid_path, tmp)
        if ok:
            if os.path.exists(wav_path):
                os.remove(wav_path)
            shutil.move(tmp, wav_path)
            return True
        if os.path.exists(tmp):
            os.remove(tmp)
        return False
    from server.features.music.midi_out import build_midi
    tmp_wavs = []
    try:
        for sf, idxs in groups.items():
            lanes_secs = [sections[i] for i in idxs]
            kit = sf != base and any(s.get("kit") for s in lanes_secs)
            _, note_map = kit_file(next((s.get("kit") for s in lanes_secs if s.get("kit")), None))
            mid_tmp = tempfile.mktemp(suffix=".mid")
            with open(mid_tmp, "wb") as f:
                f.write(build_midi(sections, tempo, lanes=set(idxs),
                                   kit=kit, note_map=note_map if kit else None))
            wav_tmp = _fast_render(sf, mid_tmp, tempfile.mktemp(suffix=".wav"))
            os.remove(mid_tmp)
            if wav_tmp is None:
                return False
            tmp_wavs.append(wav_tmp)
        return _mix_wavs(tmp_wavs, wav_path)
    finally:
        for p in tmp_wavs:
            if os.path.exists(p):
                os.remove(p)
