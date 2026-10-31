"""Check the genealogy inputs before any analysis.

What: confirm every input file exists, check columns, nulls, dates and joins,
and preview the registry rule, the non-English share and the copy download.
Inputs: GitSkills parquet (artifacts, repos, mining runs, siblings),
wild_history versions, blobs and repo_status, MinHash shards and clusters.
Outputs: ``data_check_report.json`` under ``data_dir(dev)``. The process
exits 1 when any check is FAIL.
How to run: ``python -m skill_genealogy.check_data [--dev] [--force]``.
``--dev`` only changes the report folder. The checks always read the full inputs.
"""

from __future__ import annotations

import glob
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

import skill_genealogy.settings as settings
from skill_genealogy.file_helpers import (
    build_arg_parser,
    connect_duckdb,
    outputs_exist,
    print_sample_records,
    print_summary,
)
from skill_genealogy.text_helpers import detect_language, normalize_text

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

# A null share above this fraction is WARN. Stage 1 sets the cut at 1%.
NULL_SHARE_WARN_ABOVE = 0.01
# A join share below this fraction is WARN. Stage 1 sets the cut at 99%.
JOIN_COVERAGE_WARN_BELOW = 0.99

DATASET_BUILDER = "python src/data/download_data.py"
WILD_HISTORY_BUILDER = "main pipeline step 3f"
DEDUP_BUILDER = "python -m skillsig dedup"

ARTIFACT_COLUMNS: tuple[str, ...] = (
    "repo_full_name",
    "path",
    "file_sha",
    "content",
    "first_commit_at",
    "last_commit_at",
    "commit_count",
    "content_sha_ok",
    "name",
    "description",
)
REPO_COLUMNS: tuple[str, ...] = (
    "full_name",
    "owner",
    "stars",
    "is_fork",
    "license",
    "created_at",
)
SIBLING_COLUMNS: tuple[str, ...] = (
    "repo_full_name",
    "artifact_path",
    "entry_name",
    "entry_type",
    "entry_size",
    "entry_sha",
    "content",
)

_VIEW_NAME = re.compile(r"^[a-z_]+$")


@dataclass
class CheckResult:
    """One data check: a name, PASS/WARN/FAIL, the numbers behind it, and a message.

    ``status`` is ``PASS``, ``WARN`` or ``FAIL``. ``numbers`` is what the
    printed table and the JSON report show. ``message`` is one sentence.
    """

    name: str
    status: str
    numbers: dict[str, object]
    message: str


def _plain(value: object) -> object:
    """Turn one value into something ``json.dumps`` can write."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return round(float(value), 6)
    if isinstance(value, (float, np.floating)):
        return round(float(value), 6)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _result(name: str, status: str, numbers: dict[str, object], message: str) -> CheckResult:
    """Build one check, with numbers converted for JSON."""
    if status not in {PASS, WARN, FAIL}:
        raise ValueError(status)
    plain = _plain(numbers)
    if not isinstance(plain, dict):
        raise TypeError(type(plain))
    return CheckResult(name, status, plain, message)


def _share(part: int, total: int) -> float | None:
    """Return ``part / total`` rounded to 6 places, or None when ``total`` is 0."""
    if total <= 0:
        return None
    return round(part / total, 6)


def _null_status(null_rows: int, total: int) -> str:
    """WARN when the null share is above 1 percent.

    1 of 100 is exactly 1 percent and passes. 2 of 100 is a warning.
    Zero rows is a warning because a share cannot be computed.

    Example:
    >>> _null_status(1, 100)
    'PASS'
    >>> _null_status(2, 100)
    'WARN'
    >>> _null_status(0, 0)
    'WARN'
    """
    if total <= 0:
        return WARN
    if null_rows / total > NULL_SHARE_WARN_ABOVE:
        return WARN
    return PASS


def _coverage_status(found: int, total: int) -> str:
    """WARN when the share found is below 99 percent.

    99 of 100 passes. 98 of 100 is a warning. Zero rows is a warning
    because a share cannot be computed.

    Example:
    >>> _coverage_status(99, 100)
    'PASS'
    >>> _coverage_status(98, 100)
    'WARN'
    >>> _coverage_status(0, 0)
    'WARN'
    """
    if total <= 0:
        return WARN
    if found / total < JOIN_COVERAGE_WARN_BELOW:
        return WARN
    return PASS


def _quote_path(path: str) -> str:
    """Return a single-quoted SQL string with quotes escaped."""
    return "'" + path.replace("'", "''") + "'"


# Query Q1.1, see QUERIES.md
REGISTER_PARQUET_SQL = "CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM read_parquet({path})"

# Query Q1.2, see QUERIES.md
DESCRIBE_VIEW_SQL = "DESCRIBE {view_name}"

# Query Q1.3, see QUERIES.md
VIEW_ROW_COUNT_SQL = "SELECT count(*) AS rows FROM {view_name}"


def _check_view_name(view_name: str) -> None:
    """Reject a view name that is not a plain identifier."""
    if _VIEW_NAME.fullmatch(view_name) is None:
        raise ValueError(f"bad view name: {view_name}")


def _register_parquet(con: duckdb.DuckDBPyConnection, view_name: str, path: str) -> None:
    """Point a view at a parquet file or glob. The path is read from settings at call time."""
    _check_view_name(view_name)
    sql = REGISTER_PARQUET_SQL.format(view_name=view_name, path=_quote_path(path))
    con.execute(sql)


def _column_names(con: duckdb.DuckDBPyConnection, view_name: str) -> list[str]:
    """Return the column names of a view, in file order."""
    _check_view_name(view_name)
    rows = con.execute(DESCRIBE_VIEW_SQL.format(view_name=view_name)).fetchall()
    return [str(row[0]) for row in rows]


def _row_count(con: duckdb.DuckDBPyConnection, view_name: str) -> int:
    """Return the number of rows in a view."""
    _check_view_name(view_name)
    row = con.execute(VIEW_ROW_COUNT_SQL.format(view_name=view_name)).fetchone()
    return int(row[0]) if row else 0


def read_inputs(con: duckdb.DuckDBPyConnection) -> None:
    """Register the parquet inputs as views.

    Blobs and repo_status are not registered: stage 1 only checks that those
    two files exist. Versions, clusters and the four GitSkills tables are
    registered. Creating a view does not read the file body.

    Inputs: an open DuckDB connection. Paths come from ``settings``.
    Outputs: views ``artifacts``, ``repos``, ``mining_runs``,
    ``artifact_siblings``, ``wild_versions`` and ``clusters``.
    """
    _register_parquet(con, "artifacts", settings.ARTIFACTS_GLOB)
    _register_parquet(con, "repos", settings.REPOS_GLOB)
    _register_parquet(con, "mining_runs", settings.MINING_RUNS_GLOB)
    _register_parquet(con, "artifact_siblings", settings.SIBLINGS_GLOB)
    versions = settings.WILD_HISTORY_DIR / "versions.parquet"
    _register_parquet(con, "wild_versions", str(versions))
    _register_parquet(con, "clusters", str(settings.CLUSTERS_PATH))


def _glob_result(name: str, pattern: str, builder: str) -> CheckResult:
    """PASS when a glob matches at least one file. FAIL names the builder."""
    matches = [hit for hit in glob.glob(pattern) if Path(hit).is_file()]
    if not matches:
        return _result(name, FAIL, {"files": 0}, f"missing {pattern} (build with: {builder})")
    count = len(matches)
    word = "file" if count == 1 else "files"
    return _result(name, PASS, {"files": count}, f"found {count} {word}")


def _file_result(name: str, path: Path, builder: str) -> CheckResult:
    """PASS when one file is on disk. FAIL names the builder."""
    if path.is_file():
        return _result(name, PASS, {"files": 1}, f"found {path.name}")
    return _result(name, FAIL, {"files": 0}, f"missing {path} (build with: {builder})")


def _signature_files(folder: Path) -> list[Path]:
    """Return MinHash signature arrays, not the id lists.

    ``shard_*.npy`` also matches ``shard_00000.ids.npy``, so those id files
    are left out. Only real signature matrices remain.
    """
    if not folder.is_dir():
        return []
    found = [
        path
        for path in folder.glob("shard_*.npy")
        if path.is_file() and not path.name.endswith(".ids.npy")
    ]
    return sorted(found)


def _id_files(folder: Path) -> list[Path]:
    """Return ``shard_*.ids.npy`` files. These hold file_sha values, not sketches."""
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob("shard_*.ids.npy") if path.is_file())


def _shard_result(name: str, files: list[Path], folder: Path, label: str) -> CheckResult:
    """PASS when at least one shard file of this kind exists."""
    if files:
        count = len(files)
        word = label[:-1] if count == 1 and label.endswith("s") else label
        return _result(name, PASS, {"files": count}, f"found {count} {word}")
    return _result(
        name,
        FAIL,
        {"files": 0},
        f"missing {label} under {folder} (build with: {DEDUP_BUILDER})",
    )


def check_files_exist() -> list[CheckResult]:
    """FAIL when an input is missing, and name the command that builds it.

    Dataset tables are built by ``python src/data/download_data.py``.
    wild_history is built by main pipeline step 3f. MinHash shards and
    clusters are built by ``python -m skillsig dedup``.

    Inputs: paths from ``settings``. Nothing is read except file names.
    Outputs: one check per input. A present file is PASS.
    """
    wild = settings.WILD_HISTORY_DIR
    shards = settings.MINHASH_SHARDS_DIR
    return [
        _glob_result("artifacts", settings.ARTIFACTS_GLOB, DATASET_BUILDER),
        _glob_result("repos", settings.REPOS_GLOB, DATASET_BUILDER),
        _glob_result("mining_runs", settings.MINING_RUNS_GLOB, DATASET_BUILDER),
        _glob_result("artifact_siblings", settings.SIBLINGS_GLOB, DATASET_BUILDER),
        _file_result("wild_history_versions", wild / "versions.parquet", WILD_HISTORY_BUILDER),
        _file_result("wild_history_blobs", wild / "blobs.parquet", WILD_HISTORY_BUILDER),
        _file_result(
            "wild_history_repo_status",
            wild / "repo_status.parquet",
            WILD_HISTORY_BUILDER,
        ),
        _file_result("minhash_plan", shards / "plan.json", DEDUP_BUILDER),
        _shard_result("minhash_shard_ids", _id_files(shards), shards, "shard id files"),
        _shard_result("minhash_signatures", _signature_files(shards), shards, "signature files"),
        _file_result("clusters", settings.CLUSTERS_PATH, DEDUP_BUILDER),
    ]


def _columns_result(name: str, found: list[str], required: tuple[str, ...]) -> CheckResult:
    """FAIL when any required column is absent. Extra columns are fine."""
    missing = [column for column in required if column not in found]
    if missing:
        return _result(
            name,
            FAIL,
            {"missing": missing},
            "missing columns: " + ", ".join(missing),
        )
    return _result(
        name,
        PASS,
        {"columns": len(found)},
        "all required columns are present",
    )


def _versions_column_result(con: duckdb.DuckDBPyConnection) -> CheckResult:
    """List wild_history version columns. WARN only when the file has no rows."""
    columns = _column_names(con, "wild_versions")
    rows = _row_count(con, "wild_versions")
    status = WARN if rows == 0 else PASS
    if rows == 0:
        message = "versions.parquet has columns but no rows: " + ", ".join(columns)
    else:
        message = "columns: " + ", ".join(columns)
    return _result(
        "wild_history_versions_columns",
        status,
        {"rows": rows, "columns": columns},
        message,
    )


def check_required_columns(con: duckdb.DuckDBPyConnection) -> list[CheckResult]:
    """FAIL when a required column is missing. Version columns are only listed.

    Artifacts, repos and siblings have a fixed column list. wild_history
    versions is described and printed. An empty versions file is WARN.
    A missing column name there is not a failure: later cards need the list.

    Inputs: a DuckDB connection. Parquet paths come from ``settings``.
    Outputs: four checks.
    """
    _register_parquet(con, "artifacts", settings.ARTIFACTS_GLOB)
    _register_parquet(con, "repos", settings.REPOS_GLOB)
    _register_parquet(con, "artifact_siblings", settings.SIBLINGS_GLOB)
    versions = settings.WILD_HISTORY_DIR / "versions.parquet"
    _register_parquet(con, "wild_versions", str(versions))
    return [
        _columns_result("artifacts_columns", _column_names(con, "artifacts"), ARTIFACT_COLUMNS),
        _columns_result("repos_columns", _column_names(con, "repos"), REPO_COLUMNS),
        _columns_result(
            "artifact_siblings_columns",
            _column_names(con, "artifact_siblings"),
            SIBLING_COLUMNS,
        ),
        _versions_column_result(con),
    ]


# Query Q1.4, see QUERIES.md
ARTIFACT_COUNTS_SQL = """
SELECT
    count(*) AS artifact_rows,
    count(DISTINCT file_sha) AS distinct_file_sha,
    count(DISTINCT repo_full_name) AS distinct_repos,
    count(*) FILTER (WHERE file_sha IS NULL) AS null_file_sha,
    count(*) FILTER (WHERE content IS NULL) AS null_content,
    count(*) FILTER (WHERE first_commit_at IS NULL) AS null_first_commit_at,
    count(*) FILTER (WHERE content_sha_ok = 1) AS content_sha_ok_rows,
    count(DISTINCT file_sha) FILTER (WHERE content IS NOT NULL) AS file_sha_with_content,
    count(DISTINCT file_sha) FILTER (WHERE first_commit_at IS NOT NULL)
        AS file_sha_with_first_commit_at
FROM artifacts
"""

# Query Q1.5, see QUERIES.md
REPO_COUNTS_SQL = """
SELECT
    count(*) AS repo_rows,
    count(DISTINCT full_name) AS distinct_repos,
    count(*) FILTER (WHERE stars IS NULL) AS null_stars
FROM repos
"""


def _artifact_count_result(row: tuple[object, ...]) -> CheckResult:
    """Build the artifact count check. WARN when any null share is above 1%.

    ``file_sha_without_content`` is distinct texts that never have a content
    row. ``file_sha_with_first_commit_at`` is distinct texts that have a date.
    The warning still uses the share of rows, not these distinct counts.
    """
    rows = int(row[0])
    distinct_file_sha = int(row[1])
    distinct_repos = int(row[2])
    null_file_sha = int(row[3])
    null_content = int(row[4])
    null_first = int(row[5])
    content_sha_ok_rows = int(row[6])
    file_sha_with_content = int(row[7])
    file_sha_with_first_commit_at = int(row[8])
    shares = {
        "file_sha": _null_status(null_file_sha, rows),
        "content": _null_status(null_content, rows),
        "first_commit_at": _null_status(null_first, rows),
    }
    warned = [name for name, status in shares.items() if status == WARN]
    status = WARN if warned or rows == 0 else PASS
    if warned:
        message = "null share above 1% for " + ", ".join(warned)
    elif rows == 0:
        message = "artifacts has no rows"
    else:
        message = "null shares are at or below 1%"
    return _result(
        "artifact_counts_and_nulls",
        status,
        {
            "artifact_rows": rows,
            "distinct_file_sha": distinct_file_sha,
            "distinct_repos": distinct_repos,
            "null_file_sha_share": _share(null_file_sha, rows),
            "null_content_share": _share(null_content, rows),
            "null_first_commit_at_share": _share(null_first, rows),
            "content_sha_ok_share": _share(content_sha_ok_rows, rows),
            "file_sha_without_content": distinct_file_sha - file_sha_with_content,
            "file_sha_with_first_commit_at": file_sha_with_first_commit_at,
        },
        message,
    )


def _repo_count_result(row: tuple[object, ...]) -> CheckResult:
    """Build the repo count check. WARN when the null share of stars is above 1%."""
    rows = int(row[0])
    distinct_repos = int(row[1])
    null_stars = int(row[2])
    status = _null_status(null_stars, rows)
    if rows == 0:
        message = "repos has no rows"
    elif status == WARN:
        message = "null share of stars is above 1%"
    else:
        message = "null share of stars is at or below 1%"
    return _result(
        "repo_counts_and_nulls",
        status,
        {
            "repo_rows": rows,
            "distinct_repos": distinct_repos,
            "null_stars_share": _share(null_stars, rows),
        },
        message,
    )


def check_counts_and_nulls(con: duckdb.DuckDBPyConnection) -> list[CheckResult]:
    """Count rows, distinct texts and repos, and the null share of key columns.

    Null shares are for ``file_sha``, ``content`` and ``first_commit_at``
    on artifacts, and ``stars`` on repos. A share above 1% is WARN.
    ``content_sha_ok = 1`` is reported and does not change the status.

    Inputs: a DuckDB connection and the artifacts and repos globs.
    Outputs: two checks.
    """
    _register_parquet(con, "artifacts", settings.ARTIFACTS_GLOB)
    _register_parquet(con, "repos", settings.REPOS_GLOB)
    artifact_row = con.execute(ARTIFACT_COUNTS_SQL).fetchone()
    repo_row = con.execute(REPO_COUNTS_SQL).fetchone()
    if artifact_row is None or repo_row is None:
        raise RuntimeError("count queries returned no row")
    return [_artifact_count_result(artifact_row), _repo_count_result(repo_row)]


# Query Q1.6, see QUERIES.md
COMMIT_DATES_SQL = """
SELECT
    min(TRY_CAST(first_commit_at AS TIMESTAMP)) AS first_min,
    max(TRY_CAST(first_commit_at AS TIMESTAMP)) AS first_max,
    min(TRY_CAST(last_commit_at AS TIMESTAMP)) AS last_min,
    max(TRY_CAST(last_commit_at AS TIMESTAMP)) AS last_max,
    count(*) FILTER (
        WHERE coalesce(first_commit_at, '') <> ''
          AND TRY_CAST(first_commit_at AS TIMESTAMP) IS NULL
    ) AS first_unparsed,
    count(*) FILTER (
        WHERE coalesce(last_commit_at, '') <> ''
          AND TRY_CAST(last_commit_at AS TIMESTAMP) IS NULL
    ) AS last_unparsed,
    count(*) FILTER (
        WHERE CAST(TRY_CAST(first_commit_at AS TIMESTAMP) AS DATE) > CAST(? AS DATE)
           OR CAST(TRY_CAST(last_commit_at AS TIMESTAMP) AS DATE) > CAST(? AS DATE)
    ) AS commits_after_today
FROM artifacts
"""

# Query Q1.7, see QUERIES.md
MINING_DATES_SQL = """
SELECT
    min(TRY_CAST(started_at AS TIMESTAMP)) AS capture_start,
    max(TRY_CAST(finished_at AS TIMESTAMP)) AS capture_end,
    count(*) FILTER (
        WHERE coalesce(started_at, '') <> ''
          AND TRY_CAST(started_at AS TIMESTAMP) IS NULL
    ) AS start_unparsed,
    count(*) FILTER (
        WHERE coalesce(finished_at, '') <> ''
          AND TRY_CAST(finished_at AS TIMESTAMP) IS NULL
    ) AS end_unparsed,
    count(*) FILTER (
        WHERE CAST(TRY_CAST(started_at AS TIMESTAMP) AS DATE) > CAST(? AS DATE)
           OR CAST(TRY_CAST(finished_at AS TIMESTAMP) AS DATE) > CAST(? AS DATE)
    ) AS runs_after_today
FROM mining_runs
"""


def _utc_today() -> date:
    """Return today's calendar day in UTC. The stored timestamps are UTC."""
    return datetime.now(UTC).date()


def check_date_ranges(con: duckdb.DuckDBPyConnection) -> list[CheckResult]:
    """Report the capture window and the commit window. WARN on a date after today.

    Mining runs supply the capture start and end. Artifacts supply the
    earliest and latest ``first_commit_at`` and ``last_commit_at``.
    A timestamp whose calendar day is after today (UTC) is WARN.
    A non-empty value that is not an ISO timestamp is WARN too.

    Inputs: a DuckDB connection, artifacts and mining_runs.
    Outputs: one check.
    """
    _register_parquet(con, "artifacts", settings.ARTIFACTS_GLOB)
    _register_parquet(con, "mining_runs", settings.MINING_RUNS_GLOB)
    today = _utc_today()
    commits = con.execute(COMMIT_DATES_SQL, [today, today]).fetchone()
    mining = con.execute(MINING_DATES_SQL, [today, today]).fetchone()
    if commits is None or mining is None:
        raise RuntimeError("date queries returned no row")
    return [_date_result(commits, mining, today)]


def _date_result(
    commits: tuple[object, ...],
    mining: tuple[object, ...],
    today: date,
) -> CheckResult:
    """Turn the two date rows into one WARN or PASS check."""
    first_min, first_max, last_min, last_max, first_unparsed, last_unparsed, after = commits
    capture_start, capture_end, start_unparsed, end_unparsed, runs_after = mining
    unparsed = int(first_unparsed) + int(last_unparsed) + int(start_unparsed) + int(end_unparsed)
    commits_after = int(after)
    runs_after_today = int(runs_after)
    missing_window = capture_start is None or capture_end is None or first_max is None
    status = PASS
    reasons: list[str] = []
    if commits_after > 0 or runs_after_today > 0:
        status = WARN
        reasons.append("a date is after today")
    if unparsed > 0:
        status = WARN
        reasons.append("a date could not be parsed")
    if missing_window:
        status = WARN
        reasons.append("a date window is missing")
    message = "; ".join(reasons) if reasons else f"dates are on or before {today.isoformat()}"
    return _result(
        "date_ranges",
        status,
        {
            "today_utc": today,
            "capture_start": capture_start,
            "capture_end": capture_end,
            "first_commit_min": first_min,
            "first_commit_max": first_max,
            "last_commit_min": last_min,
            "last_commit_max": last_max,
            "commits_after_today": commits_after,
            "runs_after_today": runs_after_today,
            "unparsed_dates": unparsed,
        },
        message,
    )


# Query Q1.8, see QUERIES.md
ARTIFACT_REPO_COVERAGE_SQL = """
SELECT
    count(DISTINCT artifacts.repo_full_name) AS artifact_repos,
    count(DISTINCT repos.full_name) AS found
FROM artifacts
LEFT JOIN (SELECT DISTINCT full_name FROM repos) AS repos
    ON artifacts.repo_full_name = repos.full_name
"""

# Query Q1.9, see QUERIES.md
WILD_HISTORY_COVERAGE_SQL = """
WITH targets AS (
    SELECT DISTINCT repo, target_path
    FROM wild_versions
    WHERE repo IS NOT NULL AND target_path IS NOT NULL
),
artifact_paths AS (
    SELECT DISTINCT repo_full_name, path
    FROM artifacts
)
SELECT
    (SELECT count(*) FROM targets) AS targets,
    count(*) AS found
FROM targets
JOIN artifact_paths
    ON targets.repo = artifact_paths.repo_full_name
   AND targets.target_path = artifact_paths.path
"""

# Query Q1.10, see QUERIES.md
MINHASH_COVERAGE_SQL = """
WITH ids AS (
    SELECT DISTINCT file_sha
    FROM minhash_ids
    WHERE file_sha IS NOT NULL AND file_sha <> ''
),
known AS (
    SELECT DISTINCT file_sha
    FROM artifacts
    WHERE file_sha IS NOT NULL
)
SELECT
    (SELECT count(*) FROM ids) AS ids,
    (SELECT count(*) FROM ids INNER JOIN known USING (file_sha)) AS found,
    (SELECT min(length(file_sha)) FROM ids) AS id_char_min,
    (SELECT max(length(file_sha)) FROM ids) AS id_char_max
"""

# Query Q1.11, see QUERIES.md
CLUSTER_COVERAGE_SQL = """
WITH cluster_ids AS (
    SELECT DISTINCT file_sha
    FROM clusters
    WHERE file_sha IS NOT NULL
),
known AS (
    SELECT DISTINCT file_sha
    FROM artifacts
    WHERE file_sha IS NOT NULL
)
SELECT
    (SELECT count(*) FROM cluster_ids) AS cluster_ids,
    count(*) AS found
FROM cluster_ids
JOIN known USING (file_sha)
"""


def _coverage_result(name: str, found: int, total: int, noun: str) -> CheckResult:
    """One join check. WARN when ``found / total`` is below 99 percent."""
    status = _coverage_status(found, total)
    share = _share(found, total)
    if total <= 0:
        message = f"no {noun} to join"
    elif status == WARN:
        message = f"{noun} coverage is below 99%"
    else:
        message = f"{noun} coverage is at least 99%"
    return _result(name, status, {"found": found, "total": total, "share": share}, message)


def _artifact_repo_coverage(con: duckdb.DuckDBPyConnection) -> CheckResult:
    """Share of artifact repos that appear in the repos table."""
    row = con.execute(ARTIFACT_REPO_COVERAGE_SQL).fetchone()
    if row is None:
        raise RuntimeError("artifact repo coverage returned no row")
    total = int(row[0])
    found = int(row[1]) if row[1] is not None else 0
    return _coverage_result("artifacts_repos_join", found, total, "artifact repos")


def _wild_history_coverage(con: duckdb.DuckDBPyConnection) -> CheckResult:
    """Share of wild_history (repo, target_path) pairs found in artifacts."""
    columns = set(_column_names(con, "wild_versions"))
    if "repo" not in columns or "target_path" not in columns:
        return _result(
            "wild_history_join",
            FAIL,
            {"columns": sorted(columns)},
            "versions.parquet needs columns repo and target_path",
        )
    row = con.execute(WILD_HISTORY_COVERAGE_SQL).fetchone()
    if row is None:
        raise RuntimeError("wild history coverage returned no row")
    total = int(row[0])
    found = int(row[1]) if row[1] is not None else 0
    return _coverage_result("wild_history_join", found, total, "wild_history paths")


def _load_minhash_ids(con: duckdb.DuckDBPyConnection, files: list[Path]) -> None:
    """Load shard id arrays into ``minhash_ids``. Signature matrices are not read."""
    con.execute("DROP VIEW IF EXISTS minhash_ids")
    con.execute("DROP TABLE IF EXISTS minhash_ids")
    con.execute("CREATE TEMP TABLE minhash_ids (file_sha VARCHAR)")
    for path in files:
        cleaned = np.char.strip(np.load(path).astype(str))
        batch = pd.DataFrame({"file_sha": cleaned})
        con.register("minhash_id_batch", batch)
        con.execute("INSERT INTO minhash_ids SELECT file_sha FROM minhash_id_batch")
        con.unregister("minhash_id_batch")


def _minhash_coverage(con: duckdb.DuckDBPyConnection) -> CheckResult:
    """Share of distinct shard ids that are a file_sha in artifacts."""
    files = _id_files(settings.MINHASH_SHARDS_DIR)
    if not files:
        return _result("minhash_id_join", WARN, {"found": 0, "total": 0}, "no shard id files")
    _load_minhash_ids(con, files)
    row = con.execute(MINHASH_COVERAGE_SQL).fetchone()
    if row is None:
        raise RuntimeError("minhash coverage returned no row")
    total = int(row[0])
    found = int(row[1]) if row[1] is not None else 0
    result = _coverage_result("minhash_id_join", found, total, "minhash ids")
    result.numbers["id_char_min"] = None if row[2] is None else int(row[2])
    result.numbers["id_char_max"] = None if row[3] is None else int(row[3])
    return result


def _cluster_coverage(con: duckdb.DuckDBPyConnection) -> CheckResult:
    """Share of cluster file_sha values found among artifact file_sha values."""
    columns = set(_column_names(con, "clusters"))
    if "file_sha" not in columns:
        return _result(
            "cluster_id_join",
            FAIL,
            {"columns": sorted(columns)},
            "clusters parquet needs a file_sha column",
        )
    row = con.execute(CLUSTER_COVERAGE_SQL).fetchone()
    if row is None:
        raise RuntimeError("cluster coverage returned no row")
    total = int(row[0])
    found = int(row[1]) if row[1] is not None else 0
    return _coverage_result("cluster_id_join", found, total, "cluster ids")


def check_join_coverage(con: duckdb.DuckDBPyConnection) -> list[CheckResult]:
    """WARN when a join finds less than 99 percent of the ids it should.

    Four joins: artifact repos in the repos table, wild_history
    ``(repo, target_path)`` in artifacts, MinHash shard ids in distinct
    ``file_sha`` (id files only, not signature matrices), and cluster
    ``file_sha`` in distinct ``file_sha``.

    Inputs: a DuckDB connection and the paths in ``settings``.
    Outputs: four checks.
    """
    _register_parquet(con, "artifacts", settings.ARTIFACTS_GLOB)
    _register_parquet(con, "repos", settings.REPOS_GLOB)
    versions = settings.WILD_HISTORY_DIR / "versions.parquet"
    _register_parquet(con, "wild_versions", str(versions))
    _register_parquet(con, "clusters", str(settings.CLUSTERS_PATH))
    return [
        _artifact_repo_coverage(con),
        _wild_history_coverage(con),
        _minhash_coverage(con),
        _cluster_coverage(con),
    ]


def _signature_shape(folder: Path) -> dict[str, object]:
    """Read the header of the first signature matrix. The values are not loaded."""
    files = _signature_files(folder)
    if not files:
        return {}
    matrix = np.load(files[0], mmap_mode="r")
    columns = int(matrix.shape[1]) if matrix.ndim == 2 else None
    return {
        "signature_file": files[0].name,
        "signature_dtype": str(matrix.dtype),
        "signature_rows": int(matrix.shape[0]),
        "signature_columns": columns,
    }


def _short_plan_value(value: object) -> object:
    """Keep a scalar plan value. A list or object is stored as its size.

    ``input_stamp`` is a list of file paths. The report keeps the length,
    not the paths.

    Example:
    >>> _short_plan_value([{"path": "a"}, {"path": "b"}])
    {'length': 2}
    >>> _short_plan_value(128)
    128
    """
    if isinstance(value, list):
        return {"length": len(value)}
    if isinstance(value, dict):
        return {"keys": sorted(str(key) for key in value)}
    return value


def check_minhash_settings() -> CheckResult:
    """PASS when plan.json uses our permutation count and shingle size.

    Every key in the file is kept, including the seed. A list or object
    value is stored as its size so a long file stamp does not fill the
    report. A different ``num_perm`` or ``shingle_words`` is FAIL. A
    different seed is WARN: the sketches would not match ours, but the
    two size fields still match. The first signature file's dtype and
    width are recorded from its header.

    Inputs: ``settings.MINHASH_SHARDS_DIR / plan.json``.
    Outputs: one check.
    """
    path = settings.MINHASH_SHARDS_DIR / "plan.json"
    if not path.is_file():
        return _result(
            "minhash_settings",
            FAIL,
            {},
            f"missing {path} (build with: {DEDUP_BUILDER})",
        )
    plan = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict):
        return _result("minhash_settings", FAIL, {}, "plan.json is not an object")
    numbers: dict[str, object] = {
        str(key): _short_plan_value(value) for key, value in plan.items()
    }
    numbers.update(_signature_shape(settings.MINHASH_SHARDS_DIR))
    perm_ok = plan.get("num_perm") == settings.MINHASH_NUM_PERM
    shingle_ok = plan.get("shingle_words") == settings.SHINGLE_WORDS
    seed = plan.get("seed")
    seed_ok = seed == settings.MINHASH_SEED
    numbers["seed_matches"] = seed_ok
    width = numbers.get("signature_columns")
    width_ok = width is None or width == settings.MINHASH_NUM_PERM
    if not perm_ok or not shingle_ok:
        return _result(
            "minhash_settings",
            FAIL,
            numbers,
            "num_perm or shingle_words does not match settings",
        )
    if not seed_ok or not width_ok:
        return _result(
            "minhash_settings",
            WARN,
            numbers,
            "plan size matches; seed or signature width does not",
        )
    return _result(
        "minhash_settings",
        PASS,
        numbers,
        "num_perm and shingle_words match settings",
    )


# Query Q1.12, see QUERIES.md
REGISTRY_PREVIEW_SQL = """
WITH per_repo AS (
    SELECT
        repo_full_name,
        count(DISTINCT file_sha) AS skills
    FROM artifacts
    WHERE file_sha IS NOT NULL
    GROUP BY repo_full_name
),
registries AS (
    SELECT per_repo.repo_full_name
    FROM per_repo
    JOIN repos ON per_repo.repo_full_name = repos.full_name
    WHERE repos.stars >= ?
      AND repos.is_fork = 0
      AND per_repo.skills >= ?
)
SELECT
    (SELECT count(*) FROM registries) AS repos,
    count(DISTINCT artifacts.file_sha) AS skills,
    count(*) AS files
FROM artifacts
JOIN registries ON artifacts.repo_full_name = registries.repo_full_name
"""


def preview_registry_rule(con: duckdb.DuckDBPyConnection) -> CheckResult:
    """Count repos that pass the registry rule before the English filter.

    A repo needs ``stars >= MIN_REGISTRY_STARS``, ``is_fork = 0``, and at
    least ``MIN_REGISTRY_ENGLISH_SKILLS`` distinct ``file_sha``. Language
    is not applied yet. ``skills`` is the distinct texts inside those repos.
    ``files`` is the artifact rows. Zero repos is WARN.

    Inputs: a DuckDB connection, artifacts and repos.
    Outputs: one check.
    """
    _register_parquet(con, "artifacts", settings.ARTIFACTS_GLOB)
    _register_parquet(con, "repos", settings.REPOS_GLOB)
    row = con.execute(
        REGISTRY_PREVIEW_SQL,
        [settings.MIN_REGISTRY_STARS, settings.MIN_REGISTRY_ENGLISH_SKILLS],
    ).fetchone()
    if row is None:
        raise RuntimeError("registry preview returned no row")
    repos = int(row[0])
    skills = int(row[1]) if row[1] is not None else 0
    files = int(row[2]) if row[2] is not None else 0
    status = WARN if repos == 0 else PASS
    message = (
        "no repo passes the registry rule before the English filter"
        if repos == 0
        else "registry rule before the English filter"
    )
    return _result(
        "registry_rule_preview",
        status,
        {
            "repos": repos,
            "skills": skills,
            "files": files,
            "min_stars": settings.MIN_REGISTRY_STARS,
            "min_skills": settings.MIN_REGISTRY_ENGLISH_SKILLS,
        },
        message,
    )


# Query Q1.13, see QUERIES.md
LANGUAGE_SAMPLE_SQL = """
WITH distinct_sha AS (
    SELECT file_sha
    FROM artifacts
    WHERE file_sha IS NOT NULL AND content IS NOT NULL
    GROUP BY file_sha
),
sampled AS (
    SELECT file_sha
    FROM distinct_sha
    ORDER BY hash(file_sha || '{seed}')
    LIMIT {sample_size}
)
SELECT sampled.file_sha, any_value(artifacts.content) AS content
FROM artifacts
JOIN sampled ON artifacts.file_sha = sampled.file_sha
WHERE artifacts.content IS NOT NULL
GROUP BY sampled.file_sha
"""


def _language_result(rows: list[tuple[object, ...]], sample_size: int) -> CheckResult:
    """Count language labels. ``unknown`` counts as English for the share we drop."""
    counts: Counter[str] = Counter()
    for row in rows:
        content = row[1] if isinstance(row[1], str) else ""
        counts[detect_language(normalize_text(content))] += 1
    total = sum(counts.values())
    if total == 0:
        return _result(
            "non_english_share",
            WARN,
            {"sampled": 0, "requested_sample_size": sample_size},
            "no texts to label",
        )
    dropped = sum(count for code, count in counts.items() if code not in {"en", "unknown"})
    shares = {
        code: round(count / total, 6)
        for code, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    }
    return _result(
        "non_english_share",
        PASS,
        {
            "sampled": total,
            "requested_sample_size": sample_size,
            "seed": settings.RANDOM_SEED,
            "non_english_share": round(dropped / total, 6),
            "english_share": shares.get("en", 0.0),
            "unknown_share": shares.get("unknown", 0.0),
            "share_by_language": shares,
        },
        "unknown counts as English; non_english_share is what the filter drops",
    )


def preview_non_english_share(
    con: duckdb.DuckDBPyConnection,
    sample_size: int = 5000,
) -> CheckResult:
    """Label a seeded sample of distinct texts and report the share of each language.

    The sample is the first ``sample_size`` distinct ``file_sha`` values in
    order of ``hash(file_sha || RANDOM_SEED)``. A reservoir sample changed
    between runs when DuckDB used more than one thread, so the hash order
    keeps the same seed on the same texts. Each text is normalised and then
    passed to ``detect_language`` (algorithm A8). ``unknown`` counts as
    English, so ``non_english_share`` is the share the English filter drops.

    Inputs: a DuckDB connection, artifacts, and ``sample_size`` (default 5000).
    Outputs: one check. An empty sample is WARN.
    """
    _register_parquet(con, "artifacts", settings.ARTIFACTS_GLOB)
    if sample_size < 1:
        return _result(
            "non_english_share",
            WARN,
            {"requested_sample_size": sample_size},
            "sample_size must be at least 1",
        )
    sql = LANGUAGE_SAMPLE_SQL.format(
        sample_size=int(sample_size),
        seed=int(settings.RANDOM_SEED),
    )
    rows = con.execute(sql).fetchall()
    return _language_result(rows, sample_size)


# Query Q1.14, see QUERIES.md
COPY_DOWNLOAD_SQL = """
WITH per_repo AS (
    SELECT repo_full_name, count(DISTINCT file_sha) AS skills
    FROM artifacts
    WHERE file_sha IS NOT NULL
    GROUP BY repo_full_name
),
registries AS (
    SELECT per_repo.repo_full_name
    FROM per_repo
    JOIN repos ON per_repo.repo_full_name = repos.full_name
    WHERE repos.stars >= ?
      AND repos.is_fork = 0
      AND per_repo.skills >= ?
),
registry_shas AS (
    SELECT DISTINCT artifacts.file_sha AS file_sha
    FROM artifacts
    JOIN registries ON artifacts.repo_full_name = registries.repo_full_name
    WHERE artifacts.file_sha IS NOT NULL
),
copies AS (
    SELECT artifacts.repo_full_name, artifacts.path
    FROM artifacts
    JOIN registry_shas ON artifacts.file_sha = registry_shas.file_sha
    WHERE artifacts.repo_full_name NOT IN (SELECT repo_full_name FROM registries)
),
copy_paths AS (
    SELECT DISTINCT repo_full_name, path
    FROM copies
),
wild_paths AS (
    SELECT DISTINCT repo, target_path
    FROM wild_versions
),
marked AS (
    SELECT
        copy_paths.repo_full_name,
        copy_paths.path,
        wild_paths.repo IS NOT NULL AS has_history
    FROM copy_paths
    LEFT JOIN wild_paths
        ON copy_paths.repo_full_name = wild_paths.repo
       AND copy_paths.path = wild_paths.target_path
)
SELECT
    (SELECT count(*) FROM copies) AS copy_files,
    (SELECT count(*) FROM copy_paths) AS copy_paths,
    count(DISTINCT repo_full_name) AS copy_repos,
    count(*) FILTER (WHERE has_history) AS paths_with_wild_history,
    count(DISTINCT repo_full_name) FILTER (WHERE NOT has_history) AS repos_to_clone
FROM marked
"""


def preview_copy_download_size(con: duckdb.DuckDBPyConnection) -> CheckResult:
    """Count exact copies outside candidate registries. This is a lower bound.

    A copy is an artifact row whose ``file_sha`` also appears inside a
    candidate registry, and whose repo is not itself a candidate. Near
    copies are not included. ``paths_with_wild_history`` is how many of
    those ``(repo, path)`` pairs already occur in wild_history versions.
    ``repos_to_clone`` is distinct repos that still have at least one of
    those paths missing from wild_history.

    Inputs: a DuckDB connection, artifacts, repos and wild_history versions.
    Outputs: one check.
    """
    _register_parquet(con, "artifacts", settings.ARTIFACTS_GLOB)
    _register_parquet(con, "repos", settings.REPOS_GLOB)
    versions = settings.WILD_HISTORY_DIR / "versions.parquet"
    _register_parquet(con, "wild_versions", str(versions))
    row = con.execute(
        COPY_DOWNLOAD_SQL,
        [settings.MIN_REGISTRY_STARS, settings.MIN_REGISTRY_ENGLISH_SKILLS],
    ).fetchone()
    if row is None:
        raise RuntimeError("copy download preview returned no row")
    # An empty marked set still returns one row of zeros, except DISTINCT
    # counts on an empty group, which are 0. Guard Nones anyway.
    values = [0 if item is None else int(item) for item in row]
    return _result(
        "copy_download_size",
        PASS,
        {
            "copy_files": values[0],
            "copy_paths": values[1],
            "copy_repos": values[2],
            "paths_with_wild_history": values[3],
            "repos_to_clone": values[4],
        },
        "exact file_sha copies only; near copies are not included",
    )


def save_report(results: list[CheckResult], path: Path) -> None:
    """Write the checks and the run date as JSON.

    The file is an object with ``run_date`` (UTC) and ``checks``, a list of
    ``name``, ``status``, ``numbers`` and ``message``. The parent folder is
    created if it is missing.

    Inputs: the check list and the destination path.
    Outputs: that JSON file.
    """
    payload = {
        "run_date": datetime.now(UTC).isoformat(),
        "checks": [
            {
                "name": item.name,
                "status": item.status,
                "numbers": _plain(item.numbers),
                "message": item.message,
            }
            for item in results
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def save_outputs(results: list[CheckResult], path: Path) -> None:
    """Write the stage output. Stage 1 has one file, the JSON report.

    Inputs: the check list and the report path.
    Outputs: the file written by ``save_report``.
    """
    save_report(results, path)


def _has_fail(results: list[CheckResult]) -> bool:
    """True when any check is FAIL."""
    return any(item.status == FAIL for item in results)


def _brief(numbers: dict[str, object]) -> str:
    """One short cell of key numbers for the printed table."""
    parts: list[str] = []
    for key, value in numbers.items():
        if isinstance(value, dict):
            shown = ", ".join(f"{name}:{value[name]}" for name in list(value)[:4])
            parts.append(f"{key}={{{shown}}}")
        elif isinstance(value, list):
            shown = ", ".join(str(item) for item in value[:8])
            if len(value) > 8:
                shown += ", ..."
            parts.append(f"{key}=[{shown}]")
        else:
            parts.append(f"{key}={value}")
    text = ", ".join(parts)
    if len(text) > 180:
        return text[:177] + "..."
    return text


def _print_table(results: list[CheckResult]) -> None:
    """Print ``status | check | key numbers`` for every check."""
    print(f"{'status':<6} | {'check':<32} | key numbers")
    for item in results:
        print(f"{item.status:<6} | {item.name:<32} | {_brief(item.numbers)}")


def _results_frame(results: list[CheckResult]) -> pd.DataFrame:
    """A small frame so the stage can print two sample report rows."""
    return pd.DataFrame(
        {
            "name": [item.name for item in results],
            "status": [item.status for item in results],
            "message": [item.message for item in results],
            "numbers": [json.dumps(item.numbers) for item in results],
        }
    )


def _tally(results: list[CheckResult]) -> dict[str, object]:
    """Count PASS, WARN and FAIL."""
    return {
        "checks": len(results),
        "pass": sum(item.status == PASS for item in results),
        "warn": sum(item.status == WARN for item in results),
        "fail": sum(item.status == FAIL for item in results),
    }


def _sample_results(results: list[CheckResult]) -> list[CheckResult]:
    """Return the registry preview and the language sample, when both exist.

    Those two rows show what the report holds. A run that stopped early
    prints its first two rows instead.
    """
    by_name = {item.name: item for item in results}
    chosen = [
        by_name[name]
        for name in ("registry_rule_preview", "non_english_share")
        if name in by_name
    ]
    if len(chosen) == 2:
        return chosen
    return results[:2]


def _finish(results: list[CheckResult], path: Path, elapsed: float) -> int:
    """Print the table and two sample rows, save the report, and return the exit code."""
    _print_table(results)
    summary = _tally(results)
    summary["elapsed_seconds"] = round(elapsed, 1)
    summary["report"] = str(path)
    print_summary("check_data", summary)
    print_sample_records(_results_frame(_sample_results(results)), "Sample report entries", 2)
    save_outputs(results, path)
    return 1 if _has_fail(results) else 0


def _exit_from_saved_report(path: Path) -> int:
    """Return 1 when the saved report contains a FAIL, otherwise 0."""
    print(f"skip: {path} already exists (pass --force to check again)")
    saved = json.loads(path.read_text(encoding="utf-8"))
    checks = saved.get("checks", [])
    failed = any(item.get("status") == FAIL for item in checks)
    return 1 if failed else 0


def main() -> int:
    """Run the ten checks, write the report, and return 1 when any check is FAIL.

    ``--dev`` changes only the folder of ``data_check_report.json``.
    ``--force`` rebuilds that file when it already exists. Without
    ``--force``, a saved report is kept and its FAIL/PASS is the exit code.
    A FAIL stops the later steps so a missing file does not crash the SQL.

    Inputs: command-line flags and the paths in ``settings``.
    Outputs: the JSON report and an exit code.
    """
    parser = build_arg_parser("Check genealogy inputs, columns, joins and size previews.")
    args = parser.parse_args()
    report_path = settings.data_dir(args.dev) / "data_check_report.json"
    if outputs_exist([report_path]) and not args.force:
        return _exit_from_saved_report(report_path)
    started = time.perf_counter()
    con = connect_duckdb()
    try:
        # Step 1: Files exist
        print("Step 1: Files exist", flush=True)
        results = check_files_exist()
        if not _has_fail(results):
            read_inputs(con)
        # Step 2: Required columns
        if not _has_fail(results):
            print("Step 2: Required columns", flush=True)
            results.extend(check_required_columns(con))
        # Step 3: Counts and nulls
        if not _has_fail(results):
            print("Step 3: Counts and nulls", flush=True)
            results.extend(check_counts_and_nulls(con))
        # Step 4: Date ranges
        if not _has_fail(results):
            print("Step 4: Date ranges", flush=True)
            results.extend(check_date_ranges(con))
        # Step 5: Join coverage
        if not _has_fail(results):
            print("Step 5: Join coverage", flush=True)
            results.extend(check_join_coverage(con))
        # Step 6: MinHash settings
        if not _has_fail(results):
            print("Step 6: MinHash settings", flush=True)
            results.append(check_minhash_settings())
        # Step 7: Registry rule preview
        if not _has_fail(results):
            print("Step 7: Registry rule preview", flush=True)
            results.append(preview_registry_rule(con))
        # Step 8: Non-English share
        if not _has_fail(results):
            print("Step 8: Non-English share", flush=True)
            results.append(preview_non_english_share(con))
        # Step 9: Copy download preview
        if not _has_fail(results):
            print("Step 9: Copy download preview", flush=True)
            results.append(preview_copy_download_size(con))
    finally:
        con.close()
    # Step 10: Print a table, save the report, exit 1 on any FAIL
    print("Step 10: Print a table, save the report, exit 1 on any FAIL", flush=True)
    return _finish(results, report_path, time.perf_counter() - started)


if __name__ == "__main__":
    raise SystemExit(main())
