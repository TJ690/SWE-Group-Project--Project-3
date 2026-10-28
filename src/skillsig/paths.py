"""
STAGE: package
MODULE: paths
PURPOSE: Resolve the repository root and stage output paths from Config.
INPUTS: Config.paths
OUTPUTS: none
RQ LINK: none (infrastructure)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skillsig.config import Config

STAGES = (
    "s0_intake",
    "s1_registry",
    "s2_features",
    "s3_labeling",
    "s4_validation",
    "s5_analysis",
    "s6_report",
)


def repo_root(start: Path | None = None) -> Path:
    """Nearest ancestor of `start` (default cwd) with pyproject.toml and config/, else checkout."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "config").is_dir():
            return candidate
    return Path(__file__).resolve().parents[2]


def resolve(cfg: Config, rel: Path) -> Path:
    """Absolute path for a config path; relative paths are anchored at cfg.root."""
    return rel if rel.is_absolute() else cfg.root / rel


def derived_path(cfg: Config, stage: str, name: str, suffix: str = ".parquet") -> Path:
    """`<derived>/<stage>_<name><suffix>`, e.g. derived_path(cfg, "s2_features", "wording")."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {', '.join(STAGES)}")
    return resolve(cfg, cfg.paths.derived) / f"{stage}_{name}{suffix}"
