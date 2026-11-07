#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/download_registry_readmes.log"

nohup "$ROOT/.venv/bin/python" -u -m skill_genealogy.download_registry_readmes > "$LOG" 2>&1 &
echo "started pid $! log $LOG"
echo "follow: tail -f $LOG"
