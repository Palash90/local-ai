"""FluidSynth MIDI->WAV rendering (real sampled instruments).

Uses a user-prefix fluidsynth (no root) when present; render.py falls back to
the numpy synth if the binary/soundfont are unavailable.
"""

import os
import shutil
import subprocess
import tempfile

_PREFIX = os.path.expanduser("~/local-ai-files/music/vendor")
_DEFAULT_BIN = os.path.join(_PREFIX, "usr", "bin", "fluidsynth")
_DEFAULT_LIB = os.path.join(_PREFIX, "usr", "lib", "x86_64-linux-gnu")
_DEFAULT_SF = os.path.expanduser("~/local-ai-files/music/soundfonts/GeneralUser-GS.sf2")


def _candidate():
    return os.environ.get("FLUIDSYNTH_BIN", _DEFAULT_BIN)


def soundfont_path():
    return os.environ.get("FLUID_SOUNDFONT", _DEFAULT_SF)


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


def render_midi_to_wav(mid_path, wav_path):
    """Render a MIDI file to WAV via fluidsynth fast-render. Returns bool ok."""
    if not available():
        return False
    tmp = tempfile.mktemp(suffix=".wav")
    cmd = [
        _candidate(), "-ni", "-a", "file", "-r", "44100",
        "--fast-render=" + tmp, soundfont_path(), mid_path,
    ]
    try:
        subprocess.run(
            cmd, env=_env(), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=180, check=True,
        )
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        return False
    if os.path.exists(tmp):
        if os.path.exists(wav_path):
            os.remove(wav_path)
        shutil.move(tmp, wav_path)
        return True
    return False
