"""
STAGE: s6_report
MODULE: style
PURPOSE: Shared look for the report figures: palette, figure setup, title block, saving.
INPUTS: none
OUTPUTS: none (helpers draw what callers pass)
RQ LINK: none (presentation)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Reference palette (dataviz skill), light surface; same values as s4 edit_robustness.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
MUTED = "#c3c2b7"
REFERENCE = "#898781"
ACCENT = "#2a78d6"
CATEGORICAL = ("#2a78d6", "#eb6834", "#1baf7a", "#c3c2b7")


def new_figure(width: float = 7.6, height: float = 4.2) -> tuple[Any, Any]:
    """A figure and axes on the report surface (Agg backend, 150 dpi)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(width, height), dpi=150, facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    return fig, ax


def title(fig: Any, headline: str, subtitle: str) -> None:
    """Finding as the headline, provenance as the subtitle, both left-aligned above the axes."""
    fig.text(0.01, 0.985, headline, ha="left", va="top", fontsize=10.5, weight="bold", color=INK)
    fig.text(0.01, 0.925, subtitle, ha="left", va="top", fontsize=8, color=INK_2)


def tidy(ax: Any, grid: str | None = "x") -> None:
    """Hide the top and right spines, mute the rest, and draw a light grid on one axis."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=8)
    if grid:
        ax.grid(axis=grid, color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)


def thousands(ax: Any, axis: str = "x") -> None:
    """Plain tick numbers with thousands separators (no 1e6 offset)."""
    from matplotlib.ticker import FuncFormatter

    target = ax.xaxis if axis == "x" else ax.yaxis
    target.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value:,.0f}"))


def save(fig: Any, path: Path, top: float = 0.86) -> Path:
    """Lay out under the title block, write the PNG, and close the figure."""
    import matplotlib.pyplot as plt

    fig.tight_layout(rect=(0, 0, 1, top))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path
