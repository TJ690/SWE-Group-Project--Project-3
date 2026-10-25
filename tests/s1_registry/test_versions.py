"""Registry versions: one row per blob, sequence, idioms, and the snapshot flag."""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import manifest_path
from skillsig.s1_registry.sources import Source, clone_dir
from skillsig.s1_registry.versions import build_versions, description_idiom, run

_REPO = "acme/skills"
_SNAPSHOT = date(2026, 7, 20)
_RAW = [
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
_OUT = [
    "lineage_id",
    "repo",
    "skill",
    "blob",
    "version_seq",
    "first_commit",
    "first_date",
    "last_date",
    "n_commits",
    "subject",
    "release_label",
    "size_bytes",
    "ai_trailer_at_introduction",
    "desc_idiom",
    "post_snapshot",
    "renamed_group",
]


@pytest.mark.parametrize(
    ("desc", "idiom"),
    [
        ("Use this skill whenever the user wants to...", "use_this_skill_whenever"),
        ("Use when the user asks for a code review.", "use_when"),
        ("Guide for creating effective skills.", "guide_for"),
        (
            "Create distinctive, production-grade frontend interfaces. "
            "Use this skill when the user asks to build web components.",
            "capability_summary",
        ),
        ("Notes from the planning meeting.", "other"),
    ],
)
def test_description_idiom_samples(desc: str, idiom: str) -> None:
    assert description_idiom(desc) == idiom


def test_description_idiom_related_openings() -> None:
    assert description_idiom("Use this skill when the user wants a spreadsheet.") == "use_when"
    assert description_idiom("This skill should be used when the user asks to create a hook.") == (
        "use_when"
    )
    assert description_idiom("A comprehensive guide for creating diagrams.") == "guide_for"
    assert description_idiom(None) == "other"
    assert description_idiom("   ") == "other"


def test_same_blob_two_commits_is_one_row() -> None:
    early, late = _sha(9), _sha(1)
    blob = _sha(50)
    frame = _build(
        [
            _hit(early, "2026-01-02", blob, subject="Release v1.2.3", release="1.2.3"),
            _hit(late, "2026-06-01", blob, subject="touch again", release="9.9.9"),
        ]
    )
    assert list(frame.columns) == _OUT
    assert len(frame) == 1
    row = frame.iloc[0]
    assert int(row["n_commits"]) == 2
    assert row["first_commit"] == early
    assert row["first_date"] == "2026-01-02"
    assert row["last_date"] == "2026-06-01"
    assert row["subject"] == "Release v1.2.3"
    assert row["release_label"] == "1.2.3"
    assert row["lineage_id"] == f"{_REPO}::demo"
    assert int(row["version_seq"]) == 1
    assert bool(row["ai_trailer_at_introduction"]) is False
    assert pd.isna(row["size_bytes"])
    assert row["desc_idiom"] == "other"
    assert pd.isna(row["renamed_group"])


def test_version_seq_increases_with_date() -> None:
    frame = _build(
        [
            _hit(_sha(3), "2026-03-01", _sha(30), skill="alpha"),
            _hit(_sha(1), "2026-01-15", _sha(10), skill="alpha"),
            _hit(_sha(2), "2026-02-01", _sha(20), skill="alpha"),
            _hit(_sha(4), "2026-01-01", _sha(40), skill="beta"),
        ]
    )
    alpha = frame[frame["skill"] == "alpha"].sort_values("version_seq")
    dates = list(alpha["first_date"])
    assert dates == sorted(dates)
    assert list(alpha["version_seq"].astype(int)) == [1, 2, 3]
    assert list(alpha["first_date"]) == ["2026-01-15", "2026-02-01", "2026-03-01"]
    beta = frame[frame["skill"] == "beta"]
    assert list(beta["version_seq"].astype(int)) == [1]


def test_same_day_orders_by_commit_and_trailer_is_any_that_day() -> None:
    blob = _sha(70)
    frame = _build(
        [
            _hit(_sha(2), "2026-01-01", blob, subject="later sha"),
            _hit(_sha(1), "2026-01-01", blob, subject="earlier sha"),
            _hit(_sha(8), "2026-02-01", _sha(80)),
            _hit(_sha(9), "2026-06-01", _sha(80)),
        ],
        trailers=[(_sha(2), True), (_sha(9), True)],
    )
    intro = frame[frame["blob"] == blob].iloc[0]
    later = frame[frame["blob"] == _sha(80)].iloc[0]
    assert intro["first_commit"] == _sha(1)
    assert intro["subject"] == "earlier sha"
    assert bool(intro["ai_trailer_at_introduction"]) is True
    assert bool(later["ai_trailer_at_introduction"]) is False
    assert int(intro["version_seq"]) < int(later["version_seq"])


def test_post_snapshot_when_first_date_is_after_snapshot() -> None:
    frame = _build(
        [
            _hit(_sha(1), "2026-07-19", _sha(10)),
            _hit(_sha(2), "2026-07-20", _sha(20)),
            _hit(_sha(3), "2026-07-21", _sha(30)),
        ]
    )
    flags = dict(zip(frame["first_date"], frame["post_snapshot"], strict=True))
    assert flags == {"2026-07-19": False, "2026-07-20": False, "2026-07-21": True}


def test_renamed_group_is_shared_inside_one_repo() -> None:
    shared = _sha(90)
    frame = _build(
        [
            _hit(_sha(1), "2026-01-01", shared, skill="old-name"),
            _hit(_sha(2), "2026-02-01", shared, skill="new-name"),
            _hit(_sha(3), "2026-03-01", _sha(91), skill="new-name"),
            _hit(_sha(4), "2026-01-01", shared, skill="old-name", repo="other/repo"),
        ]
    )
    group = f"{_REPO}::{shared}"
    renamed = frame[frame["blob"] == shared]
    inside = renamed[renamed["repo"] == _REPO]
    assert set(inside["skill"]) == {"old-name", "new-name"}
    assert set(inside["renamed_group"]) == {group}
    assert int(inside[inside["skill"] == "old-name"]["version_seq"].iloc[0]) == 1
    solo = frame[frame["blob"] == _sha(91)].iloc[0]
    assert pd.isna(solo["renamed_group"])
    outside = renamed[renamed["repo"] == "other/repo"].iloc[0]
    assert pd.isna(outside["renamed_group"])


def test_null_commit_fields_are_dropped() -> None:
    frame = _build(
        [
            _hit(_sha(1), "2026-01-01", _sha(10)),
            _hit(None, "2026-02-01", _sha(11)),
        ]
    )
    assert len(frame) == 1
    assert frame.iloc[0]["blob"] == _sha(10)


def test_missing_raw_names_the_mine_command(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match="registry --only mine"):
        run(tmp_cfg)


def test_run_fills_size_and_idiom_from_the_clone(tmp_cfg: Config) -> None:
    origin = Source(_REPO, "A", "acme", "mined", None, None)
    _write_sources(tmp_cfg.root)
    clone = clone_dir(tmp_cfg, origin)
    good, bad = _commit_skills(clone)
    _write_inputs(tmp_cfg, good, bad)
    _write_mining_run(tmp_cfg)

    result = run(tmp_cfg, dev=True, force=True)
    path = resolve(tmp_cfg, tmp_cfg.paths.derived) / "s1_registry_versions.parquet"
    frame = pd.read_parquet(path)
    assert not result.skipped
    assert result.rows == {"s1_registry_versions": len(frame)}
    assert manifest_path(path).is_file()
    assert set(frame["repo"]) == {_REPO, "acme/mirror"}

    origin_rows = frame[frame["repo"] == _REPO].set_index("skill")
    good_row = origin_rows.loc["good"]
    bad_row = origin_rows.loc["bad"]
    assert int(good_row["size_bytes"]) == good["size"]
    assert good_row["desc_idiom"] == "use_this_skill_whenever"
    assert bool(good_row["post_snapshot"]) is True
    assert bool(good_row["ai_trailer_at_introduction"]) is True
    assert int(bad_row["size_bytes"]) == bad["size"]
    assert bad_row["desc_idiom"] == "other"
    mirror = frame[(frame["repo"] == "acme/mirror") & (frame["skill"] == "good")].iloc[0]
    assert int(mirror["size_bytes"]) == good["size"]
    assert mirror["desc_idiom"] == "use_this_skill_whenever"

    again = run(tmp_cfg)
    assert again.skipped
    assert again.rows["s1_registry_versions"] == result.rows["s1_registry_versions"]


def _build(
    rows: list[dict[str, object]],
    trailers: list[tuple[str, bool]] | None = None,
    snapshot: date = _SNAPSHOT,
) -> pd.DataFrame:
    raw = pd.DataFrame(rows, columns=_RAW)
    flagged = trailers or []
    trailer_frame = pd.DataFrame(
        {
            "repo": [_REPO] * len(flagged),
            "commit": [commit for commit, _flag in flagged],
            "ai_tools": [["claude"] if flag else [] for _commit, flag in flagged],
            "has_ai_trailer": [flag for _commit, flag in flagged],
        }
    )
    return build_versions(raw, trailer_frame, snapshot)


def _hit(
    commit: str | None,
    day: str,
    blob: str,
    *,
    skill: str = "demo",
    subject: str = "Add skill",
    release: str | None = None,
    repo: str = _REPO,
) -> dict[str, object]:
    return {
        "repo": repo,
        "commit": commit,
        "commit_date": day,
        "subject": subject,
        "path": f"skills/{skill}/SKILL.md",
        "skill": skill,
        "blob": blob,
        "status": "A",
        "release_label": release,
    }


def _sha(n: int) -> str:
    return f"{n:040x}"


def _write_sources(root: Path) -> None:
    path = root / "config" / "registry_sources.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "sources:",
                "  - {repo: acme/skills, tier: A, group: acme, status: mined}",
                "  - {repo: acme/mirror, tier: A, group: acme, status: mined, "
                "mirror_of: acme/skills}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _commit_skills(clone: Path) -> tuple[dict[str, object], dict[str, object]]:
    _init_repo(clone)
    good_text = (
        "---\n"
        "name: good\n"
        "description: Use this skill whenever the user wants to test widgets.\n"
        "---\n\n"
        "Body.\n"
    )
    bad_text = "---\n: :\n---\n\nnot yaml\n"
    good_path = clone / "skills" / "good" / "SKILL.md"
    bad_path = clone / "skills" / "bad" / "SKILL.md"
    good_path.parent.mkdir(parents=True)
    bad_path.parent.mkdir(parents=True)
    good_path.write_text(good_text, encoding="utf-8")
    bad_path.write_text(bad_text, encoding="utf-8")
    commit = _commit(clone, "Add skills")
    good_blob = _blob(clone, commit, "skills/good/SKILL.md")
    bad_blob = _blob(clone, commit, "skills/bad/SKILL.md")
    return (
        {"commit": commit, "blob": good_blob, "size": len(good_text.encode())},
        {"commit": commit, "blob": bad_blob, "size": len(bad_text.encode())},
    )


def _write_inputs(cfg: Config, good: dict[str, object], bad: dict[str, object]) -> None:
    derived = resolve(cfg, cfg.paths.derived)
    derived.mkdir(parents=True, exist_ok=True)
    rows = []
    for repo in (_REPO, "acme/mirror"):
        for skill, item in (("good", good), ("bad", bad)):
            if repo == "acme/mirror" and skill == "bad":
                continue
            rows.append(
                _hit(
                    str(item["commit"]),
                    "2026-08-01",
                    str(item["blob"]),
                    skill=skill,
                    repo=repo,
                )
            )
    pd.DataFrame(rows, columns=_RAW).to_parquet(derived / "s1_versions_raw.parquet", index=False)
    pd.DataFrame(
        {
            "repo": [_REPO, "acme/mirror"],
            "commit": [str(good["commit"]), str(good["commit"])],
            "ai_tools": [["claude"], ["claude"]],
            "has_ai_trailer": [True, True],
        }
    ).to_parquet(derived / "s1_commit_trailers.parquet", index=False)


def _write_mining_run(cfg: Config) -> None:
    directory = resolve(cfg, cfg.paths.raw) / "mining_runs"
    directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "run_id": ["r"],
            "artifact_type": ["skill"],
            "query": ["q"],
            "started_at": ["2026-07-01T00:00:00"],
            "finished_at": ["2026-07-20T22:26:56"],
            "discovered": [1],
            "note": ["t"],
        }
    ).to_parquet(directory / "runs.parquet", index=False)


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


def _blob(repo: Path, commit: str, rel: str) -> str:
    return _git(repo, "rev-parse", f"{commit}:{rel}").strip()
