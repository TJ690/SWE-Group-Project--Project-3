"""Specificity: rare identifiers across near-duplicate clusters."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s0_intake.devsample import output_path as devsample_path
from skillsig.s2_features.specificity import (
    MIN_IDENTIFIERS,
    RARE_MAX_CLUSTERS,
    identifiers,
    run,
    score,
)


def test_identifiers_extracts_each_kind() -> None:
    text = (
        "---\nname: some_front_name\n---\n"
        "Run `make deploy-prod` then edit src/app/config.ts and settings.yaml.\n"
        "Use --dry-run with fetchUserData and load_config_file.\n"
        "See https://example.com/some/path_here for docs. Version 2024."
    )
    got = identifiers(text)
    for expected in (
        "make deploy-prod",
        "src/app/config.ts",
        "settings.yaml",
        "--dry-run",
        "fetchuserdata",
        "load_config_file",
    ):
        assert expected in got
    assert "some_front_name" not in got  # front matter dropped
    assert not any("example.com" in token for token in got)  # URLs dropped
    assert "2024" not in got


def test_identifiers_empty() -> None:
    assert identifiers("") == frozenset()
    assert identifiers("Plain prose with no code at all.") == frozenset()


def test_score_counts_rare_and_nulls_small_sets() -> None:
    df = {"a_x": 1, "b_y": RARE_MAX_CLUSTERS, "c_z": 2}
    got = score(frozenset({"a_x", "b_y", "c_z"}), df)
    assert got == {"n_identifiers": 3, "n_rare": 2, "specificity": pytest.approx(2 / 3)}
    assert score(frozenset({"a_x"}), df)["specificity"] is None
    assert MIN_IDENTIFIERS == 3


def _write(cfg: Config) -> None:
    common = "Use `shared_tool` and `common_cmd` and `widely_used`."
    rows = []
    # Five skills in five clusters share the common identifiers -> those are not rare.
    for i in range(5):
        rows.append((f"{i:040x}", common + f" Also `own_thing_{i}`.", i))
    # One copy of skill 0 in the same cluster must not make own_thing_0 common.
    rows.append((f"{9:040x}", common + " Also `own_thing_0`.", 0))
    pad = "\n" + "filler text " * 30
    art = [
        {"file_sha": sha, "dedup_primary": 1, "content": text + pad, "body_chars": 400}
        for sha, text, _ in rows
    ]
    _pq(resolve(cfg, cfg.paths.raw) / "artifacts" / "part-0.parquet", art)
    clusters = [
        {"file_sha": sha, "cluster_id": c, "cluster_size": 1, "threshold": 0.8, "nn_sim_est": 0.0}
        for sha, _, c in rows
    ]
    _pq(resolve(cfg, cfg.paths.derived) / "s2_clusters.parquet", clusters)


def _pq(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def test_run_counts_clusters_not_copies(tmp_cfg: Config) -> None:
    _write(tmp_cfg)
    result = run(tmp_cfg)
    table = pq.read_table(result.outputs[0]).to_pylist()
    by_sha = {row["file_sha"]: row for row in table}
    first = by_sha[f"{0:040x}"]
    assert first["n_identifiers"] == 4
    assert first["n_rare"] == 1  # own_thing_0: 1 cluster despite 2 files
    assert first["specificity"] == pytest.approx(0.25)
    assert run(tmp_cfg).skipped is True


def test_dev_scores_only_sampled_but_df_uses_all(tmp_cfg: Config) -> None:
    _write(tmp_cfg)
    _pq(devsample_path(tmp_cfg), [{"file_sha": f"{1:040x}", "stratum": "x"}])
    table = pq.read_table(run(tmp_cfg, dev=True).outputs[0]).to_pylist()
    assert [row["file_sha"] for row in table] == [f"{1:040x}"]
    assert table[0]["n_rare"] == 1  # shared identifiers still counted as common


def test_missing_clusters_names_command(tmp_cfg: Config) -> None:
    _pq(resolve(tmp_cfg, tmp_cfg.paths.raw) / "artifacts" / "part-0.parquet", [{"file_sha": "x"}])
    with pytest.raises(FileNotFoundError, match="skillsig dedup"):
        run(tmp_cfg)
