"""
STAGE: s1_registry
MODULE: mine
PURPOSE: Expose mine_git.run for `python -m skillsig registry --only mine`.
INPUTS: none (re-exports mine_git)
OUTPUTS: none
RQ LINK: RQ1 (registry version history) and RQ2 (commit trailers and release timing)
"""

from skillsig.s1_registry.mine_git import run

__all__ = ["run"]
