"""
STAGE: s4_validation
MODULE: template_provenance
PURPOSE: E2 - exact registry recovery separately from near-candidate agreement.
INPUTS: s2_template, s3_labels_v1 (copies), s2_clusters (primary threshold), s3_labels_v2_1
OUTPUTS: results/s4_template_provenance.csv, results/s4_template_unexplained.csv
RQ LINK: RQ1 (validity of the highly_templated signal against a known answer)

Exact registry matches establish content identity; origins are excluded. Near candidates
share the MinHash mechanism with clustering, so their agreement is not independent recall.
Neither the positive-only exact reference nor similarity-conditioned near cases establish
overall precision, copying direction or true authorship. The mixed row is exploratory.
"""

from __future__ import annotations

import logging

import duckdb
import pandas as pd

from skillsig.config import Config
from skillsig.provenance import StageResult
from skillsig.s4_validation import _io
from skillsig.s4_validation.stats import wilson_interval

log = logging.getLogger(__name__)

_UNEXPLAINED_NOTE = "agreement, not precision: sources outside the 31 mined repos are not known"


def reference_notes(kind: str) -> dict[str, str]:
    """Near matches share a similarity mechanism with the detector; they are not truth."""
    if kind == "exact":
        return {
            "reference_type": "exact_blob_identity",
            "metric_scope": "recovery_on_exact_registry_matches",
            "interpretation": "Positive reference for identical content, not authorship, "
            "copying direction or overall precision; registry origins excluded.",
        }
    if kind == "near":
        return {
            "reference_type": "similarity_conditioned_candidates",
            "metric_scope": "agreement_on_near_match_candidates",
            "interpretation": "Similarity-selected reference shares the MinHash mechanism; "
            "not independent recall or verified copying. Paired review pending.",
        }
    return {
        "reference_type": "mixed_exploratory",
        "metric_scope": "agreement_on_mixed_reference",
        "interpretation": "Combines exact matches and unverified near candidates; "
        "not independent recall or overall precision.",
    }


def provenance_frame(con: duckdb.DuckDBPyConnection, cfg: Config) -> pd.DataFrame:
    """file_sha, registry_kind (exact|near|none), copies, cluster_size, copy_rule, cluster_rule."""
    minimum = int(cfg.thresholds.templated_copies_min)
    jaccard = float(cfg.thresholds.templated_jaccard_min)
    threshold = float(cfg.minhash.lsh_threshold)
    query = f"""
        WITH tmpl AS (
            SELECT file_sha, bool_or(match_kind = 'exact') AS exact_match,
                   bool_or(match_kind = 'near' AND est_jaccard >= {jaccard!r}) AS near_match
            FROM read_parquet({_io.sql(_io.derived(cfg, "s2_template.parquet"))})
            GROUP BY file_sha
        ),
        v1 AS (
            SELECT file_sha, max(copies) AS copies
            FROM read_parquet({_io.sql(_io.derived(cfg, "s3_labels_v1.parquet"))})
            GROUP BY file_sha
        ),
        cl AS (
            SELECT file_sha, max(cluster_size) AS cluster_size
            FROM read_parquet({_io.sql(_io.derived(cfg, "s2_clusters.parquet"))})
            WHERE abs(threshold - {threshold!r}) < 1e-9
            GROUP BY file_sha
        )
        SELECT l.file_sha,
               CASE
                   WHEN t.exact_match THEN 'exact'
                   WHEN t.near_match THEN 'near'
                   ELSE 'none'
               END AS registry_kind,
               coalesce(list_contains(l.reasons, 'registry_origin'), false) AS registry_origin,
               coalesce(v1.copies, 1) AS copies,
               coalesce(cl.cluster_size, 1) AS cluster_size,
               l.label AS label_v21
        FROM read_parquet({_io.sql(_io.derived(cfg, "s3_labels_v2_1.parquet"))}) l
        LEFT JOIN tmpl t USING (file_sha)
        LEFT JOIN v1 USING (file_sha)
        LEFT JOIN cl USING (file_sha)
    """
    frame = con.execute(query).fetchdf()
    frame["copy_rule"] = frame["copies"] >= minimum
    frame["cluster_rule"] = frame["cluster_size"] >= minimum
    return frame


def recall_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Exact-reference recovery and separately scoped near/mixed agreement."""
    known = frame[(frame["registry_kind"] != "none") & ~frame["registry_origin"].astype(bool)]
    rows = []
    for name, part in (
        ("exact", known[known["registry_kind"] == "exact"]),
        ("near", known[known["registry_kind"] == "near"]),
        ("exact_or_near", known),
    ):
        n = len(part)
        copy_hits = int(part["copy_rule"].sum())
        cluster_hits = int(part["cluster_rule"].sum())
        either = int((part["copy_rule"] | part["cluster_rule"]).sum())
        low, high = wilson_interval(either, n)
        copy_low, copy_high = wilson_interval(copy_hits, n)
        cluster_low, cluster_high = wilson_interval(cluster_hits, n)
        rows.append(
            {
                "known_set": name,
                "n": n,
                "copy_rule_hits": copy_hits,
                "copy_recall": copy_hits / n if n else float("nan"),
                "cluster_rule_hits": cluster_hits,
                "cluster_recall": cluster_hits / n if n else float("nan"),
                "either_hits": either,
                "either_recall": either / n if n else float("nan"),
                "ci_low": low,
                "ci_high": high,
                "copy_ci_low": copy_low,
                "copy_ci_high": copy_high,
                "cluster_ci_low": cluster_low,
                "cluster_ci_high": cluster_high,
                **reference_notes(name),
            }
        )
    return pd.DataFrame(rows)


def unexplained_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Among skills a generic rule flags, the share that traces to a registry source."""
    rows = []
    for rule, mask in (
        ("copy_rule", frame["copy_rule"]),
        ("cluster_rule", frame["cluster_rule"]),
        ("either", frame["copy_rule"] | frame["cluster_rule"]),
    ):
        part = frame[mask]
        matched = int((part["registry_kind"] != "none").sum())
        rows.append(
            {
                "rule": rule,
                "n": len(part),
                "registry_matched": matched,
                "share_registry": matched / len(part) if len(part) else float("nan"),
                "note": _UNEXPLAINED_NOTE,
            }
        )
    return pd.DataFrame(rows)


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write the E2 recall and agreement tables over the full population."""
    inputs = [
        _io.derived(cfg, "s2_template.parquet"),
        _io.derived(cfg, "s3_labels_v1.parquet"),
        _io.derived(cfg, "s2_clusters.parquet"),
        _io.derived(cfg, "s3_labels_v2_1.parquet"),
    ]
    _io.require(
        list(
            zip(
                inputs,
                [
                    "python -m skillsig features --only template_sim",
                    "python -m skillsig label --only rules_v1",
                    "python -m skillsig dedup",
                    "python -m skillsig label --only rules_v2_1",
                ],
                strict=True,
            )
        )
    )
    outputs = [
        _io.results(cfg, "s4_template_provenance.csv"),
        _io.results(cfg, "s4_template_unexplained.csv"),
    ]
    if not force and _io.up_to_date(outputs, cfg, inputs):
        return StageResult("s4_validation", outputs, skipped=True, rows=_io.saved_rows(outputs))
    con = duckdb.connect()
    try:
        frame = provenance_frame(con, cfg)
    finally:
        con.close()
    recall = recall_table(frame)
    unexplained = unexplained_table(frame)
    _io.write_csv(recall, outputs[0], cfg, inputs)
    _io.write_csv(unexplained, outputs[1], cfg, inputs)
    best = recall.set_index("known_set").loc["exact"]
    log.info(
        "E2: generic rules recover %s of %s exact registry matches (%.3f); "
        "near matches are similarity-conditioned agreement only",
        best["either_hits"],
        best["n"],
        best["either_recall"],
    )
    return StageResult(
        "s4_validation",
        outputs,
        rows={"s4_template_provenance": len(recall), "s4_template_unexplained": len(unexplained)},
    )
