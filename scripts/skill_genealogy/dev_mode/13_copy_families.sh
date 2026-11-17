#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/assign_families_dev_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 7a and 7b (steps 1-11) on the dev tables: history links, strongest link,
# origin version, arrival groups, families, recall per text method. Needs the dev stage 5c tables and
# the dev copied-skill history (11 and 12 scripts). Families cover every
# English dataset text, so it takes several minutes. Pass --force to rebuild
# the six stage 7 files.
"$ROOT/.venv/bin/python" -u -m skill_genealogy.assign_families --dev "$@" 2>&1 | tee "$LOG"
