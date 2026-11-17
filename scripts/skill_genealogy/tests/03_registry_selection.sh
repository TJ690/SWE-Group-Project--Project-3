#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

exec "$ROOT/.venv/bin/python" -m pytest -q \
  tests/skill_genealogy/test_select_registries.py
