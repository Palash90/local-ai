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


def test_quality_judge_same_model_grading(stub_state):
    """Content judges verdict on the GPU chat model by design: the critic
    must NOT pass a self-grade exclusion or request a GPU fallback — the
    GPU path is the only path."""
    import threading
    import types
    import server.features.judge as jd
    import server.features.critic as cr
    calls = {}

    def fake_verify(user_input, answer, model_id=None,
                    allow_gpu_fallback=False, exclude_models=None):
        calls["exclude"] = exclude_models
        calls["fallback"] = allow_gpu_fallback
        return None  # simulate "GPU judge unavailable"

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
    assert calls["exclude"] is None
    assert calls["fallback"] is False
    assert jv and "GPU judge unavailable" in jv["note"]


def test_self_artifact_citation_filter():
    from server.features.critic import _is_self_artifact, extract_citations
    assert _is_self_artifact("https://palashkantikundu.in/output/palash/x.png")
    assert _is_self_artifact("/music/palash/gen_a.wav")
    assert not _is_self_artifact("https://example.com/article")
    answer = ("Here! [Image](https://palashkantikundu.in/output/palash/x.png) "
              "and (Doe, Science, 2024) [https://ex.org/a]")
    urls = [c["url"] for c in extract_citations(answer)]
    assert urls == ["https://ex.org/a"]


def test_self_artifact_hallucinated_url():
    """LLM fabricates googleusercontent.com/drive/... URLs for generated
    artifacts instead of relative /output/... / /music/... paths. Those fakes
    must be treated as self-artifacts so they never reach fetch_page /
    web_search verification (the Raga Malkauns 404->403 cascade)."""
    from server.features.critic import _is_self_artifact, extract_citations
    assert _is_self_artifact(
        "https://www.googleusercontent.com/drive/output/palash/gen_cd713f79__00001_.png")
    assert _is_self_artifact(
        "https://www.googleusercontent.com/drive/music/palash/gen_54b9cd2395fc.wav")
    assert _is_self_artifact("/drive/output/palash/gen_abc123.png")
    assert _is_self_artifact(
        "https://storage.googleapis.com/x/output/gen_c15e672f.png")
    # Real external URLs must NOT be filtered.
    assert not _is_self_artifact("https://darbar.org/exploring-raag-malkauns/")
    assert not _is_self_artifact(
        "https://www.indianclassicalmusic.com/raga-malkauns-analysis/")
    answer = (
        "Audio: [Play](https://www.googleusercontent.com/drive/music/palash/gen_54b9cd2395fc.wav) "
        "Image: [View](https://www.googleusercontent.com/drive/output/palash/gen_cd713f79__00001_.png) "
        "and (Darbar, n.d.) [https://darbar.org/exploring-raag-malkauns/]")
    urls = [c["url"] for c in extract_citations(answer)]
    assert urls == ["https://darbar.org/exploring-raag-malkauns/"]


def test_fake_artifact_link_re_catches_googleusercontent():
    from server.features.orchestration import _FAKE_ARTIFACT_LINK_RE
    dirty = ("See [Image](https://www.googleusercontent.com/drive/output/palash/gen_cd713f79__00001_.png) "
             "and [Image of a flute](https://storage.googleapis.com/x/y.png) done.")
    clean = _FAKE_ARTIFACT_LINK_RE.sub("", dirty)
    assert "googleusercontent" not in clean
    assert "storage.googleapis" not in clean
    assert clean.strip().endswith("done.")


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


def _var_score():
    return ("@tempo 120\n@genre jazz\n"
            "@section verse bars=10 energy=0.6\n"
            "@section chorus bars=10 energy=0.9\n"
            "@section bridge bars=6 energy=0.8\n"
            "@section outro bars=4 energy=0.4\n")


def test_variation_catches_duplicate_leads():
    from server.features.critic import _variation_problems
    score = (_var_score()
             + "[MELODY sax vol=86]\n"
               "D4 q F4 e A4 e G4 h | D4 q C4 e D4 e R q | "
               "D4 q F4 e A4 e G4 h | D4 q C4 e D4 e R q |\n"
               "[MELODY2 trumpet vol=80]\n"
               "D4 q F4 e A4 e G4 h | D4 q C4 e D4 e R q | "
               "D4 q F4 e A4 e G4 h | D4 q C4 e D4 e R q |\n"
               "[HARMONY epiano vol=70]\nD3:min7 w | G3:7 w |\n"
               "[BASS ebass vol=78]\nD2 h D2 q A2 q | G2 h G2 q D2 q |\n"
               "[RHYTHM]\nBD q SN q BD q SN q |\n")
    notes = _variation_problems(score)
    assert any("duplicates" in n for n in notes), notes


def test_variation_allows_octave_doubling_and_handoff():
    from server.features.critic import _variation_problems
    score = (_var_score()
             + "[MELODY sax vol=86]\n"
               "D4! q F4 e A4 e G4 h | E4 q D4 e C4 e D4 h |\n"
               "[MELODY2 epiano vol=78]\n"
               "R w | R w | D5! q F5 e A5 e G5 h | E5 q D5 e C5 e D5 h |\n"
               "[HARMONY epiano vol=70]\nD3:min7 w | G3:7 w |\n"
               "[BASS ebass vol=78]\nD2 h D2 q A2 q | G2 h G2 q D2 q |\n"
               "[RHYTHM]\nBD q SN q BD q SN q |\n"
               "[DRONE tanpura vol=60]\nD2:5 w | D2:5 w |\n")
    notes = _variation_problems(score)
    assert not any("duplicates" in n for n in notes), notes


def test_variation_single_vamp_and_no_dynamics():
    from server.features.critic import _variation_problems
    score = (_var_score()
             + "[MELODY sax vol=86]\n"
               "D4 q F4 e A4 e G4 h | G4 q F4 e D4 e C4 h |\n"
               "[HARMONY epiano vol=70]\nD3:min7 w | G3:7 w |\n"
               "[RHYTHM]\nBD q SN q BD q SN q |\n")
    notes = _variation_problems(score)
    assert any("covers" in n for n in notes), notes
    assert any("dynamics" in n for n in notes), notes


def test_variation_gate_wires_through_mismatch(stub_state):
    from server.features.critic import _requirement_mismatch
    score = (_var_score()
             + "[MELODY sax vol=86]\n"
               "D4 q F4 e A4 e G4 h | D4 q C4 e D4 e R q |\n"
               "[HARMONY epiano vol=70]\nD3:min7 w | G3:7 w |\n"
               "[RHYTHM]\nBD q SN q BD q SN q |\n")
    tasks = {"t": {"music_file": "palash/gen_x.wav",
                   "music_url": "/music/palash/gen_x.wav",
                   "music_duration": 60.0,
                   "music_score": score,
                   "music_levels": [
                       {"name": "X", "program": 0, "drum": False,
                        "vol": 80, "notes": 10, "instrument": "sax",
                        "use": "lead", "style": "melody"}]}}
    stub_state._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks, sessions={})
    reason = _requirement_mismatch(
        "t", "s1", "a jazz piece please", "Here is your piece!")
    assert reason == "variation"
    assert tasks["t"]["_variation_notes"]


def test_cadence_gate():
    from server.features.critic import _cadence_problems
    good = ("@section verse bars=2 energy=0.6\n"
            "[MELODY sax vol=80]\nD4 q E4 q G4 q A4 q |\n"
            "[BASS ebass vol=75]\nD2 h A2 q D2 q |\n"
            "[HARMONY epiano vol=70]\nD3:min7 w |\n")
    assert _cadence_problems(good) == []
    bad_bass = ("@section verse bars=2 energy=0.6\n"
                "[MELODY sax vol=80]\nD4 q E4 q G4 q A4 q |\n"
                "[BASS ebass vol=75]\nD2 w | G2 w |\n")
    assert any("bass" in n for n in _cadence_problems(bad_bass))
    bad_melody = ("@section verse bars=2 energy=0.6\n"
                  "[MELODY sax vol=80]\nD4 q E4 q G4 q E4 q |\n"
                  "[BASS ebass vol=75]\nD2 w | D2 w |\n")
    assert any("melody" in n for n in _cadence_problems(bad_melody))
    assert _cadence_problems("[MELODY sax vol=80]\nD4 q |") == []


def test_lead_home_gate():
    from server.features.critic import _lead_home_problems
    sax_lead = "[MELODY sax vol=80]\nD4 q E4 q G4 q A4 q |\n"
    sitar_lead = "[MELODY sitar vol=85]\nD4 q E4 q G4 q A4 q |\n"
    assert _lead_home_problems("jazz fusion with sitar", sax_lead) == []
    bad = _lead_home_problems("jazz fusion with sitar", sitar_lead)
    assert bad and "Sitar" in bad[0] and "jazz" in bad[0].lower()
    # explicitly assigned lead is the user's call
    assert _lead_home_problems("sitar-led jazz fusion", sitar_lead) == []
    # single tradition: no gate
    assert _lead_home_problems("indian classical piece", sitar_lead) == []
    # unresolvable genre: no gate
    assert _lead_home_problems("zydeco funk fusion party", sitar_lead) == []


def test_cadence_wires_through_mismatch(stub_state):
    from server.features.critic import _requirement_mismatch
    score = ("@section verse bars=2 energy=0.6\n"
             "[MELODY sax vol=80]\nD4 q E4 q G4 q E4 q |\n"
             "[HARMONY epiano vol=70]\nD3:min7 w |\n"
             "[BASS ebass vol=75]\nD2 w | D2 w |\n")
    tasks = {"t": {"music_file": "palash/gen_x.wav",
                   "music_url": "/music/palash/gen_x.wav",
                   "music_duration": 30.0,
                   "music_score": score,
                   "music_levels": [
                       {"name": "X", "program": 0, "drum": False,
                        "vol": 80, "notes": 10, "instrument": "sax",
                        "use": "lead", "style": "melody"}]}}
    stub_state._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks, sessions={})
    assert _requirement_mismatch(
        "t", "s1", "a short jazz piece", "Here is your piece!") == "cadence"


def test_raga_names_imply_indian_family(stub_state):
    from server.features.critic import _requirement_mismatch
    tasks = {"t": {"music_file": "palash/gen_x.wav",
                   "music_url": "/music/palash/gen_x.wav",
                   "music_duration": 60.0,
                   "music_levels": [
                       {"name": "X", "program": 0, "drum": False,
                        "vol": 80, "notes": 10, "instrument": "sax",
                        "use": "lead", "style": "melody"}]}}
    stub_state._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks, sessions={})
    reason = _requirement_mismatch(
        "t", "s1", "Compose a Raga Durga - Dorian fusion",
        "Here is your fusion piece!")
    assert reason == "fusion_imbalance"
    assert tasks["t"]["_fusion_missing"] == ["indian"]


def test_korean_joins_eastasia_bucket(stub_state):
    from server.features.critic import _requirement_mismatch
    koto = [{"name": "X", "program": 0, "drum": False, "vol": 80,
             "notes": 10, "instrument": "koto",
             "use": "lead", "style": "melody"}]
    tasks = {"t": {"music_file": "palash/gen_x.wav",
                   "music_url": "/music/palash/gen_x.wav",
                   "music_duration": 60.0, "music_levels": koto}}
    stub_state._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks, sessions={})
    # koto satisfies a korean ask via the shared eastasia bucket
    assert _requirement_mismatch(
        "t", "s1", "compose a korean music piece",
        "Here is your piece!") is None
    tasks2 = {"t": {"music_file": "palash/gen_x.wav",
                    "music_url": "/music/palash/gen_x.wav",
                    "music_duration": 60.0,
                    "music_levels": [
                        {"name": "X", "program": 0, "drum": False,
                         "vol": 80, "notes": 10, "instrument": "sax",
                         "use": "lead", "style": "melody"}]}}
    stub_state._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks2, sessions={})
    reason = _requirement_mismatch(
        "t", "s1", "korean jazz fusion please", "Here is your fusion piece!")
    assert reason == "fusion_imbalance"
    assert tasks2["t"]["_fusion_missing"] == ["eastasian"]


def test_systemic_note_fires_on_dominated_errors():
    from server.features.critic import _systemic_error_note
    many = [f"line {i}: 'C4' missing a duration" for i in range(19)]
    note = _systemic_error_note(many)
    assert "19 of your" in note and "lead-sheet" in note
    assert _systemic_error_note(many[:3]) == ""
    assert _systemic_error_note([]) == ""
    assert _systemic_error_note(["line 2: bad token 'ZZZ'"] * 6) == ""


def test_systemic_note_counts_mixed_missing_duration_phrasing():
    # Real failure shape: chord messages say "missing a duration" while
    # R messages say "needs a duration" — the cluster must count both,
    # or the loud warning never fires and retries fly blind.
    from server.features.critic import _systemic_error_note
    mixed = [
        "line 15: chord 'G2:7' missing a duration (e.g. 'G2:7 w')",
        "line 15: 'R' needs a duration too ('R q' rests a quarter)",
        "line 16: 'R' needs a duration too ('R q' rests a quarter)",
        "line 16: 'R' needs a duration too ('R q' rests a quarter)",
        "line 16: chord 'G2:7' missing a duration (e.g. 'G2:7 w')",
    ]
    note = _systemic_error_note(mixed)
    assert "5 of your" in note and "lead-sheet" in note
    assert _systemic_error_note(mixed[:3]) == ""


def test_score_errors_steering_teaches_ar_duration():
    from server.features import critic as cr
    text = dict(cr._STEERING_PRESETS)["score_errors"] if hasattr(cr, "_STEERING_PRESETS") else None
    if text is None:
        import re as _re
        src = open("server/features/critic.py").read()
        m = _re.search(r'"score_errors": \(\s*"(.*?)"\s*\)', src, _re.S)
        assert m, "score_errors steering missing"
        text = m.group(1)
    assert "C3:min7ar w" in text
    assert "never a bare" in text


def test_lead_presence_gate():
    from server.features.critic import _lead_presence_problems
    sax_lead = "[MELODY sax vol=80]\nD4 q E4 q G4 q A4 q |\n"
    santoor_lead = "[MELODY santoor vol=80]\nD4 q E4 q G4 q A4 q |\n"
    side_lane = ("[MELODY sax vol=80]\nD4 q E4 q G4 q A4 q |\n"
                 "[Santoor vol=80]\nD4 q E4 q G4 q A4 q |\n")
    # santoor-led ask, sax leads: fires
    bad = _lead_presence_problems("santoor-led fusion piece", sax_lead)
    assert bad and "Santoor" in bad[0] and "MELODY" in bad[0]
    # santoor leads: silent
    assert _lead_presence_problems("santoor-led fusion piece", santoor_lead) == []
    # santoor on MELODY2 counts as lead voice
    duo = sax_lead + "[MELODY2 santoor vol=80]\nD4 q E4 q G4 q A4 q |\n"
    assert _lead_presence_problems("santoor carries the melody", duo) == []
    # custom-role side lane is not a lead voice: fires
    assert _lead_presence_problems("piece for santoor", side_lane) != []
    # bare topic mention: silent
    assert _lead_presence_problems("what does a santoor sound like", sax_lead) == []
    # negated: silent
    assert _lead_presence_problems("fusion without santoor", sax_lead) == []
    # no lead phrasing at all: silent
    assert _lead_presence_problems("calm bossa piece with sax", sax_lead) == []


def test_lead_presence_wires_through_mismatch(stub_state):
    from server.features.critic import _requirement_mismatch
    score = ("[MELODY sax vol=80]\nD4 q E4 q G4 q A4 q |\n"
             "[HARMONY epiano vol=70]\nD3:min7 w |\n"
             "[PAD santoor vol=70]\nD4 w | D4 w |\n")
    tasks = {"t": {"music_file": "palash/gen_x.wav",
                   "music_url": "/music/palash/gen_x.wav",
                   "music_duration": 30.0,
                   "music_score": score,
                   "music_levels": [
                       {"instrument": "sax", "name": "MELODY"},
                       {"instrument": "epiano", "name": "HARMONY"},
                       {"instrument": "santoor", "name": "SANTOOR"},
                   ]}}
    reason = _run(stub_state, tasks,
                  "santoor-led calm fusion", "Here is your santoor-led piece!")
    assert reason == "lead_presence"


def test_lane_roles_gate():
    from server.features.critic import _lane_role_problems
    good = ("[MELODY santoor vol=80]\nD4 q E4 q G4 q A4 q |\n"
            "[HARMONY piano vol=65]\nC3:maj7 w |\n")
    assert _lane_role_problems("any ask", good) == []
    # comment-only roles + bare headers: fires
    bad = ("# MELODY: Santoor (Primary Lead)\n"
           "[Santoor vol=80]\nD4 q E4 q G4 q A4 q |\n"
           "[Piano vol=65]\nC3:maj7 w |\n")
    notes = _lane_role_problems("any ask", bad)
    assert notes and any("Santoor" in n for n in notes)
    # missing harmony voice: fires
    no_harm = ("[MELODY santoor vol=80]\nD4 q E4 q G4 q A4 q |\n"
               "[BASS ebass vol=70]\nC2 w | C2 w |\n")
    assert any("HARMONY" in n for n in _lane_role_problems("any ask", no_harm))
    # missing melody voice: fires
    no_mel = ("[HARMONY piano vol=65]\nC3:maj7 w | C3:maj7 w |\n"
              "[BASS ebass vol=70]\nC2 w | C2 w |\n")
    assert any("MELODY" in n for n in _lane_role_problems("any ask", no_mel))
    # explicit-solo single lane: exempt
    solo = "[Piano vol=90]\nC4 q E4 q G4 q A4 q |\n"
    assert _lane_role_problems("piano solo please", solo) == []
    # unparseable: silent, not a crash
    assert _lane_role_problems("any ask", "") == []


def test_lane_roles_wires_through_mismatch(stub_state):
    from server.features.critic import _requirement_mismatch
    score = ("[Santoor vol=80]\nD4 q E4 q G4 q A4 q |\n"
             "[Piano vol=65]\nC3:maj7 w |\n")
    tasks = {"t": {"music_file": "palash/gen_x.wav",
                   "music_url": "/music/palash/gen_x.wav",
                   "music_duration": 30.0,
                   "music_score": score,
                   "music_levels": [
                       {"instrument": "santoor", "name": "SANTOOR"},
                       {"instrument": "piano", "name": "Piano"},
                   ]}}
    reason = _run(stub_state, tasks,
                  "compose something calm", "Here is your piece!")
    assert reason == "lane_roles"


def test_missing_instruments_bass_alias_family(stub_state):
    from server.features.critic import _missing_instruments
    levels = [{"instrument": "bassguitar", "name": "BASS"}]
    # "bass" ask must not flag a rendered bassguitar lane.
    assert _missing_instruments("deep bass groove", levels) == set()
    # Genuinely absent still fires.
    assert _missing_instruments("santoor melody", levels) == {"SANTOOR"}


def test_skip_exhausted_falls_through_to_length(stub_state):
    from server.features.critic import _requirement_mismatch
    score = ("[MELODY santoor vol=80]\nD4 q E4 q G4 q G4 q |\n"
             "[HARMONY piano vol=70]\nD3:min7 w |\n"
             "[BASS bassguitar vol=70]\nC2 h G2 q C2 q |\n")
    tasks = {"t": {"music_file": "palash/gen_x.wav",
                   "music_url": "/music/palash/gen_x.wav",
                   "music_duration": 29.83,
                   "music_score": score,
                   "music_levels": [
                       {"instrument": "santoor", "name": "MELODY"},
                       {"instrument": "piano", "name": "HARMONY"},
                       {"instrument": "bassguitar", "name": "BASS"},
                   ]}}
    ask = "santoor-led calm piece, about a minute"
    # No skip: all gates silent except length (short render).
    assert _run(stub_state, tasks, ask, "Here is your piece!") == "length_mismatch"
    # Length itself skipped with nothing else firing: the deferred fallback
    # returns it (so the decision layer finalizes with the right reason).
    from server.features import state as st
    import threading, types
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks, sessions={})
    try:
        assert _requirement_mismatch(
            "t", "s1", ask, "Here is your piece!",
            _skip=frozenset({"length_mismatch"})) == "length_mismatch"
    finally:
        pass


def test_retry_decision_second_chance_past_spent_gate(stub_state):
    from server.features.critic import _retry_decision
    from server.features import state as st
    import threading, types
    # Spent "variation" must not mask a still-actionable length_mismatch.
    score = ("[MELODY santoor vol=80]\nD4 q E4 q G4 q G4 q |\n"
             "[HARMONY piano vol=70]\nD3:min7 w |\n")
    tasks = {"t": {"music_file": "palash/gen_x.wav",
                   "music_url": "/music/palash/gen_x.wav",
                   "music_duration": 29.83,
                   "music_score": score,
                   "music_levels": [
                       {"instrument": "santoor", "name": "MELODY"},
                       {"instrument": "piano", "name": "HARMONY"},
                   ],
                   "music_errors": [],
                   "_mismatch_counts": {"variation": 1},
                   "_mismatch_done": 1,
                   "_original_message": "santoor piece, about a minute"}}
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks, sessions={})
    try:
        action, reason = _retry_decision(
            "t", None, "variation", "Here!",
            _ctx=("s1", "santoor piece, about a minute"))
    finally:
        pass
    assert (action, reason) == ("retry", "length_mismatch")


def test_length_math_steering_has_numbers(stub_state):
    from server.features import critic as cr
    from server.features import state as st
    import threading, types
    tasks = {"t": {"music_score": "@tempo 90\n[MELODY santoor]\nD4 q |\n",
                   "music_duration": 29.83,
                   "_original_message": "piece about a minute"}}
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks,
        sessions={"s1": []}, sessions_meta={},
    )
    # Stub the session/round plumbing the reschedule tail needs.
    ep = st._Registry.entrypoint
    ep.set_status = lambda *a, **k: None
    ep.save_sessions = lambda: None
    ep._start_llm_round = lambda *a, **k: None
    try:
        cr._reschedule("t", "s1", 1, "length_mismatch", None)
    finally:
        pass
    note = tasks["t"].get("_last_mismatch")
    assert note == "length_mismatch"
    steering = st._Registry.entrypoint.sessions["s1"][-1]["content"]
    assert "LENGTH MATH" in steering
    assert "60" in steering and "90" in steering and "29.83" in steering
    assert "round(60 × 90 ÷ 240) = 22" in steering or "= 22" in steering or "= 23" in steering


def _orch_ns(monkeypatch, **over):
    import threading
    import server.features.state as st
    import server.features.orchestration as orch
    base = {
        "_data_lock": threading.RLock(),
        "tasks": {},
        "sessions": {},
        "sessions_meta": {},
        "_task_queues": {"gpu": [], "cpu": [], "guardrail": []},
        "_current_task_ids": {"gpu": None, "cpu": None, "guardrail": None},
    }
    base.update(over)
    ep = types.SimpleNamespace(**base)
    monkeypatch.setattr(st._Registry, "entrypoint", ep)
    return orch, ep


def test_session_busy_statuses(monkeypatch):
    import threading
    import server.features.state as st
    import server.features.orchestration as orch
    tasks = {
        "w1": {"session_id": "s1", "status": "working"},
        "p1": {"session_id": "s2", "status": "parked"},
        "q1": {"session_id": "s3", "status": "queued"},
        "d1": {"session_id": "s4", "status": "done"},
    }
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks,
        _current_task_ids={"gpu": None, "cpu": None, "guardrail": None})
    try:
        assert orch._session_busy("s1") is True
        assert orch._session_busy("s2") is True
        assert orch._session_busy("s3") is False  # queued alone never blocks
        assert orch._session_busy("s4") is False
        assert orch._session_busy("s1", exclude_tid="w1") is False
        assert orch._session_busy("nope") is False
    finally:
        pass


def test_pick_runnable_skips_busy_session(monkeypatch):
    import threading
    import server.features.state as st
    import server.features.orchestration as orch
    tasks = {"w1": {"session_id": "s1", "status": "working"}}
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks,
        _current_task_ids={"gpu": "w1", "cpu": None, "guardrail": None})
    try:
        q = [
            {"task_id": "t-same", "session_id": "s1"},
            {"task_id": "t-other", "session_id": "s2"},
        ]
        assert orch._pick_runnable_index(q, "gpu") == 1
        # Peer-review children bypass (parent works by construction).
        q2 = [{"task_id": "t-peer", "session_id": "s1", "_peer_review": True}]
        assert orch._pick_runnable_index(q2, "gpu") == 0
        # All blocked → None (lane holds instead of spinning).
        assert orch._pick_runnable_index(
            [{"task_id": "t-same", "session_id": "s1"}], "gpu") is None
    finally:
        pass


def test_higher_priority_ignores_same_session_waiter(monkeypatch):
    import threading
    import server.features.state as st
    import server.features.orchestration as orch
    import server.features.llm as _llm
    q = [{"task_id": "w", "session_id": "s1"}]
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks={},
        _task_queues={"gpu": q}, _queue_locks={"gpu": threading.RLock()})
    # Head rank: UI. Same session → must not preempt (park-loop guard).
    monkeypatch.setattr(orch, "_lane_rank", lambda item: orch.LANE_RANK_UI)
    try:
        assert orch._higher_priority_waiting(exclude_sid="s1") is False
        assert orch._higher_priority_waiting(exclude_sid="s2") is True
        assert orch._higher_priority_waiting() is True
    finally:
        pass


def test_steering_carries_original_ask(stub_state):
    import threading
    import server.features.state as st
    import server.features.critic as cr
    tasks = {"t": {"_original_message": "Perform a research on Raga X",
                   "music_duration": 0}}
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks,
        sessions={"s1": []}, sessions_meta={"s1": {}},
        set_status=lambda *a, **k: None,
        save_sessions=lambda: None,
        _start_llm_round=lambda *a, **k: None,
    )
    try:
        cr._reschedule("t", "s1", 0, "citations", None)
    finally:
        pass
    notes = [m for m in tasks and st._Registry.entrypoint.sessions["s1"]
             if m.get("_steering")]
    assert len(notes) == 1
    assert "Perform a research on Raga X" in notes[0]["content"]


def test_identical_steering_not_duplicated(stub_state):
    import threading
    import server.features.state as st
    import server.features.critic as cr
    tasks = {"t": {"_original_message": "ask",
                   "music_duration": 0}}
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks,
        sessions={"s1": []}, sessions_meta={"s1": {}},
        set_status=lambda *a, **k: None,
        save_sessions=lambda: None,
        _start_llm_round=lambda *a, **k: None,
    )
    try:
        cr._reschedule("t", "s1", 0, "citations", None)
        cr._reschedule("t", "s1", 0, "citations", None)
    finally:
        pass
    notes = [m for m in st._Registry.entrypoint.sessions["s1"]
             if m.get("_steering")]
    assert len(notes) == 1


def test_second_chance_skips_spent_next_reason(stub_state):
    from server.features.critic import _retry_decision
    from server.features import state as st
    import threading, types
    # research_structure spent AND nothing else actionable: must finalize,
    # not retry the same spent reason (self-requeue churn).
    tasks = {"t": {"music_file": "x.wav",
                   "music_url": "/music/x.wav",
                   "music_duration": 60.0,
                   "music_score": ("[MELODY piano vol=80]\nC4 q E4 q G4 q G4 q |\n"
                                   "[HARMONY epiano vol=70]\nC3:maj7 w |\n"),
                   "music_levels": [
                       {"instrument": "piano", "name": "MELODY"},
                       {"instrument": "epiano", "name": "HARMONY"},
                   ],
                   "_mismatch_counts": {"research_structure": 1},
                   "_mismatch_done": 1,
                   "_original_message": "compose something"}}
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks, sessions={})
    try:
        action, reason = _retry_decision(
            "t", None, "research_structure", "Here!",
            _ctx=("s1", "compose something"))
    finally:
        pass
    assert action == "finalize"


def test_second_chance_no_retry_on_spent_fallback(stub_state):
    from server.features.critic import _retry_decision
    from server.features import state as st
    import threading, types
    # Only "variation" fires and it is already spent: the deferred fallback
    # hands it back, but retrying must NOT happen — finalize instead.
    score = ("[MELODY piano vol=80]\nC4 q C4 q C4 q C4 q |\n"
             "[HARMONY epiano vol=70]\nC3:maj7 w |\n")
    tasks = {"t": {"music_file": "x.wav",
                   "music_url": "/music/x.wav",
                   "music_duration": 60.0,
                   "music_score": score,
                   "music_levels": [
                       {"instrument": "piano", "name": "MELODY"},
                       {"instrument": "epiano", "name": "HARMONY"},
                   ],
                   "_mismatch_counts": {"variation": 1},
                   "_mismatch_done": 1,
                   "_original_message": "compose something"}}
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(), tasks=tasks, sessions={})
    try:
        action, reason = _retry_decision(
            "t", None, "variation", "Here!",
            _ctx=("s1", "compose something"))
    finally:
        pass
    assert action == "finalize" and reason == "variation"
