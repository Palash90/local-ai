"""Pitch <-> MIDI + program maps (extension point for multi-instrument)."""

NOTE_OFFSETS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
CHORD_INTERVALS = {
    "maj": (0, 4, 7), "min": (0, 3, 7), "7": (0, 4, 7, 10),
    "5": (0, 7), "56": (0, 7, 10),
    "maj7": (0, 4, 7, 11), "min7": (0, 3, 7, 10), "dim7": (0, 3, 6, 9),
    "m7b5": (0, 3, 6, 10),
    "dim": (0, 3, 6), "aug": (0, 4, 8), "sus4": (0, 5, 7), "sus2": (0, 2, 7),
}
# Lane-name -> GM program. Many aliases so a score can name real instruments.
PROGRAMS = {
    "PIANO": 0, "EPIANO": 4, "CELESTA": 8, "GLCKENSPIEL": 9, "MUSICBOX": 10,
    "VIBES": 11, "MARIMBA": 12, "ORGAN": 16, "ACCORDEON": 21,
    "GUITAR": 25, "NYLON": 24, "STEEL": 25, "EGUITAR": 27, "JAZZGUITAR": 26,
    "BASS": 32, "EBASS": 33, "CONTRABASS": 32,
    "VIOLIN": 40, "VIOLA": 41, "CELLO": 42, "STRINGS": 48, "TREMOLO": 48,
    "SYNTHSTRINGS": 50, "CHOIR": 52, "VOICES": 52,
    "HARP": 46, "TIMPANI": 47,
    "FLUTE": 73, "RECORDER": 74, "OBOE": 68, "CLARINET": 71, "SAX": 65,
    "TRUMPET": 56, "TROMBONE": 57, "FRENCHHORN": 60, "TUBA": 58,
    "BRASS": 61, "SYNTH": 80, "LEAD": 80, "PAD": 89,
    # World / traditional instruments. GM has real voices for Sitar, Banjo,
    # Shamisen, Koto, Kalimba, Bagpipe, Fiddle, Shanai and Dulcimer(=Santoor).
    # Non-GM instruments map to their nearest GM colour (GeneralUser GS has no
    # dedicated guzheng/veena/ektara sample), so they read convincingly.
    "SITAR": 104, "TAMBRA": 104, "TANPURA": 104, "TAMBURA": 104,
    "VEENA": 104, "SAROD": 104, "EKTARA": 104,
    "IKTARA": 104, "DILRUBA": 105,
    "BANJO": 105, "UKULELE": 105, "SHAMISEN": 106,
    "KOTO": 107, "GUZHENG": 107, "KOTO13": 107, "YANGQIN": 107, "CITHARA": 107,
    "KALIMBA": 108, "MBIRA": 108,
    "BAGPIPE": 109, "FIDDL": 110,
    "SHANAI": 111, "SHEHNAI": 111, "SARANGI": 111,
    "SANTOOR": 15, "SANTUR": 15, "SANTOOR1": 15, "DULCIMER": 15, "YANGQIN2": 15,
    "SHAKUHACHI": 77, "OCARINA": 79, "PANFLUTE": 75, "SURN": 111,
    "STEELDRUM": 114, "MARIMBA2": 12,
    "DIZI": 73, "FIDDL": 110, "CLAV": 7, "SLAP": 36, "FRETLESS": 35,
    "SYNTHSTRINGS": 50, "TUBULAR": 14, "VIBES": 11, "MARIMBA": 12,
}
DEFAULT_PROGRAM = 0
# Named drum kits usable as the instrument word in a percussion lane
# ([RHYTHM tabla vol=85]). "kit" is the standard kit of the base SF2.
DRUM_STYLES = {"KIT", "TABLA", "DHOLAK", "DARBUKA"}
# Sensible per-instrument mix levels (0-100) so a multi-lane piece balances
# without the user hand-tuning: sustained/loud voices sit lower, delicate ones
# higher. Overridable per lane with [NAME vol=NN].
DEFAULT_VOL = {
    0: 92, 4: 85, 8: 88, 9: 85, 10: 85, 11: 85, 12: 85, 16: 78, 21: 80,
    24: 82, 25: 82, 26: 82, 27: 80, 32: 85, 33: 82, 40: 80, 41: 80,
    42: 80, 46: 85, 47: 82, 48: 72, 50: 68, 52: 72, 56: 78, 57: 78,
    58: 78, 60: 76, 61: 76, 65: 78, 68: 78, 71: 78, 73: 82, 74: 80,
    80: 72, 89: 66,
    104: 82, 105: 82, 106: 84, 107: 82, 108: 80, 109: 76, 110: 82,
    111: 82, 15: 84, 77: 80, 79: 80, 75: 80, 114: 78, 12: 82,
}


def note_to_midi(name):
    import re
    m = re.match(r"^([A-Ga-g])([#b]?)(-?\d+)$", name.strip())
    if not m:
        raise ValueError(f"bad note {name!r}")
    base, acc, octv = m.groups()
    v = 12 * (int(octv) + 1) + NOTE_OFFSETS[base.upper()]
    if acc == "#":
        v += 1
    elif acc == "b":
        v -= 1
    if not 0 <= v <= 127:
        raise ValueError(f"note out of range: {name!r}")
    return v


def chord_pitches(root, quality, octave=4):
    root_midi = note_to_midi(f"{root}{octave}")
    return [root_midi + i for i in CHORD_INTERVALS.get(quality, (0, 4, 7))]
