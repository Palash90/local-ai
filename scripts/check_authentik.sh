#!/usr/bin/env bash
# Authentik health probe (read-only): compose services, JWKS, outpost.
# Exit 0 healthy, 1 any failure. Safe to run against the live SSO stack.
set -u
FAIL=0
say() { printf '%s\n' "$*"; }
bad() { say "FAIL: $*"; FAIL=1; }
ok() { say "ok: $*"; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 1. compose services healthy
if command -v docker >/dev/null 2>&1; then
  UNHEALTHY=$(docker ps --filter "name=authentik" --format '{{.Names}} {{.Status}}' 2>/dev/null \
    | grep -v "(healthy)" || true)
  if [ -z "$(docker ps --filter "name=authentik" -q 2>/dev/null)" ]; then
    bad "no authentik containers running"
  elif [ -n "$UNHEALTHY" ]; then
    bad "unhealthy authentik containers: $UNHEALTHY"
  else
    ok "authentik containers healthy"
  fi
else
  bad "docker not available"
fi

# 2. JWKS reachable (same URL family the backends verify against)
BASE_URL="${AUTHENTIK_BASE_URL:-https://home.palashkantikundu.in/sso}"
for url in \
  "$BASE_URL/application/o/token/" \
; do
  # token endpoint should answer 405/400 (exists), not 404/5xx
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "$url" || echo 000)
  case "$code" in
    400|401|405) ok "SSO endpoint alive ($url -> $code)" ;;
    *) bad "SSO endpoint unexpected ($url -> $code)" ;;
  esac
done

# 3. local outpost proxy port (nginx auth_request path dependency)
if (echo > /dev/tcp/127.0.0.1/9010) 2>/dev/null; then
  ok "outpost proxy port 9010 reachable"
else
  bad "outpost proxy port 9010 refused"
fi

if [ "$FAIL" -eq 0 ]; then say "check_authentik: HEALTHY"; else say "check_authentik: UNHEALTHY"; fi
exit "$FAIL"
