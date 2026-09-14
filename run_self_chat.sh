#!/usr/bin/env bash
# Launch self-chat (Kaya/Kolpo story pipeline) detached in the background.
#
# The pipeline asks "Keep sessions {y/n}" on stdin, which kills a naive
# background launch with EOFError — the answer is piped in instead.
# Usage: ./run_self_chat.sh [y|n]   (default: n)
set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$HOME/local-ai-files/self-chat.log"

ANSWER="${1:-n}"
if [ "$ANSWER" != "y" ] && [ "$ANSWER" != "n" ]; then
    echo "Usage: $0 [y|n]" >&2
    exit 1
fi

if pgrep -f 'self-chat\.py' >/dev/null 2>&1; then
    echo "self-chat already running (pid $(pgrep -f 'self-chat\.py' | head -1)) — not starting another"
    exit 1
fi

mkdir -p "$(dirname "$LOG_FILE")"
printf '%s\n' "$ANSWER" | setsid nohup python3 -u "$REPO_DIR/self-chat.py" >>"$LOG_FILE" 2>&1 &
disown
sleep 5
if pgrep -f 'self-chat\.py' >/dev/null 2>&1; then
    echo "started self-chat (pid $(pgrep -f 'self-chat\.py' | head -1), keep_sessions=$ANSWER, log $LOG_FILE)"
else
    echo "FAILED to start — check $LOG_FILE"
    exit 1
fi
