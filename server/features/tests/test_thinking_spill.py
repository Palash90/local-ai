"""Reasoning-spill detection: a final answer that is actually leaked
internal monologue (budget cut thinking mid-sentence; model continued
in the content channel)."""

from server.features.orchestration import _looks_like_thinking_spill


SPILLED = """is a whole note.
Actually, looking at the error: `line 11: bad token 'q'`.
Wait, `DHA` is a drum note. `q` is a duration.
Let's look at the `generate_music` tool documentation for the score DSL.
Wait, the previous successful call used `C4 q G4 q`.
Actually, `C4 q` means `C4` with duration `q`.
""" + "More analysis narration. " * 10


def test_spill_detected_on_observed_shape():
    assert _looks_like_thinking_spill(SPILLED) is True


def test_normal_lowercase_opener_not_flagged():
    assert _looks_like_thinking_spill(
        "here's your track — santoor melody over a tanpura drone with "
        "tabla theka, 60 seconds at 60 BPM. The player is above.") is False


def test_uppercase_opener_not_flagged_even_with_markers():
    assert _looks_like_thinking_spill(
        "Actually, here's the answer you asked for. Wait — there are "
        "three options. Actually, let's compare them: Wait, the first "
        "one is cheapest. Hmm, the second is faster. " + "x" * 200
    ) is False


def test_short_content_not_flagged():
    assert _looks_like_thinking_spill("is a whole note.") is False
    assert _looks_like_thinking_spill("") is False
    assert _looks_like_thinking_spill(None) is False
