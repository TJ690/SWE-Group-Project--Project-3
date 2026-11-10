#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/find_copies_copies_dev_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 5c only (steps 10-18) against the dev lineages. Needs the dev
# registry_signatures.npz and lineage_roots.parquet (09 and 10 scripts) and the
# full dataset signatures. Every dataset text is searched, so it takes a few
# minutes. Pass --force to rebuild the four stage 5c files.
"$ROOT/.venv/bin/python" -u -m skill_genealogy.find_copies --dev --only copies "$@" 2>&1 | tee "$LOG"
