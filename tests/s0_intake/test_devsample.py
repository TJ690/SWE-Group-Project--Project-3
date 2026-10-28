from __future__ import annotations

import dataclasses
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s0_intake.devsample import build_devsample
from skillsig.s0_intake.loader import connect

_OTHER_VIEWS = (
    "repos/part-00000.parquet",
    "artifact_siblings/part-00000.parquet",
    "mining_runs/part-00000.parquet",
)


def _overwrite_artifacts(cfg: Config, rows: list[tuple[str, str, int, int]]) -> None:
    path = resolve(cfg, cfg.paths.raw) / "artifacts" / "part-00000.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "file_sha": [row[0] for row in rows],
            "location_class": [row[1] for row in rows],
            "history_fetched": [row[2] for row in rows],
            "dedup_primary": [row[3] for row in rows],
        }
    )
    pq.write_table(table, path, compression="zstd")


def _stratified_rows() -> tuple[list[tuple[str, str, int, int]], dict[str, str]]:
    """8 singleton skills, 5 double copies, 4 skills with 10 copies each."""
    rows: list[tuple[str, str, int, int]] = []
    expected: dict[str, str] = {}
    for i in range(8):
        sha = f"a-{i:02d}"
        rows.append((sha, "canonical", 1, 1))
        expected[sha] = "canonical|history=1|copies=1"
    for i in range(5):
        sha = f"b-{i:02d}"
        rows.append((sha, "other", 0, 1))
        rows.append((sha, "zzz-copy", 1, 0))
        expected[sha] = "other|history=0|copies=2-9"
    for i in range(4):
        sha = f"c-{i:02d}"
        rows.append((sha, "skills-dir", 1, 1))
        for _ in range(9):
            rows.append((sha, "zzz-copy", 0, 0))
        expected[sha] = "skills-dir|history=1|copies=>=10"
    return rows, expected


def _read(path: Path) -> list[tuple[str, str]]:
    table = pq.read_table(path)
    shas = table.column("file_sha").to_pylist()
    strata = table.column("stratum").to_pylist()
    return list(zip(shas, strata, strict=True))


def _stub_other_views(cfg: Config) -> None:
    raw = resolve(cfg, cfg.paths.raw)
    generated = resolve(cfg, cfg.paths.generated)
    stub = pa.table({"file_sha": ["stub"]})
    for rel in _OTHER_VIEWS:
        path = raw / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(stub, path, compression="zstd")
    for name in ("skill_features", "file_agents", "repo_profile", "sibling_files"):
        path = generated / f"{name}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(stub, path, compression="zstd")


def test_sample_is_deterministic_and_covers_strata(tmp_cfg: Config) -> None:
    rows, expected = _stratified_rows()
    _stub_other_views(tmp_cfg)
    _overwrite_artifacts(tmp_cfg, rows)
    con = connect(tmp_cfg)
    try:
        first = _read(build_devsample(con, tmp_cfg, n=6))
        second = _read(build_devsample(con, tmp_cfg, n=6))
    finally:
        con.close()

    assert first == second
    assert [sha for sha, _ in first] == sorted(sha for sha, _ in first)
    assert len(first) == 6
    assert {stratum for _, stratum in first} == set(expected.values())
    assert all(stratum == expected[sha] for sha, stratum in first)

    other = dataclasses.replace(tmp_cfg, seed=tmp_cfg.seed + 1)
    con = connect(other)
    try:
        reshuffled = _read(build_devsample(con, other, n=6))
    finally:
        con.close()
    assert [sha for sha, _ in reshuffled] != [sha for sha, _ in first]


def test_force_include_always_kept(tmp_cfg: Config) -> None:
    rows, expected = _stratified_rows()
    _stub_other_views(tmp_cfg)
    _overwrite_artifacts(tmp_cfg, rows)
    # n=1 lands in the largest stratum (the a-* skills). b-00 is outside that quota.
    forced_cfg = dataclasses.replace(tmp_cfg)
    object.__setattr__(forced_cfg, "dev_force_include", ("b-00", "not-a-real-sha"))
    con = connect(forced_cfg)
    try:
        sampled = dict(_read(build_devsample(con, forced_cfg, n=1)))
    finally:
        con.close()
    assert sampled["b-00"] == expected["b-00"]
    assert sampled["not-a-real-sha"] == "absent"
