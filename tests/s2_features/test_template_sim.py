"""Nearest registry version for each skill signature."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from datasketch import MinHashLSH

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s0_data.devsample import output_path as devsample_path
from skillsig.s1_registry.sources import Source, clone_dir
from skillsig.s2_features.minhash_cluster import signature
from skillsig.s2_features.template_sim import build_registry_index, nearest_registry, run

_REPO = "acme/skills"
_LINEAGE = "acme/skills::demo"
_NEAR = "b" * 40
_NONE = "c" * 40
_EXTRA = "d" * 40
_UNRELATED = " ".join(f"north{i:04d}" for i in range(220))


def _skill(edited: bool) -> str:
    parts = [" ".join(f"p{k}w{i:03d}" for i in range(40)) for k in range(10)]
    if edited:
        parts[4] = " ".join(f"edited{i:03d}" for i in range(40))
    body = "\n\n".join(parts)
    return (
        "---\n"
        "name: demo\n"
        "description: Use this skill whenever the user wants a widget.\n"
        "---\n\n"
        f"{body}\n"
    )


@pytest.fixture
def scored(tmp_cfg: Config) -> dict[str, object]:
    _write_sources(tmp_cfg.root)
    blob = _commit_skill(clone_dir(tmp_cfg, Source(_REPO, "A", "acme", "mined", None, None)))
    _write_versions(tmp_cfg, blob)
    _write_matches(tmp_cfg, blob)
    _write_devsample(tmp_cfg, [blob, _NEAR, _NONE])
    texts = [_skill(False), _skill(True), _UNRELATED, _UNRELATED]
    sigs = [signature(text, tmp_cfg) for text in texts]
    _write_shard(tmp_cfg, [blob, _NEAR, _NONE, _EXTRA], sigs)
    result = run(tmp_cfg, dev=True)
    table = pq.read_table(result.outputs[0])
    rows = {row["file_sha"]: row for row in table.to_pylist()}
    return {"blob": blob, "rows": rows, "result": result, "columns": table.column_names}


def test_exact_blob_is_exact(scored: dict[str, object], tmp_cfg: Config) -> None:
    blob = str(scored["blob"])
    rows = scored["rows"]
    assert isinstance(rows, dict)
    row = rows[blob]
    assert scored["columns"] == [
        "file_sha",
        "match_kind",
        "nearest_blob",
        "nearest_lineage_id",
        "est_jaccard",
    ]
    assert set(rows) == {blob, _NEAR, _NONE}
    assert row["match_kind"] == "exact"
    assert row["nearest_blob"] == blob
    assert row["nearest_lineage_id"] == _LINEAGE
    assert row["est_jaccard"] == 1.0
    result = scored["result"]
    assert result.skipped is False
    assert result.rows == {"s2_template": 3}
    again = run(tmp_cfg, dev=True)
    assert again.skipped is True
    assert again.rows == {"s2_template": 3}


def test_edited_paragraph_is_near(scored: dict[str, object], tmp_cfg: Config) -> None:
    blob = str(scored["blob"])
    rows = scored["rows"]
    assert isinstance(rows, dict)
    row = rows[_NEAR]
    direct = float(np.mean(signature(_skill(False), tmp_cfg) == signature(_skill(True), tmp_cfg)))
    assert row["match_kind"] == "near"
    assert row["nearest_blob"] == blob
    assert row["nearest_lineage_id"] == _LINEAGE
    assert 0.8 <= row["est_jaccard"] <= 1.0
    assert row["est_jaccard"] == direct
    index = build_registry_index(tmp_cfg)
    assert isinstance(index, MinHashLSH)
    found, score = nearest_registry(signature(_skill(True), tmp_cfg), index)
    assert found == blob
    assert score == direct


def test_unrelated_skill_is_none(scored: dict[str, object], tmp_cfg: Config) -> None:
    rows = scored["rows"]
    assert isinstance(rows, dict)
    row = rows[_NONE]
    assert row["match_kind"] == "none"
    assert row["nearest_blob"] is None
    assert row["nearest_lineage_id"] is None
    assert row["est_jaccard"] == 0.0
    found, score = nearest_registry(signature(_UNRELATED, tmp_cfg), build_registry_index(tmp_cfg))
    assert found is None
    assert score == 0.0


def test_missing_versions_names_the_versions_command(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match=r"registry --only versions"):
        run(tmp_cfg, dev=True)


def test_missing_matches_names_the_match_command(tmp_cfg: Config) -> None:
    _write_versions(tmp_cfg, "a" * 40)
    with pytest.raises(FileNotFoundError, match=r"registry --only match"):
        run(tmp_cfg, dev=True)


def test_missing_shards_names_the_dedup_command(tmp_cfg: Config) -> None:
    _write_versions(tmp_cfg, "a" * 40)
    _write_matches(tmp_cfg, "a" * 40)
    _write_devsample(tmp_cfg, ["a" * 40])
    with pytest.raises(FileNotFoundError, match=r"skillsig dedup"):
        run(tmp_cfg, dev=True)


def _write_sources(root: Path) -> None:
    path = root / "config" / "registry_sources.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "sources:\n  - {repo: acme/skills, tier: A, group: acme, status: mined}\n",
        encoding="utf-8",
    )


def _commit_skill(clone: Path) -> str:
    _init_repo(clone)
    path = clone / "skills" / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(_skill(False), encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "Add skill")
    return _git(clone, "rev-parse", "HEAD:skills/demo/SKILL.md").strip()


def _write_versions(cfg: Config, blob: str) -> None:
    path = resolve(cfg, cfg.paths.derived) / "s1_registry_versions.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "lineage_id": [_LINEAGE],
                "repo": [_REPO],
                "skill": ["demo"],
                "blob": [blob],
                "version_seq": pa.array([1], type=pa.int64()),
                "first_date": ["2026-01-01"],
            }
        ),
        path,
        compression="zstd",
    )


def _write_matches(cfg: Config, blob: str) -> None:
    path = resolve(cfg, cfg.paths.derived) / "s1_registry_matches.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"blob": [blob], "origin_repo": [_REPO], "origin_skill": ["demo"]}),
        path,
        compression="zstd",
    )


def _write_devsample(cfg: Config, file_shas: list[str]) -> None:
    path = devsample_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "file_sha": pa.array(file_shas, type=pa.string()),
                "stratum": pa.array(["x"] * len(file_shas), type=pa.string()),
            }
        ),
        path,
        compression="zstd",
    )


def _write_shard(cfg: Config, ids: list[str], sigs: list[np.ndarray]) -> None:
    directory = resolve(cfg, cfg.paths.derived) / "s2_minhash_shards"
    directory.mkdir(parents=True, exist_ok=True)
    np.save(directory / "shard_00000.npy", np.stack(sigs))
    np.save(directory / "shard_00000.ids.npy", np.asarray(ids, dtype="U64"))


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "dev@example.com")
    _git(path, "config", "user.name", "Dev")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "config", "core.autocrlf", "false")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout
