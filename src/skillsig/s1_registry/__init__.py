"""
STAGE: s1_registry
MODULE: s1_registry
PURPOSE: Registry stage entry for loading the source list of skill repositories.
INPUTS: config/registry_sources.yaml
OUTPUTS: none
RQ LINK: RQ1 (templated copies from registry repos) and RQ2 (registries tracked for updates)
"""

from __future__ import annotations

from skillsig.s1_registry.sources import Source, clone_dir, load_sources

__all__ = [
    "Source",
    "clone_dir",
    "load_sources",
]
