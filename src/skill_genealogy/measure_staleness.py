"""Stage 10: how far behind its registry skill each copy is (RQ3).

What: for every English copy file of stage 7, the registry version its text
still holds (algorithm A44), how many later releases it missed up to an as-of
date and since when (A45), the registry package version its folder holds and
the later packages it missed (A46), what the missed updates touched (commands,
network, scripts, script risk flags), and a state: ``up_to_date``, ``behind``,
``stale``, ``orphaned``, ``removed`` or ``unknown`` (A47). The copy's own history
gives its sync events: when it first took a registry version, later updates and
downgrades, and how long after the release each came (A48). A state describes
the files; it does not say who maintains the copy or why.

Inputs (dev or full data folder): ``copy_files.parquet`` and ``families.parquet``
(stage 7), ``lineages.parquet``, ``lineage_versions.parquet`` and
``lineage_changes.parquet`` (4a), ``lineage_package_versions.parquet`` and
``lineage_folder_manifests.parquet`` (4b), ``copied_skill_history/`` (6).
Shared full folder: ``skill_language.parquet`` (2a) and ``registry_history/`` (3).
GitSkills ``artifacts`` (dates and text of copies without history) and
``artifact_siblings`` (their folder listing).

Outputs: ``copy_staleness.parquet`` (one row per copy file) and
``sync_events.parquet`` (one row per copy or update event in a copy's history).

Run: ``python -m skill_genealogy.measure_staleness [--dev] [--force] [--stale-days N]``.
Design note: docs/skill_genealogy/design/16_staleness.md.
"""

from __future__ import annotations

import bisect
import math
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path

import pandas as pd

from skill_genealogy import settings
from skill_genealogy.assign_families import copy_history_dir, read_copy_histories
from skill_genealogy.build_registry_lineages import history_dir, normalized_sha
from skill_genealogy.download_copied_skill_history import skill_folder_of
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
from skill_genealogy.find_copies import _quote_path
from skill_genealogy.measure_copy_changes import (
    Progress,
    _ordered_history_rows,
    batches_of_files,
    comparable_shingles,
    load_copy_folder_rows,
    load_dataset_texts,
    load_listed_folders,
    load_script_flags,
    load_sibling_files,
    load_stored_texts,
    snapshot_copy_folders,
    snapshot_folder_manifests,
)
from skill_genealogy.text_helpers import exact_jaccard

STALENESS_COLUMNS = [
    "repo", "path", "lineage_id", "link", "arrival_group", "current_basis", "current_blob",
    "held_seq", "held_basis", "held_similarity", "held_release_date",
    "as_of_date", "as_of_basis", "latest_seq",
    "skill_md_versions_behind", "first_missed_release",
    "package_held_seq", "package_basis", "package_versions_behind", "first_missed_package",
    "missed_script_updates", "days_stale",
    "missed_substantive", "missed_commands", "missed_network", "missed_scripts", "missed_risky",
    "stopped_changing_at", "copied_before_release", "registry_removed", "days_since_removed",
    "copy_state", "stale_after_days",
]  # fmt: skip
SYNC_EVENT_COLUMNS = [
    "repo", "path", "lineage_id", "commit", "commit_date", "event_kind",
    "from_seq", "to_seq", "versions_skipped", "release_date", "lag_days",
]  # fmt: skip
# A47: every state a copy can have, from least to most severe. A text held by
# several files is reported with its most severe state.
COPY_STATES = ("up_to_date", "unknown", "removed", "orphaned", "behind", "stale")
EVENT_KINDS = ("copy", "update", "downgrade")
# Where a copy's package lag comes from (A46).
PACKAGE_BASES = ("measured", "files_not_kept", "no_folder", "no_package")

# Query Q10.1, see docs/skill_genealogy/code_flow/16_staleness.md
# Newest commit date of each registry's downloaded history (stage 3).
REGISTRY_NEWEST_COMMIT_SQL = """
SELECT changes.repo AS repo, max(CAST(changes.commit_date AS TIMESTAMPTZ)) AS newest_commit
FROM read_parquet({path}) AS changes
SEMI JOIN wanted_repos ON changes.repo = wanted_repos.repo
GROUP BY changes.repo
"""

# Query Q10.2, see docs/skill_genealogy/code_flow/16_staleness.md
# The dataset's dates of each copy file: last commit and capture (discovered_at).
DATASET_DATES_SQL = """
SELECT artifacts.repo_full_name AS repo, artifacts.path AS path, artifacts.file_sha AS file_sha,
       max(artifacts.last_commit_at) AS last_commit_at,
       min(artifacts.discovered_at) AS discovered_at
FROM read_parquet({path}) AS artifacts
SEMI JOIN wanted_files
  ON artifacts.repo_full_name = wanted_files.repo
 AND artifacts.path = wanted_files.path
 AND artifacts.file_sha = wanted_files.file_sha
GROUP BY artifacts.repo_full_name, artifacts.path, artifacts.file_sha
"""

# Query Q10.3, see docs/skill_genealogy/code_flow/16_staleness.md
# Every script that differs between a package version with changed_scripts and the
# package version before it, for the wanted lineages (stage 4b tables).
SCRIPT_DELTAS_SQL = """
WITH packages AS (
    SELECT lineage_id, package_version_seq, changed_scripts,
           lag(package_version_seq) OVER (
               PARTITION BY lineage_id ORDER BY package_version_seq) AS previous_seq
    FROM read_parquet({packages})
    SEMI JOIN wanted_lineages USING (lineage_id)
),
changed AS (SELECT * FROM packages WHERE changed_scripts),
scripts AS (
    SELECT lineage_id, package_version_seq, path, sha
    FROM read_parquet({manifests})
    SEMI JOIN wanted_lineages USING (lineage_id)
    WHERE file_category = 'script'
),
new_files AS (
    SELECT changed.lineage_id, changed.package_version_seq, scripts.path, scripts.sha
    FROM changed JOIN scripts USING (lineage_id, package_version_seq)
),
old_files AS (
    SELECT changed.lineage_id, changed.package_version_seq, scripts.path, scripts.sha
    FROM changed JOIN scripts
      ON scripts.lineage_id = changed.lineage_id
     AND scripts.package_version_seq = changed.previous_seq
)
SELECT coalesce(new_files.lineage_id, old_files.lineage_id) AS lineage_id,
       coalesce(new_files.package_version_seq, old_files.package_version_seq)
           AS package_version_seq,
       coalesce(new_files.path, old_files.path) AS path,
       old_files.sha AS old_sha, new_files.sha AS new_sha
FROM new_files FULL OUTER JOIN old_files
  ON new_files.lineage_id = old_files.lineage_id
 AND new_files.package_version_seq = old_files.package_version_seq
 AND new_files.path = old_files.path
WHERE new_files.sha IS DISTINCT FROM old_files.sha
"""

# Query Q10.4, see docs/skill_genealogy/code_flow/16_staleness.md
# Files shared by each copy folder and each registry package version of its lineage
# committed on or before the copy's package limit (A46). SKILL.md counts as a file:
# the package's SKILL.md is its text version's blob.
SHARED_PACKAGE_FILES_SQL = """
WITH registry_files AS (
    SELECT manifests.lineage_id, manifests.package_version_seq, manifests.path, manifests.sha
    FROM read_parquet({manifests}) AS manifests
    SEMI JOIN batch_lineages USING (lineage_id)
    UNION ALL
    SELECT packages.lineage_id, packages.package_version_seq, 'SKILL.md' AS path,
           packages.skill_md_blob AS sha
    FROM batch_packages AS packages
),
candidates AS (
    SELECT copies.repo, copies.path, packages.lineage_id, packages.package_version_seq,
           packages.commit_date
    FROM batch_copies AS copies
    JOIN batch_packages AS packages
      ON packages.lineage_id = copies.lineage_id AND packages.commit_date <= copies.package_limit
),
shared AS (
    SELECT copy_files.repo, copy_files.path, registry_files.package_version_seq,
           count(*) AS shared_files,
           count(*) FILTER (WHERE copy_files.file_path = 'SKILL.md') AS shared_skill_md
    FROM copy_files
    JOIN batch_copies USING (repo, path)
    JOIN registry_files
      ON registry_files.lineage_id = batch_copies.lineage_id
     AND registry_files.path = copy_files.file_path
     AND registry_files.sha = copy_files.sha
    GROUP BY ALL
)
SELECT candidates.repo, candidates.path, candidates.package_version_seq,
       coalesce(shared.shared_files, 0) AS shared_files,
       coalesce(shared.shared_skill_md, 0) AS shared_skill_md
FROM candidates LEFT JOIN shared
  ON shared.repo = candidates.repo AND shared.path = candidates.path
 AND shared.package_version_seq = candidates.package_version_seq
"""


# Paths


def output_paths(dev: bool) -> dict[str, Path]:
    """The two stage 10 output files of this mode."""
    folder = settings.data_dir(dev)
    return {
        "copy_staleness": folder / "copy_staleness.parquet",
        "sync_events": folder / "sync_events.parquet",
    }


# Small value helpers


def _timestamp(value: object) -> pd.Timestamp | None:
    """A UTC timestamp, or None for a missing value.

    >>> _timestamp("2026-03-01T10:00:00+02:00")
    Timestamp('2026-03-01 08:00:00+0000', tz='UTC')
    >>> _timestamp(None) is None
    True
    """
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    stamp = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(stamp) else stamp


def days_between(later: object, earlier: object) -> int | None:
    """Whole days from ``earlier`` to ``later``; None when either is missing.

    >>> days_between("2026-10-23", "2026-03-01")
    236
    >>> days_between(None, "2026-03-01") is None
    True
    """
    end, start = _timestamp(later), _timestamp(earlier)
    if end is None or start is None:
        return None
    return int((end - start).days)


def _flag(value: object) -> bool:
    """True only for a true value; a missing value is false.

    >>> _flag(True), _flag(float("nan")), _flag(None), _flag(pd.NA)
    (True, False, False, False)
    """
    if value is None or value is pd.NA:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return bool(value)


def _empty_list(value: object) -> bool:
    """True for a missing value or an empty list."""
    if value is None:
        return True
    try:
        return len(value) == 0  # type: ignore[arg-type]
    except TypeError:
        return bool(pd.isna(value))


# Step 1: the copies and their dates


def select_copies_to_measure(
    copy_files: pd.DataFrame, families: pd.DataFrame, english: set[str]
) -> pd.DataFrame:
    """Copy files with a lineage, an English text, and not a registry's own file.

    Unlike stage 9, copies made before the registry release
    (``copied_before_release``) and copies without an origin stay: they are counted.

    >>> files = pd.DataFrame({"file_sha": ["a", "b", "c", "d"],
    ...     "lineage_id": ["L", "L", None, "L"],
    ...     "copied_before_release": [False, True, False, False]})
    >>> families = pd.DataFrame({"file_sha": ["a", "b", "c", "d"],
    ...     "registry_self": [False, False, False, True]})
    >>> select_copies_to_measure(files, families, {"a", "b", "c", "d"})["file_sha"].tolist()
    ['a', 'b']
    """
    self_shas = set(families.loc[families["registry_self"].astype(bool), "file_sha"])
    keep = (
        copy_files["lineage_id"].notna()
        & copy_files["file_sha"].isin(english)
        & ~copy_files["file_sha"].isin(self_shas)
    )
    return copy_files.loc[keep].reset_index(drop=True)


def current_copies(
    copies: pd.DataFrame, histories: pd.DataFrame, dataset_dates: pd.DataFrame
) -> pd.DataFrame:
    """Each copy's current text, its date and where they come from.

    The current text is the newest ``SKILL.md`` blob of the stage 6 history
    (deletes skipped, as in stage 9), dated by its commit; ``current_basis`` is
    ``history``. A copy without a history row has its dataset text (``file_sha``)
    dated ``last_commit_at``; ``current_basis`` is ``dataset``. Adds
    ``current_blob, current_date, current_commit, current_basis, last_commit_at,
    discovered_at``.

    >>> copies = pd.DataFrame({"repo": ["a", "b"], "path": ["SKILL.md"] * 2,
    ...     "file_sha": ["x", "d"]})
    >>> history = pd.DataFrame({"repo": ["a", "a"], "path": ["SKILL.md"] * 2,
    ...     "blob": ["x", "y"], "status": ["A", "M"], "commit_seq": [1, 2],
    ...     "commit": ["c1", "c2"],
    ...     "commit_date": pd.to_datetime(["2026-01-01", "2026-02-01"], utc=True)})
    >>> dates = pd.DataFrame({"repo": ["b"], "path": ["SKILL.md"], "file_sha": ["d"],
    ...     "last_commit_at": ["2026-04-01T00:00:00Z"], "discovered_at": ["2026-07-10T00:00:00Z"]})
    >>> current_copies(copies, history, dates)[["current_blob", "current_basis"]].values.tolist()
    [['y', 'history'], ['d', 'dataset']]
    """
    newest = _ordered_history_rows(histories).drop_duplicates(["repo", "path"], keep="last")
    newest = newest.loc[:, ["repo", "path", "blob", "commit_date", "commit"]].rename(
        columns={"blob": "current_blob", "commit_date": "current_date", "commit": "current_commit"}
    )
    frame = copies.merge(newest, on=["repo", "path"], how="left")
    frame = frame.merge(dataset_dates, on=["repo", "path", "file_sha"], how="left")
    for column in ("last_commit_at", "discovered_at", "current_date"):
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    has_history = frame["current_blob"].notna()
    frame["current_basis"] = has_history.map({True: "history", False: "dataset"})
    frame["current_blob"] = frame["current_blob"].where(has_history, frame["file_sha"])
    frame["current_date"] = frame["current_date"].where(has_history, frame["last_commit_at"])
    return frame


def registry_snapshot_dates(
    lineages: pd.DataFrame, newest_commits: Mapping[str, object], mined_from: object
) -> dict[str, pd.Timestamp]:
    """``lineage_id -> snapshot date`` of the registry that holds the lineage.

    The snapshot is the later of the registry's newest downloaded commit
    (Query Q10.1) and ``mined_from``, the first day any registry was mined: a
    history never holds a release after its own snapshot, and every registry
    was mined on or after ``mined_from``.

    >>> lineages = pd.DataFrame({"lineage_id": ["r/a::x", "r/b::y"], "repo": ["r/a", "r/b"]})
    >>> dates = registry_snapshot_dates(lineages, {"r/a": "2026-11-06T09:00:00Z",
    ...     "r/b": "2026-05-01T00:00:00Z"}, "2026-11-02")
    >>> [str(day.date()) for day in dates.values()]
    ['2026-11-06', '2026-11-02']
    """
    floor = _timestamp(mined_from)
    snapshot: dict[str, pd.Timestamp] = {}
    for lineage_id, repo in zip(lineages["lineage_id"], lineages["repo"], strict=True):
        newest = _timestamp(newest_commits.get(repo))
        snapshot[str(lineage_id)] = floor if newest is None or newest < floor else newest
    return snapshot


def choose_as_of_date(
    copy: Mapping[str, object], registry_snapshot_date: object
) -> tuple[pd.Timestamp | None, str]:
    """The day a copy's lag is measured at, and why (rule 2).

    A copy deleted in its own repo stops falling behind on the day it was
    deleted (``copy_removed``). A copy deleted after its registry's snapshot is
    measured at the snapshot (``registry_snapshot``), because later releases
    are not in the data; its state is still ``removed``. A copy
    with a downloaded history is measured at its registry's snapshot
    (``registry_snapshot``). A copy known only from the dataset is measured at
    the day the dataset captured it (``dataset_capture``).

    >>> choose_as_of_date({"removed": True, "removed_date": "2026-06-01",
    ...     "current_basis": "history"}, "2026-11-02")[1]
    'copy_removed'
    >>> choose_as_of_date({"removed": True, "removed_date": "2026-11-05",
    ...     "current_basis": "history"}, "2026-11-02")[1]
    'registry_snapshot'
    >>> choose_as_of_date({"removed": False, "current_basis": "dataset",
    ...     "discovered_at": "2026-07-10"}, "2026-11-02")
    (Timestamp('2026-07-10 00:00:00+0000', tz='UTC'), 'dataset_capture')
    """
    snapshot = _timestamp(registry_snapshot_date)
    removed_date = _timestamp(copy.get("removed_date"))
    if _flag(copy.get("removed")) and removed_date is not None:
        if snapshot is not None and snapshot < removed_date:
            return snapshot, "registry_snapshot"
        return removed_date, "copy_removed"
    if copy.get("current_basis") == "history":
        return snapshot, "registry_snapshot"
    return _timestamp(copy.get("discovered_at")), "dataset_capture"


# Step 2: the version a copy holds


def index_lineage_versions(lineage_versions: pd.DataFrame) -> dict[str, list[dict]]:
    """Per lineage: its versions (``version_seq, blob, normalized_sha, release_date``) by number."""
    frame = lineage_versions.sort_values(["lineage_id", "version_seq"], kind="mergesort")
    index: dict[str, list[dict]] = defaultdict(list)
    for lineage_id, seq, blob, text_sha, release in zip(
        frame["lineage_id"], frame["version_seq"], frame["blob"], frame["normalized_sha"],
        pd.to_datetime(frame["release_date"], utc=True), strict=True,
    ):  # fmt: skip
        index[str(lineage_id)].append(
            {
                "version_seq": int(seq),
                "blob": str(blob),
                "normalized_sha": text_sha,
                "release_date": release,
            }  # fmt: skip
        )
    return dict(index)


def _newest_on_or_before(versions: list[dict], limit: pd.Timestamp | None) -> dict | None:
    """The newest of ``versions`` released on or before ``limit``, else the oldest."""
    if not versions:
        return None
    if limit is not None:
        earlier = [version for version in versions if version["release_date"] <= limit]
        if earlier:
            return earlier[-1]
    return versions[0]


# Algorithm A44, see docs/skill_genealogy/code_flow/16_staleness.md
def choose_held_version(
    copy: Mapping[str, object],
    versions: list[dict],
    texts: Mapping[str, str] | None = None,
) -> tuple[int | None, str, float | None]:
    """The registry version a copy's current text holds: ``(seq, basis, similarity)``.

    1. A version with the same blob, or the same text up to whitespace
       (``normalized_sha``), is held exactly (``exact_current``, similarity 1).
       When several versions have that text, the newest released by the as-of
       date wins (a registry can return to an older text).
    2. Else the version most similar to the copy (exact Jaccard of the 5-word
       shingles of the body, as in stage 9b) among those released on or before
       the copy's date (``similar_before_date``); a tie goes to the newer one.
    3. No version released by then: ``copied_before_release``; no text to
       compare: ``no_text``; no shingle shared with any version (similarity
       0): ``no_overlap``, because the newest version would win the tie with no
       evidence. All three hold no version.

    ``copy`` has ``current_blob, current_date, as_of_date``; ``versions`` are
    the lineage's versions from ``index_lineage_versions``; ``texts`` maps a
    blob to its text.

    >>> day = lambda text: pd.Timestamp(text, tz="UTC")
    >>> versions = [{"version_seq": 1, "blob": "b1", "normalized_sha": "n1",
    ...     "release_date": day("2026-01-10")}, {"version_seq": 2, "blob": "b2",
    ...     "normalized_sha": "n2", "release_date": day("2026-03-01")}]
    >>> choose_held_version({"current_blob": "b1", "current_date": day("2026-09-01"),
    ...     "as_of_date": day("2026-10-23")}, versions)
    (1, 'exact_current', 1.0)
    >>> choose_held_version({"current_blob": "c", "current_date": day("2026-01-01"),
    ...     "as_of_date": day("2026-10-23")}, versions, {"c": "x"})
    (None, 'copied_before_release', None)
    """
    texts = texts or {}
    blob = str(copy["current_blob"])
    as_of = _timestamp(copy.get("as_of_date"))
    exact = [version for version in versions if version["blob"] == blob]
    copy_text = texts.get(blob)
    if not exact and copy_text is not None:
        text_sha = normalized_sha(copy_text)
        exact = [version for version in versions if version["normalized_sha"] == text_sha]
    if exact:
        return int(_newest_on_or_before(exact, as_of)["version_seq"]), "exact_current", 1.0
    limit = _timestamp(copy.get("current_date"))
    limit = as_of if limit is None else limit
    eligible = [v for v in versions if limit is None or v["release_date"] <= limit]
    if not eligible:
        return None, "copied_before_release", None
    if copy_text is None:
        return None, "no_text", None
    copy_shingles = comparable_shingles(copy_text)
    best: tuple[float, int] | None = None
    for version in eligible:
        version_text = texts.get(version["blob"])
        if version_text is None:
            continue
        score = exact_jaccard(copy_shingles, comparable_shingles(version_text))
        if best is None or score >= best[0]:
            best = (score, int(version["version_seq"]))
    if best is None:
        return None, "no_text", None
    if best[0] <= 0:
        return None, "no_overlap", None
    return best[1], "similar_before_date", round(best[0], 4)


# Step 3: releases missed


# Algorithm A45, see docs/skill_genealogy/code_flow/16_staleness.md
def count_versions_behind(
    held_seq: int, as_of_date: object, versions: list[dict]
) -> tuple[int, pd.Timestamp | None]:
    """Versions after the held one, released on or before the as-of date: ``(count, first)``.

    ``versions`` are the lineage's versions by number; stage 4a numbers them in
    release order, so "after the held one" is a higher number and two versions
    of the same moment are still counted in order. ``first`` is the release
    date of the earliest missed version, None when nothing was missed.

    >>> day = lambda text: pd.Timestamp(text, tz="UTC")
    >>> versions = [{"version_seq": n, "release_date": day(d)} for n, d in
    ...     [(1, "2026-01-10"), (2, "2026-03-01"), (3, "2026-05-15")]]
    >>> count_versions_behind(1, day("2026-10-23"), versions)
    (2, Timestamp('2026-03-01 00:00:00+0000', tz='UTC'))
    >>> count_versions_behind(1, day("2026-04-01"), versions)[0]
    1
    """
    as_of = _timestamp(as_of_date)
    seqs = [version["version_seq"] for version in versions]
    start = bisect.bisect_right(seqs, int(held_seq))
    if as_of is None:
        return 0, None
    end = bisect.bisect_right([version["release_date"] for version in versions], as_of)
    if end <= start:
        return 0, None
    return end - start, versions[start]["release_date"]


def latest_version_by(versions: list[dict], as_of_date: object) -> int | None:
    """The newest version of the lineage released on or before the as-of date.

    >>> day = lambda text: pd.Timestamp(text, tz="UTC")
    >>> versions = [{"version_seq": 1, "release_date": day("2026-01-10")},
    ...     {"version_seq": 2, "release_date": day("2026-03-01")}]
    >>> latest_version_by(versions, day("2026-02-01"))
    1
    """
    as_of = _timestamp(as_of_date)
    if not versions:
        return None
    if as_of is None:
        return int(versions[-1]["version_seq"])
    end = bisect.bisect_right([version["release_date"] for version in versions], as_of)
    return int(versions[end - 1]["version_seq"]) if end else None


# Step 4: the package a copy's folder holds


def index_packages(
    package_versions: pd.DataFrame, lineage_versions: pd.DataFrame
) -> dict[str, list[dict]]:
    """Per lineage: package versions (``seq, commit_date, changed_scripts, skill_md_blob``).

    Ordered by package number, which stage 4b gives in commit order. The
    ``SKILL.md`` blob is the blob of the package's text version.
    """
    blobs = lineage_versions.loc[:, ["lineage_id", "version_seq", "blob"]]
    frame = package_versions.merge(blobs, on=["lineage_id", "version_seq"], how="left")
    frame = frame.sort_values(["lineage_id", "package_version_seq"], kind="mergesort")
    index: dict[str, list[dict]] = defaultdict(list)
    for lineage_id, seq, date, scripts, blob in zip(
        frame["lineage_id"], frame["package_version_seq"],
        pd.to_datetime(frame["commit_date"], utc=True), frame["changed_scripts"],
        frame["blob"], strict=True,
    ):  # fmt: skip
        index[str(lineage_id)].append(
            {
                "seq": int(seq),
                "commit_date": date,
                "changed_scripts": bool(scripts),
                "skill_md_blob": None if pd.isna(blob) else str(blob),
            }  # fmt: skip
        )
    return dict(index)


# Algorithm A46, see docs/skill_genealogy/code_flow/16_staleness.md
def choose_held_package_version(
    copy_files: set[tuple[str, str]],
    packages: list[dict],
    package_files: Mapping[int, set[tuple[str, str]]],
    package_limit: object,
) -> int | None:
    """The registry package version a copy folder holds (plain version of Query Q10.4).

    Among the lineage's package versions committed on or before
    ``package_limit`` (the copy's date), the one that shares the most
    ``(path, sha)`` files with the copy; ``SKILL.md`` counts as a file. A tie
    goes to the newest. None when no package version is that old.

    >>> day = lambda text: pd.Timestamp(text, tz="UTC")
    >>> packages = [{"seq": 1, "commit_date": day("2026-01-10"), "skill_md_blob": "t"},
    ...     {"seq": 2, "commit_date": day("2026-03-01"), "skill_md_blob": "t"}]
    >>> files = {1: {("scripts/a.sh", "old")}, 2: {("scripts/a.sh", "new")}}
    >>> choose_held_package_version({("SKILL.md", "t"), ("scripts/a.sh", "old")},
    ...     packages, files, day("2026-06-01"))
    1
    >>> choose_held_package_version({("SKILL.md", "t")}, packages, files, day("2026-06-01"))
    2
    """
    limit = _timestamp(package_limit)
    best: tuple[int, int] | None = None
    for package in packages:
        if limit is not None and package["commit_date"] > limit:
            continue
        registry = set(package_files.get(package["seq"], set()))
        if package.get("skill_md_blob"):
            registry.add(("SKILL.md", str(package["skill_md_blob"])))
        shared = len(registry & copy_files)
        if best is None or shared >= best[0]:
            best = (shared, package["seq"])
    return None if best is None else best[1]


def pick_held_packages(shared: pd.DataFrame) -> dict[tuple[str, str], tuple[int, int, int]]:
    """``(repo, path) -> (package_version_seq, shared_files, shared_skill_md)`` (Query Q10.4).

    The most shared files wins; a tie goes to the higher (newer) package number.

    >>> rows = pd.DataFrame({"repo": ["r"] * 3, "path": ["SKILL.md"] * 3,
    ...     "package_version_seq": [1, 2, 3], "shared_files": [2, 1, 2],
    ...     "shared_skill_md": [1, 1, 0]})
    >>> pick_held_packages(rows)
    {('r', 'SKILL.md'): (3, 2, 0)}
    """
    if shared.empty:
        return {}
    ordered = shared.sort_values(
        ["repo", "path", "shared_files", "package_version_seq"], kind="mergesort"
    ).drop_duplicates(["repo", "path"], keep="last")
    return {
        (repo, path): (int(seq), int(count), int(skill_md))
        for repo, path, seq, count, skill_md in zip(
            ordered["repo"],
            ordered["path"],
            ordered["package_version_seq"],
            ordered["shared_files"],
            ordered["shared_skill_md"],
            strict=True,
        )  # fmt: skip
    }


def package_basis(
    folder_known: bool, held: tuple[int, int, int] | None, held_file_count: int
) -> str:
    """Whether the package lag of a copy is measured, and if not why.

    ``held`` is ``(package_version_seq, shared_files, shared_skill_md)`` from
    ``pick_held_packages``. ``no_folder``: the copy's folder is not known.
    ``no_package``: no package version is old enough. ``files_not_kept``: the
    registry package has files besides ``SKILL.md`` and the copy keeps none of
    them, so it cannot miss an update to them. ``measured`` otherwise.

    >>> package_basis(True, (2, 1, 1), 3)
    'files_not_kept'
    >>> package_basis(True, (2, 3, 1), 3), package_basis(True, (2, 1, 1), 0)
    ('measured', 'measured')
    """
    if not folder_known:
        return "no_folder"
    if held is None:
        return "no_package"
    if held_file_count > 0 and held[1] - held[2] <= 0:
        return "files_not_kept"
    return "measured"


# Step 5: what the missed updates touched


def script_deltas_by_package(
    deltas: pd.DataFrame, flags: Mapping[str, list[str] | None]
) -> dict[tuple[str, int], dict[str, object]]:
    """Per ``(lineage_id, package_version_seq)``: scripts added and whether risk flags changed.

    ``deltas`` are Query Q10.3 rows (``old_sha`` null for an added script).
    A risk flag changed when a script's flags differ from its old flags, or an
    added or removed script has a flag. A script whose text is not known has
    no flags to compare and changes nothing.

    >>> deltas = pd.DataFrame({"lineage_id": ["L", "L"], "package_version_seq": [2, 2],
    ...     "path": ["a.sh", "b.sh"], "old_sha": ["o", None], "new_sha": ["n", "m"]})
    >>> script_deltas_by_package(deltas, {"o": [], "n": ["network"], "m": []})
    {('L', 2): {'scripts_added': 1, 'risk_changed': True}}
    """
    result: dict[tuple[str, int], dict[str, object]] = {}
    for lineage_id, seq, old_sha, new_sha in zip(
        deltas["lineage_id"], deltas["package_version_seq"], deltas["old_sha"],
        deltas["new_sha"], strict=True,
    ):  # fmt: skip
        entry = result.setdefault((str(lineage_id), int(seq)),
                                  {"scripts_added": 0, "risk_changed": False})  # fmt: skip
        old = None if pd.isna(old_sha) else flags.get(str(old_sha))
        new = None if pd.isna(new_sha) else flags.get(str(new_sha))
        if pd.isna(old_sha):
            entry["scripts_added"] += 1
        if pd.isna(old_sha) and new:
            entry["risk_changed"] = True
        elif pd.isna(new_sha) and old:
            entry["risk_changed"] = True
        elif old is not None and new is not None and set(old) != set(new):
            entry["risk_changed"] = True
    return result


def find_missed_updates(
    missed_text_changes: Iterable[Mapping[str, object]],
    missed_script_changes: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """Count what the missed registry updates touched.

    ``missed_text_changes`` are ``lineage_changes`` rows of the missed text
    releases; ``missed_script_changes`` are ``script_deltas_by_package``
    entries of the missed package versions. Each count is a number of missed
    updates: ``missed_substantive`` (state substantive), ``missed_commands``,
    ``missed_network``, ``missed_scripts`` (a text update naming a new script,
    or a package adding a script) and ``missed_script_updates`` (package
    versions that changed a script). ``missed_risky`` is true when a missed
    update added a command, a network reference or a script, or changed a
    script's risk flags.

    >>> text = [{"state": "substantive", "commands_added": ["rm -rf x"],
    ...     "network_refs_added": [], "scripts_mentioned_added": []}]
    >>> find_missed_updates(text, [{"scripts_added": 0, "risk_changed": False}])
    ... # doctest: +NORMALIZE_WHITESPACE
    {'missed_substantive': 1, 'missed_commands': 1, 'missed_network': 0, 'missed_scripts': 0,
     'missed_script_updates': 1, 'missed_risky': True}
    """
    counts = {"missed_substantive": 0, "missed_commands": 0, "missed_network": 0,
              "missed_scripts": 0, "missed_script_updates": 0}  # fmt: skip
    risk_changed = False
    for change in missed_text_changes:
        counts["missed_substantive"] += change.get("state") == "substantive"
        counts["missed_commands"] += not _empty_list(change.get("commands_added"))
        counts["missed_network"] += not _empty_list(change.get("network_refs_added"))
        counts["missed_scripts"] += not _empty_list(change.get("scripts_mentioned_added"))
    for change in missed_script_changes:
        counts["missed_script_updates"] += 1
        counts["missed_scripts"] += int(change.get("scripts_added", 0)) > 0
        risk_changed = risk_changed or bool(change.get("risk_changed"))
    risky = risk_changed or any(
        counts[name] > 0 for name in ("missed_commands", "missed_network", "missed_scripts")
    )
    return {**counts, "missed_risky": risky}


# Step 6: the state


# Algorithm A47, see docs/skill_genealogy/code_flow/16_staleness.md
def classify_copy_state(
    versions_behind: int | None,
    days_stale: int | None,
    registry_removed: bool,
    stale_after_days: int,
    copy_removed: bool = False,
) -> str:
    """The state of a copy, checked in this order.

    ``removed``: the copy was deleted in its own repo. ``orphaned``: the
    registry removed the skill by the as-of date. ``unknown``: no held version
    (``versions_behind`` is None). ``up_to_date``: nothing missed, text or
    package. ``stale``: behind for more than ``stale_after_days`` days.
    ``behind`` otherwise.

    >>> classify_copy_state(2, 236, False, 30)
    'stale'
    >>> classify_copy_state(2, 236, False, 400)
    'behind'
    >>> classify_copy_state(0, None, True, 30)
    'orphaned'
    """
    if copy_removed:
        return "removed"
    if registry_removed:
        return "orphaned"
    if versions_behind is None:
        return "unknown"
    if versions_behind == 0:
        return "up_to_date"
    if days_stale is not None and days_stale > stale_after_days:
        return "stale"
    return "behind"


# Step 7: sync events


def _seq_of_blob(
    blob: str, versions: list[dict], date: pd.Timestamp | None
) -> tuple[int, pd.Timestamp] | None:
    """The lineage version a blob is, by date (as A44 rule 1): ``(seq, release_date)``."""
    matches = [version for version in versions if version["blob"] == blob]
    chosen = _newest_on_or_before(matches, date)
    return None if chosen is None else (int(chosen["version_seq"]), chosen["release_date"])


# Algorithm A48, see docs/skill_genealogy/code_flow/16_staleness.md
def find_sync_events(
    histories: pd.DataFrame,
    lineage_of: Mapping[tuple[str, str], str],
    versions_by_lineage: Mapping[str, list[dict]],
) -> pd.DataFrame:
    """Walk each copy's history and list when it took a registry version.

    A history text that is a version of the copy's own lineage (same blob) is
    a registry text; other texts are local edits and are skipped. The first
    registry text is a ``copy``; a later higher version is an ``update``
    (``versions_skipped`` = versions between the two), a lower one a
    ``downgrade``. ``lag_days`` = commit date − release date of the version
    taken. An inherited commit (a fork's registry history) sets the version the
    copy starts from but is not an event.

    >>> day = lambda text: pd.Timestamp(text, tz="UTC")
    >>> versions = {"L": [{"version_seq": n, "blob": f"v{n}", "normalized_sha": None,
    ...     "release_date": day(d)} for n, d in [(1, "2026-01-10"), (2, "2026-03-01"),
    ...     (3, "2026-05-15")]]}
    >>> history = pd.DataFrame({"repo": ["r"] * 4, "path": ["SKILL.md"] * 4,
    ...     "blob": ["v1", "mine", "v3", "v2"], "status": ["A", "M", "M", "M"],
    ...     "commit": ["c1", "c2", "c3", "c4"], "commit_seq": [1, 2, 3, 4],
    ...     "inherited": [False] * 4, "commit_date": [day("2026-02-01"),
    ...     day("2026-04-01"), day("2026-06-01"), day("2026-07-01")]})
    >>> events = find_sync_events(history, {("r", "SKILL.md"): "L"}, versions)
    >>> events["event_kind"].tolist(), events["to_seq"].tolist()
    (['copy', 'update', 'downgrade'], [1, 3, 2])
    >>> int(events.loc[1, "versions_skipped"])
    1
    """
    rows = []
    for (repo, path), group in _ordered_history_rows(histories).groupby(
        ["repo", "path"], sort=False
    ):
        lineage_id = lineage_of.get((repo, path))
        versions = versions_by_lineage.get(str(lineage_id), [])
        if not versions:
            continue
        current: int | None = None
        for row in group.itertuples(index=False):
            date = _timestamp(row.commit_date)
            found = _seq_of_blob(str(row.blob), versions, date)
            if found is None:
                continue
            seq, release = found
            inherited = getattr(row, "inherited", False)
            if pd.notna(inherited) and bool(inherited):
                current = seq
                continue
            if current is not None and seq == current:
                continue
            kind = "copy" if current is None else ("update" if seq > current else "downgrade")
            rows.append(
                {
                    "repo": repo,
                    "path": path,
                    "lineage_id": lineage_id,
                    "commit": row.commit,
                    "commit_date": date,
                    "event_kind": kind,
                    "from_seq": current,
                    "to_seq": seq,
                    "versions_skipped": seq - current - 1 if kind == "update" else None,
                    "release_date": release,
                    "lag_days": days_between(date, release),
                }  # fmt: skip
            )
            current = seq
    frame = pd.DataFrame(rows, columns=SYNC_EVENT_COLUMNS)
    for column in ("from_seq", "to_seq", "versions_skipped", "lag_days"):
        frame[column] = frame[column].astype("Int64")
    return frame


# Step 8: one row per copy


def measure_copy(
    copy: Mapping[str, object],
    versions: list[dict],
    text_changes: Mapping[int, Mapping[str, object]],
    packages: list[dict],
    script_deltas: Mapping[tuple[str, int], Mapping[str, object]],
    stale_after_days: int,
) -> dict[str, object]:
    """The staleness row of one copy whose held version and package are chosen.

    ``copy`` has the held columns (``held_seq``, ``package_held_seq``,
    ``package_basis``), ``as_of_date``, ``removed`` and the lineage's
    ``registry_removed``/``removed_at``. ``text_changes`` maps a version number
    to its ``lineage_changes`` row. ``days_stale`` counts from the first missed
    release of the text or, when earlier, of the package.
    """
    as_of = _timestamp(copy["as_of_date"])
    held_seq = copy.get("held_seq")
    held = None
    if held_seq is not None and not pd.isna(held_seq):
        position = bisect.bisect_left([v["version_seq"] for v in versions], int(held_seq))
        if position < len(versions) and versions[position]["version_seq"] == int(held_seq):
            held = versions[position]
    row: dict[str, object] = {
        "held_release_date": None if held is None else held["release_date"],
        "latest_seq": latest_version_by(versions, as_of),
    }
    text_behind, first_text = None, None
    missed_text: list[Mapping[str, object]] = []
    if held is not None:
        text_behind, first_text = count_versions_behind(held["version_seq"], as_of, versions)
        missed_seqs = range(held["version_seq"] + 1, held["version_seq"] + 1 + text_behind)
        missed_text = [text_changes[seq] for seq in missed_seqs if seq in text_changes]
    package_behind, first_package, missed_scripts = _package_lag(
        copy, packages, script_deltas, as_of
    )
    firsts = [day for day in (first_text, first_package) if day is not None]
    days_stale = days_between(as_of, min(firsts)) if firsts else None
    behind = None if text_behind is None else text_behind + (package_behind or 0)
    removed_at = _timestamp(copy.get("removed_at"))
    orphaned = _flag(copy.get("registry_removed")) and (
        removed_at is None or as_of is None or removed_at <= as_of
    )
    row.update(
        {
            "skill_md_versions_behind": text_behind,
            "first_missed_release": first_text,
            "package_versions_behind": package_behind,
            "first_missed_package": first_package,
            "days_stale": days_stale,
            **find_missed_updates(missed_text, missed_scripts),
            "registry_removed": orphaned,
            "days_since_removed": days_between(as_of, removed_at) if orphaned else None,
            "copy_state": classify_copy_state(
                behind, days_stale, orphaned, stale_after_days, _flag(copy.get("removed"))
            ),
            "stale_after_days": stale_after_days,
        }
    )
    if copy.get("package_basis") != "measured":
        row["missed_script_updates"] = None
    return row


def _package_lag(
    copy: Mapping[str, object],
    packages: list[dict],
    script_deltas: Mapping[tuple[str, int], Mapping[str, object]],
    as_of: pd.Timestamp | None,
) -> tuple[int | None, pd.Timestamp | None, list[Mapping[str, object]]]:
    """Package versions after the held one up to the as-of date: count, first, script changes."""
    held_seq = copy.get("package_held_seq")
    if copy.get("package_basis") != "measured" or held_seq is None or pd.isna(held_seq):
        return None, None, []
    held = next((package for package in packages if package["seq"] == int(held_seq)), None)
    if held is None:
        return None, None, []
    later = [
        package for package in packages
        if package["seq"] > held["seq"] and (as_of is None or package["commit_date"] <= as_of)
    ]  # fmt: skip
    scripts = [
        script_deltas.get((str(copy["lineage_id"]), package["seq"]),
                          {"scripts_added": 0, "risk_changed": False})  # fmt: skip
        for package in later if package["changed_scripts"]
    ]  # fmt: skip
    return len(later), (later[0]["commit_date"] if later else None), scripts


# Reading inputs


def read_inputs(dev: bool) -> dict[str, object]:
    """Check the input files, then read the small tables and the copy histories.

    A missing file raises ``FileNotFoundError`` naming the command that builds it.
    """
    folder, full = settings.data_dir(dev), settings.data_dir(False)
    flag = " --dev" if dev else ""
    paths: dict[str, Path | str] = {
        "copy_files": folder / "copy_files.parquet",
        "families": folder / "families.parquet",
        "lineages": folder / "lineages.parquet",
        "lineage_versions": folder / "lineage_versions.parquet",
        "lineage_changes": folder / "lineage_changes.parquet",
        "package_versions": folder / "lineage_package_versions.parquet",
        "folder_manifests": folder / "lineage_folder_manifests.parquet",
        "skill_language": full / "skill_language.parquet",
        "registry_changes": history_dir() / "registry_history_changes.parquet",
        "registry_texts": history_dir() / "registry_history_texts.parquet",
        "history_changes": copy_history_dir(dev) / "copied_skill_history_changes.parquet",
        "history_texts": copy_history_dir(dev) / "copied_skill_history_texts.parquet",
        "artifacts": settings.ARTIFACTS_GLOB,
        "siblings": settings.SIBLINGS_GLOB,
    }
    stage4 = f"python -m skill_genealogy.build_registry_lineages{flag}"
    stage6 = f"python -m skill_genealogy.download_copied_skill_history{flag}"
    stage7 = f"python -m skill_genealogy.assign_families{flag}"
    check_inputs_exist(
        paths,
        {
            "copy_files": stage7, "families": stage7, "lineages": stage4,
            "lineage_versions": stage4, "lineage_changes": stage4,
            "package_versions": stage4, "folder_manifests": stage4,
            "skill_language": "python -m skill_genealogy.select_registries --force-languages",
            "registry_changes": "python -m skill_genealogy.download_registry_history",
            "registry_texts": "python -m skill_genealogy.download_registry_history",
            "history_changes": stage6, "history_texts": stage6,
            "artifacts": "python src/data/download_data.py",
            "siblings": "python src/data/download_data.py",
        },
    )  # fmt: skip
    language = read_parquet(paths["skill_language"])
    copies = select_copies_to_measure(
        read_parquet(paths["copy_files"]),
        read_parquet(paths["families"]),
        set(language.loc[language["is_english"], "file_sha"]),
    )
    lineages = read_parquet(paths["lineages"])
    return {
        "paths": paths,
        "copies": copies,
        "lineages": lineages,
        "versions": read_parquet(paths["lineage_versions"]),
        "text_changes": read_parquet(paths["lineage_changes"]),
        "package_versions": read_parquet(paths["package_versions"]),
        "histories": read_copy_histories(dev, copies),
        "dataset_dates": load_dataset_dates(copies),
        "newest_commits": load_registry_newest_commits(paths["registry_changes"], lineages),
    }


def _query(sql: str, tables: Mapping[str, pd.DataFrame], **paths: Path | str) -> pd.DataFrame:
    """Run one stage 10 query with the given tables registered and paths filled in."""
    con = connect_duckdb()
    try:
        con.execute(f"SET memory_limit = '{settings.HISTORY_DUCKDB_MEMORY_LIMIT}'")
        for name, table in tables.items():
            con.register(name, table)
        filled = {name: _quote_path(str(path)) for name, path in paths.items()}
        return con.execute(sql.format(**filled)).df()
    finally:
        con.close()


def load_registry_newest_commits(path: Path | str, lineages: pd.DataFrame) -> dict[str, object]:
    """Query Q10.1: ``registry repo -> newest downloaded commit date``."""
    repos = lineages.loc[:, ["repo"]].drop_duplicates()
    rows = _query(REGISTRY_NEWEST_COMMIT_SQL, {"wanted_repos": repos}, path=path)
    return dict(zip(rows["repo"], rows["newest_commit"], strict=True))


def load_dataset_dates(copies: pd.DataFrame) -> pd.DataFrame:
    """Query Q10.2: ``last_commit_at`` and ``discovered_at`` of each copy file's dataset row."""
    wanted = copies.loc[:, ["repo", "path", "file_sha"]].drop_duplicates()
    return _query(DATASET_DATES_SQL, {"wanted_files": wanted}, path=settings.ARTIFACTS_GLOB)


def load_script_deltas(paths: Mapping[str, Path | str], lineage_ids: set[str]) -> pd.DataFrame:
    """Query Q10.3: per package version with changed scripts, the scripts that differ."""
    wanted = pd.DataFrame({"lineage_id": sorted(lineage_ids)})
    return _query(
        SCRIPT_DELTAS_SQL, {"wanted_lineages": wanted},
        packages=paths["package_versions"], manifests=paths["folder_manifests"],
    )  # fmt: skip


# Building


def assign_as_of_dates(copies: pd.DataFrame, snapshots: Mapping[str, pd.Timestamp]) -> pd.DataFrame:
    """Add ``as_of_date`` and ``as_of_basis`` to every copy (rule 2)."""
    chosen = [
        choose_as_of_date(copy, snapshots.get(str(copy["lineage_id"])))
        for copy in copies.to_dict("records")
    ]
    return copies.assign(
        as_of_date=pd.to_datetime([day for day, _basis in chosen], utc=True),
        as_of_basis=[basis for _day, basis in chosen],
    )


def texts_for_held_versions(
    dev: bool, batch: pd.DataFrame, versions_by_lineage: Mapping[str, list[dict]]
) -> dict[str, str]:
    """Texts A44 needs for the copies of a batch whose blob is no registry version.

    The copy's text (history store, else the dataset) and the texts of the
    lineage versions released by the copy's date (registry store).
    """
    copy_blobs, registry_blobs = set(), set()
    for copy in batch.to_dict("records"):
        versions = versions_by_lineage.get(str(copy["lineage_id"]), [])
        if any(version["blob"] == copy["current_blob"] for version in versions):
            continue
        copy_blobs.add(str(copy["current_blob"]))
        limit = _timestamp(copy["current_date"])
        limit = _timestamp(copy["as_of_date"]) if limit is None else limit
        registry_blobs.update(
            version["blob"] for version in versions
            if limit is None or version["release_date"] <= limit
        )  # fmt: skip
    if not copy_blobs:
        return {}
    texts = load_stored_texts(copy_history_dir(dev) / "copied_skill_history_texts.parquet",
                              copy_blobs)  # fmt: skip
    texts.update(load_dataset_texts(copy_blobs - set(texts)))
    texts.update(load_stored_texts(history_dir() / "registry_history_texts.parquet",
                                   registry_blobs - set(texts)))  # fmt: skip
    return texts


def current_folders(
    paths: Mapping[str, Path | str], batch: pd.DataFrame
) -> tuple[dict[tuple[str, str], tuple], dict[tuple[str, str], pd.Timestamp]]:
    """Each copy's current folder (``(path, sha)`` files) and its last folder commit.

    A copy with a history takes its folder after the newest commit while its
    ``SKILL.md`` is there, else after its last ``SKILL.md`` commit (stage 9c,
    A42). A copy known only from the dataset takes its file listing, when
    GitSkills listed the folder. ``stopped_changing_at`` is the newest commit of
    the folder's own history.
    """
    history = batch.loc[batch["current_basis"] == "history"]
    folder_rows = load_copy_folder_rows(Path(paths["history_changes"]), history)
    wanted = {
        f"{repo}::{skill_folder_of(path)}": {str(commit)}
        for repo, path, commit in zip(
            history["repo"], history["path"], history["current_commit"], strict=True
        )  # fmt: skip
    }
    at_commit, newest = snapshot_copy_folders(folder_rows, wanted)
    folders: dict[tuple[str, str], tuple] = {}
    for repo, path, commit in zip(history["repo"], history["path"], history["current_commit"],
                                  strict=True):  # fmt: skip
        key = f"{repo}::{skill_folder_of(path)}"
        files = newest.get(key, at_commit.get((key, str(commit))))
        if files is not None:
            folders[(repo, path)] = tuple((file[0], file[1]) for file in files)
    dataset = batch.loc[batch["current_basis"] == "dataset"].assign(basis="dataset", version_seq=1)
    if not dataset.empty:
        snapshots = snapshot_folder_manifests(
            dataset, load_sibling_files(dataset), load_listed_folders(dataset)
        )
        for (repo, path, _seq), files in snapshots.items():
            folders[(repo, path)] = tuple((file[0], file[1]) for file in files)
    stopped: dict[tuple[str, str], pd.Timestamp] = {}
    if not folder_rows.empty:
        dates = pd.to_datetime(folder_rows["commit_date"], utc=True, errors="coerce")
        newest_dates = dates.groupby([folder_rows["repo"], folder_rows["skill_folder"]]).max()
        for repo, path in zip(history["repo"], history["path"], strict=True):
            stamp = newest_dates.get((repo, skill_folder_of(path)))
            if stamp is not None and not pd.isna(stamp):
                stopped[(repo, path)] = stamp
    return folders, stopped


def held_packages_for_batch(
    paths: Mapping[str, Path | str],
    batch: pd.DataFrame,
    folders: Mapping[tuple[str, str], tuple],
    package_frame: pd.DataFrame,
) -> dict[tuple[str, str], tuple[int, int, int]]:
    """A46 for the copies of a batch whose folder is known (Query Q10.4)."""
    known = batch.loc[[(r, p) in folders for r, p in zip(batch["repo"], batch["path"],
                                                           strict=True)]]  # fmt: skip
    if known.empty:
        return {}
    limit = known["current_date"].where(known["current_date"].notna(), known["as_of_date"])
    batch_copies = known.loc[:, ["repo", "path", "lineage_id"]].assign(package_limit=limit)
    files = [
        (repo, path, "SKILL.md", blob)
        for repo, path, blob in zip(
            known["repo"], known["path"], known["current_blob"], strict=True
        )  # fmt: skip
    ]
    files += [
        (repo, path, file_path, sha)
        for repo, path in zip(known["repo"], known["path"], strict=True)
        for file_path, sha in folders[(repo, path)]
    ]
    copy_files = pd.DataFrame(files, columns=["repo", "path", "file_path", "sha"])
    lineage_ids = batch_copies.loc[:, ["lineage_id"]].drop_duplicates()
    batch_packages = package_frame.merge(lineage_ids, on="lineage_id")
    shared = _query(
        SHARED_PACKAGE_FILES_SQL,
        {
            "batch_copies": batch_copies,
            "copy_files": copy_files,
            "batch_lineages": lineage_ids,
            "batch_packages": batch_packages,
        },  # fmt: skip
        manifests=paths["folder_manifests"],
    )
    return pick_held_packages(shared)


def _package_frame(packages_by_lineage: Mapping[str, list[dict]]) -> pd.DataFrame:
    """The package index as a table for Query Q10.4."""
    rows = [
        (lineage_id, package["seq"], package["commit_date"], package["skill_md_blob"])
        for lineage_id, packages in packages_by_lineage.items() for package in packages
    ]  # fmt: skip
    return pd.DataFrame(
        rows, columns=["lineage_id", "package_version_seq", "commit_date", "skill_md_blob"]
    )


def _package_file_counts(paths: Mapping[str, Path | str]) -> dict[tuple[str, int], int]:
    """Number of files (besides ``SKILL.md``) of every package version."""
    rows = _query(
        "SELECT lineage_id, package_version_seq, count(*) AS files "
        "FROM read_parquet({path}) GROUP BY ALL", {}, path=paths["folder_manifests"],
    )  # fmt: skip
    return {
        (str(lineage_id), int(seq)): int(count)
        for lineage_id, seq, count in zip(
            rows["lineage_id"], rows["package_version_seq"], rows["files"], strict=True
        )
    }


def build_staleness(
    dev: bool, stale_after_days: int, inputs: Mapping[str, object] | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stage 10 steps 1-8: as-of dates, held versions and packages, lag, state, sync events."""
    progress = Progress(dev, settings.REPO_ROOT / "logs" /
                        f"measure_staleness_{'dev' if dev else 'full'}.progress.json")  # fmt: skip
    inputs = inputs or read_inputs(dev)
    paths = inputs["paths"]
    lineages = inputs["lineages"]
    # Step 1: current text and as-of date of every copy
    progress.stage("current texts", f"{len(inputs['copies']):,} copies")
    copies = current_copies(inputs["copies"], inputs["histories"], inputs["dataset_dates"])
    snapshots = registry_snapshot_dates(
        lineages, inputs["newest_commits"], settings.REGISTRY_MINED_FROM
    )
    copies = assign_as_of_dates(copies, snapshots)
    removed = lineages.loc[:, ["lineage_id", "registry_removed", "removed_at"]]
    copies = copies.merge(removed, on="lineage_id", how="left")
    versions_by_lineage = index_lineage_versions(inputs["versions"])
    packages_by_lineage = index_packages(inputs["package_versions"], inputs["versions"])
    package_frame = _package_frame(packages_by_lineage)
    file_counts = _package_file_counts(paths)
    # Step 5a: script changes of every package version (Q10.3) and their flags (A16)
    deltas = load_script_deltas(paths, set(copies["lineage_id"].astype(str)))
    shas = set(deltas["old_sha"].dropna()) | set(deltas["new_sha"].dropna())
    progress.stage("flag registry scripts", f"{len(deltas):,} script changes, {len(shas):,} texts")
    script_deltas = script_deltas_by_package(deltas, load_script_flags(dev, shas))
    # Steps 2 and 4: held version (A44) and held package (A46), in batches
    held_rows: list[dict] = []
    size = settings.COPY_CHANGE_FILE_BATCH
    batches = math.ceil(len(copies) / size)
    progress.stage("held versions and packages", f"{batches} batches of {size:,} copies")
    progress.start_compare()
    for number, batch in enumerate(batches_of_files(copies, size), start=1):
        texts = texts_for_held_versions(dev, batch, versions_by_lineage)
        folders, stopped = current_folders(paths, batch)
        packages = held_packages_for_batch(paths, batch, folders, package_frame)
        for copy in batch.to_dict("records"):
            key = (copy["repo"], copy["path"])
            versions = versions_by_lineage.get(str(copy["lineage_id"]), [])
            seq, basis, similarity = choose_held_version(copy, versions, texts)
            held_package = packages.get(key)
            file_count = 0 if held_package is None else file_counts.get(
                (str(copy["lineage_id"]), held_package[0]), 0)  # fmt: skip
            held_rows.append({
                "repo": key[0], "path": key[1], "held_seq": seq, "held_basis": basis,
                "held_similarity": similarity,
                "package_held_seq": None if held_package is None else held_package[0],
                "package_basis": package_basis(key in folders, held_package, file_count),
                "stopped_changing_at": stopped.get(key),
            })  # fmt: skip
        progress.batch(number, batches, min(number * size, len(copies)), len(copies),
                       len(held_rows), "staleness", found_label="copies_held")  # fmt: skip
    copies = copies.merge(pd.DataFrame(held_rows), on=["repo", "path"], how="left")
    # Steps 3, 5 and 6: releases missed (A45), missed content, state (A47)
    progress.stage("measure lag")
    staleness = measure_all_copies(
        copies, versions_by_lineage, inputs["text_changes"], packages_by_lineage,
        script_deltas, stale_after_days,
    )  # fmt: skip
    # Step 7: sync events (A48)
    progress.stage("sync events")
    keys = zip(copies["repo"], copies["path"], strict=True)
    lineage_of = dict(zip(keys, copies["lineage_id"], strict=True))
    events = find_sync_events(inputs["histories"], lineage_of, versions_by_lineage)
    progress.finish()
    return staleness, events


def measure_all_copies(
    copies: pd.DataFrame,
    versions_by_lineage: Mapping[str, list[dict]],
    text_changes: pd.DataFrame,
    packages_by_lineage: Mapping[str, list[dict]],
    script_deltas: Mapping[tuple[str, int], Mapping[str, object]],
    stale_after_days: int,
) -> pd.DataFrame:
    """``measure_copy`` for every copy, then the output columns in order."""
    changes_by_lineage: dict[str, dict[int, dict]] = defaultdict(dict)
    for change in text_changes.to_dict("records"):
        changes_by_lineage[str(change["lineage_id"])][int(change["to_seq"])] = change
    rows = []
    for copy in copies.to_dict("records"):
        lineage_id = str(copy["lineage_id"])
        measured = measure_copy(
            copy, versions_by_lineage.get(lineage_id, []), changes_by_lineage.get(lineage_id, {}),
            packages_by_lineage.get(lineage_id, []), script_deltas, stale_after_days,
        )  # fmt: skip
        rows.append({**copy, **measured})
    frame = pd.DataFrame(rows)
    frame["stopped_changing_at"] = frame["stopped_changing_at"].where(
        frame["current_basis"] == "history"
    )
    for column in ("held_seq", "latest_seq", "skill_md_versions_behind", "package_held_seq",
                   "package_versions_behind", "missed_script_updates", "days_stale",
                   "days_since_removed"):  # fmt: skip
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Int64")
    for column in ("held_release_date", "first_missed_release", "first_missed_package",
                   "stopped_changing_at", "as_of_date"):  # fmt: skip
        frame[column] = pd.to_datetime(frame[column], utc=True)
    frame["copied_before_release"] = frame["copied_before_release"].fillna(False).astype(bool)
    return frame.loc[:, STALENESS_COLUMNS]


# Summary


def text_states(staleness: pd.DataFrame, blob_of: pd.Series) -> pd.Series:
    """One state per text: the most severe state of the files that hold it.

    >>> frame = pd.DataFrame({"copy_state": ["up_to_date", "stale", "behind"]})
    >>> text_states(frame, pd.Series(["t", "t", "u"])).to_dict()
    {'t': 'stale', 'u': 'behind'}
    """
    rank = {state: number for number, state in enumerate(COPY_STATES)}
    ranked = pd.DataFrame({"blob": blob_of.to_numpy(),
                           "rank": staleness["copy_state"].map(rank).to_numpy()})  # fmt: skip
    worst = ranked.groupby("blob")["rank"].max()
    return worst.map(dict(enumerate(COPY_STATES)))


def _share(count: int, total: int) -> float:
    """``count / total`` rounded to 4 places; 0 for an empty total."""
    return round(count / total, 4) if total else 0.0


def summary_numbers(staleness: pd.DataFrame, events: pd.DataFrame) -> dict[str, object]:
    """The counts the run prints: states, splits, medians, missed content, events."""
    total = len(staleness)
    states = staleness["copy_state"].value_counts()
    numbers: dict[str, object] = {"copy files": total}
    for state in COPY_STATES:
        count = int(states.get(state, 0))
        numbers[f"state {state}"] = f"{count:,} ({_share(count, total)})"
    if len(staleness):
        texts = text_states(staleness, staleness["current_blob"]).value_counts()
        numbers["texts by worst state"] = {state: int(texts.get(state, 0)) for state in COPY_STATES}
    for group, frame in staleness.groupby("arrival_group", dropna=False):
        shares = frame["copy_state"].value_counts(normalize=True).round(4).to_dict()
        numbers[f"arrival {group}"] = f"{len(frame):,} {shares}"
    exact = staleness.loc[(staleness["link"] == "exact") & staleness["days_stale"].notna()]
    numbers["median days stale, exact copies behind"] = (
        float(exact["days_stale"].median()) if len(exact) else None
    )
    behind = staleness["copy_state"].isin(["behind", "stale"])
    for name in ("missed_commands", "missed_network", "missed_scripts", "missed_script_updates"):
        numbers[f"behind with {name}"] = int((behind & (staleness[name].fillna(0) > 0)).sum())
    numbers["behind with missed_risky"] = int((behind & staleness["missed_risky"]).sum())
    numbers["behind only on the package"] = int(
        (behind & (staleness["skill_md_versions_behind"] == 0)).sum()
    )
    numbers["held basis"] = staleness["held_basis"].value_counts().to_dict()
    numbers["package basis"] = staleness["package_basis"].value_counts().to_dict()
    numbers["as-of basis"] = staleness["as_of_basis"].value_counts().to_dict()
    numbers["sync events"] = events["event_kind"].value_counts().to_dict()
    numbers["sync events dated before their release"] = int((events["lag_days"] < 0).sum())
    lags = events.groupby("event_kind")["lag_days"].median()
    numbers["median lag days by event"] = {kind: float(lag) for kind, lag in lags.items()}
    return numbers


# Outputs


def save_outputs(dev: bool, staleness: pd.DataFrame, events: pd.DataFrame) -> None:
    """Write both outputs, sorted by repo and path."""
    paths = output_paths(dev)
    write_parquet(staleness, paths["copy_staleness"], sort_by=["repo", "path"])
    write_parquet(events, paths["sync_events"], sort_by=["repo", "path", "commit_date"])


def main() -> None:
    """Run stage 10: ``--dev``, ``--force``, ``--stale-days N``."""
    parser = build_arg_parser("Measure how far each copy is behind its registry skill (stage 10).")
    parser.add_argument(
        "--stale-days", type=int, default=None,
        help=f"days behind before a copy is stale (default {settings.STALE_AFTER_DAYS})",
    )  # fmt: skip
    args = parser.parse_args()
    stale_after_days = (
        settings.STALE_AFTER_DAYS if args.stale_days is None else int(args.stale_days)
    )
    paths = output_paths(args.dev)
    if outputs_exist(list(paths.values())) and not args.force:
        print("stage 10 outputs exist; pass --force to rebuild")
        staleness = read_parquet(paths["copy_staleness"])
        events = read_parquet(paths["sync_events"])
        print_summary("copy staleness (saved)", summary_numbers(staleness, events))
        return
    started = time.perf_counter()
    staleness, events = build_staleness(args.dev, stale_after_days)
    save_outputs(args.dev, staleness, events)
    print_summary(
        f"copy staleness (stale after {stale_after_days} days)",
        {
            **summary_numbers(staleness, events),
            "seconds": round(time.perf_counter() - started, 1),
        },  # fmt: skip
    )
    print_sample_records(staleness, "sample copy_staleness rows")
    print_sample_records(events, "sample sync_events rows")


if __name__ == "__main__":
    main()
