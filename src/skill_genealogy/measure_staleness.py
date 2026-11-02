"""How far behind a copied skill is, in text and in scripts.

What: the registry version a copy still holds, how many releases it missed,
how long ago the first miss was, and whether it stopped following the registry.
Inputs: English copy files, lineage versions, changes, packages and manifests,
copy histories, registry history, and the signature files.
Outputs: ``copy_staleness.parquet`` and ``sync_events.parquet`` under
``data_dir(dev)``.
Run: ``python -m skill_genealogy.measure_staleness --dev --stale-days 90``.
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

import skill_genealogy.settings as settings
from skill_genealogy.assign_families import (
    _boilerplate_lines,
    _bundle_arrays,
    _jaccard_fast,
    _signatures_for_ids,
)
from skill_genealogy.compare_texts import file_category, flag_script_risks
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
from skill_genealogy.find_copies import load_signatures, signature_for_text
from skill_genealogy.measure_copy_changes import (
    _fetch,
    _prepare_duckdb,
    _quote_path,
    _read_folder_history,
    _read_sibling_files,
    _remove_tree,
    assemble_copy_folder_manifests,
)
from skill_genealogy.settings import data_dir

# Stage 9 keeps a literal IN list under DuckDB's query-size limit. Same cap here.
_TEXT_SHA_BATCH = 200
_STATE_ORDER = ("up_to_date", "behind", "stale", "orphaned")
_STATE_RANK = {"up_to_date": 0, "orphaned": 1, "behind": 2, "stale": 3}
_ZERO_BLOB = "0" * 40

STALENESS_COLUMNS = [
    "repo",
    "path",
    "lineage_id",
    "held_seq",
    "held_basis",
    "held_release_date",
    "as_of_date",
    "as_of_basis",
    "latest_seq",
    "skill_md_versions_behind",
    "first_missed_release",
    "days_stale",
    "package_held_seq",
    "package_versions_behind",
    "missed_script_updates",
    "missed_substantive",
    "missed_commands",
    "missed_network",
    "missed_scripts",
    "missed_risky",
    "stopped_changing_at",
    "copied_before_release",
    "copy_state",
    "days_since_removed",
    "stale_after_days",
    "current_sha",
]
EVENT_COLUMNS = [
    "repo",
    "path",
    "lineage_id",
    "commit",
    "commit_date",
    "event_kind",
    "from_seq",
    "to_seq",
    "versions_skipped",
    "release_date",
    "lag_days",
]
_MISSED_KEYS = (
    "missed_substantive",
    "missed_commands",
    "missed_network",
    "missed_scripts",
    "missed_risky",
    "missed_script_updates",
)

# Query Q10.1, see QUERIES.md
COPIES_SQL = """
SELECT
    copies.repo,
    copies.path,
    copies.file_sha,
    copies.lineage_id
FROM read_parquet({copies}) AS copies
JOIN read_parquet({language}) AS language
  ON copies.file_sha = language.file_sha
ANTI JOIN (
    SELECT file_sha
    FROM read_parquet({families})
    WHERE coalesce(registry_self, false)
) AS registry_only
  ON copies.file_sha = registry_only.file_sha
WHERE language.is_english
  AND copies.lineage_id IS NOT NULL
  AND copies.repo IS NOT NULL
  AND copies.path IS NOT NULL
"""

# Query Q10.2, see QUERIES.md
WILD_HISTORY_SQL = """
SELECT
    wild.repo,
    wild.target_path AS path,
    wild.commit,
    wild.commit_at AS commit_date,
    wild.blob
FROM read_parquet({path}) AS wild
JOIN copy_keys
  ON wild.repo = copy_keys.repo
 AND wild.target_path = copy_keys.path
WHERE wild.blob IS NOT NULL
  AND wild.blob <> ''
  AND lower(wild.blob) <> '{zero}'
  AND coalesce(wild.status, '') <> 'D'
"""

# Query Q10.3, see QUERIES.md
COPIED_HISTORY_SQL = """
SELECT
    copied.repo,
    CASE
        WHEN copied.skill_folder IS NULL OR copied.skill_folder IN ('', '.')
            THEN copied.file_path
        ELSE copied.skill_folder || '/' || copied.file_path
    END AS path,
    copied.commit,
    copied.commit_date,
    copied.new_sha AS blob
FROM read_parquet({path}) AS copied
JOIN copy_keys
  ON copied.repo = copy_keys.repo
 AND (
    CASE
        WHEN copied.skill_folder IS NULL OR copied.skill_folder IN ('', '.')
            THEN copied.file_path
        ELSE copied.skill_folder || '/' || copied.file_path
    END
 ) = copy_keys.path
WHERE copied.is_skill_md
  AND coalesce(copied.status, '') <> 'D'
  AND copied.new_sha IS NOT NULL
  AND copied.new_sha <> ''
  AND lower(copied.new_sha) <> '{zero}'
"""

# Query Q10.4, see QUERIES.md
REGISTRY_SNAPSHOT_SQL = """
SELECT max(commit_date) AS snapshot_at
FROM read_parquet({path})
WHERE commit_date IS NOT NULL
  AND commit_date <> ''
"""

# Query Q10.5, see QUERIES.md
# Dates only. content is not selected, so the scan does not read skill text.
FILE_DATES_SQL = """
SELECT
    artifacts.repo_full_name AS repo,
    artifacts.path AS path,
    any_value(artifacts.discovered_at) AS discovered_at,
    any_value(artifacts.last_commit_at) AS last_commit_at
FROM read_parquet({path}) AS artifacts
JOIN copy_keys
  ON artifacts.repo_full_name = copy_keys.repo
 AND artifacts.path = copy_keys.path
GROUP BY artifacts.repo_full_name, artifacts.path
"""

# Query Q10.6, see QUERIES.md
REGISTRY_COMMITS_SQL = """
SELECT DISTINCT lower(commit) AS commit
FROM read_parquet({path})
WHERE commit IS NOT NULL
  AND commit <> ''
"""

# Query Q10.7, see QUERIES.md
SCRIPT_TEXTS_SQL = """
SELECT sha AS blob, any_value(text) AS text
FROM read_parquet({path})
WHERE sha IN ({sha_list})
GROUP BY sha
"""

# Query Q10.8, see QUERIES.md
# History stores only. The dataset scan is Q10.9, and only for blobs still missing.
BLOB_TEXTS_SQL = """
SELECT blob, any_value(text) AS text
FROM (
    SELECT sha AS blob, text
    FROM read_parquet({copied})
    WHERE sha IN ({sha_list})
    UNION ALL
    SELECT blob, content AS text
    FROM read_parquet({wild})
    WHERE blob IN ({sha_list})
    UNION ALL
    SELECT sha AS blob, text
    FROM read_parquet({registry})
    WHERE sha IN ({sha_list})
) AS found
GROUP BY blob
"""

# Query Q10.9, see QUERIES.md
# content is selected only for the blob ids that history did not store.
ARTIFACT_TEXTS_SQL = """
SELECT file_sha AS blob, any_value(content) AS text
FROM read_parquet({path})
WHERE file_sha IN ({sha_list})
  AND content IS NOT NULL
GROUP BY file_sha
"""


def _frame(value: object) -> pd.DataFrame:
    """Return a DataFrame. Anything else becomes an empty frame."""
    if isinstance(value, pd.DataFrame):
        return value
    return pd.DataFrame()


def _empty_sigs() -> tuple[list[str], np.ndarray, np.ndarray]:
    """An empty signature bundle: no ids, a (0, 128) matrix, no sizes."""
    width = settings.MINHASH_NUM_PERM
    return [], np.zeros((0, width), dtype=np.uint64), np.zeros(0, dtype=np.int32)


def _missing(value: object) -> bool:
    """True when a cell is None, NaN, or NaT."""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, (list, tuple, dict, set)):
        return False
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    if isinstance(missing, bool):
        return missing
    return False


def _as_date(value: object) -> date | None:
    """Calendar day in UTC, or None when the value is missing.

    Example:
    >>> _as_date("2026-10-23T15:00:00Z").isoformat()
    '2026-10-23'
    >>> _as_date(None) is None
    True
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if _missing(value):
        return None
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if not isinstance(parsed, pd.Timestamp) or pd.isna(parsed):
        return None
    return parsed.date()


def _as_int(value: object) -> int | None:
    """Integer cell, or None when it is missing."""
    if _missing(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sha(value: object) -> str:
    """Lowercase blob id, or an empty string when the cell is missing."""
    if _missing(value):
        return ""
    text = str(value).strip().lower()
    if not text or text == _ZERO_BLOB or text == "nan":
        return ""
    return text


def _version_id(lineage_id: object, version_seq: object) -> str:
    """Signature id ``lineage_id#version_seq``, the same key stage 5 stores."""
    return f"{lineage_id}#{int(version_seq)}"


def _timeline_versions() -> pd.DataFrame:
    """Registry v1 on 10 Jan, v2 on 1 Mar, v3 on 15 May."""
    return pd.DataFrame(
        [
            {
                "lineage_id": "reg::skill",
                "version_seq": 1,
                "release_date": "2026-01-10",
                "blob": "v1",
            },
            {
                "lineage_id": "reg::skill",
                "version_seq": 2,
                "release_date": "2026-03-01",
                "blob": "v2",
            },
            {
                "lineage_id": "reg::skill",
                "version_seq": 3,
                "release_date": "2026-05-15",
                "blob": "v3",
            },
        ]
    )


def _versions_for_copy(lineage_versions: pd.DataFrame, lineage_id: object) -> pd.DataFrame:
    """Rows for one lineage. A frame with no lineage column is used as it is."""
    frame = lineage_versions if isinstance(lineage_versions, pd.DataFrame) else pd.DataFrame()
    if frame.empty or "lineage_id" not in frame.columns or _missing(lineage_id):
        return frame
    return frame.loc[frame["lineage_id"].astype(str) == str(lineage_id)]


def _exact_version_seq(copy: dict, versions: pd.DataFrame) -> int | None:
    """Highest version whose blob or normalised sha is the copy's current text."""
    if versions.empty:
        return None
    blob = _sha(copy.get("blob") or copy.get("file_sha"))
    normalised = "" if _missing(copy.get("normalized_sha")) else str(copy.get("normalized_sha"))
    matched: list[int] = []
    blobs = versions["blob"] if "blob" in versions.columns else [""] * len(versions)
    hashes = (
        versions["normalized_sha"]
        if "normalized_sha" in versions.columns
        else [""] * len(versions)
    )
    sequences = versions["version_seq"] if "version_seq" in versions.columns else []
    for sequence, version_blob, version_hash in zip(sequences, blobs, hashes, strict=False):
        number = _as_int(sequence)
        if number is None:
            continue
        same_blob = bool(blob) and _sha(version_blob) == blob
        same_text = bool(normalised) and str(version_hash) == normalised
        if same_blob or same_text:
            matched.append(number)
    if not matched:
        return None
    return max(matched)


def _eligible_release_rows(copy: dict, versions: pd.DataFrame) -> pd.DataFrame:
    """Versions released on or before the copy date. Undated versions are left out."""
    copy_day = _as_date(copy.get("copy_date"))
    if copy_day is None or versions.empty or "release_date" not in versions.columns:
        return versions.iloc[0:0]
    days = versions["release_date"].map(_as_date)
    return versions.loc[days.notna() & (days <= copy_day)]


def _similarity_scores(blob: str, lineage_id: object, sequences: list[int], sigs) -> list[float]:
    """Estimated Jaccard of one copy blob against each version signature.

    The score is the share of 128 MinHash values that agree, the same number
    ``estimate_jaccard`` returns. A missing signature scores 0.
    """
    if not sequences:
        return []
    ids, matrix, sizes = _bundle_arrays(sigs)
    index = {name: position for position, name in enumerate(ids)}
    query_at = index.get(blob)
    if query_at is None:
        return [0.0] * len(sequences)
    positions = [index.get(_version_id(lineage_id, seq)) for seq in sequences]
    scores = [0.0] * len(sequences)
    valid = [position is not None for position in positions]
    if not any(valid):
        return scores
    take = [position for position in positions if position is not None]
    part = _jaccard_fast(
        matrix[query_at],
        int(sizes[query_at]),
        matrix[np.asarray(take, dtype=np.int64)],
        sizes[np.asarray(take, dtype=np.int64)],
    )
    cursor = 0
    for offset, is_valid in enumerate(valid):
        if not is_valid:
            continue
        scores[offset] = float(part[cursor])
        cursor += 1
    return scores


def _most_similar_seq(copy: dict, eligible: pd.DataFrame, sigs) -> int | None:
    """Highest score, then newest release, then highest version number."""
    if eligible.empty or "version_seq" not in eligible.columns:
        return None
    sequences = [_as_int(value) for value in eligible["version_seq"]]
    rows = eligible.to_dict("records")
    kept = [(seq, row) for seq, row in zip(sequences, rows, strict=False) if seq]
    if not kept:
        return None
    blob = _sha(copy.get("blob") or copy.get("file_sha"))
    scores = _similarity_scores(blob, copy.get("lineage_id"), [seq for seq, _row in kept], sigs)
    ranked: list[tuple[float, date, int]] = []
    for score, (seq, row) in zip(scores, kept, strict=True):
        day = _as_date(row.get("release_date")) or date.min
        ranked.append((score, day, seq))
    ranked.sort(reverse=True)
    return ranked[0][2]


# Algorithm A44, see CODE_FLOW.md
# The card called this A41. A41 is already the stage 9b change notes.
def choose_held_version(
    copy: dict, lineage_versions: pd.DataFrame, sigs
) -> tuple[int | None, str]:
    """Return the registry version the copy holds now, and why.

    Identical current text (same blob, or the same normalised sha when the
    copy carries one) is ``exact_current``, with no date limit. Otherwise
    the most similar version released on or before the copy date is
    ``similar_before_date``. Equal scores keep the newest release. None of
    those returns ``(None, "copied_before_release")``.

    Registry v1 is 10 Jan, v2 is 1 Mar, v3 is 15 May. A copy whose blob is
    v1 holds version 1. A different blob dated 1 Feb holds version 1 by
    date. A different blob dated 1 Jan was copied before every release.

    Example:
    >>> versions = _timeline_versions()
    >>> choose_held_version(
    ...     {"lineage_id": "reg::skill", "blob": "v1", "copy_date": "2026-02-01"},
    ...     versions, _empty_sigs(),
    ... )
    (1, 'exact_current')
    >>> choose_held_version(
    ...     {"lineage_id": "reg::skill", "blob": "other", "copy_date": "2026-02-01"},
    ...     versions, _empty_sigs(),
    ... )
    (1, 'similar_before_date')
    >>> choose_held_version(
    ...     {"lineage_id": "reg::skill", "blob": "other", "copy_date": "2026-01-01"},
    ...     versions, _empty_sigs(),
    ... )
    (None, 'copied_before_release')
    """
    versions = _versions_for_copy(lineage_versions, copy.get("lineage_id"))
    exact = _exact_version_seq(copy, versions)
    if exact is not None:
        return exact, "exact_current"
    chosen = _most_similar_seq(copy, _eligible_release_rows(copy, versions), sigs)
    if chosen is None:
        return None, "copied_before_release"
    return chosen, "similar_before_date"


def _has_history(copy: dict) -> bool:
    """True when the copy dict says history was downloaded."""
    value = copy.get("has_history", copy.get("history_downloaded"))
    return value is True or value == 1


def choose_as_of_date(copy: dict, registry_snapshot_date) -> tuple[date, str]:
    """Return the day we judge the copy against, and which clock that day is.

    Downloaded history uses the registry snapshot (newest registry commit).
    A file with no history uses ``discovered_at``. When that cell is empty,
    ``last_commit_at`` is used and the basis is still ``dataset_capture``.

    Example:
    >>> choose_as_of_date(
    ...     {"has_history": True, "discovered_at": "2026-04-01"}, "2026-10-23",
    ... )
    (datetime.date(2026, 10, 23), 'registry_snapshot')
    >>> choose_as_of_date(
    ...     {"has_history": False, "discovered_at": "2026-04-01"}, "2026-10-23",
    ... )
    (datetime.date(2026, 4, 1), 'dataset_capture')
    """
    snapshot = _as_date(registry_snapshot_date)
    if _has_history(copy) and snapshot is not None:
        return snapshot, "registry_snapshot"
    captured = _as_date(copy.get("discovered_at")) or _as_date(copy.get("last_commit_at"))
    if captured is not None:
        return captured, "dataset_capture"
    if snapshot is not None:
        return snapshot, "dataset_capture"
    return date(1970, 1, 1), "dataset_capture"


def _release_days(lineage_versions: pd.DataFrame) -> list[date | None]:
    """One calendar day per version row, None when the release date is missing."""
    if not isinstance(lineage_versions, pd.DataFrame) or lineage_versions.empty:
        return []
    if "release_date" not in lineage_versions.columns:
        return []
    return [_as_date(value) for value in lineage_versions["release_date"]]


def _behind_from_dates(
    held: date | None, as_of: date | None, days: list[date | None]
) -> tuple[int, date | None]:
    """Count release days after ``held`` and on or before ``as_of``.

    A missing held day counts every dated release on or before ``as_of``:
    the copy never held a version. The second value is the earliest of them.
    """
    if as_of is None:
        return 0, None
    missed = [
        day
        for day in days
        if day is not None and day <= as_of and (held is None or day > held)
    ]
    if not missed:
        return 0, None
    return len(missed), min(missed)


# Algorithm A45, see CODE_FLOW.md
# The card called this A42. A42 is already the stage 9c folder snapshot.
def count_versions_behind(
    held_release_date, as_of_date, lineage_versions
) -> tuple[int, date | None]:
    """Return how many releases were missed, and the date of the first one.

    A release counts when its date is after the held release and on or
    before the as-of date. The held release itself does not count. With no
    held date, every release on or before the as-of date counts.

    v1 is 10 Jan, v2 is 1 Mar, v3 is 15 May. Holding v1 as of 23 Oct misses
    v2 and v3, and the first miss is 1 Mar.

    Example:
    >>> count, first = count_versions_behind("2026-01-10", "2026-10-23", _timeline_versions())
    >>> (count, first.isoformat())
    (2, '2026-03-01')
    """
    return _behind_from_dates(
        _as_date(held_release_date), _as_date(as_of_date), _release_days(lineage_versions)
    )


def _column(frame: pd.DataFrame, *names: str) -> str | None:
    """The first of ``names`` that the frame has."""
    for name in names:
        if name in frame.columns:
            return name
    return None


def _pairs_from_manifest(copy_manifest) -> set[tuple[str, str]]:
    """``(path, sha)`` pairs in a copy folder or a registry manifest."""
    if isinstance(copy_manifest, pd.DataFrame):
        frame = copy_manifest
    else:
        frame = pd.DataFrame(copy_manifest or [])
    if frame.empty:
        return set()
    path_name = _column(frame, "file_path", "path")
    if path_name is None or "sha" not in frame.columns:
        return set()
    pairs: set[tuple[str, str]] = set()
    for path, sha in zip(frame[path_name], frame["sha"], strict=False):
        blob = _sha(sha)
        if not blob or _missing(path):
            continue
        pairs.add((str(path), blob))
    return pairs


def _package_records(package_versions: pd.DataFrame) -> list[tuple[int, date | None, bool]]:
    """``(seq, release day, changed_scripts)`` for each package row."""
    if not isinstance(package_versions, pd.DataFrame) or package_versions.empty:
        return []
    if "package_version_seq" not in package_versions.columns:
        return []
    date_name = _column(package_versions, "commit_date", "release_date")
    records: list[tuple[int, date | None, bool]] = []
    for row in package_versions.to_dict("records"):
        seq = _as_int(row.get("package_version_seq"))
        if seq is None:
            continue
        day = _as_date(row.get(date_name)) if date_name else None
        changed = bool(row.get("changed_scripts")) and not _missing(row.get("changed_scripts"))
        records.append((seq, day, changed))
    return records


def _manifest_sets(manifests: pd.DataFrame) -> dict[int, set[tuple[str, str]]]:
    """Package version number to the set of ``(path, sha)`` pairs."""
    found: dict[int, set[tuple[str, str]]] = {}
    if not isinstance(manifests, pd.DataFrame) or manifests.empty:
        return found
    if "package_version_seq" not in manifests.columns:
        return found
    for seq, group in manifests.groupby("package_version_seq", sort=False):
        number = _as_int(seq)
        if number is None:
            continue
        found[number] = _pairs_from_manifest(group)
    return found


def _held_package_seq(
    copy_pairs: set[tuple[str, str]],
    packages: list[tuple[int, date | None, bool]],
    files: dict[int, set[tuple[str, str]]],
    copy_day: date | None,
) -> int | None:
    """Package released on or before ``copy_day`` with the most shared files.

    Ties keep the newest release day, then the higher version number.
    A copy with no date matches no package. Shared files are ``(path, sha)``.
    """
    if copy_day is None:
        return None
    best_seq: int | None = None
    best_overlap = -1
    best_day: date | None = None
    for seq, day, _changed in packages:
        if day is None or day > copy_day:
            continue
        overlap = len(copy_pairs & files.get(seq, set()))
        newer = best_day is None or day > best_day or (day == best_day and seq > (best_seq or 0))
        if overlap > best_overlap or (overlap == best_overlap and newer):
            best_overlap = overlap
            best_seq = seq
            best_day = day
    return best_seq


# Algorithm A46, see CODE_FLOW.md
# The card called this A43. A43 is already the registry package at a date.
def choose_held_package_version(
    copy_manifest, package_versions, manifests, copy_date
) -> int | None:
    """Return the package version that shares the most files with the copy.

    Only packages released on or before ``copy_date`` compete. The score is
    how many ``(path, sha)`` pairs they share. Ties keep the newest package.
    No package that early returns None.

    Example:
    >>> packages = pd.DataFrame([
    ...     {"package_version_seq": 1, "commit_date": "2026-01-10"},
    ...     {"package_version_seq": 2, "commit_date": "2026-05-15", "changed_scripts": True},
    ... ])
    >>> manifests = pd.DataFrame([
    ...     {"package_version_seq": 1, "path": "scripts/deploy.sh", "sha": "old"},
    ...     {"package_version_seq": 2, "path": "scripts/deploy.sh", "sha": "fixed"},
    ... ])
    >>> copy = pd.DataFrame([{"path": "scripts/deploy.sh", "sha": "old"}])
    >>> choose_held_package_version(copy, packages, manifests, "2026-06-30")
    1
    """
    packages = package_versions if isinstance(package_versions, pd.DataFrame) else pd.DataFrame()
    files = manifests if isinstance(manifests, pd.DataFrame) else pd.DataFrame(manifests or [])
    return _held_package_seq(
        _pairs_from_manifest(copy_manifest),
        _package_records(packages),
        _manifest_sets(files),
        _as_date(copy_date),
    )


def _added_count(value: object) -> int:
    """How many items a change list holds. A number is used as itself."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or text in {"[]", "nan", "None"}:
            return 0
        if text.startswith("[") and text.endswith("]"):
            inner = text[1:-1].strip()
            if not inner:
                return 0
            return len([part for part in inner.split(",") if part.strip()])
        return 1
    if isinstance(value, (list, tuple, set)):
        return len(value)
    if isinstance(value, np.ndarray):
        return int(value.size)
    if _missing(value):
        return 0
    return 0


def _row_risky(commands: int, network: int, scripts: int, risk_changed: bool) -> bool:
    """True when a missed update added a command, network, or script, or a flag changed."""
    return commands > 0 or network > 0 or scripts > 0 or risk_changed


def _count_skill_misses(
    totals: dict[str, int],
    held_seq: int | None,
    as_of: date,
    changes: list[dict],
) -> None:
    """Add missed SKILL.md steps to ``totals``.

    A change counts when ``to_seq`` is after ``held_seq`` and its release is
    on or before ``as_of``. A missing held seq counts every such change.
    ``missed_risky`` counts one when the step added a command, a network ref,
    or a script.
    """
    held = _as_int(held_seq)
    for row in changes:
        release = _as_date(row.get("release_date"))
        to_seq = _as_int(row.get("to_seq"))
        if release is None or release > as_of:
            continue
        if held is not None and (to_seq is None or to_seq <= held):
            continue
        commands = _added_count(row.get("commands_added"))
        network = _added_count(row.get("network_refs_added"))
        scripts = _added_count(row.get("scripts_mentioned_added"))
        if str(row.get("state") or "") == "substantive":
            totals["missed_substantive"] += 1
        totals["missed_commands"] += commands
        totals["missed_network"] += network
        totals["missed_scripts"] += scripts
        if _row_risky(commands, network, scripts, False):
            totals["missed_risky"] += 1


def _count_folder_misses(
    totals: dict[str, int],
    as_of: date,
    packages: list[dict],
    folder_changes: list[dict],
) -> None:
    """Add missed package updates and script-folder deltas to ``totals``.

    ``missed_script_updates`` is one per later package with ``changed_scripts``.
    A folder row adds its risky-file count. When that count is empty, the row
    still adds one if it gained a command, a network ref, a script, or a risk
    flag.
    """
    for row in packages:
        release = _as_date(row.get("commit_date") or row.get("release_date"))
        if release is not None and release > as_of:
            continue
        changed = row.get("changed_scripts")
        if bool(changed) and not _missing(changed):
            totals["missed_script_updates"] += 1
    for row in folder_changes:
        release = _as_date(row.get("release_date") or row.get("commit_date"))
        if release is not None and release > as_of:
            continue
        totals["missed_substantive"] += _added_count(row.get("substantive"))
        commands = _added_count(row.get("commands_added"))
        network = _added_count(row.get("network_refs_added"))
        scripts = _added_count(row.get("scripts_added", row.get("scripts_mentioned_added")))
        totals["missed_commands"] += commands
        totals["missed_network"] += network
        totals["missed_scripts"] += scripts
        flag = row.get("risk_flag_changed")
        risk_changed = bool(flag) and not _missing(flag)
        risky_files = _added_count(row.get("risky"))
        if risky_files:
            totals["missed_risky"] += risky_files
        elif _row_risky(commands, network, scripts, risk_changed):
            totals["missed_risky"] += 1


def _missed_counts(
    held_seq: int | None,
    as_of: date | None,
    changes: list[dict],
    packages: list[dict],
    folder_changes: list[dict],
) -> dict[str, int]:
    """Sum missed skill-text changes and missed script changes.

    ``packages`` are the package versions after the held package. A skill
    change counts when ``to_seq`` is after ``held_seq`` and its release is
    on or before ``as_of``. A missing held seq counts every such change.
    missed_risky counts one per SKILL.md change that added a command, network
    ref or script, plus one per risky script file in the missed package folders.
    """
    totals = {key: 0 for key in _MISSED_KEYS}
    if as_of is None:
        return totals
    _count_skill_misses(totals, held_seq, as_of, changes)
    _count_folder_misses(totals, as_of, packages, folder_changes)
    return totals


def find_missed_updates(
    held_seq, as_of_date, lineage_changes, package_versions, folder_changes
) -> dict:
    """Return counts of what the copy never took.

    ``lineage_changes`` are skill-text steps. ``package_versions`` should be
    the packages released after the held package: ``missed_script_updates``
    is how many of them have ``changed_scripts``. ``folder_changes`` adds
    script-file counts (substantive edits, commands, network, scripts added,
    risk-flag changes). Dates after ``as_of_date`` are left out.

    Example:
    >>> changes = pd.DataFrame([{
    ...     "to_seq": 2, "release_date": "2026-03-01", "state": "substantive",
    ...     "commands_added": ["curl"], "network_refs_added": [],
    ...     "scripts_mentioned_added": [],
    ... }])
    >>> found = find_missed_updates(1, "2026-10-23", changes, pd.DataFrame(), pd.DataFrame())
    >>> (found["missed_substantive"], found["missed_commands"], found["missed_risky"])
    (1, 1, 1)
    """
    def _records(value) -> list[dict]:
        if isinstance(value, pd.DataFrame):
            if value.empty:
                return []
            return value.to_dict("records")
        if not value:
            return []
        return list(value)

    return _missed_counts(
        _as_int(held_seq),
        _as_date(as_of_date),
        _records(lineage_changes),
        _records(package_versions),
        _records(folder_changes),
    )


# Algorithm A47, see CODE_FLOW.md
# The card called this A44. A44 is the held-version rule above.
def classify_copy_state(
    versions_behind: int,
    days_stale: int | None,
    registry_removed: bool,
    stale_after_days: int,
) -> str:
    """Return ``orphaned``, ``up_to_date``, ``stale``, or ``behind``.

    A removed registry lineage is ``orphaned``. ``versions_behind`` is the
    text misses plus the package misses, so 0 means both are current
    (``up_to_date``). A miss older than ``stale_after_days`` is ``stale``.
    Any other miss is ``behind``.

    Example:
    >>> classify_copy_state(2, 236, False, 30)
    'stale'
    >>> classify_copy_state(2, 236, False, 400)
    'behind'
    >>> classify_copy_state(0, None, True, 30)
    'orphaned'
    >>> classify_copy_state(0, None, False, 30)
    'up_to_date'
    """
    if registry_removed:
        return "orphaned"
    if int(versions_behind) <= 0:
        return "up_to_date"
    if days_stale is not None and int(days_stale) > int(stale_after_days):
        return "stale"
    return "behind"


def _commit_set(inherited_commits) -> set[str]:
    """Lowercase commit hashes from a set, a series, or a frame with ``commit``."""
    if inherited_commits is None:
        return set()
    if isinstance(inherited_commits, pd.DataFrame):
        if "commit" not in inherited_commits.columns:
            return set()
        values = inherited_commits["commit"].tolist()
    elif isinstance(inherited_commits, pd.Series):
        values = inherited_commits.tolist()
    else:
        values = list(inherited_commits)
    found: set[str] = set()
    for value in values:
        if _missing(value):
            continue
        text = str(value).strip().lower()
        if text and text != "nan":
            found.add(text)
    return found


def _version_index(
    lineage_versions: pd.DataFrame,
) -> dict[tuple[str, str], list[tuple[int, date | None]]]:
    """``(lineage, blob)`` to the versions that stored that blob."""
    found: dict[tuple[str, str], list[tuple[int, date | None]]] = {}
    if not isinstance(lineage_versions, pd.DataFrame) or lineage_versions.empty:
        return found
    if "blob" not in lineage_versions.columns:
        return found
    for row in lineage_versions.to_dict("records"):
        blob = _sha(row.get("blob"))
        seq = _as_int(row.get("version_seq"))
        if not blob or seq is None:
            continue
        lineage = "" if _missing(row.get("lineage_id")) else str(row.get("lineage_id"))
        found.setdefault((lineage, blob), []).append((seq, _as_date(row.get("release_date"))))
    return found


def _version_at(
    index: dict[tuple[str, str], list[tuple[int, date | None]]],
    lineage: str,
    blob: str,
    commit_day: date | None,
) -> tuple[int, date | None] | None:
    """The registry version of this blob that existed on the commit day.

    When several versions share the blob, the newest release on or before
    the commit wins. A blob that was only released later uses the earliest.
    """
    options = index.get((lineage, blob), [])
    if not options:
        return None
    eligible = [
        item
        for item in options
        if item[1] is not None and commit_day is not None and item[1] <= commit_day
    ]
    if eligible:
        return max(eligible, key=lambda item: (item[1], item[0]))
    dated = [item for item in options if item[1] is not None]
    if dated:
        return min(dated, key=lambda item: (item[1], item[0]))
    return max(options, key=lambda item: item[0])


def _events_for_file(
    rows: list[dict],
    index: dict[tuple[str, str], list[tuple[int, date | None]]],
    inherited: set[str],
) -> list[dict]:
    """Walk one copy's commits. Inherited commits set the version and emit nothing."""
    events: list[dict] = []
    last_seq: int | None = None
    for row in rows:
        lineage = "" if _missing(row.get("lineage_id")) else str(row.get("lineage_id"))
        commit_day = _as_date(row.get("commit_date"))
        found = _version_at(index, lineage, _sha(row.get("blob")), commit_day)
        if found is None:
            continue
        seq, release = found
        commit = "" if _missing(row.get("commit")) else str(row.get("commit")).strip().lower()
        if commit in inherited:
            last_seq = seq
            continue
        if last_seq is None:
            kind = "copy"
            from_seq = None
            skipped = 0
        elif seq > last_seq:
            kind = "update"
            from_seq = last_seq
            skipped = seq - last_seq - 1
        elif seq < last_seq:
            kind = "downgrade"
            from_seq = last_seq
            skipped = last_seq - seq - 1
        else:
            continue
        last_seq = seq
        lag = None if commit_day is None or release is None else (commit_day - release).days
        events.append(
            {
                "repo": row.get("repo"),
                "path": row.get("path"),
                "lineage_id": lineage,
                "commit": row.get("commit"),
                "commit_date": commit_day,
                "event_kind": kind,
                "from_seq": from_seq,
                "to_seq": seq,
                "versions_skipped": skipped,
                "release_date": release,
                "lag_days": lag,
            }
        )
    return events


def _event_frame(records: list[dict]) -> pd.DataFrame:
    """Sync-event rows in the stored column order."""
    if not records:
        frame = pd.DataFrame(columns=EVENT_COLUMNS)
        return frame
    frame = pd.DataFrame(records)
    for name in EVENT_COLUMNS:
        if name not in frame.columns:
            frame[name] = pd.NA
    return frame.loc[:, EVENT_COLUMNS]


# Algorithm A48, see CODE_FLOW.md
# The card called this A45. A45 is the versions-behind count above.
def find_sync_events(histories, lineage_versions, inherited_commits) -> pd.DataFrame:
    """Return copy, update, and downgrade events in commit order.

    A commit whose blob is a registry version is an event. The first such
    version is ``copy``. A later higher version is ``update`` and
    ``versions_skipped`` is how many numbers sit between the two. A lower
    version is ``downgrade``. ``lag_days`` is the commit day minus the
    release day. An inherited commit sets the starting version and is not
    an event. A blob that is not a registry version is a local edit and
    does not move the version.

    Example:
    >>> versions = _timeline_versions()
    >>> history = pd.DataFrame([
    ...     {"repo": "acme/app", "path": "skills/a/SKILL.md", "lineage_id": "reg::skill",
    ...      "commit": "c1", "commit_date": "2026-02-01", "blob": "v1"},
    ...     {"repo": "acme/app", "path": "skills/a/SKILL.md", "lineage_id": "reg::skill",
    ...      "commit": "c4", "commit_date": "2026-06-30", "blob": "v3"},
    ... ])
    >>> events = find_sync_events(history, versions, set())
    >>> list(events["event_kind"])
    ['copy', 'update']
    >>> int(events.iloc[1]["versions_skipped"])
    1
    """
    frame = histories if isinstance(histories, pd.DataFrame) else pd.DataFrame(histories or [])
    if frame.empty:
        return _event_frame([])
    inherited = _commit_set(inherited_commits)
    index = _version_index(_frame(lineage_versions))
    records: list[dict] = []
    ordered = frame.copy()
    if "commit_date" in ordered.columns:
        ordered["_day"] = ordered["commit_date"].map(_as_date)
        ordered = ordered.sort_values(
            ["repo", "path", "_day", "commit"], kind="mergesort", na_position="first"
        )
    grouped = ordered.groupby(["repo", "path"], sort=False)
    for (_repo, _path), group in grouped:
        records.extend(_events_for_file(group.to_dict("records"), index, inherited))
    return _event_frame(records)


def _output_paths(dev: bool) -> list[Path]:
    """The two stage 10 parquet files."""
    folder = data_dir(dev)
    return [folder / "copy_staleness.parquet", folder / "sync_events.parquet"]


def _input_paths(dev: bool) -> dict[str, Path | str]:
    """Stage 10 inputs. Language, signatures, wild history and artifacts are shared."""
    folder = data_dir(dev)
    full = data_dir(False)
    return {
        "copy_files": folder / "copy_files.parquet",
        "families": folder / "families.parquet",
        "skill_language": full / "skill_language.parquet",
        "lineages": folder / "lineages.parquet",
        "lineage_versions": folder / "lineage_versions.parquet",
        "lineage_changes": folder / "lineage_changes.parquet",
        "package_versions": folder / "lineage_package_versions.parquet",
        "folder_manifests": folder / "lineage_folder_manifests.parquet",
        "copied_changes": folder / "copied_skill_history" / "copied_skill_history_changes.parquet",
        "copied_texts": folder / "copied_skill_history" / "copied_skill_history_texts.parquet",
        "registry_changes": folder / "registry_history" / "registry_history_changes.parquet",
        "registry_texts": folder / "registry_history" / "registry_history_texts.parquet",
        "wild_versions": settings.WILD_HISTORY_DIR / "versions.parquet",
        "wild_blobs": settings.WILD_HISTORY_DIR / "blobs.parquet",
        "artifacts": settings.ARTIFACTS_GLOB,
        "siblings": settings.SIBLINGS_GLOB,
        "registry_signatures": folder / "registry_signatures.npz",
        "dataset_signatures": full / "dataset_signatures",
        "boilerplate": full / "boilerplate_lines.parquet",
    }


def _input_builders(dev: bool) -> dict[str, str]:
    """The command that creates each missing input."""
    flag = " --dev" if dev else ""
    families = f"python -m skill_genealogy.assign_families{flag}"
    lineages = f"python -m skill_genealogy.build_registry_lineages{flag}"
    registry = f"python -m skill_genealogy.download_registry_history{flag}"
    copied = f"python -m skill_genealogy.download_copied_skill_history{flag}"
    signatures = f"python -m skill_genealogy.find_copies{flag} --only signatures"
    return {
        "copy_files": families,
        "families": families,
        "skill_language": "python -m skill_genealogy.select_registries",
        "lineages": lineages,
        "lineage_versions": lineages,
        "lineage_changes": lineages,
        "package_versions": lineages,
        "folder_manifests": lineages,
        "copied_changes": copied,
        "copied_texts": copied,
        "registry_changes": registry,
        "registry_texts": registry,
        "wild_versions": "src/data/wild_history is read only and is not rebuilt here",
        "wild_blobs": "src/data/wild_history is read only and is not rebuilt here",
        "artifacts": "python src/data/download_data.py",
        "siblings": "python src/data/download_data.py",
        "registry_signatures": signatures,
        "dataset_signatures": "python -m skill_genealogy.find_copies --only signatures",
        "boilerplate": "python -m skill_genealogy.find_copies --only signatures",
    }


def read_inputs(dev: bool) -> dict[str, object]:
    """Read the copy list, histories, and lineage tables for this mode.

    Inputs: ``dev`` — True reads the 30-registry folder.
    Outputs: copies, histories, file dates, the snapshot day, lineage
    tables, and the set of registry commit hashes. Skill text is not loaded.
    """
    paths = _input_paths(dev)
    check_inputs_exist(paths, _input_builders(dev))
    temp_dir = data_dir(dev) / "_measure_staleness_tmp"
    _remove_tree(temp_dir)
    connection = connect_duckdb()
    try:
        _prepare_duckdb(connection, temp_dir)
        copies = _fetch(connection, COPIES_SQL.format(
            copies=_quote_path(str(paths["copy_files"])),
            language=_quote_path(str(paths["skill_language"])),
            families=_quote_path(str(paths["families"])),
        ))
        copies = copies.drop_duplicates(["repo", "path"], keep="first")
        connection.register("copy_keys", copies[["repo", "path"]])
        wild = _fetch(connection, WILD_HISTORY_SQL.format(
            path=_quote_path(str(paths["wild_versions"])), zero=_ZERO_BLOB,
        ))
        copied = _fetch(connection, COPIED_HISTORY_SQL.format(
            path=_quote_path(str(paths["copied_changes"])), zero=_ZERO_BLOB,
        ))
        dates = _fetch(connection, FILE_DATES_SQL.format(path=_quote_path(str(paths["artifacts"]))))
        snapshot_frame = _fetch(connection, REGISTRY_SNAPSHOT_SQL.format(
            path=_quote_path(str(paths["registry_changes"])),
        ))
        commits = _fetch(connection, REGISTRY_COMMITS_SQL.format(
            path=_quote_path(str(paths["registry_changes"])),
        ))
    finally:
        connection.close()
        _remove_tree(temp_dir)
    histories = pd.concat([wild, copied], ignore_index=True)
    if not histories.empty:
        histories = histories.drop_duplicates(["repo", "path", "commit", "blob"], keep="first")
        histories = histories.merge(
            copies[["repo", "path", "lineage_id"]], on=["repo", "path"], how="inner"
        )
    snapshot = None
    if not snapshot_frame.empty:
        snapshot = _as_date(snapshot_frame.iloc[0]["snapshot_at"])
    return {
        "copies": copies,
        "histories": histories,
        "dates": dates,
        "snapshot": snapshot,
        "versions": read_parquet(paths["lineage_versions"]),
        "changes": read_parquet(paths["lineage_changes"]),
        "packages": read_parquet(paths["package_versions"]),
        "manifests": read_parquet(paths["folder_manifests"]),
        "lineages": read_parquet(paths["lineages"]),
        "inherited": _commit_set(commits["commit"] if "commit" in commits.columns else []),
    }


def _newest_history(histories: pd.DataFrame) -> pd.DataFrame:
    """One row per copy: the latest commit's blob and date."""
    columns = ["repo", "path", "blob", "commit_date"]
    if histories.empty:
        return pd.DataFrame(columns=columns)
    ordered = histories.copy()
    ordered["_day"] = ordered["commit_date"].map(_as_date)
    ordered = ordered.sort_values(
        ["repo", "path", "_day", "commit"], kind="mergesort", na_position="first"
    )
    latest = ordered.drop_duplicates(["repo", "path"], keep="last")
    return latest.loc[:, columns]


def current_copies(loaded: dict[str, object]) -> pd.DataFrame:
    """Add the current blob, the copy date, and the history flag.

    The current blob is the newest history commit, otherwise the dataset
    ``file_sha``. The copy date is that commit's day, otherwise
    ``last_commit_at``. ``stopped_changing_at`` is set only when history exists.
    """
    copies = loaded["copies"]
    if not isinstance(copies, pd.DataFrame) or copies.empty:
        return pd.DataFrame(columns=["repo", "path", "lineage_id", "file_sha"])
    frame = copies.copy()
    dates = loaded["dates"]
    if isinstance(dates, pd.DataFrame) and not dates.empty:
        frame = frame.merge(dates, on=["repo", "path"], how="left")
    else:
        frame["discovered_at"] = pd.NA
        frame["last_commit_at"] = pd.NA
    latest = _newest_history(_frame(loaded["histories"]))
    latest = latest.rename(columns={"blob": "history_blob", "commit_date": "history_date"})
    frame = frame.merge(latest, on=["repo", "path"], how="left")
    has_history = frame["history_blob"].map(lambda value: bool(_sha(value)))
    frame["has_history"] = has_history
    frame["blob"] = [
        _sha(history) if flag else _sha(dataset)
        for flag, history, dataset in zip(
            has_history, frame["history_blob"], frame["file_sha"], strict=True
        )
    ]
    frame["copy_date"] = [
        history if flag else last
        for flag, history, last in zip(
            has_history, frame["history_date"], frame["last_commit_at"], strict=True
        )
    ]
    frame["stopped_changing_at"] = [
        _as_date(value) if flag else None
        for flag, value in zip(has_history, frame["history_date"], strict=True)
    ]
    return frame


def _release_of(versions: pd.DataFrame, seq: int | None) -> date | None:
    """Release day of one version number in an already filtered lineage frame."""
    if seq is None or versions.empty or "version_seq" not in versions.columns:
        return None
    matched = versions.loc[versions["version_seq"].map(_as_int) == seq]
    if matched.empty or "release_date" not in matched.columns:
        return None
    return _as_date(matched.iloc[0]["release_date"])


def _unmatched_blobs(copies: pd.DataFrame, versions: pd.DataFrame) -> set[str]:
    """Current blobs that are not a version blob of the copy's own lineage."""
    if copies.empty:
        return set()
    lineages = versions["lineage_id"] if "lineage_id" in versions.columns else []
    blobs = versions["blob"] if "blob" in versions.columns else []
    version_keys = {
        (str(lineage), _sha(blob))
        for lineage, blob in zip(lineages, blobs, strict=False)
    }
    needed: set[str] = set()
    for lineage, blob in zip(copies["lineage_id"], copies["blob"], strict=False):
        text = _sha(blob)
        if text and (str(lineage), text) not in version_keys:
            needed.add(text)
    return needed


def _sha_sql_list(shas: list[str]) -> str:
    """A comma-separated list of quoted blob ids."""
    return ",".join("'" + sha.replace("'", "") + "'" for sha in shas)


def _store_texts(frame: pd.DataFrame, found: dict[str, str]) -> None:
    """Copy non-empty text cells into ``found``, keyed by lowercase blob id."""
    for blob, text in zip(frame.get("blob", []), frame.get("text", []), strict=False):
        if isinstance(text, str) and text:
            found[_sha(blob)] = text


def _texts_for_blobs(dev: bool, blobs: set[str]) -> dict[str, str]:
    """Load skill text for specific blob ids. Other texts stay on disk.

    History stores are queried first (Q10.8). The dataset ``content`` column
    is read only for blob ids those stores did not have (Q10.9).
    """
    if not blobs:
        return {}
    paths = _input_paths(dev)
    found: dict[str, str] = {}
    pending = sorted(blobs)
    temp_dir = data_dir(dev) / "_measure_staleness_tmp"
    _remove_tree(temp_dir)
    connection = connect_duckdb()
    try:
        _prepare_duckdb(connection, temp_dir)
        for start in range(0, len(pending), _TEXT_SHA_BATCH):
            batch = pending[start : start + _TEXT_SHA_BATCH]
            query = BLOB_TEXTS_SQL.format(
                copied=_quote_path(str(paths["copied_texts"])),
                wild=_quote_path(str(paths["wild_blobs"])),
                registry=_quote_path(str(paths["registry_texts"])),
                sha_list=_sha_sql_list(batch),
            )
            _store_texts(_fetch(connection, query), found)
        missing = [blob for blob in pending if blob not in found]
        for start in range(0, len(missing), _TEXT_SHA_BATCH):
            batch = missing[start : start + _TEXT_SHA_BATCH]
            query = ARTIFACT_TEXTS_SQL.format(
                path=_quote_path(str(paths["artifacts"])),
                sha_list=_sha_sql_list(batch),
            )
            _store_texts(_fetch(connection, query), found)
    finally:
        connection.close()
        _remove_tree(temp_dir)
    return found


def _stack_signatures(parts: list[tuple[list[str], np.ndarray, np.ndarray]]):
    """One signature bundle. The first copy of an id wins."""
    ids: list[str] = []
    matrices: list[np.ndarray] = []
    sizes: list[np.ndarray] = []
    seen: set[str] = set()
    for part_ids, matrix, part_sizes in parts:
        if not part_ids:
            continue
        keep = [offset for offset, name in enumerate(part_ids) if name not in seen]
        if not keep:
            continue
        for offset in keep:
            seen.add(part_ids[offset])
            ids.append(part_ids[offset])
        matrices.append(matrix[np.asarray(keep, dtype=np.int64)])
        sizes.append(part_sizes[np.asarray(keep, dtype=np.int64)])
    if not ids:
        return _empty_sigs()
    return ids, np.vstack(matrices), np.concatenate(sizes)


def _signatures_for_unmatched(dev: bool, blobs: set[str], file_sha_of: dict[str, str]):
    """Registry signatures plus a signature for each current blob that is not exact.

    Dataset signatures are used when the blob is the dataset file sha.
    Any other blob is hashed from its stored text.
    """
    registry = load_signatures(_input_paths(dev)["registry_signatures"])
    if not blobs:
        return registry
    dataset_ids = {blob for blob in blobs if file_sha_of.get(blob, blob) == blob}
    dataset = _signatures_for_ids(_input_paths(dev)["dataset_signatures"], dataset_ids)
    have = set(dataset[0])
    missing = blobs - have
    texts = _texts_for_blobs(dev, missing)
    boilerplate = _boilerplate_lines()
    hashed_ids: list[str] = []
    hashed_rows: list[np.ndarray] = []
    hashed_sizes: list[int] = []
    for blob in sorted(missing):
        values, size = signature_for_text(texts.get(blob, ""), boilerplate)
        hashed_ids.append(blob)
        hashed_rows.append(values)
        hashed_sizes.append(size)
    hashed = _empty_sigs()
    if hashed_ids:
        hashed = (
            hashed_ids,
            np.vstack(hashed_rows),
            np.asarray(hashed_sizes, dtype=np.int32),
        )
    return _stack_signatures([registry, dataset, hashed])


def assign_held_versions(copies: pd.DataFrame, versions: pd.DataFrame, sigs) -> pd.DataFrame:
    """Add ``held_seq``, ``held_basis`` and ``held_release_date`` to each copy.

    Algorithm A44. Each copy is scored only against its own lineage.
    """
    frame = copies.copy()
    if frame.empty:
        frame["held_seq"] = pd.Series(dtype="Int64")
        frame["held_basis"] = pd.Series(dtype="object")
        frame["held_release_date"] = pd.Series(dtype="object")
        return frame
    grouped = {
        str(key): group for key, group in versions.groupby("lineage_id", sort=False)
    }
    empty = versions.iloc[0:0]
    sequences: list[int | None] = []
    reasons: list[str] = []
    releases: list[date | None] = []
    for record in frame.to_dict("records"):
        lineage = grouped.get(str(record.get("lineage_id") or ""), empty)
        seq, basis = choose_held_version(record, lineage, sigs)
        sequences.append(seq)
        reasons.append(basis)
        releases.append(_release_of(lineage, seq))
    frame["held_seq"] = pd.array(sequences, dtype="Int64")
    frame["held_basis"] = reasons
    frame["held_release_date"] = releases
    return frame


def _scripts_by_package(manifests: pd.DataFrame) -> dict[tuple[str, int], dict[str, str]]:
    """``(lineage, package seq)`` to script path → sha. Other files are left out."""
    found: dict[tuple[str, int], dict[str, str]] = {}
    if manifests.empty:
        return found
    path_name = _column(manifests, "path", "file_path")
    if path_name is None or "sha" not in manifests.columns or "lineage_id" not in manifests.columns:
        return found
    modes = manifests["mode"] if "mode" in manifests.columns else [None] * len(manifests)
    if "package_version_seq" in manifests.columns:
        sequences = manifests["package_version_seq"]
    else:
        sequences = []
    for lineage, seq, path, sha, mode in zip(
        manifests["lineage_id"],
        sequences,
        manifests[path_name],
        manifests["sha"],
        modes,
        strict=False,
    ):
        number = _as_int(seq)
        if number is None or _missing(path):
            continue
        if file_category(str(path), None if _missing(mode) else str(mode)) != "script":
            continue
        blob = _sha(sha)
        if not blob:
            continue
        found.setdefault((str(lineage), number), {})[str(path)] = blob
    return found


def _flag_set(text: str | None) -> set[str] | None:
    """Risk flags for a stored script. None means the text was not stored."""
    if text is None:
        return None
    return set(flag_script_risks(text))


def _script_step_counts(
    previous: dict[str, str], current: dict[str, str], texts: dict[str, str]
) -> dict[str, int]:
    """What changed in the script files between two package snapshots.

    ``substantive`` counts paths whose sha was added, removed, or edited.
    ``scripts_added`` counts new paths. A flag change needs both texts,
    except a new file, whose flags all count as added. A new path is a
    risky miss even when the new text has no flag.
    """
    substantive = 0
    commands = 0
    network = 0
    scripts_added = 0
    flag_changes = 0
    risky = 0
    for path in set(previous) | set(current):
        old_sha = previous.get(path)
        new_sha = current.get(path)
        if old_sha == new_sha:
            continue
        substantive += 1
        added = old_sha is None
        scripts_added += int(added)
        gained_commands, gained_network, changed = _gained_flags(old_sha, new_sha, texts, added)
        commands += int(gained_commands)
        network += int(gained_network)
        flag_changes += int(changed)
        risky += int(added or changed or gained_commands or gained_network)
    return {
        "substantive": substantive,
        "commands_added": commands,
        "network_refs_added": network,
        "scripts_added": scripts_added,
        "risk_flag_changed": flag_changes,
        "risky": risky,
    }


def _gained_flags(
    old_sha: str | None, new_sha: str | None, texts: dict[str, str], added: bool
) -> tuple[bool, bool, bool]:
    """Whether this file gained command or network flags, and whether the set changed.

    A missing text on either side of an edit is unknown, so the flags did
    not change. A new file counts every flag in its text as gained.
    """
    new_flags = None if new_sha is None else _flag_set(texts.get(new_sha))
    if added and new_flags is not None:
        return ("runs_commands" in new_flags, "network" in new_flags, bool(new_flags))
    old_flags = None if old_sha is None else _flag_set(texts.get(old_sha))
    if old_flags is None or new_flags is None:
        return False, False, False
    return (
        "runs_commands" in new_flags - old_flags,
        "network" in new_flags - old_flags,
        old_flags != new_flags,
    )


def _script_shas_to_load(packages: pd.DataFrame, manifests: pd.DataFrame) -> set[str]:
    """Script shas that differ between a package version and the one before it."""
    needed: set[str] = set()
    scripts = _scripts_by_package(manifests)
    if packages.empty or not scripts:
        return needed
    for lineage, group in packages.groupby("lineage_id", sort=False):
        ordered = sorted(
            (_as_int(seq) for seq in group["package_version_seq"]),
            key=lambda seq: seq or 0,
        )
        previous: dict[str, str] = {}
        for seq in ordered:
            if seq is None:
                continue
            current = scripts.get((str(lineage), seq), {})
            if previous:
                for path in set(previous) | set(current):
                    if previous.get(path) != current.get(path):
                        if previous.get(path):
                            needed.add(previous[path])
                        if current.get(path):
                            needed.add(current[path])
            previous = current
    return needed


def _load_registry_texts(dev: bool, shas: set[str]) -> dict[str, str]:
    """Registry script text for the shas that changed. Query Q10.7."""
    if not shas:
        return {}
    path = _input_paths(dev)["registry_texts"]
    found: dict[str, str] = {}
    pending = sorted(shas)
    temp_dir = data_dir(dev) / "_measure_staleness_tmp"
    _remove_tree(temp_dir)
    connection = connect_duckdb()
    try:
        _prepare_duckdb(connection, temp_dir)
        for start in range(0, len(pending), _TEXT_SHA_BATCH):
            batch = pending[start : start + _TEXT_SHA_BATCH]
            query = SCRIPT_TEXTS_SQL.format(
                path=_quote_path(str(path)), sha_list=_sha_sql_list(batch),
            )
            frame = _fetch(connection, query)
            for blob, text in zip(frame.get("blob", []), frame.get("text", []), strict=False):
                if isinstance(text, str):
                    found[_sha(blob)] = text
    finally:
        connection.close()
        _remove_tree(temp_dir)
    return found


def build_script_deltas(
    packages: pd.DataFrame, manifests: pd.DataFrame, texts: dict[str, str]
) -> pd.DataFrame:
    """One row per package version whose scripts differ from the previous package.

    The first package version has no previous snapshot, so it has no row.
    Counts are the script-file totals ``find_missed_updates`` adds.
    """
    columns = [
        "lineage_id",
        "package_version_seq",
        "commit_date",
        "substantive",
        "commands_added",
        "network_refs_added",
        "scripts_added",
        "risk_flag_changed",
        "risky",
    ]
    if packages.empty:
        return pd.DataFrame(columns=columns)
    records: list[dict] = []
    scripts = _scripts_by_package(manifests)
    date_name = _column(packages, "commit_date", "release_date")
    for lineage, group in packages.groupby("lineage_id", sort=False):
        ordered = group.copy()
        ordered["_seq"] = ordered["package_version_seq"].map(_as_int)
        ordered = ordered.sort_values("_seq", kind="mergesort")
        previous: dict[str, str] = {}
        for row in ordered.to_dict("records"):
            seq = _as_int(row.get("package_version_seq"))
            if seq is None:
                continue
            current = scripts.get((str(lineage), seq), {})
            if previous:
                counts = _script_step_counts(previous, current, texts)
                if counts["substantive"]:
                    records.append(
                        {
                            "lineage_id": str(lineage),
                            "package_version_seq": seq,
                            "commit_date": None if date_name is None else row.get(date_name),
                            "substantive": counts["substantive"],
                            "commands_added": counts["commands_added"],
                            "network_refs_added": counts["network_refs_added"],
                            "scripts_added": counts["scripts_added"],
                            "risk_flag_changed": counts["risk_flag_changed"] > 0,
                            "risky": counts["risky"],
                        }
                    )
            previous = current
    if not records:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(records).loc[:, columns]


def _later_packages(
    packages: list[tuple[int, date | None, bool]],
    held_seq: int | None,
    held_day: date | None,
    as_of: date | None,
) -> list[tuple[int, date | None, bool]]:
    """Packages after the held one, released on or before the as-of day.

    Holding nothing means every package on or before the as-of day is later.
    """
    if as_of is None:
        return []
    later: list[tuple[int, date | None, bool]] = []
    for seq, day, changed in packages:
        if day is None or day > as_of:
            continue
        if held_seq is None:
            later.append((seq, day, changed))
            continue
        if held_day is None:
            if seq > held_seq:
                later.append((seq, day, changed))
            continue
        if (day, seq) > (held_day, held_seq):
            later.append((seq, day, changed))
    return later


def _latest_seq(pairs: list[tuple[int, date | None]], as_of: date | None) -> int | None:
    """Highest version number released on or before the as-of day."""
    if as_of is None:
        return None
    latest: int | None = None
    for seq, day in pairs:
        if day is None or day > as_of:
            continue
        if latest is None or seq > latest:
            latest = seq
    return latest


def _removed_index(lineages: pd.DataFrame) -> dict[str, tuple[bool, date | None]]:
    """Lineage id to ``(registry_removed, removed_at day)``."""
    found: dict[str, tuple[bool, date | None]] = {}
    if lineages.empty or "lineage_id" not in lineages.columns:
        return found
    for row in lineages.to_dict("records"):
        removed = bool(row.get("registry_removed")) and not _missing(row.get("registry_removed"))
        found[str(row.get("lineage_id"))] = (removed, _as_date(row.get("removed_at")))
    return found


def _copy_manifest_index(manifests: pd.DataFrame) -> dict[tuple[str, str], set[tuple[str, str]]]:
    """``(repo, path)`` to the current folder's ``(file path, sha)`` pairs."""
    found: dict[tuple[str, str], set[tuple[str, str]]] = {}
    if manifests.empty:
        return found
    for (repo, path), group in manifests.groupby(["repo", "path"], sort=False):
        found[(str(repo), str(path))] = _pairs_from_manifest(group)
    return found


def _lineage_package_index(
    packages: pd.DataFrame,
) -> dict[str, list[tuple[int, date | None, bool]]]:
    """Lineage id to package records sorted by version number."""
    found: dict[str, list[tuple[int, date | None, bool]]] = {}
    if packages.empty:
        return found
    for lineage, group in packages.groupby("lineage_id", sort=False):
        records = _package_records(group)
        records.sort(key=lambda item: item[0])
        found[str(lineage)] = records
    return found


def _lineage_file_index(manifests: pd.DataFrame) -> dict[tuple[str, int], set[tuple[str, str]]]:
    """``(lineage, package seq)`` to file pairs. Built once for every package."""
    found: dict[tuple[str, int], set[tuple[str, str]]] = {}
    if manifests.empty or "lineage_id" not in manifests.columns:
        return found
    grouped = manifests.groupby(["lineage_id", "package_version_seq"], sort=False)
    for (lineage, seq), group in grouped:
        number = _as_int(seq)
        if number is None:
            continue
        found[(str(lineage), number)] = _pairs_from_manifest(group)
    return found


def _version_pairs(versions: pd.DataFrame) -> dict[str, list[tuple[int, date | None]]]:
    """Lineage id to ``(version_seq, release day)``."""
    found: dict[str, list[tuple[int, date | None]]] = {}
    if versions.empty:
        return found
    for row in versions.to_dict("records"):
        seq = _as_int(row.get("version_seq"))
        if seq is None:
            continue
        day = _as_date(row.get("release_date"))
        found.setdefault(str(row.get("lineage_id")), []).append((seq, day))
    return found


def _change_lists(changes: pd.DataFrame) -> dict[str, list[dict]]:
    """Lineage id to its skill-text change rows."""
    found: dict[str, list[dict]] = {}
    if changes.empty or "lineage_id" not in changes.columns:
        return found
    for lineage, group in changes.groupby("lineage_id", sort=False):
        found[str(lineage)] = group.to_dict("records")
    return found


def _delta_lists(deltas: pd.DataFrame) -> dict[str, list[dict]]:
    """Lineage id to script-change rows."""
    found: dict[str, list[dict]] = {}
    if deltas.empty or "lineage_id" not in deltas.columns:
        return found
    for lineage, group in deltas.groupby("lineage_id", sort=False):
        found[str(lineage)] = group.to_dict("records")
    return found


def _days_between(later: date | None, earlier: date | None) -> int | None:
    """Days from ``earlier`` to ``later``. None when either day is missing."""
    if later is None or earlier is None:
        return None
    return (later - earlier).days


def measure_copy_rows(
    copies: pd.DataFrame,
    loaded: dict[str, object],
    deltas: pd.DataFrame,
    copy_manifests: pd.DataFrame,
    stale_after_days: int,
) -> pd.DataFrame:
    """Fill behind counts, missed content, and the copy state for every copy.

    Algorithms A45, A46 and A47. ``versions_behind`` passed to the state
    rule is text misses plus package misses.
    """
    if copies.empty:
        return _empty_staleness()
    versions = _frame(loaded["versions"])
    version_pairs = _version_pairs(versions)
    changes = _change_lists(_frame(loaded["changes"]))
    packages = _lineage_package_index(_frame(loaded["packages"]))
    files = _lineage_file_index(_frame(loaded["manifests"]))
    delta_rows = _delta_lists(deltas)
    removed = _removed_index(_frame(loaded["lineages"]))
    folders = _copy_manifest_index(copy_manifests)
    snapshot = loaded.get("snapshot")
    records: list[dict] = []
    for record in copies.to_dict("records"):
        records.append(
            _one_copy_row(
                record, snapshot, version_pairs, changes, packages, files,
                delta_rows, removed, folders, stale_after_days,
            )
        )
    return _staleness_frame(records)


def _package_lag(
    record: dict,
    lineage: str,
    as_of: date | None,
    packages: dict[str, list[tuple[int, date | None, bool]]],
    files: dict[tuple[str, int], set[tuple[str, str]]],
    delta_rows: dict[str, list[dict]],
    folders: dict[tuple[str, str], set[tuple[str, str]]],
) -> tuple[int | None, list[dict], list[dict]]:
    """Held package, the later package rows, and their script-folder deltas.

    The held package is the one on or before the copy date that shares the
    most files. Later rows are packages after that one and on or before the
    as-of day. Folder rows are the script deltas for those package numbers.
    """
    lineage_packages = packages.get(lineage, [])
    file_sets = {seq: files.get((lineage, seq), set()) for seq, _day, _flag in lineage_packages}
    package_seq = _held_package_seq(
        folders.get((str(record.get("repo")), str(record.get("path"))), set()),
        lineage_packages,
        file_sets,
        _as_date(record.get("copy_date")),
    )
    package_day = next((day for seq, day, _flag in lineage_packages if seq == package_seq), None)
    later = _later_packages(lineage_packages, package_seq, package_day, as_of)
    later_ids = {seq for seq, _day, _flag in later}
    later_rows = [
        {"package_version_seq": seq, "commit_date": day, "changed_scripts": changed}
        for seq, day, changed in later
    ]
    folder_rows = [
        row
        for row in delta_rows.get(lineage, [])
        if _as_int(row.get("package_version_seq")) in later_ids
    ]
    return package_seq, later_rows, folder_rows


def _staleness_record(
    record: dict,
    lineage: str,
    as_of: date,
    as_of_basis: str,
    pairs: list[tuple[int, date | None]],
    held_seq: int | None,
    held_day: date | None,
    behind: int,
    first_missed: date | None,
    days_stale: int | None,
    package_seq: int | None,
    later_count: int,
    missed: dict[str, int],
    is_removed: bool,
    removed_day: date | None,
    stale_after_days: int,
) -> dict:
    """The stored columns for one copy, including its state."""
    combined = behind + later_count
    state = classify_copy_state(combined, days_stale, is_removed, stale_after_days)
    return {
        "repo": record.get("repo"),
        "path": record.get("path"),
        "lineage_id": lineage,
        "held_seq": held_seq,
        "held_basis": record.get("held_basis"),
        "held_release_date": held_day,
        "as_of_date": as_of,
        "as_of_basis": as_of_basis,
        "latest_seq": _latest_seq(pairs, as_of),
        "skill_md_versions_behind": behind,
        "first_missed_release": first_missed,
        "days_stale": days_stale,
        "package_held_seq": package_seq,
        "package_versions_behind": later_count,
        "missed_script_updates": missed["missed_script_updates"],
        "missed_substantive": missed["missed_substantive"],
        "missed_commands": missed["missed_commands"],
        "missed_network": missed["missed_network"],
        "missed_scripts": missed["missed_scripts"],
        "missed_risky": missed["missed_risky"],
        "stopped_changing_at": _as_date(record.get("stopped_changing_at")),
        "copied_before_release": record.get("held_basis") == "copied_before_release",
        "copy_state": state,
        "days_since_removed": _days_between(as_of, removed_day) if is_removed else None,
        "stale_after_days": int(stale_after_days),
        "current_sha": _sha(record.get("blob")) or None,
    }


def _one_copy_row(
    record: dict,
    snapshot,
    version_pairs: dict[str, list[tuple[int, date | None]]],
    changes: dict[str, list[dict]],
    packages: dict[str, list[tuple[int, date | None, bool]]],
    files: dict[tuple[str, int], set[tuple[str, str]]],
    delta_rows: dict[str, list[dict]],
    removed: dict[str, tuple[bool, date | None]],
    folders: dict[tuple[str, str], set[tuple[str, str]]],
    stale_after_days: int,
) -> dict:
    """One staleness row. Dates are calendar days."""
    lineage = str(record.get("lineage_id") or "")
    as_of, as_of_basis = choose_as_of_date(record, snapshot)
    pairs = version_pairs.get(lineage, [])
    days = [day for _seq, day in pairs]
    held_seq = _as_int(record.get("held_seq"))
    held_day = _as_date(record.get("held_release_date"))
    held_release = held_day if held_seq is not None else None
    behind, first_missed = _behind_from_dates(held_release, as_of, days)
    days_stale = _days_between(as_of, first_missed)
    package_seq, later_rows, folder_rows = _package_lag(
        record, lineage, as_of, packages, files, delta_rows, folders
    )
    missed = _missed_counts(held_seq, as_of, changes.get(lineage, []), later_rows, folder_rows)
    is_removed, removed_day = removed.get(lineage, (False, None))
    return _staleness_record(
        record,
        lineage,
        as_of,
        as_of_basis,
        pairs,
        held_seq,
        held_day,
        behind,
        first_missed,
        days_stale,
        package_seq,
        len(later_rows),
        missed,
        is_removed,
        removed_day,
        stale_after_days,
    )


def _empty_staleness() -> pd.DataFrame:
    """A staleness frame with the stored columns and no rows."""
    return pd.DataFrame(columns=STALENESS_COLUMNS)


def _staleness_frame(records: list[dict]) -> pd.DataFrame:
    """Staleness rows in the stored column order. Counts are integers."""
    if not records:
        return _empty_staleness()
    frame = pd.DataFrame(records)
    for name in STALENESS_COLUMNS:
        if name not in frame.columns:
            frame[name] = pd.NA
    return frame.loc[:, STALENESS_COLUMNS]


def _current_version_frame(copies: pd.DataFrame) -> pd.DataFrame:
    """One version row per copy so the folder replay keeps the latest snapshot."""
    columns = ["repo", "path", "version_seq", "commit_date", "blob", "lineage_id"]
    if copies.empty:
        return pd.DataFrame(columns=columns)
    frame = copies.loc[:, ["repo", "path", "lineage_id"]].copy()
    frame["version_seq"] = 1
    frame["commit_date"] = copies["copy_date"] if "copy_date" in copies.columns else pd.NA
    frame["blob"] = copies["blob"] if "blob" in copies.columns else ""
    return frame.loc[:, columns]


def read_current_manifests(dev: bool, copies: pd.DataFrame) -> pd.DataFrame:
    """Current folder files for each copy.

    History folders are replayed with the stage 9c walker. A copy with no
    folder history uses the dataset sibling snapshot. The root ``SKILL.md``
    is not a package file, same as a registry manifest.
    """
    versions = _current_version_frame(copies)
    if versions.empty:
        return pd.DataFrame(columns=["repo", "path", "file_path", "sha"])
    temp_dir = data_dir(dev) / "_measure_staleness_tmp"
    _remove_tree(temp_dir)
    connection = connect_duckdb()
    try:
        _prepare_duckdb(connection, temp_dir)
        changes = _read_folder_history(connection, versions, dev)
        siblings = _read_sibling_files(connection, versions, changes, dev)
        return assemble_copy_folder_manifests(versions, changes, siblings)
    finally:
        connection.close()
        _remove_tree(temp_dir)


def _share(count: int, total: int) -> float:
    """Count divided by total, rounded to 4 decimals. An empty total is 0."""
    if total <= 0:
        return 0.0
    return round(count / total, 4)


def _median_days(values: pd.Series) -> float | None:
    """Median of the non-missing day counts, or None when none are set."""
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return None
    return float(numeric.median())


def _text_states(frame: pd.DataFrame) -> pd.Series:
    """One state per current text. Mixed files keep the more severe state."""
    if frame.empty or "current_sha" not in frame.columns:
        return pd.Series(dtype="object")
    ranked = frame.loc[frame["current_sha"].map(lambda value: bool(value))].copy()
    if ranked.empty:
        return pd.Series(dtype="object")
    ranked["_rank"] = ranked["copy_state"].map(_STATE_RANK).fillna(0)
    ranked = ranked.sort_values("_rank", kind="mergesort")
    return ranked.drop_duplicates("current_sha", keep="last")["copy_state"]


def summary_numbers(staleness: pd.DataFrame, events: pd.DataFrame) -> dict[str, object]:
    """Counts the stage prints: states, stale days, risky misses, sync lag."""
    files = len(staleness)
    numbers: dict[str, object] = {"files": files}
    for state in _STATE_ORDER:
        count = int((staleness["copy_state"] == state).sum()) if files else 0
        numbers[f"files_{state}"] = count
        numbers[f"share_{state}"] = _share(count, files)
    texts = _text_states(staleness)
    numbers["texts"] = int(texts.shape[0])
    for state in _STATE_ORDER:
        count = int((texts == state).sum()) if not texts.empty else 0
        numbers[f"texts_{state}"] = count
        numbers[f"text_share_{state}"] = _share(count, int(texts.shape[0]))
    exact = staleness.loc[staleness["held_basis"].eq("exact_current")] if files else staleness
    numbers["median_days_stale_exact"] = _median_days(exact["days_stale"]) if files else None
    numbers["orphaned"] = numbers["files_orphaned"]
    script_only = 0
    if files:
        script_only = int((
            staleness["copy_state"].ne("orphaned")
            & pd.to_numeric(staleness["skill_md_versions_behind"], errors="coerce").fillna(0).eq(0)
            & pd.to_numeric(staleness["package_versions_behind"], errors="coerce").gt(0)
        ).sum())
    numbers["behind_scripts_only"] = script_only
    numbers["copied_before_release"] = (
        int(staleness["copied_before_release"].fillna(False).sum()) if files else 0
    )
    missed_names = (
        "missed_risky",
        "missed_commands",
        "missed_network",
        "missed_scripts",
        "missed_substantive",
    )
    for name in missed_names:
        if files:
            column = pd.to_numeric(staleness[name], errors="coerce").fillna(0)
        else:
            column = pd.Series(dtype=float)
        numbers[f"{name}_copies"] = int((column > 0).sum()) if files else 0
        numbers[f"{name}_sum"] = int(column.sum()) if files else 0
    numbers["sync_events"] = len(events)
    if events.empty or "event_kind" not in events.columns:
        for kind in ("copy", "update", "downgrade"):
            numbers[f"sync_{kind}"] = 0
            numbers[f"median_lag_{kind}"] = None
        numbers["median_lag_days"] = None
        return numbers
    for kind in ("copy", "update", "downgrade"):
        subset = events.loc[events["event_kind"].eq(kind)]
        numbers[f"sync_{kind}"] = len(subset)
        numbers[f"median_lag_{kind}"] = _median_days(subset["lag_days"]) if len(subset) else None
    numbers["median_lag_days"] = _median_days(events["lag_days"])
    return numbers


def save_outputs(dev: bool, staleness: pd.DataFrame, events: pd.DataFrame) -> None:
    """Write the two parquet files, sorted so a later run prints the same head."""
    paths = _output_paths(dev)
    write_parquet(staleness.loc[:, STALENESS_COLUMNS], paths[0], ["repo", "path"])
    write_parquet(events.loc[:, EVENT_COLUMNS], paths[1], ["repo", "path", "commit_date", "commit"])


def _print_run(staleness: pd.DataFrame, events: pd.DataFrame, seconds: float) -> None:
    """Print the summary and two records from each output.

    The rows are sorted the same way ``save_outputs`` writes them, so the
    sample is the head of the file.
    """
    numbers = summary_numbers(staleness, events)
    numbers["seconds"] = round(seconds, 1)
    print_summary("Staleness", numbers)
    shown = staleness
    if len(staleness) and {"repo", "path"}.issubset(staleness.columns):
        shown = staleness.sort_values(["repo", "path"], kind="mergesort")
    event_rows = events
    if len(events) and {"repo", "path", "commit_date", "commit"}.issubset(events.columns):
        event_rows = events.sort_values(
            ["repo", "path", "commit_date", "commit"], kind="mergesort"
        )
    print_sample_records(shown, "copy_staleness sample")
    print_sample_records(event_rows, "sync_events sample")


def _stale_after_days(value: int | None) -> int:
    """``--stale-days`` when it was passed, otherwise ``STALE_AFTER_DAYS``."""
    if value is None:
        return int(settings.STALE_AFTER_DAYS)
    return int(value)


def main() -> None:
    """Measure staleness for the dev sample or the full copy list.

    ``--dev`` reads and writes the dev folder. ``--force`` rebuilds the two
    outputs. ``--stale-days N`` replaces ``STALE_AFTER_DAYS`` for this run
    and is stored on every row. A second run without ``--force`` prints the
    saved summary and does not read the inputs again.
    """
    started = time.perf_counter()
    parser = build_arg_parser("measure how far copies are behind their registry skill")
    parser.add_argument(
        "--stale-days",
        type=int,
        default=None,
        help="days after the first missed release before a copy is stale (default 30)",
    )
    args = parser.parse_args()
    stale_after = _stale_after_days(args.stale_days)
    outputs = _output_paths(args.dev)
    if outputs_exist(outputs) and not args.force:
        staleness = read_parquet(outputs[0])
        events = read_parquet(outputs[1])
        print("skip")
        _print_run(staleness, events, time.perf_counter() - started)
        return
    # Step 1: English copies, lineage tables, registry snapshot date
    loaded = read_inputs(args.dev)
    copies = loaded["copies"]
    print(f"copies {len(copies)}", flush=True)
    print(f"registry snapshot {loaded['snapshot']}", flush=True)
    # Step 2: Current text, copy date, and whether history was downloaded
    current = current_copies(loaded)
    print(f"with history {int(current['has_history'].sum()) if len(current) else 0}", flush=True)
    # Step 3: A44 held version. A45 versions behind is applied with the state.
    needed = _unmatched_blobs(current, loaded["versions"])
    print(f"need similarity {len(needed)}", flush=True)
    file_sha_of = {
        _sha(blob): _sha(file_sha)
        for blob, file_sha in zip(
            current.get("blob", []), current.get("file_sha", []), strict=False
        )
    }
    sigs = _signatures_for_unmatched(args.dev, needed, file_sha_of)
    held = assign_held_versions(current, loaded["versions"], sigs)
    # Step 4: A46 held package. Missed script updates and missed content.
    print("reading copy folders", flush=True)
    manifests = read_current_manifests(args.dev, held)
    print(f"copy manifest rows {len(manifests)}", flush=True)
    script_shas = _script_shas_to_load(loaded["packages"], loaded["manifests"])
    print(f"script shas {len(script_shas)}", flush=True)
    texts = _load_registry_texts(args.dev, script_shas)
    deltas = build_script_deltas(loaded["packages"], loaded["manifests"], texts)
    print(f"script deltas {len(deltas)}", flush=True)
    # Step 5: A47 copy state. A48 sync events.
    staleness = measure_copy_rows(held, loaded, deltas, manifests, stale_after)
    events = find_sync_events(loaded["histories"], loaded["versions"], loaded["inherited"])
    # Step 6: Save, summary, two sample records
    save_outputs(args.dev, staleness, events)
    _print_run(staleness, events, time.perf_counter() - started)


if __name__ == "__main__":
    main()
