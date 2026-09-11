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


def test_music_claim_without_artifact_caught(stub_state):
    # the exact lie from the santoor incident: tool parse-failed, model claimed success
    answer = (
        "The soothing Santoor piece has been generated based on your "
        "description.\n\nHere is the image: [IMAGE: /output/palash/x.png]"
    )
    reason = _run(stub_state, {"t": {"image_file": "palash/x.png"}},
                  "generate a santoor audio and also generate an image of a "
                  "woman playing santoor", answer)
    assert reason == "music_claimed"


def test_music_claim_with_artifact_passes(stub_state):
    answer = "The piece has been generated. Enjoy your song!"
    tasks = {"t": {"music_file": "palash/gen_ab.wav",
                   "music_url": "/music/palash/gen_ab.wav"}}
    assert _run(stub_state, tasks, "compose a calm piece", answer) is None


def test_music_needed_not_delivered(stub_state):
    reason = _run(stub_state, {"t": {}},
                  "make me a short jingle for the podcast",
                  "Podcast jingles usually run 5-10 seconds with bright synths.")
    assert reason == "music_needed"


def test_no_music_terms_untouched(stub_state):
    assert _run(stub_state, {"t": {}},
                "what is the capital of France?",
                "The capital of France is Paris.") is None


def test_topic_question_no_retry(stub_state):
    reason = _run(stub_state, {"t": {}},
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


def test_image_markdown_link_without_image_retries(stub_state):
    # the exact failure: model emits only "[Image](z_image/output/...)"
    # having never called generate_image.
    reason = _run(stub_state, {"t": {"_tools_used": ["generate_music"]}},
                  "Make a bossa nova style jazz of about 2 minutes",
                  "[Image](z_image/output/palash/gen_9d8a9731.wav)")
    assert reason == "image_claimed"


def test_image_markdown_link_with_image_attached_passes(stub_state):
    tasks = {"t": {"image_file": "palash/gen_x.png"}}
    reason = _run(stub_state, tasks,
                  "Draw a piano",
                  "Here it is: ![result](/output/palash/gen_x.png)")
    assert reason is None


def test_image_link_no_false_positives(stub_state):
    from server.features.critic import _IMG_LINK_CLAIM_RE as R
    # external images and upload references are left alone
    assert not R.search("See ![chart](https://example.com/a.png) for context")
    assert not R.search("I read [your file](/uploads/doc.pdf) carefully")
    assert not R.search("The image model is z_image and it is great")
    assert R.search("[Image](z_image/output/palash/gen_9d8a9731.wav)")
    assert R.search("![result](/output/palash/gen_x.png)")


def test_image_anaphora_satisfied_by_session(stub_state):
    # turn 2 of the piano session: music re-generated, image referenced
    tasks = {"t": {"music_file": "palash/gen_2.wav", "music_url": "/music/x.wav",
                   "music_duration": 52.0}}
    sessions = {"s1": [
        {"role": "system", "content": "…"},
        {"role": "user", "content": "draw a girl playing piano"},
        {"role": "assistant", "content": "ok", "_image_url": "/output/palash/g.png"},
    ]}
    reason = _run(stub_state, tasks,
                  "Now make a longer piece of about 1 minute, with the same image",
                  "Here is the longer piece. The image remains the same.",
                  sessions=sessions)
    assert reason is None


def test_image_anaphora_without_prior_still_fires(stub_state):
    tasks = {"t": {"music_file": "palash/gen_2.wav", "music_url": "/music/x.wav",
                   "music_duration": 52.0}}
    reason = _run(stub_state, tasks,
                  "make a longer piece, with the same image",
                  "Done! The image remains the same.")
    assert reason == "image_needed"


def test_score_errors_gate(stub_state):
    # the real bossa case: ok render BUT parser dropped 12 tokens
    tasks = {"t": {"music_file": "palash/gen_b7678c21.wav",
                   "music_url": "/music/palash/gen_b7678c21.wav",
                   "music_duration": 32.16,
                   "music_errors": ["line 12: bad token 'Dm4'",
                                    "line 10: bad token '(Verse'"]}}
    answer = ("The soothing piano piece in Bossa Nova Jazz Fusion style is "
              "ready, and here is the image.")
    reason = _run(stub_state, tasks,
                  "Write a soothing, calming Piano music in Bossa Nova style "
                  "with nice rhythm", answer)
    assert reason == "score_errors"


def test_length_mismatch_gate(stub_state):
    base = {"music_file": "palash/x.wav", "music_url": "/music/palash/x.wav"}
    short = dict(base, music_duration=13.25)
    reason = _run(stub_state, {"t": short},
                  "make a longer piece of about 1 minute",
                  "Here is your extended piece!")
    assert reason == "length_mismatch"
    ok = dict(base, music_duration=52.0)
    reason = _run(stub_state, {"t": ok},
                  "make a longer piece of about 1 minute",
                  "Here is your extended piece!")
    assert reason is None


def test_duration_claim_gate(stub_state):
    tasks = {"t": {"music_file": "palash/g.wav", "music_url": "/music/palash/g.wav",
                   "music_duration": 9.3}}
    lie = ("Here is the music: a piano piece that runs for about 9.3 seconds "
           "(approx. 1 minute if looped).")
    assert _run(stub_state, tasks, "generate a short piano piece", lie) == "duration_claimed"
    honest = "The piece runs for about 9 seconds. Want it longer?"
    assert _run(stub_state, tasks, "generate a short piano piece", honest) is None


def test_claimed_duration_parsing():
    from server.features.critic import _claimed_duration_seconds
    assert _claimed_duration_seconds("runs for about 9.3 seconds") == 9.3
    assert _claimed_duration_seconds("approx. 1 minute if looped") == 60.0
    assert _claimed_duration_seconds("no durations here") is None


def test_strip_pasted_artifact_paths():
    from server.features.orchestration import _strip_pasted_artifact_paths
    text = (
        "Here is the music and the image you requested.\n\n"
        "**Music:** A short, dreamy piano piece.\n"
        "*   **Audio Link:** [Listen to the Piano Piece](/music/palash/gen_1a.wav)\n\n"
        "**Image:** A portrait shot of a girl playing the piano.\n"
        "*   **Image Link:** [View the Image](/output/palash/gen_56b.png)"
    )
    out = _strip_pasted_artifact_paths(text, True, True)
    assert "/music/" not in out and "/output/" not in out
    assert "Here is the music and the image you requested." in out
    # not attached -> kept verbatim (a reference the UI cannot show survives)
    kept = _strip_pasted_artifact_paths(text, False, False)
    assert "/music/palash/gen_1a.wav" in kept and "/output/palash/gen_56b.png" in kept
    partial = _strip_pasted_artifact_paths(text, False, True)
    assert "/output/palash/gen_56b.png" in partial
    assert "/music/" not in partial


def test_strip_bogus_z_image_links():
    from server.features.orchestration import _strip_pasted_artifact_paths
    # link-only message with music attached: dead link dropped, player remains
    out = _strip_pasted_artifact_paths(
        "[Image](z_image/output/palash/gen_9d8a9731.wav)", False, True)
    assert "z_image" not in out
    # inline bogus link unwraps to alt text, real sentence preserved
    out = _strip_pasted_artifact_paths(
        "Here is your bossa piece.\n[Image](z_image/output/palash/x.wav)",
        False, True)
    assert "z_image" not in out
    assert "Here is your bossa piece." in out
    # legit attached restatement still stripped as before; a bare working
    # /music/ link is kept (the /music/ route serves it)
    out = _strip_pasted_artifact_paths(
        "**Music:**\n[Play](/music/palash/gen_1a.wav)", False, True)
    assert "/music/palash/gen_1a.wav" in out
    assert "**Music:**" not in out


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


def test_quality_judge_excludes_generator_model(stub_state):
    import threading
    import types
    import server.features.judge as jd
    import server.features.critic as cr
    calls = {}

    def fake_verify(user_input, answer, model_id=None,
                    allow_gpu_fallback=False, exclude_models=None):
        calls["exclude"] = exclude_models
        calls["fallback"] = allow_gpu_fallback
        return None  # simulate "no independent judge"

    orig = jd.llm_verify_answer_quality
    jd.llm_verify_answer_quality = fake_verify
    stub_state._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(),
        tasks={"t": {"_original_message": "make a jingle", "_user": "zoe"}},
        sessions={},
        task_mode=lambda tid: "gpu",
        server_model_id=lambda mode: "gemma4-e4b-q4",
    )
    try:
        jv = cr._judge_answer_quality("t", "some answer")
    finally:
        jd.llm_verify_answer_quality = orig
    assert calls["exclude"] == {"gemma4-e4b-q4"}
    assert calls["fallback"] is True
    assert jv and "self-grade blocked" in jv["note"]


def test_self_artifact_citation_filter():
    from server.features.critic import _is_self_artifact, extract_citations
    assert _is_self_artifact("https://palashkantikundu.in/output/palash/x.png")
    assert _is_self_artifact("/music/palash/gen_a.wav")
    assert not _is_self_artifact("https://example.com/article")
    answer = ("Here! [Image](https://palashkantikundu.in/output/palash/x.png) "
              "and (Doe, Science, 2024) [https://ex.org/a]")
    urls = [c["url"] for c in extract_citations(answer)]
    assert urls == ["https://ex.org/a"]


def test_score_errors_budget_two_then_giveup(stub_state):
    from server.features.critic import _retry_decision

    def decide(done):
        import threading, types
        stub_state._Registry.entrypoint = types.SimpleNamespace(
            _data_lock=threading.RLock(),
            tasks={"t": {"_mismatch_done": done, "_verify_done": 0}},
            sessions={},
        )
        return _retry_decision("t", None, "score_errors")

    assert decide(0) == ("retry", "score_errors")
    assert decide(1) == ("retry", "score_errors")
    assert decide(2) == ("finalize", "score_errors_giveup")


def test_ka_tabla_alias_parses():
    from server.features.music.parse import parse_score
    secs, errs, _ = parse_score("[RHYTHM tabla]\nDHA q GHE q NA q TIN q | NA q KA q DHIN q NA q |", 120)
    assert not errs, errs
    assert [e["midi"] for e in secs[0]["events"]] == [36, 45, 38, 50, 38, 40, 47, 38]
