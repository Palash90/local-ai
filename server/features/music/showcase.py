"""Bulk showcase renderer: one clip per genre + a per-instrument timbre tour.

Lets you audition the whole system in one sitting without chatting each time.
Files are written under ``MUSIC_DIR/<user>/showcase`` so the authenticated
browser can play them at ``/api/music/<user>/showcase/<file>`` (and you can
``scp`` the folder). A ``manifest.txt`` lists every clip.
"""

import os
import shutil

from server.features.music.render import render_score
from server.features.music.random_arrange import random_score
from server.features.music import genres

# Representative world/colour instruments worth an individual audition
# (deduped, one per timbre family) so the tour is useful, not 100 files.
INSTRUMENT_TOUR = [
    "PIANO", "EPIANO", "ORGAN", "CELESTA", "MUSICBOX", "CLAV",
    "NYLON", "GUITAR", "EGUITAR", "STEELDRUM",
    "BASS", "EBASS", "SLAP", "FRETLESS", "CONTRABASS",
    "VIOLIN", "VIOLA", "CELLO", "STRINGS", "SYNTHSTRINGS", "CHOIR",
    "FLUTE", "OBOE", "CLARINET", "SAX", "TRUMPET", "TROMBONE", "FRENCHHORN",
    "HARP", "PAD", "SYNTH", "LEAD", "PANFLUTE", "OCARINA", "BAGPIPE",
    # world
    "SITAR", "VEENA", "SAROD", "TAMBRA", "EKTARA", "BANJO", "SANTOOR",
    "KOTO", "GUZHENG", "SHAMISEN", "SHAKUHACHI", "DIZI", "KALIMBA",
    "SHANAI", "SHEHNAI", "SARANGI", "FIDDL",
]

# A 4-bar phrase (melody over a simple ii-V-I-ish loop) that sounds good on any
# timbre; the instrument is what changes between clips.
def _tour_score(instrument):
    return (
        "@tempo 92\n"
        f"[MELODY {instrument} vol=92]\n"
        "E4! q G4 e A4 e B4 h | D5! q B4 e A4 e G4 h | "
        "A4! q C5 e E5 e D5 q B4 q | E5! h R e G#4 e A4 w |\n"
        "[HARMONY epiano vol=70]\n"
        "A3:min7 w | D4:7 w | G3:maj7 w | C4:maj w |\n"
        "[RHYTHM]\n"
        "BD e R e HH e HH e BD e R e SN e R e |\n"
        "BD e R e HH e HH e BD e SN e HH e OH e |\n"
        "BD e R e HH e HH e BD e R e SN e R e |\n"
        "BD e R e HH e HH e SN e HH e CR e CR e |\n"
    )


def _outdir(user="palash"):
    from server.features.music.render import _music_dir
    d = os.path.join(_music_dir(), user, "showcase")
    os.makedirs(d, exist_ok=True)
    return d


def render_genres(outdir=None, user="palash", seed=7, verbose=True):
    outdir = outdir or _outdir(user)
    manifest = []
    for gname in genres.genre_names():
        try:
            score, tempo, info = random_score(seed=seed, genre=gname)
            res = _render_to(score, tempo, outdir, f"genre_{gname}")
            if res.get("ok"):
                manifest.append((f"genre_{gname}.wav", info["key"], info["tempo"],
                                 res["duration_s"], "/".join(info["lanes"])))
                if verbose:
                    print(f"[genre:{gname:16}] {res['duration_s']:6.1f}s "
                          f"{info['key']:14} {info['tempo']}bpm  lanes={info['lanes']}")
            else:
                print(f"[genre:{gname}] FAILED {res.get('error')} {res.get('errors')}")
        except Exception as e:
            print(f"[genre:{gname}] EXC {e!r}")
    return outdir, manifest


def render_instruments(outdir=None, user="palash", verbose=True):
    outdir = outdir or _outdir(user)
    manifest = []
    for inst in INSTRUMENT_TOUR:
        try:
            res = _render_to(_tour_score(inst), 92, outdir, f"instrument_{inst.lower()}")
            if res.get("ok"):
                manifest.append((f"instrument_{inst.lower()}.wav", inst,
                                 res["duration_s"]))
                if verbose:
                    print(f"[instr:{inst:12}] {res['duration_s']:5.1f}s -> "
                          f"instrument_{inst.lower()}.wav")
            else:
                print(f"[instr:{inst}] FAILED {res.get('errors') or res.get('error')}")
        except Exception as e:
            print(f"[instr:{inst}] EXC {e!r}")
    return outdir, manifest


def _render_to(score, tempo, outdir, base):
    res = _json(render_score(score, tempo, user="local"))
    if not res.get("ok"):
        return res
    for ext in ("wav", "mid"):
        src = res.get(f"{ext}_path") or (res["wav_path"] if ext == "wav" else res["mid_path"])
        if src and os.path.exists(src):
            shutil.copy2(src, os.path.join(outdir, f"{base}.{ext}"))
    res["out"] = os.path.join(outdir, f"{base}.wav")
    return res


def _json(x):
    import json
    return json.loads(x) if isinstance(x, str) else x


def write_manifest(outdir, genre_manifest, instr_manifest):
    path = os.path.join(outdir, "manifest.txt")
    with open(path, "w") as f:
        f.write("GENRES (full band)\n")
        f.write(f"{'file':30} {'key':16} {'bpm':>4} {'dur':>6}  lanes\n")
        for row in genre_manifest:
            f.write(f"{row[0]:30} {row[1]:16} {row[2]:>4} {row[3]:>6.1f}  {row[4]}\n")
        f.write("\nINSTRUMENT TOUR (same phrase, different timbre)\n")
        for row in instr_manifest:
            f.write(f"{row[0]:32} {row[1]:12} {row[2]:>6.1f}s\n")
    return path


def run_all(user="palash", seed=7):
    outdir = _outdir(user)
    _, gm = render_genres(outdir, user, seed)
    _, im = render_instruments(outdir, user)
    man = write_manifest(outdir, gm, im)
    print(f"\nDONE. {len(gm)} genre clips + {len(im)} instrument clips.")
    print("OUTPUT  :", outdir)
    print("MANIFEST:", man)
    print(f"BROWSER : open any https://<host>/ai/ ... or /api/music/{user}/showcase/<file>.wav")
    print(f"FETCH   : scp -r <host>:{outdir} ./music-showcase")
    return outdir
