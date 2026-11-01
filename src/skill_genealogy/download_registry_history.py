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
    merge_shards,
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
) -> pd.DataFrame:
    """Clone repos in parallel and write one shard each.

    Git runs in subprocesses, so the pool is threads. Each clone is deleted
    when that repo finishes. One log line is printed per repo:
    ``repo status seconds changes``.

    Inputs: ``repos`` to mine. ``shard_dir`` and ``work_dir`` are created if
    missing. ``workers`` is the pool size. ``timeout_seconds`` limits each
    git command. ``urls`` maps a repo name to a clone URL; tests pass
    ``file://``. A missing URL uses GitHub.
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
) -> tuple[pd.DataFrame, Path, pd.DataFrame, int]:
    """Merge every finished shard and add per-repo folder and change counts.

    The status file from ``merge_shards`` has no ``skill_folders`` or
    ``changes`` column. Those counts come from the changes table: distinct
    skill folders, and change rows, for that repo. A repo with no change
    rows gets 0.

    When ``keep_repos`` is set, repos that are not on that list are removed
    from the three parquet files. Their shard files are left on disk.

    Inputs: ``shard_dir`` and ``out_dir`` (the ``registry_history`` folder).
    ``keep_repos`` is the current registry list, or None to keep every shard.
    Outputs: the changes frame, the texts path, the status frame, and how
    many merged repos were not on ``keep_repos``.
    """
    changes_path, texts_path, status_path = _output_paths(out_dir)
    merge_shards(shard_dir, changes_path, texts_path, status_path)
    changes = read_parquet(changes_path)
    status = read_parquet(status_path)
    changes, status, left_out = _drop_unlisted_repos(changes, status, texts_path, keep_repos)
    status = _attach_counts(status, changes)
    write_parquet(
        changes,
        changes_path,
        sort_by=["repo", "skill_folder", "commit_seq", "file_path"],
    )
    write_parquet(status, status_path, sort_by=["repo"])
    return changes, texts_path, status, left_out


def main() -> None:
    """Download registry history, then merge the shards.

    ``--dev`` reads the 30-registry list and writes the dev folder.
    ``--limit N`` keeps the first N names. ``--force`` merges again even
    when the three files exist; it does not re-clone a finished repo.
    ``--retry-failed`` re-clones ``timeout`` and ``error`` repos.
    ``--timeout`` sets the git limit for this run. ``--merge-only`` skips
    the clones and only merges.

    Inputs: command-line flags and the stage 2 registry parquet.
    Outputs: the three parquet files. Nothing is returned.
    """
    args = _parser().parse_args()
    timeout_seconds = _timeout_seconds(args.timeout)
    # Step 1: Read the registry list
    listed = read_registry_list(args.dev)
    repos = _apply_limit(listed, args.limit)
    history = _history_dir(args.dev)
    shard_dir = history / "shards"
    work_dir = history / "work"
    output_paths = _output_paths(history)
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
        f"registries: {len(repos)} pending: {len(pending)} timeout: {timeout_seconds}",
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
        )
    # Step 5: Merge all shards
    print("Step 5: merge shards", flush=True)
    changes, texts_path, status, left_out = save_outputs(shard_dir, history, listed)
    # Step 6: Summary
    elapsed = time.perf_counter() - started
    _print_report(
        changes,
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
    return parser


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


def _mine_repo(
    repo: str,
    shard_dir: Path,
    work_dir: Path,
    timeout_seconds: int,
    url: str | None,
) -> dict:
    """Mine one repo, write its shard, and print one progress line."""
    result, changes, texts = mine_one_repo(repo, work_dir, None, timeout_seconds, url=url)
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


def _attach_counts(status: pd.DataFrame, changes: pd.DataFrame) -> pd.DataFrame:
    """Add ``skill_folders`` and ``changes``. Missing repos get 0."""
    if status.empty:
        return _empty_status()
    counts = _counts_by_repo(changes)
    drop = [name for name in ("skill_folders", "changes") if name in status.columns]
    base = status.drop(columns=drop)
    merged = base.merge(counts, on="repo", how="left")
    merged["skill_folders"] = merged["skill_folders"].fillna(0).astype("int64")
    merged["changes"] = merged["changes"].fillna(0).astype("int64")
    return merged.loc[:, _STATUS_COLUMNS]


def _counts_by_repo(changes: pd.DataFrame) -> pd.DataFrame:
    """Distinct skill folders and change rows for each repo."""
    columns = ["repo", "skill_folders", "changes"]
    if changes.empty:
        return pd.DataFrame(columns=columns)
    grouped = changes.groupby("repo", sort=False).agg(
        skill_folders=("skill_folder", "nunique"),
        change_rows=("file_path", "size"),
    )
    grouped = grouped.reset_index().rename(columns={"change_rows": "changes"})
    return grouped.loc[:, columns]


def _load_report(history: Path) -> tuple[pd.DataFrame, Path, pd.DataFrame]:
    """Read the three outputs already on disk, for a skip run."""
    changes_path, texts_path, status_path = _output_paths(history)
    return read_parquet(changes_path), texts_path, read_parquet(status_path)


def _drop_unlisted_repos(
    changes: pd.DataFrame,
    status: pd.DataFrame,
    texts_path: Path,
    keep_repos: list[str] | None,
) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Remove repos that are not on the registry list. Shards are not deleted."""
    if keep_repos is None:
        return changes, status, 0
    keep = set(keep_repos)
    left_out = _repos_not_listed(status, keep_repos)
    if left_out == 0:
        return changes, status, 0
    kept_changes = changes[changes["repo"].astype(str).isin(keep)].reset_index(drop=True)
    kept_status = status[status["repo"].astype(str).isin(keep)].reset_index(drop=True)
    _rewrite_texts(texts_path, _blob_shas(kept_changes))
    return kept_changes, kept_status, left_out


def _repos_not_listed(status: pd.DataFrame, listed: list[str]) -> int:
    """How many status rows name a repo that is not on the registry list."""
    if status.empty:
        return 0
    listed_names = set(listed)
    return int((~status["repo"].astype(str).isin(listed_names)).sum())


def _blob_shas(changes: pd.DataFrame) -> set[str]:
    """Blob ids referenced by change rows. Blank and all-zero ids are left out."""
    if changes.empty:
        return set()
    found: set[str] = set()
    for column in ("old_sha", "new_sha"):
        if column not in changes.columns:
            continue
        for sha in changes[column].astype(str):
            lowered = sha.lower()
            if lowered in {"", "none", "nan"} or set(lowered) == {"0"}:
                continue
            found.add(lowered)
    return found


def _rewrite_texts(path: Path, keep_shas: set[str]) -> None:
    """Drop text rows whose sha is unused. The texts file is not loaded into pandas.

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
        kept = pd.DataFrame({"sha": pd.Series(list(keep_shas), dtype="string")})
        con.register("keep_sha", kept)
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
    changes: pd.DataFrame,
    texts_path: Path,
    status: pd.DataFrame,
    *,
    elapsed: float | None,
    history_dir: Path,
    shards_not_in_list: int = 0,
) -> None:
    """Print counts and two sample rows of each output."""
    totals = _change_totals(changes)
    text_rows, scripts_with_text, text_bytes = _text_stats(texts_path, _script_shas(changes))
    counts = _status_counts(status)
    repeated = duplicate_change_keys(changes)
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
    print_sample_records(changes, "Sample changes", 2)
    print_sample_records(_text_sample(texts_path), "Sample texts", 2)
    print_sample_records(status, "Sample status", 2)


def _status_counts(status: pd.DataFrame) -> dict[str, int]:
    """How many repos finished in each status. Unknown names are left out."""
    if status.empty:
        return {name: 0 for name in _STATUS_NAMES}
    tallies = status["status"].astype(str).value_counts()
    return {name: int(tallies.get(name, 0)) for name in _STATUS_NAMES}


def _change_totals(changes: pd.DataFrame) -> dict[str, int]:
    """Skill folders, change rows, and SKILL.md change rows."""
    if changes.empty:
        return {"skill_folders": 0, "changes": 0, "skill_md_versions": 0}
    folders = int(changes.groupby(["repo", "skill_folder"], sort=False).ngroups)
    return {
        "skill_folders": folders,
        "changes": len(changes),
        "skill_md_versions": int(changes["is_skill_md"].sum()),
    }


def _script_shas(changes: pd.DataFrame) -> set[str]:
    """Blob ids of script files. Blank and all-zero ids are left out."""
    if changes.empty:
        return set()
    mask = changes["is_script"].fillna(False).astype(bool)
    found: set[str] = set()
    for sha in changes.loc[mask, "new_sha"].astype(str):
        lowered = sha.lower()
        if lowered in {"", "none", "nan"} or set(lowered) == {"0"}:
            continue
        found.add(lowered)
    return found


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
