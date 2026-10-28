"""
STAGE: s4_validation
MODULE: spot_refresh
PURPOSE: `skillsig sample --only spot_refresh`: keep the E6 sample, refresh its key and sheets.
INPUTS: results/annotation/key.parquet, s3_labels_v2_1, s3_labels_v2, s3_baseline_labels, artifacts
OUTPUTS: results/annotation/key.parquet; spot_A.xlsx, spot_B.xlsx and codebook.md when still blank
RQ LINK: RQ1 (E6 precision is scored against the current labels without redrawing the sample)
"""

from __future__ import annotations

from skillsig.s4_validation.spot_check import run_refresh as run

__all__ = ["run"]
