"""Hand-made copies for state, customisation, heading overlap, and history edits."""

from __future__ import annotations

import math

import duckdb
import pandas as pd

from skill_genealogy.build_registry_lineages import rebuild_folder_manifests
from skill_genealogy.measure_copy_changes import (
    ELIGIBLE_COPIES_SQL,
    _commits_by_lineage,
    _folder_change,
    _is_customized,
    _label_copy_lineage,
    _quote_path,
    _replay_copy_folders,
    _score_version_table,
    assemble_copy_folder_manifests,
    build_change_profile,
    change_notes_for_step,
    classify_history_edits,
    compare_copy_folders,
    compare_copy_with_origin,
    current_text_of_copy,
    heading_overlap_with_registry,
    list_copy_versions,
    pick_comparison_targets,
    registry_manifest_for,
    score_pair,
    summarize_folder_changes,
)

_LINE = "L"


def _body(line_count: int, changed_line: int | None = None) -> str:
    """A front-matter skill with ``line_count`` body lines. One line can differ."""
    lines = [f"Line {index} stays here." for index in range(line_count)]
    if changed_line is not None:
        lines[changed_line] = f"Line {changed_line} was edited."
    return "---\nname: demo\n---\n" + "\n".join(lines) + "\n"


def _copies(repo: str, path: str, file_sha: str = "current") -> pd.DataFrame:
    """One origin copy. The origin blob in these tests is ``origin``."""
    return pd.DataFrame(
        [
            {
                "repo": repo,
                "path": path,
                "file_sha": file_sha,
                "lineage_id": _LINE,
                "link": "exact",
                "origin_seq": 1,
            }
        ]
    )


def _versions(*blobs: str) -> pd.DataFrame:
    """Version rows for lineage L. Extra lineages use the blob name as the id."""
    return pd.DataFrame(
        {
            "lineage_id": [_LINE] * len(blobs),
            "version_seq": list(range(1, len(blobs) + 1)),
            "blob": list(blobs),
        }
    )


def _changes(rows: list[tuple[int, list[str]]]) -> pd.DataFrame:
    """Registry changes ``(to_seq, headings_added)`` for lineage L."""
    return pd.DataFrame(
        {
            "lineage_id": [_LINE] * len(rows),
            "to_seq": [row[0] for row in rows],
            "headings_added": [row[1] for row in rows],
        }
    )


def _compare(old: str, new: str, repo: str, path: str, changes: pd.DataFrame | None = None):
    """Compare one dataset copy. No history, so the basis is the dataset text."""
    texts = {"origin": old, "current": new}
    registry_changes = changes if changes is not None else _changes([])
    frame = compare_copy_with_origin(
        _copies(repo, path),
        pd.DataFrame(),
        texts,
        _versions("origin"),
        registry_changes,
    )
    return frame.iloc[0]


def test_unchanged_copy_has_no_customisation_and_null_overlap():
    """The same text is unchanged. Both heading lists empty, so overlap is null."""
    row = _compare(_body(10), _body(10), "ab/cd", "a/b.md")
    assert row["state"] == "unchanged"
    assert row["current_basis"] == "dataset"
    assert bool(row["customized"]) is False
    assert bool(row["permissions_changed"]) is False
    assert math.isnan(row["heading_overlap"])


def test_permissions_only_edit_stays_unchanged_and_not_customized():
    """Front matter is scored on its own. The body state stays unchanged."""
    old = "---\nallowed-tools: Read\n---\n# Use\n\nDo the task.\n"
    new = "---\nallowed-tools: Read, Bash\n---\n# Use\n\nDo the task.\n"
    row = _compare(old, new, "ab/cd", "a/b.md")
    assert row["state"] == "unchanged"
    assert bool(row["permissions_changed"]) is True
    assert list(row["allowed_tools_added"]) == ["Bash"]
    assert bool(row["customized"]) is False


def test_cosmetic_copy_is_not_customized():
    """Two changed lines out of 50 is 4 percent, under the 5 percent line."""
    row = _compare(
        _body(50),
        _body(50, changed_line=3),
        "ab/cd",
        "a/b.md",
        _changes([(2, ["# Later"])]),
    )
    assert row["state"] == "cosmetic"
    assert bool(row["customized"]) is False
    assert row["heading_overlap"] == 0.0


def test_substantive_copy_with_section_and_project_name_is_customized():
    """An added section and the repo name count as customisation. Overlap is 1/2."""
    old = "---\nname: demo\n---\n# Use\n\nDo the task.\n"
    new = old + "# Deploy\n\nUse acme in this project.\n"
    row = _compare(
        old,
        new,
        "acme/widgetbox",
        "skills/demo/SKILL.md",
        _changes([(1, ["# Old"]), (2, ["# Deploy", "# Tests"])]),
    )
    assert row["state"] == "substantive"
    assert bool(row["customized"]) is True
    assert "# Deploy" in list(row["headings_added"])
    assert "acme" in list(row["names_added"])
    assert row["heading_overlap"] == 0.5


def test_heading_overlap_is_jaccard_and_null_when_both_empty():
    """Copy added Deploy; the registry later added Deploy and Tests."""
    assert heading_overlap_with_registry(["Deploy"], ["Deploy", "Tests"]) == 0.5
    assert heading_overlap_with_registry(["Deploy", "Deploy"], ["Deploy"]) == 1.0
    assert heading_overlap_with_registry(["Deploy"], []) == 0.0
    assert math.isnan(heading_overlap_with_registry([], []))


def test_removed_steps_are_not_customisation():
    """Customisation counts steps that were added, not steps that were removed."""
    assert (
        _is_customized(
            {
                "names_added": [],
                "scripts_mentioned_added": [],
                "steps_added": -1,
                "commands_added": [],
                "headings_added": [],
            }
        )
        is False
    )


def test_current_text_uses_newest_history_else_dataset():
    """A missing newest blob does not fall back to an older history row."""
    copy = {"repo": "a/r", "path": "skills/a/SKILL.md", "file_sha": "dataset"}
    dated = {
        "repo": "a/r",
        "path": "skills/a/SKILL.md",
        "blob": "dated",
        "commit_date": "2026-01-01T00:00:00Z",
        "version_seq": 1,
    }
    undated = {
        "repo": "a/r",
        "path": "skills/a/SKILL.md",
        "blob": "undated",
        "commit_date": None,
        "version_seq": 9,
    }
    texts = {"dated": "Dated", "undated": "Undated", "dataset": "Dataset", "old": "Old"}
    assert current_text_of_copy(copy, [dated, undated], texts) == ("Dated", "latest_history")
    missing_newest = [
        {
            "repo": "a/r",
            "path": "skills/a/SKILL.md",
            "blob": "old",
            "commit_date": "2026-01-01T00:00:00Z",
            "version_seq": 1,
        },
        {
            "repo": "a/r",
            "path": "skills/a/SKILL.md",
            "blob": "new",
            "commit_date": "2026-06-01T00:00:00Z",
            "version_seq": 2,
        },
    ]
    assert current_text_of_copy(copy, missing_newest, texts) == ("Dataset", "dataset")
    assert current_text_of_copy(copy, [], texts) == ("Dataset", "dataset")
    assert current_text_of_copy(copy, [], {}) == ("", "dataset")


def test_history_edits_local_then_sync_skips_inherited():
    """v1, a local edit, an inherited commit, then v2. Inherited is left out."""
    versions = pd.DataFrame(
        {
            "lineage_id": ["L", "L", "Other"],
            "version_seq": [1, 2, 1],
            "blob": ["reg-v1", "reg-v2", "other-blob"],
        }
    )
    base = "---\nname: demo\n---\n# Use\n\nDo the task.\n"
    histories = pd.DataFrame(
        [
            _history("reg-v1", base, "c1", "2026-01-01T00:00:00Z", 1, False),
            _history(
                "local-blob",
                base + "# Notes\n\nA local note.\n",
                "c2",
                "2026-02-01T00:00:00Z",
                2,
                False,
            ),
            _history("mid-blob", base + "skip\n", "c-inherited", "2026-03-01T00:00:00Z", 3, True),
            _history(
                "REG-V2",
                base + "# Deploy\n\nShip it.\n",
                "c4",
                "2026-04-01T00:00:00Z",
                4,
                False,
            ),
            _history("reg-v1", base, "b1", "2026-01-01T00:00:00Z", 1, False, repo="b/r"),
            _history(
                "other-blob",
                base + "elsewhere\n",
                "c5",
                "2026-05-01T00:00:00Z",
                2,
                False,
                repo="b/r",
            ),
        ]
    )
    result = classify_history_edits(histories, versions)
    first = result.loc[result["repo"].eq("a/r")].sort_values("commit_date")
    assert first["change_kind"].tolist() == ["local", "sync"]
    assert "c-inherited" not in set(result["commit"])
    assert bool(first["inherited"].any()) is False
    other = result.loc[result["repo"].eq("b/r")]
    assert other["change_kind"].tolist() == ["other_source"]


def test_same_blob_twice_then_v2_is_one_sync():
    """v1, the same blob at a later commit, then v2. Only v1 to v2 is an edit."""
    versions = pd.DataFrame(
        {
            "lineage_id": ["L", "L"],
            "version_seq": [1, 2],
            "blob": ["reg-v1", "reg-v2"],
        }
    )
    histories = pd.DataFrame(
        [
            _history("reg-v1", "Start\n", "c1", "2026-01-01T00:00:00Z", 1, False),
            _history("reg-v1", "Start\n", "c2", "2026-02-01T00:00:00Z", 2, False),
            _history("reg-v2", "Start\n\nMore\n", "c3", "2026-03-01T00:00:00Z", 3, False),
        ]
    )
    result = classify_history_edits(histories, versions)
    assert len(result) == 1
    assert result.iloc[0]["change_kind"] == "sync"
    assert result.iloc[0]["from_blob"] == "reg-v1"
    assert result.iloc[0]["to_blob"] == "reg-v2"


def test_missing_text_produces_no_edit():
    """A pair with no stored text on one side is not an edit."""
    versions = pd.DataFrame({"lineage_id": ["L"], "blob": ["v2"]})
    rows = [
        _history("blob-a", "Hello\n", "c1", "2026-01-01T00:00:00Z", 1, False),
        _history("blob-b", "Other\n", "c2", "2026-02-01T00:00:00Z", 2, False),
    ]
    rows[1]["text"] = None
    result = classify_history_edits(pd.DataFrame(rows), versions)
    assert result.empty
    assert result.attrs["skipped_missing_text"] == 1


def test_same_lineage_blob_is_sync_even_when_another_lineage_has_it():
    """A blob that belongs to this lineage is a sync, not another source."""
    versions = pd.DataFrame(
        {
            "lineage_id": ["L", "Other"],
            "version_seq": [1, 1],
            "blob": ["shared", "shared"],
        }
    )
    histories = pd.DataFrame(
        [
            _history("start", "Start\n", "c1", "2026-01-01T00:00:00Z", 1, False),
            _history("shared", "Start\n\nMore\n", "c2", "2026-02-01T00:00:00Z", 2, False),
        ]
    )
    assert classify_history_edits(histories, versions)["change_kind"].tolist() == ["sync"]


def test_change_profile_uses_the_same_shares_on_four_populations():
    """Hand-checked shares. An other_source edit is not its own population."""
    registry = pd.DataFrame(
        [
            _profile_change("substantive", ["# A"], [], [], [], False),
            _profile_change("unchanged", [], ["git status"], [], [], True),
        ]
    )
    edits = pd.DataFrame(
        [
            _profile_change(
                "substantive", [], [], ["http://example.test"], ["a.py"], False, "local"
            ),
            _profile_change("cosmetic", ["# B"], [], [], [], True, "sync"),
            _profile_change("substantive", ["# Z"], [], [], [], False, "other_source"),
        ]
    )
    copies = pd.DataFrame(
        [
            _profile_change("substantive", ["# C"], [], [], [], False),
            _profile_change("unchanged", [], [], [], [], False),
        ]
    )
    profile = build_change_profile(registry, edits, copies).set_index("population")
    assert profile.loc["registry_update", "changes"] == 2
    assert profile.loc["registry_update", "share_substantive"] == 0.5
    assert profile.loc["registry_update", "share_headings_added"] == 0.5
    assert profile.loc["registry_update", "share_commands_added"] == 0.5
    assert profile.loc["registry_update", "share_permissions_changed"] == 0.5
    assert profile.loc["copy_local_edit", "changes"] == 1
    assert profile.loc["copy_local_edit", "share_network_added"] == 1.0
    assert profile.loc["copy_local_edit", "share_scripts_added"] == 1.0
    assert profile.loc["copy_sync", "changes"] == 1
    assert profile.loc["copy_sync", "share_substantive"] == 0.0
    assert profile.loc["copy_sync", "share_headings_added"] == 1.0
    assert profile.loc["copy_sync", "share_permissions_changed"] == 1.0
    assert profile.loc["copy_vs_origin", "changes"] == 2
    assert profile.loc["copy_vs_origin", "share_substantive"] == 0.5
    assert list(profile.index) == [
        "registry_update",
        "copy_local_edit",
        "copy_sync",
        "copy_vs_origin",
    ]


def test_eligible_copies_sql_keeps_english_origins_only(tmp_path):
    """Drop early copies, missing origins, non-English texts, and registry-only texts."""
    copies = pd.DataFrame(
        [
            _sql_copy("keep/repo", "keep-sha", 1, False),
            _sql_copy("early/repo", "early-sha", 1, True),
            _sql_copy("nodate/repo", "nodate-sha", None, False),
            _sql_copy("other/repo", "other-sha", 1, False),
            _sql_copy("self/repo", "self-sha", 1, False),
        ]
    )
    families = pd.DataFrame(
        {
            "file_sha": ["keep-sha", "self-sha"],
            "registry_self": [False, True],
        }
    )
    language = pd.DataFrame(
        {
            "file_sha": ["keep-sha", "early-sha", "nodate-sha", "other-sha", "self-sha"],
            "is_english": [True, True, True, False, True],
        }
    )
    copies_path = tmp_path / "copies.parquet"
    families_path = tmp_path / "families.parquet"
    language_path = tmp_path / "language.parquet"
    copies.to_parquet(copies_path)
    families.to_parquet(families_path)
    language.to_parquet(language_path)
    query = ELIGIBLE_COPIES_SQL.format(
        copies=_quote_path(str(copies_path)),
        families=_quote_path(str(families_path)),
        language=_quote_path(str(language_path)),
    )
    frame = duckdb.connect().execute(query).fetchdf()
    assert frame["repo"].tolist() == ["keep/repo"]


def _history(
    blob: str,
    text: str,
    commit: str,
    commit_date: str,
    version_seq: int,
    inherited: bool,
    repo: str = "a/r",
) -> dict:
    """One history row. The second file in the sync test uses another repo."""
    return {
        "repo": repo,
        "path": "skills/a/SKILL.md",
        "lineage_id": "L",
        "blob": blob,
        "text": text,
        "commit": commit,
        "commit_date": commit_date,
        "version_seq": version_seq,
        "inherited": inherited,
    }


def _profile_change(
    state: str,
    headings: list[str],
    commands: list[str],
    network: list[str],
    scripts: list[str],
    permissions: bool,
    kind: str | None = None,
) -> dict:
    """One row with the columns the profile reads."""
    row = {
        "state": state,
        "headings_added": headings,
        "commands_added": commands,
        "network_refs_added": network,
        "scripts_mentioned_added": scripts,
        "permissions_changed": permissions,
    }
    if kind is not None:
        row["change_kind"] = kind
    return row


def test_comparison_targets_follow_the_c1_to_c4_timeline():
    versions = pd.DataFrame(
        [
            {"lineage_id": "reg::skill", "version_seq": 1, "release_date": "2026-01-10"},
            {"lineage_id": "reg::skill", "version_seq": 2, "release_date": "2026-03-01"},
            {"lineage_id": "reg::skill", "version_seq": 3, "release_date": "2026-05-15"},
        ]
    )
    dates = {1: "2026-02-01", 2: "2026-03-10", 3: "2026-04-20", 4: "2026-06-30"}
    expected = {
        1: {"prev_seq": None, "origin_seq": 1, "at_date_seq": 1, "newest_seq": 3},
        2: {"prev_seq": 1, "origin_seq": 1, "at_date_seq": 2, "newest_seq": 3},
        3: {"prev_seq": 2, "origin_seq": 1, "at_date_seq": 2, "newest_seq": 3},
        4: {"prev_seq": 3, "origin_seq": 1, "at_date_seq": 3, "newest_seq": 3},
    }
    for sequence, want in expected.items():
        copy_version = {
            "version_seq": sequence,
            "commit_date": dates[sequence],
            "lineage_id": "reg::skill",
        }
        assert pick_comparison_targets(copy_version, versions, 1) == want
    early = {"version_seq": 1, "commit_date": "2026-01-01", "lineage_id": "reg::skill"}
    assert pick_comparison_targets(early, versions, 1)["at_date_seq"] is None


def test_history_versions_are_numbered_by_date():
    copies = pd.DataFrame(
        [
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "file_sha": "dataset",
                "lineage_id": "reg::skill",
                "origin_seq": 1,
            }
        ]
    )
    histories = pd.DataFrame(
        [
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "blob": "c2",
                "commit": "later",
                "commit_date": "2026-03-10T00:00:00Z",
                "version_seq": 9,
            },
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "blob": "c1",
                "commit": "earlier",
                "commit_date": "2026-02-01T00:00:00Z",
                "version_seq": 3,
            },
        ]
    )
    listed = list_copy_versions(copies, histories, pd.DataFrame())
    assert list(listed["blob"]) == ["c1", "c2"]
    assert list(listed["version_seq"]) == [1, 2]


def test_copy_without_history_is_one_row_with_null_previous():
    copies = pd.DataFrame(
        [
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "file_sha": "datasetsha",
                "lineage_id": "reg::skill",
                "origin_seq": 1,
            }
        ]
    )
    dates = pd.DataFrame(
        [
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "last_commit_at": "2026-04-01T00:00:00Z",
            }
        ]
    )
    listed = list_copy_versions(copies, pd.DataFrame(), dates)
    assert len(listed) == 1
    assert listed.iloc[0]["blob"] == "datasetsha"
    assert str(listed.iloc[0]["commit_date"]).startswith("2026-04-01")
    lineage = pd.DataFrame(
        [
            {
                "lineage_id": "reg::skill",
                "version_seq": 1,
                "release_date": "2026-01-10",
                "blob": "originblob",
            }
        ]
    )
    texts = {
        "datasetsha": "Hello from the dataset copy.",
        "originblob": "Hello from the registry version.",
    }
    scored = _score_version_table(listed, lineage, texts, {}, {})
    row = scored.iloc[0]
    assert len(scored) == 1
    assert pd.isna(row["prev_jaccard"])
    assert pd.isna(row["prev_containment"])
    assert pd.isna(row["prev_verified"])
    assert pd.isna(row["prev_lines_added"])
    assert pd.isna(row["prev_lines_removed"])
    assert pd.isna(row["prev_body_change_pct"])
    assert row["change_notes"] == "[]"
    assert row["change_kind"] == "copy"
    assert int(row["at_date_seq"]) == 1
    assert int(row["newest_seq"]) == 1
    assert row["origin_jaccard"] == score_pair(texts["originblob"], texts["datasetsha"])["jaccard"]


def test_score_pair_on_identical_text_is_one():
    scores = score_pair(
        "zebra quartz violin marble lantern",
        "zebra quartz violin marble lantern",
    )
    assert scores["jaccard"] == 1.0
    assert scores["containment"] == 1.0
    assert scores["verified"] == 1.0
    assert scores["lines_added"] == 0
    assert scores["lines_removed"] == 0
    assert scores["body_change_pct"] == 0.0


def test_change_notes_mark_added_heading_and_network_lines():
    notes = change_notes_for_step(
        "Keep this line.",
        "Keep this line.\n## Deploy\ncurl https://x",
        "acme/app",
        "skills/a/SKILL.md",
    )
    assert [(note["line"], note["kind"]) for note in notes] == [
        (2, "heading"),
        (3, "network"),
    ]
    assert notes[0]["text"] == "## Deploy"
    assert notes[1]["text"] == "⚠ curl https://x"
    fenced = change_notes_for_step(
        "Keep",
        "Keep\n```\n## Deploy\n```",
        "acme/app",
        "skills/a/SKILL.md",
    )
    assert fenced == []


def test_permission_note_ignores_allowed_tools_in_the_body():
    old = "---\nallowed-tools: Read\n---\nKeep this line.\n"
    new = "---\nname: demo\n---\nKeep this line.\nThe allowed-tools field was removed.\n"
    notes = change_notes_for_step(old, new, "acme/app", "skills/a/SKILL.md")
    permission = [note for note in notes if note["kind"] == "permission"]
    assert permission == [
        {"line": 1, "kind": "permission", "text": "⚠ permissions changed: - Read"}
    ]


def _sql_copy(repo: str, file_sha: str, origin_seq: int | None, copied_before: bool) -> dict:
    """One copy-file row for the eligible-copies query."""
    return {
        "repo": repo,
        "path": "skills/a/SKILL.md",
        "file_sha": file_sha,
        "lineage_id": "reg::skills/a",
        "link": "exact",
        "origin_seq": origin_seq,
        "copied_before_release": copied_before,
    }


def _folder_version(seq: int, blob: str, commit_date: str) -> dict:
    """One copy version of ``acme/app`` ``skills/a/SKILL.md``."""
    return {
        "repo": "acme/app",
        "path": "skills/a/SKILL.md",
        "lineage_id": "reg::a",
        "version_seq": seq,
        "commit_date": commit_date,
        "blob": blob,
        "origin_seq": 1,
    }


def _package_timeline() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Three package versions. Text version 1 is released on 10 January."""
    packages = pd.DataFrame(
        [
            {"lineage_id": "reg::a", "package_version_seq": 1, "commit_date": "2026-01-10"},
            {"lineage_id": "reg::a", "package_version_seq": 2, "commit_date": "2026-03-01"},
            {"lineage_id": "reg::a", "package_version_seq": 3, "commit_date": "2026-05-15"},
        ]
    )
    manifests = pd.DataFrame(
        [
            {
                "lineage_id": "reg::a",
                "package_version_seq": seq,
                "path": "scripts/a.py",
                "sha": sha,
                "mode": "100644",
                "size": 10,
            }
            for seq, sha in ((1, "v1"), (2, "v2"), (3, "v3"))
        ]
    )
    releases = pd.DataFrame(
        [{"lineage_id": "reg::a", "version_seq": 1, "release_date": "2026-01-10"}]
    )
    return packages, manifests, releases


def test_modified_script_gains_a_network_flag_and_a_new_script_is_added():
    """Registry ``scripts/a.py`` is edited to call curl. ``scripts/deploy.sh`` is new."""
    packages, _, releases = _package_timeline()
    packages = packages.iloc[:1].copy()
    registry = pd.DataFrame(
        [
            {
                "lineage_id": "reg::a",
                "package_version_seq": 1,
                "path": "scripts/a.py",
                "sha": "old",
                "mode": "100644",
                "size": 10,
            }
        ]
    )
    manifests = pd.DataFrame(
        [
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "version_seq": 1,
                "file_path": "scripts/a.py",
                "sha": "new",
                "mode": "100644",
                "size": 20,
                "source": "history",
            },
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "version_seq": 1,
                "file_path": "scripts/deploy.sh",
                "sha": "dep",
                "mode": "100755",
                "size": 5,
                "source": "history",
            },
        ]
    )
    texts = {
        "old": "print('ok')\n",
        "new": "curl https://example.com\n",
        "skill": "Run scripts/a.py\n",
    }
    rows = compare_copy_folders(
        pd.DataFrame([_folder_version(1, "skill", "2026-04-20")]),
        manifests,
        packages,
        registry,
        releases,
        texts,
    )
    origin = rows.loc[rows["target"] == "origin"].set_index("file_path")
    assert origin.loc["scripts/a.py", "result"] == "modified"
    assert list(origin.loc["scripts/a.py", "new_risk_flags"]) == ["network"]
    assert origin.loc["scripts/deploy.sh", "result"] == "added"
    assert origin.loc["scripts/deploy.sh", "risk_flags"] is None
    summary = summarize_folder_changes(rows.loc[rows["target"] == "origin"])
    assert int(summary.iloc[0]["scripts_modified"]) == 1
    assert int(summary.iloc[0]["scripts_added"]) == 1
    assert int(summary.iloc[0]["new_risky_scripts"]) == 1
    assert list(summary.iloc[0]["hidden_scripts"]) == ["scripts/deploy.sh"]
    assert list(summary.iloc[0]["missing_scripts"]) == []


def test_modified_script_with_no_old_text_is_not_a_new_risk():
    """A modified script keeps its flags when the old blob has no stored text."""
    packages, _, releases = _package_timeline()
    packages = packages.iloc[:1].copy()
    registry = pd.DataFrame(
        [
            {
                "lineage_id": "reg::a",
                "package_version_seq": 1,
                "path": "scripts/a.py",
                "sha": "old",
                "mode": "100644",
                "size": 10,
            }
        ]
    )
    manifests = pd.DataFrame(
        [
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "version_seq": 1,
                "file_path": "scripts/a.py",
                "sha": "new",
                "mode": "100644",
                "size": 20,
                "source": "history",
            }
        ]
    )
    texts = {"new": "curl https://example.com\n", "skill": "Run scripts/a.py\n"}
    rows = compare_copy_folders(
        pd.DataFrame([_folder_version(1, "skill", "2026-04-20")]),
        manifests,
        packages,
        registry,
        releases,
        texts,
    )
    origin = rows.loc[rows["target"] == "origin"].iloc[0]
    assert origin["result"] == "modified"
    assert list(origin["risk_flags"]) == ["network"]
    assert origin["new_risk_flags"] is None
    summary = summarize_folder_changes(rows.loc[rows["target"] == "origin"])
    assert int(summary.iloc[0]["new_risky_scripts"]) == 0


def test_copy_without_history_uses_the_siblings_snapshot():
    """No folder changes: the sibling file is the manifest, and skipped paths are dropped."""
    versions = pd.DataFrame([_folder_version(1, "skill", "2026-04-20")])
    siblings = pd.DataFrame(
        [
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "entry_name": "SKILL.md",
                "entry_sha": "skill",
                "entry_size": 8,
            },
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "entry_name": "scripts/a.py",
                "entry_sha": "abc",
                "entry_size": 4,
            },
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "entry_name": "node_modules/x.js",
                "entry_sha": "skip",
                "entry_size": 9,
            },
        ]
    )
    found = assemble_copy_folder_manifests(versions, pd.DataFrame(), siblings)
    assert list(found["file_path"]) == ["scripts/a.py"]
    assert found.iloc[0]["sha"] == "abc"
    assert found.iloc[0]["source"] == "dataset_snapshot"
    assert found.iloc[0]["mode"] is None or pd.isna(found.iloc[0]["mode"])
    assert int(found.iloc[0]["size"]) == 4


def test_registry_manifest_is_the_package_current_at_a_date():
    """20 April sees package 2. A date before every package sees nothing. None is the last."""
    packages, manifests, releases = _package_timeline()
    assert registry_manifest_for("reg::a", "2026-04-20", packages, manifests)[0]["sha"] == "v2"
    assert registry_manifest_for("reg::a", "2025-01-01", packages, manifests) == []
    assert registry_manifest_for("reg::a", None, packages, manifests)[0]["sha"] == "v3"
    versions = pd.DataFrame([_folder_version(1, "skill", "2026-04-20")])
    copy_files = pd.DataFrame(
        [
            {
                "repo": "acme/app",
                "path": "skills/a/SKILL.md",
                "version_seq": 1,
                "file_path": "scripts/a.py",
                "sha": "copy",
                "mode": "100644",
                "size": 10,
                "source": "history",
            }
        ]
    )
    rows = compare_copy_folders(versions, copy_files, packages, manifests, releases, {})
    by_target = rows.set_index("target")
    assert by_target.loc["origin", "old_sha"] == "v1"
    assert by_target.loc["at_date", "old_sha"] == "v2"
    assert by_target.loc["newest", "old_sha"] == "v3"


def test_same_day_package_keeps_the_higher_sequence():
    """Two package versions on 1 March: the higher sequence is current that day."""
    packages = pd.DataFrame(
        [
            {"lineage_id": "reg::a", "package_version_seq": 1, "commit_date": "2026-03-01"},
            {"lineage_id": "reg::a", "package_version_seq": 2, "commit_date": "2026-03-01"},
        ]
    )
    manifests = pd.DataFrame(
        [
            {
                "lineage_id": "reg::a",
                "package_version_seq": 1,
                "path": "scripts/a.py",
                "sha": "v1",
                "mode": "100644",
                "size": None,
            },
            {
                "lineage_id": "reg::a",
                "package_version_seq": 2,
                "path": "scripts/a.py",
                "sha": "v2",
                "mode": "100644",
                "size": None,
            },
        ]
    )
    assert registry_manifest_for("reg::a", "2026-03-01", packages, manifests)[0]["sha"] == "v2"


def test_replay_matches_rebuild_folder_manifests():
    """The one-pass walk stores the same files as the package manifest walk."""
    changes = pd.DataFrame(
        [
            _folder_change("c1", 1, "A", "SKILL.md", "s1", "2026-02-01T00:00:00Z"),
            _folder_change("c1", 1, "A", "scripts/a.py", "p1", "2026-02-01T00:00:00Z"),
            _folder_change("c2", 2, "M", "SKILL.md", "s2", "2026-03-01T00:00:00Z"),
            _folder_change("c2", 2, "A", "scripts/b.py", "b1", "2026-03-01T00:00:00Z"),
            _folder_change("c3", 3, "M", "scripts/a.py", "p2", "2026-04-01T00:00:00Z"),
        ]
    )
    labeled = _label_copy_lineage(changes)
    expected = _commit_files(_commits_by_lineage(rebuild_folder_manifests(labeled)))
    found = _commit_files(_replay_copy_folders(labeled))
    assert found == expected


def _commit_files(snapshots: dict) -> dict:
    """``(path, sha, mode)`` per commit, so the two walks can be compared."""
    return {
        lineage: [
            (item["key"], [(row["path"], row["sha"], row["mode"]) for row in item["files"]])
            for item in commits
        ]
        for lineage, commits in snapshots.items()
    }


def test_last_version_keeps_a_script_edit_after_the_text():
    """A script change after the last SKILL.md commit stays on the last version only."""
    versions = pd.DataFrame(
        [
            _folder_version(1, "s1", "2026-02-01T00:00:00Z"),
            _folder_version(2, "s2", "2026-03-01T00:00:00Z"),
        ]
    )
    changes = pd.DataFrame(
        [
            _folder_change("c1", 1, "A", "SKILL.md", "s1", "2026-02-01T00:00:00Z"),
            _folder_change("c1", 1, "A", "scripts/a.py", "p1", "2026-02-01T00:00:00Z"),
            _folder_change("c2", 2, "M", "SKILL.md", "s2", "2026-03-01T00:00:00Z"),
            _folder_change("c3", 3, "M", "scripts/a.py", "p2", "2026-04-01T00:00:00Z"),
        ]
    )
    found = assemble_copy_folder_manifests(versions, changes, pd.DataFrame())
    files = found.loc[found["file_path"].notna()].set_index("version_seq")
    assert files.loc[1, "sha"] == "p1"
    assert files.loc[2, "sha"] == "p2"
    assert set(files["source"]) == {"history"}
