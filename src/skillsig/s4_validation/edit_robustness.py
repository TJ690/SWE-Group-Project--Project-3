"""
STAGE: s4_validation
MODULE: edit_robustness
PURPOSE: E3 - how often the template detector still matches registry skills edited by known amounts.
INPUTS: s2_template.parquet (registry blobs), artifacts primary rows (content), MinHash settings
OUTPUTS: results/s4_edit_robustness.csv, figures/12_edit_robustness.png
RQ LINK: RQ1 (what the templated_jaccard_min threshold means in edited lines)

Each sampled registry skill is edited in two ways at five levels: a share of its non-blank body
lines is replaced with lines from unrelated skills, or deleted. Detection uses the same
normalisation, shingles and permutations as the near-duplicate detector (minhash_cluster).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from skillsig.config import Config
from skillsig.provenance import StageResult, write_manifest
from skillsig.s2_features.minhash_cluster import signature
from skillsig.s4_validation import _io
from skillsig.s4_validation.stats import wilson_interval

log = logging.getLogger(__name__)

EDIT_LEVELS = (0.05, 0.10, 0.20, 0.40, 0.60)
MODES = ("replace", "delete")
N_SKILLS = 500
MIN_LINES = 10
_FRONT = re.compile(r"^\s*---.*?\n---\s*\n?", re.DOTALL)

# Reference palette (dataviz skill), light surface.
_SURFACE = "#fcfcfb"
_INK = "#0b0b0b"
_INK_2 = "#52514e"
_GRID = "#e4e3df"
_SERIES = {"replace": "#2a78d6", "delete": "#eb6834"}


def split_front(text: str) -> tuple[str, str]:
    match = _FRONT.match(text)
    if not match:
        return "", text
    return text[: match.end()], text[match.end() :]


def body_lines(text: str) -> list[str]:
    return [line for line in split_front(text)[1].splitlines() if line.strip()]


def edit_text(
    text: str,
    level: float,
    donor_lines: Sequence[str],
    mode: str,
    rng: np.random.Generator,
) -> str:
    """Replace (mode 'replace') or drop ('delete') round(level * n) non-blank body lines."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    front, body = split_front(text)
    lines = body.splitlines()
    candidates = [i for i, line in enumerate(lines) if line.strip()]
    k = round(level * len(candidates))
    if k == 0:
        return text
    chosen = set(rng.choice(candidates, size=k, replace=False).tolist())
    out: list[str] = []
    for i, line in enumerate(lines):
        if i not in chosen:
            out.append(line)
        elif mode == "replace":
            out.append(donor_lines[int(rng.integers(len(donor_lines)))])
    return front + "\n".join(out)


def jaccard_estimate(a: str, b: str, cfg: Config) -> float:
    """Estimated Jaccard of two texts with the detector's own MinHash signature."""
    return float(np.mean(signature(a, cfg) == signature(b, cfg)))


def robustness_table(
    originals: Sequence[str], donor_lines: Sequence[str], cfg: Config
) -> pd.DataFrame:
    """One row per (mode, level): mean Jaccard and detection rate with a Wilson CI."""
    rng = np.random.default_rng(cfg.seed)
    threshold = float(cfg.thresholds.templated_jaccard_min)
    base = [signature(text, cfg) for text in originals]
    rows = []
    for mode in MODES:
        for level in EDIT_LEVELS:
            scores = []
            for text, sig in zip(originals, base, strict=True):
                edited = edit_text(text, level, donor_lines, mode, rng)
                scores.append(float(np.mean(sig == signature(edited, cfg))))
            hits = int(sum(score >= threshold for score in scores))
            low, high = wilson_interval(hits, len(scores))
            rows.append(
                {
                    "mode": mode,
                    "level": level,
                    "n": len(scores),
                    "mean_jaccard": float(np.mean(scores)),
                    "detected": hits,
                    "detection_rate": hits / len(scores),
                    "ci_low": low,
                    "ci_high": high,
                }
            )
    return pd.DataFrame(rows)


def plot(table: pd.DataFrame, threshold: float, path: Path) -> Path:
    """Detection rate by share of lines edited, one line per edit mode."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7.2, 4.2), dpi=150, facecolor=_SURFACE)
    ax.set_facecolor(_SURFACE)
    for mode in MODES:
        part = table[table["mode"] == mode].sort_values("level")
        x = [0.0, *(part["level"] * 100)]
        y = np.array([100.0, *(part["detection_rate"] * 100)])
        low = np.array([100.0, *(part["ci_low"] * 100)])
        high = np.array([100.0, *(part["ci_high"] * 100)])
        ax.errorbar(
            x,
            y,
            yerr=[y - low, high - y],
            color=_SERIES[mode],
            linewidth=2,
            marker="o",
            markersize=6,
            capsize=0,
            elinewidth=1,
            label=f"lines {mode}d (unrelated text)" if mode == "replace" else "lines deleted",
        )
    replace = table[table["mode"] == "replace"].set_index("level")
    at10 = replace.loc[0.10, "detection_rate"] * 100
    ax.set_title(
        "Synthetic edits reduce estimated pairwise similarity\n"
        f"With 10% of lines replaced, {at10:.0f}% still reach Jaccard \u2265 {threshold:g}",
        loc="left",
        fontsize=10.5,
        color=_INK,
    )
    ax.set_xlabel("Share of body lines edited (%)", color=_INK_2)
    ax.set_ylabel("Pairs above the estimated Jaccard cutoff (%)", color=_INK_2)
    ax.set_ylim(0, 105)
    ax.set_xlim(-2, 62)
    ax.grid(axis="y", color=_GRID, linewidth=0.8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(_GRID)
    ax.tick_params(colors=_INK_2)
    ax.legend(frameon=False, loc="upper right", labelcolor=_INK_2)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=_SURFACE)
    plt.close(fig)
    return path


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Sample registry skills, edit them, and write the E3 table and figure."""
    template = _io.derived(cfg, "s2_template.parquet")
    _io.require(
        [
            (template, "python -m skillsig features --only template_sim"),
            (_io.artifacts_dir(cfg), "python src/data/download_data.py"),
        ]
    )
    inputs = [template, _io.artifacts_dir(cfg)]
    outputs = [
        _io.results(cfg, "s4_edit_robustness.csv"),
        _io.figures(cfg, "12_edit_robustness.png"),
    ]
    if not force and _io.up_to_date(outputs, cfg, inputs):
        return StageResult("s4_validation", outputs, skipped=True, rows=_io.saved_rows(outputs))
    originals, donors = _load_texts(cfg)
    donor_lines = [line for text in donors for line in body_lines(text)]
    table = robustness_table(originals, donor_lines, cfg)
    _io.write_csv(table, outputs[0], cfg, inputs)
    plot(table, float(cfg.thresholds.templated_jaccard_min), outputs[1])
    write_manifest(outputs[1], cfg=cfg, inputs=inputs, rows=len(table))
    log.info("E3: %s registry skills, %s donor lines", len(originals), len(donor_lines))
    return StageResult("s4_validation", outputs, rows={"s4_edit_robustness": len(table)})


def _load_texts(cfg: Config) -> tuple[list[str], list[str]]:
    seed = int(cfg.seed)
    art = _io.sql(_io.artifacts_glob(cfg))
    tmpl = _io.sql(_io.derived(cfg, "s2_template.parquet"))
    con = duckdb.connect()
    try:
        registry = con.execute(
            f"""
            WITH blobs AS (
                SELECT DISTINCT nearest_blob AS file_sha FROM read_parquet({tmpl})
                WHERE match_kind IN ('exact', 'near') AND nearest_blob IS NOT NULL
            )
            SELECT a.file_sha, a.content
            FROM read_parquet({art}) a JOIN blobs USING (file_sha)
            WHERE coalesce(TRY_CAST(a.dedup_primary AS INTEGER), 0) = 1
              AND a.content IS NOT NULL AND a.body_chars >= 200
            ORDER BY md5(a.file_sha || '{seed}')
            LIMIT {N_SKILLS * 2}
            """
        ).fetchall()
        donors = con.execute(
            f"""
            SELECT a.content FROM read_parquet({art}) a
            WHERE coalesce(TRY_CAST(a.dedup_primary AS INTEGER), 0) = 1
              AND a.content IS NOT NULL AND a.body_chars >= 200
              AND a.file_sha NOT IN (
                  SELECT nearest_blob FROM read_parquet({tmpl}) WHERE nearest_blob IS NOT NULL
              )
            ORDER BY md5(a.file_sha || '{seed}-donor')
            LIMIT {N_SKILLS}
            """
        ).fetchall()
    finally:
        con.close()
    originals = [text for _, text in registry if len(body_lines(text)) >= MIN_LINES][:N_SKILLS]
    return originals, [text for (text,) in donors]
