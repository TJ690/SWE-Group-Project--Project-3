#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/build_family_trees_dev_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 8: one tree per family. Needs the stage 7 tables of the same mode.
# Pass --force to rebuild family_tree.parquet and family_summary.parquet.
"$ROOT/.venv/bin/python" -u -m skill_genealogy.build_family_trees --dev "$@" 2>&1 | tee "$LOG"
