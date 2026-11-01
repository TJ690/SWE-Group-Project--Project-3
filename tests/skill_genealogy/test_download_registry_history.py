"""Tests for the registry-history download.

Shard status is fake JSON. Clones use local file:// repos built with the
G05 git helpers. No network and no dataset files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from skill_genealogy.download_registry_history import (
    _STATUS_COLUMNS,
    _mine_repo,
    _print_report,
    duplicate_change_keys,
    mine_registries,
    repos_still_to_mine,
    save_outputs,
)
from skill_genealogy.git_history import RepoResult, write_repo_shard
from tests.skill_genealogy.test_git_history import _build_renamed_skill, _commit, _init_repo


def _write_status(shard_dir: Path, repo: str, status: str) -> None:
    """Write one shard status file, the same shape ``write_repo_shard`` uses."""
    shard_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "follow_renames": False,
        "head_commit": None,
        "message": "",
        "repo": repo,
        "seconds": 1.0,
        "status": status,
    }
    stem = repo.replace("/", "__")
    path = shard_dir / f"{stem}.status.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_repos_still_to_mine_skips_finished_and_retries_failures(tmp_path: Path) -> None:
    """ok, gone and empty stay done. timeout and error return only with retry."""
    shard_dir = tmp_path / "shards"
    repos = [
        "acme/new",
        "acme/timeout",
        "acme/ok",
        "acme/error",
        "acme/gone",
        "acme/empty",
    ]
    _write_status(shard_dir, "acme/timeout", "timeout")
    _write_status(shard_dir, "acme/ok", "ok")
    _write_status(shard_dir, "acme/error", "error")
    _write_status(shard_dir, "acme/gone", "gone")
    _write_status(shard_dir, "acme/empty", "empty")
    assert repos_still_to_mine(repos, shard_dir, False) == ["acme/new"]
    assert repos_still_to_mine(repos, shard_dir, True) == [
        "acme/new",
        "acme/timeout",
        "acme/error",
    ]
    assert repos_still_to_mine(["a/b"], tmp_path / "missing", False) == ["a/b"]


def test_repos_still_to_mine_ignores_corrupt_status_and_retries_unknown(tmp_path: Path) -> None:
    """A broken status file does not crash. An unknown status is mined again."""
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    (shard_dir / "broken.status.json").write_text("{", encoding="utf-8")
    _write_status(shard_dir, "acme/weird", "weird")
    assert repos_still_to_mine(["acme/plain", "acme/weird"], shard_dir, False) == [
        "acme/plain",
        "acme/weird",
    ]


def test_mine_registries_empty_list_has_status_columns(tmp_path: Path) -> None:
    """No repos returns an empty frame with the status columns."""
    frame = mine_registries([], tmp_path / "shards", tmp_path / "work", 2)
    assert frame.empty
    assert list(frame.columns) == list(_STATUS_COLUMNS)


def _build_notes_skill(origin: Path) -> None:
    """One skill folder with a single SKILL.md, different text from the pdf fixture."""
    _init_repo(origin)
    skill = origin / "skills" / "notes"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# notes\n")
    _commit(origin, "add notes", "2026-02-01T00:00:00+00:00")


def test_mine_registries_local_repos_then_merge(tmp_path: Path) -> None:
    """Two file:// repos write ok shards. The merge counts changes, texts and folders."""
    pdf = tmp_path / "pdf"
    notes = tmp_path / "notes"
    _build_renamed_skill(pdf)
    _build_notes_skill(notes)
    shard_dir = tmp_path / "shards"
    work_dir = tmp_path / "work"
    urls = {"acme/pdf": pdf.as_uri(), "acme/notes": notes.as_uri()}
    status = mine_registries(
        ["acme/pdf", "acme/notes"],
        shard_dir,
        work_dir,
        2,
        timeout_seconds=60,
        urls=urls,
    )
    assert list(status["repo"]) == ["acme/notes", "acme/pdf"]
    assert set(status["status"]) == {"ok"}
    pdf_row = status[status["repo"] == "acme/pdf"].iloc[0]
    notes_row = status[status["repo"] == "acme/notes"].iloc[0]
    assert int(pdf_row["changes"]) == 7
    assert int(pdf_row["skill_folders"]) == 1
    assert int(notes_row["changes"]) == 1
    assert int(notes_row["skill_folders"]) == 1
    assert (shard_dir / "acme__pdf.status.json").is_file()
    assert (shard_dir / "acme__notes.texts.parquet").is_file()
    assert list(work_dir.iterdir()) == []
    assert repos_still_to_mine(["acme/pdf", "acme/notes"], shard_dir, False) == []

    out_dir = tmp_path / "registry_history"
    changes, texts_path, merged, left_out = save_outputs(shard_dir, out_dir)
    assert len(changes) == 8
    assert int(changes["is_skill_md"].sum()) == 4
    assert int(merged["changes"].sum()) == 8
    assert int(merged["skill_folders"].sum()) == 2
    assert left_out == 0
    assert list(merged.columns) == list(_STATUS_COLUMNS)
    assert not changes["file_path"].str.contains("node_modules").any()
    texts = pq.read_table(texts_path)
    assert texts.num_rows == 4
    bodies = set(texts.column("text").to_pylist())
    assert bodies == {"# pdf\n\nhello\n", "# pdf\n\nhello world\n", "echo hi\n", "# notes\n"}


def test_duplicate_change_keys_counts_repeated_paths() -> None:
    """Two rows for one file in one commit count as one repeated key."""
    frame = pd.DataFrame(
        {
            "repo": ["a/b", "a/b", "a/b"],
            "skill_folder": ["skills/pdf", "skills/pdf", "skills/pdf"],
            "commit": ["c1", "c1", "c2"],
            "file_path": ["SKILL.md", "SKILL.md", "SKILL.md"],
        }
    )
    assert duplicate_change_keys(frame) == 1
    assert duplicate_change_keys(frame.iloc[0:0]) == 0


def test_duplicate_change_keys_warning(tmp_path: Path, capsys: object) -> None:
    """The summary names duplicate_change_keys and warns when it is above zero."""
    frame = pd.DataFrame(
        {
            "repo": ["a/b", "a/b"],
            "skill_folder": ["skills/pdf", "skills/pdf"],
            "commit": ["c1", "c1"],
            "file_path": ["SKILL.md", "SKILL.md"],
            "is_skill_md": [True, True],
            "is_script": [False, False],
            "new_sha": ["aa", "bb"],
        }
    )
    _print_report(
        frame,
        tmp_path / "missing.parquet",
        pd.DataFrame(columns=["repo", "status"]),
        elapsed=None,
        history_dir=tmp_path,
    )
    printed = capsys.readouterr().out
    assert "warning: duplicate_change_keys 1" in printed
    assert "duplicate_change_keys: 1" in printed


def _one_change(repo: str, sha: str, text: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One add of SKILL.md, plus its text row."""
    changes = pd.DataFrame(
        [
            {
                "repo": repo,
                "skill_folder": "skills/a",
                "file_path": "SKILL.md",
                "commit": "a" * 40,
                "commit_date": "2026-01-01T00:00:00+00:00",
                "author_key": "b" * 40,
                "subject": "add",
                "status": "A",
                "old_file_path": "",
                "old_sha": "0" * 40,
                "new_sha": sha,
                "old_mode": "000000",
                "new_mode": "100644",
                "is_skill_md": True,
                "is_script": False,
                "commit_seq": 1,
            }
        ]
    )
    texts = pd.DataFrame({"sha": [sha], "text": [text]})
    return changes, texts


def test_save_outputs_leaves_out_an_unlisted_shard(tmp_path: Path) -> None:
    """A shard whose repo is not on the list stays on disk and stays out of the merge."""
    shard_dir = tmp_path / "shards"
    keep_changes, keep_texts = _one_change("acme/keep", "c" * 40, "# keep\n")
    extra_changes, extra_texts = _one_change("acme/extra", "d" * 40, "# extra\n")
    keep_result = RepoResult("acme/keep", "ok", "", "e" * 40, 1.0, True)
    extra_result = RepoResult("acme/extra", "ok", "", "f" * 40, 1.0, True)
    write_repo_shard(shard_dir, "acme/keep", keep_changes, keep_texts, keep_result)
    write_repo_shard(shard_dir, "acme/extra", extra_changes, extra_texts, extra_result)
    changes, texts_path, status, left_out = save_outputs(
        shard_dir, tmp_path / "out", ["acme/keep"]
    )
    assert left_out == 1
    assert list(changes["repo"]) == ["acme/keep"]
    assert list(status["repo"]) == ["acme/keep"]
    assert set(pq.read_table(texts_path).column("text").to_pylist()) == {"# keep\n"}
    assert (shard_dir / "acme__extra.status.json").is_file()
    assert (shard_dir / "acme__extra.changes.parquet").is_file()


def test_disk_error_returns_an_error_row(tmp_path: Path, monkeypatch, capsys) -> None:
    """A failed shard write prints an error line and does not stop the caller."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    skill = origin / "skills" / "a"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# a\n")
    _commit(origin, "add", "2026-05-01T00:00:00+00:00")

    def boom(*_args, **_kwargs) -> None:
        raise OSError("no space left")

    monkeypatch.setattr("skill_genealogy.download_registry_history.write_repo_shard", boom)
    row = _mine_repo("acme/a", tmp_path / "shards", tmp_path / "work", 60, origin.as_uri())
    assert row["status"] == "error"
    assert row["changes"] == 0
    assert row["message"] == "no space left"
    assert "acme/a error" in capsys.readouterr().out
    assert not (tmp_path / "shards" / "acme__a.status.json").exists()
