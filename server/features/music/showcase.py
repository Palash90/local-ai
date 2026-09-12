"""Bulk showcase renderer: one clip per genre + a per-instrument timbre tour.

Lets you audition the whole system in one sitting without chatting each time.
Files + a machine-readable ``index.json`` are written to the PUBLIC showcase
directory (``MUSIC_DIR/showcase``), which the app serves unauthenticated as a
shareable page (see showcase_page.py / the /api/public/music route).
"""

import json
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


def _outdir(user=None):
    try:
        from server.config import MUSIC_SHOWCASE_DIR as d
    except Exception:
        d = os.path.join(os.path.expanduser("~/local-ai-files/music"), "showcase")
    os.makedirs(d, exist_ok=True)
    return d


def render_genres(outdir=None, user="palash", seed=7, verbose=True):
    outdir = outdir or _outdir()
    manifest = []
    for gname in genres.genre_names():
        try:
            score, tempo, info = random_score(seed=seed, genre=gname)
            res = _render_to(score, tempo, outdir, f"genre_{gname}")
            if res.get("ok"):
                rec = {"kind": "genre", "id": gname, "title": gname.replace("_", " ").title(),
                       "file": f"genre_{gname}.wav", "key": info["key"],
                       "tempo": info["tempo"], "bars": info["bars"],
                       "duration_s": res["duration_s"], "structure": info["structure"],
                       "lanes": info["lanes"], "desc": genres.resolve(gname)["desc"]}
                manifest.append(rec)
                if verbose:
                    print(f"[genre:{gname:16}] {res['duration_s']:6.1f}s {info['key']:14} "
                          f"{info['tempo']}bpm  lanes={info['lanes']}")
            else:
                print(f"[genre:{gname}] FAILED {res.get('error')} {res.get('errors')}")
        except Exception as e:
            print(f"[genre:{gname}] EXC {e!r}")
    return outdir, manifest


# Fixed-seed cross-tradition combos: the fusion-audibility contract
# (partner lane + partner groove, lead stays home) pinned as files.
FUSION_COMBOS = [
    ("indian_classical", "jazz", 11),
    ("japanese", "ambient", 22),
    ("arabic", "cinematic", 33),
]


def render_fusions(outdir=None, user="palash", verbose=True):
    outdir = outdir or _outdir()
    manifest = []
    for primary, partner, seed in FUSION_COMBOS:
        tag = f"{primary}_x_{partner}"
        try:
            score, tempo, info = random_score(
                seed=seed, genre=primary, fusion=partner)
            res = _render_to(score, tempo, outdir, f"fusion_{tag}")
            if res.get("ok"):
                rec = {"kind": "fusion", "id": tag,
                       "title": f"{primary.replace('_', ' ').title()} x "
                                f"{partner.replace('_', ' ').title()}",
                       "file": f"fusion_{tag}.wav", "key": info["key"],
                       "tempo": info["tempo"], "bars": info["bars"],
                       "duration_s": res["duration_s"],
                       "structure": info["structure"],
                       "lanes": info["lanes"]}
                manifest.append(rec)
                if verbose:
                    print(f"[fusion:{tag:28}] {res['duration_s']:6.1f}s {info['key']:14} "
                          f"{info['tempo']}bpm  lanes={info['lanes']}")
            else:
                print(f"[fusion:{tag}] FAILED {res.get('error')} {res.get('errors')}")
        except Exception as e:
            print(f"[fusion:{tag}] EXC {e!r}")
    return outdir, manifest


def render_instruments(outdir=None, user="palash", verbose=True):
    outdir = outdir or _outdir()
    manifest = []
    for inst in INSTRUMENT_TOUR:
        try:
            res = _render_to(_tour_score(inst), 92, outdir, f"instrument_{inst.lower()}")
            if res.get("ok"):
                manifest.append({"kind": "instrument", "id": inst.lower(),
                                 "title": inst.title(), "file": f"instrument_{inst.lower()}.wav",
                                 "duration_s": res["duration_s"], "family": _family(inst)})
                if verbose:
                    print(f"[instr:{inst:12}] {res['duration_s']:5.1f}s -> instrument_{inst.lower()}.wav")
            else:
                print(f"[instr:{inst}] FAILED {res.get('errors') or res.get('error')}")
        except Exception as e:
            print(f"[instr:{inst}] EXC {e!r}")
    return outdir, manifest


_FAMILIES = {"keys": "PIANO EPIANO ORGAN CELESTA MUSICBOX CLAV SYNTH PAD LEAD".split(),
             "guitar/pluck": "NYLON GUITAR EGUITAR STEELDRUM BANJO KOTO GUZHENG SHAMISEN SITAR VEENA SAROD TAMBRA EKTARA SANTOOR HARP KALIMBA".split(),
             "bass": "BASS EBASS SLAP FRETLESS CONTRABASS".split(),
             "strings": "VIOLIN VIOLA CELLO STRINGS SYNTHSTRINGS FIDDL SARANGI".split(),
             "winds": "FLUTE OBOE CLARINET SAX SHAKUHACHI DIZI OCARINA PANFLUTE BAGPIPE".split(),
             "brass": "TRUMPET TROMBONE FRENCHHORN SHANAI SHEHNAI".split(),
             "voice": "CHOIR".split()}


def _family(inst):
    for fam, names in _FAMILIES.items():
        if inst in names:
            return fam
    return "other"


def _render_to(score, tempo, outdir, base):
    res = _json(render_score(score, tempo, user="local"))
    if not res.get("ok"):
        return res
    for ext in ("wav", "mid"):
        src = res.get(f"{ext}_path")
        if src and os.path.exists(src):
            shutil.copy2(src, os.path.join(outdir, f"{base}.{ext}"))
    res["out"] = os.path.join(outdir, f"{base}.wav")
    return res


def _json(x):
    return json.loads(x) if isinstance(x, str) else x


def write_index(outdir, clips):
    path = os.path.join(outdir, "index.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"clips": clips}, f, indent=1)
    return path


def run_all(user="palash", seed=7):
    outdir = _outdir()
    _, gm = render_genres(outdir, seed=seed)
    _, im = render_instruments(outdir)
    _, fm = render_fusions(outdir)
    idx = write_index(outdir, gm + im + fm)
    print(f"\nDONE. {len(gm)} genre clips + {len(im)} instrument clips + {len(fm)} fusion clips.")
    print("OUTPUT :", outdir)
    print("INDEX  :", idx)
    print("PUBLIC : /api/public/music/showcase  (served unauthenticated)")
    return outdir
