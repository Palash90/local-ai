#!/usr/bin/env python3
"""Validate the local-ai `.env` file before starting services.

Usage:
    python3 scripts/check_env.py [--env PATH] [--strict] [--sync] [--prune]

- Parses KEY=VALUE lines (no shell execution, values never printed).
- Errors on required variables that are missing, empty, or left as CHANGE_ME.
- Warns on optional-but-expected secrets that are empty (feature will be disabled).
- `--strict` turns warnings into errors (useful for CI / production).
- Compares against `.env.example` and reports keys present in one but not the other.
- `--sync` appends template keys missing from the live file (with template
  defaults) under a dated block, after backing up the live file to
  `.env.bak.YYYYMMDD-HHMMSS`. Existing lines are never modified.
- `--prune` comments out live keys undocumented in the template (same backup).
  Values are never printed by either mode.

Exit status: 0 when clean (or warnings only without --strict), 1 otherwise.
"""

import argparse
import datetime
import os
import shutil
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


def load_env_ordered(path):
    """Parse a dotenv file into an ordered dict of key -> raw default value."""
    vals = {}
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in vals:
                vals[key] = value.strip()
    return vals


def main():
    ap = argparse.ArgumentParser(description="Validate local-ai .env file")
    ap.add_argument("--env", default=os.path.join(HERE, ".env"))
    ap.add_argument("--example", default=os.path.join(HERE, ".env.example"))
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as errors")
    ap.add_argument("--sync", action="store_true",
                    help="append template keys missing from the live file "
                         "(backup first, existing lines untouched)")
    ap.add_argument("--prune", action="store_true",
                    help="comment out live keys undocumented in the template "
                         "(backup first, reversible)")
    args = ap.parse_args()

    errors, warnings = [], []

    if not os.path.isfile(args.env):
        print(f"ERROR: env file not found: {args.env} (copy .env.example to .env)")
        return 1

    if (args.sync or args.prune) and os.path.isfile(args.example):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{args.env}.bak.{stamp}"
        shutil.copy2(args.env, backup)
        print(f"backup: {backup}")
        with open(args.env, encoding="utf-8") as f:
            lines = f.readlines()
        live_keys = set(load_env(args.env))
        if args.sync:
            tmpl = load_env_ordered(args.example)
            missing = [k for k in tmpl if k not in live_keys]
            if missing:
                with open(args.env, "a", encoding="utf-8") as f:
                    f.write(f"\n# ── synced from .env.example on {stamp} ──\n")
                    for k in missing:
                        f.write(f"{k}={tmpl[k]}\n")
                for k in missing:
                    print(f"SYNC: appended {k} (template default)")
            else:
                print("SYNC: live file already covers the template")
        if args.prune:
            tmpl_keys = set(load_env(args.example))
            # Re-read: a --sync above may have appended since `lines` was captured.
            with open(args.env, encoding="utf-8") as f:
                lines = f.readlines()
            pruned = []
            out = []
            for raw in lines:
                stripped = raw.strip()
                key = None
                if stripped and not stripped.startswith("#"):
                    body = stripped[len("export "):].strip() if stripped.startswith("export ") else stripped
                    if "=" in body:
                        key = body.partition("=")[0].strip()
                if key and key not in tmpl_keys:
                    out.append(f"# pruned {stamp} (undocumented): {raw if raw.endswith(chr(10)) else raw + chr(10)}")
                    pruned.append(key)
                else:
                    out.append(raw)
            if pruned:
                with open(args.env, "w", encoding="utf-8") as f:
                    f.writelines(out)
                for k in pruned:
                    print(f"PRUNE: commented out {k} (reversible, see backup)")
            else:
                print("PRUNE: nothing undocumented in live file")

    # Validate the post-sync state (reload so SYNC/PRUNE results are checked).
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
