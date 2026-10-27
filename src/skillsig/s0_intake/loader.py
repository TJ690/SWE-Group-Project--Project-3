"""
STAGE: s0_intake
MODULE: loader
PURPOSE: Register DuckDB views over the GitSkills Parquet files and expose the snapshot date.
INPUTS: raw Parquet under Config.paths.raw and generated Parquet under Config.paths.generated
OUTPUTS: src/data/derived/s0_devsample_ids.parquet (via run)
RQ LINK: RQ1 / RQ2 (population frame: artifacts, repos, and the mining snapshot date)
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

import duckdb

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_intake.devsample import build_devsample, output_path
from skillsig.s0_intake.schema_check import VIEW_NAMES, check_schema

log = logging.getLogger(__name__)

_DOWNLOAD = "python src/data/download_data.py"

_RAW_GLOBS: dict[str, str] = {
    "artifacts": "artifacts/*.parquet",
    "repos": "repos/*.parquet",
    "artifact_siblings": "artifact_siblings/*.parquet",
    "mining_runs": "mining_runs/*.parquet",
}
_GENERATED_FILES: dict[str, str] = {
    "skill_features": "skill_features.parquet",
    "file_agents": "file_agents.parquet",
    "repo_profile": "repo_profile.parquet",
    "sibling_files": "sibling_files.parquet",
}


def dataset_inputs(cfg: Config) -> list[Path]:
    """Raw and generated dataset directories. Their hashes decide whether run() can skip."""
    return [resolve(cfg, cfg.paths.raw), resolve(cfg, cfg.paths.generated)]


def connect(cfg: Config) -> duckdb.DuckDBPyConnection:
    """In-memory connection with one read_parquet view per GitSkills table. Does not copy data."""
    specs = _specs(cfg)
    missing = [path for _, path in specs if not _exists(path)]
    if missing:
        raise _missing_dataset(missing[0])
    con = duckdb.connect()
    try:
        for name, path in specs:
            _register(con, name, path)
    except Exception:
        con.close()
        raise
    log.info("registered %d views", len(specs))
    return con


def snapshot_date(con: duckdb.DuckDBPyConnection) -> date:
    """max(mining_runs.finished_at) as a calendar date."""
    value = con.execute("SELECT max(finished_at) FROM mining_runs").fetchone()[0]
    if value is None:
        raise ValueError("mining_runs.finished_at has no values")
    return _as_date(value)


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Check the schema and write the dev-sample ids. Skip when inputs and config are unchanged."""
    output = output_path(cfg)
    inputs = dataset_inputs(cfg)
    if not force and inputs_unchanged(output, cfg=cfg, inputs=inputs):
        log.info("dev sample up to date: %s", output)
        return StageResult(
            stage="s0_intake", outputs=[output], skipped=True, rows=_saved_rows(output)
        )
    con = connect(cfg)
    try:
        problems = check_schema(con, dev=dev)
        if problems:
            raise RuntimeError("schema check failed:\n" + "\n".join(problems))
        written = build_devsample(con, cfg)
        counted = con.execute(
            "SELECT count(*) FROM read_parquet(?)", [written.as_posix()]
        ).fetchone()
        rows = int(counted[0])
    finally:
        con.close()
    write_manifest(written, cfg=cfg, inputs=inputs, rows=rows)
    log.info("wrote %s (%s rows)", written, rows)
    return StageResult(
        stage="s0_intake", outputs=[written], skipped=False, rows={"s0_devsample_ids": rows}
    )


def _specs(cfg: Config) -> list[tuple[str, Path]]:
    raw = resolve(cfg, cfg.paths.raw)
    generated = resolve(cfg, cfg.paths.generated)
    specs: list[tuple[str, Path]] = []
    for name in VIEW_NAMES:
        if name in _RAW_GLOBS:
            specs.append((name, raw / _RAW_GLOBS[name]))
        else:
            specs.append((name, generated / _GENERATED_FILES[name]))
    return specs


def _exists(path: Path) -> bool:
    if "*" in path.name:
        return path.parent.is_dir() and any(path.parent.glob(path.name))
    return path.is_file()


def _missing_dataset(path: Path) -> FileNotFoundError:
    shown = path.parent if "*" in path.name else path
    return FileNotFoundError(f"missing dataset at {shown}; run: {_DOWNLOAD}")


def _register(con: duckdb.DuckDBPyConnection, name: str, path: Path) -> None:
    literal = "'" + path.as_posix().replace("'", "''") + "'"
    con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet({literal})")


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return date.fromisoformat(text[:10])


def _saved_rows(output: Path) -> dict[str, int]:
    payload = json.loads(manifest_path(output).read_text(encoding="utf-8"))
    return {"s0_devsample_ids": int(payload["rows"])}
