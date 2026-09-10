"""Genre-aware, multi-voice arrangement generator.

Beyond genre (scale + instrument palettes + groove) and macro-form (@section
energy), this models real TEXTURE over time:

* melody plays alone, doubles in octaves/parallel thirds, answers call-and-response
  between two contrasting voices, or carries a counter-line -- and reverts;
* a harmony pad can thicken the chord lane on big sections;
* an auxiliary percussion layer joins on high-energy bars;
* bass usually holds one voice but is occasionally doubled by a low piano.

Every produced bar is normalised to exactly 4 beats so lanes stay aligned.
"""

import random

from server.features.music import harmony, form, moods, genres, world_scales, rhythm

PIANO_LIKE = ["PIANO", "EPIANO", "CELESTA", "MUSICBOX"]
MOTIFS = [
    ["q", "q", "h"], ["e", "e", "q", "h"], ["q", "e", "e", "q", "q"],
    ["h", "q", "q"], ["w"], ["e", "e", "e", "e", "q", "q"], ["q", "q", "q", "e", "e"],
]
BEATS = {"w": 4, "h": 2, "q": 1, "e": 0.5, "s": 0.25}
_NEXT = {"w": "h", "h": "q", "q": "e", "e": "s", "s": "s"}


def _tone_degrees(root_deg, L):
    return [root_deg + itv + o for o in (-L, 0, L) for itv in (0, 2, 4)]


def _snap(deg, targets):
    return min(targets, key=lambda t: (abs(t - deg), t > deg))


def _fit(dur, rem):
    while BEATS[dur] > rem + 1e-9 and dur != "s":
        dur = _NEXT[dur]
    return dur if BEATS[dur] <= rem + 1e-9 else "s"


class _Voiced:
    def __init__(self, scale, base_oct, rng):
        self.scale = scale
        self.L = len(scale)
        self.base_oct = base_oct
        self.rng = rng
        self.deg = 0

    def name_oct(self, deg, lift):
        name, _ = self.scale[deg % self.L]
        return f"{name}{self.base_oct + lift + deg // self.L}"

    def notes(self, cdeg, energy, prev_deg, density, functional, rng, is_end=False):
        """Return list of (dur, dyn, deg|None) summing to 4 beats."""
        if is_end:
            return [("w", "!", self.L if functional else 0)]
        L = self.L
        targets = _tone_degrees(cdeg % L, L) if functional else list(range(-L, 2 * L))
        out = []
        motif = self.rng.choice(MOTIFS)
        if energy >= 0.85:
            motif = self.rng.choice([m for m in MOTIFS if "e" in m] or [motif])
        if density < 0.5 and rng.random() < 0.4:
            motif = self.rng.choice([["q", "q", "h"], ["h", "q", "q"], ["w"]])
        beat = 0.0
        for k in range(len(motif) + 4):
            if beat >= 4.0:
                break
            dur = _fit(motif[k % len(motif)], 4.0 - beat)
            d = BEATS[dur]
            on_strong = beat == 0
            if on_strong or rng.random() < density:
                if on_strong:
                    deg, dyn = _snap(self.deg, targets), "!"
                elif functional and rng.random() < 0.24:
                    deg, dyn = _snap(self.deg + rng.choice([-1, 1]), targets), ""
                else:
                    deg, dyn = _snap(self.deg, targets), ""
                self.deg = deg
                out.append((dur, dyn, deg))
            else:
                out.append((dur, "", None))
            beat += d
        while beat < 4.0:
            out.append(("q", "", None))
            beat += 1.0
        return out


def _serialize(scale_info, notes, lift=0, shift=0):
    """notes -> bar string. scale_info=(scale, base_oct, L)."""
    scale, base_oct, L = scale_info
    toks = []
    for dur, dyn, deg in notes:
        if deg is None:
            toks.append(f"R {dur}")
        else:
            name, _ = scale[(deg + shift) % L]
            octv = base_oct + lift + (deg + shift) // L
            toks.append(f"{name}{octv} {dur}{dyn}")
    return " ".join(toks) + " |"


def _full_rest():
    return "R w |"


def _counter_line(notes, L):
    """Sparse obbligato: keep a few notes, pushed a third away, short durations."""
    out = []
    voiced = [i for i, (d, dy, g) in enumerate(notes) if g is not None]
    picks = set(voiced[::2]) if voiced else set()
    for i, (dur, dyn, deg) in enumerate(notes):
        if i in picks and deg is not None:
            out.append(("e", "", deg + 2))
            out.append(_restdur(BEATS[dur] - 0.5))
        else:
            out.append((dur, "", None))
    # normalise to 4 beats
    out = _trim_notes(out)
    return out


def _restdur(beats):
    for name, b in (("h", 2), ("q", 1), ("e", 0.5), ("s", 0.25)):
        if b <= beats + 1e-9:
            return (name, "", None)
    return ("s", "", None)


def _trim_notes(notes):
    total = sum(BEATS[d] for d, _, _ in notes)
    if total > 4.0 + 1e-9:
        acc, res = 0.0, []
        for dur, dyn, g in notes:
            if acc + BEATS[dur] > 4.0 + 1e-9:
                break
            res.append((dur, dyn, g))
            acc += BEATS[dur]
        notes = res
    while sum(BEATS[d] for d, _, _ in notes) < 4.0 - 1e-9:
        notes.append(("q", "", None))
    return notes


def _harmony_layer(notes_chords, scale, energy):
    # a sustained pad bar using the same chord root for whole notes
    root = notes_chords["root"]
    q = notes_chords["quality"]
    octv = 3 if energy < 0.6 else 4
    return f"{root}{octv}:{q} w |"


def _plan_voice(rnd, energy, functional, is_end):
    """Choose the melody texture for a bar."""
    r = rnd.random()
    if is_end:
        return "solo"
    if not functional:
        return "solo" if r < 0.7 else "double8"
    if energy >= 0.85:
        return rnd.choices(["double8", "double3", "counter", "solo"], [3, 2, 2, 2])[0]
    if energy >= 0.6:
        return rnd.choices(["solo", "response", "double8", "counter"], [4, 3, 2, 2])[0]
    return rnd.choices(["solo", "response", "solo"], [6, 2, 2])[0]


def random_score(seed=None, mood=None, genre=None, fusion=None):
    rng = random.Random(seed)
    g = genres.resolve(genre)
    moodp = moods.params(mood) if mood else None

    if g:
        scale_name = rng.choice(g["scales"])
        tonic = rng.choice(g["tonics"])
        lo, hi = g["tempo"]
        tempo = rng.randrange(lo, hi + 1, 4)
        seventh = g.get("seventh", False)
        groove_name = g.get("perc", "backbeat")
        cadence = g.get("cadence", "authentic")
        base_oct = 4
        fusion_partner = genres.resolve(fusion) if fusion else (
            genres.resolve(rng.choice(g.get("fusion", []))) if rng.random() < 0.35 else None)
        mel_a = _borrow(rng, g["melody"], fusion_partner, 0)
        mel_b = _borrow(rng, g["melody"], fusion_partner, 1)
        harm_instr = _borrow(rng, g["harmony"], fusion_partner, 0)
        pad_instr = _borrow(rng, g["harmony"], fusion_partner, 1)
        bass_instr = _borrow(rng, g["bass"], fusion_partner, 0)
    else:
        moodp = moodp or moods.params(moods.pick_mood(rng))
        scale_name = rng.choice(moodp["modes"])
        tonic = rng.choice(["C", "G", "D", "A", "E", "F", "Bb"])
        tempo = moods.choose_tempo(rng, moodp)
        seventh = rng.random() < moodp["colour"][1]
        groove_name = "backbeat"
        cadence = moodp["cadence"]
        base_oct = moodp["base_oct"]
        pop = genres.GENRES["pop"]
        mel_a = rng.choice(pop["melody"] + ["FLUTE", "VIOLIN"])
        mel_b = rng.choice(pop["melody"] + ["SAX", "CELLO"])
        harm_instr = rng.choice(pop["harmony"])
        pad_instr = rng.choice(["STRINGS", "PAD"])
        bass_instr = rng.choice(pop["bass"])
        fusion_partner = None

    functional = world_scales.heptatonic(scale_name)
    scale = harmony.spell_custom(tonic, world_scales.offsets(scale_name))
    L = len(scale)
    tonic_pc = scale[0][1]
    scale_info = (scale, base_oct, L)
    density = moodp["density"] if moodp else 0.6

    song_form = form.choose_form(rng)
    lift = moodp["energy_lift"] if moodp else 0.0
    for s in song_form:
        s["energy"] = max(0.0, min(1.0, s["energy"] + lift))
    cap = max(12, min(28, int(60 * tempo / 240)))
    total = sum(s["bars"] for s in song_form)
    while total > cap and len(song_form) > 2:
        total -= song_form.pop()["bars"]
    bars = form.expand(song_form)
    total = len(bars)

    mode_key = "minor" if any(k in scale_name for k in ("min", "hijaz", "phryg", "kurd", "bhairav", "bhairavi", "malkauns", "blues", "insen", "iwato")) else "major"
    degrees = harmony.progression(mode_key, rng, total, banks=g.get("banks") if g else None)
    if cadence in ("authentic", "pop", "minor", "jazz", "hijaz", "blues"):
        if total >= 2:
            degrees[-1] = 0
            degrees[-2] = 4
    elif cadence == "plagal":
        if total >= 2:
            degrees[-1] = 0
            degrees[-2] = 3

    chords = ([harmony.chord_on_degree(scale, d % L, seventh=(seventh and i < total - 1 and rng.random() < 0.5))
               for i, d in enumerate(degrees)] if functional else [None] * total)

    vzA = _Voiced(scale, base_oct, rng)
    vzB = _Voiced(scale, base_oct, rng)
    groove = rhythm.make(groove_name)
    aux_groove = rhythm.make("clave" if groove_name in ("none",) else "backbeat")

    lane_A, lane_B, harm_a, harm_pad, bass_a, bass_b, drum_a, drum_b = [], [], [], [], [], [], [], []
    prev_deg = None
    bass_double = rng.random() < 0.5
    pad_used = False
    for i in range(total):
        e = bars[i]["energy"]
        is_last = i == total - 1
        vzA.base_oct = base_oct + (1 if e >= 0.85 else 0)
        cdeg = degrees[i] % L
        notesA = vzA.notes(cdeg, e, prev_deg, density, functional, rng, is_end=is_last)
        prev_deg = vzA.deg
        mode = _plan_voice(rng, e, functional, is_last)

        # melody voices
        if mode == "solo":
            lane_A.append(_serialize(scale_info, notesA))
            lane_B.append(_full_rest())
        elif mode == "double8":
            lane_A.append(_serialize(scale_info, notesA))
            lane_B.append(_serialize(scale_info, notesA, lift=1))
        elif mode == "double3":
            lane_A.append(_serialize(scale_info, notesA))
            lane_B.append(_serialize(scale_info, notesA, shift=2))
        elif mode == "response":
            lane_A.append(_serialize(scale_info, notesA[:len(notesA) // 2]) if rng.random() < 0.5
                             else _serialize(scale_info, notesA))
            lane_B.append(_serialize(scale_info, _counter_line(notesA, L)) if rng.random() < 0.5
                          else _serialize(scale_info, notesA))
        elif mode == "counter":
            lane_A.append(_serialize(scale_info, notesA))
            lane_B.append(_serialize(scale_info, _counter_line(notesA, L)))

        # harmony + pad layer
        if functional:
            h = _harm_bar(chords[i], e, rng)
            harm_a.append(h)
            if e >= 0.8:
                pad_used = True
                harm_pad.append(_harmony_layer(chords[i], scale, e))
            else:
                harm_pad.append(_full_rest())
        else:
            tn = scale[0][0]
            harm_a.append(f"{tn}3:maj w |" if e < 0.7 else f"{tn}4:maj w |")
            harm_pad.append(_full_rest())

        # bass (mostly single, occasional low-piano double)
        ba = _bass_bar(scale, cdeg, e, rng, L)
        bass_a.append(ba)
        if bass_double and e >= 0.6 and rng.random() < 0.4:
            bass_b.append(ba)                       # low piano hums the same root line
        else:
            bass_b.append(_full_rest())

        # percussion + auxiliary layer
        if groove_name != "none":
            da = groove(rng, e, bars[i]["section"], bars[i]["bar_in_section"],
                        bars[i]["bars_in_section"], is_end=is_last)
            drum_a.append(da)
            if e >= 0.8 and rng.random() < 0.6:
                drum_b.append(aux_groove(rng, min(1.0, e + 0.1), bars[i]["section"],
                                         bars[i]["bar_in_section"], bars[i]["bars_in_section"]))
            else:
                drum_b.append(_full_rest())

    def _has_voice(l):
        return any(x != _full_rest() for x in l)

    lanes = [("MELODY", mel_a, " ".join(lane_A))]
    if _has_voice(lane_B) and mel_b != mel_a:
        lanes.append(("MELODY2", mel_b, " ".join(lane_B)))
    lanes.append(("HARMONY", harm_instr, " ".join(harm_a)))
    if pad_used:
        lanes.append(("PAD", pad_instr if pad_instr != harm_instr else "STRINGS", " ".join(harm_pad)))
    lanes.append(("BASS", bass_instr, " ".join(bass_a)))
    if bass_double and _has_voice(bass_b):
        lanes.append(("BASS2", "PIANO", " ".join(bass_b)))
    if groove_name != "none":
        lanes.append(("RHYTHM", None, " ".join(drum_a)))
        if _has_voice(drum_b):
            lanes.append(("PERC2", None, " ".join(drum_b)))

    label = genre or mood or "arrangement"
    parts = [f"@tempo {tempo}"]
    if genre:
        parts.append(f"@genre {genre}" + (f" x {fusion}" if fusion else ""))
    if mood:
        parts.append(f"@mood {mood}")
    parts.append(f"# {tonic} {scale_name} ({label}) | " + " ".join(
        f"{s['name']}x{s['bars']}" for s in song_form))
    for s in song_form:
        parts.append(f"@section {s['name']} bars={s['bars']} energy={round(s['energy'], 2)}")
    for name, instr, text in lanes:
        parts.append(f"[{name} {instr}]" if instr else f"[{name}]")
        parts.append(text)
    score = "\n".join(parts)
    structure = ", ".join(f"{s['name']}({s['bars']}b)" for s in song_form)
    info = {"key": f"{tonic} {scale_name}", "tonic_pc": tonic_pc, "bars": total,
            "tempo": tempo, "seed": seed, "mood": mood, "genre": genre,
            "fusion": fusion, "structure": structure, "functional": functional,
            "lanes": [f"{n}/{i}" if i else n for n, i, _ in lanes]}
    return score, tempo, info


def _borrow(rng, palette, partner, pick=0):
    """pick==0 -> genre-authentic primary voice; pick>=1 -> allow piano/duet/
    cross-genre fusion colour. Primary voices stay true to the genre."""
    if pick == 0:
        pool = list(palette)
        if partner and rng.random() < 0.22:
            pool += partner["melody"] + partner["harmony"]
    else:
        pool = list(palette) + PIANO_LIKE
        if partner and rng.random() < 0.35:
            pool += partner["melody"] + partner["harmony"]
    rng.shuffle(pool)
    return pool[pick % len(pool)]


def _harm_bar(chord, energy, rng):
    root, q = chord["root"], chord["quality"]
    octv = 3 if energy < 0.6 else 4
    tok = f"{root}{octv}:{q}"
    if energy >= 0.8 and rng.random() < 0.55:
        return " ".join([f"{tok} q"] * 4) + " |"
    r = rng.random()
    if r < 0.3:
        return f"{tok} q {tok} q {tok} h |"
    if r < 0.6:
        return f"{tok} h {tok} h |"
    return f"{tok} w |"


def _bass_bar(scale, root_deg, energy, rng, L):
    root = f"{scale[root_deg % L][0]}2"
    fifth = f"{scale[(root_deg + 4) % L][0]}2"
    if energy >= 0.8:
        return f"{root} q {root} e {fifth} e {root} q {fifth} q |"
    if energy >= 0.6:
        return f"{root} h {root} q {fifth} q |"
    return f"{root} w |"
