"""Stage 4a: turn registry SKILL.md history into lineages.

What: a lineage is one skill folder of one registry, followed through renames.
Its versions are numbered: a new version only when the normalised text changes.
A copy inside a registry starts its own lineage and names its source
(``copied_from``). A skill whose folder was deleted is marked removed. A lineage
whose newest text is not English is dropped.

Copiers are left out here, once (card G10 rule 0). Only registries whose stage 3c
``registry_kind`` is in ``LINEAGE_REGISTRY_KINDS`` (``source``, ``unclear``)
become lineages. Every later stage reads these tables, so none filters again.

Inputs (always the full data folder, also with ``--dev``):
``registry_history/registry_history_{changes,texts,status}.parquet`` (stage 3),
``registry_kinds.parquet`` (stage 3c), ``registries.parquet`` or
``dev_registries.parquet`` (stage 2c). There is no dev registry history:
``--dev`` reads the full tables limited to the 30 dev registries.
Outputs (in the dev or full data folder): ``lineages.parquet``,
``lineage_versions.parquet``, ``lineage_changes.parquet``,
``lineages_dropped.parquet``.

The changes table has 7.9M rows and the texts 20 GB, so the rows are read in
DuckDB (with ``HISTORY_DUCKDB_MEMORY_LIMIT``), only ``SKILL.md`` rows of kept
registries reach pandas, and texts are read about ``LINEAGE_TEXT_BATCH`` blobs at
a time, one batch of whole lineages after another (steps 3, 5 and 6).

Stage 4b (card G11, steps 8-10) runs in the same command: a package is the
skill folder (``SKILL.md`` plus its files). A new package version starts at
each commit where the folder's files or the text version change (Algorithm
A21). Outputs: ``lineage_package_versions.parquet`` and
``lineage_folder_manifests.parquet``. Each stage skips when its own outputs
exist; a stage 4a rebuild rebuilds stage 4b too.

Run (no network):
    python -m skill_genealogy.build_registry_lineages --dev
    python -m skill_genealogy.build_registry_lineages --force
    python -m skill_genealogy.build_registry_lineages --packages-only --force   # stage 4b only
"""

from __future__ import annotations

import hashlib
import itertools
import posixpath
import statistics
import time
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from skill_genealogy import settings
from skill_genealogy.compare_texts import (
    TextChange,
    compare_front_matter,
    compare_texts,
    file_category,
    is_skipped_path,
)
from skill_genealogy.download_registry_history import read_registry_list
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
from skill_genealogy.text_helpers import (
    detect_language,
    language_sample_and_size,
    normalize_text,
    split_front_matter,
)

# The folder's own SKILL.md, in any letter case (stage 3 lists it that way).
SKILL_MD_NAME = "skill.md"
# Rows that can be versions of the folder's own skill (rule 1). copy_source rows
# are outside rows: rule 2b reads them, they never become versions.
VERSION_ROLES: tuple[str, ...] = ("own", "former_folder")
KNOWN_ROLES: tuple[str, ...] = ("own", "former_folder", "copy_source")
# Same rule as text_helpers.is_english: "unknown" (too short or unclear) counts.
ENGLISH_CODES = frozenset({"en", "unknown"})
# A delete sorts before an add in the same commit, so a same-commit re-add wins.
_DELETE = "D"

LINEAGE_COLUMNS: list[str] = [
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
VERSION_COLUMNS: list[str] = [
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
TEXT_CHANGE_FIELDS: list[str] = [item.name for item in fields(TextChange)]
FRONT_MATTER_FIELDS: list[str] = [
    "name_changed",
    "description_changed",
    "fm_keys_added",
    "fm_keys_removed",
    "fm_keys_changed",
    "allowed_tools_added",
    "allowed_tools_removed",
    "permissions_changed",
]
CHANGE_COLUMNS: list[str] = [
    "lineage_id",
    "from_seq",
    "to_seq",
    "release_date",
    *TEXT_CHANGE_FIELDS,
    *FRONT_MATTER_FIELDS,
]
DROPPED_COLUMNS: list[str] = ["lineage_id", "reason"]
_UTC = pa.timestamp("ns", tz="UTC")
_TEXT_LIST = pa.list_(pa.string())
# Arrow type of each lineage_changes column. Batches are appended to one file,
# so the type cannot be guessed from a batch (an empty list has no type).
_CHANGE_TYPES: dict[str, pa.DataType] = {
    "lineage_id": pa.string(),
    "from_seq": pa.int64(),
    "to_seq": pa.int64(),
    "release_date": _UTC,
    "lines_added": pa.int64(),
    "lines_removed": pa.int64(),
    "body_change_pct": pa.float64(),
    "share_of_old_lines_kept": pa.float64(),
    "headings_added": _TEXT_LIST,
    "headings_removed": _TEXT_LIST,
    "commands_added": _TEXT_LIST,
    "scripts_mentioned_added": _TEXT_LIST,
    "network_refs_added": _TEXT_LIST,
    "steps_added": pa.int64(),
    "names_added": _TEXT_LIST,
    "state": pa.string(),
    "name_changed": pa.bool_(),
    "description_changed": pa.bool_(),
    "fm_keys_added": _TEXT_LIST,
    "fm_keys_removed": _TEXT_LIST,
    "fm_keys_changed": _TEXT_LIST,
    "allowed_tools_added": _TEXT_LIST,
    "allowed_tools_removed": _TEXT_LIST,
    "permissions_changed": pa.bool_(),
}
CHANGE_SCHEMA = pa.schema([(name, _CHANGE_TYPES[name]) for name in CHANGE_COLUMNS])

# Stage 4b (card G11): package versions and folder manifests.
PACKAGE_COLUMNS: list[str] = [
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
MANIFEST_SCHEMA = pa.schema(
    [
        ("lineage_id", pa.string()),
        ("package_version_seq", pa.int64()),
        ("path", pa.string()),
        ("sha", pa.string()),
        ("mode", pa.string()),
        ("size", pa.int64()),
        ("file_category", pa.string()),
    ]
)
MANIFEST_COLUMNS: list[str] = MANIFEST_SCHEMA.names
# Inside one commit: deletes first, then renames, then adds and edits (A21).
_APPLY_ORDER: dict[str, int] = {"D": 0, "R": 1}

# Query Q4.1, see code_flow/07_registry_lineages.md
# Every SKILL.md row (own, former_folder, copy_source, nested) of the kept
# registries. kept_repos is a registered one-column table. commit_date stays text
# here and is parsed as a UTC timestamp in pandas (_utc_times).
SKILL_MD_CHANGES_SQL = """
SELECT repo, skill_folder, file_path, "commit", commit_date, commit_seq, status,
       old_sha, new_sha, repo_path, old_repo_path, history_role
FROM read_parquet(?)
WHERE is_skill_md AND repo IN (SELECT repo FROM kept_repos)
"""
# Query Q4.2, see code_flow/07_registry_lineages.md
# The texts of one batch of blobs. wanted_blobs is a registered one-column table.
SKILL_TEXTS_SQL = """
SELECT sha, text
FROM read_parquet(?)
WHERE sha IN (SELECT sha FROM wanted_blobs)
"""
# Query Q4.3, see code_flow/08_package_versions.md
# Rows per registry of the kept lineages: sizes the stage 4b batches.
# kept_lineages is a registered (repo, skill_folder) table.
FOLDER_ROW_COUNTS_SQL = """
SELECT c.repo, count(*) AS row_count
FROM read_parquet(?) AS c
JOIN kept_lineages AS k ON c.repo = k.repo AND c.skill_folder = k.skill_folder
WHERE c.history_role IN ('own', 'former_folder')
GROUP BY c.repo
"""
# Query Q4.4, see code_flow/08_package_versions.md
# Every own and former-folder row (SKILL.md and folder files) of the kept
# lineages of one batch of registries. copy_source rows belong to the source.
FOLDER_CHANGES_SQL = """
SELECT k.lineage_id, c.skill_folder, c.file_path, c."commit", c.commit_date, c.commit_seq,
       c.status, c.new_sha, c.new_mode, c.repo_path, c.old_repo_path, c.history_role,
       c.is_skill_md
FROM read_parquet(?) AS c
JOIN kept_lineages AS k ON c.repo = k.repo AND c.skill_folder = k.skill_folder
WHERE c.history_role IN ('own', 'former_folder') AND c.repo IN (SELECT repo FROM batch_repos)
"""
# Query Q4.5, see code_flow/08_package_versions.md
# Byte size of the stored text of each wanted blob (scripts and SKILL.md are
# stored; most other files are not, and get no size).
BLOB_SIZES_SQL = """
SELECT sha, strlen(text) AS size
FROM read_parquet(?)
WHERE sha IN (SELECT sha FROM wanted_blobs) AND text IS NOT NULL
"""


# Step 0 and Step 1: read the inputs


def history_dir() -> Path:
    """``registry_history/`` in the full data folder; also used by ``--dev``."""
    return settings.data_dir(False) / "registry_history"


def output_paths(dev: bool) -> dict[str, Path]:
    """The four output files in the dev or full data folder."""
    folder = settings.data_dir(dev)
    return {
        "lineages": folder / "lineages.parquet",
        "versions": folder / "lineage_versions.parquet",
        "changes": folder / "lineage_changes.parquet",
        "dropped": folder / "lineages_dropped.parquet",
    }


def registry_scope(dev: bool) -> pd.DataFrame:
    """The registries of this run with their stage 3c kind, and which are kept.

    Rule 0. The registries are ``registries.parquet`` (or the 30 dev registries).
    ``kept`` is True when ``registry_kind`` is in ``LINEAGE_REGISTRY_KINDS``.
    A registry with no kind row is not kept (``registry_kind`` is null).

    Inputs: ``dev``. Outputs: ``repo``, ``registry_kind``, ``skill_folders``,
    ``kept``, one row per registry, sorted by repo.
    """
    kinds_file = settings.data_dir(False) / "registry_kinds.parquet"
    check_inputs_exist(
        {"registry_kinds": kinds_file},
        {"registry_kinds": "python -m skill_genealogy.label_registry_kinds"},
    )
    repos = pd.DataFrame({"repo": read_registry_list(dev)})
    kinds = read_parquet(kinds_file).loc[:, ["repo", "registry_kind", "skill_folders"]]
    scope = repos.merge(kinds, on="repo", how="left")
    scope["skill_folders"] = scope["skill_folders"].fillna(0).astype(int)
    scope["kept"] = scope["registry_kind"].isin(settings.LINEAGE_REGISTRY_KINDS)
    return scope.sort_values("repo", kind="mergesort").reset_index(drop=True)


def read_skill_md_changes(dev: bool) -> pd.DataFrame:
    """Read the ``SKILL.md`` change rows of the kept registries (rules 0 and 1).

    Query Q4.1 on the full stage 3 table, also with ``dev`` (then limited to the
    dev registries). Copiers are left out. Nested files (``cn/SKILL.md``) and
    ``copy_source`` rows stay in the frame: later steps need them.

    Inputs: ``dev``. Outputs: one row per ``SKILL.md`` change.
    Raises ``FileNotFoundError`` when an input is missing.
    """
    changes_file = history_dir() / "registry_history_changes.parquet"
    check_inputs_exist(
        {"registry_history_changes": changes_file},
        {"registry_history_changes": "python -m skill_genealogy.download_registry_history"},
    )
    scope = registry_scope(dev)
    return read_skill_md_rows(changes_file, scope.loc[scope["kept"], "repo"].tolist())


def read_skill_md_rows(changes_file: Path, repos: list[str]) -> pd.DataFrame:
    """Query Q4.1 on an explicit file and registry list (``read_skill_md_changes`` calls it)."""
    con = connect_duckdb()
    try:
        con.execute(f"SET memory_limit = '{settings.HISTORY_DUCKDB_MEMORY_LIMIT}'")
        con.register("kept_repos", pd.DataFrame({"repo": pd.Series(repos, dtype="string")}))
        return con.execute(SKILL_MD_CHANGES_SQL, [str(changes_file)]).df()
    finally:
        con.close()


def read_texts(texts_file: Path, blobs: list[str]) -> dict[str, str]:
    """Query Q4.2: ``sha -> text`` for the given blobs only (one batch).

    A blob that is not in the texts table is missing from the dict.
    """
    con = connect_duckdb()
    try:
        con.execute(f"SET memory_limit = '{settings.HISTORY_DUCKDB_MEMORY_LIMIT}'")
        con.register("wanted_blobs", pd.DataFrame({"sha": pd.Series(blobs, dtype="string")}))
        rows = con.execute(SKILL_TEXTS_SQL, [str(texts_file)]).fetchall()
    finally:
        con.close()
    return {str(sha): text for sha, text in rows if isinstance(text, str)}


# Step 2: join renames and copies


# Algorithm A50, see code_flow/07_registry_lineages.md
def join_renamed_folders(changes: pd.DataFrame) -> pd.DataFrame:
    """Add ``lineage_id``, ``former_paths`` and ``copied_from`` to every row.

    ``lineage_id`` is ``repo::skill_folder``, the HEAD folder stage 3 filed the
    rows under. ``former_paths`` lists the older folders of the skill, oldest
    first: the folder of each ``former_folder`` row's ``repo_path``, and the
    folder of ``old_repo_path`` of each rename (``R``) of the skill's own
    ``SKILL.md``. ``copied_from`` is the lineage of the newest ``copy_source``
    ``SKILL.md`` row before version 1 (rule 2b), or None.

    Inputs: ``SKILL.md`` rows. Outputs: the same rows with the three columns.

    Example:
    >>> rows = pd.DataFrame([_example_row(history_role="former_folder",
    ...     repo_path="skills/old/SKILL.md")])
    >>> joined = join_renamed_folders(rows)
    >>> joined.loc[0, "lineage_id"], joined.loc[0, "former_paths"]
    ('acme/copy::skills/new', ['skills/old'])
    """
    frame = changes.drop(
        columns=[name for name in ("lineage_id", "former_paths", "copied_from") if name in changes]
    ).reset_index(drop=True)
    frame["lineage_id"] = frame["repo"].astype(str) + "::" + frame["skill_folder"].astype(str)
    frame["commit_time"] = _utc_times(frame["commit_date"])
    former = _former_paths_per_lineage(frame)
    copied = _copied_from_per_lineage(frame)
    frame["former_paths"] = frame["lineage_id"].map(former)
    frame["former_paths"] = [
        value if isinstance(value, list) else [] for value in frame["former_paths"]
    ]
    frame["copied_from"] = frame["lineage_id"].map(copied).astype(object)
    frame["copied_from"] = frame["copied_from"].where(frame["copied_from"].notna(), None)
    return frame


def _former_paths_per_lineage(frame: pd.DataFrame) -> dict[str, list[str]]:
    """Older folder names per lineage, in date order (rule 2, chains included)."""
    top = _is_top_skill_md(frame["file_path"])
    role = frame["history_role"].astype(str)
    from_former = frame.loc[top & (role == "former_folder")].assign(
        folder=lambda part: part["repo_path"].map(parent_folder), step=1
    )
    renamed = top & role.isin(VERSION_ROLES) & (frame["status"] == "R")
    renamed &= frame["old_repo_path"].fillna("").map(_file_name).str.lower() == SKILL_MD_NAME
    from_rename = frame.loc[renamed].assign(
        folder=lambda part: part["old_repo_path"].map(parent_folder), step=0
    )
    found = pd.concat([from_former, from_rename], ignore_index=True)
    found = found.loc[found["folder"] != found["skill_folder"].astype(str)]
    if found.empty:
        return {}
    found = found.sort_values(["lineage_id", "commit_time", "commit_seq", "step"], kind="mergesort")
    found = found.drop_duplicates(["lineage_id", "folder"], keep="first")
    return {key: list(group) for key, group in found.groupby("lineage_id", sort=False)["folder"]}


def copy_rows_before_version_one(frame: pd.DataFrame) -> pd.DataFrame:
    """``copy_source`` SKILL.md rows strictly before the lineage's first text row.

    Rule 2b. These are the outside rows that name a copy's source; every other
    outside row (later ``copy_source`` rows, nested ``cn/SKILL.md``) is ignored.
    The first text row is the first own or former-folder ``SKILL.md`` row that is
    not a delete, by UTC time then ``commit_seq``.
    """
    top = _is_top_skill_md(frame["file_path"])
    first = _text_rows(frame).sort_values(["commit_time", "commit_seq"], kind="mergesort")
    first = first.drop_duplicates("lineage_id").loc[:, ["lineage_id", "commit_time", "commit_seq"]]
    first = first.rename(columns={"commit_time": "first_time", "commit_seq": "first_seq"})
    sources = frame.loc[top & (frame["history_role"] == "copy_source")]
    sources = sources.merge(first, on="lineage_id", how="inner")
    before = (sources["commit_time"] < sources["first_time"]) | (
        (sources["commit_time"] == sources["first_time"])
        & (sources["commit_seq"] < sources["first_seq"])
    )
    return sources.loc[before]


def _copied_from_per_lineage(frame: pd.DataFrame) -> dict[str, str]:
    """Source lineage per copied lineage: newest copy_source row before version 1."""
    sources = copy_rows_before_version_one(frame)
    sources = sources.sort_values(["lineage_id", "commit_time", "commit_seq"], kind="mergesort")
    newest = sources.groupby("lineage_id", sort=False).tail(1)
    folders = newest["repo_path"].map(parent_folder)
    keep = folders != newest["skill_folder"].astype(str)
    return dict(
        zip(
            newest.loc[keep, "lineage_id"],
            newest.loc[keep, "repo"] + "::" + folders[keep],
            strict=True,
        )
    )


# Step 3: number text versions


def one_row_per_commit(changes: pd.DataFrame) -> pd.DataFrame:
    """The rows that can give a version, at most one per commit, in time order.

    Rule 1 and rule 3. A row can give a version when it is the folder's own
    ``SKILL.md`` (no ``/`` in ``file_path``), its role is ``own`` or
    ``former_folder``, and it carries a text (not a delete). Parallel copies put
    several such rows in one commit: the ``own`` row is kept, else the
    ``former_folder`` row whose ``repo_path`` is first alphabetically. Rows are
    ordered by UTC commit time, then ``commit_seq``.

    Inputs: rows from ``join_renamed_folders``. Outputs: the chosen rows.

    Example:
    >>> rows = pd.DataFrame([
    ...     _example_row(history_role="former_folder", repo_path="plugins/p/skills/new/SKILL.md"),
    ...     _example_row(new_sha="own-blob")])
    >>> one_row_per_commit(join_renamed_folders(rows))["new_sha"].tolist()
    ['own-blob']
    """
    rows = _text_rows(changes)
    rows = rows.assign(role_rank=(rows["history_role"] != "own").astype(int))
    rows = rows.sort_values(
        ["lineage_id", "commit_time", "commit_seq", "role_rank", "repo_path"], kind="mergesort"
    )
    return rows.drop_duplicates(["lineage_id", "commit"], keep="first").drop(columns="role_rank")


# Algorithm A20, see code_flow/07_registry_lineages.md
def number_text_versions(changes: pd.DataFrame, texts: dict[str, str]) -> pd.DataFrame:
    """Number the texts of each lineage; a version is a new normalised text.

    Rows come from ``one_row_per_commit``. The text is the ``new_sha`` blob; the
    ``old_sha`` chain is not followed (a merge can name an older blob). The
    first row with a stored text is version 1, whatever its status.
    ``normalized_sha`` is the SHA-1 of ``normalize_text(text)`` without blank
    lines (``normalized_sha``). ``version_seq`` goes up only when it differs
    from the previous version, so a whitespace-only commit, blank lines
    included, is not a version. A text that returns gets a new ``version_seq`` and
    ``same_as_seq`` = the first version with that text. ``release_date`` is the
    first commit with that version. ``body_chars`` is the length of the
    normalised body without front matter, as in stage 2a.

    Inputs: rows from ``join_renamed_folders``, ``texts`` (sha -> raw text).
    Outputs: one row per version, ``VERSION_COLUMNS`` without ``language``.

    Example:
    >>> rows = pd.DataFrame([_example_row(new_sha="a", commit="c1", commit_seq=1),
    ...     _example_row(new_sha="b", commit="c2", commit_seq=2)])
    >>> versions = number_text_versions(join_renamed_folders(rows), {"a": "Hi.\\n", "b": "Hi. \\n"})
    >>> versions["version_seq"].tolist()
    [1]
    """
    records: list[dict] = []
    for lineage_id, group in one_row_per_commit(changes).groupby("lineage_id", sort=False):
        records.extend(_versions_of_lineage(str(lineage_id), group, texts))
    columns = [name for name in VERSION_COLUMNS if name != "language"]
    versions = pd.DataFrame(records, columns=columns)
    versions["same_as_seq"] = versions["same_as_seq"].astype("Int64")
    return versions


def _versions_of_lineage(lineage_id: str, group: pd.DataFrame, texts: dict[str, str]) -> list[dict]:
    """Version records of one lineage (rows already in time order)."""
    first_seq: dict[str, int] = {}
    records: list[dict] = []
    for row in group.itertuples(index=False):
        text = texts.get(str(row.new_sha))
        if text is None:
            continue
        digest = normalized_sha(text)
        if records and records[-1]["normalized_sha"] == digest:
            continue
        seq = len(records) + 1
        records.append(
            {
                "lineage_id": lineage_id,
                "version_seq": seq,
                "same_as_seq": first_seq.get(digest),
                "blob": str(row.new_sha),
                "normalized_sha": digest,
                "commit": str(row.commit),
                "commit_date": row.commit_time,
                "release_date": row.commit_time,
                "body_chars": language_sample_and_size(text)[1],
            }
        )
        first_seq.setdefault(digest, seq)
    return records


# Step 4: mark removed lineages


def mark_removed_lineages(changes: pd.DataFrame) -> pd.DataFrame:
    """Mark a lineage whose own ``SKILL.md`` ends with a delete (rule 4).

    The rows are the folder's own ``SKILL.md`` rows (role ``own``), in time
    order; in one commit a delete sorts before an add. When the last one is a
    delete (``D``), no add came later: ``registry_removed`` is True and
    ``removed_at`` is that commit's time. ``former_folder`` rows do not decide:
    a moved skill lives on in the folder it moved to, and a deleted parallel
    copy is not the skill.

    Inputs: rows from ``join_renamed_folders``.
    Outputs: ``lineage_id``, ``registry_removed``, ``removed_at``, one row per
    lineage with an own ``SKILL.md`` row.

    Example:
    >>> rows = pd.DataFrame([_example_row(status="A", commit="c1"),
    ...     _example_row(status="D", commit="c2", commit_seq=2,
    ...                  commit_date="2026-02-01T00:00:00Z")])
    >>> mark_removed_lineages(join_renamed_folders(rows))["registry_removed"].tolist()
    [True]
    """
    own = changes.loc[_is_top_skill_md(changes["file_path"]) & (changes["history_role"] == "own")]
    own = own.assign(add_rank=(own["status"] != _DELETE).astype(int))
    own = own.sort_values(["lineage_id", "commit_time", "commit_seq", "add_rank"], kind="mergesort")
    last = own.groupby("lineage_id", sort=False).tail(1)
    removed = last["status"] == _DELETE
    return pd.DataFrame(
        {
            "lineage_id": last["lineage_id"],
            "registry_removed": removed,
            "removed_at": last["commit_time"].where(removed),
        }
    ).reset_index(drop=True)


# Step 5: language


def label_lineage_language(versions: pd.DataFrame, texts: dict[str, str]) -> pd.DataFrame:
    """Add ``language`` to each version with ``detect_language`` (rule 5).

    One call per normalised text: the same ``normalized_sha`` gets the same
    label. The code is ``en``, ``unknown``, ``other`` or an ISO 639-1 code.

    Inputs: rows from ``number_text_versions``, ``texts`` (sha -> raw text).
    Outputs: the same rows plus ``language``.

    Example:
    >>> versions = pd.DataFrame({"blob": ["a"], "normalized_sha": ["n"]})
    >>> texts = {"a": "这是一个中文技能，用来检查文件并写出简短报告和每个步骤。"}
    >>> label_lineage_language(versions, texts)["language"].tolist()
    ['zh']
    """
    labels: dict[str, str] = {}
    languages: list[str] = []
    for blob, digest in zip(versions["blob"], versions["normalized_sha"], strict=True):
        if digest not in labels:
            labels[digest] = detect_language(texts.get(str(blob), ""))
        languages.append(labels[digest])
    return versions.assign(language=pd.Series(languages, index=versions.index, dtype=object))


def split_by_language(
    lineage_ids: list[str], versions: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Keep English lineages; list the others with a reason (rules 1 and 5).

    A lineage is English when its newest version's language is in
    ``ENGLISH_CODES``. A lineage with no version (only deletes, or no stored
    text) is dropped as ``no_text``; one whose newest text is not English as
    ``not_english``.

    Inputs: every lineage id of the batch, labelled versions.
    Outputs: (versions of English lineages, dropped rows ``lineage_id, reason``).
    """
    newest = versions.sort_values(["lineage_id", "version_seq"]).groupby("lineage_id").tail(1)
    language = dict(zip(newest["lineage_id"], newest["language"], strict=True))
    dropped = [
        {
            "lineage_id": lineage_id,
            "reason": "no_text" if lineage_id not in language else "not_english",
        }
        for lineage_id in lineage_ids
        if language.get(lineage_id) not in ENGLISH_CODES
    ]
    dropped_frame = pd.DataFrame(dropped, columns=DROPPED_COLUMNS)
    kept = versions.loc[~versions["lineage_id"].isin(set(dropped_frame["lineage_id"]))]
    return kept.reset_index(drop=True), dropped_frame


# Step 6: compare consecutive versions


def compare_consecutive_versions(versions: pd.DataFrame, texts: dict[str, str]) -> pd.DataFrame:
    """One row per step from version n to version n+1 of a lineage (rule 6).

    ``compare_texts`` gives the body fields (``TextChange``) and
    ``compare_front_matter`` the header fields. ``state`` is
    ``front_matter_only`` when only the front matter changed (``step_state``).
    ``release_date`` is the newer version's. A lineage with one version has no
    row. List fields hold text.

    Inputs: versions of kept lineages, ``texts`` (sha -> raw text).
    Outputs: ``CHANGE_COLUMNS``.
    """
    records: list[dict] = []
    ordered = versions.sort_values(["lineage_id", "version_seq"], kind="mergesort")
    for lineage_id, group in ordered.groupby("lineage_id", sort=False):
        repo, folder = str(lineage_id).split("::", 1)
        path = "SKILL.md" if folder == "." else f"{folder}/SKILL.md"
        steps = group.to_dict("records")
        for older, newer in zip(steps, steps[1:], strict=False):
            old_text = texts.get(str(older["blob"]), "")
            new_text = texts.get(str(newer["blob"]), "")
            record = {
                "lineage_id": lineage_id,
                "from_seq": int(older["version_seq"]),
                "to_seq": int(newer["version_seq"]),
                "release_date": newer["release_date"],
            }
            record.update(compare_texts(old_text, new_text, repo=repo, path=path).to_dict())
            record.update(compare_front_matter(old_text, new_text))
            record["state"] = step_state(str(record["state"]), old_text, new_text)
            records.append(_text_lists(record))
    return pd.DataFrame(records, columns=CHANGE_COLUMNS)


# Steps 3, 5 and 6, one batch of whole lineages at a time


def assign_text_batches(joined: pd.DataFrame, limit: int) -> dict[str, int]:
    """Batch number per lineage so a batch needs at most about ``limit`` blobs.

    Lineages are taken in id order. A lineage is never split, so one lineage
    with more than ``limit`` blobs is a batch of its own.

    Example:
    >>> rows = pd.DataFrame([_example_row(new_sha="a"),
    ...     _example_row(skill_folder="skills/b", repo_path="skills/b/SKILL.md", new_sha="b")])
    >>> assign_text_batches(join_renamed_folders(rows), 1)
    {'acme/copy::skills/b': 0, 'acme/copy::skills/new': 1}
    """
    counts = _text_rows(joined).groupby("lineage_id")["new_sha"].nunique()
    every = sorted(set(joined["lineage_id"]))
    batches: dict[str, int] = {}
    batch, used = 0, 0
    for lineage_id in every:
        blobs = int(counts.get(lineage_id, 0))
        if used and used + blobs > limit:
            batch, used = batch + 1, 0
        batches[lineage_id] = batch
        used += blobs
    return batches


def build_versions_in_batches(
    joined: pd.DataFrame, texts_file: Path, changes_file: Path, limit: int
) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Steps 3, 5 and 6 for every batch; ``lineage_changes`` is written as it goes.

    Inputs: rows from ``join_renamed_folders``, the stage 3 texts file, the
    changes output file, and the blob limit per batch.
    Outputs: (kept versions with language, dropped lineages, change rows written).
    """
    batch_of = joined["lineage_id"].map(assign_text_batches(joined, limit))
    batch_count = int(batch_of.max()) + 1 if len(batch_of) else 0
    versions_parts, dropped_parts, written = [], [], 0
    started = time.perf_counter()
    with pq.ParquetWriter(changes_file, CHANGE_SCHEMA, compression="zstd") as writer:
        for batch, rows in joined.groupby(batch_of, sort=True):
            texts = read_texts(texts_file, one_row_per_commit(rows)["new_sha"].unique().tolist())
            # Step 3: Number text versions (Algorithm A20)
            versions = number_text_versions(rows, texts)
            # Step 5: Label language and drop non-English lineages
            versions = label_lineage_language(versions, texts)
            kept, dropped = split_by_language(sorted(set(rows["lineage_id"])), versions)
            # Step 6: Compare consecutive versions
            changes = compare_consecutive_versions(kept, texts)
            writer.write_table(
                pa.Table.from_pandas(changes, schema=CHANGE_SCHEMA, preserve_index=False)
            )
            versions_parts.append(kept)
            dropped_parts.append(dropped)
            written += len(changes)
            print(
                f"progress batch {int(batch) + 1}/{batch_count}: "
                f"{rows['lineage_id'].nunique()} lineages, "
                f"{len(texts)} texts, {len(kept)} versions kept, {len(changes)} changes, "
                f"{time.perf_counter() - started:.0f} s",
                flush=True,
            )
    versions = _concat(versions_parts, VERSION_COLUMNS)
    return versions, _concat(dropped_parts, DROPPED_COLUMNS), written


# Step 7: lineage table, save, summary


def build_lineage_table(
    joined: pd.DataFrame, versions: pd.DataFrame, removed: pd.DataFrame
) -> pd.DataFrame:
    """One row per kept lineage (``LINEAGE_COLUMNS``).

    ``first_date`` is version 1's release. ``last_date`` is the newest
    version's release, or ``removed_at`` when the skill was removed later.
    ``newest_language`` is the newest version's language.
    """
    identity = joined.drop_duplicates("lineage_id").loc[
        :, ["lineage_id", "repo", "skill_folder", "former_paths", "copied_from"]
    ]
    ordered = versions.sort_values(["lineage_id", "version_seq"], kind="mergesort")
    per_lineage = ordered.groupby("lineage_id", sort=False).agg(
        first_date=("release_date", "first"),
        newest_date=("release_date", "last"),
        version_count=("version_seq", "size"),
        newest_language=("language", "last"),
    )
    table = identity.merge(per_lineage.reset_index(), on="lineage_id", how="inner")
    table = table.merge(removed, on="lineage_id", how="left")
    table["registry_removed"] = table["registry_removed"].fillna(False).astype(bool)
    later = table["registry_removed"] & (table["removed_at"] > table["newest_date"])
    table["last_date"] = table["removed_at"].where(later, table["newest_date"])
    table["removed_at"] = table["removed_at"].where(table["registry_removed"])
    table["is_english"] = table["newest_language"].isin(ENGLISH_CODES)
    return table.loc[:, LINEAGE_COLUMNS].sort_values("lineage_id").reset_index(drop=True)


def build_registry_lineages(
    changes: pd.DataFrame, texts_file: Path, paths: dict[str, Path], limit: int
) -> dict[str, object]:
    """Steps 2–7 on explicit inputs (``main`` and the tests call this).

    Inputs: rows from ``read_skill_md_changes``, the stage 3 texts file, the
    four output paths, and the blob limit per batch.
    Outputs: the four files; returns the frames and counts the summary needs.
    """
    # Step 2: Join renames and copies (Algorithm A50)
    joined = join_renamed_folders(changes)
    # Step 4: Mark removed lineages
    removed = mark_removed_lineages(joined)
    # Steps 3, 5 and 6: one batch of whole lineages at a time
    part_file = paths["changes"].with_name(paths["changes"].name + ".part")
    versions, dropped, change_rows = build_versions_in_batches(joined, texts_file, part_file, limit)
    # Step 7: Lineage table and save
    lineages = build_lineage_table(joined, versions, removed)
    write_parquet(
        versions.loc[:, VERSION_COLUMNS], paths["versions"], sort_by=["lineage_id", "version_seq"]
    )
    write_parquet(dropped, paths["dropped"], sort_by=["lineage_id"])
    write_parquet(lineages, paths["lineages"], sort_by=["lineage_id"])
    part_file.replace(paths["changes"])
    return {
        "joined": joined,
        "versions": versions,
        "dropped": dropped,
        "lineages": lineages,
        "change_rows": change_rows,
    }


def summarize_lineages(
    scope: pd.DataFrame, built: dict[str, object], status: pd.DataFrame
) -> dict[str, object]:
    """The counts card G10 rule 7 asks for (copiers, roles, lineages, versions)."""
    joined, lineages = built["joined"], built["lineages"]
    versions, dropped = built["versions"], built["dropped"]
    copiers = scope.loc[~scope["kept"]]
    roles = joined["history_role"].fillna("missing").value_counts()
    per_registry = lineages.groupby("repo").size()
    per_lineage = lineages["version_count"]
    numbers: dict[str, object] = {
        "registries": len(scope),
        "registries_kept": int(scope["kept"].sum()),
        "copier_registries_skipped": int((copiers["registry_kind"] == "copier").sum()),
        "copier_folders_skipped": int(
            copiers.loc[copiers["registry_kind"] == "copier", "skill_folders"].sum()
        ),
        "registries_without_kind": int(copiers["registry_kind"].isna().sum()),
        "skill_md_rows": len(joined),
    }
    for role in (*KNOWN_ROLES, "missing"):
        numbers[f"role_{role}"] = int(roles.get(role, 0))
    numbers["role_other"] = int(roles.drop(list(KNOWN_ROLES) + ["missing"], errors="ignore").sum())
    numbers["lineages_read"] = int(joined["lineage_id"].nunique())
    numbers["lineages_kept"] = len(lineages)
    numbers["lineages_per_registry_median"] = _median(per_registry)
    numbers["lineages_per_registry_max"] = _max_with_name(per_registry)
    numbers["versions"] = len(versions)
    numbers["versions_per_lineage_median"] = _median(per_lineage)
    numbers["versions_per_lineage_max"] = _max_with_name(
        lineages.set_index("lineage_id")["version_count"]
    )
    numbers["parallel_commits_collapsed"] = count_parallel_commits(joined)
    numbers["same_as_seq_set"] = int(versions["same_as_seq"].notna().sum())
    numbers["change_rows"] = built["change_rows"]
    numbers.update(_former_and_copy_counts(joined, lineages))
    numbers["registry_removed"] = int(lineages["registry_removed"].sum())
    for reason in ("not_english", "no_text"):
        numbers[f"dropped_{reason}"] = int((dropped["reason"] == reason).sum())
    for code, count in lineages["newest_language"].value_counts().items():
        numbers[f"newest_language_{code}"] = int(count)
    numbers.update(_follow_renames_share(status, lineages))
    return numbers


def count_parallel_commits(joined: pd.DataFrame) -> int:
    """(Lineage, commit) pairs with more than one text row, collapsed to one version.

    Example:
    >>> rows = pd.DataFrame([_example_row(),
    ...     _example_row(history_role="former_folder", repo_path="plugins/p/skills/new/SKILL.md")])
    >>> count_parallel_commits(join_renamed_folders(rows))
    1
    """
    sizes = _text_rows(joined).groupby(["lineage_id", "commit"]).size()
    return int((sizes > 1).sum())


# Steps 8-10 (stage 4b, card G11): package versions and folder manifests


def package_output_paths(dev: bool) -> dict[str, Path]:
    """The two stage 4b output files in the dev or full data folder."""
    folder = settings.data_dir(dev)
    return {
        "packages": folder / "lineage_package_versions.parquet",
        "manifests": folder / "lineage_folder_manifests.parquet",
    }


def lineage_keys(lineages: pd.DataFrame) -> pd.DataFrame:
    """``lineage_id``, ``repo``, ``skill_folder`` of the kept lineages, as text columns."""
    keys = lineages.loc[:, ["lineage_id", "repo", "skill_folder"]].astype(str)
    return keys.astype("string").reset_index(drop=True)


def count_folder_rows(changes_file: Path, keys: pd.DataFrame) -> pd.Series:
    """Query Q4.3: own and former-folder rows per registry of the kept lineages."""
    con = connect_duckdb()
    try:
        con.execute(f"SET memory_limit = '{settings.HISTORY_DUCKDB_MEMORY_LIMIT}'")
        con.register("kept_lineages", keys)
        counts = con.execute(FOLDER_ROW_COUNTS_SQL, [str(changes_file)]).df()
    finally:
        con.close()
    return counts.set_index("repo")["row_count"].sort_index()


def read_folder_changes(changes_file: Path, keys: pd.DataFrame, repos: list[str]) -> pd.DataFrame:
    """Query Q4.4: every own and former-folder row of the kept lineages of ``repos``.

    Both ``SKILL.md`` rows (they place the folder and say whether the skill is
    there) and folder-file rows. ``copy_source`` rows are left out: they are
    the history of the source skill, which has its own lineage.
    """
    con = connect_duckdb()
    try:
        con.execute(f"SET memory_limit = '{settings.HISTORY_DUCKDB_MEMORY_LIMIT}'")
        con.register("kept_lineages", keys)
        con.register("batch_repos", pd.DataFrame({"repo": pd.Series(repos, dtype="string")}))
        return con.execute(FOLDER_CHANGES_SQL, [str(changes_file)]).df()
    finally:
        con.close()


def read_blob_sizes(texts_file: Path, blobs: list[str]) -> dict[str, int]:
    """Query Q4.5: ``sha -> bytes`` of the stored text, for the blobs that have one.

    Stage 3 stores the text of ``SKILL.md`` files and of scripts up to
    ``MAX_SCRIPT_BYTES``; other files have no stored text and get no size.
    """
    if not blobs:
        return {}
    con = connect_duckdb()
    try:
        con.execute(f"SET memory_limit = '{settings.HISTORY_DUCKDB_MEMORY_LIMIT}'")
        con.register("wanted_blobs", pd.DataFrame({"sha": pd.Series(blobs, dtype="string")}))
        rows = con.execute(BLOB_SIZES_SQL, [str(texts_file)]).fetchall()
    finally:
        con.close()
    return {str(sha): int(size) for sha, size in rows}


def assign_package_batches(row_counts: pd.Series, limit: int) -> list[list[str]]:
    """Registries per batch, in name order, so a batch reads about ``limit`` rows.

    A registry is never split: one with more than ``limit`` rows is a batch of
    its own.

    Example:
    >>> assign_package_batches(pd.Series({"a/x": 3, "b/y": 3, "c/z": 9}), 6)
    [['a/x', 'b/y'], ['c/z']]
    """
    batches: list[list[str]] = []
    used = 0
    for repo, count in row_counts.sort_index().items():
        if not batches or (used and used + int(count) > limit):
            batches.append([])
            used = 0
        batches[-1].append(str(repo))
        used += int(count)
    return batches


# Step 8: rebuild folder manifests


# Algorithm A21, see code_flow/08_package_versions.md
def rebuild_folder_manifests(folder_changes: pd.DataFrame) -> pd.DataFrame:
    """Replay each lineage's file changes; the folder manifest after every commit.

    Walk a lineage's commits oldest first (UTC time, then ``commit_seq``) and
    apply each row to a running map ``repo_path -> (sha, mode)``: ``A``, ``M``
    and ``T`` set the path, ``D`` removes it, ``R`` moves ``old_repo_path`` to
    ``repo_path``. In one commit deletes go first, then renames, then the rest.
    The map is keyed by the full repo path, so parallel copies and older folder
    names never mix. The *skill folder at that time* is the folder of the
    newest ``SKILL.md`` text row, chosen as in A20 (``skill_folder_after_commit``).
    The manifest is the files under
    that folder, relative to it, without the folder's own ``SKILL.md``
    (it is the text version), skipped folders, and, at a repo-root skill, files
    outside ``ROOT_SKILL_PACKAGE_FOLDERS``.

    Inputs: rows from Query Q4.4 (``lineage_id``, ``commit``, ``commit_date``,
    ``commit_seq``, ``status``, ``new_sha``, ``new_mode``, ``repo_path``,
    ``old_repo_path``, ``history_role``, ``file_path``).
    Outputs: one row per lineage and commit: ``lineage_id``, ``commit``,
    ``commit_date`` (UTC), ``skill_md_present`` (the folder has its
    ``SKILL.md`` after the commit), ``files`` (sorted tuple of
    ``(path, sha, mode)``).

    Example:
    >>> rows = pd.DataFrame([
    ...     _example_row(status="A", file_path="SKILL.md", repo_path="skills/new/SKILL.md"),
    ...     _example_row(status="A", file_path="scripts/a.py",
    ...                  repo_path="skills/new/scripts/a.py", new_sha="p")])
    >>> rebuild_folder_manifests(rows)["files"].tolist()
    [(('scripts/a.py', 'p', '100644'),)]
    """
    records: list[dict] = []
    ordered = _order_for_replay(folder_changes)
    for lineage_id, group in ordered.groupby("lineage_id", sort=False):
        records.extend(_replay_lineage(str(lineage_id), group))
    columns = ["lineage_id", "commit", "commit_date", "skill_md_present", "files"]
    return pd.DataFrame(records, columns=columns)


def _order_for_replay(changes: pd.DataFrame) -> pd.DataFrame:
    """Own and former-folder rows in replay order: lineage, UTC time, ``commit_seq``,
    then delete / rename / rest. ``copy_source`` rows are the source's history: dropped."""
    frame = changes.loc[changes["history_role"].isin(VERSION_ROLES)].copy()
    if "lineage_id" not in frame:
        frame["lineage_id"] = frame["repo"].astype(str) + "::" + frame["skill_folder"].astype(str)
    frame["commit_time"] = _utc_times(frame["commit_date"])
    frame["apply_rank"] = frame["status"].fillna("").astype(str).str[:1].map(_APPLY_ORDER)
    frame["apply_rank"] = frame["apply_rank"].fillna(len(_APPLY_ORDER)).astype(int)
    order = ["lineage_id", "commit_time", "commit_seq", "commit", "apply_rank", "repo_path"]
    return frame.sort_values(order, kind="mergesort").reset_index(drop=True)


def _replay_lineage(lineage_id: str, group: pd.DataFrame) -> list[dict]:
    """Manifest after each commit of one lineage (rows already in replay order)."""
    state: dict[str, tuple[str, str]] = {}
    folder: str | None = None
    files: tuple = ()
    present = False
    records: list[dict] = []
    rows = group.itertuples(index=False)
    for commit, commit_rows in itertools.groupby(rows, key=lambda row: row.commit):
        commit_rows = list(commit_rows)
        for row in commit_rows:
            _apply_file_change(state, row)
        new_folder = skill_folder_after_commit(commit_rows, folder)
        if new_folder is not None and (
            new_folder != folder or _touches_package(commit_rows, new_folder)
        ):
            files, present = folder_manifest(state, new_folder)
        folder = new_folder
        records.append(
            {
                "lineage_id": lineage_id,
                "commit": str(commit),
                "commit_date": commit_rows[0].commit_time,
                "skill_md_present": present,
                "files": files,
            }
        )
    return records


def _apply_file_change(state: dict[str, tuple[str, str]], row: object) -> None:
    """Apply one ``A``/``M``/``T``/``D``/``R`` row to the running path map."""
    status = str(getattr(row, "status", "") or "")[:1]
    new_path = _path_text(getattr(row, "repo_path", None))
    old_path = _path_text(getattr(row, "old_repo_path", None))
    if status == _DELETE:
        state.pop(new_path, None)
        state.pop(old_path, None)
        return
    if status == "R" and old_path and old_path != new_path:
        state.pop(old_path, None)
    if new_path:
        state[new_path] = (str(row.new_sha), str(row.new_mode))


def skill_folder_after_commit(rows: list, folder: str | None) -> str | None:
    """The skill folder after a commit: where the ``SKILL.md`` that A20 reads lives.

    The commit's ``SKILL.md`` text rows (the folder's own file, not a delete)
    are chosen as in A20 (``one_row_per_commit``): an ``own`` row first, else
    the ``former_folder`` row whose ``repo_path`` comes first alphabetically.
    The folder is that row's ``repo_path`` folder. A commit without such a row
    keeps the folder. The ``repo_path`` decides, not the HEAD folder: stage 3
    can file a skill's older root history as ``own`` rows (``repo_path`` is
    ``SKILL.md``), and a skill can move away and come back.

    Example:
    >>> rows = [_example_tuple(history_role="former_folder", repo_path="b/SKILL.md"),
    ...         _example_tuple(history_role="former_folder", repo_path="a/SKILL.md")]
    >>> skill_folder_after_commit(rows, None), skill_folder_after_commit([], "x")
    ('a', 'x')
    """
    texts = [
        ((row.history_role != "own"), _path_text(row.repo_path))
        for row in rows
        if str(row.file_path or "").lower() == SKILL_MD_NAME and _carries_text(row)
    ]
    return parent_folder(min(texts)[1]) if texts else folder


def folder_manifest(state: dict[str, tuple[str, str]], folder: str) -> tuple[tuple, bool]:
    """Files of one folder in the path map, and whether its ``SKILL.md`` is there.

    Paths are made relative to ``folder``. The folder's own ``SKILL.md`` (any
    letter case) is not a file of the manifest; ``is_package_path`` decides
    the rest.

    Example:
    >>> state = {"s/a/SKILL.md": ("t", "100644"), "s/a/run.sh": ("r", "100755"),
    ...          "s/a/node_modules/x.js": ("n", "100644"), "s/b/x.md": ("b", "100644")}
    >>> folder_manifest(state, "s/a")
    ((('run.sh', 'r', '100755'),), True)
    """
    prefix = "" if folder == "." else f"{folder}/"
    files: list[tuple[str, str, str]] = []
    present = False
    for repo_path, (sha, mode) in state.items():
        if not repo_path.startswith(prefix):
            continue
        relative = repo_path[len(prefix) :]
        if relative.lower() == SKILL_MD_NAME:
            present = True
        elif is_package_path(relative, folder):
            files.append((relative, sha, mode))
    return tuple(sorted(files)), present


def is_package_path(relative: str, folder: str) -> bool:
    """Whether a path relative to the skill folder is a package file.

    Not inside ``SKIPPED_FOLDERS``; at a repo-root skill (``folder`` is
    ``.``) also inside one of ``ROOT_SKILL_PACKAGE_FOLDERS``.

    Example:
    >>> is_package_path("scripts/a.py", "skills/x"), is_package_path("dist/a.js", "skills/x")
    (True, False)
    >>> is_package_path("src/main.go", "."), is_package_path("scripts/a.py", ".")
    (False, True)
    """
    if not relative or is_skipped_path(relative):
        return False
    if folder != ".":
        return True
    return relative.split("/", 1)[0] in settings.ROOT_SKILL_PACKAGE_FOLDERS


def _touches_package(rows: list, folder: str) -> bool:
    """Whether a commit changed a package file or the ``SKILL.md`` of ``folder``."""
    prefix = "" if folder == "." else f"{folder}/"
    for row in rows:
        for path in (_path_text(row.repo_path), _path_text(row.old_repo_path)):
            if not path.startswith(prefix):
                continue
            relative = path[len(prefix) :]
            if relative.lower() == SKILL_MD_NAME or is_package_path(relative, folder):
                return True
    return False


# Step 9: number package versions


# Algorithm A21, see code_flow/08_package_versions.md
def number_package_versions(manifests: pd.DataFrame, versions: pd.DataFrame) -> pd.DataFrame:
    """A new package version at each commit where the files or the text version change.

    ``version_seq`` is the text version (A20) in force after the commit: the
    version whose first commit is the newest one so far. Package versions start
    at the commit of text version 1 and are only counted while the folder has
    its ``SKILL.md`` (a removed skill has none). ``changed_skill_md`` is True
    when ``version_seq`` changed, ``changed_scripts`` / ``changed_other_files``
    when a script / another file was added, removed or got a new sha or mode,
    compared with the previous package version (version 1 compares with an
    empty folder).

    Inputs: rows from ``rebuild_folder_manifests``; versions with
    ``lineage_id``, ``version_seq``, ``commit``.
    Outputs: ``PACKAGE_COLUMNS`` plus ``files``, one row per package version.

    Example:
    >>> rows = pd.DataFrame([
    ...     _example_row(status="A", commit="c1"),
    ...     _example_row(status="A", file_path="run.sh", repo_path="skills/new/run.sh",
    ...                  commit="c2", commit_seq=2, commit_date="2026-01-02T00:00:00Z")])
    >>> versions = pd.DataFrame({"lineage_id": ["acme/copy::skills/new"],
    ...     "version_seq": [1], "commit": ["c1"]})
    >>> packages = number_package_versions(rebuild_folder_manifests(rows), versions)
    >>> packages[["package_version_seq", "version_seq", "changed_scripts"]].values.tolist()
    [[1, 1, False], [2, 1, True]]
    """
    version_of: dict[str, dict[str, int]] = {}
    for lineage_id, commit, seq in zip(
        versions["lineage_id"], versions["commit"], versions["version_seq"], strict=True
    ):
        version_of.setdefault(str(lineage_id), {})[str(commit)] = int(seq)
    records: list[dict] = []
    for lineage_id, group in manifests.groupby("lineage_id", sort=False):
        records.extend(
            _packages_of_lineage(str(lineage_id), group, version_of.get(str(lineage_id), {}))
        )
    packages = pd.DataFrame(records, columns=[*PACKAGE_COLUMNS, "files"])
    for name in ("package_version_seq", "version_seq", "file_count", "script_count"):
        packages[name] = packages[name].astype("int64")
    return packages


def _packages_of_lineage(
    lineage_id: str, group: pd.DataFrame, version_of: dict[str, int]
) -> list[dict]:
    """Package version records of one lineage (manifest rows in commit order)."""
    records: list[dict] = []
    version, last_version, last_files = 0, 0, None
    for row in group.itertuples(index=False):
        version = version_of.get(row.commit, version)
        if version < 1 or not row.skill_md_present:
            continue
        if last_files is not None and row.files == last_files and version == last_version:
            continue
        scripts_changed, others_changed = changed_file_kinds(last_files or (), row.files)
        records.append(
            {
                "lineage_id": lineage_id,
                "package_version_seq": len(records) + 1,
                "version_seq": version,
                "commit": row.commit,
                "commit_date": row.commit_date,
                "changed_skill_md": version != last_version,
                "changed_scripts": scripts_changed,
                "changed_other_files": others_changed,
                "file_count": len(row.files),
                "script_count": sum(
                    file_category(path, mode) == "script" for path, _, mode in row.files
                ),
                "files": row.files,
            }
        )
        last_files, last_version = row.files, version
    return records


def changed_file_kinds(old_files: tuple, new_files: tuple) -> tuple[bool, bool]:
    """(a script changed, another file changed) between two manifests.

    A file changed when it was added, removed, or got another sha or mode. A
    rename is a remove plus an add. The kind comes from ``file_category``
    with the newer mode.

    Example:
    >>> changed_file_kinds((("a.py", "1", "100644"),), (("b.py", "1", "100644"),))
    (True, False)
    >>> changed_file_kinds((), (("notes.md", "2", "100644"),))
    (False, True)
    """
    old_map = {path: (sha, mode) for path, sha, mode in old_files}
    new_map = {path: (sha, mode) for path, sha, mode in new_files}
    kinds = {
        file_category(path, (new_map.get(path) or old_map[path])[1])
        for path in old_map.keys() | new_map.keys()
        if old_map.get(path) != new_map.get(path)
    }
    return "script" in kinds, bool(kinds - {"script"})


def folder_manifest_rows(packages: pd.DataFrame, sizes: dict[str, int]) -> pd.DataFrame:
    """One row per file per package version (``MANIFEST_COLUMNS``).

    ``size`` is the byte length of the stored text, or null when stage 3
    stored none. ``file_category`` comes from ``compare_texts.file_category``.

    Example:
    >>> packages = pd.DataFrame({"lineage_id": ["r::s"], "package_version_seq": [1],
    ...     "files": [(("run.sh", "x", "100755"),)]})
    >>> row = folder_manifest_rows(packages, {"x": 12}).loc[0]
    >>> row["path"], int(row["size"]), row["file_category"]
    ('run.sh', 12, 'script')
    """
    records = [
        (lineage_id, int(seq), path, sha, mode, sizes.get(sha), file_category(path, mode))
        for lineage_id, seq, files in zip(
            packages["lineage_id"], packages["package_version_seq"], packages["files"], strict=True
        )
        for path, sha, mode in files
    ]
    rows = pd.DataFrame(records, columns=MANIFEST_COLUMNS)
    rows["size"] = rows["size"].astype("Int64")
    return rows


def build_package_versions(
    lineages: pd.DataFrame,
    versions: pd.DataFrame,
    files: dict[str, Path],
    paths: dict[str, Path],
    row_limit: int,
) -> dict[str, object]:
    """Steps 8 and 9 for every batch of registries; manifests are written as they go.

    Inputs: kept lineages and their versions (stage 4a), ``files`` with the
    stage 3 ``changes`` and ``texts`` files, the two output paths, and the row
    limit per batch. Outputs: the two files; returns the package versions and
    the counts the summary needs.
    """
    keys = lineage_keys(lineages)
    batches = assign_package_batches(count_folder_rows(files["changes"], keys), row_limit)
    version_repo = versions["lineage_id"].astype(str).str.split("::", n=1).str[0]
    part_file = paths["manifests"].with_name(paths["manifests"].name + ".part")
    package_parts, facts = [], {"rows_read": 0, "manifest_rows": 0, "rows_with_size": 0}
    categories: dict[str, int] = {}
    started = time.perf_counter()
    with pq.ParquetWriter(part_file, MANIFEST_SCHEMA, compression="zstd") as writer:
        for index, repos in enumerate(batches):
            changes = read_folder_changes(files["changes"], keys, repos)
            # Step 8: Rebuild folder manifests (Algorithm A21)
            manifests = rebuild_folder_manifests(changes)
            # Step 9: Number package versions (Algorithm A21)
            packages = number_package_versions(manifests, versions.loc[version_repo.isin(repos)])
            blobs = sorted({sha for files_ in packages["files"] for _, sha, _ in files_})
            rows = folder_manifest_rows(packages, read_blob_sizes(files["texts"], blobs))
            writer.write_table(
                pa.Table.from_pandas(rows, schema=MANIFEST_SCHEMA, preserve_index=False)
            )
            package_parts.append(packages.drop(columns="files"))
            facts["rows_read"] += len(changes)
            facts["manifest_rows"] += len(rows)
            facts["rows_with_size"] += int(rows["size"].notna().sum())
            for name, count in rows["file_category"].value_counts().items():
                categories[name] = categories.get(name, 0) + int(count)
            print(
                f"progress package batch {index + 1}/{len(batches)}: {len(repos)} registries, "
                f"{len(changes)} rows, {len(packages)} package versions, "
                f"{len(rows)} manifest rows, "
                f"{time.perf_counter() - started:.0f} s",
                flush=True,
            )
    packages = _concat(package_parts, PACKAGE_COLUMNS)
    write_parquet(packages, paths["packages"], sort_by=["lineage_id", "package_version_seq"])
    part_file.replace(paths["manifests"])
    facts["batches"] = len(batches)
    facts["categories"] = categories
    return {"packages": packages, "facts": facts}


# Step 10: summary


def summarize_packages(
    lineages: pd.DataFrame, versions: pd.DataFrame, built: dict[str, object]
) -> dict[str, object]:
    """The counts card G11 step 10 asks for (folder files, scripts, package vs text versions)."""
    packages, facts = built["packages"], built["facts"]
    per_lineage = packages.groupby("lineage_id").size()
    text_per_lineage = lineages.set_index("lineage_id")["version_count"]
    newest = (
        packages.sort_values(["lineage_id", "package_version_seq"]).groupby("lineage_id").tail(1)
    )
    ever = packages.groupby("lineage_id")[["file_count", "script_count"]].max()
    seen = set(zip(packages["lineage_id"], packages["commit"], strict=True))
    text_commits = set(zip(versions["lineage_id"], versions["commit"], strict=True))
    lineage_count = len(lineages)
    numbers: dict[str, object] = {
        "lineages": lineage_count,
        "root_lineages": int((lineages["skill_folder"] == ".").sum()),
        "change_rows_read": facts["rows_read"],
        "package_batches": facts["batches"],
        "lineages_with_package_versions": int(per_lineage.size),
        "package_versions": len(packages),
        "text_versions": len(versions),
        "package_versions_per_lineage_median": _median(per_lineage),
        "package_versions_per_lineage_max": _max_with_name(per_lineage),
        "lineages_more_package_than_text_versions": int(
            (per_lineage > text_per_lineage.reindex(per_lineage.index).fillna(0)).sum()
        ),
        "text_versions_without_package_version": len(text_commits - seen),
        "changed_skill_md": int(packages["changed_skill_md"].sum()),
        "changed_scripts": int(packages["changed_scripts"].sum()),
        "changed_other_files": int(packages["changed_other_files"].sum()),
        "only_folder_changed": int((~packages["changed_skill_md"].astype(bool)).sum()),
        "lineages_with_files_now": int((newest["file_count"] > 0).sum()),
        "share_with_files_now": _share(int((newest["file_count"] > 0).sum()), lineage_count),
        "lineages_with_scripts_now": int((newest["script_count"] > 0).sum()),
        "share_with_scripts_now": _share(int((newest["script_count"] > 0).sum()), lineage_count),
        "lineages_with_files_ever": int((ever["file_count"] > 0).sum()),
        "lineages_with_scripts_ever": int((ever["script_count"] > 0).sum()),
        "manifest_rows": facts["manifest_rows"],
        "manifest_rows_with_size": facts["rows_with_size"],
    }
    for name in sorted(facts["categories"]):
        numbers[f"manifest_rows_{name}"] = facts["categories"][name]
    return numbers


def run_package_stage(dev: bool, lineage_paths: dict[str, Path], paths: dict[str, Path]) -> None:
    """Steps 8-10 from the saved stage 4a tables: build, save, print the summary."""
    started = time.perf_counter()
    check_inputs_exist(
        {"lineages": lineage_paths["lineages"], "lineage_versions": lineage_paths["versions"]},
        {
            name: "python -m skill_genealogy.build_registry_lineages"
            for name in ("lineages", "lineage_versions")
        },
    )
    lineages = read_parquet(lineage_paths["lineages"])
    versions = read_parquet(lineage_paths["versions"]).loc[
        :, ["lineage_id", "version_seq", "commit"]
    ]
    files = {
        "changes": history_dir() / "registry_history_changes.parquet",
        "texts": history_dir() / "registry_history_texts.parquet",
    }
    built = build_package_versions(lineages, versions, files, paths, settings.PACKAGE_ROW_BATCH)
    # Step 10: Summary
    numbers = summarize_packages(lineages, versions, built)
    numbers["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    numbers["output_folder"] = str(settings.data_dir(dev))
    print_summary("Stage 4b summary", numbers)
    _print_package_samples(paths, built["packages"])


def main() -> None:
    """Stage 4a (steps 0-7), then stage 4b (steps 8-10); each skips when its outputs exist.

    ``--packages-only`` runs only stage 4b from the saved stage 4a tables. A
    stage 4a rebuild always rebuilds stage 4b too.
    """
    parser = build_arg_parser("Turn registry SKILL.md history into lineages (no network).")
    parser.add_argument(
        "--packages-only",
        action="store_true",
        help="run only steps 8-10 (package versions) from the saved lineage tables",
    )
    args = parser.parse_args()
    dev = bool(args.dev)
    paths = output_paths(dev)
    package_paths = package_output_paths(dev)
    ran_lineages = False
    if args.packages_only:
        pass
    elif outputs_exist(list(paths.values())) and not args.force:
        print(
            f"skip: the four lineage tables exist in {settings.data_dir(dev)} (pass --force)",
            flush=True,
        )
    else:
        run_lineage_stage(dev, paths)
        ran_lineages = True
    if outputs_exist(list(package_paths.values())) and not (args.force or ran_lineages):
        print(
            f"skip: the two package tables exist in {settings.data_dir(dev)} (pass --force)",
            flush=True,
        )
        return
    run_package_stage(dev, paths, package_paths)


def run_lineage_stage(dev: bool, paths: dict[str, Path]) -> None:
    """Steps 0-7 (stage 4a): read, build the lineages in batches, save, print the summary."""
    started = time.perf_counter()
    # Step 0: Copier filter (rule 0)
    scope = registry_scope(dev)
    _print_copiers(scope)
    # Step 1: Read SKILL.md changes of the kept registries
    changes = read_skill_md_changes(dev)
    print(f"read {len(changes)} SKILL.md rows in {time.perf_counter() - started:.1f} s", flush=True)
    texts_file = history_dir() / "registry_history_texts.parquet"
    # Steps 2-7
    built = build_registry_lineages(changes, texts_file, paths, settings.LINEAGE_TEXT_BATCH)
    status = read_parquet(history_dir() / "registry_history_status.parquet")
    numbers = summarize_lineages(scope, built, status)
    numbers["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    numbers["output_folder"] = str(settings.data_dir(dev))
    print_summary("Stage 4a summary", numbers)
    _print_samples(paths, built)


# Small helpers


def parent_folder(path: object) -> str:
    """Folder of a repo path; the repo root is ``.``.

    Example:
    >>> parent_folder("skills/a/SKILL.md"), parent_folder("SKILL.md")
    ('skills/a', '.')
    """
    return posixpath.dirname(str(path or "")) or "."


def normalized_sha(text: str) -> str:
    """SHA-1 of the normalised text without blank lines: the version key (A20).

    ``normalize_text`` makes spaces, line endings and quotes the same; the blank
    lines it keeps are then dropped. So a commit that only changes whitespace,
    blank lines included, gives the same key and no new version.

    Example:
    >>> normalized_sha("Hi.\\nBye.") == normalized_sha("Hi. \\n\\nBye.\\n")
    True
    >>> normalized_sha("Hi.") == normalized_sha("Hi!")
    False
    """
    lines = [line for line in normalize_text(text).split("\n") if line.strip()]
    return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()


def step_state(text_state: str, old_text: str, new_text: str) -> str:
    """The step's state: ``front_matter_only`` when only the front matter changed.

    ``compare_texts`` reads the body only, so a step whose body is the same is
    ``unchanged`` there even when the front matter changed (a ``version:``
    bump). Such a step becomes ``front_matter_only``. Other states stay.

    Example:
    >>> step_state("unchanged", "---\\nversion: 1\\n---\\nBody", "---\\nversion: 2\\n---\\nBody")
    'front_matter_only'
    >>> step_state("cosmetic", "Body", "Body.")
    'cosmetic'
    """
    if text_state != "unchanged":
        return text_state
    old_front = split_front_matter(normalize_text(old_text))[0]
    new_front = split_front_matter(normalize_text(new_text))[0]
    return "front_matter_only" if old_front != new_front else text_state


def _file_name(path: str) -> str:
    """Last part of a path (``a/b/SKILL.md`` -> ``SKILL.md``)."""
    return posixpath.basename(str(path or ""))


def _is_top_skill_md(file_path: pd.Series) -> pd.Series:
    """True for the folder's own SKILL.md, any letter case; False for ``cn/SKILL.md``."""
    return file_path.fillna("").astype(str).str.lower() == SKILL_MD_NAME


def _text_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Own or former-folder top-level SKILL.md rows that carry a text (not a delete)."""
    mask = _is_top_skill_md(frame["file_path"]) & frame["history_role"].isin(VERSION_ROLES)
    mask &= (frame["status"] != _DELETE) & frame["new_sha"].notna()
    mask &= ~frame["new_sha"].fillna("").astype(str).str.fullmatch("0+")
    return frame.loc[mask]


def _utc_times(dates: pd.Series) -> pd.Series:
    """Parse ISO dates with any offset (``+09:00``, ``Z``) as UTC timestamps.

    Example:
    >>> times = _utc_times(pd.Series(["2026-01-01T09:00:00+09:00", "2026-01-01T00:00:00Z"]))
    >>> bool(times[0] == times[1])
    True
    """
    if isinstance(dates.dtype, pd.DatetimeTZDtype):
        return dates.dt.tz_convert("UTC")
    return pd.to_datetime(dates, utc=True, format="ISO8601", errors="coerce")


def _path_text(value: object) -> str:
    """A repo path as text; None, NaN and NA become ``""``."""
    return value if isinstance(value, str) else ""


def _carries_text(row: object) -> bool:
    """Whether a change row adds or edits a file (not a delete, not a zero sha)."""
    sha = str(getattr(row, "new_sha", "") or "")
    return str(row.status or "")[:1] != _DELETE and bool(sha) and sha.strip("0") != ""


def _share(part: int, whole: int) -> float:
    """``part / whole`` rounded to 4 places, 0.0 when ``whole`` is 0."""
    return round(part / whole, 4) if whole else 0.0


def _print_package_samples(paths: dict[str, Path], packages: pd.DataFrame) -> None:
    """Two package versions of the lineage with the most, and two manifest rows."""
    counts = packages.groupby("lineage_id").size().sort_values(ascending=False, kind="mergesort")
    top = counts.index[:1].tolist()
    print_sample_records(
        packages.loc[packages["lineage_id"].isin(top)], "Sample package versions", 2
    )
    if not top:
        return
    rows = pq.read_table(paths["manifests"], filters=[("lineage_id", "==", top[0])]).to_pandas()
    rows["size"] = rows["size"].astype("Int64")
    newest = rows.loc[rows["package_version_seq"] == rows["package_version_seq"].max()]
    print_sample_records(newest, "Sample manifest rows (newest package version)", 2)


def _text_lists(record: dict) -> dict:
    """Turn every list field into a list of strings (YAML keys can be numbers)."""
    return {
        key: [str(item) for item in value] if isinstance(value, list) else value
        for key, value in record.items()
    }


def _concat(parts: list[pd.DataFrame], columns: list[str]) -> pd.DataFrame:
    """Join batch frames; an empty frame with ``columns`` when there is none."""
    parts = [part for part in parts if len(part)]
    if not parts:
        return pd.DataFrame(columns=columns)
    return pd.concat(parts, ignore_index=True)


def _median(values: pd.Series) -> float | None:
    """Median of a count series, or None when it is empty."""
    return float(statistics.median(values.tolist())) if len(values) else None


def _max_with_name(values: pd.Series) -> str:
    """``max (name)`` of a count series indexed by name."""
    if not len(values):
        return "0"
    return f"{int(values.max())} ({values.idxmax()})"


def _former_and_copy_counts(joined: pd.DataFrame, lineages: pd.DataFrame) -> dict[str, int]:
    """Renames and copies: lineages with former paths and copied_from, outside rows used."""
    outside = ~(_is_top_skill_md(joined["file_path"]) & joined["history_role"].isin(VERSION_ROLES))
    used = len(copy_rows_before_version_one(joined))
    kept_ids = set(lineages["lineage_id"])
    copied = lineages["copied_from"].dropna()
    former = lineages["former_paths"].map(len)
    return {
        "lineages_with_former_paths": int((former > 0).sum()),
        "lineages_with_2plus_former_paths": int((former > 1).sum()),
        "lineages_copied_from": len(copied),
        "copied_from_not_a_kept_lineage": int((~copied.isin(kept_ids)).sum()),
        "outside_rows": int(outside.sum()),
        "outside_rows_used_for_copied_from": used,
        "outside_rows_ignored": int(outside.sum()) - used,
    }


def _follow_renames_share(status: pd.DataFrame, lineages: pd.DataFrame) -> dict[str, object]:
    """Kept registries mined with ``follow_renames = False``, and their share."""
    kept = status.loc[status["repo"].isin(set(lineages["repo"]))]
    without = int((~kept["follow_renames"].astype("boolean")).fillna(False).sum())
    share = round(without / len(kept), 4) if len(kept) else 0.0
    return {"registries_follow_renames_false": without, "follow_renames_false_share": share}


def _print_copiers(scope: pd.DataFrame) -> None:
    """Print the registries skipped as copiers, largest first (rule 0)."""
    skipped = scope.loc[~scope["kept"]].sort_values("skill_folders", ascending=False)
    print(
        f"copier filter: kept {int(scope['kept'].sum())} of {len(scope)} registries; "
        f"skipped {len(skipped)} registries with "
        f"{int(skipped['skill_folders'].sum())} skill folders",
        flush=True,
    )
    for row in skipped.head(10).itertuples(index=False):
        print(
            f"  skipped {row.repo} ({row.registry_kind}, {row.skill_folders} folders)", flush=True
        )


def _print_samples(paths: dict[str, Path], built: dict[str, object]) -> None:
    """Two sample records of each output (the most versions first)."""
    lineages = built["lineages"].sort_values("version_count", ascending=False, kind="mergesort")
    print_sample_records(lineages, "Sample lineages", 2)
    top = lineages["lineage_id"].head(1).tolist()
    versions = built["versions"]
    print_sample_records(versions.loc[versions["lineage_id"].isin(top)], "Sample versions", 2)
    changes = pq.read_table(paths["changes"]).slice(0, 2).to_pandas()
    print_sample_records(changes, "Sample changes", 2)
    print_sample_records(built["dropped"], "Sample dropped", 2)


def _example_row(**overrides: object) -> dict[str, object]:
    """One own SKILL.md change for the doctests; override any field."""
    row: dict[str, object] = {
        "repo": "acme/copy",
        "skill_folder": "skills/new",
        "file_path": "SKILL.md",
        "history_role": "own",
        "status": "M",
        "repo_path": "skills/new/SKILL.md",
        "old_repo_path": None,
        "old_sha": None,
        "new_sha": "blob",
        "new_mode": "100644",
        "commit": "c1",
        "commit_date": "2026-01-01T00:00:00Z",
        "commit_seq": 1,
    }
    row.update(overrides)
    return row


def _example_tuple(**overrides: object) -> SimpleNamespace:
    """``_example_row`` as an object with attributes, like a row of ``itertuples``."""
    return SimpleNamespace(**_example_row(**overrides))


if __name__ == "__main__":
    main()
