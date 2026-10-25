"""Exact Sprint 1 label counts on the full GitSkills population."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from skillsig.config import Config
from skillsig.s3_labeling.rules_v1 import LABELS, run

_EXPECTED = {
    "highly_templated": 35_768,
    "agent_assisted": 80_114,
    "likely_human": 62_798,
    "insufficient_evidence": 1_699_301,
}


@pytest.mark.data
def test_rules_v1_counts(cfg: Config) -> None:
    result = run(cfg)
    assert sum(_EXPECTED.values()) == 1_877_981
    assert result.rows == {"s3_labels_v1": 1_877_981}
    counts, distinct, empty, unexpected = _summary(result.outputs[0])
    assert counts == _EXPECTED
    assert set(counts) == set(LABELS)
    assert distinct == 1_877_981
    assert empty == 0
    assert unexpected == 0


def _summary(path: Path) -> tuple[dict[str, int], int, int, int]:
    con = duckdb.connect()
    try:
        grouped = con.execute(
            """
            SELECT label, count(*)::INTEGER
            FROM read_parquet(?)
            GROUP BY 1
            ORDER BY 1
            """,
            [path.as_posix()],
        ).fetchall()
        distinct, empty, unexpected = con.execute(
            """
            SELECT
                count(DISTINCT file_sha)::INTEGER,
                count(*) FILTER (WHERE reasons IS NULL OR len(reasons) = 0)::INTEGER,
                count(*) FILTER (
                    WHERE label NOT IN (
                        'highly_templated', 'agent_assisted',
                        'likely_human', 'insufficient_evidence'
                    )
                )::INTEGER
            FROM read_parquet(?)
            """,
            [path.as_posix()],
        ).fetchone()
    finally:
        con.close()
    return (
        {label: int(n) for label, n in grouped},
        int(distinct),
        int(empty),
        int(unexpected),
    )
