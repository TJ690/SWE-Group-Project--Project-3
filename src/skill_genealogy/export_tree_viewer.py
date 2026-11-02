"""Write a static page that draws family trees.

What: stage 12 picks the largest families, nests each tree, and copies the
viewer files into the results folder. A node keeps the stage 9 change state
and the stage 10 staleness of the first file in its repo.
Inputs: ``family_tree.parquet``, ``family_summary.parquet``,
``copy_files.parquet``, ``copy_changes.parquet``, ``copy_staleness.parquet``,
``repo_roles_final.parquet``, and ``registry_hubs.parquet`` in
``data_dir(dev)``.
Outputs: ``results_dir(dev)/tree_viewer/`` (HTML, JavaScript, CSS, and JSON).
The page loads D3 from a CDN when someone opens it. This stage does not.
Run: ``python -m skill_genealogy.export_tree_viewer --dev``
"""

from __future__ import annotations

import json
import math
import shutil
import time
from pathlib import Path

import pandas as pd

from skill_genealogy.file_helpers import (
    build_arg_parser,
    check_inputs_exist,
    connect_duckdb,
    print_sample_records,
    print_summary,
    read_parquet,
)
from skill_genealogy.settings import data_dir, results_dir

# Plan stage 12: the page lists this many families unless --top says otherwise.
DEFAULT_TOP_FAMILIES = 200
# Plan stage 12: a larger family is cut. The "+N more" marker is extra.
DEFAULT_MAX_NODES = 1000
# A dated-skill count is recovered from hub_share only inside this cap.
# Above it, several fractions can match the same float, so the sentence
# keeps the share instead of a guessed count.
_MAX_RECOVER_SKILLS = 5000
_PAGE_NAMES = ("index.html", "tree_viewer.js", "tree_viewer.css", "viewer_common.js")

# Query Q12.1, see QUERIES.md
FAMILY_SUMMARY_SQL = """
SELECT
    family_id,
    family_kind,
    texts,
    repos,
    max_generation
FROM read_parquet(?)
"""

# Query Q12.2, see QUERIES.md
FAMILY_TREE_SQL = """
SELECT
    tree.node_id AS node_id,
    tree.family_id AS family_id,
    tree.node_kind AS node_kind,
    tree.lineage_id AS lineage_id,
    tree.version_seq AS version_seq,
    tree.file_sha AS file_sha,
    tree.first_repo AS first_repo,
    cast(tree.node_date AS VARCHAR) AS node_date,
    tree.files AS files,
    tree.parent_id AS parent_id,
    tree.parent_similarity AS parent_similarity,
    tree.generation AS generation
FROM read_parquet(?) AS tree
JOIN chosen USING (family_id)
"""


def safe_file_name(identifier: str) -> str:
    """Turn a family id into one file name.

    ``/`` and ``::`` both become ``__``, so
    ``anthropics/skills::pdf`` becomes ``anthropics__skills__pdf``.
    The id that puts ``::`` in the last gap keeps that plain name.
    Any other mix of ``/`` and ``::`` adds ``~`` and a mark
    (``s`` for a slash, ``c`` for ``::``). ``a/b`` and ``a::b``
    therefore do not share a file. ``~`` is not used inside the name.

    Example:
    >>> safe_file_name("anthropics/skills::pdf")
    'anthropics__skills__pdf'
    >>> safe_file_name("a/b") != safe_file_name("a::b")
    True
    """
    parts, separators = _split_identifier(identifier)
    cleaned = [_clean_part(part) for part in parts]
    base = "__".join(cleaned).strip("._") or "family"
    if base in {".", ".."}:
        base = "family"
    if not separators:
        return base
    winner = "/".join(parts[:-1]) + "::" + parts[-1]
    if identifier == winner:
        return base
    mark = "".join("c" if separator == "::" else "s" for separator in separators)
    return f"{base}~{mark}"


def family_to_nested_json(
    tree: pd.DataFrame, node_info: pd.DataFrame, max_nodes: int
) -> dict:
    """Nest one family and keep at most ``max_nodes`` data nodes.

    Algorithm A49, see CODE_FLOW.md. Children are ordered by subtree
    size, then by file count, then by node id. A branch that does not
    fit is cut, and the omitted nodes become one ``+N more`` child.
    That child does not count toward ``max_nodes``. One root is the
    returned object. Several roots sit under a ``family`` node that
    also does not count.

    Inputs: ``tree`` rows for one family (``node_id``, ``parent_id``,
    and the display columns). ``node_info`` rows keyed by ``node_id``
    with role, path, link, stage 9 ``state``, and stage 10 ``stale``.
    ``max_nodes`` is the cap. Outputs: one JSON object.

    Example: a root, one child that itself has a child, and two leaves
    is five nodes. With ``max_nodes`` 3 the child branch is kept and
    the two leaves become ``+2 more``.
    """
    if max_nodes < 0:
        raise ValueError("max_nodes must be 0 or more")
    rows = _node_rows(tree, node_info)
    if not rows:
        return _more_node(0, 0)
    children, roots = _link_nodes(rows)
    sizes = _subtree_sizes(roots, children)
    total = sum(sizes[root] for root in roots)
    if max_nodes <= 0:
        return _more_node(total, 0)
    if len(roots) == 1:
        return _prune_node(roots[0], max_nodes, rows, children, sizes)
    kept, omitted = _take_branches(roots, max_nodes, rows, children, sizes)
    if omitted:
        kept.append(_more_node(omitted, 1))
    family_id = next(iter(rows.values())).get("family_id") or "family"
    return _family_node(str(family_id), kept)


def build_families_index(summary: pd.DataFrame, top_n: int, extra: list[str]) -> list[dict]:
    """Choose the largest families and any named extra family.

    Rows are ordered by ``texts`` descending, then ``family_id``.
    The first ``top_n`` are kept. Each id in ``extra`` is added when
    it is in the summary and not already chosen. ``depth`` is
    ``max_generation``. ``file`` is the JSON path under ``families/``.

    Inputs: the family summary, how many large families, and extra ids.
    Outputs: a list of ``id``, ``kind``, ``texts``, ``repos``, ``depth``,
    ``file``. An unknown extra id is left out.

    Example:
    >>> frame = pd.DataFrame([
    ...     {"family_id": "b", "family_kind": "source", "texts": 1,
    ...      "repos": 1, "max_generation": 0},
    ...     {"family_id": "a/x::y", "family_kind": "source", "texts": 3,
    ...      "repos": 2, "max_generation": 1},
    ... ])
    >>> build_families_index(frame, 1, ["b"])[1]["id"]
    'b'
    """
    if top_n < 0:
        raise ValueError("top_n must be 0 or more")
    if summary.empty or "family_id" not in summary.columns:
        return []
    work = summary.drop_duplicates("family_id").copy()
    work["texts"] = pd.to_numeric(work["texts"], errors="coerce").fillna(0)
    work = work.sort_values(["texts", "family_id"], ascending=[False, True], kind="mergesort")
    known = set(work["family_id"].astype(str))
    chosen: list[str] = []
    seen: set[str] = set()
    ranked = list(work["family_id"].astype(str).head(top_n))
    named = [str(item) for item in extra]
    for family_id in ranked + named:
        if family_id in seen or family_id not in known:
            continue
        seen.add(family_id)
        chosen.append(family_id)
    lookup = work.set_index(work["family_id"].astype(str))
    rows = []
    for family_id in chosen:
        record = lookup.loc[family_id]
        rows.append(
            {
                "id": family_id,
                "kind": None if _missing(record["family_kind"]) else str(record["family_kind"]),
                "texts": int(record["texts"]),
                "repos": _int(record["repos"]) or 0,
                "depth": _int(record["max_generation"]) or 0,
                "file": f"families/{safe_file_name(family_id)}.json",
            }
        )
    return rows


def build_repos_index(copy_files, repo_roles, exported_families) -> list[dict]:
    """List repos that appear in the exported families, with a hub sentence.

    A row in ``exported_families`` contributes its ``repo`` to that
    ``family_id``. ``copy_files`` adds every repo whose ``root_lineage``
    is one of those family ids (a source family is named by that
    lineage). Role, stars, skill count, and ``hub_share`` come from
    ``repo_roles``. The reason uses registry counts when they are on
    the row, dated-skill counts when those columns are present, and
    otherwise the share. ``hub_evidence`` is kept for the tooltip.

    Inputs: copy files, final repo roles, and a frame with
    ``family_id`` and ``repo``. Outputs: one dict per repo, sorted by
    repo name.

    Example: 31 re-hosted lineages out of 80 is a registry hub sentence.
    35 of 36 dated skills is the dataset-date sentence. See the tests.
    """
    pairs = _repo_family_pairs(copy_files, exported_families)
    if pairs.empty:
        return []
    roles = _prepare_roles(repo_roles)
    merged = pairs.merge(roles, on="repo", how="left")
    rows = []
    for repo, group in merged.groupby("repo", sort=True):
        record = group.iloc[0]
        families = sorted(set(group["family_id"].astype(str)))
        share = _float(record.get("hub_share"))
        rows.append(
            {
                "repo": str(repo),
                "role": _text(record.get("repo_role")),
                "stars": _int(record.get("stars")),
                "skills": _int(record.get("english_skills")),
                "hub_share": share,
                "hub_reason": _hub_reason(record),
                "hub_evidence": _text(record.get("hub_evidence")),
                "families": families,
            }
        )
    return rows


def export_tree_viewer(
    dev: bool, top_n: int, extra_families: list[str], max_nodes: int
) -> Path:
    """Write the tree viewer folder and return that path.

    A second call with the same ``top_n``, extra ids, and ``max_nodes``
    prints ``skip`` when the page and every family file are already
    there. ``--force`` is handled by ``main``, which clears the folder
    first.

    Inputs: ``dev`` selects the sample folders. ``top_n`` and
    ``extra_families`` choose families. ``max_nodes`` caps each tree.
    Outputs: ``results_dir(dev)/tree_viewer``.
    """
    started = time.perf_counter()
    extra = [str(family_id) for family_id in (extra_families or [])]
    folder = results_dir(dev) / "tree_viewer"
    options = {
        "top_n": int(top_n),
        "extra_families": sorted(extra),
        "max_nodes": int(max_nodes),
    }
    if _same_export(folder, options):
        _print_saved(folder, time.perf_counter() - started, skipped=True)
        return folder
    # Step 1: read the family tables and the role rows
    paths = read_inputs(dev)
    summary = _read_summary(paths["family_summary"])
    # Step 2: choose the largest families and any named family
    index = build_families_index(summary, top_n, extra)
    _avoid_file_collisions(index)
    _report_missing(extra, index)
    # Step 3: load those trees and the copy state
    family_ids = [row["id"] for row in index]
    tree = _read_trees(paths["family_tree"], family_ids)
    node_info = attach_copy_state(
        tree,
        read_parquet(paths["copy_files"]),
        read_parquet(paths["copy_changes"]),
        read_parquet(paths["copy_staleness"]),
        read_parquet(paths["repo_roles_final"]),
    )
    # Step 4: nest each family and cut it at max_nodes
    nested = _nest_families(tree, node_info, family_ids, max_nodes)
    # Step 5: repo index for repos in those families
    roles = _merge_registry_hubs(
        read_parquet(paths["repo_roles_final"]), read_parquet(paths["registry_hubs"])
    )
    repos = build_repos_index(
        read_parquet(paths["copy_files"]), roles, _exported_repo_rows(tree, family_ids)
    )
    # Step 6: write JSON and copy the page
    save_outputs(folder, index, nested, repos, options)
    _print_written(index, nested, repos, time.perf_counter() - started)
    return folder


def main() -> None:
    """Parse flags and write the tree viewer.

    ``--dev`` uses the sample folders. ``--top`` defaults to 200.
    ``--family`` may be repeated. ``--max-nodes`` defaults to 1000.
    ``--force`` deletes the viewer folder first. The numbered steps
    are in ``export_tree_viewer`` and match CODE_FLOW.md stage 12.
    """
    parser = build_arg_parser("write the family tree web page")
    parser.add_argument(
        "--top", type=int, default=DEFAULT_TOP_FAMILIES, help="largest families to export"
    )
    parser.add_argument("--family", action="append", default=[], help="also export this family id")
    parser.add_argument(
        "--max-nodes", type=int, default=DEFAULT_MAX_NODES, help="data nodes kept in one family"
    )
    args = parser.parse_args()
    if args.force:
        viewer = results_dir(args.dev) / "tree_viewer"
        if viewer.exists():
            shutil.rmtree(viewer)
    export_tree_viewer(args.dev, args.top, list(args.family or []), args.max_nodes)


def read_inputs(dev: bool) -> dict[str, Path]:
    """Return the stage 12 input paths after checking that each file exists.

    The error names the command that builds a missing file.
    """
    flag = " --dev" if dev else ""
    paths = {
        "family_summary": data_dir(dev) / "family_summary.parquet",
        "family_tree": data_dir(dev) / "family_tree.parquet",
        "copy_files": data_dir(dev) / "copy_files.parquet",
        "copy_changes": data_dir(dev) / "copy_changes.parquet",
        "copy_staleness": data_dir(dev) / "copy_staleness.parquet",
        "repo_roles_final": data_dir(dev) / "repo_roles_final.parquet",
        "registry_hubs": data_dir(dev) / "registry_hubs.parquet",
    }
    builders = {
        "family_summary": f"python -m skill_genealogy.build_family_trees{flag}",
        "family_tree": f"python -m skill_genealogy.build_family_trees{flag}",
        "copy_files": f"python -m skill_genealogy.assign_families{flag}",
        "copy_changes": f"python -m skill_genealogy.measure_copy_changes{flag} --only origin",
        "copy_staleness": f"python -m skill_genealogy.measure_staleness{flag}",
        "repo_roles_final": f"python -m skill_genealogy.find_copies{flag} --only rehost",
        "registry_hubs": f"python -m skill_genealogy.find_copies{flag} --only rehost",
    }
    check_inputs_exist(paths, builders)
    return paths


def attach_copy_state(
    tree: pd.DataFrame,
    copy_files: pd.DataFrame,
    copy_changes: pd.DataFrame,
    copy_staleness: pd.DataFrame,
    repo_roles: pd.DataFrame,
) -> pd.DataFrame:
    """Join each tree node to the role and the state of its first file.

    Query Q12.3, see QUERIES.md. The first file is the earliest path in
    ``copy_files`` for ``first_repo`` and ``file_sha``. Stage 9 ``state``
    and stage 10 ``copy_state`` join on that repo and path.
    ``versions_behind`` is the skill-text count plus the package count,
    which is the number the stage 10 state uses. A registry node has no
    copy row, so those fields stay empty.

    Inputs: the selected tree and the four tables. Outputs: one row per
    tree node, with ``node_id`` and the display fields.
    """
    if tree.empty:
        return pd.DataFrame(columns=["node_id"])
    nodes = tree.copy()
    nodes["node_id"] = nodes["node_id"].astype(str)
    nodes = _attach_roles(nodes, repo_roles)
    nodes = _attach_first_file(nodes, copy_files)
    nodes = _attach_change(nodes, copy_changes)
    nodes = _attach_staleness(nodes, copy_staleness)
    keep = [
        "node_id",
        "repo_role",
        "path",
        "link",
        "state",
        "stale",
        "versions_behind",
        "hub_evidence",
        "days_stale",
    ]
    for name in keep:
        if name not in nodes.columns:
            nodes[name] = None
    return nodes.loc[:, keep].drop_duplicates("node_id", keep="first").reset_index(drop=True)


def save_outputs(
    folder: Path,
    index: list[dict],
    nested: dict[str, dict],
    repos: list[dict],
    options: dict,
) -> None:
    """Copy the page and write the JSON files.

    Inputs: the viewer folder, the family index, one nested tree per
    family id, the repo index, and the options used for the skip check.
    Outputs: those files under ``folder``.
    """
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)
    _copy_page(folder)
    _write_json(folder / "families_index.json", index)
    _write_json(folder / "repos_index.json", repos)
    _write_json(folder / "export_options.json", options)
    for row in index:
        payload = nested.get(row["id"])
        if payload is None:
            payload = _more_node(0, 0)
        _write_json(folder / row["file"], payload)


def _split_identifier(identifier: str) -> tuple[list[str], list[str]]:
    """Split on ``/`` and ``::`` and return parts and separators."""
    parts: list[str] = []
    separators: list[str] = []
    buffer: list[str] = []
    index = 0
    text = "" if identifier is None else str(identifier)
    while index < len(text):
        if text.startswith("::", index):
            parts.append("".join(buffer))
            buffer = []
            separators.append("::")
            index += 2
            continue
        if text[index] == "/":
            parts.append("".join(buffer))
            buffer = []
            separators.append("/")
            index += 1
            continue
        buffer.append(text[index])
        index += 1
    parts.append("".join(buffer))
    return parts, separators


def _clean_part(part: str) -> str:
    """Keep letters, digits, dot, hyphen, and underscore."""
    cleaned = []
    for character in part:
        if character.isalnum() or character in ".-_":
            cleaned.append(character)
        else:
            cleaned.append("_")
    return "".join(cleaned)


def _node_rows(tree: pd.DataFrame, node_info: pd.DataFrame) -> dict[str, dict]:
    """Merge tree columns and node info into one dict per node id."""
    details = {}
    if node_info is not None and not node_info.empty and "node_id" in node_info.columns:
        for record in node_info.to_dict("records"):
            details[str(record["node_id"])] = record
    rows: dict[str, dict] = {}
    if tree is None or tree.empty or "node_id" not in tree.columns:
        return rows
    for record in tree.to_dict("records"):
        node_id = str(record.get("node_id"))
        if node_id in rows:
            continue
        rows[node_id] = _merged_row(record, details.get(node_id, {}))
    return rows


def _merged_row(record: dict, extra: dict) -> dict:
    """One display row. Stage 9 and stage 10 fields come from ``extra``."""
    version = _int(record.get("version_seq"))
    kind = _text(record.get("node_kind")) or "copy_text"
    repo = _text(extra.get("repo")) or _text(record.get("first_repo"))
    path = _text(extra.get("path"))
    if path is None and kind == "registry_version":
        path = _registry_path(record.get("lineage_id"))
    return {
        "node_id": str(record.get("node_id")),
        "family_id": _text(record.get("family_id")),
        "parent_id": None if _missing(record.get("parent_id")) else str(record.get("parent_id")),
        "node_kind": kind,
        "name": _display_name(kind, version, repo, str(record.get("node_id"))),
        "repo": repo,
        "repo_role": _text(extra.get("repo_role")),
        "date": _day(record.get("node_date")),
        "similarity": _float(record.get("parent_similarity")),
        "generation": _int(record.get("generation")) or 0,
        "files": _int(record.get("files")) or 0,
        "link": _text(extra.get("link")),
        "state": _text(extra.get("state")),
        "stale": _text(extra.get("stale")),
        "versions_behind": _int(extra.get("versions_behind")),
        "path": path,
        "hub_evidence": _text(extra.get("hub_evidence")),
        "days_stale": _int(extra.get("days_stale")),
    }


def _display_name(kind: str, version: int | None, repo: str | None, node_id: str) -> str:
    """Registry nodes are ``vN``. Copy nodes use the repo name."""
    if kind == "registry_version" and version is not None:
        return f"v{version}"
    if repo:
        return repo
    return node_id


def _registry_path(lineage_id: object) -> str | None:
    """The skill path implied by ``repo::folder``."""
    text = _text(lineage_id)
    if text is None or "::" not in text:
        return None
    folder = text.split("::", 1)[1]
    if folder.endswith("SKILL.md"):
        return folder
    return folder.rstrip("/") + "/SKILL.md"


def _link_nodes(rows: dict[str, dict]) -> tuple[dict[str, list[str]], list[str]]:
    """Build child lists. A cycle edge is dropped so every node is reachable."""
    children = {node_id: [] for node_id in rows}
    for node_id, row in rows.items():
        parent = row["parent_id"]
        if parent in children and parent != node_id:
            children[parent].append(node_id)
    color: dict[str, str] = {}

    def visit(node_id: str) -> None:
        color[node_id] = "grey"
        kept = []
        for child in children[node_id]:
            state = color.get(child)
            if state == "grey":
                continue
            if state != "black":
                visit(child)
            kept.append(child)
        children[node_id] = kept
        color[node_id] = "black"

    for node_id in list(rows):
        if node_id not in color:
            visit(node_id)
    child_ids = {child for kids in children.values() for child in kids}
    roots = [node_id for node_id in rows if node_id not in child_ids]
    return children, roots


def _subtree_sizes(roots: list[str], children: dict[str, list[str]]) -> dict[str, int]:
    """Return how many nodes sit in each subtree, including the node."""
    sizes: dict[str, int] = {}

    def walk(node_id: str) -> int:
        if node_id in sizes:
            return sizes[node_id]
        sizes[node_id] = 0
        total = 1 + sum(walk(child) for child in children.get(node_id, []))
        sizes[node_id] = total
        return total

    for root in roots:
        walk(root)
    for node_id in children:
        if node_id not in sizes:
            walk(node_id)
    return sizes


def _branch_key(node_id: str, rows: dict[str, dict], sizes: dict[str, int]) -> tuple:
    """Largest subtree, then most files, then node id."""
    return (-sizes[node_id], -int(rows[node_id]["files"] or 0), node_id)


def _prune_node(
    node_id: str,
    budget: int,
    rows: dict[str, dict],
    children: dict[str, list[str]],
    sizes: dict[str, int],
) -> dict:
    """Keep this node and the largest child branches that fit in ``budget``."""
    if sizes[node_id] <= budget:
        return _emit_all(node_id, rows, children, sizes)
    kept, omitted = _take_branches(children[node_id], budget - 1, rows, children, sizes)
    generation = int(rows[node_id]["generation"]) + 1
    if omitted:
        kept.append(_more_node(omitted, generation))
    return _node_object(rows[node_id], kept)


def _take_branches(
    node_ids: list[str],
    budget: int,
    rows: dict[str, dict],
    children: dict[str, list[str]],
    sizes: dict[str, int],
) -> tuple[list[dict], int]:
    """Keep whole branches until the budget runs out. Return omitted nodes."""
    ordered = sorted(node_ids, key=lambda node_id: _branch_key(node_id, rows, sizes))
    kept: list[dict] = []
    omitted = 0
    remaining = budget
    index = 0
    while index < len(ordered):
        node_id = ordered[index]
        size = sizes[node_id]
        if remaining > 0 and size <= remaining:
            kept.append(_emit_all(node_id, rows, children, sizes))
            remaining -= size
            index += 1
            continue
        if remaining > 0:
            kept.append(_prune_node(node_id, remaining, rows, children, sizes))
            remaining = 0
            index += 1
            omitted += sum(sizes[sibling] for sibling in ordered[index:])
            break
        omitted += size
        index += 1
    return kept, omitted


def _emit_all(
    node_id: str,
    rows: dict[str, dict],
    children: dict[str, list[str]],
    sizes: dict[str, int],
) -> dict:
    """The full subtree, children ordered like the cut."""
    ordered = sorted(children[node_id], key=lambda child: _branch_key(child, rows, sizes))
    kids = [_emit_all(child, rows, children, sizes) for child in ordered]
    return _node_object(rows[node_id], kids)


def _node_object(row: dict, children: list[dict]) -> dict:
    """The JSON object for one data node."""
    payload = {
        "name": row["name"],
        "node_kind": row["node_kind"],
        "repo": row["repo"],
        "repo_role": row["repo_role"],
        "date": row["date"],
        "similarity": row["similarity"],
        "generation": row["generation"],
        "files": row["files"],
        "link": row["link"],
        "state": row["state"],
        "stale": row["stale"],
        "versions_behind": row["versions_behind"],
        "path": row["path"],
        "hub_evidence": row["hub_evidence"],
        "days_stale": row["days_stale"],
        "children": children,
    }
    return payload


def _more_node(omitted: int, generation: int) -> dict:
    """The cut marker. ``omitted`` is how many data nodes were left out."""
    return {
        "name": f"+{omitted} more",
        "node_kind": "more",
        "repo": None,
        "repo_role": None,
        "date": None,
        "similarity": None,
        "generation": generation,
        "files": None,
        "link": None,
        "state": None,
        "stale": None,
        "versions_behind": None,
        "path": None,
        "hub_evidence": None,
        "days_stale": None,
        "children": [],
        "omitted": int(omitted),
    }


def _family_node(family_id: str, children: list[dict]) -> dict:
    """A parent for a family that has more than one root."""
    files = sum(int(child["files"] or 0) for child in children if child.get("node_kind") != "more")
    return {
        "name": family_id,
        "node_kind": "family",
        "repo": None,
        "repo_role": None,
        "date": None,
        "similarity": None,
        "generation": 0,
        "files": files,
        "link": None,
        "state": None,
        "stale": None,
        "versions_behind": None,
        "path": None,
        "hub_evidence": None,
        "days_stale": None,
        "children": children,
    }


def _repo_family_pairs(copy_files: pd.DataFrame, exported_families) -> pd.DataFrame:
    """One row per repo and family id, from the tree and from copy files."""
    frames = []
    if isinstance(exported_families, pd.DataFrame) and {"family_id", "repo"}.issubset(
        exported_families.columns
    ):
        tree_pairs = exported_families.loc[:, ["family_id", "repo"]].copy()
        frames.append(tree_pairs)
    family_ids: set[str] = set()
    if isinstance(exported_families, pd.DataFrame) and "family_id" in exported_families.columns:
        family_ids.update(exported_families["family_id"].dropna().astype(str))
    if (
        isinstance(copy_files, pd.DataFrame)
        and {"repo", "root_lineage"}.issubset(copy_files.columns)
        and family_ids
    ):
        copies = copy_files.loc[:, ["repo", "root_lineage"]].dropna()
        copies = copies.loc[copies["root_lineage"].astype(str).isin(family_ids)]
        if not copies.empty:
            frames.append(
                pd.DataFrame(
                    {
                        "family_id": copies["root_lineage"].astype(str),
                        "repo": copies["repo"].astype(str),
                    }
                )
            )
    if not frames:
        return pd.DataFrame(columns=["family_id", "repo"])
    pairs = pd.concat(frames, ignore_index=True)
    pairs = pairs.dropna(subset=["repo", "family_id"])
    pairs["repo"] = pairs["repo"].astype(str)
    pairs["family_id"] = pairs["family_id"].astype(str)
    pairs = pairs.loc[pairs["repo"].ne("") & pairs["repo"].ne("None")]
    return pairs.drop_duplicates().reset_index(drop=True)


def _prepare_roles(repo_roles: pd.DataFrame) -> pd.DataFrame:
    """Role columns used by the repo index. Missing columns stay empty."""
    if repo_roles is None or repo_roles.empty or "repo" not in repo_roles.columns:
        return pd.DataFrame(columns=["repo"])
    roles = repo_roles.drop_duplicates("repo").copy()
    roles["repo"] = roles["repo"].astype(str)
    return roles


def _merge_registry_hubs(repo_roles: pd.DataFrame, registry_hubs: pd.DataFrame) -> pd.DataFrame:
    """Add registry hub counts onto the role rows. Dataset-date rows are unchanged."""
    if registry_hubs is None or registry_hubs.empty or "repo" not in registry_hubs.columns:
        return repo_roles
    keep = [
        name
        for name in ("repo", "hub_reason", "english_lineages", "rehosted_lineages")
        if name in registry_hubs.columns
    ]
    hubs = registry_hubs.loc[:, keep].drop_duplicates("repo")
    hubs["repo"] = hubs["repo"].astype(str)
    roles = repo_roles.copy()
    roles["repo"] = roles["repo"].astype(str)
    return roles.merge(hubs, on="repo", how="left", suffixes=("", "_registry"))


def _hub_reason(record: pd.Series) -> str | None:
    """The hub sentence for one repo, or None when the repo was not scored.

    Registries use ``N of M skills first appeared in older registries``.
    Other repos use ``N of M dated skills first appeared in older repos``
    when the counts are known or the share matches one fraction.
    A share that matches more than one fraction stays a share.
    """
    is_hub = _is_hub(record)
    ending = "hub" if is_hub else "not a hub"
    evidence = _text(record.get("hub_evidence"))
    dated = _pair(record.get("first_seen_elsewhere"), record.get("skills_tested"))
    if evidence == "dataset_dates" and dated is not None:
        return f"{dated[0]} of {dated[1]} dated skills first appeared in older repos -> {ending}"
    stored = _text(record.get("hub_reason"))
    if evidence == "registry_history" and stored:
        return stored
    rehosted = _pair(record.get("rehosted_lineages"), record.get("english_lineages"))
    if evidence == "registry_history" and rehosted is not None:
        return (
            f"{rehosted[0]} of {rehosted[1]} skills first appeared in older registries -> {ending}"
        )
    if evidence == "dataset_dates":
        share = _float(record.get("hub_share"))
        skills = _int(record.get("english_skills"))
        recovered = _dated_skill_counts(share, skills)
        if recovered is not None:
            return (
                f"{recovered[0]} of {recovered[1]} dated skills "
                f"first appeared in older repos -> {ending}"
            )
        return _share_sentence(record.get("hub_share"), "dated skills", "older repos", ending)
    if evidence == "registry_history":
        return _share_sentence(record.get("hub_share"), "skills", "older registries", ending)
    return None


def _is_hub(record: pd.Series) -> bool:
    """True when the row is a hub or a registry hub."""
    if "is_hub" in record.index and not _missing(record.get("is_hub")):
        return bool(record.get("is_hub"))
    return _text(record.get("repo_role")) in {"hub", "registry_hub"}


def _pair(numerator: object, denominator: object) -> tuple[int, int] | None:
    """Two counts, or None when either is missing. A zero denominator is None."""
    first = _int(numerator)
    second = _int(denominator)
    if first is None or second is None or second <= 0:
        return None
    return first, second


def _dated_skill_counts(share: float | None, english_skills: int | None) -> tuple[int, int] | None:
    """Return the one fraction of dated skills that equals ``share``.

    The denominator runs from 5 to ``english_skills``, because a hub
    test needs at least five dated texts and cannot date more texts
    than the repo has. Two matching fractions return None.

    Example:
    >>> _dated_skill_counts(35 / 36, 39)
    (35, 36)
    >>> _dated_skill_counts(0.25, 17) is None
    True
    """
    if share is None or english_skills is None:
        return None
    if math.isnan(share) or english_skills < 5 or english_skills > _MAX_RECOVER_SKILLS:
        return None
    found: tuple[int, int] | None = None
    for tested in range(5, int(english_skills) + 1):
        elsewhere = int(round(share * tested))
        if elsewhere < 0 or elsewhere > tested:
            continue
        if abs(share * tested - elsewhere) < 1e-6:
            if found is not None:
                return None
            found = (elsewhere, tested)
    return found


def _share_sentence(share: object, skills: str, older: str, ending: str) -> str | None:
    """A reason that names the share when the counts are not known."""
    value = _float(share)
    if value is None:
        return None
    if value == 0:
        return f"0 {skills} first appeared in {older} -> {ending}"
    return f"share {value:.3f} of {skills} first appeared in {older} -> {ending}"


def _read_summary(path: Path) -> pd.DataFrame:
    """Family summary columns the index needs. Query Q12.1."""
    connection = connect_duckdb()
    try:
        return connection.execute(FAMILY_SUMMARY_SQL, [str(path)]).fetchdf()
    finally:
        connection.close()


def _read_trees(path: Path, family_ids: list[str]) -> pd.DataFrame:
    """Tree rows for the chosen families. Query Q12.2."""
    columns = [
        "node_id",
        "family_id",
        "node_kind",
        "lineage_id",
        "version_seq",
        "file_sha",
        "first_repo",
        "node_date",
        "files",
        "parent_id",
        "parent_similarity",
        "generation",
    ]
    if not family_ids:
        return pd.DataFrame(columns=columns)
    connection = connect_duckdb()
    try:
        connection.register("chosen", pd.DataFrame({"family_id": family_ids}))
        frame = connection.execute(FAMILY_TREE_SQL, [str(path)]).fetchdf()
    finally:
        connection.close()
    if frame.empty:
        return pd.DataFrame(columns=columns)
    return frame


def _attach_roles(nodes: pd.DataFrame, repo_roles: pd.DataFrame) -> pd.DataFrame:
    """Add ``repo_role`` and ``hub_evidence`` from the node's repo."""
    if repo_roles is None or repo_roles.empty or "repo" not in repo_roles.columns:
        nodes["repo_role"] = None
        nodes["hub_evidence"] = None
        return nodes
    keep = [name for name in ("repo", "repo_role", "hub_evidence") if name in repo_roles.columns]
    lookup = repo_roles.loc[:, keep].drop_duplicates("repo").copy()
    lookup["repo"] = lookup["repo"].astype(str)
    work = nodes.copy()
    work["_repo_key"] = work["first_repo"].map(_text)
    merged = work.merge(lookup, left_on="_repo_key", right_on="repo", how="left")
    return merged.drop(columns=["_repo_key", "repo"], errors="ignore")


def _attach_first_file(nodes: pd.DataFrame, copy_files: pd.DataFrame) -> pd.DataFrame:
    """Add the earliest path and link for this repo and text."""
    needed = {"repo", "path", "file_sha"}
    if copy_files is None or copy_files.empty or not needed.issubset(copy_files.columns):
        nodes["path"] = None
        nodes["link"] = None
        return nodes
    names = [name for name in ("repo", "path", "file_sha", "link") if name in copy_files.columns]
    files = copy_files.loc[:, names]
    files = files.dropna(subset=["repo", "file_sha"]).copy()
    files["_repo_key"] = files["repo"].map(_text)
    files["_sha_key"] = files["file_sha"].map(_sha_key)
    files = files.dropna(subset=["_repo_key", "_sha_key"])
    files = files.sort_values(["_repo_key", "_sha_key", "path"], kind="mergesort")
    files = files.drop_duplicates(["_repo_key", "_sha_key"], keep="first")
    work = nodes.copy()
    work["_repo_key"] = work["first_repo"].map(_text)
    work["_sha_key"] = work["file_sha"].map(_sha_key)
    file_cols = ["_repo_key", "_sha_key", "path"]
    if "link" in files.columns:
        file_cols.append("link")
    merged = work.merge(files.loc[:, file_cols], on=["_repo_key", "_sha_key"], how="left")
    if "link" not in merged.columns:
        merged["link"] = None
    return merged.drop(columns=["_repo_key", "_sha_key"])


def _attach_change(nodes: pd.DataFrame, copy_changes: pd.DataFrame) -> pd.DataFrame:
    """Add stage 9 ``state`` for the first file."""
    if (
        copy_changes is None
        or copy_changes.empty
        or not {"repo", "path", "state"}.issubset(copy_changes.columns)
        or "path" not in nodes.columns
    ):
        nodes["state"] = None
        return nodes
    changes = copy_changes.loc[:, ["repo", "path", "state"]].dropna(subset=["repo", "path"]).copy()
    changes["_repo_key"] = changes["repo"].map(_text)
    changes["_path_key"] = changes["path"].map(_text)
    changes = changes.drop_duplicates(["_repo_key", "_path_key"], keep="first")
    work = nodes.copy()
    work["_repo_key"] = work["first_repo"].map(_text)
    work["_path_key"] = work["path"].map(_text)
    merged = work.merge(
        changes.loc[:, ["_repo_key", "_path_key", "state"]],
        on=["_repo_key", "_path_key"],
        how="left",
    )
    return merged.drop(columns=["_repo_key", "_path_key"])


def _attach_staleness(nodes: pd.DataFrame, copy_staleness: pd.DataFrame) -> pd.DataFrame:
    """Add stage 10 state, versions behind, and days stale."""
    nodes["stale"] = None
    nodes["versions_behind"] = None
    nodes["days_stale"] = None
    needed = {"repo", "path", "copy_state"}
    if (
        copy_staleness is None
        or copy_staleness.empty
        or not needed.issubset(copy_staleness.columns)
        or "path" not in nodes.columns
    ):
        return nodes
    columns = ["repo", "path", "copy_state"]
    for name in ("skill_md_versions_behind", "package_versions_behind", "days_stale"):
        if name in copy_staleness.columns:
            columns.append(name)
    stale = copy_staleness.loc[:, columns].dropna(subset=["repo", "path"]).copy()
    stale["_repo_key"] = stale["repo"].map(_text)
    stale["_path_key"] = stale["path"].map(_text)
    stale = stale.drop_duplicates(["_repo_key", "_path_key"], keep="first")
    work = nodes.drop(columns=["stale", "versions_behind", "days_stale"]).copy()
    work["_repo_key"] = work["first_repo"].map(_text)
    work["_path_key"] = work["path"].map(_text)
    stale_rows = stale.drop(columns=["repo", "path"])
    merged = work.merge(stale_rows, on=["_repo_key", "_path_key"], how="left")
    has = merged["copy_state"].notna()
    skill = _numeric_column(merged, "skill_md_versions_behind")
    package = _numeric_column(merged, "package_versions_behind")
    merged["versions_behind"] = (skill + package).where(has)
    merged["stale"] = merged["copy_state"]
    if "days_stale" not in merged.columns:
        merged["days_stale"] = None
    else:
        merged["days_stale"] = _numeric_column(merged, "days_stale").where(has)
    return merged.drop(columns=["_repo_key", "_path_key", "copy_state"], errors="ignore")


def _numeric_column(frame: pd.DataFrame, name: str) -> pd.Series:
    """A numeric column, or zeros when the column is absent."""
    if name not in frame.columns:
        return pd.Series(0, index=frame.index, dtype="float64")
    return pd.to_numeric(frame[name], errors="coerce").fillna(0)


def _nest_families(
    tree: pd.DataFrame, node_info: pd.DataFrame, family_ids: list[str], max_nodes: int
) -> dict[str, dict]:
    """Cut each chosen family. A family with no rows becomes ``+0 more``."""
    grouped = {}
    if not tree.empty:
        for family_id, group in tree.groupby(tree["family_id"].astype(str), sort=False):
            grouped[str(family_id)] = group
    info_grouped = {}
    if not node_info.empty:
        # node_info has no family_id; the tree node_id is already unique.
        info_grouped = {"all": node_info}
    nested = {}
    for family_id in family_ids:
        group = grouped.get(family_id, pd.DataFrame())
        info = info_grouped.get("all", pd.DataFrame())
        if not group.empty and not info.empty:
            ids = set(group["node_id"].astype(str))
            info = info.loc[info["node_id"].astype(str).isin(ids)]
        nested[family_id] = family_to_nested_json(group, info, max_nodes)
    return nested


def _exported_repo_rows(tree: pd.DataFrame, family_ids: list[str]) -> pd.DataFrame:
    """Repos named on the exported tree nodes."""
    if tree.empty or "first_repo" not in tree.columns:
        return pd.DataFrame({"family_id": family_ids, "repo": [None] * len(family_ids)})
    rows = tree.loc[:, ["family_id", "first_repo"]].rename(columns={"first_repo": "repo"})
    rows = rows.loc[rows["family_id"].astype(str).isin(family_ids)]
    return rows


def _copy_page(folder: Path) -> None:
    """Copy the four viewer files next to the JSON."""
    package = Path(__file__).resolve().parent
    sources = {
        "viewer_common.js": package / "viewer_common.js",
        "index.html": package / "tree_viewer" / "index.html",
        "tree_viewer.js": package / "tree_viewer" / "tree_viewer.js",
        "tree_viewer.css": package / "tree_viewer" / "tree_viewer.css",
    }
    for name, source in sources.items():
        shutil.copyfile(source, folder / name)


def _same_export(folder: Path, options: dict) -> bool:
    """True when this folder was written with the same options and is complete."""
    names = (*_PAGE_NAMES, "families_index.json", "repos_index.json", "export_options.json")
    needed = [folder / name for name in names]
    if not all(path.is_file() for path in needed):
        return False
    try:
        saved = json.loads((folder / "export_options.json").read_text(encoding="utf-8"))
        index = json.loads((folder / "families_index.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if saved != options:
        return False
    return all((folder / row["file"]).is_file() for row in index)


def _avoid_file_collisions(index: list[dict]) -> None:
    """Give a family its own file when two ids clean to the same name."""
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


def _report_missing(extra: list[str], index: list[dict]) -> None:
    """Print named families that are not in the summary."""
    found = {row["id"] for row in index}
    missing = [family_id for family_id in extra if family_id not in found]
    if missing:
        print("named families not in the summary: " + ", ".join(missing))


def _print_saved(folder: Path, seconds: float, skipped: bool) -> None:
    """Print the counts already stored in the viewer folder."""
    if skipped:
        print("skip")
    index = json.loads((folder / "families_index.json").read_text(encoding="utf-8"))
    repos = json.loads((folder / "repos_index.json").read_text(encoding="utf-8"))
    print_summary(
        "tree viewer",
        {"families": len(index), "repos": len(repos), "seconds": round(seconds, 1)},
    )
    print_sample_records(pd.DataFrame(index), "families index", 2)


def _print_written(
    index: list[dict], nested: dict[str, dict], repos: list[dict], seconds: float
) -> None:
    """Print how many families, nodes, and repos were written, plus two rows."""
    kept, omitted = _count_nodes(list(nested.values()))
    print_summary(
        "tree viewer",
        {
            "families": len(index),
            "nodes_kept": kept,
            "nodes_omitted": omitted,
            "repos": len(repos),
            "seconds": round(seconds, 1),
        },
    )
    print_sample_records(pd.DataFrame(index), "families index", 2)
    if repos:
        sample = pd.DataFrame(repos).drop(columns=["families"], errors="ignore")
        sample = sample.astype(object)
        sample = sample.where(sample.notna(), None)
        print_sample_records(sample, "repos index", 2)


def _count_nodes(nodes: list[dict]) -> tuple[int, int]:
    """Data nodes kept, and data nodes named by ``+N more`` markers."""
    kept = 0
    omitted = 0
    pending = list(nodes)
    while pending:
        node = pending.pop()
        if node.get("node_kind") == "more":
            omitted += int(node.get("omitted") or 0)
        else:
            kept += 1
        pending.extend(node.get("children") or [])
    return kept, omitted


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
        return _json_ready(value.item())
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    return str(value)


def _sha_key(value: object) -> str | None:
    """Lower-case blob id, or None when the cell is empty."""
    text = _text(value)
    if text is None:
        return None
    return text.lower()


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
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: object) -> float | None:
    """A float cell, or None when it is missing or not a number."""
    if _missing(value):
        return None
    try:
        number = float(value)
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
    except TypeError:
        return False
    if isinstance(missing, bool):
        return missing
    return False


if __name__ == "__main__":
    main()
