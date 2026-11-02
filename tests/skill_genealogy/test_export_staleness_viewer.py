"""Staleness viewer JSON for a tiny lineage, and a one-lineage export."""

import json
from pathlib import Path

import pandas as pd

from skill_genealogy.export_staleness_viewer import (
    build_lineages_index,
    export_staleness_viewer,
    lineage_to_json,
)


def _use_tmp(monkeypatch, folder: Path) -> None:
    """Point the stage folders at ``folder``."""
    data = folder / "data"
    results = folder / "results"
    data.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("skill_genealogy.export_staleness_viewer.data_dir", lambda dev: data)
    monkeypatch.setattr("skill_genealogy.export_staleness_viewer.results_dir", lambda dev: results)


def _write(folder: Path, name: str, frame: pd.DataFrame) -> None:
    """Write one parquet table into the temp data folder."""
    frame.to_parquet(folder / "data" / name, index=False)


def _lineage() -> dict:
    """One registry skill."""
    return {
        "id": "acme/skills::pdf",
        "registry": "acme/skills",
        "registry_role": "registry",
        "removed": False,
    }


def _versions() -> pd.DataFrame:
    """Two registry versions. v2 added a command."""
    return pd.DataFrame(
        [
            {"version_seq": 1, "release_date": "2026-01-01", "missed_risky_kinds": []},
            {
                "version_seq": 2,
                "release_date": "2026-03-01",
                "missed_risky_kinds": ["command"],
            },
        ]
    )


def _copies() -> pd.DataFrame:
    """Three copies: two hold v1, one has no held version and is orphaned."""
    return pd.DataFrame(
        [
            {
                "repo": "a/one",
                "path": "skills/pdf/SKILL.md",
                "repo_role": "repository",
                "held_seq": 1,
                "skill_md_versions_behind": 1,
                "days_stale": 10,
                "stopped_changing_at": "2026-02-01",
                "missed_risky": 0,
                "package_versions_behind": 0,
                "missed_script_updates": 0,
                "as_of_basis": "registry_snapshot",
                "as_of_date": "2026-06-01",
                "copy_state": "behind",
            },
            {
                "repo": "b/two",
                "path": "skills/pdf/SKILL.md",
                "repo_role": "repository",
                "held_seq": 1,
                "skill_md_versions_behind": 1,
                "days_stale": 40,
                "stopped_changing_at": "2026-02-02",
                "missed_risky": 4,
                "package_versions_behind": 2,
                "missed_script_updates": 3,
                "as_of_basis": "registry_snapshot",
                "as_of_date": "2026-06-01",
                "copy_state": "stale",
            },
            {
                "repo": "c/old",
                "path": "skills/pdf/SKILL.md",
                "repo_role": "hub",
                "held_seq": None,
                "skill_md_versions_behind": 2,
                "days_stale": None,
                "stopped_changing_at": "2026-01-01",
                "missed_risky": 0,
                "package_versions_behind": 0,
                "missed_script_updates": 0,
                "as_of_basis": "dataset_capture",
                "as_of_date": "2026-06-01",
                "copy_state": "orphaned",
            },
        ]
    )


def test_lineage_to_json_groups_caps_and_omits_stale() -> None:
    """Copies group by held version, the cap keeps the most behind, and stale is absent."""
    page = lineage_to_json(_lineage(), _versions(), _copies(), 1)
    assert page["id"] == "acme/skills::pdf"
    assert page["registry"] == "acme/skills"
    assert page["registry_role"] == "registry"
    assert page["removed"] is False
    assert page["default_stale_days"] == 30
    assert page["as_of_dates"] == ["2026-06-01"]
    assert page["versions"][1]["missed_risky_kinds"] == ["command"]
    assert list(page["copies_by_version"]) == ["1", "none"]
    held = page["copies_by_version"]["1"]
    assert len(held) == 1
    assert held[0]["repo"] == "b/two"
    assert held[0]["versions_behind"] == 1
    assert held[0]["days_stale"] == 40
    assert held[0]["missed_risky"] == 4
    assert held[0]["package_versions_behind"] == 2
    assert held[0]["missed_script_updates"] == 3
    assert held[0]["state_basis"] == "registry_snapshot"
    assert "stale" not in held[0]
    orphan = page["copies_by_version"]["none"][0]
    assert orphan["orphaned"] is True
    assert orphan["held_seq"] is None
    assert "stale" not in orphan
    assert len(page["copy_days"]) == 3
    assert sum(1 for row in page["copy_days"] if row["orphaned"]) == 1


def test_lineage_to_json_rejects_a_negative_cap() -> None:
    """A negative table cap is not a meaningful export."""
    try:
        lineage_to_json(_lineage(), _versions(), _copies(), -1)
    except ValueError as error:
        assert "rows_per_version" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_build_lineages_index_ranks_by_copies() -> None:
    """The lineage with more copies comes first. A lineage with none is left out."""
    staleness = pd.DataFrame(
        {
            "lineage_id": ["acme/skills::other", "acme/skills::pdf", "acme/skills::pdf"],
            "repo": ["a/x", "a/x", "b/y"],
        }
    )
    lineages = pd.DataFrame(
        {
            "lineage_id": ["acme/skills::pdf", "acme/skills::other", "acme/skills::empty"],
            "repo": ["acme/skills", "acme/skills", "acme/skills"],
            "registry_role": ["registry", "registry", "registry"],
        }
    )
    index = build_lineages_index(staleness, lineages)
    assert [row["id"] for row in index] == ["acme/skills::pdf", "acme/skills::other"]
    assert index[0]["copies"] == 2
    assert index[0]["registry"] == "acme/skills"
    assert index[0]["registry_role"] == "registry"
    assert index[0]["file"].startswith("lineages/")
    assert index[0]["file"].endswith(".json")
    assert build_lineages_index(pd.DataFrame(), lineages) == []


def test_export_writes_page_and_skips(tmp_path: Path, monkeypatch, capsys) -> None:
    """A one-lineage folder has the page, capped rows, and no stale key. The next run skips."""
    _use_tmp(monkeypatch, tmp_path)
    _write(
        tmp_path,
        "lineages.parquet",
        pd.DataFrame(
            [
                {
                    "lineage_id": "acme/skills::pdf",
                    "repo": "acme/skills",
                    "registry_removed": False,
                },
                {
                    "lineage_id": "acme/skills::other",
                    "repo": "acme/skills",
                    "registry_removed": False,
                },
            ]
        ),
    )
    _write(
        tmp_path,
        "lineage_versions.parquet",
        pd.DataFrame(
            [
                {
                    "lineage_id": "acme/skills::pdf",
                    "version_seq": 1,
                    "release_date": "2026-01-01",
                },
                {
                    "lineage_id": "acme/skills::pdf",
                    "version_seq": 2,
                    "release_date": "2026-03-01",
                },
            ]
        ),
    )
    _write(
        tmp_path,
        "lineage_changes.parquet",
        pd.DataFrame(
            [
                {
                    "lineage_id": "acme/skills::pdf",
                    "to_seq": 2,
                    "commands_added": ["curl https://example.test"],
                    "network_refs_added": [],
                    "scripts_mentioned_added": [],
                }
            ]
        ),
    )
    copies = _copies().drop(columns=["repo_role"])
    copies["lineage_id"] = "acme/skills::pdf"
    _write(tmp_path, "copy_staleness.parquet", copies)
    _write(
        tmp_path,
        "repo_roles_final.parquet",
        pd.DataFrame(
            [
                {
                    "repo": "acme/skills",
                    "stars": 10,
                    "english_skills": 2,
                    "is_hub": False,
                    "hub_share": 0.0,
                    "repo_role": "registry",
                    "hub_evidence": "registry_history",
                },
                {
                    "repo": "b/two",
                    "stars": 1,
                    "english_skills": 4,
                    "is_hub": False,
                    "hub_share": 0.0,
                    "repo_role": "repository",
                    "hub_evidence": "dataset_dates",
                },
                {
                    "repo": "c/old",
                    "stars": 3,
                    "english_skills": 6,
                    "is_hub": True,
                    "hub_share": 0.5,
                    "repo_role": "hub",
                    "hub_evidence": "dataset_dates",
                },
            ]
        ),
    )
    _write(
        tmp_path,
        "registry_hubs.parquet",
        pd.DataFrame(
            [
                {
                    "repo": "acme/skills",
                    "english_lineages": 2,
                    "rehosted_lineages": 0,
                    "hub_share": 0.0,
                    "is_hub": False,
                    "hub_reason": "0 of 2 skills first appeared in older registries -> not a hub",
                }
            ]
        ),
    )
    folder = export_staleness_viewer(True, 1, 1)
    first = capsys.readouterr().out
    assert "skip" not in first
    assert (folder / "index.html").is_file()
    assert (folder / "staleness_viewer.js").is_file()
    assert (folder / "viewer_common.js").is_file()
    index = json.loads((folder / "lineages_index.json").read_text(encoding="utf-8"))
    assert len(index) == 1
    assert index[0]["id"] == "acme/skills::pdf"
    assert index[0]["copies"] == 3
    assert index[0]["registry_role"] == "registry"
    page = json.loads((folder / index[0]["file"]).read_text(encoding="utf-8"))
    assert page["copies_by_version"]["1"][0]["repo"] == "b/two"
    assert "stale" not in page["copies_by_version"]["1"][0]
    assert page["copies_by_version"]["none"][0]["orphaned"] is True
    assert page["versions"][1]["missed_risky_kinds"] == ["command"]
    assert len(page["copy_days"]) == 3
    repos = json.loads((folder / "repos_index.json").read_text(encoding="utf-8"))
    by_repo = {row["repo"]: row for row in repos}
    assert "lineages" in by_repo["b/two"]
    assert "families" not in by_repo["b/two"]
    assert by_repo["b/two"]["lineages"] == ["acme/skills::pdf"]
    assert "not a hub" in by_repo["acme/skills"]["hub_reason"]
    again = export_staleness_viewer(True, 1, 1)
    second = capsys.readouterr().out
    assert "skip" in second
    assert again == folder
