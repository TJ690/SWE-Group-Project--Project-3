#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/assign_families_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 7a and 7b (steps 1-11) on the full tables. Existing outputs are skipped; add
# --force to rebuild the six stage 7 files. Runs in the background with
# caffeinate.
nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.assign_families "$@" > "$LOG" 2>&1 &
PID=$!
nohup caffeinate -i -s -m -w "$PID" > /dev/null 2>&1 &
echo "pid: $PID  follow with: tail -f $LOG"
