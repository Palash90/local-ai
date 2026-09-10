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
CHORD_QUAL = "maj7|min7|m7b5|dim7|maj|min|dim|aug|sus4|sus2|7"
CHORD_RE = re.compile(
    r"^([A-G][#b]?)(-?\d+)?(?::(" + CHORD_QUAL + r"))?([whqes]\.?)$", re.IGNORECASE)
CHORD_SYM_RE = re.compile(
    r"^([A-G][#b]?)(-?\d+)?(?::(" + CHORD_QUAL + r"))$", re.IGNORECASE)
DUR_ONLY_RE = re.compile(r"^([whqes]\.?)$", re.IGNORECASE)
DUR_DYN_RE = re.compile(r"^([whqes]\.?)([fmp!]?)$", re.IGNORECASE)
NOTE_SYM_RE = re.compile(r"^([A-G][#b]?-?\d+|R)$", re.IGNORECASE)
BRACKET_RE = re.compile(r"^\[([^\]]*)\]\s*(.*)$")
ATTR_RE = re.compile(r"([A-Za-z]+)=(\S+)")
TEMPO_RE = re.compile(r"^@tempo\s+(\d+)\b", re.IGNORECASE)
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
    "BOG": 60, "BOGL": 61, "TBL": 65, "TBLL": 66,
    "MAR": 70, "CAB": 69, "CLV": 75, "WBH": 76, "WBL": 77, "GUI": 73,
    "GUIR": 74, "CUIC": 79, "TRIG": 81, "WHIS": 71, "TRI": 81,
}
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
        m = TEMPO_RE.match(raw.strip())
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


def _parse_header(content):
    """Split 'MELODY flute vol=90' into (name, {attr: value})."""
    parts = content.split()
    name = (parts[0] if parts else "PIANO").upper()
    attrs = {}
    for tok in parts[1:]:
        m = ATTR_RE.match(tok)
        if m:
            attrs[m.group(1).lower()] = m.group(2)
        else:
            # a bare second word names the instrument (e.g. [MELODY flute])
            attrs.setdefault("prog", tok)
    return name, attrs


def _parse_sections(text):
    """Scan @section directives -> (structure, bar_energy map).

    structure: list of {name, bars, energy, start_bar}. bar_energy: list where
    index == 0-based bar gives the energy multiplier for that bar.
    """
    structure, bar = [], 0
    for raw in (text or "").splitlines():
        m = SECTION_DEF_RE.match(raw.strip())
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
    bar_energy = []
    for seg in structure:
        bar_energy.extend([seg["energy"]] * seg["bars"])
    if not bar_energy:
        bar_energy = [1.0]
    return structure, bar_energy


def parse_score(text, tempo=120):
    """Return (sections, errors, structure). Section = {name, program, vol, drum, events}.

    Event = {type: note|chord|rest, midi|pitches, start, dur (beats), energy}.
    structure = the @section timeline (song form) shared across all lanes.
    """
    from server.features.music.theory import (
        note_to_midi, chord_pitches, PROGRAMS, DEFAULT_PROGRAM, DEFAULT_VOL)
    errors, sections = [], []
    cur = None
    structure, bar_energy = _parse_sections(text)
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("@"):
            continue
        m = BRACKET_RE.match(line)
        if m:
            name, attrs = _parse_header(m.group(1).strip())
            drum = (name in DRUM_SECTION_NAMES
                    or any(name.startswith(p) for p in DRUM_NAME_PREFIXES)
                    or str(attrs.get("prog", "")).lower() in ("drums", "drum"))
            if attrs.get("prog") is not None:
                pval = attrs["prog"]
                try:
                    program = int(pval)
                except ValueError:
                    program = PROGRAMS.get(str(pval).upper(), DEFAULT_PROGRAM)
            else:
                program = PROGRAMS.get(name, DEFAULT_PROGRAM)
            try:
                default_v = 80 if drum else DEFAULT_VOL.get(program, 100)
                vol = max(0, min(100, int(attrs.get("vol", default_v))))
            except ValueError:
                vol = 80 if drum else DEFAULT_VOL.get(program, 100)
            cur = {"name": name, "program": 0 if drum else program, "vol": vol,
                   "drum": drum, "events": [], "cursor": 0.0}
            sections.append(cur)
            line = m.group(2).strip()
            if not line:
                continue
        if cur is None:
            cur = {"name": "PIANO", "program": DEFAULT_PROGRAM, "vol": 100,
                   "drum": False, "events": [], "cursor": 0.0}
            sections.append(cur)
        toks = re.split(r"\s*\|\s*|\s+", line)
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
                            cur["events"].append(ev)
                        cur["cursor"] += beats
                        continue
                cm = CHORD_RE.match(tok)
                tm = TOKEN_RE.match(tok)
                if cm and ":" in tok:
                    root = _canon(cm.group(1))
                    octv = int(cm.group(2)) if cm.group(2) else 4
                    qual = (cm.group(3) or "maj").lower()
                    beats = _beats(cm.group(4))
                    pitches = chord_pitches(root, qual, octv)
                    ev = {"type": "chord", "pitches": pitches,
                          "start": cur["cursor"], "dur": beats}
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
    total = sum(len(s["events"]) for s in sections)
    if total > MAX_NOTES:
        errors.append(f"too many notes: {total} > {MAX_NOTES}")
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
