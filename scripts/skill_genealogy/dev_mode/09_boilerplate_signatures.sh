#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/find_copies_signatures_dev_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 5a only (steps 1-3). --dev changes only registry_signatures.npz: the
# boilerplate lines and dataset signatures are shared with the full run and are
# built here the first time (more than an hour), then skipped. No --force, so a
# dev run never deletes the shared files. Runs in the background with caffeinate.
nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.find_copies --dev --only signatures > "$LOG" 2>&1 &
PID=$!
nohup caffeinate -i -s -m -w "$PID" > /dev/null 2>&1 &
echo "pid: $PID  follow with: tail -f $LOG"
