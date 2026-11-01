"""Tests for registry lineages. Tables are hand-made. No dataset files."""

from __future__ import annotations

from dataclasses import fields

import pandas as pd

from skill_genealogy.build_registry_lineages import (
    CHANGE_COLUMNS,
    FRONT_MATTER_FIELDS,
    TEXT_CHANGE_FIELDS,
    _copied_from_not_kept,
    _finish_lineages,
    compare_consecutive_versions,
    join_renamed_folders,
    label_lineage_language,
    mark_removed_lineages,
    number_text_versions,
    read_skill_md_changes,
)
from skill_genealogy.compare_texts import TextChange, compare_front_matter

ZERO = "0" * 40
V1 = (
    "## Steps\n"
    "1. Run pdftotext input.pdf out.txt\n"
    "2. Check output encoding UTF-8\n"
    "3. Return summary JSON with fields: pages, words, errors\n"
)
V1_SPACE = V1.replace("errors\n", "errors \n")
V2 = V1 + "4. Write the summary to report.md\n"
V3 = V2 + "5. Stop when the report file is missing\n"
CHINESE = (
    "这是一个中文技能，用来检查文件并写出简短报告。\n"
    "它说明每个步骤，并且不使用英文句子。"
)


def _row(**overrides: object) -> dict[str, object]:
    """One SKILL.md change. Override any field the case cares about."""
    base: dict[str, object] = {
        "repo": "acme/copy",
        "skill_folder": "skills/new",
        "file_path": "SKILL.md",
        "history_role": "own",
        "status": "M",
        "repo_path": "skills/new/SKILL.md",
        "old_repo_path": "",
        "old_sha": "",
        "new_sha": "blob",
        "commit": "c1",
        "commit_date": "2026-01-01T00:00:00Z",
        "commit_seq": 1,
    }
    base.update(overrides)
    return base


def _build(
    rows: list[dict[str, object]],
    texts: dict[str, str],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Run the public steps and return lineages, versions, changes, dropped."""
    joined = join_renamed_folders(pd.DataFrame(rows))
    versions = number_text_versions(joined, texts)
    removed = mark_removed_lineages(joined)
    labeled = label_lineage_language(versions, texts)
    kept, dropped, lineages = _finish_lineages(joined, labeled, removed)
    changes = compare_consecutive_versions(kept, texts)
    return lineages, kept, changes, dropped


def test_edit_rename_and_delete_make_three_versions() -> None:
    """Whitespace stays one version. A rename stays one lineage. A final delete is removed."""
    rows = [
        _row(
            history_role="former_folder",
            status="A",
            repo_path="skills/old/SKILL.md",
            new_sha="v1",
            commit="c1",
            commit_date="2026-01-01T00:00:00Z",
            commit_seq=1,
        ),
        _row(
            history_role="former_folder",
            status="M",
            repo_path="skills/old/SKILL.md",
            new_sha="v1b",
            commit="c2",
            commit_date="2026-01-02T00:00:00Z",
            commit_seq=2,
        ),
        _row(
            history_role="former_folder",
            status="M",
            repo_path="skills/old/SKILL.md",
            new_sha="v2",
            commit="c3",
            commit_date="2026-01-03T00:00:00Z",
            commit_seq=3,
        ),
        _row(
            status="R",
            repo_path="skills/new/SKILL.md",
            old_repo_path="skills/old/SKILL.md",
            new_sha="v2",
            commit="c4",
            commit_date="2026-01-04T00:00:00Z",
            commit_seq=4,
        ),
        _row(
            status="M",
            new_sha="v3",
            commit="c5",
            commit_date="2026-01-05T00:00:00Z",
            commit_seq=5,
        ),
        _row(
            status="D",
            new_sha=ZERO,
            commit="c6",
            commit_date="2026-01-06T00:00:00Z",
            commit_seq=6,
        ),
    ]
    texts = {"v1": V1, "v1b": V1_SPACE, "v2": V2, "v3": V3}
    lineages, versions, changes, dropped = _build(rows, texts)
    assert len(lineages) == 1
    assert dropped.empty
    row = lineages.iloc[0]
    assert row["lineage_id"] == "acme/copy::skills/new"
    assert list(row["former_paths"]) == ["skills/old"]
    assert pd.isna(row["copied_from"])
    assert int(row["version_count"]) == 3
    assert bool(row["registry_removed"]) is True
    assert row["removed_at"] == "2026-01-06T00:00:00Z"
    assert row["last_date"] == "2026-01-06T00:00:00Z"
    assert bool(row["is_english"]) is True
    assert list(versions["version_seq"]) == [1, 2, 3]
    assert list(versions["blob"]) == ["v1", "v2", "v3"]
    assert list(versions["release_date"]) == [
        "2026-01-01T00:00:00Z",
        "2026-01-03T00:00:00Z",
        "2026-01-05T00:00:00Z",
    ]
    pairs = list(zip(changes["from_seq"], changes["to_seq"], strict=True))
    assert pairs == [(1, 2), (2, 3)]
    assert changes.iloc[0]["release_date"] == "2026-01-03T00:00:00Z"


def test_change_columns_cover_text_and_front_matter() -> None:
    """A version step carries every TextChange field and every front-matter field."""
    rows = [
        _row(status="A", new_sha="v1", commit_seq=1, commit_date="2026-01-01T00:00:00Z"),
        _row(status="M", new_sha="v2", commit_seq=2, commit_date="2026-01-02T00:00:00Z"),
    ]
    _, _, changes, _ = _build(rows, {"v1": V1, "v2": V2})
    expected = ["lineage_id", "from_seq", "to_seq", "release_date"]
    expected += [item.name for item in fields(TextChange)]
    expected += list(compare_front_matter(V1, V2))
    assert list(changes.columns) == expected
    assert TEXT_CHANGE_FIELDS == [item.name for item in fields(TextChange)]
    assert list(compare_front_matter("a", "b")) == FRONT_MATTER_FIELDS
    assert CHANGE_COLUMNS == expected


def test_revert_gets_a_new_version_seq() -> None:
    """Returning to version 1's text is version 4, and same_as_seq points at 1."""
    rows = [
        _row(status="A", new_sha="a", commit_seq=1, commit_date="2026-01-01T00:00:00Z"),
        _row(status="M", new_sha="b", commit_seq=2, commit_date="2026-01-02T00:00:00Z"),
        _row(status="M", new_sha="c", commit_seq=3, commit_date="2026-01-03T00:00:00Z"),
        _row(status="M", new_sha="a2", commit_seq=4, commit_date="2026-01-04T00:00:00Z"),
    ]
    texts = {"a": V1, "b": V2, "c": V3, "a2": V1}
    _, versions, _, _ = _build(rows, texts)
    assert list(versions["version_seq"]) == [1, 2, 3, 4]
    assert int(versions.loc[versions["version_seq"] == 4, "same_as_seq"].iloc[0]) == 1
    assert pd.isna(versions.loc[versions["version_seq"] == 1, "same_as_seq"].iloc[0])
    assert pd.isna(versions.loc[versions["version_seq"] == 2, "same_as_seq"].iloc[0])


def test_chinese_newest_version_is_dropped() -> None:
    """The newest text decides. Chinese last is dropped. English last is kept."""
    chinese_rows = [
        _row(
            skill_folder="skills/zh",
            repo_path="skills/zh/SKILL.md",
            status="A",
            new_sha="en",
            commit_seq=1,
            commit_date="2026-01-01T00:00:00Z",
        ),
        _row(
            skill_folder="skills/zh",
            repo_path="skills/zh/SKILL.md",
            status="M",
            new_sha="zh",
            commit_seq=2,
            commit_date="2026-01-02T00:00:00Z",
        ),
    ]
    kept_rows = [
        _row(
            skill_folder="skills/en",
            repo_path="skills/en/SKILL.md",
            status="A",
            new_sha="zh2",
            commit_seq=1,
            commit_date="2026-02-01T00:00:00Z",
        ),
        _row(
            skill_folder="skills/en",
            repo_path="skills/en/SKILL.md",
            status="M",
            new_sha="en2",
            commit_seq=2,
            commit_date="2026-02-02T00:00:00Z",
        ),
    ]
    texts = {"en": V1, "zh": CHINESE, "zh2": CHINESE, "en2": V2}
    lineages, versions, changes, dropped = _build(chinese_rows + kept_rows, texts)
    assert list(dropped["lineage_id"]) == ["acme/copy::skills/zh"]
    assert list(dropped["reason"]) == ["not_english"]
    assert list(lineages["lineage_id"]) == ["acme/copy::skills/en"]
    assert lineages.iloc[0]["newest_language"] == "en"
    assert bool(lineages.iloc[0]["is_english"]) is True
    assert set(versions["lineage_id"]) == {"acme/copy::skills/en"}
    assert set(changes["lineage_id"]) == {"acme/copy::skills/en"}
    assert set(versions["blob"]) == {"zh2", "en2"}


def test_branch_break_keeps_three_versions_in_date_order() -> None:
    """A new_sha that does not follow the previous old_sha is still a version."""
    rows = [
        _row(
            status="A",
            new_sha="a",
            old_sha="",
            commit_seq=1,
            commit_date="2026-01-01T00:00:00Z",
        ),
        _row(
            status="M",
            new_sha="b",
            old_sha="a",
            commit_seq=2,
            commit_date="2026-01-02T00:00:00Z",
        ),
        _row(
            status="M",
            new_sha="c",
            old_sha="a",
            commit_seq=3,
            commit_date="2026-01-03T00:00:00Z",
        ),
    ]
    texts = {"a": V1, "b": V2, "c": V3}
    _, versions, _, dropped = _build(rows, texts)
    assert dropped.empty
    assert list(versions["version_seq"]) == [1, 2, 3]
    assert list(versions["blob"]) == ["a", "b", "c"]


def test_first_row_modify_is_version_one() -> None:
    """A lineage that starts with M, and has no A, still gets version 1."""
    rows = [_row(status="M", new_sha="m1", commit_seq=1)]
    lineages, versions, _, dropped = _build(rows, {"m1": V1})
    assert dropped.empty
    assert list(versions["version_seq"]) == [1]
    assert versions.iloc[0]["blob"] == "m1"
    assert int(lineages.iloc[0]["version_count"]) == 1


def test_delete_only_lineage_is_dropped_for_no_text() -> None:
    """A lone delete has no blob, so the lineage is dropped with reason no_text."""
    rows = [_row(status="D", new_sha=ZERO, commit_seq=1)]
    lineages, versions, changes, dropped = _build(rows, {})
    assert lineages.empty
    assert versions.empty
    assert changes.empty
    assert list(dropped["reason"]) == ["no_text"]
    assert list(dropped["lineage_id"]) == ["acme/copy::skills/new"]


def test_copy_keeps_source_versions_on_the_source() -> None:
    """Destination versions start at its own SKILL.md. The source keeps its later edit."""
    rows = [
        _row(
            skill_folder="skills/dst",
            history_role="copy_source",
            status="A",
            repo_path="skills/src/SKILL.md",
            new_sha="s1",
            commit_seq=1,
            commit_date="2026-01-01T00:00:00Z",
        ),
        _row(
            skill_folder="skills/dst",
            history_role="copy_source",
            status="M",
            repo_path="skills/src/SKILL.md",
            new_sha="s2",
            commit_seq=2,
            commit_date="2026-01-02T00:00:00Z",
        ),
        _row(
            skill_folder="skills/dst",
            history_role="own",
            status="A",
            repo_path="skills/dst/SKILL.md",
            new_sha="d1",
            commit_seq=3,
            commit_date="2026-01-03T00:00:00Z",
        ),
        _row(
            skill_folder="skills/dst",
            history_role="own",
            status="M",
            repo_path="skills/dst/SKILL.md",
            new_sha="d2",
            commit_seq=4,
            commit_date="2026-01-04T00:00:00Z",
        ),
        _row(
            skill_folder="skills/src",
            history_role="own",
            status="A",
            repo_path="skills/src/SKILL.md",
            new_sha="s1",
            commit_seq=1,
            commit_date="2026-01-01T00:00:00Z",
        ),
        _row(
            skill_folder="skills/src",
            history_role="own",
            status="M",
            repo_path="skills/src/SKILL.md",
            new_sha="s2",
            commit_seq=2,
            commit_date="2026-01-02T00:00:00Z",
        ),
        _row(
            skill_folder="skills/src",
            history_role="own",
            status="M",
            repo_path="skills/src/SKILL.md",
            new_sha="s3",
            commit_seq=3,
            commit_date="2026-01-05T00:00:00Z",
        ),
    ]
    texts = {"s1": V1, "s2": V2, "s3": V3, "d1": V1, "d2": V3}
    lineages, versions, _, _ = _build(rows, texts)
    by_id = lineages.set_index("lineage_id")
    assert by_id.loc["acme/copy::skills/dst", "copied_from"] == "acme/copy::skills/src"
    assert int(by_id.loc["acme/copy::skills/dst", "version_count"]) == 2
    assert pd.isna(by_id.loc["acme/copy::skills/src", "copied_from"])
    assert int(by_id.loc["acme/copy::skills/src", "version_count"]) == 3
    dst = versions.loc[versions["lineage_id"] == "acme/copy::skills/dst", "blob"]
    assert list(dst) == ["d1", "d2"]


def test_copy_source_with_no_later_edit_is_still_a_copy() -> None:
    """A source that is never edited again is still copied_from, because it is at HEAD."""
    rows = [
        _row(
            skill_folder="skills/dst",
            history_role="copy_source",
            status="A",
            repo_path="skills/src/SKILL.md",
            new_sha="s1",
            commit_seq=1,
            commit_date="2026-01-01T00:00:00Z",
        ),
        _row(
            skill_folder="skills/dst",
            history_role="own",
            status="A",
            repo_path="skills/dst/SKILL.md",
            new_sha="d1",
            commit_seq=2,
            commit_date="2026-01-02T00:00:00Z",
        ),
        _row(
            skill_folder="skills/src",
            history_role="own",
            status="A",
            repo_path="skills/src/SKILL.md",
            new_sha="s1",
            commit_seq=1,
            commit_date="2026-01-01T00:00:00Z",
        ),
    ]
    lineages, versions, _, _ = _build(rows, {"s1": V1, "d1": V2})
    by_id = lineages.set_index("lineage_id")
    assert by_id.loc["acme/copy::skills/dst", "copied_from"] == "acme/copy::skills/src"
    dst = versions.loc[versions["lineage_id"] == "acme/copy::skills/dst", "blob"]
    assert list(dst) == ["d1"]
    assert int(by_id.loc["acme/copy::skills/src", "version_count"]) == 1


def test_dropped_source_lineage_stays_in_copied_from() -> None:
    """A non-English source is dropped. The destination still names that lineage."""
    rows = [
        _row(
            skill_folder="skills/dst",
            history_role="copy_source",
            status="A",
            repo_path="skills/src/SKILL.md",
            new_sha="s1",
            commit_seq=1,
            commit_date="2026-01-01T00:00:00Z",
        ),
        _row(
            skill_folder="skills/dst",
            history_role="own",
            status="A",
            repo_path="skills/dst/SKILL.md",
            new_sha="d1",
            commit_seq=2,
            commit_date="2026-01-02T00:00:00Z",
        ),
        _row(
            skill_folder="skills/src",
            history_role="own",
            status="A",
            repo_path="skills/src/SKILL.md",
            new_sha="s1",
            commit_seq=1,
            commit_date="2026-01-01T00:00:00Z",
        ),
    ]
    lineages, _, _, dropped = _build(rows, {"s1": CHINESE, "d1": V1})
    assert list(dropped["lineage_id"]) == ["acme/copy::skills/src"]
    assert list(dropped["reason"]) == ["not_english"]
    assert list(lineages["lineage_id"]) == ["acme/copy::skills/dst"]
    assert lineages.iloc[0]["copied_from"] == "acme/copy::skills/src"
    assert _copied_from_not_kept(lineages) == 1


def test_former_folder_rows_join_one_lineage() -> None:
    """Rows from skills/old plus own rows are one lineage, and the old name is former_paths."""
    rows = [
        _row(
            history_role="former_folder",
            status="A",
            repo_path="skills/old/SKILL.md",
            new_sha="old",
            commit_seq=1,
            commit_date="2026-01-01T00:00:00Z",
        ),
        _row(
            history_role="own",
            status="M",
            repo_path="skills/new/SKILL.md",
            new_sha="new",
            commit_seq=2,
            commit_date="2026-01-02T00:00:00Z",
        ),
    ]
    lineages, versions, _, _ = _build(rows, {"old": V1, "new": V2})
    assert len(lineages) == 1
    assert lineages.iloc[0]["lineage_id"] == "acme/copy::skills/new"
    assert list(lineages.iloc[0]["former_paths"]) == ["skills/old"]
    assert list(versions["blob"]) == ["old", "new"]


def test_rename_chain_lists_every_old_folder_in_date_order() -> None:
    """A chain of renames stays one lineage. former_paths is the older names, oldest first."""
    rows = [
        _row(
            skill_folder="skills/new",
            history_role="former_folder",
            status="A",
            repo_path="skills/a/SKILL.md",
            new_sha="a",
            commit_seq=1,
            commit_date="2026-01-01T00:00:00Z",
        ),
        _row(
            skill_folder="skills/new",
            history_role="former_folder",
            status="R",
            repo_path="skills/b/SKILL.md",
            old_repo_path="skills/a/SKILL.md",
            new_sha="b",
            commit_seq=2,
            commit_date="2026-01-02T00:00:00Z",
        ),
        _row(
            skill_folder="skills/new",
            history_role="own",
            status="R",
            repo_path="skills/new/SKILL.md",
            old_repo_path="skills/b/SKILL.md",
            new_sha="c",
            commit_seq=3,
            commit_date="2026-01-03T00:00:00Z",
        ),
    ]
    lineages, _, _, _ = _build(rows, {"a": V1, "b": V2, "c": V3})
    assert lineages.iloc[0]["lineage_id"] == "acme/copy::skills/new"
    assert list(lineages.iloc[0]["former_paths"]) == ["skills/a", "skills/b"]


def test_nested_skill_md_is_not_a_version_of_the_parent() -> None:
    """cn/SKILL.md is another skill. It does not add a version of folder x."""
    rows = [
        _row(
            skill_folder="x",
            repo_path="x/SKILL.md",
            status="A",
            new_sha="root",
            commit_seq=1,
            commit_date="2026-01-01T00:00:00Z",
        ),
        _row(
            skill_folder="x",
            file_path="cn/SKILL.md",
            repo_path="x/cn/SKILL.md",
            history_role="own",
            status="A",
            new_sha="nested",
            commit_seq=2,
            commit_date="2026-01-02T00:00:00Z",
        ),
    ]
    lineages, versions, _, _ = _build(rows, {"root": V1, "nested": CHINESE})
    assert list(lineages["lineage_id"]) == ["acme/copy::x"]
    assert list(versions["blob"]) == ["root"]
    assert int(lineages.iloc[0]["version_count"]) == 1


def test_prompt_rename_is_not_a_former_skill_folder() -> None:
    """A rename from a prompt file does not put that file's folder in former_paths."""
    rows = [
        _row(
            status="R",
            repo_path="skills/new/SKILL.md",
            old_repo_path="notes/prompt.md",
            new_sha="v1",
            commit_seq=1,
        )
    ]
    lineages, _, _, _ = _build(rows, {"v1": V1})
    assert list(lineages.iloc[0]["former_paths"]) == []


def test_read_skill_md_changes_keeps_only_skill_rows(tmp_path, monkeypatch) -> None:
    """The reader drops rows that are not SKILL.md changes."""
    history = tmp_path / "registry_history"
    history.mkdir()
    frame = pd.DataFrame(
        [
            {"file_path": "SKILL.md", "is_skill_md": True},
            {"file_path": "run.sh", "is_skill_md": False},
        ]
    )
    frame.to_parquet(history / "registry_history_changes.parquet", index=False)
    monkeypatch.setattr(
        "skill_genealogy.build_registry_lineages.data_dir",
        lambda dev: tmp_path,
    )
    got = read_skill_md_changes(True)
    assert list(got["file_path"]) == ["SKILL.md"]
