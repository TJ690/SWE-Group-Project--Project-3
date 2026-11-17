#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs/skill_genealogy
LOG="$ROOT/logs/skill_genealogy/select_registries_languages_$(date +%Y%m%d_%H%M).log"

nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.select_registries --force-languages > "$LOG" 2>&1 &
echo "started pid $! log $LOG"
