"""Salvage of truncated generate_music tool-call JSON + the strict gates
that keep model-written scores bounded."""

import json
import sys

import pytest

from server.features.music.salvage import (
    SALVAGE_MAX_CHARS, salvage_music_args)
from server.features.music.render import render_score, MAX_MODEL_SCORE_CHARS


def _truncated_args(score_body, closing=False):
    raw = '{"score":"' + score_body
    if closing:
        raw += '"}'
    return raw


def test_salvage_basic_unterminated():
    args = salvage_music_args(
        _truncated_args("@tempo 60\n[MELODY santoor]\nC4 q E4 q |"))
    assert args and args["_salvaged"]
    assert "C4 q E4 q" in args["score"]
    assert args["tempo"] == 60


def test_salvage_partial_tail_line_is_repaired():
    # cut mid-token: the junk fragment goes, complete tokens on that
    # line survive when they parse.
    args = salvage_music_args(
        _truncated_args("[MELODY piano]\nC4 q E4 q\nC4 q E4"))
    assert args
    assert args["score"].splitlines()[-1].strip() in ("C4 q E4 q", "C4 q")
    assert "[MELODY piano]" in args["score"]


def test_salvage_repairs_junk_tokens():
    # observed model habit: 'vN' after headers is now valid DSL, but other
    # junk ('p', stray ';') must be stripped, not fatal.
    args = salvage_music_args(_truncated_args(
        "[DRONE tanpura] v3\nC2:5 w\n[MELODY piano]\n; C4 q E4 q\nC4 q E4 q"))
    assert args
    assert "[DRONE tanpura] v3" in args["score"]  # v3 is legal now
    assert ";" not in args["score"]


def test_salvage_trims_to_budget():
    body = "[MELODY piano]\n" + "C4 q E4 q G4 q C5 q\n" * 500
    args = salvage_music_args(_truncated_args(body))
    assert args
    assert len(args["score"]) <= SALVAGE_MAX_CHARS
    # directives/headers at the top survive trimming
    assert args["score"].startswith("[MELODY piano]")


def test_salvage_unusable_returns_none():
    assert salvage_music_args(_truncated_args("total nonsense here")) is None
    assert salvage_music_args("") is None
    assert salvage_music_args('{"other": 1}') is None
    assert salvage_music_args(None) is None


def test_salvage_extracts_title_and_tempo_when_present():
    args = salvage_music_args(
        '{"tempo": 90, "title": "Dawn \\"Raga\\\"", "score":'
        ' "@tempo 90\\n[MELODY piano]\\nC4 q |')
    assert args["tempo"] == 90
    assert args["title"] == 'Dawn "Raga"'


def test_strict_rejects_oversized_score():
    score = "[MELODY piano]\n" + "C4 q E4 q G4 q C5 q\n" * 100
    assert len(score) > MAX_MODEL_SCORE_CHARS
    res = json.loads(render_score(score, tempo=120, strict=True))
    assert res["ok"] is False
    assert str(MAX_MODEL_SCORE_CHARS) in res["error"]
    assert "hard budget" in res["error"]


def test_strict_rejects_empty_score():
    res = json.loads(render_score("", tempo=120, strict=True))
    assert res["ok"] is False
    assert "empty score" in res["error"]


def test_strict_clips_material_past_section_grid():
    # 1-bar grid, 30 written melody bars: duration must follow the form
    # (grid), not the runaway lines.
    score = ("@tempo 60\n@section intro bars=1\n[MELODY piano]\n"
             + "C4 q E4 q G4 q C5 q\n" * 30)
    res = json.loads(render_score(score, tempo=60, strict=True))
    assert res["ok"] is True, res
    # 4 beats @ 60bpm = 4s; allow tail/attack slack
    assert res["duration_s"] <= 8.0, res["duration_s"]


def test_v_shorthand_volume():
    from server.features.music.parse import parse_score
    secs, errs, _ = parse_score("[DRONE tanpura] v3\nC2:5 w\n", 60)
    assert not errs, errs
    assert secs[0]["vol"] == 30
    secs, errs, _ = parse_score("[MELODY santoor]\nv85 C4 q E4 q |\n", 120)
    assert not errs, errs
    assert secs[0]["vol"] == 85
    secs, errs, _ = parse_score("[RHYTHM tabla] V2\nDHA q\n", 120)
    assert not errs, errs
    assert secs[0]["vol"] == 20


def test_section_colon_pairs():
    from server.features.music.parse import parse_score
    secs, errs, st = parse_score(
        "@section intro:4, main:12\n[MELODY piano]\nC4 q |\n", 120)
    assert not errs, errs
    assert [(x["name"], x["bars"]) for x in st] == [
        ("intro", 4), ("main", 12)]


def test_section_colon_pairs_with_attrs():
    from server.features.music.parse import parse_score
    _, errs, st = parse_score(
        "@section verse:2 chorus:2 energy=0.9\n[MELODY piano]\nC4 q |\n",
        120)
    assert not errs, errs
    assert [(x["name"], x["bars"]) for x in st] == [
        ("verse", 2), ("chorus", 2)]
    assert all(x["energy"] == 0.9 for x in st)


def test_dispatch_salvage_roundtrip_real_shape():
    """The exact failure shape from task 1e7df256: unterminated score
    string, hallucinated vN headers, colon-pairs sections, run-on
    melody — must come back render-ready."""
    body = (
        "@tempo 60\\n@section intro:4, main:12\\n\\n"
        "[DRONE tanpura] v3\\nC2:5 w\\n\\n"
        "[RHYTHM tabla] v2\\nDHA q\\n\\n"
        "[MELODY santoor] v8\\n"
        + "C4 q E4 q G4 q C5 q\\n" * 40)
    args = salvage_music_args(_truncated_args(body))
    assert args, "real-shape payload must salvage"
    assert len(args["score"]) <= SALVAGE_MAX_CHARS
    from server.features.music.parse import parse_score
    secs, errs, st = parse_score(args["score"], 60)
    assert not errs, errs
    assert [(x["name"], x["bars"]) for x in st] == [
        ("intro", 4), ("main", 12)]
    assert [s["name"] for s in secs] == ["DRONE", "RHYTHM", "MELODY"]
    assert secs[0]["vol"] == 30 and secs[1]["vol"] == 20
    assert secs[2]["vol"] == 80
