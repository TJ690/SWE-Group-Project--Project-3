"""
STAGE: s4_validation
MODULE: stats
PURPOSE: Small statistics shared by the evaluation checks: Wilson interval and Cohen's kappa.
INPUTS: none (pure functions)
OUTPUTS: none
RQ LINK: RQ1 (uncertainty on every precision, recall and agreement figure)
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials; (nan, nan) when n == 0."""
    if n <= 0:
        return (math.nan, math.nan)
    if not 0 <= k <= n:
        raise ValueError(f"k must be in [0, n], got k={k}, n={n}")
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def cohens_kappa(a: Sequence[str], b: Sequence[str]) -> float:
    """Cohen's kappa for two raters over the same items. 1.0 when both are constant and equal."""
    if len(a) != len(b):
        raise ValueError(f"raters have different lengths: {len(a)} vs {len(b)}")
    n = len(a)
    if n == 0:
        return math.nan
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[label] * cb[label] for label in set(ca) | set(cb)) / (n * n)
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)
