"""Artifact propagation: anaphoric reuse ("show that image again") is satisfied
by the prior artifact instead of demanding regeneration.

Covers _referenced_artifacts classification, _prior_artifact lookup (most
recent assistant artifact wins), and the _requirement_mismatch gate outcomes
for reuse-with/without prior artifact and new-ask with/without artifact.
"""

import threading
import types

import pytest


@pytest.fixture
def stub_state():
    import server.features.state as st
    prev = st._Registry.entrypoint
    yield st
    st._Registry.entrypoint = prev


def _run(st, tasks, user_input, answer, task_id="t", sessions=None):
    from server.features.critic import _requirement_mismatch
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks,
        sessions=sessions or {},
    )
    return _requirement_mismatch(task_id, "s1", user_input, answer)


def _sess(*artifacts):
    """Session whose assistant messages carry _image_url/_music_url."""
    msgs = [{"role": "user", "content": "hi"}]
    for kind, url in artifacts:
        msgs.append({"role": "assistant", "content": "done",
                     kind: url})
    return {"s1": msgs}


def test_referenced_classifies_image_reuse():
    from server.features.critic import _referenced_artifacts as ref
    assert ref("show me that image again") == {"image"}
    assert ref("display the previous picture please") == {"image"}
    assert ref("replay that song") == {"music"}
    assert ref("play that track again") == {"music"}


def test_referenced_ignores_new_asks_and_chitchat():
    from server.features.critic import _referenced_artifacts as ref
    assert ref("generate an image of a red sailboat") == set()
    assert ref("compose a calm piece") == set()
    assert ref("what is the capital of France?") == set()


def test_prior_artifact_picks_most_recent():
    from server.features.critic import _prior_artifact as prior
    import server.features.state as st
    prev = st._Registry.entrypoint
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks={},
        sessions=_sess(("_image_url", "/output/old.png"),
                       ("_image_url", "/output/new.png")),
    )
    try:
        assert prior("s1", "_image_url") == "/output/new.png"
        assert prior("s1", "_music_url") is None
        assert prior("nope", "_image_url") is None
    finally:
        st._Registry.entrypoint = prev


def test_reuse_with_prior_artifact_passes(stub_state):
    sessions = _sess(("_image_url", "/output/palash/x.png"))
    assert _run(stub_state, {"t": {}}, "show that image again",
                "Here is the image again.",
                sessions=sessions) is None


def test_reuse_phrasing_never_triggers_new_ask(stub_state):
    # By design, show/display/replay phrasing is a REUSE reference, never a
    # new-generation ask: _IMG_ASK_RE has no show/display verbs. With no
    # prior artifact there is nothing to re-attach, and a claim-less answer
    # ("Here is the image again", no embed) trips no claim gate either.
    assert _run(stub_state, {"t": {}}, "show that image again",
                "Here is the image again.") is None


def test_new_ask_without_artifact_demands(stub_state):
    assert _run(stub_state, {"t": {}},
                "generate an image of a red sailboat",
                "A red sailboat is a boat with red sails.") == "image_needed"


def test_new_ask_with_task_artifact_passes(stub_state):
    tasks = {"t": {"image_file": "palash/x.png"}}
    assert _run(stub_state, tasks, "generate an image of a red sailboat",
                "Here is your sailboat.") is None


def test_music_reuse_with_prior_artifact_passes(stub_state):
    sessions = _sess(("_music_url", "/music/palash/y.wav"))
    assert _run(stub_state, {"t": {}}, "play that track again",
                "Playing it again for you.",
                sessions=sessions) is None


def test_music_reuse_without_prior_demands(stub_state):
    assert _run(stub_state, {"t": {}}, "play that track again",
                "Playing it again for you.") == "music_needed"
