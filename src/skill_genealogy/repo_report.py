"""Explain one repository's skills in the terminal.

What: stage 14a builds one record for a repository. A copied skill names
the registry it came from, the repositories on the way, the change at
each of its versions, and whether that copy is stale.
Inputs: English skill files for that repository, ``copy_files``,
``families``, ``family_tree``, ``copy_changes``,
``copy_version_similarity``, ``copy_version_folder_summary``,
``copy_staleness``, ``repo_roles_final`` and ``registry_hubs`` in
``data_dir(dev)``. ``skill_language.parquet`` is the full-data file.
Outputs: text on standard output. ``--save`` also writes
``results_dir(dev)/repo_reports/<owner__repo>.json``.
Run: ``python -m skill_genealogy.repo_report owner/repo --dev``
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path

import pandas as pd

import skill_genealogy.settings as settings
from skill_genealogy.export_tree_viewer import _hub_reason
from skill_genealogy.file_helpers import (
    build_arg_parser,
    check_inputs_exist,
    connect_duckdb,
    outputs_exist,
    print_sample_records,
    print_summary,
)
from skill_genealogy.measure_staleness import classify_copy_state
from skill_genealogy.settings import MAX_CHAIN_STEPS, data_dir, results_dir

_STATE_NAMES = ("unchanged", "cosmetic", "substantive")
_ROLE_BADGES = {
    "registry": "REGISTRY",
    "registry_hub": "REGISTRY · HUB",
    "hub": "HUB",
    "repository": "REPOSITORY",
}
_NOTE_ORDER = ("heading", "name", "script", "command", "network", "permission")
_MISS_LABELS = (
    ("missed_network", "network call"),
    ("missed_commands", "command"),
    ("missed_scripts", "script"),
)
_STATE_WORDS = {
    "up_to_date": "up to date",
    "behind": "behind",
    "stale": "stale",
    "orphaned": "orphaned",
}

# Query Q14.1, see QUERIES.md
ROLE_SQL = """
SELECT
    roles.repo AS repo,
    roles.stars AS stars,
    roles.english_skills AS english_skills,
    roles.is_hub AS is_hub,
    roles.hub_share AS hub_share,
    roles.repo_role AS repo_role,
    roles.hub_evidence AS hub_evidence,
    roles.is_registry AS is_registry,
    hubs.hub_reason AS hub_reason,
    hubs.english_lineages AS english_lineages,
    hubs.rehosted_lineages AS rehosted_lineages
FROM repo_roles_final AS roles
LEFT JOIN registry_hubs AS hubs ON roles.repo = hubs.repo
WHERE roles.repo = ?
"""

# Query Q14.2, see QUERIES.md
SKILLS_SQL = """
SELECT
    artifacts.path AS path,
    any_value(artifacts.file_sha) AS file_sha
FROM artifacts
JOIN skill_language ON artifacts.file_sha = skill_language.file_sha
WHERE artifacts.repo_full_name = ?
  AND skill_language.is_english
  AND artifacts.path IS NOT NULL
GROUP BY artifacts.path
"""

# Query Q14.3, see QUERIES.md
COPY_FILES_SQL = """
SELECT
    repo,
    path,
    file_sha,
    lineage_id,
    link,
    origin_seq,
    cast(origin_date AS VARCHAR) AS origin_date
FROM copy_files
WHERE repo = ?
"""

# Query Q14.4, see QUERIES.md
FAMILIES_SQL = """
SELECT
    families.file_sha AS file_sha,
    families.family_id AS family_id,
    families.family_kind AS family_kind,
    families.registry_self AS registry_self
FROM families
SEMI JOIN skill_shas USING (file_sha)
"""

# Query Q14.5, see QUERIES.md
CHANGES_SQL = """
SELECT
    repo,
    path,
    state,
    body_change_pct,
    customized
FROM copy_changes
WHERE repo = ?
"""

# Query Q14.6, see QUERIES.md
SIMILARITY_SQL = """
SELECT
    repo,
    path,
    version_seq,
    cast(commit_date AS VARCHAR) AS commit_date,
    change_kind,
    prev_lines_added,
    prev_lines_removed,
    prev_body_change_pct,
    origin_jaccard,
    origin_containment,
    origin_verified,
    cast(change_notes AS VARCHAR) AS change_notes
FROM copy_version_similarity
WHERE repo = ?
"""

# Query Q14.7, see QUERIES.md
FOLDER_SQL = """
SELECT
    repo,
    path,
    version_seq,
    target,
    files_added,
    scripts_added,
    new_risky_scripts,
    missing_scripts,
    hidden_scripts
FROM copy_version_folder_summary
WHERE repo = ?
"""

# Query Q14.8, see QUERIES.md
STALENESS_SQL = """
SELECT
    repo,
    path,
    held_seq,
    skill_md_versions_behind,
    days_stale,
    package_versions_behind,
    missed_risky,
    missed_commands,
    missed_network,
    missed_scripts,
    copy_state
FROM copy_staleness
WHERE repo = ?
"""

# Query Q14.9, see QUERIES.md
TREE_FAMILY_SQL = """
SELECT DISTINCT family_id
FROM family_tree
WHERE node_id IN (SELECT node_id FROM start_nodes)
  AND family_id IS NOT NULL
"""

# Query Q14.10, see QUERIES.md
TREE_SQL = """
SELECT
    node_id,
    family_id,
    node_kind,
    lineage_id,
    version_seq,
    first_repo,
    cast(node_date AS VARCHAR) AS node_date,
    parent_id,
    parent_similarity,
    generation
FROM family_tree
WHERE family_id IN (SELECT family_id FROM needed_families)
"""

# Query Q14.11, see QUERIES.md
CHAIN_ROLES_SQL = """
SELECT
    repo,
    repo_role
FROM repo_roles_final
WHERE repo IN (SELECT repo FROM chain_repos)
"""


class UnknownRepoError(LookupError):
    """The repository is not in the role table and has no English skill."""


def recompute_copy_state(
    stored_state: str | None,
    skill_versions_behind: int | None,
    days_stale: int | None,
    package_versions_behind: int | None,
    stale_after_days: int,
) -> str:
    """Return the copy state at ``stale_after_days``. Orphaned does not change.

    Skill-text misses and package misses are added, then stage 10's rule
    (algorithm A47) names the state. A row already marked ``orphaned``
    stays ``orphaned`` at every cutoff.

    Example:
    >>> recompute_copy_state("stale", 2, 161, 0, 400)
    'behind'
    >>> recompute_copy_state("orphaned", 2, 161, 0, 30)
    'orphaned'
    >>> recompute_copy_state("up_to_date", 0, None, 0, 30)
    'up_to_date'
    """
    if _text(stored_state) == "orphaned":
        return "orphaned"
    skill_misses = _as_int(skill_versions_behind) or 0
    package_misses = _as_int(package_versions_behind) or 0
    return classify_copy_state(
        skill_misses + package_misses,
        _as_int(days_stale),
        False,
        int(stale_after_days),
    )


# Algorithm A52, see CODE_FLOW.md
# The card called this A46. A46 is the held package version.
def build_copy_chain(node_id: str, tree: pd.DataFrame, repo_roles: pd.DataFrame) -> list[dict]:
    """Walk ``parent_id`` from this text up to the root, then list the root first.

    Generation 0 is not a stop. Registry versions sit there, and each one's
    parent is the previous trunk version, down to the root lineage's first
    version. The walk keeps at most 50 nodes, counting the start. A missing
    parent ends the walk. A repeated node ends it too.

    Each step uses ``first_repo`` (for a registry version that is also the
    registry named in ``lineage_id``). ``similarity`` is that node's
    similarity to its parent. The root has none. ``role`` is the raw role
    from ``repo_roles``.

    Example:
    >>> tree = pd.DataFrame([
    ...     {"node_id": "acme/skills::pdf#1", "node_kind": "registry_version",
    ...      "first_repo": "acme/skills", "lineage_id": "acme/skills::pdf",
    ...      "node_date": "2026-01-10", "parent_id": None, "parent_similarity": None,
    ...      "generation": 0},
    ...     {"node_id": "text:hub", "node_kind": "copy_text", "first_repo": "foo/agent-skills",
    ...      "node_date": "2026-02-01", "parent_id": "acme/skills::pdf#1",
    ...      "parent_similarity": 0.97, "generation": 1},
    ...     {"node_id": "text:copy", "node_kind": "copy_text", "first_repo": "acme/agent-tools",
    ...      "node_date": "2026-03-05", "parent_id": "text:hub", "parent_similarity": 0.88,
    ...      "generation": 2},
    ... ])
    >>> roles = pd.DataFrame([
    ...     {"repo": "acme/skills", "repo_role": "registry"},
    ...     {"repo": "foo/agent-skills", "repo_role": "hub"},
    ...     {"repo": "acme/agent-tools", "repo_role": "repository"},
    ... ])
    >>> [step["repo"] for step in build_copy_chain("text:copy", tree, roles)]
    ['acme/skills', 'foo/agent-skills', 'acme/agent-tools']
    """
    if _missing(node_id) or tree is None or tree.empty or "node_id" not in tree.columns:
        return []
    nodes = _index_nodes(tree)
    roles = _role_lookup(repo_roles)
    found: list[dict] = []
    seen: set[str] = set()
    current = _text(node_id)
    while current and current not in seen and len(found) < MAX_CHAIN_STEPS:
        seen.add(current)
        row = nodes.get(current)
        if row is None:
            break
        found.append(_chain_step(row, roles))
        current = _text(row.get("parent_id"))
    found.reverse()
    return found


def build_change_steps(repo: str, path: str, versions: pd.DataFrame) -> list[dict]:
    """One change row per version of this skill, oldest version first.

    Line counts and the body-change percent come from the ``prev_*``
    columns. ``notes`` is the stored ``change_notes`` list. The first
    version has no previous text, so those counts are null and the notes
    are empty.

    Example:
    >>> versions = pd.DataFrame([{
    ...     "repo": "acme/app", "path": "skills/a/SKILL.md", "version_seq": 1,
    ...     "commit_date": "2026-02-01", "change_kind": "copy",
    ...     "prev_lines_added": None, "prev_lines_removed": None,
    ...     "prev_body_change_pct": None, "change_notes": "[]",
    ... }])
    >>> build_change_steps("acme/app", "skills/a/SKILL.md", versions)[0]["change_kind"]
    'copy'
    """
    frame = _rows_for_path(versions, repo, path)
    if frame.empty:
        return []
    if "version_seq" in frame.columns:
        frame = frame.sort_values("version_seq", kind="mergesort")
    return [_step_record(row) for row in frame.to_dict("records")]


def build_repo_record(repo: str, dev: bool, stale_after_days: int, con=None) -> dict:
    """Build the stage 14 record for one repository.

    Inputs: ``repo`` is ``owner/name``. ``dev`` chooses the data folder.
    ``stale_after_days`` recomputes ``copy_state`` from ``days_stale``.
    ``con`` is an open DuckDB connection that already has the stage tables;
    when it is omitted, this function opens one and reads the parquet files.
    Outputs: the record dict. An unknown repository raises
    ``UnknownRepoError``.

    Example: a repository with one copied skill and one independent skill
    is built in ``tests/skill_genealogy/test_repo_report.py``.
    """
    owns_connection = con is None
    connection = connect_duckdb() if owns_connection else con
    try:
        if owns_connection:
            _register_views(connection, dev)
        # Step 1: Read only this repo's rows
        tables = _read_repo_tables(connection, repo)
        if _repo_is_unknown(tables):
            raise UnknownRepoError(f"Unknown repo: {repo}")
        # Step 2: Walk each copy chain, list change steps, recompute stale
        skills = _build_skills(repo, tables, int(stale_after_days))
        return _assemble_record(repo, tables["role"], skills, int(stale_after_days))
    finally:
        if owns_connection:
            connection.close()


def format_repo_record(record: dict) -> str:
    """Return the terminal report: summary first, then one block per skill.

    A copied skill shows where it was copied from, the chain, each version,
    the total change, the folder counts and the stale line. An independent
    skill shows its family kind.

    Inputs: the dict from ``build_repo_record``. Outputs: one string.
    """
    lines = _summary_lines(record)
    lines.append("")
    skills = record.get("skills") or []
    cutoff = record.get("stale_after_days")
    for index, skill in enumerate(skills):
        if index:
            lines.append("")
        lines.extend(
            _skill_lines(skill, _text(record.get("repo")), _text(record.get("role")), cutoff)
        )
    return "\n".join(lines)


def read_inputs(dev: bool) -> dict[str, Path]:
    """Check the stage 14a files and return their paths.

    A missing file raises ``FileNotFoundError`` and names the command
    that builds it. ``skill_language.parquet`` is always the full-data
    file. Artifacts are the raw GitSkills glob.

    Inputs: ``dev`` — True reads the dev folder.
    Outputs: short name to path.
    """
    paths = _input_paths(dev)
    check_inputs_exist(paths, _input_builders(dev))
    return paths


def save_outputs(record: dict, dev: bool) -> Path:
    """Write the record as JSON under ``results_dir(dev)/repo_reports``.

    The file name replaces ``/`` with ``__``. The folder is created when
    it is missing.

    Inputs: the record and ``dev``. Outputs: the JSON path.
    """
    path = report_path(_text(record.get("repo")), dev)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def report_path(repo: str, dev: bool) -> Path:
    """Return ``repo_reports/<owner__repo>.json`` for this mode.

    Example:
    >>> report_path("acme/agent-tools", True).name
    'acme__agent-tools.json'
    """
    return results_dir(dev) / "repo_reports" / f"{repo.replace('/', '__')}.json"


def main() -> None:
    """Print one repository's report. ``--save`` also writes the JSON.

    ``--dev`` reads the sample folders. ``--stale-days`` defaults to
    ``STALE_AFTER_DAYS`` (30). ``--force`` rebuilds a saved JSON that is
    already there. An unknown repository prints a message and exits 1.
    """
    parser = build_arg_parser("explain one repository's skills")
    parser.add_argument("repo", help="owner/repo")
    parser.add_argument(
        "--stale-days",
        type=int,
        default=settings.STALE_AFTER_DAYS,
        help="days after a missed release before a copy is stale",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="write results/.../repo_reports/<owner__repo>.json",
    )
    args = parser.parse_args()
    destination = report_path(args.repo, args.dev)
    if args.save and outputs_exist([destination]) and not args.force:
        print("skip")
        return
    # Step 1: Read only this repo's rows
    # Step 2: Walk each copy chain, list change steps, recompute stale
    try:
        record = build_repo_record(args.repo, args.dev, args.stale_days)
    except UnknownRepoError as error:
        print(str(error))
        raise SystemExit(1) from error
    # Step 3: Print the summary and one block per skill
    _print_report(record)
    # Step 4: Save the same record when --save is set
    if args.save:
        print(f"saved: {save_outputs(record, args.dev)}")


def _print_report(record: dict) -> None:
    """Print the skill blocks, the counts, and two sample skills."""
    print(format_repo_record(record))
    summary = record["summary"]
    print_summary(
        "Repo report",
        {
            "repo": record["repo"],
            "skills": summary["skills"],
            "copied": summary["copied"],
            "registries": summary["registries"],
            "through_hub": summary["through_hub"],
            "stale": summary["stale"],
            "stale_after_days": record["stale_after_days"],
        },
    )
    print_sample_records(_sample_frame(record), "Sample skills")


def _register_views(connection, dev: bool) -> None:
    """Point DuckDB views at the stage 14a parquet files."""
    paths = read_inputs(dev)
    for name, path in paths.items():
        quoted = _quote(str(path))
        connection.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet({quoted})")


def _read_repo_tables(connection, repo: str) -> dict[str, pd.DataFrame]:
    """Return the role row and every per-repo table used to build the record."""
    role = _fetch(connection, ROLE_SQL, [repo])
    artifacts = _fetch(connection, SKILLS_SQL, [repo])
    copies = _fetch(connection, COPY_FILES_SQL, [repo])
    shas = _unique_shas(artifacts, copies)
    families = _families_for(connection, shas)
    tables = {
        "role": role,
        "artifacts": artifacts,
        "copies": copies,
        "families": families,
        "changes": _fetch(connection, CHANGES_SQL, [repo]),
        "versions": _fetch(connection, SIMILARITY_SQL, [repo]),
        "folders": _fetch(connection, FOLDER_SQL, [repo]),
        "staleness": _fetch(connection, STALENESS_SQL, [repo]),
    }
    tables["tree"] = _tree_for(connection, copies, families)
    tables["roles"] = _chain_roles(connection, tables["tree"], repo)
    return tables


def _families_for(connection, shas: list[str]) -> pd.DataFrame:
    """Family rows for these text ids. An empty id list returns no rows."""
    if not shas:
        return pd.DataFrame(columns=["file_sha", "family_id", "family_kind", "registry_self"])
    connection.register("skill_shas", pd.DataFrame({"file_sha": shas}))
    return _fetch(connection, FAMILIES_SQL)


def _tree_for(connection, copies: pd.DataFrame, families: pd.DataFrame) -> pd.DataFrame:
    """Family-tree nodes for the families these copies belong to.

    Independent skills are not loaded. When a copy has no family row, the
    start node is looked up so its family can still be read.
    """
    family_ids = _copy_family_ids(copies, families)
    if not family_ids:
        start_ids = _copy_node_ids(copies)
        if start_ids:
            connection.register("start_nodes", pd.DataFrame({"node_id": start_ids}))
            found = _fetch(connection, TREE_FAMILY_SQL)
            family_ids.update(_text(value) for value in found.get("family_id", []) if _text(value))
    family_ids.discard("")
    if not family_ids:
        return pd.DataFrame()
    connection.register("needed_families", pd.DataFrame({"family_id": sorted(family_ids)}))
    return _fetch(connection, TREE_SQL)


def _chain_roles(connection, tree: pd.DataFrame, repo: str) -> pd.DataFrame:
    """Role rows for this repo and for every repo named on the loaded tree."""
    names = {repo}
    if tree is not None and not tree.empty:
        if "first_repo" in tree.columns:
            names.update(_text(value) for value in tree["first_repo"] if _text(value))
        if "lineage_id" in tree.columns:
            names.update(_registry_name(value) for value in tree["lineage_id"])
    names.discard("")
    connection.register("chain_repos", pd.DataFrame({"repo": sorted(names)}))
    return _fetch(connection, CHAIN_ROLES_SQL)


def _build_skills(repo: str, tables: dict[str, pd.DataFrame], stale_after_days: int) -> list[dict]:
    """One skill dict per English path, copies first filled, then family kind only."""
    family_by_sha = _index_by(tables["families"], "file_sha")
    copy_by_path = _index_by(tables["copies"], "path")
    paths = _paths_for_repo(tables["artifacts"], tables["copies"])
    skills = []
    for path in sorted(paths):
        sha = paths[path]
        family = family_by_sha.get(sha, {})
        copy = copy_by_path.get(path)
        is_copy = copy is not None and not _is_true(family.get("registry_self"))
        skills.append(
            _one_skill(repo, path, sha, family, copy if is_copy else None, tables, stale_after_days)
        )
    return skills


def _one_skill(repo, path, sha, family, copy, tables, stale_after_days) -> dict:
    """The record for one path. ``copy`` is None when the skill is not a copy."""
    skill = {
        "path": path,
        "file_sha": sha or None,
        "family_id": _text(family.get("family_id")) or None,
        "family_kind": _text(family.get("family_kind")) or None,
        "lineage_id": None,
        "link": None,
        "origin_seq": None,
        "origin_date": None,
        "similarity": None,
        "chain": [],
        "steps": [],
        "total_change": None,
        "folder": None,
        "staleness": None,
    }
    if copy is None:
        return skill
    skill["lineage_id"] = _text(copy.get("lineage_id")) or None
    skill["link"] = _text(copy.get("link")) or None
    skill["origin_seq"] = _as_int(copy.get("origin_seq"))
    skill["origin_date"] = _day(copy.get("origin_date"))
    skill["chain"] = build_copy_chain(f"text:{sha}", tables["tree"], tables["roles"])
    skill["steps"] = build_change_steps(repo, path, tables["versions"])
    skill["similarity"] = _origin_similarity(repo, path, tables["versions"])
    skill["total_change"] = _total_change(repo, path, tables["changes"])
    skill["folder"] = _folder_record(repo, path, tables["folders"])
    skill["staleness"] = _staleness_record(repo, path, tables["staleness"], stale_after_days)
    return skill


def _assemble_record(repo, role, skills, stale_after_days) -> dict:
    """The header, the summary counts and the skill list."""
    role_row = role.iloc[0] if role is not None and not role.empty else None
    return {
        "repo": repo,
        "role": None if role_row is None else (_text(role_row.get("repo_role")) or None),
        "stars": None if role_row is None else _as_int(role_row.get("stars")),
        "hub_share": None if role_row is None else _as_float(role_row.get("hub_share")),
        "hub_reason": None if role_row is None else _hub_reason(role_row),
        "stale_after_days": int(stale_after_days),
        "summary": _summary_counts(skills),
        "skills": skills,
    }


def _summary_counts(skills: list[dict]) -> dict:
    """Copied skills, registries, hubs in the middle of a chain, and stale counts."""
    copied = [
        skill
        for skill in skills
        if skill.get("link") or skill.get("lineage_id") or skill.get("staleness") is not None
    ]
    registries = {
        _registry_name(skill.get("lineage_id"))
        for skill in copied
        if _registry_name(skill.get("lineage_id"))
    }
    scores = [
        skill["similarity"]["jaccard"]
        for skill in copied
        if skill.get("similarity") and skill["similarity"].get("jaccard") is not None
    ]
    states = {name: 0 for name in _STATE_NAMES}
    for skill in copied:
        state = (skill.get("total_change") or {}).get("state")
        if state in states:
            states[state] += 1
    return {
        "skills": len(skills),
        "copied": len(copied),
        "registries": len(registries),
        "through_hub": sum(1 for skill in copied if _passes_hub(skill.get("chain") or [])),
        "stale": sum(
            1
            for skill in copied
            if (skill.get("staleness") or {}).get("copy_state") == "stale"
        ),
        "median_similarity_to_origin": _median(scores),
        "states": states,
    }


def _origin_similarity(repo: str, path: str, versions: pd.DataFrame) -> dict | None:
    """Jaccard, containment and lines-kept against the origin, on the newest version."""
    frame = _rows_for_path(versions, repo, path)
    if frame.empty:
        return None
    if "version_seq" in frame.columns:
        frame = frame.sort_values("version_seq", kind="mergesort")
    row = frame.iloc[-1]
    return {
        "jaccard": _as_float(row.get("origin_jaccard")),
        "containment": _as_float(row.get("origin_containment")),
        "verified": _as_float(row.get("origin_verified")),
    }


def _total_change(repo: str, path: str, changes: pd.DataFrame) -> dict | None:
    """Stage 9 state for this path: state, body change percent, customised flag."""
    frame = _rows_for_path(changes, repo, path)
    if frame.empty:
        return None
    row = frame.iloc[0]
    return {
        "state": _text(row.get("state")) or None,
        "body_change_pct": _as_float(row.get("body_change_pct")),
        "customized": _is_true(row.get("customized")),
    }


def _folder_record(repo: str, path: str, folders: pd.DataFrame) -> dict | None:
    """Folder counts for the newest version compared with the origin package."""
    frame = _rows_for_path(folders, repo, path)
    if frame.empty:
        return None
    if "target" in frame.columns:
        origin = frame.loc[frame["target"].astype(str).eq("origin")]
        if not origin.empty:
            frame = origin
    if "version_seq" in frame.columns:
        frame = frame.sort_values("version_seq", kind="mergesort")
    row = frame.iloc[-1]
    return {
        "files_added": _as_int(row.get("files_added")) or 0,
        "scripts_added": _as_int(row.get("scripts_added")) or 0,
        "new_risky_scripts": _as_int(row.get("new_risky_scripts")) or 0,
        "missing_scripts": _as_list(row.get("missing_scripts")),
        "hidden_scripts": _as_list(row.get("hidden_scripts")),
    }


def _staleness_record(repo, path, staleness, stale_after_days) -> dict | None:
    """Held version and the copy state recomputed at this cutoff."""
    frame = _rows_for_path(staleness, repo, path)
    if frame.empty:
        return None
    row = frame.iloc[0]
    state = recompute_copy_state(
        _text(row.get("copy_state")) or None,
        _as_int(row.get("skill_md_versions_behind")),
        _as_int(row.get("days_stale")),
        _as_int(row.get("package_versions_behind")),
        stale_after_days,
    )
    return {
        "held_seq": _as_int(row.get("held_seq")),
        "versions_behind": _as_int(row.get("skill_md_versions_behind")),
        "days_stale": _as_int(row.get("days_stale")),
        "package_versions_behind": _as_int(row.get("package_versions_behind")),
        "missed_risky": _as_int(row.get("missed_risky")),
        "missed_commands": _as_int(row.get("missed_commands")),
        "missed_network": _as_int(row.get("missed_network")),
        "missed_scripts": _as_int(row.get("missed_scripts")),
        "copy_state": state,
    }


def _summary_lines(record: dict) -> list[str]:
    """The header and the one-line summary."""
    summary = record.get("summary") or {}
    badge = _badge(_text(record.get("role")))
    stars = record.get("stars")
    star_text = "—" if stars is None else str(stars)
    lines = [f"{record.get('repo')}   {badge}   stars {star_text}"]
    if record.get("hub_reason"):
        lines.append(str(record["hub_reason"]))
    lines.append(_summary_sentence(summary, record.get("stale_after_days")))
    median = summary.get("median_similarity_to_origin")
    median_text = "—" if median is None else f"{median:.2f}"
    states = summary.get("states") or {}
    state_text = ", ".join(f"{states.get(name, 0)} {name}" for name in _STATE_NAMES)
    lines.append(f"similarity to origin: median {median_text} | {state_text}")
    return lines


def _summary_sentence(summary: dict, stale_after_days) -> str:
    """``N skills, C copied from R registries, H through a hub, S stale``."""
    registries = int(summary.get("registries") or 0)
    registry_word = "registry" if registries == 1 else "registries"
    return (
        f"{summary.get('skills', 0)} skills, "
        f"{summary.get('copied', 0)} copied from {registries} {registry_word}, "
        f"{summary.get('through_hub', 0)} through a hub, "
        f"{summary.get('stale', 0)} stale (after {stale_after_days} days)"
    )


def _skill_lines(skill: dict, repo: str, role: str, stale_after_days) -> list[str]:
    """One skill block. A copy has the chain; any other skill has its family kind."""
    badge = _badge(role)
    lines = [f"{skill.get('path')}   role of repo: {badge}"]
    if not skill.get("link") and not skill.get("staleness"):
        kind = skill.get("family_kind") or "unknown"
        lines.append(f"  family      : {kind}")
        return lines
    lines.append(f"  copied from : {_copied_from(skill)}")
    lines.append(f"  chain       : {_format_chain(skill.get('chain') or [], repo)}")
    lines.append(f"  changes     : {_format_steps(skill.get('steps') or [])}")
    lines.append(f"  total change: {_format_total(skill.get('total_change'))}")
    lines.append(f"  folder      : {_format_folder(skill.get('folder'))}")
    stale_line = _format_stale_with_threshold(skill.get("staleness"), stale_after_days)
    lines.append(f"  staleness   : {stale_line}")
    return lines


def _copied_from(skill: dict) -> str:
    """``lineage vN  (link exact)``."""
    lineage = skill.get("lineage_id") or "(no lineage)"
    seq = skill.get("origin_seq")
    version = f" v{seq}" if seq is not None else ""
    link = skill.get("link") or "unknown"
    return f"{lineage}{version}  (link {link})"


def _format_chain(chain: list[dict], repo: str) -> str:
    """Root first, then each later repo. The repository being explained is ``this repo``."""
    if not chain:
        return "(none)"
    return " -> ".join(_format_chain_step(step, repo) for step in chain)


def _format_chain_step(step: dict, repo: str) -> str:
    """One chain step: repo, role badge, date, and similarity to the previous step."""
    name = step.get("repo") or "(unknown repo)"
    if name == repo:
        name = "this repo"
    text = f"{name} [{_badge(_text(step.get('role')))}]"
    if step.get("date"):
        text += f" {step['date']}"
    similarity = step.get("similarity")
    if similarity is not None:
        text += f" ~{similarity:.2f}"
    return text


def _format_steps(steps: list[dict]) -> str:
    """One short piece per version, separated by `` | ``."""
    if not steps:
        return "(none)"
    return " | ".join(_format_one_step(step) for step in steps)


def _format_one_step(step: dict) -> str:
    """Date, kind, line counts and a short note count."""
    date = step.get("commit_date") or "undated"
    kind = step.get("change_kind") or "edit"
    bits = [str(date), str(kind)]
    added = step.get("lines_added")
    removed = step.get("lines_removed")
    if added is not None or removed is not None:
        bits.append(f"+{added or 0}/-{removed or 0} lines")
    note = _note_summary(step.get("notes") or [])
    if note:
        bits.append(note)
    return " ".join(bits)


def _note_summary(notes: list) -> str:
    """``+1 heading, +2 command`` in a stable kind order."""
    counts: Counter[str] = Counter()
    for note in notes:
        if isinstance(note, dict) and note.get("kind"):
            counts[str(note["kind"])] += 1
    pieces = [f"+{counts[kind]} {kind}" for kind in _NOTE_ORDER if counts.get(kind)]
    return ", ".join(pieces)


def _format_total(change: dict | None) -> str:
    """``substantive, 14% of body lines`` plus customised when the flag is set."""
    if not change:
        return "(none)"
    state = change.get("state") or "unknown"
    percent = _percent(change.get("body_change_pct"))
    text = f"{state}, {percent} of body lines"
    if change.get("customized"):
        text += ", customised"
    return text


def _format_folder(folder: dict | None) -> str:
    """File and script counts, plus how many script paths are missing or hidden."""
    if not folder:
        return "(none)"
    missing = len(folder.get("missing_scripts") or [])
    hidden = len(folder.get("hidden_scripts") or [])
    return (
        f"files added {folder.get('files_added', 0)}, "
        f"scripts added {folder.get('scripts_added', 0)}, "
        f"new risky {folder.get('new_risky_scripts', 0)}, "
        f"missing {missing}, hidden {hidden}"
    )


def _format_stale(staleness: dict | None) -> str:
    """Held version, how far behind, the recomputed state, and what was missed."""
    if not staleness:
        return "(none)"
    held = staleness.get("held_seq")
    held_text = "none" if held is None else f"v{held}"
    behind = staleness.get("versions_behind")
    behind_text = "unknown" if behind is None else str(behind)
    bits = [f"holds {held_text}", f"{behind_text} versions behind"]
    if staleness.get("days_stale") is not None:
        bits.append(f"{staleness['days_stale']} days")
    state = _STATE_WORDS.get(staleness.get("copy_state"), staleness.get("copy_state") or "unknown")
    bits.append(f"{state} (threshold {staleness.get('threshold_days')})")
    bits.append(f"missed: {_missed_phrase(staleness)}")
    return ", ".join(bits)


def _missed_phrase(staleness: dict) -> str:
    """Name the missed kinds, or ``none`` when nothing risky was missed."""
    pieces = [
        label for key, label in _MISS_LABELS if (staleness.get(key) or 0) > 0
    ]
    if pieces:
        return ", ".join(pieces)
    risky = staleness.get("missed_risky") or 0
    if risky:
        return f"{risky} risky"
    return "none"


def _format_stale_with_threshold(staleness: dict | None, stale_after_days) -> str:
    """Stale line that knows the cutoff used for this report."""
    if not staleness:
        return "(none)"
    filled = dict(staleness)
    filled["threshold_days"] = stale_after_days
    return _format_stale(filled)


def _input_paths(dev: bool) -> dict[str, Path]:
    """Parquet paths and the raw artifact glob for this mode."""
    folder = data_dir(dev)
    full = data_dir(False)
    return {
        "copy_files": folder / "copy_files.parquet",
        "families": folder / "families.parquet",
        "family_tree": folder / "family_tree.parquet",
        "copy_changes": folder / "copy_changes.parquet",
        "copy_version_similarity": folder / "copy_version_similarity.parquet",
        "copy_version_folder_summary": folder / "copy_version_folder_summary.parquet",
        "copy_staleness": folder / "copy_staleness.parquet",
        "repo_roles_final": folder / "repo_roles_final.parquet",
        "registry_hubs": folder / "registry_hubs.parquet",
        "skill_language": full / "skill_language.parquet",
        "artifacts": Path(settings.ARTIFACTS_GLOB),
    }


def _input_builders(dev: bool) -> dict[str, str]:
    """The command that builds each input. The message names it when a file is missing."""
    flag = " --dev" if dev else ""
    changes = f"python -m skill_genealogy.measure_copy_changes{flag}"
    rehost = f"python -m skill_genealogy.find_copies{flag} --only rehost"
    return {
        "copy_files": f"python -m skill_genealogy.assign_families{flag}",
        "families": f"python -m skill_genealogy.assign_families{flag}",
        "family_tree": f"python -m skill_genealogy.build_family_trees{flag}",
        "copy_changes": changes,
        "copy_version_similarity": f"{changes} --only versions",
        "copy_version_folder_summary": f"{changes} --only folders",
        "copy_staleness": f"python -m skill_genealogy.measure_staleness{flag}",
        "repo_roles_final": rehost,
        "registry_hubs": rehost,
        "skill_language": "python -m skill_genealogy.select_registries",
        "artifacts": "place GitSkills artifacts under src/data/gitskills_data/data/artifacts/",
    }


def _repo_is_unknown(tables: dict[str, pd.DataFrame]) -> bool:
    """True when the repo has no role row, no English skill and no copy file."""
    return all(_frame_empty(tables.get(name)) for name in ("role", "artifacts", "copies"))


def _frame_empty(frame: pd.DataFrame | None) -> bool:
    """True when the frame is missing or has no rows."""
    return frame is None or frame.empty


def _unique_shas(artifacts: pd.DataFrame, copies: pd.DataFrame) -> list[str]:
    """Text ids from the skill list and the copy list, each id once."""
    found: list[str] = []
    for frame in (artifacts, copies):
        if _frame_empty(frame) or "file_sha" not in frame.columns:
            continue
        for value in frame["file_sha"]:
            text = _text(value)
            if text and text not in found:
                found.append(text)
    return found


def _copy_family_ids(copies: pd.DataFrame, families: pd.DataFrame) -> set[str]:
    """Family ids of copy texts. A registry's own text is left out."""
    if _frame_empty(copies) or _frame_empty(families):
        return set()
    copy_shas = {_text(value) for value in copies["file_sha"] if _text(value)}
    found: set[str] = set()
    for row in families.to_dict("records"):
        if _text(row.get("file_sha")) not in copy_shas or _is_true(row.get("registry_self")):
            continue
        family_id = _text(row.get("family_id"))
        if family_id:
            found.add(family_id)
    return found


def _copy_node_ids(copies: pd.DataFrame) -> list[str]:
    """``text:<file_sha>`` for each copy, the same id the family tree uses."""
    if _frame_empty(copies) or "file_sha" not in copies.columns:
        return []
    return [f"text:{_text(value)}" for value in copies["file_sha"] if _text(value)]


def _index_by(frame: pd.DataFrame, column: str) -> dict[str, dict]:
    """Rows keyed by one column. The first row wins when the key repeats."""
    if _frame_empty(frame) or column not in frame.columns:
        return {}
    indexed: dict[str, dict] = {}
    for row in frame.to_dict("records"):
        key = _text(row.get(column))
        if key and key not in indexed:
            indexed[key] = row
    return indexed


def _paths_for_repo(artifacts: pd.DataFrame, copies: pd.DataFrame) -> dict[str, str]:
    """Path to file sha. A copy path missing from the artifact list is still kept."""
    paths: dict[str, str] = {}
    if not _frame_empty(artifacts):
        for row in artifacts.to_dict("records"):
            path = _text(row.get("path"))
            if path:
                paths[path] = _text(row.get("file_sha"))
    if not _frame_empty(copies):
        for row in copies.to_dict("records"):
            path = _text(row.get("path"))
            if path and not paths.get(path):
                paths[path] = _text(row.get("file_sha"))
    return paths


def _index_nodes(tree: pd.DataFrame) -> dict[str, dict]:
    """Tree rows keyed by node id. The first row wins when an id is repeated."""
    nodes: dict[str, dict] = {}
    for row in tree.to_dict("records"):
        key = _text(row.get("node_id"))
        if key and key not in nodes:
            nodes[key] = row
    return nodes


def _role_lookup(repo_roles: pd.DataFrame) -> dict[str, str]:
    """Repo name to raw role. An empty frame returns an empty map."""
    if _frame_empty(repo_roles) or "repo" not in repo_roles.columns:
        return {}
    lookup: dict[str, str] = {}
    frame = repo_roles.drop_duplicates("repo")
    for row in frame.to_dict("records"):
        lookup[_text(row.get("repo"))] = _text(row.get("repo_role"))
    return lookup


def _chain_step(row: dict, roles: dict[str, str]) -> dict:
    """One chain step: repo, raw role, node kind, date, similarity to the parent."""
    repo = _step_repo(row)
    role = roles.get(repo, "")
    return {
        "repo": repo or None,
        "role": role or None,
        "node_kind": _text(row.get("node_kind")) or None,
        "date": _day(row.get("node_date")),
        "similarity": _as_float(row.get("parent_similarity")),
    }


def _step_repo(row: dict) -> str:
    """``first_repo``, or the registry named in the lineage id for a version node."""
    first = _text(row.get("first_repo"))
    if first:
        return first
    if _text(row.get("node_kind")) == "registry_version":
        return _registry_name(row.get("lineage_id"))
    return ""


def _step_record(row: dict) -> dict:
    """One version step from the ``prev_*`` columns and the note list."""
    return {
        "version_seq": _as_int(row.get("version_seq")),
        "commit_date": _day(row.get("commit_date")),
        "change_kind": _text(row.get("change_kind")) or None,
        "lines_added": _as_int(row.get("prev_lines_added")),
        "lines_removed": _as_int(row.get("prev_lines_removed")),
        "body_change_pct": _as_float(row.get("prev_body_change_pct")),
        "notes": _notes(row.get("change_notes")),
    }


def _rows_for_path(frame: pd.DataFrame | None, repo: str, path: str) -> pd.DataFrame:
    """Rows of ``frame`` for this repo and path. A missing frame returns no rows."""
    if _frame_empty(frame):
        return pd.DataFrame()
    result = frame
    if "repo" in result.columns:
        result = result.loc[result["repo"].astype(str).eq(str(repo))]
    if "path" in result.columns:
        result = result.loc[result["path"].astype(str).eq(str(path))]
    return result


def _passes_hub(chain: list[dict]) -> bool:
    """True when a copied text between the origin and this skill passed through a hub.

    The first step is the origin and the last step is this text. Later
    registry versions are still that origin, so a ``registry_version`` step
    is not a hub in between. Only a ``copy_text`` step whose role is ``hub``
    or ``registry_hub`` counts.
    """
    middle = chain[1:-1]
    return any(
        step.get("node_kind") == "copy_text" and step.get("role") in {"hub", "registry_hub"}
        for step in middle
    )


def _registry_name(lineage_id: object) -> str:
    """The repo in ``owner/repo::folder``, or an empty string."""
    text = _text(lineage_id)
    if "::" not in text:
        return ""
    return text.split("::", 1)[0]


def _notes(value: object) -> list:
    """The change-note list. A JSON string is parsed. Anything else is empty."""
    if isinstance(value, list):
        return value
    if _missing(value):
        return []
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return parsed
    return []


def _sample_frame(record: dict) -> pd.DataFrame:
    """Two short skill rows for the sample print."""
    rows = []
    for skill in (record.get("skills") or [])[:2]:
        change = skill.get("total_change") or {}
        stale = skill.get("staleness") or {}
        rows.append(
            {
                "path": skill.get("path") or "",
                "link": skill.get("link") or "",
                "state": change.get("state") or "",
                "copy_state": stale.get("copy_state") or "",
                "family_kind": skill.get("family_kind") or "",
            }
        )
    return pd.DataFrame(rows)


def _fetch(connection, sql: str, params: list | None = None) -> pd.DataFrame:
    """Run one query and return a DataFrame."""
    if params is None:
        return connection.execute(sql).df()
    return connection.execute(sql, params).df()


def _quote(text: str) -> str:
    """A single-quoted SQL string with quotes escaped."""
    return "'" + text.replace("'", "''") + "'"


def _badge(role: str) -> str:
    """The terminal badge for a raw role. An empty role is ``UNKNOWN``."""
    if not role:
        return "UNKNOWN"
    return _ROLE_BADGES.get(role, role.upper())


def _percent(value: object) -> str:
    """A body-change percent, as a whole number when it is close to one."""
    number = _as_float(value)
    if number is None:
        return "—"
    if abs(number - round(number)) < 0.05:
        return f"{round(number):.0f}%"
    return f"{number:.1f}%"


def _median(values: list[float]) -> float | None:
    """The median of the scores, or None when there are none."""
    if not values:
        return None
    return round(float(statistics.median(values)), 6)


def _day(value: object) -> str | None:
    """The ``YYYY-MM-DD`` prefix of a timestamp, or None when it is missing."""
    if _missing(value):
        return None
    text = str(value)
    day = text[:10]
    if len(day) == 10 and day[4] == "-" and day[7] == "-":
        return day
    return None


def _as_list(value: object) -> list:
    """A list cell, including a parquet list that arrives as an array."""
    if isinstance(value, (list, tuple)):
        return [item for item in (_plain_item(item) for item in value) if item is not None]
    if hasattr(value, "tolist") and not isinstance(value, str):
        listed = value.tolist()
        return [item for item in (_plain_item(item) for item in listed) if item is not None]
    if _missing(value):
        return []
    text = str(value).strip()
    if not text or text == "[]":
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return [text]
    if isinstance(parsed, list):
        return [item for item in (_plain_item(item) for item in parsed) if item is not None]
    return [text]


def _plain_item(item: object):
    """One list item as a plain string or number. Missing items are dropped later."""
    if _missing(item):
        return None
    if isinstance(item, float):
        return round(item, 6)
    if hasattr(item, "item"):
        return _plain_item(item.item())
    return item


def _as_int(value: object) -> int | None:
    """An int, or None when the cell is missing or not a number."""
    if _missing(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    """A float rounded to 6 decimals, or None when the cell is missing."""
    if _missing(value):
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _is_true(value: object) -> bool:
    """True for a real true flag. The string ``"false"`` is not true."""
    if _missing(value):
        return False
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _text(value: object) -> str:
    """The cell as text. A missing cell is an empty string."""
    if _missing(value):
        return ""
    return str(value)


def _missing(value: object) -> bool:
    """True for None and for a pandas or numpy missing value. A list is not missing."""
    if value is None:
        return True
    if isinstance(value, (list, dict, tuple, set)):
        return False
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    if isinstance(missing, bool):
        return missing
    return False


if __name__ == "__main__":
    main()
