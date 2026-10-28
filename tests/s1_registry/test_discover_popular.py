"""Popular skill families: owner rank, candidate origin, and registry membership."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import manifest_path
from skillsig.s1_registry.discover_popular import owner_repos, popular_candidates, run

_MIRROR = "a" * 40
_POPULAR = "b" * 40
_COLUMNS = [
    "file_sha",
    "name",
    "files",
    "repos",
    "owners",
    "candidate_repo",
    "candidate_stars",
    "candidate_first_commit_at",
    "in_registry",
]


def test_mirrored_file_count_ranks_below_owner_spread() -> None:
    artifacts = _mirror_copies() + _owner_copies()
    repos = [{"full_name": f"mirror{i}/pack", "stars": 1_000_000} for i in range(29)]
    repos += [{"full_name": f"user{i}/app", "stars": 1} for i in range(400)]
    ranked = popular_candidates(_connect(artifacts, repos, [_POPULAR]))
    assert list(ranked.columns) == _COLUMNS
    assert list(ranked["file_sha"]) == [_POPULAR, _MIRROR]
    assert int(ranked.iloc[0]["owners"]) == 400
    assert int(ranked.iloc[0]["files"]) == 400
    assert bool(ranked.iloc[0]["in_registry"]) is True
    mirror = ranked.iloc[1]
    assert int(mirror["files"]) == 1074
    assert int(mirror["repos"]) == 29
    assert int(mirror["owners"]) == 29
    assert bool(mirror["in_registry"]) is False
    assert len(popular_candidates(_connect(artifacts, repos, [_POPULAR]), top_n=1)) == 1


def test_known_blob_is_in_registry() -> None:
    sha = "c" * 40
    artifacts = [_occ("acme/skills", sha, "2025-10-01", 1, "known")]
    repos = [{"full_name": "acme/skills", "stars": 3}]
    ranked = popular_candidates(_connect(artifacts, repos, [sha]))
    assert bool(ranked.iloc[0]["in_registry"]) is True
    assert ranked.iloc[0]["file_sha"] == sha


def test_earliest_commit_beats_higher_stars() -> None:
    sha = "d" * 40
    artifacts = [
        _occ("early/origin", sha, "2025-10-01", 1, "demo"),
        _occ("late/mirror", sha, "2026-04-01", 0, None),
    ]
    repos = [
        {"full_name": "early/origin", "stars": 2},
        {"full_name": "late/mirror", "stars": 9000},
    ]
    row = popular_candidates(_connect(artifacts, repos, [])).iloc[0]
    assert row["candidate_repo"] == "early/origin"
    assert int(row["candidate_stars"]) == 2
    assert row["candidate_first_commit_at"] == "2025-10-01"
    assert row["name"] == "demo"


def test_equal_dates_pick_higher_stars() -> None:
    sha = "e" * 40
    artifacts = [
        _occ("low/repo", sha, "2025-10-01", 1, "skill"),
        _occ("high/repo", sha, "2025-10-01", 0, None),
    ]
    repos = [
        {"full_name": "low/repo", "stars": 1},
        {"full_name": "high/repo", "stars": 50},
    ]
    row = popular_candidates(_connect(artifacts, repos, [])).iloc[0]
    assert row["candidate_repo"] == "high/repo"
    assert int(row["candidate_stars"]) == 50
    assert row["candidate_first_commit_at"] == "2025-10-01"


def test_highest_stars_when_commit_is_missing() -> None:
    sha = "f" * 40
    artifacts = [
        _occ("dim/one", sha, None, 1, "plain"),
        _occ("bright/two", sha, None, 0, None),
    ]
    repos = [
        {"full_name": "dim/one", "stars": 4},
        {"full_name": "bright/two", "stars": 40},
    ]
    row = popular_candidates(_connect(artifacts, repos, [])).iloc[0]
    assert row["candidate_repo"] == "bright/two"
    assert int(row["candidate_stars"]) == 40
    assert row["candidate_first_commit_at"] is None


def test_star_tie_breaks_by_repo_name() -> None:
    sha = "9" * 40
    artifacts = [
        _occ("b/repo", sha, None, 1, "tied"),
        _occ("a/repo", sha, None, 0, None),
    ]
    repos = [
        {"full_name": "b/repo", "stars": 3},
        {"full_name": "a/repo", "stars": 3},
    ]
    row = popular_candidates(_connect(artifacts, repos, [])).iloc[0]
    assert row["candidate_repo"] == "a/repo"


def test_owner_repos_lists_by_files_then_stars() -> None:
    artifacts = (
        [_occ("microsoft/big", "1" * 40, None, 1, "s") for _ in range(5)]
        + [_occ("microsoft/small", "2" * 40, None, 1, "s") for _ in range(2)]
        + [_occ("other/huge", "3" * 40, None, 1, "s") for _ in range(9)]
    )
    repos = [
        {"full_name": "microsoft/big", "stars": 10},
        {"full_name": "microsoft/small", "stars": 100},
        {"full_name": "other/huge", "stars": 1},
    ]
    listed = owner_repos(_connect(artifacts, repos, []), "microsoft")
    assert list(listed.columns) == ["repo", "files", "stars"]
    assert list(listed["repo"]) == ["microsoft/big", "microsoft/small"]
    assert list(listed["files"].astype(int)) == [5, 2]
    assert list(listed["stars"].astype(int)) == [10, 100]
    assert list(owner_repos(_connect(artifacts, repos, []), "  ").columns) == [
        "repo",
        "files",
        "stars",
    ]


def test_top_n_must_be_positive() -> None:
    with pytest.raises(ValueError, match="top_n"):
        popular_candidates(duckdb.connect(), top_n=0)


def test_run_writes_candidates_and_skips(tmp_cfg: Config) -> None:
    sha = "7" * 40
    _write_inputs(
        tmp_cfg,
        [_occ("acme/skills", sha, "2025-11-01", 1, "kept")],
        [{"full_name": "acme/skills", "stars": 8}],
        [sha],
    )
    result = run(tmp_cfg, force=True)
    assert result.skipped is False
    assert result.rows == {"s1_popular_candidates": 1}
    frame = _read_csv(result.outputs[0])
    assert list(frame.columns) == _COLUMNS
    assert frame.iloc[0]["file_sha"] == sha
    assert bool(frame.iloc[0]["in_registry"]) is True
    assert frame.iloc[0]["candidate_repo"] == "acme/skills"
    assert manifest_path(result.outputs[0]).is_file()
    again = run(tmp_cfg)
    assert again.skipped
    assert again.rows == result.rows
    forced = run(tmp_cfg, force=True)
    assert forced.skipped is False


def test_dev_sample_counts_every_copy_of_sampled_blobs(tmp_cfg: Config) -> None:
    kept = "8" * 40
    dropped = "6" * 40
    artifacts = [
        _occ("a/one", kept, None, 1, "kept"),
        _occ("b/two", kept, None, 0, None),
        _occ("c/three", kept, None, 0, None),
        _occ("d/four", dropped, None, 1, "dropped"),
        _occ("e/five", dropped, None, 0, None),
    ]
    repos = [{"full_name": row["repo_full_name"], "stars": 1} for row in artifacts]
    _write_inputs(tmp_cfg, artifacts, repos, [kept])
    sample = resolve(tmp_cfg, tmp_cfg.paths.derived) / "s0_devsample_ids.parquet"
    pd.DataFrame({"file_sha": [kept], "stratum": ["x"]}).to_parquet(sample, index=False)
    result = run(tmp_cfg, dev=True, force=True)
    frame = _read_csv(result.outputs[0])
    assert list(frame["file_sha"]) == [kept]
    assert int(frame.iloc[0]["files"]) == 3
    assert int(frame.iloc[0]["owners"]) == 3


def test_missing_matches_names_the_command(tmp_cfg: Config) -> None:
    _write_inputs(
        tmp_cfg,
        [_occ("acme/skills", "5" * 40, None, 1, "s")],
        [{"full_name": "acme/skills", "stars": 1}],
        blobs=None,
    )
    with pytest.raises(FileNotFoundError, match="registry --only match"):
        run(tmp_cfg, force=True)


def _mirror_copies() -> list[dict[str, object]]:
    rows = []
    for index in range(1074):
        rows.append(
            _occ(
                f"mirror{index % 29}/pack",
                _MIRROR,
                "2024-01-01" if index == 0 else None,
                1 if index == 0 else 0,
                "mirrored-skill" if index == 0 else None,
            )
        )
    return rows


def _owner_copies() -> list[dict[str, object]]:
    return [
        _occ(
            f"user{index}/app",
            _POPULAR,
            "2025-06-01" if index == 0 else None,
            1 if index == 0 else 0,
            "popular-skill" if index == 0 else None,
        )
        for index in range(400)
    ]


def _occ(
    repo: str,
    sha: str,
    committed: str | None,
    primary: int,
    name: str | None,
) -> dict[str, object]:
    return {
        "repo_full_name": repo,
        "file_sha": sha,
        "name": name,
        "first_commit_at": committed,
        "dedup_primary": primary,
    }


def _connect(
    artifacts: list[dict[str, object]],
    repos: list[dict[str, object]],
    blobs: list[str],
) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.register("artifacts", pd.DataFrame(artifacts))
    con.register("repos", pd.DataFrame(repos))
    con.register("registry_matches", pd.DataFrame({"blob": pd.Series(blobs, dtype="string")}))
    return con


def _write_inputs(
    cfg: Config,
    artifacts: list[dict[str, object]],
    repos: list[dict[str, object]],
    blobs: list[str] | None,
) -> None:
    raw = resolve(cfg, cfg.paths.raw)
    _write_parquet(raw / "artifacts" / "part.parquet", pd.DataFrame(artifacts))
    _write_parquet(raw / "repos" / "part.parquet", pd.DataFrame(repos))
    if blobs is None:
        return
    derived = resolve(cfg, cfg.paths.derived) / "s1_registry_matches.parquet"
    _write_parquet(derived, pd.DataFrame({"blob": blobs}))


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"file_sha": "string"})


def _write_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
