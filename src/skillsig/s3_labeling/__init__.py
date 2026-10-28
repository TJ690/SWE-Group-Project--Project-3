"""
STAGE: s3_labeling
MODULE: s3_labeling
PURPOSE: Label stage entry for Sprint 1 authorship-signal rules.
INPUTS: s2_commit.parquet and artifact rows (copy counts, authors, messages)
OUTPUTS: src/data/derived/s3_labels_v1.parquet
RQ LINK: RQ1 (heuristic authorship label)
"""

from __future__ import annotations

from skillsig.s3_labeling.rules_v1 import LABELS, label_v1, run

__all__ = ["LABELS", "label_v1", "run"]
