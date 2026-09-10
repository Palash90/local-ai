"""Scale library: diatonic modes plus world pentatonic / raga / maqam sets.

Each entry is a list of semitone offsets from the tonic. `is_heptatonic` marks
7-note scales that can drive functional (chord) harmony; non-heptatonic scales
use a drone + melody texture instead.
"""

SCALES = {
    # Western diatonic modes (heptatonic)
    "major":        {"offsets": [0, 2, 4, 5, 7, 9, 11], "hept": True},
    "minor":        {"offsets": [0, 2, 3, 5, 7, 8, 10], "hept": True},
    "dorian":       {"offsets": [0, 2, 3, 5, 7, 9, 10], "hept": True},
    "mixolydian":   {"offsets": [0, 2, 4, 5, 7, 9, 10], "hept": True},
    "lydian":       {"offsets": [0, 2, 4, 6, 7, 9, 11], "hept": True},
    "harmonicminor":{"offsets": [0, 2, 3, 5, 7, 8, 11], "hept": True},
    "phrygian":     {"offsets": [0, 1, 3, 5, 7, 8, 10], "hept": True},
    "phrygianlike": {"offsets": [0, 1, 3, 5, 7, 8, 10], "hept": True},
    "locrian":      {"offsets": [0, 1, 3, 5, 6, 8, 10], "hept": True},
    # Japanese
    "hirajoshi":    {"offsets": [0, 2, 3, 7, 8], "hept": False},
    "insen":        {"offsets": [0, 1, 5, 7, 10], "hept": False},
    "yo":           {"offsets": [0, 2, 5, 7, 9], "hept": False},
    "iwato":        {"offsets": [0, 1, 5, 6, 8, 9], "hept": False},
    # Chinese / Southeast Asian
    "gong":         {"offsets": [0, 2, 4, 7, 9], "hept": False},
    "shang":        {"offsets": [0, 2, 5, 7, 10], "hept": False},
    "ju":           {"offsets": [0, 3, 5, 7, 10], "hept": False},
    "slendro":      {"offsets": [0, 2, 5, 7, 9], "hept": False},
    # Middle-Eastern maqamat (approximated to 12-TET)
    "hijaz":        {"offsets": [0, 1, 4, 5, 7, 8, 11], "hept": True},
    "nahawand":     {"offsets": [0, 2, 3, 5, 7, 8, 10], "hept": True},
    "kurd":         {"offsets": [0, 1, 3, 5, 7, 8, 10], "hept": True},
    "ras":          {"offsets": [0, 2, 4, 6, 7, 9, 11], "hept": True},
    # Indian ragas (arohan approximations, 12-TET)
    "yaman":        {"offsets": [0, 2, 4, 6, 7, 9, 11], "hept": True},
    "bhairav":      {"offsets": [0, 1, 4, 5, 7, 8, 11], "hept": True},
    "kafi":         {"offsets": [0, 2, 3, 5, 7, 9, 10], "hept": True},
    "bhairavi":     {"offsets": [0, 1, 3, 5, 7, 8, 10], "hept": True},
    "malkauns":     {"offsets": [0, 3, 5, 8, 10], "hept": False},
    "bageshri":     {"offsets": [0, 2, 3, 5, 7, 9, 10], "hept": True},
    "bilawal":      {"offsets": [0, 2, 4, 5, 7, 9, 11], "hept": True},
    # Korean
    "gyemyonori":   {"offsets": [0, 3, 5, 7, 10], "hept": False},
    "pyongjo":      {"offsets": [0, 2, 5, 7, 9], "hept": False},
    # African / gospel flavour
    "blues":        {"offsets": [0, 3, 5, 6, 7, 10], "hept": False},
    "majpent":      {"offsets": [0, 2, 4, 7, 9], "hept": False},
    "minpent":      {"offsets": [0, 3, 5, 7, 10], "hept": False},
}


def scale_exists(name):
    return name in SCALES


def offsets(name):
    return SCALES[name]["offsets"]


def heptatonic(name):
    return SCALES.get(name, SCALES["major"])["hept"]
