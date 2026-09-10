"""Emotional mood presets that shape a piece's musical parameters.

A mood bundles the levers that actually make music "sound" a certain way:
mode/scale, tempo band, harmonic movement (chord-degree banks + colour),
melodic register and density, articulation, and the dynamics curve. This is the
vocabulary the LLM is also taught in the tool docs so it can express a brief
("make something moody") in a concrete score.
"""

import random

MOODS = {
    "joyful": {
        "modes": ["major", "mixolydian"],
        "tempo": (110, 138), "base_oct": 4, "density": 0.75,
        "colour": (0.2, 0.0), "swing": 0.0, "cadence": "authentic",
        "banks": [[0, 4, 5, 3], [0, 3, 4, 3], [0, 5, 3, 4]],
        "energy_lift": 0.05,
    },
    "moody": {
        "modes": ["minor", "dorian"],
        "tempo": (66, 92), "base_oct": 4, "density": 0.45,
        "colour": (0.15, 0.15), "swing": 0.1, "cadence": "minor",
        "banks": [[0, 5, 3, 4], [0, 3, 5, 4], [5, 3, 0, 4]],
        "energy_lift": -0.05,
    },
    "inspiring": {
        "modes": ["major", "lydian"],
        "tempo": (88, 116), "base_oct": 4, "density": 0.6,
        "colour": (0.15, 0.05), "swing": 0.0, "cadence": "authentic",
        "banks": [[0, 4, 5, 3], [3, 4, 0, 0], [0, 5, 3, 4]],
        "energy_lift": 0.12,
    },
    "dreamy": {
        "modes": ["lydian", "dorian", "major"],
        "tempo": (70, 96), "base_oct": 4, "density": 0.4,
        "colour": (0.25, 0.25), "swing": 0.15, "cadence": "plagal",
        "banks": [[0, 3, 5, 4], [3, 0, 4, 5], [0, 6, 3, 4]],
        "energy_lift": 0.0,
    },
    "tense": {
        "modes": ["minor"],
        "tempo": (100, 128), "base_oct": 4, "density": 0.7,
        "colour": (0.0, 0.3), "swing": 0.0, "cadence": "minor",
        "banks": [[0, 6, 5, 4], [0, 1, 6, 5], [0, 4, 6, 5]],
        "energy_lift": 0.08,
    },
    "epic": {
        "modes": ["minor", "dorian"],
        "tempo": (78, 104), "base_oct": 4, "density": 0.55,
        "colour": (0.1, 0.1), "swing": 0.0, "cadence": "authentic",
        "banks": [[5, 3, 0, 4], [0, 5, 3, 4], [0, 3, 0, 4]],
        "energy_lift": 0.15,
    },
    "calm": {
        "modes": ["major"],
        "tempo": (58, 76), "base_oct": 4, "density": 0.35,
        "colour": (0.2, 0.15), "swing": 0.12, "cadence": "plagal",
        "banks": [[0, 3, 0, 4], [0, 5, 3, 4], [3, 4, 0, 0]],
        "energy_lift": 0.0,
    },
    "playful": {
        "modes": ["major", "mixolydian"],
        "tempo": (120, 150), "base_oct": 4, "density": 0.85,
        "colour": (0.1, 0.1), "swing": 0.2, "cadence": "authentic",
        "banks": [[0, 3, 4, 0], [0, 4, 3, 4], [5, 3, 4, 0]],
        "energy_lift": 0.05,
    },
}


def pick_mood(rng=None):
    rng = rng or random
    return rng.choice(sorted(MOODS))


def params(mood):
    return MOODS.get(mood, MOODS["inspiring"])


def choose_tempo(rng, preset):
    lo, hi = preset["tempo"]
    step = 4
    return rng.randrange(lo, hi + 1, step)


def chord_colour(rng, preset):
    """Return a quality-swap probability and seventh probability for this mood."""
    maj7, seventh = preset["colour"]
    return rng.random() < maj7, rng.random() < seventh
