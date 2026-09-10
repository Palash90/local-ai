import threading
import types

import pytest


@pytest.fixture
def stub_state():
    import server.features.state as st
    prev = st._Registry.entrypoint
    yield st
    st._Registry.entrypoint = prev


def _run(st, tasks, task_id, user_input, answer):
    from server.features.critic import _requirement_mismatch
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks
    )
    return _requirement_mismatch(task_id, user_input, answer)


def test_music_claim_without_artifact_caught(stub_state):
    # the exact lie from the santoor incident: tool parse-failed, model claimed success
    answer = (
        "The soothing Santoor piece has been generated based on your "
        "description.\n\nHere is the image: [IMAGE: /output/palash/x.png]"
    )
    reason = _run(stub_state, {"t1": {"image_file": "palash/x.png"}}, "t1",
                  "generate a santoor audio and also generate an image of a "
                  "woman playing santoor", answer)
    assert reason == "music_claimed"


def test_music_claim_with_artifact_passes(stub_state):
    answer = "The piece has been generated. Enjoy your song!"
    tasks = {"t2": {"music_file": "palash/gen_ab.wav", "music_url": "/music/palash/gen_ab.wav"}}
    assert _run(stub_state, tasks, "t2", "compose a calm piece", answer) is None


def test_music_needed_not_delivered(stub_state):
    reason = _run(stub_state, {"t3": {}}, "t3",
                  "make me a short jingle for the podcast",
                  "Podcast jingles usually run 5-10 seconds with bright synths.")
    assert reason == "music_needed"


def test_no_music_terms_untouched(stub_state):
    assert _run(stub_state, {"t4": {}}, "t4",
                "what is the capital of France?",
                "The capital of France is Paris.") is None


def test_topic_question_no_retry(stub_state):
    reason = _run(stub_state, {"t5": {}}, "t5",
                  "what sound does a santoor make?",
                  "The santoor has a bright, bell-like tone with long sustain.")
    assert reason is None


def test_claim_regex_no_false_positive(stub_state):
    from server.features.critic import _MUSIC_CLAIM_RE
    benign = (
        "I compiled a list of famous santoor recordings and their artists. "
        "The recordings were made in the 1960s."
    )
    assert not _MUSIC_CLAIM_RE.search(benign)
    assert _MUSIC_CLAIM_RE.search("I have composed a piece for you")
    assert _MUSIC_CLAIM_RE.search("Your song is ready, press play")


def test_verification_addendum_formatting():
    from server.features.critic import _verification_addendum
    verdicts = [
        {
            "url": "", "note": "LLM quality judge: answer addresses the user's "
            "request (quality 90/100)", "reason": "VERDICT: OK\nQUALITY: 90",
            "model": "gemma-judge",
        },
        {"url": "http://x.example/a", "note": "verified", "reason": "", "model": "m"},
    ]
    out = _verification_addendum(verdicts, {"_mismatch_done": 1, "_last_mismatch": "music_claimed"})
    assert "### Guardrail verification" in out
    assert "quality 90/100" in out and "`gemma-judge`" in out
    assert "VERDICT: OK QUALITY: 90" in out
    assert "http://x.example/a" in out
    assert "1 requirement re-run (music claimed)" in out
    assert _verification_addendum([], {}) == ""


def test_verification_addendum_caps_sources():
    from server.features.critic import _verification_addendum
    vs = [{"url": f"http://s/{i}", "note": "ok", "model": "m"} for i in range(10)]
    out = _verification_addendum(vs, {})
    assert "…2 more source verdicts" in out
