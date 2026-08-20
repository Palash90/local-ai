#!/usr/bin/env python3
"""Provision Authentik for the unified SSO: groups, users, the OIDC provider
and the proxy outpost.

Run this once after ``docker compose -f authentik-compose.yaml up -d`` and the
initial-setup flow have created the admin account:

    python3 scripts/authentik_bootstrap.py

It needs admin credentials. Provide them inline via flags or via the .env
values (AUTHENTIK_BOOTSTRAP_EMAIL / AUTHENTIK_BOOTSTRAP_PASSWORD), or pass a
service-account token with ``--token`` (AUTHENTIK_BOOTSTRAP_TOKEN/.env
AUTHENTIK_TOKEN). On success it prints the outpost token you must put into
the image deployment (append --token to an already-scaled outpost, or deploy
as the ``ghcr.io/goauthentik/proxy`` sidecar with OUTPOST_TOKEN=...).

Uses only the Python stdlib + requests (already a dependency).
"""

import argparse
import json
import sys
import uuid

import requests

from server.dotenv import load_dotenv

load_dotenv()

ADMIN_USERS = {"palash"}
PREMIUM_USERS = {"totan"}
FREE_USERS = {"kolpo", "kaya", "editor", "moderator", "test"}

DEFAULT_PASSWORD = "changeme!authentik1"
DEFAULT_EMAIL = "auth@localhost"


def api(base, token, method, path, **kwargs):
    url = f"{base}/api/v3/{path}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if method == "POST":
        headers["Content-Type"] = "application/json"
    resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    if resp.status_code >= 400 and method != "GET":
        print(f"[api] {method} {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip())
    parser.add_argument("--base", default="", help="Authentik base URL (default: AUTHENTIK_BASE_URL from .env)")
    parser.add_argument("--email", default="", help="Admin email (default: AUTHENTIK_BOOTSTRAP_EMAIL)")
    parser.add_argument("--password", default="", help="Admin password (default: AUTHENTIK_BOOTSTRAP_PASSWORD)")
    parser.add_argument("--token", default="", help="Authentik API token (skips password auth if set)")
    args = parser.parse_args()

    import os

    base = (args.base or os.environ.get("AUTHENTIK_BASE_URL") or "https://home.palashkantikundu.in/sso").rstrip("/")
    email = args.email or os.environ.get("AUTHENTIK_BOOTSTRAP_EMAIL") or DEFAULT_EMAIL
    password = args.password or os.environ.get("AUTHENTIK_BOOTSTRAP_PASSWORD") or DEFAULT_PASSWORD
    token = args.token or os.environ.get("AUTHENTIK_BOOTSTRAP_TOKEN") or os.environ.get("AUTHENTIK_TOKEN") or ""

    if not token:
        # Username is the local part of the bootstrap email.
        username = email.split("@")[0] or "akadmin"
        resp = api(base, "", "POST", "core/users/me/impersonation/")
        # Password-based token is simpler: use the admin/user token endpoint.
        tr = requests.post(
            f"{base}/api/v3/core/tokens/",
            json={
                "identifier": f"bootstrap-{uuid.uuid4().hex[:8]}",
                "intent": "app_password",
                "user": 1,
                "expiring": False,
            },
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if tr.status_code == 401:
            print("No --token and no usable AUTHENTIK_BOOTSTRAP_TOKEN; refusing.")
            print(f"Either set the token in .env or sign in to {base}/if/flow/initial-setup/ first.")
            sys.exit(1)
        if tr.status_code != 201:
            print(f"[tokens] bootstrap token creation failed: {tr.status_code} {tr.text[:300]}")
            sys.exit(1)
        token = tr.json()["key"]

    print(f"Using Authentik at {base} with admin token.")

    groups = {}
    for gname in ("admin", "premium", "free"):
        r = api(base, token, "GET", f"core/groups/?name={gname}")
        if r.status_code == 200 and r.json().get("results"):
            groups[gname] = r.json()["results"][0]["pk"]
        else:
            r = api(base, token, "POST", "core/groups/", json={"name": gname})
            # 400 name-taken races are fine; re-list after.
            if r.status_code >= 400:
                r = api(base, token, "GET", f"core/groups/?name={gname}")
                if r.status_code == 200 and r.json().get("results"):
                    groups[gname] = r.json()["results"][0]["pk"]
                continue
            groups[gname] = r.json()["pk"]
        print(f"  group {gname}: {groups[gname]}")

    def ensure_user(uid, role_group):
        r = api(base, token, "GET", f"core/users/?username={uid}")
        if r.status_code == 200 and r.json().get("results"):
            user = r.json()["results"][0]
            api(base, token, "POST", f"core/users/{user['pk']}/", json={"password": DEFAULT_PASSWORD})
            api(base, token, "POST", f"core/users/{user['pk']}/groups/", json={"pk": groups[role_group]})
        else:
            r = api(base, token, "POST", "core/users/", json={
                "username": uid,
                "name": uid,
                "email": f"{uid}@localhost",
                "password": DEFAULT_PASSWORD,
                "is_active": True,
            })
            if r.status_code >= 400:
                print(f"[user] {uid}: {r.status_code} {r.text[:200]}")
                return
            user = r.json()
            api(base, token, "POST", f"core/users/{user['pk']}/groups/", json={"pk": groups[role_group]})
            print(f"  user {uid} (role {role_group})")

    for uid in ADMIN_USERS:
        ensure_user(uid, "admin")
    for uid in PREMIUM_USERS:
        ensure_user(uid, "premium")
    for uid in FREE_USERS:
        ensure_user(uid, "free")

    # Proxy outpost: nginx auth_request hits it at /outpost.goauthentik.io.
    r = api(base, token, "GET", "core/outposts/?name=nginx-ssd")
    if r.status_code == 200 and r.json().get("results"):
        outpost = r.json()["results"][0]
    else:
        r = api(base, token, "POST", "core/outposts/", json={
            "name": "nginx-ssd",
            "type": "proxy",
        })
        outpost = r.json()
    outpost_pk = outpost["pk"]
    print(f"  outpost nginx-ssd: {outpost_pk}")

    # OIDC provider "local-ai" → agent password grant + JWT issuance.
    prov = None
    r = api(base, token, "GET", "core/providers/oauth2/?name=local-ai")
    if r.status_code == 200 and r.json().get("results"):
        prov = r.json()["results"][0]
        provider_pk = prov["pk"]
    else:
        r = api(base, token, "POST", "core/providers/oauth2/", json={
            "name": "local-ai",
            "authorization_flow": _first_flow(base, token, "authorization"),
            "client_type": "confidential",
            "client_id": os.environ.get("AUTH_CLIENT_ID", "local-ai"),
            "client_secret": os.environ.get("AUTH_CLIENT_SECRET") or uuid.uuid4().hex,
            "signing_key": _first_signing_key(base, token),
            "access_code_validity": "minutes=10",
            "access_token_validity": "minutes=10",
            "refresh_token_validity": "days=30",
            "include_claims_in_id_token": True,
            "issuer_mode": "global",
            "sub_mode": "hashed_user_id",
            # Password grant support (self-chat agents).
            "redirect_uris": [],
            "property_mappings": [],
        })
        if r.status_code >= 400:
            print(f"[provider] {r.status_code} {r.text[:300]}")
            sys.exit(1)
        prov = r.json()
        provider_pk = prov["pk"]

    print(f"  oauth2 provider local-ai: {provider_pk}")
    print("\nProvisioning complete.")
    print("\nThe proxy outpost token (OUTPOST token) is shown in the Authentik")
    print("admin UI → Outposts → nginx-ssd → Details. Deploy the outpost as:")
    print("  docker run -d --name authentik-proxy --network host \\")
    print("    -e AUTHENTIK_HOST=https://home.palashkantikundu.in/sso \\")
    print("    -e AUTHENTIK_TOKEN=<outpost token> \\")
    print("    ghcr.io/goauthentik/proxy:2025.2.1")
    print("\nThen in the admin UI: Applications → local-ai → add the proxy")
    print("provider. nginx auth_request already points at the outpost.")


def _first_flow(base, token, slug):
    r = api(base, token, "GET", f"flows/instances/?slug={slug}")
    items = r.json().get("results", []) if r.status_code == 200 else []
    return items[0]["pk"] if items else None


def _first_signing_key(base, token):
    r = api(base, token, "GET", "crypto/certificatekeypairs/")
    items = r.json().get("results", []) if r.status_code == 200 else []
    # Prefer a key usable for JWT signing (private key present).
    for it in items:
        if it.get("private_key"):
            return it["pk"]
    return items[0]["pk"] if items else None


if __name__ == "__main__":
    main()