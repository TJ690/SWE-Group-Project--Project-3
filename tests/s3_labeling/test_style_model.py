"""E4 PU style model."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from skillsig.config import Config
from skillsig.s3_labeling import style_model
from skillsig.s3_labeling.style_model import (
    MODELS,
    STRUCTURE,
    TEXT_FEATURES,
    WORDING,
    cross_validate,
    elkan_noto_c,
    fit_fold,
    prepare,
    summarise,
)


def _frame(n: int = 600, signal: float = 3.0, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    s = rng.random(n) < 0.35
    data = {
        "file_sha": [f"{i:040x}" for i in range(n)],
        "s": s,
        "owner": [f"o{i % 120}" for i in range(n)],
        "cluster_id": [float(i) for i in range(n)],
        "first_commit_at": ["2026-03-01T00:00:00Z"] * n,
        "ai_tool_first_ext": np.where(s, "claude", None),
        "ai_tool_last_ext": [None] * n,
    }
    for col in (*STRUCTURE, *WORDING):
        data[col] = rng.normal(size=n)
    data["specificity"] = np.where(rng.random(n) < 0.2, np.nan, rng.random(n))
    data["n_identifiers"] = rng.integers(0, 30, n)
    data["w_imperative_ratio"] = data["w_imperative_ratio"] + signal * s
    words = np.where(s, "please ensure comprehensive steps", "please run the steps")
    data["text"] = [f"{w} item{i % 7} " * 3 for i, w in enumerate(words)]
    frame = pd.DataFrame(data)
    for col in style_model._COUNTS:
        frame[col] = frame[col].abs()
    return prepare(frame)


def test_text_model_never_uses_commit_or_date() -> None:
    for name in ("text_only", "text_no_markers"):
        assert "months_since_format" not in MODELS[name]
        assert not any(c.startswith(("ai_", "first_", "batch")) for c in MODELS[name])
    assert "marker_rate" in TEXT_FEATURES
    assert "marker_rate" not in MODELS["text_no_markers"]
    assert not set(style_model.NEW_FEATURES) & set(MODELS["text_without_new"])
    assert "specificity" in MODELS["text_only"]


def test_separable_feature_gives_high_auc(cfg: Config) -> None:
    frame = _frame(signal=4.0)
    oof, folds = cross_validate(frame, cfg)
    pooled = style_model._auc(oof["s"], oof["g_text_only"])
    assert pooled > 0.95
    assert (oof["fold"] >= 0).all()
    assert oof["style_score"].max() <= 1.0


def test_random_features_near_chance(cfg: Config) -> None:
    frame = _frame(signal=0.0, seed=3)
    frame["text"] = "please run the steps"  # no lexical signal either
    oof, _ = cross_validate(frame, cfg)
    assert 0.4 <= style_model._auc(oof["s"], oof["g_text_only"]) <= 0.6


def test_lexicon_fitted_on_training_rows_only(cfg: Config, monkeypatch) -> None:
    frame = _frame(n=300)
    seen: list[int] = []
    real = style_model.fit_lexicon

    def spy(pos, neg, **kw):
        seen.append(len(pos) + len(neg))
        return real(pos, neg, **kw)

    monkeypatch.setattr(style_model, "fit_lexicon", spy)
    cross_validate(frame, cfg)
    assert len(seen) == 5
    assert all(count < len(frame) for count in seen)


def test_elkan_noto_constant_model() -> None:
    class Half:
        def predict_proba(self, x):
            return np.full((len(x), 2), 0.5)

    pos = pd.DataFrame({"a": [1.0, 2.0]})
    assert elkan_noto_c(Half(), pos, ["a"]) == 0.5
    assert np.isnan(elkan_noto_c(Half(), pos.iloc[:0], ["a"]))


def test_summary_has_usefulness_and_note(cfg: Config) -> None:
    frame = _frame(signal=4.0)
    oof, folds = cross_validate(frame, cfg)
    table, summary = summarise(frame, oof, folds, cfg)
    assert set(table["model"]) == set(MODELS)
    assert "lower bound" in summary["note"]
    assert 0.0 <= summary["recall_at_theta_hi"] <= 1.0
    assert summary["recall_at_theta_hi"] == pytest.approx(0.8, abs=0.05)
    assert isinstance(summary["style_useful"], bool)
    assert set(summary["single_feature_auc"]) == set(style_model.NEW_FEATURES)


def test_fit_fold_returns_pipeline(cfg: Config) -> None:
    frame = _frame(n=200)
    frame["marker_rate"] = 0.0
    model = fit_fold(frame, ["w_imperative_ratio"], 1)
    assert model.predict_proba(frame[["w_imperative_ratio"]].to_numpy()).shape == (200, 2)
