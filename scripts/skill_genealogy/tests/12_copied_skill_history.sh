#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

exec "$ROOT/.venv/bin/python" -m pytest -q \
  tests/skill_genealogy/test_download_copied_skill_history.py \
  --doctest-modules src/skill_genealogy/download_copied_skill_history.py
