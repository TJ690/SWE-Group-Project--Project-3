"""Build one parent link and one generation for every text in a family.

What: stage 8 turns stage 7 families into trees. A source family has a
registry trunk. Each copy text picks the most similar earlier node.
Inputs: ``families.parquet``, ``copy_files.parquet``, lineage versions
and roots, registry and dataset MinHash signatures, and artifact dates
for texts that are not copy files.
Outputs: ``family_tree.parquet`` and ``family_summary.parquet`` in
``data_dir(dev)``.
Run: ``python -m skill_genealogy.build_family_trees --dev``
"""

from __future__ import annotations

import shutil
import time
import warnings
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from skill_genealogy import settings
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
from skill_genealogy.find_copies import PART_GLOB, load_signatures

TREE_COLUMNS = [
    "node_id",
    "family_id",
    "node_kind",
    "lineage_id",
    "version_seq",
    "file_sha",
    "first_repo",
    "node_date",
    "files",
    "repos",
    "parent_id",
    "parent_similarity",
    "generation",
    "is_dated",
    "approx",
]
SUMMARY_COLUMNS = [
    "family_id",
    "family_kind",
    "texts",
    "files",
    "repos",
    "owners",
    "registry_lineages",
    "registry_versions",
    "first_date",
    "last_date",
    "max_generation",
    "mean_generation",
    "root_node",
    "root_basis",
    "approx",
]
_SOURCE_KINDS = ("source", "hub_only")
_CHUNK_FAMILIES = 2000

# Query Q8.1, see QUERIES.md
# Dates for texts that are not registry copy files. Copy files keep the
# history date from stage 7 instead of this scan.
MEMBER_DATES_SQL = """
WITH need AS (
    SELECT DISTINCT file_sha
    FROM families
    WHERE family_kind IN ('independent', 'singleton')
),
hits AS (
    SELECT
        artifacts.file_sha AS file_sha,
        artifacts.repo_full_name AS repo,
        TRY_CAST(artifacts.first_commit_at AS TIMESTAMPTZ) AS commit_at
    FROM artifacts
    SEMI JOIN need ON artifacts.file_sha = need.file_sha
),
counts AS (
    SELECT
        file_sha,
        count(*)::BIGINT AS files,
        count(DISTINCT repo)::BIGINT AS repos,
        min(commit_at) AS node_date
    FROM hits
    GROUP BY file_sha
),
earliest AS (
    SELECT hits.file_sha AS file_sha, min(hits.repo) AS first_repo
    FROM hits
    JOIN counts ON hits.file_sha = counts.file_sha
    WHERE hits.commit_at IS NOT DISTINCT FROM counts.node_date
    GROUP BY hits.file_sha
)
SELECT
    counts.file_sha AS file_sha,
    earliest.first_repo AS first_repo,
    counts.node_date AS node_date,
    counts.files AS files,
    counts.repos AS repos
FROM counts
JOIN earliest ON counts.file_sha = earliest.file_sha
"""


def registry_node_id(lineage_id: str, version_seq: int) -> str:
    """Return ``lineage_id#version_seq``, the same id as a registry signature.

    Example:
    >>> registry_node_id("acme/app::skills/a", 2)
    'acme/app::skills/a#2'
    """
    return f"{lineage_id}#{int(version_seq)}"


def copy_node_id(file_sha: str) -> str:
    """Return ``text:file_sha`` so a copy node cannot collide with a version id.

    Example:
    >>> copy_node_id("abc")
    'text:abc'
    """
    return f"text:{file_sha}"


def build_trunk_nodes(
    family_id: str,
    registry_versions: pd.DataFrame,
    roots: pd.DataFrame,
) -> pd.DataFrame:
    """One generation-0 node per registry version in this family's lineages.

    Inside one lineage the parent is the previous version. The first
    version of a re-hosted lineage points at ``copied_from_seq`` and
    stays at generation 0.

    Example:
    >>> versions = pd.DataFrame({
    ...     "lineage_id": ["acme/app::skills/a", "acme/app::skills/a", "other/app::skills/a"],
    ...     "version_seq": [1, 2, 1],
    ...     "blob": ["b1", "b2", "b1"],
    ...     "release_date": ["2026-01-01", "2026-02-01", "2026-03-01"],
    ... })
    >>> roots = pd.DataFrame({
    ...     "lineage_id": ["acme/app::skills/a", "other/app::skills/a"],
    ...     "root_lineage": ["acme/app::skills/a", "acme/app::skills/a"],
    ...     "copied_from_lineage": [None, "acme/app::skills/a"],
    ...     "copied_from_seq": [None, 1],
    ... })
    >>> nodes = build_trunk_nodes("acme/app::skills/a", versions, roots)
    >>> [int(value) for value in nodes["generation"]]
    [0, 0, 0]
    >>> nodes.loc[nodes["version_seq"].eq(2), "parent_id"].iloc[0]
    'acme/app::skills/a#1'
    >>> nodes.loc[nodes["lineage_id"].eq("other/app::skills/a"), "parent_id"].iloc[0]
    'acme/app::skills/a#1'
    """
    # Algorithm A36, see CODE_FLOW.md
    lineage_ids = _lineage_ids_for_family(family_id, roots, registry_versions)
    if not lineage_ids or registry_versions.empty:
        return _empty_tree()
    versions = registry_versions.loc[
        registry_versions["lineage_id"].astype(str).isin(lineage_ids)
    ].copy()
    if versions.empty:
        return _empty_tree()
    versions = versions.dropna(subset=["lineage_id", "version_seq"])
    versions = versions.drop_duplicates(["lineage_id", "version_seq"], keep="first")
    nodes = _version_frame(family_id, versions.reset_index(drop=True))
    return _link_version_parents(nodes, roots)


def build_member_nodes(
    family_texts: pd.DataFrame,
    copy_files: pd.DataFrame,
) -> pd.DataFrame:
    """One node per copy text. The date is the earliest file, and that file's repo.

    ``files`` and ``repos`` count the files that hold the text. A text
    with no file row is kept, undated, with counts of 0. ``registry_self``
    texts are registry blobs and are left out: the trunk already has them.

    Inputs: family rows (one per text) and file rows (``repo``,
    ``file_sha``, ``first_commit_date``). A row that already has
    ``files`` and ``repos``, and a unique ``file_sha``, is used as-is.
    Outputs: member nodes. Parents are not chosen here.
    """
    texts = _copy_texts(family_texts)
    if texts.empty:
        return _empty_tree()
    prepared = _prepared_file_rows(copy_files, set(texts["file_sha"].astype(str)))
    merged = texts.loc[:, ["file_sha", "family_id"]].merge(prepared, on="file_sha", how="left")
    merged["files"] = merged["files"].fillna(0).astype("int64")
    merged["repos"] = merged["repos"].fillna(0).astype("int64")
    return _member_frame(merged)


def score_candidates_simple(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Share of equal MinHash values, one stored row at a time.

    The share is the number of matching positions divided by the width.
    ``score_candidates`` computes the same share with one array compare.

    Example:
    >>> query = np.array([1, 1, 1, 0], dtype=np.uint64)
    >>> matrix = np.array([[1, 1, 1, 0], [1, 0, 0, 0]], dtype=np.uint64)
    >>> score_candidates_simple(query, matrix).tolist()
    [1.0, 0.5]
    """
    query, matrix = _score_inputs(query, matrix)
    width = int(query.size)
    scores = np.zeros(matrix.shape[0], dtype=np.float64)
    if width == 0 or matrix.shape[0] == 0:
        return scores
    for index, row in enumerate(matrix):
        matches = sum(int(left == right) for left, right in zip(row, query, strict=True))
        scores[index] = matches / width
    return scores


def score_candidates(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Share of equal MinHash values, as ``(matrix == query).mean(axis=1)``.

    This is the estimated Jaccard used to pick a parent. It matches
    ``score_candidates_simple``.

    Example:
    >>> query = np.array([1, 1, 1, 0], dtype=np.uint64)
    >>> matrix = np.array([[1, 1, 1, 0], [1, 0, 0, 0]], dtype=np.uint64)
    >>> score_candidates(query, matrix).tolist()
    [1.0, 0.5]
    """
    # Algorithm A37, see CODE_FLOW.md
    query, matrix = _score_inputs(query, matrix)
    if query.size == 0 or matrix.shape[0] == 0:
        return np.zeros(matrix.shape[0], dtype=np.float64)
    return np.asarray(matrix == query, dtype=np.float64).mean(axis=1)


def pick_parent_for_member(
    member: pd.Series | dict,
    earlier_nodes: pd.DataFrame,
    signatures: dict[str, object],
    max_earlier: int,
    *,
    scorer=score_candidates,
) -> tuple[str, float]:
    """Return the parent node id and the estimated Jaccard.

    Dated members see registry versions released strictly before their
    date, plus at most ``max_earlier`` dated members already placed
    (the most recent ones). A tie keeps the earliest node. The same
    blob as a registry version scores 1.0 and wins. Undated copy nodes
    in ``earlier_nodes`` are not parents. No candidate returns ``("", nan)``.

    Example:
    >>> earlier = pd.DataFrame({
    ...     "node_id": ["reg::s#1", "reg::s#2"],
    ...     "node_kind": ["registry_version", "registry_version"],
    ...     "file_sha": ["b1", "b2"],
    ...     "node_date": pd.to_datetime(["2026-01-01", "2026-03-01"], utc=True),
    ...     "is_dated": [True, True],
    ... })
    >>> member = {
    ...     "node_id": "text:a",
    ...     "file_sha": "a",
    ...     "node_date": pd.Timestamp("2026-02-01", tz="UTC"),
    ...     "is_dated": True,
    ... }
    >>> signatures = {
    ...     "reg::s#1": np.array([1, 0, 0, 0], dtype=np.uint64),
    ...     "reg::s#2": np.array([1, 1, 1, 1], dtype=np.uint64),
    ...     "text:a": np.array([1, 1, 1, 1], dtype=np.uint64),
    ... }
    >>> pick_parent_for_member(member, earlier, signatures, 2000)
    ('reg::s#1', 0.25)
    """
    # Algorithm A37, see CODE_FLOW.md
    record = _as_record(member)
    registries, dated_members = _split_earlier(earlier_nodes)
    index, matrix = _bundle(signatures)
    parent_id, similarity, _approx = _choose_parent(
        record,
        registries,
        dated_members,
        [],
        index,
        matrix,
        int(max_earlier),
        scorer,
    )
    return parent_id, similarity


def build_one_family_tree(
    family_id: str,
    family_texts: pd.DataFrame,
    copy_files: pd.DataFrame,
    registry_versions: pd.DataFrame,
    roots: pd.DataFrame,
    signatures: dict[str, object],
    max_earlier: int | None = None,
) -> pd.DataFrame:
    """Return every node of one family, with parent, similarity, and generation.

    Source and hub families get a trunk (A36). An independent family
    roots at the oldest dated member, or the member with the most files
    when none are dated. Other members then pick a parent (A37).
    ``approx`` is true on a member when the earlier-member cap dropped
    someone. ``max_earlier`` defaults to ``TREE_MAX_EARLIER_MEMBERS``.

    Inputs: one family's texts, its file rows, the lineage tables, and
    signatures keyed by node id (or a bundle with ``ids`` and ``signatures``).
    Outputs: the family tree. An empty family returns the column frame.
    """
    if max_earlier is None:
        max_earlier = settings.TREE_MAX_EARLIER_MEMBERS
    texts = family_texts
    if "family_id" not in texts.columns:
        texts = texts.copy()
        texts["family_id"] = family_id
    elif "family_id" in texts.columns:
        texts = texts.loc[texts["family_id"].astype(str).eq(str(family_id))]
    files = _files_for_family(copy_files, family_id)
    trunk = build_trunk_nodes(family_id, registry_versions, roots)
    members = build_member_nodes(texts, files)
    if trunk.empty and members.empty:
        return _empty_tree()
    kind = _family_kind(texts)
    if not members.empty:
        members = _attach_member_parents(members, trunk, signatures, int(max_earlier), kind)
    frames = [frame for frame in (trunk, members) if not frame.empty]
    return pd.concat(frames, ignore_index=True).loc[:, TREE_COLUMNS]


def summarize_families(
    tree: pd.DataFrame,
    families: pd.DataFrame,
    copy_files: pd.DataFrame,
) -> pd.DataFrame:
    """One row per family: size, registry counts, depth, and the root.

    ``texts``, ``files``, ``repos``, and ``owners`` come from the family
    table. ``mean_generation`` is the mean over copy nodes. A family with
    no tree rows is ``root_basis`` ``skipped`` and is left out of depth.

    Inputs: the tree, the family table (one row per text), and copy files
    used only when the family table has no count columns.
    Outputs: ``family_summary`` columns, one row per ``family_id``.
    """
    totals = _family_totals(families, copy_files)
    if totals.empty and tree.empty:
        return _empty_summary()
    stats = _tree_stats(tree)
    summary = totals.merge(stats, on="family_id", how="left")
    built = summary["root_node"].notna() | summary["max_generation"].notna()
    summary.loc[built, "mean_generation"] = summary.loc[built, "mean_generation"].fillna(0.0)
    missing = ~built
    summary.loc[missing, "root_basis"] = "skipped"
    summary.loc[missing, "approx"] = False
    summary["max_generation"] = summary["max_generation"].astype("Int64")
    summary["registry_lineages"] = summary["registry_lineages"].fillna(0).astype("int64")
    summary["registry_versions"] = summary["registry_versions"].fillna(0).astype("int64")
    summary["approx"] = summary["approx"].fillna(False).astype(bool)
    return summary.loc[:, SUMMARY_COLUMNS]


def read_inputs(dev: bool) -> dict[str, pd.DataFrame]:
    """Check stage 8 inputs and read the small tables.

    Signatures and artifact dates are loaded by later steps. A missing
    file raises ``FileNotFoundError`` and names the command that builds it.

    Inputs: ``dev`` — True reads the dev folder. Dataset signatures and
    the artifact glob always come from the full data folder.
    Outputs: ``families``, ``copy_files``, ``versions``, and ``roots``.
    """
    paths = _input_paths(dev)
    check_inputs_exist(paths, _input_builders(dev))
    families = read_parquet(paths["families"])
    copies = read_parquet(paths["copy_files"])
    keep = ["repo", "file_sha", "root_lineage", "first_commit_date"]
    copies = copies.loc[:, [name for name in keep if name in copies.columns]]
    versions = read_parquet(paths["lineage_versions"])
    version_keep = ["lineage_id", "version_seq", "blob", "release_date"]
    versions = versions.loc[:, [name for name in version_keep if name in versions.columns]]
    roots = read_parquet(paths["lineage_roots"])
    return {
        "families": families,
        "copy_files": copies,
        "versions": versions,
        "roots": roots,
    }


def read_member_dates(dev: bool) -> pd.DataFrame:
    """Earliest artifact date, repo, and file counts for loose texts.

    Query Q8.1. Independent texts and singletons are not in
    ``copy_files.parquet``. Source copies keep the stage 7 history date
    and are not in this result.

    Inputs: ``dev`` selects the families file. Artifacts are the raw glob.
    Outputs: ``file_sha``, ``first_repo``, ``node_date``, ``files``, ``repos``.
    """
    paths = _input_paths(dev)
    temp_dir = settings.data_dir(dev) / "_family_tree_tmp"
    _remove_tree(temp_dir)
    connection = connect_duckdb()
    try:
        _prepare_duckdb(connection, temp_dir)
        _register_view(
            connection,
            "families",
            str(paths["families"]),
            "file_sha, family_kind",
        )
        _register_view(
            connection,
            "artifacts",
            str(paths["artifacts"]),
            "repo_full_name, file_sha, first_commit_at",
        )
        with warnings.catch_warnings():
            # DuckDB's fetchdf still calls DataFrame.drop(inplace=True).
            warnings.filterwarnings(
                "ignore",
                message="The inplace keyword in DataFrame.drop is deprecated",
            )
            frame = connection.execute(MEMBER_DATES_SQL).fetchdf()
    finally:
        connection.close()
        _remove_tree(temp_dir)
    if frame.empty:
        return pd.DataFrame(columns=["file_sha", "first_repo", "node_date", "files", "repos"])
    frame["node_date"] = pd.to_datetime(frame["node_date"], utc=True, errors="coerce")
    frame["files"] = frame["files"].fillna(0).astype("int64")
    frame["repos"] = frame["repos"].fillna(0).astype("int64")
    return frame


def load_tree_signatures(dev: bool, file_shas: set[str]) -> dict[str, object]:
    """Load registry signatures and the dataset signatures for ``file_shas``.

    The result is a bundle: ``ids``, ``signatures`` (uint64 matrix), and
    ``_index`` (id to row). A dataset id ``sha`` is also stored as
    ``text:sha``, the copy node id. Texts with no stored signature are
    absent; parent search then scores them as 0.

    Inputs: ``dev`` selects the registry file. Dataset parts always come
    from the full data folder. ``file_shas`` is the copy texts that need
    a parent search.
    Outputs: the bundle. The matrix is width 128.
    """
    folder = settings.data_dir(dev)
    registry_ids, registry_matrix, _sizes = load_signatures(folder / "registry_signatures.npz")
    copy_ids, copy_matrix = _dataset_rows(_dataset_dir(), file_shas)
    return _bundle_parts(list(registry_ids), registry_matrix, copy_ids, copy_matrix)


def save_outputs(tree: pd.DataFrame, summary: pd.DataFrame, dev: bool) -> tuple[Path, Path]:
    """Write the tree and the summary, sorted so a second run matches.

    Inputs: the two frames and ``dev``.
    Outputs: the two parquet paths under ``data_dir(dev)``.
    """
    folder = settings.data_dir(dev)
    tree_path = folder / "family_tree.parquet"
    summary_path = folder / "family_summary.parquet"
    write_parquet(tree, tree_path, ["family_id", "generation", "node_date", "node_id"])
    write_parquet(summary, summary_path, ["family_id"])
    return tree_path, summary_path


def main() -> None:
    """Build family trees and write the two parquet files.

    ``--dev`` uses the dev folder. ``--force`` rebuilds the outputs.
    ``--skip-families-over N`` leaves families with more than N texts
    out of the tree and lists them. A second run without ``--force``
    prints ``skip``.
    """
    parser = build_arg_parser("Build family trees: parent links and generations.")
    parser.add_argument(
        "--skip-families-over",
        type=int,
        default=None,
        help="do not build trees for families with more texts than N",
    )
    args = parser.parse_args()
    dev = bool(args.dev)
    if args.skip_families_over is not None and args.skip_families_over < 0:
        raise SystemExit("--skip-families-over must be 0 or greater")
    if args.force:
        for path in _output_paths(dev):
            if path.is_file():
                path.unlink()
    if outputs_exist(_output_paths(dev)):
        print("skip: family tree outputs already exist", flush=True)
        _print_saved_summary(dev)
        return
    started = time.perf_counter()
    # Step 1: Read families, copy files, lineage versions, and roots.
    print("Step 1: read families, copy files, versions, roots", flush=True)
    tables = read_inputs(dev)
    families = tables["families"]
    print(
        f"families {families['family_id'].nunique()} texts {len(families)} "
        f"copy files {len(tables['copy_files'])} versions {len(tables['versions'])}",
        flush=True,
    )
    kept, skipped = _split_skipped(families, args.skip_families_over)
    _print_skipped(skipped)
    # Step 2: Member dates for texts that are not copy files (query Q8.1).
    print("Step 2: member dates", flush=True)
    loose_dates = read_member_dates(dev)
    print(f"loose texts with an artifact row {len(loose_dates)}", flush=True)
    # Step 3: Signatures for families that need a parent search.
    print("Step 3: signatures", flush=True)
    wanted = _shas_that_need_signatures(kept)
    signatures = load_tree_signatures(dev, wanted)
    print(f"signature rows {len(signatures['ids'])} wanted texts {len(wanted)}", flush=True)
    # Step 4: Trunk, members, and parents (algorithms A36 and A37).
    print("Step 4: trunks, members, parents", flush=True)
    tree = _build_all_trees(
        kept,
        tables["copy_files"],
        loose_dates,
        tables["versions"],
        tables["roots"],
        signatures,
    )
    # Step 5: Family summary.
    print("Step 5: family summary", flush=True)
    summary = summarize_families(tree, families, tables["copy_files"])
    # Step 6: Save and print counts and two sample records.
    print("Step 6: save", flush=True)
    save_outputs(tree, summary, dev)
    elapsed = time.perf_counter() - started
    _print_run_summary(tree, summary, elapsed)


def _build_all_trees(
    families: pd.DataFrame,
    copy_files: pd.DataFrame,
    loose_dates: pd.DataFrame,
    versions: pd.DataFrame,
    roots: pd.DataFrame,
    signatures: dict[str, object],
) -> pd.DataFrame:
    """Singletons are leaves. Every other kept family goes through one tree."""
    if families.empty:
        return _empty_tree()
    singles = families.loc[families["family_kind"].eq("singleton")]
    rest = families.loc[~families["family_kind"].eq("singleton")]
    frames: list[pd.DataFrame] = []
    if not singles.empty:
        frames.append(_build_leaf_nodes(singles, loose_dates))
        print(f"singleton nodes {len(singles)}", flush=True)
    if rest.empty:
        return frames[0] if frames else _empty_tree()
    frames.extend(_build_linked_trees(rest, copy_files, loose_dates, versions, roots, signatures))
    return pd.concat(frames, ignore_index=True)


def _build_linked_trees(
    families: pd.DataFrame,
    copy_files: pd.DataFrame,
    loose_dates: pd.DataFrame,
    versions: pd.DataFrame,
    roots: pd.DataFrame,
    signatures: dict[str, object],
) -> list[pd.DataFrame]:
    """Call ``build_one_family_tree`` once per source, hub, or cluster family."""
    copy_groups = _groupby_if_column(copy_files, "root_lineage")
    loose = _attach_family_id(loose_dates, families)
    loose_groups = _groupby_if_column(loose, "family_id")
    root_groups = _groupby_if_column(roots, "root_lineage")
    version_groups = _groupby_if_column(versions, "lineage_id")
    empty_versions = versions.iloc[0:0]
    empty_roots = roots.iloc[0:0]
    empty_files = copy_files.iloc[0:0]
    chunks: list[pd.DataFrame] = []
    pending: list[pd.DataFrame] = []
    built = 0
    for family_id, texts in families.groupby("family_id", sort=False):
        started = time.perf_counter()
        kind = str(texts["family_kind"].iloc[0])
        if kind in _SOURCE_KINDS:
            family_roots = _group_or_empty(root_groups, str(family_id), empty_roots)
            family_versions = _versions_for_roots(version_groups, family_roots, empty_versions)
            files = _group_or_empty(copy_groups, str(family_id), empty_files)
        else:
            family_roots = empty_roots
            family_versions = empty_versions
            files = _group_or_empty(loose_groups, str(family_id), empty_files)
        tree = build_one_family_tree(
            str(family_id),
            texts,
            files,
            family_versions,
            family_roots,
            signatures,
        )
        pending.append(tree)
        built += 1
        if len(texts) >= 1000:
            seconds = time.perf_counter() - started
            print(f"large family {family_id} texts {len(texts)} seconds {seconds:.1f}", flush=True)
        if len(pending) >= _CHUNK_FAMILIES:
            chunks.append(pd.concat(pending, ignore_index=True))
            pending = []
            print(f"linked families {built}", flush=True)
    if pending:
        chunks.append(pd.concat(pending, ignore_index=True))
        print(f"linked families {built}", flush=True)
    return chunks


def _build_leaf_nodes(texts: pd.DataFrame, dates: pd.DataFrame) -> pd.DataFrame:
    """One generation-0 node per singleton. The same shape as a one-member tree."""
    base = texts.loc[:, ["file_sha", "family_id"]].drop_duplicates("file_sha")
    if dates.empty:
        merged = base.copy()
        merged["first_repo"] = None
        merged["node_date"] = pd.NaT
        merged["files"] = 0
        merged["repos"] = 0
    else:
        keep = dates.loc[:, ["file_sha", "first_repo", "node_date", "files", "repos"]]
        merged = base.merge(keep, on="file_sha", how="left")
        merged["files"] = merged["files"].fillna(0).astype("int64")
        merged["repos"] = merged["repos"].fillna(0).astype("int64")
    return _member_frame(merged)


def _attach_member_parents(
    members: pd.DataFrame,
    trunk: pd.DataFrame,
    signatures: dict[str, object],
    max_earlier: int,
    kind: str,
) -> pd.DataFrame:
    """Fill parent, similarity, generation, and approx on member nodes."""
    index, matrix = _bundle(signatures)
    registries = [_as_record(row) for row in _registry_rows(trunk)]
    records = {
        row["node_id"]: row for row in (_as_record(item) for item in members.to_dict("records"))
    }
    root_id, dated_ids, undated_ids = _parent_order(members, kind, not trunk.empty)
    parent_of: dict[str, str | None] = {}
    similarity_of: dict[str, float] = {}
    generation_of: dict[str, int] = {}
    approx_of: dict[str, bool] = {}
    placed_dated: list[dict] = []
    extra: list[dict] = []
    if root_id is not None:
        parent_of[root_id] = None
        similarity_of[root_id] = float("nan")
        generation_of[root_id] = 0
        approx_of[root_id] = False
        root = records[root_id]
        if root["is_dated"]:
            placed_dated.append(root)
        else:
            extra.append(root)
    for node_id in dated_ids + undated_ids:
        if node_id == root_id:
            continue
        member = records[node_id]
        parent_id, similarity, approx = _choose_parent(
            member,
            registries,
            placed_dated,
            extra,
            index,
            matrix,
            max_earlier,
            score_candidates,
        )
        parent_of[node_id] = parent_id or None
        similarity_of[node_id] = similarity
        generation_of[node_id] = _child_generation(parent_id, generation_of, registries)
        approx_of[node_id] = approx
        if member["is_dated"]:
            placed_dated.append(member)
    out = members.copy()
    out["parent_id"] = pd.Series(
        [parent_of.get(node_id) for node_id in out["node_id"]],
        index=out.index,
        dtype="object",
    )
    out["parent_similarity"] = [
        similarity_of.get(node_id, float("nan")) for node_id in out["node_id"]
    ]
    out["generation"] = [int(generation_of.get(node_id, 0)) for node_id in out["node_id"]]
    out["approx"] = [bool(approx_of.get(node_id, False)) for node_id in out["node_id"]]
    return out


def _choose_parent(
    member: dict,
    registries: list[dict],
    dated_members: list[dict],
    extra_members: list[dict],
    index: dict[str, int],
    matrix: np.ndarray,
    max_earlier: int,
    scorer,
) -> tuple[str, float, bool]:
    """Parent id, similarity, and whether the member cap dropped a candidate."""
    candidates, approx = _select_candidate_records(
        member, registries, dated_members, extra_members, max_earlier
    )
    parent_id, similarity = _best_parent_records(member, candidates, index, matrix, scorer)
    return parent_id, similarity, approx


def _select_candidate_records(
    member: dict,
    registries: list[dict],
    dated_members: list[dict],
    extra_members: list[dict],
    max_earlier: int,
) -> tuple[list[dict], bool]:
    """Registries before the member, plus the most recent earlier members.

    ``dated_members`` is oldest first, so the tail is the most recent.
    ``extra_members`` is only an undated family root. ``approx`` is true
    when a dated member was left out.
    """
    if max_earlier < 0:
        raise ValueError("max_earlier must be 0 or greater")
    if member["is_dated"] and member["time_ns"] is not None:
        limit = member["time_ns"]
        chosen_registries = [
            row for row in registries if row["time_ns"] is not None and row["time_ns"] < limit
        ]
    else:
        chosen_registries = list(registries)
    approx = len(dated_members) > max_earlier
    recent = dated_members[-max_earlier:] if max_earlier else []
    return chosen_registries + recent + list(extra_members), approx


def _best_parent_records(
    member: dict,
    candidates: list[dict],
    index: dict[str, int],
    matrix: np.ndarray,
    scorer,
) -> tuple[str, float]:
    """Earliest node among the maximum estimated Jaccard. Same blob scores 1."""
    if not candidates:
        return "", float("nan")
    blob_hits = [
        row
        for row in candidates
        if row["node_kind"] == "registry_version"
        and row["file_sha"]
        and row["file_sha"] == member["file_sha"]
    ]
    if blob_hits:
        return _earliest_record(blob_hits)["node_id"], 1.0
    scores = np.zeros(len(candidates), dtype=np.float64)
    query = _query_vector(member, index, matrix)
    if query is not None:
        positions: list[int] = []
        locs: list[int] = []
        for position, row in enumerate(candidates):
            loc = index.get(row["node_id"])
            if loc is None and row["file_sha"]:
                loc = index.get(row["file_sha"])
            if loc is not None:
                positions.append(position)
                locs.append(loc)
        if locs:
            block = matrix[np.asarray(locs, dtype=np.int64)]
            scores[np.asarray(positions, dtype=np.int64)] = scorer(query, block)
    win_at = _earliest_index(candidates, scores)
    return candidates[win_at]["node_id"], float(scores[win_at])


def _earliest_record(candidates: list[dict], scores: np.ndarray | None = None) -> dict:
    """The candidate with the best score, then the earliest date, then node id."""
    return candidates[_earliest_index(candidates, scores)]


def _earliest_index(candidates: list[dict], scores: np.ndarray | None = None) -> int:
    """Index of the best score, then the earliest date, then the smallest node id."""
    if scores is None:
        chosen = list(range(len(candidates)))
    else:
        best = float(scores.max())
        chosen = [index for index, score in enumerate(scores) if float(score) == best]

    def sort_key(index: int) -> tuple:
        row = candidates[index]
        missing = row["time_ns"] is None
        return (missing, 0 if missing else row["time_ns"], row["node_id"])

    return min(chosen, key=sort_key)


def _parent_order(
    members: pd.DataFrame,
    kind: str,
    has_trunk: bool,
) -> tuple[str | None, list[str], list[str]]:
    """Root id, then dated ids, then undated ids. The root is not in those lists."""
    dated = members.loc[members["is_dated"]].sort_values(["node_date", "node_id"], kind="mergesort")
    undated = members.loc[~members["is_dated"]].sort_values("node_id", kind="mergesort")
    dated_ids = [str(node_id) for node_id in dated["node_id"]]
    undated_ids = [str(node_id) for node_id in undated["node_id"]]
    if kind in _SOURCE_KINDS and has_trunk:
        return None, dated_ids, undated_ids
    root_id = _independent_root_id(members)
    dated_ids = [node_id for node_id in dated_ids if node_id != root_id]
    undated_ids = [node_id for node_id in undated_ids if node_id != root_id]
    return root_id, dated_ids, undated_ids


def _independent_root_id(members: pd.DataFrame) -> str | None:
    """Oldest dated member, otherwise the member with the most files."""
    if members.empty:
        return None
    dated = members.loc[members["is_dated"]]
    if not dated.empty:
        ordered = dated.sort_values(
            ["node_date", "files", "node_id"],
            ascending=[True, False, True],
            kind="mergesort",
        )
        return str(ordered.iloc[0]["node_id"])
    ordered = members.sort_values(
        ["files", "repos", "node_id"],
        ascending=[False, False, True],
        kind="mergesort",
    )
    return str(ordered.iloc[0]["node_id"])


def _child_generation(parent_id: str, generation_of: dict[str, int], registries: list[dict]) -> int:
    """Parent generation plus one. A missing parent stays at generation 0."""
    if not parent_id:
        return 0
    if parent_id in generation_of:
        return generation_of[parent_id] + 1
    for row in registries:
        if row["node_id"] == parent_id:
            return int(row["generation"]) + 1
    return 1


def _lineage_ids_for_family(
    family_id: str,
    roots: pd.DataFrame,
    versions: pd.DataFrame,
) -> list[str]:
    """Lineage ids whose root is this family. A missing root row uses the family id."""
    if not roots.empty and "root_lineage" in roots.columns:
        matched = roots.loc[roots["root_lineage"].astype(str).eq(str(family_id)), "lineage_id"]
        if not matched.empty:
            return [str(item) for item in matched.tolist()]
    if not versions.empty and versions["lineage_id"].astype(str).eq(str(family_id)).any():
        return [str(family_id)]
    return []


def _link_version_parents(nodes: pd.DataFrame, roots: pd.DataFrame) -> pd.DataFrame:
    """Previous version, or the copied-from version on the first row of a re-host."""
    copied = _copied_from_by_lineage(roots)
    known = set(nodes["node_id"].astype(str))
    chains: dict[str, list[tuple[int, str]]] = {}
    for lineage_id, version_seq, node_id in zip(
        nodes["lineage_id"], nodes["version_seq"], nodes["node_id"], strict=True
    ):
        chains.setdefault(str(lineage_id), []).append((int(version_seq), str(node_id)))
    parent_of: dict[str, str | None] = {}
    for lineage_id, pairs in chains.items():
        pairs.sort()
        for index, (_seq, node_id) in enumerate(pairs):
            if index == 0:
                parent = copied.get(lineage_id)
                parent_of[node_id] = parent if parent in known else None
            else:
                parent_of[node_id] = pairs[index - 1][1]
    ordered = nodes.sort_values(["lineage_id", "version_seq"], kind="mergesort")
    ordered["parent_id"] = pd.Series(
        [parent_of[str(node_id)] for node_id in ordered["node_id"]],
        index=ordered.index,
        dtype="object",
    )
    ordered["generation"] = 0
    ordered["approx"] = False
    return ordered.reset_index(drop=True)


def _copied_from_by_lineage(roots: pd.DataFrame) -> dict[str, str]:
    """Map a re-hosted lineage to the node id it was copied from."""
    if roots.empty or "copied_from_lineage" not in roots.columns:
        return {}
    found: dict[str, str] = {}
    for row in roots.to_dict("records"):
        source = _clean_text(row.get("copied_from_lineage"))
        seq = row.get("copied_from_seq")
        lineage_id = _clean_text(row.get("lineage_id"))
        if not lineage_id or not source or seq is None or pd.isna(seq):
            continue
        found[lineage_id] = registry_node_id(source, int(seq))
    return found


def _version_frame(family_id: str, versions: pd.DataFrame) -> pd.DataFrame:
    """Registry nodes before parent links are filled."""
    versions = versions.reset_index(drop=True)
    lineage = versions["lineage_id"].astype(str)
    seq = versions["version_seq"].astype(int)
    node_date = pd.to_datetime(versions["release_date"], utc=True, errors="coerce")
    frame = _blank_nodes(len(versions))
    frame["node_id"] = [
        registry_node_id(name, number) for name, number in zip(lineage, seq, strict=True)
    ]
    frame["family_id"] = family_id
    frame["node_kind"] = "registry_version"
    frame["lineage_id"] = lineage.to_numpy()
    frame["version_seq"] = pd.array(seq.to_numpy(), dtype="Int64")
    frame["file_sha"] = [_clean_text(value) for value in versions["blob"]]
    frame["first_repo"] = lineage.str.split("::", n=1).str[0].to_numpy()
    frame["node_date"] = node_date
    frame["files"] = 1
    frame["repos"] = 1
    frame["is_dated"] = node_date.notna().to_numpy()
    return frame


def _member_frame(rows: pd.DataFrame) -> pd.DataFrame:
    """Copy-text nodes from one row per text. Parents are still empty."""
    rows = rows.reset_index(drop=True)
    node_date = pd.to_datetime(rows["node_date"], utc=True, errors="coerce")
    frame = _blank_nodes(len(rows))
    frame["node_id"] = [copy_node_id(str(sha)) for sha in rows["file_sha"]]
    frame["family_id"] = rows["family_id"].astype(str).to_numpy()
    frame["node_kind"] = "copy_text"
    frame["file_sha"] = rows["file_sha"].astype(str).to_numpy()
    frame["first_repo"] = rows["first_repo"].to_numpy() if "first_repo" in rows.columns else None
    frame["node_date"] = node_date
    frame["files"] = rows["files"].fillna(0).astype("int64").to_numpy()
    frame["repos"] = rows["repos"].fillna(0).astype("int64").to_numpy()
    frame["is_dated"] = node_date.notna().to_numpy()
    return frame


def _blank_nodes(count: int) -> pd.DataFrame:
    """A node frame of ``count`` rows with empty parents and generation 0."""
    frame = pd.DataFrame(
        {
            "node_id": pd.Series([None] * count, dtype="object"),
            "family_id": pd.Series([None] * count, dtype="object"),
            "node_kind": pd.Series([None] * count, dtype="object"),
            "lineage_id": pd.Series([None] * count, dtype="object"),
            "version_seq": pd.Series([pd.NA] * count, dtype="Int64"),
            "file_sha": pd.Series([None] * count, dtype="object"),
            "first_repo": pd.Series([None] * count, dtype="object"),
            "node_date": pd.Series([pd.NaT] * count, dtype="datetime64[ns, UTC]"),
            "files": np.zeros(count, dtype=np.int64),
            "repos": np.zeros(count, dtype=np.int64),
            "parent_id": pd.Series([None] * count, dtype="object"),
            "parent_similarity": np.full(count, np.nan),
            "generation": np.zeros(count, dtype=np.int64),
            "is_dated": np.zeros(count, dtype=bool),
            "approx": np.zeros(count, dtype=bool),
        }
    )
    return frame.loc[:, TREE_COLUMNS]


def _empty_tree() -> pd.DataFrame:
    """The tree columns and dtypes, with no rows."""
    return _blank_nodes(0)


def _empty_summary() -> pd.DataFrame:
    """The summary columns, with no rows."""
    frame = pd.DataFrame(columns=SUMMARY_COLUMNS)
    frame["texts"] = pd.Series(dtype="int64")
    frame["max_generation"] = pd.Series(dtype="Int64")
    frame["mean_generation"] = pd.Series(dtype="float64")
    frame["approx"] = pd.Series(dtype="bool")
    return frame


def _copy_texts(family_texts: pd.DataFrame) -> pd.DataFrame:
    """Family rows that are copy texts, one per ``file_sha``."""
    if family_texts.empty:
        return family_texts
    texts = family_texts
    if "registry_self" in texts.columns:
        flag = texts["registry_self"].fillna(False).astype(bool)
        texts = texts.loc[~flag]
    if "file_sha" not in texts.columns:
        raise ValueError("family texts are missing column: file_sha")
    texts = texts.drop_duplicates("file_sha")
    if "family_id" not in texts.columns:
        texts = texts.copy()
        texts["family_id"] = ""
    return texts.reset_index(drop=True)


def _prepared_file_rows(files: pd.DataFrame, wanted: set[str]) -> pd.DataFrame:
    """One row per text: earliest repo, date, and file and repo counts."""
    columns = ["file_sha", "first_repo", "node_date", "files", "repos"]
    if files.empty or "file_sha" not in files.columns:
        return pd.DataFrame(columns=columns)
    work = files.loc[files["file_sha"].astype(str).isin(wanted)].copy()
    if work.empty:
        return pd.DataFrame(columns=columns)
    work["file_sha"] = work["file_sha"].astype(str)
    if "node_date" in work.columns:
        work["node_date"] = pd.to_datetime(work["node_date"], utc=True, errors="coerce")
    else:
        source = work["first_commit_date"] if "first_commit_date" in work.columns else pd.NaT
        work["node_date"] = pd.to_datetime(source, utc=True, errors="coerce")
    if "first_repo" not in work.columns:
        work["first_repo"] = work["repo"] if "repo" in work.columns else None
    if {"files", "repos"}.issubset(work.columns) and work["file_sha"].is_unique:
        return work.loc[:, columns].reset_index(drop=True)
    work = work.sort_values(
        ["file_sha", "node_date", "first_repo"],
        kind="mergesort",
        na_position="last",
    )
    first = (
        work.groupby("file_sha", sort=False).head(1).loc[:, ["file_sha", "first_repo", "node_date"]]
    )
    counts = work.groupby("file_sha", sort=False).agg(
        files=("file_sha", "size"),
        repos=("first_repo", "nunique"),
    )
    return first.merge(counts, on="file_sha", how="left").reset_index(drop=True)


def _files_for_family(files: pd.DataFrame, family_id: str) -> pd.DataFrame:
    """File rows that belong to ``family_id``. A frame with no family column is kept."""
    if files.empty:
        return files
    if "family_id" in files.columns:
        return files.loc[files["family_id"].astype(str).eq(str(family_id))]
    if "root_lineage" in files.columns:
        return files.loc[files["root_lineage"].astype(str).eq(str(family_id))]
    return files


def _family_kind(family_texts: pd.DataFrame) -> str:
    """The kind on the first text row. An empty frame is treated as independent."""
    if family_texts.empty or "family_kind" not in family_texts.columns:
        return "independent"
    return str(family_texts["family_kind"].iloc[0])


def _family_totals(families: pd.DataFrame, copy_files: pd.DataFrame) -> pd.DataFrame:
    """One row per family with kind and the stage 7 size columns."""
    if families.empty:
        return pd.DataFrame(
            columns=["family_id", "family_kind", "texts", "files", "repos", "owners"]
        )
    named: dict[str, tuple[str, str]] = {
        "family_kind": ("family_kind", "first"),
        "texts": ("file_sha", "nunique"),
    }
    for column in ("files", "repos", "owners"):
        if column in families.columns:
            named[column] = (column, "first")
    totals = families.groupby("family_id", sort=False).agg(**named).reset_index()
    for column in ("files", "repos", "owners"):
        if column not in totals.columns:
            totals[column] = 0
    if (
        "files" not in families.columns
        and not copy_files.empty
        and "root_lineage" in copy_files.columns
    ):
        counts = copy_files.groupby("root_lineage").agg(
            files=("file_sha", "size"),
            repos=("repo", "nunique"),
        )
        totals = totals.merge(
            counts, left_on="family_id", right_index=True, how="left", suffixes=("", "_copy")
        )
        totals["files"] = totals["files_copy"].fillna(totals["files"])
        totals["repos"] = totals["repos_copy"].fillna(totals["repos"])
        totals = totals.drop(columns=["files_copy", "repos_copy"])
    for column in ("texts", "files", "repos", "owners"):
        totals[column] = totals[column].fillna(0).astype("int64")
    return totals


def _tree_stats(tree: pd.DataFrame) -> pd.DataFrame:
    """Depth, dates, registry counts, and the root, one row per family in the tree."""
    columns = [
        "family_id",
        "registry_lineages",
        "registry_versions",
        "first_date",
        "last_date",
        "max_generation",
        "mean_generation",
        "root_node",
        "root_basis",
        "approx",
    ]
    if tree.empty:
        return pd.DataFrame(columns=columns)
    work = tree.copy()
    work["is_registry"] = work["node_kind"].eq("registry_version")
    work["copy_generation"] = work["generation"].where(work["node_kind"].eq("copy_text"))
    stats = work.groupby("family_id", sort=False).agg(
        first_date=("node_date", "min"),
        last_date=("node_date", "max"),
        max_generation=("generation", "max"),
        mean_generation=("copy_generation", "mean"),
        approx=("approx", "max"),
        registry_versions=("is_registry", "sum"),
    )
    registry_rows = work.loc[work["is_registry"], ["family_id", "lineage_id"]]
    if registry_rows.empty:
        lineages = pd.Series(dtype="int64")
    else:
        lineages = registry_rows.groupby("family_id")["lineage_id"].nunique()
    stats["registry_lineages"] = lineages
    stats["registry_lineages"] = stats["registry_lineages"].fillna(0).astype("int64")
    stats["registry_versions"] = stats["registry_versions"].fillna(0).astype("int64")
    stats["max_generation"] = stats["max_generation"].astype("Int64")
    roots = _root_table(work)
    stats = stats.join(roots, how="left")
    return stats.reset_index().loc[:, columns]


def _root_table(tree: pd.DataFrame) -> pd.DataFrame:
    """The node with no parent. A registry root wins over a copy root."""
    missing = (
        tree["parent_id"].isna()
        | tree["parent_id"].astype(str).eq("")
        | tree["parent_id"].astype(str).eq("None")
    )
    roots = tree.loc[missing].copy()
    if roots.empty:
        return pd.DataFrame(columns=["root_node", "root_basis"])
    roots["registry_first"] = roots["node_kind"].eq("registry_version").astype(int)
    roots = roots.sort_values(
        ["family_id", "registry_first", "node_date", "node_id"],
        ascending=[True, False, True, True],
        kind="mergesort",
        na_position="last",
    )
    chosen = roots.drop_duplicates("family_id", keep="first").set_index("family_id")
    basis = np.where(
        chosen["node_kind"].eq("registry_version"),
        "registry",
        np.where(chosen["is_dated"].to_numpy(), "oldest", "most_copied"),
    )
    return pd.DataFrame(
        {"root_node": chosen["node_id"].to_numpy(), "root_basis": basis}, index=chosen.index
    )


def _split_skipped(
    families: pd.DataFrame,
    limit: int | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Families with more than ``limit`` texts are the skipped frame."""
    if limit is None or families.empty:
        return families, families.iloc[0:0]
    sizes = families.groupby("family_id").size()
    too_big = set(sizes[sizes > limit].index.astype(str))
    skipped = families.loc[families["family_id"].astype(str).isin(too_big)]
    kept = families.loc[~families["family_id"].astype(str).isin(too_big)]
    return kept, skipped


def _shas_that_need_signatures(families: pd.DataFrame) -> set[str]:
    """Copy texts in a source family or in a family of two or more texts."""
    if families.empty:
        return set()
    sizes = families.groupby("family_id").size()
    large = set(sizes[sizes > 1].index.astype(str))
    source = set(families.loc[families["family_kind"].isin(_SOURCE_KINDS), "family_id"].astype(str))
    needed = families.loc[families["family_id"].astype(str).isin(large | source)]
    if "registry_self" in needed.columns:
        needed = needed.loc[~needed["registry_self"].fillna(False).astype(bool)]
    return set(needed["file_sha"].astype(str))


def _print_skipped(skipped: pd.DataFrame) -> None:
    """Print how many families were left out, and the largest of them."""
    if skipped.empty:
        print("skipped families 0", flush=True)
        return
    table = (
        skipped.groupby("family_id", sort=False)
        .agg(
            family_kind=("family_kind", "first"),
            texts=("file_sha", "size"),
            repos=("repos", "first"),
        )
        .reset_index()
        .sort_values(["texts", "family_id"], ascending=[False, True])
    )
    print(f"skipped families {len(table)}", flush=True)
    print_sample_records(table, "largest skipped families", count=min(20, len(table)))


def _print_run_summary(tree: pd.DataFrame, summary: pd.DataFrame, elapsed: float) -> None:
    """Counts, depth, the ten biggest families, and two sample records."""
    numbers = _summary_numbers(tree, summary)
    numbers["seconds"] = round(elapsed, 1)
    print_summary("family tree summary", numbers)
    if not summary.empty:
        biggest = summary.sort_values(["texts", "family_id"], ascending=[False, True]).head(10)
        print("ten biggest families")
        for row in biggest.itertuples(index=False):
            print(
                f"{row.family_id} {row.family_kind} texts {int(row.texts)} "
                f"repos {int(row.repos)} depth {row.max_generation}"
            )
    print_sample_records(tree, "family tree sample")
    print_sample_records(summary, "family summary sample")
    _print_linked_sample(tree, summary)


def _print_saved_summary(dev: bool) -> None:
    """Print the summary from the files already on disk."""
    tree_path, summary_path = _output_paths(dev)
    tree = read_parquet(tree_path)
    summary = read_parquet(summary_path)
    _print_run_summary(tree, summary, 0.0)


def _summary_numbers(tree: pd.DataFrame, summary: pd.DataFrame) -> dict[str, object]:
    """Families per kind, medians, and depth. Singletons are reported apart."""
    numbers: dict[str, object] = {
        "tree_nodes": int(len(tree)),
        "summary_rows": int(len(summary)),
    }
    if summary.empty:
        return numbers
    for kind in ("source", "hub_only", "independent", "singleton"):
        numbers[f"{kind}_families"] = int(summary["family_kind"].eq(kind).sum())
    numbers["skipped_families"] = int(summary["root_basis"].eq("skipped").sum())
    numbers["approx_families"] = int(summary["approx"].fillna(False).astype(bool).sum())
    built = summary.loc[~summary["root_basis"].eq("skipped")]
    numbers.update(_depth_numbers(built, "all"))
    multi = built.loc[~built["family_kind"].eq("singleton")]
    numbers.update(_depth_numbers(multi, "without_singletons"))
    if not tree.empty:
        copies = tree.loc[tree["node_kind"].eq("copy_text")]
        numbers["copy_nodes"] = int(len(copies))
        numbers["dated_copy_nodes"] = int(copies["is_dated"].sum()) if not copies.empty else 0
    return numbers


def _depth_numbers(summary: pd.DataFrame, prefix: str) -> dict[str, object]:
    """Median texts and repos, and depth median, 90th percentile, and max."""
    if summary.empty:
        return {}
    depth = summary["max_generation"].dropna()
    numbers: dict[str, object] = {
        f"{prefix}_families": int(len(summary)),
        f"{prefix}_median_texts": float(summary["texts"].median()),
        f"{prefix}_median_repos": float(summary["repos"].median()),
    }
    if depth.empty:
        return numbers
    numbers[f"{prefix}_depth_median"] = float(depth.median())
    numbers[f"{prefix}_depth_p90"] = float(depth.quantile(0.9))
    numbers[f"{prefix}_depth_max"] = int(depth.max())
    return numbers


def _print_linked_sample(tree: pd.DataFrame, summary: pd.DataFrame) -> None:
    """Two nodes from the largest source family, so a parent link is visible."""
    if tree.empty or summary.empty:
        return
    source = summary.loc[summary["family_kind"].eq("source")].sort_values(
        ["texts", "family_id"], ascending=[False, True]
    )
    if source.empty:
        return
    family_id = source.iloc[0]["family_id"]
    sample = tree.loc[tree["family_id"].eq(family_id)].sort_values(
        ["generation", "node_date", "node_id"], kind="mergesort"
    )
    print_sample_records(sample, "largest source family", count=2)


def _dataset_rows(folder: Path, wanted: set[str]) -> tuple[list[str], np.ndarray]:
    """Dataset signature rows whose ids are in ``wanted``. Other rows are skipped."""
    width = settings.MINHASH_NUM_PERM
    if not wanted:
        return [], np.zeros((0, width), dtype=np.uint64)
    paths = sorted(folder.glob(PART_GLOB)) if folder.is_dir() else []
    kept_ids: list[str] = []
    blocks: list[np.ndarray] = []
    remaining = set(wanted)
    for part_index, path in enumerate(paths, start=1):
        if not remaining:
            break
        ids, matrix, _sizes = load_signatures(path)
        take = [index for index, name in enumerate(ids) if name in remaining]
        if take:
            chosen = [ids[index] for index in take]
            remaining.difference_update(chosen)
            kept_ids.extend(chosen)
            blocks.append(matrix[np.asarray(take, dtype=np.int64)])
        if part_index == 1 or part_index == len(paths) or part_index % 6 == 0:
            print(f"signature parts {part_index}/{len(paths)} kept {len(kept_ids)}", flush=True)
    if not kept_ids:
        return [], np.zeros((0, width), dtype=np.uint64)
    return kept_ids, np.vstack(blocks)


def _bundle_parts(
    registry_ids: list[str],
    registry_matrix: np.ndarray,
    copy_ids: list[str],
    copy_matrix: np.ndarray,
) -> dict[str, object]:
    """Stack registry and dataset signatures and index ``text:`` copy ids."""
    width = settings.MINHASH_NUM_PERM
    blocks = []
    ids: list[str] = []
    if len(registry_ids):
        ids.extend(str(name) for name in registry_ids)
        blocks.append(np.asarray(registry_matrix, dtype=np.uint64))
    if copy_ids:
        ids.extend(copy_ids)
        blocks.append(np.asarray(copy_matrix, dtype=np.uint64))
    if not blocks:
        matrix = np.zeros((0, width), dtype=np.uint64)
    elif len(blocks) == 1:
        matrix = blocks[0]
        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
    else:
        matrix = np.vstack(blocks)
    index: dict[str, int] = {}
    for position, name in enumerate(ids):
        index.setdefault(name, position)
    for name, position in list(index.items()):
        if "#" not in name:
            index.setdefault("text:" + name, position)
    return {"ids": ids, "signatures": matrix, "_index": index}


def _bundle(signatures: dict[str, object]) -> tuple[dict[str, int], np.ndarray]:
    """Return an id index and a uint64 matrix from either signature shape."""
    if "signatures" in signatures and "ids" in signatures:
        matrix = np.asarray(signatures["signatures"], dtype=np.uint64)
        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
        if matrix.size == 0:
            matrix = np.zeros((0, settings.MINHASH_NUM_PERM), dtype=np.uint64)
        cached = signatures.get("_index")
        if isinstance(cached, dict):
            return cached, matrix
        index = {}
        for position, name in enumerate(signatures["ids"]):
            index.setdefault(str(name), position)
        signatures["_index"] = index
        return index, matrix
    ids = [str(key) for key in signatures if not str(key).startswith("_")]
    if not ids:
        return {}, np.zeros((0, settings.MINHASH_NUM_PERM), dtype=np.uint64)
    matrix = np.vstack([np.asarray(signatures[key], dtype=np.uint64).reshape(-1) for key in ids])
    return {key: position for position, key in enumerate(ids)}, matrix


def _split_earlier(earlier_nodes: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """Registry rows, and dated copy rows ordered oldest first. Undated copies drop out."""
    if earlier_nodes.empty:
        return [], []
    frame = earlier_nodes.sort_values(
        ["node_date", "node_id"], kind="mergesort", na_position="last"
    )
    registries: list[dict] = []
    dated_members: list[dict] = []
    for row in frame.to_dict("records"):
        record = _as_record(row)
        if record["node_kind"] == "registry_version":
            registries.append(record)
        elif record["is_dated"]:
            dated_members.append(record)
    return registries, dated_members


def _as_record(member: object) -> dict:
    """The fields parent search needs, from a Series, a dict, or a node row."""
    if isinstance(member, pd.Series):
        data = member.to_dict()
    elif isinstance(member, dict):
        data = member
    else:
        raise TypeError("member must be a Series or a dict")
    node_date = data.get("node_date")
    is_dated = data.get("is_dated")
    if is_dated is None or pd.isna(is_dated):
        is_dated = _time_ns(node_date) is not None
    generation = data.get("generation")
    if generation is None or pd.isna(generation):
        generation = 0
    return {
        "node_id": str(data["node_id"]),
        "node_kind": str(data.get("node_kind") or "copy_text"),
        "file_sha": _clean_text(data.get("file_sha")),
        "is_dated": bool(is_dated),
        "time_ns": _time_ns(node_date),
        "generation": int(generation),
    }


def _query_vector(member: dict, index: dict[str, int], matrix: np.ndarray) -> np.ndarray | None:
    """The member's signature row, looked up by node id and then by file sha."""
    for key in (member["node_id"], member["file_sha"]):
        if key and key in index:
            return matrix[index[key]]
    return None


def _score_inputs(query: np.ndarray, matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Query as one uint64 row and matrix as uint64 ``(n, width)``."""
    query_row = np.asarray(query, dtype=np.uint64).reshape(-1)
    values = np.asarray(matrix, dtype=np.uint64)
    if values.ndim == 1:
        values = values.reshape(1, -1)
    if values.size == 0:
        width = int(query_row.size) or settings.MINHASH_NUM_PERM
        values = np.zeros((0, width), dtype=np.uint64)
    if values.shape[0] and query_row.size and values.shape[1] != query_row.size:
        raise ValueError(
            f"signature width {query_row.size} does not match stored width {values.shape[1]}"
        )
    return query_row, values


def _registry_rows(trunk: pd.DataFrame) -> list[dict]:
    """Trunk rows as dicts. An empty trunk yields an empty list."""
    if trunk.empty:
        return []
    return trunk.to_dict("records")


def _time_ns(value: object) -> int | None:
    """UTC nanoseconds, or None when the value is missing."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        return None
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    return int(stamp.value)


def _clean_text(value: object) -> str | None:
    """A string, or None when the cell is missing."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        return str(value)
    return str(value)


def _input_paths(dev: bool) -> dict[str, Path | str]:
    """Stage 8 inputs. Dataset signatures and artifacts are the full-data files."""
    folder = settings.data_dir(dev)
    return {
        "families": folder / "families.parquet",
        "copy_files": folder / "copy_files.parquet",
        "lineage_versions": folder / "lineage_versions.parquet",
        "lineage_roots": folder / "lineage_roots.parquet",
        "registry_signatures": folder / "registry_signatures.npz",
        "dataset_signatures": _dataset_dir(),
        "artifacts": settings.ARTIFACTS_GLOB,
    }


def _input_builders(dev: bool) -> dict[str, str]:
    """The command that creates each missing input."""
    flag = " --dev" if dev else ""
    families = f"python -m skill_genealogy.assign_families{flag}"
    return {
        "families": families,
        "copy_files": families,
        "lineage_versions": f"python -m skill_genealogy.build_registry_lineages{flag}",
        "lineage_roots": f"python -m skill_genealogy.find_copies{flag} --only rehost",
        "registry_signatures": f"python -m skill_genealogy.find_copies{flag} --only signatures",
        "dataset_signatures": "python -m skill_genealogy.find_copies --only signatures",
        "artifacts": "python src/data/download_data.py",
    }


def _output_paths(dev: bool) -> list[Path]:
    """The tree and the summary."""
    folder = settings.data_dir(dev)
    return [folder / "family_tree.parquet", folder / "family_summary.parquet"]


def _dataset_dir() -> Path:
    """Dataset signatures live in the full data folder for both modes."""
    return settings.data_dir(False) / "dataset_signatures"


def _groupby_if_column(frame: pd.DataFrame, column: str):
    """A groupby, or None when the column or the rows are missing."""
    if frame.empty or column not in frame.columns:
        return None
    return frame.groupby(column, sort=False)


def _group_or_empty(grouped, key: str, empty: pd.DataFrame) -> pd.DataFrame:
    """The group for ``key``, or ``empty`` when that key was not grouped."""
    if grouped is None or key not in grouped.groups:
        return empty
    return grouped.get_group(key)


def _versions_for_roots(grouped, family_roots: pd.DataFrame, empty: pd.DataFrame) -> pd.DataFrame:
    """Version rows for every lineage in this family's root table."""
    if family_roots.empty or grouped is None or "lineage_id" not in family_roots.columns:
        return empty
    parts = []
    for lineage_id in family_roots["lineage_id"].astype(str):
        if lineage_id in grouped.groups:
            parts.append(grouped.get_group(lineage_id))
    if not parts:
        return empty
    return pd.concat(parts, ignore_index=True)


def _attach_family_id(dates: pd.DataFrame, families: pd.DataFrame) -> pd.DataFrame:
    """Add ``family_id`` to loose date rows. Texts with no artifact stay in the frame."""
    if families.empty:
        return dates
    texts = families.loc[
        ~families["family_kind"].isin(_SOURCE_KINDS),
        ["file_sha", "family_id"],
    ].drop_duplicates("file_sha")
    if dates.empty:
        out = texts.copy()
        out["first_repo"] = None
        out["node_date"] = pd.NaT
        out["files"] = 0
        out["repos"] = 0
        return out
    merged = texts.merge(dates, on="file_sha", how="left")
    merged["files"] = merged["files"].fillna(0).astype("int64")
    merged["repos"] = merged["repos"].fillna(0).astype("int64")
    return merged


def _quote_path(path: str) -> str:
    """Return a single-quoted SQL string with quotes escaped."""
    return "'" + path.replace("'", "''") + "'"


def _register_view(
    connection: duckdb.DuckDBPyConnection, name: str, path: str, columns: str
) -> None:
    """Point ``name`` at a parquet file or glob, reading only ``columns``."""
    quoted = _quote_path(path)
    connection.execute(
        f"CREATE OR REPLACE VIEW {name} AS SELECT {columns} FROM read_parquet({quoted})"
    )


def _prepare_duckdb(connection: duckdb.DuckDBPyConnection, temp_dir: Path) -> None:
    """Give the artifact join a memory cap and a spill folder."""
    temp_dir.mkdir(parents=True, exist_ok=True)
    connection.execute("SET enable_progress_bar = false")
    connection.execute("SET preserve_insertion_order = false")
    connection.execute(f"SET memory_limit = '{settings.DUCKDB_MEMORY_LIMIT}'")
    connection.execute(f"SET max_temp_directory_size = '{settings.DUCKDB_TEMP_LIMIT}'")
    connection.execute(f"SET temp_directory = {_quote_path(str(temp_dir))}")


def _remove_tree(path: Path) -> None:
    """Delete a temporary folder if it is present."""
    if path.exists():
        shutil.rmtree(path)


if __name__ == "__main__":
    main()
