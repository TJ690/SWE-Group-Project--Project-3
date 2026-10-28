"""
STAGE: s2_features
MODULE: s2_features
PURPOSE: Feature stage entry for structure features and MinHash near-duplicate clusters.
INPUTS: generated skill_features.parquet; artifact text for dedup
OUTPUTS: src/data/derived/s2_structure.parquet and s2_clusters.parquet
RQ LINK: RQ1 (structure variables and near-duplicate clusters)
"""

from __future__ import annotations

from skillsig.s2_features.minhash_cluster import run as run_dedup
from skillsig.s2_features.structure import run, split_front_matter, structure_features

__all__ = [
    "run",
    "run_dedup",
    "split_front_matter",
    "structure_features",
]
