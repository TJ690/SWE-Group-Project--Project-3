"""Bulk-install flag on the full GitSkills population matches the Sprint 1 notebook rule."""

from __future__ import annotations

import duckdb
import pytest

from skillsig.config import Config
from skillsig.paths import resolve

_BULK_SKILLS = 207_368
_BULK_COMMITS = 11_529
_WITH_HISTORY = 458_137


@pytest.mark.data
def test_bulk_flag_matches_notebook_rule(cfg: Config) -> None:
    raw = resolve(cfg, cfg.paths.raw) / "artifacts"
    derived = resolve(cfg, cfg.paths.derived) / "s2_commit.parquet"
    bulk_min = int(cfg.thresholds.bulk_batch_min)
    con = duckdb.connect()
    try:
        # The EDA_authorship notebook groups representatives with history by
        # (repo_full_name, first_commit_at); the pipeline flag must agree.
        notebook = con.execute(
            f"""
            SELECT sum(n) FILTER (WHERE n >= {bulk_min}),
                   count(*) FILTER (WHERE n >= {bulk_min})
            FROM (
                SELECT repo_full_name, first_commit_at, count(*) AS n
                FROM read_parquet('{raw.as_posix()}/**/*.parquet')
                WHERE dedup_primary = 1 AND history_fetched = 1
                GROUP BY 1, 2
            )
            """
        ).fetchone()
        pipeline = con.execute(
            "SELECT count(*) FILTER (WHERE is_bulk), count(*) FILTER (WHERE history_fetched) "
            "FROM read_parquet(?)",
            [derived.as_posix()],
        ).fetchone()
    finally:
        con.close()
    assert notebook == (_BULK_SKILLS, _BULK_COMMITS)
    assert pipeline == (_BULK_SKILLS, _WITH_HISTORY)
