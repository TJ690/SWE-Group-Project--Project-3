"""
STAGE: s6_report
MODULE: figures
PURPOSE: Draw the charts of docs/report/FINDINGS.md from finished stage outputs.
INPUTS: results/s3_*.csv, results/s4_*.csv, s2_repo, s2_types, s2_clusters, s3_text_methods_oof,
        s0_devsample_ids and artifacts (dev-sample groups only); MinHash settings from Config
OUTPUTS: figures/report/*.png (one manifest each)
RQ LINK: RQ1 (presentation of the findings; no new measurements)

Every chart reads a table an earlier stage already wrote and computes nothing new beyond shares
and curves. A chart whose inputs are missing is skipped with a warning, so the stage runs on a
partial pipeline. The edit-robustness chart is drawn by s4 (figures/12_edit_robustness.png).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, write_manifest
from skillsig.s6_report import style

log = logging.getLogger(__name__)

LABELS = ("highly_templated", "agent_assisted", "likely_human", "insufficient_evidence")
CONFIDENCE = ("high", "medium", "low")
ROC_METHODS = ("ensemble", "tfidf_chars", "features_gbm", "length_only", "ai_phrase_rule")
AUC_METHODS = (
    "ensemble",
    "tfidf_chars",
    "tfidf_words",
    "features_gbm",
    "features_lr",
    "length_only",
    "ai_phrase_rule",
)
CLUSTER_BINS: tuple[tuple[int, int | None, str], ...] = (
    (1, 1, "alone (size 1)"),
    (2, 9, "2 to 9"),
    (10, 99, "10 to 99"),
    (100, 999, "100 to 999"),
    (1_000, 9_999, "1,000 to 9,999"),
    (10_000, None, "10,000 or more"),
)
_LABEL_COLOUR = {
    "highly_templated": style.CATEGORICAL[0],
    "agent_assisted": style.CATEGORICAL[1],
    "likely_human": style.CATEGORICAL[2],
    "insufficient_evidence": style.CATEGORICAL[3],
}
_STABILITY_NAMES = {
    "templated_copies_min": "Copies / cluster size",
    "templated_jaccard_min": "Near-copy similarity",
    "cluster_threshold": "Cluster similarity",
    "bulk_batch_min": "Bulk batch",
    "human_min_commits": "Revised: commits",
    "human_min_span_days": "Revised: days",
}


# --- data: shares and curves computed from stage outputs --------------------------------------


def stratum_label(stratum: str) -> str:
    """'skills-dir|history=0|copies=2-9' -> 'skills-dir, no history, 2-9 copies'."""
    location, history, copies = (stratum.split("|") + ["", "", ""])[:3]
    hist = "history" if history.endswith("=1") else "no history"
    band = copies.split("=", 1)[-1]
    word = "1 copy" if band == "1" else "10+ copies" if band == ">=10" else f"{band} copies"
    return f"{location}, {hist}, {word}"


def label_versions(v1_v2: pd.DataFrame, v2_v21: pd.DataFrame) -> pd.DataFrame:
    """Skills per label for v1, v2 and v2.1, from the two crosstabs: version, label, n."""
    parts = [
        v1_v2.groupby("v1_label")["n"]
        .sum()
        .rename_axis("label")
        .reset_index()
        .assign(version="v1"),
        v1_v2.groupby("v2_label")["n"]
        .sum()
        .rename_axis("label")
        .reset_index()
        .assign(version="v2"),
        v2_v21.groupby("label_v21")["n"]
        .sum()
        .rename_axis("label")
        .reset_index()
        .assign(version="v2.1"),
    ]
    return pd.concat(parts, ignore_index=True)[["version", "label", "n"]]


def label_confidence(v2_v21: pd.DataFrame) -> pd.DataFrame:
    """v2.1 skills per label and confidence, without insufficient_evidence (always low)."""
    frame = v2_v21.groupby(["label_v21", "confidence_v21"])["n"].sum().reset_index()
    frame.columns = ["label", "confidence", "n"]
    return frame[frame["label"] != "insufficient_evidence"].reset_index(drop=True)


def cluster_bins(sizes: pd.Series, templated_min: int) -> pd.DataFrame:
    """Skills and clusters per size band; `sizes` holds one cluster size per cluster."""
    rows = []
    for low, high, name in CLUSTER_BINS:
        part = sizes[(sizes >= low) & ((sizes <= high) if high is not None else True)]
        rows.append(
            {
                "bin": name,
                "clusters": int(len(part)),
                "skills": int(part.sum()),
                "templated": low >= templated_min,
            }
        )
    return pd.DataFrame(rows)


def lsh_match_probability(similarity: np.ndarray, bands: int, rows: int) -> np.ndarray:
    """Chance two signatures share at least one band: 1 - (1 - s^rows)^bands."""
    return 1.0 - (1.0 - np.power(similarity, rows)) ** bands


def roc_points(oof: pd.DataFrame, methods: Sequence[str]) -> pd.DataFrame:
    """ROC curve and AUC per method from out-of-fold scores: method, fpr, tpr, auc."""
    from sklearn.metrics import roc_auc_score, roc_curve

    frames = []
    for method in methods:
        fpr, tpr, _ = roc_curve(oof["y"], oof[method])
        auc = float(roc_auc_score(oof["y"], oof[method]))
        frames.append(pd.DataFrame({"method": method, "fpr": fpr, "tpr": tpr, "auc": auc}))
    return pd.concat(frames, ignore_index=True)


# --- plots: one function per chart, data in, PNG out ------------------------------------------


def plot_devsample_groups(groups: pd.DataFrame, path: Path) -> Path:
    """Share of each stratum in the full data (bar) and in the dev sample (tick)."""
    data = groups.sort_values("full", ascending=False).reset_index(drop=True)
    full = data["full"] / data["full"].sum() * 100
    sample = data["sample"] / data["sample"].sum() * 100
    fig, ax = style.new_figure(7.6, 5.6)
    y = np.arange(len(data))
    ax.barh(y, full, color=style.MUTED, height=0.65)
    ax.scatter(sample, y, marker="|", s=160, color=style.ACCENT, linewidths=2, zorder=3)
    for i, value in enumerate(full):
        ax.text(
            value + 0.6,
            i,
            f"{value:.2f}%" if value < 0.1 else f"{value:.1f}%",
            va="center",
            fontsize=7,
            color=style.INK_2,
        )
    ax.set_yticks(y, [stratum_label(s) for s in data["stratum"]], fontsize=7.5, color=style.INK)
    ax.invert_yaxis()
    ax.set_xlabel("Share of distinct skills (%)", color=style.INK_2, fontsize=8)
    style.tidy(ax, "x")
    gap = float((full - sample).abs().max())
    style.title(
        fig,
        "The dev sample matches the full data in every group",
        f"Bar = full data, tick = 50,000-skill dev sample; largest gap {gap:.2f} points",
    )
    return style.save(fig, path, top=0.9)


def plot_repository_kinds(kinds: pd.DataFrame, path: Path) -> Path:
    """Share of repositories and of SKILL.md files per repository kind (100% stacked bars)."""
    order = ["A_collection", "B_agent_tooling", "C_project"]
    names = {
        "A_collection": "Collection",
        "B_agent_tooling": "Agent tooling",
        "C_project": "Ordinary project",
    }
    colours = {
        "A_collection": style.ACCENT,
        "B_agent_tooling": style.GRID,
        "C_project": style.MUTED,
    }
    data = kinds.set_index("repo_kind").reindex(order)
    fig, ax = style.new_figure(7.6, 2.8)
    for row, column in enumerate(("repos", "files")):
        left = 0.0
        total = data[column].sum()
        for kind in order:
            share = data.loc[kind, column] / total * 100
            ax.barh(
                row,
                share,
                left=left,
                color=colours[kind],
                height=0.6,
                label=names[kind] if row == 0 else None,
            )
            ink = "white" if kind == "A_collection" else style.INK
            ax.text(
                left + share / 2,
                row,
                f"{share:.0f}%",
                ha="center",
                va="center",
                fontsize=8,
                color=ink,
            )
            left += share
    ax.set_yticks([0, 1], ["Repositories", "SKILL.md files"], fontsize=8.5, color=style.INK)
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.legend(frameon=False, ncol=3, loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=8)
    style.tidy(ax, None)
    repo_share = data.loc["A_collection", "repos"] / data["repos"].sum() * 100
    file_share = data.loc["A_collection", "files"] / data["files"].sum() * 100
    style.title(
        fig,
        f"Collections are {repo_share:.0f}% of repositories but hold "
        f"{file_share:.0f}% of skill files",
        f"Repository kind (step 2.3): {int(data['repos'].sum()):,} repositories",
    )
    return style.save(fig, path, top=0.8)


def plot_skill_types(types: pd.DataFrame, path: Path) -> Path:
    """Distinct skills per keyword type, largest first; the top named type in the accent."""
    data = types.sort_values("n", ascending=False).reset_index(drop=True)
    share = data["n"] / data["n"].sum() * 100
    named = data[data["skill_type"] != "unclassified"]["skill_type"].iloc[0]
    colours = [
        style.ACCENT if t == named else style.GRID if t == "unclassified" else style.MUTED
        for t in data["skill_type"]
    ]
    fig, ax = style.new_figure(7.6, 5.0)
    y = np.arange(len(data))
    ax.barh(y, share, color=colours, height=0.65)
    for i, value in enumerate(share):
        ax.text(value + 0.4, i, f"{value:.1f}%", va="center", fontsize=7.5, color=style.INK_2)
    ax.set_yticks(y, data["skill_type"], fontsize=8, color=style.INK)
    ax.invert_yaxis()
    ax.set_xlabel("Share of distinct skills (%)", color=style.INK_2, fontsize=8)
    style.tidy(ax, "x")
    unclassified = share[data["skill_type"] == "unclassified"].sum()
    style.title(
        fig,
        f"{unclassified:.0f}% of skills match no topic; {named} leads the rest",
        f"Distinct skills by keyword type (step 2.4), {int(data['n'].sum()):,} skills",
    )
    return style.save(fig, path, top=0.88)


def plot_scaffold_overlap(overlap: pd.DataFrame, path: Path) -> Path:
    """Skills per scaffold reason: already templated by the current rules vs. new."""
    data = overlap[overlap["scaffold_basis"] != "none"].copy()
    data["known"] = data[["registry_exact", "registry_near", "cluster", "copy_count"]].sum(axis=1)
    data = data.sort_values("total", ascending=False).reset_index(drop=True)
    fig, ax = style.new_figure(7.6, 3.6)
    y = np.arange(len(data))
    ax.barh(y, data["known"], color=style.MUTED, height=0.6, label="already templated")
    ax.barh(y, data["none"], left=data["known"], color=style.ACCENT, height=0.6, label="new")
    for i, row in data.iterrows():
        ax.text(
            row["total"] * 1.01 + data["total"].max() * 0.01,
            i,
            f"{int(row['none']):,} new",
            va="center",
            fontsize=7.5,
            color=style.INK,
        )
    ax.set_yticks(y, data["scaffold_basis"], fontsize=8, color=style.INK)
    ax.invert_yaxis()
    ax.set_xlim(0, data["total"].max() * 1.22)
    ax.set_xlabel("Readable skills", color=style.INK_2, fontsize=8)
    style.thousands(ax)
    ax.legend(frameon=False, loc="lower right", fontsize=8)
    style.tidy(ax, "x")
    new = int(data["none"].sum())
    style.title(
        fig,
        f"{new:,} additional skills have scaffold signals awaiting review",
        f"Skills per scaffold reason (step 2.9): {new:,} not templated by the current rules",
    )
    return style.save(fig, path, top=0.86)


def plot_label_versions(versions: pd.DataFrame, path: Path) -> Path:
    """Share of distinct skills per label for v1, v2 and v2.1 (100% stacked bars)."""
    order = ["v1", "v2", "v2.1"]
    fig, ax = style.new_figure(7.6, 3.2)
    for row, version in enumerate(order):
        part = versions[versions["version"] == version].set_index("label")["n"]
        total = part.sum()
        left = 0.0
        for label in LABELS:
            share = part.get(label, 0) / total * 100
            ax.barh(
                row,
                share,
                left=left,
                color=_LABEL_COLOUR[label],
                height=0.6,
                label=label if row == 0 else None,
            )
            if share > 5:
                ink = style.INK if label == "insufficient_evidence" else "white"
                ax.text(
                    left + share / 2,
                    row,
                    f"{share:.1f}%",
                    ha="center",
                    va="center",
                    fontsize=8,
                    color=ink,
                )
            left += share
    ax.set_yticks(range(len(order)), order, fontsize=9, color=style.INK)
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.legend(frameon=False, ncol=4, loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=7.5)
    style.tidy(ax, None)
    templated = versions.pivot_table(index="version", columns="label", values="n", aggfunc="sum")
    moved_templ = templated.loc["v2", "highly_templated"] - templated.loc["v1", "highly_templated"]
    moved_agent = templated.loc["v2.1", "agent_assisted"] - templated.loc["v2", "agent_assisted"]
    style.title(
        fig,
        f"v2 moved {moved_templ / 1000:,.0f},000 skills into templated; "
        f"v2.1 moved {moved_agent / 1000:,.0f},000 into agent-assisted",
        f"Share of {int(versions[versions['version'] == 'v2.1']['n'].sum()):,} "
        "distinct skills by label, per label version",
    )
    return style.save(fig, path, top=0.78)


def plot_label_confidence(confidence: pd.DataFrame, path: Path) -> Path:
    """v2.1 confidence within each label (100% stacked bars, one hue by level)."""
    labels = ["highly_templated", "agent_assisted", "likely_human"]
    alpha = {"high": 1.0, "medium": 0.6, "low": 0.25}
    fig, ax = style.new_figure(7.6, 3.0)
    for row, label in enumerate(labels):
        part = confidence[confidence["label"] == label].set_index("confidence")["n"]
        total = part.sum()
        left = 0.0
        for level in CONFIDENCE:
            share = part.get(level, 0) / total * 100
            ax.barh(
                row,
                share,
                left=left,
                color=style.ACCENT,
                alpha=alpha[level],
                height=0.6,
                label=level if row == 0 else None,
            )
            if share > 6:
                ink = "white" if level == "high" else style.INK
                ax.text(
                    left + share / 2,
                    row,
                    f"{share:.0f}%",
                    ha="center",
                    va="center",
                    fontsize=8,
                    color=ink,
                )
            left += share
    ax.set_yticks(
        range(len(labels)),
        [
            f"{label}\n{int(confidence[confidence['label'] == label]['n'].sum()):,}"
            for label in labels
        ],
        fontsize=8,
        color=style.INK,
    )
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.legend(frameon=False, ncol=3, loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=8)
    style.tidy(ax, None)
    style.title(
        fig,
        "Most human labels are low confidence; most templated labels are medium",
        "v2.1 confidence within each label, share of that label's skills",
    )
    return style.save(fig, path, top=0.8)


def plot_text_methods(methods: pd.DataFrame, path: Path) -> Path:
    """AUC per text-only method on all rows and on the matched subset, with chance at 0.5."""
    data = methods[methods["method"].isin(AUC_METHODS)]
    pivot = data.pivot_table(index="method", columns="subset", values="auc").reindex(AUC_METHODS)
    fig, ax = style.new_figure(7.6, 3.8)
    y = np.arange(len(pivot))
    ax.hlines(y, pivot["matched"], pivot["all"], color=style.MUTED, linewidth=1)
    ax.scatter(
        pivot["matched"],
        y,
        facecolors="none",
        edgecolors=style.MUTED,
        s=40,
        linewidths=1.5,
        label="matched (no bulk commits)",
        zorder=3,
    )
    colours = [style.ACCENT] * len(pivot)
    ax.scatter(pivot["all"], y, color=colours, s=40, label="all rows", zorder=3)
    for i, value in enumerate(pivot["all"]):
        ax.text(
            max(value, pivot["matched"].iloc[i]) + 0.006,
            i,
            f"{value:.2f}",
            va="center",
            fontsize=7.5,
            color=style.INK_2,
        )
    ax.axvline(0.5, color=style.REFERENCE, linestyle="--", linewidth=1)
    ax.text(0.502, -0.6, "chance", fontsize=7.5, color=style.INK_2)
    ax.set_yticks(y, pivot.index, fontsize=8, color=style.INK)
    ax.invert_yaxis()
    ax.set_xlim(0.45, 0.76)
    ax.set_xlabel("AUC", color=style.INK_2, fontsize=8)
    ax.legend(frameon=False, loc="lower right", fontsize=7.5)
    style.tidy(ax, "x")
    best = pivot["all"].max()
    style.title(
        fig,
        f"The best text-only method reaches AUC {best:.2f} against proxy labels",
        "Disclosure-positive vs. likely-human proxies; 20,000 each, 5 grouped folds (step 3.6)",
    )
    return style.save(fig, path)


def plot_roc_curves(points: pd.DataFrame, path: Path) -> Path:
    """ROC curve per text method, the ensemble in the accent, chance as a dashed diagonal."""
    dash = {"length_only": (0, (5, 3)), "ai_phrase_rule": (0, (1, 2))}
    fig, ax = style.new_figure(6.4, 5.4)
    ax.plot([0, 1], [0, 1], color=style.REFERENCE, linestyle="--", linewidth=1, label="chance")
    for method in reversed(ROC_METHODS):
        part = points[points["method"] == method]
        if part.empty:
            continue
        main = method == "ensemble"
        ax.plot(
            part["fpr"] * 100,
            part["tpr"] * 100,
            color=style.ACCENT if main else style.INK_2 if method == "tfidf_chars" else style.MUTED,
            linewidth=2.2 if main else 1.3,
            linestyle=dash.get(method, "-"),
            label=f"{method}, AUC {part['auc'].iloc[0]:.2f}",
        )
    ensemble = points[points["method"] == "ensemble"]
    at10 = float(np.interp(0.10, ensemble["fpr"], ensemble["tpr"])) if not ensemble.empty else 0
    ax.scatter([10], [at10 * 100], color=style.ACCENT, zorder=4, s=25)
    ax.annotate(
        f"{at10 * 100:.0f}% proxy recall\nat 10% proxy FPR",
        (10, at10 * 100),
        xytext=(14, at10 * 100 - 6),
        fontsize=7.5,
        color=style.INK,
    )
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles[::-1], labels[::-1], frameon=False, loc="lower right", fontsize=7.5)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.set_xlabel("Proxy negatives flagged positive (%)", color=style.INK_2, fontsize=8)
    ax.set_ylabel("Disclosure-positive proxies retained (%)", color=style.INK_2, fontsize=8)
    style.tidy(ax, "both")
    style.title(
        fig,
        "Text methods discriminate evaluated proxies, not verified authorship",
        "ROC curves of disclosure-positive vs. likely-human proxies; out-of-fold scores (step 3.6)",
    )
    return style.save(fig, path, top=0.88)


def plot_disclosure_recall(recall: pd.DataFrame, path: Path) -> Path:
    """Share of AI disclosures visible in the first or last commit, with 95% intervals."""
    names = {"all": "All registry skills", "all_3plus_commits": "Skills with 3+ commits"}
    data = recall.copy()
    data["name"] = [names.get(s, s.replace("group:", "")) for s in data["scope"]]
    head = data[data["scope"].isin(names)]
    groups = data[data["scope"].str.startswith("group:")].sort_values("any_ai", ascending=False)
    data = pd.concat([head, groups], ignore_index=True)
    fig, ax = style.new_figure(7.6, 4.2)
    y = np.arange(len(data))
    pct = data["recall"] * 100
    colours = [style.ACCENT if s == "all_3plus_commits" else style.MUTED for s in data["scope"]]
    ax.hlines(y, data["ci_low"] * 100, data["ci_high"] * 100, colors=colours, linewidth=2)
    ax.scatter(pct, y, color=colours, s=40, zorder=3)
    overall = float(head[head["scope"] == "all"]["recall"].iloc[0]) * 100
    ax.axvline(overall, color=style.REFERENCE, linestyle="--", linewidth=1)
    for i, row in data.iterrows():
        ax.text(
            103,
            i,
            f"{int(row['visible_first_or_last'])} of {int(row['any_ai'])}",
            va="center",
            fontsize=7.5,
            color=style.INK_2,
        )
    ax.set_yticks(y, data["name"], fontsize=8, color=style.INK)
    ax.invert_yaxis()
    ax.set_xlim(0, 118)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xlabel(
        "Disclosures visible in the first or last commit (%)", color=style.INK_2, fontsize=8
    )
    style.tidy(ax, "x")
    three = float(head[head["scope"] == "all_3plus_commits"]["recall"].iloc[0]) * 100
    style.title(
        fig,
        f"The labels see {overall:.0f}% of AI disclosures, but only {three:.0f}% "
        f"for skills edited 3+ times",
        "Registry skills with an AI trailer anywhere in history, 95% interval (step 4.1)",
    )
    return style.save(fig, path)


def plot_template_provenance(provenance: pd.DataFrame, path: Path) -> Path:
    """Exact-reference recovery separately from similarity-conditioned near agreement."""
    names = {
        "exact": "Exact registry matches\nidentity reference",
        "near": "Near-match candidates\nnot independent validation",
    }
    rules = [
        ("copy_recall", "copy count rule", style.GRID),
        ("cluster_recall", "cluster rule", style.MUTED),
        ("either_recall", "either rule", style.ACCENT),
    ]
    data = provenance.set_index("known_set").reindex(list(names))
    fig, ax = style.new_figure(7.6, 4.0)
    for g, known in enumerate(data.index):
        for k, (column, label, colour) in enumerate(rules):
            y = g * 4 + k
            value = data.loc[known, column] * 100
            ax.barh(y, value, color=colour, height=0.8, label=label if g == 0 else None)
            ax.text(value + 1, y, f"{value:.0f}%", va="center", fontsize=7.5, color=style.INK_2)
    ax.set_yticks(
        [g * 4 + 1 for g in range(len(data))],
        [f"{names[k]}\n{int(data.loc[k, 'n']):,} skills" for k in data.index],
        fontsize=8,
        color=style.INK,
    )
    ax.invert_yaxis()
    ax.set_xlim(0, 105)
    ax.set_xlabel("Reference cases marked by the rule (%)", color=style.INK_2, fontsize=8)
    ax.legend(frameon=False, ncol=3, loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=8)
    style.tidy(ax, "x")
    copy_all = data.loc["exact", "copy_recall"] * 100
    either_all = data.loc["exact", "either_recall"] * 100
    style.title(
        fig,
        f"Copy count alone recovers {copy_all:.0f}% of exact registry matches; "
        f"with clusters, {either_all:.0f}%",
        "Near matches are similarity-selected candidates; the two rows must not be pooled",
    )
    return style.save(fig, path, top=0.8)


def plot_evidence_matrix(matrix: pd.DataFrame, path: Path) -> Path:
    """Counts and column-normalized shares of independent reuse/disclosure signals."""
    from skillsig.s3_labeling.evidence import AGENT_SIGNALS, REUSE_SIGNALS

    index = pd.MultiIndex.from_product(
        [REUSE_SIGNALS, AGENT_SIGNALS], names=["reuse_signal", "agent_signal"]
    )
    data = matrix.set_index(["reuse_signal", "agent_signal"]).reindex(index)
    counts = data["n"].fillna(0).to_numpy().reshape(5, 3)
    values = data["share_within_agent_signal"].fillna(0).to_numpy().reshape(5, 3) * 100
    fig, ax = style.new_figure(8.6, 5.1)
    ax.imshow(values, cmap="Blues", vmin=0, vmax=100, aspect="auto")
    for r in range(5):
        for c in range(3):
            ax.text(
                c,
                r,
                f"{int(counts[r, c]):,}\n{values[r, c]:.1f}%",
                ha="center",
                va="center",
                fontsize=8,
                color="white" if values[r, c] > 55 else style.INK,
            )
    ax.set_yticks(
        range(5),
        [
            "Exact content shared",
            "Near-duplicate signal",
            "Scaffold signal only",
            "No reuse signal observed",
            "Reuse not assessed",
        ],
    )
    ax.set_xticks(
        range(3),
        [
            "Agent involvement\ndisclosed",
            "No disclosure observed\nin fetched history",
            "Commit history\nunavailable",
        ],
    )
    ax.tick_params(axis="both", labelsize=8, length=0, pad=8)
    for spine in ax.spines.values():
        spine.set_visible(False)
    total = int(counts[:, 0].sum())
    overlap = int(counts[:3, 0].sum())
    style.title(
        fig,
        f"{overlap:,} of {total:,} disclosed-agent skills also have reuse signals",
        "Counts and shares within each column; candidate reuse evidence is not verified copying",
    )
    return style.save(fig, path, top=0.84)


def plot_stability(stability: pd.DataFrame, path: Path) -> Path:
    """Change in templated share and in medium-confidence human share per threshold setting."""
    data = stability[stability["parameter"] != "default"]
    templ = data[data["label"] == "highly_templated"].reset_index(drop=True)
    human = data[data["label"] == "likely_human:medium"].reset_index(drop=True)
    names = [
        f"{_STABILITY_NAMES.get(p, p)} {v:g}"
        for p, v in zip(templ["parameter"], templ["value"], strict=True)
    ]
    fig, _ = style.new_figure(7.6, 4.6)
    fig.clf()
    ax1, ax2 = fig.subplots(1, 2, sharey=True)
    y = np.arange(len(templ))
    for ax, part, heading, span in (
        (ax1, templ, "Templated share", 4),
        (ax2, human, "Human labels at medium confidence", 8),
    ):
        ax.set_facecolor(style.SURFACE)
        values = part["delta_pp"].to_numpy()
        colours = [style.ACCENT if abs(v) >= 0.5 else style.MUTED for v in values]
        ax.barh(y, values, color=colours, height=0.6)
        for i, value in enumerate(values):
            text = "0" if abs(value) < 0.05 else f"{value:+.1f}"
            ax.text(
                value + (0.15 if value >= 0 else -0.15) * span / 4,
                i,
                text,
                va="center",
                ha="left" if value >= 0 else "right",
                fontsize=7.5,
                color=style.INK_2,
            )
        ax.axvline(0, color=style.REFERENCE, linewidth=1)
        ax.set_xlim(-span, span)
        ax.set_title(heading, fontsize=8.5, color=style.INK, loc="center")
        style.tidy(ax, None)
    ax1.set_yticks(y, names, fontsize=7.5, color=style.INK)
    ax1.invert_yaxis()
    for ax in (ax1, ax2):
        ax.set_xlabel("Change from the default (points)", color=style.INK_2, fontsize=8)
    style.title(
        fig,
        "Only the cluster and copy thresholds move label shares",
        "One threshold at a time on a seeded sample of skills (step 4.4)",
    )
    return style.save(fig, path)


def plot_spot_check(precision: pd.DataFrame, path: Path) -> Path:
    """Share of v2.1 labels the annotator agreed with, with 95% intervals."""
    data = precision[(precision["version"] == "label_v21") & (precision["n_predicted"] > 0)]
    data = data.sort_values("precision", ascending=False).reset_index(drop=True)
    fig, ax = style.new_figure(7.6, 3.0)
    y = np.arange(len(data))
    best = data["precision"].idxmax()
    colours = [style.ACCENT if i == best else style.MUTED for i in range(len(data))]
    ax.hlines(y, data["ci_low"] * 100, data["ci_high"] * 100, colors=colours, linewidth=2)
    ax.scatter(data["precision"] * 100, y, color=colours, s=50, zorder=3)
    for i, row in data.iterrows():
        ax.text(
            row["ci_high"] * 100 + 2,
            i,
            f"{int(row['agree'])} of {int(row['n_predicted'])}",
            va="center",
            fontsize=7.5,
            color=style.INK_2,
        )
    ax.set_yticks(y, data["label"], fontsize=8, color=style.INK)
    ax.invert_yaxis()
    ax.set_xlim(0, 105)
    ax.set_xlabel("Labels the annotator agreed with (%)", color=style.INK_2, fontsize=8)
    style.tidy(ax, "x")
    style.title(
        fig,
        "A agrees most often with agent-assisted labels in this sample",
        "v2.1 labels versus A's independent answers; Wilson 95% intervals (step 4.8)",
    )
    return style.save(fig, path)


def plot_rules_vs_baselines(precision: pd.DataFrame, path: Path) -> Path:
    """Agreement for v2.1 and the one-signal baselines on the same annotated skills."""
    wanted = [
        ("agent_assisted", "label_v21", "v2.1 rules"),
        ("agent_assisted", "B3_trailer", "B3 trailer on first commit"),
        ("agent_assisted", "B3b_trailer_no_bulk", "B3b trailer, no bulk"),
        ("agent_assisted", "B2_agent_folder", "B2 agent folder"),
        ("highly_templated", "label_v21", "v2.1 rules"),
        ("highly_templated", "B1_copy_count", "B1 10+ copies"),
    ]
    rows = []
    for label, version, name in wanted:
        hit = precision[(precision["version"] == version) & (precision["label"] == label)]
        if not hit.empty and int(hit["n_predicted"].iloc[0]) > 0:
            rows.append({**hit.iloc[0].to_dict(), "name": name})
    data = pd.DataFrame(rows)
    fig, ax = style.new_figure(7.6, 3.8)
    positions = []
    for i, row in data.iterrows():
        positions.append(i + (1 if row["label"] == "highly_templated" else 0))
    y = np.array(positions)
    colours = [style.ACCENT if v == "label_v21" else style.MUTED for v in data["version"]]
    ax.hlines(y, data["ci_low"] * 100, data["ci_high"] * 100, colors=colours, linewidth=2)
    ax.scatter(data["precision"] * 100, y, color=colours, s=40, zorder=3)
    for pos, (_, row) in zip(y, data.iterrows(), strict=True):
        ax.text(
            row["ci_high"] * 100 + 2,
            pos,
            f"{int(row['agree'])} of {int(row['n_predicted'])}",
            va="center",
            fontsize=7.5,
            color=style.INK_2,
        )
    ax.set_yticks(
        y,
        [f"{n}  ({lab})" for n, lab in zip(data["name"], data["label"], strict=True)],
        fontsize=7.5,
        color=style.INK,
    )
    ax.invert_yaxis()
    ax.set_xlim(0, 105)
    ax.set_xlabel("Labels the annotator agreed with (%)", color=style.INK_2, fontsize=8)
    style.tidy(ax, "x")
    style.title(
        fig,
        "v2.1 and trailer-only labels have similar agreement estimates against A",
        "A's 120-item sample; different predicted subsets; Wilson 95% intervals (step 4.8)",
    )
    return style.save(fig, path)


def plot_cluster_sizes(bins: pd.DataFrame, threshold: float, path: Path) -> Path:
    """Readable skills per near-duplicate cluster size band; templated bands in the accent."""
    total = bins["skills"].sum()
    fig, ax = style.new_figure(7.6, 3.6)
    y = np.arange(len(bins))
    colours = [style.ACCENT if t else style.MUTED for t in bins["templated"]]
    ax.barh(y, bins["skills"], color=colours, height=0.6)
    for i, row in bins.iterrows():
        share = row["skills"] / total * 100
        text = (
            f"{share:.1f}% · {row['skills']:,}"
            if row["clusters"] == row["skills"]
            else f"{share:.1f}% · {row['skills']:,} in {row['clusters']:,} "
            f"cluster{'s' if row['clusters'] != 1 else ''}"
        )
        inside = row["skills"] > bins["skills"].max() * 0.6
        ax.text(
            row["skills"] - bins["skills"].max() * 0.01
            if inside
            else row["skills"] + bins["skills"].max() * 0.01,
            i,
            text,
            va="center",
            ha="right" if inside else "left",
            fontsize=7.5,
            color=style.INK,
        )
    ax.set_yticks(y, bins["bin"], fontsize=8, color=style.INK)
    ax.invert_yaxis()
    ax.set_xlim(0, bins["skills"].max() * 1.05)
    ax.set_xlabel("Readable skills", color=style.INK_2, fontsize=8)
    style.thousands(ax)
    style.tidy(ax, "x")
    templ = bins[bins["templated"]]["skills"].sum() / total * 100
    alone = bins.iloc[0]["skills"] / total * 100
    largest = bins[bins["clusters"] > 0].iloc[-1]
    style.title(
        fig,
        f"{alone:.0f}% have no LSH candidate link; {templ:.0f}% sit in components of 10+",
        f"Readable skills by near-duplicate cluster size at similarity {threshold:g} "
        f"(step 2.6); blue = historical size rule; largest band: {largest['bin']}",
    )
    return style.save(fig, path)


def plot_lsh_curve(settings: Sequence[tuple[float, int, int]], path: Path) -> Path:
    """Chance two signatures become candidates, by true similarity, per LSH setting."""
    grid = np.linspace(0, 1, 201)
    fig, ax = style.new_figure(7.6, 4.6)
    main_threshold, main_bands, main_rows = settings[0]
    for k, (threshold, bands, rows) in enumerate(settings):
        main = k == 0
        ax.plot(
            grid,
            lsh_match_probability(grid, bands, rows) * 100,
            color=style.ACCENT if main else style.MUTED,
            linewidth=2.2 if main else 1.4,
            linestyle="-" if main else (0, (5, 3)),
            label=f"{threshold:g} setting, {bands} bands of {rows}",
        )
    marks = np.array([main_threshold - 0.1, main_threshold, main_threshold + 0.1])
    probs = lsh_match_probability(marks, main_bands, main_rows) * 100
    ax.scatter(marks, probs, color=style.ACCENT, zorder=3, s=25)
    for s, p in zip(marks, probs, strict=True):
        ax.annotate(
            f"{p:.0f}%",
            (s, p),
            xytext=(-24, 4),
            textcoords="offset points",
            fontsize=7.5,
            color=style.INK,
        )
    ax.axvline(main_threshold, color=style.REFERENCE, linestyle="--", linewidth=1)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 102)
    ax.set_xlabel(
        "True similarity of the two skills (Jaccard of shingles)", color=style.INK_2, fontsize=8
    )
    ax.set_ylabel("Chance of a direct match (%)", color=style.INK_2, fontsize=8)
    ax.legend(frameon=False, loc="upper left", fontsize=7.5)
    style.tidy(ax, "y")
    at = float(probs[1])
    style.title(
        fig,
        f"At the {main_threshold:g} setting, pairs {main_threshold * 100:.0f}% similar are "
        f"matched directly only {at:.0f}% of the time",
        "Chance two skills become candidates in one LSH pass (step 2.6)",
    )
    return style.save(fig, path)


def plot_minhash_flow(
    shingle_words: int,
    num_perm: int,
    bands: int,
    rows: int,
    low: tuple[float, int, int],
    path: Path,
) -> Path:
    """Diagram of step 2.6: body -> shingles -> signature -> bands -> buckets -> clusters."""
    from matplotlib.patches import FancyBboxPatch

    fig, ax = style.new_figure(7.6, 3.0)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 40)
    ax.axis("off")
    boxes = [
        (1, 24, "Skill body", "full text, lower-cased", False),
        (26, 24, "Shingles", f"every {shingle_words}-word window", False),
        (51, 24, "Signature", f"{num_perm} MinHash numbers", False),
        (76, 24, "Bands", f"{bands} bands of {rows} numbers", False),
        (76, 4, "Same band bucket", "candidate near-duplicates", False),
        (51, 4, "Clusters", "linked candidates joined", True),
    ]
    width, height = 23, 11
    for x, y, name, note, hot in boxes:
        ax.add_patch(
            FancyBboxPatch(
                (x, y),
                width,
                height,
                boxstyle="round,pad=0,rounding_size=1.5",
                facecolor=style.ACCENT if hot else style.SURFACE,
                alpha=0.15 if hot else 1,
                edgecolor="none",
            )
        )
        ax.add_patch(
            FancyBboxPatch(
                (x, y),
                width,
                height,
                boxstyle="round,pad=0,rounding_size=1.5",
                facecolor="none",
                edgecolor=style.ACCENT if hot else style.MUTED,
                linewidth=1.6 if hot else 1,
            )
        )
        ax.text(
            x + width / 2,
            y + height * 0.62,
            name,
            ha="center",
            va="center",
            fontsize=8.5,
            weight="bold",
            color=style.INK,
        )
        ax.text(
            x + width / 2,
            y + height * 0.28,
            note,
            ha="center",
            va="center",
            fontsize=7,
            color=style.INK_2,
        )
    arrow = {"arrowstyle": "-|>", "color": style.REFERENCE, "lw": 1}
    for x0, x1 in ((24, 26), (49, 51), (74, 76)):
        ax.annotate("", xy=(x1, 29.5), xytext=(x0, 29.5), arrowprops=arrow)
    ax.annotate("", xy=(87.5, 15), xytext=(87.5, 24), arrowprops=arrow)
    ax.annotate("", xy=(74, 9.5), xytext=(76, 9.5), arrowprops=arrow)
    ax.text(
        1,
        12,
        "Two signatures share about the same\nshare of numbers as the texts share\n"
        "shingles (Jaccard similarity).",
        fontsize=7,
        color=style.INK_2,
        va="center",
    )
    ax.text(
        1,
        0.5,
        f"The {low[0]:g} setting uses {low[1]} bands of {low[2]} numbers, so looser "
        "matches also collide.",
        fontsize=7,
        color=style.INK_2,
    )
    style.title(
        fig,
        f"Skills whose {num_perm}-number fingerprints agree on a whole band are joined",
        "How step 2.6 builds near-duplicate clusters with MinHash and LSH",
    )
    return style.save(fig, path, top=0.84)


# --- loaders: read stage outputs into the frames the plots take -------------------------------


def _derived(cfg: Config, name: str) -> Path:
    return resolve(cfg, cfg.paths.derived) / name


def _results(cfg: Config, name: str) -> Path:
    return resolve(cfg, cfg.paths.results) / name


def _sql(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _query(sql: str) -> pd.DataFrame:
    con = duckdb.connect()
    try:
        return con.execute(sql).fetchdf()
    finally:
        con.close()


def _band_settings(cfg: Config) -> list[tuple[float, int, int]]:
    from skillsig.s2_features.minhash_cluster import _band_params

    num_perm = int(cfg.minhash.num_perm)
    out = []
    for threshold in (float(cfg.minhash.lsh_threshold), float(cfg.minhash.lsh_threshold_low)):
        bands, rows = _band_params(threshold, num_perm)
        out.append((threshold, bands, rows))
    return out


def _devsample_groups(cfg: Config) -> pd.DataFrame:
    from skillsig.s0_intake.devsample import _POPULATION_SQL, output_path
    from skillsig.s0_intake.loader import connect

    con = connect(cfg)
    try:
        con.execute("DROP TABLE IF EXISTS _pop")
        con.execute(_POPULATION_SQL)
        full = con.execute("SELECT stratum, count(*) AS full FROM _pop GROUP BY 1").fetchdf()
        sample = con.execute(
            "SELECT stratum, count(*) AS sample FROM read_parquet(?) GROUP BY 1",
            [output_path(cfg).as_posix()],
        ).fetchdf()
    finally:
        con.close()
    return full.merge(sample, on="stratum", how="left").fillna({"sample": 0})


def _cluster_sizes(cfg: Config) -> pd.Series:
    threshold = float(cfg.minhash.lsh_threshold)
    frame = _query(
        f"""
        SELECT cluster_id, max(cluster_size) AS size
        FROM read_parquet({_sql(_derived(cfg, "s2_clusters.parquet"))})
        WHERE abs(threshold - {threshold!r}) < 1e-9
        GROUP BY cluster_id
        """
    )
    return frame["size"].astype(int)


# --- builders: load one figure's inputs, draw it, return the rows it shows ------------------


def _csv(cfg: Config, name: str) -> pd.DataFrame:
    return pd.read_csv(_results(cfg, name))


def _build_template_provenance(cfg: Config, path: Path) -> int:
    frame = _csv(cfg, "s4_template_provenance.csv")
    plot_template_provenance(frame, path)
    return len(frame)


def _build_evidence_matrix(cfg: Config, path: Path) -> int:
    frame = _csv(cfg, "s3_evidence_matrix.csv")
    plot_evidence_matrix(frame, path)
    return len(frame)


def _build_minhash_flow(cfg: Config, path: Path) -> int:
    main, low = _band_settings(cfg)
    plot_minhash_flow(
        int(cfg.minhash.shingle_words), int(cfg.minhash.num_perm), main[1], main[2], low, path
    )
    return 2


def _build_cluster_sizes(cfg: Config, path: Path) -> int:
    bins = cluster_bins(_cluster_sizes(cfg), int(cfg.thresholds.templated_copies_min))
    plot_cluster_sizes(bins, float(cfg.minhash.lsh_threshold), path)
    return len(bins)


def _build_lsh_curve(cfg: Config, path: Path) -> int:
    settings = _band_settings(cfg)
    plot_lsh_curve(settings, path)
    return len(settings)


def _build_scaffold_overlap(cfg: Config, path: Path) -> int:
    frame = _csv(cfg, "s4_scaffold_overlap.csv")
    plot_scaffold_overlap(frame, path)
    return len(frame)


def _build_rules_vs_baselines(cfg: Config, path: Path) -> int:
    frame = _csv(cfg, "s4_spot_precision.csv")
    plot_rules_vs_baselines(frame, path)
    return len(frame)


def _build_disclosure_recall(cfg: Config, path: Path) -> int:
    frame = _csv(cfg, "s4_disclosure_recall.csv")
    plot_disclosure_recall(frame, path)
    return len(frame)


def _build_spot_check(cfg: Config, path: Path) -> int:
    frame = _csv(cfg, "s4_spot_precision.csv")
    plot_spot_check(frame, path)
    return len(frame)


def _build_label_confidence(cfg: Config, path: Path) -> int:
    frame = label_confidence(_csv(cfg, "s3_v2_v21_crosstab.csv"))
    plot_label_confidence(frame, path)
    return len(frame)


def _build_text_methods(cfg: Config, path: Path) -> int:
    frame = _csv(cfg, "s3_text_methods.csv")
    plot_text_methods(frame, path)
    return len(frame)


def _build_roc_curves(cfg: Config, path: Path) -> int:
    oof = pd.read_parquet(_derived(cfg, "s3_text_methods_oof.parquet"))
    points = roc_points(oof, [m for m in ROC_METHODS if m in oof.columns])
    plot_roc_curves(points, path)
    return len(points)


def _build_stability(cfg: Config, path: Path) -> int:
    frame = _csv(cfg, "s4_stability.csv")
    plot_stability(frame, path)
    return len(frame)


def _build_label_versions(cfg: Config, path: Path) -> int:
    frame = label_versions(_csv(cfg, "s3_v1_v2_crosstab.csv"), _csv(cfg, "s3_v2_v21_crosstab.csv"))
    plot_label_versions(frame, path)
    return len(frame)


def _build_devsample_groups(cfg: Config, path: Path) -> int:
    groups = _devsample_groups(cfg)
    plot_devsample_groups(groups, path)
    return len(groups)


def _build_repository_kinds(cfg: Config, path: Path) -> int:
    repo = _sql(_derived(cfg, "s2_repo.parquet"))
    frame = _query(
        f"SELECT repo_kind, count(*) AS repos, sum(files) AS files FROM read_parquet({repo}) "
        "GROUP BY 1"
    )
    plot_repository_kinds(frame, path)
    return len(frame)


def _build_skill_types(cfg: Config, path: Path) -> int:
    types = _sql(_derived(cfg, "s2_types.parquet"))
    frame = _query(f"SELECT skill_type, count(*) AS n FROM read_parquet({types}) GROUP BY 1")
    plot_skill_types(frame, path)
    return len(frame)


# --- the figure list and the stage runner ----------------------------------------------------


@dataclass(frozen=True)
class Figure:
    name: str
    inputs: Callable[[Config], list[Path]]
    build: Callable[[Config, Path], int]


def _results_inputs(*names: str) -> Callable[[Config], list[Path]]:
    return lambda cfg: [_results(cfg, n) for n in names]


def _derived_inputs(*names: str) -> Callable[[Config], list[Path]]:
    return lambda cfg: [_derived(cfg, n) for n in names]


def _no_inputs(cfg: Config) -> list[Path]:
    return []


def _devsample_inputs(cfg: Config) -> list[Path]:
    return [_derived(cfg, "s0_devsample_ids.parquet"), resolve(cfg, cfg.paths.raw) / "artifacts"]


# Prefix = where the chart sits in docs/report/FINDINGS.md: f0 primary evidence view, f1-f5
# findings, a/b appendices.
FIGURES: tuple[Figure, ...] = (
    Figure(
        "f0_evidence_dimensions.png",
        _results_inputs("s3_evidence_matrix.csv"),
        _build_evidence_matrix,
    ),
    Figure(
        "f1_template_provenance.png",
        _results_inputs("s4_template_provenance.csv"),
        _build_template_provenance,
    ),
    Figure("f1_minhash_flow.png", _no_inputs, _build_minhash_flow),
    Figure("f1_cluster_sizes.png", _derived_inputs("s2_clusters.parquet"), _build_cluster_sizes),
    Figure("f1_lsh_s_curve.png", _no_inputs, _build_lsh_curve),
    Figure(
        "f1_scaffold_overlap.png",
        _results_inputs("s4_scaffold_overlap.csv"),
        _build_scaffold_overlap,
    ),
    Figure(
        "f2_rules_vs_baselines.png",
        _results_inputs("s4_spot_precision.csv"),
        _build_rules_vs_baselines,
    ),
    Figure(
        "f2_disclosure_recall.png",
        _results_inputs("s4_disclosure_recall.csv"),
        _build_disclosure_recall,
    ),
    Figure("f3_spot_check.png", _results_inputs("s4_spot_precision.csv"), _build_spot_check),
    Figure(
        "f3_label_confidence.png",
        _results_inputs("s3_v2_v21_crosstab.csv"),
        _build_label_confidence,
    ),
    Figure("f4_text_methods_auc.png", _results_inputs("s3_text_methods.csv"), _build_text_methods),
    Figure("f4_roc_curves.png", _derived_inputs("s3_text_methods_oof.parquet"), _build_roc_curves),
    Figure("f5_stability.png", _results_inputs("s4_stability.csv"), _build_stability),
    Figure(
        "a_label_versions.png",
        _results_inputs("s3_v1_v2_crosstab.csv", "s3_v2_v21_crosstab.csv"),
        _build_label_versions,
    ),
    Figure("b_devsample_groups.png", _devsample_inputs, _build_devsample_groups),
    Figure("b_repository_kinds.png", _derived_inputs("s2_repo.parquet"), _build_repository_kinds),
    Figure("b_skill_types.png", _derived_inputs("s2_types.parquet"), _build_skill_types),
)


def output_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.figures) / "report"


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Draw every report figure whose inputs exist; skip those that are up to date."""
    if dev:
        log.info("report figures read the stage outputs as they are; dev mode changes nothing")
    outputs: list[Path] = []
    rows: dict[str, int] = {}
    built = 0
    for figure in FIGURES:
        path = output_dir(cfg) / figure.name
        inputs = figure.inputs(cfg)
        missing = [p for p in inputs if not p.exists()]
        if missing:
            log.warning("skip %s: missing %s", figure.name, missing[0])
            continue
        outputs.append(path)
        if not force and inputs_unchanged(path, cfg=cfg, inputs=inputs):
            continue
        count = figure.build(cfg, path)
        write_manifest(path, cfg=cfg, inputs=inputs, rows=count)
        rows[figure.name] = count
        built += 1
        log.info("wrote %s", path)
    return StageResult("s6_report", outputs, skipped=built == 0, rows=rows)
