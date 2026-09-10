"""Song macro-form: named sections with bar counts and an energy level.

Distinct from instrument lanes (melody/harmony/bass/rhythm). Each section
carries an energy in [0,1] that the arranger maps onto register, note density,
drum intensity and lane volume, so a chorus lifts out of a verse.
"""

import random

# (section name, bars, energy 0..1)
TEMPLATES = [
    [("intro", 2, 0.35), ("verse", 8, 0.5), ("prechorus", 4, 0.68),
     ("chorus", 8, 0.9), ("verse", 8, 0.55), ("prechorus", 4, 0.68),
     ("chorus", 8, 0.9), ("bridge", 4, 0.6), ("chorus", 8, 1.0),
     ("outro", 2, 0.35)],
    [("verse", 8, 0.5), ("prechorus", 4, 0.7), ("chorus", 8, 0.92),
     ("verse", 8, 0.55), ("chorus", 8, 0.95), ("bridge", 4, 0.62),
     ("chorus", 8, 1.0)],
    [("intro", 4, 0.4), ("verse", 8, 0.55), ("chorus", 8, 0.9),
     ("verse", 8, 0.6), ("chorus", 8, 0.95), ("outro", 4, 0.4)],
    # short, radio-ish
    [("verse", 8, 0.55), ("chorus", 8, 0.95), ("verse", 8, 0.6), ("chorus", 8, 1.0)],
]


def choose_form(rng):
    tpl = rng.choice(TEMPLATES)
    if rng.random() < 0.4:
        # trim a long template so pieces vary from ~20s to ~90s
        keep = rng.choice([len(tpl), max(3, len(tpl) - 2), max(3, len(tpl) - 3)])
        tpl = tpl[:keep]
    out = []
    for name, bars, energy in tpl:
        e = max(0.0, min(1.0, energy + rng.uniform(-0.06, 0.06)))
        out.append({"name": name, "bars": bars, "energy": round(e, 3)})
    return out


def expand(form):
    """Flatten a form into one context dict per bar."""
    bars = []
    for sec in form:
        for b in range(sec["bars"]):
            bars.append({"section": sec["name"], "energy": sec["energy"],
                         "bar_in_section": b, "bars_in_section": sec["bars"]})
    return bars
