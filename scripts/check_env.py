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
- `--fill-defaults` writes SAFE_DEFAULTS literals for table keys whose live
  value is empty or missing (backup first). Secrets, feature-toggles, and
  dynamic-path keys are never filled — see SAFE_DEFAULTS.

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

# Static code defaults safe to materialize into the live file. Each value
# mirrors the documented code default (source noted); keys whose empty state
# means "disabled" (OPTIONAL_SECRETS, GUARD_LLM_MODEL, GUARDRAIL_EXTERNAL,
# MCP_USER*, passwords, API keys/tokens) and keys with dynamic/computed
# defaults (paths via expanduser/join, BASE_MODELS_DIR which has none) are
# deliberately absent — filling those would silently enable features or pin
# machine-specific paths.
SAFE_DEFAULTS = {
    # Chat/MCP core (server/mcp_gateway.py, server/mcp_client.py, config.py)
    "CHAT_HOST": "127.0.0.1",
    "CHAT_API_BASE": "http://127.0.0.1:3001",
    "MCP_HOST": "127.0.0.1",
    "MCP_TOOL_TIMEOUT": "300",
    "FORCE_GPU_LANE": "false",
    # Models/VRAM (server/config.py)
    "GPU_CTX_SIZE": "24576",
    "GPU_CTX_SIZE_26B": "32768",
    "GPU_NGL_26B": "24",
    "CPU_CTX_SIZE": "32768",
    "MAX_INPUT_TOKENS": "24576",
    "MAX_OUTPUT_TOKENS": "4096",
    "REASONING_BUDGET": "1024",
    "CPU_IDLE_UNLOAD_SECONDS": "300",
    "CPU_KV_SAVE_INTERVAL_SECONDS": "120",
    "KEEP_GPU_RESIDENT": "0",
    "KEEP_CPU_RESIDENT": "0",
    "KEEP_GUARDRAIL_RESIDENT": "0",
    "IMAGE_RENDER_RAM_HEADROOM_MB": "4000",
    "COMFYUI_RECYCLE_AFTER_RENDER": "1",
    # Guardrail lane (server/config.py, server/features/judge.py)
    "LLAMA_BASE_GUARDRAIL": "http://localhost:8083",
    "GUARD_LLM_BASE": "http://localhost:8083",
    "GUARD_LLM_MAX_TOKENS": "2048",
    "GUARD_LLM_REASONING_BUDGET": "256",
    "GUARD_LLM_TIMEOUT": "240",
    "GUARD_FAIL_CLOSED": "1",
    # Embeddings (server/features/websearch/vector_store.py)
    "LOCAL_AI_EMBED_URL": "http://localhost:8084",
    "LOCAL_AI_EMBED_MODEL": "nomic-embed-text-v1.5.Q8_0",
    "LOCAL_AI_EMBED_DIM": "768",
    # Thermal pacing (server/features/monitoring.py)
    "THERMAL_PACE_START_C": "80",
    "THERMAL_PACE_SCALE": "2.0",
    "THERMAL_PACE_MAX_S": "20.0",
    # Auth basics (server/config.py; SSO secrets excluded)
    "AUTHENTIK_BASE_URL": "https://home.palashkantikundu.in/sso",
    "AUTH_CLIENT_ID": "local-ai",
    "AUTH_SCOPE": "openid profile email groups",
    # OpenAI lane (server/config.py)
    "OPENAI_SERVER_TOOLS": "auto",
    "OPENAI_LANE_MAX_SERVER_ROUNDS": "3",
    "OPENAI_LANE_SEARCH_REWRITE": "auto",
    # Self-chat behavior (server/config.py, self-chat.py)
    "SELF_CHAT_MODE": "cpu",
    "SELF_CHAT_EDITOR_MIN_CONFIDENCE": "70",
    "SELF_CHAT_EDITOR_RESTARTS": "2",
    "SELF_CHAT_REPLAY_ATTEMPTS": "2",
    "SELF_CHAT_RETRY_ATTEMPTS": "8",
    "SELF_CHAT_ALIGNMENT": "1",
    "SELF_CHAT_PIN_CAST": "0",
    "AGENT_PEER_MAP": '{"kaya": "kolpo", "kolpo": "kaya"}',
    # Search (server/config.py)
    "SEARXNG_URL": "http://127.0.0.1:8080/",
    "SEARXNG_PUBLIC_URL": "https://home.palashkantikundu.in/search",
    "WEB_SEARCH_MIN_INTERVAL": "20",
    # TTS (server/config.py)
    "TTS_MAX_CHARS": "8000",
    "TTS_MAX_CHARS_PUBLIC": "8000",
    "TTS_CHUNK_CHARS": "1800",
    "TTS_CACHE_MAX_BYTES": "1073741824",
    # Music (server/features/music/opus.py)
    "MUSIC_OPUS_BITRATE": "64000",
    # DDNS/heartbeat (server/config.py, scripts/connection_manager.py)
    "DDNS_DOMAIN": "palashkantikundu.in",
    "DDNS_SUBDOMAIN": "home",
    "DDNS_CHECK_INTERVAL": "300",
    "HEARTBEAT_URL": "http://10.66.66.1:9863/heartbeat",
    # Postgres (authentik-compose.yaml defaults)
    "POSTGRES_DB": "authentik",
    "POSTGRES_USER": "authentik",
}


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
    ap.add_argument("--fill-defaults", action="store_true",
                    help="write SAFE_DEFAULTS literals for table keys whose "
                         "live value is empty/missing (backup first)")
    args = ap.parse_args()

    errors, warnings = [], []

    if not os.path.isfile(args.env):
        print(f"ERROR: env file not found: {args.env} (copy .env.example to .env)")
        return 1

    if (args.sync or args.prune or args.fill_defaults) and os.path.isfile(args.example):
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
        if args.fill_defaults:
            # Re-read: --sync/--prune above may have changed the file.
            with open(args.env, encoding="utf-8") as f:
                lines = f.readlines()
            current = load_env(args.env)
            filled = [k for k, v in SAFE_DEFAULTS.items()
                      if current.get(k, "").strip() == ""]
            if filled:
                # server/dotenv.py is first-wins: an existing empty KEY= line
                # shadows anything appended later, so update empty lines in
                # place and append only keys absent from the file entirely.
                out = []
                done = set()
                for raw in lines:
                    stripped = raw.strip()
                    key = None
                    if stripped and not stripped.startswith("#"):
                        body = stripped[len("export "):].strip() if stripped.startswith("export ") else stripped
                        if "=" in body:
                            key = body.partition("=")[0].strip()
                    if key in filled and key not in done:
                        eol = "\n" if raw.endswith("\n") else ""
                        out.append(f"{key}={SAFE_DEFAULTS[key]}{eol}")
                        done.add(key)
                    else:
                        out.append(raw)
                still_missing = [k for k in filled if k not in done]
                if still_missing:
                    out.append(f"\n# ── defaults filled from code on {stamp} ──\n")
                    for k in still_missing:
                        out.append(f"{k}={SAFE_DEFAULTS[k]}\n")
                with open(args.env, "w", encoding="utf-8") as f:
                    f.writelines(out)
                for k in filled:
                    print(f"FILL: {k} set to code default")
            else:
                print("FILL: no empty table keys — nothing to fill")

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
