#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/measure_copy_changes_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 9a: each copy against its origin, history edits, change profile. Needs the
# stage 7 tables of the same mode. Pass --force to rebuild the three outputs.
nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.measure_copy_changes --only origin "$@" > "$LOG" 2>&1 &
PID=$!
nohup caffeinate -i -s -m -w "$PID" > /dev/null 2>&1 &
echo "pid: $PID  follow with: tail -f $LOG"
