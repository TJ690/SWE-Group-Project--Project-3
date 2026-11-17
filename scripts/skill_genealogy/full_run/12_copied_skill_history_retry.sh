#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

# Clone the repos whose status is timeout or error again (full data).
# caffeinate keeps the Mac awake until the command ends.
exec caffeinate -i -s -m "$ROOT/.venv/bin/python" -u -m skill_genealogy.download_copied_skill_history --yes --retry-failed "$@"
