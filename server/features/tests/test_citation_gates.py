"""Anti-hallucination gates: file-asset citations + citation diversity.

Regression coverage for the Palash-research incident (png/wav file assets
cited as biographical sources; one homepage backing 6 claims; judge 90):
- file-asset URLs are never factual sources (deterministic, no LLM needed);
- a single URL backing more than VERIFY_MAX_CITES_PER_URL claims raises the
  ``citation_diversity`` retry reason through the existing bounded machinery.
"""

import threading
import types

import server.features.critic as cr


def _report(body, cites):
    heads = ["Executive Summary", "Scope and Methodology", "Findings",
             "Analysis", "Limitations and Uncertainty", "Conclusion",
             "References"]
    parts = [f"## {h}\nSome text." for h in heads]
    parts.insert(3, body + " " + " ".join(cites))
    return "\n\n".join(parts)


def _cite(meta, url):
    return f"({meta}) [{url}]"


def _fake_m(monkeypatch, research=True):
    fake_m = types.SimpleNamespace(
        _data_lock=threading.RLock(),
        tasks={"t": {"research": research}},
        sessions={},
    )
    monkeypatch.setattr(cr, "M", fake_m)
    return fake_m


# ── file-asset rule ──────────────────────────────────────────────────────

def test_file_asset_own_paths():
    assert cr._is_file_asset("https://home.example.in/output/palash/x.png") is True
    assert cr._is_file_asset("https://home.example.in/music/palash/gen_x.wav") is True
    assert cr._is_file_asset("https://home.example.in/uploads/doc.pdf") is True


def test_file_asset_media_extensions_anywhere():
    assert cr._is_file_asset("https://cdn.example.com/photo.JPG") is True
    assert cr._is_file_asset("https://example.com/track.mp3") is True
    assert cr._is_file_asset("https://example.com/clip.mp4") is True


def test_file_asset_legit_sources_pass():
    assert cr._is_file_asset("https://example.com/paper.pdf") is False
    assert cr._is_file_asset("https://example.com/article.html") is False
    assert cr._is_file_asset("https://example.com/") is False
    assert cr._is_file_asset("") is False
    assert cr._is_file_asset(None) is False


# ── diversity reason ─────────────────────────────────────────────────────

def _good_structure_many_cites(url, n):
    cites = [_cite(f"Author{i}, Venue, 2024", url) for i in range(n)]
    return _report("Findings text here.", cites)


def test_diversity_fires_on_repeated_url(monkeypatch):
    _fake_m(monkeypatch)
    answer = _good_structure_many_cites("https://one.example/a", 4)
    assert cr._requirement_mismatch(
        "t", "s1", "Give me a report on fusion cuisine trends", answer
    ) == "citation_diversity"


def test_diversity_boundary_at_limit(monkeypatch):
    _fake_m(monkeypatch)
    answer = _good_structure_many_cites("https://one.example/a", 3)
    assert cr._requirement_mismatch(
        "t", "s1", "Give me a report on fusion cuisine trends", answer
    ) is None


def test_diversity_passes_on_distinct_sources(monkeypatch):
    _fake_m(monkeypatch)
    cites = [_cite(f"Author{i}, Venue, 2024", f"https://s{i}.example/a")
             for i in range(4)]
    answer = _report("Findings text here.", cites)
    assert cr._requirement_mismatch(
        "t", "s1", "Give me a report on fusion cuisine trends", answer
    ) is None


def test_diversity_hint_present():
    assert "citation_diversity" in cr._STEERING_HINTS
    assert "INDEPENDENT" in cr._STEERING_HINTS["citation_diversity"]


def _judge_fake(monkeypatch, tasks):
    import threading
    import types

    seen = {}
    fake_m = types.SimpleNamespace(
        _data_lock=threading.RLock(),
        tasks=tasks,
    )
    monkeypatch.setattr(cr, "M", fake_m)

    def fake_verify(user_input, answer, **kw):
        seen.update(kw)
        return {"model": "m", "ok": True, "citations": True,
                "unsafe": False, "quality": 95, "reason": "ok"}

    import server.features.judge as _judge
    monkeypatch.setattr(_judge, "llm_verify_research_answer", fake_verify)
    # _judge_research_answer imports the name from judge at call time.
    return seen


def test_verify_timeout_defaults_without_override(monkeypatch):
    seen = _judge_fake(monkeypatch, {"t": {"_original_message": "q",
                                           "_user": "u"}})
    cr._judge_research_answer("t", "answer")
    assert seen.get("timeout") is None


def test_verify_timeout_value_on_override(monkeypatch):
    seen = _judge_fake(monkeypatch, {"t": {"_original_message": "q",
                                           "_user": "u",
                                           "model": "gemma4-26b"}})
    cr._judge_research_answer("t", "answer")
    assert seen.get("timeout") == 90
    assert seen.get("override") == "gemma4-26b"


def _aux_fake(monkeypatch, **over):
    import threading
    import types

    calls = {"ensure": [], "load": []}
    attrs = dict(
        _data_lock=threading.RLock(),
        tasks={},
        server_model_id=lambda mode, override=None: override or f"m-{mode}",
        server_base=lambda mode, override=None: f"http://x/{mode}",
        server_url=lambda mode, override=None: f"http://x/{mode}/chat",
        mark_slot_kv_dirty=lambda mode: None,
        ensure_llama_server=lambda mode, override=None: calls["ensure"].append(
            (mode, override)) or None,
        load_llama_model=lambda mode, model_id=None: calls["load"].append(
            (mode, model_id)) or True,
    )
    attrs.update(over)
    return types.SimpleNamespace(**attrs), calls


def test_critic_completion_ensures_loads_and_routes_override(monkeypatch):
    import types as _t

    fake_m, calls = _aux_fake(monkeypatch)
    monkeypatch.setattr(cr, "M", fake_m)

    posts = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "verdict"}}]}

    monkeypatch.setattr(
        cr, "requests",
        _t.SimpleNamespace(post=lambda *a, **k: (posts.append(k), _Resp())[1]),
    )
    out = cr._critic_completion("sys", "user", "gpu", override="gemma4-26b")
    assert out == "verdict"
    assert calls["ensure"] == [("gpu", "gemma4-26b")]
    assert calls["load"] == [("26b", "gemma4-26b")]
    assert posts[0]["json"]["model"] == "gemma4-26b"


def test_critic_completion_lane_default_unchanged(monkeypatch):
    import types as _t

    fake_m, calls = _aux_fake(monkeypatch)
    monkeypatch.setattr(cr, "M", fake_m)

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "v"}}]}

    monkeypatch.setattr(
        cr, "requests",
        _t.SimpleNamespace(post=lambda *a, **k: _Resp()),
    )
    assert cr._critic_completion("sys", "user", "cpu") == "v"
    assert calls["ensure"] == [("cpu", None)]
    assert calls["load"] == [("cpu", None)]
