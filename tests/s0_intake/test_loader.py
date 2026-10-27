from __future__ import annotations

from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s0_intake.loader import connect, run, snapshot_date
from skillsig.s0_intake.schema_check import VIEW_COLUMNS, VIEW_NAMES

_EXTRA_COLUMNS: dict[str, tuple[str, ...]] = {
    "artifact_siblings": (
        "repo_full_name",
        "artifact_path",
        "entry_name",
        "entry_type",
        "entry_size",
        "entry_sha",
        "content",
        "content_fetched",
        "skipped_reason",
    ),
    "sibling_files": (
        "repo_full_name",
        "artifact_path",
        "entry_name",
        "entry_size",
        "entry_sha",
        "skipped_reason",
        "top_folder",
        "subfolder",
        "ext",
        "file_category",
    ),
}


def write_views(cfg: Config, *, n: int = 20, omit: dict[str, str] | None = None) -> None:
    """Tiny Parquet stand-in for every view connect() registers."""
    omit = omit or {}
    columns = {**VIEW_COLUMNS, **_EXTRA_COLUMNS}
    for view, names in columns.items():
        dropped = omit.get(view)
        kept = [name for name in names if name != dropped]
        frame = {name: _values(name, n) for name in kept}
        _write(_view_path(cfg, view), frame)


def _values(name: str, n: int) -> list[object]:
    if name == "file_sha":
        return [f"{i:040x}" for i in range(n)]
    if name == "finished_at":
        early = ["2026-07-01T00:00:00+00:00"] * (n - 1)
        return early + ["2026-07-20T22:26:56+00:00"]
    if name in {"history_fetched", "dedup_primary"}:
        return [1] * n
    return [f"{name}-{i}" for i in range(n)]


def _view_path(cfg: Config, view: str) -> Path:
    raw = resolve(cfg, cfg.paths.raw)
    generated = resolve(cfg, cfg.paths.generated)
    folders = {
        "artifacts": raw / "artifacts" / "part-00000.parquet",
        "repos": raw / "repos" / "part-00000.parquet",
        "artifact_siblings": raw / "artifact_siblings" / "part-00000.parquet",
        "mining_runs": raw / "mining_runs" / "part-00000.parquet",
    }
    return folders.get(view, generated / f"{view}.parquet")


def _write(path: Path, columns: dict[str, list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table({key: pa.array(values) for key, values in columns.items()})
    pq.write_table(table, path, compression="zstd")


def test_views_register_and_join_on_file_sha(tmp_cfg: Config) -> None:
    write_views(tmp_cfg, n=20)
    con = connect(tmp_cfg)
    try:
        for name in VIEW_NAMES:
            con.execute(f"SELECT * FROM {name} LIMIT 0")
        joined = con.execute(
            """
            SELECT count(*)
            FROM artifacts a
            JOIN skill_features s ON a.file_sha = s.file_sha
            JOIN file_agents f ON a.file_sha = f.file_sha
            """
        ).fetchone()[0]
        assert joined == 20
        assert snapshot_date(con) == date(2026, 7, 20)
    finally:
        con.close()


def test_missing_dataset_names_download_command(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match=r"python src/data/download_data\.py"):
        connect(tmp_cfg)


def test_snapshot_date_is_max_finished_at(tmp_cfg: Config) -> None:
    write_views(tmp_cfg, n=3)
    con = connect(tmp_cfg)
    try:
        assert snapshot_date(con) == date(2026, 7, 20)
    finally:
        con.close()


def test_run_writes_sample_and_skips_when_unchanged(tmp_cfg: Config) -> None:
    write_views(tmp_cfg, n=20)
    first = run(tmp_cfg, dev=True)
    assert first.skipped is False
    assert first.rows == {"s0_devsample_ids": 20}
    assert first.outputs[0].name == "s0_devsample_ids.parquet"
    table = pq.read_table(first.outputs[0])
    assert table.column_names == ["file_sha", "stratum"]
    assert table.column("file_sha").to_pylist() == sorted(table.column("file_sha").to_pylist())

    second = run(tmp_cfg, dev=True)
    assert second.skipped is True
    assert second.rows == {"s0_devsample_ids": 20}

    forced = run(tmp_cfg, dev=True, force=True)
    assert forced.skipped is False


def test_run_full_mode_rejects_fixture_counts(tmp_cfg: Config) -> None:
    write_views(tmp_cfg, n=20)
    with pytest.raises(RuntimeError, match="row count"):
        run(tmp_cfg, dev=False)


def test_views_read_parquet_without_copying(tmp_cfg: Config) -> None:
    write_views(tmp_cfg, n=2)
    con = connect(tmp_cfg)
    try:
        sql = con.execute(
            "SELECT sql FROM duckdb_views() WHERE view_name = 'artifacts'"
        ).fetchone()[0]
        assert "read_parquet" in sql
        kind = con.execute(
            """
            SELECT table_type FROM information_schema.tables
            WHERE table_name = 'artifacts'
            """
        ).fetchone()[0]
        assert kind == "VIEW"
    finally:
        con.close()
