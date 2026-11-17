"""Download the git history of every dataset skill folder that copied a registry.

What: read the copy files of stage 5c, turn them into one target per
(repo, skill folder), clone each copy repo, and keep the file history of
those folders only. Each repo is saved in one DuckDB transaction, so a later
run skips a repo that already finished. This is the only source of copy
history (the main pipeline's ``wild_history`` is not used).
Inputs: ``copy_candidates.parquet`` in the dev or full data folder.
Outputs: ``copied_skill_history/`` beside ``registry_history/``: the store
``copied_skill_history.duckdb``, ``download_targets.parquet`` and three
parquet files (changes, texts, status) with the stage 3 columns.
Run:
    python -m skill_genealogy.download_copied_skill_history --dev --limit 5
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from skill_genealogy.file_helpers import (
    build_arg_parser,
    check_inputs_exist,
    outputs_exist,
    print_sample_records,
    print_summary,
    read_parquet,
    write_parquet,
)
from skill_genealogy.git_history import RepoResult, mine_one_repo
from skill_genealogy.settings import (
    COPY_HISTORY_CONFIRM_ABOVE_REPOS,
    COPY_HISTORY_MAX_FOLDERS,
    DOWNLOAD_TIMEOUT_SECONDS,
    DOWNLOAD_WORKERS,
    DUCKDB_TEMP_LIMIT,
    HISTORY_DUCKDB_MEMORY_LIMIT,
    TEXT_ROW_GROUP,
    data_dir,
)

# A repo that is done. ``empty`` has no commits and ``too_large`` is over
# COPY_HISTORY_MAX_FOLDERS, so mining either again adds nothing (A19).
_FINISHED_STATUS = frozenset({"ok", "gone", "empty", "too_large"})
# These two are mined again only with --retry-failed.
_RETRY_STATUS = frozenset({"timeout", "error"})
_STATUS_NAMES = ("ok", "gone", "empty", "too_large", "timeout", "error")
_TARGET_COLUMNS = ["repo", "skill_folder"]
_STATUS_COLUMNS = [
    "repo",
    "status",
    "message",
    "head_commit",
    "seconds",
    "follow_renames",
    "not_ours",
    "after_copy",
    "twin",
    "skill_folders",
    "changes",
]
# Same columns and order as the stage 3 changes table (git_history._CHANGE_COLUMNS).
_CHANGE_COLUMNS = [
    "repo",
    "skill_folder",
    "file_path",
    "commit",
    "commit_date",
    "author_key",
    "subject",
    "status",
    "old_file_path",
    "old_sha",
    "new_sha",
    "old_mode",
    "new_mode",
    "is_skill_md",
    "is_script",
    "commit_seq",
    "repo_path",
    "old_repo_path",
    "history_role",
]
_NON_TEXT_CHANGE_COLUMNS = frozenset({"is_skill_md", "is_script", "commit_seq"})

# Query Q6.1, see QUERIES.md
_CREATE_STORE_SQL = (
    """
    CREATE TABLE IF NOT EXISTS changes (
        repo VARCHAR, skill_folder VARCHAR, file_path VARCHAR, "commit" VARCHAR,
        commit_date VARCHAR, author_key VARCHAR, subject VARCHAR, status VARCHAR,
        old_file_path VARCHAR, old_sha VARCHAR, new_sha VARCHAR, old_mode VARCHAR,
        new_mode VARCHAR, is_skill_md BOOLEAN, is_script BOOLEAN, commit_seq BIGINT,
        repo_path VARCHAR, old_repo_path VARCHAR, history_role VARCHAR
    )
    """,
    "CREATE TABLE IF NOT EXISTS texts (sha VARCHAR PRIMARY KEY, text VARCHAR)",
    """
    CREATE TABLE IF NOT EXISTS status (
        repo VARCHAR PRIMARY KEY, status VARCHAR, message VARCHAR,
        head_commit VARCHAR, seconds DOUBLE, follow_renames BOOLEAN,
        not_ours BIGINT, after_copy BIGINT, twin BIGINT,
        skill_folders BIGINT, changes BIGINT
    )
    """,
    "CREATE INDEX IF NOT EXISTS changes_by_repo ON changes(repo)",
)

# Query Q6.2, see QUERIES.md
_EXPORT_CHANGES_SQL = """
COPY (
    SELECT * FROM changes
    ORDER BY repo, skill_folder, commit_seq, file_path, status, new_sha
) TO {path} (FORMAT PARQUET, COMPRESSION ZSTD)
"""

# Query Q6.3, see QUERIES.md. The full run's 7.9 GB of text cannot be sorted at
# once under the memory limit, so it is sorted in 16 sha ranges (about one
# sixteenth each, split at the hex digits). The ranges have no gaps, so every
# sha falls in one, and the ranges in order are the whole table sorted.
_KEEP_SHA_SQL = """
CREATE OR REPLACE TEMP TABLE keep_sha AS
SELECT new_sha AS sha FROM changes UNION SELECT old_sha FROM changes
"""
_EXPORT_TEXTS_PART_SQL = """
SELECT sha, text FROM texts
WHERE sha >= ? AND (? IS NULL OR sha < ?) AND sha IN (SELECT sha FROM keep_sha)
ORDER BY sha
"""
_SHA_SPLITS = ["", *"123456789abcdef", None]

# Query Q6.4, see QUERIES.md
_EXPORT_STATUS_SQL = """
COPY (SELECT * FROM status ORDER BY repo) TO {path} (FORMAT PARQUET, COMPRESSION ZSTD)
"""

# Query Q6.5, see QUERIES.md
_INSERT_TEXTS_SQL = """
INSERT INTO texts
SELECT incoming_texts.sha, any_value(incoming_texts.text)
FROM incoming_texts
ANTI JOIN texts USING (sha)
GROUP BY incoming_texts.sha
"""

# Query Q6.6, see QUERIES.md
# A version is the folder's own SKILL.md, as in stage 4a: a nested
# ``x/SKILL.md`` is another skill, and ``copy_source`` rows are the source's.
_VERSIONS_SQL = """
SELECT t.repo, t.skill_folder,
       count(DISTINCT CASE
           WHEN lower(c.file_path) = 'skill.md' AND c.status <> 'D'
                AND c.history_role IN ('own', 'former_folder') THEN c.new_sha
       END) AS versions
FROM report_targets AS t
JOIN status AS s ON s.repo = t.repo AND s.status = 'ok'
LEFT JOIN changes AS c ON c.repo = t.repo AND c.skill_folder = t.skill_folder
GROUP BY t.repo, t.skill_folder
"""

# Query Q6.7, see QUERIES.md
# One statement per total: a single statement with all of them does not fit
# under the memory limit on 12 million change rows.
_REPORT_CHANGES_SQL = """
CREATE OR REPLACE TEMP TABLE report_changes AS
SELECT repo, skill_folder, "commit", file_path, status, commit_date, new_sha, old_sha
FROM changes WHERE repo IN (SELECT repo FROM report_targets)
"""
_STORE_TOTALS_SQL = (
    "SELECT count(*) FROM report_changes",
    """
    SELECT count(*), coalesce(sum(octet_length(encode(text))), 0) FROM texts
    WHERE sha IN (SELECT new_sha FROM report_changes UNION SELECT old_sha FROM report_changes)
    """,
    """
    SELECT
        strftime(min(timezone('UTC', CAST(commit_date AS TIMESTAMPTZ))), '%Y-%m-%dT%H:%M:%SZ'),
        strftime(max(timezone('UTC', CAST(commit_date AS TIMESTAMPTZ))), '%Y-%m-%dT%H:%M:%SZ')
    FROM report_changes
    """,
    """
    SELECT count(*) FROM (
        SELECT 1 FROM report_changes
        GROUP BY repo, skill_folder, "commit", file_path, status HAVING count(*) > 1
    )
    """,
)

_TOTAL_NAMES = (
    "change_rows",
    "text_rows",
    "text_bytes",
    "first_commit_utc",
    "last_commit_utc",
    "duplicate_change_keys",
)

# Query Q6.8, see QUERIES.md
_STATUS_BY_REPO_SQL = "SELECT repo, status FROM status"

# Two rows each for the printed sample (not saved queries).
_SAMPLE_CHANGES_SQL = """
SELECT * FROM changes WHERE repo IN (SELECT repo FROM report_targets)
ORDER BY repo, skill_folder, commit_seq, file_path LIMIT 2
"""
_SAMPLE_STATUS_SQL = """
SELECT * FROM status WHERE repo IN (SELECT repo FROM report_targets) ORDER BY repo LIMIT 2
"""


def read_inputs(dev: bool) -> pd.DataFrame:
    """Read ``repo`` and ``path`` of every copy file from stage 5c.

    Inputs: ``dev`` chooses the data folder.
    Outputs: a frame with ``repo`` and ``path``. A missing table stops the
    run and names the stage 5c command.
    """
    path = data_dir(dev) / "copy_candidates.parquet"
    command = "python -m skill_genealogy.find_copies --only copies"
    if dev:
        command = "python -m skill_genealogy.find_copies --dev --only copies"
    check_inputs_exist({"copy_candidates": path}, {"copy_candidates": command})
    return pd.read_parquet(path, columns=["repo", "path"])


def pick_download_targets(dev: bool) -> pd.DataFrame:
    """Return one row per (repo, skill folder) of every copy file.

    Inputs: ``dev`` chooses the data folder.
    Outputs: ``repo``, ``skill_folder``, sorted, one row per pair.
    """
    return targets_from_copies(read_inputs(dev))


# Algorithm A29, see code_flow/12_copied_skill_history.md
def targets_from_copies(copies: pd.DataFrame) -> pd.DataFrame:
    """Collapse copy files to one target per (repo, skill folder).

    The skill folder is the parent folder of the file path; a file at the
    repo root has folder ``.``. Two files in one folder are one target.
    Every copy file is a target: stage 5c links copies to source lineages
    only, so copier repos are never in the table.

    Example:
    >>> copies = pd.DataFrame({
    ...     "repo": ["a/r", "a/r", "a/r", "b/r"],
    ...     "path": ["skills/pdf/SKILL.md", "skills/pdf/skill.md",
    ...              "skills/other/SKILL.md", "SKILL.md"],
    ... })
    >>> targets_from_copies(copies).values.tolist()
    [['a/r', 'skills/other'], ['a/r', 'skills/pdf'], ['b/r', '.']]
    """
    if copies.empty:
        return pd.DataFrame(columns=_TARGET_COLUMNS)
    targets = pd.DataFrame(
        {
            "repo": copies["repo"].astype(str),
            "skill_folder": copies["path"].map(skill_folder_of),
        }
    )
    targets = targets[targets["repo"].str.len() > 0].drop_duplicates()
    targets = targets.sort_values(_TARGET_COLUMNS, kind="mergesort")
    return targets.reset_index(drop=True)


def skill_folder_of(path: object) -> str:
    """Parent folder of a dataset file path. A root file is ``.``.

    Example:
    >>> skill_folder_of("skills/pdf/SKILL.md")
    'skills/pdf'
    >>> skill_folder_of("SKILL.md")
    '.'
    """
    text = str(path or "").replace("\\", "/").strip("/")
    parent = str(PurePosixPath(text).parent) if "/" in text else "."
    return parent or "."


def group_targets_by_repo(targets: pd.DataFrame) -> dict[str, list[str]]:
    """Map each repo to its sorted skill folders, without duplicates.

    Inputs: the frame from ``pick_download_targets``.
    Outputs: ``owner/repo`` to a list of folders, in repo order.

    Example:
    >>> frame = pd.DataFrame({"repo": ["b/r", "a/r", "a/r", "a/r"],
    ...                       "skill_folder": ["z", "m", "a", "a"]})
    >>> group_targets_by_repo(frame)
    {'a/r': ['a', 'm'], 'b/r': ['z']}
    """
    grouped: dict[str, list[str]] = {}
    ordered = targets.drop_duplicates(_TARGET_COLUMNS).sort_values(_TARGET_COLUMNS)
    for repo, folder in zip(ordered["repo"], ordered["skill_folder"], strict=True):
        grouped.setdefault(str(repo), []).append(str(folder))
    return grouped


# Algorithm A19, see code_flow/04_registry_history.md
def needs_mine(status: str | None, retry_failed: bool) -> bool:
    """True when a repo with this store status should be cloned.

    None means the repo is not in the store yet. ``ok``, ``gone``, ``empty``
    and ``too_large`` are done; ``timeout`` and ``error`` only with
    ``retry_failed``.

    Example:
    >>> [needs_mine(s, False) for s in (None, "ok", "gone", "empty", "too_large", "timeout")]
    [True, False, False, False, False, False]
    >>> needs_mine("error", True)
    True
    """
    if status is None:
        return True
    if status in _RETRY_STATUS:
        return retry_failed
    return status not in _FINISHED_STATUS


def repos_still_to_mine(
    targets_by_repo: dict[str, list[str]],
    connection: duckdb.DuckDBPyConnection,
    retry_failed: bool,
) -> dict[str, list[str]]:
    """Drop repos whose store status is finished (``needs_mine``).

    Inputs: the grouped targets, an open store, the ``--retry-failed`` flag.
    Outputs: the same map without the finished repos, in the same order.
    """
    known = dict(connection.execute(_STATUS_BY_REPO_SQL).fetchall())
    return {
        repo: folders
        for repo, folders in targets_by_repo.items()
        if needs_mine(known.get(repo), retry_failed)
    }


def open_history_store(path: Path) -> duckdb.DuckDBPyConnection:
    """Open (or create) the copied-skill store with its three tables.

    The store replaces stage 3's shard folder: each repo is one transaction,
    so a crash leaves either the whole repo or nothing. DuckDB spills to
    ``duckdb_tmp`` beside the store above ``HISTORY_DUCKDB_MEMORY_LIMIT``.

    Inputs: ``path`` of the ``.duckdb`` file.
    Outputs: an open connection; the caller closes it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = path.parent / "duckdb_tmp"
    temp_dir.mkdir(exist_ok=True)
    connection = duckdb.connect(str(path))
    connection.execute("SET preserve_insertion_order = false")
    connection.execute(f"SET memory_limit = '{HISTORY_DUCKDB_MEMORY_LIMIT}'")
    connection.execute(f"SET max_temp_directory_size = '{DUCKDB_TEMP_LIMIT}'")
    connection.execute(f"SET temp_directory = {_sql_quote(str(temp_dir))}")
    for statement in _CREATE_STORE_SQL:
        connection.execute(statement)
    return connection


def save_repo_to_store(
    connection: duckdb.DuckDBPyConnection,
    result: RepoResult,
    changes: pd.DataFrame,
    texts: pd.DataFrame,
) -> None:
    """Replace one repo's change rows and status, and add its new texts.

    One transaction: on any failure it rolls back and raises, so the repo
    has no status row and is mined again next run. A text is inserted only
    when its blob sha is not stored yet (texts are shared between repos).

    Inputs: an open store, the ``RepoResult`` and the two frames from
    ``mine_one_repo``. Empty frames still write the status row.
    Outputs: none.
    """
    changes = _prepare_changes(changes)
    texts = _prepare_texts(texts)
    status_row = _status_row(result, changes)
    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute("DELETE FROM changes WHERE repo = ?", [result.repo])
        connection.execute("DELETE FROM status WHERE repo = ?", [result.repo])
        if len(changes):
            connection.register("incoming_changes", changes)
            connection.execute("INSERT INTO changes SELECT * FROM incoming_changes")
        if len(texts):
            connection.register("incoming_texts", texts)
            connection.execute(_INSERT_TEXTS_SQL)
        placeholders = ", ".join("?" for _ in _STATUS_COLUMNS)
        connection.execute(f"INSERT INTO status VALUES ({placeholders})", status_row)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        for name in ("incoming_changes", "incoming_texts"):
            _unregister(connection, name)


def export_history_store(connection: duckdb.DuckDBPyConnection, out_dir: Path) -> None:
    """Write the changes, texts and status parquet files from the store.

    Texts are only the blobs a change row names, one row per sha.
    Inputs: an open store and the ``copied_skill_history`` folder.
    Outputs: the three parquet files in ``out_dir``.
    """
    changes_path, texts_path, status_path = _output_paths(out_dir)
    for sql, path in (
        (_EXPORT_CHANGES_SQL, changes_path),
        (_EXPORT_STATUS_SQL, status_path),
    ):
        path.unlink(missing_ok=True)
        connection.execute(sql.format(path=_sql_quote(str(path))))
    _export_texts(connection, texts_path)


def _export_texts(connection: duckdb.DuckDBPyConnection, path: Path) -> None:
    """Write the texts a change row names, sorted by sha, one sha range at a time.

    Each range is sorted in DuckDB and streamed into one parquet writer, so
    memory holds one range, not the whole table. A failed export leaves no file.
    """
    tmp = path.with_name(path.stem + ".partial.parquet")
    tmp.unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    connection.execute(_KEEP_SHA_SQL)
    schema = pa.schema([("sha", pa.string()), ("text", pa.string())])
    try:
        with pq.ParquetWriter(tmp, schema, compression="zstd") as writer:
            for low, high in zip(_SHA_SPLITS[:-1], _SHA_SPLITS[1:], strict=True):
                part = connection.sql(_EXPORT_TEXTS_PART_SQL, params=[low, high, high])
                for batch in part.to_arrow_reader(TEXT_ROW_GROUP):
                    writer.write_batch(batch.cast(schema), row_group_size=TEXT_ROW_GROUP)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        connection.execute("DROP TABLE IF EXISTS keep_sha")
    tmp.replace(path)


def _repo_work_dir(work_dir: Path, repo: str) -> Path:
    """A clone folder of its own for ``repo``, named by a hash of the exact name.

    The dataset can list one GitHub repo under two spellings (``Owner/Repo`` and
    ``owner/repo``). On a case-insensitive disk both would clone into one folder
    at the same time and both fail. A hash of the exact name keeps them apart.

    >>> _repo_work_dir(Path("w"), "a/B") != _repo_work_dir(Path("w"), "a/b")
    True
    """
    return work_dir / hashlib.sha1(repo.encode()).hexdigest()[:16]


def _mine_in_own_dir(
    repo: str, work_dir: Path, folders: list[str], timeout_seconds: int, url: str | None
) -> tuple[RepoResult, pd.DataFrame, pd.DataFrame]:
    """Run ``mine_one_repo`` in the repo's own folder, then delete that folder."""
    own_dir = _repo_work_dir(work_dir, repo)
    try:
        return mine_one_repo(repo, own_dir, folders, timeout_seconds, url)
    finally:
        shutil.rmtree(own_dir, ignore_errors=True)


def mine_copied_skills(
    targets_by_repo: dict[str, list[str]],
    con: duckdb.DuckDBPyConnection,
    work_dir: Path,
    workers: int,
    *,
    timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS,
    urls: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Clone each repo for its skill folders and save it to the store.

    Git runs in ``workers`` threads (each clone is a subprocess); the caller
    thread saves every finished repo, so DuckDB is used by one thread only.
    A repo with more than ``COPY_HISTORY_MAX_FOLDERS`` folders is not cloned
    and is saved as ``too_large``. One line per repo:
    ``repo status seconds changes``.

    Inputs: ``targets_by_repo`` (``owner/repo`` to folders), an open store,
    ``work_dir`` for the temporary clones, the pool size, the per-command git
    timeout, and ``urls`` (tests pass ``file://`` addresses; a missing repo
    uses GitHub).
    Outputs: one status row per repo, sorted by repo.
    """
    url_map = urls or {}
    clone, too_large = _split_too_large(targets_by_repo)
    rows = [_save_and_log(con, _too_large_result(repo, n), None, None) for repo, n in too_large]
    if clone:
        work_dir.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(clone)))) as pool:
            futures = {
                pool.submit(
                    _mine_in_own_dir,
                    repo,
                    work_dir,
                    folders,
                    timeout_seconds,
                    url_map.get(repo),
                ): repo
                for repo, folders in clone.items()
            }
            for future in as_completed(futures):
                repo = futures.pop(future)
                result, changes, texts = _future_result(repo, future)
                rows.append(_save_and_log(con, result, changes, texts))
    if not rows:
        return pd.DataFrame(columns=_STATUS_COLUMNS)
    return pd.DataFrame(rows, columns=_STATUS_COLUMNS).sort_values("repo").reset_index(drop=True)


def save_outputs(con: duckdb.DuckDBPyConnection, targets: pd.DataFrame, history: Path) -> None:
    """Write ``download_targets.parquet`` and export the store to parquet.

    Inputs: an open store, this run's targets, the ``copied_skill_history`` folder.
    Outputs: four parquet files in ``history``.
    """
    write_parquet(targets, _targets_path(history), sort_by=_TARGET_COLUMNS)
    export_history_store(con, history)


def main() -> None:
    """Run stage 6: pick targets, clone pending repos, export, summarise.

    Flags: ``--dev``, ``--force`` (export again), ``--limit N`` (first N
    repos), ``--retry-failed``, ``--merge-only`` (export only, no clone),
    ``--timeout`` (seconds per git command), ``--yes`` (allow a full run
    above ``COPY_HISTORY_CONFIRM_ABOVE_REPOS`` pending repos).
    """
    args = _parser().parse_args()
    # Step 1: Pick download targets (A29)
    targets = pick_download_targets(args.dev)
    # Step 2: Group by repo and skip repos already finished (A19)
    targets_by_repo = _limit_repos(group_targets_by_repo(targets), args.limit)
    targets = targets[targets["repo"].isin(targets_by_repo)].reset_index(drop=True)
    history = data_dir(args.dev) / "copied_skill_history"
    con = open_history_store(history / "copied_skill_history.duckdb")
    try:
        pending = (
            {} if args.merge_only else repos_still_to_mine(targets_by_repo, con, args.retry_failed)
        )
        if not (args.force or args.merge_only or pending) and outputs_exist(
            [*_output_paths(history), _targets_path(history)]
        ):
            print("skip: copied skill history is up to date (pass --force to export again)")
            _print_report(con, targets, history, None)
            return
        _print_plan(targets_by_repo, pending, args.dev)
        _require_yes(len(pending), args.dev, args.yes)
        started = time.perf_counter()
        # Step 3: Mine with a pool of workers; the main thread saves each repo
        if pending:
            print(f"Step 3: mine {len(pending)} repos with {DOWNLOAD_WORKERS} workers", flush=True)
            mine_copied_skills(
                pending,
                con,
                history / "work",
                DOWNLOAD_WORKERS,
                timeout_seconds=_timeout_seconds(args.timeout),
            )
        # Step 4: Export the store into the parquet files
        print("Step 4: export the store", flush=True)
        save_outputs(con, targets, history)
        shutil.rmtree(history / "work", ignore_errors=True)
        # Step 5: Summary and sample records
        _print_report(con, targets, history, time.perf_counter() - started)
    finally:
        con.close()


def _parser() -> argparse.ArgumentParser:
    """Shared flags plus limit, retry, merge-only, timeout and yes."""
    parser = build_arg_parser("Download the history of dataset skills that copied a registry.")
    parser.add_argument("--limit", type=int, default=None, help="only the first N repos (sorted)")
    parser.add_argument(
        "--retry-failed", action="store_true", help="mine timeout and error repos again"
    )
    parser.add_argument(
        "--merge-only", action="store_true", help="do not clone; export the store on disk"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=f"seconds for each git command (default {DOWNLOAD_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=f"continue a full run above {COPY_HISTORY_CONFIRM_ABOVE_REPOS} pending repos",
    )
    return parser


def _timeout_seconds(value: int | None) -> int:
    """The ``--timeout`` value, or the settings default."""
    if value is None:
        return DOWNLOAD_TIMEOUT_SECONDS
    if value <= 0:
        raise SystemExit("--timeout must be a positive number of seconds")
    return value


def _limit_repos(grouped: dict[str, list[str]], limit: int | None) -> dict[str, list[str]]:
    """Keep the first ``limit`` repos. None keeps all."""
    if limit is None:
        return grouped
    if limit < 0:
        raise SystemExit("--limit must be 0 or greater")
    return dict(list(grouped.items())[:limit])


def _targets_path(history: Path) -> Path:
    """``download_targets.parquet`` beside the store."""
    return history / "download_targets.parquet"


def _output_paths(history: Path) -> tuple[Path, Path, Path]:
    """Changes, texts and status parquet paths."""
    return (
        history / "copied_skill_history_changes.parquet",
        history / "copied_skill_history_texts.parquet",
        history / "copied_skill_history_status.parquet",
    )


def _split_too_large(
    targets_by_repo: dict[str, list[str]],
) -> tuple[dict[str, list[str]], list[tuple[str, int]]]:
    """Separate repos above ``COPY_HISTORY_MAX_FOLDERS`` folders from the rest."""
    clone: dict[str, list[str]] = {}
    too_large: list[tuple[str, int]] = []
    for repo, folders in targets_by_repo.items():
        if len(folders) > COPY_HISTORY_MAX_FOLDERS:
            too_large.append((repo, len(folders)))
        else:
            clone[repo] = folders
    return clone, too_large


def _too_large_result(repo: str, folder_count: int) -> RepoResult:
    """Status for a repo that is not cloned because of its folder count."""
    message = f"{folder_count} copy folders (over {COPY_HISTORY_MAX_FOLDERS}); not cloned"
    return RepoResult(repo, "too_large", message, None, 0.0, False)


def _future_result(repo: str, future) -> tuple[RepoResult, pd.DataFrame, pd.DataFrame]:
    """The miner's result. ``mine_one_repo`` never raises; this guards anyway."""
    try:
        return future.result()
    except Exception as error:  # noqa: BLE001 - one bad repo must not stop the run
        result = RepoResult(
            repo, "error", _last_line(f"{type(error).__name__}: {error}"), None, 0.0, False
        )
        return result, pd.DataFrame(), pd.DataFrame()


def _save_and_log(
    con: duckdb.DuckDBPyConnection,
    result: RepoResult,
    changes: pd.DataFrame | None,
    texts: pd.DataFrame | None,
) -> list:
    """Save one repo, print ``repo status seconds changes``, return its status row.

    A store failure (disk full, a bad value) is saved as ``error`` with the
    message, so the run goes on.
    """
    changes = pd.DataFrame() if changes is None else changes
    texts = pd.DataFrame() if texts is None else texts
    try:
        save_repo_to_store(con, result, changes, texts)
    except (OSError, duckdb.Error, ValueError, KeyError) as error:
        result = RepoResult(
            result.repo,
            "error",
            _last_line(f"store: {error}"),
            result.head_commit,
            result.seconds,
            result.follow_renames,
        )
        changes = pd.DataFrame()
        save_repo_to_store(con, result, changes, pd.DataFrame())
    row = _status_row(result, _prepare_changes(changes))
    print(f"{result.repo} {result.status} {result.seconds:.1f} {row[-1]}", flush=True)
    return row


def _status_row(result: RepoResult, changes: pd.DataFrame) -> list:
    """Status values in ``_STATUS_COLUMNS`` order."""
    folders = int(changes["skill_folder"].nunique()) if len(changes) else 0
    return [
        result.repo,
        result.status,
        result.message or "",
        result.head_commit,
        float(result.seconds),
        bool(result.follow_renames),
        int(result.not_ours),
        int(result.after_copy),
        int(result.twin),
        folders,
        len(changes),
    ]


def _prepare_changes(changes: pd.DataFrame) -> pd.DataFrame:
    """Change rows in store column order; missing text cells become ``""``."""
    if changes.empty:
        return pd.DataFrame(columns=_CHANGE_COLUMNS)
    frame = changes.loc[:, _CHANGE_COLUMNS].copy()
    for name in _CHANGE_COLUMNS:
        if name not in _NON_TEXT_CHANGE_COLUMNS:
            frame[name] = frame[name].fillna("").astype(str)
    frame["is_skill_md"] = frame["is_skill_md"].fillna(False).astype(bool)
    frame["is_script"] = frame["is_script"].fillna(False).astype(bool)
    frame["commit_seq"] = pd.to_numeric(frame["commit_seq"]).fillna(0).astype("int64")
    return frame


def _prepare_texts(texts: pd.DataFrame) -> pd.DataFrame:
    """Blob rows with a real sha, one per sha."""
    if texts.empty:
        return pd.DataFrame(columns=["sha", "text"])
    frame = texts.loc[:, ["sha", "text"]].copy()
    frame["sha"] = frame["sha"].fillna("").astype(str)
    frame["text"] = frame["text"].fillna("").astype(str)
    frame = frame[(frame["sha"].str.len() > 0) & (frame["sha"].str.strip("0") != "")]
    return frame.drop_duplicates("sha").reset_index(drop=True)


def _unregister(con: duckdb.DuckDBPyConnection, name: str) -> None:
    """Drop a registered frame; a missing name is ignored."""
    try:
        con.unregister(name)
    except duckdb.Error:
        pass


def _sql_quote(value: str) -> str:
    """A single-quoted SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _last_line(text: str) -> str:
    """The last non-empty line of an error message."""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    return lines[-1] if lines else "error"


def _print_plan(chosen: dict[str, list[str]], pending: dict[str, list[str]], dev: bool) -> None:
    """Print planned repos, folders and pending repos, with a time estimate.

    The estimate is the median seconds per repo of the dev run times the
    pending repos, divided by the workers. Fast ``gone`` repos pull the
    median down, so it is printed as a lower bound.
    """
    folders = sum(len(items) for items in chosen.values())
    line = (
        f"planned repos: {len(chosen)} folders: {folders} pending: {len(pending)} "
        f"mode: {'dev' if dev else 'full'}"
    )
    seconds = _dev_seconds_per_repo()
    if seconds is not None and pending:
        hours = len(pending) * seconds / DOWNLOAD_WORKERS / 3600
        line += f" estimate_hours: {hours:.1f} (lower bound, {seconds:.1f} s per repo in dev)"
    print(line, flush=True)


def _dev_seconds_per_repo() -> float | None:
    """Median seconds of the cloned repos in the dev status file, or None."""
    path = data_dir(True) / "copied_skill_history" / "copied_skill_history_status.parquet"
    if not path.is_file():
        return None
    status = read_parquet(path)
    seconds = status.loc[status["status"] != "too_large", "seconds"].dropna()
    return float(seconds.median()) if len(seconds) else None


def _require_yes(pending_count: int, dev: bool, yes: bool) -> None:
    """Stop a full run above the confirm line unless ``--yes`` was passed."""
    if dev or yes or pending_count <= COPY_HISTORY_CONFIRM_ABOVE_REPOS:
        return
    print(
        f"This full run has {pending_count} pending repos (over "
        f"{COPY_HISTORY_CONFIRM_ABOVE_REPOS}). Pass --yes to continue.",
        flush=True,
    )
    raise SystemExit(1)


def _print_report(
    con: duckdb.DuckDBPyConnection,
    targets: pd.DataFrame,
    history: Path,
    elapsed: float | None,
) -> None:
    """Print the stage 6 summary and two sample rows, for this run's targets only.

    ``versions`` per copied skill is the number of distinct ``SKILL.md`` blobs
    (not deletes) in its folder, over the folders of ``ok`` repos.
    """
    con.register("report_targets", targets)
    try:
        status = con.execute(
            "SELECT status, count(*) FROM status "
            "WHERE repo IN (SELECT repo FROM report_targets) GROUP BY status"
        ).fetchall()
        versions = con.execute(_VERSIONS_SQL).fetchdf()["versions"].astype(float)
        con.execute(_REPORT_CHANGES_SQL)
        values = [value for sql in _STORE_TOTALS_SQL for value in con.execute(sql).fetchone()]
        con.execute("DROP TABLE IF EXISTS report_changes")
        totals = dict(zip(_TOTAL_NAMES, values, strict=True))
        sample_changes = con.execute(_SAMPLE_CHANGES_SQL).fetchdf()
        sample_status = con.execute(_SAMPLE_STATUS_SQL).fetchdf()
    finally:
        _unregister(con, "report_targets")
    counts = dict(status)
    numbers: dict[str, object] = {
        "repos_targeted": int(targets["repo"].nunique()),
        "folders_targeted": len(targets),
        **{f"repos_{name}": int(counts.get(name, 0)) for name in _STATUS_NAMES},
        "folders_in_ok_repos": len(versions),
        "folders_without_history": int((versions == 0).sum()),
        "versions_median": _quantile(versions[versions > 0], 0.5),
        "versions_p90": _quantile(versions[versions > 0], 0.9),
        "share_more_than_one_version": _share(versions[versions > 0] > 1),
        "change_rows": int(totals["change_rows"]),
        "text_rows": int(totals["text_rows"]),
        "text_bytes": int(totals["text_bytes"]),
        "first_commit_utc": str(totals["first_commit_utc"]),
        "last_commit_utc": str(totals["last_commit_utc"]),
        "duplicate_change_keys": int(totals["duplicate_change_keys"]),
        "disk_bytes": sum(p.stat().st_size for p in history.rglob("*") if p.is_file()),
    }
    if elapsed is not None:
        numbers["elapsed_seconds"] = round(elapsed, 1)
    print_summary("Stage 6 summary", numbers)
    print_sample_records(sample_changes, "Sample changes", 2)
    print_sample_records(sample_status, "Sample status", 2)


def _quantile(values: pd.Series, q: float) -> float:
    """Quantile rounded to 2 places; 0.0 for no values."""
    return round(float(values.quantile(q)), 2) if len(values) else 0.0


def _share(flags: pd.Series) -> float:
    """Share of true values rounded to 4 places; 0.0 for no values."""
    return round(float(flags.mean()), 4) if len(flags) else 0.0


if __name__ == "__main__":
    main()
