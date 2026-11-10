#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs/skill_genealogy
LOG="$ROOT/logs/skill_genealogy/download_copied_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 6 on the full copy table (60,958 repos). Takes many hours and stops
# without --yes above 2,000 pending repos, so this script passes --yes.
# Resumes after a stop: finished repos are skipped.
nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.download_copied_skill_history --yes "$@" > "$LOG" 2>&1 &
PID=$!
nohup caffeinate -i -s -m -w "$PID" > /dev/null 2>&1 &
echo "pid: $PID  follow with: tail -f $LOG"
