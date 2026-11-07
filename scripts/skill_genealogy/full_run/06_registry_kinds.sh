#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

mkdir -p logs
LOG="$ROOT/logs/label_registry_kinds.log"

"$ROOT/.venv/bin/python" -u -m skill_genealogy.label_registry_kinds --force 2>&1 | tee "$LOG"
