#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/find_copies_signatures_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 5a only (steps 1-3) on the full lineage tables. Existing outputs are
# skipped, so a stopped run resumes at the next dataset chunk. Add --force to
# rebuild everything (it deletes the shared boilerplate list and dataset parts).
# Runs in the background; caffeinate keeps the Mac awake until the run ends.
nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.find_copies --only signatures "$@" > "$LOG" 2>&1 &
PID=$!
nohup caffeinate -i -s -m -w "$PID" > /dev/null 2>&1 &
echo "pid: $PID  follow with: tail -f $LOG"
