"""
STAGE: s6_report
MODULE: s6_report
PURPOSE: Report stage entry: `skillsig export` draws the charts of docs/report/FINDINGS.md.
INPUTS: finished outputs of s0-s4 (see figures.FIGURES)
OUTPUTS: figures/report/*.png
RQ LINK: RQ1 (presentation of the findings)
"""

from __future__ import annotations

from skillsig.s6_report.figures import run

__all__ = ["run"]
