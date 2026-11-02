#!/usr/bin/env bash
# Serve the dev family-tree viewer at http://127.0.0.1:8000/
# Open that URL. Opening index.html as a file blocks families_index.json.
set -euo pipefail

cd "$(dirname "$0")/../../.."
exec .venv/bin/python -m http.server 8000 -d results/skill_genealogy_dev/tree_viewer
