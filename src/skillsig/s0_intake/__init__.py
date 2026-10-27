"""
STAGE: s0_intake
MODULE: s0_intake
PURPOSE: Load GitSkills views, check their schema, and write the dev-sample id list.
INPUTS: raw and generated Parquet under the paths in Config
OUTPUTS: src/data/derived/s0_devsample_ids.parquet
RQ LINK: RQ1 / RQ2 (population frame and the file_sha ids dev mode restricts to)
"""

from __future__ import annotations

from skillsig.s0_intake.devsample import build_devsample
from skillsig.s0_intake.loader import connect, run, snapshot_date
from skillsig.s0_intake.schema_check import check_schema

__all__ = [
    "build_devsample",
    "check_schema",
    "connect",
    "run",
    "snapshot_date",
]
