"""Regression tests for the hybrid OpenAI lane (server-first tool routing).

The helpers live in llm.py / orchestration.py, which can't be imported
under pytest (server/dotenv.py shadows the installed `dotenv` package
once the import chain reaches pydantic). So load each helper's source
by AST and exec it in isolation with a stubbed M — testing the exact
shipped logic, not a copy.
"""

import ast
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_LLM = os.path.join(_HERE, "..", "llm.py")
_ORCH = os.path.join(_HERE, "..", "orchestration.py")


def _load(name, path, stub_M=None):
    src = open(path).read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            code = compile(ast.Module(body=[node], type_ignores=[]), path, "exec")
            ns = {"M": stub_M}
            exec(code, ns)
            return ns[name]
    raise AssertionError(f"{name} not found in {path}")


class _M:
    OPENAI_LANE_SERVER_TOOL_NAMES = {"web_search", "fetch_page", "tool_details"}
    OPENAI_LANE_MAX_SERVER_ROUNDS = 3


def _srv(name, desc="d"):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {}}}


def _cli(name):
    return {"type": "function", "function": {"name": name, "description": "c", "parameters": {}}}


def _wire():
    return _load("_openai_lane_wire_tools", _LLM, _M())


def _choice():
    return _load("_openai_lane_tool_choice", _LLM, _M())


def _part():
    m = _M()
    ns = {"M": m}
    for name in ("_is_openai_lane_server_tool", "_openai_lane_server_calls"):
        src = open(_ORCH).read()
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                exec(compile(ast.Module(body=[node], type_ignores=[]), _ORCH, "exec"), ns)
    return ns["_is_openai_lane_server_tool"], ns["_openai_lane_server_calls"]


def test_wire_merge_auto_client_plus_server():
    wire = _wire()
    server_pool = [_srv("web_search"), _srv("fetch_page"), _srv("tool_details"),
                   _srv("generate_image"), _srv("get_user_location")]
    out = wire([_cli("webfetch"), _cli("edit")], server_pool, "auto", False)
    assert [t["function"]["name"] for t in out] == \
        ["web_search", "fetch_page", "tool_details", "webfetch", "edit"]


def test_wire_never_legacy_client_only():
    wire = _wire()
    out = wire([_cli("webfetch")], [_srv("web_search")], "never", False)
    assert [t["function"]["name"] for t in out] == ["webfetch"]


def test_wire_only_drops_client():
    wire = _wire()
    out = wire([_cli("webfetch")], [_srv("web_search"), _srv("generate_image")], "only", False)
    assert [t["function"]["name"] for t in out] == ["web_search"]


def test_wire_collision_prefers_server():
    wire = _wire()
    out = wire([_cli("web_search")], [_srv("web_search")], "auto", False)
    assert [t["function"]["name"] for t in out] == ["web_search"]
    assert out[0]["function"]["description"] == "d"


def test_tool_choice_forced_auto_on_merge():
    choice = _choice()
    assert choice([_cli("webfetch")], "none", "auto", False) == "auto"
    assert choice([_cli("webfetch")], "required", "auto", False) == "auto"
    assert choice([], "none", "only", False) == "auto"
    assert choice([_cli("webfetch")], "none", "never", False) == "none"


def test_partition_server_first():
    is_srv, srv_calls = _part()
    tcs = [
        {"function": {"name": "web_search", "arguments": "{}"}},
        {"function": {"name": "webfetch", "arguments": "{}"}},
        {"function": {"name": "fetch_page", "arguments": "{}"}},
    ]
    assert is_srv(tcs[0]) is True
    assert is_srv(tcs[1]) is False
    assert is_srv(tcs[2]) is True
    assert [c["function"]["name"] for c in srv_calls(tcs)] == ["web_search", "fetch_page"]
    assert is_srv({}) is False
    assert is_srv({"function": {"name": "generate_image"}}) is False
