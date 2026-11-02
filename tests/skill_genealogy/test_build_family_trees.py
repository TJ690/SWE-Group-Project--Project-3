"""Hand-made families for trunks, parent links, the member cap, and summaries."""

from __future__ import annotations

import numpy as np
import pandas as pd

from skill_genealogy.build_family_trees import (
    TREE_COLUMNS,
    _build_leaf_nodes,
    build_member_nodes,
    build_one_family_tree,
    build_trunk_nodes,
    copy_node_id,
    pick_parent_for_member,
    registry_node_id,
    score_candidates,
    score_candidates_simple,
    summarize_families,
)

_LINEAGE = "plain/reg::skills/a"
_OTHER = "other/app::skills/a"


def _hash(*values: int) -> np.ndarray:
    """One small MinHash row. Tests use width 4 so the shares are easy to check."""
    return np.array(values, dtype=np.uint64)


def _versions() -> pd.DataFrame:
    """v1 on 10 Jan, v2 on 1 Mar."""
    return pd.DataFrame(
        {
            "lineage_id": [_LINEAGE, _LINEAGE],
            "version_seq": [1, 2],
            "blob": ["blob-v1", "blob-v2"],
            "release_date": ["2026-01-10", "2026-03-01"],
        }
    )


def _roots() -> pd.DataFrame:
    """One original lineage. ``family_id`` is this root."""
    return pd.DataFrame(
        {
            "lineage_id": [_LINEAGE],
            "root_lineage": [_LINEAGE],
            "copied_from_lineage": [None],
            "copied_from_seq": [pd.NA],
        }
    )


def _texts(shas: list[str], kind: str = "source") -> pd.DataFrame:
    """One family row per text."""
    family_id = _LINEAGE if kind in ("source", "hub_only") else "cluster:1"
    return pd.DataFrame(
        {
            "file_sha": shas,
            "family_id": family_id,
            "family_kind": kind,
            "registry_self": False,
            "files": 4,
            "repos": 3,
            "owners": 2,
        }
    )


def _files(rows: list[tuple[str, str, str | None]]) -> pd.DataFrame:
    """Copy-file rows ``(file_sha, repo, first_commit_date)``."""
    return pd.DataFrame(
        {
            "file_sha": [row[0] for row in rows],
            "repo": [row[1] for row in rows],
            "first_commit_date": [row[2] for row in rows],
        }
    )


def _chain_signatures() -> dict[str, np.ndarray]:
    """A near v1, B near v2, C the same as A, D the same as v2."""
    return {
        registry_node_id(_LINEAGE, 1): _hash(0, 0, 0, 0),
        registry_node_id(_LINEAGE, 2): _hash(1, 1, 1, 1),
        copy_node_id("A"): _hash(0, 0, 0, 1),
        copy_node_id("B"): _hash(1, 1, 1, 0),
        copy_node_id("C"): _hash(0, 0, 0, 1),
        copy_node_id("D"): _hash(1, 1, 1, 1),
    }


def _chain_tree(max_earlier: int = 2000) -> pd.DataFrame:
    """v1, v2, A in Feb, B in Apr, C in May, undated D."""
    files = _files(
        [
            ("A", "r/a", "2026-02-01"),
            ("B", "r/b", "2026-04-01"),
            ("C", "r/c", "2026-05-01"),
            ("D", "r/d", None),
        ]
    )
    return build_one_family_tree(
        _LINEAGE,
        _texts(["A", "B", "C", "D"]),
        files,
        _versions(),
        _roots(),
        _chain_signatures(),
        max_earlier=max_earlier,
    )


def _parent(tree: pd.DataFrame, file_sha: str) -> str:
    """Parent node id of one copy text."""
    return tree.loc[tree["file_sha"].eq(file_sha), "parent_id"].iloc[0]


def _generation(tree: pd.DataFrame, file_sha: str) -> int:
    """Generation of one copy text."""
    return int(tree.loc[tree["file_sha"].eq(file_sha), "generation"].iloc[0])


def test_copies_follow_v1_then_v2_then_a() -> None:
    """A copies v1, B copies v2, C is nearer A than the trunk. Generations 1, 1, 2."""
    tree = _chain_tree()
    assert list(tree.columns) == TREE_COLUMNS
    assert _parent(tree, "A") == registry_node_id(_LINEAGE, 1)
    assert _parent(tree, "B") == registry_node_id(_LINEAGE, 2)
    assert _parent(tree, "C") == copy_node_id("A")
    assert _generation(tree, "A") == 1
    assert _generation(tree, "B") == 1
    assert _generation(tree, "C") == 2
    trunk = tree.loc[tree["node_kind"].eq("registry_version")]
    assert set(trunk["generation"]) == {0}
    assert trunk.loc[trunk["version_seq"].eq(2), "parent_id"].iloc[0] == registry_node_id(
        _LINEAGE, 1
    )


def test_undated_member_attaches_and_has_no_children() -> None:
    """D matches v2 and is placed last, so nothing can choose D as a parent."""
    tree = _chain_tree()
    assert _parent(tree, "D") == registry_node_id(_LINEAGE, 2)
    assert float(tree.loc[tree["file_sha"].eq("D"), "parent_similarity"].iloc[0]) == 1.0
    assert not tree["parent_id"].eq(copy_node_id("D")).any()
    assert not bool(tree.loc[tree["file_sha"].eq("D"), "is_dated"].iloc[0])


def test_cap_keeps_only_the_latest_earlier_member() -> None:
    """With cap 1, C cannot see A. It sees B and v1, so the parent is B and approx is set.

    Without the cap, C is identical to A and that parent wins.
    """
    versions = _versions()
    roots = _roots()
    texts = _texts(["A", "B", "C"])
    files = _files(
        [
            ("A", "r/a", "2026-02-01"),
            ("B", "r/b", "2026-03-15"),
            ("C", "r/c", "2026-04-01"),
        ]
    )
    signatures = {
        registry_node_id(_LINEAGE, 1): _hash(0, 0, 0, 0),
        registry_node_id(_LINEAGE, 2): _hash(0, 0, 0, 0),
        copy_node_id("A"): _hash(1, 0, 0, 0),
        copy_node_id("B"): _hash(0, 1, 0, 0),
        copy_node_id("C"): _hash(1, 1, 0, 0),
    }
    capped = build_one_family_tree(
        _LINEAGE, texts, files, versions, roots, signatures, max_earlier=1
    )
    open_cap = build_one_family_tree(
        _LINEAGE, texts, files, versions, roots, signatures, max_earlier=10
    )
    assert _parent(capped, "C") == copy_node_id("B")
    assert bool(capped.loc[capped["file_sha"].eq("C"), "approx"].iloc[0])
    assert _parent(open_cap, "C") == copy_node_id("A")
    assert not bool(open_cap.loc[open_cap["file_sha"].eq("C"), "approx"].iloc[0])


def test_vector_and_simple_parents_agree() -> None:
    """The array compare and the row loop pick the same parent and the same scores."""
    earlier = pd.DataFrame(
        {
            "node_id": [registry_node_id(_LINEAGE, 1), copy_node_id("A"), copy_node_id("B")],
            "node_kind": ["registry_version", "copy_text", "copy_text"],
            "file_sha": ["blob-v1", "A", "B"],
            "node_date": pd.to_datetime(["2026-01-10", "2026-02-01", "2026-04-01"], utc=True),
            "is_dated": [True, True, True],
        }
    )
    member = {
        "node_id": copy_node_id("C"),
        "file_sha": "C",
        "node_kind": "copy_text",
        "node_date": pd.Timestamp("2026-05-01", tz="UTC"),
        "is_dated": True,
    }
    signatures = _chain_signatures()
    fast = pick_parent_for_member(member, earlier, signatures, 5)
    slow = pick_parent_for_member(member, earlier, signatures, 5, scorer=score_candidates_simple)
    assert fast == slow
    assert fast[0] == copy_node_id("A")
    query = signatures[copy_node_id("C")]
    matrix = np.vstack([signatures[node_id] for node_id in earlier["node_id"]])
    np.testing.assert_allclose(
        score_candidates(query, matrix), score_candidates_simple(query, matrix)
    )


def test_empty_score_rows_and_a_missing_signature() -> None:
    """No rows score nothing. A member with no signature keeps the earliest node."""
    query = _hash(1, 0)
    empty = np.zeros((0, 2), dtype=np.uint64)
    assert score_candidates(query, empty).tolist() == []
    assert score_candidates_simple(query, empty).tolist() == []
    earlier = pd.DataFrame(
        {
            "node_id": ["reg::s#1", "reg::s#2"],
            "node_kind": ["registry_version", "registry_version"],
            "file_sha": ["b1", "b2"],
            "node_date": pd.to_datetime(["2026-03-01", "2026-01-01"], utc=True),
            "is_dated": [True, True],
        }
    )
    member = {
        "node_id": "text:a",
        "file_sha": "a",
        "node_date": pd.Timestamp("2026-04-01", tz="UTC"),
        "is_dated": True,
    }
    parent, similarity = pick_parent_for_member(member, earlier, {}, 5)
    assert parent == "reg::s#2"
    assert similarity == 0.0


def test_version_released_later_is_not_the_parent() -> None:
    """A perfect match released in March is invisible to a February copy."""
    earlier = pd.DataFrame(
        {
            "node_id": ["reg::s#1", "reg::s#2"],
            "node_kind": ["registry_version", "registry_version"],
            "file_sha": ["b1", "b2"],
            "node_date": pd.to_datetime(["2026-01-01", "2026-03-01"], utc=True),
            "is_dated": [True, True],
        }
    )
    member = {
        "node_id": "text:a",
        "file_sha": "a",
        "node_date": pd.Timestamp("2026-02-01", tz="UTC"),
        "is_dated": True,
    }
    signatures = {
        "reg::s#1": _hash(1, 0, 0, 0),
        "reg::s#2": _hash(1, 1, 1, 1),
        "text:a": _hash(1, 1, 1, 1),
    }
    assert pick_parent_for_member(member, earlier, signatures, 2000) == ("reg::s#1", 0.25)


def test_same_day_version_is_not_before_the_copy() -> None:
    """Released before the copy means strictly earlier, not the same timestamp."""
    stamp = pd.Timestamp("2026-02-01", tz="UTC")
    earlier = pd.DataFrame(
        {
            "node_id": ["reg::s#1"],
            "node_kind": ["registry_version"],
            "file_sha": ["b1"],
            "node_date": [stamp],
            "is_dated": [True],
        }
    )
    member = {"node_id": "text:a", "file_sha": "a", "node_date": stamp, "is_dated": True}
    parent, similarity = pick_parent_for_member(member, earlier, {}, 5)
    assert parent == ""
    assert similarity != similarity


def test_same_blob_forces_similarity_one() -> None:
    """The identical registry blob wins even when another signature is closer."""
    earlier = pd.DataFrame(
        {
            "node_id": ["reg::s#1", "reg::s#2"],
            "node_kind": ["registry_version", "registry_version"],
            "file_sha": ["same", "other"],
            "node_date": pd.to_datetime(["2026-01-01", "2026-02-01"], utc=True),
            "is_dated": [True, True],
        }
    )
    member = {
        "node_id": "text:same",
        "file_sha": "same",
        "node_date": pd.Timestamp("2026-03-01", tz="UTC"),
        "is_dated": True,
    }
    signatures = {
        "reg::s#1": _hash(0, 0, 0, 0),
        "reg::s#2": _hash(1, 1, 1, 1),
        "text:same": _hash(1, 1, 1, 1),
    }
    assert pick_parent_for_member(member, earlier, signatures, 5) == ("reg::s#1", 1.0)


def test_rehosted_first_version_points_at_the_source() -> None:
    """A copied lineage stays generation 0 and records the version it came from."""
    versions = pd.DataFrame(
        {
            "lineage_id": [_LINEAGE, _LINEAGE, _OTHER],
            "version_seq": [1, 2, 1],
            "blob": ["b1", "b2", "b1"],
            "release_date": ["2026-01-01", "2026-02-01", "2026-03-01"],
        }
    )
    roots = pd.DataFrame(
        {
            "lineage_id": [_LINEAGE, _OTHER],
            "root_lineage": [_LINEAGE, _LINEAGE],
            "copied_from_lineage": [None, _LINEAGE],
            "copied_from_seq": [pd.NA, 1],
        }
    )
    nodes = build_trunk_nodes(_LINEAGE, versions, roots)
    assert list(nodes["generation"]) == [0, 0, 0]
    rehost = nodes.loc[nodes["lineage_id"].eq(_OTHER)].iloc[0]
    assert rehost["parent_id"] == registry_node_id(_LINEAGE, 1)
    assert int(rehost["generation"]) == 0
    original = nodes.loc[nodes["lineage_id"].eq(_LINEAGE) & nodes["version_seq"].eq(1)].iloc[0]
    assert original["parent_id"] is None


def test_member_node_uses_the_earliest_file() -> None:
    """Two files: the earlier date wins the repo, and both files count."""
    texts = _texts(["A"])
    files = _files(
        [
            ("A", "r/late", "2026-05-01"),
            ("A", "r/early", "2026-02-01"),
            ("A", "r/also", "2026-02-01"),
        ]
    )
    nodes = build_member_nodes(texts, files)
    row = nodes.iloc[0]
    assert row["first_repo"] == "r/also"
    assert int(row["files"]) == 3
    assert int(row["repos"]) == 3
    assert bool(row["is_dated"])


def test_member_without_files_is_undated() -> None:
    """A text with no file row stays in the tree with counts of 0."""
    nodes = build_member_nodes(
        _texts(["A"]), pd.DataFrame(columns=["file_sha", "repo", "first_commit_date"])
    )
    row = nodes.iloc[0]
    assert int(row["files"]) == 0
    assert int(row["repos"]) == 0
    assert not bool(row["is_dated"])
    assert row["node_id"] == copy_node_id("A")


def test_independent_root_is_the_oldest_dated_member() -> None:
    """January beats a later text and an undated text that has more files."""
    texts = _texts(["A", "B", "C"], kind="independent")
    files = pd.DataFrame(
        {
            "file_sha": ["A", "B", "C"],
            "repo": ["r/a", "r/b", "r/c"],
            "first_commit_date": ["2026-03-01", "2026-01-01", None],
            "files": [1, 1, 9],
            "repos": [1, 1, 1],
        }
    )
    signatures = {
        copy_node_id("A"): _hash(1, 1, 0, 0),
        copy_node_id("B"): _hash(0, 0, 0, 0),
        copy_node_id("C"): _hash(1, 1, 0, 0),
    }
    tree = build_one_family_tree(
        "cluster:1", texts, files, _versions().iloc[0:0], _roots().iloc[0:0], signatures
    )
    assert _parent(tree, "B") is None
    assert _generation(tree, "B") == 0
    assert _parent(tree, "A") == copy_node_id("B")
    assert _parent(tree, "C") == copy_node_id("A")
    assert _generation(tree, "C") == 2
    assert not tree["parent_id"].eq(copy_node_id("C")).fillna(False).any()
    summary = summarize_families(tree, texts, files)
    assert summary.iloc[0]["root_node"] == copy_node_id("B")
    assert summary.iloc[0]["root_basis"] == "oldest"
    assert int(summary.iloc[0]["max_generation"]) == 2


def test_undated_family_roots_at_the_most_copied_text() -> None:
    """No dates: the text with the most files is the root, and the other hangs off it."""
    texts = _texts(["A", "B"], kind="independent")
    files = pd.DataFrame(
        {
            "file_sha": ["A", "B"],
            "repo": ["r/a", "r/b"],
            "first_commit_date": [None, None],
            "files": [1, 5],
            "repos": [1, 2],
        }
    )
    tree = build_one_family_tree(
        "cluster:1", texts, files, _versions().iloc[0:0], _roots().iloc[0:0], {}
    )
    assert _parent(tree, "B") is None
    assert _parent(tree, "A") == copy_node_id("B")
    assert _generation(tree, "A") == 1
    summary = summarize_families(tree, texts, files)
    assert summary.iloc[0]["root_basis"] == "most_copied"
    assert summary.iloc[0]["root_node"] == copy_node_id("B")


def test_summary_of_the_source_chain() -> None:
    """The trunk root is v1. Depth is 2. Mean generation is the mean of the copy nodes."""
    tree = _chain_tree()
    families = _texts(["A", "B", "C", "D"])
    summary = summarize_families(tree, families, _files([]))
    row = summary.iloc[0]
    assert row["root_basis"] == "registry"
    assert row["root_node"] == registry_node_id(_LINEAGE, 1)
    assert int(row["registry_versions"]) == 2
    assert int(row["registry_lineages"]) == 1
    assert int(row["texts"]) == 4
    assert int(row["max_generation"]) == 2
    copies = tree.loc[tree["node_kind"].eq("copy_text"), "generation"]
    assert row["mean_generation"] == pd.Series(copies, dtype="float64").mean()
    assert not bool(row["approx"])


def test_leaf_nodes_match_a_one_member_tree() -> None:
    """The singleton shortcut is the same node ``build_one_family_tree`` would write."""
    texts = _texts(["A"], kind="singleton")
    texts["family_id"] = "single:A"
    dates = pd.DataFrame(
        {
            "file_sha": ["A"],
            "first_repo": ["r/a"],
            "node_date": pd.to_datetime(["2026-02-01"], utc=True),
            "files": [3],
            "repos": [2],
        }
    )
    leaf = _build_leaf_nodes(texts, dates)
    tree = build_one_family_tree(
        "single:A",
        texts,
        dates,
        _versions().iloc[0:0],
        _roots().iloc[0:0],
        {},
    )
    assert leaf.iloc[0]["node_id"] == tree.iloc[0]["node_id"]
    assert int(leaf.iloc[0]["files"]) == int(tree.iloc[0]["files"]) == 3
    assert int(leaf.iloc[0]["repos"]) == int(tree.iloc[0]["repos"]) == 2
    assert leaf.iloc[0]["first_repo"] == tree.iloc[0]["first_repo"] == "r/a"
    assert int(leaf.iloc[0]["generation"]) == 0
    assert leaf.iloc[0]["parent_id"] is None
    assert bool(leaf.iloc[0]["is_dated"])


def test_tie_keeps_the_earliest_node() -> None:
    """Equal scores keep the earlier registry version."""
    earlier = pd.DataFrame(
        {
            "node_id": ["reg::s#1", "reg::s#2"],
            "node_kind": ["registry_version", "registry_version"],
            "file_sha": ["b1", "b2"],
            "node_date": pd.to_datetime(["2026-02-01", "2026-01-01"], utc=True),
            "is_dated": [True, True],
        }
    )
    member = {
        "node_id": "text:a",
        "file_sha": "a",
        "node_date": pd.Timestamp("2026-03-01", tz="UTC"),
        "is_dated": True,
    }
    signatures = {
        "reg::s#1": _hash(1, 1, 1, 0),
        "reg::s#2": _hash(1, 0, 1, 1),
        "text:a": _hash(1, 0, 0, 0),
    }
    parent, similarity = pick_parent_for_member(member, earlier, signatures, 5)
    assert parent == "reg::s#2"
    assert similarity == 0.5
