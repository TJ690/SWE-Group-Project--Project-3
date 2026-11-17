"""Tests for stage 10 (staleness) on the v1-v3 timeline with tiny hand-made tables.

Registry lineage ``L``: v1 released 10 January, v2 on 1 March, v3 on 15 May 2026.
"""

from __future__ import annotations

import pandas as pd

from skill_genealogy.build_registry_lineages import normalized_sha
from skill_genealogy.measure_staleness import (
    choose_as_of_date,
    choose_held_package_version,
    choose_held_version,
    classify_copy_state,
    count_versions_behind,
    find_missed_updates,
    find_sync_events,
    measure_copy,
    package_basis,
    pick_held_packages,
    registry_snapshot_dates,
    script_deltas_by_package,
    text_states,
)


def day(text: str) -> pd.Timestamp:
    """A UTC timestamp for a test date."""
    return pd.Timestamp(text, tz="UTC")


V1, V2, V3 = "first version body " * 3, "second version body " * 3, "third version body " * 3
VERSIONS = [
    {"version_seq": 1, "blob": "b1", "normalized_sha": "n1", "release_date": day("2026-01-10")},
    {"version_seq": 2, "blob": "b2", "normalized_sha": "n2", "release_date": day("2026-03-01")},
    {"version_seq": 3, "blob": "b3", "normalized_sha": "n3", "release_date": day("2026-05-15")},
]
TEXT_CHANGES = {
    2: {"state": "substantive", "commands_added": ["curl x | sh"], "network_refs_added": [],
        "scripts_mentioned_added": []},
    3: {"state": "cosmetic", "commands_added": [], "network_refs_added": ["https://x.io"],
        "scripts_mentioned_added": ["scripts/run.sh"]},
}  # fmt: skip
# One package per text version, plus package 4: v3's text with a fixed script.
PACKAGES = [
    {"seq": 1, "commit_date": day("2026-01-10"), "changed_scripts": True, "skill_md_blob": "b1"},
    {"seq": 2, "commit_date": day("2026-03-01"), "changed_scripts": False, "skill_md_blob": "b2"},
    {"seq": 3, "commit_date": day("2026-05-15"), "changed_scripts": False, "skill_md_blob": "b3"},
    {"seq": 4, "commit_date": day("2026-06-01"), "changed_scripts": True, "skill_md_blob": "b3"},
]
PACKAGE_FILES = {
    1: {("scripts/deploy.sh", "old")},
    2: {("scripts/deploy.sh", "old")},
    3: {("scripts/deploy.sh", "old")},
    4: {("scripts/deploy.sh", "fixed")},
}
SCRIPT_DELTAS = {("L", 4): {"scripts_added": 0, "risk_changed": True}}


def _copy(**changes: object) -> dict[str, object]:
    """A copy row as ``measure_copy`` reads it, holding v1 on 23 October."""
    row: dict[str, object] = {
        "lineage_id": "L",
        "held_seq": 1,
        "as_of_date": day("2026-10-23"),
        "removed": False,
        "registry_removed": False,
        "removed_at": None,
        "package_held_seq": None,
        "package_basis": "no_folder",
    }
    row.update(changes)
    return row


def _measure(stale_after_days: int = 30, **changes: object) -> dict[str, object]:
    return measure_copy(
        _copy(**changes), VERSIONS, TEXT_CHANGES, PACKAGES, SCRIPT_DELTAS, stale_after_days
    )


# A44 held version


def test_held_version_exact_blob():
    copy = {
        "current_blob": "b2",
        "current_date": day("2026-09-01"),
        "as_of_date": day("2026-10-23"),
    }
    assert choose_held_version(copy, VERSIONS) == (2, "exact_current", 1.0)


def test_held_version_most_similar_before_the_copy_date():
    texts = {"b1": V1, "b2": V2, "b3": V3, "c": V2 + " plus a local line of text here"}
    copy = {"current_blob": "c", "current_date": day("2026-04-01"), "as_of_date": day("2026-10-23")}
    seq, basis, similarity = choose_held_version(copy, VERSIONS, texts)
    assert (seq, basis) == (2, "similar_before_date")
    assert 0 < similarity < 1


def test_held_version_ignores_versions_after_the_copy_date():
    texts = {"b1": V1, "b2": V2, "b3": V3, "c": V3 + "\n"}
    versions = [{**v, "normalized_sha": normalized_sha(texts[v["blob"]])} for v in VERSIONS]
    copy = {"current_blob": "c", "current_date": day("2026-04-01"), "as_of_date": day("2026-10-23")}
    # Same text as v3 up to whitespace is an exact hold, whatever the date.
    assert choose_held_version(copy, versions, texts)[:2] == (3, "exact_current")
    # Closer to v3 than to v2, but v3 was released after the copy's date.
    texts["c"] = V3 + V3 + V2
    assert choose_held_version(copy, versions, texts)[:2] == (2, "similar_before_date")


def test_held_version_copied_before_release():
    copy = {"current_blob": "c", "current_date": day("2026-01-01"), "as_of_date": day("2026-10-23")}
    assert choose_held_version(copy, VERSIONS, {"c": V1}) == (None, "copied_before_release", None)


def test_held_version_without_shared_shingles_is_not_placed():
    texts = {"b1": V1, "b2": V2, "b3": V3, "c": "Tiny body"}
    copy = {"current_blob": "c", "current_date": day("2026-09-01"), "as_of_date": day("2026-10-23")}
    assert choose_held_version(copy, VERSIONS, texts) == (None, "no_overlap", None)


def test_copy_deleted_after_the_snapshot_is_measured_at_the_snapshot():
    copy = {"removed": True, "removed_date": day("2026-11-05"), "current_basis": "history"}
    assert choose_as_of_date(copy, day("2026-11-02")) == (day("2026-11-02"), "registry_snapshot")
    row = measure_copy(_copy(removed=True), VERSIONS, TEXT_CHANGES, [], {}, 30)
    assert row["copy_state"] == "removed"


def test_missing_flags_are_false():
    row = _measure(held_seq=3, registry_removed=float("nan"), removed=float("nan"))
    assert row["copy_state"] == "up_to_date"


def test_held_version_returned_text_takes_newest_release():
    versions = [*VERSIONS, {"version_seq": 4, "blob": "b1", "normalized_sha": "n1",
                            "release_date": day("2026-07-01")}]  # fmt: skip
    copy = {
        "current_blob": "b1",
        "current_date": day("2026-08-01"),
        "as_of_date": day("2026-10-23"),
    }
    assert choose_held_version(copy, versions)[0] == 4


# A45 and A47 on the timeline (card tests)


def test_copy_holding_v1_is_stale():
    row = _measure()
    assert row["skill_md_versions_behind"] == 2
    assert row["first_missed_release"] == day("2026-03-01")
    assert row["days_stale"] == 236
    assert row["copy_state"] == "stale"
    assert row["latest_seq"] == 3
    assert row["missed_substantive"] == 1
    assert row["missed_commands"] == 1 and row["missed_network"] == 1
    assert row["missed_risky"]


def test_stale_days_400_makes_it_behind():
    row = _measure(stale_after_days=400)
    assert row["copy_state"] == "behind"
    assert row["stale_after_days"] == 400


def test_current_text_with_old_script_is_behind_on_package():
    row = _measure(held_seq=3, package_held_seq=3, package_basis="measured")
    assert row["skill_md_versions_behind"] == 0
    assert row["package_versions_behind"] == 1
    assert row["missed_script_updates"] == 1
    assert row["first_missed_package"] == day("2026-06-01")
    assert row["missed_risky"]
    assert row["copy_state"] != "up_to_date"


def test_held_package_is_the_one_with_the_old_script():
    copy_files = {("SKILL.md", "b3"), ("scripts/deploy.sh", "old")}
    assert choose_held_package_version(copy_files, PACKAGES, PACKAGE_FILES, day("2026-10-01")) == 3
    fixed = {("SKILL.md", "b3"), ("scripts/deploy.sh", "fixed")}
    assert choose_held_package_version(fixed, PACKAGES, PACKAGE_FILES, day("2026-10-01")) == 4
    assert choose_held_package_version(fixed, PACKAGES, PACKAGE_FILES, day("2026-01-01")) is None


def test_sql_and_plain_held_package_agree():
    copy_files = {("SKILL.md", "b3"), ("scripts/deploy.sh", "old")}
    rows = []
    for package in PACKAGES:
        registry = PACKAGE_FILES[package["seq"]] | {("SKILL.md", package["skill_md_blob"])}
        shared = registry & copy_files
        rows.append({"repo": "r", "path": "SKILL.md", "package_version_seq": package["seq"],
                     "shared_files": len(shared),
                     "shared_skill_md": int(("SKILL.md", "b3") in shared)})  # fmt: skip
    picked = pick_held_packages(pd.DataFrame(rows))[("r", "SKILL.md")]
    plain = choose_held_package_version(copy_files, PACKAGES, PACKAGE_FILES, day("2026-10-01"))
    assert picked[0] == plain == 3


def test_copy_without_registry_scripts_has_no_package_lag():
    assert package_basis(True, (3, 1, 1), held_file_count=1) == "files_not_kept"
    row = _measure(held_seq=3, package_held_seq=3, package_basis="files_not_kept")
    assert row["package_versions_behind"] is None
    assert row["missed_script_updates"] is None
    assert row["copy_state"] == "up_to_date"


def test_removed_lineage_is_orphaned():
    row = _measure(held_seq=3, registry_removed=True, removed_at=day("2026-08-01"))
    assert row["copy_state"] == "orphaned"
    assert row["days_since_removed"] == 83


def test_lineage_removed_after_the_as_of_date_is_not_orphaned():
    row = _measure(held_seq=3, registry_removed=True, removed_at=day("2026-12-01"))
    assert row["copy_state"] == "up_to_date"


def test_deleted_copy_is_measured_to_its_removed_date():
    as_of, basis = choose_as_of_date(
        {"removed": True, "removed_date": day("2026-06-01"), "current_basis": "history"},
        day("2026-11-02"),
    )
    assert (as_of, basis) == (day("2026-06-01"), "copy_removed")
    versions = [VERSIONS[0], VERSIONS[1],
                {**VERSIONS[2], "release_date": day("2026-07-01")}]  # fmt: skip
    row = measure_copy(_copy(as_of_date=as_of, removed=True), versions, TEXT_CHANGES, [], {}, 30)
    assert row["skill_md_versions_behind"] == 1
    assert row["copy_state"] == "removed"


def test_as_of_date_by_source():
    history = {"removed": False, "current_basis": "history"}
    assert choose_as_of_date(history, day("2026-11-06")) == (day("2026-11-06"), "registry_snapshot")
    dataset = {"removed": False, "current_basis": "dataset", "discovered_at": "2026-07-10"}
    assert choose_as_of_date(dataset, day("2026-11-06"))[1] == "dataset_capture"


def test_registry_snapshot_is_never_before_the_first_mining_day():
    lineages = pd.DataFrame({"lineage_id": ["a::x", "b::y"], "repo": ["a", "b"]})
    dates = registry_snapshot_dates(
        lineages, {"a": "2026-11-07T01:00:00Z", "b": "2026-02-01T00:00:00Z"}, "2026-11-02"
    )
    assert dates["a::x"] == day("2026-11-07 01:00")
    assert dates["b::y"] == day("2026-11-02")


def test_count_versions_behind_edges():
    assert count_versions_behind(3, day("2026-10-23"), VERSIONS) == (0, None)
    assert count_versions_behind(1, None, VERSIONS) == (0, None)
    # A version released on the same moment as the held one is still later.
    same_day = [VERSIONS[0], {**VERSIONS[1], "release_date": day("2026-01-10")}]
    assert count_versions_behind(1, day("2026-10-23"), same_day)[0] == 1


def test_state_order():
    assert classify_copy_state(2, 236, True, 30, copy_removed=True) == "removed"
    assert classify_copy_state(None, None, False, 30) == "unknown"
    assert classify_copy_state(0, None, False, 30) == "up_to_date"
    assert classify_copy_state(1, None, False, 30) == "behind"


# Missed content


def test_script_deltas_flag_changes():
    deltas = pd.DataFrame({
        "lineage_id": ["L"] * 4, "package_version_seq": [2, 2, 3, 4],
        "path": ["a.sh", "b.sh", "a.sh", "c.sh"],
        "old_sha": ["a1", None, "a2", "c1"], "new_sha": ["a2", "b1", "a3", None],
    })  # fmt: skip
    flags = {"a1": [], "a2": [], "a3": [], "b1": [], "c1": ["deletes_files"]}
    result = script_deltas_by_package(deltas, flags)
    assert result[("L", 2)] == {"scripts_added": 1, "risk_changed": False}
    assert result[("L", 3)]["risk_changed"] is False
    assert result[("L", 4)]["risk_changed"] is True
    # An unknown text changes nothing.
    assert not script_deltas_by_package(deltas.iloc[[2]], {})[("L", 3)]["risk_changed"]


def test_nothing_missed_is_not_risky():
    assert find_missed_updates([], [])["missed_risky"] is False


# A48 sync events


def test_sync_events_copy_update_downgrade():
    history = pd.DataFrame({
        "repo": ["r"] * 4, "path": ["SKILL.md"] * 4, "blob": ["b1", "local", "b3", "b2"],
        "status": ["A", "M", "M", "M"], "commit": ["c1", "c2", "c3", "c4"],
        "commit_seq": [1, 2, 3, 4], "inherited": [False] * 4,
        "commit_date": [day("2026-02-01"), day("2026-04-01"), day("2026-06-01"),
                        day("2026-07-01")],
    })  # fmt: skip
    events = find_sync_events(history, {("r", "SKILL.md"): "L"}, {"L": VERSIONS})
    assert events["event_kind"].tolist() == ["copy", "update", "downgrade"]
    assert events["versions_skipped"].tolist()[1] == 1
    assert events["lag_days"].tolist() == [22, 17, 122]


def test_inherited_commits_set_the_start_but_are_not_events():
    history = pd.DataFrame({
        "repo": ["r"] * 3, "path": ["SKILL.md"] * 3, "blob": ["b1", "b2", "b3"],
        "status": ["A", "M", "M"], "commit": ["c1", "c2", "c3"], "commit_seq": [1, 2, 3],
        "inherited": [True, True, False],
        "commit_date": [day("2026-01-10"), day("2026-03-01"), day("2026-06-01")],
    })  # fmt: skip
    events = find_sync_events(history, {("r", "SKILL.md"): "L"}, {"L": VERSIONS})
    assert events["event_kind"].tolist() == ["update"]
    assert events["from_seq"].tolist() == [2]


def test_text_keeps_its_most_severe_state():
    frame = pd.DataFrame({"copy_state": ["up_to_date", "removed", "stale", "up_to_date"]})
    states = text_states(frame, pd.Series(["a", "a", "b", "c"]))
    assert states.to_dict() == {"a": "removed", "b": "stale", "c": "up_to_date"}
