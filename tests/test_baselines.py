import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import baselines as b  # noqa: E402

CLAUDE_MSG = "Add skill\n\nCo-authored-by: Claude <noreply@anthropic.com>"


def frame(**cols):
    """Tiny input frame; unspecified columns default to 'no signal'."""
    n = len(next(iter(cols.values())))
    base = {
        "file_sha": [f"sha{i}" for i in range(n)],
        "copies": [1] * n,
        "agent_folder": ["No agent folder"] * n,
        "history_fetched": [0] * n,
        "first_commit_message": [None] * n,
        "is_bulk": [False] * n,
        "repo_created_at": [None] * n,
    }
    base.update(cols)
    return pd.DataFrame(base)


def test_copy_count_boundary():
    df = frame(copies=[9, 10, 11])
    assert list(b.copy_count(df)) == [b.INSUFFICIENT, b.TEMPLATED, b.TEMPLATED]


def test_agent_folder_named_tool_only():
    df = frame(agent_folder=["Claude Code", "Cursor", "Shared", "No agent folder", "Other dot-folder", None])
    expected = [b.AGENT, b.AGENT, b.AGENT, b.INSUFFICIENT, b.INSUFFICIENT, b.INSUFFICIENT]
    assert list(b.agent_folder(df)) == expected


@pytest.mark.parametrize(
    "message,expected",
    [
        (CLAUDE_MSG, True),
        ("x\n\nco-authored-by: GitHub Copilot <c@github.com>", True),
        ("x\n\nCo-authored-by: Jane Doe <jane@example.com>", False),
        ("Mentions claude in the subject line only", False),
        ("", False),
        (None, False),
        (np.nan, False),
    ],
)
def test_has_ai_trailer(message, expected):
    assert b.has_ai_trailer(message) is expected


def test_trailer_needs_history():
    df = frame(history_fetched=[1, 0], first_commit_message=[CLAUDE_MSG, CLAUDE_MSG])
    assert list(b.trailer(df)) == [b.AGENT, b.INSUFFICIENT]


def test_trailer_bulk_exclusion():
    df = frame(
        history_fetched=[1, 1],
        first_commit_message=[CLAUDE_MSG, CLAUDE_MSG],
        is_bulk=[False, True],
    )
    assert list(b.trailer(df)) == [b.AGENT, b.AGENT]
    assert list(b.trailer(df, exclude_bulk=True)) == [b.AGENT, b.INSUFFICIENT]


def test_trailer_missing_bulk_flag_is_not_bulk():
    df = frame(history_fetched=[1], first_commit_message=[CLAUDE_MSG], is_bulk=[np.nan])
    assert list(b.trailer(df, exclude_bulk=True)) == [b.AGENT]


def test_repo_date_boundary_and_missing():
    df = frame(
        repo_created_at=["2025-09-30T23:59:59Z", "2025-10-01T00:00:00Z", "2025-10-02T00:00:00Z", None, "not a date"]
    )
    expected = [b.HUMAN, b.INSUFFICIENT, b.INSUFFICIENT, b.INSUFFICIENT, b.INSUFFICIENT]
    assert list(b.repo_date(df)) == expected


def test_run_all_shape_and_labels():
    df = frame(copies=[1, 12], agent_folder=["Claude Code", "No agent folder"])
    out = b.run_all(df)
    assert list(out.columns) == ["file_sha", "baseline", "label"]
    assert len(out) == len(df) * len(b.BASELINES)
    assert set(out["baseline"]) == set(b.BASELINES)
    assert set(out["label"]) <= set(b.LABELS)


def test_no_baseline_crashes_on_all_missing_values():
    df = frame(
        copies=[np.nan],
        agent_folder=[None],
        history_fetched=[np.nan],
        first_commit_message=[None],
        is_bulk=[np.nan],
        repo_created_at=[None],
    )
    out = b.run_all(df)
    assert set(out["label"]) == {b.INSUFFICIENT}
