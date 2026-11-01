"""Hand-made tables for family assignment, history links, and origin dates."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from skill_genealogy.assign_families import (
    _assemble_copy_histories,
    _jaccard_fast,
    _jaccard_simple,
    build_proven_pairs,
    build_text_families,
    choose_origin_version,
    find_history_exact_links,
    find_history_near_links,
    measure_link_method_recall,
    pick_strongest_link,
    score_methods_on_pair,
)
from skill_genealogy.file_helpers import connect_duckdb
from skill_genealogy.find_copies import signature_for_text

_LINEAGE = "plain/reg::skills/a"
_PHRASE = "alpha beta gamma delta epsilon zeta eta theta"


def _versions() -> pd.DataFrame:
    """Registry v1 on 10 Jan, v2 on 1 Mar, v3 on 15 May."""
    return pd.DataFrame(
        [
            {
                "lineage_id": _LINEAGE,
                "version_seq": 1,
                "blob": "v1",
                "release_date": "2026-01-10",
                "is_hub": False,
            },
            {
                "lineage_id": _LINEAGE,
                "version_seq": 2,
                "blob": "v2",
                "release_date": "2026-03-01",
                "is_hub": False,
            },
            {
                "lineage_id": _LINEAGE,
                "version_seq": 3,
                "blob": "v3",
                "release_date": "2026-05-15",
                "is_hub": False,
            },
        ]
    )


def _copy(
    path: str,
    link: str,
    when: str | None,
    seq: int = 3,
    file_sha: str = "copy",
) -> dict[str, object]:
    """One near or exact copy row aimed at version ``seq``."""
    return {
        "repo": "a/r",
        "path": path,
        "file_sha": file_sha,
        "lineage_id": _LINEAGE,
        "version_seq": seq,
        "link": link,
        "first_commit_date": when,
        "release_date": "2026-05-15",
        "is_hub": False,
        "history_source": "none",
        "link_similarity": 0.9,
    }


def _bundle(ids: list[str], texts: list[str]) -> dict[str, object]:
    """Signatures for ``ids``, one text each, with no boilerplate removed."""
    arrays = []
    sizes = []
    for text in texts:
        values, size = signature_for_text(text, set())
        arrays.append(values)
        sizes.append(size)
    return {
        "ids": ids,
        "signatures": np.vstack(arrays),
        "sizes": np.asarray(sizes, dtype=np.int32),
    }


def _row(frame: pd.DataFrame, path: str) -> pd.Series:
    """The one copy row whose path is ``path``."""
    return frame.loc[frame["path"].eq(path)].iloc[0]


def test_origin_version_follows_the_copy_date() -> None:
    """A near copy on 1 Feb starts at v1. A copy on 1 Jan is before every release.

    v2 is also before an April copy, and that copy's text matches v2, so the
    origin is v2 rather than the older v1.
    """
    v1 = "alpha beta gamma delta epsilon one two"
    v2 = "alpha beta gamma delta epsilon two two"
    sigs = _bundle(
        ["feb", "april", _LINEAGE + "#1", _LINEAGE + "#2", _LINEAGE + "#3"],
        [v1, v2, v1, v2, "unrelated words that do not match either version here"],
    )
    copies = pd.DataFrame(
        [
            _copy("skills/feb/SKILL.md", "near", "2026-02-01", file_sha="feb"),
            _copy("skills/jan/SKILL.md", "near", "2026-01-01", file_sha="jan"),
            _copy("skills/april/SKILL.md", "near", "2026-04-01", file_sha="april"),
        ]
    )
    origin = choose_origin_version(copies, _versions(), sigs)
    february = _row(origin, "skills/feb/SKILL.md")
    january = _row(origin, "skills/jan/SKILL.md")
    april = _row(origin, "skills/april/SKILL.md")
    assert int(february.origin_seq) == 1
    assert february.origin_basis == "similar_before_date"
    assert str(february.origin_date).startswith("2026-01-10")
    assert bool(february.copied_before_release) is False
    assert bool(january.copied_before_release) is True
    assert pd.isna(january.origin_seq)
    assert january.origin_basis is None or pd.isna(january.origin_basis)
    assert int(april.origin_seq) == 2
    assert april.origin_basis == "similar_before_date"


def test_undated_lineage_is_no_date() -> None:
    """A dated near copy of a lineage with no release date is no_date."""
    versions = _versions()
    versions["release_date"] = None
    copies = pd.DataFrame([_copy("skills/a/SKILL.md", "near", "2026-02-01")])
    origin = choose_origin_version(copies, versions, _bundle(["copy"], [_PHRASE]))
    row = origin.iloc[0]
    assert row.origin_basis == "no_date"
    assert bool(row.copied_before_release) is False
    assert pd.isna(row.origin_seq)


def test_tied_similarity_keeps_the_newest_version() -> None:
    """Two eligible versions with the same signature: the newer release wins."""
    text = _PHRASE + " shared body"
    sigs = _bundle(["copy", _LINEAGE + "#1", _LINEAGE + "#2"], [text, text, text])
    copies = pd.DataFrame([_copy("skills/a/SKILL.md", "near", "2026-04-01", file_sha="copy")])
    origin = choose_origin_version(copies, _versions(), sigs)
    assert int(origin.iloc[0].origin_seq) == 2


def test_origin_without_a_date() -> None:
    """No history and no dataset date: a near link is no_date, an exact link keeps its version."""
    copies = pd.DataFrame(
        [
            _copy("skills/near/SKILL.md", "near", None, seq=3, file_sha="near"),
            _copy("skills/exact/SKILL.md", "exact", None, seq=2, file_sha="exact"),
        ]
    )
    unused = _bundle(["unused"], ["alpha beta gamma delta epsilon"])
    origin = choose_origin_version(copies, _versions(), unused)
    near = _row(origin, "skills/near/SKILL.md")
    exact = _row(origin, "skills/exact/SKILL.md")
    assert near.origin_basis == "no_date"
    assert pd.isna(near.origin_seq)
    assert near.origin_date is None or pd.isna(near.origin_date)
    assert bool(near.copied_before_release) is False
    assert exact.origin_basis == "exact"
    assert int(exact.origin_seq) == 2
    assert str(exact.origin_date).startswith("2026-03-01")
    assert bool(exact.copied_before_release) is False


def test_history_exact_beats_a_near_text_link() -> None:
    """A history blob equal to v2 beats a near link, and the origin is v2."""
    histories = pd.DataFrame(
        [
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "commit_date": "2026-04-01T00:00:00Z",
                "version_seq": 1,
                "blob": "v2",
                "source": "copied",
                "file_sha": "copy",
            }
        ]
    )
    history = find_history_exact_links(histories, _versions())
    near = pd.DataFrame(
        [
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "file_sha": "copy",
                "lineage_id": _LINEAGE,
                "version_seq": 3,
                "link": "near",
                "link_similarity": 0.91,
                "release_date": "2026-05-15",
                "is_hub": False,
                "history_source": "none",
            }
        ]
    )
    winner = pick_strongest_link(pd.concat([near, history], ignore_index=True))
    assert winner.iloc[0].link == "history_exact"
    assert int(winner.iloc[0].version_seq) == 2
    assert winner.iloc[0].history_source == "copied"
    origin = choose_origin_version(winner, _versions(), _bundle(["copy"], [_PHRASE]))
    assert origin.iloc[0].origin_basis == "exact"
    assert int(origin.iloc[0].origin_seq) == 2


def test_history_exact_keeps_the_earliest_blob() -> None:
    """An earlier hub blob beats a later non-hub blob in the same file."""
    histories = pd.DataFrame(
        [
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "commit_date": "2026-02-01T00:00:00Z",
                "version_seq": 1,
                "blob": "early",
                "source": "wild",
            },
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "commit_date": "2026-04-01T00:00:00Z",
                "version_seq": 2,
                "blob": "late",
                "source": "wild",
            },
        ]
    )
    versions = pd.DataFrame(
        [
            {
                "lineage_id": "hub/reg::skills/a",
                "version_seq": 1,
                "blob": "early",
                "release_date": "2026-01-01",
                "is_hub": True,
                "repo": "hub/reg",
            },
            {
                "lineage_id": "plain/reg::skills/a",
                "version_seq": 1,
                "blob": "late",
                "release_date": "2026-01-01",
                "is_hub": False,
                "repo": "plain/reg",
            },
        ]
    )
    link = find_history_exact_links(histories, versions).iloc[0]
    assert link.lineage_id == "hub/reg::skills/a"
    assert link.history_source == "wild"
    assert link.link_similarity == 1.0


def test_same_release_keeps_the_older_version() -> None:
    """One blob that is two versions of one lineage: the older version wins."""
    histories = pd.DataFrame(
        [
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "commit_date": "2026-02-01T00:00:00Z",
                "version_seq": 1,
                "blob": "same",
                "source": "copied",
            }
        ]
    )
    versions = pd.DataFrame(
        [
            {
                "lineage_id": _LINEAGE,
                "version_seq": 2,
                "blob": "same",
                "release_date": "2026-01-01",
                "is_hub": False,
            },
            {
                "lineage_id": _LINEAGE,
                "version_seq": 1,
                "blob": "same",
                "release_date": "2026-01-01",
                "is_hub": False,
            },
        ]
    )
    link = find_history_exact_links(histories, versions).iloc[0]
    assert int(link.version_seq) == 1
    assert link.lineage_id == _LINEAGE


def test_same_blob_prefers_non_hub_then_oldest() -> None:
    """One history blob shared by two lineages: non-hub, then the oldest date."""
    histories = pd.DataFrame(
        [
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "commit_date": "2026-02-01T00:00:00Z",
                "version_seq": 1,
                "blob": "same",
                "source": "copied",
            }
        ]
    )
    versions = pd.DataFrame(
        [
            {
                "lineage_id": "hub/reg::skills/a",
                "version_seq": 1,
                "blob": "same",
                "release_date": "2026-01-01",
                "is_hub": True,
            },
            {
                "lineage_id": "plain/reg::skills/a",
                "version_seq": 4,
                "blob": "same",
                "release_date": "2026-06-01",
                "is_hub": False,
            },
            {
                "lineage_id": "old/reg::skills/a",
                "version_seq": 2,
                "blob": "same",
                "release_date": "2026-03-01",
                "is_hub": False,
            },
        ]
    )
    link = find_history_exact_links(histories, versions).iloc[0]
    assert link.lineage_id == "old/reg::skills/a"


def test_strongest_link_order() -> None:
    """exact beats every weaker link. A tie prefers a non-hub, then the oldest date."""
    order = ["exact", "history_exact", "near", "near_contained", "history_near", "cluster"]
    rows = []
    for index, link in enumerate(order):
        rows.append(
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "link": link,
                "lineage_id": f"L{index}",
                "version_seq": index + 1,
                "link_similarity": 0.5,
                "release_date": "2026-01-01",
                "is_hub": False,
                "history_source": "copied" if link.startswith("history") else "none",
            }
        )
    assert pick_strongest_link(pd.DataFrame(rows)).iloc[0].link == "exact"
    without_exact = pd.DataFrame(rows[1:])
    assert pick_strongest_link(without_exact).iloc[0].link == "history_exact"
    ties = pd.DataFrame(
        [
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "link": "exact",
                "lineage_id": "hub/reg::skills/a",
                "version_seq": 1,
                "link_similarity": 1.0,
                "release_date": "2026-01-01",
                "is_hub": True,
                "history_source": "none",
            },
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "link": "exact",
                "lineage_id": "plain/reg::skills/a",
                "version_seq": 1,
                "link_similarity": 1.0,
                "release_date": "2026-06-01",
                "is_hub": False,
                "history_source": "none",
            },
            {
                "repo": "b/r",
                "path": "skills/b/SKILL.md",
                "link": "exact",
                "lineage_id": "new/reg::skills/b",
                "version_seq": 2,
                "link_similarity": 1.0,
                "release_date": "2026-06-01",
                "is_hub": False,
                "history_source": "none",
            },
            {
                "repo": "b/r",
                "path": "skills/b/SKILL.md",
                "link": "exact",
                "lineage_id": "old/reg::skills/b",
                "version_seq": 1,
                "link_similarity": 1.0,
                "release_date": "2026-01-01",
                "is_hub": False,
                "history_source": "none",
            },
        ]
    )
    winners = pick_strongest_link(ties)
    first = winners.loc[winners["path"].eq("skills/a/SKILL.md")].iloc[0]
    second = winners.loc[winners["path"].eq("skills/b/SKILL.md")].iloc[0]
    assert first.lineage_id == "plain/reg::skills/a"
    assert second.lineage_id == "old/reg::skills/b"


def test_history_near_uses_only_the_first_version() -> None:
    """The earliest version is scored. A later match does not replace a miss."""
    registry = _PHRASE + " registry body"
    values, size = signature_for_text(registry, set())
    sigs = {
        "ids": ["plain/reg::skills/a#1"],
        "signatures": values.reshape(1, -1),
        "sizes": np.array([size], dtype=np.int32),
        "meta": pd.DataFrame(
            [
                {
                    "version_id": "plain/reg::skills/a#1",
                    "lineage_id": "plain/reg::skills/a",
                    "version_seq": 1,
                    "release_date": "2026-01-10",
                    "is_hub": False,
                }
            ]
        ),
    }
    histories = pd.DataFrame(
        [
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "commit_date": "2026-02-01T00:00:00Z",
                "version_seq": 1,
                "blob": "first",
                "source": "wild",
                "text": "totally different words about cooking pasta sauce tonight",
            },
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "commit_date": "2026-03-01T00:00:00Z",
                "version_seq": 2,
                "blob": "second",
                "source": "wild",
                "text": registry,
            },
        ]
    )
    missed = find_history_near_links(histories, sigs, histories[["repo", "path"]])
    assert missed.empty
    histories.loc[histories["version_seq"].eq(1), "text"] = registry
    found = find_history_near_links(histories, sigs, histories[["repo", "path"]])
    assert found.iloc[0].link == "history_near"
    assert found.iloc[0].lineage_id == "plain/reg::skills/a"
    empty_text = histories.copy()
    empty_text["text"] = ""
    assert find_history_near_links(empty_text, sigs, empty_text[["repo", "path"]]).empty


def test_fast_jaccard_matches_estimate_jaccard() -> None:
    """The array compare agrees with estimate_jaccard, and a zero count scores 0."""
    left, left_size = signature_for_text("alpha beta gamma delta epsilon zeta", set())
    right, right_size = signature_for_text("alpha beta gamma delta epsilon omega", set())
    matrix = np.vstack([left, right])
    sizes = np.array([left_size, right_size], dtype=np.int32)
    simple = _jaccard_simple(left, left_size, matrix, sizes)
    fast = _jaccard_fast(left, left_size, matrix, sizes)
    assert simple[0] == 1.0
    assert np.allclose(simple, fast)
    assert fast[1] < 1.0
    assert _jaccard_fast(left, 0, matrix, sizes).tolist() == [0.0, 0.0]


def test_assemble_histories_maps_wild_dates_and_inherited_commits() -> None:
    """Wild commit_at becomes commit_date. A registry commit stays, marked inherited."""
    wild = pd.DataFrame(
        [
            {
                "repo": "a/r",
                "target_path": "skills/pdf/SKILL.md",
                "commit": "aaa",
                "commit_at": "2026-02-01T00:00:00Z",
                "blob": "b1",
                "version_seq": 4,
                "status": "A",
            },
            {
                "repo": "a/r",
                "target_path": "skills/no/SKILL.md",
                "commit": "zzz",
                "commit_at": "2026-02-01T00:00:00Z",
                "blob": "b9",
                "version_seq": 1,
                "status": "A",
            },
        ]
    )
    copied = pd.DataFrame(
        [
            {
                "repo": "a/r",
                "skill_folder": "skills/pdf",
                "file_path": "SKILL.md",
                "commit": "bbb",
                "commit_date": "2026-03-01T03:00:00+03:00",
                "new_sha": "b2",
                "commit_seq": 2,
                "status": "M",
            },
            {
                "repo": "a/r",
                "skill_folder": "skills/pdf",
                "file_path": "SKILL.md",
                "commit": "old",
                "commit_date": "2026-01-15T00:00:00Z",
                "new_sha": "b0",
                "commit_seq": 1,
                "status": "A",
            },
            {
                "repo": "a/r",
                "skill_folder": "skills/pdf",
                "file_path": "SKILL.md",
                "commit": "gone",
                "commit_date": "2026-01-01T00:00:00Z",
                "new_sha": "b8",
                "commit_seq": 3,
                "status": "D",
            },
            {
                "repo": "a/r",
                "skill_folder": ".",
                "file_path": "SKILL.md",
                "commit": "root",
                "commit_date": "2026-04-01T00:00:00Z",
                "new_sha": "b3",
                "commit_seq": 1,
                "status": "A",
            },
        ]
    )
    english = pd.DataFrame(
        [
            {"repo": "a/r", "path": "skills/pdf/SKILL.md", "file_sha": "sha-pdf"},
            {"repo": "a/r", "path": "SKILL.md", "file_sha": "sha-root"},
        ]
    )
    rows = _assemble_copy_histories(wild, copied, english, ["BBB"])
    pdf = rows.loc[rows["path"].eq("skills/pdf/SKILL.md")]
    assert set(pdf["source"]) == {"wild", "copied"}
    wild_row = pdf.loc[pdf["source"].eq("wild")].iloc[0]
    assert wild_row.commit_date == "2026-02-01T00:00:00Z"
    assert int(wild_row.version_seq) == 4
    assert bool(wild_row.inherited) is False
    copied_rows = pdf.loc[pdf["source"].eq("copied")].sort_values("version_seq")
    assert copied_rows["blob"].tolist() == ["b0", "b2"]
    assert copied_rows["version_seq"].tolist() == [1, 2]
    assert copied_rows["commit_date"].tolist()[1] == "2026-03-01T00:00:00Z"
    assert bool(copied_rows["inherited"].tolist()[1]) is True
    assert "skills/no/SKILL.md" not in set(rows["path"])
    root = rows.loc[rows["path"].eq("SKILL.md")].iloc[0]
    assert root.file_sha == "sha-root"
    assert int(root.version_seq) == 1


def _family_connection() -> tuple[object, pd.DataFrame]:
    """A tiny catalog: one hub text, one source family, one cluster, one singleton."""
    con = connect_duckdb()
    con.execute("CREATE TABLE skill_language (file_sha VARCHAR, is_english BOOLEAN)")
    con.executemany(
        "INSERT INTO skill_language VALUES (?, ?)",
        [
            ("sha-hub", True),
            ("sha-source", True),
            ("sha-copy", True),
            ("sha-a", True),
            ("sha-b", True),
            ("sha-lone", True),
            ("sha-fr", False),
        ],
    )
    con.execute(
        "CREATE TABLE artifacts (repo_full_name VARCHAR, path VARCHAR, file_sha VARCHAR)"
    )
    con.executemany(
        "INSERT INTO artifacts VALUES (?, ?, ?)",
        [
            ("hub/reg", "skills/a/SKILL.md", "sha-hub"),
            ("plain/reg", "skills/b/SKILL.md", "sha-source"),
            ("c/copy", "skills/b/SKILL.md", "sha-copy"),
            ("c/fork", "skills/b/SKILL.md", "sha-copy"),
            ("plain/other", "skills/b/SKILL.md", "sha-copy"),
            ("e/one", "x/SKILL.md", "sha-a"),
            ("e/two", "x/SKILL.md", "sha-b"),
            ("f/lone", "y/SKILL.md", "sha-lone"),
        ],
    )
    con.execute("CREATE TABLE repos (full_name VARCHAR, owner VARCHAR, is_fork INTEGER)")
    con.executemany(
        "INSERT INTO repos VALUES (?, ?, ?)",
        [
            ("hub/reg", "hub", 0),
            ("plain/reg", "plain", 0),
            ("c/copy", "c", 0),
            ("c/fork", "c", 1),
            ("plain/other", "plain", 0),
            ("e/one", "e", 0),
            ("e/two", "e", 0),
            ("f/lone", "f", 0),
        ],
    )
    con.execute("CREATE TABLE repo_roles (repo VARCHAR, is_registry BOOLEAN)")
    con.executemany(
        "INSERT INTO repo_roles VALUES (?, ?)",
        [("hub/reg", True), ("plain/reg", True)],
    )
    con.execute("CREATE TABLE lineages (lineage_id VARCHAR, repo VARCHAR)")
    con.executemany(
        "INSERT INTO lineages VALUES (?, ?)",
        [
            ("hub/reg::skills/a", "hub/reg"),
            ("plain/reg::skills/b", "plain/reg"),
            ("child/reg::skills/b", "child/reg"),
        ],
    )
    con.execute(
        "CREATE TABLE lineage_versions "
        "(lineage_id VARCHAR, version_seq INTEGER, blob VARCHAR, release_date VARCHAR)"
    )
    con.executemany(
        "INSERT INTO lineage_versions VALUES (?, ?, ?, ?)",
        [
            ("hub/reg::skills/a", 1, "sha-hub", "2026-01-01"),
            ("plain/reg::skills/b", 1, "sha-source", "2026-01-02"),
        ],
    )
    con.execute("CREATE TABLE lineage_roots (lineage_id VARCHAR, root_lineage VARCHAR)")
    con.executemany(
        "INSERT INTO lineage_roots VALUES (?, ?)",
        [
            ("hub/reg::skills/a", "hub/reg::skills/a"),
            ("plain/reg::skills/b", "plain/reg::skills/b"),
            ("child/reg::skills/b", "hub/reg::skills/a"),
        ],
    )
    con.execute("CREATE TABLE registry_hubs (repo VARCHAR, is_hub BOOLEAN)")
    con.executemany(
        "INSERT INTO registry_hubs VALUES (?, ?)",
        [("hub/reg", True), ("plain/reg", False), ("child/reg", False)],
    )
    con.execute(
        "CREATE TABLE clusters (file_sha VARCHAR, cluster_id INTEGER, threshold DOUBLE)"
    )
    con.executemany(
        "INSERT INTO clusters VALUES (?, ?, ?)",
        [
            ("sha-a", 7, 0.8),
            ("sha-b", 7, 0.8),
            ("sha-lone", 9, 0.8),
            ("sha-a", 1, 0.6),
        ],
    )
    copies = pd.DataFrame(
        [
            {
                "repo": "c/copy",
                "path": "skills/b/SKILL.md",
                "file_sha": "sha-copy",
                "lineage_id": "plain/reg::skills/b",
                "root_lineage": "plain/reg::skills/b",
                "link": "near",
                "release_date": "2026-01-02",
                "source_is_hub": False,
                "is_fork": False,
                "same_owner_as_registry": False,
            },
            {
                "repo": "c/fork",
                "path": "skills/b/SKILL.md",
                "file_sha": "sha-copy",
                "lineage_id": "plain/reg::skills/b",
                "root_lineage": "plain/reg::skills/b",
                "link": "near",
                "release_date": "2026-01-02",
                "source_is_hub": False,
                "is_fork": True,
                "same_owner_as_registry": False,
            },
            {
                "repo": "plain/other",
                "path": "skills/b/SKILL.md",
                "file_sha": "sha-copy",
                "lineage_id": "plain/reg::skills/b",
                "root_lineage": "plain/reg::skills/b",
                "link": "near",
                "release_date": "2026-01-02",
                "source_is_hub": False,
                "is_fork": False,
                "same_owner_as_registry": True,
            },
            {
                "repo": "c/child",
                "path": "skills/a/SKILL.md",
                "file_sha": "sha-child",
                "lineage_id": "child/reg::skills/b",
                "root_lineage": "hub/reg::skills/a",
                "link": "near",
                "release_date": "2026-02-01",
                "source_is_hub": False,
                "is_fork": False,
                "same_owner_as_registry": False,
            },
        ]
    )
    return con, copies


def test_family_kinds() -> None:
    """A hub root is hub_only. Two unlinked texts are one cluster. A lone text is a singleton."""
    con, copies = _family_connection()
    families = build_text_families(con, copies, True)
    assert "sha-fr" not in set(families["file_sha"])
    hub = families.loc[families["file_sha"].eq("sha-hub")].iloc[0]
    source = families.loc[families["file_sha"].eq("sha-source")].iloc[0]
    copied = families.loc[families["file_sha"].eq("sha-copy")].iloc[0]
    left = families.loc[families["file_sha"].eq("sha-a")].iloc[0]
    right = families.loc[families["file_sha"].eq("sha-b")].iloc[0]
    lone = families.loc[families["file_sha"].eq("sha-lone")].iloc[0]
    assert hub.family_kind == "hub_only"
    assert hub.family_id == "hub/reg::skills/a"
    assert bool(hub.registry_self) is True
    assert pd.isna(hub.best_link) or hub.best_link is None
    assert int(hub.files) == 1
    assert source.family_kind == "source"
    assert bool(source.registry_self) is True
    assert source.family_id == "plain/reg::skills/b"
    assert copied.family_kind == "source"
    assert bool(copied.registry_self) is False
    assert copied.best_link == "near"
    assert int(copied.files) == 3
    assert int(copied.repos) == 3
    assert int(copied.owners) == 2
    assert int(copied.files_without_forks) == 1
    assert int(copied.repos_without_forks) == 1
    assert int(source.files) == int(copied.files)
    assert left.family_kind == "independent"
    assert left.family_id == "cluster:7"
    assert right.family_id == "cluster:7"
    assert int(left.files) == 2
    assert int(left.repos) == 2
    assert int(left.owners) == 1
    assert lone.family_kind == "singleton"
    assert lone.family_id == "single:sha-lone"
    assert bool(lone.registry_self) is False
    assert int(lone.files) == 1


def _skill_lines(prefix: str, count: int, words: int) -> str:
    """``count`` lines. Each line has ``words`` tokens that no other line uses."""
    rows = []
    for index in range(count):
        tokens = [f"{prefix}{index:02d}w{word:02d}" for word in range(words)]
        rows.append(" ".join(tokens))
    return "\n".join(rows)


def _recall_of(frame: pd.DataFrame, method: str) -> pd.Series:
    """The one recall row for ``method``."""
    return frame.loc[frame["method"].eq(method)].iloc[0]


def test_link_method_recall_on_three_proven_pairs() -> None:
    """Unchanged is found by every method. A grown copy is containment only. Reworded is missed.

    The grown copy keeps 40 registry lines and adds 60 new lines, so 60% of
    its lines are new. A later registry version matches none of the three,
    and the older version still counts.
    """
    registry = _skill_lines("reg", 40, 12)
    grown = registry + "\n" + _skill_lines("new", 60, 5)
    reworded = _skill_lines("other", 40, 12)
    later = _skill_lines("later", 10, 12)
    grown_lines = grown.splitlines()
    assert len(grown_lines) == 100
    assert sum(line.startswith("new") for line in grown_lines) / len(grown_lines) == 0.6

    unchanged_flags = score_methods_on_pair([later, registry], registry)
    grown_flags = score_methods_on_pair([later, registry], grown)
    reworded_flags = score_methods_on_pair([later, registry], reworded)
    assert unchanged_flags == {
        "exact": True,
        "minhash_jaccard": True,
        "containment": True,
        "line_check": True,
        "any_text_method": True,
    }
    assert grown_flags["exact"] is False
    assert grown_flags["minhash_jaccard"] is False
    assert grown_flags["containment"] is True
    assert grown_flags["line_check"] is True
    assert grown_flags["any_text_method"] is True
    assert reworded_flags["any_text_method"] is False
    assert score_methods_on_pair([later], registry)["exact"] is False

    lineage = "plain/reg::skills/grown"
    bodies = {"same": registry, "grown": grown, "reworded": reworded}
    copies = []
    histories = []
    for name in ("same", "grown", "reworded"):
        copies.append(
            {
                "repo": "a/r",
                "path": f"skills/{name}/SKILL.md",
                "file_sha": name,
                "lineage_id": lineage,
                "link": "history_exact",
                "is_fork": False,
            }
        )
        histories.append(
            {
                "repo": "a/r",
                "path": f"skills/{name}/SKILL.md",
                "blob": name,
                "commit_date": "2026-06-01T00:00:00Z",
                "version_seq": 1,
            }
        )
    copies.append(
        {
            "repo": "a/r",
            "path": "skills/near/SKILL.md",
            "file_sha": "near",
            "lineage_id": lineage,
            "link": "near",
            "is_fork": False,
        }
    )
    pairs = build_proven_pairs(pd.DataFrame(copies), pd.DataFrame(histories))
    assert list(pairs["path"]) == [
        "skills/grown/SKILL.md",
        "skills/reworded/SKILL.md",
        "skills/same/SKILL.md",
    ]
    versions = pd.DataFrame(
        [
            {"lineage_id": lineage, "version_seq": 2, "blob": "later"},
            {"lineage_id": lineage, "version_seq": 1, "blob": "v1"},
        ]
    )
    texts = {"v1": registry, "later": later, **bodies}
    recall = measure_link_method_recall(pairs, versions, texts)
    assert list(recall["method"]) == [
        "exact",
        "minhash_jaccard",
        "containment",
        "line_check",
        "any_text_method",
    ]
    assert int(_recall_of(recall, "exact").found) == 1
    assert int(_recall_of(recall, "minhash_jaccard").found) == 1
    assert int(_recall_of(recall, "containment").found) == 2
    assert int(_recall_of(recall, "line_check").found) == 2
    assert int(_recall_of(recall, "any_text_method").found) == 2
    assert _recall_of(recall, "exact").recall == pytest.approx(1 / 3)
    assert _recall_of(recall, "containment").recall == pytest.approx(2 / 3)
    assert _recall_of(recall, "exact").recall_without_forks == pytest.approx(1 / 3)


def test_forks_inflate_recall_and_edges() -> None:
    """A matching fork raises recall. The current blob is the newest one, not an older match."""
    registry = _skill_lines("reg", 40, 12)
    reworded = _skill_lines("other", 40, 12)
    copies = pd.DataFrame(
        [
            {
                "repo": "a/fork",
                "path": "skills/a/SKILL.md",
                "file_sha": "fork-sha",
                "lineage_id": "L",
                "link": "history_exact",
                "is_fork": True,
            },
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "file_sha": "edit-sha",
                "lineage_id": "L",
                "link": "history_exact",
                "is_fork": False,
            },
        ]
    )
    histories = pd.DataFrame(
        [
            {
                "repo": "a/fork",
                "path": "skills/a/SKILL.md",
                "blob": "fork-sha",
                "commit_date": "2026-02-01T00:00:00Z",
                "version_seq": 1,
            },
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "blob": "old-match",
                "commit_date": "2026-01-01T00:00:00Z",
                "version_seq": 1,
            },
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "blob": "edit-sha",
                "commit_date": "2026-06-01T00:00:00Z",
                "version_seq": 2,
            },
        ]
    )
    pairs = build_proven_pairs(copies, histories)
    edited = pairs.loc[pairs["repo"].eq("a/r")].iloc[0]
    assert edited.current_text_sha == "edit-sha"
    versions = pd.DataFrame([{"lineage_id": "L", "version_seq": 1, "blob": "v1"}])
    texts = {"v1": registry, "fork-sha": registry, "edit-sha": reworded, "old-match": registry}
    recall = measure_link_method_recall(pairs, versions, texts)
    assert int(_recall_of(recall, "exact").found) == 1
    assert _recall_of(recall, "exact").recall == pytest.approx(0.5)
    assert _recall_of(recall, "exact").recall_without_forks == pytest.approx(0.0)

    dataset_only = pd.DataFrame(
        [
            {
                "repo": "b/r",
                "path": "skills/a/SKILL.md",
                "file_sha": "dataset",
                "lineage_id": "L",
                "link": "history_exact",
            }
        ]
    )
    fallen_back = build_proven_pairs(dataset_only, pd.DataFrame())
    assert fallen_back["current_text_sha"].iloc[0] == "dataset"
    empty = build_proven_pairs(
        pd.DataFrame(columns=["repo", "path", "lineage_id", "link"]),
        pd.DataFrame(),
    )
    assert empty.empty
    zeros = measure_link_method_recall(empty)
    assert int(zeros["proven_pairs"].iloc[0]) == 0
    assert "tfidf" not in set(zeros["method"])
    with pytest.raises(ValueError):
        measure_link_method_recall(pd.DataFrame([{"repo": "a/r"}]), versions=versions)


def test_tfidf_column_is_counted_only_when_present() -> None:
    """A G30 score of at least 0.80 is one more recall row. Below 0.80 is a miss."""
    scored = pd.DataFrame(
        {
            "exact": [True, False, False],
            "jaccard_found": [True, False, False],
            "containment_found": [True, True, False],
            "verified_found": [True, True, False],
            "is_fork": [False, False, True],
            "tfidf_instructions": [0.93, 0.10, 0.50],
        }
    )
    recall = measure_link_method_recall(scored)
    tfidf = _recall_of(recall, "tfidf")
    assert list(recall["method"])[-1] == "tfidf"
    assert int(tfidf.found) == 1
    assert tfidf.recall == pytest.approx(1 / 3)
    assert tfidf.recall_without_forks == pytest.approx(0.5)
    empty_flags = score_methods_on_pair([""], "")
    assert empty_flags["exact"] is True
    assert empty_flags["minhash_jaccard"] is False
    assert empty_flags["line_check"] is False
    assert empty_flags["any_text_method"] is True


def test_same_date_keeps_the_higher_version_seq() -> None:
    """Two history rows on one date: the higher version_seq is the current text."""
    copies = pd.DataFrame(
        [
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "file_sha": "dataset",
                "lineage_id": "L",
                "link": "history_exact",
            }
        ]
    )
    histories = pd.DataFrame(
        [
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "blob": "low",
                "commit_date": "2026-06-01T00:00:00Z",
                "version_seq": 1,
            },
            {
                "repo": "a/r",
                "path": "skills/a/SKILL.md",
                "blob": "high",
                "commit_date": "2026-06-01T00:00:00Z",
                "version_seq": 4,
            },
        ]
    )
    pairs = build_proven_pairs(copies, histories)
    assert pairs["current_text_sha"].iloc[0] == "high"
