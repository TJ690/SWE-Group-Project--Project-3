#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/measure_staleness_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 10: how far behind its registry skill each copy is (text and package),
# its state and its sync events. Needs stages 4, 6, 7 and 9 of the same mode.
# Skips when its outputs exist; pass --force to rebuild, --stale-days N to change
# the stale cutoff (default STALE_AFTER_DAYS).
nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.measure_staleness "$@" > "$LOG" 2>&1 &
PID=$!
nohup caffeinate -i -s -m -w "$PID" > /dev/null 2>&1 &
echo "pid: $PID  follow with: tail -f $LOG"
