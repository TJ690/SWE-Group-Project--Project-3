"""E5 text-method comparison."""

from __future__ import annotations

import numpy as np

from skillsig.s3_labeling.text_methods import (
    FEATURES,
    clean_text,
    decided_share,
    has_disclosure,
    rule_scores,
)


def test_features_exclude_commit_and_date() -> None:
    assert "months_since_format" not in FEATURES
    assert "marker_rate" not in FEATURES


def test_clean_text_drops_front_matter_and_vendor_names() -> None:
    text = "---\nname: x\n---\nUse Claude or Cursor to ship it."
    out = clean_text(text)
    assert "name:" not in out
    assert "Claude" not in out and "Cursor" not in out
    assert "ship it" in out


def test_rule_score_orders_llm_style_above_casual_style() -> None:
    llm = (
        "## Overview\n- **Goal**: ensure a comprehensive, robust workflow — seamlessly.\n"
        "- **Rule**: leverage best practices ✅\n"
    )
    casual = "I just run the script. it's quick, don't overthink it! we use this daily."
    assert rule_scores(llm)["ai_minus_human"] > rule_scores(casual)["ai_minus_human"]


def test_has_disclosure() -> None:
    assert has_disclosure("Generated with [Claude Code](https://claude.com)")
    assert has_disclosure("Co-Authored-By: Claude <noreply@anthropic.com>")
    assert not has_disclosure("Use this skill to generate a changelog.")


def test_decided_share_perfect_and_random() -> None:
    y = np.array([1] * 50 + [0] * 50)
    perfect = np.arange(100, 0, -1).astype(float)
    assert decided_share(y, perfect, 0.9) == (1.0, 1.0)
    flat = np.zeros(100)
    agent, human = decided_share(y, flat, 0.9)
    assert agent == 0.0 and human == 0.0


def test_decided_share_reweights_unbalanced_classes() -> None:
    # 90 agent, 10 human, no signal: at a 50/50 prior nothing reaches 80% precision.
    y = np.array([1] * 90 + [0] * 10)
    rng = np.random.default_rng(0)
    agent, _ = decided_share(y, rng.random(100), 0.8)
    assert agent < 0.2
