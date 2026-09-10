"""Diatonic harmony helpers: spell scales and build chord progressions.

Used by random_arrange to produce structurally coherent songs (melody that
agrees with the harmony under a chosen key + mode).
"""

BASE_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
PC_NAME = {0: "C", 2: "D", 4: "E", 5: "F", 7: "G", 9: "A", 11: "B"}
LETTERS = ["C", "D", "E", "F", "G", "A", "B"]
MODE_STEPS = {
    "major": [2, 2, 1, 2, 2, 2, 1],
    "minor": [2, 1, 2, 2, 1, 2, 2],
    "dorian": [2, 1, 2, 2, 2, 1, 2],
    "mixolydian": [2, 2, 1, 2, 2, 1, 2],
    "lydian": [2, 2, 2, 1, 2, 2, 1],
}


def _accidental(pc, letter):
    diff = (pc - BASE_PC[letter]) % 12
    if diff > 6:
        diff -= 12
    return "#" * diff if diff > 0 else "b" * (-diff)


def spell_scale(tonic, mode="major"):
    """Return 7 (name, pc) entries for the diatonic scale from a tonic."""
    t = tonic.strip()
    letter = t[0].upper()
    acc = 1 if "#" in t else (-1 if "b" in t else 0)
    start_pc = (BASE_PC[letter] + acc) % 12
    start_li = LETTERS.index(letter)
    steps = MODE_STEPS[mode]
    out, pc = [], start_pc
    li = start_li
    for d in range(7):
        L = LETTERS[li % 7]
        out.append((L + _accidental(pc, L) + "SCALE", pc))
        if d < 6:
            pc = (pc + steps[d]) % 12
            li += 1
    # drop the "SCALE" marker, keep names unique per octave via degree index
    return [(name.replace("SCALE", ""), p) for name, p in out]


def chord_on_degree(scale, degree, seventh=False):
    """Triad (optionally with a 7th) spelled from scale degrees (0-indexed)."""
    r_name, r_pc = scale[degree]
    third_pc = scale[(degree + 2) % 7][1]
    fifth_pc = scale[(degree + 4) % 7][1]
    seventh_pc = scale[(degree + 6) % 7][1]
    i3 = (third_pc - r_pc) % 12
    i5 = (fifth_pc - r_pc) % 12
    if i3 == 4 and i5 == 7:
        q = "maj"
    elif i3 == 3 and i5 == 7:
        q = "min"
    elif i3 == 3 and i5 == 6:
        q = "dim"
    elif i3 == 4 and i5 == 8:
        q = "aug"
    else:
        q = "maj"
    tones = [r_pc, third_pc, fifth_pc]
    if seventh and q != "aug":
        q = q + "7"
        tones.append(seventh_pc)
    return {"root": r_name, "pc": r_pc, "quality": q, "degree": degree,
            "tones": tones}


SHARP = {0: "C", 1: "C#", 2: "D", 3: "D#", 4: "E", 5: "F", 6: "F#",
         7: "G", 8: "G#", 9: "A", 10: "A#", 11: "B"}
FLAT = {0: "C", 1: "Db", 2: "D", 3: "Eb", 4: "E", 5: "F", 6: "Gb",
        7: "G", 8: "Ab", 9: "A", 10: "Bb", 11: "B"}
FLAT_KEYS = {"F", "Bb", "Eb", "Ab", "Db", "D", "G", "C", "A", "E"}
PREFER_FLAT = {"F", "Bb", "Eb", "Ab", "Db"}


def _tonic_pc(tonic):
    t = tonic.strip()
    letter = t[0].upper()
    acc = 1 if "#" in t else (-1 if "b" in t else 0)
    return (BASE_PC[letter] + acc) % 12, letter


def spell_custom(tonic, offsets):
    """Spell an arbitrary scale (semitone offsets) as (name, pc) from a tonic.

    Uses flat spellings for flat-leaning keys, sharps otherwise. Works for
    non-heptatonic (pentatonic/raga/maqam) scales where letter-based spelling
    is impossible.
    """
    tonic_pc, letter = _tonic_pc(tonic)
    table = FLAT if tonic.upper().rstrip() in PREFER_FLAT or letter in ("F", "B") else SHARP
    return [(table[(tonic_pc + o) % 12], (tonic_pc + o) % 12) for o in offsets]


def progression(mode, rng, length, banks=None):
    """A musically sensible degree progression for the mode (0-indexed)."""
    if banks is None:
        if mode == "major":
            banks = [[0, 4, 5, 3], [0, 5, 3, 4], [0, 3, 4, 0], [3, 4, 0, 5]]
        elif mode in ("minor", "dorian"):
            banks = [[0, 5, 2], [0, 6, 3], [0, 3, 5, 6], [5, 6, 0, 3]]
        else:
            banks = [[0, 3, 4, 0], [0, 6, 3, 4]]
    bank = rng.choice(banks)
    return [bank[i % len(bank)] for i in range(length)]
