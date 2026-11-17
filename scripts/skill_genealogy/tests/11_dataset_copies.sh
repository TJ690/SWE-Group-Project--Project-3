#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

exec "$ROOT/.venv/bin/python" -m pytest -q \
  tests/skill_genealogy/test_find_copies.py \
  --doctest-modules src/skill_genealogy/find_copies.py
