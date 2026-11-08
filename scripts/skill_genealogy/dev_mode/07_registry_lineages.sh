#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

# Reads the full stage 3 tables, limited to the 30 dev registries (no dev history folder).
exec "$ROOT/.venv/bin/python" -u -m skill_genealogy.build_registry_lineages --dev --force
