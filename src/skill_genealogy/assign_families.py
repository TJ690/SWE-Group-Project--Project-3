"""Stage 7a: history links, the strongest link per copy file, the origin version, families.

What: stage 5c linked each dataset text to a registry lineage by its current
text. This stage adds the evidence of each copy's own git history (stage 6):
a history that passes through a registry blob (``history_exact``) or starts
near a registry text (``history_near``). Each copy file keeps its strongest
link, the registry version it started from (chosen by date), and when and in
which batch it arrived in its repo. Every English text then gets one family:
a source lineage, a copier that holds it, an independent cluster or a
singleton. Texts only a copier holds get a per-file date comparison with
that copier. Stage 7b then takes the copies proven by their history
(``history_exact``) as a test set and measures how many of them each text
method (exact, MinHash Jaccard, containment, line check) still finds.

Inputs (dev or full data folder): ``copy_candidates.parquet``,
``text_links.parquet``, ``copier_seen.parquet`` (stage 5c),
``lineage_versions.parquet``, ``lineages.parquet`` (stage 4a),
``lineage_roots.parquet``, ``repo_roles_final.parquet`` (stage 5b),
``registry_signatures.npz`` (stage 5a) and ``copied_skill_history/`` (stage 6).
Full data folder: ``skill_language.parquet``, ``registry_kinds.parquet``,
``boilerplate_lines.parquet``, ``dataset_signatures/`` and the stage 3
change rows. Also the GitSkills artifacts and repos and
``src/data/derived/s2_clusters.parquet``.

Outputs (dev or full data folder): ``copy_files.parquet`` (one row per copy
file), ``families.parquet`` (one row per English text),
``copier_seen_files.parquet`` (one row per file of a ``copier_seen`` text),
``history_link_counts.parquet``, and for stage 7b ``proven_pairs.parquet``
(one row per history-proven copy file) and ``link_method_recall.parquet``
(one row per text method).

Run (no network):
    python -m skill_genealogy.assign_families --dev
    python -m skill_genealogy.assign_families --force
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from skill_genealogy import settings
from skill_genealogy.build_registry_lineages import normalized_sha, read_texts
from skill_genealogy.compare_texts import share_of_registry_lines_kept
from skill_genealogy.download_copied_skill_history import skill_folder_of
from skill_genealogy.file_helpers import (
    build_arg_parser,
    check_inputs_exist,
    connect_duckdb,
    outputs_exist,
    print_sample_records,
    print_summary,
    read_parquet,
    write_parquet,
)
from skill_genealogy.find_copies import (
    _line_shares,
    _load_one,
    _open_line_pool,
    _open_sign_pool,
    _prepare_duckdb,
    _quote_path,
    _sign_many,
    _signature_files,
    _texts_path,
    boilerplate_path,
    build_boilerplate_set,
    build_copy_band_index,
    choose_near_links,
    containment_estimates,
    copier_seen_path,
    copy_candidates_path,
    dataset_signatures_dir,
    lineage_roots_path,
    lineages_path,
    load_signatures,
    registry_signature_table,
    registry_signatures_path,
    repo_roles_final_path,
    search_copy_candidates,
    signature_for_text,
    text_links_path,
    version_table,
)

# Link kinds from strongest to weakest (rule 4). exact, near, near_contained
# and cluster come from the current text (stage 5c); history_exact and
# history_near from the copy's own history.
LINK_ORDER = ("exact", "history_exact", "near", "near_contained", "history_near", "cluster")
# Links whose origin is the identical registry version.
EXACT_LINKS = ("exact", "history_exact")
# History rows of the folder's own SKILL.md. copy_source rows are the history
# of another folder of the same repo before this one existed (stage 4a rule).
OWN_HISTORY_ROLES = ("own", "former_folder")
# A copy file's history comes from the stage 6 download or there is none.
HISTORY_SOURCES = ("copied", "none")
# Arrival groups (A34c), in report order.
ARRIVAL_GROUPS = ("chosen", "small_set", "bundle", "unknown")
FAMILY_KINDS = ("source", "copier_seen", "independent", "singleton")
COPIER_RESULTS = ("repo_first", "copier_first", "no_date")
# Origin-version pairs scored at once (128 values each).
ORIGIN_SCORE_BLOCK = 200_000
# Repos the user labelled by hand after stage 6 (stored = copies kept, not
# used; used = the repo uses them). A sanity check only, never data: stored
# repos should hold mostly bundle copies. The list and its sampling are in
# PROGRESS.md (G15 revision 2).
HAND_STORED_REPOS = (
    "xiaocai-admin-2001/SkillForest-opencode",
    "ddandanell/openclaw-skills",
    "adriannoes/awesome-agentic-ai",
    "harsh040506/Claude-Code-Unified-Skill-Plugin-Library",
    "Sukumar-Elley/antigravity-awesome-skills",
    "Anhvu1107/all-agent-skill",
    "cocyuhao/ai-dev-tooling-research-archive",
    "hbui290/agentic-skill-registry",
)
HAND_USED_REPOS = (
    "infosiragpt-ops/SiraGPT-APP",
    "KS-Zeeshan/zeta-v2",
    "ZLZLGe/Harbor",
    "trabelshooterss-collab/forsa-tech",
    "fannndi/service-hub",
    "pckienuit/GoldTrader-Showcase",
    "disdorqin/DARIS",
    "freedom909/real-estate-saas",
    "nastechai/nastech-agent",
    "Vintersong/NOVA-Cognition-Framework",
    "qubit-calculus/Qubit",
    "leonardoteodoroo/amino-advanced",
    "CloudDevCrusader/riddle-rush-mono-repo",
    "chayvolta/iii_iii",
    "JrG-One/AIML-Systems-App",
    "sitaurs/pbl-ppe-detection",
    "andre-sugai/cine-explorer-mvp-block",
    "YongjianWan/--agentskill",
)

COPY_FILE_COLUMNS = (
    "repo",
    "path",
    "file_sha",
    "lineage_id",
    "root_lineage",
    "link",
    "link_similarity",
    "history_source",
    "first_commit_date",
    "origin_seq",
    "origin_date",
    "origin_basis",
    "copied_before_release",
    "is_fork",
    "same_owner_as_registry",
    "repo_role",
    "arrival_commit",
    "arrival_date",
    "batch_size",
    "arrival_group",
    "arrival_in_first_commit",
    "removed",
    "removed_date",
)
FAMILY_COLUMNS = (
    "file_sha",
    "family_id",
    "family_kind",
    "best_link",
    "registry_self",
    "files",
    "repos",
    "owners",
    "files_without_forks",
    "repos_without_forks",
)
COPIER_SEEN_FILE_COLUMNS = (
    "repo",
    "path",
    "file_sha",
    "first_commit_date",
    "copier_first_date",
    "copier_first_repo",
    "copier_result",
    "weak_copiers_only",
)
HISTORY_COLUMNS = (
    "repo",
    "path",
    "skill_folder",
    "commit",
    "commit_seq",
    "commit_date",
    "status",
    "blob",
    "version_seq",
    "history_role",
    "inherited",
    "repo_created_at",
)
# Stage 7b (A34d): one row per history-proven copy file.
PROVEN_PAIR_COLUMNS = (
    "repo",
    "path",
    "lineage_id",
    "current_file_sha",
    "exact",
    "jaccard_found",
    "containment_found",
    "verified_found",
    "best_jaccard",
    "best_containment",
    "best_line_share",
    "stage5_link",
    "current_is_dataset_text",
    "is_fork",
)
# Text methods of A34d and the proven_pairs column that says whether each found the copy.
METHOD_COLUMNS = {
    "exact": "exact",
    "minhash_jaccard": "jaccard_found",
    "containment": "containment_found",
    "line_check": "verified_found",
}
RECALL_COLUMNS = (
    "method",
    "proven_pairs",
    "found",
    "recall",
    "recall_without_forks",
    "changed_pairs",
    "recall_changed_since_crawl",
)
# The best score of a proven pair over its lineage's versions (A34d).
_BEST_SCORES = {"best_jaccard": "max", "best_containment": "max", "best_line_share": "max"}
# Proven pairs whose texts are read and scored at once (A34d).
PROVEN_SCORE_BATCH = 2_000
# One candidate link of one copy file (before rule 4 picks one).
FILE_LINK_COLUMNS = (
    "repo",
    "path",
    "lineage_id",
    "version_seq",
    "release_date",
    "link",
    "link_similarity",
)

# Query Q7.1, see code_flow/13_copy_families.md
# The folder's own top-level SKILL.md rows of every copy folder, dates as UTC.
COPY_HISTORY_SQL = """
SELECT
    changes.repo AS repo,
    changes.skill_folder AS skill_folder,
    changes."commit" AS "commit",
    changes.commit_seq AS commit_seq,
    TRY_CAST(changes.commit_date AS TIMESTAMPTZ) AS commit_date,
    changes.status AS status,
    CASE WHEN changes.status <> 'D' THEN changes.new_sha END AS blob,
    changes.history_role AS history_role
FROM changes
SEMI JOIN copy_folders
  ON changes.repo = copy_folders.repo AND changes.skill_folder = copy_folders.skill_folder
WHERE changes.is_skill_md
  AND lower(changes.file_path) = 'skill.md'
  AND changes.history_role IN ('own', 'former_folder')
"""

# Query Q7.2, see code_flow/13_copy_families.md
# Commits of the copy histories that a registry history also holds (a fork or
# an imported registry history). They stay as evidence, marked inherited.
INHERITED_COMMITS_SQL = """
SELECT DISTINCT registry_changes."commit" AS "commit"
FROM registry_changes
SEMI JOIN history_commits ON registry_changes."commit" = history_commits."commit"
"""

# Query Q7.3, see code_flow/13_copy_families.md
# When GitHub created each copy repo (GitSkills repos.created_at, UTC). The
# stage 6 history holds only the copy folders, so its first commit is not the
# repo's first commit; the creation date is.
REPO_CREATED_SQL = """
SELECT repos.full_name AS repo, min(TRY_CAST(repos.created_at AS TIMESTAMPTZ)) AS repo_created_at
FROM repos
SEMI JOIN copy_repos ON repos.full_name = copy_repos.repo
GROUP BY repos.full_name
"""

# Query Q7.4, see code_flow/13_copy_families.md
# The dataset's first_commit_at of each wanted (repo, path), as UTC.
DATASET_FIRST_COMMIT_SQL = """
SELECT
    artifacts.repo_full_name AS repo,
    artifacts.path AS path,
    min(TRY_CAST(artifacts.first_commit_at AS TIMESTAMPTZ)) AS dataset_first_commit
FROM artifacts
SEMI JOIN wanted_files
  ON artifacts.repo_full_name = wanted_files.repo AND artifacts.path = wanted_files.path
GROUP BY artifacts.repo_full_name, artifacts.path
"""

# Query Q7.5, see code_flow/13_copy_families.md
# Each file outside every registry of a copier_seen text, with its dataset date.
COPIER_SEEN_FILES_SQL = """
SELECT
    artifacts.repo_full_name AS repo,
    artifacts.path AS path,
    artifacts.file_sha AS file_sha,
    min(TRY_CAST(artifacts.first_commit_at AS TIMESTAMPTZ)) AS first_commit_date
FROM artifacts
SEMI JOIN copier_texts ON artifacts.file_sha = copier_texts.file_sha
ANTI JOIN registry_repos ON artifacts.repo_full_name = registry_repos.repo
WHERE artifacts.repo_full_name IS NOT NULL AND artifacts.path IS NOT NULL
GROUP BY artifacts.repo_full_name, artifacts.path, artifacts.file_sha
"""

# Query Q7.6, see code_flow/13_copy_families.md
# Every dataset file of an English text, with fork, registry and copier flags.
ENGLISH_FILES_SQL = """
SELECT DISTINCT
    artifacts.repo_full_name AS repo,
    artifacts.path AS path,
    artifacts.file_sha AS file_sha,
    registry_repos.repo IS NOT NULL AS in_registry,
    copier_repos.repo IS NOT NULL AS in_copier,
    coalesce(forks.is_fork, 0) = 1 AS is_fork
FROM artifacts
JOIN skill_language ON artifacts.file_sha = skill_language.file_sha
LEFT JOIN registry_repos ON artifacts.repo_full_name = registry_repos.repo
LEFT JOIN copier_repos ON artifacts.repo_full_name = copier_repos.repo
LEFT JOIN (
    SELECT full_name, max(is_fork) AS is_fork FROM repos GROUP BY full_name
) AS forks ON artifacts.repo_full_name = forks.full_name
WHERE skill_language.is_english
  AND artifacts.repo_full_name IS NOT NULL
  AND artifacts.path IS NOT NULL
"""

# Query Q7.7, see code_flow/13_copy_families.md
# English texts whose every file is inside a registry repo (registry_self),
# with the root of the oldest lineage version that has that blob, and whether
# a copier holds it.
REGISTRY_TEXTS_SQL = """
WITH registry_only AS (
    SELECT file_sha, bool_or(in_copier) AS in_copier
    FROM english_files
    GROUP BY file_sha
    HAVING bool_and(in_registry)
)
SELECT
    registry_only.file_sha AS file_sha,
    registry_only.in_copier AS in_copier,
    first(version_roots.root_lineage ORDER BY version_roots.release_date,
          version_roots.lineage_id, version_roots.version_seq) AS root_lineage
FROM registry_only
LEFT JOIN version_roots ON registry_only.file_sha = version_roots.blob
GROUP BY registry_only.file_sha, registry_only.in_copier
"""

# Query Q7.8, see code_flow/13_copy_families.md
# Family per English text: source, copier_seen, then clusters of 2+ unclaimed
# English texts at CLUSTER_LINK_THRESHOLD, else singleton.
ASSIGN_FAMILIES_SQL = """
WITH english_texts AS (
    SELECT DISTINCT file_sha FROM skill_language WHERE is_english
),
claims AS (
    SELECT
        english_texts.file_sha AS file_sha,
        coalesce(linked.root_lineage, registry_texts.root_lineage) AS root_lineage,
        linked.best_link AS best_link,
        registry_texts.file_sha IS NOT NULL AS registry_self,
        (copier_texts.file_sha IS NOT NULL OR coalesce(registry_texts.in_copier, false))
            AS held_by_copier
    FROM english_texts
    LEFT JOIN linked ON english_texts.file_sha = linked.file_sha
    LEFT JOIN registry_texts ON english_texts.file_sha = registry_texts.file_sha
    LEFT JOIN copier_texts ON english_texts.file_sha = copier_texts.file_sha
),
open_members AS (
    SELECT
        clusters.file_sha AS file_sha,
        clusters.cluster_id AS cluster_id,
        count(*) OVER (PARTITION BY clusters.cluster_id) AS open_texts
    FROM clusters
    JOIN claims ON clusters.file_sha = claims.file_sha
    WHERE abs(clusters.threshold - {threshold}) < 1e-9
      AND claims.root_lineage IS NULL
      AND NOT claims.held_by_copier
)
SELECT
    claims.file_sha AS file_sha,
    CASE
        WHEN claims.root_lineage IS NOT NULL THEN claims.root_lineage
        WHEN claims.held_by_copier THEN 'copier:' || claims.file_sha
        WHEN open_members.open_texts >= 2 THEN 'cluster:' || open_members.cluster_id
        ELSE 'single:' || claims.file_sha
    END AS family_id,
    CASE
        WHEN claims.root_lineage IS NOT NULL THEN 'source'
        WHEN claims.held_by_copier THEN 'copier_seen'
        WHEN open_members.open_texts >= 2 THEN 'independent'
        ELSE 'singleton'
    END AS family_kind,
    claims.best_link AS best_link,
    claims.registry_self AS registry_self
FROM claims
LEFT JOIN open_members ON claims.file_sha = open_members.file_sha
"""

# Query Q7.9, see code_flow/13_copy_families.md
# Family totals: every English file of the family's texts, and again without
# forks and without files whose owner owns the family's root registry.
FAMILY_COUNTS_SQL = """
WITH family_files AS (
    SELECT
        text_families.family_id AS family_id,
        english_files.repo AS repo,
        lower(split_part(english_files.repo, '/', 1)) AS owner,
        english_files.is_fork AS is_fork,
        lower(split_part(english_files.repo, '/', 1))
            = coalesce(root_owners.owner, '') AS same_owner
    FROM english_files
    JOIN text_families ON english_files.file_sha = text_families.file_sha
    LEFT JOIN root_owners ON text_families.family_id = root_owners.root_lineage
)
SELECT
    family_id,
    count(*) AS files,
    count(DISTINCT repo) AS repos,
    count(DISTINCT owner) AS owners,
    count(*) FILTER (WHERE NOT is_fork AND NOT same_owner) AS files_without_forks,
    count(DISTINCT repo) FILTER (WHERE NOT is_fork AND NOT same_owner) AS repos_without_forks
FROM family_files
GROUP BY family_id
"""

# Query Q7.10, see code_flow/13_copy_families.md
# The origin version of each dated non-exact copy file: the most similar
# version of the linked lineage released on or before the first commit
# (ties: newest release, then highest version_seq).
ORIGIN_PICK_SQL = """
SELECT
    dated.file_key AS file_key,
    scores.version_seq AS origin_seq,
    scores.release_date AS origin_date
FROM dated
JOIN scores
  ON dated.lineage_id = scores.lineage_id AND dated.origin_text = scores.origin_text
WHERE scores.release_date <= dated.first_commit_date
QUALIFY row_number() OVER (
    PARTITION BY dated.file_key
    ORDER BY scores.jaccard DESC NULLS LAST, scores.release_date DESC, scores.version_seq DESC
) = 1
"""

# Query Q7.11, see code_flow/13_copy_families.md
# Dataset text of the proven copies whose current text is not a stage 6 blob.
PROVEN_DATASET_TEXTS_SQL = """
SELECT artifacts.file_sha AS sha, any_value(artifacts.content) AS text
FROM artifacts
SEMI JOIN wanted_texts ON artifacts.file_sha = wanted_texts.sha
GROUP BY artifacts.file_sha
"""


# Paths


def copy_history_dir(dev: bool) -> Path:
    """``copied_skill_history/`` (stage 6) in the dev or full data folder."""
    return settings.data_dir(dev) / "copied_skill_history"


def _registry_changes_path() -> Path:
    """The stage 3 change rows; there is no dev registry history."""
    return settings.data_dir(False) / "registry_history" / "registry_history_changes.parquet"


def output_paths(dev: bool) -> dict[str, Path]:
    """The six stage 7 output files of this mode (four of 7a, two of 7b)."""
    folder = settings.data_dir(dev)
    return {
        "copy_files": folder / "copy_files.parquet",
        "families": folder / "families.parquet",
        "copier_seen_files": folder / "copier_seen_files.parquet",
        "history_link_counts": folder / "history_link_counts.parquet",
        "proven_pairs": folder / "proven_pairs.parquet",
        "link_method_recall": folder / "link_method_recall.parquet",
    }


# Step 1: read the inputs


def read_inputs(con: duckdb.DuckDBPyConnection, dev: bool) -> dict[str, object]:
    """Check every input, register the large tables as views and load the small ones.

    Views: ``artifacts``, ``repos``, ``skill_language``, ``clusters``,
    ``changes`` (stage 6 change rows) and ``registry_changes`` (stage 3).
    Tables: ``registry_repos`` (every stage 3c registry) and ``copier_repos``
    (copiers; in dev only the dev registries' copiers, as stage 5c).
    Returns the version table (``version_table`` of stage 5b), the lineage
    roots, the copy files, the text links, ``copier_seen`` and the repo roles.
    A missing file raises ``FileNotFoundError`` naming the command that builds it.
    """
    flag = " --dev" if dev else ""
    history = copy_history_dir(dev)
    full = settings.data_dir(False)
    paths: dict[str, Path | str] = {
        "artifacts": settings.ARTIFACTS_GLOB,
        "repos": settings.REPOS_GLOB,
        "skill_language": full / "skill_language.parquet",
        "clusters": settings.CLUSTERS_PATH,
        "registry_kinds": full / "registry_kinds.parquet",
        "registry_changes": _registry_changes_path(),
        "boilerplate": boilerplate_path(),
        "dataset_signatures": dataset_signatures_dir(),
        "lineages": lineages_path(dev),
        "lineage_versions": settings.data_dir(dev) / "lineage_versions.parquet",
        "lineage_roots": lineage_roots_path(dev),
        "repo_roles_final": repo_roles_final_path(dev),
        "registry_signatures": registry_signatures_path(dev),
        "copy_candidates": copy_candidates_path(dev),
        "text_links": text_links_path(dev),
        "copier_seen": copier_seen_path(dev),
        "changes": history / "copied_skill_history_changes.parquet",
        "history_texts": history / "copied_skill_history_texts.parquet",
        "history_status": history / "copied_skill_history_status.parquet",
    }
    if dev:
        paths["dev_registries"] = full / "dev_registries.parquet"
    stage5 = f"python -m skill_genealogy.find_copies{flag}"
    check_inputs_exist(
        paths,
        {
            "artifacts": "python src/data/download_data.py",
            "repos": "python src/data/download_data.py",
            "skill_language": "python -m skill_genealogy.select_registries --force-languages",
            "clusters": "main pipeline step 5a (skillsig-dedup)",
            "registry_kinds": "python -m skill_genealogy.label_registry_kinds",
            "registry_changes": "python -m skill_genealogy.download_registry_history",
            "boilerplate": "python -m skill_genealogy.find_copies --only signatures",
            "dataset_signatures": "python -m skill_genealogy.find_copies --only signatures",
            "lineages": f"python -m skill_genealogy.build_registry_lineages{flag}",
            "lineage_versions": f"python -m skill_genealogy.build_registry_lineages{flag}",
            "lineage_roots": f"{stage5} --only rehost",
            "repo_roles_final": f"{stage5} --only rehost",
            "registry_signatures": f"{stage5} --only signatures",
            "copy_candidates": f"{stage5} --only copies",
            "text_links": f"{stage5} --only copies",
            "copier_seen": f"{stage5} --only copies",
            "changes": f"python -m skill_genealogy.download_copied_skill_history{flag}",
            "history_texts": f"python -m skill_genealogy.download_copied_skill_history{flag}",
            "history_status": f"python -m skill_genealogy.download_copied_skill_history{flag}",
            "dev_registries": "python -m skill_genealogy.select_registries",
        },
    )
    for name in ("artifacts", "repos", "skill_language", "clusters", "changes", "registry_changes"):
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT * FROM read_parquet({_quote_path(str(paths[name]))})"
        )
    kinds = read_parquet(paths["registry_kinds"])
    copiers = kinds.loc[kinds["registry_kind"] == "copier", ["repo"]]
    if dev:
        copiers = copiers.loc[copiers["repo"].isin(read_parquet(paths["dev_registries"])["repo"])]
    _create_table(con, "registry_repos", kinds.loc[:, ["repo"]].drop_duplicates())
    _create_table(con, "copier_repos", copiers.drop_duplicates())
    lineages = read_parquet(paths["lineages"])
    return {
        "paths": paths,
        "versions": version_table(read_parquet(paths["lineage_versions"]), lineages),
        "lineages": lineages,
        "roots": read_parquet(paths["lineage_roots"]),
        "copies": read_parquet(paths["copy_candidates"]),
        "text_links": read_parquet(paths["text_links"]),
        "copier_seen": read_parquet(paths["copier_seen"]),
        "repo_roles": read_parquet(paths["repo_roles_final"]).loc[:, ["repo", "repo_role"]],
        "history_status": read_parquet(paths["history_status"]).loc[:, ["repo", "status"]],
    }


def _create_table(con: duckdb.DuckDBPyConnection, name: str, frame: pd.DataFrame) -> None:
    """Copy a pandas frame into a DuckDB table ``name`` (replacing an older one)."""
    con.register(f"{name}_frame", frame.reset_index(drop=True))
    try:
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM {name}_frame")
    finally:
        con.unregister(f"{name}_frame")


def _utc(values: pd.Series) -> pd.Series:
    """Dates as UTC timestamps; anything unreadable becomes ``NaT``.

    >>> _utc(pd.Series(["2026-02-01T10:00:00+02:00", None])).tolist()
    [Timestamp('2026-02-01 08:00:00+0000', tz='UTC'), NaT]
    """
    return pd.to_datetime(values, utc=True, errors="coerce", format="ISO8601")


# Step 2: read the copy histories


def read_copy_histories(dev: bool, copies: pd.DataFrame | None = None) -> pd.DataFrame:
    """The own ``SKILL.md`` history of every copy file, one row per change (rule 1).

    Rows come from stage 6 only (``copied_skill_history_changes``): the
    folder's top-level ``SKILL.md`` (any letter case), role ``own`` or
    ``former_folder``, so a rename keeps its first name's rows and a nested
    ``x/SKILL.md`` belongs to its own folder. Each copy file gets the rows of
    its folder (the parent of ``path``). ``version_seq`` numbers the distinct
    blobs of a file in order of first appearance (UTC date, then
    ``commit_seq``); a delete has none. A commit that a registry history also
    holds (query Q7.2) is kept and marked ``inherited``.
    ``repo_created_at`` is when GitHub created the repo (query Q7.3).

    Inputs: ``dev``; optionally the copy files (``repo, path``), else
    ``copy_candidates.parquet`` is read. Outputs: ``HISTORY_COLUMNS``.
    """
    if copies is None:
        copies = read_parquet(copy_candidates_path(dev)).loc[:, ["repo", "path"]]
    files = copies.loc[:, ["repo", "path"]].drop_duplicates().copy()
    files["skill_folder"] = files["path"].map(skill_folder_of)
    history = copy_history_dir(dev)
    con = connect_duckdb()
    try:
        con.execute(f"SET memory_limit = '{settings.HISTORY_DUCKDB_MEMORY_LIMIT}'")
        for name, path in (
            ("changes", history / "copied_skill_history_changes.parquet"),
            ("registry_changes", _registry_changes_path()),
            ("repos", settings.REPOS_GLOB),
        ):
            con.execute(
                f"CREATE VIEW {name} AS SELECT * FROM read_parquet({_quote_path(str(path))})"
            )
        _create_table(con, "copy_folders", files.loc[:, ["repo", "skill_folder"]].drop_duplicates())
        _create_table(con, "copy_repos", files.loc[:, ["repo"]].drop_duplicates())
        con.execute(f"CREATE TABLE copy_history AS {COPY_HISTORY_SQL}")
        con.execute('CREATE TABLE history_commits AS SELECT DISTINCT "commit" FROM copy_history')
        inherited = set(con.execute(INHERITED_COMMITS_SQL).df()["commit"])
        rows = con.execute("SELECT * FROM copy_history").df()
        created = con.execute(REPO_CREATED_SQL).df()
    finally:
        con.close()
    rows["inherited"] = rows["commit"].isin(inherited)
    rows = rows.merge(created, on="repo", how="left")
    return assemble_histories(files, rows)


def assemble_histories(files: pd.DataFrame, rows: pd.DataFrame) -> pd.DataFrame:
    """Attach folder history rows to copy files and number each file's text versions.

    ``files``: ``repo, path, skill_folder``. ``rows``: one row per folder
    change with ``commit, commit_seq, commit_date, status, blob,
    history_role, inherited, repo_created_at``. Rows are sorted by file,
    UTC date (unknown last), ``commit_seq``.

    >>> files = pd.DataFrame({"repo": ["a/r"], "path": ["s/SKILL.md"], "skill_folder": ["s"]})
    >>> rows = pd.DataFrame({"repo": ["a/r"] * 3, "skill_folder": ["s"] * 3,
    ...     "commit": ["c1", "c2", "c3"], "commit_seq": [1, 2, 3],
    ...     "commit_date": ["2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"],
    ...     "status": ["A", "M", "M"], "blob": ["x", "y", "x"], "history_role": ["own"] * 3,
    ...     "inherited": [False] * 3, "repo_created_at": [None] * 3})
    >>> assemble_histories(files, rows)["version_seq"].tolist()
    [1, 2, 1]
    """
    joined = files.merge(rows, on=["repo", "skill_folder"], how="inner")
    joined["commit_date"] = _utc(joined["commit_date"])
    joined["commit_seq"] = joined["commit_seq"].astype("int64")
    joined = joined.sort_values(
        ["repo", "path", "commit_date", "commit_seq", "status"],
        kind="mergesort",
        na_position="last",
    ).reset_index(drop=True)
    texts = joined.loc[joined["blob"].notna() & (joined["status"] != "D")]
    firsts = texts.drop_duplicates(["repo", "path", "blob"]).copy()
    firsts["version_seq"] = firsts.groupby(["repo", "path"]).cumcount() + 1
    joined = joined.merge(
        firsts.loc[:, ["repo", "path", "blob", "version_seq"]],
        on=["repo", "path", "blob"],
        how="left",
    )
    joined.loc[joined["status"] == "D", "version_seq"] = pd.NA
    joined["version_seq"] = joined["version_seq"].astype("Int64")
    joined["inherited"] = joined["inherited"].fillna(False).astype(bool)
    return joined.loc[:, list(HISTORY_COLUMNS)]


def first_history_versions(histories: pd.DataFrame) -> pd.DataFrame:
    """The first downloaded text of each copy file: ``repo, path, blob, commit_date``.

    >>> rows = pd.DataFrame({"repo": ["a/r"] * 2, "path": ["SKILL.md"] * 2,
    ...     "blob": ["x", "y"], "version_seq": [1, 2],
    ...     "commit_date": pd.to_datetime(["2026-01-01", "2026-02-01"], utc=True)})
    >>> first_history_versions(rows)["blob"].tolist()
    ['x']
    """
    first = histories.loc[histories["version_seq"] == 1]
    return (
        first.drop_duplicates(["repo", "path"])
        .loc[:, ["repo", "path", "blob", "commit_date"]]
        .reset_index(drop=True)
    )


# Step 3: history_exact links (Algorithm A30)


# Algorithm A30, see code_flow/13_copy_families.md
def find_history_exact_links(
    histories: pd.DataFrame, registry_versions: pd.DataFrame
) -> pd.DataFrame:
    """Link a copy file whose own history holds a registry version blob.

    Rule: a history row (not a delete) whose blob equals a lineage version
    blob links the file to that version. With several, the earliest such row
    of the copy's history wins (UTC date, then ``commit_seq``); a blob in
    several lineages takes the oldest release, then ``lineage_id``, then
    ``version_seq``. Inherited commits count: they are the copy's history.

    Inputs: history rows (``HISTORY_COLUMNS``); versions (``lineage_id,
    version_seq, blob, release_date``). Outputs: ``FILE_LINK_COLUMNS`` with
    ``link = history_exact`` and similarity 1.0.

    Example: the history passes through v1's blob, then a local edit; the
    current text matches nothing, and the file links to v1.

    >>> history = pd.DataFrame({"repo": ["a/r"] * 2, "path": ["SKILL.md"] * 2,
    ...     "blob": ["v1blob", "edited"], "status": ["A", "M"], "commit_seq": [1, 2],
    ...     "commit_date": pd.to_datetime(["2026-02-01", "2026-03-01"], utc=True)})
    >>> versions = pd.DataFrame({"lineage_id": ["L", "L"], "version_seq": [1, 2],
    ...     "blob": ["v1blob", "v2blob"],
    ...     "release_date": pd.to_datetime(["2026-01-10", "2026-03-01"], utc=True)})
    >>> links = find_history_exact_links(history, versions)
    >>> links[["lineage_id", "version_seq", "link"]].values.tolist()
    [['L', 1, 'history_exact']]
    """
    rows = histories.loc[histories["blob"].notna() & (histories["status"] != "D")]
    matched = rows.loc[:, ["repo", "path", "blob", "commit_date", "commit_seq"]].merge(
        registry_versions.loc[:, ["lineage_id", "version_seq", "blob", "release_date"]],
        on="blob",
        how="inner",
    )
    ordered = matched.sort_values(
        ["repo", "path", "commit_date", "commit_seq", "release_date", "lineage_id", "version_seq"],
        kind="mergesort",
        na_position="last",
    )
    picked = ordered.drop_duplicates(["repo", "path"]).copy()
    picked["link"] = "history_exact"
    picked["link_similarity"] = 1.0
    return _as_file_links(picked)


def _as_file_links(frame: pd.DataFrame) -> pd.DataFrame:
    """``FILE_LINK_COLUMNS`` with stable types (an empty frame keeps its columns)."""
    links = frame.reindex(columns=list(FILE_LINK_COLUMNS)).reset_index(drop=True)
    links["version_seq"] = links["version_seq"].astype("int64")
    links["release_date"] = pd.to_datetime(links["release_date"], utc=True)
    links["link_similarity"] = links["link_similarity"].astype("float64")
    return links


# Step 4: signatures of the copy texts


def collect_text_signatures(
    wanted: set[str], history_texts: Path, boilerplate: set[str]
) -> dict[str, object]:
    """MinHash signatures of the given texts (blob shas), as one lookup table.

    A text that is a dataset text already has a signature in
    ``dataset_signatures/`` (stage 5a); it is read from there. Other texts
    (older versions in a copy history) are read from the stage 6 texts table
    and signed the stage 5a way (boilerplate removed), ``HISTORY_SIGN_BATCH``
    at a time. A text with no shingle left, or with no stored text, has no
    signature.

    Outputs: ``{"ids", "row" (sha -> row), "signatures" uint32 [n, 128], "sizes"}``.
    """
    ids, blocks, size_blocks = [], [], []
    for path in _signature_files(dataset_signatures_dir()):
        part_ids, signatures, sizes = _load_one(path)
        keep = np.flatnonzero(pd.Index(part_ids).isin(pd.Index(list(wanted))))
        ids.extend(part_ids[row] for row in keep)
        blocks.append(signatures[keep].astype(np.uint32))
        size_blocks.append(sizes[keep])
    missing = sorted(wanted - set(ids))
    print(f"signatures: {len(ids)} from stage 5a, {len(missing)} to sign", flush=True)
    signed_ids, signed, signed_sizes = sign_history_texts(missing, history_texts, boilerplate)
    ids.extend(signed_ids)
    blocks.append(signed)
    size_blocks.append(signed_sizes)
    return signature_book(ids, np.vstack(blocks), np.concatenate(size_blocks))


def sign_history_texts(
    shas: list[str], history_texts: Path, boilerplate: set[str]
) -> tuple[list[str], np.ndarray, np.ndarray]:
    """Sign copy-history texts in batches; return ids, ``uint32`` signatures and sizes.

    Texts with no shingle after boilerplate removal, or not stored, are left out.
    """
    from skill_genealogy.build_registry_lineages import read_texts

    ids: list[str] = []
    blocks = [np.zeros((0, settings.MINHASH_NUM_PERM), dtype=np.uint32)]
    size_blocks = [np.zeros((0,), dtype=np.int32)]
    pool = _open_sign_pool(boilerplate) if len(shas) >= 512 else None
    try:
        for start in range(0, len(shas), settings.HISTORY_SIGN_BATCH):
            batch = shas[start : start + settings.HISTORY_SIGN_BATCH]
            texts = read_texts(history_texts, batch)
            stored = [sha for sha in batch if sha in texts]
            signed = _sign_many([texts[sha] for sha in stored], boilerplate, pool)
            del texts
            kept = [index for index, (_values, size) in enumerate(signed) if size > 0]
            ids.extend(stored[index] for index in kept)
            if kept:
                blocks.append(np.vstack([signed[index][0] for index in kept]).astype(np.uint32))
                size_blocks.append(np.asarray([signed[index][1] for index in kept], np.int32))
            print(f"history signatures: {start + len(batch)}/{len(shas)}", flush=True)
    finally:
        if pool is not None:
            pool.close()
            pool.join()
    return ids, np.vstack(blocks), np.concatenate(size_blocks)


def signature_book(ids: list[str], signatures: np.ndarray, sizes: np.ndarray) -> dict[str, object]:
    """Signatures with an id -> row lookup (the first row wins for a repeated id)."""
    row: dict[str, int] = {}
    for position, key in enumerate(ids):
        row.setdefault(str(key), position)
    return {
        "ids": [str(key) for key in ids],
        "row": row,
        "signatures": np.asarray(signatures, dtype=np.uint32).reshape(
            -1, settings.MINHASH_NUM_PERM
        ),
        "sizes": np.asarray(sizes, dtype=np.int64).reshape(-1),
    }


# Step 5: history_near links (Algorithm A31)


# Algorithm A31, see code_flow/13_copy_families.md
def find_history_near_links(
    histories: pd.DataFrame,
    registry_sigs: dict[str, object],
    unlinked_files: pd.DataFrame,
    text_sigs: dict[str, object],
) -> pd.DataFrame:
    """Link a copy file whose first downloaded version is near a registry version.

    Only files in ``unlinked_files`` are scored: files with history whose
    links so far are all weaker than ``history_near`` (no exact,
    history_exact, near or near_contained link). Only the first downloaded
    version is compared; a later version is not searched. The search is the
    stage 5c near search (A25): registry versions sharing an LSH band, then
    the highest estimated Jaccard at least ``NEAR_COPY_MIN_JACCARD``, ties to
    the oldest release.

    Inputs: history rows; ``registry_sigs`` from ``registry_signature_table``
    plus ``"index"`` from ``build_copy_band_index``; files (``repo, path``);
    signatures of the first versions (``signature_book``).
    Outputs: ``FILE_LINK_COLUMNS`` with ``link = history_near``.
    """
    first = first_history_versions(histories).merge(
        unlinked_files.loc[:, ["repo", "path"]].drop_duplicates(), on=["repo", "path"]
    )
    blobs = sorted(set(first["blob"]) & set(text_sigs["row"]))
    if not blobs or not len(registry_sigs["versions"]):
        return _as_file_links(pd.DataFrame())
    rows = np.asarray([text_sigs["row"][blob] for blob in blobs], dtype=np.int64)
    query = (blobs, text_sigs["signatures"][rows], text_sigs["sizes"][rows])
    near = choose_near_links(search_copy_candidates(query, registry_sigs["index"], registry_sigs))
    found = first.merge(
        near.loc[:, ["file_sha", "lineage_id", "version_seq", "release_date", "jaccard"]],
        left_on="blob",
        right_on="file_sha",
        how="inner",
    )
    found["link"] = "history_near"
    found["link_similarity"] = found["jaccard"]
    return _as_file_links(found)


# Step 6: the strongest link of each copy file (Algorithm A32)


def text_link_rows(copies: pd.DataFrame, versions: pd.DataFrame) -> pd.DataFrame:
    """Each copy file's stage 5c link as a candidate (``FILE_LINK_COLUMNS``).

    ``link_similarity`` is the score that admitted the link: 1.0 for exact,
    the Jaccard for near, the containment for near_contained and the
    line-check share for cluster (a cluster pair was never scored).
    """
    rows = copies.merge(
        versions.loc[:, ["lineage_id", "version_seq", "release_date"]],
        on=["lineage_id", "version_seq"],
        how="left",
    )
    similarity = {
        "exact": pd.Series(1.0, index=rows.index),
        "near": rows["jaccard"],
        "near_contained": rows["containment"],
        "cluster": rows["similarity_verified"],
    }
    rows["link_similarity"] = np.nan
    for kind, values in similarity.items():
        mask = rows["link"] == kind
        rows.loc[mask, "link_similarity"] = values[mask]
    return _as_file_links(rows)


# Algorithm A32, see code_flow/13_copy_families.md
def pick_strongest_link(file_links: pd.DataFrame) -> pd.DataFrame:
    """One link per copy file: the first in ``LINK_ORDER``, then the oldest version.

    Order: exact, history_exact, near, near_contained, history_near, cluster.
    A tie (same kind) goes to the oldest release, then ``lineage_id``, then
    ``version_seq``. Copiers have no lineages, so no copier can win.

    >>> links = pd.DataFrame({"repo": ["a/r"] * 6, "path": ["SKILL.md"] * 6,
    ...     "lineage_id": ["C", "HN", "NC", "N", "HE", "E"], "version_seq": [1] * 6,
    ...     "release_date": pd.to_datetime(["2026-01-01"] * 6, utc=True),
    ...     "link": ["cluster", "history_near", "near_contained", "near", "history_exact", "exact"],
    ...     "link_similarity": [0.6, 0.9, 0.9, 0.9, 1.0, 1.0]})
    >>> pick_strongest_link(links)["link"].tolist()
    ['exact']
    >>> pick_strongest_link(links.iloc[:5])["link"].tolist()
    ['history_exact']
    """
    ranked = file_links.assign(link_rank=file_links["link"].map(LINK_ORDER.index))
    ordered = ranked.sort_values(
        ["repo", "path", "link_rank", "release_date", "lineage_id", "version_seq"],
        kind="mergesort",
        na_position="last",
    )
    return (
        ordered.drop_duplicates(["repo", "path"]).drop(columns=["link_rank"]).reset_index(drop=True)
    )


def attach_file_facts(
    winners: pd.DataFrame,
    copies: pd.DataFrame,
    histories: pd.DataFrame,
    tables: dict[str, object],
    dataset_dates: pd.DataFrame,
) -> pd.DataFrame:
    """Add text, root, flags, role, history source and first commit date to each file.

    ``root_lineage`` is the stage 5b root of the linked lineage (itself
    without a root row). ``same_owner_as_registry`` compares the copy's owner
    with the owner of the linked lineage's registry (any letter case).
    ``first_commit_date`` is the first commit of the file's history (rule 6),
    else the dataset ``first_commit_at``. ``origin_text`` is the text the
    origin is chosen with: the first downloaded version, else the current text.
    """
    files = copies.loc[:, ["repo", "path", "file_sha", "is_fork"]].merge(
        winners, on=["repo", "path"], how="left"
    )
    roots = tables["roots"].loc[:, ["lineage_id", "root_lineage"]].drop_duplicates("lineage_id")
    files = files.merge(roots, on="lineage_id", how="left")
    files["root_lineage"] = files["root_lineage"].fillna(files["lineage_id"])
    registry = tables["lineages"].loc[:, ["lineage_id", "repo"]].drop_duplicates("lineage_id")
    registry = registry.rename(columns={"repo": "registry_repo"})
    files = files.merge(registry, on="lineage_id", how="left")
    files["same_owner_as_registry"] = _owner(files["repo"]) == _owner(
        files["registry_repo"].fillna("")
    )
    files = files.merge(tables["repo_roles"].drop_duplicates("repo"), on="repo", how="left")
    first = first_history_versions(histories).rename(
        columns={"blob": "first_blob", "commit_date": "history_first_date"}
    )
    files = files.merge(first, on=["repo", "path"], how="left")
    files = files.merge(dataset_dates, on=["repo", "path"], how="left")
    files["history_source"] = np.where(
        files.set_index(["repo", "path"]).index.isin(histories.set_index(["repo", "path"]).index),
        "copied",
        "none",
    )
    files["first_commit_date"] = files["history_first_date"].fillna(
        _utc(files["dataset_first_commit"])
    )
    files["origin_text"] = files["first_blob"].fillna(files["file_sha"])
    files["is_fork"] = files["is_fork"].fillna(False).astype(bool)
    return files.drop(columns=["registry_repo", "first_blob", "history_first_date"])


def _owner(repos: pd.Series) -> pd.Series:
    """The lower-case text before the first ``/`` of each repo name."""
    return repos.astype(str).str.split("/", n=1).str[0].str.lower()


def read_dataset_dates(con: duckdb.DuckDBPyConnection, files: pd.DataFrame) -> pd.DataFrame:
    """Query Q7.4: ``repo, path, dataset_first_commit`` (UTC) for the given files."""
    con.register("wanted_files", files.loc[:, ["repo", "path"]].drop_duplicates())
    try:
        dates = con.execute(DATASET_FIRST_COMMIT_SQL).df()
    finally:
        con.unregister("wanted_files")
    dates["dataset_first_commit"] = _utc(dates["dataset_first_commit"])
    return dates


# Step 7: the origin version, by date (Algorithm A33)


# Algorithm A33, see code_flow/13_copy_families.md
def choose_origin_version(
    copy_files: pd.DataFrame, registry_versions: pd.DataFrame, sigs: dict[str, object]
) -> pd.DataFrame:
    """The registry version each copy file started from, chosen by date.

    - ``exact`` / ``history_exact``: the identical version (``origin_basis =
      exact``), even without a date.
    - other links with a ``first_commit_date``: the version of the linked
      lineage released on or before that date whose signature is most
      similar to the copy's first text (estimated Jaccard; ties and missing
      signatures go to the newest such version), ``similar_before_date``.
    - no version released on or before that date: ``copied_before_release``,
      no origin and no basis. An exact origin released after the copy's first
      commit is also ``copied_before_release`` (it keeps its origin).
    - no date at all: ``no_date``, no origin, ``copied_before_release`` False.

    Inputs: copy files (``repo, path, link, lineage_id, version_seq,
    first_commit_date, origin_text``); versions (``lineage_id, version_seq,
    release_date, version_id``); ``{"copy": signature_book of texts,
    "registry": signature_book of version ids}``.
    Outputs: ``repo, path, origin_seq, origin_date, origin_basis,
    copied_before_release``.

    Worked example: registry v1 (Jan 10), v2 (Mar 1), v3 (May 15); a near
    copy first committed Feb 1 can only start from v1. On Jan 1 it was
    copied before any release.

    >>> versions = pd.DataFrame({"lineage_id": ["L"] * 3, "version_seq": [1, 2, 3],
    ...     "release_date": pd.to_datetime(["2026-01-10", "2026-03-01", "2026-05-15"], utc=True),
    ...     "version_id": ["L#1", "L#2", "L#3"]})
    >>> files = pd.DataFrame({"repo": ["a/r", "b/r"], "path": ["SKILL.md"] * 2,
    ...     "link": ["near", "near"], "lineage_id": ["L", "L"], "version_seq": [3, 3],
    ...     "first_commit_date": pd.to_datetime(["2026-02-01", "2026-01-01"], utc=True),
    ...     "origin_text": ["t", "t"]})
    >>> empty = signature_book([], np.zeros((0, 128)), np.zeros(0))
    >>> origin = choose_origin_version(files, versions, {"copy": empty, "registry": empty})
    >>> origin[["origin_seq", "origin_basis", "copied_before_release"]].values.tolist()
    [[1, 'similar_before_date', False], [<NA>, None, True]]
    """
    result = copy_files.loc[:, ["repo", "path"]].copy()
    result["origin_seq"] = pd.Series(pd.NA, index=result.index, dtype="Int64")
    result["origin_date"] = pd.Series(pd.NaT, index=result.index, dtype="datetime64[ns, UTC]")
    result["origin_basis"] = pd.Series(None, index=result.index, dtype="object")
    result["copied_before_release"] = False
    first_date = pd.to_datetime(copy_files["first_commit_date"], utc=True)
    _fill_exact_origins(result, copy_files, registry_versions, first_date)
    fuzzy = copy_files["lineage_id"].notna() & ~copy_files["link"].isin(EXACT_LINKS)
    result.loc[fuzzy & first_date.isna(), "origin_basis"] = "no_date"
    dated = copy_files.loc[fuzzy & first_date.notna()]
    if len(dated):
        picked = _pick_dated_origins(dated, registry_versions, sigs)
        found = dated.index.isin(picked.index)
        hit = dated.index[found]
        result.loc[hit, "origin_seq"] = picked.loc[hit, "origin_seq"].astype("Int64")
        result.loc[hit, "origin_date"] = pd.to_datetime(picked.loc[hit, "origin_date"], utc=True)
        result.loc[hit, "origin_basis"] = "similar_before_date"
        result.loc[dated.index[~found], "copied_before_release"] = True
    basis = result["origin_basis"].astype(object)
    result["origin_basis"] = basis.where(basis.notna(), None)
    return result.reset_index(drop=True)


def _fill_exact_origins(
    result: pd.DataFrame,
    copy_files: pd.DataFrame,
    versions: pd.DataFrame,
    first_date: pd.Series,
) -> None:
    """Rule 6, exact links: the identical version; before release when it came later."""
    exact = copy_files["link"].isin(EXACT_LINKS)
    if not exact.any():
        return
    dates = versions.set_index(["lineage_id", "version_seq"])["release_date"]
    keys = pd.MultiIndex.from_arrays(
        [copy_files.loc[exact, "lineage_id"], copy_files.loc[exact, "version_seq"].astype("int64")]
    )
    released = pd.Series(
        pd.to_datetime(dates.reindex(keys).to_numpy(), utc=True), index=copy_files.index[exact]
    )
    result.loc[exact, "origin_seq"] = copy_files.loc[exact, "version_seq"].astype("Int64")
    result.loc[exact, "origin_date"] = released
    result.loc[exact, "origin_basis"] = "exact"
    later = released > first_date[exact]
    result.loc[later.index[later.to_numpy()], "copied_before_release"] = True


def _pick_dated_origins(
    dated: pd.DataFrame, versions: pd.DataFrame, sigs: dict[str, object]
) -> pd.DataFrame:
    """Score each (copy text, lineage) once against every version, then pick per file (Q7.10).

    Returns ``origin_seq, origin_date`` indexed like ``dated`` (files with
    no version released by their date are absent).
    """
    pairs = dated.loc[:, ["origin_text", "lineage_id"]].drop_duplicates()
    scores = pairs.merge(
        versions.loc[:, ["lineage_id", "version_seq", "release_date", "version_id"]],
        on="lineage_id",
    )
    scores["jaccard"] = estimated_jaccard(
        scores["origin_text"].tolist(),
        scores["version_id"].tolist(),
        sigs["copy"],
        sigs["registry"],
    )
    files = dated.loc[:, ["lineage_id", "origin_text", "first_commit_date"]].copy()
    files["file_key"] = np.arange(len(files), dtype=np.int64)
    files["first_commit_date"] = pd.to_datetime(files["first_commit_date"], utc=True)
    con = connect_duckdb()
    try:
        con.execute("SET TimeZone = 'UTC'")
        con.register("dated", files)
        con.register("scores", scores.drop(columns=["version_id"]))
        picked = con.execute(ORIGIN_PICK_SQL).df()
    finally:
        con.close()
    picked.index = dated.index[picked["file_key"].to_numpy()]
    return picked.loc[:, ["origin_seq", "origin_date"]]


def estimated_jaccard(
    left_ids: list[str], right_ids: list[str], left: dict[str, object], right: dict[str, object]
) -> np.ndarray:
    """Share of equal MinHash values of each pair; ``nan`` when a side has no signature.

    This is ``text_helpers.estimate_jaccard`` on whole arrays: the share of
    the 128 positions where the two signatures agree.

    >>> book = signature_book(["a", "b"], np.array([[1] * 128, [1] * 64 + [2] * 64]), np.ones(2))
    >>> estimated_jaccard(["a", "a", "x"], ["a", "b", "b"], book, book).tolist()
    [1.0, 0.5, nan]
    """
    left_rows = np.asarray([left["row"].get(str(key), -1) for key in left_ids], dtype=np.int64)
    right_rows = np.asarray([right["row"].get(str(key), -1) for key in right_ids], dtype=np.int64)
    scores = np.full(len(left_rows), np.nan)
    valid = np.flatnonzero((left_rows >= 0) & (right_rows >= 0))
    for start in range(0, len(valid), ORIGIN_SCORE_BLOCK):
        block = valid[start : start + ORIGIN_SCORE_BLOCK]
        equal = left["signatures"][left_rows[block]] == right["signatures"][right_rows[block]]
        scores[block] = equal.sum(axis=1) / settings.MINHASH_NUM_PERM
    return scores


# Step 8: arrival groups and removed copies (Algorithm A34c)


# Algorithm A34c, see code_flow/13_copy_families.md
def assign_arrival_groups(histories: pd.DataFrame, copy_files: pd.DataFrame) -> pd.DataFrame:
    """When and with how many other copies each copy folder arrived, and whether it was removed.

    Per copy folder (every file of the folder gets its values):
    - ``arrival_commit`` / ``arrival_date``: the first commit (UTC date, then
      ``commit_seq``) with a non-delete row of the folder's own SKILL.md
      (former names included, so a rename keeps its first commit);
    - ``batch_size``: copy folders of the same repo that arrived in that commit;
    - ``arrival_group``: ``chosen`` up to ``ARRIVAL_CHOSEN_MAX``, ``bundle``
      from ``ARRIVAL_BUNDLE_MIN``, else ``small_set``; ``unknown`` without
      own SKILL.md history;
    - ``arrival_in_first_commit``: the copy arrived with the repo's first
      upload: at most ``ARRIVAL_REPO_START_DAYS`` after GitHub created the repo
      (or before, for an imported history). The stage 6 log holds only the
      copy folders, so the repo's own first commit is not known;
    - ``removed`` / ``removed_date``: the folder's last commit only deletes its SKILL.md.

    Example: commit c1 adds one copy folder and c2 adds 25: the c1 folder is
    ``chosen`` (batch 1), the c2 folders are ``bundle`` (batch 25).

    >>> rows = pd.DataFrame({"repo": ["a/r"] * 26, "path": [f"s{n}/SKILL.md" for n in range(26)],
    ...     "skill_folder": [f"s{n}" for n in range(26)], "commit": ["c1"] + ["c2"] * 25,
    ...     "commit_seq": [1] + [2] * 25, "status": ["A"] * 26, "blob": ["x"] * 26,
    ...     "commit_date": pd.to_datetime(["2026-01-01"] + ["2026-02-01"] * 25, utc=True),
    ...     "repo_created_at": pd.Timestamp("2026-01-01", tz="UTC")})
    >>> groups = assign_arrival_groups(rows, rows[["repo", "path"]])
    >>> first, later = groups.iloc[0], groups.iloc[1]
    >>> int(first["batch_size"]), first["arrival_group"], bool(first["arrival_in_first_commit"])
    (1, 'chosen', True)
    >>> int(later["batch_size"]), later["arrival_group"]
    (25, 'bundle')
    """
    folders = copy_files.loc[:, ["repo", "path"]].copy()
    folders["skill_folder"] = folders["path"].map(skill_folder_of)
    copy_folders = folders.loc[:, ["repo", "skill_folder"]].drop_duplicates()
    rows = histories.merge(copy_folders, on=["repo", "skill_folder"])
    rows = rows.drop_duplicates(
        ["repo", "skill_folder", "commit", "commit_seq", "status", "blob"]
    ).sort_values(
        ["repo", "skill_folder", "commit_date", "commit_seq"], kind="mergesort", na_position="last"
    )
    arrivals = (
        rows.loc[rows["status"] != "D"]
        .drop_duplicates(["repo", "skill_folder"])
        .loc[:, ["repo", "skill_folder", "commit", "commit_date", "repo_created_at"]]
        .rename(columns={"commit": "arrival_commit", "commit_date": "arrival_date"})
    )
    arrivals["batch_size"] = arrivals.groupby(["repo", "arrival_commit"])["skill_folder"].transform(
        "size"
    )
    arrivals["arrival_group"] = np.select(
        [
            arrivals["batch_size"] <= settings.ARRIVAL_CHOSEN_MAX,
            arrivals["batch_size"] >= settings.ARRIVAL_BUNDLE_MIN,
        ],
        ["chosen", "bundle"],
        "small_set",
    )
    start_window = pd.Timedelta(days=settings.ARRIVAL_REPO_START_DAYS)
    created = pd.to_datetime(arrivals["repo_created_at"], utc=True)
    arrivals["arrival_in_first_commit"] = arrivals["arrival_date"] <= created + start_window
    result = folders.merge(
        arrivals.drop(columns=["repo_created_at"]), on=["repo", "skill_folder"], how="left"
    )
    result = result.merge(_removed_folders(rows), on=["repo", "skill_folder"], how="left")
    result["arrival_group"] = result["arrival_group"].fillna("unknown")
    result["batch_size"] = result["batch_size"].astype("Int64")
    result["arrival_in_first_commit"] = result["arrival_in_first_commit"].fillna(False).astype(bool)
    result["removed"] = result["removed"].fillna(False).astype(bool)
    return result.drop(columns=["skill_folder"]).reset_index(drop=True)


def _removed_folders(rows: pd.DataFrame) -> pd.DataFrame:
    """``repo, skill_folder, removed, removed_date``: the last commit only deletes SKILL.md.

    ``rows`` are sorted by folder, date, ``commit_seq``.
    """
    if rows.empty:
        return pd.DataFrame(columns=["repo", "skill_folder", "removed", "removed_date"])
    last = rows.groupby(["repo", "skill_folder"], sort=False).tail(1)
    last = last.loc[:, ["repo", "skill_folder", "commit"]].rename(columns={"commit": "last_commit"})
    in_last = rows.merge(last, on=["repo", "skill_folder"])
    in_last = in_last.loc[in_last["commit"] == in_last["last_commit"]]
    summary = in_last.groupby(["repo", "skill_folder"], sort=False).agg(
        kept=("status", lambda statuses: bool((statuses != "D").any())),
        removed_date=("commit_date", "max"),
    )
    summary["removed"] = ~summary["kept"]
    summary.loc[~summary["removed"], "removed_date"] = pd.NaT
    return summary.reset_index().loc[:, ["repo", "skill_folder", "removed", "removed_date"]]


# Step 9: families (Algorithm A34) and the copier result (Algorithm A34b)


def best_link_per_text(copy_files: pd.DataFrame) -> pd.DataFrame:
    """``file_sha, root_lineage, best_link``: the strongest link among a text's files.

    Different files of one text can carry different links (their histories
    differ); the text takes the first in ``LINK_ORDER``, then the oldest
    origin, then ``root_lineage``.
    """
    linked = copy_files.loc[copy_files["link"].notna()]
    ranked = linked.assign(link_rank=linked["link"].map(LINK_ORDER.index))
    ordered = ranked.sort_values(["file_sha", "link_rank", "root_lineage"], kind="mergesort")
    best = ordered.drop_duplicates("file_sha").loc[:, ["file_sha", "root_lineage", "link"]]
    return best.rename(columns={"link": "best_link"}).reset_index(drop=True)


# Algorithm A34, see code_flow/13_copy_families.md
def build_text_families(
    con: duckdb.DuckDBPyConnection, copy_files: pd.DataFrame, dev: bool
) -> pd.DataFrame:
    """One family per English text, with family totals (queries Q7.6-Q7.9).

    Order of the rules:
    1. a text whose files link to a lineage: ``family_id = root_lineage``, ``source``;
    2. a text only inside registry repos (``registry_self``) that is a lineage
       version: that version's root lineage, ``source``;
    3. an unlinked text that a copier holds (``copier_seen`` of stage 5c, or a
       registry-only text inside a copier): ``copier:<file_sha>``,
       ``copier_seen``. Checked before clusters: a crawled text is not
       independent work;
    4. an unclaimed text in an ``s2_clusters`` cluster (0.80) with at least
       one other unclaimed English text: ``cluster:<id>``, ``independent``;
    5. else ``single:<file_sha>``, ``singleton``.

    Inputs: a connection from ``read_inputs`` with ``copier_texts``,
    ``version_roots`` and ``root_owners``; the copy files; ``dev`` (unused
    by the SQL, kept for the card's signature). Outputs: ``FAMILY_COLUMNS``.
    """
    del dev
    _create_table(con, "linked", best_link_per_text(copy_files))
    con.execute(f"CREATE OR REPLACE TEMP TABLE english_files AS {ENGLISH_FILES_SQL}")
    con.execute(f"CREATE OR REPLACE TEMP TABLE registry_texts AS {REGISTRY_TEXTS_SQL}")
    sql = ASSIGN_FAMILIES_SQL.format(threshold=float(settings.CLUSTER_LINK_THRESHOLD))
    con.execute(f"CREATE OR REPLACE TEMP TABLE text_families AS {sql}")
    counts = con.execute(FAMILY_COUNTS_SQL).df()
    families = con.execute("SELECT * FROM text_families").df()
    families = families.merge(counts, on="family_id", how="left")
    for column in ("files", "repos", "owners", "files_without_forks", "repos_without_forks"):
        families[column] = families[column].fillna(0).astype("int64")
    families["registry_self"] = families["registry_self"].astype(bool)
    return families.loc[:, list(FAMILY_COLUMNS)]


def register_family_inputs(con: duckdb.DuckDBPyConnection, tables: dict[str, object]) -> None:
    """Tables the family queries read: ``copier_texts``, ``version_roots``, ``root_owners``."""
    _create_table(con, "copier_texts", tables["copier_seen"].loc[:, ["file_sha"]])
    roots = tables["roots"].loc[:, ["lineage_id", "root_lineage"]].drop_duplicates("lineage_id")
    version_roots = tables["versions"].merge(roots, on="lineage_id", how="left")
    version_roots["root_lineage"] = version_roots["root_lineage"].fillna(
        version_roots["lineage_id"]
    )
    _create_table(
        con,
        "version_roots",
        version_roots.loc[:, ["blob", "lineage_id", "version_seq", "release_date", "root_lineage"]],
    )
    owners = tables["lineages"].loc[:, ["lineage_id", "repo"]].drop_duplicates("lineage_id")
    owners = owners.rename(columns={"lineage_id": "root_lineage"})
    owners["owner"] = _owner(owners["repo"])
    _create_table(con, "root_owners", owners.loc[:, ["root_lineage", "owner"]])


# Algorithm A34b, see code_flow/13_copy_families.md
def compare_with_copier(files: pd.DataFrame) -> pd.DataFrame:
    """Who had a ``copier_seen`` text first: this repo or the copier (Pass 2 result).

    Dates compare as UTC days: ``repo_first`` when the file's first commit is
    on or before the copier's first date (the copier crawled it; the same day
    counts), ``copier_first`` when the copier had it strictly earlier,
    ``no_date`` when either date is missing. No date is guessed.

    >>> files = pd.DataFrame({
    ...     "first_commit_date": pd.to_datetime(["2026-01-01T00:00", "2026-03-01T00:00", None,
    ...         "2026-02-01T20:00"], utc=True),
    ...     "copier_first_date": pd.to_datetime(["2026-02-01T00:00"] * 3 + ["2026-02-01T08:00"],
    ...         utc=True)})
    >>> compare_with_copier(files)["copier_result"].tolist()
    ['repo_first', 'copier_first', 'no_date', 'repo_first']
    """
    result = files.copy()
    repo_day = pd.to_datetime(result["first_commit_date"], utc=True).dt.floor("D")
    copier_day = pd.to_datetime(result["copier_first_date"], utc=True).dt.floor("D")
    result["copier_result"] = np.select(
        [repo_day.isna() | copier_day.isna(), repo_day <= copier_day],
        ["no_date", "repo_first"],
        "copier_first",
    )
    return result


def build_copier_seen_files(
    con: duckdb.DuckDBPyConnection, copier_seen: pd.DataFrame
) -> pd.DataFrame:
    """Query Q7.5 and A34b: one row per file outside the registries of a ``copier_seen`` text.

    These files were not downloaded in stage 6 (only linked copies were), so
    their first commit date is the dataset ``first_commit_at``.
    """
    files = con.execute(COPIER_SEEN_FILES_SQL).df()
    files["first_commit_date"] = _utc(files["first_commit_date"])
    seen = copier_seen.loc[
        :, ["file_sha", "copier_first_date", "copier_first_repo", "weak_copiers_only"]
    ]
    files = compare_with_copier(files.merge(seen, on="file_sha", how="inner"))
    files["weak_copiers_only"] = files["weak_copiers_only"].fillna(False).astype(bool)
    return files.loc[:, list(COPIER_SEEN_FILE_COLUMNS)]


def count_history_links(copy_files: pd.DataFrame) -> pd.DataFrame:
    """``link, history_source, files``: copy files per winning link and history download.

    >>> files = pd.DataFrame({"link": ["near", "history_exact", "near"],
    ...     "history_source": ["none", "copied", "copied"]})
    >>> count_history_links(files).values.tolist()
    [['history_exact', 'copied', 1], ['near', 'copied', 1], ['near', 'none', 1]]
    """
    counts = copy_files.groupby(["link", "history_source"]).size().rename("files").reset_index()
    counts["link_rank"] = counts["link"].map(LINK_ORDER.index)
    counts = counts.sort_values(["link_rank", "history_source"], kind="mergesort")
    return counts.drop(columns=["link_rank"]).reset_index(drop=True)


# Step 10: history-proven test set and recall per text method (Algorithm A34d)


def newest_history_blobs(histories: pd.DataFrame) -> pd.DataFrame:
    """The newest text of each copy file's history: ``repo, path, blob``.

    Deletes are skipped. Order: a real date beats a missing one, then the
    later date, then the higher ``commit_seq``, then the higher ``version_seq``.

    >>> rows = pd.DataFrame({"repo": ["a/r"] * 3, "path": ["SKILL.md"] * 3,
    ...     "blob": ["x", "y", None], "status": ["A", "M", "D"], "commit_seq": [1, 2, 3],
    ...     "version_seq": [1, 2, None],
    ...     "commit_date": pd.to_datetime(["2026-01-01", "2026-02-01", "2026-03-01"], utc=True)})
    >>> newest_history_blobs(rows)["blob"].tolist()
    ['y']
    """
    rows = histories.loc[histories["blob"].notna() & (histories["status"] != "D")]
    ordered = rows.sort_values(
        ["repo", "path", "commit_date", "commit_seq", "version_seq"],
        kind="mergesort",
        na_position="first",
    )
    newest = ordered.drop_duplicates(["repo", "path"], keep="last")
    return newest.loc[:, ["repo", "path", "blob"]].reset_index(drop=True)


def build_proven_pairs(copy_files: pd.DataFrame, histories: pd.DataFrame) -> pd.DataFrame:
    """The history-proven test set: copy files whose winning link is ``history_exact``.

    Each file is paired with its linked lineage. Its current text is the
    newest blob of its own history, else its dataset text (``file_sha``).
    ``current_is_dataset_text`` is true when the two are the same blob, so the
    text stage 5c linked has not changed since the crawl. A ``copier_seen``
    file is never here: it has no lineage link.

    Outputs: ``repo, path, lineage_id, current_file_sha,
    current_is_dataset_text, is_fork``.

    >>> files = pd.DataFrame({"repo": ["a/r", "a/r"], "path": ["x/SKILL.md", "y/SKILL.md"],
    ...     "file_sha": ["crawled", "other"], "lineage_id": ["L", "L"],
    ...     "link": ["history_exact", "near"], "is_fork": [False, False]})
    >>> rows = pd.DataFrame({"repo": ["a/r"], "path": ["x/SKILL.md"], "blob": ["newer"],
    ...     "status": ["M"], "commit_seq": [1], "version_seq": [1],
    ...     "commit_date": pd.to_datetime(["2026-01-01"], utc=True)})
    >>> build_proven_pairs(files, rows)[["path", "current_file_sha"]].values.tolist()
    [['x/SKILL.md', 'newer']]
    """
    proven = copy_files.loc[
        copy_files["link"] == "history_exact", ["repo", "path", "file_sha", "lineage_id", "is_fork"]
    ]
    pairs = proven.merge(newest_history_blobs(histories), on=["repo", "path"], how="left")
    pairs["current_file_sha"] = pairs["blob"].fillna(pairs["file_sha"])
    pairs["current_is_dataset_text"] = pairs["current_file_sha"] == pairs["file_sha"]
    pairs["is_fork"] = pairs["is_fork"].fillna(False).astype(bool)
    columns = ["repo", "path", "lineage_id", "current_file_sha", "current_is_dataset_text"]
    return pairs.loc[:, [*columns, "is_fork"]].reset_index(drop=True)


def passes_text_methods(
    same_text: object, jaccard: object, containment: object, line_share: object
) -> dict[str, object]:
    """Whether one registry version passes each text method's cutoff (scalars or arrays).

    exact = same normalised text; minhash_jaccard = estimated Jaccard >=
    ``NEAR_COPY_MIN_JACCARD``; containment = estimated containment >=
    ``NEAR_COPY_MIN_CONTAINMENT``; line_check = share of registry lines kept
    >= ``MIN_VERIFIED_LINE_SHARE``. A missing score (NaN) passes nothing.

    >>> passes_text_methods(False, 0.5, 0.9, 0.5)
    {'exact': False, 'minhash_jaccard': False, 'containment': True, 'line_check': True}
    """
    return {
        "exact": same_text,
        "minhash_jaccard": jaccard >= settings.NEAR_COPY_MIN_JACCARD,
        "containment": containment >= settings.NEAR_COPY_MIN_CONTAINMENT,
        "line_check": line_share >= settings.MIN_VERIFIED_LINE_SHARE,
    }


def _signature_jaccard(
    left: np.ndarray, left_size: int, right: np.ndarray, right_size: int
) -> float:
    """Share of the 128 MinHash values that are equal; 0 when a text has no shingles."""
    if left_size <= 0 or right_size <= 0:
        return 0.0
    return float((np.asarray(left) == np.asarray(right)).sum() / settings.MINHASH_NUM_PERM)


# Algorithm A34d, see code_flow/13_copy_families.md
def score_methods_on_pair(
    registry_texts: list[str], copy_text: str, boilerplate: set[str] | frozenset[str] = frozenset()
) -> dict[str, bool]:
    """Which text methods find a proven copy: a method finds it when any lineage version passes.

    For each registry version: exact = same ``normalized_sha`` (the stage 4a
    version key); Jaccard and containment from the stage 5a signatures
    (boilerplate removed, 5-word shingles, 128 values); line check = share of
    registry body lines the copy still has. ``any_text_method`` is the or of
    the four. This is the plain version; ``score_proven_pairs`` is the fast
    one, tested to agree.

    Example: 40 registry lines inside a copy of 100 lines (60% new) are found
    by containment and the line check, not by exact or Jaccard (see tests).

    >>> body = "\\n".join(f"line {n} says something about step {n} here" for n in range(12))
    >>> score_methods_on_pair([body], body)["minhash_jaccard"]
    True
    >>> score_methods_on_pair(["a b c d e f"], "z y x w v u")["any_text_method"]
    False
    """
    copy_key = normalized_sha(copy_text)
    copy_signature, copy_size = signature_for_text(copy_text, set(boilerplate))
    found = dict.fromkeys(METHOD_COLUMNS, False)
    for registry_text in registry_texts:
        signature, size = signature_for_text(registry_text, set(boilerplate))
        jaccard = _signature_jaccard(signature, size, copy_signature, copy_size)
        containment = containment_estimates(
            np.array([jaccard]), np.array([size]), np.array([copy_size])
        )[0]
        passed = passes_text_methods(
            normalized_sha(registry_text) == copy_key,
            jaccard,
            containment,
            share_of_registry_lines_kept(registry_text, copy_text),
        )
        found = {method: bool(found[method] or passed[method]) for method in found}
    return {**found, "any_text_method": any(found.values())}


def score_proven_pairs(
    pairs: pd.DataFrame,
    versions: pd.DataFrame,
    signatures: dict[str, dict[str, object]],
    texts: dict[str, dict[str, str]],
    pool: object | None = None,
) -> pd.DataFrame:
    """``score_methods_on_pair`` for many pairs at once, with stored signatures.

    Every pair is scored against every version of its lineage. Jaccard and
    containment come from the signature books (``signatures["copy"]`` by
    sha, ``signatures["registry"]`` by ``version_id``); a missing signature
    scores 0. A missing text gives a null line share and no exact match.

    Inputs: pairs (``build_proven_pairs``); versions (``lineage_id,
    version_id, blob, normalized_sha``); ``texts = {"copies": sha -> text,
    "versions": blob -> text}``. Outputs: the pairs with ``exact,
    jaccard_found, containment_found, verified_found, best_jaccard,
    best_containment, best_line_share``.
    """
    rows = pairs.loc[:, ["repo", "path", "lineage_id", "current_file_sha"]].merge(
        versions.loc[:, ["lineage_id", "version_id", "blob", "normalized_sha"]], on="lineage_id"
    )
    copy_keys = {sha: normalized_sha(text) for sha, text in texts["copies"].items()}
    same_text = (rows["current_file_sha"].map(copy_keys) == rows["normalized_sha"]).to_numpy()
    jaccard = _book_jaccard(signatures, rows)
    copy_sizes = _book_sizes(signatures["copy"], rows["current_file_sha"])
    registry_sizes = _book_sizes(signatures["registry"], rows["version_id"])
    containment = containment_estimates(jaccard, registry_sizes, copy_sizes)
    line_share = _pair_line_shares(rows, texts, pool)
    passed = passes_text_methods(same_text, jaccard, containment, line_share)
    for method, column in METHOD_COLUMNS.items():
        rows[column] = np.asarray(passed[method], dtype=bool)
    rows["best_jaccard"], rows["best_containment"] = jaccard, containment
    rows["best_line_share"] = line_share
    found = rows.groupby(["repo", "path"], sort=False).agg(
        {**dict.fromkeys(METHOD_COLUMNS.values(), "any"), **_BEST_SCORES}
    )
    scored = pairs.merge(found.reset_index(), on=["repo", "path"], how="left")
    for column in METHOD_COLUMNS.values():
        scored[column] = scored[column].fillna(False).astype(bool)
    return scored


def _book_jaccard(signatures: dict[str, dict[str, object]], rows: pd.DataFrame) -> np.ndarray:
    """Estimated Jaccard of each (copy sha, version id) row; 0 when a signature is missing."""
    copy_rows = _book_rows(signatures["copy"], rows["current_file_sha"])
    registry_rows = _book_rows(signatures["registry"], rows["version_id"])
    jaccard = np.zeros(len(rows), dtype=np.float64)
    both = np.flatnonzero((copy_rows >= 0) & (registry_rows >= 0))
    for start in range(0, len(both), ORIGIN_SCORE_BLOCK):
        block = both[start : start + ORIGIN_SCORE_BLOCK]
        agree = (
            signatures["copy"]["signatures"][copy_rows[block]]
            == signatures["registry"]["signatures"][registry_rows[block]]
        ).sum(axis=1)
        jaccard[block] = agree / settings.MINHASH_NUM_PERM
    return jaccard


def _book_rows(book: dict[str, object], keys: pd.Series) -> np.ndarray:
    """Row of each key in a signature book, -1 when it has none."""
    return keys.astype(str).map(book["row"]).fillna(-1).to_numpy(dtype=np.int64)


def _book_sizes(book: dict[str, object], keys: pd.Series) -> np.ndarray:
    """Shingle count of each key's signature, 0 when it has none."""
    rows = _book_rows(book, keys)
    sizes = np.zeros(len(rows), dtype=np.int64)
    sizes[rows >= 0] = np.asarray(book["sizes"])[rows[rows >= 0]]
    return sizes


def _pair_line_shares(
    rows: pd.DataFrame, texts: dict[str, dict[str, str]], pool: object | None
) -> np.ndarray:
    """``share_of_registry_lines_kept`` per (version, copy) row; NaN when a text is missing."""
    shares = np.full(len(rows), np.nan)
    registry = rows["blob"].map(texts["versions"])
    copy = rows["current_file_sha"].map(texts["copies"])
    present = np.flatnonzero((registry.notna() & copy.notna()).to_numpy())
    pairs = [(registry.iat[row], copy.iat[row]) for row in present]
    shares[present] = _line_shares(pairs, pool)
    return shares


def score_proven_pairs_in_batches(
    con: duckdb.DuckDBPyConnection,
    pairs: pd.DataFrame,
    versions: pd.DataFrame,
    signatures: dict[str, dict[str, object]],
    history_texts: Path,
) -> pd.DataFrame:
    """``score_proven_pairs`` ``PROVEN_SCORE_BATCH`` pairs at a time, reading their texts.

    Registry texts come from the stage 3 texts table, copy texts from the
    stage 6 texts table; a current text that is only a dataset text is read
    once from the artifacts (query Q7.11). Pairs are sorted by lineage, so a
    batch reads few registry texts.
    """
    ordered = pairs.sort_values(["lineage_id", "repo", "path"], kind="mergesort")
    dataset_texts = _proven_dataset_texts(con, ordered, history_texts)
    pool = _open_line_pool() if len(ordered) >= PROVEN_SCORE_BATCH else None
    parts = [score_proven_pairs(ordered.iloc[:0], versions, signatures, _no_texts())]
    try:
        for start in range(0, len(ordered), PROVEN_SCORE_BATCH):
            batch = ordered.iloc[start : start + PROVEN_SCORE_BATCH]
            batch_versions = versions.loc[versions["lineage_id"].isin(batch["lineage_id"])]
            copy_shas = sorted(set(batch["current_file_sha"]))
            texts = {
                "copies": {**dataset_texts, **read_texts(history_texts, copy_shas)},
                "versions": read_texts(_texts_path(), sorted(set(batch_versions["blob"].dropna()))),
            }
            parts.append(score_proven_pairs(batch, batch_versions, signatures, texts, pool))
            print(f"proven pairs: {start + len(batch)}/{len(ordered)} scored", flush=True)
    finally:
        if pool is not None:
            pool.close()
            pool.join()
    return pd.concat(parts, ignore_index=True)


def _no_texts() -> dict[str, dict[str, str]]:
    """An empty text lookup."""
    return {"copies": {}, "versions": {}}


def _proven_dataset_texts(
    con: duckdb.DuckDBPyConnection, pairs: pd.DataFrame, history_texts: Path
) -> dict[str, str]:
    """Query Q7.11: dataset text of current texts that are not stage 6 blobs (usually none)."""
    from skill_genealogy.find_copies import _as_text

    shas = pd.DataFrame({"sha": sorted(set(pairs["current_file_sha"]))})
    con.register("proven_shas", shas)
    try:
        stored = con.execute(
            f"SELECT DISTINCT sha FROM read_parquet({_quote_path(str(history_texts))}) "
            "SEMI JOIN proven_shas USING (sha)"
        ).df()
    finally:
        con.unregister("proven_shas")
    wanted = shas.loc[~shas["sha"].isin(stored["sha"])]
    if wanted.empty:
        return {}
    con.register("wanted_texts", wanted)
    try:
        rows = con.execute(PROVEN_DATASET_TEXTS_SQL).fetchall()
    finally:
        con.unregister("wanted_texts")
    return {str(sha): _as_text(text) for sha, text in rows}


def measure_link_method_recall(pairs: pd.DataFrame) -> pd.DataFrame:
    """Recall of each text method on the proven pairs: ``found / proven_pairs``.

    Rows: exact, minhash_jaccard, containment, line_check, any_text_method
    (the or of the four), and ``tfidf`` only when a ``tfidf_found`` column
    exists (G30). ``recall_without_forks`` leaves out fork files (a fork
    shares the registry's history). ``recall_changed_since_crawl`` counts only
    pairs whose current text is not the dataset text stage 5c linked.

    >>> pairs = pd.DataFrame({"exact": [True, False, False],
    ...     "jaccard_found": [True, False, False], "containment_found": [True, True, False],
    ...     "verified_found": [True, True, False], "is_fork": [False] * 3,
    ...     "current_is_dataset_text": [True, False, False]})
    >>> measure_link_method_recall(pairs)[["method", "found", "recall"]].values.tolist()[-1]
    ['any_text_method', 2, 0.6667]
    """
    flags = pairs.loc[:, list(METHOD_COLUMNS.values())].rename(
        columns={column: method for method, column in METHOD_COLUMNS.items()}
    )
    flags["any_text_method"] = flags.any(axis=1)
    if "tfidf_found" in pairs.columns:
        flags["tfidf"] = pairs["tfidf_found"].fillna(False).astype(bool)
    not_fork = ~pairs["is_fork"].fillna(False).astype(bool)
    changed = ~pairs["current_is_dataset_text"].fillna(False).astype(bool)
    rows = []
    for method in flags.columns:
        found = flags[method].astype(bool)
        rows.append(
            {
                "method": method,
                "proven_pairs": len(found),
                "found": int(found.sum()),
                "recall": _share(int(found.sum()), len(found)),
                "recall_without_forks": _share(int(found[not_fork].sum()), int(not_fork.sum())),
                "changed_pairs": int(changed.sum()),
                "recall_changed_since_crawl": _share(int(found[changed].sum()), int(changed.sum())),
            }
        )
    return pd.DataFrame(rows, columns=list(RECALL_COLUMNS))


def build_link_method_recall(
    con: duckdb.DuckDBPyConnection,
    copy_files: pd.DataFrame,
    histories: pd.DataFrame,
    tables: dict[str, object],
    signatures: dict[str, dict[str, object]],
) -> dict[str, pd.DataFrame]:
    """Stage 7b: the proven pairs, scored, and the recall table."""
    pairs = build_proven_pairs(copy_files, histories)
    stage5 = (
        tables["copies"].loc[:, ["repo", "path", "link"]].rename(columns={"link": "stage5_link"})
    )
    pairs = pairs.merge(stage5, on=["repo", "path"], how="left")
    keys = read_parquet(tables["paths"]["lineage_versions"]).loc[
        :, ["lineage_id", "version_seq", "normalized_sha"]
    ]
    versions = tables["versions"].merge(keys, on=["lineage_id", "version_seq"], how="left")
    versions = versions.loc[versions["lineage_id"].isin(pairs["lineage_id"])]
    history_texts = tables["paths"]["history_texts"]
    scored = score_proven_pairs_in_batches(con, pairs, versions, signatures, history_texts)
    return {
        "proven_pairs": scored.loc[:, list(PROVEN_PAIR_COLUMNS)],
        "link_method_recall": measure_link_method_recall(scored),
    }


# Step 11: save and print the summary


def save_outputs(outputs: dict[str, pd.DataFrame], dev: bool) -> None:
    """Write the six stage 7 tables, sorted so a rerun gives the same files."""
    paths = output_paths(dev)
    write_parquet(
        outputs["copy_files"].loc[:, list(COPY_FILE_COLUMNS)], paths["copy_files"], ["repo", "path"]
    )
    write_parquet(outputs["families"].loc[:, list(FAMILY_COLUMNS)], paths["families"], ["file_sha"])
    write_parquet(
        outputs["copier_seen_files"].loc[:, list(COPIER_SEEN_FILE_COLUMNS)],
        paths["copier_seen_files"],
        ["repo", "path"],
    )
    write_parquet(outputs["history_link_counts"], paths["history_link_counts"])
    write_parquet(
        outputs["proven_pairs"].loc[:, list(PROVEN_PAIR_COLUMNS)],
        paths["proven_pairs"],
        ["repo", "path"],
    )
    write_parquet(outputs["link_method_recall"], paths["link_method_recall"])


def print_recall(recall: pd.DataFrame, pairs: pd.DataFrame) -> None:
    """Print the recall table and two proven pairs that no text method finds."""
    print("stage 7b link method recall (history-proven copies)")
    print(recall.to_string(index=False))
    missed = pairs.loc[~pairs.loc[:, list(METHOD_COLUMNS.values())].any(axis=1)]
    print(f"proven pairs no text method finds: {len(missed)}")
    print_sample_records(missed.loc[:, list(PROVEN_PAIR_COLUMNS)], "missed proven pair sample")


def _share(part: int, whole: int) -> float:
    """``part / whole`` rounded to 4 places, 0 when ``whole`` is 0."""
    return round(part / whole, 4) if whole else 0.0


def _arrival_numbers(copy_files: pd.DataFrame) -> dict[str, object]:
    """Folders, files, repos and first-commit arrivals per arrival group, and removals."""
    folders = copy_files.assign(skill_folder=copy_files["path"].map(skill_folder_of))
    folders = folders.drop_duplicates(["repo", "skill_folder"])
    numbers: dict[str, object] = {}
    for group in ARRIVAL_GROUPS:
        in_group = copy_files["arrival_group"] == group
        group_folders = folders.loc[folders["arrival_group"] == group]
        numbers[f"arrival_{group}_folders"] = len(group_folders)
        numbers[f"arrival_{group}_files"] = int(in_group.sum())
        numbers[f"arrival_{group}_repos"] = int(copy_files.loc[in_group, "repo"].nunique())
        numbers[f"arrival_{group}_in_first_commit"] = int(
            group_folders["arrival_in_first_commit"].sum()
        )
    with_history = folders.loc[folders["arrival_group"] != "unknown"]
    numbers["removed_folders"] = int(with_history["removed"].sum())
    numbers["removed_share_of_folders_with_history"] = _share(
        int(with_history["removed"].sum()), len(with_history)
    )
    for label, repos in (("stored", HAND_STORED_REPOS), ("used", HAND_USED_REPOS)):
        picked = folders.loc[folders["repo"].isin(repos) & (folders["arrival_group"] != "unknown")]
        numbers[f"hand_{label}_folders_with_history"] = len(picked)
        numbers[f"hand_{label}_repos_bundle_share"] = _share(
            int((picked["arrival_group"] == "bundle").sum()), len(picked)
        )
    return numbers


def print_run_summary(outputs: dict[str, pd.DataFrame], elapsed: float) -> None:
    """Print the rule 8 counts and two sample records of each output."""
    copy_files = outputs["copy_files"]
    families = outputs["families"]
    seen_files = outputs["copier_seen_files"]
    fuzzy = copy_files["lineage_id"].notna() & ~copy_files["link"].isin(EXACT_LINKS)
    no_date = int((copy_files["origin_basis"] == "no_date").sum())
    numbers: dict[str, object] = {
        "copy_files": len(copy_files),
        "copy_repos": int(copy_files["repo"].nunique()),
        **{f"files_{link}": int((copy_files["link"] == link).sum()) for link in LINK_ORDER},
        **{
            f"files_history_{source}": int((copy_files["history_source"] == source).sum())
            for source in HISTORY_SOURCES
        },
        "files_inherited_history_links": int(outputs.get("inherited_links", 0)),
        "copied_before_release": int(copy_files["copied_before_release"].sum()),
        "origin_no_date_files": no_date,
        "origin_no_date_share_of_non_exact": _share(no_date, int(fuzzy.sum())),
        "fork_share": _share(int(copy_files["is_fork"].sum()), len(copy_files)),
        **_arrival_numbers(copy_files),
        "english_texts": len(families),
        **{f"texts_{kind}": int((families["family_kind"] == kind).sum()) for kind in FAMILY_KINDS},
        **{
            f"families_{kind}": int(
                families.loc[families["family_kind"] == kind, "family_id"].nunique()
            )
            for kind in FAMILY_KINDS
        },
        "texts_registry_self": int(families["registry_self"].sum()),
        "copier_seen_files": len(seen_files),
        **{
            f"copier_seen_{result}": int((seen_files["copier_result"] == result).sum())
            for result in COPIER_RESULTS
        },
        "copier_seen_files_weak_only": int(seen_files["weak_copiers_only"].sum()),
        "seconds": round(elapsed, 1),
    }
    print_summary("stage 7a assign families", numbers)
    print_sample_records(copy_files.loc[:, list(COPY_FILE_COLUMNS)], "copy file sample")
    print_sample_records(families.loc[families["family_kind"] == "source"], "family sample")
    print_sample_records(seen_files, "copier seen file sample")
    print_sample_records(outputs["history_link_counts"], "history link counts")
    print_recall(outputs["link_method_recall"], outputs["proven_pairs"])


def build_families(dev: bool) -> dict[str, pd.DataFrame]:
    """Stage 7 steps 1-10; returns the six output tables. Nothing is written."""
    work_dir = Path(tempfile.mkdtemp(prefix="genealogy_families_"))
    con = connect_duckdb()
    try:
        _prepare_duckdb(con, work_dir / "duckdb")
        # Step 1: read the inputs
        print("Step 1: read inputs", flush=True)
        tables = read_inputs(con, dev)
        versions: pd.DataFrame = tables["versions"]
        copies: pd.DataFrame = tables["copies"]
        # Step 2: copy histories (Q7.1-Q7.3)
        histories = read_copy_histories(dev, copies)
        history_files = len(histories.loc[:, ["repo", "path"]].drop_duplicates())
        print(f"Step 2: {len(histories)} history rows for {history_files} files", flush=True)
        # Step 3: history_exact links (A30)
        history_exact = find_history_exact_links(histories, versions)
        print(f"Step 3: history_exact links {len(history_exact)}", flush=True)
        # Step 4: signatures of first versions and current texts
        text_links = text_link_rows(copies, versions)
        strong = pd.concat([text_links, history_exact], ignore_index=True)
        strong = strong.loc[strong["link"].map(LINK_ORDER.index) < LINK_ORDER.index("history_near")]
        with_history = histories.loc[:, ["repo", "path"]].drop_duplicates()
        open_files = with_history.merge(
            strong.loc[:, ["repo", "path"]].drop_duplicates(), how="left", indicator=True
        )
        open_files = open_files.loc[open_files["_merge"] == "left_only", ["repo", "path"]]
        first = first_history_versions(histories)
        newest = newest_history_blobs(histories.merge(history_exact.loc[:, ["repo", "path"]]))
        wanted = set(first["blob"]) | set(copies.loc[copies["link"] != "exact", "file_sha"])
        wanted |= set(newest["blob"])
        boilerplate = build_boilerplate_set(
            read_parquet(boilerplate_path()), settings.BOILERPLATE_MIN_OWNERS
        )
        text_sigs = collect_text_signatures(wanted, tables["paths"]["history_texts"], boilerplate)
        print(f"Step 4: signatures of {len(text_sigs['ids'])} copy texts", flush=True)
        # Step 5: history_near links (A31)
        reg_ids, reg_signatures, reg_sizes = load_signatures(registry_signatures_path(dev))
        registry_sigs = registry_signature_table(versions, reg_ids, reg_signatures, reg_sizes)
        registry_sigs["index"] = build_copy_band_index(registry_sigs["signatures"])
        history_near = find_history_near_links(histories, registry_sigs, open_files, text_sigs)
        print(
            f"Step 5: {len(open_files)} files scored, history_near links {len(history_near)}",
            flush=True,
        )
        # Step 6: strongest link per file (A32), root, flags, first commit date
        winners = pick_strongest_link(
            pd.concat([text_links, history_exact, history_near], ignore_index=True)
        )
        dates = read_dataset_dates(con, copies)
        copy_files = attach_file_facts(winners, copies, histories, tables, dates)
        print("Step 6: strongest link per file", flush=True)
        # Step 7: origin version by date (A33)
        registry_book = signature_book(
            registry_sigs["versions"]["lineage_id"]
            .astype(str)
            .str.cat(registry_sigs["versions"]["version_seq"].astype(str), sep="#")
            .tolist(),
            registry_sigs["signatures"],
            registry_sigs["sizes"],
        )
        del registry_sigs
        origin = choose_origin_version(
            copy_files, versions, {"copy": text_sigs, "registry": registry_book}
        )
        copy_files = copy_files.merge(origin, on=["repo", "path"], how="left")
        print("Step 7: origin versions", flush=True)
        # Step 8: arrival groups and removed copies (A34c)
        arrivals = assign_arrival_groups(histories, copies)
        copy_files = copy_files.merge(arrivals, on=["repo", "path"], how="left")
        print("Step 8: arrival groups", flush=True)
        # Step 9: families (A34) and copier results (A34b)
        register_family_inputs(con, tables)
        families = build_text_families(con, copy_files, dev)
        seen_files = build_copier_seen_files(con, tables["copier_seen"])
        print("Step 9: families and copier results", flush=True)
        inherited_links = _inherited_history_links(copy_files, histories, versions)
        # Step 10: history-proven test set and recall per text method (A34d, Q7.11)
        recall = build_link_method_recall(
            con, copy_files, histories, tables, {"copy": text_sigs, "registry": registry_book}
        )
        print(f"Step 10: {len(recall['proven_pairs'])} proven pairs scored", flush=True)
        return {
            "copy_files": _same_date_unit(copy_files.loc[:, list(COPY_FILE_COLUMNS)]),
            "families": families,
            "copier_seen_files": seen_files,
            "history_link_counts": count_history_links(copy_files),
            "inherited_links": inherited_links,
            **recall,
        }
    finally:
        con.close()
        shutil.rmtree(work_dir, ignore_errors=True)


def _same_date_unit(frame: pd.DataFrame) -> pd.DataFrame:
    """Store every date column as UTC microseconds, so DuckDB compares them without a cast.

    Release dates arrive in nanoseconds, commit dates in microseconds.
    """
    result = frame.copy()
    for column in ("first_commit_date", "origin_date", "arrival_date", "removed_date"):
        result[column] = pd.to_datetime(result[column], utc=True).astype("datetime64[us, UTC]")
    return result


def _inherited_history_links(
    copy_files: pd.DataFrame, histories: pd.DataFrame, versions: pd.DataFrame
) -> int:
    """Winning history_exact links whose matching blob appears only in inherited commits."""
    won = copy_files.loc[copy_files["link"] == "history_exact", ["repo", "path"]]
    rows = histories.merge(won, on=["repo", "path"]).merge(
        versions.loc[:, ["blob"]].drop_duplicates(), on="blob"
    )
    if rows.empty:
        return 0
    only = rows.groupby(["repo", "path"])["inherited"].all()
    return int(only.sum())


def run(dev: bool, force: bool) -> None:
    """Stage 7: skip when the six outputs exist (unless ``force``), else build, save, print."""
    outputs = list(output_paths(dev).values())
    if force:
        print("force: deleting the six stage 7 outputs", flush=True)
        for path in outputs:
            path.unlink(missing_ok=True)
    if outputs_exist(outputs):
        print("skip: stage 7 outputs exist (use --force to rebuild)", flush=True)
        return
    started = time.perf_counter()
    # Steps 1-10: links, origins, arrivals, families, recall per text method
    result = build_families(dev)
    # Step 11: save and print the summary
    save_outputs(result, dev)
    print_run_summary(result, time.perf_counter() - started)


def main() -> None:
    """Run stage 7a from the command line (``--dev``, ``--force``)."""
    args = build_arg_parser(
        "Stage 7: history links, strongest link, origin version, arrival groups, families, "
        "recall per text method."
    ).parse_args()
    run(bool(args.dev), bool(args.force))


if __name__ == "__main__":
    main()
