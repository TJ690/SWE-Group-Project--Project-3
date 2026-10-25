"""
STAGE: s3_labeling
MODULE: rules_v2
PURPOSE: Assign v2 authorship labels with a templated basis, confidence, and reasons.
INPUTS: s3_labels_v1.parquet; s2 commit, repo, structure, wording, template, clusters, and types
OUTPUTS: src/data/derived/s3_labels_v2.parquet, results/s3_v1_v2_crosstab.csv
RQ LINK: RQ1 (heuristic label, templated basis, and confidence)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import duckdb

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_data.devsample import output_path as devsample_path
from skillsig.s3_labeling.rules_v1 import label_v1

log = logging.getLogger(__name__)

LABELS = ("highly_templated", "agent_assisted", "likely_human", "insufficient_evidence")
TEMPLATED_BASIS = ("registry_exact", "registry_near", "cluster", "copy_count", None)

# Fixed near-threshold bands from the v2 rule (not the config cutoffs).
_BATCH_LOW, _BATCH_HIGH = 4, 8
_COUNT_LOW, _COUNT_HIGH = 8, 12
_JACCARD_LOW, _JACCARD_HIGH = 0.7, 0.9
_CONFIDENCE = {"high", "medium", "low"}
_RANK = {"low": 0, "medium": 1, "high": 2}
_BASIS_NAMES = ("registry_exact", "registry_near", "cluster", "copy_count")
_UNIT = "\x1f"
_REC = "\x1e"

_V1 = "python -m skillsig label --only rules_v1"
_COMMIT = "python -m skillsig features --only commit"
_REPO = "python -m skillsig features --only repo_context"
_STRUCTURE = "python -m skillsig features --only structure"
_WORDING = "python -m skillsig features --only wording"
_TEMPLATE = "python -m skillsig features --only template_sim"
_CLUSTERS = "python -m skillsig dedup"
_TYPES = "python -m skillsig features --only skill_type"
_DOWNLOAD = "python src/data/download_data.py"
_DEV_SAMPLE = "python -m skillsig data --dev-sample"


def label_row(feat: Mapping[str, Any], cfg: Config) -> dict:
    """First match wins: templated, then the v1 agent, human, and insufficient rules."""
    fired = _templated_fired(feat, cfg)
    basis_hits = [name for name in _BASIS_NAMES if name in fired]
    v1_label, v1_reasons = label_v1(_v1_feat(feat), cfg)
    if basis_hits:
        label = "highly_templated"
        basis: str | None = basis_hits[0]
        extra = [] if v1_label == "highly_templated" else v1_reasons
    else:
        label = v1_label
        basis = None
        extra = v1_reasons
    reasons = _merge(fired, extra) or ["insufficient_evidence"]
    return {
        "label": label,
        "templated_basis": basis,
        "confidence": _confidence(label, feat, cfg, basis_hits, v1_reasons),
        "reasons": reasons,
    }


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write s3_labels_v2.parquet and the v1 x v2 crosstab. Dev mode keeps dev-sample ids."""
    outputs = _output_paths(cfg)
    inputs = _inputs(cfg, dev=dev)
    _require_inputs(cfg, dev=dev)
    if not force and all(inputs_unchanged(path, cfg=cfg, inputs=inputs) for path in outputs):
        log.info("labels v2 up to date: %s", outputs[0])
        return StageResult(
            stage="s3_labeling", outputs=outputs, skipped=True, rows=_saved_rows(outputs)
        )
    written = _write(cfg, outputs, dev=dev)
    for path, count in zip(outputs, written, strict=True):
        write_manifest(path, cfg=cfg, inputs=inputs, rows=count)
    log.info("wrote %s (%s rows) and %s (%s rows)", outputs[0], written[0], outputs[1], written[1])
    return StageResult(
        stage="s3_labeling",
        outputs=outputs,
        skipped=False,
        rows={"s3_labels_v2": written[0], "s3_v1_v2_crosstab": written[1]},
    )


def _templated_fired(feat: Mapping[str, Any], cfg: Config) -> list[str]:
    """Templated rules that match, in basis order. Origin blocks registry rules only."""
    origin = _flag(feat.get("registry_origin"))
    exact = _text(feat.get("match_kind")) == "exact"
    near = _near(feat, cfg, exact)
    fired: list[str] = []
    if exact and not origin:
        fired.append("registry_exact")
    if near and not origin:
        fired.append("registry_near")
    if origin and (exact or near):
        fired.append("registry_origin")
    minimum = int(cfg.thresholds.templated_copies_min)
    if _at_least(feat.get("cluster_size"), minimum):
        fired.append("cluster")
    if _at_least(feat.get("copies"), minimum):
        fired.append("copy_count")
    return fired


def _near(feat: Mapping[str, Any], cfg: Config, exact: bool) -> bool:
    if exact or _text(feat.get("match_kind")) != "near":
        return False
    score = _optional_float(feat.get("est_jaccard"))
    if score is None:
        return True
    return score >= float(cfg.thresholds.templated_jaccard_min)


def _confidence(
    label: str,
    feat: Mapping[str, Any],
    cfg: Config,
    bases: list[str],
    v1_reasons: list[str],
) -> str:
    # An exact registry match, or two templated bases, outranks one adjacent signal.
    if label == "highly_templated":
        return _templated_confidence(feat, bases)
    if label == "agent_assisted":
        return _agent_confidence(feat, v1_reasons)
    if label == "likely_human":
        return "low" if _sparse(feat) else "medium"
    return _raise_to_floor("low", cfg)


def _templated_confidence(feat: Mapping[str, Any], bases: list[str]) -> str:
    if "registry_exact" in bases or len(bases) >= 2:
        return "high"
    if len(bases) == 1 and _adjacent_basis(feat, bases[0]):
        return "low"
    if len(bases) == 1:
        return "medium"
    return "low"


def _adjacent_basis(feat: Mapping[str, Any], basis: str) -> bool:
    if basis == "registry_near":
        score = _optional_float(feat.get("est_jaccard"))
        return score is not None and _JACCARD_LOW <= score <= _JACCARD_HIGH
    if basis == "cluster":
        return _COUNT_LOW <= _int(feat.get("cluster_size")) <= _COUNT_HIGH
    if basis == "copy_count":
        return _COUNT_LOW <= _int(feat.get("copies")) <= _COUNT_HIGH
    return False


def _agent_confidence(feat: Mapping[str, Any], reasons: list[str]) -> str:
    if _batch_adjacent(feat) or _sparse(feat):
        return "low"
    signals = int("coding_agent_bot" in reasons)
    trailer = any(r == "ai_trailer_first" or r.startswith("ai_trailer_first:") for r in reasons)
    signals += int(trailer)
    if signals >= 2:
        return "high"
    if signals == 1:
        return "medium"
    return "low"


def _batch_adjacent(feat: Mapping[str, Any]) -> bool:
    batch = _optional_int(feat.get("batch_size"))
    return batch is not None and _BATCH_LOW <= batch <= _BATCH_HIGH


def _sparse(feat: Mapping[str, Any]) -> bool:
    """Missing history, or a fetched history with no commits. One commit is not sparse."""
    if not _flag(feat.get("history_fetched")):
        return True
    count = _optional_int(feat.get("commit_count"))
    return count is not None and count <= 0


def _raise_to_floor(confidence: str, cfg: Config) -> str:
    floor = str(cfg.thresholds.insufficient_conf_floor)
    if floor not in _CONFIDENCE:
        floor = "low"
    if _RANK[confidence] < _RANK[floor]:
        return floor
    return confidence


def _merge(primary: list[str], extra: list[str]) -> list[str]:
    reasons = list(primary)
    for reason in extra:
        if reason not in reasons:
            reasons.append(reason)
    return reasons


def _v1_feat(feat: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "copies": _int(feat.get("copies")),
        "history_fetched": feat.get("history_fetched"),
        "batch_size": feat.get("batch_size"),
        "noncoding_bot": feat.get("noncoding_bot"),
        "coding_agent_bot": feat.get("coding_agent_bot"),
        "ai_trailer_first": feat.get("ai_trailer_first"),
        "ai_trailer_last": feat.get("ai_trailer_last"),
        "first_author_class": feat.get("first_author_class"),
        "ai_tool_first": feat.get("ai_tool_first"),
        "ai_tool_last": feat.get("ai_tool_last"),
    }


def _flag(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes"}
    return bool(value)


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _int(value: object) -> int:
    number = _optional_int(value)
    return 0 if number is None else number


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _at_least(value: object, minimum: int) -> bool:
    return _int(value) >= minimum


def _output_paths(cfg: Config) -> list[Path]:
    derived = resolve(cfg, cfg.paths.derived)
    results = resolve(cfg, cfg.paths.results)
    return [derived / "s3_labels_v2.parquet", results / "s3_v1_v2_crosstab.csv"]


def _derived(cfg: Config, name: str) -> Path:
    return resolve(cfg, cfg.paths.derived) / name


def _artifacts_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "artifacts"


def _inputs(cfg: Config, *, dev: bool) -> list[Path]:
    paths = [
        _derived(cfg, "s3_labels_v1.parquet"),
        _derived(cfg, "s2_commit.parquet"),
        _derived(cfg, "s2_repo.parquet"),
        _derived(cfg, "s2_structure.parquet"),
        _derived(cfg, "s2_wording.parquet"),
        _derived(cfg, "s2_template.parquet"),
        _derived(cfg, "s2_clusters.parquet"),
        _derived(cfg, "s2_types.parquet"),
        _artifacts_dir(cfg),
    ]
    if dev:
        paths.append(devsample_path(cfg))
    return paths


def _require_inputs(cfg: Config, *, dev: bool) -> None:
    required = [
        (_derived(cfg, "s3_labels_v1.parquet"), _V1),
        (_derived(cfg, "s2_commit.parquet"), _COMMIT),
        (_derived(cfg, "s2_repo.parquet"), _REPO),
        (_derived(cfg, "s2_structure.parquet"), _STRUCTURE),
        (_derived(cfg, "s2_wording.parquet"), _WORDING),
        (_derived(cfg, "s2_template.parquet"), _TEMPLATE),
        (_derived(cfg, "s2_clusters.parquet"), _CLUSTERS),
        (_derived(cfg, "s2_types.parquet"), _TYPES),
    ]
    for path, command in required:
        if not path.is_file():
            raise FileNotFoundError(f"missing {path}; run: {command}")
    directory = _artifacts_dir(cfg)
    if not directory.is_dir() or not any(directory.glob("*.parquet")):
        raise FileNotFoundError(f"missing {directory}; run: {_DOWNLOAD}")
    if dev and not devsample_path(cfg).is_file():
        raise FileNotFoundError(f"missing {devsample_path(cfg)}; run: {_DEV_SAMPLE}")


def _saved_rows(outputs: list[Path]) -> dict[str, int]:
    keys = ("s3_labels_v2", "s3_v1_v2_crosstab")
    saved: dict[str, int] = {}
    for key, path in zip(keys, outputs, strict=True):
        payload = json.loads(manifest_path(path).read_text(encoding="utf-8"))
        saved[key] = int(payload["rows"])
    return saved


def _write(cfg: Config, outputs: list[Path], *, dev: bool) -> tuple[int, int]:
    labels, crosstab = outputs
    labels.parent.mkdir(parents=True, exist_ok=True)
    crosstab.parent.mkdir(parents=True, exist_ok=True)
    sample = devsample_path(cfg) if dev else None
    con = duckdb.connect()
    try:
        _register(con, cfg)
        con.execute(_labels_sql(cfg, labels, sample))
        con.execute(_crosstab_sql(labels, crosstab))
        return _count(con, labels), _count(con, crosstab)
    finally:
        con.close()


_UDF_TYPES = [
    "INTEGER",
    "VARCHAR",
    "DOUBLE",
    "BIGINT",
    "BOOLEAN",
    "BOOLEAN",
    "INTEGER",
    "INTEGER",
    "BOOLEAN",
    "BOOLEAN",
    "BOOLEAN",
    "BOOLEAN",
    "VARCHAR",
    "VARCHAR",
    "VARCHAR",
]


def _register(con: duckdb.DuckDBPyConnection, cfg: Config) -> None:
    con.create_function(
        "skillsig_label_v2",
        _packer(cfg),
        _UDF_TYPES,
        "VARCHAR",
        null_handling="special",
    )


def _packer(cfg: Config):
    def pack(
        copies: int | None,
        match_kind: str | None,
        est_jaccard: float | None,
        cluster_size: int | None,
        registry_origin: bool | None,
        history_fetched: bool | None,
        commit_count: int | None,
        batch_size: int | None,
        noncoding_bot: bool | None,
        coding_agent_bot: bool | None,
        ai_trailer_first: bool | None,
        ai_trailer_last: bool | None,
        first_author_class: str | None,
        ai_tool_first: str | None,
        ai_tool_last: str | None,
    ) -> str:
        return _encode(
            cfg,
            copies,
            match_kind,
            est_jaccard,
            cluster_size,
            registry_origin,
            history_fetched,
            commit_count,
            batch_size,
            noncoding_bot,
            coding_agent_bot,
            ai_trailer_first,
            ai_trailer_last,
            first_author_class,
            ai_tool_first,
            ai_tool_last,
        )

    return pack


def _encode(cfg: Config, *values: object) -> str:
    names = (
        "copies",
        "match_kind",
        "est_jaccard",
        "cluster_size",
        "registry_origin",
        "history_fetched",
        "commit_count",
        "batch_size",
        "noncoding_bot",
        "coding_agent_bot",
        "ai_trailer_first",
        "ai_trailer_last",
        "first_author_class",
        "ai_tool_first",
        "ai_tool_last",
    )
    result = label_row(dict(zip(names, values, strict=True)), cfg)
    basis = "" if result["templated_basis"] is None else str(result["templated_basis"])
    reasons = _REC.join(str(reason) for reason in result["reasons"])
    return _UNIT.join([str(result["label"]), basis, str(result["confidence"]), reasons])


def _count(con: duckdb.DuckDBPyConnection, path: Path) -> int:
    if path.suffix == ".csv":
        query = "SELECT count(*) FROM read_csv(?, header = true)"
    else:
        query = "SELECT count(*) FROM read_parquet(?)"
    counted = con.execute(query, [path.as_posix()]).fetchone()
    return int(counted[0]) if counted else 0


def _labels_sql(cfg: Config, dest: Path, sample: Path | None) -> str:
    sample_and = ""
    if sample is not None:
        listed = _sql_string(sample)
        sample_and = f"AND file_sha IN (SELECT file_sha FROM read_parquet({listed}))"
    return _LABELS_SQL.format(
        v1=_sql_string(_derived(cfg, "s3_labels_v1.parquet")),
        commit=_sql_string(_derived(cfg, "s2_commit.parquet")),
        template=_sql_string(_derived(cfg, "s2_template.parquet")),
        clusters=_sql_string(_derived(cfg, "s2_clusters.parquet")),
        artifacts=_sql_string(_artifacts_dir(cfg) / "*.parquet"),
        sample_and=sample_and,
        dest=_sql_string(dest),
        unit=str(ord(_UNIT)),
        rec=str(ord(_REC)),
    )


def _crosstab_sql(labels: Path, dest: Path) -> str:
    return _CROSSTAB_SQL.format(labels=_sql_string(labels), dest=_sql_string(dest))


def _sql_string(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


_LABELS_SQL = """
COPY (
    WITH v1 AS (
        SELECT file_sha, v1_label, copies
        FROM (
            SELECT
                file_sha,
                label AS v1_label,
                copies,
                row_number() OVER (PARTITION BY file_sha ORDER BY label, copies) AS rn
            FROM read_parquet({v1})
        ) AS ranked
        WHERE rn = 1
          {sample_and}
    ),
    commit AS (
        SELECT
            file_sha, history_fetched, commit_count, batch_size,
            ai_trailer_first, ai_trailer_last, ai_tool_first, ai_tool_last,
            first_author_class, coding_agent_bot, noncoding_bot
        FROM (
            SELECT
                *,
                row_number() OVER (
                    PARTITION BY file_sha
                    ORDER BY history_fetched DESC NULLS LAST,
                        batch_size NULLS LAST,
                        commit_count NULLS LAST
                ) AS rn
            FROM read_parquet({commit})
        ) AS ranked
        WHERE rn = 1
    ),
    tmpl AS (
        SELECT file_sha, match_kind, est_jaccard, nearest_lineage_id
        FROM (
            SELECT
                file_sha, match_kind, est_jaccard, nearest_lineage_id,
                row_number() OVER (
                    PARTITION BY file_sha
                    ORDER BY match_kind NULLS LAST,
                        nearest_lineage_id NULLS LAST,
                        est_jaccard DESC NULLS LAST
                ) AS rn
            FROM read_parquet({template})
        ) AS ranked
        WHERE rn = 1
    ),
    clusters AS (
        SELECT file_sha, cluster_size
        FROM (
            SELECT
                file_sha, cluster_size,
                row_number() OVER (
                    PARTITION BY file_sha
                    ORDER BY cluster_size DESC, cluster_id
                ) AS rn
            FROM read_parquet({clusters})
        ) AS ranked
        WHERE rn = 1
    ),
    repos AS (
        SELECT file_sha, min(repo_full_name) AS repo_full_name
        FROM read_parquet({artifacts})
        WHERE coalesce(TRY_CAST(dedup_primary AS INTEGER), 0) = 1
        GROUP BY file_sha
    ),
    joined AS (
        SELECT
            v.file_sha,
            v.v1_label,
            v.copies,
            t.match_kind,
            t.est_jaccard,
            c.cluster_size,
            CASE
                WHEN t.nearest_lineage_id LIKE '%::%'
                 AND r.repo_full_name = split_part(t.nearest_lineage_id, '::', 1)
                 AND t.match_kind IN ('exact', 'near')
                THEN TRUE ELSE FALSE
            END AS registry_origin,
            k.history_fetched,
            k.commit_count,
            k.batch_size,
            k.noncoding_bot,
            k.coding_agent_bot,
            k.ai_trailer_first,
            k.ai_trailer_last,
            k.first_author_class,
            k.ai_tool_first,
            k.ai_tool_last
        FROM v1 AS v
        LEFT JOIN commit AS k USING (file_sha)
        LEFT JOIN tmpl AS t USING (file_sha)
        LEFT JOIN clusters AS c USING (file_sha)
        LEFT JOIN repos AS r USING (file_sha)
    ),
    packed AS (
        SELECT
            file_sha,
            v1_label,
            skillsig_label_v2(
                CAST(copies AS INTEGER),
                CAST(match_kind AS VARCHAR),
                CAST(est_jaccard AS DOUBLE),
                CAST(cluster_size AS BIGINT),
                CAST(registry_origin AS BOOLEAN),
                CAST(history_fetched AS BOOLEAN),
                CAST(commit_count AS INTEGER),
                CAST(batch_size AS INTEGER),
                CAST(noncoding_bot AS BOOLEAN),
                CAST(coding_agent_bot AS BOOLEAN),
                CAST(ai_trailer_first AS BOOLEAN),
                CAST(ai_trailer_last AS BOOLEAN),
                CAST(first_author_class AS VARCHAR),
                CAST(ai_tool_first AS VARCHAR),
                CAST(ai_tool_last AS VARCHAR)
            ) AS packed
        FROM joined
    )
    SELECT
        file_sha,
        split_part(packed, chr({unit}), 1) AS label,
        NULLIF(split_part(packed, chr({unit}), 2), '') AS templated_basis,
        split_part(packed, chr({unit}), 3) AS confidence,
        string_split(split_part(packed, chr({unit}), 4), chr({rec})) AS reasons,
        v1_label
    FROM packed
    ORDER BY file_sha
) TO {dest} (FORMAT PARQUET, COMPRESSION ZSTD)
"""

_CROSSTAB_SQL = """
COPY (
    SELECT v1_label, label AS v2_label, count(*)::BIGINT AS n
    FROM read_parquet({labels})
    GROUP BY 1, 2
    ORDER BY 1, 2
) TO {dest} (FORMAT CSV, HEADER)
"""
