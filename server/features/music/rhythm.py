"""Groove library: genre-specific percussion feel on an 8-eighth (4-beat) grid.

Every bar is exactly eight eighth-note slots (8 * 0.5 = 4 beats). Slots are
tokens like "BD e" or rests "R e". This keeps drums locked to the 4/4 bar that
the other lanes use. Fills are also 8 eighth-slots. Genre changes the placement
and the voices (taiko vs tabla vs trap hats vs bossa shakers).
"""

import random

BD, SN, HH, OH, CR = "BD e", "SN e", "HH e", "OH e", "CR e"
RD, LT, MT, HT = "RD e", "LT e", "MT e", "HT e"
RIM, CLAP, TAM, CB = "RIM e", "CLAP e", "TAM e", "CB e"
CONG, CAB, MAR, CLV = "CONG e", "CAB e", "MAR e", "CLV e"
TBL, WBH, RDC = "TBL e", "WBH e", "RDC e"
REST = "R e"


def _slots(voice=HH):
    return [voice] * 8


def _put(slots, positions, token):
    for p in positions:
        if isinstance(token, (list, tuple)):
            slots[p] = token[p % len(token)]
        else:
            slots[p] = token


def _bar(slots):
    return " ".join(slots) + " |"


def _ring(lead=CR):
    s = [REST] * 8
    s[0] = lead
    return _bar(s)


def _fill(rng, voices, accent=CR):
    roll = [rng.choice(voices) for _ in range(7)]
    s = roll + [accent]
    return _bar(s)


def backbeat(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _ring()
    if bis == bins - 1 and energy >= 0.5:
        return _fill(rng, [BD, SN, LT, MT, HT])
    s = _slots(RD if energy >= 0.9 else HH)
    _put(s, (0, 4), BD)
    if energy >= 0.5:
        _put(s, (2, 6), SN)
    if energy >= 0.7 and rng.random() < 0.4:
        s[7] = rng.choice([OH, BD, SN])
    return _bar(s)


def four_on_floor(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _ring()
    s = [REST] * 8
    _put(s, (0, 2, 4, 6), BD)
    _put(s, (1, 3, 5, 7), OH if energy >= 0.7 else HH)
    _put(s, (2, 6), CLAP if rng.random() < 0.7 else SN)
    return _bar(s)


def rock_8ths(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _bar([CR, BD] + [REST] * 6)
    if bis == bins - 1:
        return _fill(rng, [BD, SN, LT, MT, HT])
    s = _slots(HH)
    _put(s, (0, 4), BD)
    _put(s, (2, 6), SN)
    if energy >= 0.8 and rng.random() < 0.5:
        s[3] = BD
    return _bar(s)


def brush_swing(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _ring(RD)
    s = _slots(RD)                       # ride: spang-a-lang
    s[1], s[5] = SN, SN                   # brushed backbeat on 2 & 4
    if energy >= 0.6:
        s[0] = s[4] = BD                  # feathered bass
    s[3] = rng.choice([RD, SN])
    return _bar(s)


def shuffle(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _ring(BD)
    s = _slots(RD)
    s[0], s[4] = BD, BD
    s[2], s[6] = SN, SN
    s[1] = rng.choice([RD, RIM])
    return _bar(s)


def trap(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _bar([BD, REST, CLAP] + [REST] * 5)
    s = _slots(HH)                        # skittering closed hats
    s[0] = BD
    _put(s, (2, 6), CLAP)                 # half-time clap
    if rng.random() < 0.5:
        s[4] = BD
    if energy >= 0.6 and rng.random() < 0.4:
        s[3] = s[5] = OH
    if rng.random() < 0.3:
        s[7] = rng.choice([OH, SN])
    return _bar(s)


def rnb_shufle(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _bar([BD, REST, SN] + [REST] * 5)
    s = _slots(RD)
    s[0] = s[3] = BD
    s[2] = s[6] = SN
    if rng.random() < 0.5:
        s[5] = SN                          # ghost
    return _bar(s)


def funk_16(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _bar([BD, REST, SN] + [REST] * 5)
    s = _slots(HH)
    _put(s, (0, 3, 6), BD)
    _put(s, (2, 6), SN)
    if rng.random() < 0.6:
        s[1] = s[5] = SN                    # ghost 16ths feel
    if rng.random() < 0.4:
        s[7] = CB
    return _bar(s)


def taiko(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _ring(CR)
    s = [REST] * 8
    if bis == 0:
        s[0], s[1] = CR, CR
    else:
        s[0] = BD
    if energy >= 0.7:
        s[4] = BD
    if bis % 2 == 1 and energy >= 0.5:
        s[6], s[7] = LT, HT
    return _bar(s)


def chinese_perc(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _bar([CR, REST, WBH] + [REST] * 5)
    s = [REST] * 8
    _put(s, (0, 4), BD)
    _put(s, (2, 6), WBH if rng.random() < 0.5 else CLV)
    if energy >= 0.6:
        _put(s, (3, 7), CAB)
    if bis == 0:
        s[0] = CR
    return _bar(s)


def tabla(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _bar([BD, HT, MT] + [REST] * 5)
    theka = [BD, HT, RIM, MT, BD, HT, SN, MT]         # Keherwa-like
    if energy >= 0.7 and rng.random() < 0.4:
        theka = [BD, HT, MT, HT, BD, RIM, SN, HT]
    return _bar(theka)


def dholak(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _bar([CR, BD, REST, SN] + [REST] * 4)
    s = [BD, REST, HT, MT, BD, REST, SN, MT]
    if energy >= 0.8:
        s[3] = s[7] = TAM
    if bis == 0:
        s[0] = CR
    return _bar(s)


def darbuka(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _bar([BD, REST, HT, MT, HT, REST, MT, BD])
    s = [BD, REST, SN, HT, REST, BD, REST, SN]        # maqsoum-ish
    if energy >= 0.7:
        s[5] = rng.choice([BD, RIM])
    return _bar(s)


def janggu(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _bar([BD, REST, REST, HT] + [REST] * 4)
    s = [BD, REST, HT, MT, BD, REST, HT, REST]
    return _bar(s)


def clave(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _bar([CR, CLV, REST, REST, REST, CLV, REST, REST])
    s = [REST] * 8
    _put(s, (0, 2, 4, 7), CLV)                        # son-clave feel
    _put(s, (0, 4), BD)
    if energy >= 0.5:
        _put(s, (3, 5), CONG)
    _put(s, (2, 6), TBL)
    return _bar(s)


def bossa(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _bar([BD] + [REST] * 7)
    s = _slots(MAR)                                   # shaker
    _put(s, (0, 3, 5), BD)                            # bossa kick
    s[6] = RIM                                        # cross-stick
    return _bar(s)


def orchestral(rng, energy, section, bis, bins, is_end=False):
    if is_end:
        return _bar([BD, CR, REST, REST, REST, REST, REST, LT])
    s = [REST] * 8
    if bis % 2 == 0:
        s[0] = BD
    if energy >= 0.8:
        _put(s, (0, 4), BD)
        s[6] = LFT if False else MT
    if bis == 0:
        s[0] = CR
    return _bar(s)


def none(rng, energy, section, bis, bins, is_end=False):
    s = [REST] * 8
    if bis == 0 and rng.random() < 0.5:
        s[0] = "TRIG e"
    return _bar(s)


GROOVES = {
    "backbeat": backbeat, "four_on_floor": four_on_floor, "rock_8ths": rock_8ths,
    "brush_swing": brush_swing, "shuffle": shuffle, "trap": trap,
    "rnb_shufle": rnb_shufle, "funk_16": funk_16, "taiko": taiko,
    "chinese_perc": chinese_perc, "tabla": tabla, "dholak": dholak,
    "darbuka": darbuka, "janggu": janggu, "clave": clave, "bossa": bossa,
    "orchestral": orchestral, "none": none,
}


def make(groove_name):
    return GROOVES.get(groove_name, backbeat)
