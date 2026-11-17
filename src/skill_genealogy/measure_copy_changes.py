"""Stages 9a and 9b: how a copy differs from the registry version it started from (RQ2).

What: stage 7 gave every dated copy an origin, one registry version. Part
``origin`` (9a) compares the copy's current text with that origin (unchanged,
cosmetic or substantive, what was added, front matter on its own), asks whether
the headings the copy added are the ones the registry added later (algorithm
A38), labels each edit inside the copy's own history ``sync``, ``other_source``
or ``local`` (algorithm A39), and sets the four kinds of change side by side in
one profile table. Part ``versions`` (9b) scores every version of every copy
against four texts chosen by date: its previous version, the origin, the
registry version current that day and the newest registry version (algorithm
A40), and lists the notable lines each step added (algorithm A41). A label
describes the text. It does not say who typed the file.

Inputs (dev or full data folder): ``copy_files.parquet`` and ``families.parquet``
(stage 7), ``lineage_versions.parquet`` and ``lineage_changes.parquet`` (4a).
Shared full folder: ``skill_language.parquet`` (2a), ``registry_history/`` texts
(3). Dev or full: ``copied_skill_history/`` (6). GitSkills ``artifacts`` for the
text of a copy that has no stored history text.

Outputs: ``copy_changes.parquet`` (one row per copy file), ``copy_history_edits.parquet``
(one row per edit in a copy's history), ``change_profile.parquet`` (one row per
population) and ``copy_version_similarity.parquet`` (one row per copy version).

Run: ``python -m skill_genealogy.measure_copy_changes [--dev] [--force] [--only origin|versions]``.
Design note: docs/skill_genealogy/design/15_copy_changes.md.
"""

from __future__ import annotations

import bisect
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
from skill_genealogy.build_registry_lineages import history_dir, normalized_sha
from skill_genealogy.compare_texts import (
    TextChange,
    _body,
    _diff_line_lists,
    _fence_state,
    _has_word,
    compare_front_matter,
    compare_texts,
    find_headings,
    find_network_refs,
    find_project_names_added,
    find_script_mentions,
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


# The parts of the stage, in the order a full run builds them.
PARTS = {"origin": run_origin, "versions": run_versions}


def run(dev: bool, force: bool, only: str | None = None) -> None:
    """Run one part (``origin`` is 9a, ``versions`` is 9b), or every part in order."""
    for name, run_part in PARTS.items():
        if only is None or only == name:
            run_part(dev, force)


def main() -> None:
    """Run stage 9 from the command line (``--dev``, ``--force``, ``--only origin|versions``)."""
    parser = build_arg_parser(
        "Stage 9a/9b: each copy against its origin, history edits, change profile;"
        " every copy version against four comparison texts."
    )
    parser.add_argument(
        "--only",
        choices=list(PARTS),
        default=None,
        help="run one part: origin (9a) or versions (9b); default runs both, each skips when"
        " its outputs exist. Folders (G21) are added by their card",
    )
    args = parser.parse_args()
    run(bool(args.dev), bool(args.force), args.only)


if __name__ == "__main__":
    main()
