#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/assign_families_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 7a (steps 1-10) on the full tables. Existing outputs are skipped; add
# --force to rebuild the four stage 7a files. Runs in the background with
# caffeinate.
nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.assign_families "$@" > "$LOG" 2>&1 &
PID=$!
nohup caffeinate -i -s -m -w "$PID" > /dev/null 2>&1 &
echo "pid: $PID  follow with: tail -f $LOG"
