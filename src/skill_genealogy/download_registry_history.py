"""Download the git history of every selected registry.

What: clone each registry, keep every skill-folder file change, and keep
the text of SKILL.md and of small scripts. A later run skips a repo whose
shard already finished.
Inputs: ``registries.parquet``, or ``dev_registries.parquet`` with
``--dev``. Stage 2 writes both files in the full data folder.
Outputs: ``registry_history/shards`` plus three parquet files (changes,
texts, status) under the dev or full data folder.
Run:
    python -m skill_genealogy.download_registry_history --dev --limit 3
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from skill_genealogy.file_helpers import (
    build_arg_parser,
    check_inputs_exist,
    connect_duckdb,
    outputs_exist,
    print_sample_records,
    print_summary,
    read_parquet,
    write_parquet,
)
from skill_genealogy.git_history import (
    RepoResult,
    append_to_repo_shard,
    connect_history_duckdb,
    merge_shards,
    mine_deleted_folders,
    mine_one_repo,
    write_repo_shard,
)
from skill_genealogy.settings import (
    DOWNLOAD_TIMEOUT_SECONDS,
    DOWNLOAD_WORKERS,
    TEXT_ROW_GROUP,
    data_dir,
)

# A clone that finished. ``empty`` has no commits, so mining it again adds nothing.
_FINISHED_STATUS = frozenset({"ok", "gone", "empty"})
# These two are mined again only with --retry-failed.
_RETRY_STATUS = frozenset({"timeout", "error"})
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
    "history_since",
    "deleted_folders",
    "history_folders_dropped",
    "skill_folders",
    "changes",
]
_STATUS_NAMES = ("ok", "gone", "empty", "timeout", "error")
_CHANGE_KEY = ["repo", "skill_folder", "commit", "file_path"]


def read_registry_list(dev: bool) -> list[str]:
    """Return registry names, sorted, from the stage 2 parquet.

    ``dev`` True reads ``dev_registries.parquet`` (30 repos). False reads
    ``registries.parquet``. Both files live in the full data folder. Stage 2
    does not write a second copy under the dev folder.

    Inputs: ``dev`` — True for the 30-registry list.
    Outputs: ``owner/repo`` strings, sorted, with no duplicates.
    Raises ``FileNotFoundError`` when the parquet is missing.
    """
    path = _registry_list_path(dev)
    check_inputs_exist(
        {"registries": path},
        {"registries": "python -m skill_genealogy.select_registries"},
    )
    frame = read_parquet(path)
    return sorted({str(repo) for repo in frame["repo"].tolist() if str(repo)})


# Algorithm A19, see CODE_FLOW.md
def repos_still_to_mine(repos: list[str], shard_dir: Path, retry_failed: bool) -> list[str]:
    """Return the repos that still need a clone, in the same order.

    ``ok``, ``gone`` and ``empty`` are finished. ``timeout`` and ``error``
    are finished unless ``retry_failed`` is true. A missing shard, or a
    status file that cannot be read, is not finished.

    Inputs: ``repos`` is the registry list (already limited, if you limited
    it). ``shard_dir`` holds ``<owner>__<repo>.status.json``. ``retry_failed``
    is the ``--retry-failed`` flag.
    Outputs: the subset of ``repos`` to pass to ``mine_registries``.

    Example:
    >>> repos_still_to_mine(["a/b"], Path("/no/such/skill-genealogy-dir"), False)
    ['a/b']
    """
    known = _status_by_repo(shard_dir)
    return [repo for repo in repos if _needs_mine(known.get(repo), retry_failed)]


def mine_registries(
    repos: list[str],
    shard_dir: Path,
    work_dir: Path,
    workers: int,
    timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS,
    urls: dict[str, str] | None = None,
    since: str | None = None,
) -> pd.DataFrame:
    """Clone repos in parallel and write one shard each.

    Git runs in subprocesses, so the pool is threads. Each clone is deleted
    when that repo finishes. One log line is printed per repo:
    ``repo status seconds changes``.

    Inputs: ``repos`` to mine. ``shard_dir`` and ``work_dir`` are created if
    missing. ``workers`` is the pool size. ``timeout_seconds`` limits each
    git command. ``urls`` maps a repo name to a clone URL; tests pass
    ``file://``. A missing URL uses GitHub. ``since`` (``YYYY-MM-DD``) keeps
    only history from that date; None keeps all of it.
    Outputs: one status row per repo, sorted by repo. Columns are
    ``_STATUS_COLUMNS``. An empty ``repos`` list returns that frame with no rows.
    """
    if not repos:
        return _empty_status()
    shard_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    url_map = urls or {}
    pool_size = max(1, min(workers, len(repos)))
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        futures = [
            pool.submit(
                _mine_repo,
                repo,
                shard_dir,
                work_dir,
                timeout_seconds,
                url_map.get(repo),
                since,
            )
            for repo in repos
        ]
        for future in as_completed(futures):
            rows.append(future.result())
    frame = pd.DataFrame(rows).loc[:, _STATUS_COLUMNS]
    return frame.sort_values("repo", kind="mergesort").reset_index(drop=True)


def duplicate_change_keys(changes: pd.DataFrame) -> int:
    """Return how many change keys are stored on more than one row.

    A key is ``repo``, ``skill_folder``, ``commit`` and ``file_path``.
    One file in one commit should be one row. The count is the number of
    repeated keys, not the number of extra rows.

    Example:
    >>> rows = pd.DataFrame({
    ...     "repo": ["a/b", "a/b", "a/b"],
    ...     "skill_folder": ["s", "s", "s"],
    ...     "commit": ["c1", "c1", "c2"],
    ...     "file_path": ["SKILL.md", "SKILL.md", "SKILL.md"],
    ... })
    >>> duplicate_change_keys(rows)
    1
    """
    if changes.empty:
        return 0
    sizes = changes.groupby(_CHANGE_KEY, sort=False).size()
    return int((sizes > 1).sum())


def save_outputs(
    shard_dir: Path,
    out_dir: Path,
    keep_repos: list[str] | None = None,
) -> tuple[Path, Path, pd.DataFrame, int]:
    """Merge every finished shard and add per-repo folder and change counts.

    The status file from ``merge_shards`` has no ``skill_folders`` or
    ``changes`` column. Those counts come from the changes table: distinct
    skill folders, and change rows, for that repo. A repo with no change
    rows gets 0. The changes table is read with DuckDB, not loaded into
    pandas, because it holds millions of rows.

    When ``keep_repos`` is set, repos that are not on that list are removed
    from the three parquet files. Their shard files are left on disk.

    Inputs: ``shard_dir`` and ``out_dir`` (the ``registry_history`` folder).
    ``keep_repos`` is the current registry list, or None to keep every shard.
    Outputs: the changes path, the texts path, the status frame, and how
    many merged repos were not on ``keep_repos``.
    """
    changes_path, texts_path, status_path = _output_paths(out_dir)
    merge_shards(shard_dir, changes_path, texts_path, status_path)
    status = read_parquet(status_path)
    status, left_out = _drop_unlisted_repos(changes_path, status, texts_path, keep_repos)
    status = _attach_counts(status, _counts_by_repo(changes_path))
    write_parquet(status, status_path, sort_by=["repo"])
    return changes_path, texts_path, status, left_out


def repos_needing_deleted_check(repos: list[str], shard_dir: Path) -> list[str]:
    """``ok`` shards not yet checked for deleted skill folders, in list order.

    A shard is checked once ``mine_deleted_folders`` has run on it and
    ``append_to_repo_shard`` wrote ``deleted_folders_checked: true``. A shard
    mined by the fixed code with every folder already includes deleted
    folders, so it counts as checked when ``deleted_folders`` is a number.

    Example:
    >>> import tempfile, json
    >>> folder = Path(tempfile.mkdtemp())
    >>> for stem, body in [("a__x", {"status": "ok"}),
    ...                    ("b__y", {"status": "ok", "deleted_folders_checked": True}),
    ...                    ("c__z", {"status": "gone"})]:
    ...     _ = (folder / f"{stem}.status.json").write_text(json.dumps(body))
    >>> repos_needing_deleted_check(["a/x", "b/y", "c/z", "d/w"], folder)
    ['a/x']
    """
    needed: list[str] = []
    for repo in repos:
        path = shard_dir / f"{repo.replace('/', '__', 1)}.status.json"
        if not path.is_file():
            continue
        try:
            status = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if status.get("status") != "ok" or status.get("deleted_folders_checked"):
            continue
        if status.get("deleted_folders") is not None:
            continue
        needed.append(repo)
    return needed


def mine_deleted_registries(
    repos: list[str],
    shard_dir: Path,
    work_dir: Path,
    workers: int,
    timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS,
    urls: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Add deleted skill folders to finished shards, ``workers`` repos at a time.

    Each repo is cloned again, its history is compared with its shard, and
    only the missing folders are logged and appended. One line per repo:
    ``deleted <repo> <status> <seconds> <folders> <rows>``. A failed repo keeps
    its shard as it was and is tried again on the next run.

    Inputs: repos with ``ok`` shards, the shard and work folders, the pool
    size, the git timeout, and clone URLs for tests.
    Outputs: one row per repo: ``repo``, ``status``, ``message``,
    ``deleted_folders``, ``rows``, ``seconds``.
    """
    columns = ["repo", "status", "message", "deleted_folders", "rows", "seconds"]
    if not repos:
        return pd.DataFrame(columns=columns)
    work_dir.mkdir(parents=True, exist_ok=True)
    url_map = urls or {}
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(repos)))) as pool:
        futures = [
            pool.submit(_add_deleted_folders, repo, shard_dir, work_dir, timeout_seconds,
                        url_map.get(repo))
            for repo in repos
        ]
        for future in as_completed(futures):
            rows.append(future.result())
    frame = pd.DataFrame(rows).loc[:, columns]
    return frame.sort_values("repo", kind="mergesort").reset_index(drop=True)


def main() -> None:
    """Download registry history, then merge the shards.

    ``--dev`` reads the 30-registry list and writes the dev folder.
    ``--limit N`` keeps the first N names. ``--force`` merges again even
    when the three files exist; it does not re-clone a finished repo.
    ``--retry-failed`` re-clones ``timeout`` and ``error`` repos.
    ``--timeout`` sets the git limit for this run. ``--since YYYY-MM-DD``
    clones only history from that date for the repos mined in this run.
    ``--merge-only`` skips the clones and only merges.
    ``--deleted-folders`` adds skill folders that exist only in the history
    to finished shards (bug B02), optionally only for ``--repos-file``.

    Inputs: command-line flags and the stage 2 registry parquet.
    Outputs: the three parquet files. Nothing is returned.
    """
    args = _parser().parse_args()
    timeout_seconds = _timeout_seconds(args.timeout)
    since = _since_date(args.since)
    # Step 1: Read the registry list
    listed = read_registry_list(args.dev)
    repos = _apply_limit(listed, args.limit)
    history = _history_dir(args.dev)
    shard_dir = history / "shards"
    work_dir = history / "work"
    output_paths = _output_paths(history)
    if args.deleted_folders:
        _run_deleted_folders(args, repos, listed, history, timeout_seconds)
        return
    # Step 2: Skip repos whose shard status is ok or gone
    pending = [] if args.merge_only else repos_still_to_mine(repos, shard_dir, args.retry_failed)
    if _can_skip(args, output_paths, pending):
        print("skip: registry history already exists (pass --force to merge again)", flush=True)
        loaded = _load_report(history)
        _print_report(
            *loaded,
            elapsed=None,
            history_dir=history,
            shards_not_in_list=_repos_not_listed(loaded[2], listed),
        )
        return
    started = time.perf_counter()
    print(
        f"registries: {len(repos)} pending: {len(pending)} timeout: {timeout_seconds}"
        f" since: {since or 'all history'}",
        flush=True,
    )
    # Step 3: Mine with a pool of workers
    # Step 4: Log one line per repo (printed inside mine_registries)
    if pending:
        print(f"Step 3: mine {len(pending)} repos with {DOWNLOAD_WORKERS} workers", flush=True)
        mine_registries(
            pending,
            shard_dir,
            work_dir,
            DOWNLOAD_WORKERS,
            timeout_seconds=timeout_seconds,
            since=since,
        )
    # Step 5: Merge all shards
    print("Step 5: merge shards", flush=True)
    changes_path, texts_path, status, left_out = save_outputs(shard_dir, history, listed)
    # Step 6: Summary
    elapsed = time.perf_counter() - started
    _print_report(
        changes_path,
        texts_path,
        status,
        elapsed=elapsed,
        history_dir=history,
        shards_not_in_list=left_out,
    )


def _parser() -> argparse.ArgumentParser:
    """Parser for this stage: the shared flags, plus limit, retry, merge and timeout."""
    parser = build_arg_parser("Download skill-folder history for every selected registry.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="mine only the first N registry names, after sorting",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="mine repos whose shard status is timeout or error",
    )
    parser.add_argument(
        "--merge-only",
        action="store_true",
        help="do not clone; merge the shards already on disk",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=(
            "seconds for each git command "
            f"(default {DOWNLOAD_TIMEOUT_SECONDS}). "
            "With --retry-failed, only timeout and error repos are mined again."
        ),
    )
    parser.add_argument(
        "--deleted-folders",
        action="store_true",
        help=(
            "add skill folders that exist only in the history (deleted before the download) "
            "to finished ok shards, then merge"
        ),
    )
    parser.add_argument(
        "--repos-file",
        default=None,
        metavar="PATH",
        help="with --deleted-folders: only these repos (one owner/repo per line)",
    )
    parser.add_argument(
        "--since",
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "clone only commits from this date on (git --shallow-since). "
            "Applies to the repos mined in this run; finished shards keep their history."
        ),
    )
    return parser


def _run_deleted_folders(
    args: argparse.Namespace,
    repos: list[str],
    listed: list[str],
    history: Path,
    timeout_seconds: int,
) -> None:
    """``--deleted-folders``: add history-only skill folders to ok shards, then merge."""
    shard_dir = history / "shards"
    chosen = repos
    if args.repos_file:
        wanted = _read_repo_names(Path(args.repos_file))
        chosen = [repo for repo in repos if repo in wanted]
    pending = repos_needing_deleted_check(chosen, shard_dir)
    started = time.perf_counter()
    print(f"deleted-folder check: {len(chosen)} chosen, {len(pending)} not yet checked", flush=True)
    found = mine_deleted_registries(
        pending, shard_dir, history / "work", DOWNLOAD_WORKERS, timeout_seconds=timeout_seconds
    )
    ok = found[found["status"] == "ok"]
    print(
        f"deleted-folder check done: ok {len(ok)} failed {len(found) - len(ok)} "
        f"folders {int(ok['deleted_folders'].sum()) if len(ok) else 0} "
        f"rows {int(ok['rows'].sum()) if len(ok) else 0}",
        flush=True,
    )
    print("Step 5: merge shards", flush=True)
    changes_path, texts_path, status, left_out = save_outputs(shard_dir, history, listed)
    _print_report(
        changes_path,
        texts_path,
        status,
        elapsed=time.perf_counter() - started,
        history_dir=history,
        shards_not_in_list=left_out,
    )


def _add_deleted_folders(
    repo: str,
    shard_dir: Path,
    work_dir: Path,
    timeout_seconds: int,
    url: str | None,
) -> dict:
    """Mine one repo's deleted folders, append them to its shard, print one line."""
    shard_changes = read_parquet(shard_dir / f"{repo.replace('/', '__', 1)}.changes.parquet")
    result, changes, texts = mine_deleted_folders(
        repo, work_dir, shard_changes, timeout_seconds, url=url
    )
    status, message = result.status, result.message
    if status == "ok":
        try:
            append_to_repo_shard(
                shard_dir, repo, changes, texts, result.deleted_folders,
                result.history_folders_dropped, result.message,
            )
        except OSError as error:
            status, message = "error", _last_error_line(error)
    print(
        f"deleted {repo} {status} {result.seconds:.1f} {result.deleted_folders} {len(changes)}",
        flush=True,
    )
    return {
        "repo": repo,
        "status": status,
        "message": message,
        "deleted_folders": result.deleted_folders,
        "rows": len(changes),
        "seconds": result.seconds,
    }


def _read_repo_names(path: Path) -> set[str]:
    """``owner/repo`` names from a text file, one per line. Blank and ``#`` lines are skipped."""
    if not path.is_file():
        raise SystemExit(f"--repos-file not found: {path}")
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            names.add(text)
    return names


def _registry_list_path(dev: bool) -> Path:
    """Stage 2 parquet. Dev and full lists both sit in the full data folder."""
    name = "dev_registries.parquet" if dev else "registries.parquet"
    return data_dir(False) / name


def _history_dir(dev: bool) -> Path:
    """``registry_history`` under the dev or full data folder."""
    folder = data_dir(dev) / "registry_history"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _output_paths(history: Path) -> tuple[Path, Path, Path]:
    """Changes, texts and status parquet paths inside ``registry_history``."""
    return (
        history / "registry_history_changes.parquet",
        history / "registry_history_texts.parquet",
        history / "registry_history_status.parquet",
    )


def _apply_limit(repos: list[str], limit: int | None) -> list[str]:
    """Keep the first ``limit`` names. None keeps the whole list."""
    if limit is None:
        return repos
    if limit < 0:
        raise SystemExit("--limit must be 0 or greater")
    return repos[:limit]


def _timeout_seconds(value: int | None) -> int:
    """Use the flag when it is set, otherwise the settings default."""
    if value is None:
        return DOWNLOAD_TIMEOUT_SECONDS
    if value <= 0:
        raise SystemExit("--timeout must be a positive number of seconds")
    return value


def _can_skip(
    args: argparse.Namespace,
    output_paths: tuple[Path, Path, Path],
    pending: list[str],
) -> bool:
    """True when the three files exist and this run would not clone or rebuild."""
    if args.force or args.merge_only or pending:
        return False
    return outputs_exist(list(output_paths))


def _needs_mine(status: str | None, retry_failed: bool) -> bool:
    """Return True when this shard status should be cloned.

    None means there is no readable status file yet.

    Example:
    >>> _needs_mine("ok", True)
    False
    >>> _needs_mine("empty", True)
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


def _status_by_repo(shard_dir: Path) -> dict[str, str]:
    """Map ``owner/repo`` to its shard status. Unreadable files are ignored."""
    if not shard_dir.is_dir():
        return {}
    found: dict[str, str] = {}
    for path in sorted(shard_dir.glob("*.status.json")):
        repo, status = _read_status_pair(path)
        if repo and status:
            found[repo] = status
    return found


def _read_status_pair(path: Path) -> tuple[str, str]:
    """Read ``repo`` and ``status`` from one shard file. Bad JSON returns blanks."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    repo = payload.get("repo")
    status = payload.get("status")
    if not isinstance(repo, str) or not isinstance(status, str):
        return "", ""
    return repo, status


def _since_date(value: str | None) -> str | None:
    """Check ``--since``. None keeps the whole history.

    Example:
    >>> _since_date("2025-09-01")
    '2025-09-01'
    >>> _since_date(None) is None
    True
    """
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise SystemExit("--since must be a date like 2025-09-01") from None


def _mine_repo(
    repo: str,
    shard_dir: Path,
    work_dir: Path,
    timeout_seconds: int,
    url: str | None,
    since: str | None = None,
) -> dict:
    """Mine one repo, write its shard, and print one progress line."""
    result, changes, texts = mine_one_repo(
        repo, work_dir, None, timeout_seconds, url=url, since=since
    )
    try:
        write_repo_shard(shard_dir, repo, changes, texts, result)
    except OSError as error:
        # Step 4: one line per repo, flushed so a background log shows progress.
        print(f"{repo} error {result.seconds:.1f} 0", flush=True)
        return _status_row(result, 0, 0, status="error", message=_last_error_line(error))
    folder_count = 0 if changes.empty else int(changes["skill_folder"].nunique())
    change_count = len(changes)
    # Step 4: one line per repo, flushed so a background log shows progress.
    print(f"{repo} {result.status} {result.seconds:.1f} {change_count}", flush=True)
    return _status_row(result, folder_count, change_count)


def _status_row(
    result: RepoResult,
    folder_count: int,
    change_count: int,
    *,
    status: str | None = None,
    message: str | None = None,
) -> dict:
    """One status row. ``status`` and ``message`` override the mine result."""
    return {
        "repo": result.repo,
        "status": result.status if status is None else status,
        "message": result.message if message is None else message,
        "head_commit": result.head_commit,
        "seconds": result.seconds,
        "follow_renames": result.follow_renames,
        "not_ours": result.not_ours,
        "after_copy": result.after_copy,
        "twin": result.twin,
        "history_since": result.history_since,
        "deleted_folders": result.deleted_folders,
        "history_folders_dropped": result.history_folders_dropped,
        "skill_folders": folder_count,
        "changes": change_count,
    }


def _last_error_line(error: OSError) -> str:
    """The last line of a disk error, for the status message."""
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    if not lines:
        return "disk error"
    return lines[-1]


def _empty_status() -> pd.DataFrame:
    """Status frame with the output columns and no rows."""
    return pd.DataFrame(columns=_STATUS_COLUMNS)


def _attach_counts(status: pd.DataFrame, counts: pd.DataFrame) -> pd.DataFrame:
    """Add ``skill_folders`` and ``changes`` from ``_counts_by_repo``. Missing repos get 0."""
    if status.empty:
        return _empty_status()
    drop = [name for name in ("skill_folders", "changes") if name in status.columns]
    base = status.drop(columns=drop)
    merged = base.merge(counts, on="repo", how="left")
    merged["skill_folders"] = merged["skill_folders"].fillna(0).astype("int64")
    merged["changes"] = merged["changes"].fillna(0).astype("int64")
    for name in ("not_ours", "after_copy", "twin", "deleted_folders", "history_folders_dropped"):
        if name not in merged.columns:
            merged[name] = 0
        merged[name] = pd.to_numeric(merged[name], errors="coerce").fillna(0).astype("int64")
    # Shards written before --since existed have no history_since: full history.
    if "history_since" not in merged.columns:
        merged["history_since"] = pd.NA
    merged["history_since"] = merged["history_since"].astype("string")
    return merged.loc[:, _STATUS_COLUMNS]


def _counts_by_repo(changes_path: Path) -> pd.DataFrame:
    """Distinct skill folders and change rows for each repo, counted in DuckDB."""
    columns = ["repo", "skill_folders", "changes"]
    if _change_rows(changes_path) == 0:
        return pd.DataFrame(columns=columns)
    frame = _query(
        changes_path,
        f"""
        SELECT repo, count(DISTINCT skill_folder) AS skill_folders, count(*) AS changes
        FROM read_parquet({_sql_quote(str(changes_path))})
        GROUP BY repo
        """,
    )
    return frame.loc[:, columns]


def _change_rows(changes_path: Path) -> int:
    """Rows in the changes file, from its metadata. A missing file has 0."""
    if not changes_path.is_file():
        return 0
    metadata = pq.ParquetFile(changes_path).metadata
    return 0 if metadata is None else int(metadata.num_rows)


def _query(changes_path: Path, sql: str) -> pd.DataFrame:
    """Run one DuckDB query whose spill folder sits beside ``changes_path``."""
    con = connect_history_duckdb(changes_path.parent)
    try:
        return con.execute(sql).df()
    finally:
        con.close()


def _load_report(history: Path) -> tuple[Path, Path, pd.DataFrame]:
    """The three outputs already on disk, for a skip run. Changes stay on disk."""
    changes_path, texts_path, status_path = _output_paths(history)
    return changes_path, texts_path, read_parquet(status_path)


def _drop_unlisted_repos(
    changes_path: Path,
    status: pd.DataFrame,
    texts_path: Path,
    keep_repos: list[str] | None,
) -> tuple[pd.DataFrame, int]:
    """Remove repos that are not on the registry list. Shards are not deleted.

    The changes and texts files are rewritten in DuckDB, in their current order.
    """
    if keep_repos is None:
        return status, 0
    left_out = _repos_not_listed(status, keep_repos)
    if left_out == 0:
        return status, 0
    keep = set(keep_repos)
    kept_status = status[status["repo"].astype(str).isin(keep)].reset_index(drop=True)
    _keep_listed_changes(changes_path, sorted(keep))
    _rewrite_texts(texts_path, changes_path)
    return kept_status, left_out


def _keep_listed_changes(changes_path: Path, keep_repos: list[str]) -> None:
    """Rewrite the changes file with only ``keep_repos``, keeping the row order."""
    if _change_rows(changes_path) == 0:
        return
    tmp = changes_path.with_name(changes_path.stem + ".filtered.parquet")
    con = connect_history_duckdb(changes_path.parent)
    try:
        con.register("keep_repo", pd.DataFrame({"repo": pd.Series(keep_repos, dtype="string")}))
        con.execute(
            f"""
            COPY (
                SELECT * FROM read_parquet({_sql_quote(str(changes_path))})
                WHERE repo IN (SELECT repo FROM keep_repo)
            ) TO {_sql_quote(str(tmp))} (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        con.close()
    tmp.replace(changes_path)


def _repos_not_listed(status: pd.DataFrame, listed: list[str]) -> int:
    """How many status rows name a repo that is not on the registry list."""
    if status.empty:
        return 0
    listed_names = set(listed)
    return int((~status["repo"].astype(str).isin(listed_names)).sum())


def _rewrite_texts(path: Path, changes_path: Path) -> None:
    """Drop text rows whose sha no change row uses. Neither file is loaded into pandas.

    DuckDB streams the parquet into a temp file, in groups of ``TEXT_ROW_GROUP``
    rows. That file then replaces the original. A failed copy leaves the original.
    """
    tmp = path.with_name(path.stem + ".filtered.parquet")
    if tmp.exists():
        tmp.unlink()
    con = connect_duckdb()
    wrote = False
    try:
        counted = con.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchall()
        if not counted or int(counted[0][0]) == 0:
            return
        changes = _sql_quote(str(changes_path))
        con.execute(
            f"""
            CREATE TEMP TABLE keep_sha AS
            SELECT lower(old_sha) AS sha FROM read_parquet({changes})
            UNION
            SELECT lower(new_sha) FROM read_parquet({changes})
            """
        )
        con.execute(
            f"""
            COPY (
                SELECT sha, text
                FROM read_parquet({_sql_quote(str(path))})
                WHERE lower(sha) IN (SELECT sha FROM keep_sha)
                ORDER BY sha
            ) TO {_sql_quote(str(tmp))} (
                FORMAT PARQUET,
                COMPRESSION ZSTD,
                ROW_GROUP_SIZE {TEXT_ROW_GROUP}
            )
            """
        )
        wrote = True
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    finally:
        con.close()
    if wrote:
        tmp.replace(path)


def _sql_quote(value: str) -> str:
    """A single-quoted SQL string, with quotes escaped."""
    return "'" + value.replace("'", "''") + "'"


def _print_report(
    changes_path: Path,
    texts_path: Path,
    status: pd.DataFrame,
    *,
    elapsed: float | None,
    history_dir: Path,
    shards_not_in_list: int = 0,
) -> None:
    """Print counts and two sample rows of each output."""
    totals = _change_totals(changes_path)
    text_rows, scripts_with_text, text_bytes = _text_stats(
        texts_path, _script_shas(changes_path)
    )
    counts = _status_counts(status)
    dropped = _dropped_row_totals(status)
    repeated = _duplicate_change_keys_in(changes_path)
    if repeated:
        print(f"warning: duplicate_change_keys {repeated}", flush=True)
    numbers: dict[str, object] = {
        "repos_ok": counts["ok"],
        "repos_gone": counts["gone"],
        "repos_empty": counts["empty"],
        "repos_timeout": counts["timeout"],
        "repos_error": counts["error"],
        "skill_folders": totals["skill_folders"],
        "changes": totals["changes"],
        "skill_md_versions": totals["skill_md_versions"],
        "not_ours": dropped["not_ours"],
        "after_copy": dropped["after_copy"],
        "twin": dropped["twin"],
        "scripts_with_text": scripts_with_text,
        "text_rows": text_rows,
        "text_bytes": text_bytes,
        "duplicate_change_keys": repeated,
        "shards_not_in_list": shards_not_in_list,
        "disk_bytes": _folder_bytes(history_dir),
    }
    if elapsed is not None:
        numbers["elapsed_seconds"] = round(elapsed, 1)
    print_summary("Stage 3 summary", numbers)
    print_sample_records(_change_sample(changes_path), "Sample changes", 2)
    print_sample_records(_text_sample(texts_path), "Sample texts", 2)
    print_sample_records(status, "Sample status", 2)


def _dropped_row_totals(status: pd.DataFrame) -> dict[str, int]:
    """Rows dropped because another folder was not this skill, summed over repos."""
    totals = {}
    for name in ("not_ours", "after_copy", "twin"):
        if status.empty or name not in status.columns:
            totals[name] = 0
            continue
        totals[name] = int(pd.to_numeric(status[name], errors="coerce").fillna(0).sum())
    return totals


def _status_counts(status: pd.DataFrame) -> dict[str, int]:
    """How many repos finished in each status. Unknown names are left out."""
    if status.empty:
        return {name: 0 for name in _STATUS_NAMES}
    tallies = status["status"].astype(str).value_counts()
    return {name: int(tallies.get(name, 0)) for name in _STATUS_NAMES}


def _change_totals(changes_path: Path) -> dict[str, int]:
    """Skill folders, change rows, and SKILL.md change rows, counted in DuckDB."""
    if _change_rows(changes_path) == 0:
        return {"skill_folders": 0, "changes": 0, "skill_md_versions": 0}
    row = _query(
        changes_path,
        f"""
        SELECT count(DISTINCT (repo, skill_folder)) AS skill_folders,
               count(*) AS changes,
               count(*) FILTER (WHERE is_skill_md) AS skill_md_versions
        FROM read_parquet({_sql_quote(str(changes_path))})
        """,
    ).iloc[0]
    return {name: int(row[name]) for name in ("skill_folders", "changes", "skill_md_versions")}


def _duplicate_change_keys_in(changes_path: Path) -> int:
    """``duplicate_change_keys`` for the changes file, counted in DuckDB."""
    if _change_rows(changes_path) == 0:
        return 0
    keys = ", ".join(f'"{name}"' for name in _CHANGE_KEY)
    frame = _query(
        changes_path,
        f"""
        SELECT count(*) AS repeated FROM (
            SELECT 1 FROM read_parquet({_sql_quote(str(changes_path))})
            GROUP BY {keys} HAVING count(*) > 1
        )
        """,
    )
    return int(frame.iloc[0]["repeated"])


def _change_sample(changes_path: Path, count: int = 2) -> pd.DataFrame:
    """The first ``count`` change rows. The file is not loaded as a whole."""
    if _change_rows(changes_path) == 0:
        return pd.DataFrame()
    parquet = pq.ParquetFile(changes_path)
    return next(parquet.iter_batches(batch_size=count)).to_pandas()


def _script_shas(changes_path: Path) -> set[str]:
    """Blob ids of script files. Blank and all-zero ids are left out."""
    if _change_rows(changes_path) == 0:
        return set()
    frame = _query(
        changes_path,
        f"""
        SELECT DISTINCT lower(new_sha) AS sha
        FROM read_parquet({_sql_quote(str(changes_path))})
        WHERE is_script AND new_sha IS NOT NULL
        """,
    )
    return {
        sha
        for sha in frame["sha"].astype(str)
        if sha not in {"", "none", "nan"} and set(sha) != {"0"}
    }


def _text_stats(path: Path, script_shas: set[str]) -> tuple[int, int, int]:
    """Return text rows, script texts, and utf-8 bytes. Bodies are not kept."""
    if not path.is_file():
        return 0, 0, 0
    rows = 0
    scripts = 0
    total_bytes = 0
    parquet = pq.ParquetFile(path)
    batches = parquet.iter_batches(batch_size=TEXT_ROW_GROUP, columns=["sha", "text"])
    for batch in batches:
        bodies = batch.column("text").to_pylist()
        for sha, body in zip(batch.column("sha").to_pylist(), bodies, strict=True):
            text = body if isinstance(body, str) else ""
            rows += 1
            total_bytes += len(text.encode("utf-8"))
            if isinstance(sha, str) and sha.lower() in script_shas:
                scripts += 1
    return rows, scripts, total_bytes


def _text_sample(path: Path, count: int = 2) -> pd.DataFrame:
    """The first ``count`` text rows. The file is not loaded as a whole."""
    columns = ["sha", "text"]
    if not path.is_file():
        return pd.DataFrame(columns=columns)
    parquet = pq.ParquetFile(path)
    if parquet.metadata is None or parquet.metadata.num_rows == 0:
        return pd.DataFrame(columns=columns)
    batch = next(parquet.iter_batches(batch_size=count, columns=columns))
    return batch.to_pandas()


def _folder_bytes(folder: Path) -> int:
    """Bytes of every file under ``folder``, including shards."""
    if not folder.exists():
        return 0
    return sum(item.stat().st_size for item in folder.rglob("*") if item.is_file())


if __name__ == "__main__":
    main()
