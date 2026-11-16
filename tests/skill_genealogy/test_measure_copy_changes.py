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


def test_inherited_commit_is_left_out_and_pairs_skip_it():
    history = _history(["v1", "mine", "v2"], inherited=[False, True, False])
    edits = classify_history_edits(history, VERSIONS, EDIT_TEXTS)
    assert edits["from_blob"].tolist() == ["v1"]
    assert edits["to_blob"].tolist() == ["v2"]


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
