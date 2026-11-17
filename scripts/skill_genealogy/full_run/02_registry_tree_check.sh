#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/check_registry_trees.log"

nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.check_registry_trees > "$LOG" 2>&1 &
echo "started pid $! log $LOG"
echo "follow: tail -f $LOG"
