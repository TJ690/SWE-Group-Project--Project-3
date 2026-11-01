"""Give every English skill text a family, and every copy an origin version.

What: combine the stage 5 text links with wild history and copied-skill
history. Each copy file keeps its strongest link. The origin version is the
registry version that link points at, limited by the copy's first commit
date. Each English text is then placed in a family.
Inputs: copy tables, registry lineages, both history stores, the language
table, and ``s2_clusters.parquet``. See ``read_inputs``.
Outputs: ``copy_files.parquet``, ``families.parquet``,
``history_link_counts.parquet``, ``proven_pairs.parquet``, and
``link_method_recall.parquet`` in the dev or full data folder.
Run:
    python -m skill_genealogy.assign_families --dev
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from skill_genealogy import settings
from skill_genealogy.compare_texts import share_of_registry_lines_kept
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
from skill_genealogy.find_copies import load_signatures, signature_for_text
from skill_genealogy.text_helpers import (
    estimate_containment,
    estimate_jaccard,
    minhash_from_array,
    normalize_text,
)

# Lower rank is stronger. A29 is the stage 6 download-target card, so these
# start at A30. exact beats history_exact. history_near beats cluster.
LINK_RANK = {
    "exact": 0,
    "history_exact": 1,
    "near": 2,
    "near_contained": 3,
    "history_near": 4,
    "cluster": 5,
}
_EXACT_LINKS = frozenset({"exact", "history_exact"})
_STRONGER_THAN_HISTORY_NEAR = frozenset({"exact", "near", "near_contained"})
_HISTORY_LINKS = frozenset({"history_exact", "history_near"})
_ZERO_BLOB = "0" * 40
_BOILERPLATE_CACHE: set[str] | None = None
# A literal IN list is what DuckDB pushes into the parquet scan. A join
# does not, and would read every skill text. 200 shas stay under the
# query-size limit while still filtering the file.
_TEXT_SHA_BATCH = 200
_TEXT_METHOD_FLAGS = (
    ("exact", "exact"),
    ("minhash_jaccard", "jaccard_found"),
    ("containment", "containment_found"),
    ("line_check", "verified_found"),
)
_RECALL_COLUMNS = [
    "method",
    "proven_pairs",
    "found",
    "recall",
    "recall_without_forks",
]
_PROVEN_COLUMNS = [
    "repo",
    "path",
    "lineage_id",
    "current_file_sha",
    "exact",
    "jaccard_found",
    "containment_found",
    "verified_found",
]
_PROVEN_FLAGS = (
    "exact",
    "jaccard_found",
    "containment_found",
    "verified_found",
)

_HISTORY_COLUMNS = [
    "repo",
    "path",
    "commit",
    "commit_date",
    "blob",
    "version_seq",
    "source",
    "inherited",
    "file_sha",
]
_LINK_COLUMNS = [
    "repo",
    "path",
    "file_sha",
    "lineage_id",
    "version_seq",
    "link",
    "link_similarity",
    "release_date",
    "is_hub",
    "history_source",
]
_COPY_COLUMNS = [
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
]
_FAMILY_COLUMNS = [
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
]

# Query Q7.1, see QUERIES.md
# Wild history calls the date commit_at and the path target_path.
WILD_VERSIONS_SQL = """
SELECT
    repo,
    target_path AS path,
    commit,
    commit_at AS commit_date,
    blob,
    version_seq,
    status
FROM wild_versions
WHERE blob IS NOT NULL
  AND blob <> ''
  AND lower(blob) <> repeat('0', 40)
  AND coalesce(status, '') <> 'D'
"""

# Query Q7.2, see QUERIES.md
COPIED_SKILL_VERSIONS_SQL = """
SELECT
    repo,
    CASE
        WHEN skill_folder IS NULL OR skill_folder IN ('', '.') THEN file_path
        ELSE skill_folder || '/' || file_path
    END AS path,
    commit,
    commit_date,
    new_sha AS blob,
    commit_seq,
    status
FROM copied_changes
WHERE is_skill_md
  AND coalesce(status, '') <> 'D'
  AND new_sha IS NOT NULL
  AND new_sha <> ''
  AND lower(new_sha) <> repeat('0', 40)
"""

# Query Q7.3, see QUERIES.md
REGISTRY_COMMITS_SQL = """
SELECT DISTINCT lower(commit) AS commit
FROM registry_changes
WHERE commit IS NOT NULL
  AND commit <> ''
"""

# Query Q7.4, see QUERIES.md
ENGLISH_HISTORY_PATHS_SQL = """
SELECT
    artifacts.repo_full_name AS repo,
    artifacts.path AS path,
    any_value(artifacts.file_sha) AS file_sha
FROM artifacts
JOIN history_paths
  ON artifacts.repo_full_name = history_paths.repo
 AND artifacts.path = history_paths.path
JOIN skill_language
  ON artifacts.file_sha = skill_language.file_sha
ANTI JOIN registry_repos
  ON artifacts.repo_full_name = registry_repos.repo
WHERE skill_language.is_english
  AND artifacts.path IS NOT NULL
GROUP BY artifacts.repo_full_name, artifacts.path
"""

# Query Q7.5, see QUERIES.md
BLOB_TEXTS_SQL = """
SELECT blob, any_value(text) AS text
FROM (
    SELECT copied_texts.sha AS blob, copied_texts.text AS text
    FROM copied_texts
    JOIN needed_blobs ON copied_texts.sha = needed_blobs.blob
    UNION ALL
    SELECT wild_blobs.blob AS blob, wild_blobs.content AS text
    FROM wild_blobs
    JOIN needed_blobs ON wild_blobs.blob = needed_blobs.blob
) AS found
GROUP BY blob
"""

# Query Q7.6, see QUERIES.md
FILE_FIRST_COMMIT_SQL = """
SELECT
    artifacts.repo_full_name AS repo,
    artifacts.path AS path,
    any_value(artifacts.first_commit_at) AS first_commit_at
FROM artifacts
JOIN file_keys
  ON artifacts.repo_full_name = file_keys.repo
 AND artifacts.path = file_keys.path
GROUP BY artifacts.repo_full_name, artifacts.path
"""

# Query Q7.7, see QUERIES.md
# One link per text. Rank, then a non-hub, then the oldest release.
BEST_TEXT_LINK_SQL = """
SELECT
    picked.file_sha AS file_sha,
    picked.root_lineage AS root_lineage,
    picked.best_link AS best_link,
    picked.lineage_id AS lineage_id,
    coalesce(registry_hubs.is_hub, false) AS root_is_hub
FROM (
    SELECT
        file_sha,
        root_lineage,
        link AS best_link,
        lineage_id,
        row_number() OVER (
            PARTITION BY file_sha
            ORDER BY link_rank ASC,
                     source_is_hub ASC,
                     release_date ASC NULLS LAST,
                     lineage_id ASC
        ) AS rn
    FROM copy_files
    WHERE file_sha IS NOT NULL
      AND root_lineage IS NOT NULL
      AND link IS NOT NULL
) AS picked
LEFT JOIN registry_hubs
  ON split_part(picked.root_lineage, '::', 1) = registry_hubs.repo
WHERE picked.rn = 1
"""

# Query Q7.8, see QUERIES.md
# A text that is a registry blob and has no file outside a registry repo.
REGISTRY_SELF_SQL = """
WITH version_blobs AS (
    SELECT
        lower(lineage_versions.blob) AS file_sha,
        lineage_versions.lineage_id AS lineage_id,
        lineage_versions.release_date AS release_date,
        coalesce(lineage_roots.root_lineage, lineage_versions.lineage_id) AS root_lineage,
        CASE WHEN coalesce(source_hubs.is_hub, false) THEN 1 ELSE 0 END AS source_is_hub
    FROM lineage_versions
    LEFT JOIN lineage_roots
      ON lineage_versions.lineage_id = lineage_roots.lineage_id
    LEFT JOIN lineages
      ON lineage_versions.lineage_id = lineages.lineage_id
    LEFT JOIN registry_hubs AS source_hubs
      ON lineages.repo = source_hubs.repo
    WHERE lineage_versions.blob IS NOT NULL
      AND lineage_versions.blob <> ''
),
places AS (
    SELECT
        artifacts.file_sha AS file_sha,
        count(*) AS n_files,
        count(*) FILTER (WHERE coalesce(repo_roles.is_registry, false)) AS n_registry
    FROM artifacts
    JOIN version_blobs ON artifacts.file_sha = version_blobs.file_sha
    LEFT JOIN repo_roles ON artifacts.repo_full_name = repo_roles.repo
    GROUP BY artifacts.file_sha
),
inside AS (
    SELECT version_blobs.*
    FROM version_blobs
    JOIN places ON version_blobs.file_sha = places.file_sha
    WHERE places.n_files > 0
      AND places.n_files = places.n_registry
),
picked AS (
    SELECT * EXCLUDE (rn)
    FROM (
        SELECT
            inside.*,
            row_number() OVER (
                PARTITION BY file_sha
                ORDER BY source_is_hub ASC,
                         release_date ASC NULLS LAST,
                         lineage_id ASC
            ) AS rn
        FROM inside
    ) AS ranked
    WHERE rn = 1
)
SELECT
    picked.file_sha AS file_sha,
    picked.root_lineage AS root_lineage,
    coalesce(root_hubs.is_hub, false) AS root_is_hub
FROM picked
LEFT JOIN registry_hubs AS root_hubs
  ON split_part(picked.root_lineage, '::', 1) = root_hubs.repo
"""

# Query Q7.9, see QUERIES.md
ENGLISH_CLUSTERS_SQL = """
WITH members AS (
    SELECT DISTINCT clusters.file_sha AS file_sha, clusters.cluster_id AS cluster_id
    FROM clusters
    JOIN skill_language ON clusters.file_sha = skill_language.file_sha
    WHERE skill_language.is_english
      AND abs(clusters.threshold - {threshold}) < 1e-6
)
SELECT
    members.file_sha AS file_sha,
    members.cluster_id AS cluster_id,
    counts.english_texts AS english_texts
FROM members
JOIN (
    SELECT cluster_id, count(*) AS english_texts
    FROM members
    GROUP BY cluster_id
) AS counts ON members.cluster_id = counts.cluster_id
"""

# Query Q7.10, see QUERIES.md
ASSIGN_FAMILIES_SQL = """
SELECT
    english.file_sha AS file_sha,
    CASE
        WHEN best.file_sha IS NOT NULL THEN best.root_lineage
        WHEN self_text.file_sha IS NOT NULL THEN self_text.root_lineage
        WHEN clusters.english_texts >= 2
            THEN 'cluster:' || CAST(clusters.cluster_id AS VARCHAR)
        ELSE 'single:' || english.file_sha
    END AS family_id,
    CASE
        WHEN best.file_sha IS NOT NULL OR self_text.file_sha IS NOT NULL THEN
            CASE
                WHEN coalesce(best.root_is_hub, self_text.root_is_hub, false)
                THEN 'hub_only'
                ELSE 'source'
            END
        WHEN clusters.english_texts >= 2 THEN 'independent'
        ELSE 'singleton'
    END AS family_kind,
    best.best_link AS best_link,
    (self_text.file_sha IS NOT NULL AND best.file_sha IS NULL) AS registry_self
FROM (
    SELECT file_sha FROM skill_language WHERE is_english
) AS english
LEFT JOIN best_text_link AS best ON english.file_sha = best.file_sha
LEFT JOIN registry_self_text AS self_text ON english.file_sha = self_text.file_sha
LEFT JOIN english_cluster AS clusters ON english.file_sha = clusters.file_sha
"""

# Query Q7.11, see QUERIES.md
# Copy-file totals for source and hub families. Same-owner copies are
# removed together with forks. The totals are repeated on every text.
COPY_FAMILY_COUNTS_SQL = """
SELECT
    root_lineage AS family_id,
    count(*)::BIGINT AS files,
    count(DISTINCT repo)::BIGINT AS repos,
    count(DISTINCT split_part(repo, '/', 1))::BIGINT AS owners,
    count(*) FILTER (
        WHERE NOT coalesce(is_fork, false)
          AND NOT coalesce(same_owner_as_registry, false)
    )::BIGINT AS files_without_forks,
    count(DISTINCT CASE
        WHEN NOT coalesce(is_fork, false)
         AND NOT coalesce(same_owner_as_registry, false)
        THEN repo
    END)::BIGINT AS repos_without_forks
FROM copy_files
WHERE root_lineage IS NOT NULL
GROUP BY root_lineage
"""

# Query Q7.12, see QUERIES.md
LOOSE_FAMILY_COUNTS_SQL = """
SELECT
    assigned.family_id AS family_id,
    count(*)::BIGINT AS files,
    count(DISTINCT artifacts.repo_full_name)::BIGINT AS repos,
    count(DISTINCT coalesce(
        nullif(repos.owner, ''),
        split_part(artifacts.repo_full_name, '/', 1)
    ))::BIGINT AS owners,
    count(*) FILTER (
        WHERE coalesce(repos.is_fork, 0) <> 1
    )::BIGINT AS files_without_forks,
    count(DISTINCT CASE
        WHEN coalesce(repos.is_fork, 0) <> 1 THEN artifacts.repo_full_name
    END)::BIGINT AS repos_without_forks
FROM assigned
JOIN artifacts ON assigned.file_sha = artifacts.file_sha
LEFT JOIN repos ON artifacts.repo_full_name = repos.full_name
WHERE assigned.family_kind IN ('independent', 'singleton')
GROUP BY assigned.family_id
"""

# Query Q7.13, see QUERIES.md
FAMILY_WITH_COUNTS_SQL = """
SELECT
    assigned.file_sha AS file_sha,
    assigned.family_id AS family_id,
    assigned.family_kind AS family_kind,
    assigned.best_link AS best_link,
    assigned.registry_self AS registry_self,
    coalesce(counts.files, 0)::BIGINT AS files,
    coalesce(counts.repos, 0)::BIGINT AS repos,
    coalesce(counts.owners, 0)::BIGINT AS owners,
    coalesce(counts.files_without_forks, 0)::BIGINT AS files_without_forks,
    coalesce(counts.repos_without_forks, 0)::BIGINT AS repos_without_forks
FROM assigned
LEFT JOIN (
    SELECT * FROM copy_family_counts
    UNION ALL
    SELECT * FROM loose_family_counts
) AS counts ON assigned.family_id = counts.family_id
"""

# Query Q7.14, see QUERIES.md
# Registry version text for the lineages a history-exact file points at.
PROVEN_REGISTRY_TEXTS_SQL = """
SELECT sha AS blob, any_value(text) AS text
FROM read_parquet({path})
WHERE sha IN ({sha_list})
GROUP BY sha
"""

# Query Q7.15, see QUERIES.md
# Current text of a copied skill, from the stage 6 text store.
PROVEN_HISTORY_TEXTS_SQL = """
SELECT sha AS blob, any_value(text) AS text
FROM read_parquet({path})
WHERE sha IN ({sha_list})
GROUP BY sha
"""

# Query Q7.16, see QUERIES.md
# Current text when the newest blob is in wild history, not stage 6.
PROVEN_WILD_TEXTS_SQL = """
SELECT blob, any_value(content) AS text
FROM read_parquet({path})
WHERE blob IN ({sha_list})
GROUP BY blob
"""

# Query Q7.17, see QUERIES.md
# Dataset text for a sha that neither history store has.
PROVEN_DATASET_TEXTS_SQL = """
SELECT file_sha AS blob, any_value(content) AS text
FROM read_parquet({path})
WHERE file_sha IN ({sha_list})
  AND content IS NOT NULL
GROUP BY file_sha
"""


def _quote_path(path: str) -> str:
    """Return a single-quoted SQL string with quotes escaped."""
    return "'" + path.replace("'", "''") + "'"


def _language_path() -> Path:
    """Return ``skill_language.parquet`` in the full data folder."""
    return settings.data_dir(False) / "skill_language.parquet"


def _boilerplate_path() -> Path:
    """Return the shared boilerplate file in the full data folder."""
    return settings.data_dir(False) / "boilerplate_lines.parquet"


def _dataset_dir() -> Path:
    """Return the shared dataset-signature folder in the full data folder."""
    return settings.data_dir(False) / "dataset_signatures"


def _output_paths(dev: bool) -> list[Path]:
    """Return the five stage 7 parquet files for this mode."""
    folder = settings.data_dir(dev)
    return [
        folder / "copy_files.parquet",
        folder / "families.parquet",
        folder / "history_link_counts.parquet",
        folder / "proven_pairs.parquet",
        folder / "link_method_recall.parquet",
    ]


def _registry_texts_path(dev: bool) -> Path:
    """Return ``registry_history_texts.parquet`` for this mode."""
    return settings.data_dir(dev) / "registry_history" / "registry_history_texts.parquet"


def _copied_history_dir(dev: bool) -> Path:
    """Return the stage 6 folder for this mode."""
    return settings.data_dir(dev) / "copied_skill_history"


def _prepare_duckdb(con: duckdb.DuckDBPyConnection, temp_dir: Path) -> None:
    """Turn off the progress bar and give large joins a spill folder.

    Inputs: an open connection and a folder for temporary files.
    Outputs: none. The connection settings change.
    """
    temp_dir.mkdir(parents=True, exist_ok=True)
    con.execute("SET enable_progress_bar = false")
    con.execute("SET preserve_insertion_order = false")
    con.execute(f"SET memory_limit = '{settings.DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET max_temp_directory_size = '{settings.DUCKDB_TEMP_LIMIT}'")
    con.execute(f"SET temp_directory = {_quote_path(str(temp_dir))}")


def _relation_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    """True when ``name`` is a table or a view on this connection."""
    rows = con.execute(
        """
        SELECT count(*) FROM information_schema.tables
        WHERE lower(table_name) = lower(?)
        """,
        [name],
    ).fetchall()
    return bool(rows and rows[0][0])


def _register_view(con: duckdb.DuckDBPyConnection, name: str, path: str, columns: str) -> None:
    """Point ``name`` at a parquet file, reading only ``columns``."""
    quoted = _quote_path(path)
    con.execute(
        f"CREATE OR REPLACE VIEW {name} AS SELECT {columns} FROM read_parquet({quoted})"
    )


def _parse_time(value: object) -> pd.Timestamp:
    """Return a UTC timestamp, or NaT when the value is missing or invalid.

    Example:
    >>> _parse_time("2026-03-01T03:00:00+03:00").tz_convert("UTC").hour
    0
    >>> pd.isna(_parse_time(None))
    True
    """
    if value is None:
        return pd.NaT
    try:
        if pd.isna(value):
            return pd.NaT
    except TypeError:
        pass
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if isinstance(parsed, pd.Timestamp):
        return parsed
    return pd.NaT


def _format_time(value: object) -> str | None:
    """Return a UTC instant as ``YYYY-MM-DDTHH:MM:SSZ``, or None.

    Example:
    >>> _format_time("2026-02-01T00:00:00+03:00")
    '2026-01-31T21:00:00Z'
    >>> _format_time(None) is None
    True
    """
    parsed = _parse_time(value)
    if pd.isna(parsed):
        return None
    return parsed.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_bool(value: object) -> bool:
    """Return False for missing cells and true for a true flag."""
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except TypeError:
        return bool(value)
    return bool(value)


def _owner_of(repo: object) -> str:
    """Return the GitHub owner, the text before the first slash."""
    if not isinstance(repo, str) or "/" not in repo:
        return ""
    return repo.split("/", 1)[0]


def _version_id(lineage_id: object, version_seq: object) -> str:
    """Return the signature id ``lineage_id#version_seq``."""
    return f"{lineage_id}#{int(version_seq)}"


def _empty_frame(columns: list[str]) -> pd.DataFrame:
    """Return a frame with ``columns`` and no rows."""
    return pd.DataFrame(columns=columns)


def _require_columns(frame: pd.DataFrame, columns: tuple[str, ...], label: str) -> None:
    """Raise ``ValueError`` when ``frame`` is missing any of ``columns``."""
    missing = [name for name in columns if name not in frame.columns]
    if missing:
        raise ValueError(f"{label} is missing columns: {', '.join(missing)}")


def read_inputs(con: duckdb.DuckDBPyConnection, dev: bool) -> None:
    """Check the stage inputs and register views for the large tables.

    Small lineage tables are read later with ``read_parquet``. Artifact
    views do not select ``content``. A missing file raises
    ``FileNotFoundError`` and names the command that builds it.

    Inputs: an open connection and ``dev``.
    Outputs: views for artifacts, histories, clusters, and the language table.
    """
    flag = " --dev" if dev else ""
    copied = _copied_history_dir(dev)
    folder = settings.data_dir(dev)
    registry_changes = folder / "registry_history" / "registry_history_changes.parquet"
    paths = {
        "artifacts": settings.ARTIFACTS_GLOB,
        "repos": settings.REPOS_GLOB,
        "skill_language": _language_path(),
        "clusters": settings.CLUSTERS_PATH,
        "wild_versions": settings.WILD_HISTORY_DIR / "versions.parquet",
        "wild_blobs": settings.WILD_HISTORY_DIR / "blobs.parquet",
        "copied_changes": copied / "copied_skill_history_changes.parquet",
        "copied_texts": copied / "copied_skill_history_texts.parquet",
        "registry_changes": registry_changes,
        "registry_texts": _registry_texts_path(dev),
        "copy_candidates": folder / "copy_candidates.parquet",
        "text_links": folder / "text_links.parquet",
        "lineage_versions": folder / "lineage_versions.parquet",
        "lineages": folder / "lineages.parquet",
        "lineage_roots": folder / "lineage_roots.parquet",
        "registry_hubs": folder / "registry_hubs.parquet",
        "repo_roles": folder / "repo_roles_final.parquet",
        "registry_signatures": folder / "registry_signatures.npz",
        "dataset_signatures": _dataset_dir(),
        "boilerplate_lines": _boilerplate_path(),
    }
    copies = f"python -m skill_genealogy.find_copies{flag} --only copies"
    rehost = f"python -m skill_genealogy.find_copies{flag} --only rehost"
    lineages = f"python -m skill_genealogy.build_registry_lineages{flag}"
    check_inputs_exist(
        paths,
        {
            "artifacts": "python src/data/download_data.py",
            "repos": "python src/data/download_data.py",
            "skill_language": "python -m skill_genealogy.select_registries --force-languages",
            "clusters": "python -m skillsig dedup",
            "wild_versions": "main pipeline step 3f",
            "wild_blobs": "main pipeline step 3f",
            "copied_changes": f"python -m skill_genealogy.download_copied_skill_history{flag}",
            "copied_texts": f"python -m skill_genealogy.download_copied_skill_history{flag}",
            "registry_changes": f"python -m skill_genealogy.download_registry_history{flag}",
            "registry_texts": f"python -m skill_genealogy.download_registry_history{flag}",
            "copy_candidates": copies,
            "text_links": copies,
            "lineage_versions": lineages,
            "lineages": lineages,
            "lineage_roots": rehost,
            "registry_hubs": rehost,
            "repo_roles": rehost,
            "registry_signatures": (
                f"python -m skill_genealogy.find_copies{flag} --only signatures"
            ),
            "dataset_signatures": "python -m skill_genealogy.find_copies --only signatures",
            "boilerplate_lines": "python -m skill_genealogy.find_copies --only signatures",
        },
    )
    _register_view(
        con,
        "artifacts",
        str(paths["artifacts"]),
        "repo_full_name, path, file_sha, first_commit_at",
    )
    _register_view(con, "repos", str(paths["repos"]), "full_name, owner, is_fork")
    _register_view(
        con, "skill_language", str(paths["skill_language"]), "file_sha, is_english"
    )
    _register_view(
        con, "clusters", str(paths["clusters"]), "file_sha, cluster_id, threshold"
    )
    _register_view(con, "wild_versions", str(paths["wild_versions"]), "*")
    _register_view(con, "wild_blobs", str(paths["wild_blobs"]), "blob, content")
    _register_view(con, "copied_changes", str(paths["copied_changes"]), "*")
    _register_view(con, "copied_texts", str(paths["copied_texts"]), "sha, text")
    _register_view(con, "registry_changes", str(paths["registry_changes"]), "commit")
    _register_view(
        con, "repo_roles", str(paths["repo_roles"]), "repo, is_registry, is_fork, repo_role"
    )
    con.execute(
        """
        CREATE OR REPLACE VIEW registry_repos AS
        SELECT repo FROM repo_roles WHERE coalesce(is_registry, false)
        """
    )
    _register_view(
        con,
        "lineage_versions",
        str(paths["lineage_versions"]),
        "lineage_id, version_seq, blob, release_date",
    )
    _register_view(con, "lineages", str(paths["lineages"]), "lineage_id, repo")
    _register_view(
        con, "lineage_roots", str(paths["lineage_roots"]), "lineage_id, root_lineage"
    )
    _register_view(con, "registry_hubs", str(paths["registry_hubs"]), "repo, is_hub")


def _fetch_frame(con: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    """Run ``sql`` and return a DataFrame. An empty result keeps its columns."""
    frame = con.execute(sql).fetchdf()
    if frame is None:
        return pd.DataFrame()
    return frame


def _clean_blob(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop deletes and empty or all-zero blobs."""
    if frame.empty:
        return frame
    kept = frame
    if "status" in kept.columns:
        kept = kept.loc[kept["status"].fillna("").astype(str).ne("D")]
    blob = kept["blob"].fillna("").astype(str)
    return kept.loc[(blob.str.len() > 0) & (blob.str.lower() != _ZERO_BLOB)].copy()


def _copied_path(skill_folder: object, file_path: object) -> str:
    """Return ``skill_folder/file_path``, or ``file_path`` for a root file.

    Example:
    >>> _copied_path("skills/pdf", "SKILL.md")
    'skills/pdf/SKILL.md'
    >>> _copied_path(".", "SKILL.md")
    'SKILL.md'
    """
    folder = "" if skill_folder is None else str(skill_folder)
    name = "" if file_path is None else str(file_path)
    if folder in {"", "."}:
        return name
    return f"{folder}/{name}"


def _prepare_wild_rows(wild: pd.DataFrame) -> pd.DataFrame:
    """Map wild-history columns onto the shared history columns.

    ``commit_at`` becomes ``commit_date``. ``target_path`` becomes ``path``.
    ``version_seq`` is kept. The source is ``wild``.
    """
    if wild.empty:
        columns = ["repo", "path", "commit", "commit_date", "blob", "version_seq", "source"]
        return _empty_frame(columns)
    frame = wild.copy()
    if "path" not in frame.columns:
        frame["path"] = frame["target_path"]
    if "commit_date" not in frame.columns:
        frame["commit_date"] = frame["commit_at"]
    frame["source"] = "wild"
    frame["version_seq"] = pd.to_numeric(frame["version_seq"], errors="coerce").fillna(0)
    frame["version_seq"] = frame["version_seq"].astype(int)
    return _clean_blob(frame)


def _prepare_copied_rows(copied: pd.DataFrame) -> pd.DataFrame:
    """Map copied-skill ``SKILL.md`` rows onto the shared history columns.

    The path is the skill folder plus ``file_path``. ``version_seq`` is left
    empty here and filled after the English filter, in commit order.
    """
    if copied.empty:
        return _empty_frame(
            ["repo", "path", "commit", "commit_date", "blob", "commit_seq", "source"]
        )
    frame = copied.copy()
    if "path" not in frame.columns:
        frame["path"] = [
            _copied_path(folder, name)
            for folder, name in zip(frame["skill_folder"], frame["file_path"], strict=True)
        ]
    if "blob" not in frame.columns:
        frame["blob"] = frame["new_sha"]
    if "commit_seq" not in frame.columns:
        frame["commit_seq"] = 0
    frame["commit_seq"] = pd.to_numeric(frame["commit_seq"], errors="coerce").fillna(0).astype(int)
    frame["source"] = "copied"
    return _clean_blob(frame)


def _number_copied_versions(frame: pd.DataFrame) -> pd.DataFrame:
    """Number copied rows 1..n per file, in commit order. Wild rows stay."""
    if frame.empty:
        return frame
    copied = frame.loc[frame["source"].eq("copied")].sort_values(
        ["repo", "path", "commit_date", "commit_seq", "commit", "blob"],
        kind="mergesort",
    )
    if not copied.empty:
        copied = copied.copy()
        copied["version_seq"] = copied.groupby(["repo", "path"], sort=False).cumcount() + 1
    wild = frame.loc[frame["source"].eq("wild")]
    stacked = pd.concat([wild, copied], ignore_index=True)
    stacked["version_seq"] = pd.to_numeric(stacked["version_seq"], errors="coerce").fillna(0)
    stacked["version_seq"] = stacked["version_seq"].astype(int)
    return stacked


def _assemble_copy_histories(
    wild: pd.DataFrame,
    copied: pd.DataFrame,
    english_files: pd.DataFrame,
    registry_commits: pd.Series | list[str],
) -> pd.DataFrame:
    """Stack wild and copied versions for English files outside registries.

    A commit that also appears in registry history is marked ``inherited``
    and kept. Paths missing from ``english_files`` are dropped. Wild dates
    come from ``commit_at`` when ``commit_date`` is absent.

    Inputs: wild rows, copied ``SKILL.md`` rows, English ``(repo, path,
    file_sha)``, and registry commit hashes.
    Outputs: one row per kept version. Columns are ``_HISTORY_COLUMNS``.

    Example:
    >>> wild = pd.DataFrame([{
    ...     "repo": "a/r", "target_path": "skills/pdf/SKILL.md", "commit": "aaa",
    ...     "commit_at": "2026-02-01T00:00:00Z", "blob": "b1", "version_seq": 2,
    ...     "status": "A",
    ... }])
    >>> copied = pd.DataFrame([{
    ...     "repo": "a/r", "skill_folder": "skills/pdf", "file_path": "SKILL.md",
    ...     "commit": "bbb", "commit_date": "2026-03-01T00:00:00Z", "new_sha": "b2",
    ...     "commit_seq": 1, "status": "M",
    ... }])
    >>> english = pd.DataFrame([{
    ...     "repo": "a/r", "path": "skills/pdf/SKILL.md", "file_sha": "sha",
    ... }])
    >>> rows = _assemble_copy_histories(wild, copied, english, ["bbb"])
    >>> list(rows["source"])
    ['wild', 'copied']
    >>> bool(rows.loc[rows["source"].eq("copied"), "inherited"].iloc[0])
    True
    """
    wild_rows = _prepare_wild_rows(wild)
    copied_rows = _prepare_copied_rows(copied)
    stacked = pd.concat([wild_rows, copied_rows], ignore_index=True)
    if stacked.empty or english_files.empty:
        return _empty_frame(_HISTORY_COLUMNS)
    keys = english_files[["repo", "path", "file_sha"]].drop_duplicates(["repo", "path"])
    kept = stacked.merge(keys, on=["repo", "path"], how="inner")
    if kept.empty:
        return _empty_frame(_HISTORY_COLUMNS)
    kept = kept.drop_duplicates(["repo", "path", "source", "commit", "blob"], keep="first")
    kept["commit_date"] = kept["commit_date"].map(_format_time)
    if "commit_seq" not in kept.columns:
        kept["commit_seq"] = 0
    kept.loc[kept["source"].eq("wild"), "commit_seq"] = 0
    kept = _number_copied_versions(kept)
    commits = {
        str(commit).lower()
        for commit in registry_commits
        if isinstance(commit, str) and commit.strip()
    }
    kept["inherited"] = kept["commit"].fillna("").astype(str).str.lower().isin(commits)
    kept["file_sha"] = kept["file_sha"].astype(str)
    return kept[_HISTORY_COLUMNS].reset_index(drop=True)


def read_copy_histories(dev: bool) -> pd.DataFrame:
    """Read wild and copied ``SKILL.md`` versions for English copy files.

    Wild ``commit_at`` is stored as ``commit_date`` and ``target_path`` as
    ``path``. Copied rows are ``is_skill_md`` changes that are not deletes.
    A file inside a registry repo is left out. ``inherited`` is true when
    the commit hash is also in ``registry_history_changes``.

    Inputs: ``dev`` — True reads the dev folder.
    Outputs: columns ``repo, path, commit, commit_date, blob, version_seq,
    source, inherited, file_sha``. ``source`` is ``wild`` or ``copied``.

    Example:
    >>> frame = _assemble_copy_histories(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), [])
    >>> list(frame.columns) == _HISTORY_COLUMNS
    True
    """
    temp_dir = settings.data_dir(dev) / "_assign_histories_tmp"
    _remove_temp_dir(temp_dir)
    con = connect_duckdb()
    try:
        _prepare_duckdb(con, temp_dir)
        if not _relation_exists(con, "wild_versions"):
            read_inputs(con, dev)
        wild = _fetch_frame(con, WILD_VERSIONS_SQL)
        print(f"wild versions {len(wild)}", flush=True)
        copied = _fetch_frame(con, COPIED_SKILL_VERSIONS_SQL)
        print(f"copied skill versions {len(copied)}", flush=True)
        commits = _fetch_frame(con, REGISTRY_COMMITS_SQL)
        path_rows = pd.concat(
            [wild[["repo", "path"]], copied[["repo", "path"]]],
            ignore_index=True,
        ).drop_duplicates()
        # A native table, not a pandas view, so the artifact join is a hash join.
        con.register("history_paths_input", path_rows)
        con.execute(
            "CREATE OR REPLACE TEMP TABLE history_paths "
            "AS SELECT * FROM history_paths_input"
        )
        print(f"history paths {len(path_rows)}", flush=True)
        english = _fetch_frame(con, ENGLISH_HISTORY_PATHS_SQL)
        print(f"english history files {len(english)}", flush=True)
        commit_list = commits["commit"].tolist() if "commit" in commits.columns else []
        return _assemble_copy_histories(wild, copied, english, commit_list)
    finally:
        con.close()
        _remove_temp_dir(temp_dir)


def _remove_temp_dir(temp_dir: Path) -> None:
    """Delete a temporary DuckDB spill folder. A missing folder is ignored."""
    shutil.rmtree(temp_dir, ignore_errors=True)


def _bundle_arrays(sigs: object) -> tuple[list[str], np.ndarray, np.ndarray]:
    """Return ids, a uint64 matrix, and shingle counts from a signature bundle.

    ``sigs`` may be the dict used by stage 5 (``ids``, ``signatures``,
    ``sizes``) or the tuple ``load_signatures`` returns.
    """
    if isinstance(sigs, tuple):
        ids, matrix, sizes = sigs
    else:
        bundle = sigs if isinstance(sigs, dict) else {}
        ids = bundle.get("ids", [])
        matrix = bundle.get("signatures", np.zeros((0, settings.MINHASH_NUM_PERM), dtype=np.uint64))
        sizes = bundle.get("sizes", np.zeros((0,), dtype=np.int32))
    id_list = [str(item) for item in list(ids)]
    values = np.asarray(matrix, dtype=np.uint64)
    if values.size == 0:
        values = np.zeros((0, settings.MINHASH_NUM_PERM), dtype=np.uint64)
    if values.ndim == 1:
        values = values.reshape(1, -1)
    counts = np.asarray(sizes, dtype=np.int32).reshape(-1)
    return id_list, values, counts


def _jaccard_simple(
    query: np.ndarray,
    query_size: int,
    matrix: np.ndarray,
    sizes: np.ndarray,
) -> np.ndarray:
    """Estimated Jaccard of one signature against each row, via ``estimate_jaccard``.

    A shingle count of 0 scores 0. The score is the share of the 128
    permutations that agree.

    Example:
    >>> query, query_size = signature_for_text("alpha beta gamma delta epsilon", set())
    >>> float(_jaccard_simple(query, query_size, query.reshape(1, -1), np.array([query_size]))[0])
    1.0
    """
    if matrix.shape[0] == 0:
        return np.zeros(0, dtype=np.float64)
    if query_size <= 0:
        return np.zeros(matrix.shape[0], dtype=np.float64)
    sketch = minhash_from_array(np.asarray(query, dtype=np.uint64))
    scores = np.zeros(matrix.shape[0], dtype=np.float64)
    for index, (row, size) in enumerate(zip(matrix, sizes, strict=True)):
        if int(size) <= 0:
            continue
        scores[index] = estimate_jaccard(sketch, minhash_from_array(row))
    return scores


def _jaccard_fast(
    query: np.ndarray,
    query_size: int,
    matrix: np.ndarray,
    sizes: np.ndarray,
) -> np.ndarray:
    """Same score as ``_jaccard_simple``, computed with one array compare.

    Agreeing MinHash values divided by 128 is what ``estimate_jaccard``
    returns. A shingle count of 0 scores 0, including two zero arrays.

    Example:
    >>> query, query_size = signature_for_text("alpha beta gamma delta epsilon", set())
    >>> float(_jaccard_fast(query, 0, query.reshape(1, -1), np.array([query_size]))[0])
    0.0
    """
    if matrix.shape[0] == 0:
        return np.zeros(0, dtype=np.float64)
    if query_size <= 0:
        return np.zeros(matrix.shape[0], dtype=np.float64)
    scores = np.asarray(matrix == np.asarray(query, dtype=np.uint64), dtype=np.float64).mean(axis=1)
    return np.where(np.asarray(sizes).reshape(-1) <= 0, 0.0, scores)


def _registry_meta(sigs: object, ids: list[str]) -> pd.DataFrame:
    """Return lineage, version, release date, and hub flag aligned to ``ids``."""
    if isinstance(sigs, dict) and isinstance(sigs.get("meta"), pd.DataFrame):
        meta = sigs["meta"].copy()
        if "version_id" not in meta.columns:
            meta["version_id"] = [
                _version_id(lineage, seq)
                for lineage, seq in zip(meta["lineage_id"], meta["version_seq"], strict=True)
            ]
        return meta
    rows = []
    for name in ids:
        lineage, seq_text = name.rsplit("#", 1)
        rows.append(
            {
                "version_id": name,
                "lineage_id": lineage,
                "version_seq": int(seq_text),
                "release_date": None,
                "is_hub": False,
            }
        )
    return pd.DataFrame(rows)


def _boilerplate_lines() -> set[str]:
    """Return the shared boilerplate line set, or an empty set when it is missing."""
    global _BOILERPLATE_CACHE
    if _BOILERPLATE_CACHE is not None:
        return _BOILERPLATE_CACHE
    path = _boilerplate_path()
    if not path.is_file():
        _BOILERPLATE_CACHE = set()
        return _BOILERPLATE_CACHE
    frame = read_parquet(path)
    lines = set(frame["line"].astype(str).tolist()) if "line" in frame.columns else set()
    _BOILERPLATE_CACHE = lines
    return lines


def _empty_links() -> pd.DataFrame:
    """Return a link frame with the columns both history finders share."""
    return _empty_frame(_LINK_COLUMNS)


def _history_link_row(
    history: pd.Series,
    version: pd.Series,
    link: str,
    score: float,
) -> dict[str, object]:
    """One link from a history row and the registry version it matched."""
    return {
        "repo": history["repo"],
        "path": history["path"],
        "file_sha": history["file_sha"] if "file_sha" in history.index else None,
        "lineage_id": version["lineage_id"],
        "version_seq": int(version["version_seq"]),
        "link": link,
        "link_similarity": float(score),
        "release_date": version["release_date"] if "release_date" in version.index else None,
        "is_hub": _as_bool(version["is_hub"]) if "is_hub" in version.index else False,
        "history_source": history["source"] if "source" in history.index else "copied",
    }


# Algorithm A30, see CODE_FLOW.md
def find_history_exact_links(
    histories: pd.DataFrame,
    registry_versions: pd.DataFrame,
) -> pd.DataFrame:
    """Link a file whose own history contains a registry version blob.

    The earliest matching version in the copy's history wins. When that
    blob is a version of more than one lineage, a non-hub wins, then the
    oldest release. Two versions of one lineage with the same release date
    keep the older ``version_seq``. The similarity is 1.

    Inputs: history rows and registry versions with ``blob``.
    Outputs: one row per file. ``link`` is ``history_exact``.

    Example:
    >>> histories = pd.DataFrame([
    ...     {"repo": "a/r", "path": "skills/a/SKILL.md", "commit_date": "2026-02-01T00:00:00Z",
    ...      "version_seq": 1, "blob": "v2", "source": "copied"},
    ...     {"repo": "a/r", "path": "skills/a/SKILL.md", "commit_date": "2026-04-01T00:00:00Z",
    ...      "version_seq": 2, "blob": "v1", "source": "copied"},
    ... ])
    >>> versions = pd.DataFrame([
    ...     {"lineage_id": "hub/reg::skills/a", "version_seq": 2, "blob": "v2",
    ...      "release_date": "2026-03-01", "is_hub": True},
    ...     {"lineage_id": "plain/reg::skills/a", "version_seq": 1, "blob": "v1",
    ...      "release_date": "2026-01-10", "is_hub": False},
    ... ])
    >>> link = find_history_exact_links(histories, versions).iloc[0]
    >>> link.lineage_id
    'hub/reg::skills/a'
    """
    if histories.empty or registry_versions.empty:
        return _empty_links()
    _require_columns(histories, ("repo", "path", "blob", "commit_date", "version_seq"), "histories")
    _require_columns(registry_versions, ("lineage_id", "version_seq", "blob"), "registry_versions")
    left = histories.copy()
    keep = ["lineage_id", "version_seq", "blob"]
    for name in ("release_date", "is_hub"):
        if name in registry_versions.columns:
            keep.append(name)
    right = registry_versions[keep].copy()
    left["blob_key"] = left["blob"].fillna("").astype(str).str.lower()
    right["blob_key"] = right["blob"].fillna("").astype(str).str.lower()
    right = right.loc[right["blob_key"].ne("")]
    matched = left.merge(right, on="blob_key", how="inner", suffixes=("_history", "_registry"))
    if matched.empty:
        return _empty_links()
    matched["commit_ts"] = matched["commit_date"].map(_parse_time)
    if "release_date" not in matched.columns:
        matched["release_date"] = pd.NaT
    matched["release_ts"] = matched["release_date"].map(_parse_time)
    if "is_hub" not in matched.columns:
        matched["is_hub"] = False
    matched["is_hub"] = matched["is_hub"].map(_as_bool)
    if "version_seq_history" in matched.columns:
        history_seq = matched["version_seq_history"]
    else:
        history_seq = matched["version_seq"]
    if "version_seq_registry" in matched.columns:
        registry_seq = matched["version_seq_registry"]
    else:
        registry_seq = matched["version_seq"]
    matched["history_version_seq"] = pd.to_numeric(history_seq, errors="coerce").fillna(0)
    matched["history_version_seq"] = matched["history_version_seq"].astype(int)
    matched["registry_version_seq"] = pd.to_numeric(registry_seq, errors="coerce").fillna(0)
    matched["registry_version_seq"] = matched["registry_version_seq"].astype(int)
    matched = matched.sort_values(
        [
            "repo",
            "path",
            "commit_ts",
            "history_version_seq",
            "is_hub",
            "release_ts",
            "registry_version_seq",
            "lineage_id",
        ],
        kind="mergesort",
        na_position="last",
    )
    winners = matched.drop_duplicates(["repo", "path"], keep="first")
    rows = []
    for record in winners.itertuples(index=False):
        rows.append(
            {
                "repo": record.repo,
                "path": record.path,
                "file_sha": getattr(record, "file_sha", None),
                "lineage_id": record.lineage_id,
                "version_seq": int(record.registry_version_seq),
                "link": "history_exact",
                "link_similarity": 1.0,
                "release_date": _format_time(record.release_date),
                "is_hub": bool(record.is_hub),
                "history_source": getattr(record, "source", "copied"),
            }
        )
    return pd.DataFrame(rows, columns=_LINK_COLUMNS)


def _first_versions(histories: pd.DataFrame, unlinked_files: pd.DataFrame) -> pd.DataFrame:
    """Return the earliest history row of each file in ``unlinked_files``."""
    if histories.empty or unlinked_files.empty:
        return histories.iloc[0:0].copy()
    keys = unlinked_files[["repo", "path"]].drop_duplicates()
    rows = histories.merge(keys, on=["repo", "path"], how="inner")
    if rows.empty:
        return rows
    rows = rows.copy()
    rows["commit_ts"] = rows["commit_date"].map(_parse_time)
    rows["version_seq"] = pd.to_numeric(rows["version_seq"], errors="coerce").fillna(0).astype(int)
    rows = rows.sort_values(
        ["repo", "path", "commit_ts", "version_seq"],
        kind="mergesort",
        na_position="last",
    )
    return rows.drop_duplicates(["repo", "path"], keep="first")


def _best_near_version(scores: np.ndarray, meta: pd.DataFrame) -> pd.Series | None:
    """Return the best registry row at or above the near-copy Jaccard, or None."""
    if meta.empty:
        return None
    scored = meta.copy()
    scored["score"] = scores
    scored = scored.loc[scored["score"] >= settings.NEAR_COPY_MIN_JACCARD]
    if scored.empty:
        return None
    if "is_hub" not in scored.columns:
        scored["is_hub"] = False
    scored["is_hub"] = scored["is_hub"].map(_as_bool)
    if "release_date" in scored.columns:
        scored["release_ts"] = scored["release_date"].map(_parse_time)
    else:
        scored["release_ts"] = pd.NaT
    scored = scored.sort_values(
        ["score", "is_hub", "release_ts", "lineage_id"],
        ascending=[False, True, True, True],
        kind="mergesort",
        na_position="last",
    )
    return scored.iloc[0]


# Algorithm A31, see CODE_FLOW.md
def find_history_near_links(
    histories: pd.DataFrame,
    registry_sigs: object,
    unlinked_files: pd.DataFrame,
) -> pd.DataFrame:
    """Link the first downloaded version when it is near a registry version.

    Only files in ``unlinked_files`` are scored. The earliest history row is
    the one that is signed, with ``signature_for_text``. Later versions are
    not searched. Jaccard must be at least ``NEAR_COPY_MIN_JACCARD`` (0.80).
    The best score wins, then a non-hub, then the oldest release.

    Inputs: history rows (a ``text`` column on the first version), a
    signature bundle, and the files that still need a history-near link.
    Outputs: one row per file that clears the threshold. ``link`` is
    ``history_near``.

    Example:
    >>> text = "alpha beta gamma delta epsilon zeta eta theta"
    >>> values, size = signature_for_text(text, set())
    >>> histories = pd.DataFrame([{
    ...     "repo": "a/r", "path": "skills/a/SKILL.md", "commit_date": "2026-02-01T00:00:00Z",
    ...     "version_seq": 1, "blob": "b", "source": "wild", "text": text,
    ... }])
    >>> sigs = {
    ...     "ids": ["plain/reg::skills/a#1"],
    ...     "signatures": values.reshape(1, -1),
    ...     "sizes": np.array([size], dtype=np.int32),
    ...     "meta": pd.DataFrame([{
    ...         "version_id": "plain/reg::skills/a#1", "lineage_id": "plain/reg::skills/a",
    ...         "version_seq": 1, "release_date": "2026-01-10", "is_hub": False,
    ...     }]),
    ... }
    >>> link = find_history_near_links(histories, sigs, histories[["repo", "path"]]).iloc[0]
    >>> link.link
    'history_near'
    """
    first = _first_versions(histories, unlinked_files)
    if first.empty:
        return _empty_links()
    ids, matrix, sizes = _bundle_arrays(registry_sigs)
    meta = _registry_meta(registry_sigs, ids)
    if meta.empty or matrix.shape[0] == 0:
        return _empty_links()
    meta = meta.drop_duplicates("version_id").set_index("version_id", drop=False)
    ordered = [meta.loc[name] for name in ids if name in meta.index]
    if len(ordered) != matrix.shape[0]:
        keep = [index for index, name in enumerate(ids) if name in meta.index]
        matrix = matrix[np.asarray(keep, dtype=np.int64)]
        sizes = sizes[np.asarray(keep, dtype=np.int64)]
    meta = pd.DataFrame(ordered).reset_index(drop=True)
    boilerplate = _boilerplate_lines()
    rows: list[dict[str, object]] = []
    text_column = first["text"] if "text" in first.columns else pd.Series("", index=first.index)
    for record, text in zip(first.itertuples(index=False), text_column.tolist(), strict=True):
        body = text if isinstance(text, str) else ""
        if not body.strip():
            continue
        query, query_size = signature_for_text(body, boilerplate)
        if query_size <= 0:
            continue
        scores = _jaccard_fast(query, query_size, matrix, sizes)
        winner = _best_near_version(scores, meta)
        if winner is None:
            continue
        rows.append(
            _history_link_row(
                pd.Series(record._asdict()),
                winner,
                "history_near",
                float(winner["score"]),
            )
        )
    if not rows:
        return _empty_links()
    return pd.DataFrame(rows, columns=_LINK_COLUMNS)


# Algorithm A32, see CODE_FLOW.md
def pick_strongest_link(file_links: pd.DataFrame) -> pd.DataFrame:
    """Keep one link per file, using the stage 7 order.

    The order is exact, history_exact, near, near_contained, history_near,
    then cluster. A tie prefers a non-hub registry, then the oldest release.

    Inputs: candidate links with ``repo``, ``path``, ``link``, ``lineage_id``.
    ``is_hub`` and ``release_date`` decide ties.
    Outputs: one row per file, the winning candidate.

    Example:
    >>> links = pd.DataFrame([
    ...     {"repo": "a/r", "path": "skills/a/SKILL.md", "link": "near", "lineage_id": "L",
    ...      "version_seq": 3, "link_similarity": 0.9, "release_date": "2026-05-15",
    ...      "is_hub": False, "history_source": "none"},
    ...     {"repo": "a/r", "path": "skills/a/SKILL.md", "link": "history_exact", "lineage_id": "L",
    ...      "version_seq": 2, "link_similarity": 1.0, "release_date": "2026-03-01",
    ...      "is_hub": False, "history_source": "copied"},
    ... ])
    >>> pick_strongest_link(links).iloc[0].link
    'history_exact'
    """
    if file_links.empty:
        return _empty_links()
    _require_columns(file_links, ("repo", "path", "link", "lineage_id"), "file_links")
    frame = file_links.copy()
    frame["link_rank"] = frame["link"].map(LINK_RANK).fillna(99).astype(int)
    if "is_hub" not in frame.columns:
        frame["is_hub"] = False
    frame["is_hub"] = frame["is_hub"].map(_as_bool)
    frame["release_ts"] = (
        frame["release_date"].map(_parse_time) if "release_date" in frame.columns else pd.NaT
    )
    frame = frame.sort_values(
        ["repo", "path", "link_rank", "is_hub", "release_ts", "lineage_id"],
        kind="mergesort",
        na_position="last",
    )
    winners = frame.drop_duplicates(["repo", "path"], keep="first").drop(
        columns=["link_rank", "release_ts"],
        errors="ignore",
    )
    return winners.reset_index(drop=True)


def _signatures_for_ids(folder: Path, wanted: set[str]) -> tuple[list[str], np.ndarray, np.ndarray]:
    """Load signature rows whose ids are in ``wanted``. Other rows are skipped."""
    if folder.is_file():
        paths = [folder]
    else:
        paths = sorted(path for path in folder.glob("part_*.npz") if path.is_file())
    kept_ids: list[str] = []
    blocks: list[np.ndarray] = []
    size_blocks: list[np.ndarray] = []
    remaining = set(wanted)
    for path in paths:
        if not remaining:
            break
        ids, matrix, sizes = load_signatures(path)
        take = [index for index, name in enumerate(ids) if name in remaining]
        if not take:
            continue
        index = np.asarray(take, dtype=np.int64)
        picked = [ids[item] for item in take]
        kept_ids.extend(picked)
        blocks.append(matrix[index])
        size_blocks.append(sizes[index])
        remaining.difference_update(picked)
    width = settings.MINHASH_NUM_PERM
    if not kept_ids:
        return [], np.zeros((0, width), dtype=np.uint64), np.zeros((0,), dtype=np.int32)
    return kept_ids, np.vstack(blocks), np.concatenate(size_blocks)


def _score_pairs(pairs: pd.DataFrame, sigs: object) -> np.ndarray:
    """Estimated Jaccard for each ``(file_sha, version_id)`` row in ``pairs``.

    The returned array follows ``pairs`` from top to bottom, not the index labels.
    """
    scores = np.zeros(len(pairs), dtype=np.float64)
    if pairs.empty:
        return scores
    work = pairs.reset_index(drop=True)
    ids, matrix, sizes = _bundle_arrays(sigs)
    index = {name: position for position, name in enumerate(ids)}
    for file_sha, group in work.groupby("file_sha", sort=False):
        query_at = index.get(str(file_sha))
        if query_at is None:
            continue
        positions = [index.get(str(name)) for name in group["version_id"].tolist()]
        valid = [position is not None for position in positions]
        if not any(valid):
            continue
        take = [position for position in positions if position is not None]
        take_index = np.asarray(take, dtype=np.int64)
        part = _jaccard_fast(
            matrix[query_at],
            int(sizes[query_at]),
            matrix[take_index],
            sizes[take_index],
        )
        cursor = 0
        for offset, is_valid in enumerate(valid):
            if not is_valid:
                continue
            scores[int(group.index[offset])] = part[cursor]
            cursor += 1
    return scores


def _origin_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Add empty origin columns without dropping the caller's columns."""
    result = frame.copy()
    result["origin_seq"] = pd.Series(pd.NA, index=result.index, dtype="Int64")
    result["origin_date"] = pd.Series(pd.NA, index=result.index, dtype="object")
    result["origin_basis"] = pd.Series(pd.NA, index=result.index, dtype="object")
    result["copied_before_release"] = False
    if "first_commit_date" not in result.columns:
        result["first_commit_date"] = pd.Series(pd.NA, index=result.index, dtype="object")
    return result


def _release_of(dates: pd.Series, lineage: object, seq: int, fallback: object) -> object:
    """Return the registry release for one version, or ``fallback``."""
    key = (lineage, seq)
    if key in dates.index:
        return dates[key]
    return fallback


def _fill_exact_origin(result: pd.DataFrame, versions: pd.DataFrame) -> None:
    """Set the identical registry version as the origin of exact links."""
    exact = result["link"].isin(_EXACT_LINKS)
    if not exact.any():
        return
    lookup = versions.copy()
    lookup["version_seq"] = pd.to_numeric(lookup["version_seq"], errors="coerce")
    lookup = lookup.drop_duplicates(["lineage_id", "version_seq"], keep="first")
    dates = lookup.set_index(["lineage_id", "version_seq"])["release_date"]
    for position in result.index[exact]:
        lineage = result.at[position, "lineage_id"]
        seq = int(result.at[position, "version_seq"])
        fallback = result.at[position, "release_date"] if "release_date" in result.columns else None
        result.at[position, "origin_seq"] = seq
        result.at[position, "origin_basis"] = "exact"
        result.at[position, "copied_before_release"] = False
        release = _release_of(dates, lineage, seq, fallback)
        result.at[position, "origin_date"] = _format_time(release)


def _eligible_versions(copies: pd.DataFrame, versions: pd.DataFrame) -> pd.DataFrame:
    """Registry versions of the linked lineage released on or before the copy.

    Versions with no ``release_date`` are left out before the comparison.
    A column of only missing dates is naive ``NaT``, and that cannot be
    compared with the copy's UTC timestamp.
    """
    dated = copies.loc[copies["first_ts"].notna(), ["row_id", "lineage_id", "file_sha", "first_ts"]]
    if dated.empty:
        return dated
    released = versions.loc[versions["release_ts"].notna()]
    if released.empty:
        return dated.iloc[0:0]
    merged = dated.merge(released, on="lineage_id", how="inner")
    return merged.loc[merged["release_ts"] <= merged["first_ts"]]


def _pick_similar_origin(eligible: pd.DataFrame, sigs: object) -> pd.DataFrame:
    """Keep the most similar eligible version. Ties take the newest release."""
    if eligible.empty:
        return eligible
    scored = eligible.copy()
    scored["version_id"] = [
        _version_id(lineage, seq)
        for lineage, seq in zip(scored["lineage_id"], scored["version_seq"], strict=True)
    ]
    scored["score"] = _score_pairs(scored, sigs)
    scored["version_seq"] = pd.to_numeric(scored["version_seq"], errors="coerce").fillna(0)
    scored["version_seq"] = scored["version_seq"].astype(int)
    scored = scored.sort_values(
        ["row_id", "score", "release_ts", "version_seq"],
        ascending=[True, False, False, False],
        kind="mergesort",
    )
    return scored.drop_duplicates("row_id", keep="first")


# Algorithm A33, see CODE_FLOW.md
def choose_origin_version(
    copy_files: pd.DataFrame,
    registry_versions: pd.DataFrame,
    sigs: object,
) -> pd.DataFrame:
    """Choose the registry version a copy started from, using its first date.

    ``exact`` and ``history_exact`` keep the identical version
    (``origin_basis`` ``exact``), even when the file has no date. Any other
    link takes the most similar version of that lineage with ``release_date``
    on or before ``first_commit_date``. Equal scores take the newest of those
    versions. ``copied_before_release`` is true only when that lineage has at
    least one dated version and every one of them is later than the first
    commit. A lineage with no dated version, or a copy with no date at all,
    sets ``origin_basis`` ``no_date`` and leaves the origin empty. An exact
    link still keeps its version when the copy has no date.

    Registry v1 is 10 Jan, v2 is 1 Mar, v3 is 15 May. A near copy first
    committed on 1 Feb starts at v1. A near copy first committed on 1 Jan
    was copied before every release.

    Inputs: one row per copy file, the lineage versions, and signatures
    keyed by ``file_sha`` and ``lineage_id#version_seq``.
    Outputs: the same rows plus ``origin_seq``, ``origin_date``,
    ``origin_basis``, and ``copied_before_release``.

    Example:
    >>> versions = pd.DataFrame([
    ...     {"lineage_id": "plain/reg::skills/a", "version_seq": 1, "release_date": "2026-01-10"},
    ...     {"lineage_id": "plain/reg::skills/a", "version_seq": 2, "release_date": "2026-03-01"},
    ...     {"lineage_id": "plain/reg::skills/a", "version_seq": 3, "release_date": "2026-05-15"},
    ... ])
    >>> copies = pd.DataFrame([
    ...     {"repo": "a/r", "path": "skills/feb/SKILL.md", "file_sha": "feb",
    ...      "lineage_id": "plain/reg::skills/a", "version_seq": 3,
    ...      "link": "near", "first_commit_date": "2026-02-01"},
    ...     {"repo": "a/r", "path": "skills/jan/SKILL.md", "file_sha": "jan",
    ...      "lineage_id": "plain/reg::skills/a", "version_seq": 3,
    ...      "link": "near", "first_commit_date": "2026-01-01"},
    ... ])
    >>> empty = ([], np.zeros((0, 128), dtype=np.uint64), np.zeros(0, dtype=np.int32))
    >>> origin = choose_origin_version(copies, versions, empty)
    >>> int(origin.loc[origin.path.eq("skills/feb/SKILL.md"), "origin_seq"].iloc[0])
    1
    >>> bool(origin.loc[origin.path.eq("skills/jan/SKILL.md"), "copied_before_release"].iloc[0])
    True
    """
    result = _origin_columns(copy_files)
    if result.empty:
        return result
    _require_columns(result, ("lineage_id", "link", "version_seq"), "copy_files")
    _require_columns(
        registry_versions,
        ("lineage_id", "version_seq", "release_date"),
        "registry_versions",
    )
    versions = registry_versions.copy()
    versions["version_seq"] = pd.to_numeric(versions["version_seq"], errors="coerce")
    versions["release_ts"] = versions["release_date"].map(_parse_time)
    result["version_seq"] = pd.to_numeric(result["version_seq"], errors="coerce")
    result["first_ts"] = result["first_commit_date"].map(_parse_time)
    result["row_id"] = np.arange(len(result))
    _fill_exact_origin(result, versions)
    other = ~result["link"].isin(_EXACT_LINKS)
    undated = other & result["first_ts"].isna()
    result.loc[undated, "origin_basis"] = "no_date"
    result.loc[undated, "copied_before_release"] = False
    dated = result.loc[other & result["first_ts"].notna()].copy()
    eligible = _eligible_versions(dated, versions)
    chosen = _pick_similar_origin(eligible, sigs)
    chosen_rows: set[int] = set()
    if not chosen.empty:
        picked = chosen.set_index("row_id")
        chosen_rows = {int(row) for row in picked.index}
        result = result.set_index("row_id")
        result.loc[picked.index, "origin_seq"] = picked["version_seq"].to_numpy()
        origin_dates = picked["release_date"].map(_format_time).to_numpy()
        result.loc[picked.index, "origin_date"] = origin_dates
        result.loc[picked.index, "origin_basis"] = "similar_before_date"
        result.loc[picked.index, "copied_before_release"] = False
        result = result.reset_index()
    missed = other & result["first_ts"].notna() & ~result["row_id"].isin(chosen_rows)
    dated_lineages = set(versions.loc[versions["release_ts"].notna(), "lineage_id"].astype(str))
    has_dated_release = result["lineage_id"].astype(str).isin(dated_lineages)
    result.loc[missed & has_dated_release, "copied_before_release"] = True
    no_release_date = missed & ~has_dated_release
    result.loc[no_release_date, "copied_before_release"] = False
    result.loc[no_release_date, "origin_basis"] = "no_date"
    return result.drop(columns=["first_ts", "row_id"])


def _prepare_copy_files_for_families(copy_files: pd.DataFrame) -> pd.DataFrame:
    """Add the rank columns the family queries sort on."""
    frame = copy_files.copy()
    if frame.empty:
        frame = _empty_frame(
            [
                "repo",
                "path",
                "file_sha",
                "lineage_id",
                "root_lineage",
                "link",
                "link_rank",
                "source_is_hub",
                "release_date",
                "is_fork",
                "same_owner_as_registry",
            ]
        )
        return frame
    if "root_lineage" not in frame.columns:
        frame["root_lineage"] = frame["lineage_id"]
    frame["root_lineage"] = frame["root_lineage"].fillna(frame["lineage_id"])
    frame["link_rank"] = frame["link"].map(LINK_RANK).fillna(99).astype(int)
    if "source_is_hub" not in frame.columns:
        frame["source_is_hub"] = False
    frame["source_is_hub"] = frame["source_is_hub"].map(_as_bool).astype(int)
    if "release_date" not in frame.columns:
        frame["release_date"] = None
    frame["release_date"] = frame["release_date"].map(
        lambda value: None if _format_time(value) is None else str(value)
    )
    if "is_fork" not in frame.columns:
        frame["is_fork"] = False
    if "same_owner_as_registry" not in frame.columns:
        frame["same_owner_as_registry"] = False
    frame["is_fork"] = frame["is_fork"].map(_as_bool)
    frame["same_owner_as_registry"] = frame["same_owner_as_registry"].map(_as_bool)
    return frame


def _create_temp(con: duckdb.DuckDBPyConnection, name: str, sql: str) -> None:
    """Store ``sql`` as a temporary table named ``name``."""
    con.execute(f"CREATE OR REPLACE TEMP TABLE {name} AS {sql}")


# Algorithm A34, see CODE_FLOW.md
def build_text_families(
    con: duckdb.DuckDBPyConnection,
    copy_files: pd.DataFrame,
    dev: bool,
) -> pd.DataFrame:
    """Give every English text a family id and a family kind.

    A text whose files link to a lineage uses that root lineage. The kind is
    ``hub_only`` when the root registry has ``is_hub``, otherwise ``source``.
    A text that exists only inside registry repos is ``registry_self`` and
    keeps its own lineage. An unlinked text in an ``s2_clusters`` group of
    two or more English texts at threshold 0.80 is ``cluster:<id>`` and
    ``independent``. Anything left is ``single:<file_sha>`` and ``singleton``.
    ``files``, ``repos``, and ``owners`` are family totals, repeated on each
    text. The "without forks" pair also drops same-owner copies.

    Inputs: a connection that already has the input tables, or ``dev`` so
    this function can register them. ``copy_files`` is one row per copy.
    Outputs: one row per English text.

    Example:
    >>> con = connect_duckdb()
    >>> _ = con.execute("CREATE TABLE skill_language (file_sha VARCHAR, is_english BOOLEAN)")
    >>> _ = con.execute("INSERT INTO skill_language VALUES ('sha-lone', true)")
    >>> _ = con.execute(
    ...     "CREATE TABLE artifacts (repo_full_name VARCHAR, path VARCHAR, file_sha VARCHAR)"
    ... )
    >>> _ = con.execute("INSERT INTO artifacts VALUES ('f/lone', 'y/SKILL.md', 'sha-lone')")
    >>> _ = con.execute("CREATE TABLE repos (full_name VARCHAR, owner VARCHAR, is_fork INTEGER)")
    >>> _ = con.execute("INSERT INTO repos VALUES ('f/lone', 'f', 0)")
    >>> _ = con.execute("CREATE TABLE repo_roles (repo VARCHAR, is_registry BOOLEAN)")
    >>> _ = con.execute(
    ...     "CREATE TABLE lineage_versions (lineage_id VARCHAR, version_seq INTEGER, "
    ...     "blob VARCHAR, release_date VARCHAR)"
    ... )
    >>> _ = con.execute("CREATE TABLE lineage_roots (lineage_id VARCHAR, root_lineage VARCHAR)")
    >>> _ = con.execute("CREATE TABLE lineages (lineage_id VARCHAR, repo VARCHAR)")
    >>> _ = con.execute("CREATE TABLE registry_hubs (repo VARCHAR, is_hub BOOLEAN)")
    >>> _ = con.execute(
    ...     "CREATE TABLE clusters (file_sha VARCHAR, cluster_id INTEGER, threshold DOUBLE)"
    ... )
    >>> row = build_text_families(con, pd.DataFrame(), True).iloc[0]
    >>> row.family_kind
    'singleton'
    """
    if not _relation_exists(con, "artifacts"):
        read_inputs(con, dev)
    prepared = _prepare_copy_files_for_families(copy_files)
    con.register("copy_files_input", prepared)
    con.execute("CREATE OR REPLACE TEMP TABLE copy_files AS SELECT * FROM copy_files_input")
    _create_temp(con, "best_text_link", BEST_TEXT_LINK_SQL)
    _create_temp(con, "registry_self_text", REGISTRY_SELF_SQL)
    _create_temp(
        con,
        "english_cluster",
        ENGLISH_CLUSTERS_SQL.format(threshold=settings.CLUSTER_LINK_THRESHOLD),
    )
    _create_temp(con, "assigned", ASSIGN_FAMILIES_SQL)
    _create_temp(con, "copy_family_counts", COPY_FAMILY_COUNTS_SQL)
    _create_temp(con, "loose_family_counts", LOOSE_FAMILY_COUNTS_SQL)
    families = _fetch_frame(con, FAMILY_WITH_COUNTS_SQL)
    if families.empty:
        return _empty_frame(_FAMILY_COLUMNS)
    families["registry_self"] = families["registry_self"].map(_as_bool)
    for name in ("files", "repos", "owners", "files_without_forks", "repos_without_forks"):
        families[name] = pd.to_numeric(families[name], errors="coerce").fillna(0).astype(int)
    return families[_FAMILY_COLUMNS]


def _text_link_rows(candidates: pd.DataFrame, versions: pd.DataFrame) -> pd.DataFrame:
    """Turn stage 5 copy rows into link candidates. Similarity follows the link type."""
    if candidates.empty:
        return _empty_links()
    frame = candidates.copy()
    lookup = versions[["lineage_id", "version_seq", "release_date", "is_hub"]].drop_duplicates(
        ["lineage_id", "version_seq"]
    )
    frame["version_seq"] = pd.to_numeric(frame["version_seq"], errors="coerce")
    frame = frame.merge(
        lookup,
        on=["lineage_id", "version_seq"],
        how="left",
        suffixes=("", "_version"),
    )
    similarity = np.select(
        [
            frame["link"].eq("exact"),
            frame["link"].eq("near"),
            frame["link"].eq("near_contained"),
        ],
        [
            1.0,
            pd.to_numeric(frame["jaccard"], errors="coerce"),
            pd.to_numeric(frame["containment"], errors="coerce"),
        ],
        default=pd.to_numeric(frame["similarity_verified"], errors="coerce"),
    )
    frame["link_similarity"] = similarity
    frame["history_source"] = "none"
    if "is_hub" not in frame.columns:
        frame["is_hub"] = False
    return frame


def _load_lineage_tables(dev: bool) -> dict[str, pd.DataFrame]:
    """Read the small lineage, hub, role, and copy tables for this mode."""
    folder = settings.data_dir(dev)
    versions = read_parquet(folder / "lineage_versions.parquet")
    lineages = read_parquet(folder / "lineages.parquet")
    roots = read_parquet(folder / "lineage_roots.parquet")
    hubs = read_parquet(folder / "registry_hubs.parquet")
    roles = read_parquet(folder / "repo_roles_final.parquet")
    candidates = read_parquet(folder / "copy_candidates.parquet")
    versions = versions.merge(lineages[["lineage_id", "repo"]], on="lineage_id", how="left")
    versions = versions.merge(hubs[["repo", "is_hub"]], on="repo", how="left")
    versions["is_hub"] = versions["is_hub"].map(_as_bool)
    return {
        "versions": versions,
        "lineages": lineages,
        "roots": roots,
        "roles": roles,
        "candidates": candidates,
    }


def _files_blocked_from_history_near(
    candidates: pd.DataFrame,
    history_exact: pd.DataFrame,
) -> pd.DataFrame:
    """Files whose text link is already stronger than history_near, plus history_exact."""
    blocked = candidates.loc[candidates["link"].isin(_STRONGER_THAN_HISTORY_NEAR), ["repo", "path"]]
    if not history_exact.empty:
        blocked = pd.concat([blocked, history_exact[["repo", "path"]]], ignore_index=True)
    return blocked.drop_duplicates()


def _unlinked_history_files(histories: pd.DataFrame, blocked: pd.DataFrame) -> pd.DataFrame:
    """History files that are not in ``blocked``."""
    files = histories[["repo", "path"]].drop_duplicates()
    if blocked.empty:
        return files
    marked = files.merge(blocked.assign(_blocked=True), on=["repo", "path"], how="left")
    return marked.loc[marked["_blocked"].isna(), ["repo", "path"]]


def _attach_blob_text(
    con: duckdb.DuckDBPyConnection,
    histories: pd.DataFrame,
    first: pd.DataFrame,
) -> pd.DataFrame:
    """Copy blob text onto the earliest history row of each unlinked file."""
    if first.empty:
        histories = histories.copy()
        histories["text"] = ""
        return histories
    needed = pd.DataFrame({"blob": first["blob"].dropna().astype(str).unique()})
    con.register("needed_blobs", needed)
    texts = _fetch_frame(con, BLOB_TEXTS_SQL)
    labeled = first.merge(texts, on="blob", how="left") if not texts.empty else first.copy()
    if "text" not in labeled.columns:
        labeled["text"] = ""
    labeled = labeled[["repo", "path", "version_seq", "blob", "text"]]
    merged = histories.merge(labeled, on=["repo", "path", "version_seq", "blob"], how="left")
    merged["text"] = merged["text"].fillna("")
    return merged


def _enrich_winners(
    winners: pd.DataFrame,
    tables: dict[str, pd.DataFrame],
    histories: pd.DataFrame,
    con: duckdb.DuckDBPyConnection,
) -> pd.DataFrame:
    """Add root lineage, flags, role, and the first commit date."""
    frame = winners.copy()
    roots = tables["roots"]
    root_map = (
        roots.set_index("lineage_id")["root_lineage"].to_dict() if not roots.empty else {}
    )
    frame["root_lineage"] = frame["lineage_id"].map(lambda lineage: root_map.get(lineage, lineage))
    lineages = tables["lineages"][["lineage_id", "repo"]].drop_duplicates("lineage_id")
    lineage_repo = lineages.set_index("lineage_id")["repo"].to_dict()
    frame["same_owner_as_registry"] = [
        _owner_of(repo) != "" and _owner_of(repo) == _owner_of(lineage_repo.get(lineage, ""))
        for repo, lineage in zip(frame["repo"], frame["lineage_id"], strict=True)
    ]
    roles = tables["roles"][["repo", "is_fork", "repo_role"]].drop_duplicates("repo")
    frame = frame.drop(columns=[name for name in ("is_fork", "repo_role") if name in frame.columns])
    frame = frame.merge(roles, on="repo", how="left")
    frame["is_fork"] = frame["is_fork"].map(_as_bool)
    frame["source_is_hub"] = frame["is_hub"].map(_as_bool) if "is_hub" in frame.columns else False
    history_first = (
        histories.assign(_ts=histories["commit_date"].map(_parse_time))
        .groupby(["repo", "path"], sort=False)["_ts"]
        .min()
        .reset_index()
        .rename(columns={"_ts": "history_first"})
    )
    frame = frame.merge(history_first, on=["repo", "path"], how="left")
    keys = frame[["repo", "path"]].drop_duplicates()
    con.register("file_keys", keys)
    dates = _fetch_frame(con, FILE_FIRST_COMMIT_SQL)
    if not dates.empty:
        frame = frame.merge(dates, on=["repo", "path"], how="left")
    else:
        frame["first_commit_at"] = None
    frame["first_commit_date"] = [
        _format_time(history) or _format_time(dataset)
        for history, dataset in zip(frame["history_first"], frame["first_commit_at"], strict=True)
    ]
    return frame


def _combine_signatures(
    dev: bool,
    versions: pd.DataFrame,
    file_shas: set[str],
) -> tuple[dict[str, object], dict[str, object]]:
    """Load registry signatures, plus dataset signatures for the copy texts."""
    folder = settings.data_dir(dev)
    ids, matrix, sizes = load_signatures(folder / "registry_signatures.npz")
    meta_rows = []
    version_lookup = {
        (str(row.lineage_id), int(row.version_seq)): row
        for row in versions.itertuples(index=False)
        if pd.notna(row.version_seq)
    }
    for name in ids:
        lineage, seq_text = name.rsplit("#", 1)
        row = version_lookup.get((lineage, int(seq_text)))
        meta_rows.append(
            {
                "version_id": name,
                "lineage_id": lineage,
                "version_seq": int(seq_text),
                "release_date": None if row is None else row.release_date,
                "is_hub": False if row is None else bool(row.is_hub),
            }
        )
    registry = {
        "ids": ids,
        "signatures": matrix,
        "sizes": sizes,
        "meta": pd.DataFrame(meta_rows),
    }
    copy_ids, copy_matrix, copy_sizes = _signatures_for_ids(_dataset_dir(), file_shas)
    if not copy_ids:
        return registry, registry
    combined = {
        "ids": list(ids) + copy_ids,
        "signatures": np.vstack([matrix, copy_matrix]),
        "sizes": np.concatenate([sizes, copy_sizes]),
    }
    return registry, combined


def _history_link_counts(copy_files: pd.DataFrame) -> pd.DataFrame:
    """Count winning history links by link type and download source."""
    columns = ["link", "history_source", "files"]
    if copy_files.empty:
        return _empty_frame(columns)
    history = copy_files.loc[copy_files["link"].isin(_HISTORY_LINKS)]
    if history.empty:
        return _empty_frame(columns)
    counts = (
        history.groupby(["link", "history_source"], dropna=False)
        .size()
        .reset_index(name="files")
        .sort_values(["link", "history_source"], kind="mergesort")
        .reset_index(drop=True)
    )
    counts["files"] = counts["files"].astype(int)
    return counts[columns]


def _sha_key(value: object) -> str:
    """Lowercase blob sha, or ``""`` when the cell is empty or the zero sha."""
    if not isinstance(value, str):
        if value is None:
            return ""
        try:
            if pd.isna(value):
                return ""
        except TypeError:
            return ""
        value = str(value)
    text = value.strip().lower()
    if text in {"", "none", "nan", "<na>", _ZERO_BLOB}:
        return ""
    return text


def _empty_proven() -> pd.DataFrame:
    """No history-exact files. The scorer still sees its columns."""
    columns = ["repo", "path", "current_text_sha", "lineage_id", "file_sha", "is_fork"]
    return _empty_frame(columns)


def _newest_history_blob(histories: pd.DataFrame) -> pd.DataFrame:
    """One blob per file: latest real commit date, then highest version_seq.

    A missing date sorts before a real date, so an undated row does not
    beat a dated one. Within one date, the higher ``version_seq`` wins.

    Example:
    >>> rows = pd.DataFrame([
    ...     {"repo": "a/r", "path": "p", "blob": "old",
    ...      "commit_date": "2026-01-01T00:00:00Z", "version_seq": 5},
    ...     {"repo": "a/r", "path": "p", "blob": "new",
    ...      "commit_date": None, "version_seq": 9},
    ... ])
    >>> _newest_history_blob(rows)["current_text_sha"].iloc[0]
    'old'
    """
    columns = ["repo", "path", "current_text_sha"]
    if histories.empty:
        return _empty_frame(columns)
    needed = ("repo", "path", "blob", "commit_date", "version_seq")
    _require_columns(histories, needed, "histories")
    frame = histories.copy()
    frame["_ts"] = frame["commit_date"].map(_parse_time)
    frame["_missing"] = frame["_ts"].isna()
    frame["_seq"] = pd.to_numeric(frame["version_seq"], errors="coerce").fillna(0)
    frame = frame.sort_values(
        ["repo", "path", "_missing", "_ts", "_seq"],
        ascending=[True, True, False, True, True],
        kind="mergesort",
    )
    newest = frame.groupby(["repo", "path"], sort=False).tail(1)
    return newest.rename(columns={"blob": "current_text_sha"})[columns]


def build_proven_pairs(copy_files: pd.DataFrame, histories: pd.DataFrame) -> pd.DataFrame:
    """History-exact files, with the sha of the copy's current text.

    The current text is the newest history row. A file with no history row
    keeps its dataset ``file_sha``. Any other winning link is left out.
    ``is_fork`` stays on the row so recall can be computed without forks.

    Inputs: copy files after the strongest link is chosen, and history rows.
    Outputs: ``repo, path, current_text_sha, lineage_id``, plus ``file_sha``
    and ``is_fork``.

    Example:
    >>> copies = pd.DataFrame([{
    ...     "repo": "a/r", "path": "skills/a/SKILL.md", "file_sha": "now",
    ...     "lineage_id": "reg::skills/a", "link": "history_exact",
    ... }])
    >>> histories = pd.DataFrame([
    ...     {"repo": "a/r", "path": "skills/a/SKILL.md", "blob": "old",
    ...      "commit_date": "2026-01-01T00:00:00Z", "version_seq": 1},
    ...     {"repo": "a/r", "path": "skills/a/SKILL.md", "blob": "new",
    ...      "commit_date": "2026-06-01T00:00:00Z", "version_seq": 2},
    ... ])
    >>> build_proven_pairs(copies, histories)["current_text_sha"].iloc[0]
    'new'
    """
    _require_columns(copy_files, ("repo", "path", "lineage_id", "link"), "copy_files")
    proven = copy_files.loc[copy_files["link"].astype(str).eq("history_exact")].copy()
    if proven.empty:
        return _empty_proven()
    proven = proven.merge(_newest_history_blob(histories), on=["repo", "path"], how="left")
    if "file_sha" not in proven.columns:
        proven["file_sha"] = pd.NA
    missing = proven["current_text_sha"].map(_sha_key).eq("")
    proven.loc[missing, "current_text_sha"] = proven.loc[missing, "file_sha"]
    if "is_fork" not in proven.columns:
        proven["is_fork"] = False
    else:
        proven["is_fork"] = proven["is_fork"].map(_as_bool)
    proven = proven.drop_duplicates(["repo", "path"], keep="first")
    columns = ["repo", "path", "current_text_sha", "lineage_id", "file_sha", "is_fork"]
    ordered = proven[columns].sort_values(["repo", "path"], kind="mergesort")
    return ordered.reset_index(drop=True)


def _sql_sha_list(shas: list[str]) -> str:
    """Comma-separated quoted hex shas. Anything else is left out of the SQL."""
    quoted = []
    for sha in shas:
        if sha and all(character in "0123456789abcdef" for character in sha):
            quoted.append("'" + sha + "'")
    return ", ".join(quoted)


def _texts_from_frame(frame: pd.DataFrame) -> dict[str, str]:
    """Map a lowercase sha to its text. A non-string cell becomes ``""``."""
    found: dict[str, str] = {}
    if frame.empty or "blob" not in frame.columns or "text" not in frame.columns:
        return found
    for blob, text in zip(frame["blob"].tolist(), frame["text"].tolist(), strict=True):
        key = _sha_key(blob)
        if key:
            found[key] = text if isinstance(text, str) else ""
    return found


def _read_text_batches(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    path: str | Path,
    shas: list[str],
) -> dict[str, str]:
    """Read texts for ``shas`` in batches. ``sql`` has ``{path}`` and ``{sha_list}``."""
    found: dict[str, str] = {}
    unique = list(dict.fromkeys(sha for sha in shas if sha))
    for start in range(0, len(unique), _TEXT_SHA_BATCH):
        sha_list = _sql_sha_list(unique[start : start + _TEXT_SHA_BATCH])
        if not sha_list:
            continue
        query = sql.format(path=_quote_path(str(path)), sha_list=sha_list)
        found.update(_texts_from_frame(_fetch_frame(con, query)))
    return found


def _load_missing_texts(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    path: str | Path,
    shas: list[str],
    found: dict[str, str],
) -> None:
    """Add texts for shas that are not already in ``found``."""
    missing = [sha for sha in shas if sha and sha not in found]
    found.update(_read_text_batches(con, sql, path, missing))


def _version_shas(pairs: pd.DataFrame, versions: pd.DataFrame) -> list[str]:
    """Blobs of the lineages these proven pairs point at."""
    if pairs.empty or versions.empty or "blob" not in versions.columns:
        return []
    lineages = set(pairs["lineage_id"].dropna().astype(str))
    chosen = versions.loc[versions["lineage_id"].astype(str).isin(lineages)]
    return [_sha_key(value) for value in chosen["blob"].tolist()]


def _copy_shas(pairs: pd.DataFrame) -> list[str]:
    """Current history shas, plus the dataset sha used when history text is missing."""
    shas: list[str] = []
    for column in ("current_text_sha", "file_sha"):
        if column in pairs.columns:
            shas.extend(_sha_key(value) for value in pairs[column].tolist())
    return shas


def _prefer_available_text(pairs: pd.DataFrame, texts: dict[str, str]) -> pd.DataFrame:
    """Use ``file_sha`` when the newest history blob has no stored text."""
    frame = pairs.copy()
    have = frame["current_text_sha"].map(lambda value: _sha_key(value) in texts)
    if "file_sha" not in frame.columns:
        return frame
    file_keys = frame["file_sha"].map(_sha_key)
    use_file = ~have & file_keys.isin(texts) & file_keys.ne("")
    frame.loc[use_file, "current_text_sha"] = frame.loc[use_file, "file_sha"].astype(str)
    return frame


def _load_pair_texts(
    con: duckdb.DuckDBPyConnection,
    pairs: pd.DataFrame,
    versions: pd.DataFrame,
    dev: bool,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Load the current copy text and every version of the linked lineage.

    Registry history is tried first for a version blob, then the dataset.
    A copy blob is taken from stage 6, then wild history, then the dataset.
    The sha on the returned pairs is the text that will be scored.

    Inputs: an open connection, proven pairs, lineage versions, and ``dev``.
    Outputs: the pairs, and a map of lowercase sha to raw text.
    """
    if pairs.empty:
        return pairs, {}
    texts: dict[str, str] = {}
    version_shas = _version_shas(pairs, versions)
    _load_missing_texts(
        con, PROVEN_REGISTRY_TEXTS_SQL, _registry_texts_path(dev), version_shas, texts
    )
    _load_missing_texts(
        con, PROVEN_DATASET_TEXTS_SQL, settings.ARTIFACTS_GLOB, version_shas, texts
    )
    copy_shas = _copy_shas(pairs)
    copied_texts = _copied_history_dir(dev) / "copied_skill_history_texts.parquet"
    _load_missing_texts(con, PROVEN_HISTORY_TEXTS_SQL, copied_texts, copy_shas, texts)
    wild_blobs = settings.WILD_HISTORY_DIR / "blobs.parquet"
    _load_missing_texts(con, PROVEN_WILD_TEXTS_SQL, wild_blobs, copy_shas, texts)
    _load_missing_texts(con, PROVEN_DATASET_TEXTS_SQL, settings.ARTIFACTS_GLOB, copy_shas, texts)
    return _prefer_available_text(pairs, texts), texts


def _text_for_sha(texts: dict[str, str], sha: object) -> str | None:
    """The stored text, or None when this sha was not loaded."""
    key = _sha_key(sha)
    if key == "" or key not in texts:
        return None
    return texts[key]


def _lineage_text_lists(versions: pd.DataFrame, texts: dict[str, str]) -> dict[str, list[str]]:
    """Registry texts per lineage, oldest ``version_seq`` first. Missing blobs are skipped."""
    if versions.empty or "lineage_id" not in versions.columns or "blob" not in versions.columns:
        return {}
    frame = versions
    if "version_seq" in frame.columns:
        frame = frame.sort_values("version_seq", kind="mergesort")
    grouped: dict[str, list[str]] = {}
    for lineage, blob in zip(frame["lineage_id"], frame["blob"], strict=True):
        text = _text_for_sha(texts, blob)
        if text is None:
            continue
        grouped.setdefault(str(lineage), []).append(text)
    return grouped


def _body_signature(
    text: str,
    boilerplate: set[str],
    cache: dict[str, tuple[np.ndarray, int]],
) -> tuple[np.ndarray, int]:
    """MinHash values and the shingle count. A repeated body is not signed again."""
    found = cache.get(text)
    if found is None:
        found = signature_for_text(text, boilerplate)
        cache[text] = found
    return found


def _estimated_scores(
    registry_text: str,
    copy_text: str,
    boilerplate: set[str],
    cache: dict[str, tuple[np.ndarray, int]],
) -> tuple[float, float]:
    """Estimated Jaccard and containment. A body with no shingles scores 0.

    Containment is ``estimate_containment`` of that Jaccard and the two
    shingle counts, the same estimate stage 5 stores on a near link.
    """
    registry_values, registry_size = _body_signature(registry_text, boilerplate, cache)
    copy_values, copy_size = _body_signature(copy_text, boilerplate, cache)
    if registry_size == 0 or copy_size == 0:
        return 0.0, 0.0
    jaccard = estimate_jaccard(
        minhash_from_array(registry_values),
        minhash_from_array(copy_values),
    )
    return jaccard, estimate_containment(jaccard, registry_size, copy_size)


def _passes_one_version(
    registry_text: str,
    copy_text: str,
    boilerplate: set[str],
    cache: dict[str, tuple[np.ndarray, int]],
) -> dict[str, bool]:
    """One registry version against the copy. Thresholds are the stage 5 cutoffs."""
    same = normalize_text(registry_text) == normalize_text(copy_text)
    jaccard, containment = _estimated_scores(registry_text, copy_text, boilerplate, cache)
    kept = share_of_registry_lines_kept(registry_text, copy_text)
    return {
        "exact": same,
        "minhash_jaccard": jaccard >= settings.NEAR_COPY_MIN_JACCARD,
        "containment": containment >= settings.NEAR_COPY_MIN_CONTAINMENT,
        "line_check": kept >= settings.MIN_VERIFIED_LINE_SHARE,
    }


def _score_against_versions(
    registry_texts: list[str],
    copy_text: str | None,
    cache: dict[str, tuple[np.ndarray, int]],
) -> dict[str, bool]:
    """True when any version passes. A copy sha with no stored text passes nothing."""
    found = {name: False for name, _column in _TEXT_METHOD_FLAGS}
    if copy_text is None:
        found["any_text_method"] = False
        return found
    boilerplate = _boilerplate_lines()
    for registry_text in registry_texts:
        hits = _passes_one_version(registry_text, copy_text, boilerplate, cache)
        for name, _column in _TEXT_METHOD_FLAGS:
            found[name] = found[name] or hits[name]
        if all(found[name] for name, _column in _TEXT_METHOD_FLAGS):
            break
    found["any_text_method"] = any(found[name] for name, _column in _TEXT_METHOD_FLAGS)
    return found


# Algorithm A35, see CODE_FLOW.md
def score_methods_on_pair(registry_texts: list[str], copy_text: str) -> dict[str, bool]:
    """True when any lineage version passes that text method.

    ``exact`` is the same normalised text. ``minhash_jaccard`` is estimated
    Jaccard at least ``NEAR_COPY_MIN_JACCARD``. ``containment`` is estimated
    containment at least ``NEAR_COPY_MIN_CONTAINMENT``. ``line_check`` keeps
    at least ``MIN_VERIFIED_LINE_SHARE`` of the registry body lines.
    ``any_text_method`` is true when one of those four is true. An empty
    version list finds nothing.

    Example:
    >>> score_methods_on_pair(["alpha beta"], "alpha beta")["exact"]
    True
    >>> score_methods_on_pair(["alpha beta"], "other words")["exact"]
    False
    >>> score_methods_on_pair([], "alpha beta")["any_text_method"]
    False
    """
    return _score_against_versions(registry_texts, copy_text, {})


def _score_proven_pairs(
    pairs: pd.DataFrame,
    versions: pd.DataFrame,
    texts: dict[str, str],
) -> pd.DataFrame:
    """Add the four method flags. One signature cache is shared across the pairs."""
    frame = pairs.copy()
    for name in _PROVEN_FLAGS:
        frame[name] = False
    if frame.empty:
        return frame
    grouped = _lineage_text_lists(versions, texts)
    cache: dict[str, tuple[np.ndarray, int]] = {}
    flags = [
        _score_against_versions(
            grouped.get(str(lineage), []),
            _text_for_sha(texts, sha),
            cache,
        )
        for lineage, sha in zip(frame["lineage_id"], frame["current_text_sha"], strict=True)
    ]
    frame["exact"] = [row["exact"] for row in flags]
    frame["jaccard_found"] = [row["minhash_jaccard"] for row in flags]
    frame["containment_found"] = [row["containment"] for row in flags]
    frame["verified_found"] = [row["line_check"] for row in flags]
    return frame


def _flag_series(frame: pd.DataFrame, column: str) -> pd.Series:
    """Boolean column, or False on every row when the column is absent."""
    if column not in frame.columns:
        return pd.Series(False, index=frame.index, dtype=bool)
    return frame[column].map(_as_bool).astype(bool)


def _recall_row(method: str, hits: pd.Series, forks: pd.Series) -> dict[str, object]:
    """``found / proven_pairs``, and the same ratio after forks are removed."""
    total = int(len(hits))
    found = int(hits.sum()) if total else 0
    kept = ~forks
    kept_total = int(kept.sum()) if total else 0
    kept_found = int(hits.loc[kept].sum()) if kept_total else 0
    return {
        "method": method,
        "proven_pairs": total,
        "found": found,
        "recall": found / total if total else 0.0,
        "recall_without_forks": kept_found / kept_total if kept_total else 0.0,
    }


def _tfidf_hits(frame: pd.DataFrame) -> pd.Series | None:
    """True when a ``tfidf`` column is at least 0.80. None when G30 columns are absent.

    A boolean column counts as 1 or 0. G30 has not named a cutoff, so a
    numeric score uses ``NEAR_COPY_MIN_JACCARD``.
    """
    columns = [name for name in frame.columns if str(name).startswith("tfidf")]
    if not columns:
        return None
    if frame.empty:
        return pd.Series(False, index=frame.index, dtype=bool)
    numbers = frame[columns].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    return numbers.max(axis=1) >= settings.NEAR_COPY_MIN_JACCARD


def _recall_from_flags(frame: pd.DataFrame) -> pd.DataFrame:
    """One recall row per method, in the order the report prints them."""
    forks = _flag_series(frame, "is_fork")
    rows = []
    any_hit = pd.Series(False, index=frame.index, dtype=bool)
    for _method, column in _TEXT_METHOD_FLAGS:
        hits = _flag_series(frame, column)
        any_hit = any_hit | hits
        rows.append(_recall_row(_method, hits, forks))
    rows.append(_recall_row("any_text_method", any_hit, forks))
    tfidf = _tfidf_hits(frame)
    if tfidf is not None:
        rows.append(_recall_row("tfidf", tfidf, forks))
    return pd.DataFrame(rows, columns=_RECALL_COLUMNS)


def measure_link_method_recall(
    pairs: pd.DataFrame,
    versions: pd.DataFrame | None = None,
    texts: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Recall of each text method on history-proven pairs.

    ``recall`` is ``found / proven_pairs``. ``recall_without_forks`` drops
    ``is_fork`` rows from both counts, because a fork shares the registry
    history and makes the test set look larger. Pass ``versions``
    (``lineage_id``, ``blob``) and ``texts`` (sha to raw text) to score the
    pairs. A frame that already has the flag columns is counted as it is.
    ``tfidf`` is added only when a column name starts with ``tfidf``.

    Example:
    >>> pairs = pd.DataFrame([{
    ...     "exact": True, "jaccard_found": True, "containment_found": True,
    ...     "verified_found": True, "is_fork": False,
    ... }])
    >>> row = measure_link_method_recall(pairs).iloc[0]
    >>> row.method == "exact" and int(row.found) == 1 and float(row.recall) == 1.0
    True
    """
    if (versions is None) != (texts is None):
        raise ValueError("versions and texts are passed together")
    scored = pairs
    needs_score = "jaccard_found" not in pairs.columns
    if versions is not None and texts is not None and needs_score:
        scored = _score_proven_pairs(pairs, versions, texts)
    elif needs_score and not pairs.empty:
        raise ValueError("pairs need method columns, or versions and texts")
    return _recall_from_flags(scored)


def _public_copy_files(frame: pd.DataFrame) -> pd.DataFrame:
    """Return ``copy_files`` with the output columns only."""
    result = frame.copy()
    for name in _COPY_COLUMNS:
        if name not in result.columns:
            result[name] = pd.NA
    result["origin_seq"] = pd.to_numeric(result["origin_seq"], errors="coerce").astype("Int64")
    result["copied_before_release"] = result["copied_before_release"].map(_as_bool)
    result["is_fork"] = result["is_fork"].map(_as_bool)
    result["same_owner_as_registry"] = result["same_owner_as_registry"].map(_as_bool)
    result["link_similarity"] = pd.to_numeric(result["link_similarity"], errors="coerce")
    return result[_COPY_COLUMNS]


def _public_proven(frame: pd.DataFrame) -> pd.DataFrame:
    """Return proven pairs with the output columns only."""
    result = frame.copy()
    if "current_file_sha" not in result.columns:
        if "current_text_sha" in result.columns:
            result["current_file_sha"] = result["current_text_sha"]
        else:
            result["current_file_sha"] = pd.NA
    for name in _PROVEN_COLUMNS:
        if name not in result.columns:
            result[name] = False if name in _PROVEN_FLAGS else pd.NA
    for name in _PROVEN_FLAGS:
        result[name] = result[name].map(_as_bool)
    return result.loc[:, list(_PROVEN_COLUMNS)]


def _public_recall(frame: pd.DataFrame) -> pd.DataFrame:
    """Return the recall table with its five columns, in method order."""
    result = frame.copy()
    for name in _RECALL_COLUMNS:
        if name not in result.columns:
            result[name] = 0
    result["proven_pairs"] = pd.to_numeric(result["proven_pairs"], errors="coerce").fillna(0)
    result["proven_pairs"] = result["proven_pairs"].astype(int)
    result["found"] = pd.to_numeric(result["found"], errors="coerce").fillna(0).astype(int)
    return result.loc[:, list(_RECALL_COLUMNS)]


def save_outputs(
    copy_files: pd.DataFrame,
    families: pd.DataFrame,
    history_counts: pd.DataFrame,
    proven: pd.DataFrame,
    recall: pd.DataFrame,
    dev: bool,
) -> None:
    """Write the five stage 7 tables. Family rows are sorted so a rerun matches.

    Inputs: copy files, families, history-link counts, proven pairs, and recall.
    Outputs: parquet files under ``data_dir(dev)``. Recall stays in method order.
    """
    copy_path, family_path, count_path, proven_path, recall_path = _output_paths(dev)
    write_parquet(_public_copy_files(copy_files), copy_path, ["repo", "path"])
    write_parquet(families[_FAMILY_COLUMNS], family_path, ["file_sha"])
    write_parquet(history_counts, count_path, ["link", "history_source"])
    write_parquet(_public_proven(proven), proven_path, ["repo", "path"])
    write_parquet(_public_recall(recall), recall_path)


def _family_kind_numbers(families: pd.DataFrame) -> dict[str, int]:
    """Text counts, and file counts taken once per family."""
    numbers: dict[str, int] = {}
    if families.empty:
        return numbers
    one = families.drop_duplicates("family_id")
    for kind in ("source", "hub_only", "independent", "singleton"):
        texts = int(families["family_kind"].eq(kind).sum())
        files = int(one.loc[one["family_kind"].eq(kind), "files"].sum())
        numbers[f"{kind}_texts"] = texts
        numbers[f"{kind}_files"] = files
    return numbers


def _pairs_no_method_finds(proven: pd.DataFrame) -> pd.DataFrame:
    """Proven pairs where exact, Jaccard, containment and the line check all miss."""
    if proven.empty:
        return proven
    missed = pd.Series(True, index=proven.index)
    for name in _PROVEN_FLAGS:
        if name in proven.columns:
            missed = missed & ~proven[name].map(_as_bool)
    if "repo" in proven.columns and "path" in proven.columns:
        return proven.loc[missed].sort_values(["repo", "path"], kind="mergesort")
    return proven.loc[missed]


def _summary_numbers(
    copy_files: pd.DataFrame,
    families: pd.DataFrame,
    history_counts: pd.DataFrame,
    histories: pd.DataFrame,
    proven: pd.DataFrame,
    seconds: float,
) -> dict[str, object]:
    """The counts the stage prints. Family file totals are not summed per text."""
    numbers: dict[str, object] = {"seconds": round(seconds, 1)}
    numbers.update(_family_kind_numbers(families))
    if not copy_files.empty:
        for link, count in copy_files["link"].value_counts().sort_index().items():
            numbers[f"files_{link}"] = int(count)
        non_exact = ~copy_files["link"].isin(_EXACT_LINKS)
        no_date = copy_files["origin_basis"].eq("no_date")
        non_exact_count = int(non_exact.sum())
        numbers["copy_files"] = int(len(copy_files))
        numbers["copied_before_release"] = int(copy_files["copied_before_release"].sum())
        numbers["no_date_files"] = int(no_date.sum())
        numbers["no_date_share_of_non_exact"] = (
            round(int((no_date & non_exact).sum()) / non_exact_count, 4) if non_exact_count else 0.0
        )
        numbers["forks_share"] = round(float(copy_files["is_fork"].mean()), 4)
    else:
        numbers["copy_files"] = 0
    if not history_counts.empty:
        for record in history_counts.itertuples(index=False):
            numbers[f"history_{record.link}_{record.history_source}"] = int(record.files)
    if not histories.empty:
        numbers["history_rows"] = int(len(histories))
        numbers["inherited_history_rows"] = int(histories["inherited"].sum())
    numbers["proven_pairs"] = int(len(proven))
    numbers["proven_missed_by_text"] = int(len(_pairs_no_method_finds(proven)))
    return numbers


def _print_recall(recall: pd.DataFrame, proven: pd.DataFrame) -> None:
    """Print the recall table and two proven pairs that no text method finds."""
    print("link method recall")
    if recall.empty:
        print("proven_pairs: 0")
    else:
        for record in recall.itertuples(index=False):
            print(
                f"{record.method}: found {int(record.found)} of {int(record.proven_pairs)}"
                f" recall {float(record.recall):.4f}"
                f" without_forks {float(record.recall_without_forks):.4f}"
            )
    missed = _pairs_no_method_finds(proven)
    print(f"proven pairs no text method finds: {len(missed)}")
    print_sample_records(missed, "edited copies text methods miss")


def _print_outputs(
    copy_files: pd.DataFrame,
    families: pd.DataFrame,
    history_counts: pd.DataFrame,
    histories: pd.DataFrame,
    proven: pd.DataFrame,
    recall: pd.DataFrame,
    seconds: float,
) -> None:
    """Print the stage summary, the recall table, and two sample rows of each output."""
    print_summary(
        "assign families",
        _summary_numbers(copy_files, families, history_counts, histories, proven, seconds),
    )
    print_sample_records(copy_files, "copy file sample")
    print_sample_records(families, "family sample")
    print_sample_records(history_counts, "history link count sample")
    _print_recall(recall, proven)
    print_sample_records(proven, "proven pair sample")
    print_sample_records(recall, "recall sample")


def _print_saved_summary(dev: bool) -> None:
    """Reprint the summary from the five files already on disk."""
    saved = [read_parquet(path) for path in _output_paths(dev)]
    _print_outputs(
        saved[0],
        saved[1],
        saved[2],
        _empty_frame(_HISTORY_COLUMNS),
        saved[3],
        saved[4],
        0.0,
    )


def _build_file_links(
    candidates: pd.DataFrame,
    history_exact: pd.DataFrame,
    history_near: pd.DataFrame,
    versions: pd.DataFrame,
) -> pd.DataFrame:
    """Stack text links and history links. One file may appear more than once."""
    text_rows = _text_link_rows(candidates, versions)
    frames = [frame for frame in (text_rows, history_exact, history_near) if not frame.empty]
    if not frames:
        return _empty_links()
    return pd.concat(frames, ignore_index=True)


def main() -> None:
    """Assign families, then measure recall on history-proven copies.

    ``--dev`` reads and writes the dev folder. ``--force`` rebuilds the
    five outputs. A second run without ``--force`` prints ``skip``.
    """
    parser = build_arg_parser("Assign families, history links, and origin versions.")
    args = parser.parse_args()
    dev = bool(args.dev)
    if args.force:
        for path in _output_paths(dev):
            if path.is_file():
                path.unlink()
    if outputs_exist(_output_paths(dev)):
        print("skip: family outputs already exist", flush=True)
        _print_saved_summary(dev)
        return
    started = time.perf_counter()
    temp_dir = settings.data_dir(dev) / "_assign_families_tmp"
    _remove_temp_dir(temp_dir)
    con = connect_duckdb()
    try:
        _prepare_duckdb(con, temp_dir)
        read_inputs(con, dev)
        # Step 1: Read copy histories (wild commit_at, copied SKILL.md).
        print("Step 1: read copy histories", flush=True)
        histories = read_copy_histories(dev)
        print(f"history rows {len(histories)}", flush=True)
        tables = _load_lineage_tables(dev)
        versions = tables["versions"]
        # Step 2: History exact links (algorithm A30).
        print("Step 2: history exact links", flush=True)
        history_exact = find_history_exact_links(histories, versions)
        print(f"history exact files {len(history_exact)}", flush=True)
        # Step 3: History near links (algorithm A31) where a text link is not stronger.
        print("Step 3: history near links", flush=True)
        blocked = _files_blocked_from_history_near(tables["candidates"], history_exact)
        unlinked = _unlinked_history_files(histories, blocked)
        print(f"history near files {len(unlinked)}", flush=True)
        first = _first_versions(histories, unlinked)
        histories = _attach_blob_text(con, histories, first)
        registry_sigs, origin_sigs = _combine_signatures(
            dev,
            versions,
            set(tables["candidates"]["file_sha"].dropna().astype(str)),
        )
        history_near = find_history_near_links(histories, registry_sigs, unlinked)
        # Step 4: Strongest link per file (algorithm A32).
        print("Step 4: strongest link", flush=True)
        file_links = _build_file_links(tables["candidates"], history_exact, history_near, versions)
        winners = pick_strongest_link(file_links)
        winners = _enrich_winners(winners, tables, histories, con)
        # Step 5: Origin version by date (algorithm A33).
        print("Step 5: origin version", flush=True)
        copy_files = choose_origin_version(winners, versions, origin_sigs)
        # Step 6: Text families (algorithm A34, queries Q7.7–Q7.13).
        print("Step 6: text families", flush=True)
        families = build_text_families(con, copy_files, dev)
        history_counts = _history_link_counts(copy_files)
        # Step 9: Proven pairs (algorithm A35, queries Q7.14–Q7.17).
        print("Step 9: proven pairs", flush=True)
        proven_input = build_proven_pairs(copy_files, histories)
        proven_input, pair_texts = _load_pair_texts(con, proven_input, versions, dev)
        proven = _score_proven_pairs(proven_input, versions, pair_texts)
        recall = measure_link_method_recall(proven)
        print(f"proven pairs {len(proven)}", flush=True)
        # Step 7: Save and print counts and two sample records.
        print("Step 7: save outputs", flush=True)
        save_outputs(copy_files, families, history_counts, proven, recall, dev)
    finally:
        con.close()
        _remove_temp_dir(temp_dir)
    saved = [read_parquet(path) for path in _output_paths(dev)]
    _print_outputs(
        saved[0],
        saved[1],
        saved[2],
        histories,
        saved[3],
        saved[4],
        time.perf_counter() - started,
    )


if __name__ == "__main__":
    main()
