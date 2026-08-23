#!/usr/bin/env bash
# Restart the top-level local-ai services.
#
#   chat-webui          python3 ./chat-webui.py                    (port 3001)
#   markdown hosting    uvicorn markdown_hosting:app               (port 3002)
#   code hosting        python3 code_host.py .                     (port 9000)
#   nextcloud           restart cloud-app + occ files:scan --all
#
# Every service runs under nohup and appends to its own log in logs/.
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

sudo wg-quick up wg0

echo "== Starting =="
start_one "chat web ui"     "$LOG_DIR/chat-webui.log"       "$REPO_DIR" python3 ./chat-webui.py
start_one "markdown hosting" "$LOG_DIR/markdown-hosting.log" "$REPO_DIR" python3 -m uvicorn markdown_hosting:app --host 127.0.0.1 --port 3002
start_one "code hosting"    "$LOG_DIR/code-host.log"        "$GIT_DIR"  python3 code_host.py .

echo "== Nextcloud =="
BACKUP_MNT="/mnt/wwn-0x50014ee2173893e0-part1"
if ! findmnt "$BACKUP_MNT" >/dev/null 2>&1; then
    echo "  backup disk not mounted, mounting $BACKUP_MNT"
    sudo mount "$BACKUP_MNT"
fi
if findmnt "$BACKUP_MNT" >/dev/null 2>&1 \
   && docker ps --format '{{.Names}}' | grep -qx cloud-app; then
    # Restart so the bind mount re-resolves to the live directory
    docker compose -f "$REPO_DIR/docker-compose.yaml" restart cloud-app
    sleep 5
    if docker exec cloud-app ls /mnt/my_backups >/dev/null 2>&1; then
        echo "  external storage visible in container, scanning files"
        docker exec -u www-data cloud-app php occ files:scan --all
    else
        echo "  WARNING: /mnt/my_backups still empty inside cloud-app — check $BACKUP_MNT"
    fi
else
    echo "  skipped: nextcloud (disk not mounted or cloud-app not running)"
fi

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
    fi
}
check "chat web ui"      http://127.0.0.1:3001/
check "markdown hosting" http://127.0.0.1:3002/
check "code hosting"     http://127.0.0.1:9000/
