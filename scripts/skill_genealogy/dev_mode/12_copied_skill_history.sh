#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs/skill_genealogy
LOG="$ROOT/logs/skill_genealogy/download_copied_dev_$(date +%Y%m%d_%H%M).log"
echo "log: $LOG"

# Stage 6 on the dev copy table (about 4,000 repos; needs the dev
# copy_candidates.parquet from 11_dataset_copies.sh). Clones from GitHub, so it
# runs in the background. A second run resumes: finished repos are skipped.
# Pass --limit 5 for a quick check, --retry-failed to clone timeout/error again.
nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.download_copied_skill_history --dev "$@" > "$LOG" 2>&1 &
PID=$!
nohup caffeinate -i -s -m -w "$PID" > /dev/null 2>&1 &
echo "pid: $PID  follow with: tail -f $LOG"
