"""TTS word boundaries: alignment-to-word mapping incl. edge cases.

_build_words_from_alignments converts (start, end, phoneme) tuples into
per-word {w, s, e} timings consumed by the TTS API response ("words").
The chat UI does not highlight words (dead wire — see suite-speech
notes), so coverage here guards the data the API promises.
"""

from server.features.tts import _build_words_from_alignments as build


def test_basic_two_words():
    # hello: phonemes 0-999 + space; world: 1000-1999. sr=1000 => seconds.
    al = [(0, 400, "h"), (400, 999, "ow"), (999, 1000, " "),
          (1000, 1500, "w"), (1500, 1999, "ld")]
    out = build(al, "hello world", 1000)
    assert [w["w"] for w in out] == ["hello", "world"]
    assert out[0]["s"] == 0 and out[0]["e"] == 0.999
    # word start = preceding boundary phoneme's start (consumed as gap)
    assert out[1]["s"] == 0.999 and out[1]["e"] == 1.999


def test_empty_inputs():
    assert build([], "hello", 1000) == []
    assert build([(0, 1, "h")], "", 1000) == []
    assert build([], "", 1000) == []


def test_trailing_words_without_phonemes_get_zeros():
    al = [(0, 500, "h"), (500, 999, "i")]
    out = build(al, "hi there friend", 1000)
    assert [w["w"] for w in out] == ["hi", "there", "friend"]
    assert (out[1]["s"], out[1]["e"]) == (0, 0)
    assert (out[2]["s"], out[2]["e"]) == (0, 0)


def test_sil_spn_break_words_and_monotonic():
    al = [(0, 300, "h"), (300, 600, "i"), (600, 700, "sil"),
          (700, 1200, "b"), (1200, 1600, "y"), (1600, 1700, "spn"),
          (1700, 2200, "e")]
    out = build(al, "hi bye e", 1000)
    assert [w["w"] for w in out] == ["hi", "bye", "e"]
    starts = [w["s"] for w in out]
    assert starts == sorted(starts)
    assert all(w["e"] >= w["s"] for w in out)
