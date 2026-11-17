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
    MANIFEST_COLUMNS,
    PACKAGE_COLUMNS,
    TEXT_CHANGE_FIELDS,
    build_package_versions,
    build_registry_lineages,
    compare_consecutive_versions,
    join_renamed_folders,
    label_lineage_language,
    mark_removed_lineages,
    number_package_versions,
    number_text_versions,
    read_skill_md_changes,
    rebuild_folder_manifests,
    summarize_packages,
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
        "new_mode": "100644",
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


# Stage 4b (card G11): package versions and folder manifests


def _file(path: str, commit: str, day: int, status: str = "A", **overrides: object) -> dict:
    """One change of a file in ``skills/new`` (``path`` relative to the folder)."""
    row = _row(
        file_path=path,
        repo_path=f"skills/new/{path}",
        status=status,
        new_sha=f"{path}@{commit}" if status != "D" else ZERO,
        new_mode="100644" if status != "D" else "000000",
        commit=commit,
        commit_seq=day,
        commit_date=_day(day),
    )
    row.update(overrides)
    return row


def _versions(*commits: str, lineage: str = "acme/copy::skills/new") -> pd.DataFrame:
    """Text versions 1..n of one lineage, first seen at ``commits``."""
    return pd.DataFrame(
        {"lineage_id": lineage, "version_seq": range(1, len(commits) + 1), "commit": commits}
    )


def _packages(rows: list[dict], versions: pd.DataFrame) -> pd.DataFrame:
    """Steps 8 and 9 on hand-made rows."""
    return number_package_versions(rebuild_folder_manifests(pd.DataFrame(rows)), versions)


def _paths(files: tuple) -> list[str]:
    """Paths of one manifest."""
    return [path for path, _, _ in files]


def test_folder_edits_make_five_package_versions() -> None:
    """Add SKILL.md + x.md; add a.py; edit a.py; rename to b.py; delete x.md -> 5 versions."""
    rows = [
        _file("SKILL.md", "c1", 1, new_sha="v1"),
        _file("references/x.md", "c1", 1),
        _file("scripts/a.py", "c2", 2),
        _file("scripts/a.py", "c3", 3, status="M"),
        _file(
            "scripts/b.py",
            "c4",
            4,
            status="R",
            old_repo_path="skills/new/scripts/a.py",
            new_sha="scripts/a.py@c3",
        ),
        _file("references/x.md", "c5", 5, status="D"),
    ]
    packages = _packages(rows, _versions("c1"))
    assert packages["package_version_seq"].tolist() == [1, 2, 3, 4, 5]
    assert packages["version_seq"].tolist() == [1, 1, 1, 1, 1]
    assert [_paths(files) for files in packages["files"]] == [
        ["references/x.md"],
        ["references/x.md", "scripts/a.py"],
        ["references/x.md", "scripts/a.py"],
        ["references/x.md", "scripts/b.py"],
        ["scripts/b.py"],
    ]
    assert packages.loc[2, "files"][1][1] == "scripts/a.py@c3"
    assert packages["changed_skill_md"].tolist() == [True, False, False, False, False]
    assert packages["changed_scripts"].tolist() == [False, True, True, True, False]
    assert packages["changed_other_files"].tolist() == [True, False, False, False, True]
    assert packages["file_count"].tolist() == [1, 2, 2, 2, 1]
    assert packages["script_count"].tolist() == [0, 1, 1, 1, 1]


def test_node_modules_file_is_ignored() -> None:
    """A vendored file starts no package version and is not in the manifest."""
    rows = [
        _file("SKILL.md", "c1", 1, new_sha="v1"),
        _file("node_modules/left-pad/index.js", "c2", 2),
        _file("scripts/a.py", "c3", 3),
    ]
    packages = _packages(rows, _versions("c1"))
    assert packages["commit"].tolist() == ["c1", "c3"]
    assert _paths(packages.loc[1, "files"]) == ["scripts/a.py"]


def test_skill_md_is_not_in_the_manifest_but_a_nested_one_is() -> None:
    """The folder's own SKILL.md is the text version; cn/SKILL.md is a folder file."""
    rows = [_file("SKILL.md", "c1", 1, new_sha="v1"), _file("cn/SKILL.md", "c1", 1)]
    packages = _packages(rows, _versions("c1"))
    assert _paths(packages.loc[0, "files"]) == ["cn/SKILL.md"]


def test_whitespace_skill_md_edit_is_not_a_package_version() -> None:
    """A SKILL.md commit with no new text version and no file change adds nothing."""
    rows = [
        _file("SKILL.md", "c1", 1, new_sha="v1"),
        _file("SKILL.md", "c2", 2, status="M", new_sha="v1s"),
        _file("SKILL.md", "c3", 3, status="M", new_sha="v2"),
    ]
    packages = _packages(rows, _versions("c1", "c3"))
    assert packages["commit"].tolist() == ["c1", "c3"]
    assert packages["version_seq"].tolist() == [1, 2]
    assert packages["changed_skill_md"].tolist() == [True, True]


def test_files_before_skill_md_join_version_one() -> None:
    """Files added before the first SKILL.md are in package version 1, not a version 0."""
    rows = [_file("scripts/a.py", "c1", 1), _file("SKILL.md", "c2", 2, new_sha="v1")]
    packages = _packages(rows, _versions("c2"))
    assert packages["commit"].tolist() == ["c2"]
    assert _paths(packages.loc[0, "files"]) == ["scripts/a.py"]


def test_folder_rename_keeps_the_files_and_adds_no_package_version() -> None:
    """skills/old -> skills/new with the same blobs: the manifest is the same."""
    rows = [
        _row(
            file_path="SKILL.md",
            repo_path="skills/old/SKILL.md",
            history_role="former_folder",
            status="A",
            new_sha="v1",
            commit_date=_day(1),
        ),
        _row(
            file_path="run.sh",
            repo_path="skills/old/run.sh",
            history_role="former_folder",
            status="A",
            new_sha="r",
            commit_date=_day(1),
        ),
        _file("SKILL.md", "c2", 2, status="R", old_repo_path="skills/old/SKILL.md", new_sha="v1"),
        _file("run.sh", "c2", 2, status="R", old_repo_path="skills/old/run.sh", new_sha="r"),
        _file("run.sh", "c3", 3, status="M", new_sha="r2"),
    ]
    packages = _packages(rows, _versions("c1"))
    assert packages["commit"].tolist() == ["c1", "c3"]
    assert [_paths(files) for files in packages["files"]] == [["run.sh"], ["run.sh"]]
    assert packages["changed_scripts"].tolist() == [True, True]


def test_parallel_copy_delete_does_not_remove_the_own_file() -> None:
    """The same skill in two places: deleting the old copy's script keeps the own one."""
    rows = [
        _file("SKILL.md", "c1", 1, new_sha="v1"),
        _file("scripts/a.py", "c1", 1, new_sha="a"),
        _file(
            "scripts/a.py",
            "c1",
            1,
            new_sha="a",
            history_role="former_folder",
            repo_path="plugins/p/skills/new/scripts/a.py",
        ),
        _file(
            "scripts/a.py",
            "c2",
            2,
            status="D",
            history_role="former_folder",
            repo_path="plugins/p/skills/new/scripts/a.py",
        ),
    ]
    packages = _packages(rows, _versions("c1"))
    assert packages["commit"].tolist() == ["c1"]
    assert _paths(packages.loc[0, "files"]) == ["scripts/a.py"]


def test_copy_source_rows_are_not_package_files() -> None:
    """A copy_source row is the source skill's history, never this folder's file."""
    rows = [
        _file("SKILL.md", "c1", 1, new_sha="v1"),
        _file(
            "scripts/a.py",
            "c2",
            2,
            history_role="copy_source",
            repo_path="skills/new/scripts/a.py",
        ),
    ]
    packages = _packages(rows, _versions("c1"))
    assert packages["commit"].tolist() == ["c1"]
    assert packages.loc[0, "file_count"] == 0


def test_root_skill_keeps_only_the_package_folders() -> None:
    """A repo-root skill: scripts/ counts, src/ and the README do not."""

    def root(path: str, commit: str, day: int) -> dict:
        return _row(
            skill_folder=".",
            file_path=path,
            repo_path=path,
            status="A",
            new_sha=f"{path}@{commit}",
            commit=commit,
            commit_seq=day,
            commit_date=_day(day),
        )

    rows = [root("SKILL.md", "c1", 1), root("README.md", "c1", 1), root("src/main.go", "c2", 2)]
    rows.append(root("scripts/build.sh", "c3", 3))
    packages = _packages(rows, _versions("c1", lineage="acme/copy::."))
    assert packages["commit"].tolist() == ["c1", "c3"]
    assert _paths(packages.loc[1, "files"]) == ["scripts/build.sh"]


def test_removed_skill_has_no_package_version_after_the_delete() -> None:
    """SKILL.md and its script deleted together: no package version at the delete."""
    rows = [
        _file("SKILL.md", "c1", 1, new_sha="v1"),
        _file("run.sh", "c1", 1),
        _file("SKILL.md", "c2", 2, status="D"),
        _file("run.sh", "c2", 2, status="D"),
    ]
    packages = _packages(rows, _versions("c1"))
    assert packages["commit"].tolist() == ["c1"]


def test_delete_and_add_in_one_commit_keeps_the_new_file() -> None:
    """A delete runs before an add of the same path in one commit."""
    rows = [
        _file("SKILL.md", "c1", 1, new_sha="v1"),
        _file("run.sh", "c1", 1, new_sha="old"),
        _file("run.sh", "c2", 2, new_sha="new"),
        _file("run.sh", "c2", 2, status="D"),
    ]
    packages = _packages(rows, _versions("c1"))
    assert packages.loc[1, "files"] == (("run.sh", "new", "100644"),)


def _package_inputs(tmp_path: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Path]]:
    """Stage 3 files and stage 4a tables for two registries, in ``tmp_path``."""
    rows = [
        _file("SKILL.md", "c1", 1, new_sha="v1", is_skill_md=True),
        _file("scripts/a.py", "c2", 2, new_sha="py", is_skill_md=False),
        _file("references/x.md", "c3", 3, new_sha="md", is_skill_md=False),
    ]
    other = _file("SKILL.md", "c1", 1, repo="zed/other", new_sha="v2", is_skill_md=True)
    changes_file = tmp_path / "registry_history_changes.parquet"
    pd.DataFrame([*rows, other]).to_parquet(changes_file)
    texts_file = tmp_path / "registry_history_texts.parquet"
    pd.DataFrame({"sha": ["v1", "v2", "py"], "text": ["skill", "skill", "print(1)\n"]}).to_parquet(
        texts_file
    )
    ids = ["acme/copy::skills/new", "zed/other::skills/new"]
    lineages = pd.DataFrame(
        {
            "lineage_id": ids,
            "repo": ["acme/copy", "zed/other"],
            "skill_folder": "skills/new",
            "version_count": 1,
        }
    )
    versions = pd.DataFrame({"lineage_id": ids, "version_seq": 1, "commit": "c1"})
    return lineages, versions, {"changes": changes_file, "texts": texts_file}


def test_package_tables_are_written_with_sizes_for_stored_texts(tmp_path: Path) -> None:
    """End to end on parquet files: sizes come from stored texts; one batch = many batches."""
    lineages, versions, files = _package_inputs(tmp_path)
    tables = {}
    for limit in (1, 1000):
        out = tmp_path / str(limit)
        out.mkdir()
        paths = {"packages": out / "p.parquet", "manifests": out / "m.parquet"}
        build_package_versions(lineages, versions, files, paths, limit)
        tables[limit] = {name: pd.read_parquet(path) for name, path in paths.items()}
    for name in ("packages", "manifests"):
        pd.testing.assert_frame_equal(tables[1][name], tables[1000][name])
    packages, manifests = tables[1]["packages"], tables[1]["manifests"]
    assert list(packages.columns) == PACKAGE_COLUMNS
    assert list(manifests.columns) == MANIFEST_COLUMNS
    assert packages.groupby("lineage_id").size().tolist() == [3, 1]
    newest = manifests.loc[manifests["package_version_seq"] == 3]
    assert newest["path"].tolist() == ["references/x.md", "scripts/a.py"]
    assert newest["file_category"].tolist() == ["reference", "script"]
    assert newest["size"].isna().tolist() == [True, False]
    assert newest.loc[newest["path"] == "scripts/a.py", "size"].item() == 9
    numbers = summarize_packages(
        lineages,
        versions,
        {
            "packages": packages,
            "facts": {
                "rows_read": 4,
                "batches": 2,
                "manifest_rows": len(manifests),
                "rows_with_size": 2,
                "categories": {"script": 2, "reference": 1},
            },
        },
    )
    assert numbers["lineages_with_scripts_now"] == 1
    assert numbers["text_versions_without_package_version"] == 0


def test_skill_moved_away_and_back_follows_its_skill_md() -> None:
    """Moved to another folder (a version there), then re-added: every version is a package."""
    rows = [
        _file("SKILL.md", "c1", 1, new_sha="v1"),
        _file("SKILL.md", "c2", 2, status="M", new_sha="v2"),
        _file(
            "SKILL.md",
            "c3",
            3,
            status="R",
            history_role="former_folder",
            repo_path="protocols/new/SKILL.md",
            old_repo_path="skills/new/SKILL.md",
            new_sha="v3",
        ),
        _file("SKILL.md", "c4", 4, new_sha="v1"),
    ]
    packages = _packages(rows, _versions("c1", "c2", "c3", "c4"))
    assert packages["commit"].tolist() == ["c1", "c2", "c3", "c4"]
    assert packages["version_seq"].tolist() == [1, 2, 3, 4]


def test_own_rows_at_the_repo_root_before_a_move_are_package_versions() -> None:
    """Stage 3 can file a skill's older root history as own rows (repo_path SKILL.md)."""
    rows = [
        _file("SKILL.md", "c1", 1, repo_path="SKILL.md", new_sha="v1"),
        _file("scripts/a.py", "c1", 1, repo_path="scripts/a.py"),
        _file("src/main.go", "c2", 2, repo_path="src/main.go"),
        _file("SKILL.md", "c3", 3, new_sha="v2"),
    ]
    packages = _packages(rows, _versions("c1", "c3"))
    assert packages["commit"].tolist() == ["c1", "c3"]
    assert _paths(packages.loc[0, "files"]) == ["scripts/a.py"]
    assert packages.loc[1, "file_count"] == 0
