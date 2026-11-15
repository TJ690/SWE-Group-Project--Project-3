"""Tests for stage 8, family trees. Hand-made versions and copies, no dataset."""

from __future__ import annotations

import numpy as np
import pandas as pd

from skill_genealogy import build_family_trees as stage

WIDTH = 128


def utc(*dates: str | None) -> list:
    return list(pd.to_datetime(list(dates), utc=True, format="ISO8601"))


def sig(base: int, changed: int = 0) -> np.ndarray:
    """A signature of ``base`` values; ``changed`` of them differ from the base text."""
    values = np.arange(WIDTH, dtype=np.uint32) + np.uint32(base * 1000)
    values[:changed] += np.uint32(500)
    return values


def book(entries: dict[str, np.ndarray]) -> dict[str, object]:
    keys = list(entries)
    return {
        "row": {k: i for i, k in enumerate(keys)},
        "matrix": np.vstack([entries[k] for k in keys]),
    }


def versions() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "lineage_id": ["o/r::s", "o/r::s"],
            "version_seq": [1, 2],
            "blob": ["b1", "b2"],
            "release_date": utc("2026-01-10", "2026-03-01"),
        }
    )


def roots() -> pd.DataFrame:
    return pd.DataFrame(
        {"lineage_id": ["o/r::s"], "copied_from_lineage": [None], "copied_from_seq": [pd.NA]}
    )


def files(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["repo", "path", "file_sha", "first_commit_date"])


def texts(*shas: str) -> pd.DataFrame:
    return pd.DataFrame({"file_sha": list(shas), "registry_self": False})


def source_tree(max_earlier: int | None = None) -> pd.DataFrame:
    # A is v1 with 10 values changed; B is near v2; C is a copy of A; D is undated and near v2.
    signatures = book(
        {
            "o/r::s#1": sig(1),
            "o/r::s#2": sig(2),
            "A": sig(1, 10),
            "B": sig(2, 10),
            "C": sig(1, 10) + np.where(np.arange(WIDTH) % 40 == 20, 7, 0).astype(np.uint32),
            "D": sig(2, 5),
        }
    )
    earliest = stage.earliest_files(
        files(
            [
                ("x/a", "p", "A", "2026-02-01"),
                ("x/b", "p", "B", "2026-04-01"),
                ("x/c", "p", "C", "2026-05-01"),
                ("x/d", "p", "D", None),
            ]
        )
    )
    return stage.build_one_family_tree(
        "o/r::s", texts("A", "B", "C", "D"), earliest, versions(), roots(), signatures, max_earlier
    )


def parent(tree: pd.DataFrame, sha: str) -> tuple[str, int]:
    row = tree.loc[tree["node_id"] == f"text:{sha}"].iloc[0]
    return row["parent_id"], int(row["generation"])


def test_source_chain_and_trunk() -> None:
    tree = source_tree()
    trunk = tree.loc[tree["node_kind"] == "registry"].set_index("node_id")
    assert trunk.loc["o/r::s#2", "parent_id"] == "o/r::s#1"
    assert trunk["generation"].tolist() == [0, 0]
    assert parent(tree, "A") == ("o/r::s#1", 1)
    assert parent(tree, "B") == ("o/r::s#2", 1)
    assert parent(tree, "C") == ("text:A", 2)


def test_undated_attaches_and_has_no_children() -> None:
    tree = source_tree()
    assert parent(tree, "D")[0] == "o/r::s#2"
    assert "text:D" not in set(tree["parent_id"])


def test_cap_one_hides_a() -> None:
    tree = source_tree(max_earlier=1)
    assert parent(tree, "C")[0] != "text:A"
    assert bool(tree.loc[tree["node_id"] == "text:C", "approx"].iloc[0])
    assert not bool(tree.loc[tree["node_id"] == "text:A", "approx"].iloc[0])


def test_scorers_agree_and_empty() -> None:
    matrix = np.vstack([sig(1), sig(1, 40), sig(3)])
    query = sig(1)
    assert np.allclose(
        stage.score_candidates(query, matrix), stage.score_candidates_simple(query, matrix)
    )
    assert len(stage.score_candidates(query, matrix[:0])) == 0


def test_same_blob_scores_one_and_missing_signature_keeps_earliest() -> None:
    nodes = pd.DataFrame(
        {"node_id": ["n1", "n2"], "blob": ["x", "y"], "first_date": utc("2026-01-01", "2026-02-01")}
    )
    signatures = book({"n1": sig(1), "n2": sig(2)})
    assert stage.pick_parent_for_member({"blob": "y"}, nodes, signatures) == ("n2", 1.0)
    parent_id, score = stage.pick_parent_for_member({"blob": "zz"}, nodes, signatures)
    assert parent_id == "n1" and np.isnan(score)
    assert stage.pick_parent_for_member({"blob": "y"}, nodes.iloc[0:0], signatures)[0] == ""


def test_rehost_first_version_points_at_source() -> None:
    vers = pd.DataFrame(
        {
            "lineage_id": ["s/r::a", "s/r::a", "t/r::a"],
            "version_seq": [1, 2, 1],
            "blob": ["a1", "a2", "t1"],
            "release_date": utc("2026-01-01", "2026-02-01", "2026-03-01"),
        }
    )
    rts = pd.DataFrame(
        {
            "lineage_id": ["s/r::a", "t/r::a"],
            "copied_from_lineage": [None, "s/r::a"],
            "copied_from_seq": [pd.NA, 2],
        }
    )
    trunk = stage.build_trunk_nodes("s/r::a", vers, rts).set_index("node_id")
    assert trunk.loc["t/r::a#1", "parent_id"] == "s/r::a#2"
    assert trunk.loc["t/r::a#1", "generation"] == 0
    assert trunk.loc["s/r::a#1", "parent_id"] == ""


def test_earliest_file_sets_repo_and_tie_takes_smaller_repo() -> None:
    result = stage.earliest_files(
        files(
            [
                ("z/z", "p", "S", "2026-01-01"),
                ("a/a", "p", "S", "2026-01-01"),
                ("m/m", "p", "S", None),
            ]
        )
    ).iloc[0]
    assert result["repo"] == "a/a" and result["files"] == 3
    members = stage.build_member_nodes(
        texts("S", "T"), stage.earliest_files(files([("a/a", "p", "S", "2026-01-01")]))
    )
    assert members.loc[members["blob"] == "T", "first_date"].isna().all()


def test_independent_roots() -> None:
    signatures = book({"P": sig(1), "Q": sig(1, 5), "R": sig(1, 9)})
    earliest = stage.earliest_files(
        files(
            [
                ("a/a", "p", "P", "2026-01-01"),
                ("b/b", "p", "Q", "2026-02-01"),
                ("c/c", "p", "R", None),
            ]
        )
    )
    tree = stage.build_one_family_tree(
        "cluster:1",
        texts("P", "Q", "R"),
        earliest,
        versions().iloc[0:0],
        roots().iloc[0:0],
        signatures,
    )
    assert parent(tree, "P") == ("", 0)
    assert parent(tree, "Q") == ("text:P", 1)
    assert parent(tree, "R")[0] in {"text:P", "text:Q"}
    # All undated: the most copied text is the root.
    undated = stage.earliest_files(
        files([("a/a", "p", "P", None), ("b/b", "p", "Q", None), ("c/c", "p", "Q", None)])
    )
    tree = stage.build_one_family_tree(
        "cluster:2", texts("P", "Q"), undated, versions().iloc[0:0], roots().iloc[0:0], signatures
    )
    assert parent(tree, "Q") == ("", 0)
    assert parent(tree, "P")[0] == "text:Q"


def test_summary_and_leaves() -> None:
    tree = source_tree()
    fam = pd.DataFrame(
        {
            "file_sha": ["A", "B", "C", "D", "L"],
            "family_id": ["o/r::s"] * 4 + ["single:L"],
            "family_kind": ["source"] * 4 + ["singleton"],
        }
    )
    leaf = stage._build_leaf_nodes(
        fam.iloc[4:], stage.earliest_files(files([("l/l", "p", "L", "2026-01-01")]))
    )
    summary = stage.summarize_families(pd.concat([tree, leaf], ignore_index=True), fam)
    row = summary.set_index("family_id")
    assert row.loc["o/r::s", "depth"] == 2 and row.loc["o/r::s", "root_basis"] == "trunk"
    assert row.loc["o/r::s", "mean_generation"] == (1 + 1 + 2 + 1) / 4
    assert (
        row.loc["single:L", "root_basis"] == "single" and row.loc["single:L", "status"] == "built"
    )
    skipped = stage.summarize_families(tree.iloc[0:0], fam, {"o/r::s"})
    by_status = skipped.set_index("family_id")
    assert by_status.loc["o/r::s", "status"] == "skipped"
    assert by_status.loc["single:L", "status"] == "registry_only"
    # No nodes, so no root was chosen: neither "most_copied" nor "single".
    assert set(by_status["root_basis"]) == {"none"}


def test_registry_only_cluster_has_no_root_basis() -> None:
    fam = pd.DataFrame(
        {
            "file_sha": ["X", "Y"],
            "family_id": ["cluster:9", "cluster:9"],
            "family_kind": ["independent", "independent"],
        }
    )
    row = stage.summarize_families(pd.DataFrame(columns=stage.TREE_COLUMNS), fam).iloc[0]
    assert row["status"] == "registry_only" and row["root_basis"] == "none"


def test_tie_between_original_and_rehost_takes_earliest_release() -> None:
    # The re-host sorts first by lineage id but was released later: the original wins.
    vers = pd.DataFrame(
        {
            "lineage_id": ["z/orig::x", "a/rehost::x"],
            "version_seq": [1, 1],
            "blob": ["B", "B"],
            "release_date": utc("2026-01-01", "2026-03-01"),
        }
    )
    rts = pd.DataFrame(
        {
            "lineage_id": ["z/orig::x", "a/rehost::x"],
            "copied_from_lineage": [None, "z/orig::x"],
            "copied_from_seq": [pd.NA, 1],
        }
    )
    signatures = book({"z/orig::x#1": sig(1), "a/rehost::x#1": sig(1), "C": sig(1, 5)})
    earliest = stage.earliest_files(files([("x/c", "p", "C", "2026-06-01")]))
    tree = stage.build_one_family_tree("z/orig::x", texts("C"), earliest, vers, rts, signatures)
    assert parent(tree, "C")[0] == "z/orig::x#1"
    # The same blob is also matched to the original.
    same = stage.earliest_files(files([("x/b", "p", "B", "2026-06-01")]))
    tree = stage.build_one_family_tree("z/orig::x", texts("B"), same, vers, rts, signatures)
    assert parent(tree, "B") == ("z/orig::x#1", 1)


def test_copy_dated_before_every_version_attaches_to_earliest_version() -> None:
    signatures = book({"o/r::s#1": sig(1), "o/r::s#2": sig(2), "E": sig(1, 5), "F": sig(2, 5)})
    earliest = stage.earliest_files(
        files([("x/e", "p", "E", "2025-12-01"), ("x/f", "p", "F", "2025-12-02")])
    )
    tree = stage.build_one_family_tree(
        "o/r::s", texts("E", "F"), earliest, versions(), roots(), signatures
    )
    # E is first of all: no earlier node exists, so it hangs on v1. F sees E.
    assert parent(tree, "E") == ("o/r::s#1", 1)
    assert parent(tree, "F")[1] >= 1 and parent(tree, "F")[0] != ""
    assert (tree.loc[tree["node_kind"] == "copy", "parent_id"] != "").all()


def test_save_keeps_old_outputs_when_a_write_fails(tmp_path, monkeypatch) -> None:
    tree_path, summary_path = tmp_path / "t.parquet", tmp_path / "s.parquet"
    monkeypatch.setattr(stage, "family_tree_path", lambda dev: tree_path)
    monkeypatch.setattr(stage, "family_summary_path", lambda dev: summary_path)
    old = pd.DataFrame(
        {"family_id": ["a"], "generation": [0], "first_date": [None], "node_id": ["n"]}
    )
    old.to_parquet(tree_path)
    old.to_parquet(summary_path)
    calls = []

    def failing(frame, path, sort_by=None):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        frame.to_parquet(path, index=False)

    monkeypatch.setattr(stage, "write_parquet", failing)
    try:
        stage.save_outputs(old, old, True)
    except OSError:
        pass
    assert pd.read_parquet(tree_path).equals(old) and pd.read_parquet(summary_path).equals(old)
    assert not list(tmp_path.glob("*.tmp"))


def test_force_keeps_old_outputs_when_the_build_fails(tmp_path, monkeypatch) -> None:
    paths = {"family_tree": tmp_path / "t.parquet", "family_summary": tmp_path / "s.parquet"}
    for path in paths.values():
        path.write_text("old")
    monkeypatch.setattr(stage, "output_paths", lambda dev: paths)

    def boom(*args, **kwargs):
        raise RuntimeError("build failed")

    monkeypatch.setattr(stage, "build_trees", boom)
    monkeypatch.setattr(stage, "Progress", lambda dev: None)
    try:
        stage.run(True, True)
    except RuntimeError:
        pass
    assert all(path.read_text() == "old" for path in paths.values())


def test_progress_file_and_tick(tmp_path, capsys) -> None:
    path = tmp_path / "progress.json"
    progress = stage.Progress(True, every=0.0, path=path)
    progress.stage("trees", "2 families, 9 texts")
    progress.state["trees_started"] = 0.0
    calls: list[tuple[int, int]] = []
    tree = source_tree_with_tick(calls)
    progress.trees(1, 2, 4, 9, "o/r::s", 2, 4, force=True)
    state = __import__("json").loads(path.read_text())
    assert state["texts_done"] == 4 and state["percent"] == 44.44 and state["families_total"] == 2
    progress.finish()
    assert __import__("json").loads(path.read_text())["done"] is True
    assert "trees: families 1/2" in capsys.readouterr().out
    assert calls and calls[0] == (0, 4) and len(tree) == 6


def source_tree_with_tick(calls: list[tuple[int, int]]) -> pd.DataFrame:
    signatures = book({"o/r::s#1": sig(1), "o/r::s#2": sig(2), "A": sig(1, 10), "B": sig(2, 10)})
    earliest = stage.earliest_files(
        files([("x/a", "p", "A", "2026-02-01"), ("x/b", "p", "B", "2026-04-01")])
    )
    return stage.build_one_family_tree(
        "o/r::s",
        texts("A", "B", "C", "D"),
        earliest,
        versions(),
        roots(),
        signatures,
        tick=lambda done, total: calls.append((done, total)),
    )
