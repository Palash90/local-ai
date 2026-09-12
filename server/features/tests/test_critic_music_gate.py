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


def test_image_label_link_https_without_tool_retries(stub_state):
    # link-form presenting as an image also fires (no bang in the real failure)
    from server.features.critic import _MD_IMG_LABEL_LINK_RE as R2
    assert R2.search("[Image](https://storage.googleapis.com/x/gen_c15e672f.png)")
    assert not R2.search("[source article](https://example.com/a)")
    assert not R2.search("See the [documentation](https://example.com/docs) here")
    # bang-form https embed fires through the gate too
    reason = _run(stub_state, {"t": {"_tools_used": ["generate_music"],
                                     "music_file": "palash/gen_x.wav",
                                     "music_url": "/music/palash/gen_x.wav"}},
                  "Make a bollywood track of about 2 minutes",
                  "Here: ![pic](https://example.com/a.png)")
    assert reason == "image_claimed"


def test_image_embed_https_without_tool_retries(stub_state):
    # model pastes an https image URL having never called generate_image
    # (music was produced, so the music gates pass through to the image check)
    reason = _run(stub_state, {"t": {"_tools_used": ["generate_music"],
                                     "music_file": "palash/gen_x.wav",
                                     "music_url": "/music/palash/gen_x.wav"}},
                  "Make a bollywood track of about 2 minutes",
                  "[Image](https://storage.googleapis.com/x/output/gen_c15e672f.png)")
    assert reason == "image_claimed"


def test_image_embed_with_tool_attached_passes(stub_state):
    tasks = {"t": {"image_file": "palash/gen_x.png",
                   "_tools_used": ["generate_image"]}}
    reason = _run(stub_state, tasks, "Draw a piano",
                  "Here: ![result](https://example.com/a.png)")
    assert reason is None


def test_finalize_decline_attaches_music_not_image(stub_state):
    import server.features.state as st
    from server.features import orchestration as orch
    tasks = {"t": {"music_file": "palash/gen_x.wav",
                   "music_score": "s", "music_levels": [],
                   "image_file": "palash/img.png",
                   "_original_message": "make music"}}
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks,
        sessions={"s1": []}, sessions_meta={},
        task_mode=lambda tid: "gpu",
        save_sessions=lambda: None,
        context_token_report=lambda sid, msgs: {},
    )
    orch._finalize_task("t", "s1", "caption", {"timings": {}},
                        attach_image=False)
    m = st._Registry.entrypoint.sessions["s1"][-1]
    assert m["_music_url"] == "/music/palash/gen_x.wav"
    assert m["_image_url"] is None
    assert m["content"] == "caption"


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


def _fusion_tasks(levels):
    return {"t": {"music_file": "palash/gen_x.wav",
                  "music_url": "/music/palash/gen_x.wav",
                  "music_duration": 60.0,
                  "music_levels": levels}}


def _lv(instrument):
    return {"name": "X", "program": 0, "drum": False, "vol": 80, "notes": 10,
            "instrument": instrument, "use": "lead", "style": "melody"}


def test_fusion_imbalance_fires_on_missing_family(stub_state):
    from server.features.critic import _requirement_mismatch
    jazz_only = [_lv("sax"), _lv("piano"), _lv("ebass")]
    tasks = _fusion_tasks(jazz_only)
    stub_state._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks, sessions={})
    reason = _requirement_mismatch(
        "t", "s1", "jazz fusion with sitar and tabla, please",
        "Here is your fusion piece!")
    assert reason == "fusion_imbalance"
    assert tasks["t"]["_fusion_missing"] == ["indian"]


def test_fusion_balanced_passes(stub_state):
    from server.features.critic import _requirement_mismatch
    mixed = [_lv("sax"), _lv("piano"), _lv("sitar"), _lv("tabla")]
    tasks = _fusion_tasks(mixed)
    stub_state._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks, sessions={})
    assert _requirement_mismatch(
        "t", "s1", "jazz fusion with sitar and tabla, please",
        "Here is your fusion piece!") is None


def test_fusion_single_tradition_untouched(stub_state):
    from server.features.critic import _requirement_mismatch
    tasks = _fusion_tasks([_lv("piano")])
    stub_state._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks, sessions={})
    assert _requirement_mismatch(
        "t", "s1", "a calm piano piece", "Here is your piano piece!") is None


def test_unsafe_demoted_for_clean_music_answer(stub_state):
    from server.features.critic import _retry_decision
    stub_state._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(),
        tasks={"t": {"_mismatch_done": 0, "_verify_done": 0}}, sessions={})
    music_answer = ("The fusion piece is ready. Duration 60s. "
                    "The audio player will appear below.")
    assert _retry_decision(
        "t", {"unsafe": True, "quality": 30}, None, music_answer) == (
            "retry", "quality")


def test_unsafe_kept_with_leak_markers(stub_state):
    from server.features.critic import _retry_decision
    stub_state._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(),
        tasks={"t": {"_mismatch_done": 0, "_verify_done": 0}}, sessions={})
    leaked = "Here is the track. Debug: X-Authentik-Username was palash."
    assert _retry_decision(
        "t", {"unsafe": True, "quality": 30}, None, leaked) == (
            "retry", "unsafe")


def test_fake_bare_token_links_scrubbed():
    from server.features.orchestration import _FAKE_ARTIFACT_LINK_RE as rx
    assert rx.sub("", "Hear it: [Image](music_url) done") == "Hear it:  done"
    assert rx.sub("", "x [a](/output/palash/a.png) y") == "x [a](/output/palash/a.png) y"
    assert "9771012342" not in rx.sub(
        "", "Play: [Image](/[Image: 9771012342.png])!")


def test_lane_families_mapping():
    from server.features.music.theory import lane_families
    assert lane_families("santoor") == {"indian"}
    assert lane_families("TABLA") == {"indian"}
    assert lane_families("sax") == {"jazz"}
    assert lane_families("drum kit") == {"jazz"}
    assert lane_families("koto") == {"japanese"}
    assert lane_families("mysterybox") == set()


def test_mismatch_budgets_are_per_reason(stub_state):
    from server.features.critic import _retry_decision

    def decide(counts, total):
        import threading, types
        stub_state._Registry.entrypoint = types.SimpleNamespace(
            _data_lock=threading.RLock(),
            tasks={"t": {"_mismatch_done": total, "_verify_done": 0,
                         "_mismatch_counts": counts}},
            sessions={},
        )
        return _retry_decision("t", None, "fusion_imbalance")

    # length_mismatch spent the only legacy slot — fusion still gets its own
    assert decide({"length_mismatch": 1}, 1) == ("retry", "fusion_imbalance")
    assert decide({"fusion_imbalance": 1}, 2) == ("finalize", "fusion_imbalance")
    # global cap of 4 mismatch re-runs still holds
    assert decide({"length_mismatch": 1, "citations": 1}, 4) == (
        "finalize", "fusion_imbalance")


def test_fusion_checked_before_length(stub_state):
    from server.features.critic import _requirement_mismatch
    # both off: 86s vs 120s target is FINE (0.6*120=72) — make it actually off:
    # 40s vs 120s target trips length, but fusion must win (structural first)
    tasks = {"t": {"music_file": "palash/gen_x.wav",
                   "music_url": "/music/palash/gen_x.wav",
                   "music_duration": 40.0,
                   "music_levels": [
                       {"name": "X", "program": 0, "drum": False,
                        "vol": 80, "notes": 10, "instrument": "sax",
                        "use": "lead", "style": "melody"}]}}
    stub_state._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks, sessions={})
    reason = _requirement_mismatch(
        "t", "s1", "a 2 minute jazz fusion with sitar please",
        "Here is your 2 minute piece!")
    assert reason == "fusion_imbalance"


def _music_tasks(levels):
    return {"t": {"music_file": "palash/gen_x.wav",
                  "music_url": "/music/palash/gen_x.wav",
                  "music_duration": 60.0,
                  "music_levels": levels}}


def test_missing_instruments_fires(stub_state):
    from server.features.critic import _requirement_mismatch
    tasks = _music_tasks([_lv("sax"), _lv("piano")])
    stub_state._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks, sessions={})
    reason = _requirement_mismatch(
        "t", "s1", "fusion with sitar and tabla please",
        "Here is your fusion piece!")
    assert reason == "missing_instruments"
    assert sorted(tasks["t"]["_missing_instruments"]) == ["SITAR", "TABLA"]


def test_missing_instruments_passes_when_present(stub_state):
    from server.features.critic import _requirement_mismatch
    tasks = _music_tasks([_lv("sax"), _lv("sitar"), _lv("tabla")])
    stub_state._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks, sessions={})
    assert _requirement_mismatch(
        "t", "s1", "fusion with sitar and tabla please",
        "Here is your fusion piece!") is None


def test_missing_instruments_respects_negation(stub_state):
    from server.features.critic import _requirement_mismatch, _requested_instruments
    assert _requested_instruments("no tabla, just piano") == {"PIANO"}
    assert _requested_instruments("drop the sitar, keep sax") == {"SAX"}
    tasks = _music_tasks([_lv("piano")])
    stub_state._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks, sessions={})
    assert _requirement_mismatch(
        "t", "s1", "something calm on piano, no tabla please",
        "Here is your piano piece!") is None


def test_delivery_claim_catches_duration_plus_link_lie():
    from server.features.critic import _music_delivery_claim as dc
    from server.features.critic import answer_claims_artifact as aca
    lie = ('The audio piece is approximately 2 minutes and 05 seconds long.\n'
           '[Image of a flute](https://storage.googleapis.com/x/y.png)\n'
           '*(Imagine the image above is attached)*')
    assert dc(lie) and aca(lie)
    assert not dc('Stairway to Heaven is 8 minutes long and in A minor.')
    assert not dc('Raga Bhupali is serene and played in the evening.')
    assert not aca('Raga Bhupali is serene.')


def test_gcs_presented_image_links_scrubbed():
    from server.features.orchestration import _FAKE_ARTIFACT_LINK_RE as rx
    dirty = ('Hear it: [Image of a flute](https://storage.googleapis.com/x/y.png) '
             'and [a](/output/palash/a.png) plus [docs](https://example.com/a).')
    clean = rx.sub("", dirty)
    assert 'storage.googleapis' not in clean
    assert '[a](/output/palash/a.png)' in clean
    assert '[docs](https://example.com/a)' in clean
