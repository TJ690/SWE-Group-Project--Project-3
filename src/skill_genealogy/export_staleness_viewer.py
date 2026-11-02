"""Write a static page that shows how far copies are behind a registry skill.

What: stage 13 keeps the lineages with the most copies, and writes one JSON
file per lineage plus a page. The page decides stale from a day threshold.
This export does not store a stale flag.
Inputs: ``copy_staleness.parquet``, ``lineages.parquet``,
``lineage_versions.parquet``, ``lineage_changes.parquet``,
``repo_roles_final.parquet``, and ``registry_hubs.parquet`` in
``data_dir(dev)``.
Outputs: ``results_dir(dev)/staleness_viewer/`` (HTML, JavaScript, CSS,
and JSON). The page loads D3 from a CDN when someone opens it. This stage
does not.
Run: ``python -m skill_genealogy.export_staleness_viewer --dev``
"""

from __future__ import annotations

import json
import math
import shutil
import time
from pathlib import Path

import pandas as pd

import skill_genealogy.settings as settings
from skill_genealogy.export_tree_viewer import _hub_reason, _merge_registry_hubs, safe_file_name
from skill_genealogy.file_helpers import (
    build_arg_parser,
    check_inputs_exist,
    connect_duckdb,
    print_sample_records,
    print_summary,
    read_parquet,
)
from skill_genealogy.settings import data_dir, results_dir

# Plan stage 13: the page lists this many lineages unless --top says otherwise.
DEFAULT_TOP_LINEAGES = 300
# Plan stage 13: the table keeps this many copies for one held version.
DEFAULT_ROWS_PER_VERSION = 500
# Copies with no held version (copied before every release) use this group.
NO_HELD_VERSION = "none"
# A version is marked risky when the change that created it added one of these.
_RISKY_KIND_FIELDS = (
    ("command", "commands_added"),
    ("network", "network_refs_added"),
    ("script", "scripts_mentioned_added"),
)
_PAGE_NAMES = ("index.html", "staleness_viewer.js", "staleness_viewer.css", "viewer_common.js")
_ROLE_COLUMNS = (
    "repo",
    "stars",
    "english_skills",
    "is_hub",
    "hub_share",
    "repo_role",
    "hub_evidence",
)

# Query Q13.1, see QUERIES.md
LINEAGES_SQL = """
SELECT
    lineage_id,
    repo,
    registry_removed
FROM read_parquet(?)
"""

# Query Q13.2, see QUERIES.md
VERSIONS_SQL = """
SELECT
    versions.lineage_id AS lineage_id,
    versions.version_seq AS version_seq,
    cast(versions.release_date AS VARCHAR) AS release_date
FROM read_parquet(?) AS versions
SEMI JOIN chosen_lineages USING (lineage_id)
"""

# Query Q13.3, see QUERIES.md
CHANGES_SQL = """
SELECT
    changes.lineage_id AS lineage_id,
    changes.to_seq AS to_seq,
    changes.commands_added AS commands_added,
    changes.network_refs_added AS network_refs_added,
    changes.scripts_mentioned_added AS scripts_mentioned_added
FROM read_parquet(?) AS changes
SEMI JOIN chosen_lineages USING (lineage_id)
"""

# Query Q13.4, see QUERIES.md
STALENESS_SQL = """
SELECT
    repo,
    path,
    lineage_id,
    held_seq,
    skill_md_versions_behind,
    days_stale,
    package_versions_behind,
    missed_script_updates,
    missed_risky,
    cast(stopped_changing_at AS VARCHAR) AS stopped_changing_at,
    copy_state,
    cast(as_of_date AS VARCHAR) AS as_of_date,
    as_of_basis
FROM read_parquet(?)
"""

# Query Q13.5, see QUERIES.md
ROLES_SQL = """
SELECT
    roles.repo AS repo,
    roles.stars AS stars,
    roles.english_skills AS english_skills,
    roles.is_hub AS is_hub,
    roles.hub_share AS hub_share,
    roles.repo_role AS repo_role,
    roles.hub_evidence AS hub_evidence
FROM read_parquet(?) AS roles
SEMI JOIN chosen_repos USING (repo)
"""


def lineage_to_json(
    lineage: dict, versions: pd.DataFrame, copies: pd.DataFrame, rows_per_version: int
) -> dict:
    """Group one lineage's copies by the version they hold.

    Algorithm A51, see CODE_FLOW.md. Inside one held version, rows are
    ordered by ``days_stale`` descending, with missing days last, then by
    repo and path. Only the first ``rows_per_version`` rows are kept in
    ``copies_by_version``. ``copy_days`` keeps every copy, so the page
    can recompute totals when the threshold moves. ``stale`` is not a
    key. ``orphaned`` is true when ``copy_state`` is ``orphaned``.

    Inputs: ``lineage`` has ``id`` (or ``lineage_id``), ``registry``
    (or ``repo``), ``registry_role``, and ``removed``. ``versions`` has
    one row per registry version. ``copies`` has one row per copy.
    ``rows_per_version`` is the table cap. Outputs: the JSON object.

    Example:
    >>> lineage = {"id": "acme/skills::pdf", "registry": "acme/skills"}
    >>> versions = pd.DataFrame([
    ...     {"version_seq": 1, "release_date": "2026-01-01"},
    ...     {"version_seq": 2, "release_date": "2026-03-01", "commands_added": ["curl"]},
    ... ])
    >>> copies = pd.DataFrame([
    ...     {"repo": "a/one", "path": "s.md", "held_seq": 1, "days_stale": 5,
    ...      "copy_state": "behind"},
    ...     {"repo": "b/two", "path": "s.md", "held_seq": 1, "days_stale": 50,
    ...      "copy_state": "stale"},
    ... ])
    >>> page = lineage_to_json(lineage, versions, copies, 1)
    >>> page["copies_by_version"]["1"][0]["repo"]
    'b/two'
    >>> "stale" in page["copies_by_version"]["1"][0]
    False
    >>> page["versions"][1]["missed_risky_kinds"]
    ['command']
    """
    if rows_per_version < 0:
        raise ValueError("rows_per_version must be 0 or more")
    info = lineage or {}
    lineage_id = _lineage_id(info)
    records = [_copy_record(row) for row in _frame_records(copies)]
    removed = _flag(info.get("removed")) or _flag(info.get("registry_removed"))
    return {
        "id": lineage_id,
        "registry": _registry_name(info, lineage_id),
        "registry_role": _text(info.get("registry_role")),
        "removed": removed,
        "versions": _version_records(versions),
        "as_of_dates": _as_of_dates(copies),
        "default_stale_days": _default_days(info),
        "copies_by_version": _group_copies(records, rows_per_version),
        "copy_days": [_day_row(row) for row in records],
    }


def build_lineages_index(staleness: pd.DataFrame, lineages: pd.DataFrame) -> list[dict]:
    """Rank lineages by how many copies they have.

    Every lineage that appears in ``staleness`` is returned, most copies
    first, then by id. ``export_staleness_viewer`` keeps the first
    ``top_n``. ``registry`` is the lineage repo. ``registry_role`` is
    copied when the lineages frame already has that column.
    ``file`` is the JSON path under ``lineages/``.

    Inputs: copy rows with ``lineage_id``, and lineage rows with
    ``lineage_id`` and ``repo``. Outputs: a list of ``id``, ``registry``,
    ``registry_role``, ``copies``, ``file``. An empty copy table returns
    an empty list.

    Example:
    >>> copies = pd.DataFrame({"lineage_id": ["b", "a", "a"]})
    >>> owners = pd.DataFrame({
    ...     "lineage_id": ["a", "b"], "repo": ["acme/skills", "acme/skills"],
    ...     "registry_role": ["registry", "registry"],
    ... })
    >>> build_lineages_index(copies, owners)[0]["id"]
    'a'
    """
    if not isinstance(staleness, pd.DataFrame) or "lineage_id" not in staleness.columns:
        return []
    counts = _copy_counts(staleness)
    if counts.empty:
        return []
    meta = _lineage_meta(lineages)
    frame = counts.merge(meta, on="lineage_id", how="left")
    frame = frame.sort_values(["copies", "lineage_id"], ascending=[False, True], kind="mergesort")
    rows = []
    for record in frame.to_dict("records"):
        lineage_id = str(record["lineage_id"])
        registry = _text(record.get("registry"))
        if registry is None and "::" in lineage_id:
            registry = lineage_id.split("::", 1)[0]
        rows.append(
            {
                "id": lineage_id,
                "registry": registry,
                "registry_role": _text(record.get("registry_role")),
                "copies": int(record["copies"]),
                "file": f"lineages/{safe_file_name(lineage_id)}.json",
            }
        )
    return rows


def build_repos_index(
    copies: pd.DataFrame, repo_roles: pd.DataFrame, lineages: pd.DataFrame
) -> list[dict]:
    """List repos that hold an exported copy, plus each lineage's registry.

    The shape matches the tree viewer, with ``lineages`` instead of
    ``families``. Role, stars, skill count, and the hub sentence come
    from ``repo_roles``. A repo with no role row is still listed.

    Inputs: copy rows (``repo``, ``lineage_id``), role rows, and the
    exported lineage rows (``lineage_id``, ``repo``). Outputs: one dict
    per repo, sorted by repo name.
    """
    pairs = _repo_lineage_pairs(copies, lineages)
    if not pairs:
        return []
    lookup = _role_lookup(repo_roles)
    rows = []
    for repo in sorted(pairs):
        record = lookup.get(repo, pd.Series(dtype=object))
        rows.append(
            {
                "repo": repo,
                "role": _text(record.get("repo_role")),
                "stars": _int(record.get("stars")),
                "skills": _int(record.get("english_skills")),
                "hub_share": _float(record.get("hub_share")),
                "hub_reason": _hub_reason(record),
                "hub_evidence": _text(record.get("hub_evidence")),
                "lineages": sorted(pairs[repo]),
            }
        )
    return rows


def export_staleness_viewer(dev: bool, top_n: int, rows_per_version: int) -> Path:
    """Write the staleness viewer folder and return that path.

    A second call with the same ``top_n`` and ``rows_per_version`` prints
    ``skip`` when the page and every lineage file are already there.
    ``--force`` is handled by ``main``, which clears the folder first.

    Inputs: ``dev`` selects the sample folders. ``top_n`` is how many
    lineages to keep. ``rows_per_version`` caps the table rows.
    Outputs: ``results_dir(dev)/staleness_viewer``.
    """
    if top_n < 0:
        raise ValueError("top_n must be 0 or more")
    if rows_per_version < 0:
        raise ValueError("rows_per_version must be 0 or more")
    started = time.perf_counter()
    folder = results_dir(dev) / "staleness_viewer"
    options = {
        "top_n": int(top_n),
        "rows_per_version": int(rows_per_version),
        "default_stale_days": int(settings.STALE_AFTER_DAYS),
    }
    if _same_export(folder, options):
        _print_saved(folder, time.perf_counter() - started, skipped=True)
        return folder
    # Step 1: read copy staleness and the lineage list
    paths = read_inputs(dev)
    staleness = _read_table(STALENESS_SQL, paths["copy_staleness"])
    lineage_rows = _read_table(LINEAGES_SQL, paths["lineages"])
    # Step 2: rank lineages by copies and keep the top ones
    index = build_lineages_index(staleness, lineage_rows)[:top_n]
    _avoid_file_collisions(index)
    chosen = [row["id"] for row in index]
    # Step 3: read versions, risky changes, and roles for those lineages
    versions = _read_chosen(VERSIONS_SQL, paths["lineage_versions"], chosen)
    changes = _read_chosen(CHANGES_SQL, paths["lineage_changes"], chosen)
    versions = _with_risky_kinds(versions, changes)
    roles = _roles_for(paths, staleness, lineage_rows, chosen)
    _fill_registry_roles(index, roles)
    copies = _attach_repo_roles(staleness, roles)
    # Step 4: build one JSON object per lineage
    payloads = _payloads(index, lineage_rows, versions, copies, rows_per_version, options)
    # Step 5: repo index for copy repos and registry repos
    repos = build_repos_index(
        _rows_for(copies, chosen), roles, _rows_for(lineage_rows, chosen)
    )
    # Step 6: copy the page and write JSON
    save_outputs(folder, index, payloads, repos, options)
    _print_written(index, payloads, repos, time.perf_counter() - started)
    return folder


def main() -> None:
    """Parse flags and write the staleness viewer.

    ``--dev`` uses the sample folders. ``--top`` defaults to 300.
    ``--rows-per-version`` defaults to 500. ``--force`` deletes the
    viewer folder first. The numbered steps are in
    ``export_staleness_viewer`` and match CODE_FLOW.md stage 13.
    """
    parser = build_arg_parser("write the staleness web page")
    parser.add_argument(
        "--top", type=int, default=DEFAULT_TOP_LINEAGES, help="lineages with the most copies"
    )
    parser.add_argument(
        "--rows-per-version",
        type=int,
        default=DEFAULT_ROWS_PER_VERSION,
        help="table rows kept for one held version",
    )
    args = parser.parse_args()
    if args.force:
        viewer = results_dir(args.dev) / "staleness_viewer"
        if viewer.exists():
            shutil.rmtree(viewer)
    export_staleness_viewer(args.dev, args.top, args.rows_per_version)


def read_inputs(dev: bool) -> dict[str, Path]:
    """Return the stage 13 input paths after checking that each file exists.

    The error names the command that builds a missing file.
    """
    flag = " --dev" if dev else ""
    paths = {
        "copy_staleness": data_dir(dev) / "copy_staleness.parquet",
        "lineages": data_dir(dev) / "lineages.parquet",
        "lineage_versions": data_dir(dev) / "lineage_versions.parquet",
        "lineage_changes": data_dir(dev) / "lineage_changes.parquet",
        "repo_roles_final": data_dir(dev) / "repo_roles_final.parquet",
        "registry_hubs": data_dir(dev) / "registry_hubs.parquet",
    }
    builders = {
        "copy_staleness": f"python -m skill_genealogy.measure_staleness{flag}",
        "lineages": f"python -m skill_genealogy.build_registry_lineages{flag}",
        "lineage_versions": f"python -m skill_genealogy.build_registry_lineages{flag}",
        "lineage_changes": f"python -m skill_genealogy.build_registry_lineages{flag}",
        "repo_roles_final": f"python -m skill_genealogy.find_copies{flag} --only rehost",
        "registry_hubs": f"python -m skill_genealogy.find_copies{flag} --only rehost",
    }
    check_inputs_exist(paths, builders)
    return paths


def save_outputs(
    folder: Path,
    index: list[dict],
    payloads: dict[str, dict],
    repos: list[dict],
    options: dict,
) -> None:
    """Copy the page and write the JSON files.

    Inputs: the viewer folder, the lineage index, one payload per
    lineage id, the repo index, and the options used for the skip check.
    Outputs: those files under ``folder``.
    """
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)
    _copy_page(folder)
    _write_json(folder / "lineages_index.json", index)
    _write_json(folder / "repos_index.json", repos)
    _write_json(folder / "export_options.json", options)
    for row in index:
        payload = payloads.get(row["id"])
        if payload is None:
            payload = lineage_to_json({"id": row["id"]}, pd.DataFrame(), pd.DataFrame(), 0)
        _write_json(folder / row["file"], payload)


def _copy_counts(staleness: pd.DataFrame) -> pd.DataFrame:
    """One row per lineage id and how many copy rows it has."""
    work = staleness.loc[:, ["lineage_id"]].copy()
    work["lineage_id"] = work["lineage_id"].map(_text)
    work = work.dropna(subset=["lineage_id"])
    if work.empty:
        return pd.DataFrame(columns=["lineage_id", "copies"])
    counts = work.groupby("lineage_id").size().rename("copies")
    return counts.reset_index()


def _lineage_meta(lineages: pd.DataFrame) -> pd.DataFrame:
    """Registry name and role for each lineage id."""
    columns = ["lineage_id", "registry", "registry_role"]
    if not isinstance(lineages, pd.DataFrame) or "lineage_id" not in lineages.columns:
        return pd.DataFrame(columns=columns)
    frame = lineages.drop_duplicates("lineage_id").copy()
    frame["lineage_id"] = frame["lineage_id"].map(_text)
    if "registry" not in frame.columns:
        frame["registry"] = frame["repo"] if "repo" in frame.columns else None
    if "registry_role" not in frame.columns:
        frame["registry_role"] = None
    return frame.loc[:, columns].dropna(subset=["lineage_id"])


def _repo_lineage_pairs(copies: pd.DataFrame, lineages: pd.DataFrame) -> dict[str, set[str]]:
    """Map each repo to the lineage ids it should link to."""
    pairs: dict[str, set[str]] = {}
    _add_pairs(pairs, copies, "repo", "lineage_id")
    _add_pairs(pairs, lineages, "repo", "lineage_id")
    return pairs


def _add_pairs(pairs: dict[str, set[str]], frame: pd.DataFrame, repo_col: str, id_col: str) -> None:
    """Add repo and lineage pairs from one frame."""
    if not isinstance(frame, pd.DataFrame) or not {repo_col, id_col}.issubset(frame.columns):
        return
    for repo, lineage_id in zip(frame[repo_col], frame[id_col], strict=False):
        repo_text = _text(repo)
        lineage_text = _text(lineage_id)
        if repo_text and lineage_text:
            pairs.setdefault(repo_text, set()).add(lineage_text)


def _role_lookup(repo_roles: pd.DataFrame) -> dict[str, pd.Series]:
    """One role series per repo name."""
    if not isinstance(repo_roles, pd.DataFrame) or "repo" not in repo_roles.columns:
        return {}
    roles = repo_roles.drop_duplicates("repo")
    return {str(record["repo"]): record for _, record in roles.iterrows()}


def _read_table(sql: str, path: Path) -> pd.DataFrame:
    """Run one read query. The only parameter is the parquet path."""
    connection = connect_duckdb()
    try:
        return connection.execute(sql, [str(path)]).fetchdf()
    finally:
        connection.close()


def _read_chosen(sql: str, path: Path, lineage_ids: list[str]) -> pd.DataFrame:
    """Run a query that keeps rows for ``lineage_ids``."""
    if not lineage_ids:
        return pd.DataFrame()
    connection = connect_duckdb()
    try:
        chosen = pd.DataFrame({"lineage_id": [str(item) for item in lineage_ids]})
        connection.register("chosen_lineages", chosen)
        return connection.execute(sql, [str(path)]).fetchdf()
    finally:
        connection.close()


def _roles_for(
    paths: dict[str, Path],
    staleness: pd.DataFrame,
    lineages: pd.DataFrame,
    chosen: list[str],
) -> pd.DataFrame:
    """Role rows for copy repos and registry repos in the chosen lineages.

    Query Q13.5, see QUERIES.md. Registry hub sentences are joined on
    after the role read. Dataset-date rows are left as they are.
    """
    repos = _needed_repos(staleness, lineages, chosen)
    if not repos:
        return pd.DataFrame(columns=list(_ROLE_COLUMNS))
    connection = connect_duckdb()
    try:
        connection.register("chosen_repos", pd.DataFrame({"repo": sorted(repos)}))
        roles = connection.execute(ROLES_SQL, [str(paths["repo_roles_final"])]).fetchdf()
    finally:
        connection.close()
    if roles.empty:
        roles = pd.DataFrame(columns=list(_ROLE_COLUMNS))
    return _merge_registry_hubs(roles, read_parquet(paths["registry_hubs"]))


def _needed_repos(staleness: pd.DataFrame, lineages: pd.DataFrame, chosen: list[str]) -> set[str]:
    """Repo names that appear in the chosen lineages."""
    chosen_ids = set(chosen)
    repos: set[str] = set()
    if "lineage_id" in staleness.columns and "repo" in staleness.columns:
        ids = staleness["lineage_id"].map(_text)
        mask = ids.isin(chosen_ids)
        for repo in staleness.loc[mask, "repo"]:
            text = _text(repo)
            if text:
                repos.add(text)
    if "lineage_id" in lineages.columns and "repo" in lineages.columns:
        ids = lineages["lineage_id"].map(_text)
        mask = ids.isin(chosen_ids)
        for repo in lineages.loc[mask, "repo"]:
            text = _text(repo)
            if text:
                repos.add(text)
    return repos


def _fill_registry_roles(index: list[dict], roles: pd.DataFrame) -> None:
    """Set each index row's registry role from the role table."""
    lookup = _role_lookup(roles)
    for row in index:
        registry = row.get("registry") or ""
        record = lookup.get(str(registry))
        if record is None:
            continue
        role = _text(record.get("repo_role"))
        if role:
            row["registry_role"] = role


def _attach_repo_roles(copies: pd.DataFrame, roles: pd.DataFrame) -> pd.DataFrame:
    """Add ``repo_role`` onto each copy. Missing roles stay empty."""
    work = copies.copy()
    if "repo_role" not in work.columns:
        work["repo_role"] = None
    if work.empty or "repo" not in work.columns or "repo_role" not in roles.columns:
        return work
    lookup = roles.loc[:, ["repo", "repo_role"]].drop_duplicates("repo").copy()
    lookup["repo"] = lookup["repo"].map(_text)
    lookup = lookup.dropna(subset=["repo"])
    work["repo"] = work["repo"].map(lambda value: _text(value) or "")
    merged = work.drop(columns=["repo_role"]).merge(lookup, on="repo", how="left")
    return merged


def _rows_for(frame: pd.DataFrame, chosen: list[str]) -> pd.DataFrame:
    """Keep rows whose lineage id is in ``chosen``."""
    if frame.empty or "lineage_id" not in frame.columns or not chosen:
        return frame.iloc[0:0].copy()
    ids = frame["lineage_id"].map(_text)
    return frame.loc[ids.isin(set(chosen))].copy()


def _payloads(
    index: list[dict],
    lineages: pd.DataFrame,
    versions: pd.DataFrame,
    copies: pd.DataFrame,
    rows_per_version: int,
    options: dict,
) -> dict[str, dict]:
    """One JSON object per exported lineage."""
    removed = _removed_flags(lineages)
    payloads = {}
    for row in index:
        lineage_id = row["id"]
        info = {
            "id": lineage_id,
            "registry": row.get("registry"),
            "registry_role": row.get("registry_role"),
            "removed": removed.get(lineage_id, False),
            "default_stale_days": options["default_stale_days"],
        }
        version_rows = _match_lineage(versions, lineage_id)
        copy_rows = _match_lineage(copies, lineage_id)
        payloads[lineage_id] = lineage_to_json(info, version_rows, copy_rows, rows_per_version)
    return payloads


def _removed_flags(lineages: pd.DataFrame) -> dict[str, bool]:
    """True when the lineage table says the registry removed the skill."""
    if lineages.empty or "lineage_id" not in lineages.columns:
        return {}
    flags = {}
    removed_col = "registry_removed" if "registry_removed" in lineages.columns else None
    for record in lineages.to_dict("records"):
        lineage_id = _text(record.get("lineage_id"))
        if lineage_id:
            flags[lineage_id] = _flag(record.get(removed_col)) if removed_col else False
    return flags


def _match_lineage(frame: pd.DataFrame, lineage_id: str) -> pd.DataFrame:
    """Rows for one lineage id."""
    if frame.empty or "lineage_id" not in frame.columns:
        return frame.iloc[0:0].copy()
    return frame.loc[frame["lineage_id"].map(_text).eq(lineage_id)].copy()


def _with_risky_kinds(versions: pd.DataFrame, changes: pd.DataFrame) -> pd.DataFrame:
    """Add the risky kinds introduced by the change that created each version."""
    if versions.empty:
        return versions
    kinds: dict[tuple[str, int], list[str]] = {}
    for record in _frame_records(changes):
        lineage_id = _text(record.get("lineage_id"))
        seq = _int(record.get("to_seq"))
        if lineage_id is None or seq is None:
            continue
        kinds[(lineage_id, seq)] = _kinds_from_change(record)
    work = versions.copy()
    labels = []
    for record in work.to_dict("records"):
        key = (_text(record.get("lineage_id")) or "", _int(record.get("version_seq")) or -1)
        labels.append(kinds.get(key, []))
    work["missed_risky_kinds"] = labels
    return work


def _kinds_from_change(record: dict) -> list[str]:
    """Risky kinds on one version step, in a fixed order.

    A command, a network reference, or a mentioned script counts when
    the added list is not empty. The same kind is listed once.
    """
    found = []
    for kind, column in _RISKY_KIND_FIELDS:
        if _has_items(record.get(column)):
            found.append(kind)
    return found


def _version_records(versions: pd.DataFrame) -> list[dict]:
    """Registry versions in sequence order."""
    rows = []
    seen: set[int] = set()
    for record in _frame_records(versions):
        seq = _int(record.get("version_seq"))
        if seq is None:
            seq = _int(record.get("seq"))
        if seq is None:
            seq = _int(record.get("to_seq"))
        if seq is None or seq in seen:
            continue
        seen.add(seq)
        rows.append(
            {
                "seq": seq,
                "release_date": _day(record.get("release_date")),
                "missed_risky_kinds": _version_kinds(record),
            }
        )
    rows.sort(key=lambda item: item["seq"])
    return rows


def _version_kinds(record: dict) -> list[str]:
    """Use a ready list of kinds, or derive one from the change columns."""
    if "missed_risky_kinds" in record and not _missing(record.get("missed_risky_kinds")):
        return _kind_list(record.get("missed_risky_kinds"))
    return _kinds_from_change(record)


def _kind_list(value: object) -> list[str]:
    """A list of kind names. A plain string is one kind."""
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    try:
        items = list(value)  # type: ignore[arg-type]
    except TypeError:
        return []
    kinds = []
    for item in items:
        text = _text(item)
        if text and text not in kinds:
            kinds.append(text)
    return kinds


def _group_copies(records: list[dict], rows_per_version: int) -> dict[str, list[dict]]:
    """The table rows, capped, most days behind first."""
    groups: dict[str, list[dict]] = {}
    for record in records:
        groups.setdefault(_held_key(record.get("held_seq")), []).append(record)
    ordered = {}
    for key in sorted(groups, key=_group_key):
        ranked = sorted(groups[key], key=_behind_key)
        ordered[key] = ranked[:rows_per_version]
    return ordered


def _held_key(held_seq: object) -> str:
    """The JSON key for a held version. Missing becomes ``none``."""
    number = _int(held_seq)
    if number is None:
        return NO_HELD_VERSION
    return str(number)


def _group_key(key: str) -> tuple[int, int]:
    """Numeric versions first, then the group with no held version."""
    if key == NO_HELD_VERSION:
        return (1, 0)
    return (0, int(key))


def _behind_key(record: dict) -> tuple:
    """Most days behind first. A missing day sorts last."""
    days = record.get("days_stale")
    missing = days is None
    repo = record.get("repo") or ""
    path = record.get("path") or ""
    return (missing, -(days if isinstance(days, int) else 0), repo, path)


def _copy_record(record: dict) -> dict:
    """One table row. ``stale`` is left out on purpose."""
    behind = _int(record.get("versions_behind"))
    if behind is None:
        behind = _int(record.get("skill_md_versions_behind"))
    state = _text(record.get("copy_state"))
    orphaned = _flag(record.get("orphaned")) or state == "orphaned"
    basis = _text(record.get("state_basis")) or _text(record.get("as_of_basis"))
    last_change = _day(record.get("last_change")) or _day(record.get("stopped_changing_at"))
    return {
        "repo": _text(record.get("repo")),
        "repo_role": _text(record.get("repo_role")),
        "path": _text(record.get("path")),
        "held_seq": _int(record.get("held_seq")),
        "versions_behind": 0 if behind is None else behind,
        "days_stale": _int(record.get("days_stale")),
        "last_change": last_change,
        "missed_risky": _int(record.get("missed_risky")) or 0,
        "package_versions_behind": _int(record.get("package_versions_behind")) or 0,
        "missed_script_updates": _int(record.get("missed_script_updates")) or 0,
        "state_basis": basis,
        "orphaned": orphaned,
    }


def _day_row(record: dict) -> dict:
    """The compact row the charts use for every copy."""
    return {
        "held_seq": record.get("held_seq"),
        "repo_role": record.get("repo_role"),
        "days_stale": record.get("days_stale"),
        "orphaned": bool(record.get("orphaned")),
    }


def _as_of_dates(copies: pd.DataFrame) -> list[str]:
    """Sorted measurement dates on the copy rows."""
    if not isinstance(copies, pd.DataFrame) or copies.empty or "as_of_date" not in copies.columns:
        return []
    found = {_day(value) for value in copies["as_of_date"]}
    return sorted(day for day in found if day)


def _default_days(lineage: dict) -> int:
    """The slider's starting value. The study default is 30 days."""
    value = _int(lineage.get("default_stale_days")) if isinstance(lineage, dict) else None
    if value is None:
        return int(settings.STALE_AFTER_DAYS)
    return value


def _lineage_id(lineage: dict) -> str:
    """``id`` when the caller already renamed it, otherwise ``lineage_id``."""
    for key in ("id", "lineage_id"):
        text = _text(lineage.get(key))
        if text:
            return text
    return ""


def _registry_name(lineage: dict, lineage_id: str) -> str | None:
    """The registry repo, or the owner/repo half of the lineage id."""
    for key in ("registry", "repo"):
        text = _text(lineage.get(key))
        if text:
            return text
    if "::" in lineage_id:
        return lineage_id.split("::", 1)[0]
    return None


def _frame_records(frame: pd.DataFrame) -> list[dict]:
    """DataFrame rows as dicts. A missing frame is an empty list."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    return frame.to_dict("records")


def _copy_page(folder: Path) -> None:
    """Copy the four viewer files next to the JSON."""
    package = Path(__file__).resolve().parent
    sources = {
        "viewer_common.js": package / "viewer_common.js",
        "index.html": package / "staleness_viewer" / "index.html",
        "staleness_viewer.js": package / "staleness_viewer" / "staleness_viewer.js",
        "staleness_viewer.css": package / "staleness_viewer" / "staleness_viewer.css",
    }
    for name, source in sources.items():
        shutil.copyfile(source, folder / name)


def _same_export(folder: Path, options: dict) -> bool:
    """True when this folder was written with the same options and is complete."""
    names = (*_PAGE_NAMES, "lineages_index.json", "repos_index.json", "export_options.json")
    if not all((folder / name).is_file() for name in names):
        return False
    try:
        saved = json.loads((folder / "export_options.json").read_text(encoding="utf-8"))
        index = json.loads((folder / "lineages_index.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if saved != options:
        return False
    return all((folder / row["file"]).is_file() for row in index)


def _avoid_file_collisions(index: list[dict]) -> None:
    """Give a lineage its own file when two ids clean to the same name."""
    used: dict[str, str] = {}
    for row in index:
        path = row["file"]
        if path not in used:
            used[path] = row["id"]
            continue
        stem = path[:-5] if path.endswith(".json") else path
        number = 2
        while f"{stem}__{number}.json" in used:
            number += 1
        path = f"{stem}__{number}.json"
        row["file"] = path
        used[path] = row["id"]
        print(f"file name collision: {row['id']} -> {path}")


def _print_saved(folder: Path, seconds: float, skipped: bool) -> None:
    """Print the counts already stored in the viewer folder."""
    if skipped:
        print("skip")
    index = json.loads((folder / "lineages_index.json").read_text(encoding="utf-8"))
    repos = json.loads((folder / "repos_index.json").read_text(encoding="utf-8"))
    print_summary(
        "staleness viewer",
        {"lineages": len(index), "repos": len(repos), "seconds": round(seconds, 1)},
    )
    print_sample_records(pd.DataFrame(index), "lineages index", 2)


def _print_written(
    index: list[dict], payloads: dict[str, dict], repos: list[dict], seconds: float
) -> None:
    """Print how many lineages, copies, and repos were written, plus two rows."""
    table_rows = 0
    copy_count = 0
    risky_versions = 0
    orphaned = 0
    for payload in payloads.values():
        copy_count += len(payload.get("copy_days") or [])
        orphaned += sum(1 for row in payload.get("copy_days") or [] if row.get("orphaned"))
        for version in payload.get("versions") or []:
            if version.get("missed_risky_kinds"):
                risky_versions += 1
        for group in (payload.get("copies_by_version") or {}).values():
            table_rows += len(group)
    print_summary(
        "staleness viewer",
        {
            "lineages": len(index),
            "copies": copy_count,
            "table_rows": table_rows,
            "orphaned": orphaned,
            "risky_versions": risky_versions,
            "repos": len(repos),
            "repos_with_hub_reason": sum(1 for row in repos if row.get("hub_reason")),
            "seconds": round(seconds, 1),
        },
    )
    print_sample_records(pd.DataFrame(index), "lineages index", 2)
    print_sample_records(_sample_copies(index, payloads), "table rows", 2)


def _sample_copies(index: list[dict], payloads: dict[str, dict]) -> pd.DataFrame:
    """Two table rows from the largest exported lineage."""
    if not index:
        return pd.DataFrame()
    payload = payloads.get(index[0]["id"]) or {}
    rows: list[dict] = []
    for group in (payload.get("copies_by_version") or {}).values():
        rows.extend(group)
        if len(rows) >= 2:
            break
    return pd.DataFrame(rows[:2])


def _write_json(path: Path, payload: object) -> None:
    """Write JSON. The parent folder is created. NaN is not allowed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_json_ready(payload), indent=2, allow_nan=False)
    path.write_text(text + "\n", encoding="utf-8")


def _json_ready(value: object):
    """Turn pandas and numpy values into plain JSON values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _json_ready(value.item())
        except (ValueError, AttributeError):
            pass
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _has_items(value: object) -> bool:
    """True when a change cell lists at least one added item."""
    if _missing(value):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    try:
        return len(value) > 0  # type: ignore[arg-type]
    except TypeError:
        return False


def _flag(value: object) -> bool:
    """A stored true/false cell. Missing is false."""
    if _missing(value):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _day(value: object) -> str | None:
    """The calendar day of a timestamp, or None."""
    text = _text(value)
    if text is None:
        return None
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return text


def _text(value: object) -> str | None:
    """A string cell, or None when it is missing."""
    if _missing(value):
        return None
    return str(value)


def _int(value: object) -> int | None:
    """An integer cell, or None when it is missing."""
    if _missing(value):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _float(value: object) -> float | None:
    """A float cell, or None when it is missing or not a number."""
    if _missing(value):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _missing(value: object) -> bool:
    """True for None, NaN, NaT, and blank strings."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in {"", "None", "nan", "NaT", "<NA>"}
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    if isinstance(missing, bool):
        return missing
    return False


if __name__ == "__main__":
    main()
