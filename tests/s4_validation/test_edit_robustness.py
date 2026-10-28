"""E3 controlled-edit robustness of the template threshold."""

from __future__ import annotations

import numpy as np
import pytest

from skillsig.config import Config
from skillsig.s4_validation.edit_robustness import (
    EDIT_LEVELS,
    body_lines,
    edit_text,
    jaccard_estimate,
    plot,
    robustness_table,
)

_TEXT = "---\nname: demo\ndescription: d\n---\n" + "\n".join(
    f"Step {i}: run the command number {i} and check the output carefully" for i in range(20)
)
_DONORS = [f"Unrelated project line about widget {i} configuration" for i in range(50)]


def test_level_zero_is_identity(cfg: Config) -> None:
    rng = np.random.default_rng(0)
    assert edit_text(_TEXT, 0.0, _DONORS, "replace", rng) == _TEXT
    assert jaccard_estimate(_TEXT, _TEXT, cfg) == 1.0


@pytest.mark.parametrize("mode", ["replace", "delete"])
def test_edits_exact_count_and_keep_front_matter(mode: str) -> None:
    rng = np.random.default_rng(1)
    edited = edit_text(_TEXT, 0.20, _DONORS, mode, rng)
    assert edited.startswith("---\nname: demo\ndescription: d\n---\n")
    original = set(body_lines(_TEXT))
    kept = [line for line in body_lines(edited) if line in original]
    assert len(kept) == 20 - 4
    if mode == "delete":
        assert len(body_lines(edited)) == 16
    else:
        assert len(body_lines(edited)) == 20


def test_bad_mode_raises() -> None:
    with pytest.raises(ValueError, match="mode"):
        edit_text(_TEXT, 0.1, _DONORS, "shuffle", np.random.default_rng(0))


def test_jaccard_symmetric(cfg: Config) -> None:
    other = _TEXT.replace("carefully", "quickly")
    assert jaccard_estimate(_TEXT, other, cfg) == jaccard_estimate(other, _TEXT, cfg)
    assert jaccard_estimate(_TEXT, other, cfg) < 1.0


def test_table_is_deterministic_and_monotone_ish(cfg: Config) -> None:
    originals = [_TEXT.replace("command", f"command-{k}") for k in range(6)]
    first = robustness_table(originals, _DONORS, cfg)
    second = robustness_table(originals, _DONORS, cfg)
    assert first.equals(second)
    assert len(first) == 2 * len(EDIT_LEVELS)
    replace = first[first["mode"] == "replace"].sort_values("level")
    assert replace["mean_jaccard"].iloc[0] > replace["mean_jaccard"].iloc[-1]


def test_plot_writes_png(cfg: Config, tmp_path) -> None:
    table = robustness_table([_TEXT], _DONORS, cfg)
    path = plot(table, 0.8, tmp_path / "fig.png")
    assert path.stat().st_size > 1000
