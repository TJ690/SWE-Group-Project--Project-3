#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

# Stage 4b lives in build_registry_lineages.py: its tests are in the same file as stage 4a.
exec "$ROOT/.venv/bin/python" -m pytest -q \
  tests/skill_genealogy/test_build_registry_lineages.py \
  --doctest-modules src/skill_genealogy/build_registry_lineages.py
