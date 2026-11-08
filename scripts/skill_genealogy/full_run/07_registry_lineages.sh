#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/build_registry_lineages_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Takes about two hours (language labels and version comparisons). Runs in the background.
nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.build_registry_lineages --force > "$LOG" 2>&1 &
echo "pid: $!  follow with: tail -f $LOG"
