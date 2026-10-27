"""Label rules v2.1: trailer and human-confidence overrides, independent flags, run()."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s0_intake.devsample import output_path as devsample_path
from skillsig.s3_labeling.rules_v2_1 import label_row_v21, run


def _v2(label: str = "insufficient_evidence", **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "label": label,
        "templated_basis": None,
        "confidence": "low",
        "reasons": ["no_agent_signal"],
    }
    row.update(overrides)
    return row


def _feat(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "history_fetched": True,
        "commit_count": 1,
        "span_days": 0,
        "batch_size": 1,
        "first_commit_at": "2026-02-01T00:00:00Z",
        "ai_trailer_first": False,
        "ai_trailer_last": False,
        "ai_trailer_first_ext": False,
        "ai_trailer_last_ext": False,
        "coding_agent_bot": False,
        "noncoding_bot": False,
        "first_author_class": "user",
        "copies": 1,
        "filename": "SKILL.md",
        "body_chars": 1500,
    }
    row.update(overrides)
    return row


def test_templated_with_trailer_stays_templated_and_conflicts(cfg: Config) -> None:
    got = label_row_v21(
        _v2(
            "highly_templated",
            templated_basis="registry_near",
            confidence="medium",
            reasons=["registry_near"],
        ),
        _feat(ai_trailer_first=True, ai_trailer_first_ext=True),
        cfg,
    )
    assert got["label"] == "highly_templated"
    assert got["flag_templated"] is True
    assert got["flag_disclosed_agent"] is True
    assert got["conflict"] is True
    assert got["confidence"] == "low"
    assert got["reasons"][-1] == "conflict:templated+disclosed"


def test_bulk_trailer_becomes_agent_low(cfg: Config) -> None:
    got = label_row_v21(
        _v2(reasons=["bulk_batch>=6", "ai_trailer_first:claude"]),
        _feat(batch_size=8, ai_trailer_first=True, ai_trailer_first_ext=True),
        cfg,
    )
    assert (got["label"], got["confidence"]) == ("agent_assisted", "low")
    assert got["reasons"][0] == "trailer_in_bulk"
    assert "ai_trailer_first:claude" in got["reasons"]


def test_last_only_trailer(cfg: Config) -> None:
    got = label_row_v21(
        _v2(), _feat(batch_size=2, ai_trailer_last=True, ai_trailer_last_ext=True), cfg
    )
    assert got["label"] == "agent_assisted"
    assert got["reasons"][0] == "trailer_last_only"


def test_ext_only_trailer(cfg: Config) -> None:
    got = label_row_v21(_v2(), _feat(batch_size=2, ai_trailer_first_ext=True), cfg)
    assert got["label"] == "agent_assisted"
    assert got["reasons"][0] == "trailer_ext_only"


def test_noncoding_bot_trailer_is_unchanged(cfg: Config) -> None:
    got = label_row_v21(
        _v2(reasons=["noncoding_bot"]),
        _feat(
            batch_size=8,
            noncoding_bot=True,
            first_author_class="noncoding_bot",
            ai_trailer_first=True,
            ai_trailer_first_ext=True,
        ),
        cfg,
    )
    assert got["label"] == "insufficient_evidence"
    assert got["flag_disclosed_agent"] is True


def test_no_history_is_unchanged(cfg: Config) -> None:
    feat = _feat(
        history_fetched=False,
        commit_count=None,
        span_days=None,
        batch_size=None,
        first_author_class=None,
        ai_trailer_first=None,
        ai_trailer_last=None,
        ai_trailer_first_ext=None,
        ai_trailer_last_ext=None,
        coding_agent_bot=None,
        noncoding_bot=None,
        first_commit_at=None,
    )
    got = label_row_v21(_v2(reasons=["no_history"]), feat, cfg)
    assert got["label"] == "insufficient_evidence"
    assert got["flag_disclosed_agent"] is False
    assert got["human_signal_count"] == 1  # copies == 1 only
    assert got["filter_pre_format"] is False


def test_human_revised_is_medium(cfg: Config) -> None:
    got = label_row_v21(
        _v2("likely_human", confidence="medium", reasons=["batch_size=1"]),
        _feat(commit_count=3, span_days=10),
        cfg,
    )
    assert (got["label"], got["confidence"]) == ("likely_human", "medium")
    assert got["revised"] is True
    assert got["reasons"][-1] == "revised"
    assert got["human_signal_count"] == 5


def test_human_single_commit_is_low(cfg: Config) -> None:
    got = label_row_v21(
        _v2("likely_human", confidence="medium", reasons=["batch_size=1"]), _feat(), cfg
    )
    assert (got["label"], got["confidence"]) == ("likely_human", "low")
    assert got["reasons"][-1] == "not_revised"


def test_human_many_commits_short_span_is_low(cfg: Config) -> None:
    got = label_row_v21(
        _v2("likely_human", confidence="medium"), _feat(commit_count=5, span_days=2), cfg
    )
    assert got["confidence"] == "low"


def test_v2_agent_passes_through(cfg: Config) -> None:
    v2 = _v2("agent_assisted", confidence="high", reasons=["coding_agent_bot"])
    got = label_row_v21(
        v2, _feat(coding_agent_bot=True, first_author_class="coding_agent_bot"), cfg
    )
    assert (got["label"], got["confidence"], got["reasons"]) == (
        "agent_assisted",
        "high",
        ["coding_agent_bot"],
    )


@pytest.mark.parametrize(
    ("overrides", "column", "expected"),
    [
        ({"body_chars": 150}, "filter_short", True),
        ({"body_chars": None}, "filter_short", True),
        ({"body_chars": 200}, "filter_short", False),
        ({"filename": "skill.md"}, "filter_case_variant", True),
        ({"filename": None}, "filter_case_variant", False),
        ({"first_commit_at": "2025-06-01T00:00:00Z"}, "filter_pre_format", True),
        ({"first_commit_at": "2025-10-01T00:00:00Z"}, "filter_pre_format", False),
    ],
)
def test_filters(cfg: Config, overrides: dict, column: str, expected: bool) -> None:
    assert label_row_v21(_v2(), _feat(**overrides), cfg)[column] is expected


def test_string_booleans_are_read(cfg: Config) -> None:
    got = label_row_v21(_v2(), _feat(batch_size=8, ai_trailer_first="true"), cfg)
    assert got["label"] == "agent_assisted"


def test_reasons_never_empty(cfg: Config) -> None:
    got = label_row_v21(_v2(reasons=[]), _feat(), cfg)
    assert got["reasons"]


def test_thresholds_come_from_config(cfg: Config) -> None:
    import dataclasses

    strict = dataclasses.replace(
        cfg, thresholds=dataclasses.replace(cfg.thresholds, human_min_commits=10)
    )
    v2 = _v2("likely_human", confidence="medium")
    feat = _feat(commit_count=5, span_days=30)
    assert label_row_v21(v2, feat, cfg)["confidence"] == "medium"
    assert label_row_v21(v2, feat, strict)["confidence"] == "low"


def test_run_writes_labels_and_crosstab(tmp_cfg: Config) -> None:
    _write_inputs(tmp_cfg)
    first = run(tmp_cfg)
    assert first.skipped is False
    assert first.rows["s3_labels_v2_1"] == 4
    got = _read(first.outputs[0])
    assert list(got) == sorted(got)
    assert got["bulk"]["label"] == "agent_assisted"
    assert got["bulk"]["label_v2"] == "insufficient_evidence"
    assert got["human"]["confidence"] == "low"
    assert got["tmpl"]["conflict"] is True
    assert got["nohist"]["label"] == "insufficient_evidence"
    assert got["nohist"]["label_v1"] == "insufficient_evidence"
    assert all(row["reasons"] for row in got.values())
    assert first.outputs[1].name == "s3_v2_v21_crosstab.csv"
    second = run(tmp_cfg)
    assert second.skipped is True
    assert second.rows == first.rows


def test_run_dev_keeps_sampled_ids(tmp_cfg: Config) -> None:
    _write_inputs(tmp_cfg)
    _parquet(devsample_path(tmp_cfg), [{"file_sha": "bulk", "stratum": "x"}])
    assert list(_read(run(tmp_cfg, dev=True).outputs[0])) == ["bulk"]


def test_missing_v2_names_command(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match=r"label --only rules_v2"):
        run(tmp_cfg)


def _write_inputs(cfg: Config) -> None:
    derived = resolve(cfg, cfg.paths.derived)
    v2 = [
        ("bulk", "insufficient_evidence", None, "low", ["bulk_batch>=6"]),
        ("human", "likely_human", None, "medium", ["batch_size=1"]),
        ("tmpl", "highly_templated", "copy_count", "medium", ["copy_count"]),
        ("nohist", "insufficient_evidence", None, "low", ["no_history"]),
    ]
    _parquet(
        derived / "s3_labels_v2.parquet",
        [
            {
                "file_sha": s,
                "label": lab,
                "templated_basis": b,
                "confidence": c,
                "reasons": r,
                "v1_label": lab,
            }
            for s, lab, b, c, r in v2
        ],
    )
    _parquet(
        derived / "s3_labels_v1.parquet",
        [{"file_sha": s, "label": "insufficient_evidence", "copies": 1} for s, *_ in v2],
    )
    commit = [
        _commit("bulk", batch_size=8, ai_trailer_first=True, ai_trailer_first_ext=True),
        _commit("human"),
        _commit("tmpl", ai_trailer_first=True, ai_trailer_first_ext=True),
        _commit("nohist", history_fetched=False, commit_count=None, batch_size=None),
    ]
    _parquet(derived / "s2_commit.parquet", commit)
    _parquet(
        resolve(cfg, cfg.paths.raw) / "artifacts" / "part-0.parquet",
        [
            {"file_sha": s, "dedup_primary": 1, "filename": "SKILL.md", "body_chars": 900}
            for s, *_ in v2
        ],
    )


def _commit(file_sha: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "file_sha": file_sha,
        "history_fetched": True,
        "commit_count": 1,
        "span_days": 0,
        "batch_size": 1,
        "first_commit_at": "2026-01-01T00:00:00Z",
        "ai_trailer_first": False,
        "ai_trailer_last": False,
        "ai_trailer_first_ext": False,
        "ai_trailer_last_ext": False,
        "coding_agent_bot": False,
        "noncoding_bot": False,
        "first_author_class": "user",
    }
    row.update(overrides)
    return row


def _parquet(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def _read(path: Path) -> dict[str, dict[str, object]]:
    con = duckdb.connect()
    try:
        cur = con.execute("SELECT * FROM read_parquet(?) ORDER BY file_sha", [path.as_posix()])
        header = [d[0] for d in cur.description]
        return {r[0]: dict(zip(header, r, strict=True)) for r in cur.fetchall()}
    finally:
        con.close()
