"""Stages 9a, 9b and 9c: how a copy differs from the registry version it started from (RQ2).

What: stage 7 gave every dated copy an origin, one registry version. Part
``origin`` (9a) compares the copy's current text with that origin (unchanged,
cosmetic or substantive, what was added, front matter on its own), asks whether
the headings the copy added are the ones the registry added later (algorithm
A38), labels each edit inside the copy's own history ``sync``, ``other_source``
or ``local`` (algorithm A39), and sets the four kinds of change side by side in
one profile table. Part ``versions`` (9b) scores every version of every copy
against four texts chosen by date: its previous version, the origin, the
registry version current that day and the newest registry version (algorithm
A40), and lists the notable lines each step added (algorithm A41). Part
``folders`` (9c) rebuilds each copy version's skill folder (algorithm A42),
compares it with the same four targets, the registry ones being the package
version current at a date (algorithm A43), flags what added or edited scripts
can do (A16) and counts scripts the ``SKILL.md`` names but the folder lacks, or
the reverse (A17). A label describes the text. It does not say who typed the
file, and a risk flag is a capability, not a malware verdict.

Inputs (dev or full data folder): ``copy_files.parquet`` and ``families.parquet``
(stage 7), ``lineage_versions.parquet`` and ``lineage_changes.parquet`` (4a).
Shared full folder: ``skill_language.parquet`` (2a), ``registry_history/`` texts
(3). Dev or full: ``copied_skill_history/`` (6). GitSkills ``artifacts`` for the
text of a copy that has no stored history text. Part 9c also reads
``copy_version_similarity.parquet`` (9b), ``lineage_package_versions.parquet`` and
``lineage_folder_manifests.parquet`` (4b) and GitSkills ``artifact_siblings`` (the
folder of a copy with no history).

Outputs: ``copy_changes.parquet`` (one row per copy file), ``copy_history_edits.parquet``
(one row per edit in a copy's history), ``change_profile.parquet`` (one row per
population), ``copy_version_similarity.parquet`` (one row per copy version),
``copy_folder_changes.parquet`` (one row per differing file per version and target)
and ``copy_version_folder_summary.parquet`` (one row per version and target).

Run: ``python -m skill_genealogy.measure_copy_changes [--dev] [--force]
[--only origin|versions|folders]``.
Design note: docs/skill_genealogy/design/15_copy_changes.md.
"""

from __future__ import annotations

import bisect
import itertools
import json
import math
import time
from collections import ChainMap, defaultdict
from collections.abc import Mapping
from dataclasses import fields
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from skill_genealogy import settings
from skill_genealogy.assign_families import (
    copy_history_dir,
    newest_history_blobs,
    read_copy_histories,
)
from skill_genealogy.build_registry_lineages import (
    SKILL_MD_NAME,
    _apply_file_change,
    _order_for_replay,
    folder_manifest,
    history_dir,
    is_package_path,
    normalized_sha,
    skill_folder_after_commit,
)
from skill_genealogy.compare_texts import (
    TextChange,
    _body,
    _diff_line_lists,
    _fence_state,
    _has_word,
    check_script_mentions,
    compare_folders,
    compare_front_matter,
    compare_texts,
    find_headings,
    find_network_refs,
    find_project_names_added,
    find_script_mentions,
    flag_script_risks,
)
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
from skill_genealogy.text_helpers import (
    body_lines,
    exact_containment,
    exact_jaccard,
    make_word_shingles,
    normalize_text,
)

TEXT_FIELDS = [item.name for item in fields(TextChange)]
FRONT_FIELDS = [
    "name_changed",
    "description_changed",
    "fm_keys_added",
    "fm_keys_removed",
    "fm_keys_changed",
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
]
HISTORY_EDIT_COLUMNS = [
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
# The four populations of the profile, in the order of the table.
POPULATIONS = ("registry_update", "copy_local_edit", "copy_sync", "copy_vs_origin")
# What an edit inside a copy's own history can be (algorithm A39).
EDIT_KINDS = ("sync", "other_source", "local")

# Stage 9b: the four texts each copy version is scored against (algorithm A40).
TARGETS = ("prev", "origin", "at_date", "newest")
# The scores of one pair; output columns are ``<target>_<score>``.
SCORE_FIELDS = (
    "jaccard",
    "containment",
    "verified",
    "lines_added",
    "lines_removed",
    "body_change_pct",
)
# What a copy version is: the first one, a step inherited from a registry
# history (a fork), or an edit labelled by A39.
VERSION_KINDS = ("copy", "inherited", *EDIT_KINDS)
# Change-note kinds (algorithm A41), in the order that picks one kind per line.
NOTE_KINDS = ("network", "script", "command", "heading", "name")
NETWORK_MARK = "⚠ "
COPY_VERSION_COLUMNS = [
    "repo",
    "path",
    "lineage_id",
    "link",
    "origin_seq",
    "basis",
    "version_seq",
    "commit_date",
    "blob",
    "change_kind",
    *[f"prev_{score}" for score in SCORE_FIELDS],
    *[f"origin_{score}" for score in SCORE_FIELDS],
    "at_date_seq",
    *[f"at_date_{score}" for score in SCORE_FIELDS],
    "newest_seq",
    *[f"newest_{score}" for score in SCORE_FIELDS],
    "change_notes",
]


# Query Q9.1, see docs/skill_genealogy/code_flow/15_copy_changes.md
# Stored text of the wanted shas in a text parquet (registry or copied-skill history).
STORED_TEXTS_SQL = """
SELECT texts.sha AS sha, any_value(texts.text) AS text
FROM read_parquet({path}) AS texts
SEMI JOIN wanted_shas ON texts.sha = wanted_shas.sha
GROUP BY texts.sha
"""

# Query Q9.2, see docs/skill_genealogy/code_flow/15_copy_changes.md
# Which of the wanted shas have a stored, non-null text.
STORED_SHAS_SQL = """
SELECT DISTINCT texts.sha AS sha
FROM read_parquet({path}) AS texts
SEMI JOIN wanted_shas ON texts.sha = wanted_shas.sha
WHERE texts.text IS NOT NULL
"""

# Query Q9.3, see docs/skill_genealogy/code_flow/15_copy_changes.md
# The dataset text of copy files that have no stored history text.
DATASET_TEXTS_SQL = """
SELECT artifacts.file_sha AS sha, any_value(artifacts.content) AS text
FROM read_parquet({path}) AS artifacts
SEMI JOIN wanted_shas ON artifacts.file_sha = wanted_shas.sha
GROUP BY artifacts.file_sha
"""

# Query Q9.4, see docs/skill_genealogy/code_flow/15_copy_changes.md
# Dataset text and last commit date of copy files with no stored history text (stage 9b).
# The date is the file's own row; the text is any row with the same blob, because
# the file's own row can lack the content that another repo's row of the blob has.
DATASET_FILES_SQL = """
WITH file_dates AS (
    SELECT artifacts.repo_full_name AS repo, artifacts.path AS path, artifacts.file_sha AS sha,
           max(artifacts.last_commit_at) AS last_commit_at
    FROM read_parquet({path}) AS artifacts
    SEMI JOIN wanted_files
      ON artifacts.repo_full_name = wanted_files.repo
     AND artifacts.path = wanted_files.path
     AND artifacts.file_sha = wanted_files.sha
    GROUP BY artifacts.repo_full_name, artifacts.path, artifacts.file_sha
),
blob_texts AS (
    SELECT artifacts.file_sha AS sha, any_value(artifacts.content) AS text
    FROM read_parquet({path}) AS artifacts
    SEMI JOIN wanted_files ON artifacts.file_sha = wanted_files.sha
    WHERE artifacts.content IS NOT NULL
    GROUP BY artifacts.file_sha
)
SELECT file_dates.repo, file_dates.path, file_dates.sha, blob_texts.text, file_dates.last_commit_at
FROM file_dates LEFT JOIN blob_texts ON file_dates.sha = blob_texts.sha
"""


# Paths


def output_paths(dev: bool) -> dict[str, Path]:
    """The three stage 9a output files of this mode."""
    folder = settings.data_dir(dev)
    return {
        "copy_changes": folder / "copy_changes.parquet",
        "copy_history_edits": folder / "copy_history_edits.parquet",
        "change_profile": folder / "change_profile.parquet",
    }


def version_output_path(dev: bool) -> Path:
    """The stage 9b output file of this mode."""
    return settings.data_dir(dev) / "copy_version_similarity.parquet"


# Progress


class Progress:
    """Progress lines in the log and a progress file for a long run.

    ``stage`` prints one timestamped line per setup step. ``batch`` prints one
    line per finished batch: copies done, edits found, copies per second and
    an estimated time left. The same numbers go to
    ``logs/measure_copy_changes_<dev|full>.progress.json`` (replaced in one
    step, so a reader never sees half a file).
    """

    def __init__(self, dev: bool, path: Path | None = None) -> None:
        self.start = time.perf_counter()
        self.compare_start: float | None = None
        self.path = path or settings.REPO_ROOT / "logs" / (
            f"measure_copy_changes_{'dev' if dev else 'full'}.progress.json"
        )
        self.state: dict[str, object] = {"stage": "starting", "done": False}

    def _elapsed(self) -> float:
        return time.perf_counter() - self.start

    def _print(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp} +{self._elapsed():7.1f}s] {text}", flush=True)

    def _write(self) -> None:
        self.state["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self.state["elapsed_seconds"] = round(self._elapsed(), 1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.state, indent=2, default=str))
            temp.replace(self.path)
        except OSError:
            pass  # progress must never stop the build

    def stage(self, name: str, detail: str = "") -> None:
        """Start a named step and print one line."""
        self.state.update({"stage": name, "detail": detail})
        self._print(f"{name}: {detail}".rstrip(": "))
        self._write()

    def batch(
        self,
        number: int,
        batches: int,
        copies_done: int,
        copies_total: int,
        edits: int,
        source: str,
        found_label: str = "history_edits",
    ) -> None:
        """Record a finished batch and print one line with the rate and time left.

        ``edits`` is what the batches found so far, named ``found_label``
        (``history_edits`` in stage 9a, ``versions`` in stage 9b).
        """
        now = time.perf_counter()
        if self.compare_start is None:
            self.compare_start = now
        seconds = max(now - self.compare_start, 1e-9)
        rate = copies_done / seconds if copies_done else 0.0
        left = (copies_total - copies_done) / rate if rate > 0 else float("nan")
        percent = round(100 * copies_done / max(copies_total, 1), 1)
        self.state.update(
            {
                "stage": "compare",
                "batch": number,
                "batches": batches,
                "copies_done": copies_done,
                "copies_total": copies_total,
                "percent": percent,
                found_label: edits,
                "text_source": source,
                "copies_per_second": round(rate, 1),
                "eta_seconds": None if rate <= 0 else round(left),
            }
        )
        eta = f"{left / 60:.1f} min" if rate > 0 else "?"
        self._print(
            f"batch {number}/{batches} ({source}): copies {copies_done:,}/{copies_total:,}"
            f" ({percent}%) · {found_label} {edits:,} · {rate:.0f} copies/s · ETA {eta}"
        )
        self._write()

    def start_compare(self) -> None:
        """Start the clock for the rate (after the setup steps)."""
        self.compare_start = time.perf_counter()

    def finish(self) -> None:
        """Mark the run finished."""
        self.state.update({"stage": "done", "done": True})
        self._write()


# Small value helpers


def _as_list(value: object) -> list[str]:
    """A list cell (list, numpy array or missing) as a plain list of strings.

    >>> _as_list(None), _as_list(["a"]), _as_list(float("nan"))
    ([], ['a'], [])
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    return [str(item) for item in value]


def _heading_key(heading: str) -> str:
    """A heading as compared across texts: trimmed and case-folded.

    >>> _heading_key("  ## Deploy ")
    '## deploy'
    """
    return heading.strip().casefold()


# Step 3: customisation and heading overlap


def body_names_added(old_text: str, new_text: str, repo: str, path: str) -> list[str]:
    """Project names the new text adds to its body, front matter left out.

    ``names_added`` of ``compare_texts`` reads the whole file, so a name added
    only as a front-matter key (``metadata: acme:``) is in it while the body,
    and so the state, is unchanged. ``customized`` uses this body-only list.

    >>> old = "---\\nname: x\\n---\\nUse it."
    >>> body_names_added(old, old.replace("name: x", "name: x\\nacme: 1"), "acme/r", "s/SKILL.md")
    []
    >>> body_names_added(old, old + "\\nFor acme.", "acme/r", "s/SKILL.md")
    ['acme']
    """
    return find_project_names_added(_body(old_text), _body(new_text), repo, path)


def is_customized(change: Mapping[str, object]) -> bool:
    """True when a name, script mention, step, command or heading was added.

    A removed step (``steps_added`` below 0) is not customisation. The caller
    passes the body-only names (``body_names_added``) as ``names_added``.

    >>> base = {"names_added": [], "scripts_mentioned_added": [], "steps_added": 0,
    ...         "commands_added": [], "headings_added": []}
    >>> is_customized(base), is_customized({**base, "names_added": ["acme"]})
    (False, True)
    >>> is_customized({**base, "steps_added": -2})
    False
    """
    return bool(
        _as_list(change["names_added"])
        or _as_list(change["scripts_mentioned_added"])
        or _as_list(change["commands_added"])
        or _as_list(change["headings_added"])
        or int(change["steps_added"]) > 0
    )


# Algorithm A38, see docs/skill_genealogy/code_flow/15_copy_changes.md
def heading_overlap_with_registry(
    copy_headings_added: list[str], registry_headings_added_later: list[str]
) -> float:
    """Jaccard overlap of the headings the copy added and the registry added later.

    The size of the shared headings divided by the size of all headings, each
    counted once and compared trimmed and case-folded. A copy that added no
    heading has no direction to compare, so the overlap is NaN, whatever the
    registry added. A copy that added headings while the registry added none
    is 0.

    >>> heading_overlap_with_registry(["Deploy"], ["Deploy", "Tests"])
    0.5
    >>> heading_overlap_with_registry(["Deploy"], [])
    0.0
    >>> math.isnan(heading_overlap_with_registry([], ["Tests"]))
    True
    """
    copy_set = {_heading_key(heading) for heading in copy_headings_added}
    registry_set = {_heading_key(heading) for heading in registry_headings_added_later}
    if not copy_set:
        return float("nan")
    return len(copy_set & registry_set) / len(copy_set | registry_set)


def index_later_headings(registry_changes: pd.DataFrame) -> dict[str, list[tuple[int, list[str]]]]:
    """Headings each registry update added, per lineage: ``{lineage: [(to_seq, headings)]}``.

    >>> changes = pd.DataFrame({"lineage_id": ["L", "L"], "to_seq": [2, 3],
    ...                         "headings_added": [["A"], []]})
    >>> index_later_headings(changes)["L"]
    [(2, ['A']), (3, [])]
    """
    index: dict[str, list[tuple[int, list[str]]]] = defaultdict(list)
    for row in registry_changes.itertuples(index=False):
        index[str(row.lineage_id)].append((int(row.to_seq), _as_list(row.headings_added)))
    return dict(index)


def headings_added_after(
    index: Mapping[str, list[tuple[int, list[str]]]], lineage_id: str, origin_seq: int
) -> list[str]:
    """Headings the registry added in versions after the origin version.

    A heading added by the update that made the origin is not later.

    >>> index = {"L": [(1, ["Old"]), (2, ["Deploy"]), (3, ["Tests"])]}
    >>> headings_added_after(index, "L", 1)
    ['Deploy', 'Tests']
    """
    later: list[str] = []
    for to_seq, headings in index.get(lineage_id, []):
        if to_seq > origin_seq:
            later.extend(headings)
    return later


# Step 2: the current text of a copy


def current_text_of_copy(
    copy_file: Mapping[str, object],
    newest_blobs: Mapping[tuple[str, str], str],
    texts: Mapping[str, str],
) -> tuple[str, str]:
    """The copy's current text and where it came from: ``(text, basis)``.

    ``newest_blobs`` holds the newest history blob of each ``(repo, path)``.
    The basis is ``latest_history`` when that blob has a stored text, else
    ``dataset`` (the text the dataset holds for ``file_sha``). A newest blob
    without a text uses the dataset text, never an older history row. No text
    at all gives ``("", "missing")``.

    >>> copy = {"repo": "a/r", "path": "s/SKILL.md", "file_sha": "crawled"}
    >>> current_text_of_copy(copy, {("a/r", "s/SKILL.md"): "new"}, {"new": "Now", "crawled": "Old"})
    ('Now', 'latest_history')
    >>> current_text_of_copy(copy, {("a/r", "s/SKILL.md"): "gone"}, {"crawled": "Old"})
    ('Old', 'dataset')
    >>> current_text_of_copy(copy, {}, {})
    ('', 'missing')
    """
    blob = newest_blobs.get((str(copy_file["repo"]), str(copy_file["path"])))
    if blob is not None and blob in texts:
        return texts[blob], "latest_history"
    file_sha = str(copy_file["file_sha"])
    if file_sha in texts:
        return texts[file_sha], "dataset"
    return "", "missing"


# Step 3: copy against origin


def compare_copy_with_origin(
    copy_files: pd.DataFrame,
    newest_blobs: Mapping[tuple[str, str], str],
    texts: Mapping[str, str],
    origin_blobs: Mapping[tuple[str, int], str],
    later_headings: Mapping[str, list[tuple[int, list[str]]]],
) -> pd.DataFrame:
    """Compare each copy's current text with its origin version.

    Inputs: copy files (``repo, path, file_sha, lineage_id, link, origin_seq``),
    the newest history blob per file, ``sha -> text``, ``(lineage, seq) -> blob``
    of the registry versions and the index of later headings. A copy whose
    origin text or current text is missing has no row (counted by the caller).
    Outputs: one row per compared copy, columns ``COPY_CHANGE_COLUMNS``.
    """
    rows = []
    for copy in copy_files.to_dict("records"):
        origin_seq = int(copy["origin_seq"])
        origin_text = texts.get(origin_blobs.get((copy["lineage_id"], origin_seq), ""))
        current_text, basis = current_text_of_copy(copy, newest_blobs, texts)
        if origin_text is None or basis == "missing":
            continue
        change = compare_texts(origin_text, current_text, copy["repo"], copy["path"]).to_dict()
        front = compare_front_matter(origin_text, current_text)
        later = headings_added_after(later_headings, copy["lineage_id"], origin_seq)
        body_names = body_names_added(origin_text, current_text, copy["repo"], copy["path"])
        rows.append(
            {
                "repo": copy["repo"],
                "path": copy["path"],
                "lineage_id": copy["lineage_id"],
                "link": copy["link"],
                "origin_seq": origin_seq,
                "current_basis": basis,
                **change,
                **front,
                "customized": is_customized({**change, "names_added": body_names}),
                "heading_overlap": heading_overlap_with_registry(change["headings_added"], later),
            }
        )
    return pd.DataFrame(rows, columns=COPY_CHANGE_COLUMNS)


# Step 4: edits inside a copy's history


def index_lineages_by_blob(registry_versions: pd.DataFrame) -> dict[str, set[str]]:
    """Which lineages have a registry version with this blob: ``{blob: {lineage_id}}``.

    >>> versions = pd.DataFrame({"lineage_id": ["L", "M"], "blob": ["x", "x"]})
    >>> sorted(index_lineages_by_blob(versions)["x"])
    ['L', 'M']
    """
    index: dict[str, set[str]] = defaultdict(set)
    for lineage_id, blob in zip(
        registry_versions["lineage_id"], registry_versions["blob"], strict=True
    ):
        if isinstance(blob, str):
            index[blob].add(str(lineage_id))
    return dict(index)


def label_edit(to_blob: str, lineage_id: str, lineages_by_blob: Mapping[str, set[str]]) -> str:
    """``sync``, ``other_source`` or ``local`` from the lineages that hold the new blob.

    >>> owners = {"x": {"L"}, "y": {"M"}}
    >>> [label_edit(blob, "L", owners) for blob in ("x", "y", "z")]
    ['sync', 'other_source', 'local']
    """
    owners = lineages_by_blob.get(to_blob, set())
    if lineage_id in owners:
        return "sync"
    return "other_source" if owners else "local"


# Algorithm A39, see docs/skill_genealogy/code_flow/15_copy_changes.md
def classify_history_edits(
    histories: pd.DataFrame,
    registry_versions: pd.DataFrame,
    texts: Mapping[str, str],
) -> pd.DataFrame:
    """Label each edit in a copy's history ``sync``, ``other_source`` or ``local``.

    ``histories`` has one row per change of a copy file (``repo, path,
    lineage_id, blob, commit, commit_date, commit_seq, status, inherited``).
    Deletes have no blob and are dropped. The remaining rows of a file are
    paired in order, inherited rows included, so an edit made right after a
    fork's inherited commits is compared with the text it actually changed. A
    pair whose new commit is inherited (a registry history also holds it) is
    not the copy's own edit and is left out. A pair with the same blob is not
    an edit, and a pair whose text is not stored is left out. A pair whose
    texts differ only in spaces, line endings, quotes or blank lines (the same
    ``normalized_sha``) is not an edit either: stage 4a does not count such a
    change as a registry version, and the profile sets the two side by side.
    The new blob is ``sync`` when it is a version of the copy's own lineage,
    ``other_source`` when it is a version of another lineage, else ``local``.
    Each edit also gets its ``TextChange`` and ``permissions_changed``. Pairs
    left out for a missing text are counted in ``.attrs["no_text"]``, pairs
    left out as whitespace only in ``.attrs["whitespace_only"]``.

    >>> versions = pd.DataFrame({"lineage_id": ["L"], "blob": ["v2"]})
    >>> rows = pd.DataFrame({
    ...     "repo": ["a/r"] * 3, "path": ["s/SKILL.md"] * 3, "lineage_id": ["L"] * 3,
    ...     "blob": ["v1", "mine", "v2"], "commit": ["c1", "c2", "c3"], "commit_seq": [1, 2, 3],
    ...     "commit_date": pd.to_datetime(["2026-01-01", "2026-02-01", "2026-03-01"], utc=True),
    ...     "status": ["A", "M", "M"], "inherited": [False] * 3})
    >>> texts = {"v1": "One", "mine": "One\\nTwo", "v2": "One\\nTwo\\nThree"}
    >>> classify_history_edits(rows, versions, texts)["change_kind"].tolist()
    ['local', 'sync']
    """
    lineages_by_blob = index_lineages_by_blob(registry_versions)
    kept = histories.loc[histories["blob"].notna() & (histories["status"] != "D")]
    kept = kept.sort_values(
        ["repo", "path", "commit_date", "commit_seq"], kind="mergesort", na_position="first"
    )
    edits, no_text, whitespace_only = [], 0, 0
    for (_repo, _path), file_rows in kept.groupby(["repo", "path"], sort=False):
        records = file_rows.to_dict("records")
        for before, after in zip(records, records[1:], strict=False):
            if bool(after["inherited"]) or before["blob"] == after["blob"]:
                continue
            if before["blob"] not in texts or after["blob"] not in texts:
                no_text += 1
                continue
            if normalized_sha(texts[before["blob"]]) == normalized_sha(texts[after["blob"]]):
                whitespace_only += 1
                continue
            edits.append(build_edit_row(before, after, texts, lineages_by_blob))
    frame = pd.DataFrame(edits, columns=HISTORY_EDIT_COLUMNS)
    frame.attrs["no_text"] = no_text
    frame.attrs["whitespace_only"] = whitespace_only
    return frame


def build_edit_row(
    before: Mapping[str, object],
    after: Mapping[str, object],
    texts: Mapping[str, str],
    lineages_by_blob: Mapping[str, set[str]],
) -> dict[str, object]:
    """One edit row: the step from history row ``before`` to ``after``."""
    old_text, new_text = texts[str(before["blob"])], texts[str(after["blob"])]
    change = compare_texts(old_text, new_text, str(after["repo"]), str(after["path"])).to_dict()
    front = compare_front_matter(old_text, new_text)
    lineage_id = str(after["lineage_id"])
    return {
        "repo": after["repo"],
        "path": after["path"],
        "lineage_id": lineage_id,
        "from_blob": before["blob"],
        "to_blob": after["blob"],
        "commit": after["commit"],
        "commit_date": after["commit_date"],
        "change_kind": label_edit(str(after["blob"]), lineage_id, lineages_by_blob),
        "inherited": False,
        **change,
        "permissions_changed": front["permissions_changed"],
    }


# Step 5: the change profile


def _share(frame: pd.DataFrame, flags: pd.Series) -> float:
    """Share of rows where ``flags`` is true; NaN for an empty population."""
    return float(flags.sum()) / len(frame) if len(frame) else float("nan")


def _profile_row(population: str, frame: pd.DataFrame) -> dict[str, object]:
    """One profile row: the number of changes and the share with each kind of change."""
    if frame.empty:
        has = {name: pd.Series(dtype=bool) for name in PROFILE_COLUMNS[2:]}
    else:
        listed = lambda column: frame[column].map(lambda cell: len(_as_list(cell)) > 0)  # noqa: E731
        has = {
            "share_substantive": frame["state"] == "substantive",
            "share_headings_added": listed("headings_added"),
            "share_commands_added": listed("commands_added"),
            "share_network_added": listed("network_refs_added"),
            "share_scripts_added": listed("scripts_mentioned_added"),
            "share_permissions_changed": frame["permissions_changed"].astype(bool),
        }
    row: dict[str, object] = {"population": population, "changes": len(frame)}
    row.update({name: _share(frame, flags) for name, flags in has.items()})
    return row


def build_change_profile(
    registry_changes: pd.DataFrame, history_edits: pd.DataFrame, copy_changes: pd.DataFrame
) -> pd.DataFrame:
    """The same shares for four populations of change.

    ``registry_update`` is a registry's own version-to-version change,
    ``copy_local_edit`` and ``copy_sync`` are edits inside copy histories (A39),
    ``copy_vs_origin`` is each copy against its origin. ``other_source`` edits
    are not a population. An empty population has 0 changes and null shares.

    >>> registry = pd.DataFrame({"state": ["substantive", "cosmetic"],
    ...     "headings_added": [["A"], []], "commands_added": [[], []],
    ...     "network_refs_added": [[], []], "scripts_mentioned_added": [[], []],
    ...     "permissions_changed": [False, False]})
    >>> profile = build_change_profile(registry, registry.iloc[0:0], registry.iloc[0:0])
    >>> by_population = profile.set_index("population")
    >>> by_population.loc["registry_update", ["changes", "share_substantive"]].tolist()
    [2.0, 0.5]
    """
    frames = {
        "registry_update": registry_changes,
        "copy_local_edit": history_edits.loc[history_edits["change_kind"] == "local"]
        if "change_kind" in history_edits
        else history_edits,
        "copy_sync": history_edits.loc[history_edits["change_kind"] == "sync"]
        if "change_kind" in history_edits
        else history_edits,
        "copy_vs_origin": copy_changes,
    }
    rows = [_profile_row(name, frames[name]) for name in POPULATIONS]
    return pd.DataFrame(rows, columns=PROFILE_COLUMNS)


# Step 1: the copies and their texts


def select_copies(
    copy_files: pd.DataFrame, families: pd.DataFrame, english: set[str]
) -> pd.DataFrame:
    """Copy files that can be compared with an origin (rule 1).

    A copy has an origin version, was not copied before the registry release
    (``copied_before_release``), its text is English and it is not a registry's
    own file (``registry_self``).

    >>> files = pd.DataFrame({"file_sha": ["a", "b", "c", "d"], "origin_seq": [1, 1, None, 1],
    ...                       "copied_before_release": [False, True, False, False]})
    >>> families = pd.DataFrame({"file_sha": ["a", "b", "c", "d"],
    ...                          "registry_self": [False, False, False, True]})
    >>> select_copies(files, families, {"a", "b", "c", "d"})["file_sha"].tolist()
    ['a']
    """
    self_shas = set(families.loc[families["registry_self"], "file_sha"])
    keep = (
        copy_files["origin_seq"].notna()
        & ~copy_files["copied_before_release"].astype(bool)
        & copy_files["file_sha"].isin(english)
        & ~copy_files["file_sha"].isin(self_shas)
    )
    return copy_files.loc[keep].reset_index(drop=True)


def _text_path(dev: bool, which: str) -> Path:
    """The text parquet of the registry history (shared) or the copied-skill history."""
    if which == "registry":
        return history_dir() / "registry_history_texts.parquet"
    return copy_history_dir(dev) / "copied_skill_history_texts.parquet"


def _register_shas(con: duckdb.DuckDBPyConnection, shas: set[str]) -> None:
    """Make ``wanted_shas`` (column ``sha``) the table the text queries join to."""
    con.register("wanted_shas", pd.DataFrame({"sha": sorted(shas)}))


def only_stored_text(rows: list[tuple[object, object]]) -> dict[str, str]:
    """``sha -> text`` from query rows, leaving out a row whose text cell is null.

    A null text is a missing text, not an empty file. Turning it into ``""`` would
    make the copy look like a text that lost every line.

    >>> only_stored_text([("a", "Body"), ("b", None)])
    {'a': 'Body'}
    """
    return {str(sha): text for sha, text in rows if isinstance(text, str)}


def load_stored_texts(path: Path, shas: set[str]) -> dict[str, str]:
    """Query Q9.1: ``sha -> text`` for the wanted shas that the text parquet stores."""
    if not shas:
        return {}
    con = connect_duckdb()
    try:
        con.execute(f"SET memory_limit = '{settings.HISTORY_DUCKDB_MEMORY_LIMIT}'")
        _register_shas(con, shas)
        rows = con.execute(STORED_TEXTS_SQL.format(path=_quote_path(str(path)))).fetchall()
    finally:
        con.close()
    return only_stored_text(rows)


def stored_shas(path: Path, shas: set[str]) -> set[str]:
    """Query Q9.2: the wanted shas that the text parquet stores (no text is read)."""
    if not shas:
        return set()
    con = connect_duckdb()
    try:
        _register_shas(con, shas)
        rows = con.execute(STORED_SHAS_SQL.format(path=_quote_path(str(path)))).fetchall()
    finally:
        con.close()
    return {str(row[0]) for row in rows}


def load_dataset_texts(shas: set[str]) -> dict[str, str]:
    """Query Q9.3: ``file_sha -> text`` from the dataset, for copies with no history text."""
    if not shas:
        return {}
    con = connect_duckdb()
    try:
        con.execute(f"SET memory_limit = '{settings.DUCKDB_MEMORY_LIMIT}'")
        _register_shas(con, shas)
        rows = con.execute(
            DATASET_TEXTS_SQL.format(path=_quote_path(settings.ARTIFACTS_GLOB))
        ).fetchall()
    finally:
        con.close()
    return only_stored_text(rows)


def read_inputs(dev: bool) -> dict[str, object]:
    """Check the input files, then read the small tables.

    A missing file raises ``FileNotFoundError`` naming the command that builds it.
    """
    folder, full = settings.data_dir(dev), settings.data_dir(False)
    flag = " --dev" if dev else ""
    paths: dict[str, Path | str] = {
        "copy_files": folder / "copy_files.parquet",
        "families": folder / "families.parquet",
        "lineage_versions": folder / "lineage_versions.parquet",
        "lineage_changes": folder / "lineage_changes.parquet",
        "skill_language": full / "skill_language.parquet",
        "registry_texts": _text_path(dev, "registry"),
        "history_texts": _text_path(dev, "history"),
        "artifacts": settings.ARTIFACTS_GLOB,
    }
    stage7 = f"python -m skill_genealogy.assign_families{flag}"
    stage4 = f"python -m skill_genealogy.build_registry_lineages{flag}"
    check_inputs_exist(
        paths,
        {
            "copy_files": stage7,
            "families": stage7,
            "lineage_versions": stage4,
            "lineage_changes": stage4,
            "skill_language": "python -m skill_genealogy.select_registries --force-languages",
            "registry_texts": "python -m skill_genealogy.download_registry_history",
            "history_texts": f"python -m skill_genealogy.download_copied_skill_history{flag}",
            "artifacts": "python src/data/download_data.py",
        },
    )
    language = read_parquet(paths["skill_language"])
    return {
        "copy_files": read_parquet(paths["copy_files"]),
        "families": read_parquet(paths["families"]),
        "versions": read_parquet(paths["lineage_versions"]),
        "registry_changes": read_parquet(paths["lineage_changes"]),
        "english": set(language.loc[language["is_english"], "file_sha"]),
    }


# Build


def batches_of_files(copies: pd.DataFrame, size: int) -> list[pd.DataFrame]:
    """Split the copy files into batches of ``size`` rows; a file is never split."""
    return [copies.iloc[start : start + size] for start in range(0, len(copies), size)]


def find_copies_needing_dataset_text(
    dev: bool, copies: pd.DataFrame, newest_blobs: Mapping[tuple[str, str], str]
) -> pd.Series:
    """True for copies whose newest history blob has no stored text.

    Most copies have a stored history text. The rest (no history, or a newest
    blob that was not stored) fall back to the dataset text of ``file_sha``.
    They are batched apart from the others, so the dataset is read only for them
    (query Q9.3) and never held as one big dictionary.
    """
    keys = list(zip(copies["repo"], copies["path"], strict=True))
    stored = stored_shas(
        _text_path(dev, "history"), {newest_blobs[k] for k in keys if k in newest_blobs}
    )
    needs = [newest_blobs.get(key) not in stored for key in keys]
    return pd.Series(needs, index=copies.index, dtype=bool)


def build_copy_measures(
    dev: bool, inputs: Mapping[str, object], progress: Progress | None = None
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """Steps 1-5: compare copies with origins, label history edits, build the profile.

    Returns ``copy_changes``, ``history_edits``, ``profile`` and a dict of
    counts (copies selected, compared, left out for a missing text, edits
    left out for a missing text). ``progress`` (optional) gets a line per
    setup step and per batch.
    """
    progress = progress or Progress(dev)
    versions, registry_changes = inputs["versions"], inputs["registry_changes"]
    # Step 1: the copies, their histories and the origin texts (read once: a few hundred MB)
    copies = select_copies(inputs["copy_files"], inputs["families"], inputs["english"])
    progress.stage("read copy histories", f"{len(copies):,} copies selected")
    histories = read_copy_histories(dev, copies)
    histories = histories.merge(copies.loc[:, ["repo", "path", "lineage_id"]], on=["repo", "path"])
    newest_blobs = {
        (row.repo, row.path): row.blob for row in newest_history_blobs(histories).itertuples()
    }
    origin_blobs = {
        (row.lineage_id, int(row.version_seq)): row.blob
        for row in versions.loc[:, ["lineage_id", "version_seq", "blob"]].itertuples()
    }
    wanted_origins = {
        origin_blobs[(lineage, int(seq))]
        for lineage, seq in zip(copies["lineage_id"], copies["origin_seq"], strict=True)
        if (lineage, int(seq)) in origin_blobs
    }
    progress.stage(
        "read origin texts",
        f"{len(histories):,} history rows, {len(wanted_origins):,} origin versions",
    )
    origin_texts = load_stored_texts(_text_path(dev, "registry"), wanted_origins)
    later = index_later_headings(registry_changes)
    progress.stage("find copies without a stored history text")
    needs_dataset = find_copies_needing_dataset_text(dev, copies, newest_blobs)
    groups = [(copies.loc[~needs_dataset], False), (copies.loc[needs_dataset], True)]
    size = settings.COPY_CHANGE_FILE_BATCH
    batches = sum(math.ceil(len(group) / size) for group, _uses in groups)
    progress.stage(
        "compare",
        f"{int((~needs_dataset).sum()):,} copies with a history text,"
        f" {int(needs_dataset.sum()):,} read from the dataset; {batches} batches of {size:,}",
    )
    progress.start_compare()
    changes, edits = [], []
    no_edit_text, whitespace_edits, copies_done, edits_found, number = 0, 0, 0, 0, 0
    for group, uses_dataset in groups:
        for batch in batches_of_files(group, size):
            keys = batch.loc[:, ["repo", "path"]]
            batch_history = histories.merge(keys, on=["repo", "path"])
            history_texts = load_stored_texts(
                _text_path(dev, "history"), set(batch_history["blob"].dropna())
            )
            dataset_texts = load_dataset_texts(set(batch["file_sha"])) if uses_dataset else {}
            texts = ChainMap(history_texts, dataset_texts, origin_texts)
            # Steps 2-3: current text, comparison with the origin, heading overlap (A38)
            changes.append(
                compare_copy_with_origin(batch, newest_blobs, texts, origin_blobs, later)
            )
            # Step 4: the edits inside each history (A39)
            batch_edits = classify_history_edits(batch_history, versions, texts)
            no_edit_text += batch_edits.attrs.get("no_text", 0)
            whitespace_edits += batch_edits.attrs.get("whitespace_only", 0)
            edits.append(batch_edits)
            number += 1
            copies_done += len(batch)
            edits_found += len(batch_edits)
            progress.batch(
                number,
                batches,
                copies_done,
                len(copies),
                edits_found,
                "dataset" if uses_dataset else "history",
            )
    copy_changes = (
        pd.concat(changes, ignore_index=True)
        if changes
        else pd.DataFrame(columns=COPY_CHANGE_COLUMNS)
    )
    history_edits = (
        pd.concat(edits, ignore_index=True) if edits else pd.DataFrame(columns=HISTORY_EDIT_COLUMNS)
    )
    # Step 5: the four populations, registry updates of the lineages that have copies
    own_lineages = registry_changes["lineage_id"].isin(set(copies["lineage_id"]))
    profile = build_change_profile(registry_changes.loc[own_lineages], history_edits, copy_changes)
    counts = {
        "copies_selected": len(copies),
        "copies_compared": len(copy_changes),
        "copies_left_out_no_text": len(copies) - len(copy_changes),
        "edits_left_out_no_text": no_edit_text,
        "edits_left_out_whitespace_only": whitespace_edits,
    }
    return copy_changes, history_edits, profile, counts


# Stage 9b, step 1: the versions of each copy


def _ordered_history_rows(histories: pd.DataFrame) -> pd.DataFrame:
    """History rows with a blob, deletes left out, in the order A39 pairs them."""
    kept = histories.loc[histories["blob"].notna() & (histories["status"] != "D")]
    return kept.sort_values(
        ["repo", "path", "commit_date", "commit_seq"], kind="mergesort", na_position="first"
    )


def _versions_of_one_file(
    file_rows: list[dict], texts: Mapping[str, str], counts: dict[str, int]
) -> list[dict]:
    """The kept versions of one file's ordered history rows.

    A row is left out when its text is not stored (``no_text``), when it has
    the same blob as the last kept version, or when its text differs from it
    only in spaces, line endings, quotes or blank lines (``whitespace_only``,
    the same ``normalized_sha``; A20, A39).
    """
    kept: list[dict] = []
    last_blob, last_sha = None, None
    for row in file_rows:
        blob = str(row["blob"])
        if blob not in texts:
            counts["no_text"] += 1
            continue
        if blob == last_blob:
            continue
        text_sha = normalized_sha(texts[blob])
        if text_sha == last_sha:
            counts["whitespace_only"] += 1
            continue
        kept.append(row)
        last_blob, last_sha = blob, text_sha
    return kept


def list_copy_versions(
    copy_files: pd.DataFrame,
    histories: pd.DataFrame,
    dataset_files: pd.DataFrame,
    texts: Mapping[str, str],
) -> pd.DataFrame:
    """One row per version of each copy file, numbered 1, 2, ... in date order.

    A copy's versions are its stage 6 history rows (``_versions_of_one_file``).
    A copy with no kept history version gets one row: its dataset text
    (``file_sha``) dated ``last_commit_at`` from ``dataset_files`` (``repo,
    path, file_sha, last_commit_at``), basis ``dataset``. A copy with neither
    has no row. Counts of left-out rows are in ``.attrs``.

    >>> copies = pd.DataFrame({"repo": ["a/r", "b/r"], "path": ["SKILL.md"] * 2,
    ...     "file_sha": ["x", "d"], "lineage_id": ["L"] * 2, "link": ["exact"] * 2,
    ...     "origin_seq": [1, 1]})
    >>> history = pd.DataFrame({"repo": ["a/r"] * 3, "path": ["SKILL.md"] * 3,
    ...     "blob": ["x", "x", "y"], "status": ["A", "M", "M"], "commit_seq": [1, 2, 3],
    ...     "commit": ["c1", "c2", "c3"], "inherited": [False] * 3,
    ...     "commit_date": pd.to_datetime(["2026-01-01", "2026-02-01", "2026-03-01"], utc=True)})
    >>> dates = pd.DataFrame({"repo": ["b/r"], "path": ["SKILL.md"], "file_sha": ["d"],
    ...     "last_commit_at": pd.to_datetime(["2026-04-01"], utc=True)})
    >>> rows = list_copy_versions(copies, history, dates, {"x": "One", "y": "Two", "d": "Old"})
    >>> rows[["repo", "version_seq", "blob", "basis"]].values.tolist()
    [['a/r', 1, 'x', 'history'], ['a/r', 2, 'y', 'history'], ['b/r', 1, 'd', 'dataset']]
    """
    counts = {"no_text": 0, "whitespace_only": 0, "no_version": 0}
    by_file: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in _ordered_history_rows(histories).to_dict("records"):
        by_file[(row["repo"], row["path"])].append(row)
    dated = {
        (row.repo, row.path, row.file_sha): row.last_commit_at
        for row in dataset_files.itertuples(index=False)
    }
    rows = []
    for copy in copy_files.to_dict("records"):
        key = (copy["repo"], copy["path"])
        base = {name: copy[name] for name in ("repo", "path", "lineage_id", "link", "origin_seq")}
        kept = _versions_of_one_file(by_file.get(key, []), texts, counts)
        for number, version in enumerate(kept, start=1):
            rows.append(
                {
                    **base,
                    "basis": "history",
                    "version_seq": number,
                    "commit_date": version["commit_date"],
                    "blob": str(version["blob"]),
                    "inherited": bool(version["inherited"]),
                }
            )
        if kept:
            continue
        file_sha = str(copy["file_sha"])
        if file_sha in texts and (*key, file_sha) in dated:
            rows.append(
                {
                    **base,
                    "basis": "dataset",
                    "version_seq": 1,
                    "commit_date": dated[(*key, file_sha)],
                    "blob": file_sha,
                    "inherited": False,
                }
            )
        else:
            counts["no_version"] += 1
    columns = ["repo", "path", "lineage_id", "link", "origin_seq", "basis", "version_seq",
               "commit_date", "blob", "inherited"]  # fmt: skip
    frame = pd.DataFrame(rows, columns=columns)
    frame["commit_date"] = pd.to_datetime(frame["commit_date"], utc=True)
    frame.attrs.update(counts)
    return frame


# Stage 9b, step 2: the four comparison targets


# Algorithm A40, see docs/skill_genealogy/code_flow/15_copy_changes.md
def pick_comparison_targets(
    copy_version: Mapping[str, object], lineage_versions: pd.DataFrame, origin_seq: int
) -> dict[str, int | None]:
    """The four texts a copy version is scored against, chosen by date.

    ``prev`` is the copy's previous version (none for version 1). ``origin`` is
    the version stage 7 picked, the same for every version of the copy.
    ``at_date`` is the newest lineage version released on or before the copy
    version's date (the one with the latest release date, then the highest
    ``version_seq``); none when nothing was released by then or the date is
    missing. ``newest`` is the lineage's highest ``version_seq``.

    Registry v1 is 10 Jan, v2 1 Mar, v3 15 May; copy c3 is 20 Apr, version 3:

    >>> versions = pd.DataFrame({"lineage_id": ["L"] * 3, "version_seq": [1, 2, 3],
    ...     "release_date": pd.to_datetime(["2026-01-10", "2026-03-01", "2026-05-15"], utc=True)})
    >>> c3 = {"lineage_id": "L", "version_seq": 3,
    ...       "commit_date": pd.Timestamp("2026-04-20", tz="UTC")}
    >>> pick_comparison_targets(c3, versions, 1)
    {'prev_version_seq': 2, 'origin_seq': 1, 'at_date_seq': 2, 'newest_seq': 3}
    """
    own = lineage_versions.loc[lineage_versions["lineage_id"] == copy_version["lineage_id"]]
    version_seq = int(copy_version["version_seq"])
    date = copy_version["commit_date"]
    at_date = None
    if not pd.isna(date):
        released = own.loc[own["release_date"] <= date]
        if len(released):
            last = released.sort_values(["release_date", "version_seq"]).iloc[-1]
            at_date = int(last["version_seq"])
    return {
        "prev_version_seq": version_seq - 1 if version_seq > 1 else None,
        "origin_seq": int(origin_seq),
        "at_date_seq": at_date,
        "newest_seq": int(own["version_seq"].max()) if len(own) else None,
    }


def index_lineage_releases(lineage_versions: pd.DataFrame) -> dict[str, tuple[list, list, int]]:
    """Per lineage: release dates (ns, sorted), the matching seqs and the newest seq.

    The fast form of the lookups in ``pick_comparison_targets``; versions are
    sorted by release date, then ``version_seq``. Dates are nanoseconds since
    1970 whatever the column's unit, so they compare with ``Timestamp.value``.
    A version without a release date is never an at-date target, but still
    counts for the newest seq.

    >>> versions = pd.DataFrame({"lineage_id": ["L", "L", "L"], "version_seq": [2, 1, 3],
    ...     "release_date": pd.to_datetime(["2026-03-01", "2026-01-10", None], utc=True)})
    >>> index_lineage_releases(versions)["L"][1:]
    ([1, 2], 3)
    """
    ordered = lineage_versions.sort_values(["lineage_id", "release_date", "version_seq"])
    index: dict[str, tuple[list, list, int]] = {}
    for lineage_id, rows in ordered.groupby("lineage_id", sort=False):
        dated = rows.loc[rows["release_date"].notna()]
        dates = dated["release_date"].dt.as_unit("ns").astype("int64").tolist()
        seqs = [int(seq) for seq in dated["version_seq"]]
        index[str(lineage_id)] = (dates, seqs, int(rows["version_seq"].max()))
    return index


def targets_from_index(
    releases: Mapping[str, tuple[list, list, int]],
    lineage_id: str,
    version_seq: int,
    commit_date: pd.Timestamp,
    origin_seq: int,
) -> dict[str, int | None]:
    """``pick_comparison_targets`` with a binary search on the indexed release dates.

    >>> versions = pd.DataFrame({"lineage_id": ["L"] * 3, "version_seq": [1, 2, 3],
    ...     "release_date": pd.to_datetime(["2026-01-10", "2026-03-01", "2026-05-15"], utc=True)})
    >>> targets_from_index(index_lineage_releases(versions), "L", 1,
    ...                    pd.Timestamp("2026-01-01", tz="UTC"), 1)["at_date_seq"] is None
    True
    """
    dates, seqs, newest = releases.get(lineage_id, ([], [], None))
    at_date = None
    if not pd.isna(commit_date) and dates:
        position = bisect.bisect_right(dates, pd.Timestamp(commit_date).value)
        at_date = seqs[position - 1] if position else None
    return {
        "prev_version_seq": version_seq - 1 if version_seq > 1 else None,
        "origin_seq": int(origin_seq),
        "at_date_seq": at_date,
        "newest_seq": newest,
    }


# Stage 9b, step 3: scores of one pair


# Rule 1: the scores of two identical texts.
IDENTICAL_SCORES: dict[str, float | int] = {
    "jaccard": 1.0,
    "containment": 1.0,
    "verified": 1.0,
    "lines_added": 0,
    "lines_removed": 0,
    "body_change_pct": 0.0,
}


def comparable_shingles(text: str) -> set[str]:
    """The 5-word shingles of the whole body (front matter left out).

    No boilerplate line is removed. Stage 5a removes lines many owners share so
    that unrelated texts are not linked through them; here the two texts are
    already known to be related, and in a popular skill nearly every line is
    such a line.

    >>> sorted(comparable_shingles("---\\nname: x\\n---\\na b c d e f"))
    ['a b c d e', 'b c d e f']
    """
    return make_word_shingles("\n".join(body_lines(text)))


def scores_from_parts(
    target_lines: list[str],
    copy_lines: list[str],
    target_shingles: set[str],
    copy_shingles: set[str],
) -> dict[str, float | int | None]:
    """The six scores of a pair of different texts from their body lines and shingle sets.

    One line diff gives the lines kept, added and removed; the target's line
    count is kept plus removed. ``verified`` is the share of target lines the
    copy kept (A10), ``body_change_pct`` is (added + removed) / target lines
    * 100, as in ``compare_texts``. Rule 3, nothing to measure is null, not 0:
    Jaccard is null when neither body has a word, containment when the target
    has none, ``verified`` and ``body_change_pct`` when the target has no body
    line. A copy that deleted its whole body has something to measure: 0.

    >>> deleted = scores_from_parts(["Old step one two three"], [], {"step one two three"}, set())
    >>> deleted["jaccard"], deleted["containment"], deleted["verified"]
    (0.0, 0.0, 0.0)
    >>> empty_target = scores_from_parts([], ["New"], set(), set())
    >>> empty_target["verified"], empty_target["body_change_pct"]
    (None, None)
    """
    diff = _diff_line_lists(target_lines, copy_lines)
    kept = sum(operation == " " for operation, _line in diff)
    added = sum(operation == "+" for operation, _line in diff)
    removed = sum(operation == "-" for operation, _line in diff)
    target_count = kept + removed
    return {
        "jaccard": (
            exact_jaccard(target_shingles, copy_shingles)
            if target_shingles or copy_shingles
            else None
        ),
        "containment": (
            exact_containment(target_shingles, copy_shingles) if target_shingles else None
        ),
        "verified": kept / target_count if target_count else None,
        "lines_added": added,
        "lines_removed": removed,
        "body_change_pct": (added + removed) / target_count * 100 if target_count else None,
    }


def score_pair(old_text: str, new_text: str) -> dict[str, float | int | None]:
    """Exact Jaccard, containment, verified share and line counts of a target and a copy text.

    ``old_text`` is the target (previous version or registry text): containment
    is the share of its shingles the copy holds, ``verified`` the share of its
    body lines the copy kept unchanged. Rule 1: identical texts score
    ``IDENTICAL_SCORES`` whatever their length.

    >>> [score_pair("Hi", "Hi")[name] for name in SCORE_FIELDS]
    [1.0, 1.0, 1.0, 0, 0, 0.0]
    """
    if old_text == new_text:
        return dict(IDENTICAL_SCORES)
    return scores_from_parts(
        body_lines(old_text),
        body_lines(new_text),
        comparable_shingles(old_text),
        comparable_shingles(new_text),
    )


class PairScorer:
    """``score_pair`` and ``change_notes_for_step`` on blobs, each pair computed once.

    Many copies are the same text as their origin, so the same pair of blobs
    comes back often. Body lines and shingles are kept per blob, scores and
    notes per pair. ``texts`` maps blob to text; a missing text gives None.
    """

    def __init__(self, texts: Mapping[str, str]) -> None:
        self.texts = texts
        self.parts: dict[str, tuple[list[str], set[str]]] = {}
        self.scores: dict[tuple[str, str], dict[str, float | int | None]] = {}
        self.notes: dict[tuple[str, str, str, str], str] = {}

    def _parts(self, blob: str) -> tuple[list[str], set[str]]:
        if blob not in self.parts:
            text = self.texts[blob]
            self.parts[blob] = (body_lines(text), comparable_shingles(text))
        return self.parts[blob]

    def score(
        self, target_blob: str | None, copy_blob: str
    ) -> dict[str, float | int | None] | None:
        """The six scores of target against copy, or None when a text is missing.

        Rule 1: the same blob is the same text, so it scores ``IDENTICAL_SCORES``.
        """
        if target_blob is None or target_blob not in self.texts or copy_blob not in self.texts:
            return None
        if target_blob == copy_blob:
            return IDENTICAL_SCORES
        key = (target_blob, copy_blob)
        if key not in self.scores:
            target_lines, target_shingles = self._parts(target_blob)
            copy_lines, copy_shingles = self._parts(copy_blob)
            self.scores[key] = scores_from_parts(
                target_lines, copy_lines, target_shingles, copy_shingles
            )
        return self.scores[key]

    def change_notes(self, prev_blob: str | None, blob: str, repo: str, path: str) -> str:
        """The A41 notes of the step from ``prev_blob`` as a JSON string; ``[]`` without one."""
        if prev_blob is None or prev_blob not in self.texts:
            return "[]"
        key = (prev_blob, blob, repo, path)
        if key not in self.notes:
            notes = change_notes_for_step(self.texts[prev_blob], self.texts[blob], repo, path)
            self.notes[key] = json.dumps(notes, ensure_ascii=False)
        return self.notes[key]


# Stage 9b, step 4: change notes


def _body_start(lines: list[str]) -> int:
    """Index of the first body line of normalised text lines (after the front matter).

    >>> _body_start(["---", "name: x", "---", "Body"]), _body_start(["Body"])
    (3, 0)
    """
    if not lines or lines[0].strip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index + 1
    return 0


def _numbered_body_lines(text: str) -> list[tuple[int, str]]:
    """``(line number, line)`` of the non-blank body lines of the normalised text.

    The lines are those of ``body_lines``; numbers are 1-based in the whole
    normalised text, front matter included.

    >>> _numbered_body_lines("---\\nname: x\\n---\\nOne\\n\\nTwo")
    [(4, 'One'), (6, 'Two')]
    """
    lines = normalize_text(text).split("\n")
    start = _body_start(lines)
    return [
        (index + 1, line) for index, line in enumerate(lines[start:], start=start) if line.strip()
    ]


def _added_line_positions(old_text: str, numbered: list[tuple[int, str]]) -> set[int]:
    """Positions in ``numbered`` (the new body) of the lines the line diff marks as added."""
    diff = _diff_line_lists(body_lines(old_text), [line for _number, line in numbered])
    added, position = set(), 0
    for operation, _line in diff:
        if operation == "-":
            continue
        if operation == "+":
            added.add(position)
        position += 1
    return added


def _note_kind(line: str, fence: str, names_added: list[str]) -> str | None:
    """The first matching kind of ``NOTE_KINDS`` for one added body line, or None.

    ``fence`` is ``""`` outside a code fence, ``"shell"`` inside a bash/sh/shell
    fence, ``"other"`` inside any other fence. ``names_added`` are the project
    words the step's new text has and its old text lacks (A11), worked out once
    per step: a line is a ``name`` note when it holds one of them. Checking the
    old text again for every added line took hours on one very large file.

    >>> _note_kind("curl https://x", "", [])
    'network'
    >>> _note_kind("## Deploy", "other", [])
    >>> _note_kind("Built for ACME.", "", ["acme"])
    'name'
    """
    stripped = line.strip()
    found = {
        "network": bool(find_network_refs(stripped)),
        "script": bool(find_script_mentions(stripped)),
        "command": fence == "shell" or (fence == "" and stripped.startswith("$ ")),
        "heading": fence == "" and bool(find_headings(stripped)),
        "name": any(_has_word(stripped, word) for word in names_added),
    }
    return next((kind for kind in NOTE_KINDS if found[kind]), None)


def _permission_note(old_text: str, new_text: str) -> dict[str, object] | None:
    """A ``permission`` note when ``allowed-tools`` changed, on the line of that key (else 1)."""
    front = compare_front_matter(old_text, new_text)
    if not front["permissions_changed"]:
        return None
    lines = normalize_text(new_text).split("\n")
    end = _body_start(lines)
    line_number = next(
        (index + 1 for index in range(end) if lines[index].startswith("allowed-tools")), 1
    )
    added = ", ".join(front["allowed_tools_added"]) or "-"
    removed = ", ".join(front["allowed_tools_removed"]) or "-"
    text = f"allowed-tools added: {added}; removed: {removed}"
    return {"line": line_number, "kind": "permission", "text": text}


# Algorithm A41, see docs/skill_genealogy/code_flow/15_copy_changes.md
def change_notes_for_step(old_text: str, new_text: str, repo: str, path: str) -> list[dict]:
    """Notes on the added lines of one step: ``[{line, kind, text}]``, sorted by line.

    A body line the line diff marks as added gets one note, of the first kind
    that matches in this order: ``network`` (a URL, curl or wget; the text
    starts with ``⚠``), ``script`` (a script path), ``command`` (a line in a
    shell fence or starting with ``$ ``), ``heading`` (outside a fence),
    ``name`` (a project word of repo or path that the old body lacks). A
    changed ``allowed-tools`` adds a ``permission`` note. ``line`` is 1-based
    in the normalised new text. Any other added line has no note.

    >>> notes = change_notes_for_step("Keep this line.",
    ...     "Keep this line.\\n## Deploy\\ncurl https://x", "a/r", "SKILL.md")
    >>> [(note["line"], note["kind"], note["text"]) for note in notes]
    [(2, 'heading', '## Deploy'), (3, 'network', '⚠ curl https://x')]
    """
    numbered = _numbered_body_lines(new_text)
    added = _added_line_positions(old_text, numbered)
    names_added = body_names_added(old_text, new_text, repo, path) if added else []
    notes: list[dict[str, object]] = []
    fence = ""
    for position, (line_number, line) in enumerate(numbered):
        updated = _fence_state(line, fence)
        if updated is not None:
            fence = updated
            continue
        if position not in added:
            continue
        kind = _note_kind(line, fence, names_added)
        if kind is not None:
            text = (NETWORK_MARK if kind == "network" else "") + line.strip()
            notes.append({"line": line_number, "kind": kind, "text": text})
    permission = _permission_note(old_text, new_text)
    if permission is not None:
        notes.append(permission)
    return sorted(notes, key=lambda note: note["line"])


# Stage 9b, step 5: one row per scored copy version


def _version_kind(version: Mapping[str, object], lineages_by_blob: Mapping[str, set[str]]) -> str:
    """``copy`` for version 1, ``inherited`` for a registry-history commit, else the A39 label.

    >>> owners = {"v2": {"L"}}
    >>> row = {"version_seq": 2, "inherited": False, "blob": "v2", "lineage_id": "L"}
    >>> _version_kind(row, owners), _version_kind({**row, "version_seq": 1}, owners)
    ('sync', 'copy')
    """
    if int(version["version_seq"]) == 1:
        return "copy"
    if bool(version["inherited"]):
        return "inherited"
    return label_edit(str(version["blob"]), str(version["lineage_id"]), lineages_by_blob)


def _registry_blob_index(lineage_versions: pd.DataFrame) -> dict[tuple[str, int], str]:
    """``(lineage_id, version_seq) -> blob`` of every registry version."""
    return {
        (str(row.lineage_id), int(row.version_seq)): str(row.blob)
        for row in lineage_versions.loc[:, ["lineage_id", "version_seq", "blob"]].itertuples()
    }


def score_copy_versions(
    versions: pd.DataFrame,
    releases: Mapping[str, tuple[list, list, int]],
    registry_blobs: Mapping[tuple[str, int], str],
    lineages_by_blob: Mapping[str, set[str]],
    scorer: PairScorer,
) -> pd.DataFrame:
    """Score every copy version against its four targets (A40) and add its notes (A41).

    ``versions`` comes from ``list_copy_versions`` (rows of a file in order).
    A target whose text is missing has null scores. Outputs: ``COPY_VERSION_COLUMNS``.
    """
    rows = []
    prev_blob: str | None = None
    for version in versions.to_dict("records"):
        if int(version["version_seq"]) == 1:
            prev_blob = None
        lineage_id, blob = str(version["lineage_id"]), str(version["blob"])
        targets = targets_from_index(
            releases, lineage_id, int(version["version_seq"]), version["commit_date"],
            int(version["origin_seq"]),
        )  # fmt: skip
        target_blobs = {
            "prev": prev_blob,
            "origin": registry_blobs.get((lineage_id, targets["origin_seq"])),
            "at_date": registry_blobs.get((lineage_id, targets["at_date_seq"])),
            "newest": registry_blobs.get((lineage_id, targets["newest_seq"])),
        }
        row = {name: version[name] for name in COPY_VERSION_COLUMNS[:9]}
        row["change_kind"] = _version_kind(version, lineages_by_blob)
        row["at_date_seq"], row["newest_seq"] = targets["at_date_seq"], targets["newest_seq"]
        for target in TARGETS:
            scores = scorer.score(target_blobs[target], blob) or {}
            row.update({f"{target}_{name}": scores.get(name) for name in SCORE_FIELDS})
        row["change_notes"] = scorer.change_notes(prev_blob, blob, version["repo"], version["path"])
        rows.append(row)
        prev_blob = blob
    frame = pd.DataFrame(rows, columns=COPY_VERSION_COLUMNS)
    for column in ("origin_seq", "version_seq", "at_date_seq", "newest_seq"):
        frame[column] = frame[column].astype("Int64")
    for target in TARGETS:
        for name in SCORE_FIELDS:
            kind = "Int64" if name.startswith("lines_") else "float64"
            frame[f"{target}_{name}"] = frame[f"{target}_{name}"].astype(kind)
    return frame


# Stage 9b: reading texts and running the batches


def load_dataset_files(copies: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str]]:
    """Query Q9.4: the dataset text and ``last_commit_at`` of these copy files.

    Returns ``repo, path, file_sha, last_commit_at`` (UTC) and ``file_sha -> text``.
    """
    columns = ["repo", "path", "file_sha", "last_commit_at"]
    if copies.empty:
        return pd.DataFrame(columns=columns), {}
    con = connect_duckdb()
    try:
        con.execute(f"SET memory_limit = '{settings.DUCKDB_MEMORY_LIMIT}'")
        wanted = copies.loc[:, ["repo", "path", "file_sha"]].rename(columns={"file_sha": "sha"})
        con.register("wanted_files", wanted.drop_duplicates())
        rows = con.execute(
            DATASET_FILES_SQL.format(path=_quote_path(settings.ARTIFACTS_GLOB))
        ).fetchall()
    finally:
        con.close()
    texts = only_stored_text([(sha, text) for _repo, _path, sha, text, _date in rows])
    dates = pd.DataFrame(
        [(repo, path, sha, date) for repo, path, sha, _text, date in rows], columns=columns
    )
    dates["last_commit_at"] = pd.to_datetime(dates["last_commit_at"], utc=True, errors="coerce")
    return dates, texts


def find_copies_without_history_text(
    dev: bool, copies: pd.DataFrame, histories: pd.DataFrame
) -> pd.Series:
    """True for copies whose history has no blob with a stored text (query Q9.2).

    These copies get the dataset text (query Q9.4) and are batched apart.
    """
    rows = _ordered_history_rows(histories)
    stored = stored_shas(_text_path(dev, "history"), set(rows["blob"]))
    stored_rows = rows.loc[rows["blob"].isin(stored)]
    with_text = set(zip(stored_rows["repo"], stored_rows["path"], strict=True))
    keys = zip(copies["repo"], copies["path"], strict=True)
    return pd.Series([key not in with_text for key in keys], index=copies.index, dtype=bool)


def registry_blobs_needed(
    copies: pd.DataFrame,
    histories: pd.DataFrame,
    releases: Mapping[str, tuple[list, list, int]],
    registry_blobs: Mapping[tuple[str, int], str],
) -> set[str]:
    """Registry blobs the history versions can be scored against: origin, newest, at-date.

    Every history row's date is looked up, so the set covers every version kept
    later. The dataset versions add their at-date blobs per batch.
    """
    wanted: set[str] = set()
    origins = dict(zip(zip(copies["repo"], copies["path"], strict=True), copies["origin_seq"],
                       strict=True))  # fmt: skip
    for lineage_id, origin_seq in zip(copies["lineage_id"], copies["origin_seq"], strict=True):
        newest = releases.get(lineage_id, ([], [], None))[2]
        for seq in (int(origin_seq), newest):
            if (lineage_id, seq) in registry_blobs:
                wanted.add(registry_blobs[(lineage_id, seq)])
    rows = _ordered_history_rows(histories)
    for row in rows.loc[:, ["repo", "path", "lineage_id", "commit_date"]].itertuples(index=False):
        origin_seq = origins.get((row.repo, row.path))
        if origin_seq is None:
            continue
        seq = targets_from_index(releases, row.lineage_id, 1, row.commit_date, origin_seq)
        blob = registry_blobs.get((row.lineage_id, seq["at_date_seq"]))
        if blob is not None:
            wanted.add(blob)
    return wanted


def add_missing_texts(cache: dict[str, str], path: Path, shas: set[str]) -> None:
    """Load into ``cache`` the wanted shas it does not hold yet (query Q9.1)."""
    missing = {sha for sha in shas if sha not in cache}
    cache.update(load_stored_texts(path, missing))


def build_copy_version_similarity(
    dev: bool, inputs: Mapping[str, object] | None = None, progress: Progress | None = None
) -> pd.DataFrame:
    """Stage 9b steps 1-5: list, target and score every copy version.

    The copies are those of stage 9a (``select_copies``). Registry texts are
    loaded once for the history versions; copy texts are read per batch of
    ``COPY_CHANGE_FILE_BATCH`` files. Copies with no stored history text are
    batched apart and read the dataset (Q9.4). Counts are in ``.attrs``.
    """
    inputs = inputs or read_inputs(dev)
    progress = progress or Progress(dev)
    lineage_versions = inputs["versions"]
    # Step 1: the copies and their histories
    copies = select_copies(inputs["copy_files"], inputs["families"], inputs["english"])
    progress.stage("read copy histories", f"{len(copies):,} copies selected")
    histories = read_copy_histories(dev, copies)
    histories = histories.merge(copies.loc[:, ["repo", "path", "lineage_id"]], on=["repo", "path"])
    # Step 2: the target lookups (A40) and the registry texts they need
    releases = index_lineage_releases(lineage_versions)
    registry_blobs = _registry_blob_index(lineage_versions)
    lineages_by_blob = index_lineages_by_blob(lineage_versions)
    registry_texts: dict[str, str] = {}
    wanted = registry_blobs_needed(copies, histories, releases, registry_blobs)
    progress.stage("read registry texts", f"{len(wanted):,} registry versions")
    add_missing_texts(registry_texts, _text_path(dev, "registry"), wanted)
    progress.stage("find copies without a stored history text")
    needs_dataset = find_copies_without_history_text(dev, copies, histories)
    groups = [(copies.loc[~needs_dataset], False), (copies.loc[needs_dataset], True)]
    size = settings.COPY_CHANGE_FILE_BATCH
    batches = sum(math.ceil(len(group) / size) for group, _uses in groups)
    progress.stage(
        "score versions",
        f"{int((~needs_dataset).sum()):,} copies with history texts,"
        f" {int(needs_dataset.sum()):,} read from the dataset; {batches} batches of {size:,}",
    )
    progress.start_compare()
    frames, counts = [], {"no_text": 0, "whitespace_only": 0, "no_version": 0}
    copies_done, versions_found, number = 0, 0, 0
    for group, uses_dataset in groups:
        for batch in batches_of_files(group, size):
            batch_history = histories.merge(batch.loc[:, ["repo", "path"]], on=["repo", "path"])
            history_texts = load_stored_texts(
                _text_path(dev, "history"), set(batch_history["blob"].dropna())
            )
            dates, dataset_texts = load_dataset_files(batch if uses_dataset else batch.iloc[0:0])
            # Step 3: the versions of each copy
            versions = list_copy_versions(
                batch, batch_history, dates, ChainMap(history_texts, dataset_texts)
            )
            for name in counts:
                counts[name] += versions.attrs.get(name, 0)
            if uses_dataset:
                add_missing_texts(
                    registry_texts,
                    _text_path(dev, "registry"),
                    registry_blobs_needed(batch, _as_history(versions), releases, registry_blobs),
                )
            # Steps 4-5: targets (A40), scores and change notes (A41)
            texts = ChainMap(history_texts, dataset_texts, registry_texts)
            scorer = PairScorer(texts)
            frames.append(
                score_copy_versions(versions, releases, registry_blobs, lineages_by_blob, scorer)
            )
            number += 1
            copies_done += len(batch)
            versions_found += len(frames[-1])
            progress.batch(
                number, batches, copies_done, len(copies), versions_found,
                "dataset" if uses_dataset else "history", found_label="versions",
            )  # fmt: skip
    frame = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=COPY_VERSION_COLUMNS)
    )
    frame.attrs.update(
        {
            "copies_selected": len(copies),
            "copies_with_versions": int(frame.drop_duplicates(["repo", "path"]).shape[0]),
            "copies_left_out_no_text": counts["no_version"],
            "history_rows_left_out_no_text": counts["no_text"],
            "history_rows_left_out_whitespace_only": counts["whitespace_only"],
        }
    )
    return frame


def _as_history(versions: pd.DataFrame) -> pd.DataFrame:
    """Dataset version rows in the shape ``registry_blobs_needed`` reads (one dated row each)."""
    rows = versions.loc[:, ["repo", "path", "lineage_id", "commit_date", "blob"]].copy()
    rows["status"], rows["commit_seq"] = "A", 0
    return rows


# Stage 9b: summary


def drift_of_origin_jaccard(versions: pd.DataFrame) -> dict[str, float | None]:
    """Median origin Jaccard at the first and the last version of copies with two or more.

    Drift is last minus first; below 0 means later texts share less with the origin.

    >>> rows = pd.DataFrame({"repo": ["a"] * 2 + ["b"], "path": ["p"] * 3,
    ...     "version_seq": [1, 2, 1], "origin_jaccard": [1.0, 0.6, 0.9]})
    >>> drift_of_origin_jaccard(rows)
    {'median_origin_jaccard_first': 1.0, 'median_origin_jaccard_last': 0.6, 'drift': -0.4}
    """
    ordered = versions.sort_values(["repo", "path", "version_seq"])
    sizes = ordered.groupby(["repo", "path"])["version_seq"].transform("size")
    multi = ordered.loc[sizes > 1]
    if multi.empty:
        return {"median_origin_jaccard_first": None, "median_origin_jaccard_last": None,
                "drift": None}  # fmt: skip
    # drop_duplicates keeps a null score of the first or last version (rule 3);
    # groupby().first() would skip to the next non-null one.
    keys = ["repo", "path"]
    first = float(multi.drop_duplicates(keys, keep="first")["origin_jaccard"].median())
    last = float(multi.drop_duplicates(keys, keep="last")["origin_jaccard"].median())
    return {
        "median_origin_jaccard_first": round(first, 4),
        "median_origin_jaccard_last": round(last, 4),
        "drift": round(last - first, 4),
    }


def share_behind_at_date(
    versions: pd.DataFrame, registry_blobs: Mapping[tuple[str, int], str]
) -> float | None:
    """Share of versions with an at-date target whose registry had moved past the origin.

    A version is behind when the at-date version is newer than the origin
    (``at_date_seq > origin_seq``) and the copy's text is not that version's
    text. Versions without an at-date target are left out. Stage 10 measures
    staleness properly; this is the summary number of the card.

    >>> rows = pd.DataFrame({"lineage_id": ["L"] * 3, "origin_seq": [1] * 3,
    ...     "at_date_seq": [1, 2, 2], "blob": ["a", "a", "b"]})
    >>> share_behind_at_date(rows, {("L", 1): "a", ("L", 2): "b"})
    0.3333
    """
    dated = versions.loc[versions["at_date_seq"].notna()]
    if dated.empty:
        return None
    at_blob = [
        registry_blobs.get((lineage, int(seq)))
        for lineage, seq in zip(dated["lineage_id"], dated["at_date_seq"], strict=True)
    ]
    moved = dated["at_date_seq"].astype(int) > dated["origin_seq"].astype(int)
    behind = moved & (dated["blob"] != pd.Series(at_blob, index=dated.index))
    return round(float(behind.mean()), 4)


def note_kind_counts(versions: pd.DataFrame) -> dict[str, int]:
    """How many notes of each kind the change notes hold.

    >>> note = '[{"line": 2, "kind": "heading", "text": "# A"}]'
    >>> rows = pd.DataFrame({"change_notes": [note, "[]"]})
    >>> note_kind_counts(rows)["heading"]
    1
    """
    counts = dict.fromkeys((*NOTE_KINDS, "permission"), 0)
    for cell in versions["change_notes"]:
        for note in json.loads(cell):
            counts[note["kind"]] += 1
    return counts


def print_version_summary(
    versions: pd.DataFrame, lineage_versions: pd.DataFrame, seconds: float
) -> None:
    """Print the stage 9b counts, the drift and two sample records."""
    registry_blobs = _registry_blob_index(lineage_versions)
    multi = versions.groupby(["repo", "path"]).size()
    print_summary(
        "Stage 9b copy version similarity",
        {
            **versions.attrs,
            "copy_versions_scored": len(versions),
            "copies_with_more_than_one_version": int((multi > 1).sum()),
            "basis": versions["basis"].value_counts().to_dict(),
            "change_kind": versions["change_kind"].value_counts().to_dict(),
            **drift_of_origin_jaccard(versions),
            "share_behind_at_date": share_behind_at_date(versions, registry_blobs),
            "versions_without_at_date": int(versions["at_date_seq"].isna().sum()),
            "versions_with_notes": int((versions["change_notes"] != "[]").sum()),
            "note_kinds": note_kind_counts(versions),
            "seconds": round(seconds, 1),
        },
    )
    print_sample_records(versions, "copy_version_similarity.parquet, 2 sample records")


# Outputs


def save_outputs(
    copy_changes: pd.DataFrame, history_edits: pd.DataFrame, profile: pd.DataFrame, dev: bool
) -> None:
    """Write the three parquet files, sorted so a rerun gives the same files."""
    paths = output_paths(dev)
    parts = [
        (copy_changes, paths["copy_changes"], ["repo", "path"]),
        (history_edits, paths["copy_history_edits"], ["repo", "path", "commit_date", "commit"]),
        (profile, paths["change_profile"], None),
    ]
    temps = [path.with_name(path.name + ".tmp") for _frame, path, _sort in parts]
    try:
        for (frame, _path, sort_by), temp in zip(parts, temps, strict=True):
            write_parquet(frame, temp, sort_by)
        for (_frame, path, _sort), temp in zip(parts, temps, strict=True):
            temp.replace(path)
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)


def _state_shares(frame: pd.DataFrame) -> dict[str, float]:
    """Share of rows that are unchanged, cosmetic and substantive."""
    return {
        f"share_{state}": round(float((frame["state"] == state).mean()), 4)
        if len(frame)
        else float("nan")
        for state in ("unchanged", "cosmetic", "substantive")
    }


def shares_by_link(copy_changes: pd.DataFrame) -> pd.DataFrame:
    """Copies, substantive share and customised share for each link kind of stage 7.

    A ``cluster`` link is a loose text match, so its copies differ from the origin
    far more than an ``exact`` or ``near`` link. Reading the totals without this
    split mixes the two.

    >>> rows = pd.DataFrame({"link": ["exact", "cluster", "cluster"],
    ...     "state": ["unchanged", "substantive", "cosmetic"], "customized": [False, True, False]})
    >>> float(shares_by_link(rows).set_index("link").loc["cluster", "share_substantive"])
    0.5
    """
    grouped = copy_changes.groupby("link")
    return pd.DataFrame(
        {
            "copies": grouped.size(),
            "share_substantive": grouped["state"].apply(
                lambda s: float((s == "substantive").mean())
            ),
            "share_customized": grouped["customized"].apply(lambda s: float(s.astype(bool).mean())),
        }
    ).reset_index()


def print_run_summary(
    copy_changes: pd.DataFrame,
    history_edits: pd.DataFrame,
    profile: pd.DataFrame,
    counts: dict[str, int],
    seconds: float,
) -> None:
    """Print the counts, the RQ2 shares and two sample records of each output."""
    kinds = history_edits["change_kind"].value_counts() if len(history_edits) else {}
    overlap = copy_changes["heading_overlap"].dropna()
    print_summary(
        "Stage 9a copy against origin",
        {
            **counts,
            "current_basis": copy_changes["current_basis"].value_counts().to_dict(),
            "state_shares_of_files": _state_shares(copy_changes),
            "customized": int(copy_changes["customized"].sum()),
            "customized_share": round(float(copy_changes["customized"].mean()), 4)
            if len(copy_changes)
            else None,
            "names_added": int(
                copy_changes["names_added"].map(lambda cell: len(_as_list(cell)) > 0).sum()
            ),
            "headings_added": int(
                copy_changes["headings_added"].map(lambda cell: len(_as_list(cell)) > 0).sum()
            ),
            "commands_added": int(
                copy_changes["commands_added"].map(lambda cell: len(_as_list(cell)) > 0).sum()
            ),
            "scripts_added": int(
                copy_changes["scripts_mentioned_added"]
                .map(lambda cell: len(_as_list(cell)) > 0)
                .sum()
            ),
            "heading_overlap_mean": round(float(overlap.mean()), 4) if len(overlap) else None,
            "heading_overlap_rows": len(overlap),
            "permissions_changed": int(copy_changes["permissions_changed"].sum()),
            "history_edits": {kind: int(kinds.get(kind, 0)) for kind in EDIT_KINDS},
            "seconds": round(seconds, 1),
        },
    )
    print(profile.round(4).to_string(index=False))
    print("by link kind")
    print(shares_by_link(copy_changes).round(4).to_string(index=False))
    print_sample_records(copy_changes, "copy_changes.parquet, 2 sample records")
    print_sample_records(history_edits, "copy_history_edits.parquet, 2 sample records")
    print_sample_records(profile, "change_profile.parquet, 2 sample records")


def save_version_outputs(versions: pd.DataFrame, dev: bool) -> None:
    """Write ``copy_version_similarity.parquet`` under a temporary name, then replace the file."""
    path = version_output_path(dev)
    temp = path.with_name(path.name + ".tmp")
    try:
        write_parquet(versions, temp, ["repo", "path", "version_seq"])
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def run_origin(dev: bool, force: bool) -> None:
    """Stage 9a: skip when the three outputs exist (unless ``force``), else build, save, print.

    With ``force`` the old files stay until the new build has finished, so a failed run
    keeps the previous outputs.
    """
    outputs = list(output_paths(dev).values())
    if outputs_exist(outputs) and not force:
        print("skip: stage 9a outputs exist (use --force to rebuild)", flush=True)
        return
    started = time.perf_counter()
    progress = Progress(dev)
    progress.stage("read inputs", "dev" if dev else "full")
    inputs = read_inputs(dev)
    copy_changes, history_edits, profile, counts = build_copy_measures(dev, inputs, progress)
    # Step 6: save and print
    progress.stage("save outputs")
    save_outputs(copy_changes, history_edits, profile, dev)
    progress.finish()
    print_run_summary(copy_changes, history_edits, profile, counts, time.perf_counter() - started)


def run_versions(dev: bool, force: bool) -> None:
    """Stage 9b: skip when the output exists (unless ``force``), else build, save, print.

    With ``force`` the old file stays until the new build has finished.
    """
    if outputs_exist([version_output_path(dev)]) and not force:
        print("skip: stage 9b output exists (use --force to rebuild)", flush=True)
        return
    started = time.perf_counter()
    progress = Progress(dev)
    progress.stage("read inputs", "dev" if dev else "full")
    inputs = read_inputs(dev)
    versions = build_copy_version_similarity(dev, inputs, progress)
    # Step 6: save and print
    progress.stage("save outputs")
    save_version_outputs(versions, dev)
    progress.finish()
    print_version_summary(versions, inputs["versions"], time.perf_counter() - started)


# Stage 9c: copy folder changes and risky scripts (card G21)

# One output row per file that differs between a copy version's folder and a
# target folder. Files that are the same (same path, sha and mode) are not
# stored; the summary still counts every compared pair.
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
FOLDER_SUMMARY_COLUMNS = [
    "repo",
    "path",
    "version_seq",
    "target",
    "target_package_seq",
    "files_added",
    "files_removed",
    "files_modified",
    "files_renamed",
    "scripts_added",
    "scripts_modified",
    "new_risky_scripts",
    "missing_scripts",
    "hidden_scripts",
    "folder_source",
]
# Where a copy folder comes from: the stage 6 history, or the dataset's
# artifact_siblings listing when the copy version has no history (basis dataset).
FOLDER_SOURCES = ("history", "dataset_snapshot")
# compare_folders results that change a script's text, so its flags are read.
_SCRIPT_TEXT_RESULTS = ("added", "modified", "renamed")
# One manifest file: path, sha, mode (None in a dataset snapshot), size (None
# when no text is stored).
ManifestFile = tuple[str, str, str | None, int | None]


# Query Q9.5, see docs/skill_genealogy/code_flow/15_copy_changes.md
# Every own and former-folder change row (SKILL.md and folder files) of the
# copy folders of one batch. copy_source rows belong to the source repo.
COPY_FOLDER_ROWS_SQL = """
SELECT changes.repo, changes.skill_folder, changes.file_path, changes."commit",
       changes.commit_date, changes.commit_seq, changes.status, changes.new_sha,
       changes.new_mode, changes.repo_path, changes.old_repo_path, changes.history_role
FROM read_parquet({path}) AS changes
SEMI JOIN wanted_folders
  ON changes.repo = wanted_folders.repo AND changes.skill_folder = wanted_folders.skill_folder
WHERE changes.history_role IN ('own', 'former_folder')
"""

# Query Q9.6, see docs/skill_genealogy/code_flow/15_copy_changes.md
# Byte size of every stored copied-skill text (stage 6 stores scripts, SKILL.md and
# small text files; other files get no size).
STORED_SIZES_SQL = """
SELECT texts.sha AS sha, max(strlen(texts.text)) AS size
FROM read_parquet({path}) AS texts
WHERE texts.text IS NOT NULL
GROUP BY texts.sha
"""

# Query Q9.7, see docs/skill_genealogy/code_flow/15_copy_changes.md
# The dataset's file listing of each wanted SKILL.md folder (copies with no history).
SIBLING_FILES_SQL = """
SELECT siblings.repo_full_name AS repo, siblings.artifact_path AS path,
       siblings.entry_name AS file_path, siblings.entry_sha AS sha, siblings.entry_size AS size
FROM read_parquet({path}) AS siblings
SEMI JOIN wanted_files
  ON siblings.repo_full_name = wanted_files.repo AND siblings.artifact_path = wanted_files.path
WHERE siblings.entry_type = 'file'
"""

# Query Q9.8, see docs/skill_genealogy/code_flow/15_copy_changes.md
# The files of the wanted registry package versions (stage 4b manifests).
REGISTRY_MANIFEST_SQL = """
SELECT manifests.lineage_id, manifests.package_version_seq, manifests.path, manifests.sha,
       manifests.mode, manifests.size
FROM read_parquet({path}) AS manifests
SEMI JOIN wanted_packages
  ON manifests.lineage_id = wanted_packages.lineage_id
 AND manifests.package_version_seq = wanted_packages.package_version_seq
"""

# Query Q9.10, see docs/skill_genealogy/code_flow/15_copy_changes.md
# Which wanted SKILL.md files had their folder listed by GitSkills. Without a
# listing the folder is unknown, not empty (artifact_siblings covers only these).
LISTED_FOLDERS_SQL = """
SELECT DISTINCT artifacts.repo_full_name AS repo, artifacts.path AS path
FROM read_parquet({path}) AS artifacts
SEMI JOIN wanted_files
  ON artifacts.repo_full_name = wanted_files.repo AND artifacts.path = wanted_files.path
WHERE artifacts.composition_fetched = 1
"""

# Query Q9.9, see docs/skill_genealogy/code_flow/15_copy_changes.md
# Script text from the dataset's file listing, for snapshot files with no history text.
SIBLING_TEXTS_SQL = """
SELECT siblings.entry_sha AS sha, any_value(siblings.content) AS text
FROM read_parquet({path}) AS siblings
SEMI JOIN wanted_shas ON siblings.entry_sha = wanted_shas.sha
WHERE siblings.content IS NOT NULL
GROUP BY siblings.entry_sha
"""


def folder_output_paths(dev: bool) -> dict[str, Path]:
    """The two stage 9c output files of this mode."""
    folder = settings.data_dir(dev)
    return {
        "folder_changes": folder / "copy_folder_changes.parquet",
        "folder_summary": folder / "copy_version_folder_summary.parquet",
    }


# Stage 9c, step 1: the commit of each copy version


def version_commits(versions: pd.DataFrame, histories: pd.DataFrame) -> dict[tuple, str]:
    """``(repo, path, version_seq) -> commit`` for the history versions of stage 9b.

    Stage 9b keeps a version's blob and date, not its commit. The history rows
    are walked in the A39 order next to the versions of the same file; a version
    takes the first row at or after the previous match with the same blob and date.
    So a text that comes back (A -> B -> A) gets the later commit the second time.

    >>> history = pd.DataFrame({"repo": ["r"] * 3, "path": ["SKILL.md"] * 3,
    ...     "blob": ["a", "b", "a"], "status": ["A", "M", "M"], "commit": ["c1", "c2", "c3"],
    ...     "commit_seq": [1, 2, 3],
    ...     "commit_date": pd.to_datetime(["2026-01-01"] * 3, utc=True)})
    >>> versions = pd.DataFrame({"repo": ["r"] * 3, "path": ["SKILL.md"] * 3,
    ...     "basis": ["history"] * 3, "version_seq": [1, 2, 3], "blob": ["a", "b", "a"],
    ...     "commit_date": pd.to_datetime(["2026-01-01"] * 3, utc=True)})
    >>> list(version_commits(versions, history).values())
    ['c1', 'c2', 'c3']
    """
    by_file: dict[tuple[str, str], list[tuple[str, object, str]]] = defaultdict(list)
    rows = _ordered_history_rows(histories)
    for repo, path, blob, date, commit in zip(
        rows["repo"], rows["path"], rows["blob"], rows["commit_date"], rows["commit"], strict=True
    ):
        by_file[(repo, path)].append((str(blob), date, str(commit)))
    found: dict[tuple, str] = {}
    history_versions = versions.loc[versions["basis"] == "history"].sort_values(
        ["repo", "path", "version_seq"], kind="mergesort"
    )
    position: dict[tuple[str, str], int] = {}
    for repo, path, seq, blob, date in zip(
        history_versions["repo"], history_versions["path"], history_versions["version_seq"],
        history_versions["blob"], history_versions["commit_date"], strict=True,
    ):  # fmt: skip
        file_rows = by_file.get((repo, path), [])
        start = position.get((repo, path), 0)
        for index in range(start, len(file_rows)):
            row_blob, row_date, commit = file_rows[index]
            if row_blob == str(blob) and _same_time(row_date, date):
                found[(repo, path, int(seq))] = commit
                position[(repo, path)] = index + 1
                break
    return found


def _same_time(left: object, right: object) -> bool:
    """Whether two dates are the same instant; two missing dates count as the same."""
    if pd.isna(left) or pd.isna(right):
        return bool(pd.isna(left) and pd.isna(right))
    return pd.Timestamp(left) == pd.Timestamp(right)


# Stage 9c, step 2: the copy folder of each version


# Algorithm A42, see docs/skill_genealogy/code_flow/15_copy_changes.md
def snapshot_copy_folders(
    folder_rows: pd.DataFrame, wanted: Mapping[str, set[str]]
) -> tuple[dict[tuple[str, str], tuple], dict[str, tuple]]:
    """Replay each copy folder's history (the A21 walk) and keep the folder at chosen commits.

    ``folder_rows`` are Query Q9.5 rows (``repo, skill_folder`` and the A21
    columns). A folder is keyed ``repo::skill_folder``. The rows are applied in
    the A21 order with the A21 rules: ``A``/``M``/``T`` set a path, ``D`` drops
    it, ``R`` moves it, the skill folder follows its ``SKILL.md`` and the
    manifest leaves out ``SKILL.md`` and vendored folders. ``wanted`` maps a
    folder key to the commits whose folder is kept (the commits of its
    versions). Returns the folder after each wanted commit,
    ``{(key, commit): files}``, and the folder after the folder's newest commit,
    ``{key: files}``, kept only while its ``SKILL.md`` is still there.
    ``files`` is a sorted tuple of ``(path, sha, mode)``.

    >>> rows = pd.DataFrame({"repo": ["r"] * 3, "skill_folder": ["s"] * 3,
    ...     "file_path": ["SKILL.md", "a.py", "b.sh"], "commit": ["c1", "c1", "c2"],
    ...     "commit_date": ["2026-01-01T00:00:00Z"] * 2 + ["2026-02-01T00:00:00Z"],
    ...     "commit_seq": [1, 1, 2], "status": ["A", "A", "A"], "new_sha": ["t", "p", "q"],
    ...     "new_mode": ["100644"] * 3, "repo_path": ["s/SKILL.md", "s/a.py", "s/b.sh"],
    ...     "old_repo_path": [None] * 3, "history_role": ["own"] * 3})
    >>> at_commit, newest = snapshot_copy_folders(rows, {"r::s": {"c1"}})
    >>> at_commit[("r::s", "c1")], newest["r::s"]
    ((('a.py', 'p', '100644'),), (('a.py', 'p', '100644'), ('b.sh', 'q', '100644')))
    """
    at_commit: dict[tuple[str, str], tuple] = {}
    newest: dict[str, tuple] = {}
    if folder_rows.empty:
        return at_commit, newest
    ordered = _order_for_replay(folder_rows)
    for key, group in ordered.groupby("lineage_id", sort=False):
        key = str(key)
        commits = wanted.get(key, set())
        state: dict[str, tuple[str, str]] = {}
        folder: str | None = None
        for commit, commit_rows in itertools.groupby(
            group.itertuples(index=False), key=lambda row: row.commit
        ):
            commit_rows = list(commit_rows)
            for row in commit_rows:
                _apply_file_change(state, row)
            folder = skill_folder_after_commit(commit_rows, folder)
            if folder is not None and str(commit) in commits:
                at_commit[(key, str(commit))] = folder_manifest(state, folder)[0]
        if folder is not None:
            files, present = folder_manifest(state, folder)
            if present:
                newest[key] = files
    return at_commit, newest


def _with_sizes(files: tuple, sizes: Mapping[str, int]) -> tuple[ManifestFile, ...]:
    """Add the stored text size to each ``(path, sha, mode)`` file."""
    return tuple((path, sha, mode, sizes.get(sha)) for path, sha, mode in files)


def history_folder_manifests(
    versions: pd.DataFrame,
    commits: Mapping[tuple, str],
    at_commit: Mapping[tuple[str, str], tuple],
    newest: Mapping[str, tuple],
    sizes: Mapping[str, int],
) -> dict[tuple, tuple[ManifestFile, ...]]:
    """The folder of each history version: the folder after its commit (A42).

    The last version of a copy takes the folder after the folder's newest commit
    instead, so files added after the last ``SKILL.md`` edit are not lost. A
    version whose commit was not found has no folder.

    >>> versions = pd.DataFrame({"repo": ["r", "r"], "path": ["s/SKILL.md"] * 2,
    ...     "basis": ["history"] * 2, "version_seq": [1, 2]})
    >>> commits = {("r", "s/SKILL.md", 1): "c1", ("r", "s/SKILL.md", 2): "c2"}
    >>> at = {("r::s", "c1"): (), ("r::s", "c2"): (("a.py", "p", "100644"),)}
    >>> newest = {"r::s": (("a.py", "p", "100644"), ("b.sh", "q", "100755"))}
    >>> folders = history_folder_manifests(versions, commits, at, newest, {"p": 3})
    >>> folders[("r", "s/SKILL.md", 1)], [f[0] for f in folders[("r", "s/SKILL.md", 2)]]
    ((), ['a.py', 'b.sh'])
    """
    history = versions.loc[versions["basis"] == "history"]
    last_seq = history.groupby(["repo", "path"])["version_seq"].max().to_dict()
    folders: dict[tuple, tuple[ManifestFile, ...]] = {}
    for repo, path, seq in zip(history["repo"], history["path"], history["version_seq"],
                               strict=True):  # fmt: skip
        key = f"{repo}::{skill_folder_of(path)}"
        commit = commits.get((repo, path, int(seq)))
        if commit is None or (key, commit) not in at_commit:
            continue
        files = at_commit[(key, commit)]
        if int(seq) == int(last_seq[(repo, path)]) and key in newest:
            files = newest[key]
        folders[(repo, path, int(seq))] = _with_sizes(files, sizes)
    return folders


def snapshot_folder_manifests(
    versions: pd.DataFrame, sibling_files: pd.DataFrame, listed: set[tuple[str, str]]
) -> dict[tuple, tuple[ManifestFile, ...]]:
    """The folder of each dataset version: the dataset's file listing (``artifact_siblings``).

    ``sibling_files`` has ``repo, path, file_path, sha, size`` (Query Q9.7).
    ``listed`` holds the ``(repo, path)`` whose folder GitSkills listed
    (``composition_fetched``, Query Q9.10). A listed folder with no sibling row
    holds only its ``SKILL.md``; a folder that was never listed is unknown and
    gets no folder, so its registry files are not read as removed.
    The listing has no file mode. Paths are relative to the ``SKILL.md`` folder;
    the package rule of A21 (``is_package_path``) applies, so a repo-root skill
    keeps only ``scripts/``, ``references/`` and ``assets/``.

    >>> versions = pd.DataFrame({"repo": ["r"], "path": ["s/SKILL.md"],
    ...     "basis": ["dataset"], "version_seq": [1]})
    >>> siblings = pd.DataFrame({"repo": ["r"] * 3, "path": ["s/SKILL.md"] * 3,
    ...     "file_path": ["SKILL.md", "scripts/a.py", "node_modules/x.js"],
    ...     "sha": ["t", "abc", "n"], "size": [9, 4, 2]})
    >>> snapshot_folder_manifests(versions, siblings, {("r", "s/SKILL.md")})[("r", "s/SKILL.md", 1)]
    (('scripts/a.py', 'abc', None, 4),)
    >>> snapshot_folder_manifests(versions, siblings, set())
    {}
    """
    listing: dict[tuple[str, str], list[ManifestFile]] = defaultdict(list)
    for repo, path, file_path, sha, size in zip(
        sibling_files["repo"], sibling_files["path"], sibling_files["file_path"],
        sibling_files["sha"], sibling_files["size"], strict=True,
    ):  # fmt: skip
        relative = str(file_path or "").strip("/")
        if relative.lower() == SKILL_MD_NAME or not is_package_path(
            relative, skill_folder_of(path)
        ):
            continue
        size_value = None if pd.isna(size) else int(size)
        listing[(repo, path)].append((relative, str(sha), None, size_value))
    dataset = versions.loc[versions["basis"] == "dataset"]
    return {
        (repo, path, int(seq)): tuple(sorted(listing.get((repo, path), [])))
        for repo, path, seq in zip(
            dataset["repo"], dataset["path"], dataset["version_seq"], strict=True
        )  # fmt: skip
        if (repo, path) in listed
    }


# Stage 9c, step 3: the registry package of each target


def index_packages(packages: pd.DataFrame) -> dict[str, list[tuple[int, int, int]]]:
    """Per lineage: ``(commit date in ns, package_version_seq, version_seq)``, oldest first.

    Sorted by date, then package number, so the last of several packages with
    the same date is the higher number. A package without a date sorts first.

    >>> packages = pd.DataFrame({"lineage_id": ["L", "L"], "package_version_seq": [2, 1],
    ...     "version_seq": [1, 1],
    ...     "commit_date": pd.to_datetime(["2026-02-01", "2026-01-01"], utc=True)})
    >>> [seq for _date, seq, _text in index_packages(packages)["L"]]
    [1, 2]
    """
    frame = packages.loc[:, ["lineage_id", "commit_date", "package_version_seq", "version_seq"]]
    dates = pd.to_datetime(frame["commit_date"], utc=True).dt.as_unit("ns")
    frame = frame.assign(date_ns=dates.astype("int64").where(dates.notna(), -(2**62)))
    frame = frame.sort_values(["lineage_id", "date_ns", "package_version_seq"], kind="mergesort")
    index: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for lineage_id, date_ns, package_seq, text_seq in zip(
        frame["lineage_id"], frame["date_ns"], frame["package_version_seq"],
        frame["version_seq"], strict=True,
    ):  # fmt: skip
        index[str(lineage_id)].append((int(date_ns), int(package_seq), int(text_seq)))
    return dict(index)


# Algorithm A43, see docs/skill_genealogy/code_flow/15_copy_changes.md
def package_at_date(
    index: Mapping[str, list[tuple[int, int, int]]], lineage_id: str, date: object
) -> int | None:
    """The package version current at a date: the newest one committed on or before it.

    None when the date is missing or older than every package version (the
    package did not exist yet).

    >>> packages = pd.DataFrame({"lineage_id": ["L"] * 3, "package_version_seq": [1, 2, 3],
    ...     "version_seq": [1, 1, 2],
    ...     "commit_date": pd.to_datetime(["2026-01-10", "2026-03-01", "2026-05-15"], utc=True)})
    >>> index = index_packages(packages)
    >>> package_at_date(index, "L", pd.Timestamp("2026-04-20", tz="UTC"))
    2
    >>> package_at_date(index, "L", pd.Timestamp("2026-01-01", tz="UTC")) is None
    True
    """
    rows = index.get(lineage_id, [])
    if pd.isna(date) or not rows:
        return None
    position = bisect.bisect_right([row[0] for row in rows], pd.Timestamp(date).value)
    return rows[position - 1][1] if position else None


def origin_package(
    index: Mapping[str, list[tuple[int, int, int]]],
    lineage_id: str,
    origin_seq: int,
    copy_date: object,
) -> int | None:
    """The package version a copy started from (A43).

    Among the package versions whose ``SKILL.md`` is the origin text version,
    the newest one committed on or before the copy's first version. When the
    copy date is missing or older than all of them, the first one. A registry
    can add a script after a release without editing ``SKILL.md``; a copy made
    after that got the script, so it is not counted as added by the copy.

    >>> packages = pd.DataFrame({"lineage_id": ["L"] * 3, "package_version_seq": [1, 2, 3],
    ...     "version_seq": [1, 1, 2],
    ...     "commit_date": pd.to_datetime(["2026-01-10", "2026-03-01", "2026-05-15"], utc=True)})
    >>> index = index_packages(packages)
    >>> origin_package(index, "L", 1, pd.Timestamp("2026-06-01", tz="UTC"))
    2
    >>> origin_package(index, "L", 1, None)
    1
    """
    rows = [row for row in index.get(lineage_id, []) if row[2] == int(origin_seq)]
    if not rows:
        return None
    if not pd.isna(copy_date):
        moment = pd.Timestamp(copy_date).value
        earlier = [row for row in rows if row[0] <= moment]
        if earlier:
            return earlier[-1][1]
    return rows[0][1]


def registry_manifest_for(
    lineage_id: str, release_date: object, package_versions: pd.DataFrame, manifests: pd.DataFrame
) -> list[dict]:
    """The files of the lineage's package version current at a date (plain form of A43).

    ``package_versions`` and ``manifests`` are the stage 4b tables. Returns
    ``compare_folders`` rows (``path, sha, mode, size``); empty when no package
    version existed at that date.

    >>> packages = pd.DataFrame({"lineage_id": ["L", "L"], "package_version_seq": [1, 2],
    ...     "version_seq": [1, 2],
    ...     "commit_date": pd.to_datetime(["2026-01-01", "2026-03-01"], utc=True)})
    >>> files = pd.DataFrame({"lineage_id": ["L", "L"], "package_version_seq": [1, 2],
    ...     "path": ["a.py", "b.py"], "sha": ["x", "y"], "mode": ["100644"] * 2, "size": [1, 2]})
    >>> [row["path"] for row in registry_manifest_for("L", pd.Timestamp("2026-02-01", tz="UTC"),
    ...                                               packages, files)]
    ['a.py']
    """
    seq = package_at_date(index_packages(package_versions), lineage_id, release_date)
    if seq is None:
        return []
    rows = manifests.loc[
        (manifests["lineage_id"] == lineage_id) & (manifests["package_version_seq"] == seq)
    ]
    return _as_manifest(_manifest_tuples(rows))


def _manifest_tuples(rows: pd.DataFrame) -> tuple[ManifestFile, ...]:
    """Manifest rows (``path, sha, mode, size``) as a sorted tuple of files."""
    files = [
        (str(path), str(sha), None if pd.isna(mode) else str(mode),
         None if pd.isna(size) else int(size))
        for path, sha, mode, size in zip(rows["path"], rows["sha"], rows["mode"], rows["size"],
                                         strict=True)
    ]  # fmt: skip
    return tuple(sorted(files))


def _as_manifest(files: tuple[ManifestFile, ...]) -> list[dict]:
    """The ``compare_folders`` form of a tuple of files."""
    return [{"path": path, "sha": sha, "mode": mode, "size": size}
            for path, sha, mode, size in files]  # fmt: skip


def pick_folder_targets(
    version: Mapping[str, object],
    first_date: object,
    index: Mapping[str, list[tuple[int, int, int]]],
) -> dict[str, int | None]:
    """The registry package version of each registry target of one copy version.

    ``origin``: ``origin_package`` at the copy's first version date.
    ``at_date``: the package current at this version's date. ``newest``: the
    lineage's last package. ``prev`` is the copy's own previous folder, not a package.

    >>> packages = pd.DataFrame({"lineage_id": ["L"] * 2, "package_version_seq": [1, 2],
    ...     "version_seq": [1, 2],
    ...     "commit_date": pd.to_datetime(["2026-01-01", "2026-03-01"], utc=True)})
    >>> version = {"lineage_id": "L", "origin_seq": 1,
    ...     "commit_date": pd.Timestamp("2026-02-01", tz="UTC")}
    >>> pick_folder_targets(version, version["commit_date"], index_packages(packages))
    {'origin': 1, 'at_date': 1, 'newest': 2}
    """
    lineage_id = str(version["lineage_id"])
    rows = index.get(lineage_id, [])
    return {
        "origin": origin_package(index, lineage_id, int(version["origin_seq"]), first_date),
        "at_date": package_at_date(index, lineage_id, version["commit_date"]),
        "newest": rows[-1][1] if rows else None,
    }


# Stage 9c, step 4: compare the folders


def compare_copy_folders(
    copy_versions: pd.DataFrame,
    copy_manifests: Mapping[tuple, tuple[ManifestFile, ...]],
    package_index: Mapping[str, list[tuple[int, int, int]]],
    registry_manifests: Mapping[tuple[str, int], tuple[ManifestFile, ...]],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compare every copy version's folder with its four targets (A15 per pair).

    ``copy_versions`` are stage 9b rows of whole files in version order;
    ``copy_manifests`` maps ``(repo, path, version_seq)`` to the folder (A42).
    ``prev`` is the copy's previous folder; the registry targets come from
    ``pick_folder_targets``. A target with no folder (version 1 for ``prev``,
    no package at the date) is not compared. Returns the differing file rows
    (``FOLDER_CHANGE_COLUMNS`` without flags) and one row per compared pair
    (``repo, path, version_seq, target, target_package_seq``). Pairs with the
    same two folders are compared once.

    >>> versions = pd.DataFrame({"repo": ["r"], "path": ["s/SKILL.md"], "lineage_id": ["L"],
    ...     "origin_seq": [1], "version_seq": [1],
    ...     "commit_date": pd.to_datetime(["2026-02-01"], utc=True)})
    >>> packages = pd.DataFrame({"lineage_id": ["L"], "package_version_seq": [1],
    ...     "version_seq": [1], "commit_date": pd.to_datetime(["2026-01-01"], utc=True)})
    >>> registry = {("L", 1): (("a.py", "x", "100644", 1),)}
    >>> copy = {("r", "s/SKILL.md", 1): (("a.py", "y", "100644", 5),)}
    >>> rows, pairs = compare_copy_folders(versions, copy, index_packages(packages), registry)
    >>> rows[["target", "file_path", "result"]].values.tolist()[0], len(pairs)
    (['origin', 'a.py', 'modified'], 3)
    """
    rows: list[dict] = []
    pairs: list[dict] = []
    memo: dict[tuple, list[dict]] = {}
    previous: tuple[ManifestFile, ...] | None = None
    first_date: object = None
    for version in copy_versions.to_dict("records"):
        key = (version["repo"], version["path"], int(version["version_seq"]))
        if key[2] == 1:
            previous, first_date = None, version["commit_date"]
        files = copy_manifests.get(key)
        if files is None:
            previous = None
            continue
        targets: dict[str, tuple[object, tuple | None]] = {"prev": (None, previous)}
        for target, seq in pick_folder_targets(version, first_date, package_index).items():
            target_files = registry_manifests.get((str(version["lineage_id"]), seq))
            targets[target] = (seq, target_files if seq is not None else None)
        for target in TARGETS:
            seq, old_files = targets[target]
            if old_files is None:
                continue
            pairs.append({"repo": key[0], "path": key[1], "version_seq": key[2],
                          "target": target, "target_package_seq": seq})  # fmt: skip
            for row in _differing_files(old_files, files, memo):
                rows.append({"repo": key[0], "path": key[1], "version_seq": key[2],
                             "target": target, **row})  # fmt: skip
        previous = files
    changes = pd.DataFrame(rows, columns=FOLDER_CHANGE_COLUMNS[:-2])
    compared = pd.DataFrame(
        pairs, columns=["repo", "path", "version_seq", "target", "target_package_seq"]
    )
    compared["target_package_seq"] = compared["target_package_seq"].astype("Int64")
    return changes, compared


def _differing_files(
    old_files: tuple[ManifestFile, ...], new_files: tuple[ManifestFile, ...], memo: dict
) -> list[dict]:
    """``compare_folders`` rows that are not ``same`` with the same mode, cached per pair."""
    key = (old_files, new_files)
    if key not in memo:
        if old_files == new_files:
            memo[key] = []
        else:
            memo[key] = [
                {
                    "file_path": row["path"],
                    "old_file_path": row["old_path"],
                    "result": row["result"],
                    "old_sha": row["old_sha"],
                    "new_sha": row["new_sha"],
                    "mode_changed": row["mode_changed"],
                    "category": row["category"],
                    "size_change": row["size_change"],
                }
                for row in compare_folders(_as_manifest(old_files), _as_manifest(new_files))
                if row["result"] != "same" or row["mode_changed"]
            ]
    return memo[key]


# Stage 9c, step 5: risk flags and mentions


def flags_of_texts(texts: Mapping[str, str]) -> dict[str, list[str] | None]:
    """``sha -> flag_script_risks(text)`` (A16); None for a text over ``MAX_SCRIPT_BYTES``.

    A larger script is compared by sha only (``settings.MAX_SCRIPT_BYTES``).

    >>> flags_of_texts({"a": "curl https://x", "b": "print(1)"})
    {'a': ['network'], 'b': []}
    """
    flags: dict[str, list[str] | None] = {}
    for sha, text in texts.items():
        too_big = len(text.encode("utf-8", "replace")) > settings.MAX_SCRIPT_BYTES
        flags[sha] = None if too_big else flag_script_risks(text)
    return flags


def add_risk_flags(
    folder_changes: pd.DataFrame, flags: Mapping[str, list[str] | None]
) -> pd.DataFrame:
    """Add ``risk_flags`` and ``new_risk_flags`` to the script rows that changed a text.

    Only added, modified and renamed scripts get flags; every other row has
    nulls. ``risk_flags`` are the flags of the new text, null without a text.
    ``new_risk_flags`` are the flags the target's file (``old_sha``) did not
    have; an added script has no target file, so all its flags are new. A
    modified script whose old text is missing has null new flags: null is
    unknown, not safe.

    >>> rows = pd.DataFrame({"result": ["modified", "added", "modified"],
    ...     "category": ["script"] * 3, "old_sha": ["o", None, "gone"],
    ...     "new_sha": ["n", "d", "n"]})
    >>> flagged = add_risk_flags(rows, {"o": ["network"], "n": ["network", "runs_commands"],
    ...                                 "d": ["deletes_files"]})
    >>> flagged["new_risk_flags"].tolist()
    [['runs_commands'], ['deletes_files'], None]
    """
    risk, new = [], []
    for result, category, old_sha, new_sha in zip(
        folder_changes["result"], folder_changes["category"], folder_changes["old_sha"],
        folder_changes["new_sha"], strict=True,
    ):  # fmt: skip
        if category != "script" or result not in _SCRIPT_TEXT_RESULTS:
            risk.append(None)
            new.append(None)
            continue
        new_flags = flags.get(str(new_sha))
        old_flags = None if result == "added" else flags.get(str(old_sha))
        risk.append(new_flags)
        if new_flags is None or (result != "added" and old_flags is None):
            new.append(None)
        else:
            new.append([flag for flag in new_flags if flag not in (old_flags or [])])
    return folder_changes.assign(risk_flags=risk, new_risk_flags=new)


def script_shas_to_flag(folder_changes: pd.DataFrame) -> set[str]:
    """The old and new shas of the scripts whose flags ``add_risk_flags`` reads.

    >>> rows = pd.DataFrame({"result": ["modified", "removed"], "category": ["script"] * 2,
    ...     "old_sha": ["o", "r"], "new_sha": ["n", None]})
    >>> sorted(script_shas_to_flag(rows))
    ['n', 'o']
    """
    scripts = folder_changes.loc[
        (folder_changes["category"] == "script")
        & folder_changes["result"].isin(_SCRIPT_TEXT_RESULTS)
    ]
    shas = set(scripts["new_sha"].dropna()) | set(scripts["old_sha"].dropna())
    return {str(sha) for sha in shas if sha}


def mention_counts(
    copy_manifests: Mapping[tuple, tuple[ManifestFile, ...]],
    blobs: Mapping[tuple, str],
    texts: Mapping[str, str],
) -> dict[tuple, tuple[int | None, int | None]]:
    """``(missing_scripts, hidden_scripts)`` of each copy version (A17).

    ``check_script_mentions`` on the version's ``SKILL.md`` text and its folder:
    a missing script is named in the text but not in the folder, a hidden one
    is in the folder but not named. Both are null when the text is not stored.

    >>> folders = {("r", "SKILL.md", 1): (("scripts/a.py", "x", "100644", 1),
    ...                                   ("scripts/c.py", "y", "100644", 1))}
    >>> mention_counts(folders, {("r", "SKILL.md", 1): "t"}, {"t": "Run scripts/a.py and b.sh"})
    {('r', 'SKILL.md', 1): (1, 1)}
    """
    counts: dict[tuple, tuple[int | None, int | None]] = {}
    for key, files in copy_manifests.items():
        text = texts.get(str(blobs.get(key)))
        if text is None:
            counts[key] = (None, None)
            continue
        found = check_script_mentions(text, _as_manifest(files))
        counts[key] = (len(found["missing_scripts"]), len(found["hidden_scripts"]))
    return counts


# Stage 9c, step 6: the summary per version and target


def summarize_folder_changes(
    folder_changes: pd.DataFrame,
    compared: pd.DataFrame,
    mentions: Mapping[tuple, tuple[int | None, int | None]] | None = None,
    sources: Mapping[tuple, str] | None = None,
) -> pd.DataFrame:
    """One row per compared (copy version, target): counts of what differs.

    ``files_*`` count every file by result; ``scripts_added`` and
    ``scripts_modified`` count scripts; ``new_risky_scripts``
    counts added or modified scripts with a non-empty ``new_risk_flags``. A pair
    whose folders are the same has zeros. ``missing_scripts`` and
    ``hidden_scripts`` belong to the copy version and repeat on its targets.

    >>> rows = pd.DataFrame({"repo": ["r"] * 2, "path": ["p"] * 2, "version_seq": [1, 1],
    ...     "target": ["origin"] * 2, "result": ["modified", "added"],
    ...     "category": ["script"] * 2, "new_risk_flags": [["network"], []]})
    >>> pairs = pd.DataFrame({"repo": ["r"] * 2, "path": ["p"] * 2, "version_seq": [1, 1],
    ...     "target": ["origin", "newest"], "target_package_seq": [1, 2]})
    >>> summary = summarize_folder_changes(rows, pairs)
    >>> summary[["target", "files_added", "scripts_modified", "new_risky_scripts"]].values.tolist()
    [['origin', 1, 1, 1], ['newest', 0, 0, 0]]
    """
    keys = ["repo", "path", "version_seq", "target"]
    result = folder_changes["result"]
    is_script = folder_changes["category"] == "script"
    risky = folder_changes["new_risk_flags"].map(lambda cell: cell is not None and len(cell) > 0)
    flags = folder_changes.loc[:, keys].copy()
    for name in ("added", "removed", "modified", "renamed"):
        flags[f"files_{name}"] = (result == name).astype("int64")
    flags["scripts_added"] = (is_script & (result == "added")).astype("int64")
    flags["scripts_modified"] = (is_script & (result == "modified")).astype("int64")
    changed = result.isin(("added", "modified"))
    flags["new_risky_scripts"] = (is_script & changed & risky.astype(bool)).astype("int64")
    counts = flags.groupby(keys, as_index=False).sum()
    summary = compared.merge(counts, on=keys, how="left")
    count_columns = [column for column in counts.columns if column not in keys]
    summary[count_columns] = summary[count_columns].fillna(0).astype("int64")
    mentions, sources = mentions or {}, sources or {}
    version_keys = list(zip(summary["repo"], summary["path"], summary["version_seq"], strict=True))
    summary["missing_scripts"] = pd.array(
        [mentions.get(key, (None, None))[0] for key in version_keys], dtype="Int64"
    )
    summary["hidden_scripts"] = pd.array(
        [mentions.get(key, (None, None))[1] for key in version_keys], dtype="Int64"
    )
    summary["folder_source"] = [sources.get(key) for key in version_keys]
    return summary.loc[:, FOLDER_SUMMARY_COLUMNS]


# Stage 9c: reading inputs


def read_folder_inputs(dev: bool) -> dict[str, object]:
    """Check the stage 9c inputs, then read the copy versions and package versions.

    A missing file raises ``FileNotFoundError`` naming the command that builds it.
    """
    folder = settings.data_dir(dev)
    flag = " --dev" if dev else ""
    paths: dict[str, Path | str] = {
        "copy_versions": version_output_path(dev),
        "package_versions": folder / "lineage_package_versions.parquet",
        "folder_manifests": folder / "lineage_folder_manifests.parquet",
        "history_changes": copy_history_dir(dev) / "copied_skill_history_changes.parquet",
        "history_texts": _text_path(dev, "history"),
        "registry_texts": _text_path(dev, "registry"),
        "siblings": settings.SIBLINGS_GLOB,
        "artifacts": settings.ARTIFACTS_GLOB,
    }
    stage4 = f"python -m skill_genealogy.build_registry_lineages{flag}"
    stage6 = f"python -m skill_genealogy.download_copied_skill_history{flag}"
    check_inputs_exist(
        paths,
        {
            "copy_versions": f"python -m skill_genealogy.measure_copy_changes{flag}"
            " --only versions",
            "package_versions": stage4,
            "folder_manifests": stage4,
            "history_changes": stage6,
            "history_texts": stage6,
            "registry_texts": "python -m skill_genealogy.download_registry_history",
            "siblings": "python src/data/download_data.py",
            "artifacts": "python src/data/download_data.py",
        },
    )
    columns = ["repo", "path", "lineage_id", "origin_seq", "basis", "version_seq",
               "commit_date", "blob"]  # fmt: skip
    versions = read_parquet(paths["copy_versions"]).loc[:, columns]
    versions = versions.sort_values(["repo", "path", "version_seq"], kind="mergesort")
    return {
        "copy_versions": versions.reset_index(drop=True),
        "package_versions": read_parquet(paths["package_versions"]),
        "paths": paths,
    }


def _run_query(sql: str, path: Path | str, tables: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Run one stage 9c query on a parquet path with the given tables registered."""
    con = connect_duckdb()
    try:
        con.execute(f"SET memory_limit = '{settings.HISTORY_DUCKDB_MEMORY_LIMIT}'")
        for name, table in tables.items():
            con.register(name, table)
        return con.execute(sql.format(path=_quote_path(str(path)))).df()
    finally:
        con.close()


def load_copy_folder_rows(path: Path, copies: pd.DataFrame) -> pd.DataFrame:
    """Query Q9.5: the folder change rows of these copies' skill folders."""
    folders = pd.DataFrame(
        {"repo": copies["repo"], "skill_folder": copies["path"].map(skill_folder_of)}
    ).drop_duplicates()
    return _run_query(COPY_FOLDER_ROWS_SQL, path, {"wanted_folders": folders})


def load_stored_sizes(path: Path) -> dict[str, int]:
    """Query Q9.6: ``sha -> byte size`` of every stored copied-skill text."""
    frame = _run_query(STORED_SIZES_SQL, path, {})
    return dict(zip(frame["sha"].astype(str), frame["size"].astype(int), strict=True))


def load_sibling_files(copies: pd.DataFrame) -> pd.DataFrame:
    """Query Q9.7: the dataset file listing of these copies' folders."""
    if copies.empty:
        return pd.DataFrame(columns=["repo", "path", "file_path", "sha", "size"])
    wanted = copies.loc[:, ["repo", "path"]].drop_duplicates()
    return _run_query(SIBLING_FILES_SQL, settings.SIBLINGS_GLOB, {"wanted_files": wanted})


def load_listed_folders(copies: pd.DataFrame) -> set[tuple[str, str]]:
    """Query Q9.10: ``(repo, path)`` of these copies whose folder GitSkills listed."""
    if copies.empty:
        return set()
    wanted = copies.loc[:, ["repo", "path"]].drop_duplicates()
    rows = _run_query(LISTED_FOLDERS_SQL, settings.ARTIFACTS_GLOB, {"wanted_files": wanted})
    return set(zip(rows["repo"], rows["path"], strict=True))


def load_registry_manifests(
    path: Path, wanted: set[tuple[str, int]]
) -> dict[tuple[str, int], tuple[ManifestFile, ...]]:
    """Query Q9.8: the files of the wanted package versions; an empty package has ``()``."""
    folders: dict[tuple[str, int], tuple[ManifestFile, ...]] = dict.fromkeys(wanted, ())
    if not wanted:
        return folders
    table = pd.DataFrame(sorted(wanted), columns=["lineage_id", "package_version_seq"])
    rows = _run_query(REGISTRY_MANIFEST_SQL, path, {"wanted_packages": table})
    for (lineage_id, seq), group in rows.groupby(["lineage_id", "package_version_seq"]):
        folders[(str(lineage_id), int(seq))] = _manifest_tuples(group)
    return folders


def load_sibling_texts(shas: set[str]) -> dict[str, str]:
    """Query Q9.9: script texts from the dataset file listing."""
    if not shas:
        return {}
    con = connect_duckdb()
    try:
        con.execute(f"SET memory_limit = '{settings.DUCKDB_MEMORY_LIMIT}'")
        _register_shas(con, shas)
        rows = con.execute(
            SIBLING_TEXTS_SQL.format(path=_quote_path(settings.SIBLINGS_GLOB))
        ).fetchall()
    finally:
        con.close()
    return only_stored_text(rows)


def load_script_flags(dev: bool, shas: set[str]) -> dict[str, list[str] | None]:
    """Flags of the wanted script shas, read in chunks of ``SCRIPT_TEXT_BATCH`` shas.

    A sha's text comes from the copied-skill history, then the registry history,
    then the dataset file listing. Texts are dropped after each chunk; only the
    flags are kept. A sha with no text anywhere is left out (unknown).
    """
    flags: dict[str, list[str] | None] = {}
    ordered = sorted(shas)
    for start in range(0, len(ordered), settings.SCRIPT_TEXT_BATCH):
        chunk = set(ordered[start : start + settings.SCRIPT_TEXT_BATCH])
        texts = load_stored_texts(_text_path(dev, "history"), chunk)
        texts.update(load_stored_texts(_text_path(dev, "registry"), chunk - set(texts)))
        texts.update(load_sibling_texts(chunk - set(texts)))
        flags.update(flags_of_texts(texts))
    return flags


# Stage 9c: the build


def registry_packages_needed(
    copy_versions: pd.DataFrame, package_index: Mapping[str, list[tuple[int, int, int]]]
) -> set[tuple[str, int]]:
    """``(lineage_id, package_version_seq)`` of every registry target of these versions."""
    wanted: set[tuple[str, int]] = set()
    first_date: object = None
    for version in copy_versions.to_dict("records"):
        if int(version["version_seq"]) == 1:
            first_date = version["commit_date"]
        for seq in pick_folder_targets(version, first_date, package_index).values():
            if seq is not None:
                wanted.add((str(version["lineage_id"]), seq))
    return wanted


def build_copy_folder_changes(
    dev: bool, inputs: Mapping[str, object] | None = None, progress: Progress | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stage 9c steps 1-6: copy folders, targets, diffs, flags, mentions, summary.

    Copies are those of stage 9b (``copy_version_similarity``), in batches of
    ``COPY_CHANGE_FILE_BATCH`` files. Returns ``copy_folder_changes`` and
    ``copy_version_folder_summary``; counts are in the summary's ``.attrs``.
    """
    inputs = inputs or read_folder_inputs(dev)
    progress = progress or Progress(dev)
    paths = inputs["paths"]
    versions = inputs["copy_versions"]
    copies = versions.drop_duplicates(["repo", "path"]).loc[:, ["repo", "path"]]
    # Step 1: the commit of each history version
    progress.stage("read copy histories", f"{len(copies):,} copies, {len(versions):,} versions")
    commits = version_commits(versions, read_copy_histories(dev, copies))
    # Step 3: the registry packages of every target (A43), read once
    package_index = index_packages(inputs["package_versions"])
    wanted = registry_packages_needed(versions, package_index)
    progress.stage("read registry packages", f"{len(wanted):,} package versions")
    registry = load_registry_manifests(paths["folder_manifests"], wanted)
    progress.stage("read copy text sizes")
    sizes = load_stored_sizes(paths["history_texts"])
    size = settings.COPY_CHANGE_FILE_BATCH
    batches = math.ceil(len(copies) / size)
    progress.stage("compare folders", f"{batches} batches of {size:,} copies")
    progress.start_compare()
    changes, pairs = [], []
    mentions: dict[tuple, tuple[int | None, int | None]] = {}
    sources: dict[tuple, str] = {}
    counts = {"versions_without_folder": 0, "folder_rows_read": 0, "sibling_files_read": 0}
    copies_done = 0
    for number, batch in enumerate(batches_of_files(copies, size), start=1):
        batch_versions = versions.merge(batch, on=["repo", "path"])
        # Step 2: the copy folder of each version (A42), or the dataset snapshot
        folder_rows = load_copy_folder_rows(paths["history_changes"], batch)
        wanted_commits: dict[str, set[str]] = defaultdict(set)
        for repo, path, seq in zip(batch_versions["repo"], batch_versions["path"],
                                   batch_versions["version_seq"], strict=True):  # fmt: skip
            commit = commits.get((repo, path, int(seq)))
            if commit is not None:
                wanted_commits[f"{repo}::{skill_folder_of(path)}"].add(commit)
        at_commit, newest = snapshot_copy_folders(folder_rows, wanted_commits)
        dataset = batch_versions.loc[batch_versions["basis"] == "dataset"]
        siblings = load_sibling_files(dataset)
        folders = history_folder_manifests(batch_versions, commits, at_commit, newest, sizes)
        listed = load_listed_folders(dataset)
        snapshots = snapshot_folder_manifests(batch_versions, siblings, listed)
        folders.update(snapshots)
        for key in folders:
            sources[key] = "dataset_snapshot" if key in snapshots else "history"
        counts["versions_without_folder"] += len(batch_versions) - len(folders)
        counts["folder_rows_read"] += len(folder_rows)
        counts["sibling_files_read"] += len(siblings)
        # Step 4: compare with prev, origin, at-date and newest (A15)
        batch_changes, batch_pairs = compare_copy_folders(
            batch_versions, folders, package_index, registry
        )
        changes.append(batch_changes)
        pairs.append(batch_pairs)
        # Step 5b: missing and hidden scripts (A17)
        mentions.update(_batch_mentions(dev, batch_versions, folders))
        copies_done += len(batch)
        progress.batch(
            number, batches, copies_done, len(copies), sum(len(frame) for frame in changes),
            "folders", found_label="folder_rows",
        )  # fmt: skip
    folder_changes = _concat_frames(changes, FOLDER_CHANGE_COLUMNS[:-2])
    compared = _concat_frames(pairs, ["repo", "path", "version_seq", "target",
                                      "target_package_seq"])  # fmt: skip
    # Step 5a: risk flags of added and modified scripts (A16)
    shas = script_shas_to_flag(folder_changes)
    progress.stage("flag scripts", f"{len(shas):,} script texts")
    folder_changes = add_risk_flags(folder_changes, load_script_flags(dev, shas))
    # Step 6: the summary per copy version and target
    progress.stage("summarize")
    summary = summarize_folder_changes(folder_changes, compared, mentions, sources)
    summary.attrs.update(
        {
            "copies": len(copies),
            "copy_versions": len(versions),
            "registry_packages_read": len(wanted),
            "script_shas_flagged": len(shas),
            **counts,
        }
    )
    return folder_changes, summary


def _batch_mentions(
    dev: bool, batch_versions: pd.DataFrame, folders: Mapping[tuple, tuple[ManifestFile, ...]]
) -> dict[tuple, tuple[int | None, int | None]]:
    """Missing and hidden script counts of one batch; reads its ``SKILL.md`` texts."""
    blobs = {
        (repo, path, int(seq)): str(blob)
        for repo, path, seq, blob in zip(
            batch_versions["repo"], batch_versions["path"], batch_versions["version_seq"],
            batch_versions["blob"], strict=True,
        )
    }  # fmt: skip
    history = set(batch_versions.loc[batch_versions["basis"] == "history", "blob"])
    dataset = set(batch_versions.loc[batch_versions["basis"] == "dataset", "blob"])
    texts = load_stored_texts(_text_path(dev, "history"), {str(blob) for blob in history})
    texts.update(load_dataset_texts({str(blob) for blob in dataset}))
    return mention_counts(folders, blobs, texts)


def _concat_frames(frames: list[pd.DataFrame], columns: list[str]) -> pd.DataFrame:
    """Concatenate batch frames; an empty list gives an empty frame with ``columns``."""
    frames = [frame for frame in frames if len(frame)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)


# Stage 9c: summary and outputs


def folder_headline(summary: pd.DataFrame, folder_changes: pd.DataFrame) -> dict[str, object]:
    """The RQ2 folder numbers, read at each copy's last version against its origin package.

    A folder differs when a file was added, removed, modified or renamed, or a
    mode changed (a row in ``folder_changes``). ``new_risk_by_flag`` counts
    copies with at least one added or modified script that gained the flag.

    >>> summary = pd.DataFrame({"repo": ["r", "s"], "path": ["p", "p"], "version_seq": [1, 1],
    ...     "target": ["origin"] * 2, "files_added": [1, 0], "files_removed": [0, 0],
    ...     "files_modified": [0, 0], "files_renamed": [0, 0], "scripts_added": [1, 0],
    ...     "scripts_modified": [0, 0], "new_risky_scripts": [1, 0],
    ...     "missing_scripts": [0, 2], "hidden_scripts": [1, 0]})
    >>> changes = pd.DataFrame({"repo": ["r"], "path": ["p"], "version_seq": [1],
    ...     "target": ["origin"], "result": ["added"], "category": ["script"],
    ...     "new_risk_flags": [["network"]]})
    >>> numbers = folder_headline(summary, changes)
    >>> numbers["copies_folder_differs_from_origin"], numbers["new_risk_by_flag"]
    (1, {'network': 1})
    """
    last = summary.sort_values(["repo", "path", "version_seq"]).drop_duplicates(
        ["repo", "path", "target"], keep="last"
    )
    origin = last.loc[last["target"] == "origin"]
    changes = origin.merge(folder_changes, on=["repo", "path", "version_seq", "target"])
    differs = changes.drop_duplicates(["repo", "path"])
    risky = changes.loc[changes["result"].isin(("added", "modified"))]
    by_flag: dict[str, int] = {}
    for flag in sorted(settings.SCRIPT_RISK_PATTERNS):
        has_flag = risky["new_risk_flags"].map(
            lambda cell, f=flag: cell is not None and f in list(cell)
        )
        copies = risky.loc[has_flag].drop_duplicates(["repo", "path"])
        if len(copies):
            by_flag[flag] = len(copies)
    scripts_changed = (origin["scripts_added"] + origin["scripts_modified"]) > 0
    total = len(origin)
    return {
        "copies_with_origin_package": total,
        "copies_folder_differs_from_origin": len(differs),
        "share_folder_differs": round(len(differs) / total, 4) if total else None,
        "copies_scripts_added_or_modified": int(scripts_changed.sum()),
        "copies_new_risky_scripts": int((origin["new_risky_scripts"] > 0).sum()),
        "share_new_risky_scripts": round(float((origin["new_risky_scripts"] > 0).mean()), 4)
        if total
        else None,
        "new_risk_by_flag": by_flag,
        "copies_missing_scripts": int((origin["missing_scripts"].fillna(0) > 0).sum()),
        "copies_hidden_scripts": int((origin["hidden_scripts"].fillna(0) > 0).sum()),
    }


def print_folder_summary(
    folder_changes: pd.DataFrame, summary: pd.DataFrame, seconds: float
) -> None:
    """Print the stage 9c counts, the headline and two sample records of each output."""
    last = summary.drop_duplicates(["repo", "path"], keep="last")
    print_summary(
        "Stage 9c copy folder changes",
        {
            **summary.attrs,
            "folder_change_rows": len(folder_changes),
            "summary_rows": len(summary),
            "pairs_by_target": summary["target"].value_counts().to_dict(),
            "results": folder_changes["result"].value_counts().to_dict(),
            "categories": folder_changes["category"].value_counts().to_dict(),
            "folder_source_of_last_version": last["folder_source"].value_counts().to_dict(),
            **folder_headline(summary, folder_changes),
            "seconds": round(seconds, 1),
        },
    )
    flagged = folder_changes.loc[
        folder_changes["new_risk_flags"].map(lambda cell: cell is not None and len(cell) > 0)
    ]
    print_sample_records(
        pd.concat([folder_changes.head(1), flagged.head(1)]),
        "copy_folder_changes.parquet, 2 sample records (the second has a new risk flag)",
    )
    print_sample_records(
        summary.loc[summary["files_added"] + summary["files_modified"] > 0],
        "copy_version_folder_summary.parquet, 2 sample records",
    )


def save_folder_outputs(folder_changes: pd.DataFrame, summary: pd.DataFrame, dev: bool) -> None:
    """Write the two stage 9c files under temporary names, then replace both."""
    paths = folder_output_paths(dev)
    keys = ["repo", "path", "version_seq", "target"]
    parts = [
        (folder_changes, paths["folder_changes"], [*keys, "file_path"]),
        (summary, paths["folder_summary"], keys),
    ]
    temps = [path.with_name(path.name + ".tmp") for _frame, path, _sort in parts]
    try:
        for (frame, _path, sort_by), temp in zip(parts, temps, strict=True):
            write_parquet(frame, temp, sort_by)
        for (_frame, path, _sort), temp in zip(parts, temps, strict=True):
            temp.replace(path)
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)


def run_folders(dev: bool, force: bool) -> None:
    """Stage 9c: skip when the two outputs exist (unless ``force``), else build, save, print.

    With ``force`` the old files stay until the new build has finished.
    """
    if outputs_exist(list(folder_output_paths(dev).values())) and not force:
        print("skip: stage 9c outputs exist (use --force to rebuild)", flush=True)
        return
    started = time.perf_counter()
    progress = Progress(dev)
    progress.stage("read inputs", "dev" if dev else "full")
    inputs = read_folder_inputs(dev)
    folder_changes, summary = build_copy_folder_changes(dev, inputs, progress)
    # Step 7: save and print
    progress.stage("save outputs")
    save_folder_outputs(folder_changes, summary, dev)
    progress.finish()
    print_folder_summary(folder_changes, summary, time.perf_counter() - started)


# The parts of the stage, in the order a full run builds them.
PARTS = {"origin": run_origin, "versions": run_versions, "folders": run_folders}


def run(dev: bool, force: bool, only: str | None = None) -> None:
    """Run one part (``origin`` 9a, ``versions`` 9b, ``folders`` 9c), or every part in order."""
    for name, run_part in PARTS.items():
        if only is None or only == name:
            run_part(dev, force)


def main() -> None:
    """Run stage 9 from the command line: ``--dev``, ``--force``, ``--only <part>``."""
    parser = build_arg_parser(
        "Stage 9a/9b/9c: each copy against its origin, history edits, change profile;"
        " every copy version against four comparison texts; every copy folder against"
        " four comparison folders."
    )
    parser.add_argument(
        "--only",
        choices=list(PARTS),
        default=None,
        help="run one part: origin (9a), versions (9b) or folders (9c); default runs all three"
        " in order, each skips when its outputs exist",
    )
    args = parser.parse_args()
    run(bool(args.dev), bool(args.force), args.only)


if __name__ == "__main__":
    main()
