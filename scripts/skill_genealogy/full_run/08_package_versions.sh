#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/build_package_versions_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 4b only (steps 8-10), from the full lineage tables. Runs in the background;
# caffeinate keeps the Mac awake until the run ends.
nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.build_registry_lineages --packages-only --force > "$LOG" 2>&1 &
PID=$!
nohup caffeinate -i -w "$PID" > /dev/null 2>&1 &
echo "pid: $PID  follow with: tail -f $LOG"
