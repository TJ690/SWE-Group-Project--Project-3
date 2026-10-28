"""Wilson interval and Cohen's kappa."""

from __future__ import annotations

import math

import pytest
from sklearn.metrics import cohen_kappa_score

from skillsig.s4_validation.stats import cohens_kappa, wilson_interval


def test_wilson_known_value() -> None:
    low, high = wilson_interval(30, 60)
    assert low == pytest.approx(0.377, abs=0.005)
    assert high == pytest.approx(0.623, abs=0.005)


def test_wilson_edges() -> None:
    assert wilson_interval(0, 10)[0] == 0.0
    assert wilson_interval(10, 10)[1] == 1.0
    assert all(math.isnan(x) for x in wilson_interval(0, 0))
    with pytest.raises(ValueError):
        wilson_interval(5, 3)


def test_kappa_identical_is_one() -> None:
    assert cohens_kappa(["a", "b", "a"], ["a", "b", "a"]) == 1.0
    assert cohens_kappa(["a", "a"], ["a", "a"]) == 1.0


def test_kappa_matches_sklearn() -> None:
    a = ["t", "t", "h", "a", "i", "i", "h", "a", "t", "i"]
    b = ["t", "h", "h", "a", "i", "a", "h", "a", "t", "t"]
    assert cohens_kappa(a, b) == pytest.approx(cohen_kappa_score(a, b))


def test_kappa_hand_example() -> None:
    # 2x2: 20 yes/yes, 5 yes/no, 10 no/yes, 15 no/no -> po=0.7, pe=0.5 -> kappa=0.4
    a = ["y"] * 25 + ["n"] * 25
    b = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    assert cohens_kappa(a, b) == pytest.approx(0.4)
