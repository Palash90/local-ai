"""Batch-2 tool paths: browser_navigate SSRF/render gate + themes_db round-trip."""

import socket
import threading
import types

import pytest

import server.db as db
from server.features import themes_db as TH
from server.features import tools as TL
from server.features import state


@pytest.fixture()
def stub_m(monkeypatch):
    m = types.SimpleNamespace(_image_active=False,
                              _data_lock=threading.Lock(), tasks={})
    prev = state._Registry.entrypoint
    state.register_entrypoint(m)
    try:
        yield m
    finally:
        state.register_entrypoint(prev)


def test_gate_ignores_other_tools(stub_m):
    assert TL._browser_navigate_gate("web_search", {"url": "http://x/"}) == ""


def test_gate_blocks_render_window(stub_m):
    stub_m._image_active = True
    r = TL._browser_navigate_gate("browser__browser_navigate",
                                  {"url": "https://example.com/"})
    assert "image render owns the machine" in r


def test_gate_blocks_private_hosts(stub_m, monkeypatch):
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "93.184.216.34")
    for bad in ("http://localhost:8081/x", "http://metadata.google.internal/",
                "http://printer.lan/", "ftp://example.com/x", "not a url", ""):
        r = TL._browser_navigate_gate("browser__browser_navigate", {"url": bad})
        assert r.startswith("Refused"), bad


def test_gate_blocks_resolved_private_ip(stub_m, monkeypatch):
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "127.0.0.1")
    r = TL._browser_navigate_gate("browser__browser_navigate",
                                  {"url": "http://example.com/"})
    assert "private/internal" in r


def test_gate_allows_public(stub_m, monkeypatch):
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "93.184.216.34")
    assert TL._browser_navigate_gate(
        "browser__browser_navigate", {"url": "https://example.com/a"}) == ""


@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    p = str(tmp_path / "themes.db")
    monkeypatch.setattr(db, "DB_PATH", p)
    import sqlite3
    conn = sqlite3.connect(p)
    try:
        db._create_tables(conn)
        conn.commit()
    finally:
        conn.close()
    return p


def test_theme_log_round_trip(tmpdb):
    row, existed = TH.theme_log_create(scope="s1", genre="g", mood="m",
                                       role="r", persona="p", theme="hello")
    tid = row["id"]
    assert tid and existed is False
    rows = TH.theme_log_list(scope="s1")
    assert any(r["id"] == tid for r in rows)
    TH.theme_log_complete(tid)
    done = [r for r in TH.theme_log_list(scope="s1", status="completed")]
    assert any(r["id"] == tid for r in done)


def test_combo_hash_stable_and_scoped(tmpdb):
    h1 = TH.combo_hash("g", "m", "r", "p", {"a": 1})
    h2 = TH.combo_hash("g", "m", "r", "p", {"a": 1})
    h3 = TH.combo_hash("g", "m", "r", "p", {"a": 2})
    assert h1 == h2 and h1 != h3
    row, _ = TH.theme_log_create(scope="s1", genre="g", mood="m",
                                   role="r", persona="p", theme="t")
    assert TH.theme_log_check("s1", genre="g", mood="m", role="r",
                              persona="p") is not None
    assert TH.theme_log_check("s2", genre="g", mood="m", role="r",
                              persona="p") is None


def test_theme_stats(tmpdb):
    # distinct combos (same scope+combo upserts instead of inserting)
    TH.theme_log_create(scope="s9", genre="g1", theme="a")
    TH.theme_log_create(scope="s9", genre="g2", theme="b")
    s = TH.theme_log_stats(scope="s9")
    assert s["total"] >= 2
    assert s["per_scope"]["s9"]["active"] >= 2
