"""Registry union coverage on the full GitSkills population."""

from __future__ import annotations

import pandas as pd
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s1_registry.match import run
from skillsig.s1_registry.sources import load_sources

_BLOBS = 6_719
_FILES = 216_440
_PDF_FILES = 1_409
_TIERS = {"A": (140, 23_686), "B": (5_047, 155_526), "G": (1_535, 38_631)}
_LINEAGE_COLUMNS = [
    "lineage_id",
    "versions",
    "versions_seen",
    "copies",
    "owners",
    "first_date",
    "last_date",
    "span_days",
]


@pytest.mark.data
def test_registry_coverage(cfg: Config) -> None:
    result = run(cfg)
    matches = pd.read_parquet(result.outputs[0])
    lineages = pd.read_parquet(result.outputs[1])
    assert result.rows["s1_registry_matches"] == _BLOBS
    assert matches["blob"].nunique() == _BLOBS
    assert len(matches) == _BLOBS
    assert int(matches["files"].sum()) == _FILES
    assert _tier_totals(cfg, matches) == _TIERS
    assert _pdf_files(cfg, matches) == _PDF_FILES
    assert list(lineages.columns) == _LINEAGE_COLUMNS
    assert lineages["lineage_id"].is_unique
    assert len(lineages) > 0


def _pdf_files(cfg: Config, matches: pd.DataFrame) -> int:
    versions = pd.read_parquet(resolve(cfg, cfg.paths.derived) / "s1_registry_versions.parquet")
    pdf = versions.loc[
        versions["repo"].eq("anthropics/skills")
        & versions["skill"].eq("pdf")
        & versions["first_date"].astype(str).str.startswith("2025-10"),
        "blob",
    ]
    assert len(pdf) == 1
    row = matches.loc[matches["blob"].eq(pdf.iloc[0])]
    assert len(row) == 1
    return int(row.iloc[0]["files"])


def _tier_totals(cfg: Config, matches: pd.DataFrame) -> dict[str, tuple[int, int]]:
    """Count a blob in every tier that holds it. The union counts that blob once."""
    versions = pd.read_parquet(resolve(cfg, cfg.paths.derived) / "s1_registry_versions.parquet")
    tier_of = {source.repo: source.tier for source in load_sources(cfg) if source.mirror_of is None}
    held = versions.loc[versions["repo"].isin(tier_of), ["blob", "repo"]].copy()
    held["tier"] = held["repo"].map(tier_of)
    present = held.loc[held["blob"].isin(set(matches["blob"])), ["blob", "tier"]].drop_duplicates()
    files = matches.set_index("blob")["files"]
    totals: dict[str, tuple[int, int]] = {}
    for tier, group in present.groupby("tier", sort=True):
        blobs = group["blob"].drop_duplicates()
        totals[str(tier)] = (int(blobs.nunique()), int(files.loc[blobs].sum()))
    return totals
