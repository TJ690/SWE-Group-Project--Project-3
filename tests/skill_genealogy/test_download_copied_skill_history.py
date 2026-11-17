"""Tests for stage 6, the copied-skill history download.

Targets come from tiny hand-made copy tables. The clone tests use throw-away
local repos and ``file://`` URLs (helpers from the G05 tests). No network and
no dataset files.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from tests.skill_genealogy.test_git_history import _commit, _init_repo

from skill_genealogy import download_copied_skill_history as stage6
from skill_genealogy.download_copied_skill_history import (
    _CHANGE_COLUMNS,
    export_history_store,
    group_targets_by_repo,
    mine_copied_skills,
    open_history_store,
    pick_download_targets,
    repos_still_to_mine,
    save_repo_to_store,
)
from skill_genealogy.file_helpers import write_parquet
from skill_genealogy.git_history import _CHANGE_COLUMNS as GIT_CHANGE_COLUMNS
from skill_genealogy.git_history import RepoResult


def _use_tmp_data(monkeypatch, tmp_path: Path):
    """Point ``data_dir`` of the stage at a throw-away folder."""

    def fake(dev: bool) -> Path:
        folder = tmp_path / ("dev" if dev else "full")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    monkeypatch.setattr(stage6, "data_dir", fake)
    return fake


def _write_copies(fake, rows: list[tuple[str, str]]) -> None:
    """Write a dev ``copy_candidates.parquet`` with ``repo`` and ``path``."""
    write_parquet(
        pd.DataFrame(rows, columns=["repo", "path"]), fake(True) / "copy_candidates.parquet"
    )


def _changes(repo: str, folder: str, sha: str) -> pd.DataFrame:
    """One SKILL.md add row with the miner's columns."""
    row = {name: "" for name in GIT_CHANGE_COLUMNS}
    row.update(
        repo=repo,
        skill_folder=folder,
        file_path="SKILL.md",
        commit="c" * 40,
        commit_date="2026-01-01T00:00:00+00:00",
        status="A",
        new_sha=sha,
        is_skill_md=True,
        is_script=False,
        commit_seq=1,
        history_role="own",
    )
    return pd.DataFrame([row])


def _ok(repo: str) -> RepoResult:
    return RepoResult(repo, "ok", "", "h" * 40, 1.0, True)


def test_change_columns_match_the_registry_table() -> None:
    """The store keeps the stage 3 changes columns, in the same order."""
    assert _CHANGE_COLUMNS == list(GIT_CHANGE_COLUMNS)


def test_one_target_per_repo_and_folder(tmp_path: Path, monkeypatch) -> None:
    """Two files in one folder are one target; different folders are two; root is '.'."""
    fake = _use_tmp_data(monkeypatch, tmp_path)
    _write_copies(
        fake,
        [
            ("a/one", "skills/pdf/SKILL.md"),
            ("a/one", "skills/pdf/skill.md"),
            ("a/one", "skills/other/SKILL.md"),
            ("b/two", "SKILL.md"),
        ],
    )
    got = pick_download_targets(True)
    assert got.values.tolist() == [
        ["a/one", "skills/other"],
        ["a/one", "skills/pdf"],
        ["b/two", "."],
    ]
    assert group_targets_by_repo(got) == {"a/one": ["skills/other", "skills/pdf"], "b/two": ["."]}


def test_missing_copy_table_names_the_builder(tmp_path: Path, monkeypatch) -> None:
    """A missing stage 5c table stops the run and names the command."""
    _use_tmp_data(monkeypatch, tmp_path)
    with pytest.raises((FileNotFoundError, SystemExit)) as raised:
        pick_download_targets(True)
    assert "find_copies --dev --only copies" in str(raised.value)


def test_finished_repo_is_not_mined_again(tmp_path: Path) -> None:
    """ok, gone, empty and too_large stay done; timeout and error need --retry-failed."""
    con = open_history_store(tmp_path / "store.duckdb")
    for repo, status in [
        ("a/ok", "ok"),
        ("a/gone", "gone"),
        ("a/empty", "empty"),
        ("a/big", "too_large"),
        ("a/slow", "timeout"),
        ("a/bad", "error"),
    ]:
        save_repo_to_store(
            con, RepoResult(repo, status, "", None, 0.0, False), pd.DataFrame(), pd.DataFrame()
        )
    targets = {
        name: ["x"] for name in ["a/ok", "a/gone", "a/empty", "a/big", "a/slow", "a/bad", "a/new"]
    }
    assert list(repos_still_to_mine(targets, con, False)) == ["a/new"]
    assert list(repos_still_to_mine(targets, con, True)) == ["a/slow", "a/bad", "a/new"]
    con.close()


def test_save_repo_replaces_rows_and_keeps_one_text(tmp_path: Path) -> None:
    """A second save replaces the repo's rows; a sha shared by two repos is one text."""
    con = open_history_store(tmp_path / "store.duckdb")
    texts = pd.DataFrame({"sha": ["s" * 40], "text": ["# hi\n"]})
    save_repo_to_store(con, _ok("a/r"), _changes("a/r", "x", "s" * 40), texts)
    save_repo_to_store(con, _ok("a/r"), _changes("a/r", "y", "s" * 40), texts)
    save_repo_to_store(con, _ok("b/r"), _changes("b/r", "z", "s" * 40), texts)
    folders = con.execute("SELECT repo, skill_folder FROM changes ORDER BY repo").fetchall()
    assert folders == [("a/r", "y"), ("b/r", "z")]
    assert con.execute("SELECT count(*) FROM texts").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM status").fetchone()[0] == 2
    export_history_store(con, tmp_path)
    exported = pd.read_parquet(tmp_path / "copied_skill_history_texts.parquet")
    assert exported["sha"].tolist() == ["s" * 40]
    con.close()


def test_failed_save_rolls_back(tmp_path: Path, monkeypatch) -> None:
    """A save that fails after the delete leaves the repo's earlier rows untouched."""
    con = open_history_store(tmp_path / "store.duckdb")
    save_repo_to_store(con, _ok("a/r"), _changes("a/r", "x", "s" * 40), pd.DataFrame())
    monkeypatch.setattr(stage6, "_INSERT_TEXTS_SQL", "INSERT INTO no_such_table VALUES (1)")
    texts = pd.DataFrame({"sha": ["t" * 40], "text": ["# new\n"]})
    with pytest.raises(Exception, match="no_such_table"):
        save_repo_to_store(con, _ok("a/r"), _changes("a/r", "y", "t" * 40), texts)
    assert con.execute("SELECT skill_folder FROM changes").fetchall() == [("x",)]
    assert con.execute("SELECT count(*) FROM status").fetchone()[0] == 1
    con.close()


def test_too_large_repo_is_not_cloned(tmp_path: Path, monkeypatch) -> None:
    """A repo above the folder cap is saved as too_large without a clone."""
    monkeypatch.setattr(stage6, "COPY_HISTORY_MAX_FOLDERS", 2)
    con = open_history_store(tmp_path / "store.duckdb")
    status = mine_copied_skills(
        {"a/r": ["a", "b", "c"]}, con, tmp_path / "work", 2, urls={"a/r": "file:///does/not/exist"}
    )
    assert status["status"].tolist() == ["too_large"]
    assert "3 copy folders" in status["message"].iloc[0]
    con.close()


def test_mine_copied_skills_keeps_only_named_folders(tmp_path: Path) -> None:
    """A file:// repo mined for one folder stores only that folder's rows and texts."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    for name in ("keep", "skip"):
        (origin / "skills" / name).mkdir(parents=True)
        (origin / "skills" / name / "SKILL.md").write_text(f"# {name}\n")
    _commit(origin, "add", "2026-01-01T00:00:00+00:00")
    (origin / "skills" / "keep" / "SKILL.md").write_text("# keep\n\nmore\n")
    _commit(origin, "edit", "2026-01-02T00:00:00+00:00")
    con = open_history_store(tmp_path / "store.duckdb")
    work = tmp_path / "work"
    status = mine_copied_skills(
        {"o/r": ["skills/keep"]}, con, work, 1, urls={"o/r": origin.resolve().as_uri()}
    )
    assert status["status"].tolist() == ["ok"]
    assert status["changes"].tolist() == [2]
    folders = con.execute("SELECT DISTINCT skill_folder FROM changes").fetchall()
    assert folders == [("skills/keep",)]
    texts = [row[0] for row in con.execute("SELECT text FROM texts ORDER BY text").fetchall()]
    assert texts == ["# keep\n", "# keep\n\nmore\n"]
    assert not any(work.iterdir())
    con.close()


def test_report_counts_only_this_runs_repos(tmp_path: Path, capsys) -> None:
    """The summary counts the targeted repos only and prints commit dates in UTC."""
    con = open_history_store(tmp_path / "store.duckdb")
    first = _changes("a/r", "x", "s" * 40)
    first["commit_date"] = "2026-01-01T03:00:00+03:00"
    save_repo_to_store(con, _ok("a/r"), first, pd.DataFrame({"sha": ["s" * 40], "text": ["x"]}))
    save_repo_to_store(con, _ok("b/r"), _changes("b/r", "y", "t" * 40), pd.DataFrame())
    targets = pd.DataFrame({"repo": ["a/r", "a/r"], "skill_folder": ["x", "missing"]})
    stage6._print_report(con, targets, tmp_path, None)
    out = capsys.readouterr().out
    assert "repos_targeted: 1" in out
    assert "repos_ok: 1" in out
    assert "folders_without_history: 1" in out
    assert "change_rows: 1" in out
    assert "first_commit_utc: 2026-01-01T00:00:00Z" in out
    con.close()


def test_nested_skill_md_is_not_a_version(tmp_path: Path, capsys) -> None:
    """A parent folder's history holds nested skills; only its own SKILL.md is a version."""
    con = open_history_store(tmp_path / "store.duckdb")
    own = _changes("a/r", "skills", "s" * 40)
    nested = _changes("a/r", "skills", "t" * 40)
    nested["file_path"] = "pdf/SKILL.md"
    nested["repo_path"] = "skills/pdf/SKILL.md"
    save_repo_to_store(con, _ok("a/r"), pd.concat([own, nested]), pd.DataFrame())
    targets = pd.DataFrame({"repo": ["a/r"], "skill_folder": ["skills"]})
    stage6._print_report(con, targets, tmp_path, None)
    out = capsys.readouterr().out
    assert "share_more_than_one_version: 0" in out
    assert "versions_median: 1" in out
    con.close()
