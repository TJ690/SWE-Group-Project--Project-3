"""Repository context: project kind, name flags, share measures, age at the snapshot."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s2_features.repo_context import (
    benchmark_like_name,
    name_flags,
    repo_kind,
    repo_name,
    run,
)

_CREATED = "2025-10-01"
_PUSHED = "2026-01-01T00:00:00Z"
_COLUMNS = (
    "repo_full_name",
    "owner",
    "stars",
    "language",
    "created_at",
    "pushed_at",
    "files",
    "copy_share",
    "size_tier",
    "repo_kind",
    "benchmark_like_name",
    "age_days_at_snapshot",
    "repo_name",
    "is_fork",
    "distinct_skills",
    "name_is_collection",
    "name_has_skill",
    "name_has_agent",
    "shared_share",
    "copied_in_share",
    "registry_origin_share",
)


def test_repo_name_drops_the_owner() -> None:
    assert repo_name("github/spec-kit") == "spec-kit"
    assert repo_name("solo") == "solo"
    assert repo_name(None) == ""


def test_name_flags_ignore_the_owner() -> None:
    assert name_flags("github/spec-kit") == (False, False, False)
    assert name_flags("agentic-labs/notes") == (False, False, False)
    assert name_flags("ada/awesome-skills") == (True, True, False)
    assert name_flags("ada/claude-agents") == (False, False, True)


def test_hub_inside_github_is_not_a_collection_word() -> None:
    assert name_flags("ada/github-tools")[0] is False
    assert name_flags("ada/skills-hub")[0] is True
    assert name_flags("ada/SkillHub")[0] is True


def test_malskillbench_name_is_benchmark_like() -> None:
    assert benchmark_like_name("x/MalSkillBench") is True
    assert benchmark_like_name("lxyeternal/MalSkillBench") is True


def test_benchmark_like_name_needs_test_at_a_word_start() -> None:
    assert benchmark_like_name("ada/skill-DEMO") is True
    assert benchmark_like_name("ada/test-skills") is True
    assert benchmark_like_name("ada/my_testing") is True
    assert benchmark_like_name("ada/latest") is False
    assert benchmark_like_name("ada/contest") is False
    assert benchmark_like_name("testorg/notes") is False  # owner is ignored
    assert benchmark_like_name("ada/notes") is False
    assert benchmark_like_name(None) is False


def test_shared_share_0_7_is_collection() -> None:
    assert repo_kind(_kind_row(shared_share=0.7, files=10)) == "A_collection"


def test_one_file_personal_python_repo_is_project() -> None:
    assert repo_kind(_kind_row(files=1, shared_share=0.0)) == "C_project"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"shared_share": 0.5}, "A_collection"),
        ({"files": 101}, "A_collection"),
        ({"files": 5000}, "A_collection"),
        ({"name_is_collection": True, "name_has_skill": True}, "A_collection"),
        ({"name_has_skill": True}, "B_agent_tooling"),
        ({"name_has_agent": True}, "B_agent_tooling"),
        ({"shared_share": 0.499, "files": 100}, "C_project"),
        ({"shared_share": None, "files": None}, "C_project"),
        ({"copy_share": 1.0}, "C_project"),  # the legacy column no longer decides
    ],
)
def test_repo_kind_priority(overrides: dict[str, object], expected: str) -> None:
    assert repo_kind(_kind_row(**overrides)) == expected


def test_repo_kind_cutoffs_are_parameters() -> None:
    row = _kind_row(shared_share=0.4, files=50)
    assert repo_kind(row) == "C_project"
    assert repo_kind(row, share_min=0.3) == "A_collection"
    assert repo_kind(row, files_min=50) == "A_collection"


def test_run_writes_kinds_and_shares(tmp_cfg: Config) -> None:
    _write_all(tmp_cfg)
    result = run(tmp_cfg)
    assert result.skipped is False
    assert result.rows == {"s2_repo": 7}
    assert result.outputs[0] == resolve(tmp_cfg, tmp_cfg.paths.derived) / "s2_repo.parquet"

    by_name = _read(result.outputs[0])
    assert list(by_name) == sorted(by_name)

    registry = by_name["reg/skills"]
    assert registry["registry_origin_share"] == pytest.approx(1.0)
    assert registry["copied_in_share"] == pytest.approx(0.0)
    assert registry["shared_share"] == pytest.approx(0.5)
    assert registry["repo_kind"] == "A_collection"

    copies = by_name["some/copies"]
    assert copies["shared_share"] == pytest.approx(0.7)
    assert copies["copied_in_share"] == pytest.approx(0.1)
    assert copies["registry_origin_share"] == pytest.approx(0.0)
    assert copies["copy_share"] == pytest.approx(0.9)  # legacy column passes through
    assert copies["repo_kind"] == "A_collection"
    assert copies["language"] is None

    bench = by_name["x/MalSkillBench"]
    assert bench["benchmark_like_name"] is True
    assert bench["name_has_skill"] is True
    assert bench["shared_share"] == pytest.approx(0.1)
    assert bench["repo_kind"] == "B_agent_tooling"

    assert by_name["github/spec-kit"]["name_is_collection"] is False
    assert by_name["github/spec-kit"]["repo_kind"] == "C_project"
    assert by_name["agentic-labs/notes"]["repo_kind"] == "C_project"

    personal = by_name["ada/notes"]
    assert personal["repo_kind"] == "C_project"
    assert personal["repo_name"] == "notes"
    assert personal["is_fork"] is False
    assert personal["distinct_skills"] == 1
    assert personal["age_days_at_snapshot"] == 292
    assert personal["stars"] == 2

    orphan = by_name["orphan/nodate"]
    assert orphan["age_days_at_snapshot"] is None
    assert orphan["shared_share"] is None
    assert orphan["repo_kind"] == "C_project"

    again = run(tmp_cfg)
    assert again.skipped is True
    assert again.rows == {"s2_repo": 7}
    assert run(tmp_cfg, force=True).skipped is False
    dev_run = run(tmp_cfg, dev=True)
    assert dev_run.skipped is True


def test_run_uses_config_cutoffs(tmp_cfg: Config) -> None:
    _write_all(tmp_cfg)
    looser = dataclasses.replace(
        tmp_cfg,
        thresholds=dataclasses.replace(tmp_cfg.thresholds, collection_copy_share_min=0.05),
    )
    by_name = _read(run(looser).outputs[0])
    assert by_name["x/MalSkillBench"]["repo_kind"] == "A_collection"


def test_missing_repo_profile_names_repos_sql(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match=r"repos\.sql"):
        run(tmp_cfg)


def test_missing_artifacts_names_the_download_command(tmp_cfg: Config) -> None:
    _write_profile(tmp_cfg, _sample_rows()[:1])
    with pytest.raises(FileNotFoundError, match="download_data.py"):
        run(tmp_cfg)


def test_missing_registry_matches_names_the_command(tmp_cfg: Config) -> None:
    _write_profile(tmp_cfg, _sample_rows()[:1])
    _write_artifacts(tmp_cfg)
    _write_mining_runs(tmp_cfg)
    with pytest.raises(FileNotFoundError, match="registry --only match"):
        run(tmp_cfg)


def test_missing_mining_runs_names_the_download_command(tmp_cfg: Config) -> None:
    _write_profile(tmp_cfg, _sample_rows()[:1])
    _write_artifacts(tmp_cfg)
    _write_matches(tmp_cfg)
    with pytest.raises(FileNotFoundError, match="download_data.py"):
        run(tmp_cfg)


def _kind_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "full_name": "ada/notes",
        "files": 1,
        "shared_share": 0.0,
        "copy_share": 0.0,
        "name_is_collection": False,
        "name_has_skill": False,
        "name_has_agent": False,
    }
    row.update(overrides)
    return row


def _write_all(cfg: Config) -> None:
    _write_profile(cfg, _sample_rows())
    _write_artifacts(cfg)
    _write_matches(cfg)
    _write_mining_runs(cfg)


def _sample_rows() -> list[dict[str, object]]:
    return [
        _profile("ada/notes", stars=2, files=1),
        _profile("reg/skills", files=2, copy_share=1.0),
        _profile("some/copies", language=None, files=10, copy_share=0.9, size_tier="6-20"),
        _profile("x/MalSkillBench", files=30, copy_share=0.1, size_tier="21-100"),
        _profile("github/spec-kit", files=3, size_tier="2-5", name_is_collection=True),
        _profile("agentic-labs/notes", files=1, name_has_agent=True),
        _profile("orphan/nodate", language=None, created_at=None, pushed_at=None, files=1),
    ]


def _profile(
    full_name: str,
    *,
    language: str | None = "Python",
    stars: int = 0,
    files: int = 1,
    copy_share: float = 0.0,
    size_tier: str = "1",
    name_is_collection: bool = False,
    name_has_agent: bool = False,
    created_at: str | None = _CREATED,
    pushed_at: str | None = _PUSHED,
) -> dict[str, object]:
    # name_* here mimic db/repos.sql (owner included); run() must recompute them.
    return {
        "full_name": full_name,
        "owner": full_name.split("/", 1)[0],
        "stars": stars,
        "language": language,
        "created_at": created_at,
        "pushed_at": pushed_at,
        "files": files,
        "distinct_skills": files,
        "is_fork": False,
        "copy_share": copy_share,
        "size_tier": size_tier,
        "name_has_skill": False,
        "name_is_collection": name_is_collection,
        "name_has_agent": name_has_agent,
    }


def _write_profile(cfg: Config, rows: list[dict[str, object]]) -> None:
    path = resolve(cfg, cfg.paths.generated) / "repo_profile.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    strings = ("full_name", "owner", "language", "created_at", "pushed_at", "size_tier")
    flags = ("name_has_skill", "name_is_collection", "name_has_agent", "is_fork")
    columns: dict[str, pa.Array] = {
        name: pa.array([row[name] for row in rows], type=pa.string()) for name in strings
    }
    for name in ("stars", "files", "distinct_skills"):
        columns[name] = pa.array([row[name] for row in rows], type=pa.int64())
    columns["copy_share"] = pa.array([row["copy_share"] for row in rows], type=pa.float64())
    for name in flags:
        columns[name] = pa.array([row[name] for row in rows], type=pa.bool_())
    pq.write_table(pa.table(columns), path, compression="zstd")


def _artifact_rows() -> list[tuple[str, str]]:
    rows = [("ada/notes", "a1"), ("reg/skills", "r1"), ("reg/skills", "r2")]
    rows += [("some/copies", "r1")]
    rows += [("some/copies", f"c{i}") for i in range(6)]
    rows += [("other/holder", f"c{i}") for i in range(6)]
    rows += [("some/copies", f"u{i}") for i in range(3)]
    rows += [("x/MalSkillBench", f"m{i}") for i in range(30)]
    rows += [("other/holder", f"m{i}") for i in range(3)]
    rows += [("github/spec-kit", f"g{i}") for i in range(3)]
    rows += [("agentic-labs/notes", "n1")]
    return rows


def _write_artifacts(cfg: Config) -> None:
    path = resolve(cfg, cfg.paths.raw) / "artifacts" / "part-00000.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = _artifact_rows()
    pq.write_table(
        pa.table(
            {
                "repo_full_name": [repo for repo, _ in rows],
                "file_sha": [sha for _, sha in rows],
            }
        ),
        path,
        compression="zstd",
    )


def _write_matches(cfg: Config) -> None:
    path = resolve(cfg, cfg.paths.derived) / "s1_registry_matches.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"blob": ["r1", "r2"], "origin_repo": ["reg/skills", "reg/skills"]}),
        path,
        compression="zstd",
    )


def _write_mining_runs(cfg: Config) -> None:
    path = resolve(cfg, cfg.paths.raw) / "mining_runs" / "part-00000.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "finished_at": [
                    "2026-07-01T00:00:00+00:00",
                    "2026-07-20T22:26:56.553671+00:00",
                ]
            }
        ),
        path,
        compression="zstd",
    )


def _read(path: Path) -> dict[str, dict[str, object]]:
    con = duckdb.connect()
    try:
        described = con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [path.as_posix()])
        names = [row[0] for row in described.fetchall()]
        assert names == list(_COLUMNS)
        table = con.execute(
            "SELECT * FROM read_parquet(?) ORDER BY repo_full_name", [path.as_posix()]
        )
        header = [item[0] for item in table.description]
        return {row[0]: dict(zip(header, row, strict=True)) for row in table.fetchall()}
    finally:
        con.close()
