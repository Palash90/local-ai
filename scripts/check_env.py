#!/usr/bin/env python3
"""Validate the local-ai `.env` file before starting services.

Usage:
    python3 scripts/check_env.py [--env PATH] [--strict]

- Parses KEY=VALUE lines (no shell execution, values never printed).
- Errors on required variables that are missing, empty, or left as CHANGE_ME.
- Warns on optional-but-expected secrets that are empty (feature will be disabled).
- `--strict` turns warnings into errors (useful for CI / production).
- Compares against `.env.example` and reports keys present in one but not the other.

Exit status: 0 when clean (or warnings only without --strict), 1 otherwise.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Required: compose/services refuse to start or fail closed without these.
REQUIRED = ("POSTGRES_PASSWORD", "AUTHENTIK_SECRET_KEY")

# Optional: empty means the corresponding feature is disabled/degraded.
OPTIONAL_SECRETS = (
    "AUTH_CLIENT_SECRET",
    "AUTH_AGENTS_CLIENT_SECRET",
    "MCP_USER_PASSWORD",
    "MCP_OAUTH_CLIENT_SECRET",
    "SELF_CHAT_PASSWORD",
    "GODADDY_API_KEY",
    "GODADDY_API_SECRET",
    "GOOGLE_API_KEY",
    "GOOGLE_CSE_ID",
    "OPENAI_API_KEY",
    "TTS_INTERNAL_TOKEN",
    "SURFACE_ATTACKS_KEY",
)

PLACEHOLDERS = ("", "CHANGE_ME", "changeme", "TODO", "xxx")


def load_env(path):
    """Parse a dotenv file into a dict. No expansion, no execution."""
    vals = {}
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                print(f"WARN: {path}:{lineno}: ignoring malformed line (no '=')")
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            vals[key] = value
    return vals


def main():
    ap = argparse.ArgumentParser(description="Validate local-ai .env file")
    ap.add_argument("--env", default=os.path.join(HERE, ".env"))
    ap.add_argument("--example", default=os.path.join(HERE, ".env.example"))
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as errors")
    args = ap.parse_args()

    errors, warnings = [], []

    if not os.path.isfile(args.env):
        print(f"ERROR: env file not found: {args.env} (copy .env.example to .env)")
        return 1
    vals = load_env(args.env)

    for key in REQUIRED:
        if vals.get(key, "").strip() in PLACEHOLDERS:
            errors.append(f"{key} is required but missing/empty/placeholder")

    for key in OPTIONAL_SECRETS:
        if vals.get(key, "").strip() in PLACEHOLDERS:
            warnings.append(f"{key} is empty — related feature will be disabled")

    if os.path.isfile(args.example):
        example_keys = set(load_env(args.example))
        env_keys = set(vals)
        for key in sorted(example_keys - env_keys):
            warnings.append(f"{key} is in .env.example but missing from .env")
        for key in sorted(env_keys - example_keys):
            warnings.append(f"{key} is in .env but not documented in .env.example")

    for w in warnings:
        print(f"WARN: {w}")
    for e in errors:
        print(f"ERROR: {e}")

    if errors or (args.strict and warnings):
        print(f"check_env: {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    print(f"check_env: OK ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
