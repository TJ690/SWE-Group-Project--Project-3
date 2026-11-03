"""How a copy differs from the registry version it started from.

What: change state, customisation, front matter, heading overlap, and
whether a later edit synced, came from another lineage, or was local.
Stage 9b scores every copy version against four texts picked by date.
Stage 9c compares each copy version's folder with those same four targets
and flags scripts whose risk patterns are new.
Inputs: copy files with an origin, lineage versions, changes, package
versions and folder manifests, copy histories, copied-skill folder
changes, artifact siblings, and the text stores.
Outputs: ``copy_changes.parquet``, ``copy_history_edits.parquet``,
``change_profile.parquet``, ``copy_version_similarity.parquet``,
``copy_folder_changes.parquet`` and ``copy_version_folder_summary.parquet``
under ``data_dir(dev)``.
Run: ``python -m skill_genealogy.measure_copy_changes --dev --only origin``,
``--only versions`` or ``--only folders``.
"""

from __future__ import annotations

import difflib
import json
import math
import re
import shutil
import time
import warnings
from dataclasses import fields
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import skill_genealogy.settings as settings
from skill_genealogy.assign_families import _assemble_copy_histories
from skill_genealogy.build_registry_lineages import (
    _APPLY_ORDER,
    _apply_change_rows,
    _clean,
    _commit_seq,
    _iter_commits,
    _prepare_folder_changes,
)
from skill_genealogy.compare_texts import (
    TextChange,
    _fence_state,
    check_script_mentions,
    compare_folders,
    compare_front_matter,
    compare_texts,
    find_headings,
    find_network_refs,
    find_project_names_added,
    find_script_mentions,
    flag_script_risks,
    is_skipped_path,
    new_risk_flags,
    share_of_registry_lines_kept,
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
from skill_genealogy.settings import data_dir
from skill_genealogy.text_helpers import (
    body_lines,
    exact_containment,
    exact_jaccard,
    make_word_shingles,
    normalize_text,
    remove_boilerplate_lines,
)

# How many copy files are scored before their texts are dropped. Each batch
# reads every text store once, so the full data needs few, large batches:
# one pass over the artifact files takes about 8 seconds. 20,000 files keep
# one batch of skill texts well under 1 GB. It is not a research cutoff.
_VERSION_FILE_BATCH = 20_000
_SCORE_NAMES = (
    "jaccard",
    "containment",
    "verified",
    "lines_added",
    "lines_removed",
    "body_change_pct",
)
_NOTE_KIND_ORDER = {
    "heading": 0,
    "name": 1,
    "script": 2,
    "command": 3,
    "network": 4,
    "permission": 5,
}
_BOILERPLATE_CACHE: set[str] | None = None
_ZERO_BLOB = "0" * 40
_STATE_NAMES = ("unchanged", "cosmetic", "substantive")
_STATE_RANK = {"unchanged": 0, "cosmetic": 1, "substantive": 2}
_PROFILE_ORDER = (
    "registry_update",
    "copy_local_edit",
    "copy_sync",
    "copy_vs_origin",
)

TEXT_FIELDS = [item.name for item in fields(TextChange)]
FRONT_FIELDS = [
    "name_changed",
    "description_changed",
    "fm_keys_added",
    "fm_keys_removed",
    "allowed_tools_added",
    "allowed_tools_removed",
    "permissions_changed",
]
COPY_CHANGE_COLUMNS = [
    "repo",
    "path",
    "lineage_id",
    "link",
    "origin_seq",
    "current_basis",
    *TEXT_FIELDS,
    *FRONT_FIELDS,
    "customized",
    "heading_overlap",
    "current_sha",
    "origin_sha",
]
HISTORY_COLUMNS = [
    "repo",
    "path",
    "lineage_id",
    "from_blob",
    "to_blob",
    "commit",
    "commit_date",
    "change_kind",
    "inherited",
    *TEXT_FIELDS,
    "permissions_changed",
]
PROFILE_COLUMNS = [
    "population",
    "changes",
    "share_substantive",
    "share_headings_added",
    "share_commands_added",
    "share_network_added",
    "share_scripts_added",
    "share_permissions_changed",
]
SIMILARITY_COLUMNS = [
    "repo",
    "path",
    "lineage_id",
    "version_seq",
    "commit_date",
    "blob",
    "change_kind",
    *[f"prev_{name}" for name in _SCORE_NAMES],
    *[f"origin_{name}" for name in _SCORE_NAMES],
    "at_date_seq",
    *[f"at_date_{name}" for name in _SCORE_NAMES],
    "newest_seq",
    *[f"newest_{name}" for name in _SCORE_NAMES],
    "change_notes",
]
_VERSION_LIST_COLUMNS = [
    "repo",
    "path",
    "lineage_id",
    "origin_seq",
    "version_seq",
    "commit_date",
    "blob",
]
_FOLDER_TARGETS = ("prev", "origin", "at_date", "newest")
_TARGET_RANK = {name: index for index, name in enumerate(_FOLDER_TARGETS)}
_CHANGED_RESULTS = ("added", "removed", "modified", "renamed")
COPY_MANIFEST_COLUMNS = [
    "repo",
    "path",
    "version_seq",
    "file_path",
    "sha",
    "mode",
    "size",
    "source",
]
FOLDER_CHANGE_COLUMNS = [
    "repo",
    "path",
    "version_seq",
    "target",
    "file_path",
    "old_file_path",
    "result",
    "old_sha",
    "new_sha",
    "mode_changed",
    "category",
    "size_change",
    "risk_flags",
    "new_risk_flags",
]
# The stored types of copy_folder_changes.parquet. Batches are written with
# these types, so a column that is empty in one batch keeps its type.
FOLDER_CHANGE_SCHEMA = pa.schema(
    [
        ("repo", pa.large_string()),
        ("path", pa.large_string()),
        ("version_seq", pa.int64()),
        ("target", pa.large_string()),
        ("file_path", pa.large_string()),
        ("old_file_path", pa.large_string()),
        ("result", pa.large_string()),
        ("old_sha", pa.large_string()),
        ("new_sha", pa.large_string()),
        ("mode_changed", pa.bool_()),
        ("category", pa.large_string()),
        ("size_change", pa.int64()),
        ("risk_flags", pa.list_(pa.string())),
        ("new_risk_flags", pa.list_(pa.string())),
    ]
)
FOLDER_SUMMARY_COLUMNS = [
    "repo",
    "path",
    "version_seq",
    "target",
    "files_added",
    "files_removed",
    "files_modified",
    "scripts_added",
    "scripts_modified",
    "new_risky_scripts",
    "missing_scripts",
    "hidden_scripts",
    "folder_source",
]

# Query Q9.1, see QUERIES.md
ELIGIBLE_COPIES_SQL = """
SELECT
    copies.repo,
    copies.path,
    copies.file_sha,
    copies.lineage_id,
    copies.link,
    copies.origin_seq
FROM read_parquet({copies}) AS copies
JOIN read_parquet({language}) AS language
  ON copies.file_sha = language.file_sha
ANTI JOIN (
    SELECT file_sha
    FROM read_parquet({families})
    WHERE registry_self
) AS registry_only
  ON copies.file_sha = registry_only.file_sha
WHERE copies.origin_seq IS NOT NULL
  AND NOT coalesce(copies.copied_before_release, false)
  AND language.is_english
"""

# Query Q9.2, see QUERIES.md
COPIED_HISTORY_SQL = """
SELECT
    copied.repo,
    copied.skill_folder,
    copied.file_path,
    copied.commit,
    copied.commit_date,
    copied.new_sha,
    copied.commit_seq,
    copied.status
FROM read_parquet({path}) AS copied
JOIN origin_copies
  ON copied.repo = origin_copies.repo
 AND (
    CASE
        WHEN copied.skill_folder IS NULL OR copied.skill_folder IN ('', '.')
            THEN copied.file_path
        ELSE copied.skill_folder || '/' || copied.file_path
    END
 ) = origin_copies.path
WHERE copied.is_skill_md
  AND coalesce(copied.status, '') <> 'D'
  AND copied.new_sha IS NOT NULL
  AND copied.new_sha <> ''
  AND lower(copied.new_sha) <> repeat('0', 40)
"""

# Query Q9.3, see QUERIES.md
WILD_HISTORY_SQL = """
SELECT
    wild.repo,
    wild.target_path,
    wild.commit,
    wild.commit_at,
    wild.blob,
    wild.version_seq,
    wild.status
FROM read_parquet({path}) AS wild
JOIN origin_copies
  ON wild.repo = origin_copies.repo
 AND wild.target_path = origin_copies.path
WHERE wild.blob IS NOT NULL
  AND wild.blob <> ''
  AND lower(wild.blob) <> repeat('0', 40)
  AND coalesce(wild.status, '') <> 'D'
"""

# Query Q9.4, see QUERIES.md
REGISTRY_COMMITS_SQL = """
SELECT DISTINCT lower(commit) AS commit
FROM read_parquet({path})
WHERE commit IS NOT NULL
  AND commit <> ''
"""

# Query Q9.5, see QUERIES.md
REGISTRY_TEXTS_SQL = """
SELECT sha AS blob, any_value(text) AS text
FROM read_parquet({path})
WHERE sha IN ({sha_list})
GROUP BY sha
"""

# Query Q9.6, see QUERIES.md
COPIED_TEXTS_SQL = """
SELECT sha AS blob, any_value(text) AS text
FROM read_parquet({path})
WHERE sha IN ({sha_list})
GROUP BY sha
"""

# Query Q9.7, see QUERIES.md
WILD_TEXTS_SQL = """
SELECT blob, any_value(content) AS text
FROM read_parquet({path})
WHERE blob IN ({sha_list})
GROUP BY blob
"""

# Query Q9.8, see QUERIES.md
DATASET_TEXTS_SQL = """
SELECT file_sha AS blob, any_value(content) AS text
FROM read_parquet({path})
WHERE file_sha IN ({sha_list})
  AND content IS NOT NULL
GROUP BY file_sha
"""

# Query Q9.9, see QUERIES.md
# Dates only. content is not selected, so the scan does not read skill text.
LAST_COMMIT_SQL = """
SELECT
    artifacts.repo_full_name AS repo,
    artifacts.path AS path,
    any_value(artifacts.last_commit_at) AS last_commit_at
FROM read_parquet({path}) AS artifacts
JOIN undated_copies
  ON artifacts.repo_full_name = undated_copies.repo
 AND artifacts.path = undated_copies.path
GROUP BY artifacts.repo_full_name, artifacts.path
"""

# Query Q9.10, see QUERIES.md
# Own and former-folder rows only. copy_source is the source skill's history.
FOLDER_HISTORY_SQL = """
SELECT
    changes.repo,
    changes.skill_folder,
    changes.file_path,
    changes.commit,
    changes.commit_date,
    changes.commit_seq,
    changes.status,
    changes.old_file_path,
    changes.new_sha,
    changes.new_mode,
    changes.history_role,
    changes.repo_path,
    changes.old_repo_path
FROM read_parquet({path}) AS changes
JOIN copy_folders
  ON changes.repo = copy_folders.repo
 AND changes.skill_folder = copy_folders.skill_folder
WHERE changes.history_role IN ('own', 'former_folder')
"""

# Query Q9.11, see QUERIES.md
# entry_sha is the git blob id. content is not selected.
SIBLING_FILES_SQL = """
SELECT
    siblings.repo_full_name AS repo,
    siblings.artifact_path AS path,
    siblings.entry_name,
    siblings.entry_sha,
    siblings.entry_size
FROM read_parquet({path}) AS siblings
JOIN snapshot_copies
  ON siblings.repo_full_name = snapshot_copies.repo
 AND siblings.artifact_path = snapshot_copies.path
WHERE siblings.entry_type = 'file'
"""

# Query Q9.12, see QUERIES.md
# Scores and change notes are not selected.
FOLDER_VERSIONS_SQL = """
SELECT
    versions.repo,
    versions.path,
    versions.lineage_id,
    versions.version_seq,
    versions.commit_date,
    versions.blob,
    copies.origin_seq
FROM read_parquet({versions}) AS versions
JOIN read_parquet({copies}) AS copies
  ON versions.repo = copies.repo
 AND versions.path = copies.path
"""

# Query Q9.13, see QUERIES.md
# Registry package files of the lineages in one batch, in file order. The
# file is sorted by lineage, so the scan skips the other row groups.
BATCH_MANIFESTS_SQL = """
SELECT * EXCLUDE (file_row_number)
FROM read_parquet({path}, file_row_number = true)
WHERE lineage_id IN (SELECT lineage_id FROM batch_lineages)
ORDER BY file_row_number
"""

# Query Q9.14, see QUERIES.md
# Every batch part of copy_folder_changes.parquet, in the stored order.
FOLDER_MERGE_SQL = """
SELECT * EXCLUDE (row_order)
FROM read_parquet({parts})
ORDER BY
    repo NULLS LAST,
    path NULLS LAST,
    version_seq NULLS LAST,
    target NULLS LAST,
    file_path NULLS LAST,
    row_order
"""

# Query Q9.15, see QUERIES.md
# The saved file is printed from three small reads, not loaded whole.
SAVED_ORIGIN_ROWS_SQL = """
SELECT rows.*
FROM read_parquet({path}) AS rows
JOIN latest_versions USING (repo, path, version_seq)
WHERE rows.target = 'origin'
"""
SAVED_HEAD_ROWS_SQL = """
SELECT *
FROM read_parquet({path}, file_row_number = true)
WHERE file_row_number < 2
ORDER BY file_row_number
"""
SAVED_RISKY_ROWS_SQL = """
SELECT *
FROM read_parquet({path}, file_row_number = true)
WHERE len(new_risk_flags) > 0
ORDER BY file_row_number
LIMIT 20
"""


def current_text_of_copy(
    copy_file: dict,
    histories,
    texts: dict[str, str],
) -> tuple[str, str]:
    """Return the copy's newest text and whether it came from history.

    The newest history row is the latest real commit date, then the
    highest ``version_seq``. An undated row does not beat a dated one.
    When that blob has no stored text, the dataset ``file_sha`` is used.
    The basis is ``latest_history`` or ``dataset``.

    Example:
    >>> copy = {"repo": "a/r", "path": "skills/a/SKILL.md", "file_sha": "dataset"}
    >>> hist = pd.DataFrame([
    ...     {"repo": "a/r", "path": "skills/a/SKILL.md", "blob": "new",
    ...      "commit_date": "2026-06-01T00:00:00Z", "version_seq": 2},
    ... ])
    >>> current_text_of_copy(copy, hist, {"new": "Now", "dataset": "Old"})
    ('Now', 'latest_history')
    >>> current_text_of_copy(copy, pd.DataFrame(), {"dataset": "Old"})
    ('Old', 'dataset')
    """
    rows = _history_rows(histories, copy_file.get("repo"), copy_file.get("path"))
    newest = _newest_history_row(rows)
    if newest is not None:
        stored = _lookup_text(texts, newest.get("blob"))
        if stored is not None:
            return stored, "latest_history"
    dataset = _lookup_text(texts, copy_file.get("file_sha"))
    if dataset is None:
        return "", "dataset"
    return dataset, "dataset"


def compare_copy_with_origin(
    copy_files: pd.DataFrame,
    histories,
    texts: dict[str, str],
    registry_versions: pd.DataFrame,
    registry_changes: pd.DataFrame,
) -> pd.DataFrame:
    """Compare each copy's current text with the origin version.

    Uses ``compare_texts`` and ``compare_front_matter``. ``customized`` is
    true when a project name, script mention, step, command or heading was
    added. ``heading_overlap`` is algorithm A38. ``current_sha`` is the blob
    whose text was compared, or empty when that text was missing.

    Inputs: origin copies, history rows, sha-to-text, lineage versions and
    lineage changes.
    Outputs: one row per copy. Columns are ``COPY_CHANGE_COLUMNS``.
    """
    if copy_files.empty:
        return _empty_frame(COPY_CHANGE_COLUMNS)
    grouped = _rows_by_file(_as_frame(histories))
    blobs = _version_blobs(registry_versions)
    later = _later_heading_index(registry_changes)
    text_map = _text_map(texts)
    body_cache: dict[tuple[str, str], dict] = {}
    name_cache: dict[tuple[str, str, str, str], dict] = {}
    front_cache: dict[tuple[str, str], dict] = {}
    records = copy_files.to_dict("records")
    rows = []
    total = len(records)
    for index, record in enumerate(records, start=1):
        rows.append(
            _one_copy_row(
                record, grouped, text_map, blobs, later, body_cache, name_cache, front_cache
            )
        )
        if total >= 5000 and index % 5000 == 0:
            print(f"compared {index} of {total}", flush=True)
    frame = pd.DataFrame(rows)
    return frame.reindex(columns=COPY_CHANGE_COLUMNS)


# Algorithm A38, see CODE_FLOW.md
def heading_overlap_with_registry(
    copy_headings_added: list[str],
    registry_headings_added_later: list[str],
) -> float:
    """Jaccard of headings the copy added and headings the registry added later.

    The score is the size of the shared headings divided by the size of
    the union. A heading is counted once. Both lists empty returns NaN,
    which the table stores as null. One side empty returns 0.

    The G19 card called this A37. A37 is already the family-tree parent step.

    Example:
    >>> heading_overlap_with_registry(["Deploy"], ["Deploy", "Tests"])
    0.5
    >>> heading_overlap_with_registry(["Deploy"], [])
    0.0
    >>> math.isnan(heading_overlap_with_registry([], []))
    True
    """
    copy_set = set(copy_headings_added)
    registry_set = set(registry_headings_added_later)
    if not copy_set and not registry_set:
        return float("nan")
    shared = copy_set & registry_set
    union = copy_set | registry_set
    return len(shared) / len(union)


# Algorithm A39, see CODE_FLOW.md
def classify_history_edits(
    histories: pd.DataFrame,
    registry_versions: pd.DataFrame,
) -> pd.DataFrame:
    """Label each non-inherited step sync, other_source or local.

    Inherited rows are dropped, then each file is paired in commit order.
    A consecutive pair with the same blob sha is skipped.
    A pair is also skipped when either blob has no stored text.
    The new blob is ``sync`` when it is a version of this copy's lineage,
    ``other_source`` when it is a version of a different lineage, and
    ``local`` otherwise. Each pair also stores a ``TextChange`` and
    ``permissions_changed`` from the two texts on the history rows.

    The G19 card called this A38. A38 is heading overlap.

    Example:
    >>> versions = pd.DataFrame({"lineage_id": ["L"], "blob": ["v2"]})
    >>> rows = pd.DataFrame([
    ...     {"repo": "a/r", "path": "p", "lineage_id": "L", "blob": "v1",
    ...      "text": "One", "commit": "c1", "commit_date": "2026-01-01T00:00:00Z",
    ...      "version_seq": 1, "inherited": False},
    ...     {"repo": "a/r", "path": "p", "lineage_id": "L", "blob": "local",
    ...      "text": "One\\nTwo", "commit": "c2", "commit_date": "2026-02-01T00:00:00Z",
    ...      "version_seq": 2, "inherited": False},
    ... ])
    >>> classify_history_edits(rows, versions)["change_kind"].iloc[0]
    'local'
    """
    if histories.empty:
        return _edits_frame([], 0)
    by_blob = _lineages_by_blob(registry_versions)
    body_cache: dict[tuple[str, str], dict] = {}
    name_cache: dict[tuple[str, str, str, str], dict] = {}
    front_cache: dict[tuple[str, str], dict] = {}
    edited: list[dict] = []
    skipped_missing_text = 0
    for file_rows in _rows_by_file(histories).values():
        kept = [row for row in file_rows if not _is_true(row.get("inherited"))]
        kept.sort(key=_history_sort_key)
        pairs = zip(kept, kept[1:], strict=False)
        for previous, current in pairs:
            if _sha_key(previous.get("blob")) == _sha_key(current.get("blob")):
                continue
            if not _has_stored_text(previous) or not _has_stored_text(current):
                skipped_missing_text += 1
                continue
            edited.append(
                _one_edit(previous, current, by_blob, body_cache, name_cache, front_cache)
            )
    return _edits_frame(edited, skipped_missing_text)


def build_change_profile(
    registry_changes,
    history_edits,
    copy_changes,
) -> pd.DataFrame:
    """Share of the same edit measures on four populations.

    Populations, in order: registry version-to-version updates, local
    copy edits, sync edits, and each copy against its origin.
    ``other_source`` edits are not a population. An empty population has
    ``changes`` 0 and null shares. A missing permissions column is a null
    share.

    Example:
    >>> registry = pd.DataFrame([{
    ...     "state": "substantive", "headings_added": ["# A"], "commands_added": [],
    ...     "network_refs_added": [], "scripts_mentioned_added": [],
    ...     "permissions_changed": False,
    ... }])
    >>> profile = build_change_profile(registry, pd.DataFrame(), pd.DataFrame())
    >>> int(profile.loc[profile["population"].eq("registry_update"), "changes"].iloc[0])
    1
    """
    edits = _as_frame(history_edits)
    frames = {
        "registry_update": _as_frame(registry_changes),
        "copy_local_edit": _edits_of_kind(edits, "local"),
        "copy_sync": _edits_of_kind(edits, "sync"),
        "copy_vs_origin": _as_frame(copy_changes),
    }
    rows = [_profile_row(name, frames[name]) for name in _PROFILE_ORDER]
    return pd.DataFrame(rows).reindex(columns=PROFILE_COLUMNS)


def list_copy_versions(copy_files, histories, dataset_texts) -> pd.DataFrame:
    """One row per copy version, in date order, numbered from 1.

    A file with history rows uses those rows (wild or copied). The same
    commit and blob is kept once. A file with no history rows gets one row:
    the dataset ``file_sha``, dated by ``last_commit_at`` from
    ``dataset_texts`` or from the copy row. ``dataset_texts`` is a table
    with ``repo``, ``path`` and ``last_commit_at``. It is not the text body.

    Example:
    >>> copies = pd.DataFrame([{
    ...     "repo": "acme/app", "path": "skills/a/SKILL.md", "file_sha": "sha",
    ...     "lineage_id": "reg::a", "origin_seq": 1,
    ... }])
    >>> dates = pd.DataFrame([{
    ...     "repo": "acme/app", "path": "skills/a/SKILL.md",
    ...     "last_commit_at": "2026-04-01T00:00:00Z",
    ... }])
    >>> rows = list_copy_versions(copies, pd.DataFrame(), dates)
    >>> int(rows.iloc[0]["version_seq"]), rows.iloc[0]["blob"]
    (1, 'sha')
    """
    copies = _as_frame(copy_files)
    if copies.empty:
        return _empty_frame(_VERSION_LIST_COLUMNS)
    if "repo" in copies.columns and "path" in copies.columns:
        copies = copies.drop_duplicates(["repo", "path"], keep="first")
    grouped = _rows_by_file(_as_frame(histories))
    dates = _dataset_dates(dataset_texts, copies)
    rows: list[dict] = []
    for record in copies.to_dict("records"):
        key = (str(record.get("repo") or ""), str(record.get("path") or ""))
        file_rows = grouped.get(key, [])
        if file_rows:
            rows.extend(_numbered_history(record, file_rows))
        else:
            rows.append(_dataset_version_row(record, dates.get(key)))
    if not rows:
        return _empty_frame(_VERSION_LIST_COLUMNS)
    return pd.DataFrame(rows).reindex(columns=_VERSION_LIST_COLUMNS)


# Algorithm A40, see CODE_FLOW.md
def pick_comparison_targets(
    copy_version: dict,
    lineage_versions: pd.DataFrame | dict,
    origin_seq: int,
) -> dict:
    """Pick previous, origin, at-date and newest version numbers.

    ``lineage_versions`` is the version table, or ``target_index`` of it.

    Previous is this copy's own earlier version. Version 1 has none.
    Origin is ``origin_seq``, the same for every version of this copy.
    At-date is the newest lineage version whose ``release_date`` is on or
    before ``commit_date``. Newest is the highest ``version_seq`` overall.
    A copy dated before every release has no at-date version.

    ```text
    registry:  v1 (Jan 10) ------ v2 (Mar 01) ------------- v3 (May 15)
    copy:            c1 (Feb 01) ---- c2 (Mar 10) ---- c3 (Apr 20) ---- c4 (Jun 30)
    For c3: previous = c2, origin = v1, at-date = v2, newest = v3.
    ```

    Example:
    >>> versions = pd.DataFrame([
    ...     {"lineage_id": "reg::skill", "version_seq": 1, "release_date": "2026-01-10"},
    ...     {"lineage_id": "reg::skill", "version_seq": 2, "release_date": "2026-03-01"},
    ...     {"lineage_id": "reg::skill", "version_seq": 3, "release_date": "2026-05-15"},
    ... ])
    >>> c1 = {"version_seq": 1, "commit_date": "2026-02-01", "lineage_id": "reg::skill"}
    >>> pick_comparison_targets(c1, versions, 1)
    {'prev_seq': None, 'origin_seq': 1, 'at_date_seq': 1, 'newest_seq': 3}
    >>> c2 = {"version_seq": 2, "commit_date": "2026-03-10", "lineage_id": "reg::skill"}
    >>> pick_comparison_targets(c2, versions, 1)
    {'prev_seq': 1, 'origin_seq': 1, 'at_date_seq': 2, 'newest_seq': 3}
    >>> c3 = {"version_seq": 3, "commit_date": "2026-04-20", "lineage_id": "reg::skill"}
    >>> pick_comparison_targets(c3, versions, 1)
    {'prev_seq': 2, 'origin_seq': 1, 'at_date_seq': 2, 'newest_seq': 3}
    >>> c4 = {"version_seq": 4, "commit_date": "2026-06-30", "lineage_id": "reg::skill"}
    >>> pick_comparison_targets(c4, versions, 1)
    {'prev_seq': 3, 'origin_seq': 1, 'at_date_seq': 3, 'newest_seq': 3}
    >>> early = {"version_seq": 1, "commit_date": "2026-01-01", "lineage_id": "reg::skill"}
    >>> pick_comparison_targets(early, versions, 1)["at_date_seq"] is None
    True
    """
    sequence = _as_int(copy_version.get("version_seq")) or 0
    previous = sequence - 1 if sequence > 1 else None
    if isinstance(lineage_versions, dict):
        lineage = copy_version.get("lineage_id")
        key = None if _is_missing(lineage) or str(lineage) == "" else str(lineage)
        newest, releases = lineage_versions.get(key, (None, []))
    else:
        frame = _lineage_rows(copy_version, lineage_versions)
        newest, releases = _newest_seq(frame), _release_pairs(frame)
    origin = _as_int(origin_seq)
    return {
        "prev_seq": previous,
        "origin_seq": origin,
        "at_date_seq": _at_date_from_pairs(releases, copy_version.get("commit_date")),
        "newest_seq": newest,
    }


def target_index(
    lineage_versions: pd.DataFrame,
) -> dict[str | None, tuple[int | None, list[tuple[pd.Timestamp, int]]]]:
    """Newest version and dated releases of every lineage, for the target picker.

    ``pick_comparison_targets`` takes this in place of the version table.
    It gives the same targets without scanning every lineage version for
    each copy version. The key None holds the whole table, which is what a
    copy with no lineage id is compared against.

    Example:
    >>> versions = pd.DataFrame([
    ...     {"lineage_id": "reg::skill", "version_seq": 1, "release_date": "2026-01-10"},
    ...     {"lineage_id": "reg::skill", "version_seq": 2, "release_date": "2026-03-01"},
    ... ])
    >>> c1 = {"version_seq": 1, "commit_date": "2026-02-01", "lineage_id": "reg::skill"}
    >>> pick_comparison_targets(c1, target_index(versions), 1)
    {'prev_seq': None, 'origin_seq': 1, 'at_date_seq': 1, 'newest_seq': 2}
    """
    frame = _as_frame(lineage_versions)
    index: dict[str | None, tuple[int | None, list[tuple[pd.Timestamp, int]]]] = {
        None: (_newest_seq(frame), _release_pairs(frame))
    }
    if frame.empty or "lineage_id" not in frame.columns:
        return index
    for lineage, rows in frame.groupby(frame["lineage_id"].astype(str), sort=False):
        index[str(lineage)] = (_newest_seq(rows), _release_pairs(rows))
    return index


def score_pair(old_text: str, new_text: str) -> dict:
    """Score the new text against the target text.

    Jaccard and containment use exact 5-word shingles after the same
    boilerplate removal as stage 5. Containment asks how much of the target
    (the first text) is still in the new text. ``verified`` is the share of
    target body lines kept. Line counts come from ``compare_texts``.

    Example:
    >>> scores = score_pair(
    ...     "zebra quartz violin marble lantern",
    ...     "zebra quartz violin marble lantern",
    ... )
    >>> (scores["jaccard"], scores["containment"], scores["verified"])
    (1.0, 1.0, 1.0)
    >>> (scores["lines_added"], scores["lines_removed"], scores["body_change_pct"])
    (0, 0, 0.0)
    """
    old_shingles = _shingle_set(old_text)
    new_shingles = _shingle_set(new_text)
    change = compare_texts(old_text, new_text)
    return {
        "jaccard": exact_jaccard(old_shingles, new_shingles),
        "containment": exact_containment(old_shingles, new_shingles),
        "verified": share_of_registry_lines_kept(old_text, new_text),
        "lines_added": change.lines_added,
        "lines_removed": change.lines_removed,
        "body_change_pct": change.body_change_pct,
    }


# Algorithm A41, see CODE_FLOW.md
def change_notes_for_step(old_text: str, new_text: str, repo: str, path: str) -> list[dict]:
    """Notes for lines this version adds, with 1-based numbers in the new text.

    A note is an added line that is a heading, a project name, a script
    mention, a command, or a network reference. A network note starts with
    ``⚠``. A front-matter permission change is a note even when that line
    was edited rather than inserted. A heading inside a code fence is not
    a heading. The same line can produce more than one note.

    Example:
    >>> notes = change_notes_for_step(
    ...     "Keep this line.",
    ...     "Keep this line.\\n## Deploy\\ncurl https://x",
    ...     "acme/app",
    ...     "skills/a/SKILL.md",
    ... )
    >>> [(item["line"], item["kind"]) for item in notes]
    [(2, 'heading'), (3, 'network')]
    >>> notes[1]["text"]
    '⚠ curl https://x'
    >>> change_notes_for_step(
    ...     "Keep", "Keep\\n```\\n## Deploy\\n```", "acme/app", "skills/a/SKILL.md"
    ... )
    []
    """
    names = find_project_names_added(old_text, new_text, repo, path)
    added = _added_line_numbers(old_text, new_text)
    notes = _notes_on_added_lines(new_text, added, names)
    notes.extend(_permission_notes(old_text, new_text))
    notes.sort(key=lambda note: (note["line"], _NOTE_KIND_ORDER[note["kind"]]))
    return notes


def build_copy_version_similarity(dev: bool) -> pd.DataFrame:
    """Score every copy version against the four date targets.

    Inputs: ``dev`` — True reads the dev folder.
    Outputs: one row per copy version. The caller writes the parquet file.
    """
    temp_dir = data_dir(dev) / "_copy_versions_tmp"
    _remove_tree(temp_dir)
    connection = connect_duckdb()
    try:
        _prepare_duckdb(connection, temp_dir)
        # Step 1: Read copies with an origin, and lineage versions.
        print("Step 1: read origin copies and lineage versions", flush=True)
        tables = read_inputs(dev)
        copies = read_origin_copies(connection, dev)
        versions = tables["versions"]
        print(
            f"origin copies {len(copies)} lineage versions {len(versions)}",
            flush=True,
        )
        # Step 2: Read copy histories and dataset dates for files with none.
        print("Step 2: copy histories and dataset dates", flush=True)
        histories = _read_histories(connection, copies, dev)
        dates = _last_commit_dates(connection, copies, histories, dev)
        print(f"history rows {len(histories)} files without history {len(dates)}", flush=True)
        # Step 3: One row per history version, or one dataset row.
        print("Step 3: list copy versions", flush=True)
        listed = list_copy_versions(copies, histories, dates)
        print(f"copy versions {len(listed)}", flush=True)
        # Step 4: Load texts per batch and score the four targets (A40, A41).
        print("Step 4: score versions in batches", flush=True)
        return _score_in_batches(connection, listed, versions, dev)
    finally:
        connection.close()
        _remove_tree(temp_dir)


def copy_folder_manifests(dev: bool) -> pd.DataFrame:
    """One folder file per copy version, from history or the dataset snapshot.

    Copies whose skill folder has copied-skill changes are replayed (A42).
    Copies with no such changes get ``artifact_siblings`` rows, source
    ``dataset_snapshot``. A version whose folder is empty still has one
    row, and ``file_path`` is null, so the source is not lost.

    Inputs: ``dev`` — True reads the dev folder.
    Outputs: ``repo, path, version_seq, file_path, sha, mode, size, source``.
    """
    temp_dir = data_dir(dev) / "_copy_folders_tmp"
    _remove_tree(temp_dir)
    connection = connect_duckdb()
    try:
        _prepare_duckdb(connection, temp_dir)
        versions = _read_folder_versions(connection, dev)
        changes = _read_folder_history(connection, versions, dev)
        siblings = _read_sibling_files(connection, versions, changes, dev)
        return assemble_copy_folder_manifests(versions, changes, siblings)
    finally:
        connection.close()
        _remove_tree(temp_dir)


# Algorithm A42, see CODE_FLOW.md
def assemble_copy_folder_manifests(
    versions: pd.DataFrame,
    folder_changes: pd.DataFrame,
    siblings: pd.DataFrame,
) -> pd.DataFrame:
    """Replay each copy folder and keep the snapshot at each version.

    The walk is the same one package manifests use (A21): delete, then
    rename, then add or modify. The root ``SKILL.md`` is not a package
    file. A version's snapshot is the folder after the commit that wrote
    its blob, and before the next version's commit. The last version also
    keeps later folder commits, so a script edit that did not change
    ``SKILL.md`` stays in the current folder. No folder changes means the
    sibling files, on every version of that copy.

    Example:
    >>> import pandas as pd
    >>> versions = pd.DataFrame([{
    ...     "repo": "acme/app", "path": "skills/a/SKILL.md", "version_seq": 1,
    ...     "commit_date": "2026-02-01T00:00:00Z", "blob": "s1",
    ... }])
    >>> changes = pd.DataFrame([
    ...     _folder_change("c1", 1, "A", "SKILL.md", "s1"),
    ...     _folder_change("c1", 1, "A", "scripts/a.py", "p1"),
    ... ])
    >>> found = assemble_copy_folder_manifests(versions, changes, pd.DataFrame())
    >>> (found.iloc[0]["file_path"], found.iloc[0]["sha"], found.iloc[0]["source"])
    ('scripts/a.py', 'p1', 'history')
    >>> siblings = pd.DataFrame([{
    ...     "repo": "acme/app", "path": "skills/a/SKILL.md",
    ...     "entry_name": "scripts/a.py", "entry_sha": "abc", "entry_size": 4,
    ... }])
    >>> shot = assemble_copy_folder_manifests(versions, pd.DataFrame(), siblings)
    >>> (shot.iloc[0]["sha"], shot.iloc[0]["source"], int(shot.iloc[0]["size"]))
    ('abc', 'dataset_snapshot', 4)
    """
    version_frame = _as_frame(versions)
    if version_frame.empty:
        return _empty_copy_manifests()
    prepared = _with_skill_folder(version_frame)
    changes = _as_frame(folder_changes)
    snapshots, anchors = _history_snapshots(changes)
    sibling_files = _sibling_index(_as_frame(siblings))
    records = _manifest_records(prepared, snapshots, anchors, sibling_files)
    return _copy_manifest_frame(records)


# Algorithm A43, see CODE_FLOW.md
def registry_manifest_for(
    lineage_id: str,
    release_date,
    package_versions: pd.DataFrame,
    manifests: pd.DataFrame,
) -> list[dict]:
    """Return the package manifest current at ``release_date``.

    The current package version is the newest one whose ``commit_date``
    is on or before ``release_date``. The same timestamp keeps the higher
    ``package_version_seq``. A missing date means the last package version.
    A date before every package version returns an empty list. Each dict
    has ``path``, ``sha``, ``mode`` and ``size``.

    Example:
    >>> import pandas as pd
    >>> packages = pd.DataFrame([
    ...     {"lineage_id": "reg::a", "package_version_seq": 1, "commit_date": "2026-01-10"},
    ...     {"lineage_id": "reg::a", "package_version_seq": 2, "commit_date": "2026-03-01"},
    ...     {"lineage_id": "reg::a", "package_version_seq": 3, "commit_date": "2026-05-15"},
    ... ])
    >>> manifests = pd.DataFrame([
    ...     {"lineage_id": "reg::a", "package_version_seq": seq, "path": "scripts/a.py",
    ...      "sha": sha, "mode": "100644", "size": None}
    ...     for seq, sha in ((1, "v1"), (2, "v2"), (3, "v3"))
    ... ])
    >>> registry_manifest_for("reg::a", "2026-04-20", packages, manifests)[0]["sha"]
    'v2'
    >>> registry_manifest_for("reg::a", "2025-01-01", packages, manifests)
    []
    >>> registry_manifest_for("reg::a", None, packages, manifests)[0]["sha"]
    'v3'
    """
    missing_date = _is_missing(release_date)
    sequence = _package_seq_at(
        lineage_id, release_date, package_versions, newest=missing_date
    )
    if sequence is None:
        return []
    return _files_for_package(lineage_id, sequence, manifests)


def compare_copy_folders(
    copy_versions: pd.DataFrame,
    copy_manifests: pd.DataFrame,
    package_versions: pd.DataFrame,
    registry_manifests: pd.DataFrame,
    lineage_versions: pd.DataFrame | dict,
    texts: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Diff each copy version's folder against the four targets.

    ``prev`` uses the copy's previous manifest. The other three use the
    registry package current at that target's date (A43): origin is the
    origin text version's release, at-date is the copy version's commit
    date, and newest is the last package version. Added or modified
    scripts with stored text get ``flag_script_risks`` and
    ``new_risk_flags``. A script with no text keeps both flags null.
    ``missing_scripts`` and ``hidden_scripts`` come from the copy's own
    ``SKILL.md`` and its manifest, and are repeated on every file row.

    Example:
    >>> import pandas as pd
    >>> versions = pd.DataFrame([{
    ...     "repo": "acme/app", "path": "skills/a/SKILL.md", "lineage_id": "reg::a",
    ...     "version_seq": 1, "commit_date": "2026-04-20", "blob": "skill", "origin_seq": 1,
    ... }])
    >>> manifests = pd.DataFrame([{
    ...     "repo": "acme/app", "path": "skills/a/SKILL.md", "version_seq": 1,
    ...     "file_path": "scripts/run.sh", "sha": "n", "mode": "100644", "size": 3,
    ...     "source": "history",
    ... }])
    >>> packages = pd.DataFrame([{
    ...     "lineage_id": "reg::a", "package_version_seq": 1, "commit_date": "2026-01-10",
    ... }])
    >>> registry = pd.DataFrame(
    ...     columns=["lineage_id", "package_version_seq", "path", "sha", "mode", "size"]
    ... )
    >>> releases = pd.DataFrame(
    ...     [{"lineage_id": "reg::a", "version_seq": 1, "release_date": "2026-01-10"}]
    ... )
    >>> rows = compare_copy_folders(versions, manifests, packages, registry, releases, {})
    >>> rows.loc[rows["target"] == "origin", "result"].iloc[0]
    'added'
    """
    version_frame = _as_frame(copy_versions)
    if version_frame.empty:
        return _empty_folder_changes()
    indexed = _index_copy_manifests(_as_frame(copy_manifests))
    releases = _as_release_index(lineage_versions)
    packages = _as_package_index(package_versions)
    registry_files = _as_registry_index(registry_manifests)
    records: list[dict] = []
    grouped = version_frame.groupby(["repo", "path"], sort=False)
    for (_, _), group in grouped:
        ordered = group.sort_values("version_seq", kind="mergesort")
        records.extend(
            _diffs_for_file(
                ordered.to_dict("records"),
                indexed,
                packages,
                registry_files,
                releases,
                texts,
            )
        )
    return _folder_change_frame(records)


def summarize_folder_changes(folder_changes: pd.DataFrame) -> pd.DataFrame:
    """Count added, removed and modified files for each version and target.

    Script counts use ``category == script``. ``new_risky_scripts`` is how
    many of those added or modified scripts have a non-empty
    ``new_risk_flags`` list. Mention lists and ``folder_source`` are copied
    from the file rows when those columns are present.

    Example:
    >>> import pandas as pd
    >>> rows = pd.DataFrame([{
    ...     "repo": "acme/app", "path": "skills/a/SKILL.md", "version_seq": 1,
    ...     "target": "origin", "result": "added", "category": "script",
    ...     "new_risk_flags": ["network"], "folder_source": "history",
    ... }])
    >>> summary = summarize_folder_changes(rows)
    >>> int(summary.iloc[0]["scripts_added"]), int(summary.iloc[0]["new_risky_scripts"])
    (1, 1)
    """
    frame = _as_frame(folder_changes)
    if frame.empty or "target" not in frame.columns:
        return _empty_folder_summary()
    records = [
        _one_folder_summary(key, group)
        for key, group in frame.groupby(["repo", "path", "version_seq", "target"], sort=False)
    ]
    return _folder_summary_frame(records)


def read_origin_copies(
    connection: duckdb.DuckDBPyConnection,
    dev: bool,
) -> pd.DataFrame:
    """Return English copy files that have an origin and are not registry-only.

    Query Q9.1. ``copied_before_release`` rows are left out. A text that
    stage 7 marked ``registry_self`` is left out.

    Inputs: an open connection and ``dev``.
    Outputs: ``repo, path, file_sha, lineage_id, link, origin_seq``.
    """
    paths = _input_paths(dev)
    query = ELIGIBLE_COPIES_SQL.format(
        copies=_quote_path(str(paths["copy_files"])),
        families=_quote_path(str(paths["families"])),
        language=_quote_path(str(paths["skill_language"])),
    )
    frame = _fetch(connection, query)
    return _clean_copies(frame)


def read_inputs(dev: bool) -> dict[str, pd.DataFrame]:
    """Check stage 9a inputs and read versions and registry changes.

    Origin copies are loaded by ``read_origin_copies`` on the caller's
    connection. A missing file raises ``FileNotFoundError`` and names the
    command that builds it.

    Inputs: ``dev`` — True reads the dev folder.
    Outputs: ``versions`` and ``changes``.
    """
    paths = _input_paths(dev)
    check_inputs_exist(paths, _input_builders(dev))
    versions = read_parquet(paths["lineage_versions"])
    changes = read_parquet(paths["lineage_changes"])
    return {"versions": versions, "changes": changes}


def save_outputs(
    changes: pd.DataFrame,
    edits: pd.DataFrame,
    profile: pd.DataFrame,
    dev: bool,
) -> tuple[Path, Path, Path]:
    """Write the three stage 9a tables.

    Inputs: copy-vs-origin rows, history edits, the profile, and ``dev``.
    Outputs: the three parquet paths under ``data_dir(dev)``.
    """
    folder = data_dir(dev)
    change_path = folder / "copy_changes.parquet"
    edit_path = folder / "copy_history_edits.parquet"
    profile_path = folder / "change_profile.parquet"
    write_parquet(changes.reindex(columns=COPY_CHANGE_COLUMNS), change_path, ["repo", "path"])
    write_parquet(
        edits.reindex(columns=HISTORY_COLUMNS),
        edit_path,
        ["repo", "path", "commit_date", "commit"],
    )
    write_parquet(profile.reindex(columns=PROFILE_COLUMNS), profile_path)
    return change_path, edit_path, profile_path


def main() -> None:
    """Run the stage 9 part named by ``--only``.

    ``--dev`` uses the dev folder. ``--force`` rebuilds that part's outputs.
    The default is ``origin`` (stage 9a). ``versions`` is stage 9b.
    ``folders`` is stage 9c and writes the two folder tables. A second run
    of a part that already has its files prints ``skip``.
    """
    parser = build_arg_parser(
        "Compare copies with registry versions, score each version, and diff folders."
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=("origin", "versions", "folders"),
        help="origin is stage 9a; versions is stage 9b; folders is stage 9c",
    )
    args = parser.parse_args()
    parts = set(args.only) if args.only else {"origin"}
    dev = bool(args.dev)
    force = bool(args.force)
    if "origin" in parts:
        _run_origin(dev, force)
    if "versions" in parts:
        _run_versions(dev, force)
    if "folders" in parts:
        _run_folders(dev, force)


def _run_origin(dev: bool, force: bool) -> None:
    """Compare each copy with its origin and write the three stage 9a tables."""
    if force:
        for path in _output_paths(dev):
            if path.is_file():
                path.unlink()
    if outputs_exist(_output_paths(dev)):
        print("skip: copy change outputs already exist", flush=True)
        _print_saved_summary(dev)
        return
    started = time.perf_counter()
    temp_dir = data_dir(dev) / "_copy_changes_tmp"
    _remove_tree(temp_dir)
    connection = connect_duckdb()
    try:
        _prepare_duckdb(connection, temp_dir)
        # Step 1: Read copies with an origin, lineage versions, and lineage changes.
        print("Step 1: read origin copies, versions, registry changes", flush=True)
        tables = read_inputs(dev)
        copies = read_origin_copies(connection, dev)
        versions = tables["versions"]
        registry_changes = tables["changes"]
        print(
            f"origin copies {len(copies)} versions {len(versions)} "
            f"registry changes {len(registry_changes)}",
            flush=True,
        )
        # Step 2: Read copy histories for those files (queries Q9.2–Q9.4).
        print("Step 2: copy histories", flush=True)
        histories = _read_histories(connection, copies, dev)
        print(f"history rows {len(histories)}", flush=True)
        # Steps 3–5 run per batch of copy files, so only one batch of texts is held.
        # Step 3: Load origin texts and current texts (queries Q9.5–Q9.8).
        # Step 4: Compare each copy with its origin (algorithm A38).
        # Step 5: Classify history edits (algorithm A39).
        print("Steps 3-5: load texts, compare with origin, classify edits", flush=True)
        changes, edits = _compare_origin_batches(
            connection, copies, histories, versions, registry_changes, dev
        )
    finally:
        connection.close()
        _remove_tree(temp_dir)
    print(f"copy changes {len(changes)} history edits {len(edits)}", flush=True)
    # Step 6: Build the change profile.
    print("Step 6: change profile", flush=True)
    profile = build_change_profile(registry_changes, edits, changes)
    # Step 7: Save and print counts and two sample records.
    print("Step 7: save", flush=True)
    save_outputs(changes, edits, profile, dev)
    _print_run_summary(changes, edits, profile, time.perf_counter() - started)


def _compare_origin_batches(
    connection: duckdb.DuckDBPyConnection,
    copies: pd.DataFrame,
    histories: pd.DataFrame,
    versions: pd.DataFrame,
    registry_changes: pd.DataFrame,
    dev: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stage 9a steps 3–5 for batches of copy files.

    Every comparison and every history edit belongs to one copy file, so
    a batch gives the same rows as the whole table at once. The count of
    pairs skipped for a missing text is summed over the batches.
    """
    change_parts: list[pd.DataFrame] = []
    edit_parts: list[pd.DataFrame] = []
    skipped = 0
    loaded = 0
    asked = 0
    files = copies[["repo", "path"]].drop_duplicates()
    for start in range(0, len(files), _VERSION_FILE_BATCH):
        batch_files = files.iloc[start : start + _VERSION_FILE_BATCH]
        batch = copies.merge(batch_files, on=["repo", "path"], how="inner")
        batch_histories = (
            histories.merge(batch_files, on=["repo", "path"], how="inner")
            if not histories.empty
            else histories
        )
        needed = _needed_shas(batch, versions, batch_histories)
        texts = _load_texts(connection, needed, dev, quiet=True)
        loaded += len(texts)
        asked += len(needed)
        change_parts.append(
            compare_copy_with_origin(batch, batch_histories, texts, versions, registry_changes)
        )
        edits = classify_history_edits(_attach_text(batch_histories, texts), versions)
        skipped += int(edits.attrs.get("skipped_missing_text", 0))
        edit_parts.append(edits)
        batch_no = start // _VERSION_FILE_BATCH + 1
        print(f"batch {batch_no} files {len(batch_files)} texts {len(texts)}", flush=True)
    print(f"texts loaded {loaded} of {asked}", flush=True)
    changes = _concat_or_empty(change_parts, COPY_CHANGE_COLUMNS)
    edits = _concat_or_empty(edit_parts, HISTORY_COLUMNS)
    edits.attrs["skipped_missing_text"] = skipped
    return changes, edits


def _concat_or_empty(parts: list[pd.DataFrame], columns: list[str]) -> pd.DataFrame:
    """Rows of every part, or an empty frame with ``columns``.

    The frame is built from the row dicts, as a single call would build it,
    so a column that is empty in one batch does not change its type.
    """
    rows = [row for part in parts for row in part.to_dict("records")]
    if not rows:
        return _empty_frame(columns)
    return pd.DataFrame(rows).reindex(columns=columns)


def _run_versions(dev: bool, force: bool) -> None:
    """Score every copy version and write ``copy_version_similarity.parquet``."""
    path = _version_output_path(dev)
    if force and path.is_file():
        path.unlink()
    if outputs_exist([path]):
        print("skip: copy version similarity already exists", flush=True)
        _print_version_summary(read_parquet(path), 0.0)
        return
    started = time.perf_counter()
    scored = build_copy_version_similarity(dev)
    # Step 5: Save and print counts and two sample records.
    print("Step 5: save", flush=True)
    _save_version_output(scored, dev)
    _print_version_summary(scored, time.perf_counter() - started)


def _run_folders(dev: bool, force: bool) -> None:
    """Diff copy folders and write the two stage 9c tables."""
    paths = _folder_output_paths(dev)
    if force:
        for path in paths:
            if path.is_file():
                path.unlink()
    if outputs_exist(paths):
        print("skip: copy folder outputs already exist", flush=True)
        _print_saved_folder_summary(dev)
        return
    started = time.perf_counter()
    temp_dir = data_dir(dev) / "_copy_folders_tmp"
    _remove_tree(temp_dir)
    connection = connect_duckdb()
    try:
        _prepare_duckdb(connection, temp_dir)
        # Step 1: Read copy versions, package versions, and registry manifests.
        print("Step 1: copy versions, package versions, registry manifests", flush=True)
        _check_folder_inputs(dev)
        versions = _read_folder_versions(connection, dev)
        packages = read_parquet(_folder_input_paths(dev)["package_versions"])
        releases = read_parquet(_folder_input_paths(dev)["lineage_versions"])
        print(f"copy versions {len(versions)} package versions {len(packages)}", flush=True)
        # Step 2: Rebuild copy manifests (algorithm A42, queries Q9.10 and Q9.11).
        print("Step 2: copy folder manifests", flush=True)
        changes = _read_folder_history(connection, versions, dev)
        siblings = _read_sibling_files(connection, versions, changes, dev)
        print(f"folder changes {len(changes)} sibling files {len(siblings)}", flush=True)
        manifests = assemble_copy_folder_manifests(versions, changes, siblings)
        print(f"manifest rows {len(manifests)}", flush=True)
        # Step 3: Compare each version with the four targets (A43, A15, A16).
        # Registry manifests are read per batch (Q9.13). File rows go to part files.
        print("Step 3: compare folders", flush=True)
        result = _compare_folder_batches(
            connection, versions, manifests, packages, releases, dev, temp_dir / "parts"
        )
        print(f"folder change rows {result['file_rows']}", flush=True)
        # Step 4: Summarize counts, mentions and empty comparisons (A17).
        print("Step 4: summarize folder changes", flush=True)
        summary = _complete_folder_summary(
            result["summary"], result["notes"], versions, packages, releases
        )
        # Step 5: Save and print counts and two sample records.
        print("Step 5: save", flush=True)
        _save_folder_outputs(result["parts"], summary, dev, connection)
    finally:
        connection.close()
        _remove_tree(temp_dir)
    _print_folder_summary(
        result["origin_rows"],
        summary,
        versions,
        time.perf_counter() - started,
        file_rows=result["file_rows"],
        head=result["head"],
        risky=result["risky"],
    )


def _one_copy_row(
    record: dict,
    grouped: dict[tuple[str, str], list[dict]],
    texts: dict[str, str],
    blobs: dict[tuple[str, int], str],
    later: dict[str, list[tuple[int, list[str]]]],
    body_cache: dict[tuple[str, str], dict],
    name_cache: dict[tuple[str, str, str, str], dict],
    front_cache: dict[tuple[str, str], dict],
) -> dict:
    """One copy-vs-origin row, including overlap and customisation."""
    repo = str(record.get("repo") or "")
    path = str(record.get("path") or "")
    lineage_id = str(record.get("lineage_id") or "")
    origin_seq = _as_int(record.get("origin_seq")) or 0
    origin_sha = blobs.get((lineage_id, origin_seq), "")
    origin_text = _lookup_text(texts, origin_sha)
    file_rows = grouped.get((repo, path), [])
    current_text, basis = current_text_of_copy(record, file_rows, texts)
    current_sha = _current_sha(record, file_rows, texts, basis)
    change = _cached_change(
        origin_text or "",
        current_text,
        origin_sha or "missing-origin",
        current_sha or "missing-current",
        repo,
        path,
        body_cache,
        name_cache,
    )
    front = _cached_front(
        origin_text or "",
        current_text,
        origin_sha or "missing-origin",
        current_sha or "missing-current",
        front_cache,
    )
    added = _as_str_list(change["headings_added"])
    registry_added = _headings_after(later.get(lineage_id, []), origin_seq)
    row = {
        "repo": repo,
        "path": path,
        "lineage_id": lineage_id,
        "link": "" if _is_missing(record.get("link")) else str(record.get("link")),
        "origin_seq": origin_seq,
        "current_basis": basis,
        "customized": _is_customized(change),
        "heading_overlap": heading_overlap_with_registry(added, registry_added),
        "current_sha": current_sha,
        "origin_sha": origin_sha if origin_text is not None else "",
    }
    row.update(_copy_lists(change))
    row.update(_copy_front(front))
    return row


def _edits_frame(edited: list[dict], skipped_missing_text: int) -> pd.DataFrame:
    """History-edit rows, plus how many pairs were skipped for a missing text."""
    if not edited:
        frame = _empty_frame(HISTORY_COLUMNS)
    else:
        frame = pd.DataFrame(edited).reindex(columns=HISTORY_COLUMNS)
    frame.attrs["skipped_missing_text"] = skipped_missing_text
    return frame


def _has_stored_text(row: dict) -> bool:
    """True when this row has a stored text. An empty string counts. None does not."""
    return isinstance(row.get("text"), str)


def _one_edit(
    previous: dict,
    current: dict,
    by_blob: dict[str, set[str]],
    body_cache: dict[tuple[str, str], dict],
    name_cache: dict[tuple[str, str, str, str], dict],
    front_cache: dict[tuple[str, str], dict],
) -> dict:
    """One history step: kind, body change, and permissions flag."""
    repo = str(current.get("repo") or "")
    path = str(current.get("path") or "")
    lineage_id = str(current.get("lineage_id") or previous.get("lineage_id") or "")
    from_blob = str(previous.get("blob") or "")
    to_blob = str(current.get("blob") or "")
    old_text = previous.get("text") if isinstance(previous.get("text"), str) else ""
    new_text = current.get("text") if isinstance(current.get("text"), str) else ""
    change = _cached_change(
        old_text,
        new_text,
        _sha_key(from_blob) or "missing-from",
        _sha_key(to_blob) or "missing-to",
        repo,
        path,
        body_cache,
        name_cache,
    )
    front = _cached_front(
        old_text,
        new_text,
        _sha_key(from_blob) or "missing-from",
        _sha_key(to_blob) or "missing-to",
        front_cache,
    )
    row = {
        "repo": repo,
        "path": path,
        "lineage_id": lineage_id,
        "from_blob": from_blob,
        "to_blob": to_blob,
        "commit": "" if current.get("commit") is None else str(current.get("commit")),
        "commit_date": _date_cell(current.get("commit_date")),
        "change_kind": _edit_kind(to_blob, lineage_id, by_blob),
        "inherited": False,
        "permissions_changed": bool(front["permissions_changed"]),
    }
    row.update(_copy_lists(change))
    return row


def _is_customized(change: dict) -> bool:
    """True when a name, script, step, command or heading was added.

    A removed step (``steps_added`` below 0) is not customisation.

    Example:
    >>> _is_customized({"names_added": ["acme"], "scripts_mentioned_added": [],
    ...                 "steps_added": 0, "commands_added": [], "headings_added": []})
    True
    >>> _is_customized({"names_added": [], "scripts_mentioned_added": [],
    ...                 "steps_added": -1, "commands_added": [], "headings_added": []})
    False
    """
    steps = _as_int(change.get("steps_added")) or 0
    return bool(
        _has_items(change.get("names_added"))
        or _has_items(change.get("scripts_mentioned_added"))
        or steps > 0
        or _has_items(change.get("commands_added"))
        or _has_items(change.get("headings_added"))
    )


def _cached_change(
    old_text: str,
    new_text: str,
    old_key: str,
    new_key: str,
    repo: str,
    path: str,
    body_cache: dict[tuple[str, str], dict],
    name_cache: dict[tuple[str, str, str, str], dict],
) -> dict:
    """``compare_texts`` once per text pair, then project names for this repo."""
    name_key = (old_key, new_key, repo, path)
    cached = name_cache.get(name_key)
    if cached is not None:
        return cached
    body_key = (old_key, new_key)
    change = body_cache.get(body_key)
    if change is None:
        change = compare_texts(old_text, new_text, repo, path).to_dict()
        body_cache[body_key] = change
    names = find_project_names_added(old_text, new_text, repo, path)
    if list(change["names_added"]) != names:
        change = _copy_lists(change)
        change["names_added"] = names
    name_cache[name_key] = change
    return change


def _cached_front(
    old_text: str,
    new_text: str,
    old_key: str,
    new_key: str,
    cache: dict[tuple[str, str], dict],
) -> dict:
    """``compare_front_matter`` once per text pair."""
    key = (old_key, new_key)
    found = cache.get(key)
    if found is None:
        found = compare_front_matter(old_text, new_text)
        cache[key] = found
    return found


def _edit_kind(to_blob: str, lineage_id: str, by_blob: dict[str, set[str]]) -> str:
    """Sync, other source, or local, from the new blob's lineage membership."""
    owners = by_blob.get(_sha_key(to_blob), set())
    if lineage_id in owners:
        return "sync"
    if owners:
        return "other_source"
    return "local"


def _profile_row(population: str, frame: pd.DataFrame) -> dict:
    """One profile row. Empty input yields null shares."""
    changes = int(len(frame))
    return {
        "population": population,
        "changes": changes,
        "share_substantive": _share_state(frame),
        "share_headings_added": _share_items(frame, "headings_added"),
        "share_commands_added": _share_items(frame, "commands_added"),
        "share_network_added": _share_items(frame, "network_refs_added"),
        "share_scripts_added": _share_items(frame, "scripts_mentioned_added"),
        "share_permissions_changed": _share_flag(frame, "permissions_changed"),
    }


def _share_state(frame: pd.DataFrame) -> float:
    """Share of rows whose body state is substantive."""
    if frame.empty or "state" not in frame.columns:
        return float("nan")
    substantive = frame["state"].astype(str).eq("substantive").sum()
    return float(substantive) / len(frame)


def _share_items(frame: pd.DataFrame, column: str) -> float:
    """Share of rows where ``column`` lists at least one added item."""
    if frame.empty or column not in frame.columns:
        return float("nan")
    return float(frame[column].map(_has_items).sum()) / len(frame)


def _share_flag(frame: pd.DataFrame, column: str) -> float:
    """Share of rows where a boolean column is true."""
    if frame.empty or column not in frame.columns:
        return float("nan")
    return float(frame[column].map(_is_true).sum()) / len(frame)


def _edits_of_kind(edits: pd.DataFrame, kind: str) -> pd.DataFrame:
    """History rows whose ``change_kind`` is ``kind``. Other kinds are left out."""
    if edits.empty or "change_kind" not in edits.columns:
        return edits.iloc[0:0]
    return edits.loc[edits["change_kind"].astype(str).eq(kind)]


def _read_histories(
    connection: duckdb.DuckDBPyConnection,
    copies: pd.DataFrame,
    dev: bool,
) -> pd.DataFrame:
    """Wild and copied versions for the origin copies, with ``lineage_id``.

    Queries Q9.2, Q9.3 and Q9.4. Version numbers and ``inherited`` follow
    stage 7, via ``_assemble_copy_histories``.
    """
    if copies.empty:
        return _empty_frame(["repo", "path", "commit", "commit_date", "blob", "lineage_id"])
    paths = _input_paths(dev)
    keys = copies[["repo", "path", "file_sha"]].drop_duplicates(["repo", "path"])
    connection.register("origin_copies", keys)
    copied_sql = COPIED_HISTORY_SQL.format(path=_quote_path(str(paths["copied_changes"])))
    wild_sql = WILD_HISTORY_SQL.format(path=_quote_path(str(paths["wild_versions"])))
    copied = _fetch(connection, copied_sql)
    wild = _fetch(connection, wild_sql)
    commits = _fetch(
        connection,
        REGISTRY_COMMITS_SQL.format(path=_quote_path(str(paths["registry_changes"]))),
    )
    commit_list = commits["commit"].tolist() if "commit" in commits.columns else []
    histories = _assemble_copy_histories(wild, copied, keys, commit_list)
    meta = copies[["repo", "path", "lineage_id"]].drop_duplicates(["repo", "path"])
    if histories.empty:
        histories = histories.copy()
        histories["lineage_id"] = pd.Series(dtype=str)
        return histories
    return histories.merge(meta, on=["repo", "path"], how="left")


def _load_texts(
    connection: duckdb.DuckDBPyConnection,
    shas: list[str],
    dev: bool,
    quiet: bool = False,
) -> dict[str, str]:
    """Load texts for ``shas`` from the four stores, registry first.

    Queries Q9.5–Q9.8. A sha found in an earlier store is not read again.
    ``quiet`` skips the progress line. An empty sha list reads nothing.
    """
    if not shas:
        return {}
    paths = _input_paths(dev)
    found: dict[str, str] = {}
    stores = (
        (REGISTRY_TEXTS_SQL, paths["registry_texts"]),
        (COPIED_TEXTS_SQL, paths["copied_texts"]),
        (WILD_TEXTS_SQL, paths["wild_blobs"]),
        (DATASET_TEXTS_SQL, paths["artifacts"]),
    )
    for sql, path in stores:
        _load_missing(connection, sql, path, shas, found)
        if not quiet:
            print(f"texts so far {len(found)}", flush=True)
    return found


def _load_missing(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    path: Path | str,
    shas: list[str],
    found: dict[str, str],
) -> None:
    """Add texts for shas that are not already in ``found``.

    The shas go in as one registered table, so each store is read once
    per call, not once per 200 shas. Only lowercase hex shas are asked for.
    """
    missing = [
        sha
        for sha in dict.fromkeys(shas)
        if sha and sha not in found and all(character in "0123456789abcdef" for character in sha)
    ]
    if not missing:
        return
    connection.register("wanted_shas", pd.DataFrame({"sha": missing}))
    try:
        query = sql.format(path=_quote_path(str(path)), sha_list="SELECT sha FROM wanted_shas")
        found.update(_texts_from_frame(_fetch(connection, query)))
    finally:
        connection.unregister("wanted_shas")


def _needed_shas(
    copies: pd.DataFrame,
    versions: pd.DataFrame,
    histories: pd.DataFrame,
) -> list[str]:
    """Origin blobs, dataset file shas, and every history blob."""
    blobs = _version_blobs(versions)
    shas: list[str] = []
    for record in copies.to_dict("records"):
        origin_seq = _as_int(record.get("origin_seq")) or 0
        lineage_id = str(record.get("lineage_id") or "")
        shas.append(blobs.get((lineage_id, origin_seq), ""))
        shas.append(_sha_key(record.get("file_sha")))
    if not histories.empty and "blob" in histories.columns:
        shas.extend(_sha_key(blob) for blob in histories["blob"].tolist())
    return [sha for sha in dict.fromkeys(shas) if sha]


def _attach_text(histories: pd.DataFrame, texts: dict[str, str]) -> pd.DataFrame:
    """Copy history rows with a ``text`` column. A blob with no stored text is null."""
    frame = histories.copy()
    if frame.empty:
        frame["text"] = pd.Series(dtype=object)
        return frame
    text_map = _text_map(texts)
    frame["text"] = [_lookup_text(text_map, blob) for blob in frame["blob"].tolist()]
    return frame


def _current_sha(record: dict, file_rows: list[dict], texts: dict[str, str], basis: str) -> str:
    """Blob id of the text that was compared, or empty when it was missing."""
    if basis == "latest_history":
        newest = _newest_history_row(file_rows)
        sha = _sha_key(None if newest is None else newest.get("blob"))
        if sha and _lookup_text(texts, sha) is not None:
            return sha
        return ""
    sha = _sha_key(record.get("file_sha"))
    if sha and _lookup_text(texts, sha) is not None:
        return sha
    return ""


def _version_blobs(versions: pd.DataFrame) -> dict[tuple[str, int], str]:
    """Map ``(lineage_id, version_seq)`` to a lowercase blob sha."""
    found: dict[tuple[str, int], str] = {}
    if versions.empty or "blob" not in versions.columns:
        return found
    triples = zip(
        versions["lineage_id"],
        versions["version_seq"],
        versions["blob"],
        strict=True,
    )
    for lineage, seq, blob in triples:
        number = _as_int(seq)
        sha = _sha_key(blob)
        if number is None or not sha:
            continue
        found[(str(lineage), number)] = sha
    return found


def _lineages_by_blob(versions: pd.DataFrame) -> dict[str, set[str]]:
    """Map a lowercase blob to the lineage ids that contain it."""
    found: dict[str, set[str]] = {}
    if versions.empty or "blob" not in versions.columns or "lineage_id" not in versions.columns:
        return found
    for lineage, blob in zip(versions["lineage_id"], versions["blob"], strict=True):
        sha = _sha_key(blob)
        if sha:
            found.setdefault(sha, set()).add(str(lineage))
    return found


def _later_heading_index(changes: pd.DataFrame) -> dict[str, list[tuple[int, list[str]]]]:
    """Headings added by each registry change, keyed by lineage."""
    grouped: dict[str, list[tuple[int, list[str]]]] = {}
    if changes.empty or "headings_added" not in changes.columns:
        return grouped
    triples = zip(changes["lineage_id"], changes["to_seq"], changes["headings_added"], strict=True)
    for lineage, to_seq, headings in triples:
        number = _as_int(to_seq)
        if number is None:
            continue
        grouped.setdefault(str(lineage), []).append((number, _as_str_list(headings)))
    return grouped


def _headings_after(events: list[tuple[int, list[str]]], origin_seq: int) -> list[str]:
    """Headings added in registry versions whose ``to_seq`` is after the origin."""
    seen: set[str] = set()
    ordered: list[str] = []
    for to_seq, headings in events:
        if to_seq <= origin_seq:
            continue
        for heading in headings:
            if heading not in seen:
                seen.add(heading)
                ordered.append(heading)
    return ordered


def _history_rows(histories, repo: object, path: object) -> list[dict]:
    """History dicts for one repo and path. A list is filtered too."""
    want_repo = "" if repo is None else str(repo)
    want_path = "" if path is None else str(path)
    if isinstance(histories, list):
        return [
            row
            for row in histories
            if str(row.get("repo") or "") == want_repo and str(row.get("path") or "") == want_path
        ]
    frame = _as_frame(histories)
    if frame.empty or "repo" not in frame.columns or "path" not in frame.columns:
        return []
    mask = frame["repo"].astype(str).eq(want_repo) & frame["path"].astype(str).eq(want_path)
    return frame.loc[mask].to_dict("records")


def _rows_by_file(frame: pd.DataFrame) -> dict[tuple[str, str], list[dict]]:
    """Group history rows by ``(repo, path)``."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    if frame.empty or "repo" not in frame.columns or "path" not in frame.columns:
        return grouped
    for record in frame.to_dict("records"):
        key = (str(record.get("repo") or ""), str(record.get("path") or ""))
        grouped.setdefault(key, []).append(record)
    return grouped


def _newest_history_row(rows: list[dict]) -> dict | None:
    """Latest dated row, then highest ``version_seq``. Undated rows lose to dated ones."""
    if not rows:
        return None
    return max(rows, key=_history_sort_key)


def _history_sort_key(row: dict) -> tuple[int, float, int, str]:
    """Sort key: dated before undated, then time, version, commit."""
    stamp = _parse_time(row.get("commit_date"))
    dated = 1 if stamp is not None else 0
    seconds = 0.0 if stamp is None else stamp.timestamp()
    seq = _as_int(row.get("version_seq")) or 0
    commit = "" if row.get("commit") is None else str(row.get("commit"))
    return (dated, seconds, seq, commit)


def _clean_copies(frame: pd.DataFrame) -> pd.DataFrame:
    """Cast origin copies to the columns the rest of the stage reads."""
    if frame.empty:
        return _empty_frame(["repo", "path", "file_sha", "lineage_id", "link", "origin_seq"])
    cleaned = frame.copy()
    for column in ("repo", "path", "file_sha", "lineage_id", "link"):
        cleaned[column] = cleaned[column].fillna("").astype(str)
    cleaned["origin_seq"] = pd.to_numeric(cleaned["origin_seq"], errors="coerce").astype("int64")
    cleaned = cleaned.drop_duplicates(["repo", "path"], keep="first")
    columns = ["repo", "path", "file_sha", "lineage_id", "link", "origin_seq"]
    return cleaned[columns].reset_index(drop=True)


def _text_map(texts: dict[str, str]) -> dict[str, str]:
    """Lowercase sha keys. A non-string text is left out."""
    cleaned: dict[str, str] = {}
    for key, value in texts.items():
        sha = _sha_key(key)
        if sha and isinstance(value, str):
            cleaned[sha] = value
    return cleaned


def _lookup_text(texts: dict[str, str], sha: object) -> str | None:
    """Stored text, or None when this sha is absent. Empty text is present."""
    key = _sha_key(sha)
    if not key or key not in texts:
        return None
    return texts[key]


def _texts_from_frame(frame: pd.DataFrame) -> dict[str, str]:
    """Map a lowercase sha to its text. A non-string cell is skipped."""
    found: dict[str, str] = {}
    if frame.empty or "blob" not in frame.columns or "text" not in frame.columns:
        return found
    for blob, text in zip(frame["blob"].tolist(), frame["text"].tolist(), strict=True):
        key = _sha_key(blob)
        if key and isinstance(text, str):
            found[key] = text
    return found


def _copy_lists(change: dict) -> dict:
    """Copy a change dict so a cached list is not shared across rows."""
    copied: dict = {}
    for key, value in change.items():
        copied[key] = list(value) if isinstance(value, list) else value
    return copied


def _copy_front(front: dict) -> dict:
    """Front-matter fields, with lists copied and flags as plain bools."""
    copied: dict = {}
    for key in FRONT_FIELDS:
        value = front.get(key)
        if isinstance(value, list):
            copied[key] = list(value)
        elif key in {"name_changed", "description_changed", "permissions_changed"}:
            copied[key] = bool(value)
        else:
            copied[key] = _as_str_list(value)
    return copied


def _as_str_list(value: object) -> list[str]:
    """A list of strings. A scalar string is one item. Missing is empty."""
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if _is_missing(value):
        return []
    try:
        raw = list(value)  # type: ignore[arg-type]
    except TypeError:
        return []
    items: list[str] = []
    for item in raw:
        if _is_missing(item):
            continue
        items.append(str(item))
    return items


def _has_items(value: object) -> bool:
    """True when the cell lists at least one string."""
    return len(_as_str_list(value)) > 0


def _is_true(value: object) -> bool:
    """True for real true flags. Missing and the string ``false`` are false."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes"}
    if _is_missing(value):
        return False
    return bool(value)


def _is_missing(value: object) -> bool:
    """True for None and NaN. A list is never missing."""
    if value is None:
        return True
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(missing) if isinstance(missing, bool) else False


def _as_int(value: object) -> int | None:
    """Integer value, or None when the cell is missing or not a number."""
    if isinstance(value, bool) or _is_missing(value):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _sha_key(value: object) -> str:
    """Lowercase blob sha, or ``""`` when the cell is empty or the zero sha."""
    if not isinstance(value, str):
        if _is_missing(value):
            return ""
        value = str(value)
    text = value.strip().lower()
    if text in {"", "none", "nan", "<na>", _ZERO_BLOB}:
        return ""
    return text


def _parse_dates(values: pd.Series) -> pd.Series:
    """UTC timestamps. Mixed ISO-8601 offsets are parsed in one pass."""
    return pd.to_datetime(values, utc=True, errors="coerce", format="ISO8601")


def _parse_time(value: object) -> pd.Timestamp | None:
    """UTC timestamp, or None when the value is missing or invalid."""
    if _is_missing(value):
        return None
    parsed = pd.to_datetime(value, utc=True, errors="coerce", format="ISO8601")
    if not isinstance(parsed, pd.Timestamp) or pd.isna(parsed):
        return None
    return parsed


def _date_cell(value: object) -> str | None:
    """Commit date as text, or None when it is missing."""
    if _is_missing(value):
        return None
    text = str(value).strip()
    return text or None


def _as_frame(value) -> pd.DataFrame:
    """DataFrame as itself. A list of dicts becomes a frame. None is empty."""
    if isinstance(value, pd.DataFrame):
        return value
    if not value:
        return pd.DataFrame()
    return pd.DataFrame(list(value))


def _empty_frame(columns: list[str]) -> pd.DataFrame:
    """A frame with ``columns`` and no rows."""
    return pd.DataFrame(columns=columns)


def _quote_path(path: str) -> str:
    """A single-quoted SQL string with quotes escaped."""
    return "'" + path.replace("'", "''") + "'"


def _fetch(connection: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    """Run ``sql``. DuckDB's fetch still calls ``DataFrame.drop(inplace=True)``."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="The inplace keyword in DataFrame.drop is deprecated",
        )
        frame = connection.execute(sql).fetchdf()
    if frame is None:
        return pd.DataFrame()
    return frame


def _prepare_duckdb(connection: duckdb.DuckDBPyConnection, temp_dir: Path) -> None:
    """Turn off the progress bar and give large scans a spill folder."""
    temp_dir.mkdir(parents=True, exist_ok=True)
    connection.execute("SET enable_progress_bar = false")
    connection.execute("SET preserve_insertion_order = false")
    connection.execute(f"SET memory_limit = '{settings.DUCKDB_MEMORY_LIMIT}'")
    connection.execute(f"SET max_temp_directory_size = '{settings.DUCKDB_TEMP_LIMIT}'")
    connection.execute(f"SET temp_directory = {_quote_path(str(temp_dir))}")


def _input_paths(dev: bool) -> dict[str, Path | str]:
    """Stage 9a inputs. Language, wild history and artifacts are full-data files."""
    folder = data_dir(dev)
    copied = folder / "copied_skill_history"
    registry = folder / "registry_history"
    return {
        "copy_files": folder / "copy_files.parquet",
        "families": folder / "families.parquet",
        "skill_language": data_dir(False) / "skill_language.parquet",
        "lineage_versions": folder / "lineage_versions.parquet",
        "lineage_changes": folder / "lineage_changes.parquet",
        "copied_changes": copied / "copied_skill_history_changes.parquet",
        "copied_texts": copied / "copied_skill_history_texts.parquet",
        "wild_versions": settings.WILD_HISTORY_DIR / "versions.parquet",
        "wild_blobs": settings.WILD_HISTORY_DIR / "blobs.parquet",
        "registry_texts": registry / "registry_history_texts.parquet",
        "registry_changes": registry / "registry_history_changes.parquet",
        "artifacts": settings.ARTIFACTS_GLOB,
    }


def _input_builders(dev: bool) -> dict[str, str]:
    """The command that creates each missing input."""
    flag = " --dev" if dev else ""
    families = f"python -m skill_genealogy.assign_families{flag}"
    lineages = f"python -m skill_genealogy.build_registry_lineages{flag}"
    registry = f"python -m skill_genealogy.download_registry_history{flag}"
    copied = f"python -m skill_genealogy.download_copied_skill_history{flag}"
    return {
        "copy_files": families,
        "families": families,
        "skill_language": "python -m skill_genealogy.select_registries",
        "lineage_versions": lineages,
        "lineage_changes": lineages,
        "copied_changes": copied,
        "copied_texts": copied,
        "wild_versions": "src/data/wild_history is read only and is not rebuilt here",
        "wild_blobs": "src/data/wild_history is read only and is not rebuilt here",
        "registry_texts": registry,
        "registry_changes": registry,
        "artifacts": "python src/data/download_data.py",
    }


def _output_paths(dev: bool) -> list[Path]:
    """The three stage 9a parquet files."""
    folder = data_dir(dev)
    return [
        folder / "copy_changes.parquet",
        folder / "copy_history_edits.parquet",
        folder / "change_profile.parquet",
    ]


def _remove_tree(path: Path) -> None:
    """Delete a temp folder if it is present."""
    if path.exists():
        shutil.rmtree(path)


def _print_saved_summary(dev: bool) -> None:
    """Print counts from the three files already on disk."""
    folder = data_dir(dev)
    changes = read_parquet(folder / "copy_changes.parquet")
    edits = read_parquet(folder / "copy_history_edits.parquet")
    profile = read_parquet(folder / "change_profile.parquet")
    _print_run_summary(changes, edits, profile, 0.0)


def _print_run_summary(
    changes: pd.DataFrame,
    edits: pd.DataFrame,
    profile: pd.DataFrame,
    elapsed: float,
) -> None:
    """Print shares, added-item counts, and two sample rows of each table."""
    numbers = _summary_numbers(changes, edits, profile, elapsed)
    print_summary("copy vs origin", numbers)
    print_sample_records(changes, "copy changes sample")
    print_sample_records(edits, "history edits sample")
    print_sample_records(profile, "change profile sample")


def _summary_numbers(
    changes: pd.DataFrame,
    edits: pd.DataFrame,
    profile: pd.DataFrame,
    elapsed: float,
) -> dict[str, object]:
    """File and text shares, customisation, overlap, and the four populations."""
    states = changes["state"] if "state" in changes.columns else pd.Series(dtype=str)
    file_shares = _state_shares(states)
    text_states = _text_states(changes)
    text_shares = _state_shares(text_states)
    numbers: dict[str, object] = {
        "files": len(changes),
        "texts": int(text_states.shape[0]),
        "file_share_unchanged": _round(file_shares["unchanged"]),
        "file_share_cosmetic": _round(file_shares["cosmetic"]),
        "file_share_substantive": _round(file_shares["substantive"]),
        "text_share_unchanged": _round(text_shares["unchanged"]),
        "text_share_cosmetic": _round(text_shares["cosmetic"]),
        "text_share_substantive": _round(text_shares["substantive"]),
        "customized_files": int(changes["customized"].map(_is_true).sum()) if len(changes) else 0,
        "customized_share": _round(_flag_share(changes, "customized")),
        "files_headings_added": _item_count(changes, "headings_added"),
        "files_commands_added": _item_count(changes, "commands_added"),
        "files_scripts_added": _item_count(changes, "scripts_mentioned_added"),
        "files_network_added": _item_count(changes, "network_refs_added"),
        "files_names_added": _item_count(changes, "names_added"),
        "files_steps_added": _step_count(changes),
        "heading_overlap_mean": _round(_overlap_mean(changes)),
        "heading_overlap_rows": _overlap_rows(changes),
        "permissions_changed": _flag_count(changes, "permissions_changed"),
        "origin_text_missing": _blank_count(changes, "origin_sha"),
        "current_text_missing": _blank_count(changes, "current_sha"),
        "history_edits": len(edits),
        "history_local": _kind_count(edits, "local"),
        "history_sync": _kind_count(edits, "sync"),
        "history_other_source": _kind_count(edits, "other_source"),
        "seconds": round(elapsed, 1),
    }
    skipped = edits.attrs.get("skipped_missing_text") if hasattr(edits, "attrs") else None
    if skipped is not None:
        numbers["history_skipped_missing_text"] = int(skipped)
    for _, row in profile.iterrows():
        population = str(row["population"])
        numbers[f"{population}_changes"] = int(row["changes"])
        numbers[f"{population}_share_substantive"] = _round(row["share_substantive"])
    return numbers


def _text_states(changes: pd.DataFrame) -> pd.Series:
    """One state per current text. A mixed text keeps the strongest edit.

    Substantive beats cosmetic, and cosmetic beats unchanged.
    """
    if changes.empty or "current_sha" not in changes.columns or "state" not in changes.columns:
        return pd.Series(dtype=str)
    usable = changes.loc[changes["current_sha"].astype(str).ne("")]
    if usable.empty:
        return pd.Series(dtype=str)
    ranks = usable["state"].map(lambda value: _STATE_RANK.get(str(value), -1))
    worst = ranks.groupby(usable["current_sha"], sort=False).max()
    inverse = {number: name for name, number in _STATE_RANK.items()}
    return worst.map(inverse)


def _state_shares(states: pd.Series) -> dict[str, float]:
    """Share of each body state. An empty series yields null shares."""
    if len(states) == 0:
        return {name: float("nan") for name in _STATE_NAMES}
    labels = states.astype(str)
    return {name: float(labels.eq(name).sum()) / len(states) for name in _STATE_NAMES}


def _item_count(frame: pd.DataFrame, column: str) -> int:
    """Rows where ``column`` lists at least one item."""
    if frame.empty or column not in frame.columns:
        return 0
    return int(frame[column].map(_has_items).sum())


def _step_count(frame: pd.DataFrame) -> int:
    """Rows that added at least one step."""
    if frame.empty or "steps_added" not in frame.columns:
        return 0
    added = frame["steps_added"].map(lambda value: (_as_int(value) or 0) > 0)
    return int(added.sum())


def _blank_count(frame: pd.DataFrame, column: str) -> int:
    """Rows whose sha column is empty, meaning the text was not stored."""
    if frame.empty or column not in frame.columns:
        return 0
    return int(frame[column].fillna("").astype(str).eq("").sum())


def _flag_count(frame: pd.DataFrame, column: str) -> int:
    """Rows where a boolean column is true."""
    if frame.empty or column not in frame.columns:
        return 0
    return int(frame[column].map(_is_true).sum())


def _flag_share(frame: pd.DataFrame, column: str) -> float:
    """Share of rows where a boolean column is true."""
    if frame.empty or column not in frame.columns:
        return float("nan")
    return float(frame[column].map(_is_true).sum()) / len(frame)


def _overlap_mean(frame: pd.DataFrame) -> float:
    """Mean heading overlap, ignoring nulls (both sides added nothing)."""
    if frame.empty or "heading_overlap" not in frame.columns:
        return float("nan")
    return float(frame["heading_overlap"].mean(skipna=True))


def _overlap_rows(frame: pd.DataFrame) -> int:
    """Rows whose heading overlap is a number, not null."""
    if frame.empty or "heading_overlap" not in frame.columns:
        return 0
    return int(frame["heading_overlap"].notna().sum())


def _kind_count(edits: pd.DataFrame, kind: str) -> int:
    """History rows of one ``change_kind``."""
    if edits.empty or "change_kind" not in edits.columns:
        return 0
    return int(edits["change_kind"].astype(str).eq(kind).sum())


def _round(value: object) -> float:
    """Four decimal places. Null stays null."""
    if value is None:
        return float("nan")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")
    if math.isnan(number):
        return number
    return round(number, 4)


def _dataset_dates(dataset_texts, copies: pd.DataFrame) -> dict[tuple[str, str], object]:
    """Map ``(repo, path)`` to ``last_commit_at``. The copy row fills a gap."""
    found: dict[tuple[str, str], object] = {}
    frame = dataset_texts if isinstance(dataset_texts, pd.DataFrame) else pd.DataFrame()
    if not frame.empty and {"repo", "path", "last_commit_at"}.issubset(frame.columns):
        for record in frame.to_dict("records"):
            key = (str(record.get("repo") or ""), str(record.get("path") or ""))
            found[key] = record.get("last_commit_at")
    if "last_commit_at" in copies.columns:
        for record in copies.to_dict("records"):
            key = (str(record.get("repo") or ""), str(record.get("path") or ""))
            if key not in found or _is_missing(found.get(key)):
                found[key] = record.get("last_commit_at")
    return found


def _numbered_history(record: dict, file_rows: list[dict]) -> list[dict]:
    """History rows for one file, earliest first, ``version_seq`` from 1."""
    origin = _as_int(record.get("origin_seq")) or 0
    lineage = str(record.get("lineage_id") or "")
    repo = str(record.get("repo") or "")
    path = str(record.get("path") or "")
    rows = []
    for number, row in enumerate(_unique_history(file_rows), start=1):
        rows.append(
            {
                "repo": repo,
                "path": path,
                "lineage_id": lineage,
                "origin_seq": origin,
                "version_seq": number,
                "commit_date": _date_cell(row.get("commit_date")),
                "blob": _sha_key(row.get("blob")),
            }
        )
    return rows


def _unique_history(file_rows: list[dict]) -> list[dict]:
    """Dated rows first. The same commit and blob is kept once."""
    kept: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in sorted(file_rows, key=_history_sort_key):
        blob = _sha_key(row.get("blob"))
        if not blob:
            continue
        commit = "" if _is_missing(row.get("commit")) else str(row.get("commit"))
        marker = (commit, blob)
        if marker in seen:
            continue
        seen.add(marker)
        kept.append(row)
    return kept


def _dataset_version_row(record: dict, last_commit_at: object) -> dict:
    """The single captured row for a copy that has no history."""
    return {
        "repo": str(record.get("repo") or ""),
        "path": str(record.get("path") or ""),
        "lineage_id": str(record.get("lineage_id") or ""),
        "origin_seq": _as_int(record.get("origin_seq")) or 0,
        "version_seq": 1,
        "commit_date": _date_cell(last_commit_at),
        "blob": _sha_key(record.get("file_sha")),
    }


def _lineage_rows(copy_version: dict, lineage_versions: pd.DataFrame) -> pd.DataFrame:
    """Versions of this copy's lineage. An empty id keeps the whole frame."""
    frame = _as_frame(lineage_versions)
    lineage = copy_version.get("lineage_id")
    if _is_missing(lineage) or str(lineage) == "" or "lineage_id" not in frame.columns:
        return frame
    return frame.loc[frame["lineage_id"].astype(str).eq(str(lineage))]


def _newest_seq(frame: pd.DataFrame) -> int | None:
    """Highest ``version_seq``, or None when the lineage has no versions."""
    if frame.empty or "version_seq" not in frame.columns:
        return None
    numbers = []
    for value in frame["version_seq"]:
        number = _as_int(value)
        if number is not None:
            numbers.append(number)
    if not numbers:
        return None
    return max(numbers)


def _release_pairs(frame: pd.DataFrame) -> list[tuple[pd.Timestamp, int]]:
    """``(release time, version_seq)`` for every version with both values."""
    if frame.empty or "version_seq" not in frame.columns:
        return []
    pairs = []
    for record in frame.to_dict("records"):
        sequence = _as_int(record.get("version_seq"))
        released = _parse_time(record.get("release_date"))
        if sequence is not None and released is not None:
            pairs.append((released, sequence))
    return pairs


def _at_date_from_pairs(
    releases: list[tuple[pd.Timestamp, int]],
    commit_date: object,
) -> int | None:
    """Newest lineage version released on or before ``commit_date``.

    The same release date keeps the higher ``version_seq``. A missing copy
    date, or a date before every release, returns None.
    """
    stamp = _parse_time(commit_date)
    if stamp is None:
        return None
    best_seq = None
    best_time = None
    for released, sequence in releases:
        if released > stamp:
            continue
        later = best_time is None or released > best_time
        tied = released == best_time and sequence > best_seq
        if later or tied:
            best_seq = sequence
            best_time = released
    return best_seq


def _shingle_set(text: str) -> set[str]:
    """Exact shingles of the body after boilerplate lines are removed."""
    kept = remove_boilerplate_lines(body_lines(text), _boilerplate_lines())
    return make_word_shingles("\n".join(kept))


def _boilerplate_lines() -> set[str]:
    """Shared boilerplate lines, or an empty set when the file is missing."""
    global _BOILERPLATE_CACHE
    if _BOILERPLATE_CACHE is not None:
        return _BOILERPLATE_CACHE
    path = data_dir(False) / "boilerplate_lines.parquet"
    if not path.is_file():
        _BOILERPLATE_CACHE = set()
        return _BOILERPLATE_CACHE
    frame = read_parquet(path)
    lines = set(frame["line"].astype(str)) if "line" in frame.columns else set()
    _BOILERPLATE_CACHE = lines
    return lines


def _added_line_numbers(old_text: str, new_text: str) -> set[int]:
    """1-based line numbers in the new text that the diff marks as added.

    Both texts are normalised first, so a line ending does not count.
    ``difflib.SequenceMatcher(..., autojunk=False)`` finds the shared
    stretches and does not skip lines that appear often. A replaced line
    is added, because the new wording is new.
    """
    matcher = difflib.SequenceMatcher(
        None, _file_lines(old_text), _file_lines(new_text), autojunk=False
    )
    added: set[int] = set()
    for tag, _old_start, _old_end, new_start, new_end in matcher.get_opcodes():
        if tag not in {"insert", "replace"}:
            continue
        added.update(range(new_start + 1, new_end + 1))
    return added


def _file_lines(text: str) -> list[str]:
    """Normalised lines, including blank lines in the middle. Empty is []."""
    normalized = normalize_text(text)
    if normalized == "":
        return []
    return normalized.split("\n")


def _body_start_line(text: str) -> int:
    """1-based number of the first body line. 1 when there is no front matter."""
    lines = _file_lines(text)
    if not lines or lines[0].strip() != "---":
        return 1
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index + 2
    return 1


def _notes_on_added_lines(text: str, added: set[int], names: list[str]) -> list[dict]:
    """Notes for added body lines. Front matter is left to the permission note."""
    body_start = _body_start_line(text)
    fence = ""
    notes: list[dict] = []
    for number, line in enumerate(_file_lines(text), start=1):
        if number < body_start:
            continue
        updated = _fence_state(line, fence)
        if updated is not None:
            fence = updated
            continue
        if number not in added:
            continue
        for note in _line_notes(line, fence, names):
            notes.append({"line": number, "kind": note["kind"], "text": note["text"]})
    return notes


def _line_notes(line: str, fence: str, names: list[str]) -> list[dict]:
    """Heading, name, script, command and network notes on one body line."""
    stripped = line.strip()
    if not stripped:
        return []
    notes: list[dict] = []
    if fence == "" and find_headings(stripped):
        notes.append({"kind": "heading", "text": stripped})
    for name in names:
        if _name_on_line(stripped, name):
            notes.append({"kind": "name", "text": name})
    for script in find_script_mentions(stripped):
        notes.append({"kind": "script", "text": script})
    if fence == "shell" or (fence == "" and stripped.startswith("$ ")):
        notes.append({"kind": "command", "text": stripped})
    if find_network_refs(stripped):
        notes.append({"kind": "network", "text": "⚠ " + stripped})
    return notes


def _name_on_line(line: str, word: str) -> bool:
    """True when ``word`` is a whole word on this line, ignoring letter case."""
    pattern = rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])"
    return re.search(pattern, line, flags=re.IGNORECASE) is not None


def _permission_notes(old_text: str, new_text: str) -> list[dict]:
    """One note when ``allowed-tools`` changes. The line is in the new text."""
    front = compare_front_matter(old_text, new_text)
    if not front["permissions_changed"]:
        return []
    line = _permission_line(new_text)
    if line is None:
        return []
    return [{"line": line, "kind": "permission", "text": _permission_text(front)}]


def _permission_line(text: str) -> int | None:
    """Line of ``allowed-tools`` in the front matter, or line 1 when it is absent.

    Only the lines between the opening ``---`` and the closing ``---`` are
    searched. A mention in the body does not count. An empty text is None.
    """
    lines = _file_lines(text)
    if not lines:
        return None
    front_end = _body_start_line(text)
    for number, line in enumerate(lines, start=1):
        if number >= front_end:
            break
        if "allowed-tools" in line.lower():
            return number
    return 1


def _permission_text(front: dict) -> str:
    """The version-player sentence for a permission change."""
    added = _as_str_list(front.get("allowed_tools_added"))
    removed = _as_str_list(front.get("allowed_tools_removed"))
    parts = []
    if added:
        parts.append("+ " + ", ".join(added))
    if removed:
        parts.append("- " + ", ".join(removed))
    detail = "; ".join(parts) if parts else "changed"
    return f"⚠ permissions changed: {detail}"


def _last_commit_dates(
    connection: duckdb.DuckDBPyConnection,
    copies: pd.DataFrame,
    histories: pd.DataFrame,
    dev: bool,
) -> pd.DataFrame:
    """``last_commit_at`` for origin copies that have no history row.

    Query Q9.9. The artifact ``content`` column is not read.
    """
    empty = _empty_frame(["repo", "path", "last_commit_at"])
    if copies.empty:
        return empty
    need = _copies_without_history(copies, histories)
    if need.empty:
        return empty
    connection.register("undated_copies", need)
    query = LAST_COMMIT_SQL.format(path=_quote_path(str(_input_paths(dev)["artifacts"])))
    return _fetch(connection, query)


def _copies_without_history(copies: pd.DataFrame, histories: pd.DataFrame) -> pd.DataFrame:
    """``repo`` and ``path`` of copies that have no history row."""
    keys = copies[["repo", "path"]].drop_duplicates()
    history = _as_frame(histories)
    if history.empty or "repo" not in history.columns or "path" not in history.columns:
        return keys
    have = history[["repo", "path"]].drop_duplicates()
    merged = keys.merge(have, on=["repo", "path"], how="left", indicator=True)
    return merged.loc[merged["_merge"].eq("left_only"), ["repo", "path"]].reset_index(drop=True)


def _score_in_batches(
    connection: duckdb.DuckDBPyConnection,
    listed: pd.DataFrame,
    versions: pd.DataFrame,
    dev: bool,
) -> pd.DataFrame:
    """Score copy files in batches. Each batch loads its own texts and drops them.

    The registry target texts are loaded per batch too, so the full data
    never holds every lineage text at once. The score caches only save
    work, so they are also dropped with the batch.
    """
    if listed.empty:
        return _empty_similarity()
    lookups = _version_lookups(versions)
    files = listed[["repo", "path"]].drop_duplicates()
    pieces = []
    total = len(files)
    for start in range(0, total, _VERSION_FILE_BATCH):
        batch_files = files.iloc[start : start + _VERSION_FILE_BATCH]
        chunk = listed.merge(batch_files, on=["repo", "path"], how="inner")
        texts = _load_texts(connection, _chunk_shas(chunk, lookups), dev, quiet=True)
        batch_no = start // _VERSION_FILE_BATCH + 1
        print(
            f"batch {batch_no} files {len(batch_files)} versions {len(chunk)} texts {len(texts)}",
            flush=True,
        )
        pieces.append(_score_version_table(chunk, versions, texts, {}, {}, lookups))
    return _cast_similarity(pd.concat(pieces, ignore_index=True))


def _version_lookups(versions: pd.DataFrame) -> dict[str, dict]:
    """Blob, lineage and target lookups, built once for every batch."""
    return {
        "blobs": _version_blobs(versions),
        "by_blob": _lineages_by_blob(versions),
        "targets": target_index(versions),
    }


def _chunk_shas(chunk: pd.DataFrame, lookups: dict[str, dict]) -> list[str]:
    """Copy blobs and the lineage blobs the four targets point at."""
    blobs = lookups["blobs"]
    shas = [_sha_key(blob) for blob in chunk["blob"].tolist()]
    for record in chunk.to_dict("records"):
        origin = _as_int(record.get("origin_seq")) or 0
        targets = pick_comparison_targets(record, lookups["targets"], origin)
        lineage = str(record.get("lineage_id") or "")
        for sequence in (targets["origin_seq"], targets["at_date_seq"], targets["newest_seq"]):
            if sequence is None:
                continue
            shas.append(blobs.get((lineage, int(sequence)), ""))
    return [sha for sha in dict.fromkeys(shas) if sha]


def _score_version_table(
    listed: pd.DataFrame,
    lineage_versions: pd.DataFrame,
    texts: dict[str, str],
    score_cache: dict[tuple[str, str], dict] | None = None,
    note_cache: dict[tuple[str, str, str, str], str] | None = None,
    lookups: dict[str, dict] | None = None,
) -> pd.DataFrame:
    """Similarity rows for ``listed``. Missing texts leave that target null.

    ``lookups`` is ``_version_lookups(lineage_versions)``. A caller that
    scores many batches builds it once. Without it, it is built here.
    """
    frame = _as_frame(listed)
    if frame.empty:
        return _empty_similarity()
    scores = {} if score_cache is None else score_cache
    notes = {} if note_cache is None else note_cache
    found = _version_lookups(lineage_versions) if lookups is None else lookups
    blobs = found["blobs"]
    by_blob = found["by_blob"]
    text_map = _text_map(texts)
    previous = _rows_by_seq(frame)
    rows = []
    for record in frame.to_dict("records"):
        rows.append(
            _one_similarity_row(
                record,
                found["targets"],
                blobs,
                by_blob,
                text_map,
                previous,
                scores,
                notes,
            )
        )
    return _cast_similarity(pd.DataFrame(rows).reindex(columns=SIMILARITY_COLUMNS))


def _rows_by_seq(frame: pd.DataFrame) -> dict[tuple[str, str, int], dict]:
    """Map ``(repo, path, version_seq)`` to that version row."""
    found: dict[tuple[str, str, int], dict] = {}
    for record in frame.to_dict("records"):
        sequence = _as_int(record.get("version_seq"))
        if sequence is None:
            continue
        key = (str(record.get("repo") or ""), str(record.get("path") or ""), sequence)
        found[key] = record
    return found


def _one_similarity_row(
    record: dict,
    lineage_versions: pd.DataFrame | dict,
    blobs: dict[tuple[str, int], str],
    by_blob: dict[str, set[str]],
    texts: dict[str, str],
    previous: dict[tuple[str, str, int], dict],
    score_cache: dict[tuple[str, str], dict],
    note_cache: dict[tuple[str, str, str, str], str],
) -> dict:
    """One copy version, with four score groups and the step's change notes."""
    origin = _as_int(record.get("origin_seq")) or 0
    targets = pick_comparison_targets(record, lineage_versions, origin)
    repo = str(record.get("repo") or "")
    path = str(record.get("path") or "")
    lineage = str(record.get("lineage_id") or "")
    copy_sha = _sha_key(record.get("blob"))
    copy_text = _lookup_text(texts, copy_sha)
    prev_text, prev_sha = _previous_text(record, targets["prev_seq"], previous, texts)
    row = _version_fields(record, targets, by_blob, repo, path, lineage, copy_sha)
    row["change_notes"] = _notes_json(
        prev_text, prev_sha, copy_text, copy_sha, repo, path, note_cache
    )
    sources = [("prev", prev_text, prev_sha)]
    for prefix in ("origin", "at_date", "newest"):
        sha = _lineage_sha(lineage, targets[prefix + "_seq"], blobs)
        sources.append((prefix, _lookup_text(texts, sha), sha))
    for prefix, old_text, old_sha in sources:
        _add_target_scores(row, prefix, old_text, old_sha, copy_text, copy_sha, score_cache)
    return row


def _version_fields(
    record: dict,
    targets: dict,
    by_blob: dict[str, set[str]],
    repo: str,
    path: str,
    lineage: str,
    copy_sha: str,
) -> dict:
    """Identity columns for one copy version, before the four scores."""
    sequence = _as_int(record.get("version_seq")) or 0
    return {
        "repo": repo,
        "path": path,
        "lineage_id": lineage,
        "version_seq": sequence,
        "commit_date": _date_cell(record.get("commit_date")),
        "blob": copy_sha,
        "change_kind": _version_kind(sequence, copy_sha, lineage, by_blob),
        "at_date_seq": targets["at_date_seq"],
        "newest_seq": targets["newest_seq"],
    }


def _add_target_scores(
    row: dict,
    prefix: str,
    old_text: str | None,
    old_sha: str,
    new_text: str | None,
    new_sha: str,
    cache: dict[tuple[str, str], dict],
) -> None:
    """Write one target's six scores onto ``row``."""
    row.update(_prefixed_scores(prefix, old_text, old_sha, new_text, new_sha, cache))


def _version_kind(
    version_seq: int,
    blob: str,
    lineage_id: str,
    by_blob: dict[str, set[str]],
) -> str:
    """The first recorded version is a copy. Later versions use the edit label."""
    if version_seq <= 1:
        return "copy"
    return _edit_kind(blob, lineage_id, by_blob)


def _previous_text(
    record: dict,
    prev_seq: int | None,
    previous: dict[tuple[str, str, int], dict],
    texts: dict[str, str],
) -> tuple[str | None, str]:
    """Text and blob of the previous version. The first version has neither."""
    if prev_seq is None:
        return None, ""
    key = (str(record.get("repo") or ""), str(record.get("path") or ""), prev_seq)
    earlier = previous.get(key)
    if earlier is None:
        return None, ""
    sha = _sha_key(earlier.get("blob"))
    return _lookup_text(texts, sha), sha


def _lineage_sha(lineage_id: str, sequence: int | None, blobs: dict[tuple[str, int], str]) -> str:
    """Blob of one lineage version, or ``""`` when that version is missing."""
    if sequence is None:
        return ""
    return blobs.get((lineage_id, int(sequence)), "")


def _prefixed_scores(
    prefix: str,
    old_text: str | None,
    old_sha: str,
    new_text: str | None,
    new_sha: str,
    cache: dict[tuple[str, str], dict],
) -> dict:
    """Six score columns. A missing text on either side leaves them null."""
    if old_text is None or new_text is None:
        scores = {name: None for name in _SCORE_NAMES}
    else:
        key = (old_sha, new_sha)
        scores = cache.get(key) if old_sha and new_sha else None
        if scores is None:
            scores = score_pair(old_text, new_text)
            if old_sha and new_sha:
                cache[key] = scores
    return {f"{prefix}_{name}": scores[name] for name in _SCORE_NAMES}


def _notes_json(
    old_text: str | None,
    old_sha: str,
    new_text: str | None,
    new_sha: str,
    repo: str,
    path: str,
    cache: dict[tuple[str, str, str, str], str],
) -> str:
    """JSON list of change notes. No previous text means an empty list."""
    if old_text is None or new_text is None:
        return "[]"
    key = (old_sha, new_sha, repo, path)
    cached = cache.get(key) if old_sha and new_sha else None
    if cached is None:
        notes = change_notes_for_step(old_text, new_text, repo, path)
        cached = json.dumps(notes, ensure_ascii=False)
        if old_sha and new_sha:
            cache[key] = cached
    return cached


def _empty_similarity() -> pd.DataFrame:
    """A similarity table with the output columns and no rows."""
    return _empty_frame(SIMILARITY_COLUMNS)


def _cast_similarity(frame: pd.DataFrame) -> pd.DataFrame:
    """Float scores, nullable target sequences, and a JSON notes string."""
    cast = frame.reindex(columns=SIMILARITY_COLUMNS).copy()
    if cast.empty:
        return cast
    for name in _SCORE_NAMES:
        for prefix in ("prev", "origin", "at_date", "newest"):
            column = f"{prefix}_{name}"
            cast[column] = pd.to_numeric(cast[column], errors="coerce")
    cast["version_seq"] = pd.to_numeric(cast["version_seq"], errors="coerce").astype("int64")
    for column in ("at_date_seq", "newest_seq"):
        cast[column] = pd.to_numeric(cast[column], errors="coerce").astype("Int64")
    cast["change_notes"] = cast["change_notes"].fillna("[]").astype(str)
    return cast


def _version_output_path(dev: bool) -> Path:
    """Path of ``copy_version_similarity.parquet`` for this mode."""
    return data_dir(dev) / "copy_version_similarity.parquet"


def _save_version_output(frame: pd.DataFrame, dev: bool) -> Path:
    """Write the similarity table, sorted by repo, path and version."""
    path = _version_output_path(dev)
    write_parquet(frame.reindex(columns=SIMILARITY_COLUMNS), path, ["repo", "path", "version_seq"])
    return path


def _print_version_summary(frame: pd.DataFrame, elapsed: float) -> None:
    """Print the drift counts and two sample version rows."""
    print_summary("copy version similarity", _version_numbers(frame, elapsed))
    print_sample_records(frame, "copy version similarity sample")


def _version_numbers(frame: pd.DataFrame, elapsed: float) -> dict[str, object]:
    """Versions scored, multi-version copies, origin drift, and share behind."""
    first = _edge_jaccard(frame, "first")
    last = _edge_jaccard(frame, "last")
    behind = _behind_mask(frame)
    return {
        "copy_versions": len(frame),
        "copies": _copy_count(frame),
        "copies_with_multiple_versions": _multi_copy_count(frame),
        "median_origin_jaccard_first": _round(first),
        "median_origin_jaccard_last": _round(last),
        "origin_jaccard_drift": _round(_drift(first, last)),
        "share_behind_at_date": _round(_behind_share(frame, behind)),
        "versions_behind_at_date": int(behind.sum()) if len(frame) else 0,
        "versions_missing_at_date": _missing_at_date(frame),
        "seconds": round(elapsed, 1),
    }


def _copy_count(frame: pd.DataFrame) -> int:
    """How many distinct copy files are in the table."""
    if frame.empty or "repo" not in frame.columns:
        return 0
    return int(frame.groupby(["repo", "path"], sort=False).ngroups)


def _multi_copy_count(frame: pd.DataFrame) -> int:
    """Copies that have more than one version."""
    if frame.empty or "version_seq" not in frame.columns:
        return 0
    sizes = frame.groupby(["repo", "path"], sort=False)["version_seq"].size()
    return int((sizes > 1).sum())


def _edge_jaccard(frame: pd.DataFrame, which: str) -> float:
    """Median origin Jaccard of the first or last version of multi-version copies."""
    if frame.empty or "origin_jaccard" not in frame.columns:
        return float("nan")
    sizes = frame.groupby(["repo", "path"], sort=False)["version_seq"].transform("size")
    multi = frame.loc[sizes > 1]
    if multi.empty:
        return float("nan")
    picker = "idxmin" if which == "first" else "idxmax"
    chosen = getattr(multi.groupby(["repo", "path"], sort=False)["version_seq"], picker)()
    return float(multi.loc[chosen, "origin_jaccard"].median(skipna=True))


def _drift(first: float, last: float) -> float:
    """Last median minus first median. A negative number is drift away from the origin."""
    if math.isnan(first) or math.isnan(last):
        return float("nan")
    return last - first


def _behind_mask(frame: pd.DataFrame) -> pd.Series:
    """True when the at-date version is older than the newest lineage version."""
    if frame.empty or "at_date_seq" not in frame.columns or "newest_seq" not in frame.columns:
        return pd.Series(dtype=bool)
    at_date = pd.to_numeric(frame["at_date_seq"], errors="coerce")
    newest = pd.to_numeric(frame["newest_seq"], errors="coerce")
    return at_date.notna() & newest.notna() & (at_date < newest)


def _behind_share(frame: pd.DataFrame, behind: pd.Series) -> float:
    """Share of versions whose at-date target is not the newest lineage version."""
    if frame.empty:
        return float("nan")
    return float(behind.sum()) / len(frame)


def _missing_at_date(frame: pd.DataFrame) -> int:
    """Versions with no lineage release on or before their commit date."""
    if frame.empty or "at_date_seq" not in frame.columns:
        return 0
    return int(pd.to_numeric(frame["at_date_seq"], errors="coerce").isna().sum())


def _folder_change(
    commit: str,
    commit_seq: int,
    status: str,
    file_path: str,
    new_sha: str,
    commit_date: str = "2026-02-01T00:00:00Z",
) -> dict[str, object]:
    """One copied-skill folder change for the manifest doctest."""
    return {
        "repo": "acme/app",
        "skill_folder": "skills/a",
        "commit": commit,
        "commit_date": commit_date,
        "commit_seq": commit_seq,
        "status": status,
        "file_path": file_path,
        "old_file_path": "",
        "new_sha": new_sha,
        "new_mode": "100644",
        "history_role": "own",
        "repo_path": f"skills/a/{file_path}",
        "old_repo_path": "",
    }


def _check_folder_inputs(dev: bool) -> None:
    """Raise ``FileNotFoundError`` when a stage 9c input is missing."""
    flag = " --dev" if dev else ""
    lineages = f"python -m skill_genealogy.build_registry_lineages{flag}"
    versions = f"python -m skill_genealogy.measure_copy_changes{flag} --only versions"
    copied = f"python -m skill_genealogy.download_copied_skill_history{flag}"
    check_inputs_exist(
        _folder_input_paths(dev),
        {
            "copy_files": f"python -m skill_genealogy.assign_families{flag}",
            "copy_versions": versions,
            "package_versions": lineages,
            "folder_manifests": lineages,
            "lineage_versions": lineages,
            "copied_changes": copied,
            "copied_texts": copied,
            "registry_texts": f"python -m skill_genealogy.download_registry_history{flag}",
            "siblings": "python src/data/download_data.py",
        },
    )


def _folder_input_paths(dev: bool) -> dict[str, Path | str]:
    """Stage 9c inputs. Siblings are the raw dataset glob."""
    folder = data_dir(dev)
    copied = folder / "copied_skill_history"
    registry = folder / "registry_history"
    return {
        "copy_files": folder / "copy_files.parquet",
        "copy_versions": folder / "copy_version_similarity.parquet",
        "package_versions": folder / "lineage_package_versions.parquet",
        "folder_manifests": folder / "lineage_folder_manifests.parquet",
        "lineage_versions": folder / "lineage_versions.parquet",
        "copied_changes": copied / "copied_skill_history_changes.parquet",
        "copied_texts": copied / "copied_skill_history_texts.parquet",
        "registry_texts": registry / "registry_history_texts.parquet",
        "siblings": settings.SIBLINGS_GLOB,
    }


def _folder_output_paths(dev: bool) -> list[Path]:
    """The two stage 9c parquet files."""
    folder = data_dir(dev)
    return [
        folder / "copy_folder_changes.parquet",
        folder / "copy_version_folder_summary.parquet",
    ]


def _read_folder_versions(connection: duckdb.DuckDBPyConnection, dev: bool) -> pd.DataFrame:
    """Copy versions joined to ``origin_seq``. Query Q9.12."""
    paths = _folder_input_paths(dev)
    query = FOLDER_VERSIONS_SQL.format(
        versions=_quote_path(str(paths["copy_versions"])),
        copies=_quote_path(str(paths["copy_files"])),
    )
    return _fetch(connection, query)


def _read_folder_history(
    connection: duckdb.DuckDBPyConnection,
    versions: pd.DataFrame,
    dev: bool,
) -> pd.DataFrame:
    """Folder changes for the skill folders those versions sit in. Query Q9.10."""
    folders = _copy_folder_keys(versions)
    if folders.empty:
        return pd.DataFrame()
    connection.register("copy_folders", folders)
    query = FOLDER_HISTORY_SQL.format(
        path=_quote_path(str(_folder_input_paths(dev)["copied_changes"]))
    )
    return _fetch(connection, query)


def _read_sibling_files(
    connection: duckdb.DuckDBPyConnection,
    versions: pd.DataFrame,
    changes: pd.DataFrame,
    dev: bool,
) -> pd.DataFrame:
    """Sibling files for copies whose folder was not in the history table. Query Q9.11."""
    keys = _snapshot_keys(versions, changes)
    empty = pd.DataFrame(columns=["repo", "path", "entry_name", "entry_sha", "entry_size"])
    if keys.empty:
        return empty
    connection.register("snapshot_copies", keys)
    query = SIBLING_FILES_SQL.format(path=_quote_path(str(_folder_input_paths(dev)["siblings"])))
    return _fetch(connection, query)


def _copy_folder_keys(versions: pd.DataFrame) -> pd.DataFrame:
    """One ``repo, skill_folder`` row for each copy path. The root folder is ``.``."""
    if versions.empty or "repo" not in versions.columns or "path" not in versions.columns:
        return pd.DataFrame(columns=["repo", "skill_folder"])
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    for repo, path in zip(versions["repo"], versions["path"], strict=True):
        key = ("" if _is_missing(repo) else str(repo), _skill_folder_of(path))
        if key in seen:
            continue
        seen.add(key)
        rows.append({"repo": key[0], "skill_folder": key[1]})
    return pd.DataFrame(rows, columns=["repo", "skill_folder"])


def _snapshot_keys(versions: pd.DataFrame, changes: pd.DataFrame) -> pd.DataFrame:
    """Copy paths whose skill folder has no own or former-folder history rows."""
    have: set[tuple[str, str]] = set()
    if not changes.empty and {"repo", "skill_folder"}.issubset(changes.columns):
        for repo, folder in zip(changes["repo"], changes["skill_folder"], strict=True):
            have.add(_folder_pair(repo, folder))
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    if versions.empty or "path" not in versions.columns:
        return pd.DataFrame(columns=["repo", "path"])
    for repo, path in zip(versions["repo"], versions["path"], strict=True):
        pair = _folder_pair(repo, _skill_folder_of(path))
        if pair in have:
            continue
        key = (pair[0], "" if _is_missing(path) else str(path))
        if key in seen:
            continue
        seen.add(key)
        rows.append({"repo": key[0], "path": key[1]})
    return pd.DataFrame(rows, columns=["repo", "path"])


def _folder_pair(repo: object, folder: object) -> tuple[str, str]:
    """``(repo, skill_folder)`` with the root folder written as ``.``."""
    repo_text = "" if _is_missing(repo) else str(repo)
    return repo_text, _folder_name(folder)


def _history_snapshots(
    changes: pd.DataFrame,
) -> tuple[dict[str, list[dict]], dict[tuple[str, str], list[dict]]]:
    """Per-commit file lists, and the commits that wrote each SKILL.md blob."""
    if changes.empty:
        return {}, {}
    labeled = _label_copy_lineage(changes)
    anchors = _skill_anchors(labeled)
    return _replay_copy_folders(labeled), anchors


def _replay_copy_folders(changes: pd.DataFrame) -> dict[str, list[dict]]:
    """Folder after each commit, using the same apply rules as A21.

    Dates are parsed once for the whole table. Parsing them again inside
    every skill folder repeats the same order and reloads timezone data.
    """
    prepared = _prepare_folder_changes(changes)
    if prepared.empty:
        return {}
    ordered = _order_all_folders(prepared)
    snapshots: dict[str, list[dict]] = {}
    for index, (lineage_id, group) in enumerate(ordered.groupby("lineage_id", sort=False), start=1):
        snapshots[str(lineage_id)] = _replay_one_lineage(group)
        if index % 5000 == 0:
            print(f"folders replayed {index}", flush=True)
    return snapshots


def _order_all_folders(prepared: pd.DataFrame) -> pd.DataFrame:
    """Oldest commit first, then delete, rename, and add, within each folder."""
    ordered = prepared.copy()
    ordered["sort_time"] = _parse_dates(ordered["commit_date"])
    status = ordered["status"].map(_clean).str[:1]
    ordered["apply_rank"] = status.map(_APPLY_ORDER).fillna(5)
    return ordered.sort_values(
        ["lineage_id", "sort_time", "commit_seq", "apply_rank", "file_path"],
        kind="mergesort",
    )


def _replay_one_lineage(ordered: pd.DataFrame) -> list[dict]:
    """Apply one folder's commits. ``ordered`` is already oldest-first."""
    state: dict[str, tuple[str, str]] = {}
    commits: list[dict] = []
    for commit, rows in _iter_commits(ordered):
        _apply_change_rows(state, rows)
        commits.append(_snapshot_commit(commit, rows[0], state))
    return commits


def _snapshot_commit(commit: str, first: dict, state: dict[str, tuple[str, str]]) -> dict:
    """The folder after one commit: time key plus each package file."""
    when = first.get("sort_time")
    if not isinstance(when, pd.Timestamp) or pd.isna(when):
        when = None
    files = [_stored_file(path, sha, mode, None) for path, (sha, mode) in sorted(state.items())]
    return {"key": _commit_key(when, _commit_seq(first.get("commit_seq")), commit), "files": files}


def _label_copy_lineage(changes: pd.DataFrame) -> pd.DataFrame:
    """Set ``lineage_id`` to ``repo::skill_folder`` so the A21 walk groups copies."""
    labeled = changes.copy()
    repos = labeled["repo"] if "repo" in labeled.columns else [""] * len(labeled)
    folders = labeled["skill_folder"] if "skill_folder" in labeled.columns else ["."] * len(labeled)
    labeled["lineage_id"] = [
        _folder_key(repo, folder) for repo, folder in zip(repos, folders, strict=True)
    ]
    return labeled


def _skill_anchors(changes: pd.DataFrame) -> dict[tuple[str, str], list[dict]]:
    """Map ``(lineage, blob)`` to the commits that wrote that root SKILL.md blob."""
    subset = _anchor_rows(changes)
    found: dict[tuple[str, str], list[dict]] = {}
    for record in subset.to_dict("records"):
        sha = _sha_key(record.get("new_sha"))
        lineage = "" if _is_missing(record.get("lineage_id")) else str(record.get("lineage_id"))
        if not sha or not lineage:
            continue
        found.setdefault((lineage, sha), []).append(_anchor_item(record))
    return found


def _anchor_rows(changes: pd.DataFrame) -> pd.DataFrame:
    """Root SKILL.md rows that are not deletes, with the date parsed once."""
    if changes.empty or "file_path" not in changes.columns:
        return changes.iloc[0:0]
    relative = changes["file_path"].map(_relative_entry)
    keep = relative.str.lower().eq("skill.md")
    if "status" in changes.columns:
        status = changes["status"].map(lambda value: "" if _is_missing(value) else str(value))
        keep = keep & ~status.str.startswith("D")
    subset = changes.loc[keep].copy()
    if subset.empty or "commit_date" not in subset.columns:
        return subset
    subset["_when"] = _parse_dates(subset["commit_date"])
    return subset


def _anchor_item(record: dict) -> dict:
    """Sort key and timestamp for one SKILL.md commit."""
    commit = "" if _is_missing(record.get("commit")) else str(record.get("commit"))
    seq = _as_int(record.get("commit_seq")) or 0
    when = record.get("_when")
    if not isinstance(when, pd.Timestamp) or pd.isna(when):
        when = _parse_time(record.get("commit_date"))
    return {"key": _commit_key(when, seq, commit), "time": when}


def _commits_by_lineage(walked: pd.DataFrame) -> dict[str, list[dict]]:
    """Lineage to commit snapshots, oldest first. A null path adds no file."""
    if walked.empty or "lineage_id" not in walked.columns:
        return {}
    grouped: dict[str, dict[str, dict]] = {}
    for record in walked.to_dict("records"):
        lineage = "" if _is_missing(record.get("lineage_id")) else str(record.get("lineage_id"))
        commit = "" if _is_missing(record.get("commit")) else str(record.get("commit"))
        when = _parse_time(record.get("commit_date"))
        seq = _as_int(record.get("commit_seq")) or 0
        bucket = grouped.setdefault(lineage, {})
        entry = bucket.get(commit)
        if entry is None:
            entry = {"key": _commit_key(when, seq, commit), "files": []}
            bucket[commit] = entry
        path = record.get("path")
        if _is_missing(path) or str(path) == "":
            continue
        entry["files"].append(
            _stored_file(str(path), record.get("sha"), record.get("mode"), record.get("size"))
        )
    return {
        lineage: sorted(commits.values(), key=lambda item: item["key"])
        for lineage, commits in grouped.items()
    }


def _manifest_records(
    versions: pd.DataFrame,
    snapshots: dict[str, list[dict]],
    anchors: dict[tuple[str, str], list[dict]],
    siblings: dict[tuple[str, str], list[dict]],
) -> list[dict]:
    """Manifest rows for every copy version."""
    records: list[dict] = []
    grouped = versions.groupby(["repo", "path"], sort=False)
    for (repo, path), group in grouped:
        ordered = group.sort_values("version_seq", kind="mergesort").to_dict("records")
        lineage = _folder_key(repo, _skill_folder_of(path))
        commits = snapshots.get(lineage)
        if commits:
            records.extend(
                _history_version_rows(str(repo), str(path), lineage, ordered, commits, anchors)
            )
        else:
            files = siblings.get((str(repo), str(path)), [])
            records.extend(_snapshot_version_rows(str(repo), str(path), ordered, files))
    return records


def _history_version_rows(
    repo: str,
    path: str,
    lineage: str,
    ordered: list[dict],
    commits: list[dict],
    anchors: dict[tuple[str, str], list[dict]],
) -> list[dict]:
    """One history snapshot per version of this copy."""
    rows: list[dict] = []
    last = len(ordered) - 1
    for index, version in enumerate(ordered):
        files = _files_for_history_version(version, ordered, index, last, lineage, commits, anchors)
        rows.extend(_stamp_files(repo, path, version, files, "history"))
    return rows


def _files_for_history_version(
    version: dict,
    ordered: list[dict],
    index: int,
    last: int,
    lineage: str,
    commits: list[dict],
    anchors: dict[tuple[str, str], list[dict]],
) -> list[dict]:
    """Folder files current at this version. The last version has no upper bound."""
    blob = _sha_key(version.get("blob"))
    anchor = _choose_anchor(anchors.get((lineage, blob), []), version.get("commit_date"))
    if anchor is None:
        return _files_by_date(commits, version.get("commit_date"), is_last=index == last)
    upper = None if index == last else _upper_key(ordered[index + 1], lineage, anchors)
    return _files_in_range(commits, anchor["key"], upper)


def _choose_anchor(anchors: list[dict], commit_date: object) -> dict | None:
    """The commit of this blob closest to ``commit_date``, preferring one on or before it."""
    if not anchors:
        return None
    stamp = _parse_time(commit_date)
    if stamp is None:
        return max(anchors, key=lambda item: item["key"])
    not_after = [item for item in anchors if item["time"] is not None and item["time"] <= stamp]
    pool = not_after or anchors

    def distance(item: dict) -> float:
        when = item["time"]
        if when is None:
            return 10**18
        return abs((when - stamp).total_seconds())

    return min(pool, key=lambda item: (distance(item), -item["key"][1]))


def _upper_key(
    next_version: dict,
    lineage: str,
    anchors: dict[tuple[str, str], list[dict]],
) -> tuple | None:
    """Exclusive end of this version: the next version's anchor, else its date."""
    blob = _sha_key(next_version.get("blob"))
    anchor = _choose_anchor(anchors.get((lineage, blob), []), next_version.get("commit_date"))
    if anchor is not None:
        return anchor["key"]
    stamp = _parse_time(next_version.get("commit_date"))
    if stamp is None:
        return None
    return (int(stamp.value), -1, "")


def _files_in_range(commits: list[dict], lower: tuple, upper: tuple | None) -> list[dict]:
    """Files of the latest snapshot at or after ``lower`` and before ``upper``."""
    chosen = None
    for commit in commits:
        if commit["key"] < lower:
            continue
        if upper is not None and commit["key"] >= upper:
            break
        chosen = commit
    if chosen is None:
        return []
    return list(chosen["files"])


def _files_by_date(commits: list[dict], commit_date: object, *, is_last: bool) -> list[dict]:
    """Latest snapshot on or before the date. The last undated version uses the end."""
    if not commits:
        return []
    stamp = _parse_time(commit_date)
    if stamp is None:
        return list(commits[-1]["files"]) if is_last else []
    limit = int(stamp.value)
    chosen = None
    for commit in commits:
        if commit["key"][0] < 0:
            continue
        if commit["key"][0] <= limit:
            chosen = commit
        else:
            break
    if chosen is None and is_last:
        return list(commits[-1]["files"])
    if chosen is None:
        return []
    return list(chosen["files"])


def _snapshot_version_rows(
    repo: str, path: str, ordered: list[dict], files: list[dict]
) -> list[dict]:
    """The same sibling snapshot on every version. Source is ``dataset_snapshot``."""
    rows: list[dict] = []
    for version in ordered:
        rows.extend(_stamp_files(repo, path, version, files, "dataset_snapshot"))
    return rows


def _stamp_files(repo: str, path: str, version: dict, files: list[dict], source: str) -> list[dict]:
    """Manifest rows for one version. An empty folder keeps one null path."""
    sequence = _as_int(version.get("version_seq")) or 0
    if not files:
        return [
            {
                "repo": repo,
                "path": path,
                "version_seq": sequence,
                "file_path": None,
                "sha": None,
                "mode": None,
                "size": None,
                "source": source,
            }
        ]
    return [
        {
            "repo": repo,
            "path": path,
            "version_seq": sequence,
            "file_path": item["path"],
            "sha": item["sha"],
            "mode": item["mode"],
            "size": item["size"],
            "source": source,
        }
        for item in files
    ]


def _sibling_index(siblings: pd.DataFrame) -> dict[tuple[str, str], list[dict]]:
    """``(repo, path)`` to package files. Root SKILL.md and skipped folders are dropped."""
    if siblings.empty:
        return {}
    found: dict[tuple[str, str], dict[str, dict]] = {}
    for record in siblings.to_dict("records"):
        repo = "" if _is_missing(record.get("repo")) else str(record.get("repo"))
        path = "" if _is_missing(record.get("path")) else str(record.get("path"))
        relative = _relative_entry(record.get("entry_name"))
        if not relative or _is_root_skill_name(relative) or is_skipped_path(relative):
            continue
        slot = found.setdefault((repo, path), {})
        slot[relative] = _stored_file(
            relative, record.get("entry_sha"), None, record.get("entry_size")
        )
    return {key: list(files.values()) for key, files in found.items()}


def _with_skill_folder(versions: pd.DataFrame) -> pd.DataFrame:
    """Copy of ``versions`` with ``lineage_key`` set from the skill folder."""
    frame = versions.copy()
    paths = frame["path"] if "path" in frame.columns else pd.Series([""] * len(frame))
    repos = frame["repo"] if "repo" in frame.columns else pd.Series([""] * len(frame))
    frame["lineage_key"] = [
        _folder_key(repo, _skill_folder_of(path)) for repo, path in zip(repos, paths, strict=True)
    ]
    return frame


def _skill_folder_of(path: object) -> str:
    """Parent of a dataset path. A file at the repo root has folder ``.``."""
    relative = _relative_entry(path)
    if "/" not in relative:
        return "."
    return relative.rsplit("/", 1)[0]


def _folder_name(folder: object) -> str:
    """Skill folder text. Blank and ``.`` are both the repo root."""
    if _is_missing(folder):
        return "."
    text = str(folder).strip().strip("/")
    return text or "."


def _folder_key(repo: object, folder: object) -> str:
    """``repo::skill_folder``, the group key of the copy-folder walk."""
    repo_text = "" if _is_missing(repo) else str(repo).strip()
    return f"{repo_text}::{_folder_name(folder)}"


def _relative_entry(value: object) -> str:
    """Forward-slash path with ``.`` and empty parts removed."""
    if _is_missing(value):
        return ""
    parts = [part for part in str(value).replace("\\", "/").split("/") if part not in ("", ".")]
    return "/".join(parts)


def _is_root_skill_name(path: str) -> bool:
    """True for ``SKILL.md`` at the skill-folder root, in any letter case."""
    return "/" not in path and path.lower() == "skill.md"


def _stored_file(path: str, sha: object, mode: object, size: object) -> dict:
    """One manifest file. A blank sha stays blank and is not paired as a rename."""
    mode_text = None if _is_missing(mode) or str(mode) == "" else str(mode)
    sha_text = "" if _is_missing(sha) else str(sha)
    return {"path": path, "sha": sha_text, "mode": mode_text, "size": _optional_size(size)}


def _commit_key(when: pd.Timestamp | None, seq: int, commit: str) -> tuple:
    """Order commits by time, then ``commit_seq``, then the commit id."""
    nanos = -1 if when is None else int(when.value)
    return (nanos, seq, commit)


def _optional_size(value: object) -> int | None:
    """Byte size as an int, or None when it is missing."""
    if _is_missing(value) or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    return None


def _copy_manifest_frame(records: list[dict]) -> pd.DataFrame:
    """The copy-manifest table. ``size`` stays a nullable integer."""
    if not records:
        return _empty_copy_manifests()
    frame = pd.DataFrame(records)
    frame = frame.reindex(columns=COPY_MANIFEST_COLUMNS)
    frame["version_seq"] = frame["version_seq"].astype("int64")
    frame["size"] = pd.array([_optional_size(value) for value in frame["size"]], dtype="Int64")
    return frame


def _empty_copy_manifests() -> pd.DataFrame:
    """A copy-manifest table with the right columns and no rows."""
    frame = pd.DataFrame(columns=COPY_MANIFEST_COLUMNS)
    frame["version_seq"] = pd.Series(dtype="int64")
    frame["size"] = pd.Series(dtype="Int64")
    return frame


def _as_package_index(package_versions) -> dict[str, list[dict]]:
    """Lineage to ``{seq, time}`` rows. A dict is already that index."""
    if isinstance(package_versions, dict):
        return package_versions
    return _package_index(_as_frame(package_versions))


def _package_index(packages: pd.DataFrame) -> dict[str, list[dict]]:
    """Lineage to its package versions. ``time`` is ``commit_date``."""
    found: dict[str, list[dict]] = {}
    if packages.empty or "lineage_id" not in packages.columns:
        return found
    for record in packages.to_dict("records"):
        sequence = _as_int(record.get("package_version_seq"))
        if sequence is None:
            continue
        lineage = str(record.get("lineage_id") or "")
        found.setdefault(lineage, []).append(
            {"seq": sequence, "time": _parse_time(record.get("commit_date"))}
        )
    return found


def _package_seq_at(lineage_id: str, release_date, package_versions, *, newest: bool) -> int | None:
    """Package sequence current at a date. ``newest`` ignores the date and takes the last."""
    indexed = _as_package_index(package_versions)
    rows = indexed.get(str(lineage_id), [])
    if not rows:
        return None
    if newest:
        return max(item["seq"] for item in rows)
    stamp = _parse_time(release_date)
    if stamp is None:
        return None
    best = None
    for item in rows:
        when = item["time"]
        if when is None or when > stamp:
            continue
        later = best is None or when > best["time"]
        tied = best is not None and when == best["time"] and item["seq"] > best["seq"]
        if later or tied:
            best = item
    if best is None:
        return None
    return int(best["seq"])


def _as_registry_index(manifests) -> dict[tuple[str, int], list[dict]]:
    """``(lineage, package_version_seq)`` to files. A dict is already that index."""
    if isinstance(manifests, dict):
        return manifests
    return _registry_file_index(_as_frame(manifests))


def _registry_file_index(manifests: pd.DataFrame) -> dict[tuple[str, int], list[dict]]:
    """Package version to its files. A null path is an empty folder and is skipped."""
    found: dict[tuple[str, int], list[dict]] = {}
    if manifests.empty or "package_version_seq" not in manifests.columns:
        return found
    for record in manifests.to_dict("records"):
        sequence = _as_int(record.get("package_version_seq"))
        path = record.get("path")
        if sequence is None or _is_missing(path) or str(path) == "":
            continue
        lineage = str(record.get("lineage_id") or "")
        item = _stored_file(str(path), record.get("sha"), record.get("mode"), record.get("size"))
        found.setdefault((lineage, sequence), []).append(item)
    return found


def _files_for_package(lineage_id: str, sequence: int, manifests) -> list[dict]:
    """Files of one package version, sorted by path."""
    indexed = _as_registry_index(manifests)
    files = list(indexed.get((str(lineage_id), int(sequence)), []))
    files.sort(key=lambda item: item["path"])
    return files


def _index_copy_manifests(manifests: pd.DataFrame) -> dict[tuple[str, str, int], list[dict]]:
    """``(repo, path, version_seq)`` to manifest rows, including a null path."""
    found: dict[tuple[str, str, int], list[dict]] = {}
    if manifests.empty:
        return found
    for record in manifests.to_dict("records"):
        sequence = _as_int(record.get("version_seq"))
        if sequence is None:
            continue
        repo = "" if _is_missing(record.get("repo")) else str(record.get("repo"))
        path = "" if _is_missing(record.get("path")) else str(record.get("path"))
        found.setdefault((repo, path, sequence), []).append(record)
    return found


def _as_release_index(versions) -> dict[tuple[str, int], object]:
    """``(lineage_id, version_seq)`` to ``release_date``. A dict is already that index."""
    if isinstance(versions, dict):
        return versions
    return _release_index(_as_frame(versions))


def _release_index(versions: pd.DataFrame) -> dict[tuple[str, int], object]:
    """``(lineage_id, version_seq)`` to ``release_date``."""
    found: dict[tuple[str, int], object] = {}
    if versions.empty or "version_seq" not in versions.columns:
        return found
    for record in versions.to_dict("records"):
        sequence = _as_int(record.get("version_seq"))
        if sequence is None:
            continue
        lineage = str(record.get("lineage_id") or "")
        found[(lineage, sequence)] = record.get("release_date")
    return found


def _diffs_for_file(
    version_rows: list[dict],
    indexed: dict[tuple[str, str, int], list[dict]],
    packages: dict[str, list[dict]],
    registry_files: dict[tuple[str, int], list[dict]],
    releases: dict[tuple[str, int], object],
    texts: dict[str, str] | None,
) -> list[dict]:
    """Folder diffs of one copy, for every version and every target that exists."""
    records: list[dict] = []
    for index, version in enumerate(version_rows):
        repo = str(version.get("repo") or "")
        path = str(version.get("path") or "")
        sequence = _as_int(version.get("version_seq")) or 0
        current = indexed.get((repo, path, sequence), [])
        files = _real_files(current)
        source = _source_of(current)
        mentions = _mention_pair(version, files, texts)
        if index > 0:
            previous = _as_int(version_rows[index - 1].get("version_seq")) or 0
            prev_files = _real_files(indexed.get((repo, path, previous), []))
            records.extend(
                _diff_rows(version, "prev", prev_files, files, texts, mentions, source)
            )
        registry_targets = (
            ("origin", _origin_date(version, releases), False),
            ("at_date", version.get("commit_date"), False),
            ("newest", None, True),
        )
        for target, release_date, newest in registry_targets:
            records.extend(
                _registry_rows(
                    version,
                    target,
                    release_date,
                    newest,
                    files,
                    packages,
                    registry_files,
                    texts,
                    mentions,
                    source,
                )
            )
    return records


def _origin_date(version: dict, releases: dict[tuple[str, int], object]) -> object:
    """Release date of this copy's origin text version, or None."""
    sequence = _as_int(version.get("origin_seq"))
    if sequence is None:
        return None
    lineage = str(version.get("lineage_id") or "")
    return releases.get((lineage, sequence))


def _registry_rows(
    version: dict,
    target: str,
    release_date: object,
    newest: bool,
    files: list[dict],
    packages: dict[str, list[dict]],
    registry_files: dict[tuple[str, int], list[dict]],
    texts: dict[str, str] | None,
    mentions: tuple,
    source: str,
) -> list[dict]:
    """Diff against one registry package. No package at that date writes no rows."""
    lineage = str(version.get("lineage_id") or "")
    sequence = _package_seq_at(lineage, release_date, packages, newest=newest)
    if sequence is None:
        return []
    old = registry_files.get((lineage, sequence), [])
    return _diff_rows(version, target, old, files, texts, mentions, source)


def _diff_rows(
    version: dict,
    target: str,
    old: list[dict],
    new: list[dict],
    texts: dict[str, str] | None,
    mentions: tuple,
    source: str,
) -> list[dict]:
    """One output row per file ``compare_folders`` returns for this target."""
    missing, hidden = mentions
    rows: list[dict] = []
    repo = str(version.get("repo") or "")
    path = str(version.get("path") or "")
    sequence = _as_int(version.get("version_seq")) or 0
    for item in compare_folders(old, new):
        risk, fresh = _script_flags(item, texts)
        rows.append(
            {
                "repo": repo,
                "path": path,
                "version_seq": sequence,
                "target": target,
                "file_path": item["path"],
                "old_file_path": item["old_path"],
                "result": item["result"],
                "old_sha": item["old_sha"],
                "new_sha": item["new_sha"],
                "mode_changed": bool(item["mode_changed"]),
                "category": item["category"],
                "size_change": item["size_change"],
                "risk_flags": risk,
                "new_risk_flags": fresh,
                "missing_scripts": missing,
                "hidden_scripts": hidden,
                "folder_source": source,
            }
        )
    return rows


def _real_files(rows: list[dict]) -> list[dict]:
    """Package files. A null ``file_path`` is only there to keep the source."""
    files: list[dict] = []
    for row in rows:
        relative = row.get("file_path")
        if _is_missing(relative) or str(relative) == "":
            continue
        files.append(_stored_file(str(relative), row.get("sha"), row.get("mode"), row.get("size")))
    return files


def _source_of(rows: list[dict]) -> str:
    """``history`` or ``dataset_snapshot``. An empty list is history."""
    for row in rows:
        source = row.get("source")
        if isinstance(source, str) and source:
            return source
    return "history"


def _mention_pair(
    version: dict,
    files: list[dict],
    texts: dict[str, str] | None,
) -> tuple[list[str] | None, list[str] | None]:
    """Missing and hidden scripts, or nulls when the SKILL.md text is absent."""
    if texts is None:
        return None, None
    text = _lookup_text(texts, version.get("blob"))
    if text is None:
        return None, None
    found = check_script_mentions(text, files)
    return list(found["missing_scripts"]), list(found["hidden_scripts"])


def _script_flags(
    row: dict, texts: dict[str, str] | None
) -> tuple[list[str] | None, list[str] | None]:
    """Risk flags for an added or modified script.

    No new text leaves both null. An added script treats every flag as new.
    A modified script whose old text is missing keeps the new flags and sets
    ``new_risk_flags`` to null, so a missing blob is not counted as new risk.
    """
    if texts is None:
        return None, None
    if row.get("category") != "script" or row.get("result") not in ("added", "modified"):
        return None, None
    new_text = _lookup_text(texts, row.get("new_sha"))
    if new_text is None:
        return None, None
    risk = flag_script_risks(new_text)
    if row.get("result") == "added":
        return risk, new_risk_flags(None, new_text)
    old_text = _lookup_text(texts, row.get("old_sha"))
    if old_text is None:
        return risk, None
    return risk, new_risk_flags(old_text, new_text)


def _folder_change_frame(records: list[dict]) -> pd.DataFrame:
    """File-level folder diffs, including mention columns used by the summary."""
    if not records:
        return _empty_folder_changes()
    frame = pd.DataFrame(records)
    frame["version_seq"] = frame["version_seq"].astype("int64")
    frame["mode_changed"] = frame["mode_changed"].astype(bool)
    frame["size_change"] = pd.array(
        [_optional_size(value) for value in frame["size_change"]], dtype="Int64"
    )
    return frame


def _empty_folder_changes() -> pd.DataFrame:
    """A folder-change table with the saved columns plus the summary extras."""
    columns = [*FOLDER_CHANGE_COLUMNS, "missing_scripts", "hidden_scripts", "folder_source"]
    frame = pd.DataFrame(columns=columns)
    frame["version_seq"] = pd.Series(dtype="int64")
    frame["size_change"] = pd.Series(dtype="Int64")
    return frame


def _one_folder_summary(key: tuple, group: pd.DataFrame) -> dict:
    """Counts for one ``repo, path, version_seq, target``."""
    repo, path, sequence, target = key
    return {
        "repo": repo,
        "path": path,
        "version_seq": int(sequence),
        "target": target,
        "files_added": _result_count(group, "added"),
        "files_removed": _result_count(group, "removed"),
        "files_modified": _result_count(group, "modified"),
        "scripts_added": _script_result_count(group, "added"),
        "scripts_modified": _script_result_count(group, "modified"),
        "new_risky_scripts": _risky_script_count(group),
        "missing_scripts": _first_list(group, "missing_scripts"),
        "hidden_scripts": _first_list(group, "hidden_scripts"),
        "folder_source": _first_text(group, "folder_source"),
    }


def _result_count(group: pd.DataFrame, result: str) -> int:
    """How many file rows have this result."""
    if "result" not in group.columns:
        return 0
    return int(group["result"].eq(result).sum())


def _script_result_count(group: pd.DataFrame, result: str) -> int:
    """How many script rows have this result."""
    if "result" not in group.columns or "category" not in group.columns:
        return 0
    mask = group["category"].eq("script") & group["result"].eq(result)
    return int(mask.sum())


def _risky_script_count(group: pd.DataFrame) -> int:
    """Added or modified scripts whose ``new_risk_flags`` list is not empty."""
    if "new_risk_flags" not in group.columns:
        return 0
    count = 0
    for value in group["new_risk_flags"]:
        if _flag_names(value):
            count += 1
    return count


def _first_list(group: pd.DataFrame, column: str) -> list | None:
    """The first list in ``column``, or None when every cell is missing."""
    if column not in group.columns:
        return None
    for value in group[column]:
        names = _flag_names(value)
        if names or value == []:
            return names
    return None


def _first_text(group: pd.DataFrame, column: str) -> str | None:
    """The first non-empty string in ``column``."""
    if column not in group.columns:
        return None
    for value in group[column]:
        if isinstance(value, str) and value:
            return value
    return None


def _flag_names(value: object) -> list[str]:
    """A list of flag or path names. Anything else is an empty list."""
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    if _is_missing(value) or isinstance(value, str):
        return []
    if hasattr(value, "tolist"):
        converted = value.tolist()
        if isinstance(converted, list):
            return [str(item) for item in converted]
    return []


def _folder_summary_frame(records: list[dict]) -> pd.DataFrame:
    """The per-version summary. Count columns stay integers."""
    if not records:
        return _empty_folder_summary()
    frame = pd.DataFrame(records).reindex(columns=FOLDER_SUMMARY_COLUMNS)
    frame["version_seq"] = frame["version_seq"].astype("int64")
    for name in (
        "files_added",
        "files_removed",
        "files_modified",
        "scripts_added",
        "scripts_modified",
        "new_risky_scripts",
    ):
        frame[name] = frame[name].fillna(0).astype("int64")
    return frame


def _empty_folder_summary() -> pd.DataFrame:
    """A folder summary with the right columns and no rows."""
    frame = pd.DataFrame(columns=FOLDER_SUMMARY_COLUMNS)
    frame["version_seq"] = pd.Series(dtype="int64")
    for name in (
        "files_added",
        "files_removed",
        "files_modified",
        "scripts_added",
        "scripts_modified",
        "new_risky_scripts",
    ):
        frame[name] = pd.Series(dtype="int64")
    return frame


def _compare_folder_batches(
    connection: duckdb.DuckDBPyConnection,
    versions: pd.DataFrame,
    manifests: pd.DataFrame,
    packages: pd.DataFrame,
    releases: pd.DataFrame,
    dev: bool,
    parts_dir: Path,
) -> dict[str, object]:
    """Compare in file batches. Texts, registry files and file rows stay per batch.

    Each batch reads the registry files of its own lineages (Q9.13) and
    writes its file rows to ``parts_dir``. The summary is built per batch,
    because every summary key belongs to one copy file. Kept in memory:
    the summary, the notes, the origin rows of each copy's latest version
    (for the printed shares), and the first rows and first risky rows (for
    the printed samples).
    """
    package_index = _package_index(packages)
    release_index = _release_index(releases)
    manifest_groups = _manifest_groups(manifests)
    latest = _latest_keys(versions)
    registry_path = _folder_input_paths(dev)["folder_manifests"]
    parts_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    summary_records: list[dict] = []
    note_rows: list[dict] = []
    origin_frames: list[pd.DataFrame] = []
    head_frames: list[pd.DataFrame] = []
    risky_frames: list[pd.DataFrame] = []
    file_rows = 0
    batches = _file_batches(versions, _VERSION_FILE_BATCH)
    for number, batch in enumerate(batches, start=1):
        batch_manifests = _manifests_for_batch(batch, manifest_groups)
        registry_index = _registry_file_index(
            _batch_registry_manifests(connection, batch, registry_path)
        )
        rows = compare_copy_folders(
            batch, batch_manifests, package_index, registry_index, release_index, texts=None
        )
        cache: dict[str, str] = {}
        _load_folder_texts(connection, _folder_text_shas(rows, batch), cache, set(), dev)
        rows = _fill_risk_flags(rows, cache)
        note_rows.extend(_note_rows(batch, _index_copy_manifests(batch_manifests), cache))
        summary_records.extend(summarize_folder_changes(rows).to_dict("records"))
        if not rows.empty:
            part_path = parts_dir / f"part_{number:05d}.parquet"
            parts.append(_write_folder_part(rows, file_rows, part_path))
            origin_frames.append(_latest_origin_rows(rows, latest))
            if sum(len(frame) for frame in head_frames) < 2:
                head_frames.append(rows.head(2))
            if sum(len(frame) for frame in risky_frames) < 2:
                risky_frames.append(_risky_rows(rows).head(2))
        file_rows += len(rows)
        print(f"folder batch {number} of {len(batches)} file rows {len(rows)}", flush=True)
    return {
        "parts": parts,
        "summary": _folder_summary_frame(summary_records),
        "notes": pd.DataFrame(note_rows),
        "origin_rows": _frames_or_empty(origin_frames),
        "head": _frames_or_empty(head_frames).head(2),
        "risky": _frames_or_empty(risky_frames).head(2),
        "file_rows": file_rows,
    }


def _frames_or_empty(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """The non-empty frames stacked, or an empty folder-change table."""
    kept = [frame for frame in frames if not frame.empty]
    return pd.concat(kept, ignore_index=True) if kept else _empty_folder_changes()


def _batch_registry_manifests(
    connection: duckdb.DuckDBPyConnection,
    batch: pd.DataFrame,
    path: Path | str,
) -> pd.DataFrame:
    """Registry package files of the lineages in ``batch``. Query Q9.13."""
    lineages = pd.DataFrame(
        {"lineage_id": sorted({str(item) for item in batch["lineage_id"].dropna()})}
    )
    connection.register("batch_lineages", lineages)
    try:
        return _fetch(connection, BATCH_MANIFESTS_SQL.format(path=_quote_path(str(path))))
    finally:
        connection.unregister("batch_lineages")


def _write_folder_part(rows: pd.DataFrame, first_row: int, path: Path) -> Path:
    """Write one batch of file rows with the stored types and a running row number.

    ``row_order`` keeps the batch order, so the merged file breaks sort
    ties the way one stable sort of every row would.
    """
    stored = rows.reindex(columns=FOLDER_CHANGE_COLUMNS)
    table = pa.Table.from_pandas(stored, schema=FOLDER_CHANGE_SCHEMA, preserve_index=False)
    order = pa.array(range(first_row, first_row + len(stored)), type=pa.int64())
    pq.write_table(table.append_column("row_order", order), path, compression="zstd")
    return path


def _latest_origin_rows(rows: pd.DataFrame, latest: set[tuple[str, str, int]]) -> pd.DataFrame:
    """File rows of the origin target, for the latest version of each copy."""
    if rows.empty or "target" not in rows.columns:
        return rows.iloc[0:0]
    mask = [
        target == "origin" and (str(repo), str(path), int(seq or 0)) in latest
        for repo, path, seq, target in zip(
            rows["repo"], rows["path"], rows["version_seq"], rows["target"], strict=True
        )
    ]
    return rows.loc[mask]


def _manifest_groups(manifests: pd.DataFrame) -> dict[tuple[str, str], pd.DataFrame]:
    """``(repo, path)`` to that copy's manifest rows."""
    if manifests.empty or "repo" not in manifests.columns:
        return {}
    grouped = manifests.groupby(["repo", "path"], sort=False)
    return {key: group for key, group in grouped}


def _file_batches(versions: pd.DataFrame, size: int) -> list[pd.DataFrame]:
    """Slices of ``versions``, each covering at most ``size`` copy files."""
    if versions.empty:
        return []
    keys = versions.loc[:, ["repo", "path"]].drop_duplicates().to_dict("records")
    frames: list[pd.DataFrame] = []
    for start in range(0, len(keys), size):
        chunk = keys[start : start + size]
        wanted = {(str(item["repo"]), str(item["path"])) for item in chunk}
        mask = [
            (str(repo), str(path)) in wanted
            for repo, path in zip(versions["repo"], versions["path"], strict=True)
        ]
        frames.append(versions.loc[mask].copy())
    return frames


def _manifests_for_batch(
    batch: pd.DataFrame,
    groups: dict[tuple[str, str], pd.DataFrame],
) -> pd.DataFrame:
    """Manifest rows for the copies in this batch."""
    frames = []
    seen: set[tuple[str, str]] = set()
    for repo, path in zip(batch["repo"], batch["path"], strict=True):
        key = (str(repo), str(path))
        if key in seen:
            continue
        seen.add(key)
        group = groups.get(key)
        if group is not None and not group.empty:
            frames.append(group)
    if not frames:
        return _empty_copy_manifests()
    return pd.concat(frames, ignore_index=True)


def _folder_text_shas(rows: pd.DataFrame, versions: pd.DataFrame) -> list[str]:
    """Script shas that were added or modified, plus each version's SKILL.md blob."""
    found: list[str] = []
    if not rows.empty and {"category", "result"}.issubset(rows.columns):
        mask = rows["category"].eq("script") & rows["result"].isin(["added", "modified"])
        for column in ("old_sha", "new_sha"):
            if column not in rows.columns:
                continue
            found.extend(_sha_key(value) for value in rows.loc[mask, column].tolist())
    if not versions.empty and "blob" in versions.columns:
        found.extend(_sha_key(value) for value in versions["blob"].tolist())
    return [sha for sha in dict.fromkeys(found) if sha]


def _load_folder_texts(
    connection: duckdb.DuckDBPyConnection,
    shas: list[str],
    cache: dict[str, str],
    tried: set[str],
    dev: bool,
) -> None:
    """Read texts that are not already in ``cache``. Queries Q9.5–Q9.8."""
    missing = [sha for sha in shas if sha not in tried]
    if not missing:
        return
    tried.update(missing)
    cache.update(_load_texts(connection, missing, dev, quiet=True))


def _fill_risk_flags(frame: pd.DataFrame, texts: dict[str, str]) -> pd.DataFrame:
    """Set risk columns now that script texts are loaded."""
    if frame.empty:
        return frame
    risks: list[list[str] | None] = []
    fresh: list[list[str] | None] = []
    for record in frame.to_dict("records"):
        risk, new = _script_flags(record, texts)
        risks.append(risk)
        fresh.append(new)
    updated = frame.copy()
    updated["risk_flags"] = risks
    updated["new_risk_flags"] = fresh
    return updated


def _note_rows(
    versions: pd.DataFrame,
    indexed: dict[tuple[str, str, int], list[dict]],
    texts: dict[str, str],
) -> list[dict]:
    """One mention row per version, including versions whose folder diff is empty."""
    rows: list[dict] = []
    for record in versions.to_dict("records"):
        repo = str(record.get("repo") or "")
        path = str(record.get("path") or "")
        sequence = _as_int(record.get("version_seq")) or 0
        stored = indexed.get((repo, path, sequence), [])
        missing, hidden = _mention_pair(record, _real_files(stored), texts)
        rows.append(
            {
                "repo": repo,
                "path": path,
                "version_seq": sequence,
                "missing_scripts": missing,
                "hidden_scripts": hidden,
                "folder_source": _source_of(stored),
            }
        )
    return rows


def _complete_folder_summary(
    summary: pd.DataFrame,
    notes: pd.DataFrame,
    versions: pd.DataFrame,
    packages: pd.DataFrame,
    releases: pd.DataFrame,
) -> pd.DataFrame:
    """Summary rows, with a zero row where the folders were compared and matched nothing.

    ``summary`` is ``summarize_folder_changes`` of every file row. The
    batches build it, because the file rows are not kept in memory.
    """
    summary = _overlay_notes(summary, notes)
    package_index = _package_index(packages)
    release_index = _release_index(releases)
    return _add_zero_summaries(summary, notes, versions, package_index, release_index)


def _overlay_notes(summary: pd.DataFrame, notes: pd.DataFrame) -> pd.DataFrame:
    """Fill mention lists and ``folder_source`` from the per-version notes."""
    if summary.empty or notes.empty:
        return summary
    indexed = _note_index(notes)
    frame = summary.copy()
    missing: list[object] = []
    hidden: list[object] = []
    sources: list[object] = []
    for record in frame.to_dict("records"):
        note = indexed.get(_note_key(record), {})
        missing.append(_kept_list(record.get("missing_scripts"), note.get("missing_scripts")))
        hidden.append(_kept_list(record.get("hidden_scripts"), note.get("hidden_scripts")))
        source = record.get("folder_source")
        if not isinstance(source, str) or not source:
            source = note.get("folder_source")
        sources.append(source)
    frame["missing_scripts"] = missing
    frame["hidden_scripts"] = hidden
    frame["folder_source"] = sources
    return frame


def _kept_list(current: object, fallback: object) -> object:
    """``current`` when it is a list, otherwise ``fallback``."""
    if isinstance(current, list):
        return current
    if isinstance(fallback, list):
        return fallback
    return None


def _add_zero_summaries(
    summary: pd.DataFrame,
    notes: pd.DataFrame,
    versions: pd.DataFrame,
    packages: dict[str, list[dict]],
    releases: dict[tuple[str, int], object],
) -> pd.DataFrame:
    """Add a zero row for each compared target that produced no file row."""
    present = _summary_keys(summary)
    indexed = _note_index(notes)
    extra: list[dict] = []
    if versions.empty:
        return summary
    for record in versions.to_dict("records"):
        for target in _targets_for_version(record, packages, releases):
            key = (*_note_key(record), target)
            if key in present:
                continue
            note = indexed.get(_note_key(record), {})
            extra.append(_zero_summary(record, target, note))
    if not extra:
        return summary
    added = _folder_summary_frame(extra)
    if summary.empty:
        return added
    return pd.concat([summary, added], ignore_index=True)


def _targets_for_version(
    version: dict,
    packages: dict[str, list[dict]],
    releases: dict[tuple[str, int], object],
) -> list[str]:
    """Targets that have a baseline. A date with no package version is left out."""
    targets: list[str] = []
    sequence = _as_int(version.get("version_seq")) or 0
    if sequence > 1:
        targets.append("prev")
    lineage = str(version.get("lineage_id") or "")
    origin = _package_seq_at(lineage, _origin_date(version, releases), packages, newest=False)
    if origin is not None:
        targets.append("origin")
    at_date = _package_seq_at(lineage, version.get("commit_date"), packages, newest=False)
    if at_date is not None:
        targets.append("at_date")
    newest = _package_seq_at(lineage, None, packages, newest=True)
    if newest is not None:
        targets.append("newest")
    return targets


def _zero_summary(version: dict, target: str, note: dict) -> dict:
    """A compared target whose two folders had no files."""
    return {
        "repo": str(version.get("repo") or ""),
        "path": str(version.get("path") or ""),
        "version_seq": _as_int(version.get("version_seq")) or 0,
        "target": target,
        "files_added": 0,
        "files_removed": 0,
        "files_modified": 0,
        "scripts_added": 0,
        "scripts_modified": 0,
        "new_risky_scripts": 0,
        "missing_scripts": note.get("missing_scripts"),
        "hidden_scripts": note.get("hidden_scripts"),
        "folder_source": note.get("folder_source"),
    }


def _summary_keys(summary: pd.DataFrame) -> set[tuple]:
    """``(repo, path, version_seq, target)`` already in the summary."""
    if summary.empty:
        return set()
    return {
        (str(repo), str(path), int(seq), str(target))
        for repo, path, seq, target in zip(
            summary["repo"], summary["path"], summary["version_seq"], summary["target"], strict=True
        )
    }


def _note_index(notes: pd.DataFrame) -> dict[tuple, dict]:
    """``(repo, path, version_seq)`` to the mention row."""
    if notes.empty:
        return {}
    return {_note_key(record): record for record in notes.to_dict("records")}


def _note_key(record: dict) -> tuple:
    """The version key shared by notes and summary rows."""
    return (
        str(record.get("repo") or ""),
        str(record.get("path") or ""),
        int(_as_int(record.get("version_seq")) or 0),
    )


def _save_folder_outputs(
    parts: list[Path],
    summary: pd.DataFrame,
    dev: bool,
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Write the file diffs from the batch parts, and the per-version summary."""
    folder = data_dir(dev)
    target = folder / "copy_folder_changes.parquet"
    if parts:
        _merge_folder_parts(connection, parts, target)
    else:
        write_parquet(_empty_folder_changes().reindex(columns=FOLDER_CHANGE_COLUMNS), target)
    write_parquet(
        summary.reindex(columns=FOLDER_SUMMARY_COLUMNS),
        folder / "copy_version_folder_summary.parquet",
        ["repo", "path", "version_seq", "target"],
    )


def _print_saved_folder_summary(dev: bool) -> None:
    """Print counts from the two folder files already on disk.

    Only the origin rows of each copy's latest version, the first rows and
    the first risky rows are read. The full-data file is too large for memory.
    """
    folder = data_dir(dev)
    path = _quote_path(str(folder / "copy_folder_changes.parquet"))
    summary = read_parquet(folder / "copy_version_folder_summary.parquet")
    versions = summary.loc[:, ["repo", "path", "version_seq"]].drop_duplicates()
    latest = pd.DataFrame(sorted(_latest_keys(versions)), columns=["repo", "path", "version_seq"])
    connection = connect_duckdb()
    try:
        connection.register("latest_versions", latest)
        file_rows = connection.execute(f"SELECT count(*) FROM read_parquet({path})").fetchone()[0]
        origin_rows = _fetch(connection, SAVED_ORIGIN_ROWS_SQL.format(path=path))
        head = _fetch(connection, SAVED_HEAD_ROWS_SQL.format(path=path))
        risky = _fetch(connection, SAVED_RISKY_ROWS_SQL.format(path=path))
        head = head.drop(columns=["file_row_number"], errors="ignore")
        risky = _risky_rows(risky.drop(columns=["file_row_number"], errors="ignore")).head(2)
    finally:
        connection.close()
    _print_folder_summary(
        origin_rows, summary, versions, 0.0, file_rows=int(file_rows), head=head, risky=risky
    )


def _merge_folder_parts(
    connection: duckdb.DuckDBPyConnection,
    parts: list[Path],
    target: Path,
) -> None:
    """Sort every batch part into one file, streaming, with the part schema.

    The order is repo, path, version_seq, target, file_path, then the batch
    row order, with nulls last. That is the stable pandas sort the file
    used before, so the rows come out in the same order. DuckDB spills the
    sort to its temp folder, so the rows never sit in memory at once.
    """
    listed = "[" + ", ".join(_quote_path(str(path)) for path in parts) + "]"
    query = FOLDER_MERGE_SQL.format(parts=listed)
    schema = pq.read_schema(parts[0])
    schema = schema.remove(schema.get_field_index("row_order"))
    target.parent.mkdir(parents=True, exist_ok=True)
    # A relation keeps ORDER BY in the stream. execute() does not when
    # preserve_insertion_order is off, and it copies every row first.
    reader = connection.sql(query).to_arrow_reader(100_000)
    with pq.ParquetWriter(target, schema, compression="zstd") as writer:
        for batch in reader:
            writer.write_table(pa.Table.from_batches([batch]).cast(schema))


def _print_folder_summary(
    rows: pd.DataFrame,
    summary: pd.DataFrame,
    versions: pd.DataFrame,
    elapsed: float,
    file_rows: int | None = None,
    head: pd.DataFrame | None = None,
    risky: pd.DataFrame | None = None,
) -> None:
    """Print folder shares, flag counts, and two sample rows of each table.

    ``rows`` needs only the origin rows of each copy's latest version.
    ``file_rows``, ``head`` and ``risky`` replace the count and samples
    taken from ``rows`` when the caller did not keep every file row.
    """
    numbers = _folder_numbers(rows, summary, versions, elapsed)
    if file_rows is not None:
        numbers["file_rows"] = file_rows
    print_summary("copy folders", numbers)
    print_sample_records(rows if head is None else head, "folder changes sample")
    print_sample_records(summary, "folder summary sample")
    print_sample_records(_risky_rows(rows) if risky is None else risky, "new risk sample")


def _folder_numbers(
    rows: pd.DataFrame,
    summary: pd.DataFrame,
    versions: pd.DataFrame,
    elapsed: float,
) -> dict[str, object]:
    """Shares for the latest version of each copy, compared with the origin package."""
    latest = _latest_keys(versions)
    origin = _origin_summary(summary, latest)
    compared = len(origin)
    differs = _origin_diff_copies(rows, latest)
    changed = _positive_copies(origin, ("scripts_added", "scripts_modified"))
    risky = _positive_copies(origin, ("new_risky_scripts",))
    flags = _copies_by_flag(rows, latest)
    numbers: dict[str, object] = {
        "file_rows": len(rows),
        "summary_rows": len(summary),
        "copy_versions": int(versions.drop_duplicates(["repo", "path", "version_seq"]).shape[0])
        if not versions.empty
        else 0,
        "latest_copies": len(latest),
        "origin_compared": compared,
        "origin_differs": differs,
        "origin_differs_share": _round(differs / compared) if compared else 0.0,
        "changed_scripts": changed,
        "changed_scripts_share": _round(changed / compared) if compared else 0.0,
        "new_risky_scripts": risky,
        "new_risky_scripts_share": _round(risky / compared) if compared else 0.0,
        "missing_scripts": _list_copies(origin, "missing_scripts"),
        "hidden_scripts": _list_copies(origin, "hidden_scripts"),
        "folder_source_history": _source_copies(origin, "history"),
        "folder_source_snapshot": _source_copies(origin, "dataset_snapshot"),
        "seconds": round(elapsed, 1),
    }
    numbers.update(flags)
    return numbers


def _latest_keys(versions: pd.DataFrame) -> set[tuple[str, str, int]]:
    """``(repo, path, version_seq)`` of the highest version of each copy."""
    if versions.empty or "version_seq" not in versions.columns:
        return set()
    ordered = versions.sort_values("version_seq", kind="mergesort")
    last = ordered.groupby(["repo", "path"], sort=False).tail(1)
    return {
        (str(repo), str(path), int(seq))
        for repo, path, seq in zip(last["repo"], last["path"], last["version_seq"], strict=True)
    }


def _origin_summary(summary: pd.DataFrame, latest: set[tuple[str, str, int]]) -> pd.DataFrame:
    """Origin-target rows of the latest version of each copy."""
    if summary.empty or "target" not in summary.columns:
        return summary.iloc[0:0]
    mask = [
        target == "origin" and (str(repo), str(path), int(seq)) in latest
        for repo, path, seq, target in zip(
            summary["repo"], summary["path"], summary["version_seq"], summary["target"], strict=True
        )
    ]
    return summary.loc[mask]


def _origin_diff_copies(rows: pd.DataFrame, latest: set[tuple[str, str, int]]) -> int:
    """Latest copies whose origin folder is not identical, including a mode change."""
    if rows.empty or "target" not in rows.columns:
        return 0
    found: set[tuple[str, str]] = set()
    for record in rows.to_dict("records"):
        if record.get("target") != "origin":
            continue
        key = (
            str(record.get("repo") or ""),
            str(record.get("path") or ""),
            int(record.get("version_seq") or 0),
        )
        if key not in latest:
            continue
        changed = record.get("result") in _CHANGED_RESULTS or bool(record.get("mode_changed"))
        if changed:
            found.add((key[0], key[1]))
    return len(found)


def _positive_copies(origin: pd.DataFrame, columns: tuple[str, ...]) -> int:
    """Rows where any of ``columns`` is greater than zero."""
    if origin.empty:
        return 0
    count = 0
    for record in origin.to_dict("records"):
        if any((_as_int(record.get(name)) or 0) > 0 for name in columns):
            count += 1
    return count


def _copies_by_flag(rows: pd.DataFrame, latest: set[tuple[str, str, int]]) -> dict[str, int]:
    """Latest copies with each new risk flag on the origin target."""
    found: dict[str, set[tuple[str, str]]] = {}
    if not rows.empty and "new_risk_flags" in rows.columns:
        for record in rows.to_dict("records"):
            if record.get("target") != "origin":
                continue
            key = (
                str(record.get("repo") or ""),
                str(record.get("path") or ""),
                int(record.get("version_seq") or 0),
            )
            if key not in latest:
                continue
            for name in _flag_names(record.get("new_risk_flags")):
                found.setdefault(name, set()).add((key[0], key[1]))
    return {f"new_flag_{name}": len(copies) for name, copies in sorted(found.items())}


def _list_copies(origin: pd.DataFrame, column: str) -> int:
    """Origin rows whose list column is not empty."""
    if origin.empty or column not in origin.columns:
        return 0
    return sum(1 for value in origin[column] if _flag_names(value))


def _source_copies(origin: pd.DataFrame, source: str) -> int:
    """Origin rows with this ``folder_source``."""
    if origin.empty or "folder_source" not in origin.columns:
        return 0
    return int(origin["folder_source"].eq(source).sum())


def _risky_rows(rows: pd.DataFrame) -> pd.DataFrame:
    """File rows whose ``new_risk_flags`` list is not empty."""
    if rows.empty or "new_risk_flags" not in rows.columns:
        return rows.iloc[0:0]
    mask = [_flag_names(value) != [] for value in rows["new_risk_flags"]]
    return rows.loc[mask]


if __name__ == "__main__":
    main()
