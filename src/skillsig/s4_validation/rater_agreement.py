"""
STAGE: s4_validation
MODULE: rater_agreement
PURPOSE: Agreement between independent raters (A, B, C) on shared spot-check items.
INPUTS: spot_{A,B,C}_done.xlsx rows, already read by spot_check
OUTPUTS: summary dict, 4x4 confusion and paired review rows (written by spot_check)
RQ LINK: RQ1 (how reliable the human reference is)

Independent answers only; never adjudicates the original answers.
"""

from __future__ import annotations

import pandas as pd

from skillsig.s4_validation.stats import cohens_kappa, wilson_interval

LABELS = ("highly_templated", "agent_assisted", "likely_human", "insufficient_evidence")


def validate(sheet: pd.DataFrame, allowed_ids: set[str], rater: str) -> pd.DataFrame:
    """Reject ambiguous joins and invalid answers rather than silently dropping them."""
    out = sheet.copy()
    for name in ("sample_id", "your_label"):
        if name not in out:
            raise ValueError(f"{rater}: missing column {name}")
        out[name] = out[name].fillna("").astype(str).str.strip()
    out["your_label"] = out["your_label"].str.lower()
    if out["sample_id"].eq("").any() or out["sample_id"].duplicated().any():
        raise ValueError(f"{rater}: blank or duplicate sample_id")
    unknown = set(out["sample_id"]) - allowed_ids
    if unknown:
        raise ValueError(f"{rater}: unexpected sample_ids: {sorted(unknown)}")
    bad = set(out["your_label"]) - {*LABELS, ""}
    if bad:
        raise ValueError(f"{rater}: invalid labels: {sorted(bad)}")
    for name in ("your_confidence", "notes"):
        if name not in out:
            out[name] = ""
        out[name] = out[name].fillna("").astype(str)
    return out


def compare(a: pd.DataFrame, b: pd.DataFrame) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """Same-item agreement, full 4x4 confusion, and private paired review evidence."""
    columns = ["sample_id", "your_label", "your_confidence", "notes"]
    left = a.loc[a["your_label"].ne(""), columns]
    right = b.loc[b["your_label"].ne(""), columns]
    pairs = left.merge(right, on="sample_id", suffixes=("_a", "_b"), validate="one_to_one")
    pairs = pairs.sort_values("sample_id").reset_index(drop=True)
    pairs["agree"] = pairs["your_label_a"] == pairs["your_label_b"]
    n, matches = len(pairs), int(pairs["agree"].sum())
    low, high = wilson_interval(matches, n)
    summary = {
        "n_a_labelled": len(left),
        "n_b_labelled": len(right),
        "n_overlap": n,
        "n_agree": matches,
        "n_disagree": n - matches,
        "raw_agreement": matches / n if n else float("nan"),
        "raw_agreement_ci_low": low,
        "raw_agreement_ci_high": high,
        "kappa": cohens_kappa(pairs["your_label_a"].tolist(), pairs["your_label_b"].tolist()),
        "overlap_label_counts_a": pairs["your_label_a"].value_counts().to_dict(),
        "overlap_label_counts_b": pairs["your_label_b"].value_counts().to_dict(),
        "reference": "independent answers; no adjudication",
    }
    counts = pairs.groupby(["your_label_a", "your_label_b"]).size()
    confusion = pd.DataFrame(
        [
            {"label_a": x, "label_b": y, "n": int(counts.get((x, y), 0))}
            for x in LABELS
            for y in LABELS
        ]
    )
    return summary, confusion, pairs


def pairwise(
    raters: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """compare() for every pair of raters: summaries, 4x4 confusions and disagreeing rows."""
    names = list(raters)
    summaries, confusions, disagreements = [], [], []
    for i, x in enumerate(names):
        for y in names[i + 1 :]:
            summary, confusion, pairs = compare(raters[x], raters[y])
            summaries.append(
                {
                    "rater_x": x,
                    "rater_y": y,
                    **{k: summary[k] for k in _PAIR_FIELDS},
                    "reference": summary["reference"],
                }
            )
            confusion = confusion.rename(columns={"label_a": "label_x", "label_b": "label_y"})
            confusions.append(confusion.assign(rater_x=x, rater_y=y))
            rows = pairs.loc[~pairs["agree"]].drop(columns="agree")
            rows.columns = [c.removesuffix("_a") + "_x" if c.endswith("_a") else c for c in rows]
            rows.columns = [c.removesuffix("_b") + "_y" if c.endswith("_b") else c for c in rows]
            disagreements.append(rows.assign(rater_x=x, rater_y=y))
    order = ["rater_x", "rater_y"]
    confusion = pd.concat(confusions, ignore_index=True)
    rows = pd.concat(disagreements, ignore_index=True)
    return (
        pd.DataFrame(summaries),
        confusion[order + [c for c in confusion if c not in order]],
        rows[order + [c for c in rows if c not in order]],
    )


_PAIR_FIELDS = (
    "n_overlap",
    "n_agree",
    "n_disagree",
    "raw_agreement",
    "raw_agreement_ci_low",
    "raw_agreement_ci_high",
    "kappa",
)
