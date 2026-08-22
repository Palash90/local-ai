#!/usr/bin/env bash
# Restart the top-level local-ai services.
#
#   chat-webui          python3 ./chat-webui.py                    (port 3001)
#   markdown hosting    uvicorn markdown_hosting:app               (port 3002)
#   code hosting        python3 code_host.py .                     (port 9000)
#   request tracker     sudo python3 server/track_dashboard.py     (port 8093)
#
# Every service runs under nohup and appends to its own log in logs/.
# The tracker reads /var/log/nginx/track.log, so it runs under sudo —
# you may be prompted for your password once.
set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GIT_DIR="$(dirname "$REPO_DIR")"
LOG_DIR="$REPO_DIR/logs"
mkdir -p "$LOG_DIR"

stop_all() {
    local pattern="$1" name="$2"
    if pgrep -f "$pattern" >/dev/null 2>&1; then
        pkill -TERM -f "$pattern" 2>/dev/null
        for _ in $(seq 1 20); do
            pgrep -f "$pattern" >/dev/null 2>&1 || break
            sleep 0.5
        done
        pkill -KILL -f "$pattern" 2>/dev/null
        echo "  stopped: $name"
    else
        echo "  not running: $name"
    fi
}

start_one() {
    local name="$1" logfile="$2" dir="$3"
    shift 3
    (
        cd "$dir" || exit 1
        nohup "$@" >>"$logfile" 2>&1 &
        echo "  started: $name (pid $!, log $logfile)"
    )
}

echo "== Stopping =="
stop_all 'chat-webui\.py'      "chat web ui"
stop_all 'markdown_hosting'    "markdown hosting"
stop_all 'code_host\.py'       "code hosting"
stop_all 'track_dashboard'     "request tracker"

sudo wg-quick up wg0

# Tracker log must exist and be readable (same prep as local_cloud.sh).
sudo touch /var/log/nginx/track.log
sudo chmod 640 /var/log/nginx/track.log

echo "== Starting =="
start_one "chat web ui"     "$LOG_DIR/chat-webui.log"       "$REPO_DIR" python3 ./chat-webui.py
start_one "markdown hosting" "$LOG_DIR/markdown-hosting.log" "$REPO_DIR" python3 -m uvicorn markdown_hosting:app --host 127.0.0.1 --port 3002
start_one "code hosting"    "$LOG_DIR/code-host.log"        "$GIT_DIR"  python3 code_host.py .
start_one "request tracker" "$LOG_DIR/track-dashboard.log"  "$REPO_DIR" sudo python3 server/track_dashboard.py --port 8093 --log /var/log/nginx/track.log

sleep 3
echo "== Health =="
check() {
    local name="$1" url="$2"
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null)
    if [ "$code" != "000" ]; then
        echo "  UP   $name ($url -> HTTP $code)"
    else
        echo "  DOWN $name ($url) — check its log in $LOG_DIR/"
        [ "$name" = "request tracker" ] && \
            echo "       (tracker needs sudo; run this script from a terminal so the password can be prompted)"
    fi
}
check "chat web ui"      http://127.0.0.1:3001/
check "markdown hosting" http://127.0.0.1:3002/
check "code hosting"     http://127.0.0.1:9000/
check "request tracker"  http://127.0.0.1:8093/
