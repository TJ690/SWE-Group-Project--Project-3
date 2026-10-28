from __future__ import annotations

from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s0_intake.loader import connect, snapshot_date
from skillsig.s0_intake.schema_check import VIEW_COLUMNS, check_schema

_EXTRA_COLUMNS: dict[str, tuple[str, ...]] = {
    "artifact_siblings": ("repo_full_name", "artifact_path", "entry_name"),
    "sibling_files": ("repo_full_name", "artifact_path", "entry_name"),
}

_DICTIONARY_COUNTS: tuple[tuple[str, int], ...] = (
    ("artifacts", 3_797_117),
    ("artifact_siblings", 7_264_865),
    ("repos", 282_200),
    ("mining_runs", 7),
    ("skill_features", 1_840_872),
    ("file_agents", 3_797_117),
)


def write_views(cfg: Config, *, n: int = 20, omit: dict[str, str] | None = None) -> None:
    omit = omit or {}
    columns = {**VIEW_COLUMNS, **_EXTRA_COLUMNS}
    raw = resolve(cfg, cfg.paths.raw)
    generated = resolve(cfg, cfg.paths.generated)
    folders = {
        "artifacts": raw / "artifacts" / "part-00000.parquet",
        "repos": raw / "repos" / "part-00000.parquet",
        "artifact_siblings": raw / "artifact_siblings" / "part-00000.parquet",
        "mining_runs": raw / "mining_runs" / "part-00000.parquet",
    }
    for view, names in columns.items():
        dropped = omit.get(view)
        kept = [name for name in names if name != dropped]
        frame = {name: _values(name, n) for name in kept}
        path = folders.get(view, generated / f"{view}.parquet")
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table({key: pa.array(values) for key, values in frame.items()}),
            path,
            compression="zstd",
        )


def _values(name: str, n: int) -> list[object]:
    if name == "file_sha":
        return [f"{i:040x}" for i in range(n)]
    if name == "finished_at":
        return ["2026-07-01T00:00:00+00:00"] * (n - 1) + ["2026-07-20T22:26:56+00:00"]
    if name in {"history_fetched", "dedup_primary"}:
        return [1] * n
    return [f"{name}-{i}" for i in range(n)]


def test_missing_column_is_named(tmp_cfg: Config) -> None:
    write_views(tmp_cfg, omit={"artifacts": "body_chars"})
    con = connect(tmp_cfg)
    try:
        problems = check_schema(con)
    finally:
        con.close()
    assert "artifacts missing column body_chars" in problems


def test_dev_skips_count_checks(tmp_cfg: Config) -> None:
    write_views(tmp_cfg, n=20)
    con = connect(tmp_cfg)
    try:
        assert check_schema(con, dev=True) == []
        problems = check_schema(con, dev=False)
    finally:
        con.close()
    assert problems
    assert all("row count" in problem for problem in problems)
    assert any("3797117" in problem for problem in problems)


@pytest.mark.data
def test_full_counts_match_data_dictionary(cfg: Config) -> None:
    con = connect(cfg)
    try:
        assert check_schema(con) == []
        assert snapshot_date(con) == date(2026, 7, 20)
        primary = con.execute(
            "SELECT count(*) FROM artifacts WHERE dedup_primary = 1"
        ).fetchone()[0]
        assert int(primary) == 1_877_981
        for view, expected in _DICTIONARY_COUNTS:
            actual = int(con.execute(f"SELECT count(*) FROM {view}").fetchone()[0])
            assert actual == expected
    finally:
        con.close()
