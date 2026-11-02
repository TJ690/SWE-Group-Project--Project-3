"""Staleness on the c1–c4 / v1–v3 timeline, plus the script and sync edges."""

from datetime import date

import numpy as np
import pandas as pd

from skill_genealogy.find_copies import signature_for_text
from skill_genealogy.measure_staleness import (
    _empty_sigs,
    _script_step_counts,
    _timeline_versions,
    choose_as_of_date,
    choose_held_package_version,
    choose_held_version,
    classify_copy_state,
    count_versions_behind,
    find_missed_updates,
    find_sync_events,
)

AS_OF = date(2026, 10, 23)
V2 = date(2026, 3, 1)


def _copy(blob: str, copy_date: str) -> dict:
    """One copy of ``reg::skill``."""
    return {"lineage_id": "reg::skill", "blob": blob, "copy_date": copy_date}


def test_copy_holding_v1_is_two_versions_behind_on_oct_23():
    """c1 holds v1. As of 23 Oct it missed v2 (1 Mar) and v3 (15 May)."""
    versions = _timeline_versions()
    seq, basis = choose_held_version(_copy("v1", "2026-02-01"), versions, _empty_sigs())
    assert (seq, basis) == (1, "exact_current")
    held_day = date(2026, 1, 10)
    behind, first = count_versions_behind(held_day, AS_OF, versions)
    assert behind == 2
    assert first == V2
    days_stale = (AS_OF - first).days
    assert days_stale == 236
    assert classify_copy_state(behind, days_stale, False, 30) == "stale"


def test_stale_days_400_keeps_the_same_copy_behind():
    """236 days is behind the default of 30 and not behind a 400-day grace."""
    behind, first = count_versions_behind("2026-01-10", AS_OF, _timeline_versions())
    days_stale = (AS_OF - first).days
    assert classify_copy_state(behind, days_stale, False, 400) == "behind"


def test_versions_after_the_as_of_day_are_not_missed():
    """On 20 Apr (c3) only v2 has been released after v1. v3 is still in the future."""
    behind, first = count_versions_behind("2026-01-10", "2026-04-20", _timeline_versions())
    assert behind == 1
    assert first == V2


def test_current_skill_md_with_an_old_script_is_not_up_to_date():
    """The text matches v3. The script still matches the package from before the fix."""
    versions = _timeline_versions()
    seq, basis = choose_held_version(_copy("v3", "2026-06-30"), versions, _empty_sigs())
    assert (seq, basis) == (3, "exact_current")
    text_behind, first = count_versions_behind("2026-05-15", AS_OF, versions)
    assert text_behind == 0
    assert first is None
    packages = pd.DataFrame(
        [
            {"package_version_seq": 1, "commit_date": "2026-01-10", "changed_scripts": False},
            {"package_version_seq": 2, "commit_date": "2026-05-15", "changed_scripts": True},
        ]
    )
    manifests = pd.DataFrame(
        [
            {"package_version_seq": 1, "path": "SKILL.md", "sha": "skill-new"},
            {"package_version_seq": 1, "path": "scripts/deploy.sh", "sha": "old"},
            {"package_version_seq": 2, "path": "SKILL.md", "sha": "skill-new"},
            {"package_version_seq": 2, "path": "scripts/deploy.sh", "sha": "fixed"},
        ]
    )
    copy_manifest = pd.DataFrame(
        [
            {"path": "SKILL.md", "sha": "skill-new"},
            {"path": "scripts/deploy.sh", "sha": "old"},
        ]
    )
    held_package = choose_held_package_version(copy_manifest, packages, manifests, "2026-06-30")
    assert held_package == 1
    later = packages.loc[packages["package_version_seq"] > held_package]
    assert len(later) == 1
    found = find_missed_updates(seq, AS_OF, pd.DataFrame(), later, pd.DataFrame())
    assert found["missed_script_updates"] == 1
    state = classify_copy_state(text_behind + len(later), None, False, 30)
    assert state != "up_to_date"
    assert state == "behind"


def test_removed_lineage_is_orphaned():
    """A removed registry wins over a stale text."""
    assert classify_copy_state(2, 236, True, 30) == "orphaned"
    assert classify_copy_state(0, None, True, 30) == "orphaned"


def test_copy_dated_before_every_release_is_not_silently_fixed():
    """1 Jan is before v1. An unmatched blob is copied_before_release."""
    seq, basis = choose_held_version(
        _copy("other", "2026-01-01"), _timeline_versions(), _empty_sigs()
    )
    assert (seq, basis) == (None, "copied_before_release")


def test_identical_blob_is_exact_even_when_that_release_is_later():
    """Exact text does not wait for the release date. v3's blob on 1 Feb is still v3."""
    seq, basis = choose_held_version(_copy("v3", "2026-02-01"), _timeline_versions(), _empty_sigs())
    assert (seq, basis) == (3, "exact_current")


def test_similar_copy_keeps_the_closer_version_on_or_before_its_date():
    """Both v1 and v2 are out by 30 Jun. The copy is closer to v1, so it holds v1."""
    v1 = "alpha beta gamma delta epsilon one two"
    v2 = "alpha beta gamma delta epsilon two three"
    copy = v1 + "\nextra note"
    sketches = [signature_for_text(text, set()) for text in (copy, v1, v2)]
    sigs = (
        ["copy", "reg::skill#1", "reg::skill#2"],
        np.vstack([values for values, _size in sketches]),
        np.asarray([size for _values, size in sketches], dtype=np.int32),
    )
    versions = _timeline_versions().iloc[:2]
    seq, basis = choose_held_version(_copy("copy", "2026-06-30"), versions, sigs)
    assert (seq, basis) == (1, "similar_before_date")


def test_as_of_date_uses_the_snapshot_or_the_capture_day():
    """History uses the registry snapshot. No history uses discovered_at."""
    assert choose_as_of_date(
        {"has_history": True, "discovered_at": "2026-04-01"}, AS_OF
    ) == (AS_OF, "registry_snapshot")
    assert choose_as_of_date(
        {"has_history": False, "discovered_at": "2026-04-01"}, AS_OF
    ) == (date(2026, 4, 1), "dataset_capture")


def test_a_fixed_script_that_adds_curl_is_a_risky_miss():
    """The registry's later script calls curl. That is a network flag the copy missed."""
    counts = _script_step_counts(
        {"scripts/deploy.sh": "old"},
        {"scripts/deploy.sh": "new"},
        {"old": "print('ok')\n", "new": "curl https://example.com/fix.sh\n"},
    )
    assert counts["substantive"] == 1
    assert counts["scripts_added"] == 0
    assert counts["network_refs_added"] == 1
    assert counts["risk_flag_changed"] == 1
    assert counts["risky"] == 1


def test_sync_events_copy_then_update_skipping_v2_then_downgrade():
    """c1 copies v1, a later commit jumps to v3, and the next commit goes back to v1."""
    history = pd.DataFrame(
        [
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "lineage_id": "reg::skill",
                "commit": "c1",
                "commit_date": "2026-02-01",
                "blob": "v1",
            },
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "lineage_id": "reg::skill",
                "commit": "c4",
                "commit_date": "2026-06-30",
                "blob": "v3",
            },
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "lineage_id": "reg::skill",
                "commit": "c5",
                "commit_date": "2026-07-01",
                "blob": "v1",
            },
        ]
    )
    events = find_sync_events(history, _timeline_versions(), set())
    assert list(events["event_kind"]) == ["copy", "update", "downgrade"]
    update = events.iloc[1]
    assert int(update["from_seq"]) == 1
    assert int(update["to_seq"]) == 3
    assert int(update["versions_skipped"]) == 1
    assert int(events.iloc[0]["lag_days"]) == (date(2026, 2, 1) - date(2026, 1, 10)).days


def test_inherited_commit_sets_the_version_and_is_not_an_event():
    """An inherited v1 is the start. The next real commit, v2, is an update, not a copy."""
    history = pd.DataFrame(
        [
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "lineage_id": "reg::skill",
                "commit": "inherited",
                "commit_date": "2026-01-10",
                "blob": "v1",
            },
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "lineage_id": "reg::skill",
                "commit": "c2",
                "commit_date": "2026-03-10",
                "blob": "v2",
            },
        ]
    )
    events = find_sync_events(history, _timeline_versions(), {"inherited"})
    assert list(events["event_kind"]) == ["update"]
    assert int(events.iloc[0]["from_seq"]) == 1
    assert int(events.iloc[0]["to_seq"]) == 2
    assert int(events.iloc[0]["versions_skipped"]) == 0


def test_missed_skill_change_counts_commands_and_risk_once():
    """One substantive step that adds a command is one risky miss, not two."""
    changes = pd.DataFrame(
        [
            {
                "to_seq": 2,
                "release_date": "2026-03-01",
                "state": "substantive",
                "commands_added": ["curl"],
                "network_refs_added": ["https://example.com"],
                "scripts_mentioned_added": [],
            }
        ]
    )
    found = find_missed_updates(1, AS_OF, changes, pd.DataFrame(), pd.DataFrame())
    assert found["missed_substantive"] == 1
    assert found["missed_commands"] == 1
    assert found["missed_network"] == 1
    assert found["missed_risky"] == 1
