"""Tests for copied-skill history downloads.

Targets are tiny tables. The clone test uses a throw-away git repo and a
file:// URL. No network and no dataset files.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

from skill_genealogy.download_copied_skill_history import (
    _CHANGE_COLUMNS,
    _dev_seconds_per_repo,
    _folder_bytes,
    _print_plan,
    _print_report,
    _repos_still_to_mine,
    export_history_store,
    group_targets_by_repo,
    main,
    mine_copied_skills,
    open_history_store,
    pick_download_targets,
    save_repo_to_store,
)
from skill_genealogy.file_helpers import write_parquet
from skill_genealogy.git_history import _CHANGE_COLUMNS as _GIT_CHANGE_COLUMNS
from skill_genealogy.git_history import RepoResult
from skill_genealogy.git_history import mine_one_repo as _real_mine

_EMAIL = "Tester@Example.com"


def _use_tmp_data(monkeypatch, tmp_path: Path):
    """Point ``data_dir`` at a throw-away folder."""

    def fake(dev: bool) -> Path:
        folder = tmp_path / ("dev" if dev else "full")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    monkeypatch.setattr(
        "skill_genealogy.download_copied_skill_history.data_dir",
        fake,
    )
    return fake


def _copies(rows: list[tuple[str, str, bool]]) -> pd.DataFrame:
    """A copy-candidate frame: repo, path, has_wild_history."""
    return pd.DataFrame(rows, columns=["repo", "path", "has_wild_history"])


def test_change_columns_match_the_git_history_table() -> None:
    """The store keeps every column the miner writes."""
    assert _CHANGE_COLUMNS == list(_GIT_CHANGE_COLUMNS)


def test_pick_download_targets_skips_wild_history(tmp_path: Path, monkeypatch) -> None:
    """Only folders with a file that lacks wild history are targets."""
    fake = _use_tmp_data(monkeypatch, tmp_path)
    frame = _copies(
        [
            ("a/one", "skills/pdf/SKILL.md", False),
            ("a/one", "skills/pdf/Skill.md", False),
            ("a/one", "skills/other/SKILL.md", True),
            ("b/two", "skills/x/SKILL.md", True),
            ("c/root", "SKILL.md", False),
        ]
    )
    write_parquet(frame, fake(True) / "copy_candidates.parquet")
    got = pick_download_targets(True, False)
    assert list(zip(got["repo"], got["skill_folder"], got["reason"], strict=True)) == [
        ("a/one", "skills/pdf", "no_wild_history"),
        ("c/root", ".", "no_wild_history"),
    ]
    grouped = group_targets_by_repo(got)
    assert grouped["a/one"] == ["skills/pdf"]
    assert len(grouped["a/one"]) == 1


def test_include_wild_adds_covered_folders(tmp_path: Path, monkeypatch) -> None:
    """``include_wild`` adds a folder whose files all have wild history."""
    fake = _use_tmp_data(monkeypatch, tmp_path)
    frame = _copies(
        [
            ("a/one", "skills/pdf/SKILL.md", False),
            ("a/one", "skills/pdf/Skill.md", True),
            ("b/two", "skills/left/SKILL.md", True),
            ("b/two", "skills/right/SKILL.md", True),
        ]
    )
    write_parquet(frame, fake(True) / "copy_candidates.parquet")
    got = pick_download_targets(True, True)
    assert list(zip(got["repo"], got["skill_folder"], got["reason"], strict=True)) == [
        ("a/one", "skills/pdf", "no_wild_history"),
        ("b/two", "skills/left", "include_wild"),
        ("b/two", "skills/right", "include_wild"),
    ]
    assert group_targets_by_repo(got)["b/two"] == ["skills/left", "skills/right"]


def test_pick_download_targets_names_the_builder(tmp_path: Path, monkeypatch) -> None:
    """A missing copy table names the stage 5 command."""
    _use_tmp_data(monkeypatch, tmp_path)
    try:
        pick_download_targets(True, False)
    except FileNotFoundError as error:
        message = str(error)
    else:
        raise AssertionError("expected FileNotFoundError")
    assert "find_copies" in message
    assert "--dev" in message


def test_finished_repo_is_not_mined_again(tmp_path: Path) -> None:
    """ok, gone and empty stay done. timeout and error need --retry-failed."""
    connection = open_history_store(tmp_path / "copied_skill_history.duckdb")
    try:
        connection.execute(
            """
            INSERT INTO status VALUES
            ('a/ok', 'ok', '', 'abc', 1.0, true, 1, 2),
            ('a/gone', 'gone', 'missing', NULL, 1.0, false, 0, 0),
            ('a/empty', 'empty', '', NULL, 1.0, false, 0, 0),
            ('a/timeout', 'timeout', 'timed out', NULL, 1.0, false, 0, 0),
            ('a/error', 'error', 'boom', NULL, 1.0, false, 0, 0)
            """
        )
        grouped = {
            "a/ok": ["skills/a"],
            "a/gone": ["skills/a"],
            "a/empty": ["skills/a"],
            "a/timeout": ["skills/a"],
            "a/error": ["skills/a"],
            "a/new": ["skills/a"],
        }
        assert list(_repos_still_to_mine(grouped, connection, False)) == ["a/new"]
        assert list(_repos_still_to_mine(grouped, connection, True)) == [
            "a/timeout",
            "a/error",
            "a/new",
        ]
    finally:
        connection.close()


def test_save_repo_replaces_rows_and_keeps_one_text(tmp_path: Path) -> None:
    """A second save of the same repo replaces its changes. A shared sha is one text."""
    connection = open_history_store(tmp_path / "copied_skill_history.duckdb")
    try:
        sha = "ab" * 20
        save_repo_to_store(
            connection,
            RepoResult("a/one", "ok", "", "h" * 40, 1.5, True),
            _change_frame("a/one", "skills/old", sha),
            pd.DataFrame({"sha": [sha], "text": ["# old\n"]}),
        )
        save_repo_to_store(
            connection,
            RepoResult("a/one", "ok", "", "h" * 40, 2.0, True),
            _change_frame("a/one", "skills/new", sha),
            pd.DataFrame({"sha": [sha], "text": ["# new\n"]}),
        )
        save_repo_to_store(
            connection,
            RepoResult("b/two", "ok", "", "i" * 40, 1.0, True),
            _change_frame("b/two", "skills/new", sha),
            pd.DataFrame({"sha": [sha], "text": ["# other\n"]}),
        )
        out = tmp_path / "out"
        export_history_store(connection, out)
    finally:
        connection.close()
    changes = pd.read_parquet(out / "copied_skill_history_changes.parquet")
    texts = pd.read_parquet(out / "copied_skill_history_texts.parquet")
    status = pd.read_parquet(out / "copied_skill_history_status.parquet")
    assert list(changes["skill_folder"]) == ["skills/new", "skills/new"]
    assert list(changes["repo"]) == ["a/one", "b/two"]
    assert list(texts["sha"]) == [sha]
    assert list(texts["text"]) == ["# old\n"]
    assert int(status.loc[status["repo"] == "a/one", "changes"].iloc[0]) == 1
    assert int(status.loc[status["repo"] == "a/one", "skill_folders"].iloc[0]) == 1


def test_report_counts_follow_the_targeted_repos(tmp_path: Path, capsys) -> None:
    """A limited target list does not count repos that are only in the store."""
    connection = open_history_store(tmp_path / "copied_skill_history.duckdb")
    try:
        sha = "cd" * 20
        save_repo_to_store(
            connection,
            RepoResult("a/one", "ok", "", "h" * 40, 1.0, True),
            _change_frame("a/one", "skills/new", sha),
            pd.DataFrame({"sha": [sha], "text": ["# one\n"]}),
        )
        save_repo_to_store(
            connection,
            RepoResult("b/two", "ok", "", "i" * 40, 2.0, True),
            _change_frame("b/two", "skills/new", sha),
            pd.DataFrame({"sha": [sha], "text": ["# two\n"]}),
        )
        targets = pd.DataFrame(
            {
                "repo": ["a/one"],
                "skill_folder": ["skills/new"],
                "reason": ["no_wild_history"],
            }
        )
        _print_report(connection, targets, tmp_path, None)
    finally:
        connection.close()
    summary = capsys.readouterr().out
    assert "repos_targeted: 1" in summary
    assert "repos_ok: 1" in summary
    assert "changes: 1" in summary
    assert "b/two" not in summary


def test_dev_seconds_is_the_median_of_every_status(tmp_path: Path, monkeypatch) -> None:
    """Gone rows count. The median of 10s and 2s is 6, not the ok mean of 10."""
    fake = _use_tmp_data(monkeypatch, tmp_path)
    history = fake(True) / "copied_skill_history"
    history.mkdir()
    write_parquet(
        pd.DataFrame(
            {
                "repo": ["a/ok", "b/gone"],
                "status": ["ok", "gone"],
                "seconds": [10.0, 2.0],
            }
        ),
        history / "copied_skill_history_status.parquet",
    )
    assert _dev_seconds_per_repo() == 6.0


def test_plan_marks_the_estimate_as_a_lower_bound(capsys, monkeypatch) -> None:
    """The hour line says it is a lower bound."""
    monkeypatch.setattr(
        "skill_genealogy.download_copied_skill_history._dev_seconds_per_repo",
        lambda: 4.0,
    )
    _print_plan({"a/r": ["skills/a"]}, {"a/r": ["skills/a"]}, False)
    assert "lower bound" in capsys.readouterr().out


def test_mine_copied_skills_keeps_only_named_folders(tmp_path: Path) -> None:
    """A folder list mines that folder and leaves the other skill out."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    keep = origin / "skills" / "keep"
    skip = origin / "skills" / "skip"
    keep.mkdir(parents=True)
    skip.mkdir(parents=True)
    (keep / "SKILL.md").write_text("# keep\n")
    (keep / "run.sh").write_text("echo keep\n")
    (skip / "SKILL.md").write_text("# skip\n")
    _commit(origin, "add both", "2026-04-01T00:00:00+00:00")
    (keep / "SKILL.md").write_text("# keep\n\nmore\n")
    _commit(origin, "edit keep", "2026-04-02T00:00:00+00:00")
    connection = open_history_store(tmp_path / "copied_skill_history.duckdb")
    try:
        status = mine_copied_skills(
            {"acme/copy": ["skills/keep"]},
            connection,
            tmp_path / "work",
            1,
            timeout_seconds=60,
            urls={"acme/copy": origin.as_uri()},
        )
        out = tmp_path / "out"
        export_history_store(connection, out)
    finally:
        connection.close()
    assert list(status["status"]) == ["ok"]
    assert int(status["skill_folders"].iloc[0]) == 1
    assert not any((tmp_path / "work").iterdir())
    changes = pd.read_parquet(out / "copied_skill_history_changes.parquet")
    texts = pd.read_parquet(out / "copied_skill_history_texts.parquet")
    assert set(changes["skill_folder"]) == {"skills/keep"}
    assert "skills/skip" not in set(changes["repo_path"].astype(str))
    bodies = "\n".join(texts["text"].astype(str))
    assert "# skip" not in bodies
    assert "# keep" in bodies
    assert "echo keep" in bodies
    skill_rows = changes[changes["file_path"] == "SKILL.md"]
    assert len(skill_rows) >= 2


def test_main_removes_the_work_folder(tmp_path: Path, monkeypatch) -> None:
    """After a file:// run, work/ is gone before disk_bytes is counted."""
    fake = _use_tmp_data(monkeypatch, tmp_path)
    origin = tmp_path / "origin"
    _init_repo(origin)
    keep = origin / "skills" / "keep"
    keep.mkdir(parents=True)
    (keep / "SKILL.md").write_text("# keep\n")
    _commit(origin, "add keep", "2026-04-01T00:00:00+00:00")
    dev = fake(True)
    write_parquet(
        _copies([("acme/copy", "skills/keep/SKILL.md", False)]),
        dev / "copy_candidates.parquet",
    )
    work_present: list[bool] = []
    real_bytes = _folder_bytes

    def bytes_after_cleanup(folder: Path) -> int:
        work_present.append((folder / "work").exists())
        return real_bytes(folder)

    def mine_local(repo, work_dir, folders, timeout_seconds, url=None):
        stale = Path(work_dir) / "sQVe__dotfiles"
        stale.mkdir(parents=True, exist_ok=True)
        (stale / "blob").write_bytes(b"x" * 200_000)
        return _real_mine(repo, work_dir, folders, timeout_seconds, origin.as_uri())

    monkeypatch.setattr(
        "skill_genealogy.download_copied_skill_history.mine_one_repo",
        mine_local,
    )
    monkeypatch.setattr(
        "skill_genealogy.download_copied_skill_history._folder_bytes",
        bytes_after_cleanup,
    )
    monkeypatch.setattr(sys, "argv", ["download_copied_skill_history", "--dev"])
    main()
    history = dev / "copied_skill_history"
    assert work_present == [False]
    assert not (history / "work").exists()


def test_keyerror_from_the_miner_is_saved_as_error(tmp_path: Path, monkeypatch) -> None:
    """A KeyError from the miner is status error and names KeyError."""
    sha = "4fd78c9e85695eefe9a4d30f5315ca7b6201fb3d"

    def boom(*_args, **_kwargs):
        raise KeyError(sha)

    monkeypatch.setattr(
        "skill_genealogy.download_copied_skill_history.mine_one_repo",
        boom,
    )
    connection = open_history_store(tmp_path / "copied_skill_history.duckdb")
    try:
        status = mine_copied_skills(
            {"acme/copy": ["skills/keep"]},
            connection,
            tmp_path / "work",
            1,
        )
        saved = connection.execute("SELECT status, message FROM status").fetchone()
    finally:
        connection.close()
    expected = f"KeyError: {sha} (commit missing from the mined history)"
    assert list(status["status"]) == ["error"]
    assert status["message"].iloc[0] == expected
    assert saved == ("error", expected)


def _change_frame(repo: str, folder: str, sha: str) -> pd.DataFrame:
    """One add of SKILL.md, for the store round-trip test."""
    row = {name: "" for name in _CHANGE_COLUMNS}
    row.update(
        {
            "repo": repo,
            "skill_folder": folder,
            "file_path": "SKILL.md",
            "commit": "c" * 40,
            "commit_date": "2026-01-01T00:00:00+00:00",
            "status": "A",
            "new_sha": sha,
            "old_sha": "0" * 40,
            "is_skill_md": True,
            "is_script": False,
            "commit_seq": 1,
            "repo_path": f"{folder}/SKILL.md",
            "history_role": "own",
        }
    )
    return pd.DataFrame([row])


def _git(cwd: Path, args: list[str], env: dict[str, str] | None = None) -> None:
    """Run git and raise AssertionError with stderr when it fails."""
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr or completed.stdout
        raise AssertionError(detail)


def _git_env(date: str) -> dict[str, str]:
    """Author and committer for one commit, with a fixed date."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Tester",
            "GIT_AUTHOR_EMAIL": _EMAIL,
            "GIT_COMMITTER_NAME": "Tester",
            "GIT_COMMITTER_EMAIL": _EMAIL,
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _init_repo(path: Path) -> None:
    """Create an empty repo that allows a blobless file:// clone."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, ["init", "-b", "main"])
    _git(path, ["config", "user.email", _EMAIL])
    _git(path, ["config", "user.name", "Tester"])
    _git(path, ["config", "uploadpack.allowFilter", "true"])
    _git(path, ["config", "core.autocrlf", "false"])
    _git(path, ["config", "core.filemode", "true"])


def _commit(path: Path, message: str, date: str) -> None:
    """Stage everything and commit it at ``date``."""
    env = _git_env(date)
    _git(path, ["add", "."], env)
    _git(path, ["commit", "-m", message], env)
