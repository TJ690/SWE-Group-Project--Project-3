"""Summary tables on a hand-made family, two copies, and a tiny old run."""

import sys
from pathlib import Path

import pandas as pd
import pytest

from skill_genealogy.summarize_results import (
    compare_with_old_genealogy,
    main,
    rq1_family_tables,
    rq2_change_tables,
    rq3_staleness_tables,
    scope_tables,
    write_tables,
)


def _use_tmp(monkeypatch, folder: Path) -> None:
    """Point data and result folders at ``folder``."""
    data = folder / "data"
    results = folder / "results"
    data.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("skill_genealogy.summarize_results.data_dir", lambda dev: data)
    monkeypatch.setattr("skill_genealogy.summarize_results.results_dir", lambda dev: results)


def _write(folder: Path, name: str, frame: pd.DataFrame) -> None:
    """Write one parquet table into the tiny data folder."""
    frame.to_parquet(folder / "data" / name, index=False)


def _lists(values: list[list[str]]) -> pd.Series:
    """A parquet list column. Empty and non-empty lists stay lists."""
    return pd.Series(values, dtype=object)


def _build_inputs(folder: Path) -> None:
    """Families, two copies, scope rows, and the profile file the comparison reads."""
    _write(
        folder,
        "family_summary.parquet",
        pd.DataFrame(
            [
                ["acme/skills::a", "source", 1, 1, 1, 0],
                ["acme/skills::b", "source", 3, 3, 2, 4],
                ["cluster:1", "independent", 4, 4, 1, 1],
            ],
            columns=["family_id", "family_kind", "texts", "files", "repos", "max_generation"],
        ),
    )
    family_rows = [
        ["acme/skills::a", "source", 1, 1],
        ["acme/skills::b", "source", 2, 1],
        ["acme/skills::b", "source", 2, 1],
        ["acme/skills::b", "source", 2, 1],
    ]
    family_rows.extend([["cluster:1", "independent", 2, 1]] * 4)
    _write(
        folder,
        "families.parquet",
        pd.DataFrame(
            family_rows,
            columns=["family_id", "family_kind", "files_without_forks", "repos_without_forks"],
        ),
    )
    _write(
        folder,
        "repo_roles_final.parquet",
        pd.DataFrame(
            {"repo": ["acme/skills", "other/x"], "repo_role": ["registry", "repository"]}
        ),
    )
    _write(
        folder,
        "lineage_roots.parquet",
        pd.DataFrame(
            {
                "lineage_id": ["acme/skills::a", "acme/skills::b"],
                "is_original": [True, False],
            }
        ),
    )
    _write(
        folder,
        "copy_changes.parquet",
        pd.DataFrame(
            {
                "lineage_id": ["acme/skills::a", "acme/skills::a"],
                "state": ["unchanged", "substantive"],
                "current_sha": ["sha-a", "sha-b"],
                "customized": [False, True],
                "headings_added": _lists([[], ["H"]]),
                "commands_added": _lists([[], ["c"]]),
                "names_added": _lists([[], []]),
                "scripts_mentioned_added": _lists([[], []]),
                "network_refs_added": _lists([[], []]),
                "steps_added": [0, 1],
                "permissions_changed": [False, True],
            }
        ),
    )
    _write(
        folder,
        "copy_history_edits.parquet",
        pd.DataFrame(
            {
                "lineage_id": ["acme/skills::a", "acme/skills::a"],
                "change_kind": ["local", "sync"],
                "state": ["substantive", "cosmetic"],
                "headings_added": _lists([["H"], []]),
                "commands_added": _lists([[], []]),
                "network_refs_added": _lists([[], []]),
                "scripts_mentioned_added": _lists([[], []]),
                "permissions_changed": [False, False],
            }
        ),
    )
    _write(
        folder,
        "lineage_changes.parquet",
        pd.DataFrame(
            {
                "lineage_id": ["acme/skills::a"],
                "state": ["substantive"],
                "headings_added": _lists([[]]),
                "commands_added": _lists([[]]),
                "network_refs_added": _lists([[]]),
                "scripts_mentioned_added": _lists([[]]),
                "permissions_changed": [False],
            }
        ),
    )
    _write(
        folder,
        "copy_version_folder_summary.parquet",
        pd.DataFrame(
            [
                ["copy/a", "a.md", 1, "origin", 0, 0, 0, 0, 0, 0, [], []],
                ["copy/b", "b.md", 1, "origin", 0, 0, 0, 0, 0, 0, [], []],
                ["copy/b", "b.md", 2, "origin", 1, 0, 0, 0, 1, 1, ["s.sh", "t.sh"], []],
            ],
            columns=[
                "repo",
                "path",
                "version_seq",
                "target",
                "files_added",
                "files_removed",
                "files_modified",
                "scripts_added",
                "scripts_modified",
                "new_risky_scripts",
                "missing_scripts",
                "hidden_scripts",
            ],
        ),
    )
    _write(
        folder,
        "copy_folder_changes.parquet",
        pd.DataFrame(
            {
                "repo": ["copy/b"],
                "path": ["b.md"],
                "version_seq": [2],
                "target": ["origin"],
                "result": ["modified"],
                "mode_changed": [False],
                "new_risk_flags": _lists([["network"]]),
            }
        ),
    )
    _write(
        folder,
        "copy_files.parquet",
        pd.DataFrame(
            {
                "repo": ["copy/a", "copy/b"],
                "path": ["a.md", "b.md"],
                "lineage_id": ["acme/skills::a", "acme/skills::a"],
                "is_fork": [False, True],
                "repo_role": ["repository", "repository"],
            }
        ),
    )
    _write(
        folder,
        "copy_staleness.parquet",
        pd.DataFrame(
            {
                "repo": ["copy/a", "copy/b"],
                "path": ["a.md", "b.md"],
                "lineage_id": ["acme/skills::a", "acme/skills::a"],
                "as_of_basis": ["registry_snapshot", "registry_snapshot"],
                "copy_state": ["up_to_date", "stale"],
                "current_sha": ["sha-a", "sha-b"],
                "missed_risky": [0, 2],
                "missed_commands": [0, 1],
                "missed_network": [0, 0],
                "missed_scripts": [0, 0],
                "missed_substantive": [0, 4],
                "skill_md_versions_behind": [0, 2],
                "days_stale": [10, 40],
                "stale_after_days": [30, 30],
            }
        ),
    )
    _write(
        folder,
        "sync_events.parquet",
        pd.DataFrame(
            {
                "repo": ["copy/a", "copy/b"],
                "path": ["a.md", "b.md"],
                "lineage_id": ["acme/skills::a", "acme/skills::a"],
                "event_kind": ["copy", "update"],
                "lag_days": [10, 0],
                "versions_skipped": [0, 4],
            }
        ),
    )
    _write(
        folder,
        "skill_language.parquet",
        pd.DataFrame(
            {
                "file_sha": ["t1", "t2", "t3", "t4"],
                "language": ["en", "en", "en", "zh"],
                "is_english": [True, True, True, False],
            }
        ),
    )
    _write(
        folder,
        "registries.parquet",
        pd.DataFrame(
            {
                "repo": ["acme/skills", "other/x"],
                "stars": [200, 5],
                "english_skills": [8, 1],
            }
        ),
    )
    _write(folder, "dev_registries.parquet", pd.DataFrame({"repo": ["acme/skills"]}))
    _write(
        folder,
        "proven_pairs.parquet",
        pd.DataFrame(
            {
                "repo": ["copy/a", "copy/b"],
                "path": ["a.md", "b.md"],
                "lineage_id": ["acme/skills::a", "acme/skills::a"],
                "exact": [True, False],
                "jaccard_found": [False, False],
                "containment_found": [False, False],
                "verified_found": [False, False],
            }
        ),
    )
    _write(
        folder,
        "change_profile.parquet",
        pd.DataFrame({"population": ["copy_vs_origin"], "share_substantive": [0.5]}),
    )


def _build_old(folder: Path) -> Path:
    """A 31-registry folder with the same shares as the tiny new tables, except the profile."""
    old = folder / "old31"
    old.mkdir()
    pd.DataFrame(
        {
            "family_kind": ["registry"] * 4 + ["independent"] * 4,
        }
    ).to_parquet(old / "g1_families.parquet", index=False)
    pd.DataFrame(
        {
            "state": ["unchanged", "substantive"],
            "customized": [False, True],
        }
    ).to_parquet(old / "g2_copy_summary.parquet", index=False)
    pd.DataFrame(
        {"population": ["copy_vs_origin"], "share_substantive": [0.25]}
    ).to_parquet(old / "g2_change_profile.parquet", index=False)
    pd.DataFrame(
        {"versions_behind": [0, 2], "stale": [False, True]}
    ).to_parquet(old / "g3_copy_staleness.parquet", index=False)
    pd.DataFrame({"event_kind": ["copy", "update"]}).to_parquet(
        old / "g3_sync_events.parquet", index=False
    )
    return old


def _shares_sum(frame: pd.DataFrame, column: str, group: list[str] | None = None) -> None:
    """Assert ``column`` sums to 1, overall or inside each group."""
    if group:
        grouped = frame.groupby(group, dropna=False)[column].sum()
        values = list(grouped)
    else:
        values = [frame[column].sum()]
    for value in values:
        assert value == pytest.approx(1, abs=0.002)


def test_family_tables_split_role_and_forks(tmp_path: Path, monkeypatch) -> None:
    """Source texts are half. Fork-free file counts are lower. Shares sum to 1."""
    _use_tmp(monkeypatch, tmp_path)
    _build_inputs(tmp_path)
    tables = rq1_family_tables(True)
    kinds = tables["rq1_family_kinds"]
    _shares_sum(kinds, "texts_share")
    _shares_sum(kinds, "files_share")
    _shares_sum(kinds, "files_without_forks_share")
    source = kinds.loc[kinds["family_kind"].eq("source")].iloc[0]
    assert source["registry_role"] == "registry"
    assert int(source["texts"]) == 4
    assert int(source["files"]) == 4
    assert int(source["files_without_forks"]) == 3
    assert source["texts_share"] == pytest.approx(0.5)
    loose = kinds.loc[kinds["family_kind"].eq("independent")].iloc[0]
    assert loose["registry_role"] == "none"
    sizes = tables["rq1_family_sizes"]
    source_size = sizes.loc[sizes["family_kind"].eq("source")].iloc[0]
    assert source_size["texts_median"] == pytest.approx(2)
    assert source_size["texts_p90"] == pytest.approx(2.8)
    assert int(source_size["texts_max"]) == 3
    assert source_size["depth_p90"] == pytest.approx(3.6)
    top = tables["rq1_top_families"]
    assert list(top["family_id"]) == ["cluster:1", "acme/skills::b", "acme/skills::a"]
    assert len(top) <= 20
    origins = tables["rq1_registry_original_vs_rehosted"]
    _shares_sum(origins, "share")
    assert set(origins["lineage_kind"]) == {"original", "rehosted"}
    assert int(origins["lineages"].sum()) == 2


def test_change_tables_have_expected_shares(tmp_path: Path, monkeypatch) -> None:
    """One of two copies is substantive and customised. Only the latest folder row counts."""
    _use_tmp(monkeypatch, tmp_path)
    _build_inputs(tmp_path)
    tables = rq2_change_tables(True)
    states = tables["rq2_change_states"]
    _shares_sum(states, "share", ["unit", "registry_role"])
    files = states.loc[states["unit"].eq("file")]
    assert int(files.loc[files["state"].eq("substantive"), "count"].iloc[0]) == 1
    custom = tables["rq2_customisation"].iloc[0]
    assert custom["registry_role"] == "registry"
    assert custom["customized_share"] == pytest.approx(0.5)
    assert custom["headings_share"] == pytest.approx(0.5)
    assert custom["names_share"] == pytest.approx(0)
    assert int(custom["steps"]) == 1
    profile = tables["rq2_change_profile"].set_index("population")
    assert list(profile.index) == [
        "registry_update",
        "copy_local_edit",
        "copy_sync",
        "copy_vs_origin",
    ]
    assert profile.loc["copy_vs_origin", "share_substantive"] == pytest.approx(0.5)
    assert profile.loc["copy_sync", "share_substantive"] == pytest.approx(0)
    assert profile.loc["registry_update", "share_substantive"] == pytest.approx(1)
    folders = tables["rq2_folder_changes"].iloc[0]
    assert int(folders["copies"]) == 2
    assert folders["differs_share"] == pytest.approx(0.5)
    assert folders["changed_scripts_share"] == pytest.approx(0.5)
    assert folders["hidden_scripts_share"] == pytest.approx(0)
    risky = tables["rq2_risky_scripts"].set_index("flag")
    assert risky.loc["any", "share"] == pytest.approx(0.5)
    assert risky.loc["network", "share"] == pytest.approx(0.5)


def test_staleness_tables_split_state_and_misses(tmp_path: Path, monkeypatch) -> None:
    """Two snapshot copies, one stale. State shares sum to 1. One copy missed a risky update."""
    _use_tmp(monkeypatch, tmp_path)
    _build_inputs(tmp_path)
    tables = rq3_staleness_tables(True)
    states = tables["rq3_copy_states"]
    _shares_sum(states, "share", ["registry_role", "as_of_basis", "copy_repo_role"])
    assert set(states["copy_state"]) == {"up_to_date", "stale"}
    assert states["copy_repo_role"].eq("repository").all()
    assert int(states["stale_after_days"].iloc[0]) == 30
    missed = tables["rq3_missed_risky"].set_index("kind")
    assert missed.loc["risky", "share"] == pytest.approx(0.5)
    assert int(missed.loc["risky", "events"]) == 2
    assert missed.loc["network", "share"] == pytest.approx(0)
    assert int(missed.loc["substantive", "events"]) == 4
    events = tables["rq3_sync_events"]
    _shares_sum(events, "share", ["registry_role", "copy_repo_role"])
    update = events.loc[events["event_kind"].eq("update")].iloc[0]
    assert int(update["versions_skipped_max"]) == 4
    assert update["lag_days_median"] == pytest.approx(0)


def test_scope_tables_keep_the_dev_registry_and_recall(tmp_path: Path, monkeypatch) -> None:
    """Language shares sum to 1. Dev mode drops the registry that is not on the dev list."""
    _use_tmp(monkeypatch, tmp_path)
    _build_inputs(tmp_path)
    tables = scope_tables(True)
    _shares_sum(tables["scope_language"], "share")
    chinese = tables["scope_language"].loc[tables["scope_language"]["language"].eq("zh")].iloc[0]
    assert bool(chinese["is_english"]) is False
    assert chinese["share"] == pytest.approx(0.25)
    registries = tables["scope_registries"]
    assert list(registries["repo"]) == ["acme/skills"]
    assert registries.iloc[0]["repo_role"] == "registry"
    assert registries.iloc[0]["english_skills_share"] == pytest.approx(1)
    recall = tables["scope_link_recall"].set_index("method")
    assert recall.loc["exact", "recall"] == pytest.approx(0.5)
    assert recall.loc["exact", "recall_without_forks"] == pytest.approx(1)
    assert recall.loc["any_text_method", "recall"] == pytest.approx(0.5)
    assert int(recall.loc["minhash_jaccard", "found"]) == 0


def test_compare_names_the_old_run_and_returns_none_without_it(tmp_path: Path, monkeypatch) -> None:
    """None when no old folder exists. With a 31 folder, shares and the note are filled."""
    _use_tmp(monkeypatch, tmp_path)
    _build_inputs(tmp_path)
    monkeypatch.setattr("skill_genealogy.summarize_results._old_genealogy_roots", lambda: [])
    assert compare_with_old_genealogy(True) is None
    old = _build_old(tmp_path)
    monkeypatch.setattr(
        "skill_genealogy.summarize_results._old_genealogy_roots",
        lambda: [("31", old)],
    )
    compared = compare_with_old_genealogy(True)
    assert compared is not None
    assert list(compared.columns) == ["measure", "old_31_or_100", "new", "note"]
    by_measure = compared.set_index("measure")
    assert by_measure.loc["text_share_registry_family", "old_31_or_100"] == "31: 0.500"
    assert by_measure.loc["text_share_registry_family", "new"] == pytest.approx(0.5)
    assert "English" in by_measure.loc["text_share_independent", "note"]
    assert by_measure.loc["file_share_substantive", "new"] == pytest.approx(0.5)
    assert by_measure.loc["profile_substantive_copy_vs_origin", "old_31_or_100"] == "31: 0.250"
    assert by_measure.loc["share_stale_after_30_days", "new"] == pytest.approx(0.5)
    assert by_measure.loc["event_share_downgrade", "old_31_or_100"] == "31: 0.000"
    assert by_measure["note"].map(lambda text: "dev sample" in text).all()


def test_write_tables_and_main_skip(tmp_path: Path, monkeypatch, capsys) -> None:
    """CSVs land in the results folder. A second run prints skip and does not need new inputs."""
    _use_tmp(monkeypatch, tmp_path)
    _build_inputs(tmp_path)
    monkeypatch.setattr("skill_genealogy.summarize_results._old_genealogy_roots", lambda: [])
    frame = pd.DataFrame({"texts": [1]})
    write_tables({"rq1_family_kinds": frame}, tmp_path / "out")
    written = pd.read_csv(tmp_path / "out" / "rq1_family_kinds.csv")
    assert list(written["texts"]) == [1]
    monkeypatch.setattr(sys, "argv", ["summarize_results", "--dev"])
    main()
    results = tmp_path / "results"
    assert (results / "rq1_family_kinds.csv").is_file()
    assert (results / "rq3_sync_events.csv").is_file()
    assert not (results / "compare_old_genealogy.csv").is_file()
    main()
    captured = capsys.readouterr()
    assert "skip" in captured.out
