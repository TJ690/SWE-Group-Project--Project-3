"""Turn registry SKILL.md history into one lineage per skill folder.

What: group each folder's SKILL.md commits into numbered text versions,
follow renames, name copies, mark deletions, and drop a lineage whose
newest text is not English.
Inputs: ``registry_history_changes.parquet``, ``registry_history_texts.parquet``,
``registry_history_status.parquet`` under ``data_dir(dev)``, and
``registries.parquet`` in the full data folder.
Outputs: ``lineages.parquet``, ``lineage_versions.parquet``,
``lineage_changes.parquet``, ``lineages_dropped.parquet``,
``lineage_package_versions.parquet`` and ``lineage_folder_manifests.parquet``
in ``data_dir(dev)``.
Run:
    python -m skill_genealogy.build_registry_lineages --dev
"""

from __future__ import annotations

import hashlib
import statistics
import time
from dataclasses import fields
from pathlib import Path

import pandas as pd

from skill_genealogy.compare_texts import (
    TextChange,
    compare_front_matter,
    compare_texts,
    file_category,
    is_skipped_path,
)
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
from skill_genealogy.settings import LINEAGE_TEXT_BATCH, data_dir
from skill_genealogy.text_helpers import detect_language, normalize_text

SKILL_MD = "SKILL.md"
OWN_ROLES = frozenset({"own", "former_folder"})
KNOWN_ROLES = frozenset({"own", "former_folder", "copy_source"})
ENGLISH_CODES = frozenset({"en", "unknown"})

# Query Q4.1, see QUERIES.md
SKILL_MD_CHANGES_SQL = """
SELECT *
FROM read_parquet(?)
WHERE is_skill_md
"""

# Query Q4.2, see QUERIES.md
SKILL_TEXTS_SQL = """
SELECT sha, text
FROM read_parquet(?)
WHERE sha IN (SELECT unnest(?))
"""

# Query Q4.3, see QUERIES.md
LINEAGE_FOLDER_CHANGES_SQL = """
SELECT *
FROM read_parquet(?)
WHERE history_role IN ('own', 'former_folder')
  AND repo || '::' || skill_folder IN (SELECT unnest(?))
"""

# Deletes first, then renames, then adds. A same-commit replace keeps the new file.
_APPLY_ORDER = {"D": 0, "R": 1, "T": 2, "A": 3, "C": 3, "M": 4}

TEXT_CHANGE_FIELDS = [item.name for item in fields(TextChange)]
FRONT_MATTER_FIELDS = [
    "name_changed",
    "description_changed",
    "fm_keys_added",
    "fm_keys_removed",
    "allowed_tools_added",
    "allowed_tools_removed",
    "permissions_changed",
]
CHANGE_COLUMNS = [
    "lineage_id",
    "from_seq",
    "to_seq",
    "release_date",
    *TEXT_CHANGE_FIELDS,
    *FRONT_MATTER_FIELDS,
]
VERSION_COLUMNS = [
    "lineage_id",
    "version_seq",
    "same_as_seq",
    "blob",
    "normalized_sha",
    "commit",
    "commit_date",
    "release_date",
    "body_chars",
    "language",
]
VERSION_CORE = [name for name in VERSION_COLUMNS if name != "language"]
LINEAGE_COLUMNS = [
    "lineage_id",
    "repo",
    "skill_folder",
    "former_paths",
    "copied_from",
    "first_date",
    "last_date",
    "version_count",
    "registry_removed",
    "removed_at",
    "newest_language",
    "is_english",
]
JOIN_COLUMNS = ["repo", "skill_folder", "lineage_id", "former_paths", "copied_from"]
OUTPUT_NAMES = (
    "lineages.parquet",
    "lineage_versions.parquet",
    "lineage_changes.parquet",
    "lineages_dropped.parquet",
    "lineage_package_versions.parquet",
    "lineage_folder_manifests.parquet",
)
PACKAGE_COLUMNS = [
    "lineage_id",
    "package_version_seq",
    "version_seq",
    "commit",
    "commit_date",
    "changed_skill_md",
    "changed_scripts",
    "changed_other_files",
    "file_count",
    "script_count",
]
STORED_MANIFEST_COLUMNS = [
    "lineage_id",
    "package_version_seq",
    "path",
    "sha",
    "mode",
    "size",
    "file_category",
]
COMMIT_MANIFEST_COLUMNS = [
    "lineage_id",
    "commit",
    "commit_date",
    "commit_seq",
    "path",
    "sha",
    "mode",
    "size",
    "file_category",
]


def read_skill_md_changes(dev: bool) -> pd.DataFrame:
    """Read ``is_skill_md`` rows from the stage 3 changes table.

    Nested files such as ``cn/SKILL.md`` stay in the frame. Later steps
    ignore them. The texts are loaded separately, and only for these blobs.

    Inputs: ``dev`` — True reads the dev history folder.
    Outputs: one row per SKILL.md change, including copies and old folders.
    Raises ``FileNotFoundError`` when the changes file is missing.
    """
    path = _history_dir(dev) / "registry_history_changes.parquet"
    flag = " --dev" if dev else ""
    command = f"python -m skill_genealogy.download_registry_history{flag}"
    check_inputs_exist(
        {"registry_history_changes": path},
        {"registry_history_changes": command},
    )
    connection = connect_duckdb()
    try:
        return connection.execute(SKILL_MD_CHANGES_SQL, [str(path)]).fetchdf()
    finally:
        connection.close()


# Algorithm A50, see CODE_FLOW.md
def join_renamed_folders(changes: pd.DataFrame) -> pd.DataFrame:
    """Attach ``lineage_id``, ``former_paths`` and ``copied_from``.

    The id is ``repo::skill_folder``, the folder at HEAD. ``former_paths``
    lists older skill folders, oldest first. ``copied_from`` is the newest
    copy-source SKILL.md before this folder's own SKILL.md.

    Inputs: SKILL.md rows with ``history_role`` and ``repo_path``.
    Outputs: the same rows plus those three columns.

    Example:
    >>> import pandas as pd
    >>> frame = pd.DataFrame([{
    ...     "repo": "acme/app", "skill_folder": "skills/new",
    ...     "file_path": "SKILL.md", "history_role": "former_folder",
    ...     "status": "A", "repo_path": "skills/old/SKILL.md",
    ...     "old_repo_path": "", "commit_date": "2026-01-01T00:00:00Z",
    ...     "commit_seq": 1,
    ... }])
    >>> joined = join_renamed_folders(frame)
    >>> joined.loc[0, "lineage_id"]
    'acme/app::skills/new'
    >>> joined.loc[0, "former_paths"]
    ['skills/old']
    """
    frame = _without_join_columns(changes)
    if frame.empty:
        return _empty_joined(frame)
    _require_join_columns(frame)
    return _attach_lineage_keys(frame)


# Algorithm A20, see CODE_FLOW.md
def number_text_versions(changes: pd.DataFrame, texts: dict[str, str]) -> pd.DataFrame:
    """Number the texts of each lineage. A version is a new normalised text.

    Rows are ordered by commit date, then ``commit_seq``. The text is the
    ``new_sha`` blob. ``old_sha`` is not followed: a merge can name an
    older blob and that is not an error. ``version_seq`` starts at 1 and
    rises only when ``normalize_text`` changes the hash. A later commit
    that brings an older text back gets a new ``version_seq`` and
    ``same_as_seq`` points at the first version with that hash.
    ``release_date`` is the date of the first commit of that version.

    Inputs: ``changes`` — rows from ``join_renamed_folders``.
    ``texts`` — blob sha to raw text.
    Outputs: one row per text version. No ``language`` column yet.

    Example:
    >>> import pandas as pd
    >>> rows = pd.DataFrame([
    ...     _version_row("a", 1, "2026-01-01T00:00:00Z"),
    ...     _version_row("b", 2, "2026-01-02T00:00:00Z"),
    ... ])
    >>> texts = {"a": "Hello team.\\n", "b": "Hello team. \\n"}
    >>> list(number_text_versions(rows, texts)["version_seq"])
    [1]
    """
    _require(changes, ["lineage_id", "new_sha", "commit", "commit_date", "commit_seq"], "changes")
    rows = _own_skill_rows(changes)
    if rows.empty:
        return _empty_versions()
    ordered = _with_sort_time(rows)
    records: list[dict] = []
    grouped = ordered.groupby("lineage_id", sort=False)
    for lineage_id, group in grouped:
        records.extend(_versions_of(str(lineage_id), group, texts))
    return _versions_frame(records)


def mark_removed_lineages(changes: pd.DataFrame) -> pd.DataFrame:
    """Mark a lineage whose last own SKILL.md row is a delete.

    The rows are this folder's own SKILL.md plus former-folder SKILL.md,
    in commit order. A delete with no later add is ``registry_removed``.
    ``removed_at`` is that commit's date. A row that is only a copy source
    does not decide removal.

    Inputs: ``changes`` — rows from ``join_renamed_folders``.
    Outputs: ``lineage_id``, ``registry_removed``, ``removed_at``.
    One row per lineage that has an own SKILL.md row.
    """
    rows = _own_skill_rows(changes)
    if rows.empty:
        return _empty_removed()
    ordered = _with_sort_time(rows)
    last = ordered.groupby("lineage_id", sort=False).tail(1)
    flags = [_clean(status) == "D" for status in last["status"]]
    removed_at = [
        _clean(date) if flag else None
        for date, flag in zip(last["commit_date"], flags, strict=True)
    ]
    return pd.DataFrame(
        {
            "lineage_id": last["lineage_id"].to_numpy(),
            "registry_removed": flags,
            "removed_at": removed_at,
        }
    )


def label_lineage_language(versions: pd.DataFrame, texts: dict[str, str]) -> pd.DataFrame:
    """Add ``language`` to each version, using ``detect_language`` once per blob.

    The code is ``en``, ``unknown``, ``other``, or an ISO 639-1 code such
    as ``zh``. A lineage is English when its newest version is ``en`` or
    ``unknown``, the same rule as ``is_english``.

    Inputs: ``versions`` — rows from ``number_text_versions``.
    ``texts`` — blob sha to raw text.
    Outputs: the same rows plus ``language``.
    """
    labeled = versions.copy()
    if labeled.empty:
        labeled["language"] = pd.Series(dtype="object")
        return labeled
    cache: dict[str, str] = {}
    languages: list[str] = []
    for blob in labeled["blob"]:
        key = _clean(blob).lower()
        if key not in cache:
            cache[key] = detect_language(_lookup_text(texts, key) or "")
        languages.append(cache[key])
    labeled["language"] = languages
    return labeled


def compare_consecutive_versions(versions: pd.DataFrame, texts: dict[str, str]) -> pd.DataFrame:
    """Compare each version with the next one in the same lineage.

    ``compare_texts`` fills the body fields. ``compare_front_matter`` fills
    the header fields. ``release_date`` is the date of the newer version.
    A lineage with one version has no change row.

    Inputs: ``versions`` — version rows that should be kept.
    ``texts`` — blob sha to raw text.
    Outputs: one row per consecutive pair.
    """
    needed = ["lineage_id", "version_seq", "blob", "release_date"]
    _require(versions, needed, "versions")
    if versions.empty:
        return _empty_version_changes()
    ordered = versions.sort_values(["lineage_id", "version_seq"], kind="mergesort")
    records: list[dict] = []
    grouped = ordered.groupby("lineage_id", sort=False)
    for lineage_id, group in grouped:
        records.extend(_changes_for_lineage(str(lineage_id), group, texts))
    if not records:
        return _empty_version_changes()
    return pd.DataFrame(records).loc[:, CHANGE_COLUMNS]


# Algorithm A21, see CODE_FLOW.md
def rebuild_folder_manifests(folder_changes: pd.DataFrame) -> pd.DataFrame:
    """Replay each lineage's folder changes into one manifest per commit.

    Walk commits oldest first. ``A`` and ``M`` set ``path -> (sha, mode)``.
    ``D`` removes that path. ``R`` moves the old path to the new path.
    The root ``SKILL.md`` is not stored: it is the text version. A path
    inside ``SKIPPED_FOLDERS`` is ignored. ``copy_source`` rows are ignored.
    A file that never lived under this skill folder, or under a former
    name of it, is ignored. A commit that leaves no package files still
    has one row, and ``path`` is null, so the commit is not lost.

    Inputs: change rows with ``lineage_id`` or ``repo`` and ``skill_folder``,
    plus ``commit``, ``commit_date``, ``commit_seq``, ``status``,
    ``file_path``, ``new_sha`` and ``new_mode``.
    Outputs: one row per file per commit. ``size`` is null because the
    history table does not store blob size.

    Example:
    >>> import pandas as pd
    >>> rows = pd.DataFrame([
    ...     _package_change("c1", 1, "A", "SKILL.md", "", "s", "100644"),
    ...     _package_change("c2", 2, "A", "scripts/a.py", "", "p", "100644"),
    ... ])
    >>> found = rebuild_folder_manifests(rows)
    >>> list(found.loc[found["path"].notna(), "path"])
    ['scripts/a.py']
    """
    if folder_changes.empty:
        return _empty_commit_manifests()
    prepared = _prepare_folder_changes(folder_changes)
    if prepared.empty:
        return _empty_commit_manifests()
    records: list[dict] = []
    grouped = prepared.groupby("lineage_id", sort=False)
    for lineage_id, group in grouped:
        records.extend(_manifests_of_lineage(str(lineage_id), group))
    return _commit_manifests_frame(records)


# Algorithm A21, see CODE_FLOW.md
def number_package_versions(manifests: pd.DataFrame, versions: pd.DataFrame) -> pd.DataFrame:
    """Number a package version when the folder or the SKILL.md version changes.

    ``version_seq`` is the text version in force after that commit.
    ``changed_skill_md`` is true when this commit is the one that introduced
    that text version. ``changed_scripts`` and ``changed_other_files`` come
    from the manifest diff. ``file_category`` decides which flag. A commit
    that only repeats the same text, or only touches a skipped folder, is
    not a package version. ``package_version_seq`` starts at 1.

    Inputs: ``manifests`` from ``rebuild_folder_manifests``.
    ``versions`` — text versions with ``lineage_id``, ``version_seq`` and ``commit``.
    Outputs: one row per package version.

    Example:
    >>> import pandas as pd
    >>> rows = pd.DataFrame([
    ...     _package_change("c1", 1, "A", "SKILL.md", "", "s", "100644"),
    ...     _package_change("c2", 2, "A", "scripts/a.py", "", "p", "100644"),
    ... ])
    >>> manifests = rebuild_folder_manifests(rows)
    >>> versions = pd.DataFrame([{
    ...     "lineage_id": "acme/app::skills/a", "version_seq": 1,
    ...     "commit": "c1", "commit_date": "2026-01-01T00:00:00Z",
    ... }])
    >>> packages = number_package_versions(manifests, versions)
    >>> list(packages["package_version_seq"])
    [1, 2]
    >>> bool(packages.iloc[1]["changed_scripts"])
    True
    """
    if not versions.empty:
        _require(versions, ["lineage_id", "version_seq", "commit"], "versions")
    if manifests.empty and versions.empty:
        return _empty_packages()
    records: list[dict] = []
    for lineage_id in _package_lineage_ids(manifests, versions):
        manifest_group = _group_for(manifests, lineage_id)
        version_group = _group_for(versions, lineage_id)
        records.extend(_packages_of_lineage(lineage_id, manifest_group, version_group))
    return _packages_frame(records)


def main() -> None:
    """Build lineage tables and package versions from registry history.

    ``--dev`` reads and writes the dev data folder. ``--force`` rebuilds
    the six parquet files when they already exist. Without ``--force``,
    a second run prints the saved counts and does not rewrite them.

    Inputs: command-line flags and the stage 3 history tables.
    Outputs: the six parquet files. Nothing is returned.
    """
    args = build_arg_parser("Build one lineage per registry skill folder.").parse_args()
    paths = _output_paths(args.dev)
    # Step 1: Read SKILL.md changes
    if outputs_exist(paths) and not args.force:
        print("skip: lineages already exist (pass --force to build again)", flush=True)
        _print_saved(args.dev)
        return
    started = time.perf_counter()
    _check_inputs(args.dev)
    changes = read_skill_md_changes(args.dev)
    status = _read_status(args.dev)
    registries = _read_registry_names()
    # Step 2: Join renames and copies (algorithm A50)
    joined = join_renamed_folders(changes)
    # Step 4: Mark removed lineages
    removed = mark_removed_lineages(joined)
    # Steps 3, 5 and 6 run one batch of lineages at a time
    labeled, version_changes = _versions_in_batches(args.dev, joined, LINEAGE_TEXT_BATCH)
    kept, dropped, lineages = _finish_lineages(joined, labeled, removed)
    copied_from_not_kept = _copied_from_not_kept(lineages)
    # Step 7: Save and print the summary
    _save(args.dev, lineages, kept, version_changes, dropped)
    elapsed = time.perf_counter() - started
    _print_report(
        changes,
        joined,
        lineages,
        kept,
        version_changes,
        dropped,
        status,
        registries,
        elapsed,
        copied_from_not_kept,
    )
    # Step 8: Rebuild folder manifests (algorithm A21)
    folder_changes = _read_lineage_folder_changes(args.dev, lineages)
    print(f"Step 8: rebuild folder manifests from {len(folder_changes)} changes", flush=True)
    manifests = rebuild_folder_manifests(folder_changes)
    # Step 9: Number package versions
    print(f"Step 9: number package versions for {len(lineages)} lineages", flush=True)
    packages = number_package_versions(manifests, kept)
    # Step 10: Save package tables and print the summary
    stored = _manifests_for_packages(manifests, packages)
    _save_packages(args.dev, packages, stored)
    _print_package_report(lineages, packages, stored, time.perf_counter() - started)


def _package_change(
    commit: str,
    commit_seq: int,
    status: str,
    file_path: str,
    old_file_path: str,
    new_sha: str,
    new_mode: str,
) -> dict[str, object]:
    """One folder-change row for the package-version doctests."""
    return {
        "lineage_id": "acme/app::skills/a",
        "commit": commit,
        "commit_date": f"2026-01-0{commit_seq}T00:00:00Z",
        "commit_seq": commit_seq,
        "status": status,
        "file_path": file_path,
        "old_file_path": old_file_path,
        "new_sha": new_sha,
        "new_mode": new_mode,
        "history_role": "own",
    }


def _version_row(sha: str, commit_seq: int, commit_date: str) -> dict[str, object]:
    """One own SKILL.md row for the ``number_text_versions`` doctest."""
    return {
        "lineage_id": "acme/app::skills/a",
        "repo": "acme/app",
        "skill_folder": "skills/a",
        "file_path": SKILL_MD,
        "history_role": "own",
        "status": "M",
        "new_sha": sha,
        "commit": f"c{commit_seq}",
        "commit_date": commit_date,
        "commit_seq": commit_seq,
    }


def _without_join_columns(changes: pd.DataFrame) -> pd.DataFrame:
    """Drop lineage columns so a second call does not duplicate them."""
    frame = changes.copy()
    extra = [column for column in ("lineage_id", "former_paths", "copied_from") if column in frame]
    if extra:
        frame = frame.drop(columns=extra)
    return frame


def _empty_joined(frame: pd.DataFrame) -> pd.DataFrame:
    """An empty change table that already has the three lineage columns."""
    empty = frame.copy()
    for column in ("lineage_id", "former_paths", "copied_from"):
        empty[column] = pd.Series(dtype="object")
    return empty


def _require_join_columns(frame: pd.DataFrame) -> None:
    """The columns A50 needs. Raises ``KeyError`` when one is missing."""
    _require(frame, ["repo", "skill_folder", "file_path", "history_role"], "changes")
    _require(
        frame,
        ["status", "repo_path", "old_repo_path", "commit_date", "commit_seq"],
        "changes",
    )


def _attach_lineage_keys(frame: pd.DataFrame) -> pd.DataFrame:
    """Merge one identity row per folder. A missing copy source stays None."""
    keys = _lineage_key_table(frame)
    joined = frame.merge(keys, on=["repo", "skill_folder"], how="left")
    joined["former_paths"] = [_as_path_list(value) for value in joined["former_paths"]]
    joined["copied_from"] = _as_object(
        [_none_if_missing(value) for value in joined["copied_from"]],
        joined.index,
    )
    return joined


def _lineage_key_table(changes: pd.DataFrame) -> pd.DataFrame:
    """One identity row per skill folder: id, former paths, copy source."""
    ordered = _with_sort_time(changes)
    rows: list[dict] = []
    grouped = ordered.groupby(["repo", "skill_folder"], sort=False)
    for (repo, folder), group in grouped:
        folder_name = str(folder)
        rows.append(
            {
                "repo": repo,
                "skill_folder": folder,
                "lineage_id": f"{repo}::{folder_name}",
                "former_paths": _former_paths(group, folder_name),
                "copied_from": _copied_from_value(group, str(repo)),
            }
        )
    return pd.DataFrame(rows)


def _former_paths(group: pd.DataFrame, skill_folder: str) -> list[str]:
    """Older skill folders, first time each one appears in commit order."""
    seen: list[str] = []
    for row in group.itertuples(index=False):
        for folder in _folders_from_row(row, skill_folder):
            if folder not in seen:
                seen.append(folder)
    return seen


def _folders_from_row(row: object, skill_folder: str) -> list[str]:
    """Former skill folders named by this row. A prompt rename is not one."""
    found: list[str] = []
    role = _clean(row.history_role)
    if role == "former_folder" and _clean(row.file_path) == SKILL_MD:
        folder = _parent_folder(_clean(row.repo_path))
        if folder and folder != skill_folder:
            found.append(folder)
    old = _clean(row.old_repo_path)
    renamed = _clean(row.status) == "R" and role == "own" and _file_name(old) == SKILL_MD
    if renamed:
        folder = _parent_folder(old)
        if folder and folder != skill_folder and folder not in found:
            found.append(folder)
    return found


def _copied_from_value(group: pd.DataFrame, repo: str) -> str | None:
    """Lineage id of the newest copy-source SKILL.md before version 1."""
    cut = _version_cut(group)
    if cut is None:
        return None
    newest_path = ""
    for row in group.itertuples(index=False):
        if not _is_used_source(row, cut):
            continue
        newest_path = _clean(row.repo_path)
    folder = _parent_folder(newest_path)
    skill_folder = str(group.iloc[0]["skill_folder"])
    if not folder or folder == skill_folder:
        return None
    return f"{repo}::{folder}"


def _version_cut(group: pd.DataFrame) -> tuple[object, object] | None:
    """Sort key of the first own or former SKILL.md row. None when absent."""
    for row in group.itertuples(index=False):
        if _is_version_row(row):
            return (row.sort_time, row.commit_seq)
    return None


def _is_version_row(row: object) -> bool:
    """True for this folder's own SKILL.md, including a former folder name."""
    return _clean(row.file_path) == SKILL_MD and _clean(row.history_role) in OWN_ROLES


def _is_used_source(row: object, cut: tuple[object, object] | None) -> bool:
    """True when this copy-source SKILL.md sits strictly before version 1."""
    if cut is None:
        return False
    if _clean(row.history_role) != "copy_source" or _clean(row.file_path) != SKILL_MD:
        return False
    return (row.sort_time, row.commit_seq) < cut


def _versions_of(lineage_id: str, group: pd.DataFrame, texts: dict[str, str]) -> list[dict]:
    """Version rows for one lineage. The same normalised text is one version."""
    seen: dict[str, int] = {}
    previous = ""
    started = False
    seq = 0
    records: list[dict] = []
    for row in group.itertuples(index=False):
        sha = _clean(row.new_sha)
        text = _lookup_text(texts, sha)
        if text is None:
            continue
        digest = _normalized_sha(text)
        if started and digest == previous:
            continue
        seq += 1
        started = True
        previous = digest
        same = seen.get(digest)
        seen.setdefault(digest, seq)
        records.append(
            {
                "lineage_id": lineage_id,
                "version_seq": seq,
                "same_as_seq": same,
                "blob": sha,
                "normalized_sha": digest,
                "commit": _clean(row.commit),
                "commit_date": _clean(row.commit_date),
                "release_date": _clean(row.commit_date),
                "body_chars": len(text),
            }
        )
    return records


def _changes_for_lineage(lineage_id: str, group: pd.DataFrame, texts: dict[str, str]) -> list[dict]:
    """One change row for each step from version n to version n+1."""
    repo, folder = _split_lineage_id(lineage_id)
    path = f"{folder}/{SKILL_MD}" if folder else SKILL_MD
    rows = list(group.to_dict("records"))
    found: list[dict] = []
    for older, newer in zip(rows, rows[1:], strict=False):
        old_text = _lookup_text(texts, _clean(older["blob"])) or ""
        new_text = _lookup_text(texts, _clean(newer["blob"])) or ""
        change = compare_texts(old_text, new_text, repo=repo, path=path)
        matter = compare_front_matter(old_text, new_text)
        record = {
            "lineage_id": lineage_id,
            "from_seq": int(older["version_seq"]),
            "to_seq": int(newer["version_seq"]),
            "release_date": newer["release_date"],
        }
        record.update(change.to_dict())
        record.update(matter)
        found.append(record)
    return found


def _finish_lineages(
    joined: pd.DataFrame,
    labeled: pd.DataFrame,
    removed: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Drop empty and non-English lineages, then build the lineage table."""
    dropped = _dropped_lineages(joined, labeled)
    dropped_ids = set(dropped["lineage_id"])
    if labeled.empty:
        kept = labeled.copy()
    else:
        kept = labeled.loc[~labeled["lineage_id"].isin(dropped_ids)].copy()
    lineages = _lineage_frame(joined, kept, removed)
    return kept, dropped, lineages


def _dropped_lineages(joined: pd.DataFrame, labeled: pd.DataFrame) -> pd.DataFrame:
    """``no_text`` when nothing was stored, ``not_english`` when the newest is not."""
    if "lineage_id" not in joined.columns:
        return pd.DataFrame(columns=["lineage_id", "reason"])
    candidates = _own_skill_rows(joined)["lineage_id"].drop_duplicates()
    newest = _newest_language(labeled)
    rows: list[dict] = []
    for lineage_id in candidates:
        if lineage_id not in newest:
            rows.append({"lineage_id": lineage_id, "reason": "no_text"})
            continue
        if not _lineage_is_english(newest[lineage_id]):
            rows.append({"lineage_id": lineage_id, "reason": "not_english"})
    return pd.DataFrame(rows, columns=["lineage_id", "reason"])


def _newest_language(versions: pd.DataFrame) -> dict[str, str]:
    """Language code of the highest ``version_seq`` in each lineage."""
    if versions.empty or "language" not in versions.columns:
        return {}
    ordered = versions.sort_values(["lineage_id", "version_seq"], kind="mergesort")
    last = ordered.groupby("lineage_id", sort=False).tail(1)
    return dict(zip(last["lineage_id"], last["language"], strict=True))


def _lineage_frame(
    joined: pd.DataFrame,
    versions: pd.DataFrame,
    removed: pd.DataFrame,
) -> pd.DataFrame:
    """One summary row for each lineage that is still in ``versions``."""
    if versions.empty:
        return _empty_lineages()
    kept_ids = set(versions["lineage_id"])
    identity = joined.groupby("lineage_id", sort=False).head(1)
    identity = identity.loc[identity["lineage_id"].isin(kept_ids), JOIN_COLUMNS]
    dates = _lineage_dates(versions, removed)
    frame = identity.merge(dates, on="lineage_id", how="left")
    frame["former_paths"] = [_as_path_list(value) for value in frame["former_paths"]]
    frame["copied_from"] = _as_object(
        [_none_if_missing(value) for value in frame["copied_from"]],
        frame.index,
    )
    frame["is_english"] = frame["newest_language"].map(_lineage_is_english)
    frame["registry_removed"] = frame["registry_removed"].map(_is_true)
    frame["version_count"] = frame["version_count"].fillna(0).astype(int)
    return frame.loc[:, LINEAGE_COLUMNS].reset_index(drop=True)


def _lineage_dates(versions: pd.DataFrame, removed: pd.DataFrame) -> pd.DataFrame:
    """First date, last date, version count, newest language and the delete flag."""
    first = versions.loc[versions["version_seq"] == 1, ["lineage_id", "commit_date"]]
    first = first.rename(columns={"commit_date": "first_date"})
    ordered = versions.sort_values("version_seq", kind="mergesort")
    last = ordered.groupby("lineage_id", sort=False).tail(1)
    dates = last.loc[:, ["lineage_id", "commit_date", "language"]].rename(
        columns={"commit_date": "last_commit", "language": "newest_language"}
    )
    counts = versions.groupby("lineage_id").size().rename("version_count")
    dates = dates.merge(first, on="lineage_id", how="left")
    dates = dates.merge(counts, on="lineage_id", how="left")
    flags = removed.loc[:, ["lineage_id", "registry_removed", "removed_at"]]
    dates = dates.merge(flags, on="lineage_id", how="left")
    dates["last_date"] = [
        _later_stamp(commit, removed_at, flag)
        for commit, removed_at, flag in zip(
            dates["last_commit"],
            dates["removed_at"],
            dates["registry_removed"],
            strict=True,
        )
    ]
    return dates


def _later_stamp(commit_date: object, removed_at: object, removed_flag: object) -> str:
    """Use the delete date when the skill was removed after its last version."""
    if not _is_true(removed_flag) or not isinstance(removed_at, str) or removed_at == "":
        return str(commit_date)
    version_at = pd.to_datetime(commit_date, utc=True, errors="coerce")
    deleted_at = pd.to_datetime(removed_at, utc=True, errors="coerce")
    if pd.isna(deleted_at) or (not pd.isna(version_at) and deleted_at < version_at):
        return str(commit_date)
    return removed_at


def _lineage_is_english(language: object) -> bool:
    """True when the newest version counts as English.

    ``en`` and ``unknown`` match ``text_helpers.is_english``. Any other
    code is dropped from the lineage tables.

    Example:
    >>> _lineage_is_english("en")
    True
    >>> _lineage_is_english("unknown")
    True
    >>> _lineage_is_english("zh")
    False
    """
    return language in ENGLISH_CODES


def _outside_row_counts(changes: pd.DataFrame) -> dict[str, int]:
    """How many outside rows named a copy, and how many were left unused."""
    if changes.empty:
        return {"outside_rows_used": 0, "outside_rows_ignored": 0}
    ordered = _with_sort_time(changes)
    used = 0
    ignored = 0
    grouped = ordered.groupby(["repo", "skill_folder"], sort=False)
    for _, group in grouped:
        cut = _version_cut(group)
        for row in group.itertuples(index=False):
            if _is_version_row(row):
                continue
            if _is_used_source(row, cut):
                used += 1
            else:
                ignored += 1
    return {"outside_rows_used": used, "outside_rows_ignored": ignored}


def _role_counts(changes: pd.DataFrame) -> dict[str, int]:
    """Counts of ``history_role`` on SKILL.md rows, plus missing and other."""
    counts = {"own": 0, "former_folder": 0, "copy_source": 0, "other": 0, "missing": 0}
    if changes.empty or "history_role" not in changes.columns:
        counts["missing"] = len(changes)
        return counts
    text = changes["history_role"].fillna("").astype(str).str.strip()
    counts["missing"] = int((text == "").sum())
    for role, size in text.value_counts().items():
        if role == "":
            continue
        if role in KNOWN_ROLES:
            counts[role] = int(size)
        else:
            counts["other"] += int(size)
    return counts


def _normalized_sha(text: str) -> str:
    """SHA-1 hex of the normalised text. Whitespace-only edits share a hash.

    Example:
    >>> _normalized_sha("Hello \\n") == _normalized_sha("Hello\\n")
    True
    >>> _normalized_sha("Hello\\n") == _normalized_sha("Hello there\\n")
    False
    """
    normalized = normalize_text(text)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _parent_folder(path: str) -> str:
    """Folder that holds this repo path. A root file has an empty folder.

    Example:
    >>> _parent_folder("skills/old/SKILL.md")
    'skills/old'
    >>> _parent_folder("SKILL.md")
    ''
    """
    if "/" not in path:
        return ""
    return path.rsplit("/", 1)[0]


def _file_name(path: str) -> str:
    """The last path piece. An empty path stays empty."""
    if path == "":
        return ""
    return path.rsplit("/", 1)[-1]


def _lookup_text(texts: dict[str, str], sha: str) -> str | None:
    """Return the raw text for ``sha``, or None when the blob has no text."""
    if not _has_blob(sha):
        return None
    if sha in texts:
        return texts[sha]
    return texts.get(sha.lower())


def _has_blob(sha: str) -> bool:
    """False for a blank sha and for git's all-zero add/delete sha."""
    if sha == "" or sha.lower() in {"none", "nan", "<na>"}:
        return False
    return set(sha) != {"0"}


def _clean(value: object) -> str:
    """String value, or ``""`` when the cell is missing."""
    if isinstance(value, str):
        return value
    return ""


def _as_path_list(value: object) -> list[str]:
    """Copy a former-path cell into a plain list of strings."""
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _as_object(values: list[str | None], index: pd.Index) -> pd.Series:
    """An object series. A plain list would turn None into NaN."""
    return pd.Series(values, index=index, dtype="object")


def _none_if_missing(value: object) -> str | None:
    """Keep a real lineage id. None and NaN become None."""
    if isinstance(value, str) and value:
        return value
    return None


def _optional_int(value: object) -> int | None:
    """An int, or None when the cell is missing. NaN counts as missing."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return int(value)


def _is_true(value: object) -> bool:
    """True only for a boolean True. NaN, None and other types are False."""
    if isinstance(value, bool):
        return bool(value)
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        return False
    item = getattr(value, "item", None)
    if item is None:
        return False
    try:
        return bool(item())
    except (TypeError, ValueError):
        return False


def _split_lineage_id(lineage_id: str) -> tuple[str, str]:
    """Split ``owner/repo::folder`` into the repo and the folder."""
    repo, folder = lineage_id.split("::", 1)
    return repo, folder


def _with_sort_time(changes: pd.DataFrame) -> pd.DataFrame:
    """Copy ordered by commit date, then ``commit_seq``."""
    ordered = changes.copy()
    ordered["sort_time"] = pd.to_datetime(ordered["commit_date"], utc=True, errors="coerce")
    ordered = ordered.sort_values(["sort_time", "commit_seq"], kind="mergesort")
    return ordered.reset_index(drop=True)


def _own_skill_rows(changes: pd.DataFrame) -> pd.DataFrame:
    """Own and former-folder rows whose path is exactly ``SKILL.md``."""
    _require(changes, ["lineage_id", "file_path", "history_role"], "changes")
    role = changes["history_role"].isin(list(OWN_ROLES))
    skill = changes["file_path"] == SKILL_MD
    return changes.loc[role & skill].copy()


def _require(frame: pd.DataFrame, columns: list[str], label: str) -> None:
    """Raise ``ValueError`` when ``frame`` is missing any of ``columns``."""
    missing = [name for name in columns if name not in frame.columns]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"{label} needs columns: {joined}")


def _versions_in_batches(
    dev: bool,
    joined: pd.DataFrame,
    limit: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Steps 3, 5 and 6 for whole lineages, with one batch of texts in memory.

    Each batch reads only its own SKILL.md texts, numbers and labels its
    versions, and compares the versions of its English lineages. Every
    step works on one lineage at a time, so the result is the same as one
    big batch.

    Inputs: ``joined`` — rows from ``join_renamed_folders``.
    ``limit`` — about this many distinct blobs per batch.
    Outputs: labeled versions (every lineage) and version changes (kept lineages).
    """
    batches = _lineage_batches(joined, limit) or [joined]
    labeled_parts: list[pd.DataFrame] = []
    change_parts: list[pd.DataFrame] = []
    for number, batch in enumerate(batches, start=1):
        texts = _read_skill_texts(dev, batch)
        # Step 3: Number text versions (algorithm A20)
        versions = number_text_versions(batch, texts)
        # Step 5: Label language and drop non-English lineages
        print(
            f"Batch {number}/{len(batches)}: label language for {len(versions)} versions",
            flush=True,
        )
        labeled = label_lineage_language(versions, texts)
        dropped_ids = set(_dropped_lineages(batch, labeled)["lineage_id"])
        kept = labeled.loc[~labeled["lineage_id"].isin(dropped_ids)] if len(labeled) else labeled
        # Step 6: Compare consecutive versions
        change_parts.append(compare_consecutive_versions(kept, texts))
        labeled_parts.append(labeled)
        del texts
    return _concat_parts(labeled_parts), _concat_parts(change_parts)


def _lineage_batches(joined: pd.DataFrame, limit: int) -> list[pd.DataFrame]:
    """Split ``joined`` into whole lineages with about ``limit`` distinct blobs each.

    Lineages go in ``lineage_id`` order. One lineage larger than ``limit`` is
    its own batch. Rows without a lineage id are left out; no step uses them.
    """
    if joined.empty or "lineage_id" not in joined.columns:
        return []
    counts = joined.groupby("lineage_id")["new_sha"].nunique().sort_index()
    groups: list[list[str]] = []
    current: list[str] = []
    size = 0
    for lineage_id, count in counts.items():
        if current and size + int(count) > limit:
            groups.append(current)
            current, size = [], 0
        current.append(str(lineage_id))
        size += int(count)
    if current:
        groups.append(current)
    batch_of = {lineage_id: number for number, ids in enumerate(groups) for lineage_id in ids}
    numbers = joined["lineage_id"].map(batch_of)
    return [group for _, group in joined.groupby(numbers, sort=True)]


def _concat_parts(parts: list[pd.DataFrame]) -> pd.DataFrame:
    """Stack the batch results. Empty parts are skipped so column types stay."""
    filled = [part for part in parts if not part.empty]
    if not filled:
        return parts[0]
    return pd.concat(filled, ignore_index=True)


def _read_skill_texts(dev: bool, changes: pd.DataFrame) -> dict[str, str]:
    """Blob sha to raw text, only for shas that appear on these rows."""
    if changes.empty or "new_sha" not in changes.columns:
        return {}
    wanted = sorted({sha.lower() for sha in changes["new_sha"].map(_clean) if _has_blob(sha)})
    if not wanted:
        return {}
    path = _history_dir(dev) / "registry_history_texts.parquet"
    connection = connect_duckdb()
    try:
        frame = connection.execute(SKILL_TEXTS_SQL, [str(path), wanted]).fetchdf()
    finally:
        connection.close()
    found: dict[str, str] = {}
    for sha, text in zip(frame["sha"], frame["text"], strict=True):
        body = text if isinstance(text, str) else ""
        found[str(sha)] = body
        found[str(sha).lower()] = body
    return found


def _read_status(dev: bool) -> pd.DataFrame:
    """Stage 3 status rows. ``follow_renames`` is read from here."""
    return read_parquet(_history_dir(dev) / "registry_history_status.parquet")


def _read_registry_names() -> set[str]:
    """Repo names from stage 2. The file always lives in the full data folder."""
    frame = read_parquet(data_dir(False) / "registries.parquet")
    return set(frame["repo"].astype(str))


def _check_inputs(dev: bool) -> None:
    """Stop with the builder command when a stage 3 or stage 2 file is missing."""
    history = _history_dir(dev)
    flag = " --dev" if dev else ""
    command = f"python -m skill_genealogy.download_registry_history{flag}"
    check_inputs_exist(
        {
            "registry_history_changes": history / "registry_history_changes.parquet",
            "registry_history_texts": history / "registry_history_texts.parquet",
            "registry_history_status": history / "registry_history_status.parquet",
            "registries": data_dir(False) / "registries.parquet",
        },
        {
            "registry_history_changes": command,
            "registry_history_texts": command,
            "registry_history_status": command,
            "registries": "python -m skill_genealogy.select_registries",
        },
    )


def _history_dir(dev: bool) -> Path:
    """``registry_history`` under the dev or full data folder. It is not created."""
    return data_dir(dev) / "registry_history"


def _output_paths(dev: bool) -> list[Path]:
    """The six lineage and package parquet files for this mode."""
    folder = data_dir(dev)
    return [folder / name for name in OUTPUT_NAMES]


def _save(
    dev: bool,
    lineages: pd.DataFrame,
    versions: pd.DataFrame,
    version_changes: pd.DataFrame,
    dropped: pd.DataFrame,
) -> None:
    """Write the four tables, sorted, under ``data_dir(dev)``."""
    folder = data_dir(dev)
    write_parquet(lineages.loc[:, LINEAGE_COLUMNS], folder / OUTPUT_NAMES[0], ["lineage_id"])
    write_parquet(
        versions.loc[:, VERSION_COLUMNS],
        folder / OUTPUT_NAMES[1],
        ["lineage_id", "version_seq"],
    )
    write_parquet(
        version_changes.loc[:, CHANGE_COLUMNS],
        folder / OUTPUT_NAMES[2],
        ["lineage_id", "from_seq"],
    )
    write_parquet(
        dropped.loc[:, ["lineage_id", "reason"]],
        folder / OUTPUT_NAMES[3],
        ["lineage_id", "reason"],
    )


def _print_report(
    changes: pd.DataFrame,
    joined: pd.DataFrame,
    lineages: pd.DataFrame,
    versions: pd.DataFrame,
    version_changes: pd.DataFrame,
    dropped: pd.DataFrame,
    status: pd.DataFrame,
    registries: set[str],
    elapsed: float,
    copied_from_not_kept: int,
) -> None:
    """Print counts, the copy and rename notes, and two rows of each output."""
    outputs = _sorted_lineage_outputs(lineages, versions, version_changes, dropped)
    roles = _role_counts(changes)
    if roles["missing"]:
        print(f"warning: history_role missing on {roles['missing']} SKILL.md rows", flush=True)
    counts = _lineage_summary(
        changes, joined, *outputs, status, registries, elapsed, roles, copied_from_not_kept
    )
    print_summary("Registry lineages", counts)
    _print_lineage_samples(*outputs)


def _print_saved(dev: bool) -> None:
    """Print counts and two rows from the parquet files already on disk."""
    folder = data_dir(dev)
    lineages = read_parquet(folder / OUTPUT_NAMES[0])
    versions = read_parquet(folder / OUTPUT_NAMES[1])
    version_changes = read_parquet(folder / OUTPUT_NAMES[2])
    dropped = read_parquet(folder / OUTPUT_NAMES[3])
    packages = read_parquet(folder / OUTPUT_NAMES[4])
    stored = read_parquet(folder / OUTPUT_NAMES[5])
    reasons = _reason_counts(dropped)
    removed = 0 if lineages.empty else int(lineages["registry_removed"].map(_is_true).sum())
    print_summary(
        "Lineages (saved)",
        {
            "lineages": len(lineages),
            "versions": len(versions),
            "version_changes": len(version_changes),
            "removed": removed,
            "dropped_not_english": reasons.get("not_english", 0),
            "dropped_no_text": reasons.get("no_text", 0),
            "package_versions": len(packages),
            "manifest_files": len(stored),
        },
    )
    print_sample_records(lineages, "Sample lineages")
    print_sample_records(versions, "Sample lineage versions")
    print_sample_records(version_changes, "Sample lineage changes")
    print_sample_records(dropped, "Sample dropped lineages")
    print_sample_records(packages, "Sample package versions")
    print_sample_records(stored, "Sample folder manifests")


def _per_registry_counts(lineages: pd.DataFrame, status: pd.DataFrame) -> list[int]:
    """Kept lineages in each mined registry, including a registry with zero."""
    if lineages.empty:
        counts = pd.Series(dtype="int64")
    else:
        counts = lineages.groupby("repo").size()
    repos: list[str] = []
    if not status.empty and "repo" in status.columns:
        repos.extend(str(repo) for repo in status["repo"])
    for repo in counts.index.astype(str):
        if repo not in repos:
            repos.append(repo)
    return [int(counts.get(repo, 0)) for repo in repos]


def _version_counts(lineages: pd.DataFrame) -> list[int]:
    """How many text versions each kept lineage has."""
    if lineages.empty:
        return []
    return [int(value) for value in lineages["version_count"]]


def _copied_from_count(lineages: pd.DataFrame) -> int:
    """How many kept lineages name a source lineage."""
    if lineages.empty:
        return 0
    named = [_none_if_missing(value) for value in lineages["copied_from"]]
    return sum(value is not None for value in named)


def _copied_from_not_kept(lineages: pd.DataFrame) -> int:
    """How many kept lineages name a source that is not a kept lineage id.

    The text stays in ``copied_from``. A renamed source is stored under
    the HEAD folder, so the id from the copy is not in this table. A
    source dropped for language is in ``lineages_dropped``, not here.
    """
    if lineages.empty or "copied_from" not in lineages.columns:
        return 0
    kept = set(lineages["lineage_id"].astype(str))
    named = [_none_if_missing(value) for value in lineages["copied_from"]]
    return sum(value is not None and value not in kept for value in named)


def _sorted_lineage_outputs(
    lineages: pd.DataFrame,
    versions: pd.DataFrame,
    version_changes: pd.DataFrame,
    dropped: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """The same row order as the four parquet files."""
    return (
        lineages.sort_values("lineage_id", kind="mergesort"),
        versions.sort_values(["lineage_id", "version_seq"], kind="mergesort"),
        version_changes.sort_values(["lineage_id", "from_seq"], kind="mergesort"),
        dropped.sort_values(["lineage_id", "reason"], kind="mergesort"),
    )


def _lineage_summary(
    changes: pd.DataFrame,
    joined: pd.DataFrame,
    lineages: pd.DataFrame,
    versions: pd.DataFrame,
    version_changes: pd.DataFrame,
    dropped: pd.DataFrame,
    status: pd.DataFrame,
    registries: set[str],
    elapsed: float,
    roles: dict[str, int],
    copied_from_not_kept: int,
) -> dict[str, object]:
    """The counts printed after a build. Key order is the summary order."""
    outside = _outside_row_counts(joined)
    per_repo = _per_registry_counts(lineages, status)
    per_lineage = _version_counts(lineages)
    reasons = _reason_counts(dropped)
    false_n, false_share = _follow_false(status)
    missing = _repos_not_listed(changes, status, registries)
    removed = 0 if lineages.empty else int(lineages["registry_removed"].map(_is_true).sum())
    return {
        "seconds": round(elapsed, 1),
        "skill_md_rows": len(changes),
        "history_role_own": roles["own"],
        "history_role_former_folder": roles["former_folder"],
        "history_role_copy_source": roles["copy_source"],
        "history_role_other": roles["other"],
        "history_role_missing": roles["missing"],
        "lineages": len(lineages),
        "lineages_per_registry_median": _show_number(_median(per_repo)),
        "lineages_per_registry_max": _show_max(per_repo),
        "versions": len(versions),
        "versions_per_lineage_median": _show_number(_median(per_lineage)),
        "versions_per_lineage_max": _show_max(per_lineage),
        "version_changes": len(version_changes),
        "removed": removed,
        "dropped_not_english": reasons.get("not_english", 0),
        "dropped_no_text": reasons.get("no_text", 0),
        "copied_from": _copied_from_count(lineages),
        "copied_from_not_kept": copied_from_not_kept,
        "outside_rows_used": outside["outside_rows_used"],
        "outside_rows_ignored": outside["outside_rows_ignored"],
        "follow_renames_false": false_n,
        "follow_renames_false_share": round(false_share, 4),
        "history_repos_not_in_registries": len(missing),
    }


def _print_lineage_samples(
    lineages: pd.DataFrame,
    versions: pd.DataFrame,
    version_changes: pd.DataFrame,
    dropped: pd.DataFrame,
) -> None:
    """Two rows of each saved table."""
    print_sample_records(lineages, "Sample lineages")
    print_sample_records(versions, "Sample lineage versions")
    print_sample_records(version_changes, "Sample lineage changes")
    print_sample_records(dropped, "Sample dropped lineages")


def _reason_counts(dropped: pd.DataFrame) -> dict[str, int]:
    """How many dropped lineages have each reason."""
    if dropped.empty or "reason" not in dropped.columns:
        return {}
    return {str(reason): int(size) for reason, size in dropped["reason"].value_counts().items()}


def _follow_false(status: pd.DataFrame) -> tuple[int, float]:
    """How many mined registries had ``follow_renames`` off, and that share."""
    if status.empty or "follow_renames" not in status.columns:
        return 0, 0.0
    false_n = int(status["follow_renames"].eq(False).sum())
    return false_n, false_n / len(status)


def _repos_not_listed(
    changes: pd.DataFrame,
    status: pd.DataFrame,
    registries: set[str],
) -> list[str]:
    """History repos that are not in ``registries.parquet``."""
    seen: set[str] = set()
    if not changes.empty and "repo" in changes.columns:
        seen.update(changes["repo"].astype(str))
    if not status.empty and "repo" in status.columns:
        seen.update(status["repo"].astype(str))
    return sorted(seen - registries)


def _median(values: list[int]) -> float | None:
    """Median of ``values``, or None when the list is empty."""
    if not values:
        return None
    return float(statistics.median(values))


def _show_number(value: float | None) -> str:
    """Print a whole median as an integer and any other median as a decimal."""
    if value is None:
        return "n/a"
    if value == int(value):
        return str(int(value))
    return str(value)


def _show_max(values: list[int]) -> str:
    """The largest value, or ``n/a`` when there are none."""
    if not values:
        return "n/a"
    return str(max(values))


def _empty_versions() -> pd.DataFrame:
    """A version table with the right columns and no rows."""
    frame = pd.DataFrame(columns=VERSION_CORE)
    frame["same_as_seq"] = pd.Series(dtype="Int64")
    frame["version_seq"] = pd.Series(dtype="int64")
    frame["body_chars"] = pd.Series(dtype="int64")
    return frame


def _versions_frame(records: list[dict]) -> pd.DataFrame:
    """Build the version table. ``same_as_seq`` stays null when the text is new."""
    if not records:
        return _empty_versions()
    frame = pd.DataFrame(records, columns=VERSION_CORE)
    frame["same_as_seq"] = pd.array(
        [_optional_int(value) for value in frame["same_as_seq"]],
        dtype="Int64",
    )
    frame["version_seq"] = frame["version_seq"].astype("int64")
    frame["body_chars"] = frame["body_chars"].astype("int64")
    return frame


def _empty_removed() -> pd.DataFrame:
    """A removal table with the right columns and no rows."""
    return pd.DataFrame(
        {
            "lineage_id": pd.Series(dtype="object"),
            "registry_removed": pd.Series(dtype="bool"),
            "removed_at": pd.Series(dtype="object"),
        }
    )


def _empty_lineages() -> pd.DataFrame:
    """A lineage table with the right columns and no rows."""
    frame = pd.DataFrame(columns=LINEAGE_COLUMNS)
    frame["version_count"] = pd.Series(dtype="int64")
    frame["registry_removed"] = pd.Series(dtype="bool")
    frame["is_english"] = pd.Series(dtype="bool")
    return frame


def _empty_version_changes() -> pd.DataFrame:
    """A change table with every TextChange and front-matter column, and no rows."""
    return pd.DataFrame(columns=CHANGE_COLUMNS)


def _read_lineage_folder_changes(dev: bool, lineages: pd.DataFrame) -> pd.DataFrame:
    """Own and former-folder changes for the kept English lineages.

    Query Q4.3. ``copy_source`` rows stay out: they are the source skill's
    history, and that source has its own lineage. An empty lineage list
    reads nothing.

    Inputs: ``dev`` — which history folder. ``lineages`` — kept lineage ids.
    Outputs: the matching change rows. Raises nothing when the list is empty.
    """
    if lineages.empty or "lineage_id" not in lineages.columns:
        return _empty_folder_changes()
    lineage_ids = [str(item) for item in lineages["lineage_id"]]
    path = _history_dir(dev) / "registry_history_changes.parquet"
    connection = connect_duckdb()
    try:
        return connection.execute(LINEAGE_FOLDER_CHANGES_SQL, [str(path), lineage_ids]).fetchdf()
    finally:
        connection.close()


def _prepare_folder_changes(folder_changes: pd.DataFrame) -> pd.DataFrame:
    """Keep rows this lineage owns, and make sure ``lineage_id`` is set."""
    needed = ["commit", "commit_date", "commit_seq", "status", "file_path", "new_sha", "new_mode"]
    _require(folder_changes, needed, "folder_changes")
    prepared = _ensure_lineage_id(folder_changes)
    if "old_file_path" not in prepared.columns:
        prepared = prepared.copy()
        prepared["old_file_path"] = ""
    return _own_folder_rows(prepared)


def _ensure_lineage_id(frame: pd.DataFrame) -> pd.DataFrame:
    """Use ``lineage_id`` when every row has one. Otherwise build ``repo::folder``."""
    if "lineage_id" in frame.columns:
        present = [_none_if_missing(value) for value in frame["lineage_id"]]
        if all(item is not None for item in present):
            return frame
    _require(frame, ["repo", "skill_folder"], "folder_changes")
    copied = frame.copy()
    copied["lineage_id"] = [
        f"{_clean(repo)}::{_clean(folder)}"
        for repo, folder in zip(copied["repo"], copied["skill_folder"], strict=True)
    ]
    return copied


def _own_folder_rows(changes: pd.DataFrame) -> pd.DataFrame:
    """Drop ``copy_source`` rows. A blank role is kept, so a small test still runs."""
    if "history_role" not in changes.columns:
        return changes
    role = changes["history_role"].map(_clean)
    keep = role.isin(list(OWN_ROLES)) | role.eq("")
    return changes.loc[keep].copy()


def _manifests_of_lineage(lineage_id: str, group: pd.DataFrame) -> list[dict]:
    """Apply one lineage's commits and record the folder after each commit."""
    ordered = _ordered_folder_changes(group)
    state: dict[str, tuple[str, str]] = {}
    records: list[dict] = []
    for commit, rows in _iter_commits(ordered):
        _apply_change_rows(state, rows)
        first = rows[0]
        records.extend(
            _snapshot_rows(
                lineage_id,
                commit,
                _clean(first["commit_date"]),
                _commit_seq(first["commit_seq"]),
                state,
            )
        )
    return records


def _ordered_folder_changes(group: pd.DataFrame) -> pd.DataFrame:
    """Commit date, then ``commit_seq``, then delete / rename / add."""
    ordered = _with_sort_time(group)
    status = ordered["status"].map(_clean).str[:1]
    ordered["apply_rank"] = status.map(_APPLY_ORDER).fillna(5)
    return ordered.sort_values(
        ["sort_time", "commit_seq", "apply_rank", "file_path"],
        kind="mergesort",
    ).reset_index(drop=True)


def _iter_commits(ordered: pd.DataFrame):
    """Yield ``(commit, rows)`` in the order ``ordered`` already has."""
    current = ""
    bucket: list[dict] = []
    for row in ordered.to_dict("records"):
        commit = _clean(row["commit"])
        if bucket and commit != current:
            yield current, bucket
            bucket = []
        current = commit
        bucket.append(row)
    if bucket:
        yield current, bucket


def _apply_change_rows(state: dict[str, tuple[str, str]], rows: list[dict]) -> None:
    """Apply every file change in one commit to ``state``."""
    for row in rows:
        _apply_one_change(state, row)


def _apply_one_change(state: dict[str, tuple[str, str]], row: dict) -> None:
    """One ``A``, ``M``, ``D`` or ``R`` against the running path map.

    A rename that keeps the same skill-relative path (the folder was renamed)
    updates sha and mode in place. A copy (``C``) adds the new path and
    leaves the old path.
    """
    status = _clean(row.get("status"))[:1]
    new_path = _manifest_path(row, "new")
    old_path = _manifest_path(row, "old")
    if status == "D":
        path = new_path or old_path
        if path:
            state.pop(path, None)
        return
    if status == "R":
        if old_path and old_path != new_path:
            state.pop(old_path, None)
        if new_path:
            state[new_path] = (_clean(row.get("new_sha")), _clean(row.get("new_mode")))
        return
    if new_path and status in {"A", "M", "C", "T"}:
        state[new_path] = (_clean(row.get("new_sha")), _clean(row.get("new_mode")))


def _manifest_path(row: dict, side: str) -> str:
    """Skill-relative path to store, or ``""`` when this side is not a package file."""
    relative = _clean(row.get("file_path" if side == "new" else "old_file_path"))
    repo_path = _clean(row.get("repo_path" if side == "new" else "old_repo_path"))
    folder = _clean(row.get("skill_folder"))
    if not relative or not _path_in_this_skill(repo_path, relative, folder):
        return ""
    if _is_root_skill_md(relative) or is_skipped_path(relative):
        return ""
    return relative


def _path_in_this_skill(repo_path: str, relative: str, folder: str) -> bool:
    """True when the file sits in this skill folder or in a former name of it.

    A blank ``repo_path`` has no evidence the file is outside, so the row is
    kept. Stage 3 leaves ``file_path`` equal to ``repo_path`` when the file
    was never under a skill folder. Those rows are outside the package.
    """
    if not repo_path or folder in {"", "."}:
        return True
    if repo_path.startswith(folder.strip("/") + "/"):
        return True
    return bool(relative) and repo_path != relative and repo_path.endswith("/" + relative)


def _is_root_skill_md(path: str) -> bool:
    """True for ``SKILL.md`` at the skill-folder root, in any letter case."""
    return "/" not in path and path.lower() == "skill.md"


def _snapshot_rows(
    lineage_id: str,
    commit: str,
    commit_date: str,
    commit_seq: int,
    state: dict[str, tuple[str, str]],
) -> list[dict]:
    """One manifest row per file. An empty folder still records the commit."""
    base = {
        "lineage_id": lineage_id,
        "commit": commit,
        "commit_date": commit_date,
        "commit_seq": commit_seq,
        "size": None,
    }
    if not state:
        return [{**base, "path": None, "sha": None, "mode": None, "file_category": None}]
    rows = []
    for path in sorted(state):
        sha, mode = state[path]
        rows.append(
            {
                **base,
                "path": path,
                "sha": sha,
                "mode": mode,
                "file_category": file_category(path, mode),
            }
        )
    return rows


def _commit_manifests_frame(records: list[dict]) -> pd.DataFrame:
    """The per-commit manifest table. ``size`` stays a nullable integer."""
    if not records:
        return _empty_commit_manifests()
    frame = pd.DataFrame(records, columns=COMMIT_MANIFEST_COLUMNS)
    frame["commit_seq"] = frame["commit_seq"].astype("int64")
    frame["size"] = pd.array([None] * len(frame), dtype="Int64")
    return frame


def _empty_commit_manifests() -> pd.DataFrame:
    """A per-commit manifest table with the right columns and no rows."""
    frame = pd.DataFrame(columns=COMMIT_MANIFEST_COLUMNS)
    frame["commit_seq"] = pd.Series(dtype="int64")
    frame["size"] = pd.Series(dtype="Int64")
    return frame


def _empty_folder_changes() -> pd.DataFrame:
    """A change table with no rows, used when no lineage was kept."""
    return pd.DataFrame(columns=["lineage_id", "repo", "skill_folder", "history_role"])


def _package_lineage_ids(manifests: pd.DataFrame, versions: pd.DataFrame) -> list[str]:
    """Lineage ids that have a manifest commit or a text version, first-seen order."""
    found: list[str] = []
    for frame in (manifests, versions):
        if frame.empty or "lineage_id" not in frame.columns:
            continue
        for value in frame["lineage_id"]:
            lineage_id = _clean(value)
            if lineage_id and lineage_id not in found:
                found.append(lineage_id)
    return found


def _group_for(frame: pd.DataFrame, lineage_id: str) -> pd.DataFrame:
    """Rows of one lineage, or an empty frame when that lineage is absent."""
    if frame.empty or "lineage_id" not in frame.columns:
        return frame.iloc[0:0]
    return frame.loc[frame["lineage_id"].map(_clean) == lineage_id]


def _packages_of_lineage(
    lineage_id: str,
    manifest_group: pd.DataFrame,
    version_group: pd.DataFrame,
) -> list[dict]:
    """Package versions of one lineage, oldest commit first."""
    commits = _package_commits(manifest_group, version_group)
    if commits.empty:
        return []
    files = _files_by_commit(manifest_group)
    introduced = _versions_by_commit(version_group)
    state: dict[str, tuple[str, str]] = {}
    running: int | None = None
    sequence = 0
    records: list[dict] = []
    for row in commits.to_dict("records"):
        commit = _clean(row["commit"])
        after = files[commit] if commit in files else state
        new_version = introduced.get(commit)
        skill_changed = new_version is not None and new_version != running
        scripts_changed, others_changed = _folder_change_flags(state, after)
        if skill_changed or scripts_changed or others_changed:
            sequence += 1
            current = new_version if new_version is not None else running
            records.append(
                _package_record(
                    lineage_id,
                    sequence,
                    current,
                    commit,
                    _clean(row["commit_date"]),
                    skill_changed,
                    scripts_changed,
                    others_changed,
                    after,
                )
            )
        if new_version is not None:
            running = new_version
        state = after
    return records


def _package_commits(manifest_group: pd.DataFrame, version_group: pd.DataFrame) -> pd.DataFrame:
    """Every commit that can change the package, ordered by date then ``commit_seq``."""
    rows: list[dict] = []
    seen: set[str] = set()
    rows.extend(_commit_keys(manifest_group, seen))
    rows.extend(_commit_keys(version_group, seen))
    if not rows:
        return pd.DataFrame(columns=["commit", "commit_date", "commit_seq"])
    return _with_sort_time(pd.DataFrame(rows))


def _commit_keys(frame: pd.DataFrame, seen: set[str]) -> list[dict]:
    """One row per commit not already in ``seen``. ``seen`` is updated."""
    if frame.empty or "commit" not in frame.columns:
        return []
    rows: list[dict] = []
    for record in frame.to_dict("records"):
        commit = _clean(record.get("commit"))
        if not commit or commit in seen:
            continue
        seen.add(commit)
        rows.append(
            {
                "commit": commit,
                "commit_date": _clean(record.get("commit_date")),
                "commit_seq": _commit_seq(record.get("commit_seq")),
            }
        )
    return rows


def _files_by_commit(group: pd.DataFrame) -> dict[str, dict[str, tuple[str, str]]]:
    """Commit to the path map after that commit. A null path means an empty folder."""
    found: dict[str, dict[str, tuple[str, str]]] = {}
    if group.empty or "commit" not in group.columns:
        return found
    for record in group.to_dict("records"):
        commit = _clean(record.get("commit"))
        files = found.setdefault(commit, {})
        path = _none_if_missing(record.get("path"))
        if path is None:
            continue
        files[path] = (_clean(record.get("sha")), _clean(record.get("mode")))
    return found


def _versions_by_commit(versions: pd.DataFrame) -> dict[str, int]:
    """Commit to the text ``version_seq`` that commit introduced."""
    if versions.empty or "commit" not in versions.columns:
        return {}
    found: dict[str, int] = {}
    ordered = versions.sort_values("version_seq", kind="mergesort")
    for record in ordered.to_dict("records"):
        number = _optional_int(record.get("version_seq"))
        commit = _clean(record.get("commit"))
        if number is None or not commit:
            continue
        found[commit] = number
    return found


def _folder_change_flags(
    before: dict[str, tuple[str, str]],
    after: dict[str, tuple[str, str]],
) -> tuple[bool, bool]:
    """Whether any script changed, and whether any other package file changed."""
    scripts = False
    others = False
    for path in set(before) | set(after):
        if before.get(path) == after.get(path):
            continue
        stored = after.get(path) or before[path]
        if file_category(path, stored[1]) == "script":
            scripts = True
        else:
            others = True
    return scripts, others


def _package_record(
    lineage_id: str,
    sequence: int,
    version_seq: int | None,
    commit: str,
    commit_date: str,
    skill_changed: bool,
    scripts_changed: bool,
    others_changed: bool,
    files: dict[str, tuple[str, str]],
) -> dict:
    """One package-version row. ``version_seq`` is null when no text exists yet."""
    return {
        "lineage_id": lineage_id,
        "package_version_seq": sequence,
        "version_seq": version_seq,
        "commit": commit,
        "commit_date": commit_date,
        "changed_skill_md": skill_changed,
        "changed_scripts": scripts_changed,
        "changed_other_files": others_changed,
        "file_count": len(files),
        "script_count": _script_count(files),
    }


def _script_count(files: dict[str, tuple[str, str]]) -> int:
    """How many manifest files ``file_category`` calls a script."""
    return sum(file_category(path, mode) == "script" for path, (_, mode) in files.items())


def _packages_frame(records: list[dict]) -> pd.DataFrame:
    """The package-version table. ``version_seq`` stays nullable."""
    if not records:
        return _empty_packages()
    frame = pd.DataFrame(records, columns=PACKAGE_COLUMNS)
    frame["package_version_seq"] = frame["package_version_seq"].astype("int64")
    frame["version_seq"] = pd.array(
        [_optional_int(value) for value in frame["version_seq"]],
        dtype="Int64",
    )
    frame["file_count"] = frame["file_count"].astype("int64")
    frame["script_count"] = frame["script_count"].astype("int64")
    for name in ("changed_skill_md", "changed_scripts", "changed_other_files"):
        frame[name] = frame[name].astype(bool)
    return frame


def _empty_packages() -> pd.DataFrame:
    """A package-version table with the right columns and no rows."""
    frame = pd.DataFrame(columns=PACKAGE_COLUMNS)
    frame["package_version_seq"] = pd.Series(dtype="int64")
    frame["version_seq"] = pd.Series(dtype="Int64")
    frame["file_count"] = pd.Series(dtype="int64")
    frame["script_count"] = pd.Series(dtype="int64")
    frame["changed_skill_md"] = pd.Series(dtype="bool")
    frame["changed_scripts"] = pd.Series(dtype="bool")
    frame["changed_other_files"] = pd.Series(dtype="bool")
    return frame


def _manifests_for_packages(manifests: pd.DataFrame, packages: pd.DataFrame) -> pd.DataFrame:
    """Folder files at package versions only. Empty packages have no file rows."""
    if manifests.empty or packages.empty or "path" not in manifests.columns:
        return _empty_stored_manifests()
    files = manifests.loc[manifests["path"].map(_none_if_missing).notna()].copy()
    if files.empty:
        return _empty_stored_manifests()
    keys = packages.loc[:, ["lineage_id", "commit", "package_version_seq"]]
    merged = files.merge(keys, on=["lineage_id", "commit"], how="inner")
    merged["size"] = pd.array([None] * len(merged), dtype="Int64")
    return merged.loc[:, STORED_MANIFEST_COLUMNS]


def _empty_stored_manifests() -> pd.DataFrame:
    """A stored manifest table with the right columns and no rows."""
    frame = pd.DataFrame(columns=STORED_MANIFEST_COLUMNS)
    frame["package_version_seq"] = pd.Series(dtype="int64")
    frame["size"] = pd.Series(dtype="Int64")
    return frame


def _save_packages(dev: bool, packages: pd.DataFrame, stored: pd.DataFrame) -> None:
    """Write the two package tables under ``data_dir(dev)``."""
    folder = data_dir(dev)
    write_parquet(
        packages.loc[:, PACKAGE_COLUMNS],
        folder / OUTPUT_NAMES[4],
        ["lineage_id", "package_version_seq"],
    )
    write_parquet(
        stored.loc[:, STORED_MANIFEST_COLUMNS],
        folder / OUTPUT_NAMES[5],
        ["lineage_id", "package_version_seq", "path"],
    )


def _print_package_report(
    lineages: pd.DataFrame,
    packages: pd.DataFrame,
    stored: pd.DataFrame,
    elapsed: float,
) -> None:
    """Print package counts and two rows of each package table."""
    ordered_packages = packages
    ordered_stored = stored
    if not packages.empty:
        ordered_packages = packages.sort_values(
            ["lineage_id", "package_version_seq"], kind="mergesort"
        )
    if not stored.empty:
        ordered_stored = stored.sort_values(
            ["lineage_id", "package_version_seq", "path"], kind="mergesort"
        )
    print_summary("Package versions", _package_summary(lineages, packages, stored, elapsed))
    print_sample_records(ordered_packages, "Sample package versions")
    print_sample_records(ordered_stored, "Sample folder manifests")


def _package_summary(
    lineages: pd.DataFrame,
    packages: pd.DataFrame,
    stored: pd.DataFrame,
    elapsed: float,
) -> dict[str, object]:
    """Counts for step 10. Shares use the kept English lineages as the base."""
    lineage_count = 0 if lineages.empty else len(lineages)
    with_files, with_scripts = _lineages_with_files(stored)
    per_package = _counts_per_lineage(packages)
    per_text = _version_counts(lineages)
    ahead = _packages_ahead_of_text(lineages, packages)
    return {
        "seconds": round(elapsed, 1),
        "lineages": lineage_count,
        "lineages_with_folder_files": with_files,
        "lineages_with_folder_files_share": _share(with_files, lineage_count),
        "lineages_with_scripts": with_scripts,
        "lineages_with_scripts_share": _share(with_scripts, lineage_count),
        "package_versions": len(packages),
        "manifest_files": len(stored),
        "package_versions_per_lineage_median": _show_number(_median(per_package)),
        "package_versions_per_lineage_max": _show_max(per_package),
        "skill_md_versions_per_lineage_median": _show_number(_median(per_text)),
        "skill_md_versions_per_lineage_max": _show_max(per_text),
        "lineages_with_more_package_than_text_versions": ahead,
    }


def _lineages_with_files(stored: pd.DataFrame) -> tuple[int, int]:
    """How many lineages have any package file, and how many have a script."""
    if stored.empty or "lineage_id" not in stored.columns:
        return 0, 0
    with_files = stored["lineage_id"].nunique()
    scripts = stored.loc[stored["file_category"] == "script", "lineage_id"]
    return int(with_files), int(scripts.nunique())


def _counts_per_lineage(packages: pd.DataFrame) -> list[int]:
    """How many rows each lineage has. A lineage with no rows is left out."""
    if packages.empty or "lineage_id" not in packages.columns:
        return []
    return [int(size) for size in packages.groupby("lineage_id").size()]


def _packages_ahead_of_text(lineages: pd.DataFrame, packages: pd.DataFrame) -> int:
    """Lineages whose package-version count is higher than the text-version count."""
    if lineages.empty or packages.empty:
        return 0
    package_counts = packages.groupby("lineage_id").size()
    ahead = 0
    for lineage_id, version_count in zip(
        lineages["lineage_id"], lineages["version_count"], strict=True
    ):
        package_count = int(package_counts.get(lineage_id, 0))
        text_count = _optional_int(version_count) or 0
        if package_count > text_count:
            ahead += 1
    return ahead


def _share(part: int, whole: int) -> float:
    """``part / whole``, or 0 when ``whole`` is 0. Rounded to 4 decimal places."""
    if whole == 0:
        return 0.0
    return round(part / whole, 4)


def _commit_seq(value: object) -> int:
    """``commit_seq`` as an int. A missing value sorts as 0."""
    number = _optional_int(value)
    return 0 if number is None else number


if __name__ == "__main__":
    main()
