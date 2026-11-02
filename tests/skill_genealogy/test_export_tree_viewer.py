"""Tree viewer export on a five-node family and two hand-made repos."""

import json
import sys
from pathlib import Path

import pandas as pd

from skill_genealogy.export_tree_viewer import (
    build_families_index,
    build_repos_index,
    export_tree_viewer,
    family_to_nested_json,
    main,
    safe_file_name,
)


def _use_tmp(monkeypatch, folder: Path) -> None:
    """Point the stage folders at ``folder``."""
    data = folder / "data"
    results = folder / "results"
    data.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("skill_genealogy.export_tree_viewer.data_dir", lambda dev: data)
    monkeypatch.setattr("skill_genealogy.export_tree_viewer.results_dir", lambda dev: results)


def _write(folder: Path, name: str, frame: pd.DataFrame) -> None:
    """Write one parquet table into the temp data folder."""
    frame.to_parquet(folder / "data" / name, index=False)


def _five_nodes() -> pd.DataFrame:
    """Root, one branch of two nodes, and two leaves."""
    return pd.DataFrame(
        [
            _node(
                "root", None, "registry_version", 0, "acme/skills", 1, 1, None, "acme/skills::pdf"
            ),
            _node("big", "root", "copy_text", 1, "hub/one", 8, None, 0.97, None, "bbb"),
            _node("leaf", "big", "copy_text", 2, "hub/two", 2, None, 0.9, None, "ccc"),
            _node("small-a", "root", "copy_text", 1, "other/a", 1, None, 0.5, None, "ddd"),
            _node("small-b", "root", "copy_text", 1, "other/b", 1, None, 0.4, None, "eee"),
        ]
    )


def _node(node_id, parent, kind, generation, repo, files, version, similarity, lineage, sha="aaa"):
    """One tree row."""
    return {
        "node_id": node_id,
        "family_id": "acme/skills::pdf",
        "node_kind": kind,
        "lineage_id": lineage,
        "version_seq": version,
        "file_sha": sha,
        "first_repo": repo,
        "node_date": "2026-02-01 00:00:00+00:00",
        "files": files,
        "parent_id": parent,
        "parent_similarity": similarity,
        "generation": generation,
    }


def _info() -> pd.DataFrame:
    """Stage 9 and stage 10 fields for the large branch."""
    return pd.DataFrame(
        [
            {
                "node_id": "big",
                "repo_role": "hub",
                "path": "skills/pdf/SKILL.md",
                "link": "exact",
                "state": "cosmetic",
                "stale": "stale",
                "versions_behind": 2,
                "hub_evidence": "dataset_dates",
                "days_stale": 40,
            }
        ]
    )


def _real_count(node: dict) -> int:
    """Data nodes, not the ``+N more`` marker."""
    count = 0 if node["node_kind"] == "more" else 1
    for child in node["children"]:
        count += _real_count(child)
    return count


def test_nested_json_keeps_the_largest_branch() -> None:
    """Five nodes and a cap of 3 keep the branch and add ``+2 more``."""
    tree = family_to_nested_json(_five_nodes(), _info(), 3)
    assert tree["name"] == "v1"
    assert tree["generation"] == 0
    assert tree["node_kind"] == "registry_version"
    assert tree["path"] == "pdf/SKILL.md"
    big = next(child for child in tree["children"] if child["name"] == "hub/one")
    assert big["generation"] == 1
    assert big["repo_role"] == "hub"
    assert big["state"] == "cosmetic"
    assert big["stale"] == "stale"
    assert big["versions_behind"] == 2
    assert big["children"][0]["name"] == "hub/two"
    assert big["children"][0]["generation"] == 2
    more = next(child for child in tree["children"] if child["node_kind"] == "more")
    assert more["name"] == "+2 more"
    assert _real_count(tree) == 3


def test_max_nodes_one_keeps_only_the_root() -> None:
    """A cap of 1 leaves every descendant in one marker."""
    tree = family_to_nested_json(_five_nodes(), pd.DataFrame(), 1)
    assert [child["name"] for child in tree["children"]] == ["+4 more"]
    assert _real_count(tree) == 1


def test_safe_file_name_matches_the_example_and_stays_unique() -> None:
    """The documented id uses underscores. ``/`` and ``::`` do not collide."""
    assert safe_file_name("anthropics/skills::pdf") == "anthropics__skills__pdf"
    assert safe_file_name("a/b") != safe_file_name("a::b")
    assert "/" not in safe_file_name("owner/repo::skill/path")
    assert ":" not in safe_file_name("owner/repo::skill/path")


def test_hub_reason_uses_registry_counts_and_dated_counts() -> None:
    """31 of 80 is a registry sentence. 35 of 36 is a dataset-date sentence."""
    roles = pd.DataFrame(
        [
            {
                "repo": "old/reg",
                "repo_role": "registry_hub",
                "stars": 10,
                "english_skills": 80,
                "hub_share": 31 / 80,
                "is_hub": True,
                "hub_evidence": "registry_history",
                "rehosted_lineages": 31,
                "english_lineages": 80,
            },
            {
                "repo": "aydabd/cacad",
                "repo_role": "hub",
                "stars": 0,
                "english_skills": 39,
                "hub_share": 35 / 36,
                "is_hub": True,
                "hub_evidence": "dataset_dates",
                "skills_tested": 36,
                "first_seen_elsewhere": 35,
            },
            {
                "repo": "ambiguous/repo",
                "repo_role": "hub",
                "stars": 1,
                "english_skills": 17,
                "hub_share": 0.25,
                "is_hub": True,
                "hub_evidence": "dataset_dates",
            },
        ]
    )
    exported = pd.DataFrame(
        {
            "family_id": ["old/reg::pdf", "cluster:1", "cluster:2"],
            "repo": ["old/reg", "aydabd/cacad", "ambiguous/repo"],
        }
    )
    copies = pd.DataFrame(columns=["repo", "root_lineage"])
    rows = {row["repo"]: row for row in build_repos_index(copies, roles, exported)}
    assert rows["old/reg"]["hub_reason"] == (
        "31 of 80 skills first appeared in older registries -> hub"
    )
    assert rows["aydabd/cacad"]["hub_reason"] == (
        "35 of 36 dated skills first appeared in older repos -> hub"
    )
    assert rows["aydabd/cacad"]["hub_evidence"] == "dataset_dates"
    assert rows["ambiguous/repo"]["hub_reason"].startswith("share 0.250")


def test_families_index_adds_the_named_family() -> None:
    """The extra id is kept even when it is not in the top row."""
    summary = pd.DataFrame(
        [
            {
                "family_id": "big",
                "family_kind": "independent",
                "texts": 9,
                "repos": 1,
                "max_generation": 2,
            },
            {
                "family_id": "small",
                "family_kind": "source",
                "texts": 1,
                "repos": 1,
                "max_generation": 0,
            },
        ]
    )
    index = build_families_index(summary, 1, ["small"])
    assert [row["id"] for row in index] == ["big", "small"]
    assert index[1]["file"] == "families/small.json"
    assert index[0]["depth"] == 2


def test_export_writes_the_page_and_valid_json(tmp_path: Path, monkeypatch) -> None:
    """Tiny inputs produce the page, both indexes, and a cut tree."""
    _use_tmp(monkeypatch, tmp_path)
    _write_inputs(tmp_path)
    folder = export_tree_viewer(True, 1, ["extra/family::skill"], 3)
    html = (folder / "index.html").read_text(encoding="utf-8")
    assert "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js" in html
    for name in ("viewer_common.js", "tree_viewer.js", "tree_viewer.css", "repos_index.json"):
        assert (folder / name).is_file()
    index = json.loads((folder / "families_index.json").read_text(encoding="utf-8"))
    assert {row["id"] for row in index} == {"acme/skills::pdf", "extra/family::skill"}
    big_row = next(row for row in index if row["id"] == "acme/skills::pdf")
    big = json.loads((folder / big_row["file"]).read_text(encoding="utf-8"))
    assert big["children"][-1]["name"] == "+2 more"
    states = [(child["state"], child["stale"]) for child in big["children"]]
    assert ("cosmetic", "stale") in states
    repos = json.loads((folder / "repos_index.json").read_text(encoding="utf-8"))
    by_repo = {row["repo"]: row for row in repos}
    assert by_repo["hub/one"]["role"] == "hub"
    assert "35 of 36" in by_repo["hub/one"]["hub_reason"]
    assert "acme/skills::pdf" in by_repo["hub/one"]["families"]
    monkeypatch.setattr(sys, "argv", ["export_tree_viewer", "--dev"])
    main()
    assert (folder / "index.html").is_file()


def _write_inputs(folder: Path) -> None:
    """The seven parquet files the stage reads."""
    summary = pd.DataFrame(
        [
            {
                "family_id": "acme/skills::pdf",
                "family_kind": "source",
                "texts": 4,
                "repos": 4,
                "max_generation": 2,
            },
            {
                "family_id": "extra/family::skill",
                "family_kind": "singleton",
                "texts": 1,
                "repos": 1,
                "max_generation": 0,
            },
        ]
    )
    extra = _node("only", None, "copy_text", 0, "solo/repo", 1, None, None, None, "fff")
    extra["family_id"] = "extra/family::skill"
    tree = pd.concat([_five_nodes(), pd.DataFrame([extra])], ignore_index=True)
    copies = pd.DataFrame(
        [
            {
                "repo": "hub/one",
                "path": "skills/pdf/SKILL.md",
                "file_sha": "bbb",
                "link": "exact",
                "root_lineage": "acme/skills::pdf",
            }
        ]
    )
    changes = pd.DataFrame(
        [{"repo": "hub/one", "path": "skills/pdf/SKILL.md", "state": "cosmetic"}]
    )
    staleness = pd.DataFrame(
        [
            {
                "repo": "hub/one",
                "path": "skills/pdf/SKILL.md",
                "copy_state": "stale",
                "skill_md_versions_behind": 1,
                "package_versions_behind": 1,
                "days_stale": 40,
            }
        ]
    )
    roles = pd.DataFrame(
        [
            {
                "repo": "acme/skills",
                "repo_role": "registry",
                "stars": 100,
                "english_skills": 5,
                "hub_share": 0.0,
                "is_hub": False,
                "hub_evidence": "registry_history",
            },
            {
                "repo": "hub/one",
                "repo_role": "hub",
                "stars": 0,
                "english_skills": 39,
                "hub_share": 35 / 36,
                "is_hub": True,
                "hub_evidence": "dataset_dates",
                "skills_tested": 36,
                "first_seen_elsewhere": 35,
            },
        ]
    )
    hubs = pd.DataFrame(
        [
            {
                "repo": "acme/skills",
                "hub_reason": "0 of 5 skills first appeared in older registries -> not a hub",
                "english_lineages": 5,
                "rehosted_lineages": 0,
                "hub_share": 0.0,
                "is_hub": False,
            }
        ]
    )
    _write(folder, "family_summary.parquet", summary)
    _write(folder, "family_tree.parquet", tree)
    _write(folder, "copy_files.parquet", copies)
    _write(folder, "copy_changes.parquet", changes)
    _write(folder, "copy_staleness.parquet", staleness)
    _write(folder, "repo_roles_final.parquet", roles)
    _write(folder, "registry_hubs.parquet", hubs)
