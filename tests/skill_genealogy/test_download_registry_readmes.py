"""Tests for stage 3b, the registry README download.

Throw-away repos are built with git init. Clones use a file:// URL.
No network and no dataset files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
from tests.skill_genealogy.test_git_history import _commit, _git, _init_repo

from skill_genealogy.download_registry_readmes import (
    README_COLUMNS,
    download_registry_readmes,
    fetch_repo_readme,
    repos_still_to_fetch,
    summarize_readmes,
)


def _repo_with_two_readmes(origin: Path) -> tuple[str, str]:
    """README.md says ``first`` at the first commit and ``second`` at HEAD. Return both commits."""
    _init_repo(origin)
    # GitHub serves any reachable commit by id; a local origin needs this setting.
    _git(origin, ["config", "uploadpack.allowAnySHA1InWant", "true"])
    (origin / "skills" / "a").mkdir(parents=True)
    (origin / "skills" / "a" / "SKILL.md").write_text("# a\n")
    (origin / "README.md").write_text("# first\n")
    _commit(origin, "first", "2026-01-01T00:00:00+00:00")
    first = _head(origin)
    (origin / "README.md").write_text("# second\n")
    _commit(origin, "second", "2026-02-01T00:00:00+00:00")
    return first, _head(origin)


def _head(origin: Path) -> str:
    """The commit id at HEAD of ``origin``."""
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=origin, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def test_readme_is_read_at_the_stage3_commit(tmp_path: Path) -> None:
    """An older stage 3 commit gives that commit's README, not the newest one."""
    origin = tmp_path / "origin"
    first, _second = _repo_with_two_readmes(origin)
    row = fetch_repo_readme("acme/a", first, tmp_path / "work", 60, url=origin.as_uri())
    assert row["status"] == "ok"
    assert row["commit"] == first
    assert row["commit_basis"] == "stage3_head"
    assert row["readme_path"] == "README.md"
    assert row["readme_text"] == "# first\n"
    assert row["readme_bytes"] == len(b"# first\n")
    assert set(row) == set(README_COLUMNS)
    assert not any((tmp_path / "work").iterdir())


def test_missing_commit_falls_back_to_the_newest_commit(tmp_path: Path) -> None:
    """A stage 3 commit the remote no longer has uses HEAD and says so in the message."""
    origin = tmp_path / "origin"
    _first, second = _repo_with_two_readmes(origin)
    row = fetch_repo_readme("acme/a", "f" * 40, tmp_path / "work", 60, url=origin.as_uri())
    assert row["status"] == "ok"
    assert row["commit"] == second
    assert row["commit_basis"] == "current_head"
    assert "stage 3 commit not fetched" in str(row["message"])
    assert row["readme_text"] == "# second\n"


def test_github_folder_readme_wins_and_no_readme_is_a_status(tmp_path: Path) -> None:
    """``.github/README.md`` is chosen over the root one; a repo with none is ``no_readme``."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    (origin / ".github").mkdir()
    (origin / ".github" / "README.md").write_text("# shown on github\n")
    (origin / "README.md").write_text("# root\n")
    _commit(origin, "add", "2026-03-01T00:00:00+00:00")
    row = fetch_repo_readme("acme/gh", None, tmp_path / "work", 60, url=origin.as_uri())
    assert row["readme_path"] == ".github/README.md"
    assert row["readme_text"] == "# shown on github\n"
    assert row["commit_basis"] == "current_head"

    bare = tmp_path / "bare"
    _init_repo(bare)
    (bare / "SKILL.md").write_text("# s\n")
    _commit(bare, "add", "2026-03-01T00:00:00+00:00")
    none = fetch_repo_readme("acme/none", None, tmp_path / "work", 60, url=bare.as_uri())
    assert none["status"] == "no_readme"
    assert none["readme_text"] is None


def test_missing_repo_is_a_status_not_a_crash(tmp_path: Path) -> None:
    """A clone URL that does not exist gives a failed status row."""
    row = fetch_repo_readme(
        "acme/gone", None, tmp_path / "work", 60, url=(tmp_path / "nope").as_uri()
    )
    assert row["status"] in {"gone", "error"}
    assert row["readme_text"] is None


def test_download_saves_rows_and_a_rerun_skips_them(tmp_path: Path) -> None:
    """Two repos give two saved rows; finished rows are not fetched again."""
    one = tmp_path / "one"
    first, _second = _repo_with_two_readmes(one)
    two = tmp_path / "two"
    _init_repo(two)
    (two / "SKILL.md").write_text("# s\n")
    _commit(two, "add", "2026-03-01T00:00:00+00:00")
    out = tmp_path / "data" / "registry_readmes.parquet"
    urls = {"acme/one": one.as_uri(), "acme/two": two.as_uri()}
    table = download_registry_readmes(
        ["acme/one", "acme/two"],
        {"acme/one": first, "acme/two": None},
        out,
        workers=2,
        timeout_seconds=60,
        urls=urls,
    )
    assert list(table["repo"]) == ["acme/one", "acme/two"]
    assert list(table["status"]) == ["ok", "no_readme"]
    assert pq.read_table(out).num_rows == 2
    assert not (out.parent / "readme_tmp").exists()
    assert repos_still_to_fetch(["acme/one", "acme/two"], table, retry_failed=True) == []
    numbers = summarize_readmes(table, ["acme/one", "acme/two", "acme/three"])
    assert numbers["status_ok"] == 1
    assert numbers["status_no_readme"] == 1
    assert numbers["basis_stage3_head"] == 1
    assert numbers["not_fetched"] == 1


def test_a_new_row_replaces_the_old_one(tmp_path: Path) -> None:
    """Fetching a repo again keeps one row for it."""
    origin = tmp_path / "origin"
    _first, second = _repo_with_two_readmes(origin)
    out = tmp_path / "registry_readmes.parquet"
    old = pd.DataFrame([{**dict.fromkeys(README_COLUMNS), "repo": "acme/a", "status": "error"}])
    table = download_registry_readmes(
        ["acme/a"], {"acme/a": second}, out, previous=old, timeout_seconds=60,
        urls={"acme/a": origin.as_uri()},
    )
    assert list(table["status"]) == ["ok"]
    assert table.iloc[0]["readme_text"] == "# second\n"
