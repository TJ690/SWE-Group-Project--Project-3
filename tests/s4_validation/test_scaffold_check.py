"""Scaffold signal checks: overlap, recall, agent confound, spot sheet."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s4_validation.scaffold_check import (
    confound_table,
    evidence,
    overlap_table,
    recall_table,
    run,
    scaffold_frame,
    spot_sample,
)

_PLANTED = "always run the validation script before you publish the skill to the registry"


def _write(cfg: Config) -> None:
    d = resolve(cfg, cfg.paths.derived)
    scaffold = [
        # sha, owner, basis, family, existing, agent
        ("a1", "o1", "outline", 1, "none", True),
        ("a2", "o2", "outline", 1, "cluster", False),
        ("a3", "o3", "shared_lines", 2, "registry_exact", False),
        ("a4", "o4", None, 3, "registry_near", False),
        ("a5", "o5", None, None, "none", True),
    ]
    _pq(
        d / "s2_scaffold.parquet",
        [
            {
                "file_sha": s,
                "owner": o,
                "scaffold_basis": b,
                "placeholder_generator": None,
                "outline_family_id": f,
                "masked_sha": None,
            }
            for s, o, b, f, _e, _a in scaffold
        ],
    )
    _pq(
        d / "s3_labels_v2_1.parquet",
        [
            {
                "file_sha": s,
                "label": "x",
                "templated_basis": None if e == "none" else e,
                "flag_disclosed_agent": a,
                "reasons": ["x"],
            }
            for s, _o, _b, _f, e, a in scaffold
        ],
    )
    tmpl = [("a3", "exact", 1.0), ("a4", "near", 0.9), ("a1", "none", 0.1)]
    _pq(
        d / "s2_template.parquet",
        [{"file_sha": s, "match_kind": k, "est_jaccard": j} for s, k, j in tmpl],
    )
    _pq(
        d / "s3_labels_v1.parquet",
        [{"file_sha": s, "label": "x", "copies": 1} for s, *_ in scaffold],
    )
    _pq(
        d / "s2_clusters.parquet",
        [
            {
                "file_sha": "a4",
                "cluster_id": 1,
                "cluster_size": 12,
                "threshold": 0.8,
                "nn_sim_est": 0.9,
            }
        ],
    )
    raw = resolve(cfg, cfg.paths.raw)
    texts = {
        "a1": "intro " + _PLANTED + " " + " ".join(f"x{i}" for i in range(20)),
        "a2": " ".join(f"y{i}" for i in range(20)) + " " + _PLANTED,
    }
    _pq(
        raw / "artifacts" / "part-00000.parquet",
        [{"file_sha": s, "content": t, "dedup_primary": 1} for s, t in texts.items()],
    )


def _pq(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def _frame(cfg: Config) -> pd.DataFrame:
    con = duckdb.connect()
    try:
        return scaffold_frame(con, cfg)
    finally:
        con.close()


def test_overlap_counts_new_hits(tmp_cfg: Config) -> None:
    _write(tmp_cfg)
    table = overlap_table(_frame(tmp_cfg)).set_index("scaffold_basis")
    assert table.loc["outline", "none"] == 1
    assert table.loc["outline", "cluster"] == 1
    assert table.loc["outline", "total"] == 2
    assert table.loc["none", "total"] == 2
    assert table.loc["masked_copy", "total"] == 0


def test_recall_on_known_copies() -> None:
    known = pd.DataFrame(
        {
            "registry_kind": ["exact", "near", "near", "none"],
            "registry_origin": [False, False, False, False],
            "copy_rule": [False, False, True, False],
            "cluster_rule": [False, True, False, False],
            "scaffold_basis": ["shared_lines", None, "outline", "outline"],
        }
    )
    table = recall_table(known).set_index("known_set")
    both = table.loc["exact_or_near"]
    assert both["n"] == 3
    assert both["scaffold_hits"] == 2
    assert both["scaffold_only_hits"] == 1
    assert both["combined_recall"] == pytest.approx(1.0)


def test_confound_rates(tmp_cfg: Config) -> None:
    _write(tmp_cfg)
    table = confound_table(_frame(tmp_cfg)).set_index("group")
    assert table.loc["disclosed_agent", "n"] == 2
    assert table.loc["disclosed_agent", "outline_rate"] == pytest.approx(0.5)
    assert table.loc["not_disclosed", "outline_only_hits"] == 1


def test_spot_sample_partner_from_other_owner(tmp_cfg: Config) -> None:
    _write(tmp_cfg)
    cfg = dataclasses.replace(tmp_cfg, sample=dataclasses.replace(tmp_cfg.sample, per_label=5))
    sample = spot_sample(_frame(cfg), cfg).set_index("file_sha")
    assert set(sample.index) == {"a1", "a2", "a3"}
    assert sample.loc["a1", "partner_sha"] == "a2"
    assert pd.isna(sample.loc["a3", "partner_sha"])  # alone in its family
    sheet = evidence(sample.reset_index(), {"a1": "", "a2": ""}, cfg)
    assert list(sheet.columns)[-2:] == ["annotator_label", "notes"]


def test_run_writes_tables_with_evidence(tmp_cfg: Config) -> None:
    _write(tmp_cfg)
    result = run(tmp_cfg)
    assert [p.name for p in result.outputs] == [
        "s4_scaffold_overlap.csv",
        "s4_scaffold_recall.csv",
        "s4_scaffold_agent_confound.csv",
        "s4_scaffold_spot.csv",
    ]
    spot = pd.read_csv(result.outputs[3]).set_index("file_sha")
    assert spot.loc["a1", "winnow_overlap"] > 0
    assert _PLANTED in spot.loc["a1", "shared_passage_1"]
    assert run(tmp_cfg).skipped is True
