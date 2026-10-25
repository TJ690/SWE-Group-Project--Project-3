"""
STAGE: s2_features
MODULE: skill_type
PURPOSE: Assign a v0 keyword skill type from the skill name and description.
INPUTS: artifacts primary rows (name, description); src/data/derived/s2_repo.parquet
OUTPUTS: src/data/derived/s2_types.parquet, results/s2_skill_type_by_project.csv
RQ LINK: RQ1 (skill type as context for authorship signals) and RQ2 (type by project kind)
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import duckdb

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_data.devsample import output_path as devsample_path

log = logging.getLogger(__name__)

_DOWNLOAD = "python src/data/download_data.py"
_DEV_SAMPLE = "python -m skillsig data --dev-sample"
_REPO_CONTEXT = "python -m skillsig features --only repo_context"

SKILL_TYPES = (
    "meta",
    "office_documents",
    "security",
    "testing_debugging",
    "review_git",
    "frontend_design",
    "planning_agent_workflow",
    "devops_cloud",
    "data_ml_research",
    "backend_api_languages",
    "docs_writing_marketing",
    "business_product",
    "integrations_tools",
    "unclassified",
)

# First match wins. Edit a pattern and re-run the skill-type tests.
# `unclassified` is the fallback when nothing matches, so it has no pattern.
# The word `test` is intentionally broad (known v0 false positives).
RULES: list[tuple[str, str]] = [
    (
        "meta",
        r"skill-creator|skill-installer|find-skills|writing-skills|template-skill",
    ),
    (
        "office_documents",
        r"pdf|docx|xlsx|pptx|spreadsheet|powerpoint",
    ),
    (
        "security",
        r"\bsecurity\b|cybersecur|vulnerab|pentest|penetration[- ]test"
        r"|\bowasp\b|\bcve-\d|\bxss\b|\bcsrf\b|malware|infosec"
        r"|\bsast\b|\bdast\b|threat[- ]model",
    ),
    (
        "testing_debugging",
        r"\btest(?:s|ing|er|ed)?\b|\bdebug(?:ging|ger|ged)?\b"
        r"|\bpytest\b|\btdd\b|\bunittest\b|\bbugfix(?:es)?\b"
        r"|systematic-debugging|test-driven",
    ),
    (
        "review_git",
        r"code-review|\breviews?\b|\bcommits?\b|\bgit\b"
        r"|pull[- ]requests?|merge[- ]requests?|\bchangelog\b|\bworktrees?\b",
    ),
    (
        "frontend_design",
        r"frontend|front-end|\breact\b|\bvue\b|\bsvelte\b|\bangular\b"
        r"|\bcss\b|tailwind|\bfigma\b|\bui\b|\bux\b|\bdesign\b",
    ),
    (
        "planning_agent_workflow",
        r"brainstorm|\bplans?\b|\bplanning\b|workflow|subagent|orchestrat"
        r"|multi-agent|agentic|writing-plans|executing-plans",
    ),
    (
        "devops_cloud",
        r"devops|kubernetes|\bk8s\b|\bdocker\b|terraform|\bci/?cd\b"
        r"|\baws\b|\bazure\b|\bgcp\b|\bcloud\b|deploy(?:ment|ing|s)?\b"
        r"|\binfrastructure\b|\bhelm\b",
    ),
    (
        "data_ml_research",
        r"machine[- ]learning|\bml\b|\bllm\b|\bnlp\b|data[- ]science"
        r"|\bpandas\b|jupyter|\bresearch\b|pytorch|tensorflow"
        r"|\bsklearn\b|\bembedding\b|\bdataset\b",
    ),
    (
        "backend_api_languages",
        r"\bapis?\b|backend|back-end|\bpython\b|\bjava\b|\bgolang\b|\brust\b"
        r"|\btypescript\b|\bjavascript\b|node\.?js|\bgraphql\b|\brest\b"
        r"|\bpostgres(?:ql)?\b|\bredis\b|\bdjango\b|\bflask\b|\bfastapi\b",
    ),
    (
        "docs_writing_marketing",
        r"documentation|\breadme\b|copywriting|\bmarketing\b|\bblog\b"
        r"|technical[- ]writing|\bseo\b|\bnewsletter\b",
    ),
    (
        "business_product",
        r"\bproduct\b|\bprd\b|\broadmap\b|\bpricing\b|\bsales\b|\bfinance\b"
        r"|\binvoice\b|\bstartup\b|go-to-market|\bgtm\b|\bcustomers?\b|\bokrs?\b",
    ),
    (
        "integrations_tools",
        r"\bmcp\b|\bslack\b|\bnotion\b|\bjira\b|\bwebhooks?\b|\bzapier\b"
        r"|\bintegrations?\b|\boauth\b|github[- ]actions?",
    ),
]

_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern)) for label, pattern in RULES
)


def skill_type(name: str | None, description: str | None) -> str:
    """First matching keyword rule on lowercased name and description."""
    text = _haystack(name, description)
    if not text:
        return "unclassified"
    for label, pattern in _COMPILED:
        if pattern.search(text):
            return label
    return "unclassified"


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write s2_types.parquet and the skill-type by project-kind counts."""
    outputs = _output_paths(cfg)
    inputs = _inputs(cfg, dev=dev)
    _require_inputs(cfg, dev=dev)
    if not force and _current(cfg, outputs, inputs):
        log.info("skill types up to date: %s", outputs[0])
        return StageResult(
            stage="s2_features", outputs=outputs, skipped=True, rows=_saved_rows(outputs)
        )
    if dev:
        log.info("skill types keep dev-sample skills and every copy of those skills")
    written = _write(cfg, outputs[0], outputs[1], dev=dev)
    for path, count in zip(outputs, written, strict=True):
        write_manifest(path, cfg=cfg, inputs=inputs, rows=count)
    log.info("wrote %s (%s rows) and %s (%s rows)", outputs[0], written[0], outputs[1], written[1])
    return StageResult(
        stage="s2_features",
        outputs=outputs,
        skipped=False,
        rows={"s2_types": written[0], "s2_skill_type_by_project": written[1]},
    )


def _haystack(name: str | None, description: str | None) -> str:
    left = "" if name is None else str(name).strip()
    right = "" if description is None else str(description).strip()
    return f"{left} {right}".strip().lower()


def _output_paths(cfg: Config) -> list[Path]:
    derived = resolve(cfg, cfg.paths.derived)
    results = resolve(cfg, cfg.paths.results)
    return [derived / "s2_types.parquet", results / "s2_skill_type_by_project.csv"]


def _repo_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s2_repo.parquet"


def _artifacts_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "artifacts"


def _inputs(cfg: Config, *, dev: bool) -> list[Path]:
    paths = [_artifacts_dir(cfg), _repo_path(cfg)]
    if dev:
        paths.append(devsample_path(cfg))
    return paths


def _require_inputs(cfg: Config, *, dev: bool) -> None:
    directory = _artifacts_dir(cfg)
    if not directory.is_dir() or not any(directory.glob("*.parquet")):
        raise FileNotFoundError(f"missing {directory}; run: {_DOWNLOAD}")
    repo = _repo_path(cfg)
    if not repo.is_file():
        raise FileNotFoundError(f"missing {repo}; run: {_REPO_CONTEXT}")
    if dev and not devsample_path(cfg).is_file():
        raise FileNotFoundError(f"missing {devsample_path(cfg)}; run: {_DEV_SAMPLE}")


def _current(cfg: Config, outputs: list[Path], inputs: list[Path]) -> bool:
    return all(inputs_unchanged(path, cfg=cfg, inputs=inputs) for path in outputs)


def _write(cfg: Config, types_path: Path, csv_path: Path, *, dev: bool) -> tuple[int, int]:
    types_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    sample = devsample_path(cfg) if dev else None
    con = duckdb.connect()
    try:
        _register(con)
        con.execute(_types_sql(_artifacts_dir(cfg), types_path, sample))
        con.execute(_counts_sql(_artifacts_dir(cfg), types_path, _repo_path(cfg), csv_path))
        n_types = _count(con, types_path)
        n_csv = _count(con, csv_path)
    finally:
        con.close()
    return n_types, n_csv


def _register(con: duckdb.DuckDBPyConnection) -> None:
    con.create_function(
        "skill_type",
        skill_type,
        ["VARCHAR", "VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )


def _count(con: duckdb.DuckDBPyConnection, path: Path) -> int:
    if path.suffix == ".csv":
        query = "SELECT count(*) FROM read_csv(?, header = true)"
    else:
        query = "SELECT count(*) FROM read_parquet(?)"
    counted = con.execute(query, [path.as_posix()]).fetchone()
    return int(counted[0]) if counted else 0


_TYPES_SQL = """
COPY (
    SELECT
        file_sha,
        skill_type(CAST(name AS VARCHAR), CAST(description AS VARCHAR)) AS skill_type
    FROM read_parquet({artifacts})
    WHERE coalesce(TRY_CAST(dedup_primary AS INTEGER), 0) = 1
      AND file_sha IS NOT NULL
      {sample_filter}
    ORDER BY file_sha
) TO {dest} (FORMAT PARQUET, COMPRESSION ZSTD)
"""

_COUNTS_SQL = """
COPY (
    SELECT
        t.skill_type,
        r.repo_kind,
        CAST(r.language AS VARCHAR) AS language,
        count(*)::BIGINT AS files
    FROM read_parquet({artifacts}) AS a
    JOIN read_parquet({types}) AS t ON a.file_sha = t.file_sha
    LEFT JOIN read_parquet({repos}) AS r ON a.repo_full_name = r.repo_full_name
    GROUP BY t.skill_type, r.repo_kind, r.language
    ORDER BY t.skill_type, r.repo_kind NULLS LAST, r.language NULLS LAST
) TO {dest} (FORMAT CSV, HEADER)
"""


def _types_sql(artifacts: Path, dest: Path, sample: Path | None) -> str:
    sample_filter = ""
    if sample is not None:
        listed = _sql_string(sample)
        sample_filter = f"AND file_sha IN (SELECT file_sha FROM read_parquet({listed}))"
    return _TYPES_SQL.format(
        artifacts=_sql_string(artifacts / "*.parquet"),
        dest=_sql_string(dest),
        sample_filter=sample_filter,
    )


def _counts_sql(artifacts: Path, types: Path, repos: Path, dest: Path) -> str:
    return _COUNTS_SQL.format(
        artifacts=_sql_string(artifacts / "*.parquet"),
        types=_sql_string(types),
        repos=_sql_string(repos),
        dest=_sql_string(dest),
    )


def _sql_string(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _saved_rows(outputs: list[Path]) -> dict[str, int]:
    keys = ("s2_types", "s2_skill_type_by_project")
    saved: dict[str, int] = {}
    for key, path in zip(keys, outputs, strict=True):
        payload = json.loads(manifest_path(path).read_text(encoding="utf-8"))
        saved[key] = int(payload["rows"])
    return saved
