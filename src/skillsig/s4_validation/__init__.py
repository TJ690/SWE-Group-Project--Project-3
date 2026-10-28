"""
STAGE: s4_validation
MODULE: s4_validation
PURPOSE: Evaluation stage entry: `skillsig sample` draws the E6 spot-check; `skillsig evaluate`
         runs E1, E2, E3, E5 and scores E6 when the annotated sheets are back.
INPUTS: labels, features and registry tables (see each module)
OUTPUTS: results/s4_*.csv|json, results/annotation/*, figures/12_edit_robustness.png
RQ LINK: RQ1 (known-answer evaluation design plus a small manual spot-check)

E4 (disclosed hold-out for the text signal) lives in s3_labeling.style_model.
"""

from __future__ import annotations

import logging

from skillsig.config import Config
from skillsig.provenance import StageResult
from skillsig.s4_validation import (
    disclosure_recall,
    edit_robustness,
    spot_check,
    stability,
    template_provenance,
)

log = logging.getLogger(__name__)

EVALUATIONS = (disclosure_recall, template_provenance, edit_robustness, stability)


def run_sample(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Draw the E6 spot-check sheets (never overwrites sheets that already exist)."""
    return spot_check.run_draw(cfg, dev=dev, force=force)


def run_evaluate(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """E1, E2, E3, E5, then E6 scoring if spot_A_done.xlsx (or .csv) exists."""
    outputs, rows = [], {}
    for module in EVALUATIONS:
        result = module.run(cfg, dev=dev, force=force)
        outputs += result.outputs
        rows.update(result.rows)
    if spot_check.done_path(cfg, "A") is not None:
        result = spot_check.run_score(cfg, dev=dev, force=force)
        outputs += result.outputs
        rows.update(result.rows)
    else:
        log.warning("E6 not scored: results/annotation/spot_A_done.xlsx is not there yet")
    return StageResult("s4_validation", outputs, rows=rows)


run = run_sample

__all__ = ["run", "run_evaluate", "run_sample"]
