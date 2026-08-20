"""Unified RBAC / SSO identity layer backed by Authentik.

This is the SINGLE identity provider for every app on the box. There is no
more ``users.json``; users, passwords and roles live in Authentik.

Two identity paths converge here:

1. Browser users — nginx runs ``auth_request`` against the Authentik proxy
   outpost and forwards the ``X-Authentik-*`` claim headers upstream. Backends
   trust those headers (only nginx can reach the app ports).

2. Machine agents (self-chat.py) — obtain an OIDC access token via Authentik's
   OAuth2 password grant and send it as ``Authorization: Bearer <jwt>``.
   Backends verify the JWT signature against Authentik's JWKS.

The resolved identity is always a dict::

    {
        "username": str,
        "email": str,
        "name": str,
        "groups": [str, ...],   # raw Authentik group names
        "role": "admin" | "premium" | "free",
        "uid": str,             # Authentik user UUID (unique id across renames)
    }
"""

import re
import threading
import time

import jwt
import requests

from server.config import (
    AUTH_CLIENT_ID,
    AUTH_CLIENT_SECRET,
    AUTH_ISSUER,
    AUTH_JWKS_URL,
    AUTH_ROLE_GROUPS,
    AUTH_SCOPE,
    AUTH_TOKEN_URL,
)

# Highest role wins when a user belongs to several groups.
_ROLE_LEVEL = {"free": 0, "premium": 1, "admin": 2}

_jwks_cache = None
_jwks_cache_at = 0.0
_jwks_lock = threading.Lock()
_JWKS_TTL = 300


def _first(seq):
    for item in seq or ():
        if item:
            return item
    return None


def role_from_groups(groups):
    """Map Authentik group names to the app role scale (free/premium/admin)."""
    best = "free"
    best_level = -1
    for g in groups or ():
        role = AUTH_ROLE_GROUPS.get(g.lower().strip())
        if role and _ROLE_LEVEL.get(role, -1) > best_level:
            best = role
            best_level = _ROLE_LEVEL[role]
    return best


def _split_groups(raw):
    if not raw:
        return []
    return [g.strip() for g in re.split(r"[,\s]+", str(raw)) if g.strip()]


def identity_from_headers(headers):
    """Resolve identity from the nginx-injected X-Authentik-* claim headers.

    These headers are set by nginx's auth_request subrequest against the
    Authentik proxy outpost and are only present on nginx-fronted traffic.
    Returns None when absent (e.g. direct localhost calls or agents).
    """
    username = _first(
        [headers.get("X-Authentik-Username"), headers.get("X-Authentik-User")]
    )
    if not username:
        return None
    groups = _split_groups(
        _first(
            [
                headers.get("X-Authentik-Groups"),
                headers.get("X-Authentik-Group"),
            ]
        )
    )
    return {
        "username": username,
        "email": headers.get("X-Authentik-Email", ""),
        "name": headers.get("X-Authentik-Name", ""),
        "groups": groups,
        "role": role_from_groups(groups),
        "uid": headers.get("X-Authentik-UID", ""),
    }


def _fetch_jwks():
    """Return the Authentik JWKS key set (cached for _JWKS_TTL seconds)."""
    global _jwks_cache, _jwks_cache_at
    with _jwks_lock:
        now = time.time()
        if _jwks_cache is not None and now - _jwks_cache_at < _JWKS_TTL:
            return _jwks_cache
        if not AUTH_JWKS_URL:
            return []
        try:
            resp = requests.get(AUTH_JWKS_URL, timeout=5)
            resp.raise_for_status()
            _jwks_cache = resp.json().get("keys", [])
            _jwks_cache_at = now
        except (requests.RequestException, ValueError):
            _jwks_cache = _jwks_cache or []
            _jwks_cache_at = now
        return _jwks_cache


def identity_from_bearer(authorization):
    """Verify an ``Authorization: Bearer <jwt>`` token and resolve its identity.

    Verifies the token signature against Authentik's JWKS and enforces the
    issuer (the Authentik OIDC provider URL). Returns None for expired, bogus
    or missing tokens. Raises on transient network failures (so callers can
    treat those distinctly from a plain "no identity").
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    keys = _fetch_jwks()
    if not keys:
        raise RuntimeError("Authentik JWKS unavailable — cannot verify access token")

    decoded = None
    for key in keys:
        try:
            decoded = jwt.decode(
                token,
                key,
                algorithms=[key.get("alg", "RS256")],
                issuer=AUTH_ISSUER,
                options={"verify_aud": False},
            )
            break
        except jwt.InvalidTokenError:
            continue
    if not decoded:
        return None

    username = _first(
        [
            decoded.get("preferred_username"),
            decoded.get("username"),
            decoded.get("email"),
            decoded.get("sub"),
        ]
    )
    groups = _split_groups(
        _first([decoded.get("groups"), decoded.get("ak_groups")])
    )
    return {
        "username": username,
        "email": decoded.get("email", ""),
        "name": decoded.get("name", ""),
        "groups": groups,
        "role": role_from_groups(groups),
        "uid": decoded.get("sub", ""),
    }


def get_identity(headers):
    """Resolve identity from request headers (browser path + agent path)."""
    identity = identity_from_headers(headers)
    if identity:
        return identity
    return identity_from_bearer(headers.get("Authorization", ""))


def get_current_user(headers):
    """Return just the authenticated username (or None)."""
    identity = get_identity(headers)
    return identity["username"] if identity else None


def required_role_level(role):
    """Level for the given role name (free=0, premium=1, admin=2)."""
    return _ROLE_LEVEL.get(role, 0)


def oidc_password_grant(username, password):
    """Exchange agent credentials for an Authentik OIDC access token.

    Used by self-chat.py so the automated agents authenticate through the same
    identity provider as humans. Returns the access token string. Raises on any
    failure so callers can surface a clear error.
    """
    if not AUTH_TOKEN_URL or not AUTH_CLIENT_ID:
        raise RuntimeError("Authentik OIDC not configured (AUTH_TOKEN_URL/AUTH_CLIENT_ID)")
    resp = requests.post(
        AUTH_TOKEN_URL,
        data={
            "grant_type": "password",
            "username": username,
            "password": password,
            "client_id": AUTH_CLIENT_ID,
            "client_secret": AUTH_CLIENT_SECRET,
            "scope": AUTH_SCOPE,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    access_token = data.get("access_token")
    if not access_token:
        raise RuntimeError(f"Authentik password grant returned no access token: {data}")
    return access_token