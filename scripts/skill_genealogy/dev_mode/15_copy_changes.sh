#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/measure_copy_changes_dev_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 9a and 9b: each copy against its origin, history edits, change profile, and every
# copy version against four comparison texts. Needs the
# stage 7 tables of the same mode. Each part skips when its outputs exist; pass
# --only origin|versions to run one part, --force to rebuild.
"$ROOT/.venv/bin/python" -u -m skill_genealogy.measure_copy_changes --dev "$@" 2>&1 | tee "$LOG"
