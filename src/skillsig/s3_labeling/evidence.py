"""
STAGE: s3_labeling
MODULE: evidence
PURPOSE: Report reuse and disclosed agent involvement as independent evidence dimensions.
INPUTS: s3_labels_v2_1, s3_labels_v1 (copies), s2_commit, s2_template, s2_clusters, s2_scaffold
OUTPUTS: s3_evidence; results/s3_evidence_matrix.csv, _signals.csv, _legacy_crosstab.csv,
         _summary.json
RQ LINK: RQ1 (copying and agent use observed side by side rather than as exclusive labels)

No authorship verdict or confidence penalty is assigned to their co-occurrence.
Historical labels and the independent blind annotation remain unchanged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import duckdb

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, write_manifest

log = logging.getLogger(__name__)

REUSE_SIGNALS = (
    "exact_content_shared",
    "near_duplicate_signal",
    "scaffold_signal",
    "no_reuse_signal_observed",
    "reuse_not_assessed",
)
AGENT_SIGNALS = ("disclosed", "no_disclosure_observed", "history_unavailable")
FLAGS = (
    "flag_exact_content_shared",
    "flag_registry_exact",
    "flag_registry_near",
    "flag_near_duplicate_cluster",
    "flag_scaffold",
)
SOURCES = {
    "labels": "s3_labels_v2_1.parquet",
    "copies": "s3_labels_v1.parquet",
    "commits": "s2_commit.parquet",
    "templates": "s2_template.parquet",
    "clusters": "s2_clusters.parquet",
    "scaffolds": "s2_scaffold.parquet",
}


def _sql(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def build(con: duckdb.DuckDBPyConnection, cfg: Config, *, dev: bool) -> int:
    """Build a temporary evidence table without materializing the population in pandas."""
    derived = resolve(cfg, cfg.paths.derived)
    for name, filename in SOURCES.items():
        path = derived / filename
        if not path.is_file():
            raise FileNotFoundError(f"missing {path}; build its upstream stage first")
        con.execute(f"CREATE TEMP VIEW {name} AS SELECT * FROM read_parquet({_sql(path)})")
    for name in ("labels", "copies", "commits", "scaffolds"):
        n, unique = con.execute(f"SELECT count(*), count(DISTINCT file_sha) FROM {name}").fetchone()
        if n != unique:
            raise ValueError(f"{name}: null or duplicate file_sha")
    missing = con.execute("""
        SELECT count(*) FROM labels l
        LEFT JOIN copies v USING (file_sha) LEFT JOIN commits k USING (file_sha)
        WHERE v.file_sha IS NULL OR k.file_sha IS NULL OR v.copies IS NULL
    """).fetchone()[0]
    if missing:
        raise ValueError(f"missing copies or commit coverage for {missing} labelled skills")
    sample = ""
    if dev:
        path = derived / "s0_devsample_ids.parquet"
        if not path.is_file():
            raise FileNotFoundError(path)
        sample = f"WHERE l.file_sha IN (SELECT file_sha FROM read_parquet({_sql(path)}))"
    threshold = repr(float(cfg.minhash.lsh_threshold))
    near_min = repr(float(cfg.thresholds.templated_jaccard_min))
    bulk_min = int(cfg.thresholds.bulk_batch_min)
    con.execute(f"""
        CREATE TEMP TABLE evidence AS
        WITH t AS (
            SELECT file_sha, bool_or(match_kind = 'exact') AS registry_exact,
                bool_or(match_kind = 'near' AND est_jaccard >= {near_min}) AS registry_near
            FROM templates GROUP BY file_sha
        ), cl AS (
            SELECT file_sha, max(cluster_size) AS cluster_size FROM clusters
            WHERE abs(threshold - {threshold}) < 1e-9 GROUP BY file_sha
        ), signals AS (
            SELECT l.file_sha, l.label AS legacy_label,
                l.templated_basis AS legacy_templated_basis, l.conflict AS legacy_conflict,
                coalesce(list_contains(l.reasons, 'registry_origin'), false) AS registry_origin,
                v.copies, coalesce(cl.cluster_size, 1) AS cluster_size,
                t.file_sha IS NOT NULL OR sc.file_sha IS NOT NULL AS text_assessed,
                coalesce(k.history_fetched, false) AS history_fetched,
                coalesce(l.flag_disclosed_agent, false) AS disclosed_agent,
                coalesce(k.ai_trailer_first_ext, false) OR
                    coalesce(k.ai_trailer_first, false) AS disclosure_first,
                coalesce(k.ai_trailer_last_ext, false) OR
                    coalesce(k.ai_trailer_last, false) AS disclosure_last,
                coalesce(k.coding_agent_bot, false) AS coding_agent_bot,
                k.batch_size,
                coalesce(k.batch_size >= {bulk_min}, false) AS bulk_commit,
                v.copies >= 2 AS flag_exact_content_shared,
                coalesce(t.registry_exact, false) AND NOT registry_origin AS flag_registry_exact,
                coalesce(t.registry_near, false) AND NOT registry_origin AS flag_registry_near,
                coalesce(cl.cluster_size >= 2, false) AS flag_near_duplicate_cluster,
                sc.scaffold_basis,
                sc.scaffold_basis IS NOT NULL AS flag_scaffold
            FROM labels l JOIN copies v USING (file_sha) JOIN commits k USING (file_sha)
            LEFT JOIN t USING (file_sha) LEFT JOIN cl USING (file_sha)
            LEFT JOIN scaffolds sc USING (file_sha)
            {sample}
        )
        SELECT *,
            CASE WHEN flag_exact_content_shared OR flag_registry_exact THEN 'exact_content_shared'
                 WHEN flag_registry_near OR flag_near_duplicate_cluster THEN 'near_duplicate_signal'
                 WHEN flag_scaffold THEN 'scaffold_signal'
                 WHEN text_assessed THEN 'no_reuse_signal_observed'
                 ELSE 'reuse_not_assessed' END AS reuse_signal,
            CASE WHEN disclosed_agent THEN 'disclosed'
                 WHEN history_fetched THEN 'no_disclosure_observed'
                 ELSE 'history_unavailable' END AS agent_signal,
            (flag_exact_content_shared OR flag_registry_exact OR flag_registry_near OR
             flag_near_duplicate_cluster OR flag_scaffold) AND disclosed_agent
                AS reuse_and_disclosure
        FROM signals ORDER BY file_sha
    """)
    inconsistent = con.execute("""
        SELECT count(*) FROM evidence WHERE
            disclosed_agent != (history_fetched AND
                (disclosure_first OR disclosure_last OR coding_agent_bot))
    """).fetchone()[0]
    if inconsistent:
        raise ValueError(f"disclosure flags disagree with commit inputs for {inconsistent} skills")
    return int(con.execute("SELECT count(*) FROM evidence").fetchone()[0])


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Add a separate evidence artifact; never rewrites historical labels or annotations."""
    derived, results = resolve(cfg, cfg.paths.derived), resolve(cfg, cfg.paths.results)
    inputs = [derived / n for n in SOURCES.values()]
    if dev:
        inputs.append(derived / "s0_devsample_ids.parquet")
    outputs = [
        derived / "s3_evidence.parquet",
        *[
            results / f"s3_evidence_{name}"
            for name in (
                "matrix.csv",
                "signals.csv",
                "legacy_crosstab.csv",
                "summary.json",
            )
        ],
    ]
    if not force and all(inputs_unchanged(p, cfg=cfg, inputs=inputs) for p in outputs):
        return StageResult("s3_labeling.evidence", outputs, skipped=True)
    derived.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        n = build(con, cfg, dev=dev)
        con.execute(f"COPY evidence TO {_sql(outputs[0])} (FORMAT PARQUET, COMPRESSION ZSTD)")
        reuse_values = ",".join(f"('{x}')" for x in REUSE_SIGNALS)
        agent_values = ",".join(f"('{x}')" for x in AGENT_SIGNALS)
        matrix = con.execute(f"""
            WITH grid AS (
                SELECT * FROM (VALUES {reuse_values}) r(reuse_signal)
                CROSS JOIN (VALUES {agent_values}) a(agent_signal)
            ), counts AS (
                SELECT reuse_signal, agent_signal, count(*) AS n FROM evidence GROUP BY ALL
            )
            SELECT g.*, coalesce(c.n, 0) AS n,
                coalesce(c.n, 0) / nullif({n}, 0)::DOUBLE AS share_all,
                coalesce(c.n, 0) / nullif(sum(c.n) OVER
                    (PARTITION BY g.agent_signal), 0)::DOUBLE AS share_within_agent_signal
            FROM grid g LEFT JOIN counts c USING (reuse_signal, agent_signal)
            ORDER BY reuse_signal, agent_signal
        """).fetchdf()
        signals = con.execute(
            " UNION ALL ".join(
                f"SELECT '{flag}' AS signal, agent_signal, count(*) AS n "
                f"FROM evidence WHERE {flag} GROUP BY agent_signal"
                for flag in FLAGS
            )
            + " ORDER BY signal, agent_signal"
        ).fetchdf()
        legacy = con.execute("""
            SELECT legacy_label, reuse_signal, agent_signal, count(*) AS n
            FROM evidence GROUP BY ALL ORDER BY ALL
        """).fetchdf()
        totals = con.execute("""
            SELECT count(*) FILTER (WHERE disclosed_agent),
                count(*) FILTER (WHERE reuse_and_disclosure),
                count(*) FILTER (WHERE legacy_label = 'highly_templated' AND disclosed_agent),
                count(*) FILTER (WHERE agent_signal = 'history_unavailable') FROM evidence
        """).fetchone()
        summary = {
            "n": n,
            "dev": dev,
            "disclosed_agent": totals[0],
            "reuse_and_disclosure": totals[1],
            "legacy_templated_with_disclosure": totals[2],
            "history_unavailable": totals[3],
            "note": "Independent observed evidence; no verified authorship or copying direction.",
            "reuse_summary_order": list(REUSE_SIGNALS),
            "cluster_note": "LSH candidate components, not pairwise-verified similarity families.",
            "scaffold_note": "Measured candidate signal; manual validation remains incomplete.",
            "agent_note": "First/last commit disclosure or coding-agent bot; may concern install "
            "or maintenance. No disclosure does not establish human authorship.",
        }
        for path, frame in zip(outputs[1:4], (matrix, signals, legacy), strict=True):
            frame.to_csv(path, index=False)
        outputs[4].write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        counts = [n, len(matrix), len(signals), len(legacy), 1]
        for path, count in zip(outputs, counts, strict=True):
            write_manifest(path, cfg=cfg, inputs=inputs, rows=count)
        log.info("evidence: %s skills, %s with both reuse signals and disclosure", n, totals[1])
        return StageResult(
            "s3_labeling.evidence",
            outputs,
            rows=dict(
                zip(
                    [p.name for p in outputs],
                    counts,
                    strict=True,
                )
            ),
        )
    finally:
        con.close()
