"""Rule baselines B0-B3b and the owner + cluster grouped split."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s3_labeling.baselines import (
    BASELINES,
    group_keys,
    group_split,
    predict_rules,
    run,
)


def _features() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "label_v2": [
                "insufficient_evidence",
                "insufficient_evidence",
                "highly_templated",
                "agent_assisted",
                "insufficient_evidence",
            ],
            "copies": [1, 12, 10, 1, None],
            "agent_folder": [
                "Claude Code",
                "Shared (.agents)",
                "No agent folder",
                "Cursor",
                None,
            ],
            "ai_trailer_first": [True, True, False, None, True],
            "batch_size": [1, 10, 1, None, 5],
        }
    )


def test_baseline_names() -> None:
    assert BASELINES == (
        "B0_majority",
        "B1_copy_count",
        "B2_agent_folder",
        "B3_trailer",
        "B3b_trailer_no_bulk",
    )


def test_b0_is_constant_majority(cfg: Config) -> None:
    got = predict_rules(_features(), "B0_majority", cfg)
    assert set(got) == {"insufficient_evidence"}


def test_b1_copy_count(cfg: Config) -> None:
    got = predict_rules(_features(), "B1_copy_count", cfg).tolist()
    assert got == [
        "insufficient_evidence",
        "highly_templated",
        "highly_templated",
        "insufficient_evidence",
        "insufficient_evidence",
    ]


def test_b2_ignores_shared_and_missing_folders(cfg: Config) -> None:
    got = predict_rules(_features(), "B2_agent_folder", cfg).tolist()
    assert got == [
        "agent_assisted",
        "insufficient_evidence",
        "insufficient_evidence",
        "agent_assisted",
        "insufficient_evidence",
    ]


def test_b3_and_b3b_differ_on_bulk(cfg: Config) -> None:
    b3 = predict_rules(_features(), "B3_trailer", cfg).tolist()
    b3b = predict_rules(_features(), "B3b_trailer_no_bulk", cfg).tolist()
    assert b3[1] == "agent_assisted" and b3b[1] == "insufficient_evidence"  # batch 10
    assert b3[4] == b3b[4] == "agent_assisted"  # batch 5 < 6
    assert b3[3] == "insufficient_evidence"  # null trailer


def test_unknown_baseline_raises(cfg: Config) -> None:
    with pytest.raises(ValueError, match="B9"):
        predict_rules(_features(), "B9", cfg)


def test_group_keys_merge_owners_sharing_a_cluster() -> None:
    owner = pd.Series(["a", "b", "c", "a", "d"])
    cluster = pd.Series([1, 1, 2, None, None])
    groups = group_keys(owner, cluster).tolist()
    assert groups[0] == groups[1] == groups[3]  # a and b share cluster 1; a's other skill joins
    assert groups[2] != groups[0]
    assert groups[4] not in {groups[0], groups[2]}
    assert groups == group_keys(owner, cluster).tolist()  # deterministic


def test_group_split_never_leaks_owner_or_cluster() -> None:
    rng = np.random.default_rng(0)
    n = 400
    owner_idx = rng.integers(0, 60, n)
    # Mostly owner-private clusters, plus a few clusters shared by owner pairs (2k, 2k+1).
    private = owner_idx * 3 + rng.integers(0, 3, n)
    shared = 1000 + owner_idx // 2
    cluster = np.where(rng.random(n) < 0.1, shared, private).astype(float)
    cluster[rng.random(n) < 0.2] = np.nan
    df = pd.DataFrame({"owner": [f"o{i}" for i in owner_idx], "cluster_id": cluster})
    folds = group_split(df, seed=1, n_splits=5)
    assert len(folds) == 5
    for train, test in folds:
        tr, te = df.iloc[train], df.iloc[test]
        assert not set(tr["owner"]) & set(te["owner"])
        assert not set(tr["cluster_id"].dropna()) & set(te["cluster_id"].dropna())
    assert sorted(np.concatenate([t for _, t in folds]).tolist()) == list(range(n))
    assert [t.tolist() for _, t in folds] == [t.tolist() for _, t in group_split(df, seed=1)]


def test_run_writes_labels_and_summary(tmp_cfg: Config) -> None:
    _write_inputs(tmp_cfg)
    result = run(tmp_cfg)
    labels = pd.read_parquet(result.outputs[0])
    assert list(labels.columns) == ["file_sha", *BASELINES]
    assert labels["file_sha"].tolist() == ["a", "b", "c"]
    row = labels.set_index("file_sha")
    assert row.loc["a", "B2_agent_folder"] == "agent_assisted"
    assert row.loc["b", "B1_copy_count"] == "highly_templated"
    assert row.loc["c", "B3_trailer"] == "agent_assisted"
    assert row.loc["c", "B3b_trailer_no_bulk"] == "insufficient_evidence"
    summary = pd.read_csv(result.outputs[1])
    shares = summary.groupby("baseline")["share"].sum()
    assert np.allclose(shares.to_numpy(), 1.0)
    assert run(tmp_cfg).skipped is True


def test_missing_v2_names_command(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match=r"label --only rules_v2"):
        run(tmp_cfg)


def _write_inputs(cfg: Config) -> None:
    derived = resolve(cfg, cfg.paths.derived)
    _parquet(
        derived / "s3_labels_v2.parquet",
        [
            {"file_sha": "a", "label": "insufficient_evidence"},
            {"file_sha": "b", "label": "highly_templated"},
            {"file_sha": "c", "label": "insufficient_evidence"},
        ],
    )
    _parquet(
        derived / "s3_labels_v1.parquet",
        [
            {"file_sha": "a", "label": "x", "copies": 1},
            {"file_sha": "b", "label": "x", "copies": 15},
            {"file_sha": "c", "label": "x", "copies": 1},
        ],
    )
    _parquet(
        derived / "s2_commit.parquet",
        [
            {"file_sha": "a", "ai_trailer_first": False, "batch_size": 1},
            {"file_sha": "b", "ai_trailer_first": False, "batch_size": 1},
            {"file_sha": "c", "ai_trailer_first": True, "batch_size": 9},
        ],
    )
    _parquet(
        resolve(cfg, cfg.paths.raw) / "artifacts" / "part-0.parquet",
        [
            {"file_sha": s, "dedup_primary": 1, "repo_full_name": f"o/{s}", "path": "SKILL.md"}
            for s in ("a", "b", "c")
        ],
    )
    _parquet(
        resolve(cfg, cfg.paths.generated) / "file_agents.parquet",
        [
            {
                "repo_full_name": "o/a",
                "path": "SKILL.md",
                "file_sha": "a",
                "agent_folder": "Claude Code",
            },
            {
                "repo_full_name": "o/b",
                "path": "SKILL.md",
                "file_sha": "b",
                "agent_folder": "No agent folder",
            },
        ],
    )


def _parquet(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)
