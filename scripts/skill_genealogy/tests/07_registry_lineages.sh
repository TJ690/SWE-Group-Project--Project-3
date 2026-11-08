#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

exec "$ROOT/.venv/bin/python" -m pytest -q \
  tests/skill_genealogy/test_build_registry_lineages.py \
  tests/skill_genealogy/test_compare_texts.py \
  tests/skill_genealogy/test_compare_folders.py \
  --doctest-modules src/skill_genealogy/build_registry_lineages.py src/skill_genealogy/compare_texts.py
