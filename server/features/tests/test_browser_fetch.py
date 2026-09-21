"""Headed-browser fallback for bot-blocked fetches.

AST-isolated like test_openai_search_rewrite (tools.py/fetch.py pull heavy
deps that can't import under pytest due to the server/dotenv.py shadowing).
"""

import ast
import ipaddress
import json
import os
import socket
import types
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.join(_HERE, "..", "tools.py")
_FETCH = os.path.join(_HERE, "..", "websearch", "fetch.py")


def _load_tools_gate():
    tree = ast.parse(open(_TOOLS).read())
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_browser_navigate_gate":
            exec(compile(ast.Module(body=[node], type_ignores=[]), _TOOLS, "exec"), ns)
    return ns["_browser_navigate_gate"]


def _load_hint():
    tree = ast.parse(open(_FETCH).read())
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_browser_fallback_hint":
            exec(compile(ast.Module(body=[node], type_ignores=[]), _FETCH, "exec"), ns)
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_BROWSER_FALLBACK_TOKENS"
                for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), _FETCH, "exec"), ns)
    return ns["_browser_fallback_hint"]


def _gate_ns(image_active=False):
    gate = _load_tools_gate()
    M = types.SimpleNamespace(_image_active=image_active)
    real_gethostbyname = socket.gethostbyname

    def fake_dns(host):
        if host == "example.com":
            return "93.184.216.34"
        if host in ("localhost",):
            return "127.0.0.1"
        return real_gethostbyname(host)

    return gate, M, fake_dns


def _run_gate(tool, args, image_active=False, dns=None):
    gate, M, fake_dns = _gate_ns(image_active)
    g = {
        "_browser_navigate_gate": gate, "M": M, "urlparse": urlparse,
        "socket": types.SimpleNamespace(gethostbyname=dns or fake_dns),
        "ipaddress": ipaddress,
    }
    # Re-exec with controlled globals so socket/ipaddress/M resolve to fakes.
    tree = ast.parse(open(_TOOLS).read())
    ns = dict(g)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_browser_navigate_gate":
            exec(compile(ast.Module(body=[node], type_ignores=[]), _TOOLS, "exec"), ns)
    return ns["_browser_navigate_gate"](tool, args)


# ── hint ──────────────────────────────────────────────────────────────────

def test_hint_fires_on_bot_block():
    hint = _load_hint()
    for err in (
        "403 Client Error: Forbidden",
        "429 Too Many Requests",
        "Access denied by cloudflare",
        "captcha required",
    ):
        out = hint(err)
        assert "browser__browser_navigate" in out, err
        assert "browser__browser_evaluate" in out, err
        assert "document.body.innerText" in out, err


def test_hint_silent_otherwise():
    hint = _load_hint()
    for err in ("404 Not Found", "Name or service not known", "", "timeout"):
        assert hint(err) == "", err


# ── gate ──────────────────────────────────────────────────────────────────

def test_gate_allows_public_url():
    assert _run_gate("browser__browser_navigate",
                     {"url": "https://example.com/article"}) == ""


def test_gate_ignores_other_tools():
    assert _run_gate("browser__browser_evaluate",
                     {"function": "() => 1"}) == ""
    assert _run_gate("codebase-search__search_graph", {"q": "x"}) == ""


def test_gate_refuses_loopback_and_lan():
    for url in (
        "http://localhost:8080/x",
        "http://127.0.0.1:9000/",
        "http://192.168.1.10/",
        "http://10.0.0.5/",
        "http://172.16.0.9/",
        "http://169.254.169.254/latest/meta-data/",
        "http://printer.local/",
        "ftp://example.com/x",
        "not-a-url",
        "",
    ):
        err = _run_gate("browser__browser_navigate", {"url": url})
        assert err.startswith("Refused to open"), url


def test_gate_refuses_during_render():
    err = _run_gate("browser__browser_navigate",
                    {"url": "https://example.com/article"},
                    image_active=True)
    assert "image render owns the machine" in err


# ── MCP config ────────────────────────────────────────────────────────────

def test_mcp_config_has_headed_browser():
    cfg_path = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "mcp_config.json"))
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    browser = cfg["mcpServers"]["browser"]
    assert browser.get("enabled") is True
    assert browser["command"] == "npx"
    assert "@playwright/mcp@0.0.81" in browser["args"]
    assert "--headless" not in browser["args"], "headed for now (later flip)"
    assert "--no-sandbox" in browser["args"]


# ── read persistence (real imports; conftest pins real dotenv) ────────────

NAV_RESULT = (
    "### Ran Playwright code\n```js\nawait page.goto('https://example.com/a');\n```\n"
    "### Page\n- Page URL: https://example.com/a\n- Page Title: Example Article\n"
)

EVAL_RESULT = (
    '### Result\n"First paragraph.\\nSecond paragraph."\n'
    "### Ran Playwright code\n```js\nawait page.evaluate('...')\n```"
)


def _task_ns():
    import threading
    import server.features.state as st
    prev = st._Registry.entrypoint
    tasks = {"t1": {}}
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(),
        _image_active=False,
        tasks=tasks,
    )
    return st, prev, tasks


def test_extract_evaluate_text():
    from server.features.tools import _browser_extract_evaluate_text
    out = _browser_extract_evaluate_text(EVAL_RESULT)
    assert "First paragraph." in out and "Second paragraph." in out
    assert "### Ran" not in out
    assert _browser_extract_evaluate_text("no markers here") == ""
    assert _browser_extract_evaluate_text("") == ""


def test_navigate_remembers_url_and_title(monkeypatch):
    from server.features import tools as tools_mod
    st, prev, tasks = _task_ns()
    try:
        tools_mod._browser_note_result(
            "t1", "browser__browser_navigate", {"url": "https://example.com/a"},
            NAV_RESULT)
    finally:
        st._Registry.entrypoint = prev
    assert tasks["t1"]["_browser_url"] == "https://example.com/a"
    assert tasks["t1"]["_browser_title"] == "Example Article"


def test_evaluate_persists_details_and_cache(monkeypatch):
    from server.features import tools as tools_mod
    from server.features.websearch import vector_store as page_cache
    st, prev, tasks = _task_ns()
    tasks["t1"]["_browser_url"] = "https://example.com/a"
    tasks["t1"]["_browser_title"] = "Example Article"
    puts = []
    monkeypatch.setattr(page_cache, "page_get", lambda *a, **k: None)
    monkeypatch.setattr(
        page_cache, "page_put",
        lambda canon, url, title, text, **k: puts.append((canon, url, title, text)),
    )
    try:
        tools_mod._browser_note_result(
            "t1", "browser__browser_evaluate",
            {"function": "() => document.body.innerText.slice(0,8000)"},
            EVAL_RESULT)
    finally:
        st._Registry.entrypoint = prev
    details = tasks["t1"]["_search_details"]
    assert len(details) == 1
    entry = details[0]
    assert entry["tool"] == "fetch_page"  # grounded, like direct fetches
    assert entry["url"] == "https://example.com/a"
    assert entry["via"] == "browser"
    assert "First paragraph." in entry["content"]
    assert puts and puts[0][0] == "https://example.com/a"
    assert "First paragraph." in puts[0][3]


def test_evaluate_skips_put_on_cache_hit(monkeypatch):
    from server.features import tools as tools_mod
    from server.features.websearch import vector_store as page_cache
    st, prev, tasks = _task_ns()
    tasks["t1"]["_browser_url"] = "https://example.com/a"
    puts = []
    monkeypatch.setattr(
        page_cache, "page_get", lambda *a, **k: {"text": "old"})
    monkeypatch.setattr(
        page_cache, "page_put",
        lambda *a, **k: puts.append(a),
    )
    try:
        tools_mod._browser_note_result(
            "t1", "browser__browser_evaluate", {"function": "() => 1"},
            EVAL_RESULT)
    finally:
        st._Registry.entrypoint = prev
    assert puts == []  # direct/cache entry wins; details still recorded
    assert tasks["t1"]["_search_details"][0]["via"] == "browser"


def test_evaluate_without_navigate_persists_nothing(monkeypatch):
    from server.features import tools as tools_mod
    from server.features.websearch import vector_store as page_cache
    st, prev, tasks = _task_ns()
    puts = []
    monkeypatch.setattr(page_cache, "page_get", lambda *a, **k: None)
    monkeypatch.setattr(
        page_cache, "page_put", lambda *a, **k: puts.append(a))
    try:
        tools_mod._browser_note_result(
            "t1", "browser__browser_evaluate", {"function": "() => 1"},
            EVAL_RESULT)
    finally:
        st._Registry.entrypoint = prev
    assert tasks["t1"].get("_search_details", []) == []
    assert puts == []


def test_browser_entry_grounds_citations():
    from server.features.critic import _retrieved_urls
    import threading
    import server.features.state as st
    prev = st._Registry.entrypoint
    st._Registry.entrypoint = types.SimpleNamespace(
        _data_lock=threading.RLock(),
        tasks={"t1": {"_search_details": [{
            "tool": "fetch_page", "url": "https://example.com/a",
            "content": "hello", "via": "browser",
        }]}},
    )
    try:
        assert "example.com/a" in _retrieved_urls("t1")
    finally:
        st._Registry.entrypoint = prev
