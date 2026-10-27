"""E5b text lean for no-history skills."""

from __future__ import annotations

import numpy as np
import pandas as pd

from skillsig.s3_labeling.text_score import cutoffs, lean


def test_cutoffs_separate_clean_classes() -> None:
    oof = pd.DataFrame(
        {"y": [1] * 50 + [0] * 50, "m": np.r_[np.linspace(0.6, 1, 50), np.linspace(0, 0.4, 50)]}
    )
    hi, lo = cutoffs(oof, "m", 0.9)
    assert hi <= 0.6 and lo >= 0.4


def test_cutoffs_without_signal_decide_nothing() -> None:
    oof = pd.DataFrame({"y": [1, 0] * 50, "m": [0.5] * 100})
    hi, lo = cutoffs(oof, "m", 0.8)
    assert np.isinf(hi) and np.isinf(lo)


def test_lean_bands() -> None:
    out = lean(np.array([0.9, 0.5, 0.1]), hi=0.8, lo=0.2)
    assert list(out) == ["text_lean_agent", "text_uncertain", "text_lean_human"]
