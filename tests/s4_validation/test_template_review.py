"""Evidence-aware review uses actual paired content, not circular labels or silent missing text."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from skillsig.paths import resolve
from skillsig.s4_validation import template_review as tr


def test_exact_pair_jaccard_uses_whole_body_and_handles_missing(cfg):
    prefix = " ".join(f"word{i}" for i in range(2000))
    assert tr.pair_metrics(prefix, prefix, cfg)["body_jaccard"] == 1
    changed = prefix + " " + " ".join(f"other{i}" for i in range(2000))
    assert tr.pair_metrics(prefix, changed, cfg)["body_jaccard"] < 0.51
    assert tr.pair_metrics(prefix, None, cfg)["body_jaccard"] is None
    assert not tr.pair_metrics(prefix, None, cfg)["pair_available"]


def test_render_escapes_source_text_and_hides_prediction(cfg):
    text = "<script>alert('unsafe')</script>"
    case = {
        "pair_id": "P001",
        "left_sha": "a",
        "right_sha": "b",
        "left_repo": "x/y",
        "right_repo": "u/v",
        "left_text": text,
        "right_text": text,
        "metrics": tr.pair_metrics(text, text, cfg),
        "stratum": "near_registry",
        "legacy_label": "highly_templated",
    }
    page = tr.render([case])
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "near_registry" not in page and "highly_templated" not in page
    assert "exact normalized-body shingle Jaccard" in page


def test_registry_text_is_a_verified_local_object_without_fetch(tmp_cfg, monkeypatch):
    payload = b"actual registry content"
    blob = hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()
    clone = resolve(tmp_cfg, tmp_cfg.paths.registry_repos) / "x__y"
    (clone / ".git").mkdir(parents=True)
    calls = []

    def fake(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=payload)

    monkeypatch.setattr(tr.subprocess, "run", fake)
    assert tr.local_blob(tmp_cfg, "x/y", blob) == payload.decode()
    assert calls[0][1]["env"]["GIT_NO_LAZY_FETCH"] == "1"
    assert calls[0][0][-3:] == ["cat-file", "blob", blob]
    assert tr.local_blob(tmp_cfg, "../bad", blob) is None
    with pytest.raises(ValueError, match="identity"):
        tr.local_blob(tmp_cfg, "x/y", "0" * 40)


def test_pair_scoring_preserves_uncertain_and_missing_answers():
    key = pd.DataFrame(
        {
            "pair_id": ["P1", "P2", "P3"],
            "stratum": ["near_registry"] * 3,
            "pair_available": [True, True, False],
        }
    )
    answers = pd.DataFrame(
        {
            "pair_id": ["P1", "P3"],
            "material_reuse": ["yes", "uncertain"],
            "shared_scaffold": ["yes", "uncertain"],
        }
    )
    result = tr.score(key, answers).set_index("judgment")
    assert result.loc["material_reuse", "yes"] == 1
    assert result.loc["material_reuse", "uncertain"] == 1
    assert result.loc["material_reuse", "unanswered"] == 1
    assert result.loc["material_reuse", "sample_n"] == 3
    answers.loc[1, "material_reuse"] = "no"
    with pytest.raises(ValueError, match="comparison text"):
        tr.score(key, answers)
    with pytest.raises(ValueError, match="duplicate"):
        tr.score(key, pd.concat([answers, answers.iloc[:1]]))


def _inputs(cfg):
    d = resolve(cfg, cfg.paths.derived)
    d.mkdir(parents=True)
    ids = ["e", "n", "c", "s", "p", "q"]
    pd.DataFrame(
        {
            "file_sha": ids,
            "label": ["highly_templated"] * 6,
            "templated_basis": ["registry_exact", "registry_near", "cluster", None, None, None],
            "reasons": [["x"]] * 6,
        }
    ).to_parquet(d / "s3_labels_v2_1.parquet", index=False)
    pd.DataFrame({"file_sha": ids, "copies": [10] * 6}).to_parquet(
        d / "s3_labels_v1.parquet", index=False
    )
    pd.DataFrame(
        {
            "file_sha": ids,
            "owner": ids,
            "scaffold_basis": [None, None, None, "outline", None, None],
            "placeholder_generator": [None] * 6,
            "masked_sha": [None] * 6,
            "outline_family_id": [1, 2, 3, 4, 3, 4],
        }
    ).to_parquet(d / "s2_scaffold.parquet")
    pd.DataFrame(
        {
            "file_sha": ids,
            "cluster_id": [1, 2, 3, 4, 3, 5],
            "cluster_size": [1, 1, 10, 1, 10, 1],
            "threshold": [0.8] * 6,
        }
    ).to_parquet(d / "s2_clusters.parquet")
    pd.DataFrame(
        {
            "file_sha": ids,
            "match_kind": ["exact", "near", "none", "none", "none", "none"],
            "est_jaccard": [1.0, 0.9, 0.0, 0.0, 0.0, 0.0],
            "nearest_blob": ["registry_e", "registry_n", None, None, None, None],
            "nearest_lineage_id": ["x/y::e", "x/y::n", None, None, None, None],
        }
    ).to_parquet(d / "s2_template.parquet")
    raw = resolve(cfg, cfg.paths.raw) / "artifacts"
    raw.mkdir(parents=True)
    pd.DataFrame(
        {
            "file_sha": ids,
            "content": ["a shared passage with enough words " + x for x in ids],
            "repo_full_name": [x + "/repo" for x in ids],
            "dedup_primary": [1] * 6,
        }
    ).to_parquet(raw / "part.parquet")


def test_draw_pair_sources_and_preserve_existing_review(tmp_cfg):
    _inputs(tmp_cfg)
    cfg = dataclasses.replace(
        tmp_cfg, sample=dataclasses.replace(tmp_cfg.sample, similarity_pairs=4)
    )
    con = duckdb.connect()
    try:
        tr._population(con, cfg)
        first, design = tr.draw(con, cfg)
        second, _ = tr.draw(con, cfg)
        assert first.file_sha.tolist() == second.file_sha.tolist()
        assert set(first.stratum) == set(tr.STRATA)
        by = first.set_index("stratum")
        assert by.loc["cluster_candidate", "partner_sha"] == "p"
        assert by.loc["scaffold_candidate", "partner_sha"] == "q"
        assert all(x["shortfall"] == 0 for x in design.values())
    finally:
        con.close()
    result = tr.run(cfg)
    summary = json.loads(result.outputs[-1].read_text())
    assert summary["pairs"] == 4 and summary["pairs_with_text"] == 2
    evidence = json.loads(result.outputs[3].read_text())
    # A missing registry object must not be replaced by a copy of the subject itself.
    assert all(x["right_text"] is None for x in evidence if x["right_sha"].startswith("registry"))
    sheet = result.outputs[0]
    assert "stratum" not in pd.read_csv(sheet).columns
    before = sheet.read_bytes()
    assert tr.run(cfg, force=True).skipped
    assert sheet.read_bytes() == before


def test_shared_line_partner_uses_actual_overlap_and_exposes_missing_parts(tmp_cfg):
    _inputs(tmp_cfg)
    sample = pd.DataFrame(
        {
            "file_sha": ["s"],
            "stratum": ["scaffold_candidate"],
            "scaffold_basis": ["shared_lines"],
            "partner_sha": ["p"],
        }
    )
    con = duckdb.connect()
    try:
        tr._population(con, tmp_cfg)
        tr.improve_shared_line_partners(con, tmp_cfg, sample)
        assert sample.partner_sha.isna().all()
        parts = resolve(tmp_cfg, tmp_cfg.paths.derived) / "s2_scaffold_parts"
        parts.mkdir()
        pd.DataFrame(
            {
                "file_sha": ["s", "s", "p", "q", "q"],
                "owner": ["s", "s", "p", "q", "q"],
                "line_hash": [1, 2, 1, 1, 2],
            }
        ).to_parquet(parts / "lines_00000.parquet")
        tr.improve_shared_line_partners(con, tmp_cfg, sample)
        assert sample.partner_sha.tolist() == ["q"]
    finally:
        con.close()
