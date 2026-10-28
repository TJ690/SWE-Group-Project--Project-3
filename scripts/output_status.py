"""
STAGE: tooling
MODULE: output_status
PURPOSE: Print whether stage outputs exist and which mode (full or dev) built them, from their manifests.
INPUTS: output paths given on the command line (globs allowed), their <output>.manifest.json files
OUTPUTS: one line per output on stdout
RQ LINK: none (infrastructure, used by the agent skills in .claude/skills/)

    python scripts/output_status.py src/data/derived/s2_commit.parquet results/s4_*.csv
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path


def describe(output: Path) -> str:
    if not output.exists():
        return f"MISSING  {output}"
    manifest = output.with_name(output.name + ".manifest.json")
    if not manifest.exists():
        return f"EXISTS   {output}  (no manifest)"
    try:
        m = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"EXISTS   {output}  (unreadable manifest)"
    # A dev run of a module that filters by the sample lists the sample ids among its inputs.
    # Modules that only read earlier outputs do not, so their mode shows as unrecorded.
    dev = any("s0_devsample_ids" in name for name in m.get("inputs", {}))
    commit = (m.get("git_commit") or "?")[:8]
    return (
        f"EXISTS   {output}  mode={'dev' if dev else 'full/unrecorded'}  rows={m.get('rows')}"
        f"  built={m.get('created_utc')}  commit={commit}"
    )


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    missing = False
    for arg in argv:
        matches = sorted(glob.glob(arg)) or [arg]
        for path in matches:
            line = describe(Path(path))
            missing |= line.startswith("MISSING")
            print(line)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
