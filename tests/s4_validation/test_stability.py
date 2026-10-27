"""E5 threshold stability."""

from __future__ import annotations

import pandas as pd
import pytest

from skillsig.config import Config
from skillsig.s4_validation.stability import relabel, settings, shares


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "file_sha": "x",
        "copies": 1,
        "match_kind": "none",
        "est_jaccard": None,
        "cluster_size": 1,
        "cluster_size_low": 1,
        "registry_origin": False,
        "history_fetched": True,
        "commit_count": 1,
        "span_days": 0,
        "batch_size": 1,
        "first_commit_at": "2026-01-01T00:00:00Z",
        "noncoding_bot": False,
        "coding_agent_bot": False,
        "ai_trailer_first": False,
        "ai_trailer_last": False,
        "ai_trailer_first_ext": False,
        "ai_trailer_last_ext": False,
        "first_author_class": "user",
        "ai_tool_first": None,
        "ai_tool_last": None,
        "filename": "SKILL.md",
        "body_chars": 900,
    }
    row.update(overrides)
    return row


def test_settings_include_default_and_skip_unchanged(cfg: Config) -> None:
    got = settings(cfg)
    assert got[0][0] == "default"
    params = [p for p, *_ in got]
    assert params.count("templated_copies_min") == 2
    assert ("cluster_threshold", 0.6) in [(p, v) for p, v, *_ in got]


def test_copy_threshold_moves_templated(cfg: Config) -> None:
    frame = pd.DataFrame([_row(copies=9), _row(copies=11)])
    by = {p: (c, col) for p, v, c, col in settings(cfg) if p in ("default",) or v == 8}
    default = relabel(frame, by["default"][0])
    lower = relabel(frame, by["templated_copies_min"][0])
    assert default.count("highly_templated") == 1
    assert lower.count("highly_templated") == 2


def test_low_cluster_column_is_used(cfg: Config) -> None:
    frame = pd.DataFrame([_row(cluster_size=2, cluster_size_low=40)])
    assert relabel(frame, cfg)[0] != "highly_templated"
    assert relabel(frame, cfg, cluster_column="cluster_size_low")[0] == "highly_templated"


def test_nan_values_are_treated_as_missing(cfg: Config) -> None:
    frame = pd.DataFrame([_row(history_fetched=None, commit_count=float("nan"), batch_size=None)])
    assert relabel(frame, cfg) == ["insufficient_evidence"]


def test_shares_sum_to_one() -> None:
    got = shares(["likely_human", "agent_assisted", "agent_assisted", "highly_templated"])
    assert sum(got.values()) == pytest.approx(1.0)
    assert got["agent_assisted"] == 0.5
