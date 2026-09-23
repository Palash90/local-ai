"""Self-chat pipeline config logic: task parsing, persona rotation, merging.

self-chat.py parses pytest's argv at import (module-level parse_args), so
sys.argv is pinned before import. Live-pipeline runs stay manual; everything
deterministic here is pinned.
"""

import sys

sys.argv = ["self-chat.py"]

import importlib

import pytest

sc = importlib.import_module("self-chat")


def test_parse_tasks_defaults_and_splits():
    out = sc._parse_tasks([
        {"task": "  Write a story  ", "languages": "English, Hindi",
         "mediums": "text", "roles": "premium"},
        {"task": "   "},
        {"no-task": True},
    ])
    assert len(out) == 1
    t = out[0]
    assert t["task"] == "Write a story"
    assert t["languages"] == ["English", "Hindi"]
    assert t["mediums"] == ["text"]
    assert t["roles"] == ["premium"]
    assert t["genre"] == "General"


def test_parse_tasks_explicit_all():
    out = sc._parse_tasks([{
        "task": "x", "languages": ["Bengali"], "mediums": ["image", "text"],
        "roles": ["admin"], "genre": "Adventure", "checklist": {"a": 1},
    }])
    assert out[0]["languages"] == ["Bengali"]
    assert out[0]["checklist"] == {"a": 1}
    assert out[0]["genre"] == "Adventure"


def test_deep_merge_recursive():
    target = {"a": {"x": 1, "y": 2}, "b": 1}
    out = sc.deep_merge(target, {"a": {"y": 3, "z": 4}, "c": 5})
    assert out == {"a": {"x": 1, "y": 3, "z": 4}, "b": 1, "c": 5}


def test_persona_round_robin_cycles_without_repeat():
    sc._persona_cycles = {}
    pool = {"Friends": {"Happy": {}}, "Rivals": {"Tense": {}}}
    seen = {sc.pick_persona_round_robin(pool, "General", {})[:2] for _ in range(2)}
    assert seen == {("Friends", "Happy"), ("Rivals", "Tense")}
    # third pick reshuffles (cycle exhausted) — still a valid candidate
    rel, mood, _ = sc.pick_persona_round_robin(pool, "General", {})
    assert (rel, mood) in seen


def test_persona_safety_rule_excludes_parent_child_horror():
    sc._persona_cycles = {}
    pool = {"Parent & Child": {"Calm": {}}, "Friends": {"Happy": {}}}
    for _ in range(4):
        rel, _, _ = sc.pick_persona_round_robin(
            pool, "Adventure & Horror", {"Adventure & Horror": ["Parent & Child", "Friends"]})
        assert rel == "Friends"


def test_persona_role_gating():
    sc._persona_cycles = {}
    pool = {"A": {"M": {"required_role": "premium"}}, "B": {"N": {}}}
    rel, _, _ = sc.pick_persona_round_robin(pool, "G", {}, task_roles=["free"])
    assert rel == "B"
    sc._persona_cycles = {}
    got = {sc.pick_persona_round_robin(pool, "G", {}, task_roles=["premium"])[:1]
           for _ in range(2)}
    assert {("A",), ("B",)} >= got and got
