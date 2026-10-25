"""
STAGE: s2_features
MODULE: repo_context
PURPOSE: Repository context features and a three-way project-kind label.
INPUTS: generated repo_profile.parquet; mining_runs when the snapshot date is auto
OUTPUTS: src/data/derived/s2_repo.parquet
RQ LINK: RQ1 (project kind, copy share, and repository size as context for authorship signals)
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import duckdb

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_data.loader import snapshot_date

log = logging.getLogger(__name__)

_DOWNLOAD = "python src/data/download_data.py"
_BUILD_REPO_PROFILE = (
    'python -c "import duckdb; duckdb.connect().execute('
    "open('db/repos.sql').read())\""
)
_LARGE_TIERS = frozenset({"101-1000", ">1000"})
_BENCH = re.compile(r"sample|bench|test|demo|mirror|collection", re.IGNORECASE)
_OUTPUT_COLUMNS = (
    "repo_full_name",
    "owner",
    "stars",
    "language",
    "created_at",
    "pushed_at",
    "files",
    "copy_share",
    "size_tier",
    "repo_kind",
    "benchmark_like_name",
    "age_days_at_snapshot",
)


def repo_kind(row: Mapping) -> str:
    """A_collection, else B_agent_tooling, else C_project. First match wins."""
    if _is_collection(row):
        return "A_collection"
    if _flag(row.get("name_has_skill")) or _flag(row.get("name_has_agent")):
        return "B_agent_tooling"
    return "C_project"


def benchmark_like_name(name: str | None) -> bool:
    """True when owner/repo contains sample, bench, test, demo, mirror, or collection."""
    if not name:
        return False
    return _BENCH.search(str(name)) is not None


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write s2_repo.parquet. Dev mode keeps every repository; the sample is file-level."""
    output = _output_path(cfg)
    inputs = _inputs(cfg)
    _require_inputs(cfg)
    if not force and inputs_unchanged(output, cfg=cfg, inputs=inputs):
        log.info("repo context up to date: %s", output)
        return StageResult(
            stage="s2_features", outputs=[output], skipped=True, rows=_saved_rows(output)
        )
    if dev:
        log.info("repo context keeps every repository; the dev sample is file-level")
    rows = _write_repo(cfg, output)
    write_manifest(output, cfg=cfg, inputs=inputs, rows=rows)
    log.info("wrote %s (%s rows)", output, rows)
    return StageResult(
        stage="s2_features", outputs=[output], skipped=False, rows={"s2_repo": rows}
    )


def _is_collection(row: Mapping) -> bool:
    return (
        _flag(row.get("name_is_collection"))
        or row.get("size_tier") in _LARGE_TIERS
        or _large_copy_share(row.get("copy_share"))
    )


def _flag(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes"}
    return bool(value)


def _large_copy_share(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        share = float(value)
    except (TypeError, ValueError):
        return False
    return share >= 0.5


def _kind_from_fields(
    name_is_collection: object,
    size_tier: object,
    copy_share: object,
    name_has_skill: object,
    name_has_agent: object,
) -> str:
    return repo_kind(
        {
            "name_is_collection": name_is_collection,
            "size_tier": size_tier,
            "copy_share": copy_share,
            "name_has_skill": name_has_skill,
            "name_has_agent": name_has_agent,
        }
    )


def _output_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s2_repo.parquet"


def _repo_profile_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.generated) / "repo_profile.parquet"


def _mining_runs_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "mining_runs"


def _snapshot_auto(cfg: Config) -> bool:
    return cfg.rq2.snapshot_date.strip() == "auto"


def _inputs(cfg: Config) -> list[Path]:
    paths = [_repo_profile_path(cfg)]
    if _snapshot_auto(cfg):
        paths.append(_mining_runs_dir(cfg))
    return paths


def _require_inputs(cfg: Config) -> None:
    source = _repo_profile_path(cfg)
    if not source.is_file():
        raise FileNotFoundError(f"missing {source}; run: {_BUILD_REPO_PROFILE}")
    if not _snapshot_auto(cfg):
        return
    directory = _mining_runs_dir(cfg)
    if not directory.is_dir() or not any(directory.glob("*.parquet")):
        raise FileNotFoundError(f"missing {directory}; run: {_DOWNLOAD}")


def _snapshot(cfg: Config) -> date:
    configured = cfg.rq2.snapshot_date.strip()
    if configured != "auto":
        return date.fromisoformat(configured)
    con = duckdb.connect()
    try:
        con.execute("SET TimeZone = 'UTC'")
        listed = _sql_string(_mining_runs_dir(cfg) / "*.parquet")
        con.execute(f"CREATE VIEW mining_runs AS SELECT * FROM read_parquet({listed})")
        return snapshot_date(con)
    finally:
        con.close()


def _write_repo(cfg: Config, output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshot = _snapshot(cfg).isoformat()
    con = duckdb.connect()
    try:
        con.execute("SET TimeZone = 'UTC'")
        _register(con)
        con.execute(_copy_sql(_repo_profile_path(cfg), output, snapshot))
        counted = con.execute(
            "SELECT count(*) FROM read_parquet(?)", [output.as_posix()]
        ).fetchone()
    finally:
        con.close()
    return int(counted[0]) if counted else 0


def _register(con: duckdb.DuckDBPyConnection) -> None:
    con.create_function(
        "repo_kind",
        _kind_from_fields,
        ["BOOLEAN", "VARCHAR", "DOUBLE", "BOOLEAN", "BOOLEAN"],
        "VARCHAR",
        null_handling="special",
    )
    con.create_function(
        "benchmark_like_name",
        benchmark_like_name,
        ["VARCHAR"],
        "BOOLEAN",
        null_handling="special",
    )


_COPY_SQL = """
COPY (
    SELECT
        full_name AS repo_full_name,
        owner,
        CAST(stars AS INTEGER) AS stars,
        CAST(language AS VARCHAR) AS language,
        CAST(created_at AS VARCHAR) AS created_at,
        CAST(pushed_at AS VARCHAR) AS pushed_at,
        CAST(files AS INTEGER) AS files,
        CAST(copy_share AS DOUBLE) AS copy_share,
        size_tier,
        repo_kind(
            CAST(name_is_collection AS BOOLEAN),
            CAST(size_tier AS VARCHAR),
            CAST(copy_share AS DOUBLE),
            CAST(name_has_skill AS BOOLEAN),
            CAST(name_has_agent AS BOOLEAN)
        ) AS repo_kind,
        benchmark_like_name(CAST(full_name AS VARCHAR)) AS benchmark_like_name,
        CAST(date_diff(
            'day',
            CAST(try_cast(created_at AS TIMESTAMPTZ) AS DATE),
            DATE '{snapshot}'
        ) AS INTEGER) AS age_days_at_snapshot
    FROM read_parquet({source})
    ORDER BY repo_full_name
) TO {dest} (FORMAT PARQUET, COMPRESSION ZSTD)
"""


def _copy_sql(source: Path, dest: Path, snapshot: str) -> str:
    return _COPY_SQL.format(
        snapshot=snapshot,
        source=_sql_string(source),
        dest=_sql_string(dest),
    )


def _sql_string(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _saved_rows(output: Path) -> dict[str, int]:
    payload = json.loads(manifest_path(output).read_text(encoding="utf-8"))
    return {"s2_repo": int(payload["rows"])}
