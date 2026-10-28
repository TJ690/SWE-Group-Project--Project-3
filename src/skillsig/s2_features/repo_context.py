"""
STAGE: s2_features
MODULE: repo_context
PURPOSE: Repository context features, share measures, and a three-way project-kind label.
INPUTS: generated repo_profile.parquet; artifacts (repo_full_name, file_sha);
        s1_registry_matches.parquet; mining_runs when the snapshot date is auto
OUTPUTS: src/data/derived/s2_repo.parquet
RQ LINK: RQ1 (project kind, shared content, and repository size as context for authorship signals)
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
from skillsig.s0_intake.loader import snapshot_date

log = logging.getLogger(__name__)

_DOWNLOAD = "python src/data/download_data.py"
_MATCH = "python -m skillsig registry --only match"
_BUILD_REPO_PROFILE = (
    'python -c "import duckdb; duckdb.connect().execute('
    "open('db/repos.sql').read())\""
)
# Name words are matched on the repository part only (never the owner).
# `hub` is not counted inside `github`; `test` must start a word (not `latest`).
_COLLECTION = re.compile(r"awesome|collection|library|marketplace|registry|(?<!git)hub", re.I)
_SKILL = re.compile(r"skill", re.I)
_AGENT = re.compile(r"claude|agent|codex|cursor|copilot|gemini", re.I)
_BENCH = re.compile(r"sample|bench|(?<![a-z])test|demo|mirror|collection", re.I)
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
    "repo_name",
    "is_fork",
    "distinct_skills",
    "name_is_collection",
    "name_has_skill",
    "name_has_agent",
    "shared_share",
    "copied_in_share",
    "registry_origin_share",
)


def repo_name(full_name: str | None) -> str:
    """The part after `owner/`; the whole string when there is no slash."""
    if not full_name:
        return ""
    return str(full_name).split("/", 1)[-1]


def name_flags(full_name: str | None) -> tuple[bool, bool, bool]:
    """(name_is_collection, name_has_skill, name_has_agent) on the repository name only."""
    name = repo_name(full_name)
    return (
        _COLLECTION.search(name) is not None,
        _SKILL.search(name) is not None,
        _AGENT.search(name) is not None,
    )


def repo_kind(row: Mapping, *, files_min: int = 101, share_min: float = 0.5) -> str:
    """A_collection, else B_agent_tooling, else C_project. First match wins.

    A: collection name, or `files >= files_min`, or `shared_share >= share_min`.
    B: the repository name has a skill or agent word.
    """
    if _is_collection(row, files_min=files_min, share_min=share_min):
        return "A_collection"
    if _flag(row.get("name_has_skill")) or _flag(row.get("name_has_agent")):
        return "B_agent_tooling"
    return "C_project"


def benchmark_like_name(name: str | None) -> bool:
    """True when the repo name has sample, bench, test (word start), demo, mirror, or collection."""
    if not name:
        return False
    return _BENCH.search(repo_name(name)) is not None


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write s2_repo.parquet. Dev mode keeps every repository; the sample is file-level."""
    output = _output_path(cfg)
    _require_inputs(cfg)
    inputs = _inputs(cfg)
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


def _is_collection(row: Mapping, *, files_min: int, share_min: float) -> bool:
    return (
        _flag(row.get("name_is_collection"))
        or _at_least(row.get("files"), files_min)
        or _at_least(row.get("shared_share"), share_min)
    )


def _flag(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes"}
    return bool(value)


def _at_least(value: object, minimum: float) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if number != number:  # NaN
        return False
    return number >= minimum


def _name_is_collection(full_name: str | None) -> bool:
    return name_flags(full_name)[0]


def _name_has_skill(full_name: str | None) -> bool:
    return name_flags(full_name)[1]


def _name_has_agent(full_name: str | None) -> bool:
    return name_flags(full_name)[2]


def _output_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s2_repo.parquet"


def _repo_profile_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.generated) / "repo_profile.parquet"


def _artifacts_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "artifacts"


def _matches_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s1_registry_matches.parquet"


def _mining_runs_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "mining_runs"


def _snapshot_auto(cfg: Config) -> bool:
    return cfg.rq2.snapshot_date.strip() == "auto"


def _inputs(cfg: Config) -> list[Path]:
    paths = [_repo_profile_path(cfg), _artifacts_dir(cfg), _matches_path(cfg)]
    if _snapshot_auto(cfg):
        paths.append(_mining_runs_dir(cfg))
    return paths


def _require_inputs(cfg: Config) -> None:
    source = _repo_profile_path(cfg)
    if not source.is_file():
        raise FileNotFoundError(f"missing {source}; run: {_BUILD_REPO_PROFILE}")
    artifacts = _artifacts_dir(cfg)
    if not artifacts.is_dir() or not any(artifacts.glob("*.parquet")):
        raise FileNotFoundError(f"missing {artifacts}; run: {_DOWNLOAD}")
    matches = _matches_path(cfg)
    if not matches.is_file():
        raise FileNotFoundError(f"missing {matches}; run: {_MATCH}")
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
        _register(con, cfg)
        con.execute(_copy_sql(cfg, output, snapshot))
        counted = con.execute(
            "SELECT count(*) FROM read_parquet(?)", [output.as_posix()]
        ).fetchone()
    finally:
        con.close()
    return int(counted[0]) if counted else 0


def _register(con: duckdb.DuckDBPyConnection, cfg: Config) -> None:
    files_min = int(cfg.thresholds.collection_files_min)
    share_min = float(cfg.thresholds.collection_copy_share_min)

    def kind(
        name_is_collection: object,
        files: object,
        shared_share: object,
        name_has_skill: object,
        name_has_agent: object,
    ) -> str:
        row = {
            "name_is_collection": name_is_collection,
            "files": files,
            "shared_share": shared_share,
            "name_has_skill": name_has_skill,
            "name_has_agent": name_has_agent,
        }
        return repo_kind(row, files_min=files_min, share_min=share_min)

    con.create_function(
        "repo_kind",
        kind,
        ["BOOLEAN", "BIGINT", "DOUBLE", "BOOLEAN", "BOOLEAN"],
        "VARCHAR",
        null_handling="special",
    )
    for udf, func in (
        ("name_is_collection", _name_is_collection),
        ("name_has_skill", _name_has_skill),
        ("name_has_agent", _name_has_agent),
    ):
        con.create_function(udf, func, ["VARCHAR"], "BOOLEAN", null_handling="special")
    con.create_function(
        "repo_name", repo_name, ["VARCHAR"], "VARCHAR", null_handling="special"
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
    WITH arts AS (
        SELECT CAST(repo_full_name AS VARCHAR) AS repo, CAST(file_sha AS VARCHAR) AS file_sha
        FROM read_parquet({artifacts})
        WHERE file_sha IS NOT NULL AND repo_full_name IS NOT NULL
    ),
    holders AS (
        SELECT file_sha, count(DISTINCT lower(repo)) AS n_repos
        FROM arts
        GROUP BY file_sha
    ),
    origins AS (
        SELECT CAST(blob AS VARCHAR) AS file_sha, lower(CAST(origin_repo AS VARCHAR)) AS origin
        FROM read_parquet({matches})
        WHERE blob IS NOT NULL AND origin_repo IS NOT NULL
    ),
    shares AS (
        SELECT
            lower(a.repo) AS repo_key,
            avg(CASE WHEN h.n_repos > 1 THEN 1.0 ELSE 0.0 END) AS shared_share,
            avg(CASE WHEN o.origin IS NOT NULL AND o.origin <> lower(a.repo)
                THEN 1.0 ELSE 0.0 END) AS copied_in_share,
            avg(CASE WHEN o.origin = lower(a.repo) THEN 1.0 ELSE 0.0 END)
                AS registry_origin_share
        FROM arts a
        JOIN holders h USING (file_sha)
        LEFT JOIN origins o USING (file_sha)
        GROUP BY lower(a.repo)
    ),
    base AS (
        SELECT
            p.*,
            name_is_collection(CAST(p.full_name AS VARCHAR)) AS nic,
            name_has_skill(CAST(p.full_name AS VARCHAR)) AS nhs,
            name_has_agent(CAST(p.full_name AS VARCHAR)) AS nha,
            s.shared_share AS s_shared,
            s.copied_in_share AS s_copied,
            s.registry_origin_share AS s_origin
        FROM read_parquet({source}) AS p
        LEFT JOIN shares AS s ON s.repo_key = lower(CAST(p.full_name AS VARCHAR))
    )
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
        repo_kind(nic, CAST(files AS BIGINT), s_shared, nhs, nha) AS repo_kind,
        benchmark_like_name(CAST(full_name AS VARCHAR)) AS benchmark_like_name,
        CAST(date_diff(
            'day',
            CAST(try_cast(created_at AS TIMESTAMPTZ) AS DATE),
            DATE '{snapshot}'
        ) AS INTEGER) AS age_days_at_snapshot,
        repo_name(CAST(full_name AS VARCHAR)) AS repo_name,
        CAST(is_fork AS BOOLEAN) AS is_fork,
        CAST(distinct_skills AS INTEGER) AS distinct_skills,
        nic AS name_is_collection,
        nhs AS name_has_skill,
        nha AS name_has_agent,
        CAST(s_shared AS DOUBLE) AS shared_share,
        CAST(s_copied AS DOUBLE) AS copied_in_share,
        CAST(s_origin AS DOUBLE) AS registry_origin_share
    FROM base
    ORDER BY repo_full_name
) TO {dest} (FORMAT PARQUET, COMPRESSION ZSTD)
"""


def _copy_sql(cfg: Config, dest: Path, snapshot: str) -> str:
    return _COPY_SQL.format(
        snapshot=snapshot,
        source=_sql_string(_repo_profile_path(cfg)),
        artifacts=_sql_string(_artifacts_dir(cfg) / "*.parquet"),
        matches=_sql_string(_matches_path(cfg)),
        dest=_sql_string(dest),
    )


def _sql_string(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _saved_rows(output: Path) -> dict[str, int]:
    payload = json.loads(manifest_path(output).read_text(encoding="utf-8"))
    return {"s2_repo": int(payload["rows"])}
