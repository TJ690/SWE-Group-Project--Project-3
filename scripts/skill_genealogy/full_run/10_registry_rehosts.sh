#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/find_copies_rehost_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 5b only (steps 4-9) on the full lineage tables. Existing outputs are
# skipped; add --force to rebuild the three stage 5b files (the signatures are
# never deleted by --only rehost). Runs in the background with caffeinate.
nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.find_copies --only rehost "$@" > "$LOG" 2>&1 &
PID=$!
nohup caffeinate -i -s -m -w "$PID" > /dev/null 2>&1 &
echo "pid: $PID  follow with: tail -f $LOG"
