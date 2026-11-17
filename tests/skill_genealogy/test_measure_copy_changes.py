"""Tests for stage 9a (copy against origin) with tiny hand-made tables."""

from __future__ import annotations

import math
from collections import ChainMap

import pandas as pd

from skill_genealogy.measure_copy_changes import (
    COPY_CHANGE_COLUMNS,
    HISTORY_EDIT_COLUMNS,
    PROFILE_COLUMNS,
    build_change_profile,
    classify_history_edits,
    compare_copy_with_origin,
    current_text_of_copy,
    heading_overlap_with_registry,
    headings_added_after,
    index_later_headings,
    select_copies,
)

BASE = "---\nname: demo\n---\n# Demo\n" + "\n".join(
    f"Line {number} of the skill." for number in range(40)
)


def _copy_row(**changes: object) -> dict[str, object]:
    """One copy file row with the columns the comparison reads."""
    row: dict[str, object] = {
        "repo": "acme/tools",
        "path": "skills/demo/SKILL.md",
        "file_sha": "dataset",
        "lineage_id": "reg/skills::demo",
        "link": "exact",
        "origin_seq": 1,
    }
    row.update(changes)
    return row


def _compare(current: str, origin: str = BASE, later: dict | None = None) -> pd.Series:
    """Compare one copy whose current text is ``current`` with ``origin``."""
    texts = {"origin": origin, "current": current}
    return compare_copy_with_origin(
        pd.DataFrame([_copy_row()]),
        {("acme/tools", "skills/demo/SKILL.md"): "current"},
        texts,
        {("reg/skills::demo", 1): "origin"},
        later or {},
    ).iloc[0]


def test_unchanged_copy():
    row = _compare(BASE)
    assert row["state"] == "unchanged"
    assert not row["customized"]
    assert math.isnan(row["heading_overlap"])
    assert row["current_basis"] == "latest_history"


def test_front_matter_only_edit_keeps_body_unchanged_and_flags_permissions():
    edited = BASE.replace("name: demo", "name: demo\nallowed-tools: Bash")
    row = _compare(edited)
    assert row["state"] == "unchanged"
    assert row["permissions_changed"]
    assert row["allowed_tools_added"] == ["Bash"]


def test_project_name_added_only_in_front_matter_is_not_customization():
    edited = BASE.replace("name: demo", "name: demo\nmetadata:\n  acme:\n    tags: [x]")
    row = _compare(edited)
    assert row["state"] == "unchanged"
    assert "acme" in row["names_added"]  # names_added still reads the whole file
    assert not row["customized"]


def test_cosmetic_copy():
    row = _compare(BASE.replace("Line 3 of the skill.", "Line 3 of this skill."))
    assert row["state"] == "cosmetic"
    assert not row["customized"]


def test_substantive_copy_with_section_and_project_name_is_customized():
    edited = (
        BASE
        + "\n## Deploy\nRun the acme release for acme.\n"
        + "\n".join(f"Extra {number}" for number in range(10))
    )
    row = _compare(edited)
    assert row["state"] == "substantive"
    assert row["customized"]
    assert "## Deploy" in row["headings_added"]
    assert "acme" in row["names_added"]


def test_removed_step_alone_is_not_customization():
    origin = BASE + "\n1. First\n2. Second"
    row = _compare(BASE + "\n1. First", origin=origin)
    assert row["steps_added"] < 0
    assert not row["customized"]


def test_missing_origin_or_current_text_leaves_the_copy_out():
    frame = compare_copy_with_origin(
        pd.DataFrame([_copy_row()]), {}, {"origin": BASE}, {("reg/skills::demo", 1): "origin"}, {}
    )
    assert frame.empty
    assert list(frame.columns) == COPY_CHANGE_COLUMNS


def test_heading_overlap_cases():
    assert heading_overlap_with_registry(["Deploy"], ["Deploy", "Tests"]) == 0.5
    assert heading_overlap_with_registry(["Deploy", "Deploy"], ["deploy "]) == 1.0
    assert heading_overlap_with_registry(["Deploy"], []) == 0.0
    assert math.isnan(heading_overlap_with_registry([], []))
    # A copy that added no heading has no direction, even when the registry added some.
    assert math.isnan(heading_overlap_with_registry([], ["Tests"]))


def test_headings_added_after_skips_the_update_that_made_the_origin():
    changes = pd.DataFrame(
        {
            "lineage_id": ["L", "L", "L"],
            "to_seq": [2, 3, 4],
            "headings_added": [["Old"], ["Deploy"], ["Tests"]],
        }
    )
    index = index_later_headings(changes)
    assert headings_added_after(index, "L", 2) == ["Deploy", "Tests"]
    assert headings_added_after(index, "other", 1) == []


def test_heading_overlap_is_written_on_the_copy_row():
    edited = BASE + "\n## Deploy\nSteps."
    later = {"reg/skills::demo": [(2, ["## Deploy", "## Tests"])]}
    assert _compare(edited, later=later)["heading_overlap"] == 0.5


def test_current_text_newest_history_then_dataset_then_missing():
    copy = _copy_row()
    key = ("acme/tools", "skills/demo/SKILL.md")
    assert current_text_of_copy(copy, {key: "new"}, {"new": "N", "dataset": "D"}) == (
        "N",
        "latest_history",
    )
    assert current_text_of_copy(copy, {key: "gone"}, {"dataset": "D"}) == ("D", "dataset")
    assert current_text_of_copy(copy, {}, {"dataset": "D"}) == ("D", "dataset")
    assert current_text_of_copy(copy, {}, {}) == ("", "missing")


def _history(
    blobs: list[str], inherited: list[bool] | None = None, status: list[str] | None = None
):
    """History rows of one copy file with one commit per blob."""
    count = len(blobs)
    return pd.DataFrame(
        {
            "repo": ["acme/tools"] * count,
            "path": ["skills/demo/SKILL.md"] * count,
            "lineage_id": ["reg/skills::demo"] * count,
            "blob": blobs,
            "commit": [f"c{number}" for number in range(count)],
            "commit_seq": list(range(count)),
            "commit_date": pd.to_datetime(
                [f"2026-0{number + 1}-01" for number in range(count)], utc=True
            ),
            "status": status or ["A"] + ["M"] * (count - 1),
            "inherited": inherited or [False] * count,
        }
    )


VERSIONS = pd.DataFrame(
    {"lineage_id": ["reg/skills::demo", "reg/other::demo"], "blob": ["v2", "w1"]}
)
EDIT_TEXTS = {
    "v1": BASE,
    "mine": BASE + "\nA local line.",
    "v2": BASE + "\nA local line.\nA registry line.",
    "w1": BASE + "\nAnother source.",
}


def test_history_edits_local_then_sync():
    edits = classify_history_edits(_history(["v1", "mine", "v2"]), VERSIONS, EDIT_TEXTS)
    assert edits["change_kind"].tolist() == ["local", "sync"]
    assert edits["from_blob"].tolist() == ["v1", "mine"]
    assert list(edits.columns) == HISTORY_EDIT_COLUMNS


def test_history_edit_to_another_lineage_is_other_source():
    edits = classify_history_edits(_history(["v1", "w1"]), VERSIONS, EDIT_TEXTS)
    assert edits["change_kind"].tolist() == ["other_source"]


def test_inherited_commit_is_left_out_but_the_next_edit_starts_from_it():
    history = _history(["v1", "mine", "v2"], inherited=[False, True, False])
    edits = classify_history_edits(history, VERSIONS, EDIT_TEXTS)
    assert edits["from_blob"].tolist() == ["mine"]
    assert edits["to_blob"].tolist() == ["v2"]


def test_first_local_edit_of_a_fork_is_kept():
    # A fork: two inherited registry commits, then the copy's own edit.
    history = _history(["v1", "v2", "mine"], inherited=[True, True, False])
    texts = {**EDIT_TEXTS, "mine": EDIT_TEXTS["v2"] + "\nMy fork line."}
    edits = classify_history_edits(history, VERSIONS, texts)
    assert edits["from_blob"].tolist() == ["v2"]
    assert edits["to_blob"].tolist() == ["mine"]
    assert edits["change_kind"].tolist() == ["local"]


def test_same_blob_deletes_and_missing_texts_are_not_edits():
    same = classify_history_edits(_history(["v1", "v1", "mine"]), VERSIONS, EDIT_TEXTS)
    assert same["to_blob"].tolist() == ["mine"]
    deleted = classify_history_edits(
        _history(["v1", "mine", "v2"], status=["A", "D", "M"]), VERSIONS, EDIT_TEXTS
    )
    assert deleted["from_blob"].tolist() == ["v1"]
    assert deleted["to_blob"].tolist() == ["v2"]
    missing = classify_history_edits(_history(["v1", "unknown"]), VERSIONS, EDIT_TEXTS)
    assert missing.empty
    assert missing.attrs["no_text"] == 1


def test_whitespace_only_edit_is_not_an_edit():
    # Trailing spaces and an extra blank line: stage 4a would not count a new version.
    texts = {**EDIT_TEXTS, "spaced": EDIT_TEXTS["v1"].replace("\n", "  \n", 3) + "\n\n"}
    edits = classify_history_edits(_history(["v1", "spaced", "mine"]), VERSIONS, texts)
    assert edits["from_blob"].tolist() == ["spaced"]
    assert edits["to_blob"].tolist() == ["mine"]
    assert edits.attrs["whitespace_only"] == 1


def test_history_edit_flags_a_permission_change():
    texts = {"v1": BASE, "mine": BASE.replace("name: demo", "name: demo\nallowed-tools: Bash")}
    edits = classify_history_edits(_history(["v1", "mine"]), VERSIONS, texts)
    assert edits["permissions_changed"].tolist() == [True]
    assert edits["state"].tolist() == ["unchanged"]


def test_chain_map_lookup_works_for_texts():
    texts = ChainMap({"v1": BASE}, {"mine": BASE + "\nx"})
    assert len(classify_history_edits(_history(["v1", "mine"]), VERSIONS, texts)) == 1


def _changes(states: list[str], headings: list[list[str]], permissions: list[bool]) -> pd.DataFrame:
    """A change table with the columns the profile reads."""
    count = len(states)
    return pd.DataFrame(
        {
            "state": states,
            "headings_added": headings,
            "commands_added": [[]] * count,
            "network_refs_added": [[]] * count,
            "scripts_mentioned_added": [["run.sh"]] + [[]] * (count - 1),
            "permissions_changed": permissions,
        }
    )


def test_profile_shares_and_populations():
    registry = _changes(["substantive", "cosmetic"], [["A"], []], [False, False])
    edits = _changes(
        ["substantive", "unchanged", "cosmetic"], [[], [], ["B"]], [True, False, False]
    )
    edits["change_kind"] = ["local", "sync", "other_source"]
    copies = _changes(["substantive"] * 4, [["A"]] * 2 + [[]] * 2, [False] * 4)
    profile = build_change_profile(registry, edits, copies).set_index("population")
    assert list(build_change_profile(registry, edits, copies).columns) == PROFILE_COLUMNS
    assert profile.loc["registry_update", "changes"] == 2
    assert profile.loc["registry_update", "share_substantive"] == 0.5
    assert profile.loc["registry_update", "share_headings_added"] == 0.5
    assert profile.loc["copy_local_edit", ["changes", "share_permissions_changed"]].tolist() == [
        1,
        1.0,
    ]
    assert profile.loc["copy_sync", ["changes", "share_substantive"]].tolist() == [1, 0.0]
    assert profile.loc["copy_vs_origin", "share_scripts_added"] == 0.25
    assert profile["changes"].sum() == 2 + 1 + 1 + 4  # other_source is not a population


def test_empty_population_has_zero_changes_and_null_shares():
    empty = _changes(["substantive"], [[]], [False]).iloc[0:0]
    profile = build_change_profile(empty, pd.DataFrame(), empty).set_index("population")
    assert profile.loc["copy_sync", "changes"] == 0
    assert math.isnan(profile.loc["copy_sync", "share_substantive"])


def test_select_copies_keeps_dated_english_copies_outside_registries():
    files = pd.DataFrame(
        {
            "file_sha": ["keep", "early", "noorigin", "self", "dutch"],
            "origin_seq": [1, 1, None, 1, 1],
            "copied_before_release": [False, True, False, False, False],
        }
    )
    families = pd.DataFrame(
        {"file_sha": files["file_sha"], "registry_self": [False, False, False, True, False]}
    )
    english = {"keep", "early", "noorigin", "self"}
    assert select_copies(files, families, english)["file_sha"].tolist() == ["keep"]


def test_null_text_is_missing_not_empty():
    from skill_genealogy.measure_copy_changes import only_stored_text

    assert only_stored_text([("a", None), ("b", "Body")]) == {"b": "Body"}


def test_save_outputs_keeps_old_files_when_a_write_fails(tmp_path, monkeypatch):
    import pytest

    from skill_genealogy import measure_copy_changes as module

    paths = {
        name: tmp_path / f"{name}.parquet"
        for name in ("copy_changes", "copy_history_edits", "change_profile")
    }
    monkeypatch.setattr(module, "output_paths", lambda dev: paths)
    empty = pd.DataFrame({"repo": ["a"], "path": ["p"], "commit_date": [1], "commit": ["c"]})
    module.save_outputs(empty, empty, empty, True)
    before = paths["change_profile"].read_bytes()
    original = module.write_parquet

    def failing(frame, path, sort_by=None):
        if path.name.startswith("change_profile"):
            raise OSError("disk full")
        original(frame, path, sort_by)

    monkeypatch.setattr(module, "write_parquet", failing)
    with pytest.raises(OSError):
        module.save_outputs(empty.assign(repo="changed"), empty, empty, True)
    assert paths["change_profile"].read_bytes() == before
    assert pd.read_parquet(paths["copy_changes"])["repo"].tolist() == ["a"]
    assert not list(tmp_path.glob("*.tmp"))


def test_stored_text_queries_ignore_a_null_text(tmp_path):
    from skill_genealogy.measure_copy_changes import load_stored_texts, stored_shas

    path = tmp_path / "texts.parquet"
    pd.DataFrame({"sha": ["a", "b", "c"], "text": ["Body", None, "Other"]}).to_parquet(path)
    wanted = {"a", "b", "missing"}
    assert load_stored_texts(path, wanted) == {"a": "Body"}
    assert stored_shas(path, wanted) == {"a"}


def test_copies_needing_dataset_text_are_those_without_a_stored_newest_blob(tmp_path, monkeypatch):
    from skill_genealogy import measure_copy_changes as module

    path = tmp_path / "texts.parquet"
    pd.DataFrame({"sha": ["stored", "nulltext"], "text": ["Body", None]}).to_parquet(path)
    monkeypatch.setattr(module, "_text_path", lambda dev, which: path)
    copies = pd.DataFrame({"repo": ["r"] * 4, "path": ["a", "b", "c", "d"]})
    newest = {("r", "a"): "stored", ("r", "b"): "nulltext", ("r", "c"): "absent"}  # d: no history
    needs = module.find_copies_needing_dataset_text(True, copies, newest)
    assert needs.tolist() == [False, True, True, True]


def test_progress_prints_batches_and_writes_the_file(tmp_path, capsys):
    import json

    from skill_genealogy.measure_copy_changes import Progress

    path = tmp_path / "progress.json"
    progress = Progress(True, path=path)
    progress.stage("compare", "2 batches")
    progress.start_compare()
    progress.batch(1, 2, 50, 100, 3, "history")
    state = json.loads(path.read_text())
    assert state["copies_done"] == 50
    assert state["percent"] == 50.0
    assert state["eta_seconds"] is not None
    progress.finish()
    assert json.loads(path.read_text())["done"] is True
    out = capsys.readouterr().out
    assert "compare: 2 batches" in out
    assert "batch 1/2 (history): copies 50/100 (50.0%)" in out


# Stage 9b: copy version similarity


def _registry_versions() -> pd.DataFrame:
    """Registry v1 10 Jan, v2 1 Mar, v3 15 May (the card's worked example)."""
    return pd.DataFrame(
        {
            "lineage_id": ["L"] * 3,
            "version_seq": [1, 2, 3],
            "blob": ["v1", "v2", "v3"],
            "release_date": pd.to_datetime(["2026-01-10", "2026-03-01", "2026-05-15"], utc=True),
        }
    )


def test_comparison_targets_follow_the_c1_to_c4_timeline():
    from skill_genealogy.measure_copy_changes import (
        index_lineage_releases,
        pick_comparison_targets,
        targets_from_index,
    )

    versions = _registry_versions()
    dates = ["2026-02-01", "2026-03-10", "2026-04-20", "2026-06-30"]
    expected = [(None, 1, 1, 3), (1, 1, 2, 3), (2, 1, 2, 3), (3, 1, 3, 3)]
    releases = index_lineage_releases(versions)
    for seq, (date, want) in enumerate(zip(dates, expected, strict=True), start=1):
        copy = {"lineage_id": "L", "version_seq": seq, "commit_date": pd.Timestamp(date, tz="UTC")}
        targets = pick_comparison_targets(copy, versions, 1)
        got = (
            targets["prev_version_seq"],
            targets["origin_seq"],
            targets["at_date_seq"],
            targets["newest_seq"],
        )
        assert got == want
        assert targets_from_index(releases, "L", seq, copy["commit_date"], 1) == targets


def test_targets_before_every_release_or_undated_have_no_at_date():
    from skill_genealogy.measure_copy_changes import (
        index_lineage_releases,
        pick_comparison_targets,
        targets_from_index,
    )

    versions = _registry_versions()
    releases = index_lineage_releases(versions)
    for date in (pd.Timestamp("2026-01-01", tz="UTC"), pd.NaT):
        copy = {"lineage_id": "L", "version_seq": 1, "commit_date": date}
        targets = pick_comparison_targets(copy, versions, 2)
        assert targets["at_date_seq"] is None and targets["newest_seq"] == 3
        assert targets_from_index(releases, "L", 1, date, 2) == targets


def test_fast_target_lookup_agrees_with_the_plain_one_on_microsecond_dates():
    from skill_genealogy.measure_copy_changes import (
        index_lineage_releases,
        pick_comparison_targets,
        targets_from_index,
    )

    versions = _registry_versions()
    versions["release_date"] = versions["release_date"].dt.as_unit("us")
    releases = index_lineage_releases(versions)
    days = pd.date_range("2025-12-25", "2026-07-01", freq="7D", tz="UTC")
    for date in list(days) + list(versions["release_date"]):
        copy = {"lineage_id": "L", "version_seq": 2, "commit_date": date}
        assert targets_from_index(releases, "L", 2, date, 1) == pick_comparison_targets(
            copy, versions, 1
        )


def test_score_pair_on_identical_text_is_one():
    from skill_genealogy.measure_copy_changes import score_pair

    scores = score_pair(BASE, BASE)
    assert scores == {
        "jaccard": 1.0,
        "containment": 1.0,
        "verified": 1.0,
        "lines_added": 0,
        "lines_removed": 0,
        "body_change_pct": 0.0,
    }


def test_score_pair_matches_compare_texts_and_keeps_shared_lines():
    from skill_genealogy.compare_texts import compare_texts, share_of_registry_lines_kept
    from skill_genealogy.measure_copy_changes import score_pair

    edited = BASE.replace("Line 3 of the skill.", "A new line.") + "\nAnother added line here."
    scores = score_pair(BASE, edited)
    change = compare_texts(BASE, edited)
    assert scores["lines_added"] == change.lines_added == 2
    assert scores["lines_removed"] == change.lines_removed == 1
    assert scores["body_change_pct"] == change.body_change_pct
    assert scores["verified"] == share_of_registry_lines_kept(BASE, edited)
    assert 0 < scores["jaccard"] < scores["containment"] < 1
    # A line many owners share still counts: adding one lowers Jaccard.
    with_line = BASE + "\nPlease install the tool first"
    assert score_pair(BASE, with_line)["jaccard"] < 1.0


def test_score_rules_identical_and_too_short():
    """Rule 1: identical texts are 1, even with no body; rule 3: only nothing to measure is null."""
    from skill_genealogy.measure_copy_changes import IDENTICAL_SCORES, PairScorer, score_pair

    tiny = "---\nname: t\n---\nInsert instructions below"
    empty = "---\nname: t\n---\n"
    assert score_pair(empty, empty) == IDENTICAL_SCORES
    scorer = PairScorer({"a": empty, "b": empty.replace("name: t", "name: u"), "c": tiny})
    assert scorer.score("a", "a") == IDENTICAL_SCORES
    scores = scorer.score("a", "b")  # no body on either side, front matter differs
    assert scores["jaccard"] is None and scores["containment"] is None
    assert scores["verified"] is None and scores["body_change_pct"] is None
    added = scorer.score("a", "c")  # body added to an empty template
    assert added["jaccard"] == 0.0 and added["containment"] is None
    assert added["verified"] is None and added["body_change_pct"] is None
    deleted = scorer.score("c", "a")  # whole body deleted: measurable, 0
    assert deleted["jaccard"] == deleted["containment"] == deleted["verified"] == 0.0
    assert scorer.score("c", "c") == IDENTICAL_SCORES


def test_name_note_ignores_project_word_in_old_front_matter():
    """A body line adding the repo word is a name note even when the front matter had it."""
    from skill_genealogy.measure_copy_changes import change_notes_for_step

    old = "---\nname: acme-helper\n---\nUse it."
    notes = change_notes_for_step(old, old + "\nBuilt for ACME.", "acme/tools", "SKILL.md")
    assert [(note["line"], note["kind"]) for note in notes] == [(5, "name")]


def test_one_line_edit_of_long_text_scores_high():
    """A one-line edit of a 25-line text keeps a high Jaccard (the frontend-design case)."""
    from skill_genealogy.measure_copy_changes import score_pair

    lines = [f"Step {n}: write the component and check the colours carefully." for n in range(25)]
    old = "\n".join(lines)
    new = "\n".join(lines[:-1] + ["Use soft colours for every element in the page."])
    scores = score_pair(old, new)
    assert scores["jaccard"] > 0.8 and scores["verified"] == 24 / 25


def _version_inputs():
    copies = pd.DataFrame(
        [
            _copy_row(repo="a/r", file_sha="h2"),
            _copy_row(repo="b/r", file_sha="d1"),
            _copy_row(repo="c/r", file_sha="gone"),
        ]
    )
    copies["lineage_id"] = "L"
    history = pd.DataFrame(
        {
            "repo": ["a/r"] * 4,
            "path": ["skills/demo/SKILL.md"] * 4,
            "blob": ["h1", "h1b", "h2", None],
            "status": ["A", "M", "M", "D"],
            "commit": ["c1", "c2", "c3", "c4"],
            "commit_seq": [1, 2, 3, 4],
            "inherited": [False] * 4,
            "commit_date": pd.to_datetime(
                ["2026-02-01", "2026-03-10", "2026-04-20", "2026-06-30"], utc=True
            ),
        }
    )
    dates = pd.DataFrame(
        {
            "repo": ["b/r"],
            "path": ["skills/demo/SKILL.md"],
            "file_sha": ["d1"],
            "last_commit_at": pd.to_datetime(["2026-04-01"], utc=True),
        }
    )
    texts = {
        "h1": BASE,
        "h1b": BASE + "\n\n",  # whitespace only: not a new version
        "h2": BASE + "\n## Deploy\ncurl https://x",
        "d1": BASE,
        "v1": BASE,
        "v2": BASE + "\nMore.",
        "v3": BASE + "\nMore.\nEven more.",
    }
    return copies, history, dates, texts


def test_copy_versions_collapse_whitespace_and_skip_deletes():
    from skill_genealogy.measure_copy_changes import list_copy_versions

    copies, history, dates, texts = _version_inputs()
    versions = list_copy_versions(copies, history, dates, texts)
    rows = versions[["repo", "version_seq", "blob", "basis"]].values.tolist()
    assert rows == [["a/r", 1, "h1", "history"], ["a/r", 2, "h2", "history"],
                    ["b/r", 1, "d1", "dataset"]]  # fmt: skip
    assert versions.attrs["whitespace_only"] == 1
    assert versions.attrs["no_version"] == 1  # c/r: no history and no dataset text


def test_history_versions_are_numbered_by_date():
    from skill_genealogy.measure_copy_changes import list_copy_versions

    copies, history, dates, texts = _version_inputs()
    shuffled = history.iloc[::-1].reset_index(drop=True)
    versions = list_copy_versions(copies.iloc[:1], shuffled, dates, texts)
    assert versions["blob"].tolist() == ["h1", "h2"]


def test_copy_without_history_is_one_row_with_null_previous():
    import json

    from skill_genealogy.measure_copy_changes import (
        PairScorer,
        _registry_blob_index,
        index_lineage_releases,
        index_lineages_by_blob,
        list_copy_versions,
        score_copy_versions,
    )

    copies, history, dates, texts = _version_inputs()
    registry = _registry_versions()
    versions = list_copy_versions(copies, history, dates, texts)
    scored = score_copy_versions(
        versions,
        index_lineage_releases(registry),
        _registry_blob_index(registry),
        index_lineages_by_blob(registry),
        PairScorer(texts),
    )
    dataset_row = scored.loc[scored["repo"] == "b/r"].iloc[0]
    assert dataset_row["change_kind"] == "copy"
    assert pd.isna(dataset_row["prev_jaccard"]) and pd.isna(dataset_row["prev_lines_added"])
    assert dataset_row["origin_jaccard"] == 1.0
    assert dataset_row["at_date_seq"] == 2 and dataset_row["newest_seq"] == 3
    assert json.loads(dataset_row["change_notes"]) == []
    second = scored.loc[(scored["repo"] == "a/r") & (scored["version_seq"] == 2)].iloc[0]
    assert second["change_kind"] == "local"
    assert second["prev_lines_added"] == 2
    assert second["at_date_seq"] == 2
    assert [note["kind"] for note in json.loads(second["change_notes"])] == ["heading", "network"]


def test_change_notes_mark_added_heading_and_network_lines():
    from skill_genealogy.measure_copy_changes import change_notes_for_step

    old = "Keep this line."
    notes = change_notes_for_step(old, old + "\n## Deploy\ncurl https://x", "a/r", "SKILL.md")
    assert notes == [
        {"line": 2, "kind": "heading", "text": "## Deploy"},
        {"line": 3, "kind": "network", "text": "⚠ curl https://x"},
    ]
    fenced = old + "\n```\n## Deploy\n```"
    assert change_notes_for_step(old, fenced, "a/r", "SKILL.md") == []
    assert change_notes_for_step(old, old, "a/r", "SKILL.md") == []


def test_change_notes_count_lines_after_front_matter_and_flag_permissions():
    from skill_genealogy.measure_copy_changes import change_notes_for_step

    old = "---\nname: x\n---\nKeep."
    new = "---\nname: x\nallowed-tools: Bash\n---\nKeep.\n```bash\nnpm test\n```\nFor acme."
    notes = change_notes_for_step(old, new, "acme/tools", "skills/x/SKILL.md")
    assert [(note["line"], note["kind"]) for note in notes] == [
        (3, "permission"),
        (7, "command"),
        (9, "name"),
    ]


def test_drift_and_share_behind():
    from skill_genealogy.measure_copy_changes import drift_of_origin_jaccard, share_behind_at_date

    rows = pd.DataFrame(
        {
            "repo": ["a", "a", "b"],
            "path": ["p"] * 3,
            "lineage_id": ["L"] * 3,
            "version_seq": [1, 2, 1],
            "origin_seq": [1, 1, 1],
            "at_date_seq": pd.array([1, 2, None], dtype="Int64"),
            "blob": ["v1", "mine", "v1"],
            "origin_jaccard": [1.0, 0.5, 1.0],
        }
    )
    assert drift_of_origin_jaccard(rows)["drift"] == -0.5
    assert share_behind_at_date(rows, {("L", 1): "v1", ("L", 2): "v2"}) == 0.5


def test_save_version_outputs_replaces_the_file(tmp_path, monkeypatch):
    from skill_genealogy import measure_copy_changes as module

    target = tmp_path / "copy_version_similarity.parquet"
    monkeypatch.setattr(module, "version_output_path", lambda dev: target)
    frame = pd.DataFrame({"repo": ["b", "a"], "path": ["p", "p"], "version_seq": [1, 1]})
    module.save_version_outputs(frame, True)
    assert pd.read_parquet(target)["repo"].tolist() == ["a", "b"]
    assert not list(tmp_path.glob("*.tmp"))
