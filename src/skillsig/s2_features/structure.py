"""
STAGE: s2_features
MODULE: structure
PURPOSE: Compute SKILL.md structure features with the same definitions as skill_features.
INPUTS: one skill text; generated skill_features.parquet when run() copies the table
OUTPUTS: src/data/derived/s2_structure.parquet
RQ LINK: RQ1 (structure variables: length, headings, lists, code, links, front matter)
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

import duckdb
import yaml

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_data.devsample import output_path as devsample_path

log = logging.getLogger(__name__)

# Same expressions as db/content_and_structure.sql. \s there is ASCII whitespace.
_WS = r"[ \t\n\r\f\v]"
_FRONT_BODY = re.compile(r"^\s*---.*?\n---\s*\n?", re.DOTALL | re.ASCII)
_FRONT_FM = re.compile(r"^\s*---(.*?)\n---", re.DOTALL | re.ASCII)
_HEADINGS = re.compile(rf"(?m)^#{{1,6}}{_WS}")
_H2 = re.compile(rf"(?m)^##{_WS}")
_NUMBERED = re.compile(rf"(?m)^{_WS}*[0-9]+[.)]{_WS}")
_BULLETS = re.compile(rf"(?m)^{_WS}*[-*+]{_WS}")
_FENCES = re.compile(rf"(?m)^{_WS}*```")
_TABLES = re.compile(rf"(?m)^\|.*\|{_WS}*$")
_LINKS = re.compile(r"\[[^\]]+\]\([^)]+\)")
_EXAMPLES = re.compile(rf"(?m)^#{{1,6}}{_WS}*(examples?|usage|quick start)")
_WHEN = re.compile(rf"(?m)^#{{1,6}}{_WS}*(when to use|use when|triggers?)")
_FM_TOOLS = re.compile(rf"(?m)^allowed-tools{_WS}*:")
_FM_LICENSE = re.compile(rf"(?m)^license{_WS}*:")
_FM_VERSION = re.compile(rf"(?m)^(version|metadata){_WS}*:")
_SPLIT_WS = re.compile(rf"{_WS}+")

_STRUCTURE_COLUMNS: tuple[str, ...] = (
    "desc_chars",
    "body_words",
    "headings",
    "h2",
    "numbered_items",
    "bullet_items",
    "code_blocks",
    "table_rows",
    "links",
    "has_examples_section",
    "has_when_to_use",
    "fm_allowed_tools",
    "fm_license",
    "fm_version_or_metadata",
    "non_ascii_ratio",
)
_OUTPUT_COLUMNS: tuple[str, ...] = (
    "file_sha",
    "location_class",
    "frontmatter_valid",
    "body_chars",
    *_STRUCTURE_COLUMNS,
)

_BUILD_SKILL_FEATURES = (
    'python -c "import duckdb; duckdb.connect().execute('
    "open('db/content_and_structure.sql').read())\""
)
_BUILD_DEV_SAMPLE = "python -m skillsig data --dev-sample"


def split_front_matter(text: str) -> tuple[dict[str, Any], str, bool]:
    """Return (front matter mapping, body, frontmatter_valid). Never raises on bad YAML."""
    body, fm, opened = _split_text(text)
    if not opened:
        return {}, body, False
    loaded = _load_front_matter(fm)
    if loaded is None:
        return {}, body, False
    return loaded, body, True


def structure_features(text: str) -> dict[str, float | int | bool]:
    """Structure columns from db/content_and_structure.sql, for one skill text."""
    body, fm, opened = _split_text(text)
    loaded = _load_front_matter(fm) if opened else None
    lowered_body = body.lower()
    lowered_fm = fm.lower()
    return {
        "desc_chars": _description_chars(loaded),
        "body_words": len(_SPLIT_WS.split(_trim_spaces(body))),
        "headings": len(_HEADINGS.findall(body)),
        "h2": len(_H2.findall(body)),
        "numbered_items": len(_NUMBERED.findall(body)),
        "bullet_items": len(_BULLETS.findall(body)),
        "code_blocks": len(_FENCES.findall(body)) // 2,
        "table_rows": len(_TABLES.findall(body)),
        "links": len(_LINKS.findall(body)),
        "has_examples_section": _EXAMPLES.search(lowered_body) is not None,
        "has_when_to_use": _WHEN.search(lowered_body) is not None,
        "fm_allowed_tools": _FM_TOOLS.search(lowered_fm) is not None,
        "fm_license": _FM_LICENSE.search(lowered_fm) is not None,
        "fm_version_or_metadata": _FM_VERSION.search(lowered_fm) is not None,
        "non_ascii_ratio": _non_ascii_ratio(body),
    }


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Copy skill_features into s2_structure.parquet. Dev mode keeps the dev-sample ids."""
    output = _output_path(cfg)
    inputs = _inputs(cfg, dev=dev)
    _require_inputs(cfg, dev=dev)
    if not force and inputs_unchanged(output, cfg=cfg, inputs=inputs):
        log.info("structure features up to date: %s", output)
        return StageResult(
            stage="s2_features", outputs=[output], skipped=True, rows=_saved_rows(output)
        )
    rows = _copy_skill_features(cfg, output, dev=dev)
    write_manifest(output, cfg=cfg, inputs=inputs, rows=rows)
    log.info("wrote %s (%s rows)", output, rows)
    return StageResult(
        stage="s2_features", outputs=[output], skipped=False, rows={"s2_structure": rows}
    )


def _split_text(text: str) -> tuple[str, str, bool]:
    match = _FRONT_FM.match(text)
    if match is None:
        return text, "", False
    body = _FRONT_BODY.sub("", text, count=1)
    return body, match.group(1), True


def _load_front_matter(fm: str) -> dict[str, Any] | None:
    # The closing fence's newline is outside the capture; put it back so block
    # scalars keep the same trailing newline as the stored description.
    text = fm if fm.endswith("\n") else f"{fm}\n"
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _description_chars(loaded: dict[str, Any] | None) -> int:
    if not loaded:
        return 0
    description = loaded.get("description")
    if isinstance(description, str):
        return len(description)
    if description is None:
        return 0
    return len(str(description))


def _trim_spaces(text: str) -> str:
    """DuckDB trim() drops Unicode space separators (Zs), not tabs or newlines."""
    start, end = 0, len(text)
    while start < end and unicodedata.category(text[start]) == "Zs":
        start += 1
    while end > start and unicodedata.category(text[end - 1]) == "Zs":
        end -= 1
    return text[start:end]


def _non_ascii_ratio(body: str) -> float:
    non_ascii = sum(ord(char) > 127 for char in body)
    return non_ascii / max(len(body), 1)


def _output_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s2_structure.parquet"


def _skill_features_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.generated) / "skill_features.parquet"


def _inputs(cfg: Config, *, dev: bool) -> list[Path]:
    paths = [_skill_features_path(cfg)]
    if dev:
        paths.append(devsample_path(cfg))
    return paths


def _require_inputs(cfg: Config, *, dev: bool) -> None:
    source = _skill_features_path(cfg)
    if not source.is_file():
        raise FileNotFoundError(f"missing {source}; run: {_BUILD_SKILL_FEATURES}")
    if not dev:
        return
    sample = devsample_path(cfg)
    if not sample.is_file():
        raise FileNotFoundError(f"missing {sample}; run: {_BUILD_DEV_SAMPLE}")


def _copy_skill_features(cfg: Config, output: Path, *, dev: bool) -> int:
    """Copy the SQL-built table. Does not recompute features from skill text."""
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = ", ".join(_OUTPUT_COLUMNS)
    where = ""
    if dev:
        sample = _sql_string(devsample_path(cfg))
        where = f"WHERE file_sha IN (SELECT file_sha FROM read_parquet({sample}))"
    dest = _sql_string(output)
    source = _sql_string(_skill_features_path(cfg))
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            COPY (
                SELECT {columns}
                FROM read_parquet({source})
                {where}
                ORDER BY file_sha
            ) TO {dest} (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        counted = con.execute(
            "SELECT count(*) FROM read_parquet(?)", [output.as_posix()]
        ).fetchone()
    finally:
        con.close()
    return int(counted[0]) if counted else 0


def _sql_string(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _saved_rows(output: Path) -> dict[str, int]:
    payload = json.loads(manifest_path(output).read_text(encoding="utf-8"))
    return {"s2_structure": int(payload["rows"])}
