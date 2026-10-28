"""E2 template rules vs. known registry provenance."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s4_validation.template_provenance import (
    provenance_frame,
    recall_table,
    run,
    unexplained_table,
)


def _write(cfg: Config) -> None:
    d = resolve(cfg, cfg.paths.derived)
    # k1-k4 known copies (k1-k3 copied >= 10 times); o1 is a registry origin file; u1 unmatched
    tmpl = [
        ("k1", "exact", 1.0),
        ("k2", "exact", 1.0),
        ("k3", "near", 0.9),
        ("k4", "near", 0.85),
        ("o1", "exact", 1.0),
        ("u1", "none", 0.3),
        ("lo", "near", 0.7),
    ]
    _pq(
        d / "s2_template.parquet",
        [{"file_sha": s, "match_kind": k, "est_jaccard": j} for s, k, j in tmpl],
    )
    copies = {"k1": 12, "k2": 30, "k3": 10, "k4": 1, "o1": 1, "u1": 15, "lo": 1}
    _pq(
        d / "s3_labels_v1.parquet",
        [{"file_sha": s, "label": "x", "copies": c} for s, c in copies.items()],
    )
    clusters = [
        {"file_sha": "k4", "cluster_size": 50, "threshold": 0.6},  # low threshold: ignored
        {"file_sha": "k4", "cluster_size": 2, "threshold": 0.8},
        {"file_sha": "u1", "cluster_size": 20, "threshold": 0.8},
    ]
    _pq(d / "s2_clusters.parquet", [{**c, "cluster_id": 1, "nn_sim_est": 0.0} for c in clusters])
    labels = []
    for s in copies:
        reasons = ["registry_origin"] if s == "o1" else ["x"]
        labels.append({"file_sha": s, "label": "highly_templated", "reasons": reasons})
    _pq(d / "s3_labels_v2_1.parquet", labels)


def _pq(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def _frame(cfg: Config) -> pd.DataFrame:
    con = duckdb.connect()
    try:
        return provenance_frame(con, cfg)
    finally:
        con.close()


def test_recall_on_known_copies(tmp_cfg: Config) -> None:
    _write(tmp_cfg)
    table = recall_table(_frame(tmp_cfg)).set_index("known_set")
    both = table.loc["exact_or_near"]
    assert both["n"] == 4  # o1 (origin) and lo (below Jaccard 0.8) excluded
    assert both["copy_rule_hits"] == 3
    assert both["copy_recall"] == pytest.approx(0.75)
    assert both["cluster_rule_hits"] == 0  # k4's 0.6 cluster is ignored
    assert table.loc["exact", "reference_type"] == "exact_blob_identity"
    assert table.loc["near", "reference_type"] == "similarity_conditioned_candidates"
    assert table.loc["exact_or_near", "reference_type"] == "mixed_exploratory"
    assert table.loc["exact", "n"] == 2
    assert table.loc["exact", "either_recall"] == 1.0
    assert table.loc["near", "either_recall"] == 0.5


def test_missing_near_similarity_is_not_a_positive_reference(tmp_cfg: Config) -> None:
    _write(tmp_cfg)
    path = resolve(tmp_cfg, tmp_cfg.paths.derived) / "s2_template.parquet"
    data = pd.read_parquet(path)
    data.loc[data.file_sha.eq("k3"), "est_jaccard"] = None
    data.to_parquet(path, index=False)
    assert _frame(tmp_cfg).set_index("file_sha").loc["k3", "registry_kind"] == "none"


def test_unexplained_share(tmp_cfg: Config) -> None:
    _write(tmp_cfg)
    table = unexplained_table(_frame(tmp_cfg)).set_index("rule")
    assert table.loc["copy_rule", "n"] == 4  # k1, k2, k3, u1
    assert table.loc["copy_rule", "registry_matched"] == 3
    assert "agreement" in table.loc["either", "note"]


def test_run_writes_both_tables(tmp_cfg: Config) -> None:
    _write(tmp_cfg)
    result = run(tmp_cfg)
    assert [p.name for p in result.outputs] == [
        "s4_template_provenance.csv",
        "s4_template_unexplained.csv",
    ]
    assert run(tmp_cfg).skipped is True
