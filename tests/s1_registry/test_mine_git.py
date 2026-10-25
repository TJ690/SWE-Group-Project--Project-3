"""Git history mining: SKILL.md versions, release subjects, and AI co-author trailers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pandas as pd
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import manifest_path
from skillsig.s1_registry.mine_git import clone_all, mine_repo, mine_trailers, run
from skillsig.s1_registry.sources import Source, clone_dir

_VERSION_COLUMNS = [
    "repo",
    "commit",
    "commit_date",
    "subject",
    "path",
    "skill",
    "blob",
    "status",
    "release_label",
]
_REPO = "acme/widget"


def test_add_modify_delete_rows_and_statuses(mined: dict[str, object]) -> None:
    df = _versions(mined)
    assert list(df.columns) == _VERSION_COLUMNS
    assert set(df["commit"]) == {mined["added"], mined["modified"]}
    added = _row(df, str(mined["added"]))
    modified = _row(df, str(mined["modified"]))
    assert added["status"] == "A"
    assert modified["status"] == "M"
    assert set(df["status"]) == {"A", "M"}
    assert added["path"] == "skills/widget/SKILL.md"
    assert added["skill"] == "widget"
    assert added["blob"] == _blob(mined["path"], mined["added"], "skills/widget/SKILL.md")
    assert modified["blob"] == _blob(mined["path"], mined["modified"], "skills/widget/SKILL.md")
    assert added["blob"] != modified["blob"]
    assert len(str(added["commit"])) == 40
    assert len(str(added["blob"])) == 40


def test_release_subject_parsed_to_7_0_0(mined: dict[str, object]) -> None:
    df = _versions(mined)
    added = _row(df, str(mined["added"]))
    modified = _row(df, str(mined["modified"]))
    assert added["subject"] == "Release v7.0.0 | widget"
    assert added["release_label"] == "7.0.0"
    assert pd.isna(modified["release_label"])


def test_claude_coauthor_trailer(mined: dict[str, object]) -> None:
    trailers = _trailers(mined)
    added = _row(trailers, str(mined["added"]))
    modified = _row(trailers, str(mined["modified"]))
    deleted = _row(trailers, str(mined["deleted"]))
    assert bool(added["has_ai_trailer"]) is True
    assert list(added["ai_tools"]) == ["claude"]
    assert bool(modified["has_ai_trailer"]) is False
    assert list(modified["ai_tools"]) == []
    assert bool(deleted["has_ai_trailer"]) is False
    assert len(str(added["commit"])) == 40


def test_root_skill_and_release_without_v(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "SKILL.md").write_text("root\n", encoding="utf-8")
    nested = repo / "nested" / "SKILL.md"
    nested.parent.mkdir()
    nested.write_text("nested\n", encoding="utf-8")
    _commit(repo, "Release 7.0.0")
    df = mine_repo(repo, "acme/skills")
    assert set(zip(df["path"], df["skill"], strict=True)) == {
        ("SKILL.md", "skills"),
        ("nested/SKILL.md", "nested"),
    }
    assert set(df["release_label"]) == {"7.0.0"}
    assert set(df["status"]) == {"A"}


def test_quoted_skill_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    cafe = repo / "café" / "SKILL.md"
    quoted = repo / 'a"b' / "SKILL.md"
    cafe.parent.mkdir()
    quoted.parent.mkdir()
    cafe.write_text("u\n", encoding="utf-8")
    quoted.write_text("q\n", encoding="utf-8")
    _commit(repo, "Add quoted paths")
    df = mine_repo(repo, "acme/skills")
    assert set(df["path"]) == {"café/SKILL.md", 'a"b/SKILL.md'}
    assert set(df["skill"]) == {"café", 'a"b'}


def test_human_coauthor_and_short_version_are_not_signals(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "SKILL.md").write_text("v\n", encoding="utf-8")
    sha = _commit(repo, "Release v7.0\n\nCo-authored-by: Ada Lovelace <ada@example.com>")
    versions = mine_repo(repo, "acme/skills")
    trailers = mine_trailers(repo, "acme/skills")
    assert pd.isna(_row(versions, sha)["release_label"])
    assert bool(_row(trailers, sha)["has_ai_trailer"]) is False
    assert list(_row(trailers, sha)["ai_tools"]) == []


def test_repo_without_skill_md_has_columns_and_trailer_row(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _commit(repo, "init")
    versions = mine_repo(repo, "acme/skills")
    trailers = mine_trailers(repo, "acme/skills")
    assert versions.empty
    assert list(versions.columns) == _VERSION_COLUMNS
    assert len(trailers) == 1
    assert bool(trailers.iloc[0]["has_ai_trailer"]) is False
    assert list(trailers.iloc[0]["ai_tools"]) == []


def test_missing_clone_names_the_registry_command(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="registry --only mine"):
        mine_repo(tmp_path / "missing", "acme/skills")


def test_clone_all_refuses_non_git_dest(tmp_cfg: Config) -> None:
    source = _source("acme/skills", None)
    dest = clone_dir(tmp_cfg, source)
    dest.mkdir(parents=True)
    (dest / "junk").write_text("x", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="not a git repo"):
        clone_all(tmp_cfg, [source])


def test_clone_all_clones_then_fetches_and_skips_mirrors(
    tmp_cfg: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    origin = tmp_path / "origin"
    _init_repo(origin)
    (origin / "SKILL.md").write_text("one\n", encoding="utf-8")
    _commit(origin, "Add skill")
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(origin), str(bare)], check=True, capture_output=True
    )
    _git(bare, "config", "uploadpack.allowFilter", "true")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    monkeypatch.setattr(
        "skillsig.s1_registry.mine_git._remote_url", lambda source: bare.as_uri()
    )
    source = _source("acme/skills", None)
    mirror = _source("acme/mirror", "acme/skills")
    first = clone_all(tmp_cfg, [source, mirror])
    assert first == [clone_dir(tmp_cfg, source)]
    assert not clone_dir(tmp_cfg, mirror).exists()
    assert not (first[0] / "SKILL.md").exists()
    assert len(mine_repo(first[0], "acme/skills")) == 1

    (origin / "SKILL.md").write_text("two\n", encoding="utf-8")
    _commit(origin, "Edit skill")
    _git(origin, "remote", "add", "upstream", bare.as_uri())
    _git(origin, "push", "upstream", "HEAD:main")
    second = clone_all(tmp_cfg, [source, mirror])
    assert second == first
    mined = mine_repo(second[0], "acme/skills")
    assert set(mined["status"]) == {"A", "M"}
    assert len(mined) == 2


def test_run_copies_mirror_rows_and_skips_when_unchanged(
    tmp_cfg: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_sources(
        tmp_cfg.root,
        """
sources:
  - {repo: acme/skills, tier: A, group: acme, status: mined}
  - {repo: acme/mirror, tier: A, group: acme, status: mined, mirror_of: acme/skills}
""",
    )
    repo = tmp_path / "clone"
    shas = _skill_history(repo)
    monkeypatch.setattr(
        "skillsig.s1_registry.mine_git.clone_all", lambda cfg, sources: [repo]
    )
    result = run(tmp_cfg, dev=True, force=True)
    versions_path = resolve(tmp_cfg, tmp_cfg.paths.derived) / "s1_versions_raw.parquet"
    trailers_path = resolve(tmp_cfg, tmp_cfg.paths.derived) / "s1_commit_trailers.parquet"
    versions = pd.read_parquet(versions_path)
    trailers = pd.read_parquet(trailers_path)
    assert not result.skipped
    assert set(versions["repo"]) == {"acme/skills", "acme/mirror"}
    origin = versions[versions["repo"] == "acme/skills"]
    mirror = versions[versions["repo"] == "acme/mirror"]
    assert list(origin["commit"]) == list(mirror["commit"])
    assert set(origin["release_label"].dropna()) == {"7.0.0"}
    assert bool(origin["release_label"].isna().any())
    assert result.rows == {
        "s1_versions_raw": len(versions),
        "s1_commit_trailers": len(trailers),
    }
    claude = trailers[trailers["commit"] == shas["added"]]
    assert set(claude["repo"]) == {"acme/skills", "acme/mirror"}
    assert bool(claude["has_ai_trailer"].all())
    assert all(list(tools) == ["claude"] for tools in claude["ai_tools"])
    assert manifest_path(versions_path).is_file()
    assert manifest_path(trailers_path).is_file()
    again = run(tmp_cfg, dev=True)
    assert again.skipped
    assert again.rows["s1_versions_raw"] == result.rows["s1_versions_raw"]


@pytest.fixture
def mined(tmp_path: Path) -> dict[str, object]:
    path = tmp_path / "repo"
    shas = _skill_history(path)
    return {
        "path": path,
        "repo": _REPO,
        **shas,
        "versions": mine_repo(path, _REPO),
        "trailers": mine_trailers(path, _REPO),
    }


def _versions(mined: dict[str, object]) -> pd.DataFrame:
    frame = mined["versions"]
    assert isinstance(frame, pd.DataFrame)
    return frame


def _trailers(mined: dict[str, object]) -> pd.DataFrame:
    frame = mined["trailers"]
    assert isinstance(frame, pd.DataFrame)
    return frame


def _row(frame: pd.DataFrame, commit: str) -> pd.Series:
    return frame.loc[frame["commit"] == commit].iloc[0]


def _skill_history(path: Path) -> dict[str, str]:
    _init_repo(path)
    skill = path / "skills" / "widget" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("version one\n", encoding="utf-8")
    added = _commit(
        path,
        "Release v7.0.0 | widget\n\nCo-authored-by: Claude <noreply@anthropic.com>",
    )
    skill.write_text("version two\n", encoding="utf-8")
    modified = _commit(path, "Revise the skill text")
    skill.unlink()
    deleted = _commit(path, "Remove the skill")
    return {"added": added, "modified": modified, "deleted": deleted}


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "dev@example.com")
    _git(path, "config", "user.name", "Dev")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "config", "core.autocrlf", "false")


def _commit(path: Path, message: str) -> str:
    _git(path, "add", "-A")
    _git(path, "commit", "-m", message)
    return _git(path, "rev-parse", "HEAD").strip()


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout


def _blob(path: object, commit: object, rel: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", f"{commit}:{rel}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _source(repo: str, mirror_of: str | None) -> Source:
    return Source(repo=repo, tier="A", group="acme", status="mined", mirror_of=mirror_of, note=None)


def _write_sources(root: Path, text: str) -> None:
    path = root / "config" / "registry_sources.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")
