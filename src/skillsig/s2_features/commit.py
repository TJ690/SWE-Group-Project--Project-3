"""
STAGE: s2_features
MODULE: commit
PURPOSE: Commit-history features for AI trailers, bot class, batch size, and message class.
INPUTS: artifacts parquet (representative first and last commit author, type, message, timestamps)
OUTPUTS: src/data/derived/s2_commit.parquet
RQ LINK: RQ1 (AI trailer, coding-agent bot, and bulk batch; message class is not a label input)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_data.devsample import output_path as devsample_path

log = logging.getLogger(__name__)

AI_TOOLS = ("claude", "cursor", "copilot", "codex", "gemini")

_DOWNLOAD = "python src/data/download_data.py"
_DEV_SAMPLE = "python -m skillsig data --dev-sample"

# Tool names count only on a Co-authored-by line, not later in the message.
_COAUTHOR_LINE = re.compile(r"(?im)^[ \t]*co-authored-by:[ \t]*(.*)$")
_TRAILER_LINE = re.compile(
    r"(?im)^[ \t]*(?:co-authored-by|signed-off-by|generated-by):[^\n]*\n?"
)
_TOOL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("claude", re.compile(r"claude|anthropic", re.IGNORECASE)),
    ("cursor", re.compile(r"cursor", re.IGNORECASE)),
    ("copilot", re.compile(r"copilot", re.IGNORECASE)),
    ("codex", re.compile(r"codex|openai", re.IGNORECASE)),
    ("gemini", re.compile(r"gemini", re.IGNORECASE)),
)
_WS = re.compile(r"\s+")
_INSTALL = re.compile(
    r"(^|[^a-z])((re)?sync|install|import|vendor)s?([^a-z]|$)|materiali[sz]e"
)
_SCAFFOLD = re.compile(r"^(initial commit|first commit|init|initial drop)([^a-z]|$)")
_CONVENTIONAL = re.compile(r"^(feat|fix|chore|docs|refactor|style|test|perf|build|ci)[(: ]")
_ADD = re.compile(r"^(add|added|adding|create|created)([^a-z]|$)")

_BOT_TYPES = frozenset({"bot", "programmaticaccessbot"})
# These are not an authorship signal, even when the login also names a coding agent.
_NONCODING_LOGIN = re.compile(
    r"github-actions|dependabot|release|sync|publish",
    re.IGNORECASE,
)
_CODING_AGENT_LOGIN = re.compile(
    r"claude|cursor|copilot|codex|devin|jules|gemini",
    re.IGNORECASE,
)

_OUTPUT_COLUMNS = (
    "file_sha",
    "history_fetched",
    "commit_count",
    "first_commit_at",
    "last_commit_at",
    "span_days",
    "ai_tool_first",
    "ai_tool_last",
    "ai_trailer_first",
    "ai_trailer_last",
    "first_author_class",
    "coding_agent_bot",
    "noncoding_bot",
    "batch_size",
    "is_bulk",
    "msg_class_first",
)


def ai_tools_in_message(msg: str | None) -> frozenset[str]:
    """AI tools named on Co-authored-by lines. A mention in the subject or body does not count."""
    if not msg:
        return frozenset()
    found: set[str] = set()
    for value in _COAUTHOR_LINE.findall(msg):
        for name, pattern in _TOOL_PATTERNS:
            if pattern.search(value):
                found.add(name)
    return frozenset(found)


def strip_trailers(msg: str | None) -> str:
    """Drop Co-authored-by, Signed-off-by, and Generated-by lines. None becomes an empty string."""
    if not msg:
        return ""
    return _TRAILER_LINE.sub("", msg)


def classify_message(msg: str | None) -> str:
    """First matching message class. A missing message is 'other', not a short message."""
    if msg is None:
        return "other"
    body = _WS.sub(" ", strip_trailers(msg).lower()).strip()
    if _INSTALL.search(body):
        return "install_sync_import"
    if _SCAFFOLD.search(body):
        return "scaffold"
    if _CONVENTIONAL.search(body):
        return "conventional"
    if _ADD.search(body) or "via upload" in body:
        return "add_upload"
    if len(body) <= 12:
        return "short_generic"
    return "other"


def classify_author(login: str | None, author_type: str | None) -> str:
    """coding_agent_bot, noncoding_bot, user, organization, or unknown."""
    name = "" if login is None else str(login)
    kind = "" if author_type is None else str(author_type).strip().lower()
    if kind in _BOT_TYPES or name.lower().endswith("[bot]"):
        return _bot_class(name)
    if kind == "user":
        return "user"
    if kind == "organization":
        return "organization"
    return "unknown"


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write s2_commit.parquet. Dev mode keeps dev-sample ids; batch size stays full-population."""
    output = _output_path(cfg)
    inputs = _inputs(cfg, dev=dev)
    _require_inputs(cfg, dev=dev)
    if not force and inputs_unchanged(output, cfg=cfg, inputs=inputs):
        log.info("commit features up to date: %s", output)
        return StageResult(
            stage="s2_features", outputs=[output], skipped=True, rows=_saved_rows(output)
        )
    rows = _write_commit(cfg, output, dev=dev)
    write_manifest(output, cfg=cfg, inputs=inputs, rows=rows)
    log.info("wrote %s (%s rows)", output, rows)
    return StageResult(
        stage="s2_features", outputs=[output], skipped=False, rows={"s2_commit": rows}
    )


def _bot_class(login: str) -> str:
    if _NONCODING_LOGIN.search(login):
        return "noncoding_bot"
    if _CODING_AGENT_LOGIN.search(login):
        return "coding_agent_bot"
    return "noncoding_bot"


def _ai_tool_label(msg: str | None) -> str | None:
    found = ai_tools_in_message(msg)
    if not found:
        return None
    return ",".join(name for name in AI_TOOLS if name in found)


def _span_days(first: str | None, last: str | None) -> int | None:
    start = _utc_date(first)
    end = _utc_date(last)
    if start is None or end is None:
        return None
    return (end - start).days


def _utc_date(value: str | None) -> date | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.date()
    return parsed.astimezone(UTC).date()


def _output_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s2_commit.parquet"


def _artifacts_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "artifacts"


def _inputs(cfg: Config, *, dev: bool) -> list[Path]:
    paths = [_artifacts_dir(cfg)]
    if dev:
        paths.append(devsample_path(cfg))
    return paths


def _require_inputs(cfg: Config, *, dev: bool) -> None:
    directory = _artifacts_dir(cfg)
    if not directory.is_dir() or not any(directory.glob("*.parquet")):
        raise FileNotFoundError(f"missing {directory}; run: {_DOWNLOAD}")
    if not dev:
        return
    sample = devsample_path(cfg)
    if not sample.is_file():
        raise FileNotFoundError(f"missing {sample}; run: {_DEV_SAMPLE}")


def _write_commit(cfg: Config, output: Path, *, dev: bool) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    sample = devsample_path(cfg) if dev else None
    con = duckdb.connect()
    try:
        _register(con)
        glob = _sql_string(_artifacts_dir(cfg) / "*.parquet")
        con.execute(f"CREATE TEMP VIEW _artifacts AS SELECT * FROM read_parquet({glob})")
        con.execute(
            _copy_sql(
                output,
                dev=dev,
                sample=sample,
                bulk_min=int(cfg.thresholds.bulk_batch_min),
            )
        )
        counted = con.execute(
            "SELECT count(*) FROM read_parquet(?)", [output.as_posix()]
        ).fetchone()
        return int(counted[0]) if counted else 0
    finally:
        con.close()


def _register(con: duckdb.DuckDBPyConnection) -> None:
    con.create_function(
        "ai_tool_label", _ai_tool_label, ["VARCHAR"], "VARCHAR", null_handling="special"
    )
    con.create_function(
        "msg_class", classify_message, ["VARCHAR"], "VARCHAR", null_handling="special"
    )
    con.create_function(
        "author_class",
        classify_author,
        ["VARCHAR", "VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )
    con.create_function(
        "span_days", _span_days, ["VARCHAR", "VARCHAR"], "INTEGER", null_handling="special"
    )


_COPY_SQL = """
COPY (
    WITH reps AS (
        SELECT
            file_sha,
            repo_full_name,
            coalesce(history_fetched, 0) = 1 AS hist,
            commit_count,
            first_commit_at,
            last_commit_at,
            first_commit_author,
            first_commit_author_type,
            first_commit_message,
            last_commit_message
        FROM _artifacts
        WHERE dedup_primary = 1
    ),
    batches AS (
        SELECT repo_full_name, first_commit_at, count(*)::INTEGER AS batch_size
        FROM reps
        WHERE hist
        GROUP BY 1, 2
    ),
    scored AS (
        SELECT
            r.file_sha,
            r.hist,
            r.commit_count,
            r.first_commit_at,
            r.last_commit_at,
            CASE WHEN r.hist THEN span_days(r.first_commit_at, r.last_commit_at) END
                AS span_days,
            CASE WHEN r.hist THEN ai_tool_label(r.first_commit_message) END AS ai_tool_first,
            CASE WHEN r.hist THEN ai_tool_label(r.last_commit_message) END AS ai_tool_last,
            CASE WHEN r.hist THEN author_class(
                r.first_commit_author, r.first_commit_author_type
            ) END AS first_author_class,
            CASE WHEN r.hist THEN msg_class(r.first_commit_message) END AS msg_class_first,
            b.batch_size
        FROM reps r
        LEFT JOIN batches b
            ON r.repo_full_name IS NOT DISTINCT FROM b.repo_full_name
           AND r.first_commit_at IS NOT DISTINCT FROM b.first_commit_at
    )
    SELECT {columns}
    FROM (
        SELECT
            file_sha,
            hist AS history_fetched,
            CAST(CASE WHEN hist THEN commit_count END AS INTEGER) AS commit_count,
            CASE WHEN hist THEN first_commit_at END AS first_commit_at,
            CASE WHEN hist THEN last_commit_at END AS last_commit_at,
            span_days,
            ai_tool_first,
            ai_tool_last,
            CASE WHEN hist THEN ai_tool_first IS NOT NULL END AS ai_trailer_first,
            CASE WHEN hist THEN ai_tool_last IS NOT NULL END AS ai_trailer_last,
            first_author_class,
            CASE WHEN hist THEN first_author_class = 'coding_agent_bot' END
                AS coding_agent_bot,
            CASE WHEN hist THEN first_author_class = 'noncoding_bot' END AS noncoding_bot,
            CASE WHEN hist THEN batch_size END AS batch_size,
            CASE WHEN hist THEN batch_size >= {bulk_min} END AS is_bulk,
            msg_class_first
        FROM scored
        {where}
    )
    ORDER BY file_sha
) TO {dest} (FORMAT PARQUET, COMPRESSION ZSTD)
"""


def _copy_sql(dest: Path, *, dev: bool, sample: Path | None, bulk_min: int) -> str:
    where = ""
    if dev and sample is not None:
        listed = _sql_string(sample)
        where = f"WHERE file_sha IN (SELECT file_sha FROM read_parquet({listed}))"
    return _COPY_SQL.format(
        columns=", ".join(_OUTPUT_COLUMNS),
        where=where,
        bulk_min=int(bulk_min),
        dest=_sql_string(dest),
    )


def _sql_string(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _saved_rows(output: Path) -> dict[str, int]:
    payload = json.loads(manifest_path(output).read_text(encoding="utf-8"))
    return {"s2_commit": int(payload["rows"])}
