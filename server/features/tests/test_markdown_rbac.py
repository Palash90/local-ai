"""Markdown-hosting RBAC matrix (mocked Authentik identity, no server)."""

import types

import pytest
from fastapi import HTTPException

import markdown_hosting as md
import server.auth as auth


def _req(monkeypatch, identity):
    monkeypatch.setattr(auth, "get_identity", lambda headers: identity)
    monkeypatch.setattr(auth, "get_current_user",
                        lambda headers: (identity or {}).get("username"))
    return types.SimpleNamespace(headers={})


def _code(fn, *a, **k):
    try:
        fn(*a, **k)
    except HTTPException as e:
        return e.status_code
    return 200


def test_guest_matrix(monkeypatch):
    r = _req(monkeypatch, None)
    assert _code(md.enforce_rbac, "free_stories", request=r) == 200
    assert _code(md.enforce_rbac, "premium_stories", request=r) == 401
    assert _code(md.enforce_rbac, "admin_stories", request=r) == 401
    assert _code(md.enforce_rbac, "nope", request=r) == 404


def test_free_user_forbidden_not_unauthorized(monkeypatch):
    r = _req(monkeypatch, {"username": "u", "role": "free"})
    assert _code(md.enforce_rbac, "free_stories", request=r) == 200
    assert _code(md.enforce_rbac, "premium_stories", request=r) == 403
    assert _code(md.enforce_rbac, "admin_stories", request=r) == 403


def test_premium_and_admin_ladder(monkeypatch):
    r = _req(monkeypatch, {"username": "t", "role": "premium"})
    assert _code(md.enforce_rbac, "premium_stories", request=r) == 200
    assert _code(md.enforce_rbac, "admin_stories", request=r) == 403
    r = _req(monkeypatch, {"username": "a", "role": "admin"})
    assert _code(md.enforce_rbac, "admin_stories", request=r) == 200


def test_role_levels_and_legacy_groups():
    assert md.user_role_level(None) == 0
    assert md.user_role_level("palash") == 2
    assert md.user_role_level("totan") == 1
    assert md.user_role_level("nobody") == 0
    assert md.user_role_level(None, "premium") == 1
    assert md.user_role_level(None, "bogus") == 0
