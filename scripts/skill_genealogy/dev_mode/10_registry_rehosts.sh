#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/find_copies_rehost_dev_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 5b only (steps 4-9) on the dev lineages. Needs the dev
# registry_signatures.npz (09_boilerplate_signatures.sh). Takes a few seconds.
# Pass --force to rebuild the three stage 5b files.
"$ROOT/.venv/bin/python" -u -m skill_genealogy.find_copies --dev --only rehost "$@" 2>&1 | tee "$LOG"
