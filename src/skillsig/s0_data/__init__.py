"""
STAGE: s0_data
MODULE: s0_data
PURPOSE: Load GitSkills views, check their schema, and write the dev-sample id list.
INPUTS: raw and generated Parquet under the paths in Config
OUTPUTS: src/data/derived/s0_devsample_ids.parquet
RQ LINK: RQ1 / RQ2 (population frame and the file_sha ids dev mode restricts to)
"""

from __future__ import annotations

from skillsig.s0_data.devsample import build_devsample
from skillsig.s0_data.loader import connect, run, snapshot_date
from skillsig.s0_data.schema_check import check_schema

__all__ = [
    "build_devsample",
    "check_schema",
    "connect",
    "run",
    "snapshot_date",
]
