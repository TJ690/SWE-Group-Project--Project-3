"""Reuse and agent disclosures co-occur without a forced authorship verdict."""

from __future__ import annotations

import dataclasses
import json

import pandas as pd
import pytest

from skillsig.paths import resolve
from skillsig.s3_labeling import evidence


def _fixture(cfg):
    d = resolve(cfg, cfg.paths.derived)
    d.mkdir(parents=True)
    ids = ["exact", "near", "scaffold", "none", "unreadable", "origin"]
    pd.DataFrame(
        {
            "file_sha": ids,
            "label": [
                "highly_templated",
                "highly_templated",
                "insufficient_evidence",
                "likely_human",
                "insufficient_evidence",
                "agent_assisted",
            ],
            "templated_basis": ["registry_exact", "cluster", None, None, None, None],
            "conflict": [True, False, False, False, False, False],
            "reasons": [[], [], [], [], [], ["registry_origin"]],
            "flag_disclosed_agent": [True, False, True, False, False, True],
        }
    ).to_parquet(d / "s3_labels_v2_1.parquet", index=False)
    pd.DataFrame({"file_sha": ids, "copies": [3, 1, 1, 1, 1, 1]}).to_parquet(
        d / "s3_labels_v1.parquet",
        index=False,
    )
    pd.DataFrame(
        {
            "file_sha": ids,
            "history_fetched": [True, False, True, True, False, True],
            "ai_trailer_first_ext": [True, False, False, False, False, False],
            "ai_trailer_first": [False] * 6,
            "ai_trailer_last": [False] * 6,
            "ai_trailer_last_ext": [False, False, True, False, False, False],
            "coding_agent_bot": [False, False, False, False, False, True],
            "batch_size": [10, None, 1, 1, None, 1],
        }
    ).to_parquet(d / "s2_commit.parquet", index=False)
    pd.DataFrame(
        {
            "file_sha": ["exact", "near", "scaffold", "none", "origin"],
            "match_kind": ["exact", "none", "none", "none", "exact"],
            "est_jaccard": [1.0, 0.0, 0.0, 0.0, 1.0],
        }
    ).to_parquet(d / "s2_template.parquet", index=False)
    pd.DataFrame(
        {
            "file_sha": ["exact", "near", "scaffold", "none", "none", "origin"],
            "threshold": [0.8, 0.8, 0.8, 0.8, 0.6, 0.8],
            "cluster_size": [3, 2, 1, 1, 50, 1],
        }
    ).to_parquet(d / "s2_clusters.parquet", index=False)
    pd.DataFrame(
        {
            "file_sha": ["exact", "near", "scaffold", "none", "origin"],
            "scaffold_basis": ["shared_lines", None, "placeholder", None, None],
        }
    ).to_parquet(d / "s2_scaffold.parquet", index=False)
    return d


def test_overlapping_signals_and_absence_are_reported_separately(tmp_cfg):
    d = _fixture(tmp_cfg)
    before = (d / "s3_labels_v2_1.parquet").read_bytes()
    result = evidence.run(tmp_cfg)
    frame = pd.read_parquet(result.outputs[0]).set_index("file_sha")
    assert len(frame) == 6
    assert frame.loc["exact", "reuse_signal"] == "exact_content_shared"
    assert frame.loc["exact", "agent_signal"] == "disclosed"
    assert frame.loc["exact", "reuse_and_disclosure"]
    assert frame.loc["exact", "bulk_commit"]
    assert frame.loc["near", "reuse_signal"] == "near_duplicate_signal"
    assert frame.loc["near", "agent_signal"] == "history_unavailable"
    assert frame.loc["scaffold", "reuse_signal"] == "scaffold_signal"
    assert frame.loc["scaffold", "agent_signal"] == "disclosed"
    assert frame.loc["none", "reuse_signal"] == "no_reuse_signal_observed"
    assert frame.loc["none", "agent_signal"] == "no_disclosure_observed"
    assert frame.loc["unreadable", "reuse_signal"] == "reuse_not_assessed"
    assert not frame.loc["origin", "flag_registry_exact"]
    assert frame.loc["origin", "agent_signal"] == "disclosed"
    assert frame.loc["origin", "reuse_signal"] == "no_reuse_signal_observed"
    assert (d / "s3_labels_v2_1.parquet").read_bytes() == before
    matrix = pd.read_csv(result.outputs[1])
    assert len(matrix) == 15
    assert matrix.n.sum() == 6
    assert matrix.share_all.sum() == pytest.approx(1)
    assert matrix.groupby("agent_signal").share_within_agent_signal.sum().eq(1).all()
    assert pd.read_csv(result.outputs[3]).n.sum() == 6
    summary = json.loads(result.outputs[4].read_text())
    assert summary["reuse_and_disclosure"] == 2
    assert summary["legacy_templated_with_disclosure"] == 1
    assert evidence.run(tmp_cfg).skipped


def test_dev_filters_ids_and_records_sample_as_input(tmp_cfg):
    d = _fixture(tmp_cfg)
    pd.DataFrame({"file_sha": ["exact", "unreadable"]}).to_parquet(
        d / "s0_devsample_ids.parquet",
        index=False,
    )
    result = evidence.run(tmp_cfg, dev=True)
    assert len(pd.read_parquet(result.outputs[0])) == 2
    manifest = json.loads(
        result.outputs[0].with_name(result.outputs[0].name + ".manifest.json").read_text()
    )
    assert any("s0_devsample_ids" in name for name in manifest["inputs"])


@pytest.mark.parametrize("source", ["s3_labels_v2_1", "s3_labels_v1", "s2_commit", "s2_scaffold"])
def test_duplicate_keys_are_rejected(tmp_cfg, source):
    d = _fixture(tmp_cfg)
    path = d / f"{source}.parquet"
    data = pd.read_parquet(path)
    pd.concat([data, data.iloc[:1]], ignore_index=True).to_parquet(path, index=False)
    with pytest.raises(ValueError, match="duplicate"):
        evidence.run(tmp_cfg)


def test_missing_coverage_or_inconsistent_disclosure_fails(tmp_cfg):
    d = _fixture(tmp_cfg)
    path = d / "s2_commit.parquet"
    data = pd.read_parquet(path)
    data.iloc[1:].to_parquet(path, index=False)
    with pytest.raises(ValueError, match="coverage"):
        evidence.run(tmp_cfg)
    data.loc[0, "ai_trailer_first_ext"] = False
    data.to_parquet(path, index=False)
    with pytest.raises(ValueError, match="disclosure flags"):
        evidence.run(tmp_cfg)


def test_registry_near_threshold_is_applied(tmp_cfg):
    d = _fixture(tmp_cfg)
    path = d / "s2_template.parquet"
    data = pd.read_parquet(path)
    data.loc[data.file_sha == "none", ["match_kind", "est_jaccard"]] = ["near", 0.75]
    data.to_parquet(path, index=False)
    evidence.run(tmp_cfg)
    assert (
        not pd.read_parquet(d / "s3_evidence.parquet")
        .set_index("file_sha")
        .loc["none", "flag_registry_near"]
    )
    cfg = dataclasses.replace(
        tmp_cfg,
        thresholds=dataclasses.replace(
            tmp_cfg.thresholds,
            templated_jaccard_min=0.7,
        ),
    )
    evidence.run(cfg, force=True)
    assert (
        pd.read_parquet(d / "s3_evidence.parquet")
        .set_index("file_sha")
        .loc["none", "flag_registry_near"]
    )
