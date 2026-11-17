"""Stage 2b: count SKILL.md files in the repo itself for registry candidates.

What: the registry rule counts English skills in the dataset. GitHub code search
did not capture every skill of some popular repos (bug B01): obra/superpowers has
1 skill in the dataset and anthropics/skills has 3. This stage takes every
non-fork with at least ``TREE_CHECK_MIN_STARS`` stars and 1 to
``MIN_REGISTRY_ENGLISH_SKILLS - 1`` English skills in the dataset, clones only the
newest commit of its default branch with no file contents (GIT9), lists its
SKILL.md paths (GIT3), and saves the count. Stage 2c (``select_registries``)
reads the saved table. Stage 2c never uses the network.

Order: 2a language labels, 2b this stage, 2c registry rule and roles. Each runs once.

Inputs: the dataset ``artifacts`` and ``repos`` tables and
``skill_language.parquet`` from stage 2a. The English skill counts come from
query Q2.4, the same count stage 2c uses.
Outputs: ``registry_tree_counts.parquet`` in the full data folder. One row per
checked repo: ``repo``, ``status``, ``message``, ``head_commit``,
``tree_skill_md``, ``seconds``, ``checked_at``.

A later run skips ``ok``, ``gone`` and ``empty`` rows. ``timeout`` and ``error``
are checked again only with ``--retry-failed``. ``--force`` checks every candidate
again. The table is saved every ``SAVE_EVERY`` repos, so a stopped run continues.

Run (network):
    python -m skill_genealogy.check_registry_trees --limit 20
    python -m skill_genealogy.check_registry_trees
then stage 2c:
    python -m skill_genealogy.select_registries --force
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from skill_genealogy import settings
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
    clone_head_tree_only,
    head_commit,
    list_skill_md_paths,
)

# Results that will not change on a second try.
_FINISHED_STATUS = frozenset({"ok", "gone", "empty"})
# Checked again only with --retry-failed.
_RETRY_STATUS = frozenset({"timeout", "error"})
# Write the table after this many finished repos.
SAVE_EVERY = 200
TREE_COUNT_COLUMNS: tuple[str, ...] = (
    "repo",
    "status",
    "message",
    "head_commit",
    "tree_skill_md",
    "seconds",
    "checked_at",
)


def tree_counts_path() -> Path:
    """``registry_tree_counts.parquet`` in the full data folder (stage 2 has no dev folder)."""
    return settings.data_dir(False) / "registry_tree_counts.parquet"


def tree_check_candidates(skills_per_repo: pd.DataFrame) -> list[str]:
    """Repos whose skills are counted from their own tree, most stars first.

    A candidate is a non-fork with ``stars >= TREE_CHECK_MIN_STARS`` and
    between 1 and ``MIN_REGISTRY_ENGLISH_SKILLS - 1`` English skills in the
    dataset. A repo with no English skill in the dataset is left out. A missing
    star count or fork flag is not a candidate. Ties on stars go by repo name,
    so ``--limit`` always takes the same repos.

    Inputs: a frame with ``repo``, ``stars``, ``is_fork``, ``english_skills``
    (the Q2.4 skill counts from ``english_skill_counts``).
    Outputs: ``owner/repo`` names.

    Example:
    >>> repos = pd.DataFrame({
    ...     "repo": ["a/few", "b/many", "c/fork", "d/none", "e/small"],
    ...     "stars": [500, 900, 900, 900, 50],
    ...     "is_fork": [0, 0, 1, 0, 0],
    ...     "english_skills": [2, 9, 2, 0, 2],
    ... })
    >>> tree_check_candidates(repos)
    ['a/few']
    """
    stars = pd.to_numeric(skills_per_repo["stars"], errors="coerce")
    forks = pd.to_numeric(skills_per_repo["is_fork"], errors="coerce")
    skills = pd.to_numeric(skills_per_repo["english_skills"], errors="coerce").fillna(0)
    mask = (stars >= settings.TREE_CHECK_MIN_STARS) & forks.eq(0)
    mask = mask & (skills >= 1) & (skills < settings.MIN_REGISTRY_ENGLISH_SKILLS)
    chosen = skills_per_repo.loc[mask.fillna(False), ["repo", "stars"]].copy()
    chosen["repo"] = chosen["repo"].astype(str)
    chosen = chosen.sort_values(["stars", "repo"], ascending=[False, True], kind="mergesort")
    return chosen["repo"].tolist()


def count_repo_skill_md(
    repo: str,
    work_dir: Path,
    timeout_seconds: int,
    url: str | None = None,
) -> dict[str, object]:
    """Clone one repo at depth 1 and count its SKILL.md files at HEAD.

    The count uses GIT3: any letter case of ``skill.md``, outside
    ``SKIPPED_FOLDERS``. It does not read the texts, so it does not check the
    language; stage 4 drops lineages that are not English. The clone is deleted
    before this returns.

    Inputs: ``repo`` is ``owner/repo``. ``work_dir`` is a folder for the
    temporary clone. ``timeout_seconds`` limits each git command. ``url``
    replaces the GitHub address (tests pass a ``file://`` URL).
    Outputs: one row with the ``TREE_COUNT_COLUMNS``. ``status`` is ``ok``,
    ``empty`` (no commit), ``gone``, ``timeout`` or ``error``.
    ``tree_skill_md`` is null unless the status is ``ok`` or ``empty``.
    """
    started = time.perf_counter()
    row: dict[str, object] = {
        "repo": repo,
        "status": "error",
        "message": "",
        "head_commit": None,
        "tree_skill_md": None,
    }
    work_dir.mkdir(parents=True, exist_ok=True)
    clone_root = Path(tempfile.mkdtemp(prefix="tree_", dir=work_dir))
    clone_dir = clone_root / "clone"
    try:
        clone_head_tree_only(repo, clone_dir, timeout_seconds, url=url)
        commit = head_commit(clone_dir)
        if commit is None:
            row.update(status="empty", message="no commits on the default branch", tree_skill_md=0)
        else:
            paths = list_skill_md_paths(clone_dir)
            row.update(status="ok", head_commit=commit, tree_skill_md=len(paths))
    except GitError as error:
        status, message = classify_git_error(error)
        row.update(status=status, message=message)
    except OSError as error:
        row.update(status="error", message=str(error).strip() or type(error).__name__)
    finally:
        shutil.rmtree(clone_root, ignore_errors=True)
    row["seconds"] = round(time.perf_counter() - started, 2)
    row["checked_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return row


def repos_still_to_check(
    candidates: list[str],
    done: pd.DataFrame,
    *,
    retry_failed: bool,
) -> list[str]:
    """Candidates without a finished row, in candidate order.

    ``ok``, ``gone`` and ``empty`` are finished. ``timeout`` and ``error``
    count as finished unless ``retry_failed`` is true.

    Example:
    >>> done = pd.DataFrame({"repo": ["a/x", "b/y"], "status": ["ok", "timeout"]})
    >>> repos_still_to_check(["a/x", "b/y", "c/z"], done, retry_failed=False)
    ['c/z']
    >>> repos_still_to_check(["a/x", "b/y", "c/z"], done, retry_failed=True)
    ['b/y', 'c/z']
    """
    if done.empty:
        return list(candidates)
    status = dict(zip(done["repo"].astype(str), done["status"].astype(str), strict=True))
    skip = set(_FINISHED_STATUS)
    if not retry_failed:
        skip |= _RETRY_STATUS
    return [repo for repo in candidates if status.get(repo) not in skip]


def check_registry_trees(
    repos: list[str],
    out_path: Path,
    *,
    previous: pd.DataFrame | None = None,
    workers: int = settings.DOWNLOAD_WORKERS,
    timeout_seconds: int = settings.DOWNLOAD_TIMEOUT_SECONDS,
    urls: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Count SKILL.md files for ``repos`` and save them with the earlier rows.

    A new row replaces an earlier row for the same repo. The table is written
    every ``SAVE_EVERY`` finished repos and once at the end.

    Inputs: ``repos`` to check. ``out_path`` is the parquet file. ``previous``
    is the table already on disk, or None. ``workers`` clones run at once.
    ``urls`` maps a repo to a clone URL (tests).
    Outputs: the full table, sorted by repo, also written to ``out_path``.
    """
    rows = _rows_by_repo(previous)
    work_dir = out_path.parent / "tree_check_tmp"
    finished = 0
    if repos:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(repos)))) as pool:
            futures = {
                pool.submit(
                    count_repo_skill_md,
                    repo,
                    work_dir,
                    timeout_seconds,
                    (urls or {}).get(repo),
                ): repo
                for repo in repos
            }
            for future in as_completed(futures):
                row = future.result()
                rows[str(row["repo"])] = row
                finished += 1
                if finished % SAVE_EVERY == 0:
                    _save(rows, out_path)
                    print(f"checked {finished} of {len(repos)} repos", flush=True)
    shutil.rmtree(work_dir, ignore_errors=True)
    return _save(rows, out_path)


def summarize_tree_counts(table: pd.DataFrame, candidates: list[str]) -> dict[str, object]:
    """Counts for the stage summary: status, promoted and rejected candidates.

    Promoted means ``ok`` with at least ``MIN_REGISTRY_ENGLISH_SKILLS`` SKILL.md files.

    Example:
    >>> table = pd.DataFrame({
    ...     "repo": ["a/x", "b/y", "c/z"],
    ...     "status": ["ok", "ok", "gone"],
    ...     "tree_skill_md": [12, 2, None],
    ... })
    >>> summarize_tree_counts(table, ["a/x", "b/y", "c/z", "d/w"])["promoted"]
    1
    """
    listed = table.loc[table["repo"].astype(str).isin(set(candidates))]
    counts = listed["status"].astype(str).value_counts()
    ok = listed.loc[listed["status"].astype(str) == "ok"]
    found = pd.to_numeric(ok["tree_skill_md"], errors="coerce").fillna(0)
    promoted = int((found >= settings.MIN_REGISTRY_ENGLISH_SKILLS).sum())
    numbers: dict[str, object] = {
        "candidates": len(candidates),
        "checked": len(listed),
        "not_checked": len(candidates) - len(listed),
        "promoted": promoted,
        "rejected": len(ok) - promoted,
    }
    for name in sorted(_FINISHED_STATUS | _RETRY_STATUS):
        numbers[f"status_{name}"] = int(counts.get(name, 0))
    return numbers


def main() -> None:
    """Count English skills per repo (Q2.4), pick candidates, clone them, save the counts."""
    # Imported here: select_registries (stage 2c) imports this module.
    from skill_genealogy.select_registries import english_skill_counts

    args = _parser().parse_args()
    if args.dev:
        print("dev: the tree table still goes to the full data folder", flush=True)
    out_path = tree_counts_path()
    candidates = tree_check_candidates(english_skill_counts())
    previous = None if args.force or not out_path.is_file() else read_parquet(out_path)
    todo = repos_still_to_check(
        candidates,
        previous if previous is not None else pd.DataFrame(columns=["repo", "status"]),
        retry_failed=bool(args.retry_failed),
    )
    if args.limit is not None:
        todo = todo[: max(0, int(args.limit))]
    print(f"{len(candidates)} candidates, {len(todo)} to check", flush=True)
    started = time.perf_counter()
    table = check_registry_trees(
        todo,
        out_path,
        previous=previous,
        workers=int(args.workers),
        timeout_seconds=int(args.timeout),
    )
    numbers = summarize_tree_counts(table, candidates)
    numbers["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    numbers["output"] = str(out_path)
    print_summary("registry tree check", numbers)
    top = table.sort_values("tree_skill_md", ascending=False, na_position="last")
    print_sample_records(top, "Most SKILL.md files", 10)
    if numbers["promoted"]:
        print("next: python -m skill_genealogy.select_registries --force", flush=True)


def _parser() -> argparse.ArgumentParser:
    """Stage flags plus ``--limit``, ``--workers``, ``--timeout``, ``--retry-failed``."""
    parser = build_arg_parser("Count SKILL.md files in popular repos the dataset under-counts.")
    parser.add_argument("--limit", type=int, default=None, help="check at most N repos")
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
        help="check timeout and error rows again",
    )
    return parser


def _rows_by_repo(previous: pd.DataFrame | None) -> dict[str, dict[str, object]]:
    """Earlier rows keyed by repo."""
    if previous is None or previous.empty:
        return {}
    frame = previous.reindex(columns=list(TREE_COUNT_COLUMNS))
    return {str(row["repo"]): row for row in frame.to_dict("records")}


def _save(rows: dict[str, dict[str, object]], out_path: Path) -> pd.DataFrame:
    """Write the rows sorted by repo and return them as a frame."""
    frame = pd.DataFrame(list(rows.values()), columns=list(TREE_COUNT_COLUMNS))
    frame["tree_skill_md"] = pd.to_numeric(frame["tree_skill_md"], errors="coerce").astype("Int64")
    frame["seconds"] = pd.to_numeric(frame["seconds"], errors="coerce")
    for name in ("repo", "status", "message", "head_commit", "checked_at"):
        frame[name] = frame[name].astype("object")
    frame = frame.sort_values("repo", kind="mergesort").reset_index(drop=True)
    write_parquet(frame, out_path)
    return frame


if __name__ == "__main__":
    main()
