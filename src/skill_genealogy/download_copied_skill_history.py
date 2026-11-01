"""Download git history for dataset skills that copied a registry.

What: choose skill folders from ``copy_candidates.parquet``, clone each
repo that wild_history does not already cover, and keep the skill-folder
file history. A later run skips a repo that already finished.
Inputs: ``copy_candidates.parquet`` in the dev or full data folder.
Outputs: ``copied_skill_history/`` (a DuckDB store plus parquet files),
kept apart from ``registry_history/``. There is no shards folder.
Run:
    python -m skill_genealogy.download_copied_skill_history --dev --limit 5
"""

from __future__ import annotations

import argparse
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

import duckdb
import pandas as pd

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
    DOWNLOAD_TIMEOUT_SECONDS,
    DOWNLOAD_WORKERS,
    TEXT_ROW_GROUP,
    data_dir,
)

# A clone that finished. ``empty`` has no commits, so mining it again adds nothing.
# Same rule as stage 3 (algorithm A19). The card names ok and gone; empty is included.
_FINISHED_STATUS = frozenset({"ok", "gone", "empty"})
# These two are mined again only with --retry-failed.
_RETRY_STATUS = frozenset({"timeout", "error"})
# A full run above this many pending repos waits for --yes.
# The card sets 2,000: cloning that many repos takes hours.
_CONFIRM_ABOVE_REPOS = 2000
_REASON_MISSING = "no_wild_history"
_REASON_WILD = "include_wild"
_TARGET_COLUMNS = ["repo", "skill_folder", "reason"]
_STATUS_COLUMNS = [
    "repo",
    "status",
    "message",
    "head_commit",
    "seconds",
    "follow_renames",
    "skill_folders",
    "changes",
]
_STATUS_NAMES = ("ok", "gone", "empty", "timeout", "error")
# Same columns as git_history._CHANGE_COLUMNS (the G05 changes table).
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
_STRING_CHANGE_COLUMNS = [
    name
    for name in _CHANGE_COLUMNS
    if name not in {"is_skill_md", "is_script", "commit_seq"}
]

# Query Q6.1, see QUERIES.md
_CREATE_STORE_SQL = (
    """
    CREATE TABLE IF NOT EXISTS changes (
        repo VARCHAR,
        skill_folder VARCHAR,
        file_path VARCHAR,
        commit VARCHAR,
        commit_date VARCHAR,
        author_key VARCHAR,
        subject VARCHAR,
        status VARCHAR,
        old_file_path VARCHAR,
        old_sha VARCHAR,
        new_sha VARCHAR,
        old_mode VARCHAR,
        new_mode VARCHAR,
        is_skill_md BOOLEAN,
        is_script BOOLEAN,
        commit_seq BIGINT,
        repo_path VARCHAR,
        old_repo_path VARCHAR,
        history_role VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS texts (
        sha VARCHAR PRIMARY KEY,
        text VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS status (
        repo VARCHAR PRIMARY KEY,
        status VARCHAR,
        message VARCHAR,
        head_commit VARCHAR,
        seconds DOUBLE,
        follow_renames BOOLEAN,
        skill_folders BIGINT,
        changes BIGINT
    )
    """,
    "CREATE INDEX IF NOT EXISTS changes_by_repo ON changes(repo)",
)

# Query Q6.2, see QUERIES.md
_EXPORT_CHANGES_SQL = """
COPY (
    SELECT
        repo, skill_folder, file_path, commit, commit_date, author_key, subject,
        status, old_file_path, old_sha, new_sha, old_mode, new_mode,
        is_skill_md, is_script, commit_seq, repo_path, old_repo_path, history_role
    FROM changes
    ORDER BY repo, skill_folder, commit_seq, file_path, status, new_sha
) TO {path} (FORMAT PARQUET, COMPRESSION ZSTD)
"""

# Query Q6.3, see QUERIES.md
_EXPORT_TEXTS_SQL = f"""
COPY (
    SELECT sha, text
    FROM texts
    WHERE sha IN (SELECT new_sha FROM changes UNION SELECT old_sha FROM changes)
      AND sha <> ''
      AND sha <> repeat('0', 40)
    ORDER BY sha
) TO {{path}} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {TEXT_ROW_GROUP})
"""

# Query Q6.4, see QUERIES.md
_EXPORT_STATUS_SQL = """
COPY (
    SELECT
        repo, status, message, head_commit, seconds, follow_renames,
        skill_folders, changes
    FROM status
    ORDER BY repo
) TO {path} (FORMAT PARQUET, COMPRESSION ZSTD)
"""

# Query Q6.5, see QUERIES.md
_VERSIONS_SQL = """
SELECT repo, skill_folder, versions
FROM (
    SELECT
        repo,
        skill_folder,
        count(DISTINCT CASE
            WHEN is_skill_md
             AND status <> 'D'
             AND new_sha IS NOT NULL
             AND new_sha <> ''
             AND lower(new_sha) <> repeat('0', 40)
            THEN lower(new_sha)
        END) AS versions
    FROM changes
    WHERE repo IN (SELECT repo FROM report_repos)
    GROUP BY repo, skill_folder
)
WHERE versions > 0
"""

# Query Q6.6, see QUERIES.md
_STORE_COUNTS_SQL = """
WITH used AS (
    SELECT new_sha AS sha FROM changes
    WHERE repo IN (SELECT repo FROM report_repos)
    UNION
    SELECT old_sha FROM changes
    WHERE repo IN (SELECT repo FROM report_repos)
)
SELECT
    (
        SELECT count(*) FROM changes
        WHERE repo IN (SELECT repo FROM report_repos)
    ) AS change_rows,
    (SELECT count(*) FROM texts WHERE sha IN (SELECT sha FROM used)) AS text_rows,
    (
        SELECT coalesce(sum(octet_length(encode(text))), 0)
        FROM texts
        WHERE sha IN (SELECT sha FROM used)
          AND sha <> ''
          AND sha <> repeat('0', 40)
    ) AS text_bytes,
    (
        SELECT count(*) FROM (
            SELECT repo, skill_folder, commit, file_path
            FROM changes
            WHERE repo IN (SELECT repo FROM report_repos)
            GROUP BY repo, skill_folder, commit, file_path
            HAVING count(*) > 1
        )
    ) AS duplicate_change_keys
"""

# Query Q6.7, see QUERIES.md
_INSERT_TEXTS_SQL = """
INSERT INTO texts
SELECT incoming_texts.sha, any_value(incoming_texts.text)
FROM incoming_texts
ANTI JOIN texts USING (sha)
GROUP BY incoming_texts.sha
"""

# Query Q6.8, see QUERIES.md
_STATUS_BY_REPO_SQL = "SELECT repo, status FROM status"


def pick_download_targets(dev: bool, include_wild: bool) -> pd.DataFrame:
    """Return one row per skill folder this run should clone.

    A folder is the parent of a copy-candidate file path. Folders with any
    file that lacks wild history are always kept. ``include_wild`` also
    keeps folders whose files all have wild history.

    Inputs: ``dev`` chooses the data folder. ``include_wild`` is the flag.
    Outputs: columns ``repo``, ``skill_folder``, ``reason``
    (``no_wild_history`` or ``include_wild``), sorted by repo and folder.
    Raises ``FileNotFoundError`` when ``copy_candidates.parquet`` is missing.
    """
    path = data_dir(dev) / "copy_candidates.parquet"
    command = "python -m skill_genealogy.find_copies --only copies"
    if dev:
        command = "python -m skill_genealogy.find_copies --dev --only copies"
    check_inputs_exist({"copy_candidates": path}, {"copy_candidates": command})
    copies = pd.read_parquet(path, columns=["repo", "path", "has_wild_history"])
    return _targets_from_copies(copies, include_wild)


# Algorithm A29, see CODE_FLOW.md
def _targets_from_copies(copies: pd.DataFrame, include_wild: bool) -> pd.DataFrame:
    """Collapse copy files to one folder row, dropping folders we will not clone.

    Two files in one folder are one target. A folder is ``no_wild_history``
    when any file has ``has_wild_history`` false. It is ``include_wild``
    only when every file already has wild history and ``include_wild`` is true.

    Example:
    >>> copies = pd.DataFrame({
    ...     "repo": ["a/r", "a/r", "a/r"],
    ...     "path": ["skills/pdf/SKILL.md", "skills/pdf/run.sh", "skills/other/SKILL.md"],
    ...     "has_wild_history": [False, False, True],
    ... })
    >>> list(_targets_from_copies(copies, False)["skill_folder"])
    ['skills/pdf']
    >>> both = _targets_from_copies(copies, True)
    >>> list(zip(both["skill_folder"], both["reason"]))
    [('skills/other', 'include_wild'), ('skills/pdf', 'no_wild_history')]
    """
    if copies.empty:
        return _empty_targets()
    work = copies.loc[:, ["repo", "path", "has_wild_history"]].copy()
    work["repo"] = work["repo"].map(_text_cell)
    work = work[work["repo"].str.len() > 0]
    if work.empty:
        return _empty_targets()
    work["skill_folder"] = work["path"].map(_skill_folder)
    work["missing"] = ~work["has_wild_history"].fillna(False).astype(bool)
    grouped = (
        work.groupby(["repo", "skill_folder"], sort=True, as_index=False)["missing"].any()
    )
    if not include_wild:
        grouped = grouped[grouped["missing"]]
    if grouped.empty:
        return _empty_targets()
    grouped["reason"] = _REASON_WILD
    grouped.loc[grouped["missing"], "reason"] = _REASON_MISSING
    return grouped.loc[:, _TARGET_COLUMNS].reset_index(drop=True)


def _skill_folder(path: object) -> str:
    """Parent folder of a dataset file path. A root file is ``.``.

    Example:
    >>> _skill_folder("skills/pdf/SKILL.md")
    'skills/pdf'
    >>> _skill_folder("SKILL.md")
    '.'
    """
    text = _text_cell(path).replace("\\", "/").strip("/")
    if "/" not in text:
        return "."
    parent = str(PurePosixPath(text).parent)
    if parent in {"", "."}:
        return "."
    return parent


def group_targets_by_repo(targets: pd.DataFrame) -> dict[str, list[str]]:
    """Map each repo to its skill folders, sorted by repo and folder.

    Inputs: the frame from ``pick_download_targets`` (or the same columns).
    Outputs: ``owner/repo`` to a list of folder paths. Duplicate folders
    are dropped. An empty frame returns an empty dict.

    Example:
    >>> frame = pd.DataFrame({
    ...     "repo": ["b/r", "a/r", "a/r"],
    ...     "skill_folder": ["z", "m", "a"],
    ... })
    >>> group_targets_by_repo(frame)
    {'a/r': ['a', 'm'], 'b/r': ['z']}
    """
    if targets.empty:
        return {}
    ordered = targets.loc[:, ["repo", "skill_folder"]].copy()
    ordered["repo"] = ordered["repo"].map(_text_cell)
    ordered["skill_folder"] = ordered["skill_folder"].map(_skill_folder_cell)
    ordered = ordered.sort_values(["repo", "skill_folder"], kind="mergesort")
    grouped: dict[str, list[str]] = {}
    for repo, folder in zip(ordered["repo"], ordered["skill_folder"], strict=True):
        if not repo:
            continue
        folders = grouped.setdefault(repo, [])
        if not folders or folders[-1] != folder:
            folders.append(folder)
    return grouped


def open_history_store(path: Path) -> duckdb.DuckDBPyConnection:
    """Open the copied-skill store, creating the file and the three tables.

    ``save_repo_to_store`` writes one repo in one transaction. The file
    replaces the shard folder used by stage 3.

    Inputs: ``path`` — usually ``copied_skill_history.duckdb``.
    Outputs: an open DuckDB connection. The caller closes it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    for statement in _CREATE_STORE_SQL:
        connection.execute(statement)
    return connection


def save_repo_to_store(
    connection: duckdb.DuckDBPyConnection,
    result: RepoResult,
    changes: pd.DataFrame,
    texts: pd.DataFrame,
) -> None:
    """Replace one repo's changes, texts and status in one transaction.

    Texts are inserted only when that blob sha is not already stored.
    A failure rolls the transaction back and raises, so a half-written
    repo is mined again on the next run.

    Inputs: ``connection`` from ``open_history_store``. ``result`` is the
    mine status. ``changes`` and ``texts`` are the frames from
    ``mine_one_repo``. Empty frames still write a status row.
    Outputs: none. The store includes this repo when the function returns.
    """
    prepared = _prepare_changes(changes)
    prepared_texts = _prepare_texts(texts)
    folder_count = 0 if prepared.empty else int(prepared["skill_folder"].nunique())
    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute("DELETE FROM changes WHERE repo = ?", [result.repo])
        connection.execute("DELETE FROM status WHERE repo = ?", [result.repo])
        if not prepared.empty:
            _insert_changes(connection, prepared)
        if not prepared_texts.empty:
            _insert_texts(connection, prepared_texts)
        connection.execute(
            """
            INSERT INTO status (
                repo, status, message, head_commit, seconds,
                follow_renames, skill_folders, changes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                result.repo,
                result.status,
                result.message or "",
                result.head_commit,
                float(result.seconds),
                bool(result.follow_renames),
                folder_count,
                len(prepared),
            ],
        )
        connection.execute("COMMIT")
    except Exception:
        _rollback(connection)
        raise
    finally:
        _unregister(connection, "incoming_changes")
        _unregister(connection, "incoming_texts")


def export_history_store(
    connection: duckdb.DuckDBPyConnection, out_dir: Path
) -> tuple[Path, Path, Path]:
    """Write the three parquet files from the store.

    Texts are only the blobs named by a change row, one row per sha.
    The connection stays open.

    Inputs: ``connection`` from ``open_history_store``. ``out_dir`` is the
    ``copied_skill_history`` folder.
    Outputs: paths of the changes, texts and status parquet files.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    changes_path, texts_path, status_path = _output_paths(out_dir)
    _copy_query(connection, _EXPORT_CHANGES_SQL, changes_path)
    _copy_query(connection, _EXPORT_TEXTS_SQL, texts_path)
    _copy_query(connection, _EXPORT_STATUS_SQL, status_path)
    return changes_path, texts_path, status_path


def mine_copied_skills(
    targets_by_repo: dict[str, list[str]],
    connection: duckdb.DuckDBPyConnection,
    work_dir: Path,
    workers: int,
    *,
    timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS,
    urls: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Clone the given repos and save each one on this thread.

    Git runs in a thread pool. DuckDB writes stay on the caller thread,
    one transaction per repo. One log line is printed per repo:
    ``repo status seconds changes``.

    Inputs: ``targets_by_repo`` maps ``owner/repo`` to skill folders.
    ``connection`` is an open store. ``work_dir`` holds temporary clones.
    ``workers`` is the pool size. ``timeout_seconds`` limits each git
    command. ``urls`` maps a repo to a clone URL; tests pass ``file://``.
    A missing URL uses GitHub.
    Outputs: one status row per repo that was mined, sorted by repo.
    An empty map returns that frame with no rows.
    """
    if not targets_by_repo:
        return _empty_status()
    work_dir.mkdir(parents=True, exist_ok=True)
    url_map = urls or {}
    pool_size = max(1, min(workers, len(targets_by_repo)))
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        futures = {
            pool.submit(
                mine_one_repo,
                repo,
                work_dir,
                folders,
                timeout_seconds,
                url_map.get(repo),
            ): repo
            for repo, folders in targets_by_repo.items()
        }
        for future in as_completed(futures):
            repo = futures.pop(future)
            rows.append(_save_mined_repo(connection, repo, future))
            # Drop the clone frames. The future keeps them until this reference goes.
            del future
    frame = pd.DataFrame(rows).loc[:, _STATUS_COLUMNS]
    return frame.sort_values("repo", kind="mergesort").reset_index(drop=True)


def main() -> None:
    """Download copied-skill history, then export the store.

    ``--dev`` reads the dev copy table and writes the dev folder.
    ``--limit N`` keeps the first N repo names. ``--include-wild`` also
    clones folders that already have wild history. ``--force`` exports
    again and does not re-clone a finished repo. ``--retry-failed``
    re-clones ``timeout`` and ``error``. ``--merge-only`` only exports.
    A full run with more than 2,000 pending repos stops unless ``--yes``.

    Inputs: command-line flags and ``copy_candidates.parquet``.
    Outputs: the store and the parquet files. Nothing is returned.
    """
    args = _parser().parse_args()
    # Step 1: Pick download targets
    targets = pick_download_targets(args.dev, args.include_wild)
    grouped = group_targets_by_repo(targets)
    chosen = _limit_repos(grouped, args.limit)
    chosen_targets = _targets_for_repos(targets, chosen)
    history = _history_dir(args.dev)
    work_dir = history / "work"
    output_paths = [*_output_paths(history), _targets_path(history)]
    connection = open_history_store(_store_path(history))
    try:
        # Step 2: Group by repo and skip repos already ok or gone
        pending = {}
        if not args.merge_only:
            pending = _repos_still_to_mine(chosen, connection, args.retry_failed)
        if _can_skip(args, output_paths, pending):
            print(
                "skip: copied skill history already exists (pass --force to export again)",
                flush=True,
            )
            _finish_run(connection, chosen_targets, history, work_dir, None)
            return
        _print_plan(chosen, pending, args.dev)
        _require_yes(len(pending), args.dev, args.yes)
        started = time.perf_counter()
        _write_targets(chosen_targets, history)
        # Step 3: Mine with a pool of workers; the main thread saves each repo
        if pending:
            print(
                f"Step 3: mine {len(pending)} repos with {DOWNLOAD_WORKERS} workers",
                flush=True,
            )
            mine_copied_skills(pending, connection, work_dir, DOWNLOAD_WORKERS)
        # Step 4: Export the store
        print("Step 4: export the store", flush=True)
        export_history_store(connection, history)
        # Step 5: Remove leftover clones, then print the summary
        elapsed = time.perf_counter() - started
        _finish_run(connection, chosen_targets, history, work_dir, elapsed)
    finally:
        connection.close()


def _parser() -> argparse.ArgumentParser:
    """Parser for this stage: shared flags, plus limit, retry, merge and wild."""
    parser = build_arg_parser(
        "Download skill-folder history for dataset files that copied a registry."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="mine only the first N repo names, after sorting",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="mine repos whose status is timeout or error",
    )
    parser.add_argument(
        "--merge-only",
        action="store_true",
        help="do not clone; export the store already on disk",
    )
    parser.add_argument(
        "--include-wild",
        action="store_true",
        help="also clone folders whose SKILL.md is already in wild_history",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "continue a full run when more than "
            f"{_CONFIRM_ABOVE_REPOS} repos are pending"
        ),
    )
    return parser


def _history_dir(dev: bool) -> Path:
    """``copied_skill_history`` under the dev or full data folder."""
    folder = data_dir(dev) / "copied_skill_history"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _store_path(history: Path) -> Path:
    """The DuckDB file inside ``copied_skill_history``."""
    return history / "copied_skill_history.duckdb"


def _targets_path(history: Path) -> Path:
    """The target list written beside the store."""
    return history / "download_targets.parquet"


def _output_paths(history: Path) -> tuple[Path, Path, Path]:
    """Changes, texts and status parquet paths."""
    return (
        history / "copied_skill_history_changes.parquet",
        history / "copied_skill_history_texts.parquet",
        history / "copied_skill_history_status.parquet",
    )


def _empty_targets() -> pd.DataFrame:
    """Target frame with the output columns and no rows."""
    return pd.DataFrame(columns=_TARGET_COLUMNS)


def _empty_status() -> pd.DataFrame:
    """Status frame with the output columns and no rows."""
    return pd.DataFrame(columns=_STATUS_COLUMNS)


def _empty_change_frame() -> pd.DataFrame:
    """Changes frame with the store columns and no rows."""
    return pd.DataFrame(columns=_CHANGE_COLUMNS)


def _empty_text_frame() -> pd.DataFrame:
    """Texts frame with ``sha`` and ``text`` and no rows."""
    return pd.DataFrame(columns=["sha", "text"])


def _text_cell(value: object) -> str:
    """One cell as text. Missing values become an empty string."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        return str(value)
    return str(value)


def _skill_folder_cell(value: object) -> str:
    """Keep a folder name. An empty name is the repo root."""
    text = _text_cell(value).replace("\\", "/").strip("/")
    return text or "."


def _limit_repos(
    grouped: dict[str, list[str]], limit: int | None
) -> dict[str, list[str]]:
    """Keep the first ``limit`` repo names. None keeps the whole map."""
    if limit is None:
        return grouped
    if limit < 0:
        raise SystemExit("--limit must be 0 or greater")
    names = list(grouped)[:limit]
    return {name: grouped[name] for name in names}


def _targets_for_repos(
    targets: pd.DataFrame, chosen: dict[str, list[str]]
) -> pd.DataFrame:
    """Rows of ``targets`` whose repo is in ``chosen``."""
    if targets.empty or not chosen:
        return _empty_targets()
    keep = set(chosen)
    mask = targets["repo"].map(_text_cell).isin(keep)
    return targets.loc[mask, _TARGET_COLUMNS].reset_index(drop=True)


def _write_targets(targets: pd.DataFrame, history: Path) -> None:
    """Write ``download_targets.parquet`` for the repos this run considers."""
    write_parquet(targets, _targets_path(history), sort_by=["repo", "skill_folder"])


# Algorithm A19, see CODE_FLOW.md
def _repos_still_to_mine(
    targets_by_repo: dict[str, list[str]],
    connection: duckdb.DuckDBPyConnection,
    retry_failed: bool,
) -> dict[str, list[str]]:
    """Drop repos whose store status is already finished.

    ``ok``, ``gone`` and ``empty`` stay done. ``timeout`` and ``error``
    stay done unless ``retry_failed`` is true. A repo with no status row
    is mined. Order follows ``targets_by_repo``.
    """
    known = _status_by_repo(connection)
    return {
        repo: folders
        for repo, folders in targets_by_repo.items()
        if _needs_mine(known.get(repo), retry_failed)
    }


def _needs_mine(status: str | None, retry_failed: bool) -> bool:
    """Return True when this store status should be cloned.

    None means the repo is not in the store yet.

    Example:
    >>> _needs_mine("ok", True)
    False
    >>> _needs_mine("empty", True)
    False
    >>> _needs_mine("gone", False)
    False
    >>> _needs_mine("timeout", False)
    False
    >>> _needs_mine("timeout", True)
    True
    >>> _needs_mine(None, False)
    True
    """
    if status is None:
        return True
    if status in _RETRY_STATUS:
        return retry_failed
    if status in _FINISHED_STATUS:
        return False
    return True


def _status_by_repo(connection: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Map ``owner/repo`` to the status stored for it."""
    rows = connection.execute(_STATUS_BY_REPO_SQL).fetchall()
    return {str(repo): str(status) for repo, status in rows if repo and status}


def _can_skip(
    args: argparse.Namespace,
    output_paths: list[Path],
    pending: dict[str, list[str]],
) -> bool:
    """True when the parquet files exist and this run would not clone or export."""
    if args.force or args.merge_only or pending:
        return False
    return outputs_exist(output_paths)


def _print_plan(
    chosen: dict[str, list[str]],
    pending: dict[str, list[str]],
    dev: bool,
) -> None:
    """Print how many repos this run will clone, and a lower-bound hour estimate.

    The estimate uses the median seconds of every dev status row, including
    fast ``gone`` repos, so it is labelled a lower bound.
    """
    folders = sum(len(items) for items in chosen.values())
    seconds = _dev_seconds_per_repo()
    estimate = ""
    if seconds is not None and pending:
        hours = len(pending) * seconds / DOWNLOAD_WORKERS / 3600
        estimate = (
            f" estimate_hours: {hours:.1f} seconds_per_repo: {seconds:.1f}"
            " (lower bound)"
        )
    mode = "dev" if dev else "full"
    print(
        f"planned repos: {len(chosen)} folders: {folders} "
        f"pending: {len(pending)} mode: {mode}{estimate}",
        flush=True,
    )


def _require_yes(pending_count: int, dev: bool, yes: bool) -> None:
    """Stop a large full run unless the person passed ``--yes``.

    ``--dev`` is not asked. A count at or below 2,000 is not asked.
    """
    if dev or yes or pending_count <= _CONFIRM_ABOVE_REPOS:
        return
    print(
        f"This full run has {pending_count} pending repos, "
        f"above {_CONFIRM_ABOVE_REPOS}. Pass --yes to continue.",
        flush=True,
    )
    raise SystemExit(1)


def _dev_seconds_per_repo() -> float | None:
    """Median seconds of every row in the dev status file, or None.

    ``gone`` and ``empty`` rows are included. They are often under a second,
    so this median is a lower bound on how long a real clone takes.
    """
    path = (
        data_dir(True)
        / "copied_skill_history"
        / "copied_skill_history_status.parquet"
    )
    if not path.is_file():
        return None
    status = read_parquet(path)
    if status.empty or "seconds" not in status.columns:
        return None
    seconds = pd.to_numeric(status["seconds"], errors="coerce").dropna()
    if seconds.empty:
        return None
    return float(seconds.median())


def _save_mined_repo(
    connection: duckdb.DuckDBPyConnection,
    repo: str,
    future,
) -> dict:
    """Save one finished clone and return its status row. Prints one line."""
    try:
        result, changes, texts = future.result()
    except Exception as error:
        result = RepoResult(repo, "error", _error_message(error), None, 0.0, False)
        changes = _empty_change_frame()
        texts = _empty_text_frame()
    if result.status == "error":
        result.message = _name_missing_commit(result.message or "")
    folder_count = 0 if changes.empty else int(changes["skill_folder"].nunique())
    change_count = 0 if changes.empty else len(changes)
    status_name, message, saved_changes, saved_folders = _commit_repo(
        connection, result, changes, texts, folder_count, change_count
    )
    saved_seconds = float(result.seconds)
    saved_repo = result.repo
    saved_head = result.head_commit
    saved_follow = bool(result.follow_renames)
    del result, changes, texts
    # Step 3: one line per repo, flushed so a background log shows progress.
    print(f"{saved_repo} {status_name} {saved_seconds:.1f} {saved_changes}", flush=True)
    return {
        "repo": saved_repo,
        "status": status_name,
        "message": message,
        "head_commit": saved_head,
        "seconds": saved_seconds,
        "follow_renames": saved_follow,
        "skill_folders": saved_folders,
        "changes": saved_changes,
    }


def _commit_repo(
    connection: duckdb.DuckDBPyConnection,
    result: RepoResult,
    changes: pd.DataFrame,
    texts: pd.DataFrame,
    folder_count: int,
    change_count: int,
) -> tuple[str, str, int, int]:
    """Write the mine result. A disk error is stored as status ``error``."""
    try:
        save_repo_to_store(connection, result, changes, texts)
    except (OSError, duckdb.Error) as error:
        message = _last_line(str(error))
        failed = RepoResult(
            result.repo,
            "error",
            message,
            result.head_commit,
            result.seconds,
            result.follow_renames,
        )
        try:
            save_repo_to_store(
                connection, failed, _empty_change_frame(), _empty_text_frame()
            )
        except (OSError, duckdb.Error):
            pass
        return "error", message, 0, 0
    return result.status, result.message or "", change_count, folder_count


def _prepare_changes(changes: pd.DataFrame) -> pd.DataFrame:
    """Changes columns in store order. Missing text becomes ``""``."""
    if changes.empty:
        return _empty_change_frame()
    frame = changes.loc[:, list(_CHANGE_COLUMNS)].copy()
    for name in _STRING_CHANGE_COLUMNS:
        frame[name] = frame[name].map(_text_cell)
    frame["is_skill_md"] = frame["is_skill_md"].fillna(False).astype(bool)
    frame["is_script"] = frame["is_script"].fillna(False).astype(bool)
    frame["commit_seq"] = (
        pd.to_numeric(frame["commit_seq"], errors="coerce").fillna(0).astype("int64")
    )
    return frame


def _prepare_texts(texts: pd.DataFrame) -> pd.DataFrame:
    """Blob rows with a real sha. The all-zero sha and blanks are dropped."""
    if texts.empty:
        return _empty_text_frame()
    frame = texts.loc[:, ["sha", "text"]].copy()
    frame["sha"] = frame["sha"].map(_text_cell)
    frame["text"] = frame["text"].map(_text_cell)
    frame = frame[frame["sha"].str.len() > 0]
    frame = frame[frame["sha"].map(lambda sha: set(sha) != {"0"})]
    return frame.drop_duplicates("sha", keep="first").reset_index(drop=True)


def _insert_changes(connection: duckdb.DuckDBPyConnection, changes: pd.DataFrame) -> None:
    """Append prepared change rows. The caller is inside the transaction."""
    connection.register("incoming_changes", changes.loc[:, _CHANGE_COLUMNS])
    columns = ", ".join(_CHANGE_COLUMNS)
    connection.execute(
        f"INSERT INTO changes ({columns}) SELECT {columns} FROM incoming_changes"
    )


def _insert_texts(connection: duckdb.DuckDBPyConnection, texts: pd.DataFrame) -> None:
    """Append blob texts whose sha is not already stored."""
    connection.register("incoming_texts", texts.loc[:, ["sha", "text"]])
    connection.execute(_INSERT_TEXTS_SQL)


def _copy_query(connection: duckdb.DuckDBPyConnection, sql: str, path: Path) -> None:
    """Run a COPY statement. An existing parquet file is replaced."""
    if path.exists():
        path.unlink()
    connection.execute(sql.format(path=_sql_quote(str(path))))


def _sql_quote(value: str) -> str:
    """A single-quoted SQL string, with quotes escaped."""
    return "'" + value.replace("'", "''") + "'"


def _rollback(connection: duckdb.DuckDBPyConnection) -> None:
    """Undo the open transaction. A second failure is ignored."""
    try:
        connection.execute("ROLLBACK")
    except duckdb.Error:
        return


def _unregister(connection: duckdb.DuckDBPyConnection, name: str) -> None:
    """Drop a registered frame. Missing names are ignored."""
    try:
        connection.unregister(name)
    except duckdb.Error:
        return


def _last_line(text: str) -> str:
    """The last non-empty line of an error, or a short fallback."""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines:
        return "disk error"
    return lines[-1]


def _error_message(error: BaseException) -> str:
    """One line for a miner exception.

    ``str(KeyError)`` is only the quoted sha. The stored text uses
    ``type(error).__name__`` and says that commit is missing from the
    mined history. Other exceptions keep their last line.
    """
    text = _last_line(str(error))
    if isinstance(error, KeyError):
        sha = text.strip("'\"")
        return (
            f"{type(error).__name__}: {sha} "
            "(commit missing from the mined history)"
        )
    return text


def _name_missing_commit(message: str) -> str:
    """Replace a bare quoted sha with the KeyError text.

    ``mine_one_repo`` catches ``KeyError`` and keeps only ``str(exc)``.
    A message that is already readable is returned unchanged.
    """
    text = message.strip()
    if len(text) != 42 or not text.startswith("'") or not text.endswith("'"):
        return message
    sha = text[1:-1]
    if len(sha) == 40 and all(char in "0123456789abcdef" for char in sha):
        return f"KeyError: {sha} (commit missing from the mined history)"
    return message


def _finish_run(
    connection: duckdb.DuckDBPyConnection,
    targets: pd.DataFrame,
    history_dir: Path,
    work_dir: Path,
    elapsed: float | None,
) -> None:
    """Delete leftover clones, then print the summary.

    ``disk_bytes`` is measured after ``work`` is gone, so a timed-out
    clone is not counted. The history folder itself stays.

    Inputs: the open store, this run's targets, the history folder,
    the ``work`` path, and seconds since the run started (or None).
    Outputs: lines on standard output. ``work`` is removed when it exists.
    """
    _remove_work_dir(work_dir)
    _print_report(connection, targets, history_dir, elapsed)


def _remove_work_dir(work_dir: Path) -> None:
    """Delete ``copied_skill_history/work`` when it exists.

    A finished clone is already removed. A timeout can leave the folder
    behind. Removing the folder itself keeps it out of ``disk_bytes``.
    """
    if work_dir.exists():
        shutil.rmtree(work_dir)


def _print_report(
    connection: duckdb.DuckDBPyConnection,
    targets: pd.DataFrame,
    history_dir: Path,
    elapsed: float | None,
) -> None:
    """Print counts and two sample rows for the repos in ``targets``.

    Status, version and change counts use those repos only, so a ``--limit``
    run does not report the rest of the store. The parquet files still hold
    every saved repo. Disk bytes are the history folder. The caller removes
    ``work`` first, so a leftover clone is not included.
    """
    _register_report_repos(connection, targets)
    try:
        counts = _status_counts(connection)
        versions = _version_summary(connection)
        totals = _store_totals(connection)
        samples = (
            _sample_query(connection, _SAMPLE_CHANGES_SQL),
            _sample_query(connection, _SAMPLE_TEXTS_SQL),
            _sample_query(connection, _SAMPLE_STATUS_SQL),
        )
    finally:
        _unregister(connection, "report_repos")
    if totals["duplicate_change_keys"]:
        print(
            f"warning: duplicate_change_keys {totals['duplicate_change_keys']}",
            flush=True,
        )
    numbers: dict[str, object] = {
        "repos_targeted": _repo_count(targets),
        "folders_targeted": len(targets),
        "repos_ok": counts["ok"],
        "repos_gone": counts["gone"],
        "repos_empty": counts["empty"],
        "repos_timeout": counts["timeout"],
        "repos_error": counts["error"],
        "skills_with_versions": versions["skills_with_versions"],
        "versions_median": versions["versions_median"],
        "versions_p90": versions["versions_p90"],
        "share_more_than_one_version": versions["share_more_than_one_version"],
        "changes": totals["change_rows"],
        "text_rows": totals["text_rows"],
        "text_bytes": totals["text_bytes"],
        "duplicate_change_keys": totals["duplicate_change_keys"],
        "disk_bytes": _folder_bytes(history_dir),
    }
    if elapsed is not None:
        numbers["elapsed_seconds"] = round(elapsed, 1)
    print_summary("Stage 6 summary", numbers)
    print_sample_records(samples[0], "Sample changes", 2)
    print_sample_records(samples[1], "Sample texts", 2)
    print_sample_records(samples[2], "Sample status", 2)


# Two rows for the printed sample. Not a saved query: the export queries are Q6.2–Q6.4.
_SAMPLE_CHANGES_SQL = """
SELECT *
FROM changes
WHERE repo IN (SELECT repo FROM report_repos)
ORDER BY repo, skill_folder, commit_seq, file_path
LIMIT 2
"""
_SAMPLE_TEXTS_SQL = """
SELECT sha, text
FROM texts
WHERE sha IN (
    SELECT new_sha FROM changes WHERE repo IN (SELECT repo FROM report_repos)
    UNION
    SELECT old_sha FROM changes WHERE repo IN (SELECT repo FROM report_repos)
)
ORDER BY sha
LIMIT 2
"""
_SAMPLE_STATUS_SQL = """
SELECT repo, status, message, head_commit, seconds, follow_renames, skill_folders, changes
FROM status
WHERE repo IN (SELECT repo FROM report_repos)
ORDER BY repo
LIMIT 2
"""


def _repo_count(targets: pd.DataFrame) -> int:
    """Distinct repos in the target frame."""
    if targets.empty:
        return 0
    return int(targets["repo"].map(_text_cell).nunique())


def _register_report_repos(
    connection: duckdb.DuckDBPyConnection, targets: pd.DataFrame
) -> None:
    """Register the repos this summary is about. An empty list matches nothing."""
    if targets.empty:
        names: list[str] = []
    else:
        names = sorted({_text_cell(repo) for repo in targets["repo"] if _text_cell(repo)})
    connection.register("report_repos", pd.DataFrame({"repo": names}))


def _status_counts(connection: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """How many targeted repos finished in each status."""
    frame = connection.execute(
        """
        SELECT status FROM status
        WHERE repo IN (SELECT repo FROM report_repos)
        """
    ).fetchdf()
    if frame.empty:
        return {name: 0 for name in _STATUS_NAMES}
    tallies = frame["status"].astype(str).value_counts()
    return {name: int(tallies.get(name, 0)) for name in _STATUS_NAMES}


def _version_summary(connection: duckdb.DuckDBPyConnection) -> dict[str, float | int]:
    """Median and 90th percentile of SKILL.md blob versions per skill folder.

    A version is a distinct non-delete ``new_sha`` on a ``SKILL.md`` row.
    Folders with no such blob are left out of the median. The share is the
    fraction of the counted folders that have more than one version.
    """
    frame = connection.execute(_VERSIONS_SQL).fetchdf()
    empty = {
        "skills_with_versions": 0,
        "versions_median": 0.0,
        "versions_p90": 0.0,
        "share_more_than_one_version": 0.0,
    }
    if frame.empty:
        return empty
    versions = frame["versions"].astype(float)
    return {
        "skills_with_versions": int(len(versions)),
        "versions_median": round(float(versions.median()), 4),
        "versions_p90": round(float(versions.quantile(0.9)), 4),
        "share_more_than_one_version": round(float((versions > 1).mean()), 4),
    }


def _store_totals(connection: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Change rows, text rows, text bytes and repeated change keys."""
    row = connection.execute(_STORE_COUNTS_SQL).fetchone()
    if row is None:
        return {
            "change_rows": 0,
            "text_rows": 0,
            "text_bytes": 0,
            "duplicate_change_keys": 0,
        }
    return {
        "change_rows": int(row[0]),
        "text_rows": int(row[1]),
        "text_bytes": int(row[2]),
        "duplicate_change_keys": int(row[3]),
    }


def _sample_query(connection: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    """Up to two rows. An empty table returns an empty frame."""
    return connection.execute(sql).fetchdf()


def _folder_bytes(folder: Path) -> int:
    """Bytes of every file under ``folder``, including the store."""
    if not folder.exists():
        return 0
    return sum(item.stat().st_size for item in folder.rglob("*") if item.is_file())


if __name__ == "__main__":
    main()
