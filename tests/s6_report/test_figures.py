"""Report figures: the shares and curves they compute, and that each chart draws."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s6_report import figures as fig

_V1_V2 = pd.DataFrame(
    {
        "v1_label": [
            "highly_templated",
            "insufficient_evidence",
            "insufficient_evidence",
            "agent_assisted",
            "likely_human",
        ],
        "v2_label": [
            "highly_templated",
            "highly_templated",
            "insufficient_evidence",
            "agent_assisted",
            "likely_human",
        ],
        "n": [10, 30, 50, 6, 4],
    }
)
_V2_V21 = pd.DataFrame(
    {
        "label_v2": [
            "highly_templated",
            "insufficient_evidence",
            "insufficient_evidence",
            "agent_assisted",
            "likely_human",
            "likely_human",
        ],
        "label_v21": [
            "highly_templated",
            "agent_assisted",
            "insufficient_evidence",
            "agent_assisted",
            "likely_human",
            "likely_human",
        ],
        "confidence_v21": ["high", "low", "low", "medium", "low", "medium"],
        "n": [40, 5, 45, 6, 3, 1],
    }
)
_PRECISION = pd.DataFrame(
    {
        "version": ["label_v21"] * 4
        + ["B3_trailer", "B3b_trailer_no_bulk", "B2_agent_folder", "B1_copy_count"],
        "label": [
            "highly_templated",
            "agent_assisted",
            "likely_human",
            "insufficient_evidence",
            "agent_assisted",
            "agent_assisted",
            "agent_assisted",
            "highly_templated",
        ],
        "n_predicted": [31, 30, 30, 29, 28, 16, 57, 6],
        "agree": [1, 25, 6, 15, 23, 11, 32, 1],
        "precision": [1 / 31, 25 / 30, 6 / 30, 15 / 29, 23 / 28, 11 / 16, 32 / 57, 1 / 6],
        "ci_low": [0.01, 0.66, 0.1, 0.34, 0.64, 0.44, 0.43, 0.03],
        "ci_high": [0.16, 0.93, 0.37, 0.69, 0.92, 0.86, 0.68, 0.56],
    }
)


def _is_png(path: Path) -> bool:
    return path.is_file() and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_stratum_label() -> None:
    assert fig.stratum_label("skills-dir|history=0|copies=1") == "skills-dir, no history, 1 copy"
    assert fig.stratum_label("canonical|history=1|copies=>=10") == "canonical, history, 10+ copies"
    assert fig.stratum_label("other|history=1|copies=2-9") == "other, history, 2-9 copies"


def test_label_versions_sum_each_version() -> None:
    frame = fig.label_versions(_V1_V2, _V2_V21)
    totals = frame.groupby("version")["n"].sum()
    assert totals["v1"] == totals["v2"] == totals["v2.1"] == 100
    v2 = frame[frame["version"] == "v2"].set_index("label")["n"]
    assert v2["highly_templated"] == 40
    v21 = frame[frame["version"] == "v2.1"].set_index("label")["n"]
    assert v21["agent_assisted"] == 11


def test_label_confidence_drops_insufficient() -> None:
    frame = fig.label_confidence(_V2_V21)
    assert "insufficient_evidence" not in set(frame["label"])
    human = frame[frame["label"] == "likely_human"].set_index("confidence")["n"]
    assert human.to_dict() == {"low": 3, "medium": 1}


def test_cluster_bins_count_skills_and_mark_templated() -> None:
    sizes = pd.Series([1, 1, 1, 3, 9, 12, 150, 20_000])
    bins = fig.cluster_bins(sizes, templated_min=10).set_index("bin")
    assert bins.loc["alone (size 1)", "skills"] == 3
    assert bins.loc["2 to 9", "clusters"] == 2
    assert bins.loc["2 to 9", "skills"] == 12
    assert bins.loc["10,000 or more", "skills"] == 20_000
    assert not bins.loc["2 to 9", "templated"]
    assert bins.loc["10 to 99", "templated"]
    assert bins["skills"].sum() == sizes.sum()


def test_lsh_probability_matches_the_band_formula() -> None:
    probs = fig.lsh_match_probability(np.array([0.0, 0.8, 1.0]), bands=9, rows=13)
    assert probs[0] == 0.0
    assert probs[1] == pytest.approx(0.399, abs=0.001)
    assert probs[2] == 1.0


def test_roc_points_auc() -> None:
    oof = pd.DataFrame(
        {"y": [0, 0, 1, 1], "good": [0.1, 0.2, 0.8, 0.9], "bad": [0.9, 0.1, 0.5, 0.2]}
    )
    points = fig.roc_points(oof, ["good", "bad"])
    assert points[points["method"] == "good"]["auc"].iloc[0] == 1.0
    assert points[points["method"] == "bad"]["auc"].iloc[0] == 0.5


def test_evidence_chart_keeps_reuse_and_disclosure_on_separate_axes(tmp_path):
    from skillsig.s3_labeling.evidence import AGENT_SIGNALS, REUSE_SIGNALS

    matrix = pd.DataFrame(
        [
            {
                "reuse_signal": reuse,
                "agent_signal": agent,
                "n": 10,
                "share_within_agent_signal": 0.2,
            }
            for reuse in REUSE_SIGNALS
            for agent in AGENT_SIGNALS
        ]
    )
    path = tmp_path / "evidence.png"
    assert fig.plot_evidence_matrix(matrix, path) == path
    assert _is_png(path)


@pytest.mark.parametrize(
    "draw",
    [
        lambda p: fig.plot_label_versions(fig.label_versions(_V1_V2, _V2_V21), p),
        lambda p: fig.plot_label_confidence(fig.label_confidence(_V2_V21), p),
        lambda p: fig.plot_spot_check(_PRECISION, p),
        lambda p: fig.plot_rules_vs_baselines(_PRECISION, p),
        lambda p: fig.plot_cluster_sizes(
            fig.cluster_bins(pd.Series([1, 1, 5, 40, 2_000]), 10), 0.8, p
        ),
        lambda p: fig.plot_lsh_curve([(0.8, 9, 13), (0.6, 18, 7)], p),
        lambda p: fig.plot_minhash_flow(5, 128, 9, 13, (0.6, 18, 7), p),
        lambda p: fig.plot_roc_curves(
            fig.roc_points(
                pd.DataFrame({"y": [0, 1, 0, 1], "ensemble": [0.2, 0.7, 0.4, 0.9]}), ["ensemble"]
            ),
            p,
        ),
    ],
)
def test_each_chart_writes_a_png(draw, tmp_path: Path) -> None:
    path = tmp_path / "chart.png"
    assert draw(path) == path
    assert _is_png(path)


def test_run_draws_what_it_can_and_skips_the_rest(tmp_cfg: Config) -> None:
    results = resolve(tmp_cfg, tmp_cfg.paths.results)
    results.mkdir(parents=True)
    _V1_V2.to_csv(results / "s3_v1_v2_crosstab.csv", index=False)
    _V2_V21.to_csv(results / "s3_v2_v21_crosstab.csv", index=False)
    _PRECISION.to_csv(results / "s4_spot_precision.csv", index=False)

    first = fig.run(tmp_cfg)
    names = {p.name for p in first.outputs}
    expected = {
        "a_label_versions.png",
        "f3_label_confidence.png",
        "f3_spot_check.png",
        "f2_rules_vs_baselines.png",
        "f1_minhash_flow.png",
        "f1_lsh_s_curve.png",
    }
    assert names == expected
    assert all(_is_png(p) for p in first.outputs)
    assert all(p.with_name(p.name + ".manifest.json").is_file() for p in first.outputs)

    second = fig.run(tmp_cfg)
    assert second.skipped
    assert fig.run(tmp_cfg, force=True).rows.keys() == expected
