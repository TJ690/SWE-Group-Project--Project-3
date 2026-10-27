"""Label rules v2: templated basis, confidence, reasons, and deterministic output."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s0_intake.devsample import output_path as devsample_path
from skillsig.s3_labeling.rules_v2 import LABELS, TEMPLATED_BASIS, label_row, run

_ORIGIN = "origin/repo::demo"


def _feat(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "copies": 1,
        "match_kind": "none",
        "est_jaccard": None,
        "cluster_size": 1,
        "registry_origin": False,
        "history_fetched": True,
        "commit_count": 1,
        "batch_size": 1,
        "noncoding_bot": False,
        "coding_agent_bot": False,
        "ai_trailer_first": False,
        "ai_trailer_last": False,
        "first_author_class": "user",
        "ai_tool_first": None,
        "ai_tool_last": None,
    }
    base.update(overrides)
    return base


def test_constants() -> None:
    assert LABELS == (
        "highly_templated",
        "agent_assisted",
        "likely_human",
        "insufficient_evidence",
    )
    assert TEMPLATED_BASIS == ("registry_exact", "registry_near", "cluster", "copy_count", None)


def test_registry_exact_is_high_confidence(cfg: Config) -> None:
    got = label_row(_feat(match_kind="exact", est_jaccard=1.0), cfg)
    assert got["label"] == "highly_templated"
    assert got["templated_basis"] == "registry_exact"
    assert got["confidence"] == "high"
    assert got["reasons"][0] == "registry_exact"


def test_bulk_batch_is_not_agent_assisted(cfg: Config) -> None:
    got = label_row(
        _feat(batch_size=8, ai_trailer_first=True, ai_tool_first="claude"),
        cfg,
    )
    assert got["label"] != "agent_assisted"
    assert got["label"] == "insufficient_evidence"
    assert got["reasons"] == ["bulk_batch>=6", "ai_trailer_first:claude"]
    assert got["confidence"] == "low"


def test_no_history_single_copy_is_insufficient(cfg: Config) -> None:
    got = label_row(_feat(history_fetched=False, commit_count=None, copies=1), cfg)
    assert got["label"] == "insufficient_evidence"
    assert got["templated_basis"] is None
    assert got["confidence"] == "low"
    assert got["reasons"] == ["no_history"]


def test_registry_origin_is_not_templated_by_registry(cfg: Config) -> None:
    got = label_row(_feat(match_kind="exact", est_jaccard=1.0, registry_origin=True), cfg)
    assert got["label"] == "likely_human"
    assert got["templated_basis"] is None
    assert got["reasons"][0] == "registry_origin"
    copied = label_row(
        _feat(match_kind="exact", est_jaccard=1.0, registry_origin=True, copies=15),
        cfg,
    )
    assert copied["label"] == "highly_templated"
    assert copied["templated_basis"] == "copy_count"
    assert copied["reasons"] == ["registry_origin", "copy_count"]


@pytest.mark.parametrize(
    ("overrides", "basis", "confidence"),
    [
        ({"copies": 11}, "copy_count", "low"),
        ({"copies": 13}, "copy_count", "medium"),
        ({"cluster_size": 10}, "cluster", "low"),
        ({"match_kind": "near", "est_jaccard": 0.85}, "registry_near", "low"),
        ({"match_kind": "near", "est_jaccard": 0.95}, "registry_near", "medium"),
        (
            {"match_kind": "near", "est_jaccard": 0.85, "copies": 20, "cluster_size": 20},
            "registry_near",
            "high",
        ),
        ({"copies": 10, "cluster_size": 10}, "cluster", "high"),
    ],
)
def test_confidence_bands(
    cfg: Config, overrides: dict[str, object], basis: str, confidence: str
) -> None:
    got = label_row(_feat(history_fetched=False, commit_count=None, **overrides), cfg)
    assert got["label"] == "highly_templated"
    assert got["templated_basis"] == basis
    assert got["confidence"] == confidence


def test_agent_signals_and_adjacent_batch(cfg: Config) -> None:
    trailer = _feat(ai_trailer_first=True, ai_tool_first="claude", copies=2)
    assert label_row(trailer, cfg)["confidence"] == "medium"
    both = _feat(
        ai_trailer_first=True,
        ai_tool_first="cursor",
        coding_agent_bot=True,
        first_author_class="coding_agent_bot",
        copies=2,
    )
    assert label_row(both, cfg) == {
        "label": "agent_assisted",
        "templated_basis": None,
        "confidence": "high",
        "reasons": ["coding_agent_bot", "ai_trailer_first:cursor"],
    }
    adjacent = _feat(batch_size=5, ai_trailer_first=True, ai_tool_first="claude", copies=2)
    got = label_row(adjacent, cfg)
    assert got["label"] == "agent_assisted"
    assert got["confidence"] == "low"


def test_wording_and_structure_do_not_change_the_label(cfg: Config) -> None:
    agent = _feat(ai_trailer_first=True, ai_tool_first="claude", copies=2, batch_size=2)
    leaked = {
        **agent,
        "w_imperative_ratio": 1.0,
        "w_phrase_hits": 40.0,
        "headings": 99,
        "body_words": 3,
        "skill_type": "meta",
    }
    assert label_row(leaked, cfg) == label_row(agent, cfg)


def test_reasons_never_empty(cfg: Config) -> None:
    rows = [
        _feat(),
        _feat(history_fetched=False, commit_count=None),
        _feat(match_kind="exact", est_jaccard=1.0),
        _feat(batch_size=8, ai_trailer_first=True, ai_tool_first="claude"),
        _feat(copies=20, cluster_size=4),
        {},
    ]
    for row in rows:
        got = label_row(row, cfg)
        assert got["reasons"]
        assert all(isinstance(reason, str) and reason for reason in got["reasons"])
        assert got["label"] in LABELS
        assert got["confidence"] in {"high", "medium", "low"}
        assert got["templated_basis"] in TEMPLATED_BASIS


def test_label_row_is_deterministic(cfg: Config) -> None:
    row = _feat(match_kind="near", est_jaccard=0.91, copies=14, cluster_size=9)
    assert label_row(row, cfg) == label_row(row, cfg)


def test_thresholds_come_from_config(cfg: Config) -> None:
    tight = dataclasses.replace(
        cfg,
        thresholds=dataclasses.replace(
            cfg.thresholds,
            templated_jaccard_min=0.99,
            templated_copies_min=3,
            bulk_batch_min=4,
            insufficient_conf_floor="medium",
        ),
    )
    near = _feat(match_kind="near", est_jaccard=0.95, history_fetched=False, commit_count=None)
    assert label_row(near, cfg)["templated_basis"] == "registry_near"
    assert label_row(near, tight)["label"] == "insufficient_evidence"
    assert label_row(near, tight)["confidence"] == "medium"
    copied = label_row(_feat(copies=3, history_fetched=False), tight)
    assert copied["templated_basis"] == "copy_count"
    blocked = _feat(batch_size=4, ai_trailer_first=True, ai_tool_first="claude")
    assert label_row(blocked, tight)["label"] == "insufficient_evidence"


def test_run_is_deterministic(tmp_cfg: Config) -> None:
    _write_inputs(tmp_cfg)
    first = run(tmp_cfg)
    assert first.skipped is False
    assert first.rows == {"s3_labels_v2": 6, "s3_v1_v2_crosstab": 4}
    assert first.outputs[0].name == "s3_labels_v2.parquet"
    assert first.outputs[1].name == "s3_v1_v2_crosstab.csv"
    got = _read_labels(first.outputs[0])
    assert list(got) == sorted(got)
    assert set(got) == {
        "agent_both",
        "bulk8",
        "exact_copy",
        "exact_origin",
        "near_only",
        "no_hist",
    }
    assert got["exact_copy"][:3] == ("highly_templated", "registry_exact", "high")
    assert got["exact_origin"][0] == "likely_human"
    assert got["exact_origin"][1] is None
    assert got["exact_origin"][3][0] == "registry_origin"
    assert got["bulk8"][0] != "agent_assisted"
    assert got["no_hist"][0] == "insufficient_evidence"
    assert got["no_hist"][3]
    assert got["near_only"][:3] == ("highly_templated", "registry_near", "low")
    assert got["agent_both"][:3] == ("agent_assisted", None, "high")
    assert all(row[3] for row in got.values())
    assert _columns(first.outputs[0]) == [
        "file_sha",
        "label",
        "templated_basis",
        "confidence",
        "reasons",
        "v1_label",
    ]
    assert _read_crosstab(first.outputs[1]) == [
        ("agent_assisted", "agent_assisted", 1),
        ("insufficient_evidence", "highly_templated", 2),
        ("insufficient_evidence", "insufficient_evidence", 2),
        ("likely_human", "likely_human", 1),
    ]
    assert sum(count for _, _, count in _read_crosstab(first.outputs[1])) == len(got)

    second = run(tmp_cfg)
    assert second.skipped is True
    assert second.rows == first.rows
    assert _read_labels(second.outputs[0]) == got
    forced = run(tmp_cfg, force=True)
    assert forced.skipped is False
    assert _read_labels(forced.outputs[0]) == got


def test_run_dev_keeps_sampled_ids(tmp_cfg: Config) -> None:
    _write_inputs(tmp_cfg)
    _parquet(
        devsample_path(tmp_cfg),
        [{"file_sha": "exact_copy", "stratum": "x"}, {"file_sha": "missing", "stratum": "x"}],
    )
    result = run(tmp_cfg, dev=True)
    got = _read_labels(result.outputs[0])
    assert list(got) == ["exact_copy"]
    assert got["exact_copy"][0] == "highly_templated"


def test_cluster_basis_uses_the_primary_threshold(tmp_cfg: Config) -> None:
    """Only cfg.minhash.lsh_threshold clusters count; the low threshold is for sensitivity."""
    _write_inputs(tmp_cfg)
    primary = float(tmp_cfg.minhash.lsh_threshold)
    low = float(tmp_cfg.minhash.lsh_threshold_low)
    _parquet(
        resolve(tmp_cfg, tmp_cfg.paths.derived) / "s2_clusters.parquet",
        [
            _cluster("no_hist", 1, 1, primary),
            _cluster("no_hist", 2, 50, low),
            _cluster("bulk8", 3, 15, primary),
            _cluster("bulk8", 4, 15, low),
        ],
    )
    got = _read_labels(run(tmp_cfg).outputs[0])
    assert got["no_hist"][0] == "insufficient_evidence"
    assert "cluster" not in got["no_hist"][3]
    assert got["bulk8"][:2] == ("highly_templated", "cluster")


def test_missing_v1_names_command(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match=r"label --only rules_v1"):
        run(tmp_cfg)


def test_missing_template_names_command(tmp_cfg: Config) -> None:
    derived = resolve(tmp_cfg, tmp_cfg.paths.derived)
    for name in (
        "s3_labels_v1.parquet",
        "s2_commit.parquet",
        "s2_repo.parquet",
        "s2_structure.parquet",
        "s2_wording.parquet",
    ):
        _parquet(derived / name, [{"file_sha": "only"}])
    with pytest.raises(FileNotFoundError, match=r"features --only template_sim"):
        run(tmp_cfg)


def _write_inputs(cfg: Config) -> None:
    derived = resolve(cfg, cfg.paths.derived)
    _parquet(
        derived / "s3_labels_v1.parquet",
        [
            {"file_sha": "exact_copy", "label": "insufficient_evidence", "copies": 1},
            {"file_sha": "exact_origin", "label": "likely_human", "copies": 1},
            {"file_sha": "bulk8", "label": "insufficient_evidence", "copies": 1},
            {"file_sha": "no_hist", "label": "insufficient_evidence", "copies": 1},
            {"file_sha": "near_only", "label": "insufficient_evidence", "copies": 1},
            {"file_sha": "agent_both", "label": "agent_assisted", "copies": 2},
        ],
    )
    _parquet(
        derived / "s2_commit.parquet",
        [
            _commit("exact_copy"),
            _commit("exact_origin"),
            _commit("bulk8", batch_size=8, ai_trailer_first=True, ai_tool_first="claude"),
            _commit("near_only", history_fetched=False, commit_count=None, batch_size=None),
            _commit(
                "agent_both",
                coding_agent_bot=True,
                ai_trailer_first=True,
                ai_tool_first="cursor",
                first_author_class="coding_agent_bot",
                batch_size=1,
            ),
        ],
    )
    _parquet(
        derived / "s2_template.parquet",
        [
            _template("exact_copy", "exact", _ORIGIN, 1.0),
            _template("exact_origin", "exact", _ORIGIN, 1.0),
            _template("near_only", "near", "other/lineage::demo", 0.85),
        ],
    )
    _parquet(
        derived / "s2_clusters.parquet",
        [
            {
                "file_sha": "unused",
                "cluster_id": 1,
                "cluster_size": 1,
                "threshold": 0.8,
                "nn_sim_est": 0.0,
            }
        ],
    )
    unused = ("s2_repo.parquet", "s2_structure.parquet", "s2_wording.parquet", "s2_types.parquet")
    for name in unused:
        _parquet(derived / name, [{"file_sha": "unused"}])
    artifacts = resolve(cfg, cfg.paths.raw) / "artifacts" / "artifacts.parquet"
    _parquet(
        artifacts,
        [
            _artifact("exact_copy", "other/repo"),
            _artifact("exact_origin", "origin/repo"),
            _artifact("bulk8", "owner/bulk"),
            _artifact("no_hist", "owner/none"),
            _artifact("near_only", "owner/near"),
            _artifact("agent_both", "owner/agent"),
        ],
    )


def _commit(file_sha: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "file_sha": file_sha,
        "history_fetched": True,
        "commit_count": 1,
        "batch_size": 1,
        "ai_trailer_first": False,
        "ai_trailer_last": False,
        "ai_tool_first": None,
        "ai_tool_last": None,
        "first_author_class": "user",
        "coding_agent_bot": False,
        "noncoding_bot": False,
    }
    row.update(overrides)
    return row


def _template(file_sha: str, kind: str, lineage: str, score: float) -> dict[str, object]:
    return {
        "file_sha": file_sha,
        "match_kind": kind,
        "nearest_blob": "blob",
        "nearest_lineage_id": lineage,
        "est_jaccard": score,
    }


def _cluster(file_sha: str, cluster_id: int, size: int, threshold: float) -> dict[str, object]:
    return {
        "file_sha": file_sha,
        "cluster_id": cluster_id,
        "cluster_size": size,
        "threshold": threshold,
        "nn_sim_est": None,
    }


def _artifact(file_sha: str, repo: str) -> dict[str, object]:
    return {"file_sha": file_sha, "repo_full_name": repo, "dedup_primary": 1}


def _parquet(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")


def _read_labels(path: Path) -> dict[str, tuple[str, str | None, str, list[str], str]]:
    con = duckdb.connect()
    try:
        fetched = con.execute(
            """
            SELECT file_sha, label, templated_basis, confidence, reasons, v1_label
            FROM read_parquet(?)
            ORDER BY file_sha
            """,
            [path.as_posix()],
        ).fetchall()
    finally:
        con.close()
    return {
        sha: (label, basis, confidence, list(reasons), v1_label)
        for sha, label, basis, confidence, reasons, v1_label in fetched
    }


def _read_crosstab(path: Path) -> list[tuple[str, str, int]]:
    con = duckdb.connect()
    try:
        fetched = con.execute(
            """
            SELECT v1_label, v2_label, n
            FROM read_csv(?, header = true)
            ORDER BY 1, 2
            """,
            [path.as_posix()],
        ).fetchall()
    finally:
        con.close()
    return [(v1, v2, int(count)) for v1, v2, count in fetched]


def _columns(path: Path) -> list[str]:
    con = duckdb.connect()
    try:
        names = con.execute(
            "SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet(?))",
            [path.as_posix()],
        ).fetchall()
    finally:
        con.close()
    return [name for (name,) in names]
