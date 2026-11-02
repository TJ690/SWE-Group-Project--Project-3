"""
What: small CSV tables for RQ1 (families), RQ2 (changes) and RQ3 (staleness).
Inputs: family, copy, staleness and scope parquet files under data_dir.
    Old 31- and 100-registry tables under src/data/genealogy*/ are read
    when they exist, and are never written.
Outputs: one CSV per table in results_dir(dev).
How to run: python -m skill_genealogy.summarize_results [--dev] [--force]

These tables inherit method limits that must be reported with them, for
example that repo-root skills keep only the files their SKILL.md names. The
list is "Limits to report with the results" in
docs/skill_genealogy/ARCHITECTURE_DECISIONS.md. Keep it when the numbers change.
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from skill_genealogy.file_helpers import (
    build_arg_parser,
    check_inputs_exist,
    connect_duckdb,
    outputs_exist,
    print_sample_records,
    print_summary,
)
from skill_genealogy.settings import REPO_ROOT, STALE_AFTER_DAYS, data_dir, results_dir

# Registry role of the repo named before "::" in a family or lineage id.
# registry and registry_hub stay. Every other role, including a missing one, is none.
_ROLE = """
CASE
    WHEN roles.repo_role IN ('registry', 'registry_hub') THEN roles.repo_role
    ELSE 'none'
END
""".strip()

_TABLE_NAMES = (
    "rq1_family_kinds",
    "rq1_family_sizes",
    "rq1_top_families",
    "rq1_registry_original_vs_rehosted",
    "rq2_change_states",
    "rq2_customisation",
    "rq2_change_profile",
    "rq2_folder_changes",
    "rq2_risky_scripts",
    "rq3_copy_states",
    "rq3_missed_risky",
    "rq3_sync_events",
    "scope_language",
    "scope_registries",
    "scope_link_recall",
)

_PROFILE_ORDER = ("registry_update", "copy_local_edit", "copy_sync", "copy_vs_origin")
_STATE_NAMES = ("unchanged", "cosmetic", "substantive")
_EVENT_KINDS = ("copy", "update", "downgrade")
_MISS_KINDS = (
    ("risky", "missed_risky"),
    ("commands", "missed_commands"),
    ("network", "missed_network"),
    ("scripts", "missed_scripts"),
    ("substantive", "missed_substantive"),
)


def _quote(value: str) -> str:
    """Return a single-quoted SQL string with quotes escaped."""
    return "'" + str(value).replace("'", "''") + "'"


def _fill(sql: str, **tokens: str) -> str:
    """Replace ``{name}`` tokens. A leftover brace is a mistake in the query."""
    filled = sql
    for name, value in tokens.items():
        filled = filled.replace("{" + name + "}", value)
    if "{" in filled or "}" in filled:
        start = filled.find("{")
        raise ValueError(f"unfilled SQL placeholder near: {filled[start:start + 40]}")
    return filled


def _query(sql: str) -> pd.DataFrame:
    """Run one DuckDB query and return a DataFrame.

    Share, median, p90 and recall columns become floats. Counts stay as
    DuckDB returned them.
    """
    connection = connect_duckdb()
    try:
        frame = connection.execute(sql).fetchdf()
    finally:
        connection.close()
    return _as_numbers(frame)


def _as_numbers(frame: pd.DataFrame) -> pd.DataFrame:
    """Cast share, median, p90 and recall columns to float."""
    out = frame.copy()
    for column in out.columns:
        name = str(column)
        numeric = (
            "share" in name
            or name.endswith(("_median", "_p90"))
            or name in {"recall", "recall_without_forks"}
        )
        if numeric:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    return out


def _round_share(value: float) -> float:
    """Round a share to three decimal places.

    Example:
    >>> _round_share(2 / 3)
    0.667
    """
    return round(float(value), 3)


def _roles_path(dev: bool) -> Path:
    """Return repo roles for this mode.

    ``repo_roles_final.parquet`` is the dated hub test. When that file is
    not in the mode folder, the stage-2 ``repo_roles.parquet`` is used.
    """
    final = data_dir(dev) / "repo_roles_final.parquet"
    if final.is_file():
        return final
    preview = data_dir(False) / "repo_roles.parquet"
    if preview.is_file():
        return preview
    return final


def _input_paths(dev: bool) -> dict[str, Path]:
    """Every parquet path this stage can read. Missing files are not checked here."""
    mode = data_dir(dev)
    full = data_dir(False)
    paths = {
        "family_summary": mode / "family_summary.parquet",
        "families": mode / "families.parquet",
        "roles": _roles_path(dev),
        "lineage_roots": mode / "lineage_roots.parquet",
        "copy_changes": mode / "copy_changes.parquet",
        "copy_history_edits": mode / "copy_history_edits.parquet",
        "lineage_changes": mode / "lineage_changes.parquet",
        "folder_summary": mode / "copy_version_folder_summary.parquet",
        "folder_changes": mode / "copy_folder_changes.parquet",
        "copy_staleness": mode / "copy_staleness.parquet",
        "copy_files": mode / "copy_files.parquet",
        "sync_events": mode / "sync_events.parquet",
        "change_profile": mode / "change_profile.parquet",
        "skill_language": full / "skill_language.parquet",
        "registries": full / "registries.parquet",
        "proven_pairs": mode / "proven_pairs.parquet",
    }
    if dev:
        paths["dev_registries"] = full / "dev_registries.parquet"
    return paths


def _builders(dev: bool) -> dict[str, str]:
    """Command that builds each input. The flag matches ``dev``."""
    flag = " --dev" if dev else ""
    trees = f"python -m skill_genealogy.build_family_trees{flag}"
    families = f"python -m skill_genealogy.assign_families{flag}"
    rehost = f"python -m skill_genealogy.find_copies{flag} --only rehost"
    origin = f"python -m skill_genealogy.measure_copy_changes{flag} --only origin"
    folders = f"python -m skill_genealogy.measure_copy_changes{flag} --only folders"
    lineages = f"python -m skill_genealogy.build_registry_lineages{flag}"
    stale = f"python -m skill_genealogy.measure_staleness{flag}"
    select = "python -m skill_genealogy.select_registries"
    return {
        "family_summary": trees,
        "families": families,
        "roles": rehost,
        "lineage_roots": rehost,
        "copy_changes": origin,
        "copy_history_edits": origin,
        "change_profile": origin,
        "lineage_changes": lineages,
        "folder_summary": folders,
        "folder_changes": folders,
        "copy_staleness": stale,
        "copy_files": families,
        "sync_events": stale,
        "skill_language": select,
        "registries": select,
        "dev_registries": select,
        "proven_pairs": families,
    }


def _require(dev: bool, *names: str) -> dict[str, Path]:
    """Return every input path, after checking that ``names`` exist."""
    paths = _input_paths(dev)
    chosen = {name: paths[name] for name in names}
    check_inputs_exist(chosen, _builders(dev))
    return paths


def read_inputs(dev: bool) -> dict[str, Path]:
    """Check every input this stage reads and return the paths.

    Inputs: ``dev`` — True for the 30-registry sample.
    Outputs: a name-to-path map. Raises ``FileNotFoundError`` when a file
    is missing. The old genealogy folders are not required.
    """
    paths = _input_paths(dev)
    check_inputs_exist(paths, _builders(dev))
    return paths


def _output_paths(dev: bool) -> list[Path]:
    """CSV paths this run writes. The old comparison is included when those folders exist."""
    folder = results_dir(dev)
    names = list(_TABLE_NAMES)
    if _old_genealogy_roots():
        names.append("compare_old_genealogy")
    return [folder / f"{name}.csv" for name in names]


def _old_genealogy_roots() -> list[tuple[str, Path]]:
    """Return ``(label, folder)`` for each old output folder that has families.

    ``31`` is ``src/data/genealogy``. ``100`` is
    ``src/data/genealogy_100/genealogy``. A folder counts when
    ``g1_families.parquet`` is there. This function does not write.
    """
    candidates = (
        ("31", REPO_ROOT / "src" / "data" / "genealogy"),
        ("100", REPO_ROOT / "src" / "data" / "genealogy_100" / "genealogy"),
    )
    found = []
    for label, folder in candidates:
        if (folder / "g1_families.parquet").is_file():
            found.append((label, folder))
    return found


# Query Q11.1, see QUERIES.md
FAMILY_KINDS_SQL = """
WITH one AS (
    SELECT
        family_id,
        any_value(files_without_forks) AS files_without_forks,
        any_value(repos_without_forks) AS repos_without_forks
    FROM read_parquet({families})
    GROUP BY family_id
),
labeled AS (
    SELECT
        summary.family_kind AS family_kind,
        {role} AS registry_role,
        summary.texts AS texts,
        summary.files AS files,
        summary.repos AS repos,
        coalesce(one.files_without_forks, 0) AS files_without_forks,
        coalesce(one.repos_without_forks, 0) AS repos_without_forks
    FROM read_parquet({summary}) AS summary
    LEFT JOIN one ON summary.family_id = one.family_id
    LEFT JOIN read_parquet({roles}) AS roles
      ON split_part(summary.family_id, '::', 1) = roles.repo
)
SELECT
    family_kind,
    registry_role,
    count(*) AS families,
    sum(texts)::BIGINT AS texts,
    round(sum(texts) * 1.0 / nullif(sum(sum(texts)) OVER (), 0), 3) AS texts_share,
    sum(files)::BIGINT AS files,
    round(sum(files) * 1.0 / nullif(sum(sum(files)) OVER (), 0), 3) AS files_share,
    sum(repos)::BIGINT AS repos,
    round(sum(repos) * 1.0 / nullif(sum(sum(repos)) OVER (), 0), 3) AS repos_share,
    sum(files_without_forks)::BIGINT AS files_without_forks,
    round(
        sum(files_without_forks) * 1.0 / nullif(sum(sum(files_without_forks)) OVER (), 0),
        3
    ) AS files_without_forks_share,
    sum(repos_without_forks)::BIGINT AS repos_without_forks,
    round(
        sum(repos_without_forks) * 1.0 / nullif(sum(sum(repos_without_forks)) OVER (), 0),
        3
    ) AS repos_without_forks_share
FROM labeled
GROUP BY family_kind, registry_role
ORDER BY family_kind, registry_role
"""

# Query Q11.2, see QUERIES.md
FAMILY_SIZES_SQL = """
WITH labeled AS (
    SELECT
        summary.family_kind AS family_kind,
        {role} AS registry_role,
        summary.texts AS texts,
        summary.repos AS repos,
        coalesce(summary.max_generation, 0) AS depth
    FROM read_parquet({summary}) AS summary
    LEFT JOIN read_parquet({roles}) AS roles
      ON split_part(summary.family_id, '::', 1) = roles.repo
)
SELECT
    family_kind,
    registry_role,
    count(*) AS families,
    round(quantile_cont(texts, 0.5), 3) AS texts_median,
    round(quantile_cont(texts, 0.9), 3) AS texts_p90,
    max(texts) AS texts_max,
    round(quantile_cont(repos, 0.5), 3) AS repos_median,
    round(quantile_cont(repos, 0.9), 3) AS repos_p90,
    max(repos) AS repos_max,
    round(quantile_cont(depth, 0.5), 3) AS depth_median,
    round(quantile_cont(depth, 0.9), 3) AS depth_p90,
    max(depth) AS depth_max
FROM labeled
GROUP BY family_kind, registry_role
ORDER BY family_kind, registry_role
"""

# Query Q11.3, see QUERIES.md
TOP_FAMILIES_SQL = """
WITH one AS (
    SELECT
        family_id,
        any_value(files_without_forks) AS files_without_forks,
        any_value(repos_without_forks) AS repos_without_forks
    FROM read_parquet({families})
    GROUP BY family_id
)
SELECT
    summary.family_id AS family_id,
    summary.family_kind AS family_kind,
    {role} AS registry_role,
    summary.texts AS texts,
    summary.files AS files,
    summary.repos AS repos,
    coalesce(summary.max_generation, 0) AS depth,
    coalesce(one.files_without_forks, 0) AS files_without_forks,
    coalesce(one.repos_without_forks, 0) AS repos_without_forks
FROM read_parquet({summary}) AS summary
LEFT JOIN one ON summary.family_id = one.family_id
LEFT JOIN read_parquet({roles}) AS roles
  ON split_part(summary.family_id, '::', 1) = roles.repo
ORDER BY summary.texts DESC, summary.files DESC, summary.family_id
LIMIT 20
"""

# Query Q11.4, see QUERIES.md
ORIGINAL_VS_REHOSTED_SQL = """
WITH labeled AS (
    SELECT
        {role} AS registry_role,
        CASE WHEN roots.is_original THEN 'original' ELSE 'rehosted' END AS lineage_kind
    FROM read_parquet({roots}) AS roots
    LEFT JOIN read_parquet({roles}) AS roles
      ON split_part(roots.lineage_id, '::', 1) = roles.repo
)
SELECT
    registry_role,
    lineage_kind,
    count(*) AS lineages,
    round(count(*) * 1.0 / nullif(sum(count(*)) OVER (), 0), 3) AS share
FROM labeled
GROUP BY registry_role, lineage_kind
ORDER BY registry_role, lineage_kind
"""

# Query Q11.5, see QUERIES.md
CHANGE_STATES_SQL = """
WITH files AS (
    SELECT
        {role} AS registry_role,
        changes.state AS state,
        nullif(changes.current_sha, '') AS current_sha
    FROM read_parquet({changes}) AS changes
    LEFT JOIN read_parquet({roles}) AS roles
      ON split_part(changes.lineage_id, '::', 1) = roles.repo
),
file_counts AS (
    SELECT 'file' AS unit, registry_role, state, count(*) AS count
    FROM files
    GROUP BY registry_role, state
),
text_rank AS (
    SELECT
        registry_role,
        current_sha,
        max(CASE state
            WHEN 'substantive' THEN 2
            WHEN 'cosmetic' THEN 1
            ELSE 0
        END) AS rank
    FROM files
    WHERE current_sha IS NOT NULL
    GROUP BY registry_role, current_sha
),
text_counts AS (
    SELECT
        'text' AS unit,
        registry_role,
        CASE rank WHEN 2 THEN 'substantive' WHEN 1 THEN 'cosmetic' ELSE 'unchanged' END AS state,
        count(*) AS count
    FROM text_rank
    GROUP BY registry_role, rank
)
SELECT
    unit,
    registry_role,
    state,
    count,
    round(count * 1.0 / nullif(sum(count) OVER (PARTITION BY unit, registry_role), 0), 3) AS share
FROM (
    SELECT * FROM file_counts
    UNION ALL
    SELECT * FROM text_counts
) AS counts
ORDER BY unit, registry_role,
    CASE state
        WHEN 'unchanged' THEN 1
        WHEN 'cosmetic' THEN 2
        WHEN 'substantive' THEN 3
        ELSE 4
    END
"""

# Query Q11.6, see QUERIES.md
CUSTOMISATION_SQL = """
WITH files AS (
    SELECT
        {role} AS registry_role,
        changes.customized AS customized,
        changes.headings_added AS headings_added,
        changes.commands_added AS commands_added,
        changes.names_added AS names_added,
        changes.scripts_mentioned_added AS scripts_mentioned_added,
        changes.network_refs_added AS network_refs_added,
        changes.steps_added AS steps_added
    FROM read_parquet({changes}) AS changes
    LEFT JOIN read_parquet({roles}) AS roles
      ON split_part(changes.lineage_id, '::', 1) = roles.repo
)
SELECT
    registry_role,
    count(*) AS files,
    count(*) FILTER (WHERE customized) AS customized,
    round(count(*) FILTER (WHERE customized) * 1.0 / nullif(count(*), 0), 3) AS customized_share,
    count(*) FILTER (WHERE length(headings_added) > 0) AS headings,
    round(
        count(*) FILTER (WHERE length(headings_added) > 0) * 1.0 / nullif(count(*), 0), 3
    ) AS headings_share,
    count(*) FILTER (WHERE length(commands_added) > 0) AS commands,
    round(
        count(*) FILTER (WHERE length(commands_added) > 0) * 1.0 / nullif(count(*), 0), 3
    ) AS commands_share,
    count(*) FILTER (WHERE length(names_added) > 0) AS names,
    round(
        count(*) FILTER (WHERE length(names_added) > 0) * 1.0 / nullif(count(*), 0), 3
    ) AS names_share,
    count(*) FILTER (WHERE length(scripts_mentioned_added) > 0) AS scripts,
    round(
        count(*) FILTER (WHERE length(scripts_mentioned_added) > 0) * 1.0 / nullif(count(*), 0),
        3
    ) AS scripts_share,
    count(*) FILTER (WHERE length(network_refs_added) > 0) AS network,
    round(
        count(*) FILTER (WHERE length(network_refs_added) > 0) * 1.0 / nullif(count(*), 0), 3
    ) AS network_share,
    count(*) FILTER (WHERE steps_added > 0) AS steps,
    round(count(*) FILTER (WHERE steps_added > 0) * 1.0 / nullif(count(*), 0), 3) AS steps_share
FROM files
GROUP BY registry_role
ORDER BY registry_role
"""

_PROFILE_METRICS = """
    count(*) AS changes,
    round(count(*) FILTER (WHERE state = 'substantive') * 1.0 / nullif(count(*), 0), 3)
        AS share_substantive,
    round(count(*) FILTER (WHERE length(headings_added) > 0) * 1.0 / nullif(count(*), 0), 3)
        AS share_headings_added,
    round(count(*) FILTER (WHERE length(commands_added) > 0) * 1.0 / nullif(count(*), 0), 3)
        AS share_commands_added,
    round(count(*) FILTER (WHERE length(network_refs_added) > 0) * 1.0 / nullif(count(*), 0), 3)
        AS share_network_added,
    round(
        count(*) FILTER (WHERE length(scripts_mentioned_added) > 0) * 1.0 / nullif(count(*), 0),
        3
    ) AS share_scripts_added,
    round(count(*) FILTER (WHERE permissions_changed) * 1.0 / nullif(count(*), 0), 3)
        AS share_permissions_changed
"""

# Query Q11.7, see QUERIES.md
CHANGE_PROFILE_SQL = """
WITH copies AS (
    SELECT
        {role} AS registry_role,
        changes.state AS state,
        changes.headings_added AS headings_added,
        changes.commands_added AS commands_added,
        changes.network_refs_added AS network_refs_added,
        changes.scripts_mentioned_added AS scripts_mentioned_added,
        changes.permissions_changed AS permissions_changed
    FROM read_parquet({changes}) AS changes
    LEFT JOIN read_parquet({roles}) AS roles
      ON split_part(changes.lineage_id, '::', 1) = roles.repo
),
edits AS (
    SELECT
        {role} AS registry_role,
        edits.change_kind AS change_kind,
        edits.state AS state,
        edits.headings_added AS headings_added,
        edits.commands_added AS commands_added,
        edits.network_refs_added AS network_refs_added,
        edits.scripts_mentioned_added AS scripts_mentioned_added,
        edits.permissions_changed AS permissions_changed
    FROM read_parquet({edits}) AS edits
    LEFT JOIN read_parquet({roles}) AS roles
      ON split_part(edits.lineage_id, '::', 1) = roles.repo
),
updates AS (
    SELECT
        {role} AS registry_role,
        updates.state AS state,
        updates.headings_added AS headings_added,
        updates.commands_added AS commands_added,
        updates.network_refs_added AS network_refs_added,
        updates.scripts_mentioned_added AS scripts_mentioned_added,
        updates.permissions_changed AS permissions_changed
    FROM read_parquet({updates}) AS updates
    LEFT JOIN read_parquet({roles}) AS roles
      ON split_part(updates.lineage_id, '::', 1) = roles.repo
)
SELECT * FROM (
    SELECT 'registry_update' AS population, registry_role,
""" + _PROFILE_METRICS + """
    FROM updates
    GROUP BY registry_role
    UNION ALL
    SELECT 'copy_local_edit' AS population, registry_role,
""" + _PROFILE_METRICS + """
    FROM edits
    WHERE change_kind = 'local'
    GROUP BY registry_role
    UNION ALL
    SELECT 'copy_sync' AS population, registry_role,
""" + _PROFILE_METRICS + """
    FROM edits
    WHERE change_kind = 'sync'
    GROUP BY registry_role
    UNION ALL
    SELECT 'copy_vs_origin' AS population, registry_role,
""" + _PROFILE_METRICS + """
    FROM copies
    GROUP BY registry_role
) AS profile
ORDER BY CASE population
    WHEN 'registry_update' THEN 1
    WHEN 'copy_local_edit' THEN 2
    WHEN 'copy_sync' THEN 3
    ELSE 4
END, registry_role
"""

# Query Q11.8, see QUERIES.md
FOLDER_CHANGES_SQL = """
WITH latest AS (
    SELECT repo, path, max(version_seq) AS version_seq
    FROM read_parquet({summary})
    WHERE target = 'origin'
    GROUP BY repo, path
),
rows AS (
    SELECT
        {role} AS registry_role,
        summary.files_added AS files_added,
        summary.files_removed AS files_removed,
        summary.files_modified AS files_modified,
        summary.scripts_added AS scripts_added,
        summary.scripts_modified AS scripts_modified,
        summary.new_risky_scripts AS new_risky_scripts,
        summary.missing_scripts AS missing_scripts,
        summary.hidden_scripts AS hidden_scripts
    FROM read_parquet({summary}) AS summary
    JOIN latest
      ON summary.repo = latest.repo
     AND summary.path = latest.path
     AND summary.version_seq = latest.version_seq
    LEFT JOIN read_parquet({copies}) AS copies
      ON summary.repo = copies.repo AND summary.path = copies.path
    LEFT JOIN read_parquet({roles}) AS roles
      ON split_part(copies.lineage_id, '::', 1) = roles.repo
    WHERE summary.target = 'origin'
),
differs AS (
    SELECT
        {role} AS registry_role,
        count(DISTINCT (changes.repo, changes.path)) AS differs
    FROM read_parquet({file_changes}) AS changes
    JOIN latest
      ON changes.repo = latest.repo
     AND changes.path = latest.path
     AND changes.version_seq = latest.version_seq
    LEFT JOIN read_parquet({copies}) AS copies
      ON changes.repo = copies.repo AND changes.path = copies.path
    LEFT JOIN read_parquet({roles}) AS roles
      ON split_part(copies.lineage_id, '::', 1) = roles.repo
    WHERE changes.target = 'origin'
      AND (
          changes.result IN ('added', 'removed', 'modified', 'renamed')
          OR coalesce(changes.mode_changed, false)
      )
    GROUP BY registry_role
),
counted AS (
    SELECT
        registry_role,
        count(*) AS copies,
        count(*) FILTER (
            WHERE coalesce(scripts_added, 0) + coalesce(scripts_modified, 0) > 0
        ) AS changed_scripts,
        round(count(*) FILTER (
            WHERE coalesce(scripts_added, 0) + coalesce(scripts_modified, 0) > 0
        ) * 1.0 / nullif(count(*), 0), 3) AS changed_scripts_share,
        count(*) FILTER (WHERE coalesce(new_risky_scripts, 0) > 0) AS new_risky,
        round(count(*) FILTER (WHERE coalesce(new_risky_scripts, 0) > 0) * 1.0
            / nullif(count(*), 0), 3) AS new_risky_share,
        count(*) FILTER (WHERE length(missing_scripts) > 0) AS missing_scripts,
        round(count(*) FILTER (WHERE length(missing_scripts) > 0) * 1.0
            / nullif(count(*), 0), 3) AS missing_scripts_share,
        count(*) FILTER (WHERE length(hidden_scripts) > 0) AS hidden_scripts,
        round(count(*) FILTER (WHERE length(hidden_scripts) > 0) * 1.0
            / nullif(count(*), 0), 3) AS hidden_scripts_share
    FROM rows
    GROUP BY registry_role
)
SELECT
    counted.registry_role,
    counted.copies,
    coalesce(differs.differs, 0) AS differs,
    round(coalesce(differs.differs, 0) * 1.0 / nullif(counted.copies, 0), 3) AS differs_share,
    counted.changed_scripts,
    counted.changed_scripts_share,
    counted.new_risky,
    counted.new_risky_share,
    counted.missing_scripts,
    counted.missing_scripts_share,
    counted.hidden_scripts,
    counted.hidden_scripts_share
FROM counted
LEFT JOIN differs ON counted.registry_role = differs.registry_role
ORDER BY counted.registry_role
"""

# Query Q11.9, see QUERIES.md
RISKY_SCRIPTS_SQL = """
WITH latest AS (
    SELECT repo, path, max(version_seq) AS version_seq
    FROM read_parquet({summary})
    WHERE target = 'origin'
    GROUP BY repo, path
),
joined AS (
    SELECT
        summary.repo AS repo,
        summary.path AS path,
        {role} AS registry_role,
        summary.new_risky_scripts AS new_risky_scripts,
        changes.new_risk_flags AS new_risk_flags
    FROM read_parquet({summary}) AS summary
    JOIN latest
      ON summary.repo = latest.repo
     AND summary.path = latest.path
     AND summary.version_seq = latest.version_seq
    LEFT JOIN read_parquet({changes}) AS changes
      ON summary.repo = changes.repo
     AND summary.path = changes.path
     AND summary.version_seq = changes.version_seq
     AND changes.target = 'origin'
    LEFT JOIN read_parquet({copies}) AS copies
      ON summary.repo = copies.repo AND summary.path = copies.path
    LEFT JOIN read_parquet({roles}) AS roles
      ON split_part(copies.lineage_id, '::', 1) = roles.repo
    WHERE summary.target = 'origin'
),
totals AS (
    SELECT registry_role, count(*) AS copies
    FROM (
        SELECT DISTINCT registry_role, repo, path FROM joined
    ) AS one
    GROUP BY registry_role
),
flags AS (
    SELECT registry_role, flag, count(DISTINCT (repo, path)) AS copies
    FROM joined
    CROSS JOIN unnest(new_risk_flags) AS u(flag)
    WHERE flag IS NOT NULL AND flag <> ''
    GROUP BY registry_role, flag
    UNION ALL
    SELECT registry_role, 'any' AS flag, count(DISTINCT (repo, path)) AS copies
    FROM joined
    WHERE new_risky_scripts > 0
    GROUP BY registry_role
)
SELECT
    totals.registry_role AS registry_role,
    coalesce(flags.flag, 'any') AS flag,
    coalesce(flags.copies, 0) AS copies,
    round(coalesce(flags.copies, 0) * 1.0 / nullif(totals.copies, 0), 3) AS share
FROM totals
LEFT JOIN flags ON totals.registry_role = flags.registry_role
ORDER BY registry_role, flag
"""

# Query Q11.10, see QUERIES.md
COPY_STATES_SQL = """
WITH rows AS (
    SELECT
        {role} AS registry_role,
        staleness.as_of_basis AS as_of_basis,
        coalesce(copies.repo_role, 'unknown') AS copy_repo_role,
        staleness.copy_state AS copy_state,
        nullif(staleness.current_sha, '') AS current_sha,
        staleness.stale_after_days AS stale_after_days
    FROM read_parquet({staleness}) AS staleness
    LEFT JOIN read_parquet({copies}) AS copies
      ON staleness.repo = copies.repo AND staleness.path = copies.path
    LEFT JOIN read_parquet({roles}) AS roles
      ON split_part(staleness.lineage_id, '::', 1) = roles.repo
)
SELECT
    registry_role,
    as_of_basis,
    copy_repo_role,
    copy_state,
    count(*) AS files,
    count(DISTINCT current_sha) AS texts,
    round(count(*) * 1.0 / nullif(sum(count(*)) OVER (
        PARTITION BY registry_role, as_of_basis, copy_repo_role
    ), 0), 3) AS share,
    max(stale_after_days) AS stale_after_days
FROM rows
GROUP BY registry_role, as_of_basis, copy_repo_role, copy_state
ORDER BY registry_role, as_of_basis, copy_repo_role,
    CASE copy_state
        WHEN 'up_to_date' THEN 1
        WHEN 'behind' THEN 2
        WHEN 'stale' THEN 3
        WHEN 'orphaned' THEN 4
        ELSE 5
    END
"""

# Query Q11.11, see QUERIES.md
MISSED_RISKY_SQL = """
WITH rows AS (
    SELECT
        {role} AS registry_role,
        coalesce(copies.repo_role, 'unknown') AS copy_repo_role,
        staleness.missed_risky AS missed_risky,
        staleness.missed_commands AS missed_commands,
        staleness.missed_network AS missed_network,
        staleness.missed_scripts AS missed_scripts,
        staleness.missed_substantive AS missed_substantive
    FROM read_parquet({staleness}) AS staleness
    LEFT JOIN read_parquet({copies}) AS copies
      ON staleness.repo = copies.repo AND staleness.path = copies.path
    LEFT JOIN read_parquet({roles}) AS roles
      ON split_part(staleness.lineage_id, '::', 1) = roles.repo
)
SELECT * FROM (
""" + " UNION ALL ".join(
    f"""
    SELECT
        registry_role,
        copy_repo_role,
        '{kind}' AS kind,
        count(*) FILTER (WHERE coalesce({column}, 0) > 0) AS copies,
        coalesce(sum({column}), 0)::BIGINT AS events,
        round(count(*) FILTER (WHERE coalesce({column}, 0) > 0) * 1.0
            / nullif(count(*), 0), 3) AS share
    FROM rows
    GROUP BY registry_role, copy_repo_role
    """
    for kind, column in _MISS_KINDS
) + """
) AS missed
ORDER BY registry_role, copy_repo_role,
    CASE kind
        WHEN 'risky' THEN 1
        WHEN 'commands' THEN 2
        WHEN 'network' THEN 3
        WHEN 'scripts' THEN 4
        ELSE 5
    END
"""

# Query Q11.12, see QUERIES.md
SYNC_EVENTS_SQL = """
WITH rows AS (
    SELECT
        {role} AS registry_role,
        coalesce(copies.repo_role, 'unknown') AS copy_repo_role,
        events.event_kind AS event_kind,
        events.lag_days AS lag_days,
        events.versions_skipped AS versions_skipped
    FROM read_parquet({events}) AS events
    LEFT JOIN read_parquet({copies}) AS copies
      ON events.repo = copies.repo AND events.path = copies.path
    LEFT JOIN read_parquet({roles}) AS roles
      ON split_part(events.lineage_id, '::', 1) = roles.repo
)
SELECT
    registry_role,
    copy_repo_role,
    event_kind,
    count(*) AS events,
    round(count(*) * 1.0 / nullif(sum(count(*)) OVER (
        PARTITION BY registry_role, copy_repo_role
    ), 0), 3) AS share,
    round(quantile_cont(lag_days, 0.5), 3) AS lag_days_median,
    max(versions_skipped) AS versions_skipped_max
FROM rows
GROUP BY registry_role, copy_repo_role, event_kind
ORDER BY registry_role, copy_repo_role, event_kind
"""

# Query Q11.13, see QUERIES.md
SCOPE_LANGUAGE_SQL = """
SELECT
    language,
    is_english,
    count(*) AS texts,
    round(count(*) * 1.0 / nullif(sum(count(*)) OVER (), 0), 3) AS share
FROM read_parquet({language})
GROUP BY language, is_english
ORDER BY texts DESC, language
"""

# Query Q11.14, see QUERIES.md
SCOPE_REGISTRIES_SQL = """
SELECT
    registries.repo AS repo,
    registries.stars AS stars,
    registries.english_skills AS english_skills,
    coalesce(roles.repo_role, 'none') AS repo_role,
    round(registries.english_skills * 1.0 / nullif(sum(registries.english_skills) OVER (), 0), 3)
        AS english_skills_share
FROM read_parquet({registries}) AS registries
LEFT JOIN read_parquet({roles}) AS roles
  ON registries.repo = roles.repo
WHERE {dev_clause}
ORDER BY registries.english_skills DESC, registries.repo
"""

# Query Q11.15, see QUERIES.md
SCOPE_RECALL_SQL = """
WITH copies AS (
    SELECT repo, path, any_value(is_fork) AS is_fork, any_value(lineage_id) AS lineage_id
    FROM read_parquet({copies})
    GROUP BY repo, path
),
pairs AS (
    SELECT
        {role} AS registry_role,
        coalesce(copies.is_fork, false) AS is_fork,
        coalesce(pairs.exact, false) AS exact,
        coalesce(pairs.jaccard_found, false) AS jaccard_found,
        coalesce(pairs.containment_found, false) AS containment_found,
        coalesce(pairs.verified_found, false) AS verified_found
    FROM read_parquet({pairs}) AS pairs
    LEFT JOIN copies ON pairs.repo = copies.repo AND pairs.path = copies.path
    LEFT JOIN read_parquet({roles}) AS roles
      ON split_part(coalesce(pairs.lineage_id, copies.lineage_id), '::', 1) = roles.repo
),
flags AS (
    SELECT registry_role, is_fork, 'exact' AS method, exact AS found FROM pairs
    UNION ALL
    SELECT registry_role, is_fork, 'minhash_jaccard', jaccard_found FROM pairs
    UNION ALL
    SELECT registry_role, is_fork, 'containment', containment_found FROM pairs
    UNION ALL
    SELECT registry_role, is_fork, 'line_check', verified_found FROM pairs
    UNION ALL
    SELECT
        registry_role,
        is_fork,
        'any_text_method',
        exact OR jaccard_found OR containment_found OR verified_found
    FROM pairs
)
SELECT
    registry_role,
    method,
    count(*) AS proven_pairs,
    count(*) FILTER (WHERE found) AS found,
    round(count(*) FILTER (WHERE found) * 1.0 / nullif(count(*), 0), 3) AS recall,
    round(
        count(*) FILTER (WHERE found AND NOT is_fork) * 1.0
            / nullif(count(*) FILTER (WHERE NOT is_fork), 0),
        3
    ) AS recall_without_forks
FROM flags
GROUP BY registry_role, method
ORDER BY registry_role,
    CASE method
        WHEN 'exact' THEN 1
        WHEN 'minhash_jaccard' THEN 2
        WHEN 'containment' THEN 3
        WHEN 'line_check' THEN 4
        ELSE 5
    END
"""

# Query Q11.16, see QUERIES.md
OLD_TEXT_SHARES_SQL = """
SELECT
    count(*) FILTER (WHERE family_kind = 'registry') * 1.0 / nullif(count(*), 0)
        AS registry_family,
    count(*) FILTER (WHERE family_kind = 'independent') * 1.0 / nullif(count(*), 0)
        AS independent,
    count(*) FILTER (WHERE family_kind = 'singleton') * 1.0 / nullif(count(*), 0)
        AS singleton
FROM read_parquet({path})
"""

# Query Q11.17, see QUERIES.md
NEW_TEXT_SHARES_SQL = """
SELECT
    count(*) FILTER (WHERE family_kind IN ('source', 'hub_only')) * 1.0
        / nullif(count(*), 0) AS registry_family,
    count(*) FILTER (WHERE family_kind = 'independent') * 1.0 / nullif(count(*), 0)
        AS independent,
    count(*) FILTER (WHERE family_kind = 'singleton') * 1.0 / nullif(count(*), 0)
        AS singleton
FROM read_parquet({path})
"""

# Query Q11.18, see QUERIES.md
STATE_SHARES_SQL = """
SELECT
    count(*) FILTER (WHERE state = 'unchanged') * 1.0 / nullif(count(*), 0) AS unchanged,
    count(*) FILTER (WHERE state = 'cosmetic') * 1.0 / nullif(count(*), 0) AS cosmetic,
    count(*) FILTER (WHERE state = 'substantive') * 1.0 / nullif(count(*), 0) AS substantive
FROM read_parquet({path})
"""

# Query Q11.19, see QUERIES.md
CUSTOM_SHARE_SQL = """
SELECT count(*) FILTER (WHERE customized) * 1.0 / nullif(count(*), 0) AS share
FROM read_parquet({path})
"""

# Query Q11.20, see QUERIES.md
PROFILE_SHARES_SQL = """
SELECT population, share_substantive
FROM read_parquet({path})
"""

# Query Q11.21, see QUERIES.md
OLD_NOT_BEHIND_SQL = """
SELECT count(*) FILTER (WHERE versions_behind = 0) * 1.0 / nullif(count(*), 0) AS share
FROM read_parquet({path})
"""

# Query Q11.22, see QUERIES.md
NEW_NOT_BEHIND_SQL = """
SELECT count(*) FILTER (WHERE skill_md_versions_behind = 0) * 1.0
    / nullif(count(*), 0) AS share
FROM read_parquet({path})
"""

# Query Q11.23, see QUERIES.md
OLD_STALE_SQL = """
SELECT count(*) FILTER (WHERE stale) * 1.0 / nullif(count(*), 0) AS share
FROM read_parquet({path})
"""

# Query Q11.24, see QUERIES.md
NEW_STALE_SQL = """
SELECT count(*) FILTER (WHERE days_stale > {grace}) * 1.0 / nullif(count(*), 0) AS share
FROM read_parquet({path})
"""

# Query Q11.25, see QUERIES.md
EVENT_SHARES_SQL = """
SELECT
    count(*) FILTER (WHERE event_kind = 'copy') * 1.0 / nullif(count(*), 0) AS copy,
    count(*) FILTER (WHERE event_kind = 'update') * 1.0 / nullif(count(*), 0) AS update,
    count(*) FILTER (WHERE event_kind = 'downgrade') * 1.0 / nullif(count(*), 0) AS downgrade
FROM read_parquet({path})
"""

# (sql column, measure name, note text). ``{name}`` is the sql column.
_TEXT_MEASURES = (
    (
        "registry_family",
        "text_share_registry_family",
        "Share of texts in a registry family. The old family_kind is registry. "
        "The new kinds source and hub_only are added together. "
        "Old rows are every text. "
        "New rows are English texts only. The old lists are hand-picked.",
    ),
    (
        "independent",
        "text_share_independent",
        "Share of texts in an independent family. The label matches. "
        "Old rows are every text. New rows are English texts only.",
    ),
    (
        "singleton",
        "text_share_singleton",
        "Share of texts in a singleton family. The label matches. "
        "Old rows are every text. New rows are English texts only.",
    ),
)
_STATE_NOTE = (
    "Share of copy files whose body state is {name}. The three state names match. "
    "Old copies come from the hand-picked list. New copies are English files "
    "with an origin version. Counts are different populations."
)
_STATE_MEASURES = tuple((name, f"file_share_{name}", _STATE_NOTE) for name in _STATE_NAMES)
_CUSTOM_MEASURES = (
    (
        "share",
        "file_share_customized",
        "Share of copy files that added a heading, command, name, script mention "
        "or step. The flag name is customized on both sides. Populations differ, "
        "as in the state rows.",
    ),
)
_NOT_BEHIND_MEASURES = (
    (
        "share",
        "share_text_not_behind",
        "Share of copies whose skill text is not behind the newest release. "
        "Old uses versions_behind = 0. New uses skill_md_versions_behind = 0. "
        "A new copy can still be behind on scripts. That lag is not in this share.",
    ),
)
_STALE_MEASURES = (
    (
        "share",
        "share_stale_after_30_days",
        "Share of copies that are stale after 30 days. Old uses the stored stale flag "
        "(grace_days 30). New uses days_stale > 30, not copy_state, because the file "
        "may have been built with another cutoff. A package-only lag has no days_stale "
        "and is not stale here.",
    ),
)
_EVENT_NOTE = (
    "Share of sync events whose kind is {name}. "
    "Kinds are copy, update and downgrade. The new stage downloads history "
    "for many more files, so the counts differ."
)
_EVENT_MEASURES = tuple((name, f"event_share_{name}", _EVENT_NOTE) for name in _EVENT_KINDS)
# (old parquet name, old sql, new parquet name, new sql, measure specs)
_SHARE_GROUPS = (
    (
        "g1_families.parquet",
        OLD_TEXT_SHARES_SQL,
        "families.parquet",
        NEW_TEXT_SHARES_SQL,
        _TEXT_MEASURES,
    ),
    (
        "g2_copy_summary.parquet",
        STATE_SHARES_SQL,
        "copy_changes.parquet",
        STATE_SHARES_SQL,
        _STATE_MEASURES,
    ),
    (
        "g2_copy_summary.parquet",
        CUSTOM_SHARE_SQL,
        "copy_changes.parquet",
        CUSTOM_SHARE_SQL,
        _CUSTOM_MEASURES,
    ),
    (
        "g3_copy_staleness.parquet",
        OLD_NOT_BEHIND_SQL,
        "copy_staleness.parquet",
        NEW_NOT_BEHIND_SQL,
        _NOT_BEHIND_MEASURES,
    ),
    (
        "g3_copy_staleness.parquet",
        OLD_STALE_SQL,
        "copy_staleness.parquet",
        NEW_STALE_SQL,
        _STALE_MEASURES,
    ),
    (
        "g3_sync_events.parquet",
        EVENT_SHARES_SQL,
        "sync_events.parquet",
        EVENT_SHARES_SQL,
        _EVENT_MEASURES,
    ),
)


def _family_sql(sql: str, paths: dict[str, Path]) -> pd.DataFrame:
    """Run a family query. ``{role}`` is the registry-role expression."""
    text = _fill(
        sql,
        role=_ROLE,
        summary=_quote(paths["family_summary"]),
        families=_quote(paths["families"]),
        roles=_quote(paths["roles"]),
        roots=_quote(paths["lineage_roots"]),
    )
    return _query(text)


def rq1_family_tables(dev: bool) -> dict[str, pd.DataFrame]:
    """Build the four RQ1 tables.

    Inputs: ``dev``. Reads family summary, family text rows, repo roles and
    lineage roots.
    Outputs: ``rq1_family_kinds``, ``rq1_family_sizes``, ``rq1_top_families``,
    ``rq1_registry_original_vs_rehosted``. Shares are rounded to 3 decimals.
    ``repos`` is the sum of each family's repo count, so a repo in two
    families is counted twice. ``files_without_forks`` also drops same-owner
    copies on source families, which is how stage 7 defined that column.
    """
    paths = _require(dev, "family_summary", "families", "roles", "lineage_roots")
    return {
        "rq1_family_kinds": _family_sql(FAMILY_KINDS_SQL, paths),
        "rq1_family_sizes": _family_sql(FAMILY_SIZES_SQL, paths),
        "rq1_top_families": _family_sql(TOP_FAMILIES_SQL, paths),
        "rq1_registry_original_vs_rehosted": _family_sql(ORIGINAL_VS_REHOSTED_SQL, paths),
    }


def _change_sql(sql: str, paths: dict[str, Path]) -> pd.DataFrame:
    """Run a change query with the copy, edit, update and role paths filled in."""
    text = _fill(
        sql,
        role=_ROLE,
        changes=_quote(paths["copy_changes"]),
        edits=_quote(paths["copy_history_edits"]),
        updates=_quote(paths["lineage_changes"]),
        roles=_quote(paths["roles"]),
        summary=_quote(paths["folder_summary"]),
        copies=_quote(paths["copy_files"]),
        file_changes=_quote(paths["folder_changes"]),
    )
    return _query(text)


def rq2_change_tables(dev: bool) -> dict[str, pd.DataFrame]:
    """Build the five RQ2 tables.

    Inputs: ``dev``. Reads copy changes, history edits, lineage changes,
    folder summaries, folder file rows, copy files and repo roles.
    Outputs: change states, customisation, the change profile, folder
    changes and risky-script flags. Folder rows are the latest copy
    version compared with the origin package. A text state is the strongest
    state among files that share ``current_sha``.
    """
    paths = _require(
        dev,
        "copy_changes",
        "copy_history_edits",
        "lineage_changes",
        "folder_summary",
        "folder_changes",
        "copy_files",
        "roles",
    )
    risky = _fill(
        RISKY_SCRIPTS_SQL,
        role=_ROLE,
        summary=_quote(paths["folder_summary"]),
        changes=_quote(paths["folder_changes"]),
        copies=_quote(paths["copy_files"]),
        roles=_quote(paths["roles"]),
    )
    return {
        "rq2_change_states": _change_sql(CHANGE_STATES_SQL, paths),
        "rq2_customisation": _change_sql(CUSTOMISATION_SQL, paths),
        "rq2_change_profile": _change_sql(CHANGE_PROFILE_SQL, paths),
        "rq2_folder_changes": _change_sql(FOLDER_CHANGES_SQL, paths),
        "rq2_risky_scripts": _query(risky),
    }


def rq3_staleness_tables(dev: bool) -> dict[str, pd.DataFrame]:
    """Build the three RQ3 tables.

    Inputs: ``dev``. Reads copy staleness, sync events, copy files and roles.
    Outputs: copy state by registry role, as-of basis and the copy repo's
    role; missed content; sync events. ``share`` on copy states sums to 1
    inside each of those three columns. ``stale_after_days`` is the cutoff
    stored on the staleness file, not a new cutoff.
    """
    paths = _require(dev, "copy_staleness", "sync_events", "copy_files", "roles")
    common = {
        "role": _ROLE,
        "staleness": _quote(paths["copy_staleness"]),
        "copies": _quote(paths["copy_files"]),
        "roles": _quote(paths["roles"]),
        "events": _quote(paths["sync_events"]),
    }
    return {
        "rq3_copy_states": _query(_fill(COPY_STATES_SQL, **common)),
        "rq3_missed_risky": _query(_fill(MISSED_RISKY_SQL, **common)),
        "rq3_sync_events": _query(_fill(SYNC_EVENTS_SQL, **common)),
    }


def scope_tables(dev: bool) -> dict[str, pd.DataFrame]:
    """Build the language, registry and recall tables.

    Inputs: ``dev``. Language labels and the registry list always come from
    the full data folder. Recall is split by the registry role of the proved
    copy. In dev mode the registry table keeps ``dev_registries`` only.
    Outputs: ``scope_language``, ``scope_registries``, ``scope_link_recall``.
    """
    names = ["skill_language", "registries", "roles", "proven_pairs", "copy_files"]
    if dev:
        names.append("dev_registries")
    paths = _require(dev, *names)
    if dev:
        dev_clause = (
            "registries.repo IN (SELECT repo FROM read_parquet("
            + _quote(paths["dev_registries"])
            + "))"
        )
    else:
        dev_clause = "TRUE"
    registries = _query(
        _fill(
            SCOPE_REGISTRIES_SQL,
            registries=_quote(paths["registries"]),
            roles=_quote(paths["roles"]),
            dev_clause=dev_clause,
        )
    )
    recall = _query(
        _fill(
            SCOPE_RECALL_SQL,
            role=_ROLE,
            pairs=_quote(paths["proven_pairs"]),
            copies=_quote(paths["copy_files"]),
            roles=_quote(paths["roles"]),
        )
    )
    language = _query(_fill(SCOPE_LANGUAGE_SQL, language=_quote(paths["skill_language"])))
    return {
        "scope_language": language,
        "scope_registries": registries,
        "scope_link_recall": recall,
    }


def _one_row(sql: str, path: Path) -> dict[str, float] | None:
    """Return the first row of a share query, or None when the file is missing."""
    if not path.is_file():
        return None
    frame = _query(_fill(sql, path=_quote(path), grace=str(int(STALE_AFTER_DAYS))))
    if frame.empty:
        return None
    row = frame.iloc[0]
    values = {}
    for column in frame.columns:
        cell = row[column]
        if cell is None or pd.isna(cell):
            continue
        values[str(column)] = float(cell)
    if not values:
        return None
    return values


def _format_old(parts: list[tuple[str, float]]) -> str:
    """Join ``31: 0.500; 100: 0.223`` from the old runs that had this measure."""
    return "; ".join(f"{label}: {value:.3f}" for label, value in parts)


def _note(dev: bool, text: str) -> str:
    """Add whether ``new`` is the dev sample or the full run."""
    if dev:
        run = "The new number is the 30-registry dev sample."
    else:
        run = "The new number is the full run."
    return text + " " + run


def _append_measure(
    rows: list[dict[str, object]],
    measure: str,
    old_parts: list[tuple[str, float]],
    new_value: float | None,
    note: str,
) -> None:
    """Append one comparison row when both sides have a number."""
    if not old_parts or new_value is None:
        return
    rows.append(
        {
            "measure": measure,
            "old_31_or_100": _format_old(old_parts),
            "new": _round_share(new_value),
            "note": note,
        }
    )


def _paired_measures(
    rows: list[dict[str, object]],
    roots: list[tuple[str, Path]],
    old_file: str,
    old_sql: str,
    new_path: Path,
    new_sql: str,
    measures: list[tuple[str, str, str]],
) -> None:
    """Add one row per ``(sql column, measure name, note)`` that both sides have."""
    new_values = _one_row(new_sql, new_path)
    if new_values is None:
        return
    old_by_column: dict[str, list[tuple[str, float]]] = {column: [] for column, _, _ in measures}
    for label, folder in roots:
        found = _one_row(old_sql, folder / old_file)
        if found is None:
            continue
        for column, parts in old_by_column.items():
            if column in found:
                parts.append((label, found[column]))
    for column, measure, note in measures:
        _append_measure(rows, measure, old_by_column[column], new_values.get(column), note)


def _noted_measures(
    dev: bool,
    specs: tuple[tuple[str, str, str], ...],
) -> list[tuple[str, str, str]]:
    """Fill ``{name}`` and add the dev or full-run sentence.

    Inputs: ``dev`` and specs of ``(sql column, measure name, note text)``.
    Outputs: the same triples, with the note ready for one comparison row.
    """
    noted = []
    for column, measure, text in specs:
        noted.append((column, measure, _note(dev, text.format(name=column))))
    return noted


def compare_with_old_genealogy(dev: bool) -> pd.DataFrame | None:
    """Compare shares with the old 31- and 100-registry genealogy tables.

    Inputs: ``dev``. Old files are read from ``src/data/genealogy`` and
    ``src/data/genealogy_100/genealogy`` when ``g1_families.parquet`` is there.
    Outputs: columns ``measure``, ``old_31_or_100``, ``new``, ``note``.
    Returns None when neither old folder is present. Only shares whose
    labels mean the same thing are included. Counts are left out because
    the registry lists differ. Nothing under ``src/data/genealogy*`` is written.
    """
    roots = _old_genealogy_roots()
    if not roots:
        return None
    mode = data_dir(dev)
    rows: list[dict[str, object]] = []
    groups = list(_SHARE_GROUPS)
    # Profile rows sit after the first three groups (text, state, custom).
    for old_file, old_sql, new_name, new_sql, specs in groups[:3]:
        _paired_measures(
            rows, roots, old_file, old_sql, mode / new_name, new_sql,
            _noted_measures(dev, specs),
        )
    _append_profile_rows(rows, roots, mode / "change_profile.parquet", dev)
    for old_file, old_sql, new_name, new_sql, specs in groups[3:]:
        _paired_measures(
            rows, roots, old_file, old_sql, mode / new_name, new_sql,
            _noted_measures(dev, specs),
        )
    return pd.DataFrame(rows, columns=["measure", "old_31_or_100", "new", "note"])


def _append_profile_rows(
    rows: list[dict[str, object]],
    roots: list[tuple[str, Path]],
    new_path: Path,
    dev: bool,
) -> None:
    """Add substantive-share rows for populations that exist on both sides."""
    new_values = _profile_map(new_path)
    if not new_values:
        return
    old_maps = []
    for label, folder in roots:
        found = _profile_map(folder / "g2_change_profile.parquet")
        if found:
            old_maps.append((label, found))
    note = _note(
        dev,
        "Share of substantive steps in this population. The population names match. "
        "The old profile has no script or permission columns. "
        "The new registry_update row counts lineage text steps.",
    )
    for population in _PROFILE_ORDER:
        if population not in new_values:
            continue
        parts = [
            (label, found[population])
            for label, found in old_maps
            if population in found
        ]
        _append_measure(
            rows,
            f"profile_substantive_{population}",
            parts,
            new_values[population],
            note,
        )


def _profile_map(path: Path) -> dict[str, float]:
    """Map population name to substantive share. A missing file returns {}."""
    if not path.is_file():
        return {}
    frame = _query(_fill(PROFILE_SHARES_SQL, path=_quote(path)))
    if frame.empty or "population" not in frame.columns:
        return {}
    values = {}
    for _, row in frame.iterrows():
        cell = row["share_substantive"]
        if cell is None or pd.isna(cell):
            continue
        values[str(row["population"])] = float(cell)
    return values


def write_tables(tables: dict[str, pd.DataFrame], out_dir: Path) -> None:
    """Write each frame to ``out_dir / "{name}.csv"``.

    Inputs: ``tables`` maps a file name without ``.csv`` to a frame.
    ``out_dir`` is created if it is missing.
    Outputs: one CSV per entry, with a header and no index.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in tables.items():
        frame.to_csv(out_dir / f"{name}.csv", index=False)


def save_outputs(tables: dict[str, pd.DataFrame], dev: bool) -> None:
    """Write the summary CSVs into ``results_dir(dev)``.

    Inputs: the tables from steps 2 to 6, and ``dev``.
    Outputs: the CSV files. Returns nothing.
    """
    write_tables(tables, results_dir(dev))


def _read_written(paths: list[Path]) -> dict[str, pd.DataFrame]:
    """Read CSVs that an earlier run already wrote."""
    tables = {}
    for path in paths:
        tables[path.stem] = pd.read_csv(path)
    return tables


def _print_tables(tables: dict[str, pd.DataFrame], elapsed: float) -> None:
    """Print row counts and the first two rows of each table."""
    numbers: dict[str, object] = {"seconds": round(elapsed, 1)}
    for name, frame in tables.items():
        numbers[name] = len(frame)
    print_summary("summary tables", numbers)
    for name, frame in tables.items():
        print_sample_records(frame, name)


def main() -> None:
    """Write the RQ1–RQ3 CSVs for the dev sample or the full run.

    ``--dev`` reads the dev folder and writes ``results/skill_genealogy_dev``.
    ``--force`` rebuilds the CSVs. A second run without ``--force`` prints
    the saved tables and does not read the inputs again. Old genealogy
    folders are only read.
    """
    started = time.perf_counter()
    parser = build_arg_parser("write RQ1, RQ2 and RQ3 summary tables")
    args = parser.parse_args()
    outputs = _output_paths(args.dev)
    if outputs_exist(outputs) and not args.force:
        print("skip")
        _print_tables(_read_written(outputs), time.perf_counter() - started)
        return
    # Step 1: check input tables
    read_inputs(args.dev)
    # Step 2: RQ1 family tables
    tables = rq1_family_tables(args.dev)
    # Step 3: RQ2 change tables
    tables.update(rq2_change_tables(args.dev))
    # Step 4: RQ3 staleness tables
    tables.update(rq3_staleness_tables(args.dev))
    # Step 5: scope tables (language, registries, recall)
    tables.update(scope_tables(args.dev))
    # Step 6: old genealogy comparison, when those folders exist
    compared = compare_with_old_genealogy(args.dev)
    if compared is not None:
        tables["compare_old_genealogy"] = compared
    # Step 7: write CSVs and print the first rows
    save_outputs(tables, args.dev)
    _print_tables(tables, time.perf_counter() - started)


if __name__ == "__main__":
    main()
