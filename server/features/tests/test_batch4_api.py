"""Batch-4 surfaces: file routes, tts-words, showcase page, arranged gate."""

import base64
import io
import json
import threading
import types

import pytest

import server.api as api


def _handler(path, body=None, post=True):
    h = api.Handler.__new__(api.Handler)
    raw = json.dumps(body or {}).encode()
    h.command = "POST" if post else "GET"
    h.path = path
    h.requestline = f"{h.command} {path} HTTP/1.1"
    h.request_version = "HTTP/1.1"
    h.headers = {"Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    h.wfile = io.BytesIO()
    h._headers_buffer = []
    h.log_request = lambda *a, **k: None
    h.log_message = lambda *a, **k: None
    return h


def _responded(h):
    raw = h.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body.decode() or "{}")


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "UPLOADS_DIR", str(tmp_path))
    monkeypatch.setattr(api, "MAX_PDF_UPLOAD_BYTES", 100)
    monkeypatch.setattr(api, "get_current_user", lambda headers: "tester")
    yield tmp_path


def test_extract_file_round_trip(wired):
    h = _handler("/api/extract-file",
                 {"name": "note.txt", "data": base64.b64encode(b"hello").decode()})
    h.do_POST()
    status, data = _responded(h)
    assert status == 200 and data["url"].startswith("/uploads/")
    assert (wired / data["url"].split("/")[-1]).read_bytes() == b"hello"


def test_extract_oversized_pdf_rejected(wired):
    big = base64.b64encode(b"%PDF-" + b"x" * 200).decode()
    h = _handler("/api/extract-file", {"name": "big.pdf", "data": big})
    h.do_POST()
    status, data = _responded(h)
    assert status == 413 and "too large" in data["error"].lower()


def test_upload_image_normalizes_ext_and_validates(wired):
    tiny_png = base64.b64encode(bytes.fromhex("89504e470d0a1a0a")).decode()
    h = _handler("/api/upload-image", {"data": tiny_png, "ext": "tiff"})
    h.do_POST()
    status, data = _responded(h)
    assert status == 200 and data["url"].endswith(".jpg")
    h = _handler("/api/upload-image", {"data": "!!!not-base64!!!"})
    h.do_POST()
    assert _responded(h)[0] == 400


def test_upload_image_requires_auth(wired, monkeypatch):
    monkeypatch.setattr(api, "get_current_user", lambda headers: None)
    h = _handler("/api/upload-image", {"data": "eA=="})
    h.do_POST()
    assert _responded(h)[0] == 401


def test_tts_words_shapes(wired, monkeypatch):
    monkeypatch.setattr(api, "_get_tts_words",
                        lambda text, voice="", **k: [{"w": "hi", "s": 0, "e": 0.5}])
    h = _handler("/api/tts-words", {"text": "hi there"})
    h.do_POST()
    status, data = _responded(h)
    assert status == 200 and data["words"][0]["w"] == "hi"
    h = _handler("/api/tts-words", {"text": ""})
    h.do_POST()
    assert _responded(h)[0] == 400
    monkeypatch.setattr(api, "get_current_user", lambda headers: None)
    h = _handler("/api/tts-words", {"text": "hi"})
    h.do_POST()
    assert _responded(h)[0] == 401


def test_tts_words_internal_token_loopback(monkeypatch):
    import server.config as C
    monkeypatch.setattr(C, "TTS_INTERNAL_TOKEN", "s3cret")
    monkeypatch.setattr(api, "_get_tts_words", lambda text, voice="", **k: [])
    h = _handler("/api/tts-words", {"text": "hi", "token": "s3cret"})
    h.client_address = ("127.0.0.1", 1234)
    h.do_POST()
    assert _responded(h)[0] == 200


def test_tts_words_wrong_token_rejected(monkeypatch):
    import server.config as C
    monkeypatch.setattr(C, "TTS_INTERNAL_TOKEN", "s3cret")
    h = _handler("/api/tts-words", {"text": "hi", "token": "wrong"})
    h.client_address = ("127.0.0.1", 1234)
    h.do_POST()
    assert _responded(h)[0] == 401


def test_tts_words_non_loopback_rejected_even_with_token(monkeypatch):
    import server.config as C
    monkeypatch.setattr(C, "TTS_INTERNAL_TOKEN", "s3cret")
    h = _handler("/api/tts-words", {"text": "hi", "token": "s3cret"})
    h.client_address = ("203.0.113.9", 1234)
    h.do_POST()
    assert _responded(h)[0] == 401


def test_showcase_page_renders_clips(tmp_path):
    from server.features.music import showcase_page as sp
    d = tmp_path / "show"
    d.mkdir()
    (d / "index.json").write_text(json.dumps({"clips": [
        {"kind": "genre", "key": "yaman", "title": "Yaman Dawn",
         "tempo": 80, "structure": "A", "lanes": ["santoor"],
         "desc": "calm", "file": "y.wav"},
        {"kind": "instrument", "family": "santoor", "title": "Santoor Voice",
         "file": "s.wav"},
    ]}))
    html = sp.build_page(str(d), site_origin="https://x.test")
    assert "Yaman Dawn" in html and "Santoor Voice" in html
    assert html.count("<article") == 2


def test_showcase_page_missing_dir_still_valid():
    from server.features.music import showcase_page as sp
    html = sp.build_page("/nonexistent-dir-xyz")
    assert html.startswith("<!doctype html>")


def test_arranged_pipeline_disabled_short_circuits(monkeypatch):
    import server.config as C
    from server.features import tools as TL
    from server.features import state
    events = []
    m = types.SimpleNamespace(
        set_status=lambda *a, **k: None,
        _data_lock=threading.Lock(),
        tasks={"t1": {}},
        _event_post=lambda *a, **k: events.append(a[0]),
    )
    prev = state._Registry.entrypoint
    state.register_entrypoint(m)
    # tools.py does `from server.config import MUSIC_ARRANGER_PARAMS`
    # inside the branch, so patch it on server.config (call-time lookup).
    monkeypatch.setattr(C, "MUSIC_ARRANGER_PARAMS", "")
    try:
        tc = {"id": "c1", "function": {
            "name": "generate_music_arranged", "arguments": "{}"}}
        TL._dispatch_tool("t1", "s1", tc, None, 0, 0)
    finally:
        state.register_entrypoint(prev)
    assert events and events[0] == "tool_ok"
