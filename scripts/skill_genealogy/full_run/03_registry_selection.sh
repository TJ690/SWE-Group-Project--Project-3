#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/select_registries.log"

nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.select_registries --force > "$LOG" 2>&1 &
echo "started pid $! log $LOG"
echo "follow: tail -f $LOG"
