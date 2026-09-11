"""Parse the pitch-DSL score string into sections of note events.

A score is 1-4 instrument lanes (melody / harmony / bass / drums). Each lane
starts with a section header carrying the instrument and optional mix::

    [MELODY flute vol=88]       # name + instrument alias + lane volume 0-100
    [HARMONY piano]
    [BASS bass vol=85]
    [DRUMS]                     # a drums lane -> MIDI channel 9, GM drum tokens

Note/chord/drum syntax::

    C4 q  E4 e  R q             pitch + duration ('w h q e s', optional '.')
    C:maj w  C2:maj h           chord symbol + optional octave + duration
    BD e  HH q  SN e            GM drum hits (BD/SN/HH/OH/CR/RD/LT/MT/HT)
    |                           bar line (timing aid; ignored for placement)
    @tempo 96                   global tempo directive
"""

import difflib
import re

DUR_BEATS = {"w": 4.0, "h": 2.0, "q": 1.0, "e": 0.5, "s": 0.25}
# Trailing dynamic marker on any event: f=forte, p=piano, m=mezzo, !=accent.
DYN_MAP = {"f": 118, "m": 96, "p": 72, "!": 126}
# A dynamic marker must follow a duration letter (so chord qualities ending in
# m -- "dim" -- are never mistaken for a 'mezzo' suffix).
DYN_SUFFIX_RE = re.compile(r"(?<=[whqes.])([fmp!])$")
# On the pitch token (before the duration is merged in) only '!' is a safe
# accent marker.
LEAD_ACCENT_RE = re.compile(r"!$")
TOKEN_RE = re.compile(r"^([A-G][#b]?-?\d+|R)([whqes]\.?)$", re.IGNORECASE)
CHORD_QUAL = "maj7|min7|m7b5|dim7|maj|min|dim|aug|sus4|sus2|56|5|7"
ARP_FLAGS = {"ar": "up", "ad": "down", "au": "updown"}
CHORD_RE = re.compile(
    r"^([A-G][#b]?)(-?\d+)?(?::(" + CHORD_QUAL + r"))?(ar|ad|au)?([whqes]\.?)$",
    re.IGNORECASE)
CHORD_SYM_RE = re.compile(
    r"^([A-G][#b]?)(-?\d+)?(?::(" + CHORD_QUAL + r"))?(ar|ad|au)?$",
    re.IGNORECASE)
DUR_ONLY_RE = re.compile(r"^([whqes]\.?)$", re.IGNORECASE)
DUR_DYN_RE = re.compile(r"^([whqes]\.?)([fmp!]?)$", re.IGNORECASE)
NOTE_SYM_RE = re.compile(r"^([A-G][#b]?-?\d+|R)$", re.IGNORECASE)
BRACKET_RE = re.compile(r"^\[([^\]]*)\]\s*(.*)$")
ATTR_RE = re.compile(r"([A-Za-z]+)=(\S+)")
TEMPO_RE = re.compile(r"^@tempo\s+(\d+)\b", re.IGNORECASE)
# Trailing "#" comments ("C4 q |  # motif repeat"). The hash must be preceded
# by whitespace, so sharps in note names (F#4) never match.
_COMMENT_RE = re.compile(r"\s+#.*$")


def _strip_comment(raw):
    return _COMMENT_RE.sub("", raw)
# @section <name> [bars=N] [energy=0..1] [repeat=N]  -> a named block on the
# shared song-form timeline. Segments tile sequentially from bar 1 across every
# lane, so a chorus can lift the whole band's dynamics without touching notes.
SECTION_DEF_RE = re.compile(
    r"^@section\s+([A-Za-z_][\w-]*)\s*(.*)$", re.IGNORECASE)
SECTION_ATTR_RE = re.compile(r"(\w+)=(\S+)")

# GM percussion key map for a [DRUMS]/[RHYTHM] lane (channel 9 note numbers).
# Aliases are kept short + mnemonic so they are easy to write and LLM-legible.
DRUM_MAP = {
    "BD": 36, "KC": 36, "BD1": 36, "KICK": 36,
    "SN": 38, "SD": 38, "RIM": 37, "CLAP": 39, "ESN": 40,
    "HH": 42, "HC": 42, "PEDAL": 44, "OHH": 46, "OH": 46, "CH": 42,
    "LT": 45, "MT": 47, "HT": 50, "LFT": 41, "HFT": 43, "TOM": 47,
    "CR": 49, "CY": 49, "CR2": 57, "CHIN": 52, "SPL": 55, "RDB": 53,
    "RIDE": 51, "RD": 51, "RDC": 59,
    "CB": 56, "COWB": 56, "TAM": 54, "VB": 58, "AG": 67, "AGH": 67,
    "CONG": 63, "CGA": 63, "CGH": 62, "CGO": 64,
    "BOG": 60, "BOGL": 61, "TBL": 65, "TBLL": 66, "KA": 40, "KE": 40,
    "MAR": 70, "CAB": 69, "CLV": 75, "WBH": 76, "WBL": 77, "GUI": 73,
    "GUIR": 74, "CUIC": 79, "TRIG": 81, "WHIS": 71, "TRI": 81,
    # Tabla syllables (theka voices) -> the kit colours the engine's tabla
    # groove uses: DHA bass+open, DHIN/GE mid, NA crisp, TIN high, TA closed.
    "DHA": 36, "DHIN": 47, "NA": 38, "TIN": 50, "GHE": 45, "TA": 37,
}
# Per-stroke dynamics: a tabla/groove is NOT a metronome — bass strokes
# (Dha/Dhin) land fuller than closed taps (Ta/Ka), and sam > weak beats.
STROKE_VEL = {36: 106, 38: 110, 40: 76, 42: 80, 44: 70, 45: 92, 46: 86,
              47: 98, 49: 98, 50: 102, 51: 90, 52: 84, 53: 82, 55: 84,
              56: 90, 57: 88, 59: 90, 60: 88, 61: 84, 62: 80, 63: 88,
              64: 86, 65: 92, 66: 84, 67: 82, 69: 90, 70: 86, 71: 78,
              73: 88, 74: 84, 75: 90, 76: 82, 77: 80, 78: 86, 79: 88,
              81: 84}


def _stroke_vel(midi, rel):
    base = STROKE_VEL.get(midi, 92)
    r = rel % 4.0
    if abs(r) < 1e-9 or abs(r - 4.0) < 1e-9:
        pos = 1.0
    elif abs(r - 2.0) < 1e-9:
        pos = 0.93
    elif abs(r - round(r)) < 1e-9:
        pos = 0.87
    else:
        pos = 0.80
    return max(28, min(127, int(base * pos)))


# keep 'R' rest handled specially in the drum branch
DRUM_SECTION_NAMES = {"DRUMS", "DRUM", "PERC", "PERCUSSION", "RHYTHM"}
DRUM_NAME_PREFIXES = ("DRUM", "PERC", "RHYTHM")
_DUM_ALT = "|".join(sorted(list(DRUM_MAP) + ["R"], key=len, reverse=True))
DRUM_RE = re.compile(rf"^({_DUM_ALT})([whqes]\.?)$", re.IGNORECASE)
DRUM_SYM_RE = re.compile(rf"^({_DUM_ALT})$", re.IGNORECASE)

MAX_NOTES = 6000


def parse_tempo(text, default=120):
    """Return the tempo set by an '@tempo N' directive, or default."""
    for raw in (text or "").splitlines():
        m = TEMPO_RE.match(_strip_comment(raw).strip())
        if m:
            try:
                t = int(m.group(1))
                if 20 <= t <= 300:
                    return t
            except ValueError:
                pass
    return default


def _beats(dur):
    dotted = dur.endswith(".")
    base = DUR_BEATS[dur[0].lower()]
    return base * 1.5 if dotted else base


def _canon(root):
    """Normalize a chord root: uppercase letter, keep the accidental."""
    return root[0].upper() + root[1:] if root else root


ROLE_WORDS = {"MELODY", "HARMONY", "BASS", "PAD", "DRONE", "LEAD",
              "TEXTURE", "CHORDS", "RHYTHM", "PERC", "PERCUSSION", "DRUMS",
              "DRUM"}
ROLE_PREFIXES = ("MELODY", "HARMONY", "BASS", "RHYTHM", "PERC", "DRUM",
                 "TEXTURE", "PAD")

# Descriptive adjectives carry no structural meaning (dynamics live in
# vol=NN) — ignore silently instead of erroring, e.g. [PERC soft drum].
IGNORED_HEADER_WORDS = {"SOFT", "LOUD", "QUIET", "SOLO"}


def _is_role(u):
    return u in ROLE_WORDS or any(u.startswith(p) for p in ROLE_PREFIXES)


def _parse_header(content):
    """Split a lane header into (label, words, {attr: value}).

    Words are any of: role (MELODY2, DRONE, ...), instrument (a PROGRAMS
    key), a drum-kit word (TABLA...), or a raw GM program number — in ANY
    order, because the LLM word order is not. attrs hold vol=NN/prog=NN."""
    parts = content.split()
    words, attrs = [], {}
    for tok in parts:
        m = ATTR_RE.match(tok)
        if m:
            attrs[m.group(1).lower()] = m.group(2)
        else:
            words.append(tok)
    label = (words[0] if words else "PIANO").upper()
    return label, words, attrs


def _parse_sections(text):
    """Scan @section directives -> (structure, bar_energy map).

    structure: list of {name, bars, energy, start_bar}. bar_energy: list where
    index == 0-based bar gives the energy multiplier for that bar.
    """
    structure, bar = [], 0
    for raw in (text or "").splitlines():
        m = SECTION_DEF_RE.match(_strip_comment(raw).strip())
        if not m:
            continue
        name = m.group(1).lower()
        attrs = {k.lower(): v for k, v in SECTION_ATTR_RE.findall(m.group(2))}
        try:
            nbars = max(1, int(attrs.get("bars", 8)))
        except ValueError:
            nbars = 8
        try:
            rep = max(1, int(attrs.get("repeat", 1)))
        except ValueError:
            rep = 1
        try:
            energy = max(0.0, min(1.0, float(attrs.get("energy", 0.7))))
        except ValueError:
            energy = 0.7
        for _ in range(rep):
            structure.append({"name": name, "bars": nbars, "energy": energy,
                              "start_bar": bar})
            for _b in range(nbars):
                bar += 1
    # Global energy ramp: instead of a step per section, energy walks
    # proportionally toward each section's target so it lands exactly on the
    # section's last bar — dynamics BUILD (crescendo into chorus, gentle
    # settle in the outro) instead of snapping between fixed levels.
    bar_energy = []
    cur_e = structure[0]["energy"] if structure else 1.0
    for seg in structure:
        tgt, n = seg["energy"], seg["bars"]
        for j in range(n):
            step = (tgt - cur_e) / (n - j)
            cur_e += max(-0.2, min(0.2, step))
            bar_energy.append(max(0.0, min(1.0, cur_e)))
    if not bar_energy:
        bar_energy = [1.0]
    return structure, bar_energy


def _humanize(sections, text):
    """Deterministic life for percussion: -16..+24 ms timing drift and +/-3
    velocity drift per hit, seeded from the score itself (same score ->
    byte-identical render), plus a small 'ahead' lean on odd bars that gives
    looped thekas forward motion instead of a metronome's dead grid. Downbeats
    are nudged far less than off-grid strokes so the cycle keeps its spine."""
    import hashlib
    seed = hashlib.md5((text or "").encode()).hexdigest()
    for li, s in enumerate(sections):
        if not s["drum"]:
            continue
        for ei, e in enumerate(s["events"]):
            if e["type"] == "rest":
                continue
            h = hashlib.md5(f"{seed}:{li}:{ei}:{e['start']:.3f}".encode()
                            ).digest()
            a = h[0] / 255.0
            b = h[1] / 255.0
            rel = e["start"] % 4.0
            drift = a * 0.040 - 0.016
            if int(round(e["start"]) // 4 * 4) % 8 == 4:
                drift += 0.018
            on_beat = abs(rel - round(rel)) < 1e-9
            if on_beat:
                damp = 0.15
                drift = max(drift, 0.0)   # never drag a beat onset backward
            else:
                damp = 1.0
            e["start"] = max(0.0, e["start"] + drift * damp)
            if "vel" in e:
                e["vel"] = max(28, min(127, e["vel"] + int(b * 7) - 3))


def _in_bar(e, b):
    return b * 4 <= e["start"] < (b + 1) * 4


def _variation_pass(sections, structure, bar_energy, errors):
    """Musical interest for LOOPS ONLY.

    Lanes whose written material was shorter than the section grid were
    tiled (``_tiled``) — verbatim looping otherwise sounds like a drum
    machine. For those lanes: a rhythm fill replaces the bar before every
    section change; high-energy bars (>=0.65) widen register (melody doubles
    its strong beats an octave up as a soft ghost, harmony opens its
    top voicing, rhythm adds drive) without transposing the tune; low-energy
    bars (<=0.5, outros and quiet intros) thin rhythm off-beats so the piece lands.
    Engine-composed full-length scores have no tiled lanes and are never
    touched. Deterministic: same score -> same audio."""
    if not structure:
        return
    grid_bars = sum(sg["bars"] for sg in structure)
    if grid_bars < 2:
        return
    boundaries = {sg["start_bar"] - 1 for sg in structure if sg["start_bar"] > 0}
    mels = [x for x in sections if x.get("_tiled") and not x["drum"]
            and x["name"].startswith("MELODY")]
    harms = [x for x in sections if x.get("_tiled") and not x["drum"]
             and x["name"].startswith(("HARMONY", "PAD", "CHORDS"))]
    rhy = [x for x in sections if x.get("_tiled") and x["drum"]]
    if not (mels or harms or rhy):
        return
    # bar -> section index, from the song form grid
    bar_sec = {}
    for si, seg in enumerate(structure):
        for b in range(seg["start_bar"], seg["start_bar"] + seg["bars"]):
            bar_sec[b] = si
    for x in rhy:
        for b in sorted(boundaries):
            if b + 1 > grid_bars or b < 1:
                continue
            keep = [e for e in x["events"] if not _in_bar(e, b)]
            for pos, note in ((0, 45), (1, 47), (2, 50), (3, 49)):
                keep.append({"type": "note", "midi": note,
                             "start": b * 4 + pos, "dur": 1.0,
                             "vel": _stroke_vel(note, pos)})
            x["events"] = sorted(keep, key=lambda e: e["start"])
    # Alternate-cycle call/response on repeated melody/harmony bars — but
    # ONLY within the same section: a reprise across sections (verse 1 ->
    # verse 2, bridge returns) is song form and stays whole, while the same
    # figure looping inside one section is what sounds mechanical. Whether
    # the repeat came from tiling or the model copy-pasting the same figure
    # makes no audible difference. Odd occurrences within a section drop
    # their beat-3 stab (if >=2 events remain). Removal only — harmony can
    # never clash. First occurrences are untouched.
    for s in mels + harms:
        seen = {}
        drop_ids = set()
        for b in range(grid_bars):
            bevs = [e for e in s["events"] if _in_bar(e, b)]
            if not bevs:
                continue
            sig = tuple(
                ("n", e["midi"], round(e["start"] - b * 4, 3), round(e["dur"], 3))
                if e["type"] == "note" else
                ("c", tuple(e["pitches"]), round(e["start"] - b * 4, 3),
                 round(e["dur"], 3)) if e["type"] == "chord" else
                ("r", round(e["start"] - b * 4, 3), round(e["dur"], 3))
                for e in sorted(bevs, key=lambda e: e["start"])
            )
            key = (bar_sec.get(b, -1), sig)
            seen.setdefault(key, []).append(b)
            if len(seen[key]) % 2 == 0:
                stabs = [e for e in bevs
                         if e["type"] in ("note", "chord")
                         and abs((e["start"] - b * 4)
                                 - round(e["start"] - b * 4)) < 1e-6
                         and round(e["start"] - b * 4) == 3]
                if stabs and len(bevs) - len(stabs) >= 2:
                    drop_ids.update(id(e) for e in stabs)
        if drop_ids:
            s["events"] = [e for e in s["events"] if id(e) not in drop_ids]
    for b in range(grid_bars):
        ebar = bar_energy[min(b, len(bar_energy) - 1)]
        if ebar >= 0.65:
            for mel in mels:
                strong = [e for e in list(mel["events"])
                          if _in_bar(e, b) and e["type"] in ("note", "chord")
                          and abs((e["start"] - b * 4)
                                  - round(e["start"] - b * 4)) < 1e-6]
                for e in strong:
                    if e["type"] == "note":
                        g = dict(e)
                        g["midi"] = e["midi"] + 12
                        g["dur"] = e["dur"] * 0.75
                    else:  # chord: double only its top pitch
                        if not e["pitches"]:
                            continue
                        g = {"type": "note",
                             "midi": max(e["pitches"]) + 12,
                             "start": e["start"],
                             "dur": e["dur"] * 0.75,
                             "_cycle": e.get("_cycle", 0)}
                    g["start"] = e["start"]
                    g["vel"] = 64
                    g["ghost"] = True
                    mel["events"].append(g)
            for h in harms:
                tops = []
                for e in list(h["events"]):
                    if _in_bar(e, b) and e["type"] == "chord" and e["pitches"]:
                        tops.append({"type": "note",
                                     "midi": max(e["pitches"]) + 12,
                                     "start": e["start"],
                                     "dur": e["dur"] * 0.5, "vel": 58})
                h["events"].extend(tops)
            for r in rhy:
                be = [e for e in r["events"] if _in_bar(e, b)]
                if len(be) <= 4 and not any(
                        abs(e["start"] - (b * 4 + 3.5)) < 0.01 for e in be):
                    r["events"].append({"type": "note", "midi": 40,
                                        "start": b * 4 + 3.5, "dur": 0.5,
                                        "vel": _stroke_vel(40, 3.5)})
            for m in mels:
                m["events"].sort(key=lambda e: e["start"])
        elif ebar <= 0.5:
            for r in rhy:
                r["events"] = [
                    e for e in r["events"]
                    if not (_in_bar(e, b)
                            and abs((e["start"] - b * 4) % 1.0) > 1e-9)]
                r["events"].sort(key=lambda e: e["start"])


def parse_score(text, tempo=120):
    """Return (sections, errors, structure). Section = {name, program, vol, drum, events}.

    Event = {type: note|chord|rest, midi|pitches, start, dur (beats), energy}.
    structure = the @section timeline (song form) shared across all lanes.
    """
    from server.features.music.theory import (
        note_to_midi, chord_pitches, PROGRAMS, DEFAULT_PROGRAM, DEFAULT_VOL,
        DRUM_STYLES)
    errors, sections = [], []
    cur = None
    structure, bar_energy = _parse_sections(text)
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = _strip_comment(raw).strip()
        if not line or line.startswith("#") or line.startswith("@"):
            continue
        m = BRACKET_RE.match(line)
        if m:
            label, words, attrs = _parse_header(m.group(1).strip())
            kit = None
            instr = None
            role = None
            number = None
            leftovers = []
            for w in words:
                u = w.upper()
                if u in DRUM_STYLES:
                    if u != "KIT":
                        kit = u
                    continue
                if _is_role(u) and (role is None or u not in PROGRAMS):
                    # extra pure-role words are harmless ([PERC soft drum]):
                    # first one names the lane, the rest are ignored. But a
                    # word that is ALSO an instrument ([MELODY2 LEAD]) must
                    # still fall through to instrument resolution below.
                    if role is None:
                        role = u
                    continue
                if u in IGNORED_HEADER_WORDS:
                    continue
                if u.isdigit():
                    number = int(u)
                    continue
                if u in PROGRAMS:
                    if instr is None:
                        instr = u
                    continue
                leftovers.append(w)
            pval = str(attrs.get("prog", "") or "").upper()
            if pval and pval in PROGRAMS and instr is None:
                instr = pval
            elif pval.isdigit():
                number = int(pval)
            elif pval and pval not in DRUM_STYLES and not _is_role(pval):
                leftovers.append(attrs["prog"])
            drum = (kit is not None
                    or (role is not None and (
                        role in DRUM_SECTION_NAMES
                        or any(role.startswith(p) for p in DRUM_NAME_PREFIXES))))
            if drum:
                program = 0
            elif instr is not None:
                program = PROGRAMS[instr]
            elif number is not None:
                program = number
            elif role is not None and role in PROGRAMS:
                program = PROGRAMS[role]
            else:
                program = DEFAULT_PROGRAM
            name = role or instr or label
            for w in leftovers:
                u = w.upper()
                near = difflib.get_close_matches(
                    u, sorted(set(PROGRAMS) | DRUM_STYLES | ROLE_WORDS), 1)
                errors.append(
                    f"line {lineno}: unknown lane word {w!r}"
                    + (f" (did you mean {near[0].title()}?)" if near
                       else " — header = [ROLE instrument vol=NN], any order"))
            if not drum and instr is None and number is None and not (
                    role and role in PROGRAMS) and not leftovers:
                errors.append(
                    f"line {lineno}: lane '{label}' has no instrument"
                    " — e.g. [MELODY santoor vol=90]")
            try:
                default_v = 80 if drum else DEFAULT_VOL.get(program, 100)
                vol = max(0, min(100, int(attrs.get("vol", default_v))))
            except ValueError:
                vol = 80 if drum else DEFAULT_VOL.get(program, 100)
            cur = {"name": name, "program": 0 if drum else program, "vol": vol,
                   "drum": drum, "kit": kit, "events": [], "cursor": 0.0}
            sections.append(cur)
            line = m.group(2).strip()
            if not line:
                continue
        if cur is None:
            cur = {"name": "PIANO", "program": DEFAULT_PROGRAM, "vol": 100,
                   "drum": False, "events": [], "cursor": 0.0}
            sections.append(cur)
        _parts = line.split("|") if "|" in line else [line]
        for _pi, _part in enumerate(_parts):
            toks = [t for t in re.split(r"\s+", _part.strip()) if t]
            if not toks:
                if _pi == len(_parts) - 1:
                    continue
                cur["cursor"] = cur["cursor"] + 4.0
                continue
            _bar_start = cur["cursor"]
            i = 0
            while i < len(toks):
                tok = toks[i]
                i += 1
                if not tok:
                    continue
                # a dynamic may be attached to the pitch (accent "!") OR to the
                # duration ("q!"); accept "C4! q", "C4 q!", "C4q!".
                lead_dyn = None
                if LEAD_ACCENT_RE.search(tok):
                    lead_dyn = DYN_MAP["!"]
                    tok = LEAD_ACCENT_RE.sub("", tok)
                if (CHORD_SYM_RE.match(tok) and i < len(toks)
                        and toks[i].lower() in ARP_FLAGS):
                    tok = tok + toks[i]
                    i += 1
                if ((CHORD_SYM_RE.match(tok) or NOTE_SYM_RE.match(tok)
                     or DRUM_SYM_RE.match(tok))
                        and i < len(toks) and DUR_DYN_RE.match(toks[i])):
                    tok = tok + toks[i]
                    i += 1
                try:
                    dyn = lead_dyn
                    ds = DYN_SUFFIX_RE.search(tok)
                    if ds and ds.group(1).lower() in DYN_MAP:
                        dyn = DYN_MAP[ds.group(1).lower()]
                        tok = tok[: ds.start()]
                    if cur["drum"]:
                        dm = DRUM_RE.match(tok)
                        if dm:
                            beats = _beats(dm.group(2))
                            hit = dm.group(1).upper()
                            if hit == "R":
                                cur["events"].append({"type": "rest",
                                                      "start": cur["cursor"], "dur": beats})
                            else:
                                ev = {"type": "note", "midi": DRUM_MAP[hit],
                                      "start": cur["cursor"], "dur": beats}
                                if dyn is not None:
                                    ev["vel"] = dyn
                                else:
                                    ev["vel"] = _stroke_vel(DRUM_MAP[hit],
                                                            cur["cursor"])
                                cur["events"].append(ev)
                            cur["cursor"] += beats
                            continue
                    cm = CHORD_RE.match(tok)
                    tm = TOKEN_RE.match(tok)
                    if cm and (":" in tok or cm.group(4)):
                        root = _canon(cm.group(1))
                        octv = int(cm.group(2)) if cm.group(2) else 4
                        qual = (cm.group(3) or "maj").lower()
                        beats = _beats(cm.group(5))
                        pitches = chord_pitches(root, qual, octv)
                        arp = (cm.group(4) or "").lower()
                        if not arp:
                            ev = {"type": "chord", "pitches": pitches,
                                  "start": cur["cursor"], "dur": beats}
                            if dyn is not None:
                                ev["vel"] = dyn
                            cur["events"].append(ev)
                        else:
                            # arpeggio: play the chord tones in sequence across the
                            # written duration — one token, a rolled figure.
                            seq = (pitches if arp == "ar" else
                                   list(reversed(pitches)) if arp == "ad" else
                                   pitches + list(reversed(pitches))[1:-1])
                            step = beats / len(seq)
                            for pi, p in enumerate(seq):
                                ev = {"type": "note", "midi": p,
                                      "start": cur["cursor"] + pi * step,
                                      "dur": step}
                                if dyn is not None:
                                    ev["vel"] = dyn
                                cur["events"].append(ev)
                        cur["cursor"] += beats
                    elif tm:
                        pitch, dur = tm.group(1), tm.group(2)
                        beats = _beats(dur)
                        if pitch.upper() == "R":
                            cur["events"].append({"type": "rest", "start": cur["cursor"], "dur": beats})
                        else:
                            ev = {"type": "note", "midi": note_to_midi(pitch),
                                  "start": cur["cursor"], "dur": beats}
                            if dyn is not None:
                                ev["vel"] = dyn
                            cur["events"].append(ev)
                        cur["cursor"] += beats
                    else:
                        errors.append(f"line {lineno}: bad token {tok!r}")
                except ValueError as e:
                    errors.append(f"line {lineno}: {e}")
            _delta = cur["cursor"] - _bar_start
            if "|" not in line:
                continue
            # '|' closes a bar: a short bar is rest-padded so lanes stay
            # aligned; an overlong bar keeps flowing into the next (that IS
            # how fills read — the engine's own grooves rely on it).
            if _delta < 3.999:
                cur["cursor"] = _bar_start + 4.0

    total = sum(len(s["events"]) for s in sections)
    if total > MAX_NOTES:
        errors.append(f"too many notes: {total} > {MAX_NOTES}")
    # ---- Section-grid tiling ----
    # @section lines declare the song's bar grid. When a lane wrote LESS
    # material than the grid (a 2-bar vamp under a 21-bar song — which is
    # exactly how loop-based genres work), loop-tile that lane's bar-aligned
    # span across the grid, so the declared form — and its energy curve —
    # matches the rendered audio instead of the piece truncating to the
    # vamp's length. Lanes that already cover the grid are untouched, and
    # engine-composed full-length scores always do: zero behavior change
    # for them.
    if structure:
        grid_bars = sum(seg["bars"] for seg in structure)
        if 0 < grid_bars <= 256:
            grid_beats = grid_bars * 4.0
            for s in sections:
                evs = s["events"]
                if not evs:
                    continue
                lane_end = max(e["start"] + e["dur"] for e in evs)
                content_bars = max(1, (int(lane_end) + 3) // 4)
                if content_bars >= grid_bars:
                    continue
                span = content_bars * 4.0
                s["_tiled"] = True
                tiled = list(evs)
                offset = span
                cycle = 1
                while offset < grid_beats:
                    for e in evs:
                        st = e["start"] + offset
                        if st >= grid_beats:
                            continue
                        dup = dict(e)
                        dup["start"] = st
                        dup["_cycle"] = cycle
                        if st + dup["dur"] > grid_beats:
                            dup["dur"] = grid_beats - st
                            if dup["dur"] <= 0:
                                continue
                        tiled.append(dup)
                    offset += span
                    cycle += 1
                s["events"] = tiled
            total = sum(len(s["events"]) for s in sections)
            if total > MAX_NOTES:
                errors.append(f"too many notes: {total} > {MAX_NOTES}")
    _variation_pass(sections, structure, bar_energy, errors)
    _humanize(sections, text)
    for s in sections:
        s.pop("_tiled", None)
        for e in s["events"]:
            e.pop("_cycle", None)
    # Assign each event the energy of the song-form bar it lands in, so lanes
    # play a section's dynamics even though the notes were written flat.
    def _energy_for(start):
        bar = int(start // 4)
        if bar < 0:
            bar = 0
        idx = min(bar, len(bar_energy) - 1)
        return bar_energy[idx]
    for s in sections:
        for e in s["events"]:
            e["energy"] = _energy_for(e["start"])
        del s["cursor"]
    return sections, errors, structure
