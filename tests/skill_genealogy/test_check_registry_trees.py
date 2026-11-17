"""Tests for stage 2b: SKILL.md counts from a repo's own tree (bug B01).

Throw-away repos are built with git init. Clones use a file:// URL, so no
network is used.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pandas as pd

from skill_genealogy.check_registry_trees import (
    check_registry_trees,
    count_repo_skill_md,
    repos_still_to_check,
    summarize_tree_counts,
    tree_check_candidates,
)

_EMAIL = "tester@example.com"


def _git(cwd: Path, args: list[str]) -> None:
    """Run git with a fixed author and raise AssertionError on failure."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Tester",
            "GIT_AUTHOR_EMAIL": _EMAIL,
            "GIT_COMMITTER_NAME": "Tester",
            "GIT_COMMITTER_EMAIL": _EMAIL,
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    completed = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)


def _skill_repo(path: Path, skills: int) -> Path:
    """A repo with ``skills`` SKILL.md files, one lowercase, one under node_modules, two commits."""
    path.mkdir(parents=True)
    _git(path, ["init", "-b", "main"])
    _git(path, ["config", "uploadpack.allowFilter", "true"])
    (path / "README.md").write_text("first\n")
    _git(path, ["add", "."])
    _git(path, ["commit", "-m", "first"])
    for index in range(skills):
        folder = path / "skills" / f"s{index}"
        folder.mkdir(parents=True)
        name = "skill.md" if index == 0 else "SKILL.md"
        (folder / name).write_text(f"# s{index}\n")
    hidden = path / "node_modules" / "pkg"
    hidden.mkdir(parents=True)
    (hidden / "SKILL.md").write_text("# not a skill\n")
    _git(path, ["add", "."])
    _git(path, ["commit", "-m", "add skills"])
    return path


def test_candidates_need_stars_one_to_four_skills_and_no_fork() -> None:
    """Most stars first. Five dataset skills, zero skills, forks and small repos are left out."""
    repos = pd.DataFrame(
        {
            "repo": [
                "obra/superpowers",
                "anthropics/skills",
                "x/five",
                "x/zero",
                "x/fork",
                "x/low",
            ],
            "stars": [255_386, 163_055, 900, 900, 900, 99],
            "is_fork": [0, 0, 0, 0, 1, 0],
            "english_skills": [1, 3, 5, 0, 2, 2],
        }
    )
    assert tree_check_candidates(repos) == ["obra/superpowers", "anthropics/skills"]


def test_count_repo_skill_md_counts_head_tree_and_cleans_up(tmp_path: Path) -> None:
    """Six SKILL.md files in any case are counted. node_modules is skipped. No clone is left."""
    origin = _skill_repo(tmp_path / "origin", skills=6)
    work = tmp_path / "work"
    row = count_repo_skill_md("acme/skills", work, 60, url=origin.as_uri())
    assert row["status"] == "ok"
    assert row["tree_skill_md"] == 6
    assert isinstance(row["head_commit"], str) and len(row["head_commit"]) == 40
    assert list(work.iterdir()) == []


def test_count_repo_skill_md_reports_a_failed_clone(tmp_path: Path) -> None:
    """A clone that fails is not ok and has no count."""
    missing = (tmp_path / "missing").as_uri()
    row = count_repo_skill_md("acme/missing", tmp_path / "work", 60, url=missing)
    assert row["status"] in {"gone", "error"}
    assert row["tree_skill_md"] is None
    assert row["message"]


def test_check_registry_trees_saves_and_resumes(tmp_path: Path) -> None:
    """The table holds one row per repo. A second run has nothing left to check."""
    big = _skill_repo(tmp_path / "big", skills=7)
    small = _skill_repo(tmp_path / "small", skills=2)
    out = tmp_path / "data" / "registry_tree_counts.parquet"
    urls = {"acme/big": big.as_uri(), "acme/small": small.as_uri()}
    table = check_registry_trees(["acme/big", "acme/small"], out, workers=2, urls=urls)
    assert out.is_file()
    counts = dict(zip(table["repo"], table["tree_skill_md"], strict=True))
    assert counts == {"acme/big": 7, "acme/small": 2}
    assert not (tmp_path / "data" / "tree_check_tmp").exists()
    saved = pd.read_parquet(out)
    assert repos_still_to_check(["acme/big", "acme/small"], saved, retry_failed=True) == []
    numbers = summarize_tree_counts(saved, ["acme/big", "acme/small", "acme/later"])
    assert numbers["promoted"] == 1
    assert numbers["rejected"] == 1
    assert numbers["not_checked"] == 1
    assert numbers["status_ok"] == 2
