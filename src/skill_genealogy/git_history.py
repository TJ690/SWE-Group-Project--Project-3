"""Read one repository's skill-folder history with git.

What: clone a repo without file texts, list every version of every file
in each skill folder, and keep the text of SKILL.md and of small scripts.
Inputs: a GitHub ``owner/repo`` name, or a local clone. Tests pass a
``file://`` URL. This module does not read dataset tables and does not
choose which repos to mine.
Outputs: change rows, blob texts, and one shard per repo (changes,
texts, then status). Stages 3 and 6 call these functions.

Skill texts are stored raw. They are not passed through
``normalize_text``. Author emails are stored only as a SHA-1.

Run the tests with:
    python -m pytest -q tests/skill_genealogy/test_git_history.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from skill_genealogy.file_helpers import read_parquet, write_parquet
from skill_genealogy.settings import (
    BLOB_FETCH_BATCH,
    DOWNLOAD_TIMEOUT_SECONDS,
    MANY_PATHS_LIMIT,
    MAX_SCRIPT_BYTES,
    SCRIPT_EXTENSIONS,
    SKILL_MD_MAX_BYTES,
    SKIPPED_FOLDERS,
    TEXT_ROW_GROUP,
)

# Git prints ``@@`` then hash, author date, author email and subject.
_LOG_FORMAT = "@@%H|%aI|%ae|%s"
_LAZY_FETCH_MESSAGE = "blob fetch was refused; texts were read on demand"
_ZERO_SHA = "0" * 40
_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_OCTAL = "01234567"
_SIMPLE_ESCAPES = {
    "a": b"\a",
    "b": b"\b",
    "f": b"\f",
    "n": b"\n",
    "r": b"\r",
    "t": b"\t",
    "v": b"\v",
    "\\": b"\\",
    '"': b'"',
}
_SKIPPED_PARTS = frozenset(name.rstrip("/") for name in SKIPPED_FOLDERS)
_SCRIPT_SUFFIXES = frozenset(extension.lower() for extension in SCRIPT_EXTENSIONS)
_GONE_MARKERS = (
    "not found",
    "404",
    "authentication failed",
    "could not read username",
    "terminal prompts disabled",
    "permission denied",
    "password authentication was removed",
)
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
]
_TEXT_SCHEMA = pa.schema([("sha", pa.string()), ("text", pa.string())])
_STATUS_COLUMNS = [
    "repo",
    "status",
    "message",
    "head_commit",
    "seconds",
    "follow_renames",
]


class GitError(RuntimeError):
    """A git command failed, or it ran longer than its timeout.

    Inputs: ``message`` is the short error. ``timed_out`` is true when the
    limit was hit. ``stderr`` is the text git wrote.
    Outputs: none. Callers read ``timed_out`` and ``stderr``.
    """

    def __init__(self, message: str, *, timed_out: bool = False, stderr: str = "") -> None:
        super().__init__(message)
        self.timed_out = timed_out
        self.stderr = stderr or message


@dataclass
class RepoResult:
    """What happened when one repository was mined.

    ``status`` is ``ok``, ``gone``, ``empty``, ``timeout`` or ``error``.
    ``head_commit`` is the default-branch tip, or None when there is no commit.
    ``follow_renames`` is false when the repo has more than
    ``MANY_PATHS_LIMIT`` skill folders. ``seconds`` is how long the mine took.
    """

    repo: str
    status: str
    message: str
    head_commit: str | None
    seconds: float
    follow_renames: bool


def run_git(
    args: list[str],
    cwd: Path | None,
    timeout_seconds: int,
    *,
    allow_lazy: bool = False,
) -> str:
    """Run ``git`` and return its standard output as text.

    Prompts are disabled (``GIT_TERMINAL_PROMPT=0``). ``LC_ALL=C`` keeps
    error text in English. ``allow_lazy`` lets git download blobs it needs.
    The log commands (GIT4–GIT6) turn this on so ``--find-renames`` can see a
    rename that also edits the file. Clone, rev-parse, ls-tree, and the GIT8
    size check leave it off.

    Inputs: ``args`` is everything after ``git`` (for example ``log``).
    ``cwd`` is the working directory, or None. ``timeout_seconds`` is the limit.
    Outputs: standard output. Raises ``GitError`` on a non-zero exit,
    a timeout, or a missing git program, even when stdout is not empty.

    Example:
    >>> isinstance(run_git(["--version"], None, 30), str)
    True
    """
    data = _run_git_bytes(args, cwd, timeout_seconds, payload=None, allow_lazy=allow_lazy)
    return data.decode("utf-8", "replace")


def clone_history_only(
    repo: str,
    target_dir: Path,
    timeout_seconds: int,
    url: str | None = None,
) -> None:
    """Clone the default branch without file contents or a checkout.

    Inputs: ``repo`` is ``owner/repo``. ``target_dir`` is the new clone folder.
    ``timeout_seconds`` limits the clone. ``url`` replaces the GitHub address
    (tests pass a ``file://`` URL).
    Outputs: the clone folder. Raises ``GitError`` when clone fails.
    """
    remote = url or f"https://github.com/{repo}.git"
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    # Git command GIT1, see CODE_FLOW.md
    run_git(_clone_args(remote, target_dir), target_dir.parent, timeout_seconds)


def head_commit(clone_dir: Path) -> str | None:
    """Return the default-branch commit, or None when the branch is empty.

    Inputs: ``clone_dir`` is a git clone.
    Outputs: the full commit hash, or None. A timeout still raises ``GitError``.
    """
    return _head_commit(clone_dir, DOWNLOAD_TIMEOUT_SECONDS)


def list_skill_md_paths(clone_dir: Path) -> list[str]:
    """Return every ``SKILL.md`` path at HEAD, in any letter case.

    Paths inside ``SKIPPED_FOLDERS`` (for example ``node_modules``) are left out.

    Inputs: ``clone_dir`` is a git clone with at least one commit.
    Outputs: repo-relative paths, in the order git prints them.
    """
    return _list_skill_md_paths(clone_dir, DOWNLOAD_TIMEOUT_SECONDS)


def read_folder_log(clone_dir: Path, folder: str, follow: bool) -> list[dict]:
    """Return one dict per file change under ``folder``.

    When ``follow`` is true, GIT4 follows ``SKILL.md`` across renames and GIT5
    then reads every folder name that file has lived in. A renamed skill folder
    keeps the files it had before the rename. When ``follow`` is false, GIT5
    reads only the folder as it is now.

    Inputs: ``clone_dir``, the skill ``folder`` (``skills/pdf`` or ``.``),
    and ``follow``.
    Outputs: rows from ``parse_raw_log``, newest commit first. Paths are
    repo-relative, not relative to the skill folder.
    """
    return _read_folder_log(clone_dir, folder, follow, DOWNLOAD_TIMEOUT_SECONDS)


def read_folder_log_many(clone_dir: Path, folders: list[str]) -> list[dict]:
    """Return file changes for many folders in one git log, without rename following.

    Used when a repo has more than ``MANY_PATHS_LIMIT`` skill folders.
    One log is much cheaper than one log per folder, and ``--follow`` cannot
    take more than one path.

    Inputs: ``clone_dir`` and the skill folders.
    Outputs: rows from ``parse_raw_log``. An empty folder list returns no rows.
    """
    return _read_folder_log_many(clone_dir, folders, DOWNLOAD_TIMEOUT_SECONDS)


def list_folder_at_commit(clone_dir: Path, commit: str, folder: str) -> list[dict]:
    """List files in ``folder`` at one commit: path, mode, sha and size.

    Inputs: ``clone_dir``, the full ``commit`` hash, and the skill folder.
    Outputs: one dict per blob. ``size`` is an int, or None when this blobless
    clone does not know the size yet (git prints ``BAD`` or ``-``).
    """
    return _list_folder_at_commit(clone_dir, commit, folder, DOWNLOAD_TIMEOUT_SECONDS)


def read_blob_texts(
    clone_dir: Path,
    blob_shas: list[str],
    max_bytes: int,
) -> dict[str, str]:
    """Return blob text for each sha whose size is at most ``max_bytes``.

    Missing blobs are fetched in batches of 500 (GIT8). A blob that is still
    larger than ``max_bytes`` is left out of the result. Texts are decoded as
    UTF-8 and are not normalised. If ``git fetch`` of the blobs fails, the
    same blobs are read with ``git cat-file --batch`` and git may download
    them one by one.

    Inputs: ``clone_dir``, blob ids, and ``max_bytes``.
    Outputs: a map of sha to text. Unknown or oversized blobs are absent.
    """
    texts, _used_lazy = _read_blob_texts(
        clone_dir, blob_shas, max_bytes, DOWNLOAD_TIMEOUT_SECONDS
    )
    return texts


def parse_raw_log(text: str) -> list[dict]:
    """Turn a ``git log --raw`` transcript into one dict per file change.

    A line that starts with ``@@`` opens a commit. A line that starts with
    ``:`` is one file. The status kept here is the first letter, so ``R100``
    becomes ``R``. The author email is stored only as ``author_key``.

    Inputs: the transcript from GIT4, GIT5 or GIT6.
    Outputs: dicts with commit, commit_date, author_key, subject, status,
    old_path, path, old_sha, new_sha, old_mode and new_mode. Newest first,
    in the order git printed them.

    Example:
    >>> sample = (
    ...     "@@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ...     "|2026-01-01T00:00:00+00:00|Dev@Example.com|add\\n"
    ...     ":000000 100644 0000000000000000000000000000000000000000 "
    ...     "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb A\\tskills/pdf/SKILL.md\\n"
    ... )
    >>> row = parse_raw_log(sample)[0]
    >>> (row["status"], row["path"])
    ('A', 'skills/pdf/SKILL.md')
    """
    rows: list[dict] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("@@"):
            current = _parse_header(line)
            continue
        if current is None or not line.startswith(":"):
            continue
        change = _parse_raw_line(line)
        if change is None:
            continue
        rows.append(_change_row(current, change))
    return rows


def hash_author_email(email: str) -> str:
    """Return the SHA-1 hex digest of the lowercased, trimmed email.

    ``Test@Example.com`` and ``test@example.com`` share one key.
    The address itself is not kept.

    Inputs: an author email, possibly empty.
    Outputs: 40 hex characters.

    Example:
    >>> hash_author_email("Test@Example.com")
    '567159d622ffbb50b11b0efd307be358624a26ee'
    """
    return hashlib.sha1(email.strip().lower().encode("utf-8")).hexdigest()


def mine_one_repo(
    repo: str,
    work_dir: Path,
    folders: list[str] | None,
    timeout_seconds: int,
    url: str | None = None,
) -> tuple[RepoResult, pd.DataFrame, pd.DataFrame]:
    """Clone one repo, read its skill folders, and delete the clone.

    Never raises. A failure becomes a status of ``gone``, ``empty``,
    ``timeout`` or ``error``, plus two empty frames.

    Inputs: ``repo`` is ``owner/repo``. ``work_dir`` holds the temporary clone.
    ``folders`` None means every skill folder at HEAD. A list mines only those
    (stage 6 passes the copied skill's folder). ``timeout_seconds`` limits each
    git command. ``url`` replaces the GitHub address in tests.
    Outputs: the status, the changes frame, and the texts frame (``sha``, ``text``).
    """
    started = time.perf_counter()
    clone_dir = work_dir / _shard_stem(repo)
    try:
        result, changes, texts = _mine_checked(
            repo, clone_dir, folders, timeout_seconds, url
        )
    except GitError as error:
        result = _result_from_git_error(repo, error)
        changes, texts = _empty_changes(), _empty_texts()
    except Exception as error:
        result = RepoResult(repo, "error", _last_line(str(error)), None, 0.0, False)
        changes, texts = _empty_changes(), _empty_texts()
    finally:
        _delete_tree(clone_dir)
    result.seconds = time.perf_counter() - started
    return result, changes, texts


def write_repo_shard(
    shard_dir: Path,
    repo: str,
    changes: pd.DataFrame,
    texts: pd.DataFrame,
    result: RepoResult,
) -> None:
    """Write one repo's changes, texts and status. Status is written last.

    A crash before the status file leaves no ``.status.json``, so a later
    merge ignores the half-written repo and the download can redo it.

    Inputs: ``shard_dir``, ``repo`` (``owner/repo``), the two frames, and
    the ``RepoResult``. The frames use the columns ``mine_one_repo`` returns.
    Outputs: ``<owner>__<repo>.changes.parquet``, ``.texts.parquet`` and
    ``.status.json`` inside ``shard_dir``.
    """
    shard_dir.mkdir(parents=True, exist_ok=True)
    stem = _shard_stem(repo)
    write_parquet(changes, shard_dir / f"{stem}.changes.parquet")
    _write_text_file(shard_dir / f"{stem}.texts.parquet", texts)
    _write_status(shard_dir / f"{stem}.status.json", result)


def merge_shards(
    shard_dir: Path,
    out_changes: Path,
    out_texts: Path,
    out_status: Path,
) -> dict[str, int]:
    """Join every finished shard into three tables.

    Only a repo whose ``.status.json`` exists is included. Texts are written
    with pyarrow in row groups of 1,000 so one giant copy is not held as a
    single parquet row group. Each blob sha is written once. The ``texts``
    count is the number of distinct shas.

    Inputs: ``shard_dir`` and the three output paths.
    Outputs: those files, and a dict ``changes``, ``texts``, ``status`` with
    the row counts that were written. Running it again on the same shards
    writes the same rows.
    """
    status_paths = sorted(shard_dir.glob("*.status.json"))
    change_frames: list[pd.DataFrame] = []
    status_rows: list[dict] = []
    text_paths: list[Path] = []
    for status_path in status_paths:
        stem = status_path.name[: -len(".status.json")]
        status_rows.append(json.loads(status_path.read_text(encoding="utf-8")))
        change_frames.append(read_parquet(shard_dir / f"{stem}.changes.parquet"))
        text_paths.append(shard_dir / f"{stem}.texts.parquet")
    changes = _combined_changes(change_frames)
    status = _status_frame(status_rows)
    write_parquet(
        changes,
        out_changes,
        sort_by=["repo", "skill_folder", "commit_seq", "file_path", "status", "new_sha"],
    )
    text_rows = _write_merged_texts(text_paths, out_texts)
    write_parquet(status, out_status, sort_by=["repo"])
    return {"changes": len(changes), "texts": text_rows, "status": len(status)}


def _mine_checked(
    repo: str,
    clone_dir: Path,
    folders: list[str] | None,
    timeout_seconds: int,
    url: str | None,
) -> tuple[RepoResult, pd.DataFrame, pd.DataFrame]:
    """Clone and read. Raises ``GitError``; ``mine_one_repo`` turns that into a status."""
    _delete_tree(clone_dir)
    clone_history_only(repo, clone_dir, timeout_seconds, url)
    head = _head_commit(clone_dir, timeout_seconds)
    if head is None:
        empty = RepoResult(
            repo, "empty", "no commits on the default branch", None, 0.0, False
        )
        return empty, _empty_changes(), _empty_texts()
    chosen = _choose_folders(clone_dir, folders, timeout_seconds)
    follow = len(chosen) <= MANY_PATHS_LIMIT
    grouped = _grouped_logs(clone_dir, chosen, follow, timeout_seconds)
    changes = _changes_frame(repo, grouped)
    texts, used_lazy = _texts_for_changes(clone_dir, changes, timeout_seconds)
    message = _LAZY_FETCH_MESSAGE if used_lazy else ""
    result = RepoResult(repo, "ok", message, head, 0.0, follow)
    return result, changes, texts


def _choose_folders(
    clone_dir: Path, folders: list[str] | None, timeout_seconds: int
) -> list[str]:
    """Skill folders to mine. None means every SKILL.md parent at HEAD."""
    if folders is None:
        paths = _list_skill_md_paths(clone_dir, timeout_seconds)
        discovered = [_parent_folder(path) for path in paths]
        return _drop_skipped_folders(_unique(discovered))
    cleaned = [_clean_folder(folder) for folder in folders]
    return _drop_skipped_folders(_unique(cleaned))


def _grouped_logs(
    clone_dir: Path, folders: list[str], follow: bool, timeout_seconds: int
) -> list[tuple[str, list[dict]]]:
    """Pair each skill folder with the log rows that belong to it."""
    if follow:
        grouped = [
            (folder, _read_folder_log(clone_dir, folder, True, timeout_seconds))
            for folder in folders
        ]
    else:
        rows = _read_folder_log_many(clone_dir, folders, timeout_seconds)
        grouped = _split_rows_by_folder(folders, rows)
    return [
        (folder, _drop_deeper_skill_rows(folder, folder_rows, folders))
        for folder, folder_rows in grouped
    ]


def _split_rows_by_folder(folders: list[str], rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Give each row to the longest requested folder that contains its new path."""
    grouped: dict[str, list[dict]] = {folder: [] for folder in folders}
    for row in rows:
        folder = _folder_for_path(row["path"], folders)
        if folder is None and row["old_path"]:
            folder = _folder_for_path(row["old_path"], folders)
        if folder is None:
            continue
        grouped[folder].append(row)
    return [(folder, grouped[folder]) for folder in folders]


def _changes_frame(repo: str, grouped: list[tuple[str, list[dict]]]) -> pd.DataFrame:
    """Build the changes table. ``commit_seq`` 1 is the oldest kept commit in that folder."""
    all_folders = [folder for folder, _rows in grouped]
    records: list[dict] = []
    for folder, rows in grouped:
        prefixes = _prefixes(folder, rows, all_folders)
        visible = _rows_kept_for_skill(rows, prefixes)
        sequence = _commit_sequence(visible)
        for row in visible:
            records.append(_change_record(repo, folder, prefixes, sequence, row))
    if not records:
        return _empty_changes()
    frame = pd.DataFrame.from_records(records, columns=_CHANGE_COLUMNS)
    return _finish_changes(frame)


def _change_record(
    repo: str,
    folder: str,
    prefixes: list[str],
    sequence: dict[str, int],
    row: dict,
) -> dict:
    """One changes-table row. The caller has already dropped skipped paths."""
    file_path = _relative_to_folders(row["path"], prefixes)
    old_file_path = _relative_to_folders(row["old_path"], prefixes) if row["old_path"] else ""
    return {
        "repo": repo,
        "skill_folder": folder,
        "file_path": file_path,
        "commit": row["commit"],
        "commit_date": row["commit_date"],
        "author_key": row["author_key"],
        "subject": row["subject"],
        "status": row["status"],
        "old_file_path": old_file_path,
        "old_sha": row["old_sha"],
        "new_sha": row["new_sha"],
        "old_mode": row["old_mode"],
        "new_mode": row["new_mode"],
        "is_skill_md": _is_skill_md_name(file_path) or _is_skill_md_name(old_file_path),
        "is_script": _is_script_file(file_path, old_file_path, row["old_mode"], row["new_mode"]),
        "commit_seq": sequence[row["commit"]],
    }


def _texts_for_changes(
    clone_dir: Path, changes: pd.DataFrame, timeout_seconds: int
) -> tuple[pd.DataFrame, bool]:
    """Fetch SKILL.md texts, and script texts that are within ``MAX_SCRIPT_BYTES``."""
    skill_shas: list[str] = []
    script_shas: list[str] = []
    for row in changes.to_dict("records"):
        blobs = _real_shas(str(row["old_sha"]), str(row["new_sha"]))
        if row["is_skill_md"]:
            skill_shas.extend(blobs)
        elif row["is_script"]:
            script_shas.extend(blobs)
    skill_texts, lazy_skill = _read_blob_texts(
        clone_dir, skill_shas, SKILL_MD_MAX_BYTES, timeout_seconds
    )
    script_texts, lazy_script = _read_blob_texts(
        clone_dir, script_shas, MAX_SCRIPT_BYTES, timeout_seconds
    )
    combined = {**skill_texts, **script_texts}
    if not combined:
        return _empty_texts(), lazy_skill or lazy_script
    frame = pd.DataFrame(
        {"sha": list(combined), "text": [combined[sha] for sha in combined]}
    )
    frame = frame.sort_values(["sha"], kind="mergesort").reset_index(drop=True)
    return frame, lazy_skill or lazy_script


def _real_shas(*shas: str) -> list[str]:
    """Drop blank and all-zero blob ids. Those are not files."""
    kept: list[str] = []
    for sha in shas:
        if sha and _SHA.fullmatch(sha) and sha != _ZERO_SHA:
            kept.append(sha)
    return kept


def _read_folder_log(
    clone_dir: Path, folder: str, follow: bool, timeout_seconds: int
) -> list[dict]:
    """GIT4 plus GIT5 when following, otherwise one GIT5 log."""
    cleaned = _clean_folder(folder)
    if not follow:
        # Git command GIT5, see CODE_FLOW.md
        text = run_git(
            _log_args(follow=False, pathspecs=[_pathspec(cleaned)]),
            clone_dir,
            timeout_seconds,
            allow_lazy=True,
        )
        return parse_raw_log(text)
    follow_rows = _follow_skill_rows(clone_dir, cleaned, timeout_seconds)
    pathspecs = _followed_pathspecs(cleaned, follow_rows)
    # Git command GIT5, see CODE_FLOW.md
    text = run_git(
        _log_args(follow=False, pathspecs=pathspecs),
        clone_dir,
        timeout_seconds,
        allow_lazy=True,
    )
    return _keep_skill_life(
        cleaned, parse_raw_log(text), follow_rows, clone_dir, timeout_seconds
    )


def _follow_skill_rows(clone_dir: Path, folder: str, timeout_seconds: int) -> list[dict]:
    """GIT4 rows for each SKILL.md that lives directly in ``folder``."""
    rows: list[dict] = []
    for skill_path in _skill_files_in_folder(clone_dir, folder, timeout_seconds):
        # Git command GIT4, see CODE_FLOW.md
        text = run_git(
            _log_args(follow=True, pathspecs=[skill_path]),
            clone_dir,
            timeout_seconds,
            allow_lazy=True,
        )
        rows.extend(parse_raw_log(text))
    return rows


def _followed_pathspecs(folder: str, follow_rows: list[dict]) -> list[str]:
    """Folder pathspecs that cover every name SKILL.md has had, plus the folder now."""
    extra_folders, root_files = _targets_from_follow_log(follow_rows)
    pathspecs = [_pathspec(name) for name in _unique([folder, *extra_folders])]
    pathspecs.extend(_unique(root_files))
    return pathspecs


def _targets_from_follow_log(rows: list[dict]) -> tuple[list[str], list[str]]:
    """Parents of the followed SKILL.md, and root-level files that must not log the whole repo."""
    folders: list[str] = []
    root_files: list[str] = []
    for row in rows:
        for key in ("path", "old_path"):
            path = row[key]
            if not path:
                continue
            parent = _parent_folder(path)
            if parent == ".":
                root_files.append(path)
            else:
                folders.append(parent)
    return folders, root_files


def _keep_skill_life(
    folder: str,
    rows: list[dict],
    follow_rows: list[dict],
    clone_dir: Path,
    timeout_seconds: int,
) -> list[dict]:
    """Drop rows that belong to a different skill that reused a folder name.

    An old folder's rows stay only through the commit that renamed SKILL.md
    out of it. A later add at that path is a new skill. A plain delete is not
    a cut: restoring SKILL.md keeps the older commits, including other files.
    """
    if not rows:
        return rows
    index = _commit_index(rows)
    left = _rename_out_commits(folder, follow_rows)
    cutoff = _previous_occupant_commit(folder, follow_rows, clone_dir, timeout_seconds)
    touched = _paths_in_commit(rows, cutoff)
    kept: list[dict] = []
    for row in rows:
        if _is_previous_occupant(row, cutoff, touched, index):
            continue
        if _after_folder_was_left(folder, row, left, index):
            continue
        kept.append(row)
    return kept


def _commit_index(rows: list[dict]) -> dict[str, int]:
    """Newest-first position of each commit. A smaller index is a newer commit."""
    order: list[str] = []
    known: set[str] = set()
    for row in rows:
        commit = row["commit"]
        if commit in known:
            continue
        known.add(commit)
        order.append(commit)
    return {commit: position for position, commit in enumerate(order)}


def _rename_out_commits(folder: str, follow_rows: list[dict]) -> dict[str, str]:
    """Old folder name to the commit that renamed the followed SKILL.md out of it."""
    left: dict[str, str] = {}
    known = {folder}
    for row in follow_rows:
        if row["status"] != "R" or not row["old_path"] or not _is_skill_md_name(row["path"]):
            continue
        new_folder = _parent_folder(row["path"])
        old_folder = _parent_folder(row["old_path"])
        if new_folder not in known or old_folder in {"", ".", new_folder}:
            continue
        if old_folder not in left:
            left[old_folder] = row["commit"]
        known.add(old_folder)
    return left


def _paths_in_commit(rows: list[dict], commit: str | None) -> set[str]:
    """Paths a commit changed. Other files are not part of that cut."""
    if not commit:
        return set()
    found: set[str] = set()
    for row in rows:
        if row["commit"] != commit:
            continue
        if row["path"]:
            found.add(row["path"])
        if row["old_path"]:
            found.add(row["old_path"])
    return found


def _previous_occupant_commit(
    folder: str,
    follow_rows: list[dict],
    clone_dir: Path,
    timeout_seconds: int,
) -> str | None:
    """Commit where a previous SKILL.md was renamed out of ``folder``.

    A plain delete is not a cut. Restoring the file keeps the older history.
    ``git log --follow`` can show a rename as ``D`` when the new path is
    outside the followed file, so that commit is checked for a real ``R``.
    """
    seen_add = False
    for row in follow_rows:
        if _added_skill_here(row, folder):
            seen_add = True
            continue
        if not seen_add:
            continue
        if _renamed_skill_out(row, folder):
            return row["commit"]
        if _deleted_skill_here(row, folder):
            if _commit_renames_skill_out(clone_dir, row["commit"], folder, timeout_seconds):
                return row["commit"]
            return None
    return None


def _added_skill_here(row: dict, folder: str) -> bool:
    """True when this row adds SKILL.md in ``folder``."""
    return (
        row["status"] == "A"
        and _is_skill_md_name(row["path"])
        and _parent_folder(row["path"]) == folder
    )


def _deleted_skill_here(row: dict, folder: str) -> bool:
    """True when this row deletes SKILL.md in ``folder``."""
    return (
        row["status"] == "D"
        and _is_skill_md_name(row["path"])
        and _parent_folder(row["path"]) == folder
    )


def _renamed_skill_out(row: dict, folder: str) -> bool:
    """True when SKILL.md left ``folder`` for a different folder."""
    if row["status"] != "R" or not row["old_path"]:
        return False
    if not _is_skill_md_name(row["old_path"]) and not _is_skill_md_name(row["path"]):
        return False
    old_folder = _parent_folder(row["old_path"])
    new_folder = _parent_folder(row["path"])
    return old_folder == folder and new_folder != folder


def _commit_renames_skill_out(
    clone_dir: Path, commit: str, folder: str, timeout_seconds: int
) -> bool:
    """True when ``commit`` renames SKILL.md out of ``folder``.

    The followed-file log shows that move as a delete. The commit diff shows
    the other path, so a real rename is ``R`` and a plain delete stays ``D``.
    """
    # Git command GIT4b, see CODE_FLOW.md
    text = run_git(
        [
            "diff-tree",
            "-r",
            "--find-renames",
            "--no-abbrev",
            "--no-commit-id",
            "--raw",
            commit,
        ],
        clone_dir,
        timeout_seconds,
        allow_lazy=True,
    )
    for line in text.splitlines():
        if not line.startswith(":"):
            continue
        change = _parse_raw_line(line)
        if change is not None and _renamed_skill_out(change, folder):
            return True
    return False


def _is_previous_occupant(
    row: dict, cutoff: str | None, touched: set[str], index: dict[str, int]
) -> bool:
    """True when this row is a file the rename-out commit took to the other skill.

    Rows for files that commit did not touch stay, even when they are older.
    """
    if cutoff is None or not touched:
        return False
    if row["commit"] not in index or cutoff not in index:
        return False
    if index[row["commit"]] < index[cutoff]:
        return False
    paths = {row["path"], row["old_path"]} - {""}
    return bool(paths & touched)


def _after_folder_was_left(
    folder: str, row: dict, left: dict[str, str], index: dict[str, int]
) -> bool:
    """True when every path on the row is in an old folder after the skill left it."""
    owners = _folders_touched(row, [folder, *left])
    if not owners:
        return False
    for owner in owners:
        if owner == folder or owner not in left:
            return False
        cutoff = left[owner]
        if row["commit"] not in index or cutoff not in index:
            return False
        if index[row["commit"]] >= index[cutoff]:
            return False
    return True


def _folders_touched(row: dict, folders: list[str]) -> list[str]:
    """Skill folders among ``folders`` that contain the row's path or old path."""
    found: list[str] = []
    for key in ("path", "old_path"):
        owner = _containing_folder(row[key], folders)
        if owner and owner not in found:
            found.append(owner)
    return found


def _containing_folder(path: str, folders: list[str]) -> str | None:
    """Longest folder in ``folders`` that contains ``path``."""
    if not path:
        return None
    best: str | None = None
    best_length = -1
    for folder in folders:
        if folder in {"", "."}:
            continue
        prefix = folder.strip("/") + "/"
        if path.startswith(prefix) and len(prefix) > best_length:
            best = folder
            best_length = len(prefix)
    return best


def _read_folder_log_many(
    clone_dir: Path, folders: list[str], timeout_seconds: int
) -> list[dict]:
    """GIT6: one log, many folders, no ``--follow``."""
    cleaned = [_clean_folder(folder) for folder in folders]
    if not cleaned:
        return []
    # Git command GIT6, see CODE_FLOW.md
    text = run_git(
        _log_args(follow=False, pathspecs=[_pathspec(folder) for folder in cleaned]),
        clone_dir,
        timeout_seconds,
        allow_lazy=True,
    )
    return parse_raw_log(text)


def _skill_files_in_folder(clone_dir: Path, folder: str, timeout_seconds: int) -> list[str]:
    """SKILL.md paths whose parent is exactly ``folder``."""
    found: list[str] = []
    for path in _list_skill_md_paths(clone_dir, timeout_seconds):
        if _parent_folder(path) == folder:
            found.append(path)
    return found


def _list_skill_md_paths(clone_dir: Path, timeout_seconds: int) -> list[str]:
    """GIT3. Keep a path only when its file name is skill.md in any case."""
    # Git command GIT3, see CODE_FLOW.md
    text = run_git(
        ["ls-tree", "-r", "--name-only", "HEAD"],
        clone_dir,
        timeout_seconds,
        allow_lazy=False,
    )
    paths: list[str] = []
    for line in text.splitlines():
        path = line.strip()
        if not path or not _is_skill_md_name(path) or _is_skipped(path):
            continue
        paths.append(path)
    return paths


def _head_commit(clone_dir: Path, timeout_seconds: int) -> str | None:
    """GIT2. None when HEAD does not exist yet. A timeout still raises."""
    try:
        # Git command GIT2, see CODE_FLOW.md
        text = run_git(["rev-parse", "--verify", "HEAD"], clone_dir, timeout_seconds)
    except GitError as error:
        if error.timed_out:
            raise
        return None
    sha = text.strip()
    return sha or None


def _list_folder_at_commit(
    clone_dir: Path, commit: str, folder: str, timeout_seconds: int
) -> list[dict]:
    """GIT7. One dict per blob under the folder at that commit."""
    # Git command GIT7, see CODE_FLOW.md
    text = run_git(
        ["ls-tree", "-r", "-l", commit, "--", _pathspec(_clean_folder(folder))],
        clone_dir,
        timeout_seconds,
        allow_lazy=False,
    )
    return _parse_ls_tree(text)


def _parse_ls_tree(text: str) -> list[dict]:
    """Parse ``git ls-tree -r -l`` lines into path, mode, sha and size."""
    rows: list[dict] = []
    for line in text.splitlines():
        meta, separator, path = line.partition("\t")
        if not separator:
            continue
        parts = meta.split()
        if len(parts) < 4 or parts[1] != "blob":
            continue
        mode, _kind, sha, size_text = parts[0], parts[1], parts[2], parts[3]
        size = int(size_text) if size_text.isdigit() else None
        rows.append(
            {"path": _unquote_path(path), "mode": mode, "sha": sha, "size": size}
        )
    return rows


def _read_blob_texts(
    clone_dir: Path,
    blob_shas: list[str],
    max_bytes: int,
    timeout_seconds: int,
) -> tuple[dict[str, str], bool]:
    """GIT8. The bool is true when fetch failed and cat-file had to fetch on demand."""
    wanted = _unique_blob_shas(blob_shas)
    if not wanted:
        return {}, False
    missing = _missing_blob_shas(clone_dir, wanted, timeout_seconds)
    lazy: list[str] = []
    for start in range(0, len(missing), BLOB_FETCH_BATCH):
        batch = missing[start : start + BLOB_FETCH_BATCH]
        try:
            _fetch_blob_batch(clone_dir, batch, timeout_seconds)
        except GitError:
            lazy.extend(batch)
    still_lazy = set(lazy)
    ready = [sha for sha in wanted if sha not in still_lazy]
    texts = _texts_from_cat_file(clone_dir, ready, max_bytes, timeout_seconds, allow_lazy=False)
    if lazy:
        texts.update(
            _texts_from_cat_file(clone_dir, lazy, max_bytes, timeout_seconds, allow_lazy=True)
        )
    return texts, bool(lazy)


def _fetch_blob_batch(clone_dir: Path, shas: list[str], timeout_seconds: int) -> None:
    """Fetch blob objects by id through the partial-clone remote."""
    if not shas:
        return
    # Git command GIT8, see CODE_FLOW.md
    run_git(
        [
            "-c",
            "fetch.negotiationAlgorithm=noop",
            "fetch",
            "origin",
            "--no-tags",
            "--no-write-fetch-head",
            "--recurse-submodules=no",
            *shas,
        ],
        clone_dir,
        timeout_seconds,
    )


def _texts_from_cat_file(
    clone_dir: Path,
    shas: list[str],
    max_bytes: int,
    timeout_seconds: int,
    *,
    allow_lazy: bool,
) -> dict[str, str]:
    """Read blob texts. Without lazy fetch, skip ids that are still missing or too big."""
    if not shas:
        return {}
    chosen = shas
    if not allow_lazy:
        chosen = _shas_within_size(clone_dir, shas, max_bytes, timeout_seconds)
    if not chosen:
        return {}
    # Git command GIT8, see CODE_FLOW.md
    data = _run_git_bytes(
        ["cat-file", "--batch"],
        clone_dir,
        timeout_seconds,
        payload=("\n".join(chosen) + "\n").encode("ascii"),
        allow_lazy=allow_lazy,
    )
    return _parse_cat_file_batch(data, max_bytes)


def _missing_blob_shas(clone_dir: Path, shas: list[str], timeout_seconds: int) -> list[str]:
    """Blob ids that are not in the local clone yet."""
    checked = _batch_check(clone_dir, shas, timeout_seconds)
    return [sha for sha in shas if checked.get(sha, ("missing", None))[0] == "missing"]


def _shas_within_size(
    clone_dir: Path, shas: list[str], max_bytes: int, timeout_seconds: int
) -> list[str]:
    """Local blobs whose size is known and is at most ``max_bytes``."""
    checked = _batch_check(clone_dir, shas, timeout_seconds)
    kept: list[str] = []
    for sha in shas:
        kind, size = checked.get(sha, ("missing", None))
        if kind == "missing" or size is None or size > max_bytes:
            continue
        kept.append(sha)
    return kept


def _batch_check(
    clone_dir: Path, shas: list[str], timeout_seconds: int
) -> dict[str, tuple[str, int | None]]:
    """Ask git whether each blob is local, and its size when it is."""
    if not shas:
        return {}
    data = _run_git_bytes(
        ["cat-file", "--batch-check"],
        clone_dir,
        timeout_seconds,
        payload=("\n".join(shas) + "\n").encode("ascii"),
        allow_lazy=False,
    )
    found: dict[str, tuple[str, int | None]] = {}
    for line in data.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        sha = parts[0].decode("ascii", "replace")
        kind = parts[1].decode("ascii", "replace")
        size = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
        found[sha] = ("missing", None) if kind == "missing" else (kind, size)
    return found


def _parse_cat_file_batch(data: bytes, max_bytes: int) -> dict[str, str]:
    """Walk ``git cat-file --batch`` output. Drop blobs over ``max_bytes``."""
    found: dict[str, str] = {}
    index = 0
    length = len(data)
    while index < length:
        newline = data.find(b"\n", index)
        if newline < 0:
            break
        parts = data[index:newline].split(b" ")
        index = newline + 1
        if len(parts) < 3 or parts[1] == b"missing":
            continue
        sha = parts[0].decode("ascii", "replace")
        try:
            size = int(parts[2])
        except ValueError:
            break
        content = data[index : index + size]
        index += size
        if index < length and data[index : index + 1] == b"\n":
            index += 1
        if parts[1] != b"blob" or size > max_bytes:
            continue
        found[sha] = content.decode("utf-8", "replace")
    return found


def _parse_header(line: str) -> dict[str, str] | None:
    """Split one ``@@hash|date|email|subject`` line. Subject may contain ``|``."""
    body = line[2:]
    commit, first, rest = body.partition("|")
    commit_date, second, rest = rest.partition("|")
    email, third, subject = rest.partition("|")
    if not (first and second and third) or _SHA.fullmatch(commit) is None:
        return None
    return {
        "commit": commit,
        "commit_date": commit_date,
        "email": email,
        "subject": subject,
    }


def _parse_raw_line(line: str) -> dict[str, str] | None:
    """Split one ``:oldmode newmode oldsha newsha STATUS`` line."""
    meta, separator, paths = line.partition("\t")
    if not separator:
        return None
    parts = meta[1:].split(" ")
    if len(parts) != 5:
        return None
    old_mode, new_mode, old_sha, new_sha, status = parts
    if not status[:1].isalpha():
        return None
    if len(status) > 1 and not status[1:].isdigit():
        return None
    path_bits = paths.split("\t")
    path = _unquote_path(path_bits[-1])
    old_path = _unquote_path(path_bits[0]) if len(path_bits) > 1 else ""
    return {
        "status": status[0],
        "old_path": old_path,
        "path": path,
        "old_sha": old_sha,
        "new_sha": new_sha,
        "old_mode": old_mode,
        "new_mode": new_mode,
    }


def _change_row(header: dict[str, str], change: dict[str, str]) -> dict:
    """Join a commit header and one file change. The email becomes a hash."""
    return {
        "commit": header["commit"],
        "commit_date": header["commit_date"],
        "author_key": hash_author_email(header["email"]),
        "subject": header["subject"],
        **change,
    }


def _unquote_path(path: str) -> str:
    """Decode a git C-quoted path. A path without quotes is returned unchanged."""
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        return path
    raw = path[1:-1]
    data = bytearray()
    index = 0
    while index < len(raw):
        if raw[index] != "\\":
            data.extend(raw[index].encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(raw):
            break
        data.extend(_escaped_byte(raw, index))
        index = _escape_end(raw, index)
    return bytes(data).decode("utf-8", "replace")


def _escaped_byte(raw: str, index: int) -> bytes:
    """One byte from a git escape: ``\\t``, ``\\303`` and the rest."""
    char = raw[index]
    if char in _SIMPLE_ESCAPES:
        return _SIMPLE_ESCAPES[char]
    if char in _OCTAL:
        end = _octal_end(raw, index)
        return bytes([int(raw[index:end], 8)])
    return char.encode("utf-8")


def _escape_end(raw: str, index: int) -> int:
    """Index just after the escape that starts at ``index``."""
    char = raw[index]
    if char in _OCTAL:
        return _octal_end(raw, index)
    return index + 1


def _octal_end(raw: str, index: int) -> int:
    """End index of up to three octal digits starting at ``index``."""
    end = index
    while end < len(raw) and end < index + 3 and raw[end] in _OCTAL:
        end += 1
    return end


def _log_args(follow: bool, pathspecs: list[str]) -> list[str]:
    """Build GIT4 (with ``--follow``) or GIT5/GIT6 (without it)."""
    args = ["log"]
    if follow:
        args.append("--follow")
    args.extend(
        [
            "--raw",
            "--no-abbrev",
            "--find-renames",
            "--date=iso-strict",
            f"--format={_LOG_FORMAT}",
            "--",
            *pathspecs,
        ]
    )
    return args


def _clone_args(remote: str, target_dir: Path) -> list[str]:
    """GIT1. A ``file://`` URL also allows the file protocol, which tests need."""
    args = [
        "clone",
        "--filter=blob:none",
        "--no-checkout",
        "--single-branch",
        remote,
        str(target_dir),
    ]
    if remote.startswith("file:"):
        return ["-c", "protocol.file.allow=always", *args]
    return args


def _run_git_bytes(
    args: list[str],
    cwd: Path | None,
    timeout_seconds: int,
    payload: bytes | None,
    *,
    allow_lazy: bool,
) -> bytes:
    """Run git and return stdout bytes. Used for cat-file, whose body is not text-sized."""
    env = _git_env(allow_lazy=allow_lazy)
    try:
        completed = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=cwd,
            input=payload,
            capture_output=True,
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise GitError("timed out", timed_out=True, stderr="timed out") from error
    except FileNotFoundError as error:
        raise GitError("git is not on PATH", stderr="git is not on PATH") from error
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        if not stderr:
            stderr = completed.stdout.decode("utf-8", "replace").strip()
        message = stderr or f"git exited {completed.returncode}"
        raise GitError(message, stderr=message)
    return completed.stdout


def _git_env(*, allow_lazy: bool) -> dict[str, str]:
    """Environment for every git command.

    Lazy fetch stays off for the clone, rev-parse, ls-tree, the missing-blob
    probe, and the first cat-file in GIT8. The log commands (GIT4–GIT6) turn
    it on so a rename that also edits the file can be detected.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    if allow_lazy:
        env.pop("GIT_NO_LAZY_FETCH", None)
    else:
        env["GIT_NO_LAZY_FETCH"] = "1"
    return env


def _result_from_git_error(repo: str, error: GitError) -> RepoResult:
    """Map a git failure onto gone, timeout or error. The message is the last stderr line."""
    if error.timed_out:
        return RepoResult(repo, "timeout", "timed out", None, 0.0, False)
    text = error.stderr.casefold()
    status = "gone" if any(marker in text for marker in _GONE_MARKERS) else "error"
    return RepoResult(repo, status, _last_line(error.stderr), None, 0.0, False)


def _last_line(text: str) -> str:
    """The last non-empty line of a git error, or the whole string when it is one line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return text.strip()
    return lines[-1]


def _commit_sequence(rows: list[dict]) -> dict[str, int]:
    """Number commits oldest-first. Git prints newest first, so the last new commit is 1."""
    seen: list[str] = []
    known: set[str] = set()
    for row in rows:
        commit = row["commit"]
        if commit in known:
            continue
        known.add(commit)
        seen.append(commit)
    total = len(seen)
    return {commit: total - index for index, commit in enumerate(seen)}


def _prefixes(folder: str, rows: list[dict], folders: list[str]) -> list[str]:
    """Folder names used to strip a repo path down to a skill-relative path.

    Parents of another chosen skill's SKILL.md are left out, so a root skill
    does not treat ``skills/y/SKILL.md`` as its own ``SKILL.md``. A folder this
    skill was renamed away from is still stripped, even when a new skill uses it.
    """
    found: list[str] = []
    if folder not in {"", "."}:
        found.append(folder.strip("/"))
    others = _other_skill_folders(folder, folders)
    historical = _historical_skill_folders(folder, rows)
    for row in rows:
        for key in ("path", "old_path"):
            path = row[key]
            if not path or not _is_skill_md_name(path):
                continue
            parent = _parent_folder(path)
            if parent in {"", "."} or parent in found:
                continue
            if parent not in historical and _parent_is_other_skill(parent, others):
                continue
            found.append(parent)
    return found


def _historical_skill_folders(folder: str, rows: list[dict]) -> set[str]:
    """Folder names this skill used before the current one. Rows are newest first."""
    current = "" if folder in {"", "."} else folder.strip("/")
    names: set[str] = set()
    for row in rows:
        if row["status"] != "R" or not row["old_path"] or not _is_skill_md_name(row["path"]):
            continue
        new_parent = _parent_folder(row["path"])
        old_parent = _parent_folder(row["old_path"])
        if new_parent != current and new_parent not in names:
            continue
        if old_parent not in {"", "."}:
            names.add(old_parent)
    return names


def _other_skill_folders(folder: str, folders: list[str]) -> set[str]:
    """Chosen skill folders other than ``folder``, without the root."""
    current = "" if folder in {"", "."} else folder.strip("/")
    others: set[str] = set()
    for name in folders:
        if name in {"", "."}:
            continue
        cleaned = name.strip("/")
        if cleaned != current:
            others.add(cleaned)
    return others


def _parent_is_other_skill(parent: str, others: set[str]) -> bool:
    """True when ``parent`` is another skill folder, or sits inside one."""
    if parent in others:
        return True
    return any(parent.startswith(other + "/") for other in others)


def _drop_deeper_skill_rows(
    folder: str, rows: list[dict], folders: list[str]
) -> list[dict]:
    """Drop rows that belong to a chosen skill folder deeper than ``folder``."""
    kept: list[dict] = []
    for row in rows:
        if _owned_by_deeper_skill(row["path"], folder, folders):
            continue
        if row["old_path"] and _owned_by_deeper_skill(row["old_path"], folder, folders):
            continue
        kept.append(row)
    return kept


def _owned_by_deeper_skill(path: str, folder: str, folders: list[str]) -> bool:
    """True when the longest chosen folder for ``path`` is deeper than ``folder``.

    A historical path whose parent is not a chosen folder is kept. That is
    how a rename (``skills/pdf`` to ``skills/pdf-tools``) stays in the log.
    """
    if not path:
        return False
    owner = _folder_for_path(path, folders)
    if owner is None or owner == folder:
        return False
    if folder in {"", "."}:
        return True
    return owner.startswith(folder.strip("/") + "/")


def _row_is_skipped(row: dict, prefixes: list[str]) -> bool:
    """True when the new path sits in a skipped folder.

    A rename out of a skipped folder (``node_modules/x.js`` to ``scripts/x.js``)
    is not skipped. ``_rows_kept_for_skill`` stores it as an add, so the file
    still has a row.
    """
    return _path_is_skipped(row["path"], prefixes)


def _path_is_skipped(path: str, prefixes: list[str]) -> bool:
    """True when ``path`` or its skill-relative form sits in a skipped folder."""
    if not path:
        return False
    if _is_skipped(path):
        return True
    return _is_skipped(_relative_to_folders(path, prefixes))


def _rows_kept_for_skill(rows: list[dict], prefixes: list[str]) -> list[dict]:
    """Drop vendored new paths. A move out of a vendored folder becomes an add."""
    kept: list[dict] = []
    for row in rows:
        if _row_is_skipped(row, prefixes):
            continue
        kept.append(_add_if_old_path_skipped(row, prefixes))
    return kept


def _add_if_old_path_skipped(row: dict, prefixes: list[str]) -> dict:
    """Turn a rename out of a skipped folder into an add of the new path.

    ``old_sha`` is the all-zero id, the same value git writes on an add.
    """
    if not _path_is_skipped(row["old_path"], prefixes):
        return row
    return {
        **row,
        "status": "A",
        "old_path": "",
        "old_sha": _ZERO_SHA,
        "old_mode": "000000",
    }


def _relative_to_folders(path: str, folders: list[str]) -> str:
    """Path relative to the longest skill folder that is a prefix of ``path``."""
    if not path:
        return ""
    ordered = sorted(
        (folder.strip("/") for folder in folders if folder and folder != "."),
        key=len,
        reverse=True,
    )
    for folder in ordered:
        prefix = folder + "/"
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _folder_for_path(path: str, folders: list[str]) -> str | None:
    """The requested folder that contains ``path``, preferring the longest match."""
    best: str | None = None
    best_length = -1
    for folder in folders:
        if folder in {"", "."}:
            continue
        prefix = folder.strip("/") + "/"
        if path.startswith(prefix) and len(prefix) > best_length:
            best = folder
            best_length = len(prefix)
    if best is not None:
        return best
    if "." in folders or "" in folders:
        return "."
    return None


def _finish_changes(frame: pd.DataFrame) -> pd.DataFrame:
    """Stable column order and a stable row order for shards and tests."""
    ordered = frame.loc[:, _CHANGE_COLUMNS].copy()
    ordered["commit_seq"] = ordered["commit_seq"].astype("int64")
    ordered["is_skill_md"] = ordered["is_skill_md"].astype(bool)
    ordered["is_script"] = ordered["is_script"].astype(bool)
    ordered = ordered.sort_values(
        ["skill_folder", "commit_seq", "file_path", "status"],
        kind="mergesort",
    )
    return ordered.reset_index(drop=True)


def _empty_changes() -> pd.DataFrame:
    """Changes frame with the right columns and no rows."""
    data: dict[str, pd.Series] = {
        name: pd.Series(dtype="object") for name in _CHANGE_COLUMNS
    }
    data["commit_seq"] = pd.Series(dtype="int64")
    data["is_skill_md"] = pd.Series(dtype="bool")
    data["is_script"] = pd.Series(dtype="bool")
    return pd.DataFrame(data).loc[:, _CHANGE_COLUMNS]


def _empty_texts() -> pd.DataFrame:
    """Texts frame with ``sha`` and ``text`` and no rows."""
    return pd.DataFrame(
        {"sha": pd.Series(dtype="object"), "text": pd.Series(dtype="object")}
    )


def _is_skill_md_name(path: str) -> bool:
    """True when the file name is skill.md, in any letter case."""
    return bool(path) and PurePosixPath(path).name.lower() == "skill.md"


def _is_script_file(path: str, old_path: str, old_mode: str, new_mode: str) -> bool:
    """True for a script extension, or for mode ``100755`` on either side."""
    for candidate in (path, old_path):
        if candidate and PurePosixPath(candidate).suffix.lower() in _SCRIPT_SUFFIXES:
            return True
    return old_mode == "100755" or new_mode == "100755"


def _is_skipped(path: str) -> bool:
    """True when any folder in ``path`` is in ``SKIPPED_FOLDERS``."""
    return any(part in _SKIPPED_PARTS for part in PurePosixPath(path).parts)


def _drop_skipped_folders(folders: list[str]) -> list[str]:
    """Remove skill folders that live inside a vendored directory."""
    return [folder for folder in folders if folder == "." or not _is_skipped(folder + "/x")]


def _parent_folder(path: str) -> str:
    """Parent directory of a repo-relative path. A root file returns ``.``."""
    parent = PurePosixPath(path).parent.as_posix()
    return "." if parent == "." else parent


def _clean_folder(folder: str) -> str:
    """Normalise a folder: no trailing slash, ``.`` for the repo root."""
    text = folder.strip().replace("\\", "/").strip("/")
    return text or "."


def _pathspec(folder: str) -> str:
    """Git pathspec for a folder. The root folder is ``.``."""
    if folder in {"", "."}:
        return "."
    return folder.strip("/") + "/"


def _unique(items: list[str]) -> list[str]:
    """Drop repeats and keep the first-seen order."""
    seen: list[str] = []
    known: set[str] = set()
    for item in items:
        if item in known:
            continue
        known.add(item)
        seen.append(item)
    return seen


def _unique_blob_shas(shas: list[str]) -> list[str]:
    """Lowercased blob ids, skipping blanks, non-hashes and the all-zero id."""
    return _unique(
        [
            sha.lower()
            for sha in shas
            if sha and _SHA.fullmatch(sha.lower()) and set(sha.lower()) != {"0"}
        ]
    )


def _shard_stem(repo: str) -> str:
    """``owner/repo`` becomes ``owner__repo``, the shard file name."""
    stem = repo.replace("/", "__").strip()
    if not stem or stem in {".", ".."}:
        return "repo"
    return stem


def _delete_tree(path: Path) -> None:
    """Remove a clone folder if it exists. Errors are ignored so cleanup always finishes."""
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _write_status(path: Path, result: RepoResult) -> None:
    """Write status JSON via a temporary file, so a crash cannot leave a half status."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    payload = json.dumps(asdict(result), indent=2, sort_keys=True) + "\n"
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def _write_text_file(path: Path, frame: pd.DataFrame) -> None:
    """Write a texts parquet with a fixed string schema, sorted by sha."""
    path.parent.mkdir(parents=True, exist_ok=True)
    table = _text_table(frame)
    if table.num_rows:
        table = table.sort_by("sha")
    pq.write_table(table, path, compression="zstd")


def _text_table(frame: pd.DataFrame) -> pa.Table:
    """Texts as an Arrow table. An empty frame still has ``sha`` and ``text``."""
    if frame.empty or "sha" not in frame.columns:
        return pa.table({"sha": [], "text": []}, schema=_TEXT_SCHEMA)
    return pa.table(
        {"sha": frame["sha"].astype(str).tolist(), "text": frame["text"].astype(str).tolist()},
        schema=_TEXT_SCHEMA,
    )


def _combined_changes(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Stack shard change tables. No shards yields the empty column layout."""
    if not frames:
        return _empty_changes()
    stacked = pd.concat(frames, ignore_index=True)
    if stacked.empty:
        return _empty_changes()
    return stacked


def _status_frame(rows: list[dict]) -> pd.DataFrame:
    """One row per shard status file, with a stable column order."""
    if not rows:
        return pd.DataFrame(columns=_STATUS_COLUMNS)
    frame = pd.DataFrame(rows)
    for column in _STATUS_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    return frame.loc[:, _STATUS_COLUMNS]


def _write_merged_texts(text_paths: list[Path], out_texts: Path) -> int:
    """Stream shard texts. Each sha is written once. Return the distinct count."""
    out_texts.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    written = 0
    writer = pq.ParquetWriter(out_texts, _TEXT_SCHEMA, compression="zstd")
    try:
        for path in text_paths:
            fresh = _unseen_text_rows(pq.read_table(path, schema=_TEXT_SCHEMA), seen)
            if fresh.num_rows == 0:
                continue
            for start in range(0, fresh.num_rows, TEXT_ROW_GROUP):
                writer.write_table(fresh.slice(start, TEXT_ROW_GROUP))
            written += fresh.num_rows
        if written == 0:
            writer.write_table(pa.table({"sha": [], "text": []}, schema=_TEXT_SCHEMA))
    finally:
        writer.close()
    return written


def _unseen_text_rows(table: pa.Table, seen: set[str]) -> pa.Table:
    """Rows whose sha has not been written yet. ``seen`` is updated."""
    if table.num_rows == 0:
        return table
    keep: list[bool] = []
    for sha in table.column("sha").to_pylist():
        if sha in seen:
            keep.append(False)
            continue
        seen.add(sha)
        keep.append(True)
    if not any(keep):
        return table.slice(0, 0)
    return table.filter(pa.array(keep))
