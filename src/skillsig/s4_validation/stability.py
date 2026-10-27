"""
STAGE: s4_validation
MODULE: stability
PURPOSE: E5 - how v2.1 label shares move when one threshold at a time is changed.
INPUTS: s3_labels_v1 (copies), s2_commit, s2_template, s2_clusters (0.8 and 0.6), artifacts, v2.1
OUTPUTS: results/s4_stability.csv, results/s4_stability.json
RQ LINK: RQ1 (robustness: a label share that swings with a reasonable threshold is not a finding)

Labels are recomputed with the real rules_v2.label_row and rules_v2_1.label_row_v21 on a fixed
seeded sample of skills, one threshold varied at a time. At the default setting the recomputed
labels must match the stored v2.1 labels; the agreement is reported as a self-check.
"""

from __future__ import annotations

import dataclasses
import logging

import duckdb
import pandas as pd

from skillsig.config import Config
from skillsig.provenance import StageResult
from skillsig.s3_labeling.rules_v2 import label_row
from skillsig.s3_labeling.rules_v2_1 import label_row_v21
from skillsig.s4_validation import _io

log = logging.getLogger(__name__)

SAMPLE_N = 200_000
LABELS = ("highly_templated", "agent_assisted", "likely_human", "insufficient_evidence")
# Within-label confidence shares: thresholds that only move confidence show up here.
CONFIDENCE_ROWS = ("likely_human:medium", "agent_assisted:low", "highly_templated:high")
# (parameter, values); the config value is the default and is always included.
GRID: tuple[tuple[str, tuple[float, ...]], ...] = (
    ("templated_copies_min", (8, 12)),
    ("templated_jaccard_min", (0.7, 0.9)),
    ("bulk_batch_min", (4, 8)),
    ("human_min_commits", (2, 4)),
    ("human_min_span_days", (3, 14)),
    ("cluster_threshold", (0.6,)),
)


def feature_frame(con: duckdb.DuckDBPyConnection, cfg: Config, sample_n: int) -> pd.DataFrame:
    """Seeded sample of skills with every column the v2 and v2.1 rules read."""
    primary = float(cfg.minhash.lsh_threshold)
    low = float(cfg.minhash.lsh_threshold_low)
    d = lambda name: _io.sql(_io.derived(cfg, name))  # noqa: E731
    query = f"""
        WITH sample AS (
            SELECT file_sha, label AS stored_label
            FROM read_parquet({d("s3_labels_v2_1.parquet")})
            ORDER BY md5(file_sha || '{int(cfg.seed)}') LIMIT {int(sample_n)}
        ),
        v1 AS (
            SELECT file_sha, max(copies) AS copies FROM read_parquet({d("s3_labels_v1.parquet")})
            WHERE file_sha IN (SELECT file_sha FROM sample) GROUP BY file_sha
        ),
        tmpl AS (
            SELECT file_sha, match_kind, est_jaccard, nearest_lineage_id FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY file_sha ORDER BY match_kind NULLS LAST,
                    nearest_lineage_id NULLS LAST, est_jaccard DESC NULLS LAST) AS rn
                FROM read_parquet({d("s2_template.parquet")})
                WHERE file_sha IN (SELECT file_sha FROM sample)
            ) WHERE rn = 1
        ),
        cl AS (
            SELECT file_sha,
                   max(CASE WHEN abs(threshold - {primary!r}) < 1e-9 THEN cluster_size END)
                       AS cluster_size,
                   max(CASE WHEN abs(threshold - {low!r}) < 1e-9 THEN cluster_size END)
                       AS cluster_size_low
            FROM read_parquet({d("s2_clusters.parquet")})
            WHERE file_sha IN (SELECT file_sha FROM sample) GROUP BY file_sha
        ),
        art AS (
            SELECT file_sha, min(repo_full_name) AS repo_full_name,
                   any_value(filename) AS filename, any_value(body_chars) AS body_chars
            FROM read_parquet({_io.sql(_io.artifacts_glob(cfg))})
            WHERE coalesce(TRY_CAST(dedup_primary AS INTEGER), 0) = 1
              AND file_sha IN (SELECT file_sha FROM sample)
            GROUP BY file_sha
        )
        SELECT s.file_sha, s.stored_label, coalesce(v1.copies, 1) AS copies,
               t.match_kind, t.est_jaccard, cl.cluster_size, cl.cluster_size_low,
               (t.nearest_lineage_id LIKE '%::%'
                AND a.repo_full_name = split_part(t.nearest_lineage_id, '::', 1)
                AND t.match_kind IN ('exact', 'near')) AS registry_origin,
               a.filename, a.body_chars, k.*
        FROM sample s
        LEFT JOIN v1 USING (file_sha)
        LEFT JOIN tmpl t USING (file_sha)
        LEFT JOIN cl USING (file_sha)
        LEFT JOIN art a USING (file_sha)
        LEFT JOIN read_parquet({d("s2_commit.parquet")}) k USING (file_sha)
        ORDER BY s.file_sha
    """
    frame = con.execute(query).fetchdf()
    return frame.loc[:, ~frame.columns.duplicated()]


def relabel(frame: pd.DataFrame, cfg: Config, *, cluster_column: str = "cluster_size") -> list[str]:
    """v2.1 labels for every row under `cfg`, via the real rule functions."""
    return [
        label for label, _ in relabel_with_confidence(frame, cfg, cluster_column=cluster_column)
    ]


def relabel_with_confidence(
    frame: pd.DataFrame, cfg: Config, *, cluster_column: str = "cluster_size"
) -> list[tuple[str, str]]:
    """(label, confidence) for every row under `cfg`."""
    records = frame.to_dict("records")
    labels = []
    for row in records:
        feat = {k: (None if _missing(v) else v) for k, v in row.items()}
        feat["cluster_size"] = feat.get(cluster_column)
        v2 = label_row(feat, cfg)
        result = label_row_v21(v2, feat, cfg)
        labels.append((result["label"], result["confidence"]))
    return labels


def shares(labels: list[str]) -> dict[str, float]:
    n = len(labels)
    return {label: sum(x == label for x in labels) / n if n else 0.0 for label in LABELS}


def settings(cfg: Config) -> list[tuple[str, float, Config, str]]:
    """(parameter, value, config, cluster column) for the default and each one-at-a-time change."""
    out = [("default", 0.0, cfg, "cluster_size")]
    for param, values in GRID:
        for value in values:
            if param == "cluster_threshold":
                out.append((param, value, cfg, "cluster_size_low"))
                continue
            current = getattr(cfg.thresholds, param)
            cast = type(current)(value)
            if cast == current:
                continue
            changed = dataclasses.replace(
                cfg, thresholds=dataclasses.replace(cfg.thresholds, **{param: cast})
            )
            out.append((param, cast, changed, "cluster_size"))
    return out


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write the E5 stability table: label shares per setting and change vs. default."""
    inputs = [
        _io.derived(cfg, n)
        for n in (
            "s3_labels_v2_1.parquet",
            "s3_labels_v1.parquet",
            "s2_template.parquet",
            "s2_clusters.parquet",
            "s2_commit.parquet",
        )
    ]
    _io.require([(p, "python -m skillsig label --only rules_v2_1") for p in inputs])
    outputs = [_io.results(cfg, "s4_stability.csv"), _io.results(cfg, "s4_stability.json")]
    if not force and _io.up_to_date(outputs, cfg, inputs):
        return StageResult("s4_validation", outputs, skipped=True, rows=_io.saved_rows(outputs))
    con = duckdb.connect()
    try:
        frame = feature_frame(con, cfg, SAMPLE_N)
    finally:
        con.close()
    rows = []
    default_shares: dict[str, float] = {}
    self_check = float("nan")
    for param, value, setting_cfg, cluster_col in settings(cfg):
        pairs = relabel_with_confidence(frame, setting_cfg, cluster_column=cluster_col)
        labels = [label for label, _ in pairs]
        got = shares(labels)
        got.update(_confidence_shares(pairs))
        if param == "default":
            default_shares = got
            self_check = float((pd.Series(labels) == frame["stored_label"].to_numpy()).mean())
        for label in (*LABELS, *CONFIDENCE_ROWS):
            rows.append(
                {
                    "parameter": param,
                    "value": value,
                    "label": label,
                    "share": got[label],
                    "delta_pp": 100 * (got[label] - default_shares[label]),
                }
            )
        log.info("E5: %s=%s done", param, value)
    table = pd.DataFrame(rows)
    swing = table.groupby("label")["delta_pp"].agg(lambda s: float(s.abs().max())).to_dict()
    label_rows = table[table["label"].isin(LABELS)]
    worst = label_rows.loc[label_rows["delta_pp"].abs().idxmax()]
    summary = {
        "sample_n": len(frame),
        "default_shares": default_shares,
        "max_abs_change_pp": swing,
        "largest_change": {
            "parameter": worst["parameter"],
            "value": worst["value"],
            "label": worst["label"],
            "delta_pp": worst["delta_pp"],
        },
        "self_check_agreement_with_stored_v21": self_check,
    }
    _io.write_csv(table, outputs[0], cfg, inputs)
    _io.write_json(summary, outputs[1], cfg, inputs)
    return StageResult("s4_validation", outputs, rows={"s4_stability": len(table)})


def _confidence_shares(pairs: list[tuple[str, str]]) -> dict[str, float]:
    """Share of each label's rows at a given confidence (rows named label:confidence)."""
    out = {}
    for key in CONFIDENCE_ROWS:
        label, confidence = key.split(":")
        within = [c for lab, c in pairs if lab == label]
        out[key] = sum(c == confidence for c in within) / len(within) if within else 0.0
    return out


def _missing(value: object) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False
