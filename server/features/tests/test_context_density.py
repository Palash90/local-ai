from server.features.context import _text_tokens


def test_score_text_flagged_as_dense():
    score = ("D4! q F4 e A4 e G4 h | D4! q C4 e D4 e R q | " * 50)
    n = _text_tokens(score)
    # ~2.0 chars/token or denser — the old /4 rule under-counted ~2.6x and
    # let music sessions reach llama-server 400 exceed_context_size.
    assert n > len(score) / 3, (n, len(score))


def test_prose_stays_light():
    prose = ("The music has been updated with a santoor melody over a tanpura "
             "drone and a soft tabla groove. " * 40)
    n = _text_tokens(prose)
    assert n < len(prose) / 3.5, (n, len(prose))


def test_compaction_counter_roundtrip_and_report():
    import threading
    import types
    from server.features import sessions as S
    from server.features import state
    from server.features.context import context_token_report
    assert S._session_meta_from({"compactions": 3})["compactions"] == 3
    assert S._session_meta_from({})["compactions"] == 0
    dummy = types.SimpleNamespace(
        _effective_contexts={},
        _effective_contexts_lock=threading.Lock(),
        sessions_meta={"test-compactions": {"compactions": 2}},
        TOOLS_TOKEN_COST=100,
        PER_MESSAGE_OVERHEAD=4,
    )
    prev = state._Registry.entrypoint
    state.register_entrypoint(dummy)
    try:
        assert context_token_report("test-compactions", [])["compactions"] == 2
        assert context_token_report("no-such-sid", [])["compactions"] == 0
    finally:
        state.register_entrypoint(prev)
