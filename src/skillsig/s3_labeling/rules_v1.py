"""
STAGE: s3_labeling
MODULE: rules_v1
PURPOSE: Assign the Sprint 1 authorship label, first matching rule wins.
INPUTS: artifacts parquet; s2_commit.parquet must already exist
OUTPUTS: src/data/derived/s3_labels_v1.parquet
RQ LINK: RQ1 (heuristic label from copy count, AI trailer, bot class, and batch size)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import duckdb

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_intake.devsample import output_path as devsample_path

log = logging.getLogger(__name__)

LABELS = ("highly_templated", "agent_assisted", "likely_human", "insufficient_evidence")

_COMMIT = "python -m skillsig features --only commit"
_DOWNLOAD = "python src/data/download_data.py"
_DEV_SAMPLE = "python -m skillsig data --dev-sample"

# Notebook bot groups that are an agent-authorship signal. `^claude` is start-anchored.
_AGENTS = (
    "copilot|devin|jules|opencode|^claude|lovable|kilo-code|codex|"
    "factory-droid|amazon-q|codegen|capy-ai|warp-dev|cursor"
)
_UNIT = "\x1f"
_REC = "\x1e"


@dataclass(frozen=True)
class _Flags:
    history: bool
    batch: int | None
    noncoding: bool
    coding: bool
    ai_first: bool
    ai_last: bool
    author: str


def label_v1(row: Mapping, cfg: Config) -> tuple[str, list[str]]:
    """First match: templated, agent-assisted, likely-human, else insufficient evidence."""
    copies = int(row["copies"])
    minimum = int(cfg.thresholds.templated_copies_min)
    if copies >= minimum:
        return "highly_templated", [f"copies>={minimum}"]
    flags = _flags(row)
    if _is_agent(flags, cfg):
        return "agent_assisted", _agent_reasons(row, flags)
    if _is_human(flags, copies):
        return "likely_human", ["batch_size=1", "author=user", "copies=1", "no_ai_trailer"]
    return "insufficient_evidence", _insufficient(row, flags, copies, cfg)


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write s3_labels_v1.parquet. Dev mode keeps dev-sample ids; batches stay full-population."""
    output = _output_path(cfg)
    inputs = _inputs(cfg, dev=dev)
    _require_inputs(cfg, dev=dev)
    if not force and inputs_unchanged(output, cfg=cfg, inputs=inputs):
        log.info("labels v1 up to date: %s", output)
        return StageResult(
            stage="s3_labeling", outputs=[output], skipped=True, rows=_saved_rows(output)
        )
    rows = _write_labels(cfg, output, dev=dev)
    write_manifest(output, cfg=cfg, inputs=inputs, rows=rows)
    log.info("wrote %s (%s rows)", output, rows)
    return StageResult(
        stage="s3_labeling", outputs=[output], skipped=False, rows={"s3_labels_v1": rows}
    )


def _flags(row: Mapping) -> _Flags:
    author = row.get("first_author_class")
    return _Flags(
        history=_flag(row.get("history_fetched")),
        batch=_optional_int(row.get("batch_size")),
        noncoding=_flag(row.get("noncoding_bot")),
        coding=_flag(row.get("coding_agent_bot")),
        ai_first=_flag(row.get("ai_trailer_first")),
        ai_last=_flag(row.get("ai_trailer_last")),
        author="" if author is None else str(author),
    )


def _is_agent(flags: _Flags, cfg: Config) -> bool:
    bulk = int(cfg.thresholds.bulk_batch_min)
    if not flags.history or flags.batch is None or flags.batch >= bulk or flags.noncoding:
        return False
    return flags.coding or flags.ai_first


def _is_human(flags: _Flags, copies: int) -> bool:
    return (
        flags.history
        and flags.batch == 1
        and flags.author == "user"
        and not flags.ai_first
        and not flags.ai_last
        and copies == 1
    )


def _agent_reasons(row: Mapping, flags: _Flags) -> list[str]:
    reasons: list[str] = []
    if flags.coding:
        reasons.append("coding_agent_bot")
    if flags.ai_first:
        reasons.append(_trailer_reason("ai_trailer_first", row.get("ai_tool_first")))
    return reasons or ["agent_assisted"]


def _insufficient(row: Mapping, flags: _Flags, copies: int, cfg: Config) -> list[str]:
    if not flags.history:
        reasons = ["no_history"]
        if copies != 1:
            reasons.append(f"copies={copies}")
        return reasons
    return _history_gap(row, flags, copies, int(cfg.thresholds.bulk_batch_min))


def _history_gap(row: Mapping, flags: _Flags, copies: int, bulk: int) -> list[str]:
    reasons: list[str] = []
    batch = flags.batch
    if batch is not None and batch >= bulk:
        reasons.append(f"bulk_batch>={bulk}")
    elif batch != 1:
        reasons.append(f"batch_size={_batch_text(batch)}")
    if flags.noncoding:
        reasons.append("noncoding_bot")
    elif not flags.coding and not flags.ai_first and (batch is None or batch < bulk):
        reasons.append("no_agent_signal")
    if flags.author not in {"user", "coding_agent_bot", "noncoding_bot"}:
        reasons.append(f"author={flags.author or 'unknown'}")
    if flags.ai_first:
        reasons.append(_trailer_reason("ai_trailer_first", row.get("ai_tool_first")))
    if flags.ai_last:
        reasons.append(_trailer_reason("ai_trailer_last", row.get("ai_tool_last")))
    if copies != 1:
        reasons.append(f"copies={copies}")
    return reasons or ["insufficient_evidence"]


def _batch_text(batch: int | None) -> str:
    return "missing" if batch is None else str(batch)


def _trailer_reason(prefix: str, tool: object) -> str:
    if tool is None or not str(tool).strip():
        return prefix
    return f"{prefix}:{tool}"


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


def _output_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s3_labels_v1.parquet"


def _commit_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s2_commit.parquet"


def _artifacts_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "artifacts"


def _inputs(cfg: Config, *, dev: bool) -> list[Path]:
    paths = [_commit_path(cfg), _artifacts_dir(cfg)]
    if dev:
        paths.append(devsample_path(cfg))
    return paths


def _require_inputs(cfg: Config, *, dev: bool) -> None:
    commit = _commit_path(cfg)
    if not commit.is_file():
        raise FileNotFoundError(f"missing {commit}; run: {_COMMIT}")
    directory = _artifacts_dir(cfg)
    if not directory.is_dir() or not any(directory.glob("*.parquet")):
        raise FileNotFoundError(f"missing {directory}; run: {_DOWNLOAD}")
    if dev and not devsample_path(cfg).is_file():
        raise FileNotFoundError(f"missing {devsample_path(cfg)}; run: {_DEV_SAMPLE}")


def _write_labels(cfg: Config, output: Path, *, dev: bool) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    sample = devsample_path(cfg) if dev else None
    con = duckdb.connect()
    try:
        _register(con, cfg)
        con.execute(
            _copy_sql(
                output,
                _artifacts_dir(cfg),
                dev=dev,
                sample=sample,
            )
        )
        counted = con.execute(
            "SELECT count(*) FROM read_parquet(?)", [output.as_posix()]
        ).fetchone()
        return int(counted[0]) if counted else 0
    finally:
        con.close()


def _register(con: duckdb.DuckDBPyConnection, cfg: Config) -> None:
    def pack(
        history: bool | None,
        batch: int | None,
        noncoding: bool | None,
        coding: bool | None,
        ai_first: bool | None,
        ai_last: bool | None,
        author: str | None,
        tool_first: str | None,
        tool_last: str | None,
        copies: int | None,
    ) -> str:
        row = {
            "history_fetched": history,
            "batch_size": batch,
            "noncoding_bot": noncoding,
            "coding_agent_bot": coding,
            "ai_trailer_first": ai_first,
            "ai_trailer_last": ai_last,
            "first_author_class": author,
            "ai_tool_first": tool_first,
            "ai_tool_last": tool_last,
            "copies": 0 if copies is None else copies,
        }
        label, reasons = label_v1(row, cfg)
        return label + _UNIT + _REC.join(reasons)

    con.create_function(
        "skillsig_label_v1",
        pack,
        [
            "BOOLEAN",
            "INTEGER",
            "BOOLEAN",
            "BOOLEAN",
            "BOOLEAN",
            "BOOLEAN",
            "VARCHAR",
            "VARCHAR",
            "VARCHAR",
            "INTEGER",
        ],
        "VARCHAR",
        null_handling="special",
    )


def _tool_sql(column: str) -> str:
    parts = (
        f"CASE WHEN {column} LIKE '%co-authored-by:%claude%'"
        f" OR {column} LIKE '%co-authored-by:%anthropic%' THEN 'claude' END",
        f"CASE WHEN {column} LIKE '%co-authored-by:%cursor%' THEN 'cursor' END",
        f"CASE WHEN {column} LIKE '%co-authored-by:%copilot%' THEN 'copilot' END",
        f"CASE WHEN {column} LIKE '%co-authored-by:%codex%'"
        f" OR {column} LIKE '%co-authored-by:%openai%' THEN 'codex' END",
        f"CASE WHEN {column} LIKE '%co-authored-by:%gemini%' THEN 'gemini' END",
    )
    return "NULLIF(concat_ws(',', " + ", ".join(parts) + "), '')"


def _copy_sql(dest: Path, artifacts: Path, *, dev: bool, sample: Path | None) -> str:
    where = ""
    if dev and sample is not None:
        listed = _sql_string(sample)
        where = f"WHERE file_sha IN (SELECT file_sha FROM read_parquet({listed}))"
    return _COPY_SQL.format(
        artifacts=_sql_string(artifacts / "*.parquet"),
        agents=_AGENTS,
        tool_first=_tool_sql("msg_first"),
        tool_last=_tool_sql("msg_last"),
        where=where,
        dest=_sql_string(dest),
        unit=str(ord(_UNIT)),
        rec=str(ord(_REC)),
    )


def _sql_string(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _saved_rows(output: Path) -> dict[str, int]:
    payload = json.loads(manifest_path(output).read_text(encoding="utf-8"))
    return {"s3_labels_v1": int(payload["rows"])}


# Sprint 1 signals, not the s2_commit columns. Those use a line-anchored trailer
# and a shorter bot list, which misses the published label counts.
_COPY_SQL = """
COPY (
    WITH copies AS (
        SELECT file_sha, count(*)::INTEGER AS copies
        FROM read_parquet({artifacts})
        GROUP BY 1
    ),
    batch AS (
        SELECT repo_full_name, first_commit_at, count(*)::INTEGER AS batch_n
        FROM read_parquet({artifacts})
        WHERE dedup_primary = 1 AND history_fetched = 1
        GROUP BY 1, 2
    ),
    primaries AS (
        SELECT
            a.file_sha,
            coalesce(a.history_fetched, 0) = 1 AS has_history,
            a.first_commit_author_type,
            lower(coalesce(a.first_commit_author, '')) AS login,
            lower(coalesce(a.first_commit_message, '')) AS msg_first,
            lower(coalesce(a.last_commit_message, '')) AS msg_last,
            CASE WHEN coalesce(a.history_fetched, 0) = 1 THEN coalesce(b.batch_n, 0) END
                AS batch_size,
            c.copies
        FROM read_parquet({artifacts}) a
        JOIN copies c ON c.file_sha = a.file_sha
        LEFT JOIN batch b
            ON b.repo_full_name = a.repo_full_name
           AND b.first_commit_at = a.first_commit_at
        WHERE a.dedup_primary = 1
    ),
    classed AS (
        SELECT
            *,
            CASE
                WHEN coalesce(first_commit_author_type, '')
                    NOT IN ('Bot', 'ProgrammaticAccessBot') THEN 'plain'
                WHEN login IN ('github-actions[bot]', 'dependabot[bot]', 'renovate[bot]')
                    OR login LIKE '%release%' OR login LIKE '%sync%' THEN 'noncoding_bot'
                WHEN regexp_matches(login, '{agents}') THEN 'coding_agent_bot'
                ELSE 'noncoding_bot'
            END AS bot_class
        FROM primaries
    ),
    featured AS (
        SELECT
            file_sha,
            copies,
            has_history,
            batch_size,
            CASE WHEN has_history THEN bot_class = 'coding_agent_bot' END AS coding_agent_bot,
            CASE WHEN has_history THEN bot_class = 'noncoding_bot' END AS noncoding_bot,
            CASE
                WHEN NOT has_history THEN NULL
                WHEN bot_class = 'coding_agent_bot' THEN 'coding_agent_bot'
                WHEN bot_class = 'noncoding_bot' THEN 'noncoding_bot'
                WHEN first_commit_author_type = 'User' THEN 'user'
                WHEN first_commit_author_type = 'Organization' THEN 'organization'
                ELSE 'unknown'
            END AS first_author_class,
            CASE WHEN has_history THEN {tool_first} END AS ai_tool_first,
            CASE WHEN has_history THEN {tool_last} END AS ai_tool_last,
            CASE WHEN has_history THEN {tool_first} IS NOT NULL END AS ai_trailer_first,
            CASE WHEN has_history THEN {tool_last} IS NOT NULL END AS ai_trailer_last
        FROM classed
    ),
    packed AS (
        SELECT
            file_sha,
            copies,
            skillsig_label_v1(
                has_history,
                batch_size,
                noncoding_bot,
                coding_agent_bot,
                ai_trailer_first,
                ai_trailer_last,
                first_author_class,
                ai_tool_first,
                ai_tool_last,
                copies
            ) AS packed
        FROM featured
        {where}
    )
    SELECT
        file_sha,
        split_part(packed, chr({unit}), 1) AS label,
        copies,
        string_split(split_part(packed, chr({unit}), 2), chr({rec})) AS reasons
    FROM packed
    ORDER BY file_sha
) TO {dest} (FORMAT PARQUET, COMPRESSION ZSTD)
"""
