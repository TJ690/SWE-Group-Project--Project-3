"""Stage 9a: how a copy differs from the registry version it started from (RQ2).

What: stage 7 gave every dated copy an origin, one registry version. This stage
compares the copy's current text with that origin (unchanged, cosmetic or
substantive, what was added, front matter on its own), asks whether the headings
the copy added are the ones the registry added later (algorithm A38), labels each
edit inside the copy's own history ``sync``, ``other_source`` or ``local``
(algorithm A39), and sets the four kinds of change side by side in one profile
table. A label describes the text. It does not say who typed the file.

Inputs (dev or full data folder): ``copy_files.parquet`` and ``families.parquet``
(stage 7), ``lineage_versions.parquet`` and ``lineage_changes.parquet`` (4a).
Shared full folder: ``skill_language.parquet`` (2a), ``registry_history/`` texts
(3). Dev or full: ``copied_skill_history/`` (6). GitSkills ``artifacts`` for the
text of a copy that has no stored history text.

Outputs: ``copy_changes.parquet`` (one row per copy file), ``copy_history_edits.parquet``
(one row per edit in a copy's history) and ``change_profile.parquet`` (one row per
population).

Run: ``python -m skill_genealogy.measure_copy_changes [--dev] [--force] [--only origin]``.
Design note: docs/skill_genealogy/design/15_copy_changes.md.
"""

from __future__ import annotations

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
    compare_front_matter,
    compare_texts,
    find_project_names_added,
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


# Paths


def output_paths(dev: bool) -> dict[str, Path]:
    """The three stage 9a output files of this mode."""
    folder = settings.data_dir(dev)
    return {
        "copy_changes": folder / "copy_changes.parquet",
        "copy_history_edits": folder / "copy_history_edits.parquet",
        "change_profile": folder / "change_profile.parquet",
    }


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
    ) -> None:
        """Record a finished batch and print one line with the rate and time left."""
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
                "history_edits": edits,
                "text_source": source,
                "copies_per_second": round(rate, 1),
                "eta_seconds": None if rate <= 0 else round(left),
            }
        )
        eta = f"{left / 60:.1f} min" if rate > 0 else "?"
        self._print(
            f"batch {number}/{batches} ({source}): copies {copies_done:,}/{copies_total:,}"
            f" ({percent}%) · edits {edits:,} · {rate:.0f} copies/s · ETA {eta}"
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


def run(dev: bool, force: bool) -> None:
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


def main() -> None:
    """Run stage 9a from the command line (``--dev``, ``--force``, ``--only origin``)."""
    parser = build_arg_parser(
        "Stage 9a: each copy against its origin, history edits, change profile."
    )
    parser.add_argument(
        "--only",
        choices=["origin"],
        default="origin",
        help="the part to run; versions and folders (G20, G21) are added by their cards",
    )
    args = parser.parse_args()
    run(bool(args.dev), bool(args.force))


if __name__ == "__main__":
    main()
