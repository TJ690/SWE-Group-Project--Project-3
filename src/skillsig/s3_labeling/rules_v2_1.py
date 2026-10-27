"""
STAGE: s3_labeling
MODULE: rules_v2_1
PURPOSE: Label v2.1: v2 plus trailer and human-confidence overrides and independent flags.
INPUTS: s3_labels_v2, s3_labels_v1 (copies), s2_commit (incl. *_ext), artifacts primary rows
OUTPUTS: src/data/derived/s3_labels_v2_1.parquet, results/s3_v2_v21_crosstab.csv
RQ LINK: RQ1 (heuristic label; flags keep templated, disclosed-agent and human evidence apart)

Two overrides on top of v2 (T36, decisions 1 and 2 in the code gap plan):
- O1: a disclosed AI trailer or agent bot on non-templated content that v2 left as
  insufficient_evidence (mostly the bulk-install exclusion) becomes agent_assisted, low confidence.
- O2: likely_human gets medium confidence only with revision history; otherwise low.
A skill whose text is templated AND whose commit disclosed an agent is flagged `conflict` and
loses one confidence level. v1 and v2 are not changed; the annotated sample decides which is used.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_intake.devsample import output_path as devsample_path

log = logging.getLogger(__name__)

FORMAT_LAUNCH = "2025-10-01"
_LOWER = {"high": "medium", "medium": "low", "low": "low"}
_BATCH_ROWS = 100_000

_V2 = "python -m skillsig label --only rules_v2"
_V1 = "python -m skillsig label --only rules_v1"
_COMMIT = "python -m skillsig features --only commit"
_DOWNLOAD = "python src/data/download_data.py"
_DEV_SAMPLE = "python -m skillsig data --dev-sample"

_SCHEMA = pa.schema(
    [
        pa.field("file_sha", pa.string()),
        pa.field("label", pa.string()),
        pa.field("templated_basis", pa.string()),
        pa.field("confidence", pa.string()),
        pa.field("reasons", pa.list_(pa.string())),
        pa.field("label_v2", pa.string()),
        pa.field("label_v1", pa.string()),
        pa.field("flag_templated", pa.bool_()),
        pa.field("flag_disclosed_agent", pa.bool_()),
        pa.field("revised", pa.bool_()),
        pa.field("human_signal_count", pa.int8()),
        pa.field("conflict", pa.bool_()),
        pa.field("filter_short", pa.bool_()),
        pa.field("filter_case_variant", pa.bool_()),
        pa.field("filter_pre_format", pa.bool_()),
    ]
)


def label_row_v21(v2: Mapping[str, Any], feat: Mapping[str, Any], cfg: Config) -> dict:
    """Apply the v2.1 overrides to one v2 result; returns the label columns and all flags."""
    label = str(v2["label"])
    basis = v2.get("templated_basis") or None
    confidence = str(v2["confidence"])
    reasons = [str(r) for r in (v2.get("reasons") or []) if r]

    hist = _flag(feat.get("history_fetched"))
    first_orig = _flag(feat.get("ai_trailer_first"))
    last_orig = _flag(feat.get("ai_trailer_last"))
    first_ext = _flag(feat.get("ai_trailer_first_ext")) or first_orig
    last_ext = _flag(feat.get("ai_trailer_last_ext")) or last_orig
    agent_bot = _flag(feat.get("coding_agent_bot"))
    disclosed = hist and (first_ext or last_ext or agent_bot)
    batch = _optional_int(feat.get("batch_size"))
    commits = _optional_int(feat.get("commit_count"))
    span = _optional_int(feat.get("span_days"))
    revised = (
        hist
        and commits is not None
        and span is not None
        and commits >= int(cfg.thresholds.human_min_commits)
        and span >= int(cfg.thresholds.human_min_span_days)
    )

    # O1: disclosed agent use that v2 left as insufficient evidence.
    if label == "insufficient_evidence" and disclosed and not _flag(feat.get("noncoding_bot")):
        label = "agent_assisted"
        confidence = "low"
        reason = _trailer_reason(batch, cfg, first_orig, last_orig, first_ext, agent_bot)
        reasons = [reason, *(r for r in reasons if r != "insufficient_evidence")]

    # O2: human confidence needs positive revision evidence.
    if label == "likely_human":
        confidence = "medium" if revised else "low"
        reasons = [*reasons, "revised" if revised else "not_revised"]

    flag_templated = basis is not None or v2.get("label") == "highly_templated"
    conflict = flag_templated and disclosed
    if conflict:
        confidence = _LOWER[confidence]
        reasons = [*reasons, "conflict:templated+disclosed"]

    author = feat.get("first_author_class")
    human_signals = sum(
        (
            revised,
            batch == 1,
            _optional_int(feat.get("copies")) == 1,
            author == "user",
            hist and not disclosed,
        )
    )
    body = _optional_int(feat.get("body_chars"))
    filename = feat.get("filename")
    first_at = feat.get("first_commit_at")
    return {
        "label": label,
        "templated_basis": basis,
        "confidence": confidence,
        "reasons": reasons or ["insufficient_evidence"],
        "flag_templated": bool(flag_templated),
        "flag_disclosed_agent": bool(disclosed),
        "revised": bool(revised),
        "human_signal_count": int(human_signals),
        "conflict": bool(conflict),
        "filter_short": body is None or body < int(cfg.thresholds.min_body_chars),
        "filter_case_variant": filename is not None and str(filename) != "SKILL.md",
        "filter_pre_format": bool(first_at) and str(first_at)[:10] < FORMAT_LAUNCH,
    }


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write s3_labels_v2_1.parquet and the v2 x v2.1 crosstab. Dev mode keeps dev-sample ids."""
    outputs = _output_paths(cfg)
    inputs = _inputs(cfg, dev=dev)
    _require_inputs(cfg, dev=dev)
    if not force and all(inputs_unchanged(p, cfg=cfg, inputs=inputs) for p in outputs):
        log.info("labels v2.1 up to date: %s", outputs[0])
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
        rows={"s3_labels_v2_1": written[0], "s3_v2_v21_crosstab": written[1]},
    )


def _trailer_reason(
    batch: int | None,
    cfg: Config,
    first_orig: bool,
    last_orig: bool,
    first_ext: bool,
    agent_bot: bool,
) -> str:
    if batch is not None and batch >= int(cfg.thresholds.bulk_batch_min):
        return "trailer_in_bulk"
    if not first_ext and not agent_bot:
        return "trailer_last_only"
    if not first_orig and not last_orig and not agent_bot:
        return "trailer_ext_only"
    return "trailer_other"


def _flag(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes"}
    return bool(value)


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _derived(cfg: Config, name: str) -> Path:
    return resolve(cfg, cfg.paths.derived) / name


def _artifacts_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "artifacts"


def _output_paths(cfg: Config) -> list[Path]:
    results = resolve(cfg, cfg.paths.results)
    return [_derived(cfg, "s3_labels_v2_1.parquet"), results / "s3_v2_v21_crosstab.csv"]


def _inputs(cfg: Config, *, dev: bool) -> list[Path]:
    paths = [
        _derived(cfg, "s3_labels_v2.parquet"),
        _derived(cfg, "s3_labels_v1.parquet"),
        _derived(cfg, "s2_commit.parquet"),
        _artifacts_dir(cfg),
    ]
    if dev:
        paths.append(devsample_path(cfg))
    return paths


def _require_inputs(cfg: Config, *, dev: bool) -> None:
    for name, command in (
        ("s3_labels_v2.parquet", _V2),
        ("s3_labels_v1.parquet", _V1),
        ("s2_commit.parquet", _COMMIT),
    ):
        if not _derived(cfg, name).is_file():
            raise FileNotFoundError(f"missing {_derived(cfg, name)}; run: {command}")
    directory = _artifacts_dir(cfg)
    if not directory.is_dir() or not any(directory.glob("*.parquet")):
        raise FileNotFoundError(f"missing {directory}; run: {_DOWNLOAD}")
    if dev and not devsample_path(cfg).is_file():
        raise FileNotFoundError(f"missing {devsample_path(cfg)}; run: {_DEV_SAMPLE}")


def _saved_rows(outputs: list[Path]) -> dict[str, int]:
    keys = ("s3_labels_v2_1", "s3_v2_v21_crosstab")
    return {
        key: int(json.loads(manifest_path(path).read_text(encoding="utf-8"))["rows"])
        for key, path in zip(keys, outputs, strict=True)
    }


def _sql_string(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _commit_columns(con: duckdb.DuckDBPyConnection, path: Path) -> str:
    """Select the commit columns; *_ext may be missing in an older s2_commit."""
    described = con.execute(f"DESCRIBE SELECT * FROM {_sql_string(path)}").fetchall()
    names = {row[0] for row in described}
    wanted = (
        "history_fetched",
        "commit_count",
        "span_days",
        "batch_size",
        "first_commit_at",
        "ai_trailer_first",
        "ai_trailer_last",
        "ai_trailer_first_ext",
        "ai_trailer_last_ext",
        "coding_agent_bot",
        "noncoding_bot",
        "first_author_class",
    )
    if "ai_trailer_first_ext" not in names:
        log.warning("s2_commit has no *_ext columns; run: %s --force", _COMMIT)
    return ", ".join(f"k.{c}" if c in names else f"NULL AS {c}" for c in wanted)


def _write(cfg: Config, outputs: list[Path], *, dev: bool) -> tuple[int, int]:
    labels_path, crosstab_path = outputs
    con = duckdb.connect()
    try:
        commit = _derived(cfg, "s2_commit.parquet")
        sample_join = ""
        if dev:
            sample_join = (
                f"JOIN (SELECT DISTINCT file_sha FROM read_parquet("
                f"{_sql_string(devsample_path(cfg))})) d USING (file_sha)"
            )
        query = f"""
            WITH v1 AS (
                SELECT file_sha, any_value(label) AS label_v1, max(copies) AS copies
                FROM read_parquet({_sql_string(_derived(cfg, "s3_labels_v1.parquet"))})
                GROUP BY file_sha
            ),
            art AS (
                SELECT file_sha, any_value(filename) AS filename,
                       any_value(body_chars) AS body_chars
                FROM read_parquet({_sql_string(_artifacts_dir(cfg) / "*.parquet")})
                WHERE coalesce(TRY_CAST(dedup_primary AS INTEGER), 0) = 1
                GROUP BY file_sha
            )
            SELECT l.file_sha, l.label, l.templated_basis, l.confidence, l.reasons,
                   v1.label_v1, v1.copies, art.filename, art.body_chars,
                   {_commit_columns(con, commit)}
            FROM read_parquet({_sql_string(_derived(cfg, "s3_labels_v2.parquet"))}) l
            {sample_join}
            LEFT JOIN v1 USING (file_sha)
            LEFT JOIN art USING (file_sha)
            LEFT JOIN read_parquet({_sql_string(commit)}) k USING (file_sha)
            ORDER BY l.file_sha
        """
        relation = con.sql(query)
        # Stream in batches so 1.9M rows fit in a few GB. DuckDB 2.x: to_arrow_reader; 1.x:
        # fetch_record_batch.
        reader_of = getattr(relation, "to_arrow_reader", None) or relation.fetch_record_batch
        labels_path.parent.mkdir(parents=True, exist_ok=True)
        rows = 0
        with pq.ParquetWriter(labels_path, _SCHEMA, compression="zstd") as writer:
            for batch in reader_of(_BATCH_ROWS):
                out = _label_table(pa.Table.from_batches([batch]), cfg)
                writer.write_table(out)
                rows += out.num_rows
        crosstab_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(
            f"""
            COPY (
                SELECT label_v2, label AS label_v21, confidence AS confidence_v21,
                       count(*)::BIGINT AS n
                FROM read_parquet({_sql_string(labels_path)})
                GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
            ) TO {_sql_string(crosstab_path)} (FORMAT CSV, HEADER)
            """
        )
        crosstab_rows = con.execute(
            f"SELECT count(*) FROM read_csv({_sql_string(crosstab_path)}, header = true)"
        ).fetchall()[0][0]
        return rows, int(crosstab_rows)
    finally:
        con.close()


def _label_table(table: pa.Table, cfg: Config) -> pa.Table:
    columns = table.to_pydict()
    names = list(columns)
    rows: dict[str, list[Any]] = {field.name: [] for field in _SCHEMA}
    for i in range(table.num_rows):
        row = {name: columns[name][i] for name in names}
        v2 = {
            "label": row["label"],
            "templated_basis": row["templated_basis"],
            "confidence": row["confidence"],
            "reasons": row["reasons"],
        }
        result = label_row_v21(v2, row, cfg)
        rows["file_sha"].append(row["file_sha"])
        rows["label_v2"].append(row["label"])
        rows["label_v1"].append(row["label_v1"])
        for key, value in result.items():
            rows[key].append(value)
    return pa.table(rows, schema=_SCHEMA)
