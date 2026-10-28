#!/bin/bash
# Double-click in Finder: full forced rebuild of the skillsig pipeline: all tables and report figures.
# Keeps the Mac awake while it runs. Progress: logs/run_full_<timestamp>.log
cd "$(dirname "$0")" || exit 1
caffeinate -i bash scripts/run_full.sh
echo
echo "Finished (exit $?). You can close this window."
