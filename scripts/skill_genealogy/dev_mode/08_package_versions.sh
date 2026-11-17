#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

# Stage 4b only (steps 8-10), from the dev lineage tables of 07_registry_lineages.sh.
# Reads the full stage 3 changes table, limited to the dev lineages. A few seconds.
exec "$ROOT/.venv/bin/python" -u -m skill_genealogy.build_registry_lineages --dev --packages-only --force
