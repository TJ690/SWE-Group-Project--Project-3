"""One repo with a copied skill and an independent skill, plus the chain cap."""

import pandas as pd
import pytest

from skill_genealogy.file_helpers import connect_duckdb
from skill_genealogy.repo_report import (
    UnknownRepoError,
    build_copy_chain,
    build_repo_record,
    format_repo_record,
    main,
)

REPO = "acme/agent-tools"


def test_chain_order_is_registry_then_hub_then_repo():
    """The walk visits the trunk, then the hub, then this repo. The root is first."""
    tree = pd.DataFrame(
        [
            _node("acme/skills::pdf#1", "registry_version", "acme/skills", None, None, 0),
            _node(
                "acme/skills::pdf#2", "registry_version", "acme/skills",
                "acme/skills::pdf#1", None, 0,
            ),
            _node("text:hub", "copy_text", "foo/agent-skills", "acme/skills::pdf#2", 0.97, 1),
            _node("text:copy", "copy_text", REPO, "text:hub", 0.88, 2),
        ]
    )
    roles = pd.DataFrame(
        [
            {"repo": "acme/skills", "repo_role": "registry"},
            {"repo": "foo/agent-skills", "repo_role": "hub"},
            {"repo": REPO, "repo_role": "repository"},
        ]
    )
    chain = build_copy_chain("text:copy", tree, roles)
    assert [step["repo"] for step in chain] == [
        "acme/skills",
        "acme/skills",
        "foo/agent-skills",
        REPO,
    ]
    assert [step["role"] for step in chain] == ["registry", "registry", "hub", "repository"]
    assert chain[0]["date"] == "2026-01-10"
    assert chain[-1]["similarity"] == 0.88


def test_chain_stops_at_50_nodes():
    """A line of 55 nodes keeps the 50 nearest the start, then reverses them."""
    rows = []
    for index in range(55):
        parent = None if index == 0 else f"n{index - 1}"
        rows.append(_node(f"n{index}", "copy_text", f"r{index}", parent, 0.5, index))
    chain = build_copy_chain("n54", pd.DataFrame(rows), pd.DataFrame(columns=["repo", "repo_role"]))
    assert len(chain) == 50
    assert chain[-1]["repo"] == "r54"
    assert chain[0]["repo"] == "r5"


def test_repo_record_for_two_skills_recomputes_stale_at_400_days():
    """The copied skill is behind at 400 days, and the chain passes through the hub."""
    connection = _repo_connection()
    record = build_repo_record(REPO, True, 400, con=connection)
    assert record["role"] == "repository"
    assert record["stars"] == 240
    assert record["summary"]["skills"] == 2
    assert record["summary"]["copied"] == 1
    assert record["summary"]["registries"] == 1
    assert record["summary"]["through_hub"] == 1
    assert record["summary"]["stale"] == 0
    assert record["summary"]["median_similarity_to_origin"] == 0.86
    assert record["summary"]["states"]["substantive"] == 1
    paths = [skill["path"] for skill in record["skills"]]
    assert paths == ["skills/deploy/SKILL.md", "skills/pdf/SKILL.md"]
    assert "skills/zh/SKILL.md" not in paths

    own = record["skills"][0]
    assert own["family_kind"] == "independent"
    assert own["chain"] == []
    assert own["staleness"] is None
    assert own["link"] is None

    copied = record["skills"][1]
    assert [step["role"] for step in copied["chain"]] == [
        "registry",
        "registry",
        "hub",
        "repository",
    ]
    assert copied["chain"][2]["repo"] == "foo/agent-skills"
    assert copied["chain"][-1]["repo"] == REPO
    assert copied["similarity"] == {"jaccard": 0.86, "containment": 0.94, "verified": 0.91}
    assert copied["steps"][1]["change_kind"] == "local"
    assert copied["steps"][1]["lines_added"] == 12
    assert copied["steps"][1]["notes"][0]["kind"] == "heading"
    assert copied["total_change"]["state"] == "substantive"
    assert copied["folder"]["files_added"] == 1
    assert copied["staleness"]["copy_state"] == "behind"
    assert copied["staleness"]["days_stale"] == 161
    assert copied["staleness"]["versions_behind"] == 2

    fresh = build_repo_record(REPO, True, 30, con=connection)
    assert fresh["skills"][1]["staleness"]["copy_state"] == "stale"
    assert fresh["summary"]["stale"] == 1


def test_direct_copy_from_a_registry_hub_is_not_through_a_hub():
    """Two registry versions, then the copy, is not through a hub."""
    connection = _repo_connection()
    roles = _roles()
    roles.loc[roles["repo"].eq("acme/skills"), "repo_role"] = "registry_hub"
    roles.loc[roles["repo"].eq("acme/skills"), "is_hub"] = True
    _register(connection, "repo_roles_final", roles)
    _register(connection, "family_tree", _direct_registry_tree())
    record = build_repo_record(REPO, True, 30, con=connection)
    copied = record["skills"][1]
    assert [step["node_kind"] for step in copied["chain"]] == [
        "registry_version",
        "registry_version",
        "copy_text",
    ]
    assert [step["role"] for step in copied["chain"]] == [
        "registry_hub",
        "registry_hub",
        "repository",
    ]
    assert record["summary"]["through_hub"] == 0


def test_orphaned_copy_stays_orphaned_when_the_cutoff_changes():
    """A removed lineage stays orphaned even when the day cutoff would say stale."""
    connection = _repo_connection(copy_state="orphaned", days_stale=161, skill_behind=2)
    record = build_repo_record(REPO, True, 30, con=connection)
    assert record["skills"][1]["staleness"]["copy_state"] == "orphaned"
    assert record["summary"]["stale"] == 0


def test_format_has_the_summary_and_one_block_per_skill():
    """The text starts with the summary and names both skills."""
    record = build_repo_record(REPO, True, 400, con=_repo_connection())
    text = format_repo_record(record)
    assert "2 skills, 1 copied from 1 registry, 1 through a hub, 0 stale (after 400 days)" in text
    assert "skills/deploy/SKILL.md" in text
    assert "skills/pdf/SKILL.md" in text
    assert "family      : independent" in text
    assert "acme/skills [REGISTRY] 2026-01-10 -> acme/skills [REGISTRY]" in text
    assert "foo/agent-skills [HUB]" in text
    assert "this repo [REPOSITORY]" in text
    assert text.index("foo/agent-skills [HUB]") < text.index("this repo [REPOSITORY]")
    assert "behind (threshold 400)" in text
    assert "missed: network call" in text
    assert text.index("2 skills") < text.index("skills/deploy/SKILL.md")
    assert text.index("skills/deploy/SKILL.md") < text.index("skills/pdf/SKILL.md")


def test_unknown_repo_raises():
    """A name that is not in the tables is an error, not an empty report."""
    with pytest.raises(UnknownRepoError, match="Unknown repo: no/such"):
        build_repo_record("no/such", True, 30, con=_empty_connection())


def test_main_exits_1_for_an_unknown_repo(monkeypatch, capsys):
    """The command prints the message and exits 1."""
    monkeypatch.setattr(
        "sys.argv",
        ["repo_report", "no/such"],
    )

    def _raise(*_args, **_kwargs):
        raise UnknownRepoError("Unknown repo: no/such")

    monkeypatch.setattr("skill_genealogy.repo_report.build_repo_record", _raise)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    assert "Unknown repo: no/such" in capsys.readouterr().out


def _node(node_id, kind, repo, parent, similarity, generation) -> dict:
    """One family-tree row. The date is only used to keep the rows distinct."""
    return {
        "node_id": node_id,
        "family_id": "acme/skills::pdf",
        "node_kind": kind,
        "lineage_id": "acme/skills::pdf" if kind == "registry_version" else None,
        "version_seq": 1 if node_id.endswith("#1") else 2,
        "first_repo": repo,
        "node_date": "2026-01-10" if parent is None else "2026-03-05",
        "parent_id": parent,
        "parent_similarity": similarity,
        "generation": generation,
    }


def _repo_connection(copy_state: str = "stale", days_stale: int = 161, skill_behind: int = 2):
    """Tiny tables: one copied skill through a hub, one independent skill, one non-English."""
    connection = connect_duckdb()
    _register(connection, "repo_roles_final", _roles())
    _register(connection, "registry_hubs", pd.DataFrame(
        columns=["repo", "hub_reason", "english_lineages", "rehosted_lineages"]
    ))
    _register(connection, "artifacts", _artifacts())
    _register(connection, "skill_language", _languages())
    _register(connection, "families", _families())
    _register(connection, "copy_files", _copies())
    _register(connection, "copy_changes", _changes())
    _register(connection, "copy_version_similarity", _versions())
    _register(connection, "copy_version_folder_summary", _folders())
    _register(
        connection,
        "copy_staleness",
        _staleness(copy_state, days_stale, skill_behind),
    )
    _register(connection, "family_tree", _tree())
    return connection


def _empty_connection():
    """The same tables with no rows, so every query runs and matches nothing."""
    connection = connect_duckdb()
    _register(connection, "repo_roles_final", _roles().iloc[0:0])
    _register(connection, "registry_hubs", pd.DataFrame(
        columns=["repo", "hub_reason", "english_lineages", "rehosted_lineages"]
    ))
    _register(connection, "artifacts", _artifacts().iloc[0:0])
    _register(connection, "skill_language", _languages().iloc[0:0])
    _register(connection, "families", _families().iloc[0:0])
    _register(connection, "copy_files", _copies().iloc[0:0])
    _register(connection, "copy_changes", _changes().iloc[0:0])
    _register(connection, "copy_version_similarity", _versions().iloc[0:0])
    _register(connection, "copy_version_folder_summary", _folders().iloc[0:0])
    _register(connection, "copy_staleness", _staleness().iloc[0:0])
    _register(connection, "family_tree", pd.DataFrame(_tree()).iloc[0:0])
    return connection


def _register(connection, name: str, frame: pd.DataFrame) -> None:
    """Replace a DuckDB table with this frame."""
    connection.register(name, frame)


def _roles() -> pd.DataFrame:
    """The repo being explained, its registry, and the hub in between."""
    return pd.DataFrame(
        [
            {
                "repo": REPO,
                "stars": 240,
                "english_skills": 2,
                "is_hub": False,
                "hub_share": 0.0,
                "repo_role": "repository",
                "hub_evidence": "dataset_dates",
                "is_registry": False,
            },
            {
                "repo": "acme/skills",
                "stars": 1000,
                "english_skills": 8,
                "is_hub": False,
                "hub_share": 0.0,
                "repo_role": "registry",
                "hub_evidence": "registry_history",
                "is_registry": True,
            },
            {
                "repo": "foo/agent-skills",
                "stars": 10,
                "english_skills": 6,
                "is_hub": True,
                "hub_share": 0.5,
                "repo_role": "hub",
                "hub_evidence": "dataset_dates",
                "is_registry": False,
            },
        ]
    )


def _artifacts() -> pd.DataFrame:
    """Two English skills and one that the language table rejects."""
    return pd.DataFrame(
        [
            {"repo_full_name": REPO, "path": "skills/pdf/SKILL.md", "file_sha": "copy-sha"},
            {"repo_full_name": REPO, "path": "skills/deploy/SKILL.md", "file_sha": "own-sha"},
            {"repo_full_name": REPO, "path": "skills/zh/SKILL.md", "file_sha": "zh-sha"},
        ]
    )


def _languages() -> pd.DataFrame:
    """``zh-sha`` is not English, so that path must not appear."""
    return pd.DataFrame(
        [
            {"file_sha": "copy-sha", "is_english": True},
            {"file_sha": "own-sha", "is_english": True},
            {"file_sha": "zh-sha", "is_english": False},
        ]
    )


def _families() -> pd.DataFrame:
    """The copy sits in the registry family. The other skill is independent."""
    return pd.DataFrame(
        [
            {
                "file_sha": "copy-sha",
                "family_id": "acme/skills::pdf",
                "family_kind": "source",
                "registry_self": False,
            },
            {
                "file_sha": "own-sha",
                "family_id": "cluster:1",
                "family_kind": "independent",
                "registry_self": False,
            },
        ]
    )


def _copies() -> pd.DataFrame:
    """The one copied file, linked exactly to registry version 1."""
    return pd.DataFrame(
        [
            {
                "repo": REPO,
                "path": "skills/pdf/SKILL.md",
                "file_sha": "copy-sha",
                "lineage_id": "acme/skills::pdf",
                "link": "exact",
                "origin_seq": 1,
                "origin_date": "2026-01-10",
            }
        ]
    )


def _changes() -> pd.DataFrame:
    """Fourteen percent of the body lines changed. That is substantive."""
    return pd.DataFrame(
        [
            {
                "repo": REPO,
                "path": "skills/pdf/SKILL.md",
                "state": "substantive",
                "body_change_pct": 14.0,
                "customized": False,
            }
        ]
    )


def _versions() -> pd.DataFrame:
    """A copy, then a local edit that adds one heading."""
    notes = '[{"line": 2, "kind": "heading", "text": "## Deploy"}]'
    return pd.DataFrame(
        [
            {
                "repo": REPO,
                "path": "skills/pdf/SKILL.md",
                "version_seq": 1,
                "commit_date": "2026-02-01",
                "change_kind": "copy",
                "prev_lines_added": None,
                "prev_lines_removed": None,
                "prev_body_change_pct": None,
                "origin_jaccard": 1.0,
                "origin_containment": 1.0,
                "origin_verified": 1.0,
                "change_notes": "[]",
            },
            {
                "repo": REPO,
                "path": "skills/pdf/SKILL.md",
                "version_seq": 2,
                "commit_date": "2026-03-10",
                "change_kind": "local",
                "prev_lines_added": 12,
                "prev_lines_removed": 3,
                "prev_body_change_pct": 9.0,
                "origin_jaccard": 0.86,
                "origin_containment": 0.94,
                "origin_verified": 0.91,
                "change_notes": notes,
            },
        ]
    )


def _folders() -> pd.DataFrame:
    """The origin target is the one the record keeps. The other target is a decoy."""
    return pd.DataFrame(
        [
            {
                "repo": REPO,
                "path": "skills/pdf/SKILL.md",
                "version_seq": 2,
                "target": "at_date",
                "files_added": 9,
                "scripts_added": 9,
                "new_risky_scripts": 9,
                "missing_scripts": "[]",
                "hidden_scripts": "[]",
            },
            {
                "repo": REPO,
                "path": "skills/pdf/SKILL.md",
                "version_seq": 2,
                "target": "origin",
                "files_added": 1,
                "scripts_added": 1,
                "new_risky_scripts": 1,
                "missing_scripts": "[]",
                "hidden_scripts": "[]",
            },
        ]
    )


def _staleness(
    copy_state: str = "stale", days_stale: int = 161, skill_behind: int = 2
) -> pd.DataFrame:
    """Stored as stale at 90 days. 161 days is behind once the cutoff is 400."""
    return pd.DataFrame(
        [
            {
                "repo": REPO,
                "path": "skills/pdf/SKILL.md",
                "held_seq": 1,
                "skill_md_versions_behind": skill_behind,
                "days_stale": days_stale,
                "package_versions_behind": 0,
                "missed_risky": 1,
                "missed_commands": 0,
                "missed_network": 1,
                "missed_scripts": 0,
                "copy_state": copy_state,
            }
        ]
    )


def _direct_registry_tree() -> pd.DataFrame:
    """Registry v1, registry v2, then the copy. No copy-text hub sits between them."""
    return pd.DataFrame(
        [
            _node("acme/skills::pdf#1", "registry_version", "acme/skills", None, None, 0),
            _node(
                "acme/skills::pdf#2", "registry_version", "acme/skills",
                "acme/skills::pdf#1", None, 0,
            ),
            _node("text:copy-sha", "copy_text", REPO, "acme/skills::pdf#2", 1.0, 1),
        ]
    )


def _tree() -> pd.DataFrame:
    """Registry v1, registry v2, the hub, then this repo."""
    return pd.DataFrame(
        [
            _node("acme/skills::pdf#1", "registry_version", "acme/skills", None, None, 0),
            _node(
                "acme/skills::pdf#2", "registry_version", "acme/skills",
                "acme/skills::pdf#1", None, 0,
            ),
            _node("text:hub", "copy_text", "foo/agent-skills", "acme/skills::pdf#2", 0.97, 1),
            _node("text:copy-sha", "copy_text", REPO, "text:hub", 0.88, 2),
        ]
    )
