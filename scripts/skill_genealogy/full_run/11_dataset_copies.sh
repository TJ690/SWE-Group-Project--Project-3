#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/find_copies_copies_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 5c only (steps 10-18) on the full lineage tables. Existing outputs are
# skipped; add --force to rebuild the four stage 5c files (signatures and the
# stage 5b tables are never deleted by --only copies). Runs in the background
# with caffeinate.
nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.find_copies --only copies "$@" > "$LOG" 2>&1 &
PID=$!
nohup caffeinate -i -s -m -w "$PID" > /dev/null 2>&1 &
echo "pid: $PID  follow with: tail -f $LOG"
