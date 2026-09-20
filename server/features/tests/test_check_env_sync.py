"""check_env --sync/--prune/--fill-defaults on scratch files (never live .env).

Covers: additive-only sync, backup creation, prune-comments (not delete),
fill scoping (safe keys only; secrets/toggles/dynamics untouched), and the
values-never-printed guarantee.
"""

import importlib.util
import json
import sys

import pytest


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_env_under_test", "scripts/check_env.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()



REQ_SEED = "POSTGRES_PASSWORD=pw\nAUTHENTIK_SECRET_KEY=sk\n"


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def _run(mod, monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["check_env.py", *argv])
    rc = mod.main()
    return rc, capsys.readouterr().out


def test_sync_appends_missing_leaves_existing(mod, monkeypatch, tmp_path, capsys):
    _write(tmp_path / "example", "AAA=1\nBBB=2\nKEEPER=yes\n")
    _write(tmp_path / ".env", REQ_SEED + "KEEPER=custom\n")
    env = str(tmp_path / ".env")
    rc, out = _run(mod, monkeypatch, capsys,
                    "--env", env, "--example", str(tmp_path / "example"), "--sync")
    assert rc == 0
    body = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "KEEPER=custom" in body  # existing value untouched
    assert "AAA=1" in body and "BBB=2" in body
    assert "SYNC: appended AAA" in out
    assert list(tmp_path.glob(".env.bak.*")), "backup required"


def test_prune_comments_but_keeps_values(mod, monkeypatch, tmp_path, capsys):
    _write(tmp_path / "example",
            "AAA=1\nPOSTGRES_PASSWORD=x\nAUTHENTIK_SECRET_KEY=x\n")
    _write(tmp_path / ".env", REQ_SEED + "AAA=1\nSTALE=zzz\n")
    env = str(tmp_path / ".env")
    rc, out = _run(mod, monkeypatch, capsys,
                    "--env", env, "--example", str(tmp_path / "example"), "--prune")
    assert rc == 0
    body = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "STALE=zzz" in body  # value preserved in comment, not deleted
    assert "PRUNE: commented out STALE" in out
    assert list(tmp_path.glob(".env.bak.*"))


def test_fill_scopes_to_safe_keys(mod, monkeypatch, tmp_path, capsys):
    _write(tmp_path / "example", "X=\n")
    _write(tmp_path / ".env", REQ_SEED + "X=\n")
    monkeypatch.setattr(mod, "SAFE_DEFAULTS", {"SAFE_KEY": "lit", "OTHER": "x"},
                        raising=False)
    # SAFE_DEFAULTS bypasses the template here; fill must still skip nothing
    # in-table but the table itself is the scoping mechanism (asserted below).
    env = str(tmp_path / ".env")
    rc, out = _run(mod, monkeypatch, capsys,
                    "--env", env, "--example", str(tmp_path / "example"),
                    "--fill-defaults")
    assert rc == 0
    body = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "SAFE_KEY=lit" in body


def test_fill_skips_secrets_and_toggles(mod, monkeypatch, tmp_path, capsys):
    secret = "s3cr3t-value"
    _write(tmp_path / "example", "A=\n")
    _write(tmp_path / ".env",
            REQ_SEED + "MCP_USER_PASSWORD=\nGUARD_LLM_MODEL=\nLOCAL_AI_DB=\n"
            f"POSTGRES_PASSWORD={secret}\n")
    env = str(tmp_path / ".env")
    rc, out = _run(mod, monkeypatch, capsys,
                    "--env", env, "--example", str(tmp_path / "example"),
                    "--fill-defaults")
    assert rc == 0
    body = (tmp_path / ".env").read_text(encoding="utf-8")
    assert secret in body  # untouched...
    assert secret not in out  # ...and never printed
    for line in body.splitlines():
        assert not line.startswith("MCP_USER_PASSWORD=") or line == "MCP_USER_PASSWORD="
        assert not line.startswith("GUARD_LLM_MODEL=") or line == "GUARD_LLM_MODEL="
        assert not line.startswith("LOCAL_AI_DB=") or line == "LOCAL_AI_DB="


def test_load_env_ordered_keeps_equals_in_values(mod):
    vals = mod.load_env_ordered.__wrapped__ if hasattr(
        mod.load_env_ordered, "__wrapped__") else mod.load_env_ordered
    import tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write('AGENT_PEER_MAP={"kaya": "kolpo"}\n')
        path = f.name
    try:
        assert vals(path)["AGENT_PEER_MAP"] == '{"kaya": "kolpo"}'
    finally:
        os.unlink(path)


def test_real_safe_defaults_exclude_secrets(mod):
    table = mod.SAFE_DEFAULTS
    for forbidden in ("POSTGRES_PASSWORD", "AUTHENTIK_SECRET_KEY",
                      "MCP_USER_PASSWORD", "GUARD_LLM_MODEL", "GUARDRAIL_EXTERNAL",
                      "LOCAL_AI_DB", "BASE_MODELS_DIR", "OPENAI_API_KEY",
                      "SELF_CHAT_PASSWORD", "AUTH_CLIENT_SECRET"):
        assert forbidden not in table, forbidden
    # Spot-check known-safe entries with code-matching literals.
    assert table["GPU_NGL_26B"] == "24"
    assert table["OPENAI_SERVER_TOOLS"] == "auto"
    assert table["KEEP_GPU_RESIDENT"] == "0"
