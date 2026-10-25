"""
STAGE: s1_registry
MODULE: discover_popular
PURPOSE: Rank widely copied skills by distinct owners and name a candidate origin repo.
INPUTS: artifacts, repos, src/data/derived/s1_registry_matches.parquet
OUTPUTS: results/s1_popular_candidates.csv
RQ LINK: RQ1 (widely copied skills outside the known registry, ranked by distinct owners)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import duckdb
import pandas as pd

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_data.devsample import output_path as devsample_path

log = logging.getLogger(__name__)

_DOWNLOAD = "python src/data/download_data.py"
_MATCH = "python -m skillsig registry --only match"
_DEV_SAMPLE = "python -m skillsig data --dev-sample"

_COLUMNS = (
    "file_sha",
    "name",
    "files",
    "repos",
    "owners",
    "candidate_repo",
    "candidate_stars",
    "candidate_first_commit_at",
    "in_registry",
)
_OWNER_COLUMNS = ("repo", "files", "stars")
_ARTIFACT_COLUMNS = ("file_sha", "repo_full_name", "name", "first_commit_at", "dedup_primary")
_REPO_COLUMNS = ("full_name", "stars")
_TEXT = ("file_sha", "name", "candidate_repo", "candidate_first_commit_at")
_COUNTS = ("files", "repos", "owners")

_CANDIDATES_SQL = """
WITH occurrences AS (
    SELECT
        nullif(trim(CAST(a.file_sha AS VARCHAR)), '') AS file_sha,
        nullif(trim(CAST(a.repo_full_name AS VARCHAR)), '') AS repo_full_name,
        split_part(a.repo_full_name, '/', 1) AS owner,
        nullif(trim(CAST(a.name AS VARCHAR)), '') AS name,
        nullif(trim(CAST(a.first_commit_at AS VARCHAR)), '') AS first_commit_at,
        coalesce(TRY_CAST(a.dedup_primary AS INTEGER), 0) = 1 AS is_primary,
        TRY_CAST(r.stars AS BIGINT) AS stars
    FROM artifacts a
    LEFT JOIN repos r ON r.full_name = a.repo_full_name
    WHERE nullif(trim(CAST(a.file_sha AS VARCHAR)), '') IS NOT NULL
      AND nullif(trim(CAST(a.repo_full_name AS VARCHAR)), '') IS NOT NULL
),
skills AS (
    SELECT
        file_sha,
        count(*)::BIGINT AS files,
        count(DISTINCT repo_full_name)::BIGINT AS repos,
        count(DISTINCT owner)::BIGINT AS owners,
        coalesce(max(CASE WHEN is_primary THEN name END), max(name)) AS name
    FROM occurrences
    GROUP BY file_sha
),
holders AS (
    SELECT
        file_sha,
        repo_full_name,
        max(stars) AS stars,
        min(first_commit_at) AS first_commit_at
    FROM occurrences
    GROUP BY file_sha, repo_full_name
),
ranked AS (
    SELECT
        file_sha,
        repo_full_name,
        stars,
        first_commit_at,
        row_number() OVER (
            PARTITION BY file_sha
            ORDER BY
                first_commit_at IS NULL,
                first_commit_at,
                stars DESC NULLS LAST,
                repo_full_name
        ) AS rn
    FROM holders
)
SELECT
    s.file_sha,
    s.name,
    s.files,
    s.repos,
    s.owners,
    ranked.repo_full_name AS candidate_repo,
    ranked.stars AS candidate_stars,
    ranked.first_commit_at AS candidate_first_commit_at,
    m.blob IS NOT NULL AS in_registry
FROM skills s
JOIN ranked ON ranked.file_sha = s.file_sha AND ranked.rn = 1
LEFT JOIN (SELECT DISTINCT blob FROM registry_matches) m ON m.blob = s.file_sha
ORDER BY s.owners DESC, s.repos DESC, s.files DESC, s.file_sha
LIMIT {limit}
"""

_OWNER_SQL = """
SELECT
    a.repo_full_name AS repo,
    count(*)::BIGINT AS files,
    max(TRY_CAST(r.stars AS BIGINT)) AS stars
FROM artifacts a
LEFT JOIN repos r ON r.full_name = a.repo_full_name
WHERE split_part(a.repo_full_name, '/', 1) = ?
  AND nullif(trim(CAST(a.repo_full_name AS VARCHAR)), '') IS NOT NULL
GROUP BY a.repo_full_name
ORDER BY files DESC, stars DESC NULLS LAST, repo
"""


def popular_candidates(con: duckdb.DuckDBPyConnection, top_n: int = 200) -> pd.DataFrame:
    """Top skills by distinct owners, with a candidate origin and a registry flag.

    The candidate is the holder with the earliest first_commit_at. When no holder
    has a date, or several share that date, the higher star count wins.
    """
    limit = _top_n(top_n)
    _require_relation(con, "artifacts", _ARTIFACT_COLUMNS)
    _require_relation(con, "repos", _REPO_COLUMNS)
    _require_relation(con, "registry_matches", ("blob",))
    frame = con.execute(_CANDIDATES_SQL.format(limit=limit)).fetchdf()
    return _finish_candidates(frame)


def owner_repos(con: duckdb.DuckDBPyConnection, owner: str) -> pd.DataFrame:
    """Repos for one owner, ordered by skill-file count then stars.

    `owner` matches the leading segment of repo_full_name. An empty owner returns
    no rows. Use this list when extending config/registry_sources.yaml by hand.
    """
    _require_relation(con, "artifacts", ("repo_full_name",))
    _require_relation(con, "repos", _REPO_COLUMNS)
    text = owner.strip()
    if not text:
        return _empty_owners()
    frame = con.execute(_OWNER_SQL, [text]).fetchdf()
    return _finish_owners(frame)


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write popular-candidate rows. Dev mode keeps every copy of dev-sample blobs."""
    output = _output_path(cfg)
    inputs = _inputs(cfg, dev=dev)
    _require_inputs(cfg, dev=dev)
    if not force and inputs_unchanged(output, cfg=cfg, inputs=inputs):
        log.info("popular candidates up to date: %s", output)
        return StageResult(
            stage="s1_registry", outputs=[output], skipped=True, rows=_saved_rows(output)
        )
    if dev:
        log.info("popular candidates keep every copy of dev-sample blobs")
    rows = _write(cfg, output, dev=dev)
    write_manifest(output, cfg=cfg, inputs=inputs, rows=rows)
    log.info("wrote %s (%s rows)", output, rows)
    return StageResult(
        stage="s1_registry",
        outputs=[output],
        skipped=False,
        rows={"s1_popular_candidates": rows},
    )


def _write(cfg: Config, output: Path, *, dev: bool) -> int:
    con = _connect(cfg, dev=dev)
    try:
        frame = popular_candidates(con)
    finally:
        con.close()
    return _write_csv(frame, output)


def _finish_candidates(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return _empty_candidates()
    out = frame.loc[:, list(_COLUMNS)].copy()
    for name in _COUNTS:
        out[name] = out[name].astype("int64")
    out["candidate_stars"] = [_stars(value) for value in out["candidate_stars"]]
    out["candidate_stars"] = out["candidate_stars"].astype("Int64")
    out["in_registry"] = [bool(value) for value in out["in_registry"]]
    for name in _TEXT:
        out[name] = out[name].map(_text)
    ordered = out.sort_values(
        ["owners", "repos", "files", "file_sha"],
        ascending=[False, False, False, True],
        kind="mergesort",
    )
    return ordered.loc[:, list(_COLUMNS)].reset_index(drop=True)


def _finish_owners(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return _empty_owners()
    out = frame.loc[:, list(_OWNER_COLUMNS)].copy()
    out["repo"] = out["repo"].map(_text)
    out["files"] = out["files"].astype("int64")
    out["stars"] = [_stars(value) for value in out["stars"]]
    out["stars"] = out["stars"].astype("Int64")
    ordered = out.sort_values(
        ["files", "stars", "repo"],
        ascending=[False, False, True],
        na_position="last",
        kind="mergesort",
    )
    return ordered.loc[:, list(_OWNER_COLUMNS)].reset_index(drop=True)


def _connect(cfg: Config, *, dev: bool) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    try:
        _view(con, "artifacts", _artifacts_select(cfg, dev=dev))
        _view(con, "repos", _repos_select(cfg))
        _view(con, "registry_matches", _matches_select(cfg))
    except Exception:
        con.close()
        raise
    return con


def _artifacts_select(cfg: Config, *, dev: bool) -> str:
    listed = _quote(_artifacts_dir(cfg) / "*.parquet")
    columns = "a.file_sha, a.repo_full_name, a.name, a.first_commit_at, a.dedup_primary"
    if not dev:
        return f"SELECT {columns} FROM read_parquet({listed}) a"
    sample = _quote(devsample_path(cfg))
    return (
        f"SELECT {columns} FROM read_parquet({listed}) a "
        f"SEMI JOIN read_parquet({sample}) s ON a.file_sha = s.file_sha"
    )


def _repos_select(cfg: Config) -> str:
    listed = _quote(_repos_dir(cfg) / "*.parquet")
    return f"SELECT full_name, stars FROM read_parquet({listed})"


def _matches_select(cfg: Config) -> str:
    return f"SELECT blob FROM read_parquet({_quote(_matches_path(cfg))})"


def _view(con: duckdb.DuckDBPyConnection, name: str, select_sql: str) -> None:
    con.execute(f"CREATE VIEW {name} AS {select_sql}")


def _require_inputs(cfg: Config, *, dev: bool) -> None:
    artifacts = _artifacts_dir(cfg)
    repos = _repos_dir(cfg)
    if not artifacts.is_dir() or not any(artifacts.glob("*.parquet")):
        raise FileNotFoundError(f"missing {artifacts}; run: {_DOWNLOAD}")
    if not repos.is_dir() or not any(repos.glob("*.parquet")):
        raise FileNotFoundError(f"missing {repos}; run: {_DOWNLOAD}")
    matches = _matches_path(cfg)
    if not matches.is_file():
        raise FileNotFoundError(f"missing {matches}; run: {_MATCH}")
    if dev and not devsample_path(cfg).is_file():
        raise FileNotFoundError(f"missing {devsample_path(cfg)}; run: {_DEV_SAMPLE}")


def _inputs(cfg: Config, *, dev: bool) -> list[Path]:
    paths = [_artifacts_dir(cfg), _repos_dir(cfg), _matches_path(cfg)]
    if dev:
        paths.append(devsample_path(cfg))
    return paths


def _output_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.results) / "s1_popular_candidates.csv"


def _artifacts_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "artifacts"


def _repos_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "repos"


def _matches_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s1_registry_matches.parquet"


def _write_csv(frame: pd.DataFrame, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.loc[:, list(_COLUMNS)].to_csv(path, index=False, lineterminator="\n")
    return int(len(frame))


def _require_relation(con: duckdb.DuckDBPyConnection, name: str, columns: tuple[str, ...]) -> None:
    if name not in _relations(con):
        raise ValueError(f"{name} is not registered; expected columns {', '.join(columns)}")
    found = _column_names(con, name)
    missing = [column for column in columns if column not in found]
    if missing:
        raise ValueError(f"{name} missing columns: {missing}")


def _relations(con: duckdb.DuckDBPyConnection) -> set[str]:
    names = {str(name) for (name,) in con.execute("SHOW TABLES").fetchall()}
    try:
        names.update(str(name) for (name,) in con.execute("SHOW VIEWS").fetchall())
    except duckdb.Error:
        return names
    return names


def _column_names(con: duckdb.DuckDBPyConnection, relation: str) -> set[str]:
    rows = con.execute(f"DESCRIBE {relation}").fetchall()
    return {str(row[0]) for row in rows}


def _saved_rows(output: Path) -> dict[str, int]:
    payload = json.loads(manifest_path(output).read_text(encoding="utf-8"))
    return {"s1_popular_candidates": int(payload["rows"])}


def _empty_candidates() -> pd.DataFrame:
    frame = pd.DataFrame(columns=list(_COLUMNS))
    for name in _TEXT:
        frame[name] = pd.Series(dtype="string")
    for name in _COUNTS:
        frame[name] = pd.Series(dtype="int64")
    frame["candidate_stars"] = pd.Series(dtype="Int64")
    frame["in_registry"] = pd.Series(dtype="bool")
    return frame.loc[:, list(_COLUMNS)]


def _empty_owners() -> pd.DataFrame:
    frame = pd.DataFrame(columns=list(_OWNER_COLUMNS))
    frame["repo"] = pd.Series(dtype="string")
    frame["files"] = pd.Series(dtype="int64")
    frame["stars"] = pd.Series(dtype="Int64")
    return frame.loc[:, list(_OWNER_COLUMNS)]


def _top_n(top_n: int) -> int:
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
        raise ValueError(f"top_n must be an integer >= 1, got {top_n!r}")
    return top_n


def _stars(value: object) -> int | None:
    if _missing(value):
        return None
    return int(value)  # type: ignore[arg-type]


def _text(value: object) -> str | None:
    if _missing(value):
        return None
    text = str(value).strip()
    return text or None


def _missing(value: object) -> bool:
    if value is None or value is pd.NA:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _quote(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"
