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

Run (no network):
    python -m skill_genealogy.build_registry_lineages --dev
    python -m skill_genealogy.build_registry_lineages --force
"""

from __future__ import annotations

import hashlib
import posixpath
import statistics
import time
from dataclasses import fields
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from skill_genealogy import settings
from skill_genealogy.compare_texts import TextChange, compare_front_matter, compare_texts
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


def main() -> None:
    """Read the inputs, build the lineages in batches, save and print the summary."""
    args = build_arg_parser(
        "Turn registry SKILL.md history into lineages (no network)."
    ).parse_args()
    dev = bool(args.dev)
    paths = output_paths(dev)
    if outputs_exist(list(paths.values())) and not args.force:
        print(
            f"skip: the four lineage tables exist in {settings.data_dir(dev)} (pass --force)",
            flush=True,
        )
        return
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
        "commit": "c1",
        "commit_date": "2026-01-01T00:00:00Z",
        "commit_seq": 1,
    }
    row.update(overrides)
    return row


if __name__ == "__main__":
    main()
