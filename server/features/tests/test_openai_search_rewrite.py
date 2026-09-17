"""Regression tests for the option-(b) search rewrite.

Search-shaped client fetches (opencode webfetch of a google.com/search URL)
become server web_search calls; genuine page fetches pass through. Helpers
are AST-isolated like test_openai_hybrid_lane (llm.py/orchestration.py can't
import under pytest due to the server/dotenv.py shadowing).
"""

import ast
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ORCH = os.path.join(_HERE, "..", "orchestration.py")


class _M:
    OPENAI_LANE_SERVER_TOOL_NAMES = {"web_search", "fetch_page", "tool_details"}
    OPENAI_LANE_MAX_SERVER_ROUNDS = 3
    OPENAI_LANE_SEARCH_REWRITE = "auto"
    OPENAI_LANE_FETCH_ALIASES = {"webfetch", "fetch", "fetch_url", "web_fetch", "read_url"}


def _ns():
    src = open(_ORCH).read()
    tree = ast.parse(src)
    ns = {"M": _M(), "json": json}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in (
                "_extract_search_query", "_rewrite_search_fetches",
                "_is_openai_lane_server_tool", "_openai_lane_server_calls"):
            exec(compile(ast.Module(body=[node], type_ignores=[]), _ORCH, "exec"), ns)
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") in ("_SEARCH_ENGINE_PATHS", "_SEARCH_QUERY_PARAMS")
                for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), _ORCH, "exec"), ns)
    return ns


def _tc(name, args):
    return {"index": 0, "id": "call_1", "type": "function",
            "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}


def test_google_search_url_rewrites():
    ns = _ns()
    q = ns["_extract_search_query"](
        _tc("webfetch", {"url": "https://www.google.com/search?q=Gemma4+26B+A4B+uncensored"}))
    assert q == "Gemma4 26B A4B uncensored", q


def test_bing_duckduckgo_rewrite():
    ns = _ns()
    assert ns["_extract_search_query"](
        _tc("webfetch", {"url": "https://www.bing.com/search?q=hello+world"})) == "hello world"
    assert ns["_extract_search_query"](
        _tc("fetch", {"url": "https://duckduckgo.com/html?q=test+query"})) == "test query"


def test_genuine_page_fetch_passes_through():
    ns = _ns()
    assert ns["_extract_search_query"](
        _tc("webfetch", {"url": "https://huggingface.co/models?search=gemma"})) is None
    assert ns["_extract_search_query"](
        _tc("webfetch", {"url": "https://pypi.org/project/requests/"})) is None


def test_malformed_and_empty_fail_open():
    ns = _ns()
    ex = ns["_extract_search_query"]
    assert ex(_tc("webfetch", {"url": "https://www.google.com/search"})) is None
    assert ex(_tc("webfetch", {"url": "https://www.google.com/search?q="})) is None
    assert ex(_tc("webfetch", "not-json{{{")) is None
    assert ex(_tc("webfetch", {"url": "not a url"})) is None
    assert ex(_tc("edit", {"url": "https://www.google.com/search?q=x"})) is None
    assert ex({}) is None


def test_rewrite_preserves_id_and_feeds_partition():
    ns = _ns()
    calls = [_tc("webfetch", {"url": "https://www.google.com/search?q=abc"})]
    new_calls, n = ns["_rewrite_search_fetches"](calls, "t1")
    assert n == 1
    assert new_calls[0]["id"] == "call_1"
    assert new_calls[0]["index"] == 0
    assert new_calls[0]["function"]["name"] == "web_search"
    assert json.loads(new_calls[0]["function"]["arguments"]) == {"query": "abc"}
    # rewritten call is now a server call for the partition
    assert [c["function"]["name"] for c in ns["_openai_lane_server_calls"](new_calls)] == ["web_search"]


def test_rewrite_never_mode_passes_through():
    ns = _ns()
    ns["M"].OPENAI_LANE_SEARCH_REWRITE = "never"
    calls = [_tc("webfetch", {"url": "https://www.google.com/search?q=abc"})]
    new_calls, n = ns["_rewrite_search_fetches"](calls, "t1")
    assert n == 0
    assert new_calls[0]["function"]["name"] == "webfetch"
