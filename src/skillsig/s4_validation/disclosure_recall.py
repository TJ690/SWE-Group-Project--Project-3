"""
STAGE: s4_validation
MODULE: disclosure_recall
PURPOSE: E1 - share of AI-trailer disclosures visible from the first or last commit alone.
INPUTS: s1_versions_raw.parquet, s1_commit_trailers.parquet, config/registry_sources.yaml
OUTPUTS: results/s4_disclosure_recall.csv, results/s4_disclosure_recall.json
RQ LINK: RQ1 (labelling limitation: how much tier-A disclosure the dataset's view misses)

The dataset stores only the first and last commit message of each SKILL.md, so a trailer on a
middle commit is invisible to the labels. The registry clones keep every commit, so for their
skills the miss rate can be measured. Registry repos are popular collections, not a random sample.
"""

from __future__ import annotations

import logging

import duckdb
import pandas as pd

from skillsig.config import Config
from skillsig.provenance import StageResult
from skillsig.s1_registry.sources import load_sources
from skillsig.s4_validation import _io
from skillsig.s4_validation.stats import wilson_interval

log = logging.getLogger(__name__)

_MINE = "python -m skillsig registry --only mine_git"
_NOTE = "registry repos are popular collections, not a random sample"
_COLUMNS = [
    "scope",
    "skills",
    "skills_3plus_commits",
    "any_ai",
    "visible_first_or_last",
    "middle_only",
    "recall",
    "ci_low",
    "ci_high",
]


def skill_histories(versions: pd.DataFrame, trailers: pd.DataFrame) -> pd.DataFrame:
    """One row per (repo, path): n_commits, any_ai, first_ai, last_ai, middle_only."""
    flags = trailers[["repo", "commit", "has_ai_trailer"]].drop_duplicates(["repo", "commit"])
    merged = versions[["repo", "commit", "commit_date", "path"]].drop_duplicates(
        ["repo", "path", "commit"]
    )
    merged = merged.merge(flags, on=["repo", "commit"], how="left")
    merged["ai"] = merged["has_ai_trailer"].fillna(False).astype(bool)
    merged = merged.sort_values(["repo", "path", "commit_date", "commit"], kind="mergesort")
    grouped = merged.groupby(["repo", "path"], sort=True)
    out = pd.DataFrame(
        {
            "n_commits": grouped.size(),
            "any_ai": grouped["ai"].any(),
            "first_ai": grouped["ai"].first(),
            "last_ai": grouped["ai"].last(),
        }
    ).reset_index()
    out["middle_only"] = out["any_ai"] & ~(out["first_ai"] | out["last_ai"])
    return out


def summarise(histories: pd.DataFrame, groups: dict[str, str] | None = None) -> pd.DataFrame:
    """Rows: all, all_3plus_commits, and one per registry group when `groups` maps repo -> group."""
    frame = histories.copy()
    lookup = groups or {}
    frame["group"] = [lookup.get(repo, "other") for repo in frame["repo"]]
    rows = [_row("all", frame), _row("all_3plus_commits", frame[frame["n_commits"] >= 3])]
    if groups:
        for name, part in frame.groupby("group", sort=True):
            rows.append(_row(f"group:{name}", part))
    return pd.DataFrame(rows, columns=_COLUMNS)


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write the E1 recall table and summary. `dev` has no effect (registry data is small)."""
    versions_path = _io.derived(cfg, "s1_versions_raw.parquet")
    trailers_path = _io.derived(cfg, "s1_commit_trailers.parquet")
    _io.require([(versions_path, _MINE), (trailers_path, _MINE)])
    outputs = [
        _io.results(cfg, "s4_disclosure_recall.csv"),
        _io.results(cfg, "s4_disclosure_recall.json"),
    ]
    inputs = [versions_path, trailers_path]
    if not force and _io.up_to_date(outputs, cfg, inputs):
        return StageResult("s4_validation", outputs, skipped=True, rows=_io.saved_rows(outputs))
    con = duckdb.connect()
    try:
        versions = con.execute(f"SELECT * FROM read_parquet({_io.sql(versions_path)})").fetchdf()
        trailers = con.execute(f"SELECT * FROM read_parquet({_io.sql(trailers_path)})").fetchdf()
    finally:
        con.close()
    try:
        groups = {s.repo: s.group for s in load_sources(cfg, include_candidates=True)}
    except FileNotFoundError:
        log.warning("registry_sources.yaml not found; reporting overall rows only")
        groups = {}
    table = summarise(skill_histories(versions, trailers), groups)
    rows = _io.write_csv(table, outputs[0], cfg, inputs)
    overall = table.iloc[0].to_dict()
    _io.write_json({**overall, "note": _NOTE}, outputs[1], cfg, inputs)
    log.info(
        "E1: %s of %s disclosed skills visible on first/last commit (recall %.3f)",
        overall["visible_first_or_last"],
        overall["any_ai"],
        overall["recall"],
    )
    return StageResult("s4_validation", outputs, rows={"s4_disclosure_recall": rows})


def _row(scope: str, part: pd.DataFrame) -> dict[str, object]:
    any_ai = int(part["any_ai"].sum())
    visible = int((part["first_ai"] | part["last_ai"]).sum())
    low, high = wilson_interval(visible, any_ai)
    return {
        "scope": scope,
        "skills": len(part),
        "skills_3plus_commits": int((part["n_commits"] >= 3).sum()),
        "any_ai": any_ai,
        "visible_first_or_last": visible,
        "middle_only": int(part["middle_only"].sum()),
        "recall": visible / any_ai if any_ai else float("nan"),
        "ci_low": low,
        "ci_high": high,
    }
