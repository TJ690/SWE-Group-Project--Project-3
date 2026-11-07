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

from skill_genealogy.file_helpers import connect_duckdb, read_parquet, write_parquet
from skill_genealogy.settings import (
    BLOB_FETCH_BATCH,
    BLOB_READ_BATCH,
    DELETED_FOLDERS_MAX,
    DOWNLOAD_TIMEOUT_SECONDS,
    GIT6_MAX_PATHSPECS,
    HISTORY_DUCKDB_MEMORY_LIMIT,
    MANY_PATHS_LIMIT,
    MAX_SCRIPT_BYTES,
    PROGRESS_SECONDS,
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
    "repo_path",
    "old_repo_path",
    "history_role",
]
# Merged changes are sorted by these columns, then by every other column,
# so a second merge writes the same file.
_CHANGE_SORT = ["repo", "skill_folder", "commit_seq", "file_path", "status", "new_sha"]
_TEXT_SCHEMA = pa.schema([("sha", pa.string()), ("text", pa.string())])
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
    "moved_folders",
    "history_folders_dropped",
]
# Git's message when a --shallow-since clone finds no commit after the date.
_NO_SHALLOW_COMMITS = "no commits selected for shallow requests"


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
    ``not_ours``, ``after_copy`` and ``twin`` count change rows dropped
    because another folder was not this skill. ``history_since`` is the
    ``--shallow-since`` date the clone used, or None for full history.
    ``deleted_folders`` counts skill folders found only in the history (their
    ``SKILL.md`` was gone at HEAD) and mined by their exact path. It is None
    when the history was not searched (an explicit folder list, or a failure).
    ``moved_folders`` counts history-only folders whose skill moved to a
    folder that still exists; their rows are kept as that folder's
    ``former_folder`` history. ``history_folders_dropped`` counts history-only
    folders that were logged but not kept (no visible delete, or newer than a
    backfill snapshot).
    """

    repo: str
    status: str
    message: str
    head_commit: str | None
    seconds: float
    follow_renames: bool
    not_ours: int = 0
    after_copy: int = 0
    twin: int = 0
    history_since: str | None = None
    deleted_folders: int | None = None
    moved_folders: int | None = None
    history_folders_dropped: int | None = None


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
    since: str | None = None,
) -> None:
    """Clone the default branch without file contents or a checkout.

    ``since`` (``YYYY-MM-DD``) keeps only commits from that date on
    (``--shallow-since``), then fetches one more commit back (``--deepen=1``).
    Git shows the oldest commit of a shallow clone as adding every file, so
    that extra commit is the one with false rows, and the log readers drop
    it. Every commit on or after ``since`` keeps its true changes. When no
    commit is that new, only the newest commit is cloned and the logs keep
    nothing.

    Inputs: ``repo`` is ``owner/repo``. ``target_dir`` is the new clone folder.
    ``timeout_seconds`` limits the clone. ``url`` replaces the GitHub address
    (tests pass a ``file://`` URL). ``since`` is None for the whole history.
    Outputs: the clone folder. Raises ``GitError`` when clone fails.
    """
    remote = url or f"https://github.com/{repo}.git"
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    if since is None:
        # Git command GIT1, see CODE_FLOW.md
        run_git(_clone_args(remote, target_dir), target_dir.parent, timeout_seconds)
        return
    try:
        # Git command GIT1 with --shallow-since, see CODE_FLOW.md
        run_git(
            _clone_args(remote, target_dir, [f"--shallow-since={since}"]),
            target_dir.parent,
            timeout_seconds,
        )
    except GitError as error:
        if error.timed_out or _NO_SHALLOW_COMMITS not in error.stderr:
            raise
        _delete_tree(target_dir)
        # Git command GIT9, see CODE_FLOW.md
        run_git(_head_clone_args(remote, target_dir), target_dir.parent, timeout_seconds)
        return
    if _shallow_boundary(target_dir):
        # Git command GIT1b, see CODE_FLOW.md
        run_git(_deepen_args(remote), target_dir, timeout_seconds)


def clone_head_tree_only(
    repo: str,
    target_dir: Path,
    timeout_seconds: int,
    url: str | None = None,
) -> None:
    """Clone only the newest commit of the default branch, without file contents.

    This is enough for ``git ls-tree`` (GIT3) on HEAD. It downloads one commit
    and its trees, so it is much smaller than GIT1 on a repo with a long history.

    Inputs: ``repo`` is ``owner/repo``. ``target_dir`` is the new clone folder.
    ``timeout_seconds`` limits the clone. ``url`` replaces the GitHub address
    (tests pass a ``file://`` URL).
    Outputs: the clone folder. Raises ``GitError`` when clone fails.
    """
    remote = url or f"https://github.com/{repo}.git"
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    # Git command GIT9, see CODE_FLOW.md
    run_git(_head_clone_args(remote, target_dir), target_dir.parent, timeout_seconds)


def clone_commit_tree_only(
    repo: str,
    target_dir: Path,
    commit: str,
    timeout_seconds: int,
    url: str | None = None,
) -> None:
    """Download one exact commit and its trees, without file contents or history.

    Like GIT9, but for a commit chosen by id instead of the newest one, so a
    later stage reads the same snapshot that stage 3 mined. Git marks the new
    repo as a partial clone, so ``read_blob_texts`` can fetch single files.

    Inputs: ``repo`` is ``owner/repo``. ``target_dir`` is the new folder.
    ``commit`` is a full commit id. ``timeout_seconds`` limits each git command.
    ``url`` replaces the GitHub address (tests pass a ``file://`` URL).
    Outputs: the folder. Raises ``GitError`` when the commit cannot be fetched,
    for example after a force push removed it.
    """
    remote = url or f"https://github.com/{repo}.git"
    target_dir.mkdir(parents=True, exist_ok=True)
    run_git(["init", "--quiet", str(target_dir)], target_dir.parent, timeout_seconds)
    run_git(["remote", "add", "origin", remote], target_dir, timeout_seconds)
    allow_file = ["-c", "protocol.file.allow=always"] if remote.startswith("file:") else []
    # Git command GIT10, see CODE_FLOW.md
    run_git(
        [
            *allow_file,
            "fetch",
            "--quiet",
            "--depth",
            "1",
            "--filter=blob:none",
            "origin",
            commit,
        ],
        target_dir,
        timeout_seconds,
    )


def list_files_at_commit(clone_dir: Path, commit: str, folders: list[str]) -> list[dict]:
    """List the files directly inside ``folders`` at one commit (not recursive).

    ``""`` is the repo root. Subfolders are not listed and file contents are
    not downloaded.

    Inputs: ``clone_dir`` (a clone that has ``commit``), the full commit id,
    and folder names such as ``["", ".github", "docs"]``.
    Outputs: one dict per entry with ``path``, ``mode``, ``type`` and ``sha``,
    in git's order. A folder that does not exist adds nothing.
    """
    specs = [f"{folder.strip('/')}/" if folder.strip("/") else "." for folder in folders]
    # Git command GIT11, see CODE_FLOW.md
    text = run_git(
        ["ls-tree", "--full-tree", commit, "--", *specs],
        clone_dir,
        DOWNLOAD_TIMEOUT_SECONDS,
    )
    entries: list[dict] = []
    for line in text.splitlines():
        meta, separator, path = line.partition("\t")
        parts = meta.split()
        if not separator or len(parts) != 3:
            continue
        entries.append(
            {"path": _unquote_path(path), "mode": parts[0], "type": parts[1], "sha": parts[2]}
        )
    return entries


def classify_git_error(error: GitError) -> tuple[str, str]:
    """Map a git failure to a status (``gone``, ``timeout`` or ``error``) and a message.

    The rules are the GIT1 rules: a 404, "not found" or an auth prompt is
    ``gone``. The message is the last stderr line.

    Example:
    >>> classify_git_error(GitError("x", stderr="remote: Repository not found."))
    ('gone', 'remote: Repository not found.')
    >>> classify_git_error(GitError("x", timed_out=True))
    ('timeout', 'timed out')
    """
    result = _result_from_git_error("", error)
    return result.status, result.message


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
    rows, _drops = _read_folder_log(clone_dir, folder, follow, DOWNLOAD_TIMEOUT_SECONDS)
    return rows


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
    since: str | None = None,
) -> tuple[RepoResult, pd.DataFrame, pd.DataFrame]:
    """Clone one repo, read its skill folders, and delete the clone.

    Never raises. A failure becomes a status of ``gone``, ``empty``,
    ``timeout`` or ``error``, plus two empty frames.

    Inputs: ``repo`` is ``owner/repo``. ``work_dir`` holds the temporary clone.
    ``folders`` None means every skill folder at HEAD. A list mines only those
    (stage 6 passes the copied skill's folder). ``timeout_seconds`` limits each
    git command. ``url`` replaces the GitHub address in tests. ``since``
    (``YYYY-MM-DD``) keeps only history from that date; None keeps all of it.
    Outputs: the status, the changes frame, and the texts frame (``sha``, ``text``).
    """
    started = time.perf_counter()
    clone_dir = work_dir / _shard_stem(repo)
    try:
        result, changes, texts = _mine_checked(
            repo, clone_dir, folders, timeout_seconds, url, since
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
    result.history_since = since
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

    Only a repo whose ``.status.json`` exists is included. Changes are
    joined and sorted in DuckDB, which spills to disk, so millions of rows
    are never held in pandas. Texts are written with pyarrow in row groups
    of 1,000 so one giant copy is not held as a single parquet row group.
    Each blob sha is written once. The ``texts`` count is the number of
    distinct shas.

    Inputs: ``shard_dir`` and the three output paths.
    Outputs: those files, and a dict ``changes``, ``texts``, ``status`` with
    the row counts that were written. Running it again on the same shards
    writes the same rows.
    """
    status_paths = sorted(shard_dir.glob("*.status.json"))
    change_paths: list[Path] = []
    status_rows: list[dict] = []
    text_paths: list[Path] = []
    for status_path in status_paths:
        stem = status_path.name[: -len(".status.json")]
        status_rows.append(json.loads(status_path.read_text(encoding="utf-8")))
        change_paths.append(shard_dir / f"{stem}.changes.parquet")
        text_paths.append(shard_dir / f"{stem}.texts.parquet")
    change_rows = _write_merged_changes(change_paths, out_changes)
    status = _status_frame(status_rows)
    text_rows = _write_merged_texts(text_paths, out_texts)
    write_parquet(status, out_status, sort_by=["repo"])
    return {"changes": change_rows, "texts": text_rows, "status": len(status)}


def connect_history_duckdb(temp_root: Path):
    """DuckDB for the stage 3 merge and summary.

    The memory cap is ``HISTORY_DUCKDB_MEMORY_LIMIT``. Above it, DuckDB
    spills to ``temp_root/duckdb_tmp``. The caller closes the connection.
    """
    con = connect_duckdb()
    temp = temp_root / "duckdb_tmp"
    temp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET memory_limit = '{HISTORY_DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET temp_directory = {_sql_text(str(temp))}")
    return con


def _write_merged_changes(paths: list[Path], out_changes: Path) -> int:
    """Join shard change files into one sorted parquet. Return the row count.

    Shards with no rows are skipped, so their untyped empty columns cannot
    clash with real ones. The file is written beside the output and then
    moved into place, so a failed merge keeps the old file.
    """
    filled = [path for path in paths if pq.ParquetFile(path).metadata.num_rows > 0]
    out_changes.parent.mkdir(parents=True, exist_ok=True)
    if not filled:
        write_parquet(_empty_changes(), out_changes)
        return 0
    names: list[str] = []
    for path in filled:
        for name in pq.read_schema(path).names:
            if name not in names:
                names.append(name)
    columns = [name for name in _CHANGE_COLUMNS if name in names]
    columns += [name for name in names if name not in columns]
    order = [name for name in _CHANGE_SORT if name in columns]
    order += [name for name in columns if name not in order]
    files = "[" + ", ".join(_sql_text(str(path)) for path in filled) + "]"
    temporary = out_changes.with_name(out_changes.stem + ".merging.parquet")
    con = connect_history_duckdb(out_changes.parent)
    try:
        con.execute(
            f"""
            COPY (
                SELECT {", ".join(_sql_name(name) for name in columns)}
                FROM read_parquet({files}, union_by_name = true)
                ORDER BY {", ".join(_sql_name(name) for name in order)}
            ) TO {_sql_text(str(temporary))} (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        counted = con.execute(
            f"SELECT count(*) FROM read_parquet({_sql_text(str(temporary))})"
        ).fetchone()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        con.close()
    temporary.replace(out_changes)
    return int(counted[0]) if counted else 0


def _sql_text(value: str) -> str:
    """A single-quoted SQL string, with quotes escaped."""
    return "'" + value.replace("'", "''") + "'"


def _sql_name(name: str) -> str:
    """A double-quoted SQL column name (``commit`` is a keyword)."""
    return '"' + name.replace('"', '""') + '"'



def _mine_checked(
    repo: str,
    clone_dir: Path,
    folders: list[str] | None,
    timeout_seconds: int,
    url: str | None,
    since: str | None = None,
) -> tuple[RepoResult, pd.DataFrame, pd.DataFrame]:
    """Clone and read. Raises ``GitError``; ``mine_one_repo`` turns that into a status."""
    _delete_tree(clone_dir)
    clone_history_only(repo, clone_dir, timeout_seconds, url, since)
    head = _head_commit(clone_dir, timeout_seconds)
    if head is None:
        empty = RepoResult(
            repo, "empty", "no commits on the default branch", None, 0.0, False
        )
        return empty, _empty_changes(), _empty_texts()
    chosen = _choose_folders(clone_dir, folders, timeout_seconds)
    follow = len(chosen) <= MANY_PATHS_LIMIT
    grouped, drops = _grouped_logs(clone_dir, chosen, follow, timeout_seconds)
    changes = _changes_frame(repo, grouped)
    # The row dicts can be millions; free them before the texts are read.
    del grouped
    kept_deleted: int | None = None
    moved: int | None = None
    dropped: int | None = None
    note = ""
    if folders is None:
        deleted = deleted_skill_folders(
            _history_skill_md_paths(clone_dir, timeout_seconds),
            set(chosen),
            _covered_skill_folders(changes),
        )
        extra, kept_deleted, moved, dropped, note = _deleted_folder_rows(
            repo, clone_dir, changes, deleted, timeout_seconds
        )
        if not extra.empty:
            changes = extra if changes.empty else pd.concat([changes, extra], ignore_index=True)
            targets = _former_targets(extra)
            changes = renumber_commit_seq(collapse_move_rows(changes, targets), targets)
    texts, used_lazy = _texts_for_changes(clone_dir, changes, timeout_seconds)
    message = "; ".join(part for part in (_LAZY_FETCH_MESSAGE if used_lazy else "", note) if part)
    result = RepoResult(
        repo,
        "ok",
        message,
        head,
        0.0,
        follow,
        not_ours=drops["not_ours"],
        after_copy=drops["after_copy"],
        twin=drops["twin"],
        deleted_folders=kept_deleted,
        moved_folders=moved,
        history_folders_dropped=dropped,
    )
    return result, changes, texts


def deleted_skill_folders(
    history_paths: list[str],
    mined_folders: set[str],
    covered_folders: set[str],
) -> list[str]:
    """Skill folders that held a ``SKILL.md`` at some point but were not mined.

    Rule: the parent folder of every ``SKILL.md`` path in the history (GIT12),
    minus folders already mined, folders inside a mined folder (their files
    are already in that folder's rows), folders whose history is already kept
    under a later name (``covered_folders``: parents of the ``SKILL.md`` paths
    in the mined rows), vendored folders, and the repo root. A deleted root
    skill would mean logging every file of the repo, so it is left out.

    Example:
    >>> history = ["skills/pdf/SKILL.md", "skills/old/SKILL.md", "skills/pdf/cn/SKILL.md",
    ...            "skills/pdf-v1/SKILL.md", "node_modules/x/SKILL.md", "SKILL.md"]
    >>> deleted_skill_folders(history, {"skills/pdf"}, {"skills/pdf", "skills/pdf-v1"})
    ['skills/old']
    """
    found: list[str] = []
    for path in history_paths:
        if not _is_skill_md_name(path):
            continue
        folder = _parent_folder(path)
        if folder == "." or folder in mined_folders or folder in covered_folders:
            continue
        if _is_skipped(folder + "/x") or _ancestors(folder) & mined_folders:
            continue
        found.append(folder)
    return sorted(set(found))


def mine_deleted_folders(
    repo: str,
    work_dir: Path,
    shard_changes: pd.DataFrame,
    timeout_seconds: int,
    url: str | None = None,
) -> tuple[RepoResult, pd.DataFrame, pd.DataFrame]:
    """Mine only the deleted skill folders of a repo that already has a shard.

    The repo is cloned as in stage 3. ``deleted_skill_folders`` compares the
    history with the shard's own rows, and only the folders it returns are
    logged, by their exact path without ``--follow`` (a deleted ``SKILL.md`` is
    not at HEAD, so it cannot be followed). Never raises: a failure is a status
    with two empty frames. The clone is deleted.

    Inputs: ``repo``, ``work_dir`` for the clone, the repo's current shard
    changes, ``timeout_seconds`` per git command, ``url`` for tests.
    Outputs: a ``RepoResult`` (``deleted_folders`` is the folder count) and
    the new change and text rows, which the caller appends to the shard.
    """
    started = time.perf_counter()
    clone_dir = work_dir / _shard_stem(repo)
    try:
        _delete_tree(clone_dir)
        clone_history_only(repo, clone_dir, timeout_seconds, url)
        head = _head_commit(clone_dir, timeout_seconds)
        mined = set(shard_changes["skill_folder"].astype(str)) if len(shard_changes) else set()
        deleted = deleted_skill_folders(
            _history_skill_md_paths(clone_dir, timeout_seconds),
            mined,
            _covered_skill_folders(shard_changes),
        )
        changes, kept, moved, dropped, note = _deleted_folder_rows(
            repo, clone_dir, shard_changes, deleted, timeout_seconds
        )
        texts, _used_lazy = _texts_for_changes(clone_dir, changes, timeout_seconds)
        result = RepoResult(
            repo, "ok", note, head, 0.0, False,
            deleted_folders=kept, moved_folders=moved, history_folders_dropped=dropped,
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


def append_to_repo_shard(
    shard_dir: Path,
    repo: str,
    changes: pd.DataFrame,
    texts: pd.DataFrame,
    deleted_folders: int | None,
    dropped_folders: int | None = None,
    message: str = "",
    moved_folders: int | None = None,
) -> None:
    """Add deleted-folder rows to a finished shard and mark it as checked.

    The new change rows are added to the shard's changes, and new texts are
    added once per sha. The status file keeps its status and gets
    ``deleted_folders`` and ``deleted_folders_checked: true``. It is written
    last, so a crash leaves the old shard, which is checked again next time.

    Inputs: ``shard_dir``, ``repo``, the new rows from ``mine_deleted_folders``,
    the kept and dropped folder counts, and a note (for example a skip reason).
    Outputs: the three shard files, rewritten.
    """
    stem = _shard_stem(repo)
    changes_path = shard_dir / f"{stem}.changes.parquet"
    texts_path = shard_dir / f"{stem}.texts.parquet"
    status_path = shard_dir / f"{stem}.status.json"
    if len(changes):
        old = read_parquet(changes_path)
        joined = pd.concat([frame for frame in (old, changes) if len(frame)], ignore_index=True)
        targets = _former_targets(changes)
        joined = renumber_commit_seq(collapse_move_rows(joined, targets), targets)
        write_parquet(joined, changes_path)
    if len(texts):
        old_texts = pq.read_table(texts_path, schema=_TEXT_SCHEMA).to_pandas()
        all_texts = pd.concat([old_texts, texts], ignore_index=True)
        all_texts = all_texts.drop_duplicates("sha").sort_values("sha", kind="mergesort")
        _write_text_file(texts_path, all_texts.reset_index(drop=True))
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["deleted_folders"] = None if deleted_folders is None else int(deleted_folders)
    status["history_folders_dropped"] = None if dropped_folders is None else int(dropped_folders)
    status["moved_folders"] = None if moved_folders is None else int(moved_folders)
    status["deleted_folders_message"] = message
    status["deleted_folders_checked"] = True
    temporary = status_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(status_path)


def list_skill_md_paths_in_history(clone_dir: Path) -> list[str]:
    """Every ``SKILL.md`` path that existed in any commit of the clone (GIT12).

    Deleted and renamed skills are included. Inputs: a history clone.
    Outputs: repo-relative paths, sorted, each once.
    """
    return _history_skill_md_paths(clone_dir, DOWNLOAD_TIMEOUT_SECONDS)


def _history_skill_md_paths(clone_dir: Path, timeout_seconds: int) -> list[str]:
    """GIT12. Paths named skill.md in any case, from every commit's file list."""
    # Git command GIT12, see CODE_FLOW.md
    text = run_git(
        [
            "log",
            "--format=",
            "--name-only",
            "--no-renames",
            "--",
            ":(icase,glob)**/skill.md",
        ],
        clone_dir,
        timeout_seconds,
    )
    return sorted({_unquote_path(line.strip()) for line in text.splitlines() if line.strip()})


def _covered_skill_folders(changes: pd.DataFrame) -> set[str]:
    """Folders of the ``SKILL.md`` paths already in the rows (new and old names)."""
    if changes.empty:
        return set()
    skill = changes.loc[changes["is_skill_md"].fillna(False).astype(bool)]
    found: set[str] = set()
    for column in ("repo_path", "old_repo_path"):
        if column not in skill.columns:
            continue
        for path in skill[column].astype(str):
            if path and path not in {"nan", "None"}:
                found.add(_parent_folder(path))
    return found


def keep_deleted_folders(
    extra: pd.DataFrame,
    base: pd.DataFrame,
    created_before: str | None = None,
) -> tuple[pd.DataFrame, int, int]:
    """Keep the history-only folders that are really deleted skills.

    Rule, per folder in ``extra``:
    1. moved: one of its ``SKILL.md`` versions (blob ids) is also a ``SKILL.md``
       version in ``base`` (the folders that still exist). It is not a deleted
       skill; ``attach_moved_folders`` keeps its rows as earlier history.
    2. not visibly deleted: its newest ``SKILL.md`` row is not a delete (``D``)
       or a rename out of the folder. The folder is gone but git shows no
       delete (for example a merge or a large restructure), so it is dropped.
    3. ``created_before`` (an ISO time): a folder whose first row is later is
       dropped, so a backfill does not add skills newer than the snapshot.

    Inputs: the new rows, the existing rows of the repo, an optional time.
    Outputs: the kept rows, the kept folder count, the dropped folder count.

    Example:
    >>> base = pd.DataFrame({"skill_folder": ["s/new"], "is_skill_md": [True],
    ...     "old_sha": ["0" * 40], "new_sha": ["b" * 40], "status": ["A"],
    ...     "commit_seq": [1], "repo_path": ["s/new/SKILL.md"],
    ...     "commit_date": ["2026-03-01T00:00:00Z"]})
    >>> extra = pd.DataFrame({"skill_folder": ["s/old", "s/old", "s/gone", "s/gone"],
    ...     "is_skill_md": [True] * 4,
    ...     "old_sha": ["0" * 40, "b" * 40, "0" * 40, "c" * 40],
    ...     "new_sha": ["b" * 40, "0" * 40, "c" * 40, "0" * 40],
    ...     "status": ["A", "D", "A", "D"], "commit_seq": [1, 2, 1, 2],
    ...     "repo_path": ["s/old/SKILL.md"] * 2 + ["s/gone/SKILL.md"] * 2,
    ...     "commit_date": ["2026-01-01T00:00:00Z"] * 4})
    >>> rows, kept, dropped = keep_deleted_folders(extra, base)
    >>> sorted(set(rows["skill_folder"])), kept, dropped
    (['s/gone'], 1, 1)
    """
    if extra.empty:
        return extra, 0, 0
    base_blobs = _skill_blobs(base)
    keep: list[str] = []
    folders = list(dict.fromkeys(extra["skill_folder"].astype(str)))
    for folder, rows in extra.groupby(extra["skill_folder"].astype(str), sort=False):
        skill = rows.loc[rows["is_skill_md"].fillna(False).astype(bool)]
        if skill.empty or _skill_blobs(skill) & base_blobs:
            continue
        if not _ends_deleted(str(folder), skill):
            continue
        if created_before is not None and _first_commit_time(rows) > pd.Timestamp(created_before):
            continue
        keep.append(str(folder))
    kept_rows = extra.loc[extra["skill_folder"].astype(str).isin(set(keep))]
    return kept_rows.reset_index(drop=True), len(keep), len(folders) - len(keep)


def attach_moved_folders(extra: pd.DataFrame, base: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Keep the history of moved skills as ``former_folder`` rows of their new folder.

    A history-only folder moved when one of its ``SKILL.md`` versions is also a
    version in a folder of ``base``. Its target is the base folder that holds
    its last ``SKILL.md`` version (the text it had before the move), else the
    base folder that shares most of its versions, then the smaller name. Its
    rows get the target's ``skill_folder`` and ``history_role`` ``former_folder``;
    ``file_path`` stays relative to the old folder, as for a followed rename.
    Without these rows a skill would look younger than its copies.

    Inputs: the history-only rows and the repo's rows of folders that exist.
    Outputs: the attached rows and the number of folders attached.

    Example:
    >>> base = pd.DataFrame({"skill_folder": ["new/pdf"], "is_skill_md": [True],
    ...     "old_sha": ["0" * 40], "new_sha": ["b" * 40], "status": ["A"], "commit_seq": [1]})
    >>> extra = pd.DataFrame({"skill_folder": ["old/pdf"] * 2, "is_skill_md": [True] * 2,
    ...     "old_sha": ["0" * 40, "b" * 40], "new_sha": ["b" * 40, "0" * 40],
    ...     "status": ["A", "D"], "commit_seq": [1, 2], "history_role": ["own"] * 2})
    >>> rows, count = attach_moved_folders(extra, base)
    >>> sorted(set(rows["skill_folder"])), sorted(set(rows["history_role"])), count
    (['new/pdf'], ['former_folder'], 1)
    """
    if extra.empty or base.empty:
        return extra.iloc[0:0], 0
    holders = _blob_holders(base)
    parts: list[pd.DataFrame] = []
    for folder, rows in extra.groupby(extra["skill_folder"].astype(str), sort=False):
        target = _move_target(rows, holders, str(folder))
        if target is None:
            continue
        moved = rows.copy()
        moved["skill_folder"] = target
        moved["history_role"] = "former_folder"
        parts.append(moved)
    if not parts:
        return extra.iloc[0:0], 0
    return pd.concat(parts, ignore_index=True), len(parts)


def collapse_move_rows(changes: pd.DataFrame, folders: set[str]) -> pd.DataFrame:
    """Turn the delete + add of a moved file into one rename row, as ``--follow`` shows it.

    In ``folders``, a ``former_folder`` row with status ``D`` and a row with
    status ``A`` for the same ``file_path`` in the same commit are one move. The
    add row becomes ``R`` with the delete's old path, blob and mode, and the
    delete row is dropped. Without this, each move is two rows with one key.

    Example:
    >>> rows = pd.DataFrame({"skill_folder": ["new/pdf"] * 2, "commit": ["c2", "c2"],
    ...     "file_path": ["SKILL.md"] * 2, "status": ["D", "A"],
    ...     "history_role": ["former_folder", "own"], "old_file_path": ["", ""],
    ...     "repo_path": ["old/pdf/SKILL.md", "new/pdf/SKILL.md"], "old_repo_path": ["", ""],
    ...     "old_sha": ["b" * 40, "0" * 40], "new_sha": ["0" * 40, "b" * 40],
    ...     "old_mode": ["100644", "000000"], "new_mode": ["000000", "100644"]})
    >>> out = collapse_move_rows(rows, {"new/pdf"})
    >>> out[["status", "old_repo_path", "repo_path"]].values.tolist()
    [['R', 'old/pdf/SKILL.md', 'new/pdf/SKILL.md']]
    """
    if changes.empty or not folders:
        return changes
    key = ["skill_folder", "commit", "file_path"]
    in_scope = changes["skill_folder"].astype(str).isin(folders)
    deletes = changes.loc[
        in_scope & (changes["status"] == "D") & (changes["history_role"] == "former_folder")
    ]
    adds = changes.loc[in_scope & (changes["status"] == "A")]
    if deletes.empty or adds.empty:
        return changes
    once_d = deletes.drop_duplicates(key, keep=False)
    once_a = adds.drop_duplicates(key, keep=False)
    delete_side = once_d.reset_index()[[*key, "index", "repo_path", "old_sha", "old_mode"]]
    delete_side = delete_side.rename(
        columns={
            "index": "d_index",
            "repo_path": "d_repo_path",
            "old_sha": "d_old_sha",
            "old_mode": "d_old_mode",
        }
    )
    pairs = once_a.reset_index().merge(delete_side, on=key)
    if pairs.empty:
        return changes
    out = changes.copy()
    at = pairs["index"].to_numpy()
    out.loc[at, "status"] = "R"
    out.loc[at, "old_file_path"] = pairs["file_path"].to_numpy()
    out.loc[at, "old_repo_path"] = pairs["d_repo_path"].to_numpy()
    out.loc[at, "old_sha"] = pairs["d_old_sha"].to_numpy()
    out.loc[at, "old_mode"] = pairs["d_old_mode"].to_numpy()
    return out.drop(index=pairs["d_index"].to_numpy()).reset_index(drop=True)


def renumber_commit_seq(changes: pd.DataFrame, folders: set[str]) -> pd.DataFrame:
    """Number the commits of ``folders`` again, oldest first, by commit time.

    Used after earlier rows were attached to a folder. Other folders keep
    their numbers. Commits with the same time keep the order of their first row.

    Example:
    >>> rows = pd.DataFrame({"skill_folder": ["s"] * 3, "commit": ["c3", "c1", "c2"],
    ...     "commit_seq": [1, 1, 2],
    ...     "commit_date": ["2026-03-01T00:00:00Z", "2026-01-01T00:00:00Z",
    ...                     "2026-02-01T00:00:00Z"]})
    >>> renumber_commit_seq(rows, {"s"})["commit_seq"].tolist()
    [3, 1, 2]
    """
    if changes.empty or not folders:
        return changes
    out = changes.copy()
    mask = out["skill_folder"].astype(str).isin(folders)
    part = out.loc[mask, ["skill_folder", "commit", "commit_date"]].copy()
    part["time"] = pd.to_datetime(part["commit_date"], utc=True, errors="coerce", format="ISO8601")
    first = part.groupby(["skill_folder", "commit"], sort=False)["time"].min().reset_index()
    first = first.sort_values(["skill_folder", "time"], kind="mergesort")
    first["seq"] = first.groupby("skill_folder").cumcount() + 1
    seq = first.set_index(["skill_folder", "commit"])["seq"]
    keys = pd.MultiIndex.from_frame(out.loc[mask, ["skill_folder", "commit"]])
    out.loc[mask, "commit_seq"] = seq.reindex(keys).to_numpy()
    out["commit_seq"] = out["commit_seq"].astype("int64")
    return out


def _former_targets(rows: pd.DataFrame) -> set[str]:
    """Folders that received ``former_folder`` rows from moved folders."""
    if rows.empty or "history_role" not in rows.columns:
        return set()
    return set(rows.loc[rows["history_role"] == "former_folder", "skill_folder"].astype(str))


def _blob_holders(base: pd.DataFrame) -> dict[str, set[str]]:
    """``SKILL.md`` blob id -> base folders that hold it."""
    holders: dict[str, set[str]] = {}
    skill = base.loc[base["is_skill_md"].fillna(False).astype(bool)]
    for row in skill.loc[:, ["skill_folder", "old_sha", "new_sha"]].to_dict("records"):
        for sha in _real_shas(str(row["old_sha"]), str(row["new_sha"])):
            holders.setdefault(sha, set()).add(str(row["skill_folder"]))
    return holders


def _move_target(rows: pd.DataFrame, holders: dict[str, set[str]], folder: str) -> str | None:
    """The base folder a moved folder went to, or None when it shares no version."""
    skill = rows.loc[rows["is_skill_md"].fillna(False).astype(bool)].sort_values(
        "commit_seq", kind="mergesort"
    )
    versions: list[str] = []
    for row in skill.to_dict("records"):
        versions.extend(_real_shas(str(row["new_sha"]), str(row["old_sha"])))
    shared: dict[str, int] = {}
    for sha in versions:
        for target in holders.get(sha, set()) - {folder}:
            shared[target] = shared.get(target, 0) + 1
    if not shared:
        return None
    last = holders.get(versions[-1], set()) - {folder} if versions else set()
    if last:
        return min(last, key=lambda name: (-shared.get(name, 0), name))
    return min(shared, key=lambda name: (-shared[name], name))


def _first_commit_time(rows: pd.DataFrame) -> pd.Timestamp:
    """Earliest commit time of ``rows`` in UTC. Dates carry their own offsets (``+09:00``)."""
    times = pd.to_datetime(rows["commit_date"], utc=True, errors="coerce", format="ISO8601")
    return times.min()


def _skill_blobs(changes: pd.DataFrame) -> set[str]:
    """Real blob ids of the ``SKILL.md`` rows (old and new side)."""
    if changes.empty:
        return set()
    skill = changes.loc[changes["is_skill_md"].fillna(False).astype(bool)]
    found: set[str] = set()
    for column in ("old_sha", "new_sha"):
        for sha in skill[column].astype(str):
            found.update(_real_shas(sha))
    return found


def _ends_deleted(folder: str, skill_rows: pd.DataFrame) -> bool:
    """True when the newest ``SKILL.md`` change deletes it or renames it out of ``folder``."""
    newest = skill_rows.loc[skill_rows["commit_seq"] == skill_rows["commit_seq"].max()]
    prefix = folder.strip("/") + "/"
    for row in newest.to_dict("records"):
        if row["status"] == "D":
            return True
        if row["status"] == "R" and not str(row.get("repo_path") or "").startswith(prefix):
            return True
    return False


def _deleted_folder_rows(
    repo: str,
    clone_dir: Path,
    base: pd.DataFrame,
    deleted: list[str],
    timeout_seconds: int,
) -> tuple[pd.DataFrame, int | None, int | None, int | None, str]:
    """Log history-only folders by exact path (no --follow); keep deletions and moves.

    Returns the rows to add (deleted skills, and moved skills attached to their
    new folder), the deleted, moved and dropped folder counts, and a note.
    More than ``DELETED_FOLDERS_MAX`` candidates are not logged at all: the
    counts are None and the note says why.
    """
    if len(deleted) > DELETED_FOLDERS_MAX:
        note = (
            f"skipped {len(deleted)} history-only folders (over {DELETED_FOLDERS_MAX}); "
            "the layout was rebuilt or moved"
        )
        return _empty_changes(), None, None, None, note
    if not deleted:
        return _empty_changes(), 0, 0, 0, ""
    grouped, _drops = _grouped_logs(clone_dir, deleted, False, timeout_seconds)
    extra = _changes_frame(repo, grouped)
    del grouped
    kept, kept_count, _not_kept = keep_deleted_folders(extra, base)
    attached, moved_count = attach_moved_folders(extra, base)
    dropped = extra["skill_folder"].nunique() - kept_count - moved_count if len(extra) else 0
    rows = pd.concat([frame for frame in (kept, attached) if len(frame)], ignore_index=True)
    if rows.empty:
        rows = _empty_changes()
    return rows, kept_count, moved_count, int(dropped), ""


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
) -> tuple[list[tuple[str, list[dict]]], dict[str, int]]:
    """Pair each skill folder with its rows, and count rows dropped by reason."""
    drops = {"not_ours": 0, "after_copy": 0, "twin": 0}
    index = _FolderIndex(folders)
    if follow:
        grouped = []
        progress = _Progress(clone_dir, "folder", len(folders))
        for done, folder in enumerate(folders, start=1):
            folder_rows, part = _read_folder_log(
                clone_dir, folder, True, timeout_seconds
            )
            grouped.append((folder, folder_rows))
            for key in drops:
                drops[key] += part[key]
            progress.tick(done)
    else:
        rows = _read_folder_log_many(clone_dir, folders, timeout_seconds)
        grouped = _split_rows_by_index(folders, index, rows)
    grouped = [
        (folder, _drop_deeper_skill_rows(folder, folder_rows, index))
        for folder, folder_rows in grouped
    ]
    return grouped, drops


def _split_rows_by_folder(folders: list[str], rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Give each row to the longest requested folder that contains its new path."""
    return _split_rows_by_index(folders, _FolderIndex(folders), rows)


def _split_rows_by_index(
    folders: list[str], index: _FolderIndex, rows: list[dict]
) -> list[tuple[str, list[dict]]]:
    """``_split_rows_by_folder`` with a prebuilt index of ``folders``."""
    grouped: dict[str, list[dict]] = {folder: [] for folder in folders}
    for row in rows:
        folder = index.owner(row["path"])
        if folder is None and row["old_path"]:
            folder = index.owner(row["old_path"])
        if folder is None:
            continue
        grouped[folder].append(row)
    return [(folder, grouped[folder]) for folder in folders]


def _changes_frame(repo: str, grouped: list[tuple[str, list[dict]]]) -> pd.DataFrame:
    """Build the changes table. ``commit_seq`` 1 is the oldest kept commit in that folder."""
    records: list[dict] = []
    for folder, rows in grouped:
        prefixes = _prefixes(folder, rows)
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
        "repo_path": row["path"],
        "old_repo_path": row["old_path"] or "",
        "history_role": row.get("history_role") or "own",
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
    clone_dir: Path,
    folder: str,
    follow: bool,
    timeout_seconds: int,
) -> tuple[list[dict], dict[str, int]]:
    """GIT4 plus GIT5 when following, otherwise one GIT5 log."""
    zeros = {"not_ours": 0, "after_copy": 0, "twin": 0}
    cleaned = _clean_folder(folder)
    if not follow:
        return _one_folder_log(clone_dir, cleaned, timeout_seconds), zeros
    return _followed_folder_log(clone_dir, cleaned, timeout_seconds)


def _one_folder_log(clone_dir: Path, folder: str, timeout_seconds: int) -> list[dict]:
    """GIT5 for the folder as it is now."""
    # Git command GIT5, see CODE_FLOW.md
    text = run_git(
        _log_args(follow=False, pathspecs=[_pathspec(folder)]),
        clone_dir,
        timeout_seconds,
        allow_lazy=True,
    )
    return _log_rows(clone_dir, text)


def _followed_folder_log(
    clone_dir: Path, folder: str, timeout_seconds: int
) -> tuple[list[dict], dict[str, int]]:
    """Follow SKILL.md, then keep former names and copy sources."""
    follow_rows = _follow_skill_rows(clone_dir, folder, timeout_seconds)
    pathspecs = _followed_pathspecs(folder, follow_rows)
    # Git command GIT5, see CODE_FLOW.md
    text = run_git(
        _log_args(follow=False, pathspecs=pathspecs),
        clone_dir,
        timeout_seconds,
        allow_lazy=True,
    )
    parsed = _log_rows(clone_dir, text)
    parsed, drops = _classify_folder_rows(
        folder, parsed, follow_rows, clone_dir, timeout_seconds
    )
    kept = _keep_skill_life(folder, parsed, follow_rows, clone_dir, timeout_seconds)
    return kept, drops


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
        rows.extend(_log_rows(clone_dir, text))
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


def _classify_folder_rows(
    folder: str,
    rows: list[dict],
    follow_rows: list[dict],
    clone_dir: Path,
    timeout_seconds: int,
) -> tuple[list[dict], dict[str, int]]:
    """Mark own, former_folder and copy_source. A rename chain joins one hop at a time."""
    counts = {"not_ours": 0, "after_copy": 0, "twin": 0}
    for row in rows:
        row["history_role"] = "own"
    pending = _extra_skill_folders(folder, follow_rows)
    if not pending:
        return rows, counts
    accepted = {folder}
    trees: dict[str, list[dict]] = {}
    kept = rows
    while pending:
        kept, pending, grew = _accept_one_pass(
            folder, pending, accepted, kept, follow_rows, counts,
            trees, clone_dir, timeout_seconds,
        )
        if grew:
            continue
        for candidate in pending:
            kept = _drop_not_ours(kept, folder, candidate, [], counts)
        break
    return _drop_repeated_paths(kept, folder), counts


def _drop_repeated_paths(rows: list[dict], folder: str) -> list[dict]:
    """Keep one row when two folders strip to the same path in one commit."""
    role_rank = {"own": 0, "former_folder": 1, "copy_source": 2}
    status_rank = {"R": 0, "C": 1, "A": 2, "M": 3, "D": 4}
    best: dict[tuple[str, str], tuple[tuple[int, int], dict]] = {}
    for row in rows:
        key = (row["commit"], _row_relative_path(row, folder))
        score = (
            role_rank.get(str(row.get("history_role")), 9),
            status_rank.get(row["status"], 9),
        )
        current = best.get(key)
        if current is None or score < current[0]:
            best[key] = (score, row)
    chosen = {id(item[1]) for item in best.values()}
    return [row for row in rows if id(row) in chosen]


def _row_relative_path(row: dict, folder: str) -> str:
    """Skill-relative path used to spot two names of one file."""
    prefix = str(row.get("role_folder") or folder)
    return _relative_to_one(row["path"], prefix)


def _accept_one_pass(
    folder: str,
    pending: list[str],
    accepted: set[str],
    rows: list[dict],
    follow_rows: list[dict],
    counts: dict[str, int],
    trees: dict[str, list[dict]],
    clone_dir: Path,
    timeout_seconds: int,
) -> tuple[list[dict], list[str], bool]:
    """Accept every candidate that can join ``accepted`` on this pass."""
    waiting: list[str] = []
    grew = False
    kept = rows
    for candidate in pending:
        visible = {row["commit"] for row in kept}
        leave = _leave_toward_accepted(candidate, follow_rows, accepted, visible)
        if leave is None:
            waiting.append(candidate)
            continue
        tree = _cached_tree(trees, clone_dir, leave, timeout_seconds)
        kept, took = _apply_candidate(
            folder, candidate, leave, kept, follow_rows, tree, accepted, counts
        )
        if took:
            accepted.add(candidate)
        grew = True
    return kept, waiting, grew


def _cached_tree(
    trees: dict[str, list[dict]], clone_dir: Path, commit: str, timeout_seconds: int
) -> list[dict]:
    """GIT4b for ``commit``, read once per hop."""
    if commit not in trees:
        trees[commit] = _diff_tree_changes(clone_dir, commit, timeout_seconds)
    return trees[commit]


def _apply_candidate(
    folder: str,
    candidate: str,
    leave: str,
    rows: list[dict],
    follow_rows: list[dict],
    tree: list[dict],
    accepted: set[str],
    counts: dict[str, int],
) -> tuple[list[dict], bool]:
    """Keep or drop one GIT4 folder. True means it joined the accepted set."""
    fate = _candidate_fate(candidate, accepted, leave, rows, follow_rows, tree)
    if fate != "ours":
        return _drop_not_ours(rows, folder, candidate, tree, counts), False
    if _source_survives(candidate, tree):
        return _keep_copy_source(rows, folder, candidate, leave, counts), True
    return _keep_former_folder(rows, folder, candidate, leave, counts), True


def _source_survives(candidate: str, tree: list[dict]) -> bool:
    """True unless the leave commit deletes or renames the candidate's SKILL.md.

    ``tree`` is the GIT4b diff of that commit. A copy leaves the file in place.
    """
    for change in tree:
        if _deleted_skill(change, candidate):
            return False
        if change["status"] == "R" and _skill_left_candidate(change, candidate):
            return False
    return True


def _candidate_fate(
    candidate: str,
    accepted: set[str],
    leave: str,
    rows: list[dict],
    follow_rows: list[dict],
    tree: list[dict],
) -> str:
    """``ours`` when this hop's leave commit moves into an accepted folder.

    The full commit diff wins over the path-limited log. That log can pair
    the file with us when the commit renamed it to a third folder.
    """
    if _renamed_elsewhere(candidate, accepted, tree):
        return "third"
    groups = (
        tree,
        [row for row in rows if row["commit"] == leave],
        [row for row in follow_rows if row["commit"] == leave],
    )
    for group in groups:
        if _left_toward(candidate, accepted, group):
            return "ours"
    return "third"


def _leave_toward_accepted(
    candidate: str, follow_rows: list[dict], accepted: set[str], visible: set[str]
) -> str | None:
    """Newest commit in ``visible`` where the candidate's SKILL.md left toward ``accepted``.

    GIT4 (``--follow``) walks every branch, but GIT5 simplifies history: a
    merged branch whose net change to the pathspecs is nothing is pruned.
    A move made and undone on such a branch is in GIT4 but not in GIT5, and
    the leave commit must be a GIT5 commit.
    """
    for row in follow_rows:
        if row["commit"] not in visible:
            continue
        moved = _skill_left_candidate(row, candidate)
        moved = moved and _path_in_accepted(row["path"], accepted)
        deleted = _deleted_skill(row, candidate)
        deleted = deleted and _arrived_in(follow_rows, row["commit"], accepted)
        if moved or deleted:
            return row["commit"]
    return None


def _drop_not_ours(
    rows: list[dict],
    folder: str,
    candidate: str,
    tree: list[dict],
    counts: dict[str, int],
) -> list[dict]:
    """Drop the candidate. A false rename into this folder becomes an add."""
    third = _renamed_elsewhere(candidate, {folder}, tree)
    kept: list[dict] = []
    for row in rows:
        if _candidate_row(row, candidate, folder):
            counts["not_ours"] += 1
            continue
        if third and _moves_into(row, candidate, folder):
            kept.append(_as_plain_add(row))
            continue
        kept.append(row)
    return kept


def _keep_copy_source(
    rows: list[dict],
    folder: str,
    candidate: str,
    birth: str,
    counts: dict[str, int],
) -> list[dict]:
    """Keep the source only strictly before this folder's first SKILL.md."""
    index = _commit_index(rows)
    birth_at = index[birth]
    kept: list[dict] = []
    older = 0
    dropped = 0
    for row in rows:
        if not _candidate_row(row, candidate, folder):
            kept.append(row)
            continue
        if index[row["commit"]] > birth_at:
            row["history_role"] = "copy_source"
            row["role_folder"] = candidate
            kept.append(row)
            older += 1
            continue
        dropped += 1
    if older == 0:
        counts["twin"] += dropped
        return kept
    counts["after_copy"] += dropped
    return kept


def _keep_former_folder(
    rows: list[dict],
    folder: str,
    candidate: str,
    birth: str,
    counts: dict[str, int],
) -> list[dict]:
    """Keep the old name through the commit that left it, and store the move as R."""
    index = _commit_index(rows)
    birth_at = index[birth]
    kept: list[dict] = []
    for row in rows:
        if _moves_into(row, candidate, folder):
            row["role_folder"] = candidate
        if not _candidate_row(row, candidate, folder):
            kept.append(row)
            continue
        if index[row["commit"]] >= birth_at:
            row["history_role"] = "former_folder"
            row["role_folder"] = candidate
            kept.append(row)
            continue
        counts["not_ours"] += 1
    rewritten = _rewrite_delete_add(kept, folder, candidate, birth)
    return _drop_birth_copies(rewritten, folder, candidate, birth)


def _rewrite_delete_add(
    rows: list[dict], folder: str, candidate: str, birth: str
) -> list[dict]:
    """Turn a delete plus an add in the leave commit into one rename."""
    if any(_moves_into(row, candidate, folder) and row["commit"] == birth for row in rows):
        return rows
    deletes, adds = _delete_add_pairs(rows, folder, candidate, birth)
    paired = set(deletes) & set(adds)
    if not paired:
        return rows
    drop_ids = {id(deletes[rel]) for rel in paired} | {id(adds[rel]) for rel in paired}
    made = [_joined_rename(adds[rel], deletes[rel], candidate) for rel in sorted(paired)]
    return [row for row in rows if id(row) not in drop_ids] + made


def _delete_add_pairs(
    rows: list[dict], folder: str, candidate: str, birth: str
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Relative path to the delete under the old folder and the add under the new one."""
    deletes: dict[str, dict] = {}
    adds: dict[str, dict] = {}
    for row in rows:
        if row["commit"] != birth:
            continue
        if row["status"] == "D" and _path_in_folder(row["path"], candidate):
            deletes[_relative_to_one(row["path"], candidate)] = row
        if row["status"] == "A" and _path_in_folder(row["path"], folder):
            adds[_relative_to_one(row["path"], folder)] = row
    return deletes, adds


def _drop_birth_copies(
    rows: list[dict], folder: str, candidate: str, birth: str
) -> list[dict]:
    """Drop an old-folder row in the arrival commit when this folder has that path.

    The arrival row is the one that stays. Two rows would share file_path.
    """
    own = {
        _relative_to_one(row["path"], folder)
        for row in rows
        if row["commit"] == birth
        and row.get("history_role") != "former_folder"
        and _path_in_folder(row["path"], folder)
    }
    if not own:
        return rows
    kept: list[dict] = []
    for row in rows:
        if row.get("history_role") != "former_folder" or row["commit"] != birth:
            kept.append(row)
            continue
        rel = _relative_to_one(row["path"] or row["old_path"], candidate)
        if rel not in own:
            kept.append(row)
    return kept


def _joined_rename(add: dict, delete: dict, candidate: str) -> dict:
    """One R row. ``old_repo_path`` is the delete's full path."""
    joined = dict(add)
    joined["status"] = "R"
    joined["old_path"] = delete["path"]
    joined["old_sha"] = delete["old_sha"]
    joined["old_mode"] = delete["old_mode"]
    joined["history_role"] = "own"
    joined["role_folder"] = candidate
    return joined


def _birth_commit(folder: str, rows: list[dict]) -> str | None:
    """Oldest commit whose new path is this folder's own SKILL.md."""
    index = _commit_index(rows)
    birth = None
    best = -1
    for row in rows:
        if not _is_skill_md_name(row["path"]) or _parent_folder(row["path"]) != folder:
            continue
        place = index[row["commit"]]
        if place > best:
            birth = row["commit"]
            best = place
    return birth


def _extra_skill_folders(folder: str, follow_rows: list[dict]) -> list[str]:
    """Parents of SKILL.md in the follow log, other than ``folder``."""
    found: list[str] = []
    for row in follow_rows:
        for key in ("path", "old_path"):
            path = row[key]
            if not path or not _is_skill_md_name(path):
                continue
            parent = _parent_folder(path)
            if parent in {folder, ".", ""} or parent in found:
                continue
            found.append(parent)
    return found


def _diff_tree_changes(clone_dir: Path, commit: str, timeout_seconds: int) -> list[dict]:
    """Every file change in ``commit``, with no path limit. See GIT4b."""
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
    found: list[dict] = []
    for line in text.splitlines():
        if not line.startswith(":"):
            continue
        change = _parse_raw_line(line)
        if change is not None:
            found.append(change)
    return found


def _renamed_elsewhere(candidate: str, accepted: set[str], tree: list[dict]) -> bool:
    """True when the candidate's SKILL.md moves to a folder outside ``accepted``."""
    for change in tree:
        if not _skill_left_candidate(change, candidate):
            continue
        if not _path_in_accepted(change["path"], accepted):
            return True
    return False


def _left_toward(candidate: str, accepted: set[str], rows: list[dict]) -> bool:
    """True when the candidate's SKILL.md moves into ``accepted`` or is deleted there."""
    arrived = any(
        _is_skill_md_name(row["path"]) and _path_in_accepted(row["path"], accepted) for row in rows
    )
    for row in rows:
        if _skill_left_candidate(row, candidate) and _path_in_accepted(row["path"], accepted):
            return True
        if _deleted_skill(row, candidate) and arrived:
            return True
    return False


def _deleted_skill(row: dict, candidate: str) -> bool:
    """True when this row deletes the candidate's SKILL.md."""
    return (
        row["status"] == "D"
        and _is_skill_md_name(row["path"])
        and _path_in_folder(row["path"], candidate)
    )


def _arrived_in(rows: list[dict], commit: str, accepted: set[str]) -> bool:
    """True when ``commit`` adds or edits SKILL.md inside an accepted folder."""
    for row in rows:
        if row["commit"] != commit:
            continue
        if _is_skill_md_name(row["path"]) and _path_in_accepted(row["path"], accepted):
            return True
    return False


def _path_in_accepted(path: str, accepted: set[str]) -> bool:
    """True when ``path`` sits in any folder in ``accepted``."""
    return any(_path_in_folder(path, name) for name in accepted)


def _skill_left_candidate(change: dict, candidate: str) -> bool:
    """True when this row renames or copies SKILL.md out of ``candidate``."""
    if change["status"] not in {"R", "C"} or not change.get("old_path"):
        return False
    if not _is_skill_md_name(change["old_path"]):
        return False
    return _path_in_folder(change["old_path"], candidate)


def _candidate_row(row: dict, candidate: str, folder: str) -> bool:
    """True when the row is in the other folder, not in this one."""
    if _path_in_folder(row["path"], folder):
        return False
    if _path_in_folder(row["path"], candidate):
        return True
    return bool(row["old_path"]) and _path_in_folder(row["old_path"], candidate)


def _moves_into(row: dict, candidate: str, folder: str) -> bool:
    """True when this row renames or copies a file from ``candidate`` into ``folder``."""
    if row["status"] not in {"R", "C"} or not row.get("old_path"):
        return False
    return _path_in_folder(row["old_path"], candidate) and _path_in_folder(row["path"], folder)


def _as_plain_add(row: dict) -> dict:
    """Store a false rename as an add. The old path belonged to another skill."""
    added = dict(row)
    added["status"] = "A"
    added["old_path"] = ""
    added["old_sha"] = _ZERO_SHA
    added["old_mode"] = "000000"
    added["history_role"] = "own"
    return added


def _path_in_folder(path: str, folder: str) -> bool:
    """True when ``path`` is a file inside ``folder``. The root is only root files."""
    if not path:
        return False
    if folder in {"", "."}:
        return "/" not in path
    return path.startswith(folder.strip("/") + "/")


def _relative_to_one(path: str, folder: str) -> str:
    """Path with one folder prefix removed. A root folder returns the path."""
    if not path or folder in {"", "."}:
        return path
    prefix = folder.strip("/") + "/"
    if path.startswith(prefix):
        return path[len(prefix):]
    return path


def _keep_skill_life(
    folder: str,
    rows: list[dict],
    follow_rows: list[dict],
    clone_dir: Path,
    timeout_seconds: int,
) -> list[dict]:
    """Drop rows that belong to a different skill that reused a folder name.

    An old folder's rows stay only through the commit that renamed or copied
    SKILL.md out of it. Later commits of a copy that still exists at the old
    path belong to that other skill. A plain delete is not a cut: restoring
    SKILL.md keeps the older commits, including other files.
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
    """Old folder name to the commit that renamed or copied SKILL.md out of it.

    Status ``C`` is a copy: the old path still exists. Status ``R`` is a move.
    Rows are newest first, so the first jump away from a folder is the cut.
    """
    left: dict[str, str] = {}
    known = {folder}
    for row in follow_rows:
        if not _moved_skill_md(row):
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
    """GIT6: one log for all folders, no ``--follow``.

    The pathspecs go on standard input (``--stdin``). Tens of thousands of
    folders are more than the 1 MB argument limit of macOS. Up to
    ``GIT6_MAX_PATHSPECS`` folders are logged by name. Git compares every
    changed entry with every pathspec, so more folders than that are logged
    through their parent folders (``_covering_pathspecs``), and the rows are
    then cut back to what a log of the folders themselves shows
    (``_rows_as_if_exact``): a rename from outside the folders is an add, and
    a rename out of them is a delete. One log keeps every row in git's
    newest-first order.
    """
    cleaned = _unique([_clean_folder(folder) for folder in folders])
    if not cleaned:
        return []
    covered = len(cleaned) > GIT6_MAX_PATHSPECS
    pathspecs = (
        _covering_pathspecs(cleaned, GIT6_MAX_PATHSPECS)
        if covered
        else [_pathspec(folder) for folder in cleaned]
    )
    started = time.perf_counter()
    if covered:
        _print_progress(
            clone_dir,
            f"log {len(cleaned)} folders through {len(pathspecs)} parent pathspecs",
        )
    payload = "".join(f"{spec}\n" for spec in ["--", *pathspecs]).encode("utf-8")
    # Git command GIT6, see CODE_FLOW.md
    data = _run_git_bytes(
        _log_args(follow=False, pathspecs=[], stdin=True),
        clone_dir,
        timeout_seconds,
        payload=payload,
        allow_lazy=True,
    )
    rows = _log_rows(clone_dir, data.decode("utf-8", "replace"))
    if covered:
        rows = _rows_as_if_exact(rows, _FolderIndex(cleaned))
        seconds = time.perf_counter() - started
        _print_progress(clone_dir, f"log done: {len(rows)} rows in {seconds:.0f}s")
    return rows


def _covering_pathspecs(folders: list[str], limit: int) -> list[str]:
    """At most ``limit`` parent folders that hold every folder in ``folders``.

    Folders are cut to fewer path parts until the set is small enough. When
    even the top-level names are too many, or a folder is the repo root, the
    pathspec is ``.`` (the whole repo).

    Example:
    >>> _covering_pathspecs(["skills/a/x", "skills/a/y", "skills/b/z"], 2)
    ['skills/a/', 'skills/b/']
    >>> _covering_pathspecs(["skills/a/x", "skills/b/z", "tools/t"], 2)
    ['skills/', 'tools/']
    >>> _covering_pathspecs(["a", "b", "c"], 2)
    ['.']
    """
    if any(folder in {"", "."} for folder in folders):
        return ["."]
    parts = [folder.split("/") for folder in folders]
    depth = max(len(bits) for bits in parts)
    while depth >= 1:
        cut = {"/".join(bits[:depth]) for bits in parts}
        if len(cut) <= limit:
            kept = [
                folder
                for folder in sorted(cut)
                if not any(folder.startswith(other + "/") for other in _ancestors(folder) & cut)
            ]
            return [_pathspec(folder) for folder in kept]
        depth -= 1
    return ["."]


def _ancestors(folder: str) -> set[str]:
    """Every parent folder of ``folder``: ``a/b/c`` gives ``a`` and ``a/b``."""
    bits = folder.split("/")
    return {"/".join(bits[:size]) for size in range(1, len(bits))}


def _rows_as_if_exact(rows: list[dict], index: _FolderIndex) -> list[dict]:
    """Keep the rows a log of the folders themselves would print.

    A log through parent folders also sees files beside the skill folders.
    Rows wholly outside every folder are dropped. A rename (or copy) from
    outside into a folder becomes an add. A rename from a folder to outside
    becomes a delete; a copy to outside is dropped, since the source did not
    change. Inexact rename detection can still differ, because git stops
    looking for edited renames when a commit has too many files.
    """
    kept: list[dict] = []
    for row in rows:
        new_inside = index.owner(row["path"]) is not None
        if row["status"] not in {"R", "C"} or not row["old_path"]:
            if new_inside:
                kept.append(row)
            continue
        old_inside = index.owner(row["old_path"]) is not None
        if new_inside and old_inside:
            kept.append(row)
        elif new_inside:
            kept.append(
                {**row, "status": "A", "old_path": "", "old_sha": _ZERO_SHA, "old_mode": "000000"}
            )
        elif old_inside and row["status"] == "R":
            kept.append(
                {
                    **row,
                    "status": "D",
                    "path": row["old_path"],
                    "old_path": "",
                    "new_sha": _ZERO_SHA,
                    "new_mode": "000000",
                }
            )
    return kept


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
    batches = range(0, len(missing), BLOB_FETCH_BATCH)
    progress = _Progress(clone_dir, f"texts batch (of {len(missing)} blobs)", len(batches))
    for done, start in enumerate(batches, start=1):
        batch = missing[start : start + BLOB_FETCH_BATCH]
        try:
            _fetch_blob_batch(clone_dir, batch, timeout_seconds)
        except GitError:
            lazy.extend(batch)
        progress.tick(done)
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
    found: dict[str, str] = {}
    # Read in chunks so one huge cat-file output is never held in memory.
    for start in range(0, len(chosen), BLOB_READ_BATCH):
        part = chosen[start : start + BLOB_READ_BATCH]
        # Git command GIT8, see CODE_FLOW.md
        data = _run_git_bytes(
            ["cat-file", "--batch"],
            clone_dir,
            timeout_seconds,
            payload=("\n".join(part) + "\n").encode("ascii"),
            allow_lazy=allow_lazy,
        )
        found.update(_parse_cat_file_batch(data, max_bytes))
        del data
    return found


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


def _log_args(follow: bool, pathspecs: list[str], stdin: bool = False) -> list[str]:
    """Build GIT4 (with ``--follow``) or GIT5/GIT6 (without it).

    ``stdin`` reads the pathspecs from standard input, after a ``--`` line.
    """
    args = ["log"]
    if follow:
        args.append("--follow")
    if stdin:
        args.append("--stdin")
    args.extend(
        [
            "--raw",
            "--no-abbrev",
            "--find-renames",
            "--date=iso-strict",
            f"--format={_LOG_FORMAT}",
        ]
    )
    if not stdin:
        args.extend(["--", *pathspecs])
    return args


def _clone_args(
    remote: str, target_dir: Path, extra: list[str] | None = None
) -> list[str]:
    """GIT1. A ``file://`` URL also allows the file protocol, which tests need.

    ``extra`` adds flags before the URL, such as ``--shallow-since``.
    """
    args = [
        "clone",
        "--filter=blob:none",
        "--no-checkout",
        "--single-branch",
        *(extra or []),
        remote,
        str(target_dir),
    ]
    if remote.startswith("file:"):
        return ["-c", "protocol.file.allow=always", *args]
    return args


def _deepen_args(remote: str) -> list[str]:
    """GIT1b. Fetch one more commit behind the shallow boundary.

    The clone's own partial-clone filter still applies, so no file contents
    are downloaded.
    """
    args = ["fetch", "--quiet", "--deepen=1", "origin"]
    if remote.startswith("file:"):
        return ["-c", "protocol.file.allow=always", *args]
    return args


def _shallow_boundary(clone_dir: Path) -> frozenset[str]:
    """Commits listed in ``.git/shallow``. Empty for a clone with full history.

    Git shows each of these as adding every file it holds, because their
    parents were not downloaded. Their log rows are false and are dropped.
    """
    path = clone_dir / ".git" / "shallow"
    if not path.is_file():
        return frozenset()
    lines = path.read_text(encoding="utf-8").split()
    return frozenset(line for line in lines if line)


def _log_rows(clone_dir: Path, text: str) -> list[dict]:
    """``parse_raw_log`` without the rows of shallow-boundary commits."""
    rows = parse_raw_log(text)
    boundary = _shallow_boundary(clone_dir)
    if not boundary:
        return rows
    return [row for row in rows if row["commit"] not in boundary]


def _head_clone_args(remote: str, target_dir: Path) -> list[str]:
    """GIT9. GIT1 plus ``--depth 1``: only the newest commit and its trees."""
    args = [
        "clone",
        "--depth",
        "1",
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


def _prefixes(folder: str, rows: list[dict]) -> list[str]:
    """Folder names used to strip a repo path down to a skill-relative path.

    Only a folder this skill was renamed or copied from is stripped, so a
    nested ``cn/SKILL.md`` stays ``cn/SKILL.md``. A root skill does not treat
    ``skills/y/SKILL.md`` as its own file: that row is dropped earlier.
    A folder this skill left is still stripped, even when a new skill uses it.
    """
    found: list[str] = []
    if folder not in {"", "."}:
        found.append(folder.strip("/"))
    for row in rows:
        extra = str(row.get("role_folder") or "")
        if extra and extra not in {"", "."} and extra not in found:
            found.append(extra)
    historical = _historical_skill_folders(folder, rows)
    for row in rows:
        for key in ("path", "old_path"):
            path = row[key]
            if not path or not _is_skill_md_name(path):
                continue
            parent = _parent_folder(path)
            if parent in {"", "."} or parent in found or parent not in historical:
                continue
            found.append(parent)
    return found


def _moved_skill_md(row: dict) -> bool:
    """True when this row renames or copies SKILL.md into a different folder.

    ``C`` means the old file is still there. ``R`` means it was moved.

    Example:
    >>> _moved_skill_md({"status": "C", "old_path": "a/SKILL.md", "path": "b/SKILL.md"})
    True
    >>> _moved_skill_md({"status": "R", "old_path": "a/SKILL.md", "path": "b/README.md"})
    True
    >>> _moved_skill_md({"status": "M", "old_path": "", "path": "b/SKILL.md"})
    False
    """
    if row["status"] not in {"R", "C"} or not row["old_path"]:
        return False
    if not _is_skill_md_name(row["path"]) and not _is_skill_md_name(row["old_path"]):
        return False
    return _parent_folder(row["path"]) != _parent_folder(row["old_path"])


def _historical_skill_folders(folder: str, rows: list[dict]) -> set[str]:
    """Folder names this skill used before the current one. Rows are newest first."""
    current = "" if folder in {"", "."} else folder.strip("/")
    names: set[str] = set()
    for row in rows:
        if not _moved_skill_md(row):
            continue
        new_parent = _parent_folder(row["path"])
        old_parent = _parent_folder(row["old_path"])
        if new_parent != current and new_parent not in names:
            continue
        if old_parent not in {"", "."}:
            names.add(old_parent)
    return names


def _drop_deeper_skill_rows(
    folder: str, rows: list[dict], index: _FolderIndex
) -> list[dict]:
    """Drop rows that belong to a chosen skill folder deeper than ``folder``."""
    kept: list[dict] = []
    for row in rows:
        if _owned_by_deeper_skill(row["path"], folder, index):
            continue
        if row["old_path"] and _owned_by_deeper_skill(row["old_path"], folder, index):
            continue
        kept.append(row)
    return kept


def _owned_by_deeper_skill(path: str, folder: str, index: _FolderIndex) -> bool:
    """True when the longest chosen folder for ``path`` is deeper than ``folder``.

    A historical path whose parent is not a chosen folder is kept. That is
    how a rename (``skills/pdf`` to ``skills/pdf-tools``) stays in the log.
    """
    if not path:
        return False
    owner = index.owner(path)
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
    return _FolderIndex(folders).owner(path)


class _FolderIndex:
    """Find the longest requested folder that holds a path.

    Build it once per repo. ``owner`` walks the path's parent folders, so a
    lookup costs the path depth, not the number of folders.

    Example:
    >>> index = _FolderIndex(["skills/pdf", "skills/pdf/cn", "."])
    >>> index.owner("skills/pdf/cn/SKILL.md"), index.owner("skills/pdf/a.py")
    ('skills/pdf/cn', 'skills/pdf')
    >>> index.owner("README.md")
    '.'
    """

    def __init__(self, folders: list[str]) -> None:
        self._by_name: dict[str, str] = {}
        self._root: str | None = None
        for folder in folders:
            if folder in {"", "."}:
                if self._root is None:
                    self._root = "."
                continue
            self._by_name.setdefault(folder.strip("/"), folder)

    def owner(self, path: str) -> str | None:
        """The requested folder for ``path``, ``.`` for a root skill, or None."""
        bits = path.split("/")
        for size in range(len(bits) - 1, 0, -1):
            found = self._by_name.get("/".join(bits[:size]))
            if found is not None:
                return found
        return self._root


class _Progress:
    """Print ``progress <repo> <step> <done>/<total> <seconds>s`` for a long step.

    Nothing is printed for a step with one item. Otherwise a line is printed
    at most every ``PROGRESS_SECONDS`` and once more at the end, so a log of
    thousands of repos stays short.
    """

    def __init__(self, clone_dir: Path, step: str, total: int) -> None:
        self._clone_dir = clone_dir
        self._step = step
        self._total = total
        self._started = time.perf_counter()
        self._last = self._started

    def tick(self, done: int) -> None:
        """Record that ``done`` of ``total`` items are finished."""
        if self._total <= 1:
            return
        now = time.perf_counter()
        if done < self._total and now - self._last < PROGRESS_SECONDS:
            return
        self._last = now
        _print_progress(
            self._clone_dir,
            f"{self._step} {done}/{self._total} {now - self._started:.0f}s",
        )


def _print_progress(clone_dir: Path, text: str) -> None:
    """One progress line. The repo name is the clone folder (``owner__repo``)."""
    print(f"progress {clone_dir.name.replace('__', '/', 1)} {text}", flush=True)


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
    if table.num_rows and not _sorted_by_sha(table):
        table = table.sort_by("sha")
    pq.write_table(table, path, compression="zstd")


def _sorted_by_sha(table: pa.Table) -> bool:
    """True when ``sha`` is already in order, so the texts need no sorted copy."""
    shas = table.column("sha").to_pylist()
    return all(left <= right for left, right in zip(shas, shas[1:], strict=False))


def _text_table(frame: pd.DataFrame) -> pa.Table:
    """Texts as an Arrow table. An empty frame still has ``sha`` and ``text``."""
    if frame.empty or "sha" not in frame.columns:
        return pa.table({"sha": [], "text": []}, schema=_TEXT_SCHEMA)
    return pa.table(
        {"sha": frame["sha"].astype(str).tolist(), "text": frame["text"].astype(str).tolist()},
        schema=_TEXT_SCHEMA,
    )


def _status_frame(rows: list[dict]) -> pd.DataFrame:
    """One row per shard status file, with a stable column order."""
    if not rows:
        return pd.DataFrame(columns=_STATUS_COLUMNS)
    frame = pd.DataFrame(rows)
    for column in _STATUS_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    for column in (
        "not_ours",
        "after_copy",
        "twin",
        "deleted_folders",
        "moved_folders",
        "history_folders_dropped",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0).astype("int64")
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
