"""Stage 3b: download the README of every registry.

What: a registry's README often says what the repo is ("an aggregate catalog",
"a mirror", "our own skills"). Stage 3 keeps only skill files, so this stage
fetches one more file per registry: the README GitHub would show. It reads the
same commit stage 3 mined (``head_commit`` in ``registry_history_status``), so
the README and the history describe one snapshot. The download is the stage 3
method on a small scale: fetch that one commit without file contents or history
(GIT10), list the README places (GIT11), and fetch only the README blob (GIT8).
When that commit cannot be fetched (for example after a force push), or stage 3
has no commit for the repo, the newest commit is used instead (GIT9), and
``commit_basis`` says so.

Order: stage 3 (``download_registry_history``), then this stage. A later step
reads the README text together with the stage 3 history to tell a copier from
a source.

Inputs: ``registries.parquet`` (or ``dev_registries.parquet`` with ``--dev``)
and ``registry_history/registry_history_status.parquet`` from stage 3.
Outputs: ``registry_readmes.parquet`` in the dev or full data folder. One row
per registry: ``repo``, ``status``, ``message``, ``commit``, ``commit_basis``,
``readme_path``, ``readme_sha``, ``readme_bytes``, ``readme_text``,
``seconds``, ``fetched_at``.

``status`` is ``ok``, ``no_readme``, ``gone``, ``empty``, ``timeout`` or
``error``. A later run skips ``ok``, ``no_readme``, ``gone`` and ``empty``.
``timeout`` and ``error`` are fetched again only with ``--retry-failed``.
``--force`` fetches every registry again. The table is saved every
``SAVE_EVERY`` repos, so a stopped run continues.

Run (network):
    python -m skill_genealogy.download_registry_readmes --dev --limit 5
    python -m skill_genealogy.download_registry_readmes
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pandas as pd

from skill_genealogy import settings
from skill_genealogy.download_registry_history import read_registry_list
from skill_genealogy.file_helpers import (
    build_arg_parser,
    print_sample_records,
    print_summary,
    read_parquet,
    write_parquet,
)
from skill_genealogy.git_history import (
    GitError,
    classify_git_error,
    clone_commit_tree_only,
    clone_head_tree_only,
    head_commit,
    list_files_at_commit,
    read_blob_texts,
)

# Results that will not change on a second try.
_FINISHED_STATUS = frozenset({"ok", "no_readme", "gone", "empty"})
# Fetched again only with --retry-failed.
_RETRY_STATUS = frozenset({"timeout", "error"})
# Write the table after this many finished repos.
SAVE_EVERY = 200
# Git file modes that hold a regular file (not a link or a submodule).
_FILE_MODES = frozenset({"100644", "100755"})
README_COLUMNS: tuple[str, ...] = (
    "repo",
    "status",
    "message",
    "commit",
    "commit_basis",
    "readme_path",
    "readme_sha",
    "readme_bytes",
    "readme_text",
    "seconds",
    "fetched_at",
)


def readmes_path(dev: bool) -> Path:
    """``registry_readmes.parquet`` in the dev or full data folder."""
    return settings.data_dir(dev) / "registry_readmes.parquet"


def read_mined_commits(dev: bool) -> dict[str, str | None]:
    """The commit stage 3 mined for each repo, or None when it has none.

    Inputs: ``dev`` picks the dev or full ``registry_history`` folder.
    Outputs: ``owner/repo`` to a full commit id or None. A missing stage 3
    status file stops the run and names the command that builds it.
    """
    path = settings.data_dir(dev) / "registry_history" / "registry_history_status.parquet"
    if not path.is_file():
        flag = " --dev" if dev else ""
        raise SystemExit(
            f"missing {path}; run: python -m skill_genealogy.download_registry_history{flag}"
        )
    status = read_parquet(path)
    commits: dict[str, str | None] = {}
    for row in status.loc[:, ["repo", "head_commit"]].to_dict("records"):
        commit = row["head_commit"]
        commits[str(row["repo"])] = str(commit) if isinstance(commit, str) and commit else None
    return commits


def choose_readme(entries: list[dict]) -> dict | None:
    """The README GitHub would show, from the files listed at one commit.

    Rule: a regular file whose name is in ``README_FILE_NAMES`` (any case)
    inside a folder of ``README_FOLDERS``. The earlier folder wins
    (``.github``, then the root, then ``docs``), then the earlier name
    (``readme.md`` before ``readme.rst``), then the path.

    Inputs: dicts with ``path``, ``mode``, ``type`` and ``sha`` (GIT11 rows).
    Outputs: the chosen dict, or None when there is no README.

    Example:
    >>> rows = [
    ...     {"path": "README.rst", "mode": "100644", "type": "blob", "sha": "a"},
    ...     {"path": "readme.md", "mode": "100644", "type": "blob", "sha": "b"},
    ...     {"path": "docs/README.md", "mode": "100644", "type": "blob", "sha": "c"},
    ... ]
    >>> choose_readme(rows)["path"]
    'readme.md'
    >>> rows.append({"path": ".github/README.md", "mode": "100644", "type": "blob", "sha": "d"})
    >>> choose_readme(rows)["path"]
    '.github/README.md'
    >>> choose_readme([{"path": "README", "mode": "120000", "type": "blob", "sha": "e"}]) is None
    True
    """
    best: tuple[int, int, str] | None = None
    chosen: dict | None = None
    for entry in entries:
        if entry.get("type") != "blob" or entry.get("mode") not in _FILE_MODES:
            continue
        path = PurePosixPath(str(entry["path"]))
        folder = "" if str(path.parent) == "." else str(path.parent)
        name = path.name.lower()
        if folder not in settings.README_FOLDERS or name not in settings.README_FILE_NAMES:
            continue
        rank = (
            settings.README_FOLDERS.index(folder),
            settings.README_FILE_NAMES.index(name),
            str(path),
        )
        if best is None or rank < best:
            best, chosen = rank, entry
    return chosen


def fetch_repo_readme(
    repo: str,
    commit: str | None,
    work_dir: Path,
    timeout_seconds: int,
    url: str | None = None,
) -> dict[str, object]:
    """Fetch one registry's README at ``commit`` and return its row.

    With a ``commit``, GIT10 fetches that commit only (``commit_basis``
    ``stage3_head``). If that fails for a reason other than a timeout or a
    missing repo, or ``commit`` is None, GIT9 takes the newest commit instead
    (``current_head``). The clone is deleted before this returns.

    Inputs: ``repo`` is ``owner/repo``. ``commit`` is the stage 3 commit or
    None. ``work_dir`` holds the temporary clone. ``timeout_seconds`` limits
    each git command. ``url`` replaces the GitHub address (tests).
    Outputs: one row with the ``README_COLUMNS``.
    """
    started = time.perf_counter()
    row: dict[str, object] = dict.fromkeys(README_COLUMNS)
    row.update(repo=repo, status="error", message="")
    work_dir.mkdir(parents=True, exist_ok=True)
    clone_root = Path(tempfile.mkdtemp(prefix="readme_", dir=work_dir))
    try:
        used, basis, note = _clone_snapshot(repo, commit, clone_root, timeout_seconds, url)
        if used is None:
            row.update(status="empty", message="no commits on the default branch")
        else:
            row.update(commit=used, commit_basis=basis, message=note)
            row.update(_readme_fields(clone_root / "clone", used))
    except GitError as error:
        status, message = classify_git_error(error)
        row.update(status=status, message=message)
    except OSError as error:
        row.update(status="error", message=str(error).strip() or type(error).__name__)
    finally:
        shutil.rmtree(clone_root, ignore_errors=True)
    row["seconds"] = round(time.perf_counter() - started, 2)
    row["fetched_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return row


def repos_still_to_fetch(
    repos: list[str],
    done: pd.DataFrame,
    *,
    retry_failed: bool,
) -> list[str]:
    """Registries without a finished row, in list order.

    ``ok``, ``no_readme``, ``gone`` and ``empty`` are finished. ``timeout`` and
    ``error`` count as finished unless ``retry_failed`` is true.

    Example:
    >>> done = pd.DataFrame({"repo": ["a/x", "b/y", "c/z"], "status": ["ok", "error", "no_readme"]})
    >>> repos_still_to_fetch(["a/x", "b/y", "c/z", "d/w"], done, retry_failed=False)
    ['d/w']
    >>> repos_still_to_fetch(["a/x", "b/y", "c/z", "d/w"], done, retry_failed=True)
    ['b/y', 'd/w']
    """
    if done.empty:
        return list(repos)
    status = dict(zip(done["repo"].astype(str), done["status"].astype(str), strict=True))
    skip = set(_FINISHED_STATUS)
    if not retry_failed:
        skip |= _RETRY_STATUS
    return [repo for repo in repos if status.get(repo) not in skip]


def download_registry_readmes(
    repos: list[str],
    commits: dict[str, str | None],
    out_path: Path,
    *,
    previous: pd.DataFrame | None = None,
    workers: int = settings.DOWNLOAD_WORKERS,
    timeout_seconds: int = settings.DOWNLOAD_TIMEOUT_SECONDS,
    urls: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Fetch the README of each repo in ``repos`` and save them with earlier rows.

    A new row replaces an earlier row for the same repo. The table is written
    every ``SAVE_EVERY`` finished repos, with a progress line, and once at the end.

    Inputs: ``repos`` to fetch. ``commits`` maps a repo to its stage 3 commit.
    ``out_path`` is the parquet file. ``previous`` is the table already on
    disk, or None. ``workers`` clones run at once. ``urls`` maps a repo to a
    clone URL (tests).
    Outputs: the full table, sorted by repo, also written to ``out_path``.
    """
    rows = _rows_by_repo(previous)
    work_dir = out_path.parent / "readme_tmp"
    finished = 0
    if repos:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(repos)))) as pool:
            futures = [
                pool.submit(
                    fetch_repo_readme,
                    repo,
                    commits.get(repo),
                    work_dir,
                    timeout_seconds,
                    (urls or {}).get(repo),
                )
                for repo in repos
            ]
            for future in as_completed(futures):
                row = future.result()
                rows[str(row["repo"])] = row
                finished += 1
                if finished % SAVE_EVERY == 0:
                    _save(rows, out_path)
                    print(f"progress readmes {finished}/{len(repos)}", flush=True)
    shutil.rmtree(work_dir, ignore_errors=True)
    return _save(rows, out_path)


def summarize_readmes(table: pd.DataFrame, repos: list[str]) -> dict[str, object]:
    """Counts for the stage summary: status, commit basis and README sizes.

    Example:
    >>> table = pd.DataFrame({
    ...     "repo": ["a/x", "b/y", "c/z"],
    ...     "status": ["ok", "no_readme", "gone"],
    ...     "commit_basis": ["stage3_head", "current_head", None],
    ...     "readme_bytes": [1200, None, None],
    ...     "readme_text": ["# x", None, None],
    ... })
    >>> summarize_readmes(table, ["a/x", "b/y", "c/z", "d/w"])["not_fetched"]
    1
    """
    listed = table.loc[table["repo"].astype(str).isin(set(repos))]
    counts = listed["status"].astype(str).value_counts()
    basis = listed["commit_basis"].astype("string").value_counts()
    sizes = pd.to_numeric(listed["readme_bytes"], errors="coerce").dropna()
    numbers: dict[str, object] = {
        "registries": len(repos),
        "fetched": len(listed),
        "not_fetched": len(repos) - len(listed),
    }
    for name in sorted(_FINISHED_STATUS | _RETRY_STATUS):
        numbers[f"status_{name}"] = int(counts.get(name, 0))
    numbers["basis_stage3_head"] = int(basis.get("stage3_head", 0))
    numbers["basis_current_head"] = int(basis.get("current_head", 0))
    numbers["readme_text_kept"] = int(listed["readme_text"].notna().sum())
    numbers["median_readme_bytes"] = int(sizes.median()) if len(sizes) else 0
    return numbers


def main() -> None:
    """Read the registries and stage 3 commits, fetch the READMEs, save, summarise."""
    args = _parser().parse_args()
    # Step 1: Read the registry list and the commits stage 3 mined
    repos = read_registry_list(bool(args.dev))
    commits = read_mined_commits(bool(args.dev))
    out_path = readmes_path(bool(args.dev))
    # Step 2: Skip registries that already have a finished row
    previous = None if args.force or not out_path.is_file() else read_parquet(out_path)
    todo = repos_still_to_fetch(
        repos,
        previous if previous is not None else pd.DataFrame(columns=["repo", "status"]),
        retry_failed=bool(args.retry_failed),
    )
    if args.limit is not None:
        todo = todo[: max(0, int(args.limit))]
    print(f"registries: {len(repos)} to fetch: {len(todo)}", flush=True)
    started = time.perf_counter()
    # Step 3: Fetch each README at the stage 3 commit, 8 repos at a time
    table = download_registry_readmes(
        todo,
        commits,
        out_path,
        previous=previous,
        workers=int(args.workers),
        timeout_seconds=int(args.timeout),
    )
    # Step 4: Summary
    numbers = summarize_readmes(table, repos)
    numbers["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    numbers["output"] = str(out_path)
    print_summary("Stage 3b summary", numbers)
    print_sample_records(table.loc[table["status"] == "ok"], "Sample READMEs", 2)


def _clone_snapshot(
    repo: str,
    commit: str | None,
    clone_root: Path,
    timeout_seconds: int,
    url: str | None,
) -> tuple[str | None, str, str]:
    """Fetch the stage 3 commit, or the newest one. Return commit, basis and a note."""
    clone_dir = clone_root / "clone"
    if commit is not None:
        try:
            clone_commit_tree_only(repo, clone_dir, commit, timeout_seconds, url)
            return commit, "stage3_head", ""
        except GitError as error:
            status, message = classify_git_error(error)
            if status in {"gone", "timeout"}:
                raise
            shutil.rmtree(clone_dir, ignore_errors=True)
            note = f"stage 3 commit not fetched ({message}); used the newest commit"
    else:
        note = "stage 3 has no commit for this repo; used the newest commit"
    clone_head_tree_only(repo, clone_dir, timeout_seconds, url=url)
    return head_commit(clone_dir), "current_head", note


def _readme_fields(clone_dir: Path, commit: str) -> dict[str, object]:
    """Status and README columns for one fetched commit."""
    entries = list_files_at_commit(clone_dir, commit, list(settings.README_FOLDERS))
    chosen = choose_readme(entries)
    if chosen is None:
        return {"status": "no_readme"}
    sha = str(chosen["sha"])
    texts = read_blob_texts(clone_dir, [sha], settings.README_MAX_BYTES)
    fields: dict[str, object] = {
        "status": "ok",
        "readme_path": str(chosen["path"]),
        "readme_sha": sha,
    }
    text = texts.get(sha)
    if text is None:
        limit = settings.README_MAX_BYTES
        fields["message"] = f"README is larger than {limit} bytes; text not kept"
        return fields
    fields["readme_text"] = text
    fields["readme_bytes"] = len(text.encode("utf-8"))
    return fields


def _parser() -> argparse.ArgumentParser:
    """Stage flags plus ``--limit``, ``--workers``, ``--timeout``, ``--retry-failed``."""
    parser = build_arg_parser("Download the README of every registry, at the stage 3 commit.")
    parser.add_argument("--limit", type=int, default=None, help="fetch at most N registries")
    parser.add_argument(
        "--workers",
        type=int,
        default=settings.DOWNLOAD_WORKERS,
        help=f"clones at once (default {settings.DOWNLOAD_WORKERS})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=settings.DOWNLOAD_TIMEOUT_SECONDS,
        help=f"seconds per git command (default {settings.DOWNLOAD_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="fetch timeout and error rows again",
    )
    return parser


def _rows_by_repo(previous: pd.DataFrame | None) -> dict[str, dict[str, object]]:
    """Earlier rows keyed by repo."""
    if previous is None or previous.empty:
        return {}
    frame = previous.reindex(columns=list(README_COLUMNS))
    return {str(row["repo"]): row for row in frame.to_dict("records")}


def _save(rows: dict[str, dict[str, object]], out_path: Path) -> pd.DataFrame:
    """Write the rows sorted by repo and return them as a frame."""
    frame = pd.DataFrame(list(rows.values()), columns=list(README_COLUMNS))
    frame["readme_bytes"] = pd.to_numeric(frame["readme_bytes"], errors="coerce").astype("Int64")
    frame["seconds"] = pd.to_numeric(frame["seconds"], errors="coerce")
    for name in README_COLUMNS:
        if name not in {"readme_bytes", "seconds"}:
            frame[name] = frame[name].astype("object")
    frame = frame.sort_values("repo", kind="mergesort").reset_index(drop=True)
    write_parquet(frame, out_path)
    return frame


if __name__ == "__main__":
    main()
