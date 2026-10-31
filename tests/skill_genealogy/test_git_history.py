"""Tests for git history helpers.

Throw-away repos are built with git init. Clones use a file:// URL.
No network and no dataset files.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from skill_genealogy.git_history import (
    GitError,
    RepoResult,
    clone_history_only,
    hash_author_email,
    head_commit,
    list_folder_at_commit,
    list_skill_md_paths,
    merge_shards,
    mine_one_repo,
    parse_raw_log,
    read_blob_texts,
    read_folder_log,
    run_git,
    write_repo_shard,
)
from skill_genealogy.settings import MAX_SCRIPT_BYTES

_EMAIL = "Tester@Example.com"
_ZERO = "0" * 40
_OLD = "a" * 40
_NEW = "b" * 40
_SAME = "c" * 40


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


def _build_renamed_skill(origin: Path) -> None:
    """Add, edit, add a script, then rename skills/pdf to skills/pdf-tools."""
    _init_repo(origin)
    skill = origin / "skills" / "pdf"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# pdf\n\nhello\n")
    _commit(origin, "add skill", "2026-01-01T00:00:00+00:00")
    (skill / "SKILL.md").write_text("# pdf\n\nhello world\n")
    _commit(origin, "edit skill", "2026-01-02T00:00:00+00:00")
    (skill / "scripts").mkdir()
    (skill / "scripts" / "run.sh").write_text("echo hi\n")
    (skill / "references").mkdir()
    (skill / "references" / "api.md").write_text("api\n")
    nested = skill / "node_modules" / "pkg"
    nested.mkdir(parents=True)
    (nested / "index.js").write_text("x\n")
    _commit(origin, "add script", "2026-01-03T00:00:00+00:00")
    env = _git_env("2026-01-04T00:00:00+00:00")
    _git(origin, ["mv", "skills/pdf", "skills/pdf-tools"], env)
    _git(origin, ["commit", "-m", "rename folder"], env)


def _mine(
    origin: Path, work: Path, repo: str = "acme/pdf"
) -> tuple[RepoResult, pd.DataFrame, pd.DataFrame]:
    """Mine ``origin`` through a file:// URL into ``work``."""
    return mine_one_repo(repo, work, None, 60, url=origin.as_uri())


def test_parse_raw_log_sample() -> None:
    """A fixed transcript yields A, R and a C-quoted path. The email is hashed."""
    commit_add = "d" * 40
    commit_rename = "e" * 40
    commit_cafe = "f" * 40
    sample = (
        f"@@{commit_rename}|2026-01-03T03:04:05+00:00|Dev@Example.com|rename folder\n"
        "\n"
        f":100644 100644 {_SAME} {_SAME} R100\tskills/pdf/SKILL.md\tskills/pdf-tools/SKILL.md\n"
        f"@@{commit_add}|2026-01-02T03:04:05+00:00|Dev@Example.com|edit skill\n"
        "\n"
        f":100644 100644 {_OLD} {_NEW} M\tskills/pdf/SKILL.md\n"
        f"@@{commit_cafe}|2026-01-04T03:04:05+00:00|Dev@Example.com|add | skill\n"
        "\n"
        f':000000 100644 {_ZERO} {_NEW} A\t"skills/caf\\303\\251/SKILL.md"\n'
    )
    rows = parse_raw_log(sample)
    assert [row["status"] for row in rows] == ["R", "M", "A"]
    renamed = rows[0]
    assert renamed["old_path"] == "skills/pdf/SKILL.md"
    assert renamed["path"] == "skills/pdf-tools/SKILL.md"
    assert renamed["old_sha"] == renamed["new_sha"] == _SAME
    assert rows[1]["old_path"] == ""
    assert rows[1]["path"] == "skills/pdf/SKILL.md"
    assert rows[2]["subject"] == "add | skill"
    assert rows[2]["path"] == "skills/café/SKILL.md"
    assert rows[2]["author_key"] == hash_author_email("Dev@Example.com")
    assert "@" not in rows[2]["author_key"]
    assert parse_raw_log("") == []


def test_hash_author_email_ignores_case_and_space() -> None:
    """The same address in any case is one key. A blank email still has a key."""
    assert hash_author_email("Test@Example.com") == hash_author_email("  test@example.com ")
    assert len(hash_author_email("")) == 40


def test_folder_rename_is_followed(tmp_path: Path) -> None:
    """A renamed skill folder keeps add, edit, script add and the rename itself."""
    origin = tmp_path / "origin"
    _build_renamed_skill(origin)
    clone = tmp_path / "clone"
    clone_history_only("acme/pdf", clone, 60, url=origin.as_uri())
    head = head_commit(clone)
    assert head is not None
    assert list_skill_md_paths(clone) == ["skills/pdf-tools/SKILL.md"]
    without_follow = read_folder_log(clone, "skills/pdf-tools", follow=False)
    followed = read_folder_log(clone, "skills/pdf-tools", follow=True)
    assert not any(row["subject"] == "add skill" for row in without_follow)
    assert any(row["subject"] == "add skill" for row in followed)
    assert any(row["subject"] == "add script" for row in followed)
    listing = list_folder_at_commit(clone, head, "skills/pdf-tools")
    skill_row = next(row for row in listing if row["path"].endswith("SKILL.md"))
    assert skill_row["mode"] == "100644"
    # GIT7 leaves lazy fetch off, so a missing blob's size stays None.
    assert skill_row["size"] is None
    texts = read_blob_texts(clone, [skill_row["sha"]], max_bytes=MAX_SCRIPT_BYTES)
    assert texts[skill_row["sha"]] == "# pdf\n\nhello world\n"

    result, changes, texts_frame = _mine(origin, tmp_path / "work")
    assert result.status == "ok"
    assert result.head_commit == head
    assert result.follow_renames is True
    assert result.message == ""
    assert not any((tmp_path / "work").iterdir())
    assert set(changes["skill_folder"]) == {"skills/pdf-tools"}
    assert set(changes["author_key"]) == {hash_author_email(_EMAIL)}
    got = list(
        zip(
            changes["commit_seq"],
            changes["file_path"],
            changes["status"],
            strict=True,
        )
    )
    assert got == [
        (1, "SKILL.md", "A"),
        (2, "SKILL.md", "M"),
        (3, "references/api.md", "A"),
        (3, "scripts/run.sh", "A"),
        (4, "SKILL.md", "R"),
        (4, "references/api.md", "R"),
        (4, "scripts/run.sh", "R"),
    ]
    renamed = changes[(changes["status"] == "R") & (changes["file_path"] == "SKILL.md")].iloc[0]
    assert renamed["old_file_path"] == "SKILL.md"
    assert renamed["old_sha"] == renamed["new_sha"]
    assert renamed["commit_date"].startswith("2026-01-04")
    assert not changes["file_path"].str.contains("node_modules").any()
    script = changes[
        (changes["file_path"] == "scripts/run.sh") & (changes["status"] == "A")
    ].iloc[0]
    assert script["is_script"] and not script["is_skill_md"]
    skill = changes[(changes["file_path"] == "SKILL.md") & (changes["status"] == "A")].iloc[0]
    assert skill["is_skill_md"] and not skill["is_script"]
    reference = changes[changes["file_path"] == "references/api.md"].iloc[0]
    assert not reference["is_skill_md"] and not reference["is_script"]
    assert set(texts_frame["text"]) == {
        "# pdf\n\nhello\n",
        "# pdf\n\nhello world\n",
        "echo hi\n",
    }


def test_rename_with_edit_in_the_same_commit(tmp_path: Path) -> None:
    """A folder rename that also edits SKILL.md stays R, and both commits are numbered."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    folder = origin / "skills" / "pdf"
    folder.mkdir(parents=True)
    body = "".join(f"line {index}\n" for index in range(40))
    (folder / "SKILL.md").write_text(body)
    _commit(origin, "add skill", "2026-07-01T00:00:00+00:00")
    env = _git_env("2026-07-02T00:00:00+00:00")
    _git(origin, ["mv", "skills/pdf", "skills/pdf-tools"], env)
    (origin / "skills" / "pdf-tools" / "SKILL.md").write_text(body + "one more line\n")
    _git(origin, ["add", "."], env)
    _git(origin, ["commit", "-m", "rename and edit"], env)
    result, changes, _texts = _mine(origin, tmp_path / "work", repo="acme/edit-rename")
    assert result.status == "ok"
    assert result.message == ""
    renamed = changes[changes["status"] == "R"]
    assert list(renamed["file_path"]) == ["SKILL.md"]
    assert list(renamed["old_file_path"]) == ["SKILL.md"]
    assert sorted(int(value) for value in changes["commit_seq"].unique()) == [1, 2]
    first = changes[changes["commit_seq"] == 1]
    assert list(first["status"]) == ["A"]
    assert list(first["file_path"]) == ["SKILL.md"]


def test_reused_folder_name_stays_two_skills(tmp_path: Path) -> None:
    """A new skill at an old folder name does not join the skill that was renamed away."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    folder = origin / "s" / "p"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("# old\n")
    _commit(origin, "add p", "2026-03-01T00:00:00+00:00")
    env = _git_env("2026-03-02T00:00:00+00:00")
    _git(origin, ["mv", "s/p", "s/q"], env)
    _git(origin, ["commit", "-m", "rename to q"], env)
    reused = origin / "s" / "p"
    reused.mkdir(parents=True)
    (reused / "SKILL.md").write_text("# new p\n")
    _commit(origin, "new p", "2026-03-03T00:00:00+00:00")
    result, changes, _texts = _mine(origin, tmp_path / "work", repo="acme/reused")
    assert result.status == "ok"
    assert result.message == ""
    renamed = changes[changes["skill_folder"] == "s/q"]
    got = list(zip(renamed["file_path"], renamed["status"], renamed["commit_seq"], strict=True))
    assert got == [("SKILL.md", "A", 1), ("SKILL.md", "R", 2)]
    fresh = changes[changes["skill_folder"] == "s/p"]
    assert list(
        zip(fresh["file_path"], fresh["status"], fresh["commit_seq"], fresh["subject"], strict=True)
    ) == [("SKILL.md", "A", 1, "new p")]


def test_deleted_skill_md_keeps_older_history(tmp_path: Path) -> None:
    """A delete and restore of SKILL.md keeps older commits and files that stayed."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    folder = origin / "s" / "p"
    (folder / "scripts").mkdir(parents=True)
    (folder / "SKILL.md").write_text("# p\n")
    (folder / "scripts" / "run.sh").write_text("echo hi\n")
    _commit(origin, "add skill", "2026-03-01T00:00:00+00:00")
    (folder / "SKILL.md").write_text("# p\n\nmore\n")
    _commit(origin, "edit skill", "2026-03-02T00:00:00+00:00")
    _git(origin, ["rm", "s/p/SKILL.md"])
    _commit(origin, "delete skill", "2026-03-03T00:00:00+00:00")
    (folder / "SKILL.md").write_text("# p\n\nmore\n")
    _commit(origin, "restore skill", "2026-03-04T00:00:00+00:00")
    result, changes, _texts = _mine(origin, tmp_path / "work", repo="acme/restored")
    assert result.status == "ok"
    got = list(zip(changes["commit_seq"], changes["file_path"], changes["status"], strict=True))
    assert got == [
        (1, "SKILL.md", "A"),
        (1, "scripts/run.sh", "A"),
        (2, "SKILL.md", "M"),
        (3, "SKILL.md", "D"),
        (4, "SKILL.md", "A"),
    ]


def test_rename_out_of_skipped_folder_is_an_add(tmp_path: Path) -> None:
    """A file moved from node_modules into the skill is an add, not a dropped rename."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    skill = origin / "skills" / "p"
    (skill / "node_modules").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# p\n")
    (skill / "node_modules" / "x.js").write_text("x\n")
    _commit(origin, "add skill", "2026-03-01T00:00:00+00:00")
    (skill / "scripts").mkdir()
    env = _git_env("2026-03-02T00:00:00+00:00")
    _git(origin, ["mv", "skills/p/node_modules/x.js", "skills/p/scripts/x.js"], env)
    _git(origin, ["commit", "-m", "move script"], env)
    result, changes, _texts = _mine(origin, tmp_path / "work", repo="acme/vendored")
    assert result.status == "ok"
    moved = changes[changes["file_path"] == "scripts/x.js"]
    assert len(moved) == 1
    row = moved.iloc[0]
    assert row["status"] == "A"
    assert row["old_file_path"] == ""
    assert row["old_sha"] == _ZERO
    assert row["old_mode"] == "000000"
    assert not changes["file_path"].str.contains("node_modules").any()


def test_edited_file_rename_keeps_later_commits(tmp_path: Path) -> None:
    """An inexact rename inside the folder keeps every later commit, and seq 1 is the add."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    folder = origin / "skills" / "q"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("# q\n")
    _commit(origin, "add skill", "2026-07-11T00:00:00+00:00")
    body = "".join(f"ref {index}\n" for index in range(20))
    (folder / "ref.md").write_text(body)
    _commit(origin, "add ref", "2026-07-12T00:00:00+00:00")
    env = _git_env("2026-07-13T00:00:00+00:00")
    _git(origin, ["mv", "skills/q/ref.md", "skills/q/ref2.md"], env)
    (folder / "ref2.md").write_text(body + "edited\n")
    _git(origin, ["add", "."], env)
    _git(origin, ["commit", "-m", "rename ref"], env)
    (folder / "SKILL.md").write_text("# q\n\nmore\n")
    _commit(origin, "edit skill", "2026-07-14T00:00:00+00:00")
    result, changes, _texts = _mine(origin, tmp_path / "work", repo="acme/q")
    assert result.status == "ok"
    assert result.message == ""
    got = list(
        zip(changes["commit_seq"], changes["file_path"], changes["status"], strict=True)
    )
    assert got == [
        (1, "SKILL.md", "A"),
        (2, "ref.md", "A"),
        (3, "ref2.md", "R"),
        (4, "SKILL.md", "M"),
    ]
    renamed = changes[changes["status"] == "R"].iloc[0]
    assert renamed["old_file_path"] == "ref.md"
    assert int(changes["commit_seq"].min()) == 1


def test_root_skill_does_not_absorb_nested_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root SKILL.md and skills/y/SKILL.md stay two rows, in follow mode and in GIT6."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    (origin / "SKILL.md").write_text("# root\n")
    nested = origin / "skills" / "y"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("# y\n")
    _commit(origin, "add both", "2026-09-01T00:00:00+00:00")
    result, changes, _texts = _mine(origin, tmp_path / "work", repo="acme/root")
    assert result.status == "ok"
    assert result.follow_renames is True
    got = list(zip(changes["skill_folder"], changes["file_path"], changes["status"], strict=True))
    assert got == [(".", "SKILL.md", "A"), ("skills/y", "SKILL.md", "A")]
    monkeypatch.setattr("skill_genealogy.git_history.MANY_PATHS_LIMIT", 0)
    result_many, changes_many, _texts_many = _mine(
        origin, tmp_path / "work-many", repo="acme/root"
    )
    assert result_many.status == "ok"
    assert result_many.follow_renames is False
    got_many = list(
        zip(
            changes_many["skill_folder"],
            changes_many["file_path"],
            changes_many["status"],
            strict=True,
        )
    )
    assert got_many == got


def test_skipped_only_commit_does_not_gap_commit_seq(tmp_path: Path) -> None:
    """A commit that only touches node_modules does not leave a hole in commit_seq."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    skill = origin / "skills" / "x"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# x\n")
    _commit(origin, "add skill", "2026-08-01T00:00:00+00:00")
    nested = skill / "node_modules"
    nested.mkdir()
    (nested / "i.js").write_text("i\n")
    _commit(origin, "vendor", "2026-08-02T00:00:00+00:00")
    (skill / "SKILL.md").write_text("# x\n\nmore\n")
    _commit(origin, "edit skill", "2026-08-03T00:00:00+00:00")
    result, changes, _texts = _mine(origin, tmp_path / "work", repo="acme/gap")
    assert result.status == "ok"
    seqs = sorted(int(value) for value in changes["commit_seq"].unique())
    assert seqs == [1, 2]
    assert list(zip(changes["commit_seq"], changes["status"], strict=True)) == [
        (1, "A"),
        (2, "M"),
    ]
    assert not changes["file_path"].str.contains("node_modules").any()


def test_lowercase_skill_md_is_found(tmp_path: Path) -> None:
    """skill.md in any case is the skill file. The folder name keeps its case."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    folder = origin / "Skills" / "Demo"
    folder.mkdir(parents=True)
    (folder / "skill.md").write_text("# demo\n")
    _commit(origin, "add demo", "2026-02-01T00:00:00+00:00")
    result, changes, texts = _mine(origin, tmp_path / "work", repo="acme/demo")
    assert result.status == "ok"
    assert list(changes["skill_folder"]) == ["Skills/Demo"]
    assert list(changes["file_path"]) == ["skill.md"]
    assert bool(changes["is_skill_md"].iloc[0]) is True
    assert list(texts["text"]) == ["# demo\n"]


def test_empty_repo_status(tmp_path: Path) -> None:
    """A clone with no commits is status empty, and the clone is deleted."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    result, changes, texts = _mine(origin, tmp_path / "work", repo="acme/empty")
    assert result.status == "empty"
    assert result.head_commit is None
    assert result.follow_renames is False
    assert changes.empty and texts.empty
    assert not (tmp_path / "work" / "acme__empty").exists()


def test_missing_repo_does_not_raise(tmp_path: Path) -> None:
    """A path that is not a repo is gone or error, never an exception."""
    missing = tmp_path / "no-such-repo.git"
    result, changes, texts = mine_one_repo(
        "acme/missing",
        tmp_path / "work",
        None,
        60,
        url=missing.as_uri(),
    )
    assert result.status in {"gone", "error"}
    assert result.message
    assert changes.empty and texts.empty
    assert not (tmp_path / "work" / "acme__missing").exists()


def test_large_script_is_listed_but_not_fetched_into_texts(tmp_path: Path) -> None:
    """A 300 KB script is a change row. Its text is not stored. A small script is."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    folder = origin / "skills" / "big"
    (folder / "scripts").mkdir(parents=True)
    (folder / "tools").mkdir()
    (folder / "SKILL.md").write_text("# big\n")
    (folder / "scripts" / "small.sh").write_text("echo ok\n")
    (folder / "scripts" / "huge.sh").write_bytes(b"x" * 300_000)
    (folder / "notes.txt").write_text("notes\n")
    tool = folder / "tools" / "run"
    tool.write_text("echo tool\n")
    env = _git_env("2026-03-01T00:00:00+00:00")
    _git(origin, ["add", "."], env)
    _git(origin, ["update-index", "--chmod=+x", "skills/big/tools/run"], env)
    _git(origin, ["commit", "-m", "add big"], env)
    result, changes, texts = _mine(origin, tmp_path / "work", repo="acme/big")
    assert result.status == "ok"
    huge = changes[changes["file_path"] == "scripts/huge.sh"]
    assert not huge.empty
    assert bool(huge["is_script"].iloc[0]) is True
    assert all(len(text.encode("utf-8")) <= MAX_SCRIPT_BYTES for text in texts["text"])
    assert "echo ok\n" in set(texts["text"])
    assert "# big\n" in set(texts["text"])
    assert "notes\n" not in set(texts["text"])
    assert "echo tool\n" in set(texts["text"])
    tool_rows = changes[changes["file_path"] == "tools/run"]
    assert (tool_rows["new_mode"] == "100755").any()


def test_many_folders_do_not_follow_renames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Above MANY_PATHS_LIMIT, one log is used and follow_renames is false."""
    monkeypatch.setattr("skill_genealogy.git_history.MANY_PATHS_LIMIT", 1)
    origin = tmp_path / "origin"
    _init_repo(origin)
    for name in ("one", "two"):
        folder = origin / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"# {name}\n")
    _commit(origin, "add two", "2026-04-01T00:00:00+00:00")
    result, changes, _texts = _mine(origin, tmp_path / "work", repo="acme/many")
    assert result.status == "ok"
    assert result.follow_renames is False
    assert set(changes["skill_folder"]) == {"skills/one", "skills/two"}


@pytest.mark.parametrize(
    ("stderr", "status"),
    [
        ("remote: Repository not found.", "gone"),
        ("fatal: repository 'https://github.com/a/b.git/' not found", "gone"),
        ("fatal: Authentication failed for 'https://github.com/a/b.git/'", "gone"),
        ("fatal: could not read Username: terminal prompts disabled", "gone"),
        ("The requested URL returned error: 404", "gone"),
        ("fatal: boom", "error"),
    ],
)
def test_clone_errors_map_to_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str, status: str
) -> None:
    """Known clone failures become gone or error. They do not raise."""

    def fail(args: list[str], cwd: Path | None, timeout_seconds: int) -> str:
        raise GitError(stderr, stderr=stderr)

    monkeypatch.setattr("skill_genealogy.git_history.run_git", fail)
    result, changes, texts = mine_one_repo("acme/bad", tmp_path, None, 30)
    assert result.status == status
    assert result.message
    assert changes.empty and texts.empty


def test_timeout_is_a_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A git command that exceeds its limit is status timeout, not an exception."""

    def slow(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr("skill_genealogy.git_history.subprocess.run", slow)
    result, changes, texts = mine_one_repo(
        "acme/slow", tmp_path, None, 1, url="file:///does-not-matter"
    )
    assert result.status == "timeout"
    assert result.message == "timed out"
    assert changes.empty and texts.empty


def test_lazy_fetch_when_blob_fetch_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If git fetch of blob ids fails, cat-file still reads the text on demand."""

    def refuse(clone_dir: Path, shas: list[str], timeout_seconds: int) -> None:
        raise GitError(
            "Server does not allow request for unadvertised object",
            stderr="Server does not allow request for unadvertised object",
        )

    monkeypatch.setattr("skill_genealogy.git_history._fetch_blob_batch", refuse)
    origin = tmp_path / "origin"
    _init_repo(origin)
    folder = origin / "skills" / "pdf"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("# still here\n")
    _commit(origin, "add", "2026-05-01T00:00:00+00:00")
    result, _changes, texts = _mine(origin, tmp_path / "work", repo="acme/lazy")
    assert result.status == "ok"
    assert result.message == "blob fetch was refused; texts were read on demand"
    assert list(texts["text"]) == ["# still here\n"]


def test_merge_shards_is_stable_and_splits_text_row_groups(tmp_path: Path) -> None:
    """Merge row counts add up. A second merge writes the same rows. Texts use groups of 1000."""
    origin = tmp_path / "origin"
    _build_renamed_skill(origin)
    result, changes, texts = _mine(origin, tmp_path / "mine", repo="acme/pdf")
    shard_dir = tmp_path / "shards"
    write_repo_shard(shard_dir, "acme/pdf", changes, texts, result)
    extra_texts = pd.DataFrame(
        {
            "sha": [f"{index:040x}" for index in range(1001)] + [texts.iloc[0]["sha"]],
            "text": ["y"] * 1001 + [texts.iloc[0]["text"]],
        }
    )
    extra_changes = pd.DataFrame(
        [
            {
                "repo": "acme/extra",
                "skill_folder": "skills/extra",
                "file_path": "SKILL.md",
                "commit": "a" * 40,
                "commit_date": "2026-06-01T00:00:00+00:00",
                "author_key": hash_author_email(_EMAIL),
                "subject": "add",
                "status": "A",
                "old_file_path": "",
                "old_sha": _ZERO,
                "new_sha": "b" * 40,
                "old_mode": "000000",
                "new_mode": "100644",
                "is_skill_md": True,
                "is_script": False,
                "commit_seq": 1,
            }
        ]
    )
    extra_result = RepoResult("acme/extra", "ok", "", "c" * 40, 1.5, True)
    write_repo_shard(shard_dir, "acme/extra", extra_changes, extra_texts, extra_result)
    # A half-written repo has no status file, so merge skips it.
    (shard_dir / "acme__half.changes.parquet").write_bytes(b"not a real shard")
    out = tmp_path / "out"
    counts = merge_shards(
        shard_dir,
        out / "changes.parquet",
        out / "texts.parquet",
        out / "status.parquet",
    )
    assert counts == {
        "changes": len(changes) + 1,
        "texts": len(texts) + 1001,
        "status": 2,
    }
    merged_changes = pd.read_parquet(out / "changes.parquet")
    merged_texts = pd.read_parquet(out / "texts.parquet")
    merged_status = pd.read_parquet(out / "status.parquet")
    assert set(merged_status["repo"]) == {"acme/pdf", "acme/extra"}
    assert len(merged_changes) == counts["changes"]
    assert len(merged_texts) == counts["texts"]
    assert merged_texts["sha"].is_unique
    text_file = pq.ParquetFile(out / "texts.parquet")
    # acme/extra is 1,002 texts (1,000 + 2), including one sha also in acme/pdf.
    # The shared sha is written once, so acme/pdf adds len(texts) - 1 rows.
    assert text_file.num_row_groups == 3
    assert text_file.metadata.row_group(0).num_rows == 1000
    assert text_file.metadata.row_group(1).num_rows == 2
    assert text_file.metadata.row_group(2).num_rows == len(texts) - 1
    first = {
        "changes": merged_changes,
        "texts": merged_texts,
        "status": merged_status,
    }
    again = merge_shards(
        shard_dir,
        out / "changes.parquet",
        out / "texts.parquet",
        out / "status.parquet",
    )
    assert again == counts
    second_changes = pd.read_parquet(out / "changes.parquet")
    second_texts = pd.read_parquet(out / "texts.parquet")
    second_status = pd.read_parquet(out / "status.parquet")
    pd.testing.assert_frame_equal(first["changes"], second_changes)
    pd.testing.assert_frame_equal(first["texts"], second_texts)
    pd.testing.assert_frame_equal(first["status"], second_status)
    status_json = json.loads((shard_dir / "acme__pdf.status.json").read_text(encoding="utf-8"))
    assert status_json["status"] == "ok"
    assert not (shard_dir / "acme__pdf.status.tmp").exists()


def test_run_git_raises_on_failure(tmp_path: Path) -> None:
    """A non-zero git command raises GitError. --version returns text."""
    with pytest.raises(GitError):
        run_git(["rev-parse", "--verify", "HEAD"], tmp_path, 30)
    assert "git" in run_git(["--version"], None, 30).lower()
