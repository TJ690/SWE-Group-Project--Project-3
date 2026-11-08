"""Tests for stage 4a, registry lineages.

Tiny hand-made stage 3 rows and texts. No network and no dataset files.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from skill_genealogy import build_registry_lineages as stage
from skill_genealogy import download_registry_history, settings
from skill_genealogy.build_registry_lineages import (
    CHANGE_COLUMNS,
    FRONT_MATTER_FIELDS,
    TEXT_CHANGE_FIELDS,
    build_registry_lineages,
    compare_consecutive_versions,
    join_renamed_folders,
    label_lineage_language,
    mark_removed_lineages,
    number_text_versions,
    read_skill_md_changes,
)

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
CHINESE = "这是一个中文技能，用来检查文件并写出简短报告。\n它说明每个步骤，并且不使用英文句子。"
TEXTS = {"v1": V1, "v1s": V1_SPACE, "v2": V2, "v3": V3, "zh": CHINESE}


def _row(**overrides: object) -> dict[str, object]:
    """One own SKILL.md change of ``acme/copy`` ``skills/new``; override any field."""
    row: dict[str, object] = {
        "repo": "acme/copy",
        "skill_folder": "skills/new",
        "file_path": "SKILL.md",
        "history_role": "own",
        "status": "M",
        "repo_path": "skills/new/SKILL.md",
        "old_repo_path": None,
        "old_sha": None,
        "new_sha": "v1",
        "commit": "c1",
        "commit_date": "2026-01-01T00:00:00Z",
        "commit_seq": 1,
    }
    row.update(overrides)
    return row


def _day(day: int) -> str:
    """An ISO date in January 2026."""
    return f"2026-01-{day:02d}T00:00:00Z"


def _build(
    rows: list[dict], texts: dict[str, str] = TEXTS, tmp_path: Path | None = None, limit: int = 1000
) -> dict[str, pd.DataFrame]:
    """Run steps 2-7 and read the four outputs back."""
    assert tmp_path is not None
    texts_file = tmp_path / "registry_history_texts.parquet"
    pd.DataFrame({"sha": list(texts), "text": list(texts.values())}).to_parquet(texts_file)
    paths = {
        name: tmp_path / f"{name}.parquet"
        for name in ("lineages", "versions", "changes", "dropped")
    }
    build_registry_lineages(pd.DataFrame(rows), texts_file, paths, limit)
    return {name: pd.read_parquet(path) for name, path in paths.items()}


def _story_rows() -> list[dict]:
    """Add v1, whitespace edit, real edit (v2) in skills/old; rename; edit (v3); delete."""
    old = {"history_role": "former_folder", "repo_path": "skills/old/SKILL.md"}
    return [
        _row(**old, status="A", new_sha="v1", commit="c1", commit_seq=1, commit_date=_day(1)),
        _row(**old, new_sha="v1s", commit="c2", commit_seq=2, commit_date=_day(2)),
        _row(**old, new_sha="v2", commit="c3", commit_seq=3, commit_date=_day(3)),
        _row(
            status="R",
            old_repo_path="skills/old/SKILL.md",
            new_sha="v2",
            commit="c4",
            commit_seq=4,
            commit_date=_day(4),
        ),
        _row(new_sha="v3", commit="c5", commit_seq=5, commit_date=_day(5)),
        _row(status="D", new_sha=ZERO, commit="c6", commit_seq=6, commit_date=_day(6)),
    ]


def test_edit_rename_and_delete_make_three_versions(tmp_path: Path) -> None:
    """A whitespace edit is not a version; the rename keeps one lineage; the delete removes it."""
    out = _build(_story_rows(), tmp_path=tmp_path)
    lineage = out["lineages"].iloc[0]
    assert len(out["lineages"]) == 1
    assert lineage["lineage_id"] == "acme/copy::skills/new"
    assert list(lineage["former_paths"]) == ["skills/old"]
    assert lineage["version_count"] == 3
    assert bool(lineage["registry_removed"]) is True
    assert lineage["removed_at"] == pd.Timestamp("2026-01-06", tz="UTC")
    assert lineage["last_date"] == lineage["removed_at"]
    assert out["versions"]["blob"].tolist() == ["v1", "v2", "v3"]
    assert out["changes"][["from_seq", "to_seq"]].values.tolist() == [[1, 2], [2, 3]]
    assert out["changes"]["lines_added"].tolist() == [1, 1]


def test_revert_gets_a_new_version_seq_with_same_as_seq(tmp_path: Path) -> None:
    """Going back to the v1 text is version 4, the same text as version 1."""
    rows = [
        _row(status="A", new_sha="v1", commit="c1", commit_seq=1, commit_date=_day(1)),
        _row(new_sha="v2", commit="c2", commit_seq=2, commit_date=_day(2)),
        _row(new_sha="v3", commit="c3", commit_seq=3, commit_date=_day(3)),
        _row(new_sha="v1s", commit="c4", commit_seq=4, commit_date=_day(4)),
    ]
    versions = _build(rows, tmp_path=tmp_path)["versions"]
    assert versions["version_seq"].tolist() == [1, 2, 3, 4]
    assert versions["same_as_seq"].iloc[:3].isna().all()
    assert versions["same_as_seq"].iloc[3] == 1


def test_chinese_newest_version_is_dropped(tmp_path: Path) -> None:
    """The newest text decides: an English skill translated to Chinese is not_english."""
    rows = [
        _row(status="A", new_sha="v1", commit="c1", commit_seq=1, commit_date=_day(1)),
        _row(new_sha="zh", commit="c2", commit_seq=2, commit_date=_day(2)),
    ]
    out = _build(rows, tmp_path=tmp_path)
    assert out["lineages"].empty and out["versions"].empty and out["changes"].empty
    assert out["dropped"].values.tolist() == [["acme/copy::skills/new", "not_english"]]


def test_branch_break_keeps_three_versions_in_date_order() -> None:
    """old_sha is not followed: v3 names v1 as its parent and is still version 3."""
    rows = pd.DataFrame(
        [
            _row(status="A", new_sha="v1", commit="c1", commit_seq=1, commit_date=_day(1)),
            _row(old_sha="v1", new_sha="v2", commit="c2", commit_seq=2, commit_date=_day(2)),
            _row(old_sha="v1", new_sha="v3", commit="c3", commit_seq=3, commit_date=_day(3)),
        ]
    )
    versions = number_text_versions(join_renamed_folders(rows), TEXTS)
    assert versions["blob"].tolist() == ["v1", "v2", "v3"]


def test_first_row_modify_is_version_one_and_delete_only_is_no_text(tmp_path: Path) -> None:
    """A lineage may start with M; a lineage with only a delete has no text."""
    rows = [
        _row(status="M", new_sha="v2", commit="c1"),
        _row(
            skill_folder="skills/gone", repo_path="skills/gone/SKILL.md", status="D", new_sha=ZERO
        ),
    ]
    out = _build(rows, tmp_path=tmp_path)
    assert out["versions"]["blob"].tolist() == ["v2"]
    assert out["dropped"].values.tolist() == [["acme/copy::skills/gone", "no_text"]]


def _copy_rows(edit_source_after_copy: bool) -> list[dict]:
    """skills/src is written, then copied to skills/dst (stage 3 copy window)."""
    source = {
        "history_role": "copy_source",
        "repo_path": "skills/src/SKILL.md",
        "skill_folder": "skills/dst",
    }
    rows = [
        _row(**source, status="A", new_sha="v1", commit="c1", commit_seq=1, commit_date=_day(1)),
        _row(**source, new_sha="v2", commit="c2", commit_seq=2, commit_date=_day(2)),
        _row(
            skill_folder="skills/dst",
            repo_path="skills/dst/SKILL.md",
            status="A",
            new_sha="v2",
            commit="c3",
            commit_seq=3,
            commit_date=_day(3),
        ),
        _row(
            skill_folder="skills/dst",
            repo_path="skills/dst/SKILL.md",
            new_sha="v3",
            commit="c4",
            commit_seq=4,
            commit_date=_day(4),
        ),
        _row(
            skill_folder="skills/src",
            repo_path="skills/src/SKILL.md",
            status="A",
            new_sha="v1",
            commit="c1",
            commit_seq=1,
            commit_date=_day(1),
        ),
        _row(
            skill_folder="skills/src",
            repo_path="skills/src/SKILL.md",
            new_sha="v2",
            commit="c2",
            commit_seq=2,
            commit_date=_day(2),
        ),
    ]
    if edit_source_after_copy:
        rows.append(
            _row(
                skill_folder="skills/src",
                repo_path="skills/src/SKILL.md",
                new_sha="v3",
                commit="c5",
                commit_seq=3,
                commit_date=_day(5),
            )
        )
    return rows


def test_copy_starts_its_own_lineage_and_names_the_source(tmp_path: Path) -> None:
    """The copy has 2 versions (not 4) and copied_from; the source keeps its 3 versions."""
    out = _build(_copy_rows(edit_source_after_copy=True), tmp_path=tmp_path)
    lineages = out["lineages"].set_index("lineage_id")
    assert lineages.loc["acme/copy::skills/dst", "version_count"] == 2
    assert lineages.loc["acme/copy::skills/dst", "copied_from"] == "acme/copy::skills/src"
    assert lineages.loc["acme/copy::skills/src", "version_count"] == 3
    assert pd.isna(lineages.loc["acme/copy::skills/src", "copied_from"])


def test_source_never_edited_after_the_copy_is_still_named(tmp_path: Path) -> None:
    """A copy_source row is a real copy even when the source did not change again."""
    out = _build(_copy_rows(edit_source_after_copy=False), tmp_path=tmp_path)
    lineages = out["lineages"].set_index("lineage_id")
    assert lineages.loc["acme/copy::skills/dst", "copied_from"] == "acme/copy::skills/src"


def test_former_folder_rows_join_one_lineage() -> None:
    """Rows from skills/old and own rows are one lineage acme/copy::skills/new."""
    rows = pd.DataFrame(
        [
            _row(
                history_role="former_folder",
                repo_path="skills/old/SKILL.md",
                status="A",
                commit_date=_day(1),
            ),
            _row(new_sha="v2", commit="c2", commit_seq=2, commit_date=_day(2)),
        ]
    )
    joined = join_renamed_folders(rows)
    assert set(joined["lineage_id"]) == {"acme/copy::skills/new"}
    assert joined.loc[0, "former_paths"] == ["skills/old"]


def test_rename_chain_lists_every_old_folder_in_date_order() -> None:
    """a -> b (former rows) -> new: former_paths is [a, b]."""
    rows = pd.DataFrame(
        [
            _row(
                history_role="former_folder",
                repo_path="skills/a/SKILL.md",
                status="A",
                commit_date=_day(1),
            ),
            _row(
                history_role="former_folder",
                repo_path="skills/b/SKILL.md",
                status="R",
                old_repo_path="skills/a/SKILL.md",
                commit="c2",
                commit_seq=2,
                commit_date=_day(2),
            ),
            _row(
                status="R",
                old_repo_path="skills/b/SKILL.md",
                commit="c3",
                commit_seq=3,
                commit_date=_day(3),
            ),
        ]
    )
    assert join_renamed_folders(rows).loc[0, "former_paths"] == ["skills/a", "skills/b"]


def test_nested_skill_md_is_not_a_version_of_the_parent() -> None:
    """cn/SKILL.md in folder x is another skill."""
    rows = pd.DataFrame(
        [
            _row(skill_folder="x", repo_path="x/SKILL.md", status="A", new_sha="v1"),
            _row(
                skill_folder="x",
                repo_path="x/cn/SKILL.md",
                file_path="cn/SKILL.md",
                status="A",
                new_sha="zh",
                commit="c2",
                commit_seq=2,
                commit_date=_day(2),
            ),
        ]
    )
    versions = number_text_versions(join_renamed_folders(rows), TEXTS)
    assert versions["blob"].tolist() == ["v1"]


def test_parallel_copies_in_one_commit_make_one_version_from_the_own_row(tmp_path: Path) -> None:
    """One commit edits skills/new and plugins/p/skills/new: one version, the own text."""
    plugin = {"history_role": "former_folder", "repo_path": "plugins/p/skills/new/SKILL.md"}
    rows = [
        _row(**plugin, status="A", new_sha="v1", commit="c1"),
        _row(status="A", new_sha="v1", commit="c1"),
        _row(**plugin, new_sha="v3", commit="c2", commit_seq=2, commit_date=_day(2)),
        _row(new_sha="v2", commit="c2", commit_seq=2, commit_date=_day(2)),
    ]
    out = _build(rows, tmp_path=tmp_path)
    assert out["versions"]["blob"].tolist() == ["v1", "v2"]
    assert stage.count_parallel_commits(join_renamed_folders(pd.DataFrame(rows))) == 2


def test_dates_with_different_offsets_order_as_utc() -> None:
    """09:00+09:00 is 00:00 UTC, before 01:00Z, though the text sorts the other way."""
    rows = pd.DataFrame(
        [
            _row(
                status="A",
                new_sha="v2",
                commit="c2",
                commit_seq=1,
                commit_date="2026-01-01T01:00:00Z",
            ),
            _row(
                status="A",
                new_sha="v1",
                commit="c1",
                commit_seq=1,
                commit_date="2026-01-01T09:00:00+09:00",
            ),
        ]
    )
    versions = number_text_versions(join_renamed_folders(rows), TEXTS)
    assert versions["blob"].tolist() == ["v1", "v2"]
    assert str(versions["release_date"].dt.tz) == "UTC"


def test_delete_then_add_is_not_removed() -> None:
    """A later add after a delete means the skill is still there."""
    rows = pd.DataFrame(
        [
            _row(status="A", commit_date=_day(1)),
            _row(status="D", new_sha=ZERO, commit="c2", commit_seq=2, commit_date=_day(2)),
            _row(status="A", new_sha="v2", commit="c3", commit_seq=3, commit_date=_day(3)),
        ]
    )
    assert mark_removed_lineages(join_renamed_folders(rows))["registry_removed"].tolist() == [False]


def test_batches_give_the_same_tables_as_one_batch(tmp_path: Path) -> None:
    """A batch limit of 1 blob splits every lineage into its own batch, same result."""
    rows = _story_rows() + _copy_rows(edit_source_after_copy=True)
    (tmp_path / "one").mkdir()
    (tmp_path / "many").mkdir()
    one = _build(rows, tmp_path=tmp_path / "one", limit=1000)
    many = _build(rows, tmp_path=tmp_path / "many", limit=1)
    for name in ("lineages", "versions", "changes", "dropped"):
        pd.testing.assert_frame_equal(one[name], many[name])


def test_change_columns_cover_text_and_front_matter() -> None:
    """Every TextChange and compare_front_matter field is a change column."""
    rows = pd.DataFrame(
        [
            _row(status="A", new_sha="v1", commit_date=_day(1)),
            _row(new_sha="v2", commit="c2", commit_seq=2, commit_date=_day(2)),
        ]
    )
    versions = label_lineage_language(
        number_text_versions(join_renamed_folders(rows), TEXTS), TEXTS
    )
    changes = compare_consecutive_versions(versions, TEXTS)
    assert list(changes.columns) == CHANGE_COLUMNS
    assert set(TEXT_CHANGE_FIELDS) | set(FRONT_MATTER_FIELDS) <= set(changes.columns)
    assert changes.loc[0, "state"] in {"cosmetic", "substantive"}


@pytest.fixture
def data_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Full and dev data folders in tmp_path; a source, an unclear and a copier registry."""
    full, dev = tmp_path / "full", tmp_path / "dev"
    for folder in (full / "registry_history", dev):
        folder.mkdir(parents=True)

    def fake_data_dir(is_dev: bool) -> Path:
        return dev if is_dev else full

    monkeypatch.setattr(settings, "data_dir", fake_data_dir)
    monkeypatch.setattr(download_registry_history, "data_dir", fake_data_dir)
    repos = ["a/source", "b/unclear", "c/copier"]
    pd.DataFrame({"repo": repos}).to_parquet(full / "registries.parquet")
    pd.DataFrame({"repo": ["a/source", "c/copier"]}).to_parquet(full / "dev_registries.parquet")
    pd.DataFrame(
        {
            "repo": repos,
            "registry_kind": ["source", "unclear", "copier"],
            "skill_folders": [1, 1, 900],
        }
    ).to_parquet(full / "registry_kinds.parquet")
    rows = [_row(repo=repo, is_skill_md=True) for repo in repos]
    rows.append(_row(repo="a/source", file_path="README.md", is_skill_md=False))
    pd.DataFrame(rows).to_parquet(full / "registry_history" / "registry_history_changes.parquet")
    return tmp_path


def test_copiers_are_left_out_and_unclear_is_kept(data_folders: Path) -> None:
    """Rule 0: a copier has no row; an unclear registry is kept; only SKILL.md rows are read."""
    changes = read_skill_md_changes(False)
    assert sorted(changes["repo"]) == ["a/source", "b/unclear"]


def test_dev_reads_the_full_table_limited_to_dev_registries(data_folders: Path) -> None:
    """--dev reads the full history folder, keeps dev registries, and still skips copiers."""
    assert not (data_folders / "dev" / "registry_history").exists()
    changes = read_skill_md_changes(True)
    assert changes["repo"].tolist() == ["a/source"]


def test_prompt_renamed_to_skill_md_is_not_a_former_folder() -> None:
    """prompts/x.md -> skills/new/SKILL.md is not a rename of a skill folder."""
    rows = pd.DataFrame([_row(status="R", old_repo_path="prompts/x.md", commit_date=_day(1))])
    assert join_renamed_folders(rows).loc[0, "former_paths"] == []


def test_a_dropped_source_stays_in_copied_from(tmp_path: Path) -> None:
    """The source becomes Chinese and is dropped; the copy still names it."""
    rows = _copy_rows(edit_source_after_copy=False)
    rows.append(
        _row(
            skill_folder="skills/src",
            repo_path="skills/src/SKILL.md",
            new_sha="zh",
            commit="c9",
            commit_seq=3,
            commit_date=_day(9),
        )
    )
    out = _build(rows, tmp_path=tmp_path)
    assert out["lineages"]["lineage_id"].tolist() == ["acme/copy::skills/dst"]
    assert out["lineages"].loc[0, "copied_from"] == "acme/copy::skills/src"
    assert out["dropped"].values.tolist() == [["acme/copy::skills/src", "not_english"]]


def test_blank_line_only_commit_is_not_a_new_version() -> None:
    """Adding or removing blank lines keeps the version, like a trailing space."""
    spaced = V1.replace("\n2.", "\n\n2.")
    rows = pd.DataFrame(
        [
            _row(status="A", new_sha="v1", commit_date=_day(1)),
            _row(new_sha="blank", commit="c2", commit_seq=2, commit_date=_day(2)),
            _row(new_sha="v2", commit="c3", commit_seq=3, commit_date=_day(3)),
        ]
    )
    texts = {**TEXTS, "blank": spaced}
    versions = number_text_versions(join_renamed_folders(rows), texts)
    assert versions["blob"].tolist() == ["v1", "v2"]


def test_version_bump_is_front_matter_only_with_the_changed_key(tmp_path: Path) -> None:
    """Same body, new version: value -> state front_matter_only, fm_keys_changed [version]."""
    texts = {
        "f1": "---\nname: pdf\nversion: 1.0\n---\n" + V1,
        "f2": "---\nname: pdf\nversion: 1.1\n---\n" + V1,
    }
    rows = [
        _row(status="A", new_sha="f1", commit_date=_day(1)),
        _row(new_sha="f2", commit="c2", commit_seq=2, commit_date=_day(2)),
    ]
    changes = _build(rows, texts=texts, tmp_path=tmp_path)["changes"]
    assert changes.loc[0, "state"] == "front_matter_only"
    assert list(changes.loc[0, "fm_keys_changed"]) == ["version"]
    assert bool(changes.loc[0, "name_changed"]) is False
