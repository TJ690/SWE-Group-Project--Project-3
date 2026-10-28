"""
STAGE: s0_intake
MODULE: schema_check
PURPOSE: Report missing GitSkills columns and full-dataset row-count mismatches.
INPUTS: DuckDB views registered by loader.connect
OUTPUTS: none (a list of problem strings)
RQ LINK: RQ1 / RQ2 (guards the columns those variables are computed from)
"""

from __future__ import annotations

import duckdb

# Column lists from .cursor/rules/20-data-and-schemas.mdc.
# repo_profile on disk is the projection in db/repos.sql: description and
# metadata_fetched stay on the repos view and are not copied into that file.
VIEW_COLUMNS: dict[str, tuple[str, ...]] = {
    "artifacts": (
        "repo_full_name",
        "path",
        "filename",
        "location_class",
        "file_sha",
        "discovered_at",
        "content",
        "content_fetched",
        "frontmatter_valid",
        "name",
        "description",
        "body_chars",
        "history_fetched",
        "composition_fetched",
        "dedup_primary",
        "first_commit_at",
        "last_commit_at",
        "commit_count",
        "sibling_count",
        "sibling_bytes",
        "has_scripts",
        "has_references",
        "content_sha_ok",
        "composition_truncated",
        "first_commit_author",
        "last_commit_author",
        "first_commit_author_type",
        "last_commit_author_type",
        "first_commit_message",
        "last_commit_message",
    ),
    "repos": (
        "full_name",
        "owner",
        "stars",
        "forks",
        "is_fork",
        "language",
        "license",
        "description",
        "created_at",
        "pushed_at",
        "metadata_fetched",
    ),
    "mining_runs": (
        "run_id",
        "artifact_type",
        "query",
        "started_at",
        "finished_at",
        "discovered",
        "note",
    ),
    "skill_features": (
        "file_sha",
        "location_class",
        "frontmatter_valid",
        "body_chars",
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
    ),
    "file_agents": (
        "repo_full_name",
        "path",
        "file_sha",
        "location_class",
        "agent_folder",
    ),
    "repo_profile": (
        "full_name",
        "owner",
        "stars",
        "forks",
        "is_fork",
        "language",
        "license",
        "created_at",
        "pushed_at",
        "files",
        "distinct_skills",
        "copy_rows",
        "copy_share",
        "name_has_skill",
        "name_is_collection",
        "name_has_agent",
        "size_tier",
    ),
}

VIEW_NAMES: tuple[str, ...] = (
    "artifacts",
    "repos",
    "artifact_siblings",
    "mining_runs",
    "skill_features",
    "file_agents",
    "repo_profile",
    "sibling_files",
)

# Compared with == on the full dataset. Skipped when dev=True.
_COUNTS: tuple[tuple[str, str, int], ...] = (
    ("artifacts", "SELECT count(*) FROM artifacts", 3_797_117),
    (
        "artifacts dedup_primary=1",
        "SELECT count(*) FROM artifacts WHERE dedup_primary = 1",
        1_877_981,
    ),
    ("repos", "SELECT count(*) FROM repos", 282_200),
    ("mining_runs", "SELECT count(*) FROM mining_runs", 7),
)


def check_schema(con: duckdb.DuckDBPyConnection, *, dev: bool = False) -> list[str]:
    """Return problem strings. An empty list means the views match the expected schema."""
    problems = _missing_columns(con)
    if not dev:
        problems.extend(_bad_counts(con))
    return problems


def _column_names(con: duckdb.DuckDBPyConnection, view: str) -> set[str] | None:
    try:
        rows = con.execute(f"DESCRIBE {view}").fetchall()
    except duckdb.Error:
        return None
    return {row[0] for row in rows}


def _missing_columns(con: duckdb.DuckDBPyConnection) -> list[str]:
    problems: list[str] = []
    for view in VIEW_NAMES:
        found = _column_names(con, view)
        if found is None:
            problems.append(f"view {view} is missing")
            continue
        for column in VIEW_COLUMNS.get(view, ()):
            if column not in found:
                problems.append(f"{view} missing column {column}")
    return problems


def _bad_counts(con: duckdb.DuckDBPyConnection) -> list[str]:
    problems: list[str] = []
    for label, sql, expected in _COUNTS:
        actual = int(con.execute(sql).fetchone()[0])
        if actual != expected:
            problems.append(f"{label} row count {actual} != {expected}")
    return problems
