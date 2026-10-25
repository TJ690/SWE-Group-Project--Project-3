"""Repository context: project kind, benchmark-like names, age at the snapshot."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s2_features.repo_context import benchmark_like_name, repo_kind, run

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
)


def test_malskillbench_name_is_benchmark_like() -> None:
    assert benchmark_like_name("x/MalSkillBench") is True
    assert benchmark_like_name("lxyeternal/MalSkillBench") is True


def test_benchmark_like_name_is_a_case_insensitive_substring() -> None:
    assert benchmark_like_name("ada/skill-DEMO") is True
    assert benchmark_like_name("ada/latest") is True  # "latest" contains "test"
    assert benchmark_like_name("ada/notes") is False
    assert benchmark_like_name(None) is False


def test_copy_share_0_7_is_collection() -> None:
    row = _kind_row(copy_share=0.7, size_tier="6-20", files=10)
    assert repo_kind(row) == "A_collection"


def test_one_file_personal_python_repo_is_project() -> None:
    row = _kind_row(language="Python", files=1, size_tier="1", copy_share=0.0)
    assert row["language"] == "Python"
    assert row["files"] == 1
    assert repo_kind(row) == "C_project"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"copy_share": 0.5}, "A_collection"),
        ({"size_tier": "101-1000"}, "A_collection"),
        ({"size_tier": ">1000"}, "A_collection"),
        ({"name_is_collection": True, "name_has_skill": True}, "A_collection"),
        ({"name_has_skill": True}, "B_agent_tooling"),
        ({"name_has_agent": True}, "B_agent_tooling"),
        ({"copy_share": 0.499, "size_tier": "21-100"}, "C_project"),
        ({"copy_share": None, "size_tier": None}, "C_project"),
    ],
)
def test_repo_kind_priority(overrides: dict[str, object], expected: str) -> None:
    assert repo_kind(_kind_row(**overrides)) == expected


def test_run_writes_kinds_and_skips_when_unchanged(tmp_cfg: Config) -> None:
    _write_profile(tmp_cfg, _sample_rows())
    _write_mining_runs(tmp_cfg)
    result = run(tmp_cfg)
    assert result.skipped is False
    assert result.rows == {"s2_repo": 4}
    assert result.outputs[0] == resolve(tmp_cfg, tmp_cfg.paths.derived) / "s2_repo.parquet"

    by_name = _read(result.outputs[0])
    assert list(by_name) == [
        "ada/notes",
        "orphan/nodate",
        "some/copies",
        "x/MalSkillBench",
    ]
    bench = by_name["x/MalSkillBench"]
    assert bench["benchmark_like_name"] is True
    assert bench["repo_kind"] == "B_agent_tooling"
    copies = by_name["some/copies"]
    assert copies["repo_kind"] == "A_collection"
    assert copies["copy_share"] == pytest.approx(0.7)
    assert copies["language"] is None
    personal = by_name["ada/notes"]
    assert personal["repo_kind"] == "C_project"
    assert personal["language"] == "Python"
    assert personal["files"] == 1
    assert personal["benchmark_like_name"] is False
    assert personal["age_days_at_snapshot"] == 292
    assert personal["stars"] == 2
    assert by_name["orphan/nodate"]["age_days_at_snapshot"] is None
    assert by_name["orphan/nodate"]["repo_kind"] == "C_project"

    again = run(tmp_cfg)
    assert again.skipped is True
    assert again.rows == {"s2_repo": 4}
    forced = run(tmp_cfg, force=True)
    assert forced.skipped is False
    dev_run = run(tmp_cfg, dev=True)
    assert dev_run.skipped is True
    assert dev_run.rows == {"s2_repo": 4}


def test_missing_repo_profile_names_repos_sql(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match=r"repos\.sql"):
        run(tmp_cfg)


def test_missing_mining_runs_names_the_download_command(tmp_cfg: Config) -> None:
    _write_profile(tmp_cfg, _sample_rows()[:1])
    with pytest.raises(FileNotFoundError, match="download_data.py"):
        run(tmp_cfg)


def _kind_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "full_name": "ada/notes",
        "language": "Python",
        "files": 1,
        "size_tier": "1",
        "copy_share": 0.0,
        "name_is_collection": False,
        "name_has_skill": False,
        "name_has_agent": False,
    }
    row.update(overrides)
    return row


def _sample_rows() -> list[dict[str, object]]:
    return [
        _profile("ada/notes", language="Python", stars=2, files=1, copy_share=0.0, size_tier="1"),
        _profile(
            "some/copies",
            language=None,
            files=10,
            copy_share=0.7,
            size_tier="6-20",
        ),
        _profile(
            "x/MalSkillBench",
            language="Python",
            files=30,
            copy_share=0.1,
            size_tier="21-100",
            name_has_skill=True,
        ),
        _profile(
            "orphan/nodate",
            language=None,
            created_at=None,
            pushed_at=None,
            files=1,
            copy_share=0.0,
            size_tier="1",
        ),
    ]


def _profile(
    full_name: str,
    *,
    language: str | None = "Python",
    stars: int = 0,
    files: int = 1,
    copy_share: float = 0.0,
    size_tier: str = "1",
    name_has_skill: bool = False,
    name_is_collection: bool = False,
    name_has_agent: bool = False,
    created_at: str | None = _CREATED,
    pushed_at: str | None = _PUSHED,
) -> dict[str, object]:
    owner = full_name.split("/", 1)[0]
    return {
        "full_name": full_name,
        "owner": owner,
        "stars": stars,
        "language": language,
        "created_at": created_at,
        "pushed_at": pushed_at,
        "files": files,
        "copy_share": copy_share,
        "size_tier": size_tier,
        "name_has_skill": name_has_skill,
        "name_is_collection": name_is_collection,
        "name_has_agent": name_has_agent,
    }


def _write_profile(cfg: Config, rows: list[dict[str, object]]) -> None:
    path = resolve(cfg, cfg.paths.generated) / "repo_profile.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    strings = ("full_name", "owner", "language", "created_at", "pushed_at", "size_tier")
    flags = ("name_has_skill", "name_is_collection", "name_has_agent")
    columns: dict[str, pa.Array] = {
        name: pa.array([row[name] for row in rows], type=pa.string()) for name in strings
    }
    for name in ("stars", "files"):
        columns[name] = pa.array([row[name] for row in rows], type=pa.int64())
    columns["copy_share"] = pa.array([row["copy_share"] for row in rows], type=pa.float64())
    for name in flags:
        columns[name] = pa.array([row[name] for row in rows], type=pa.bool_())
    pq.write_table(pa.table(columns), path, compression="zstd")


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
