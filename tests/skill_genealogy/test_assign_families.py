"""Tests for stage 7a, assign families.

Tiny hand-made tables. The dates follow the worked example of A33: registry
v1 (Jan 10), v2 (Mar 1), v3 (May 15). No dataset files.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from skill_genealogy import assign_families as stage
from skill_genealogy import settings
from skill_genealogy.file_helpers import connect_duckdb
from skill_genealogy.find_copies import (
    build_copy_band_index,
    registry_signature_table,
    signature_for_text,
)

RELEASES = pd.to_datetime(["2026-01-10", "2026-03-01", "2026-05-15"], utc=True)


def utc(*dates: str | None) -> pd.Series:
    """UTC timestamps (``None`` becomes ``NaT``)."""
    return pd.Series(pd.to_datetime(list(dates), utc=True, format="ISO8601"))


def registry_versions() -> pd.DataFrame:
    """Lineage L with three versions, as ``version_table`` returns them."""
    return pd.DataFrame(
        {
            "lineage_id": ["L"] * 3,
            "version_seq": [1, 2, 3],
            "blob": ["v1blob", "v2blob", "v3blob"],
            "release_date": RELEASES,
            "repo": ["reg/one"] * 3,
            "version_id": ["L#1", "L#2", "L#3"],
        }
    )


def empty_book() -> dict[str, object]:
    """A signature book with no signatures."""
    return stage.signature_book([], np.zeros((0, settings.MINHASH_NUM_PERM)), np.zeros(0))


def copy_row(path: str, link: str, seq: int, first: str | None, text: str = "t") -> dict:
    """One copy file of lineage L for ``choose_origin_version``."""
    return {
        "repo": "a/r",
        "path": path,
        "link": link,
        "lineage_id": "L",
        "version_seq": seq,
        "first_commit_date": pd.Timestamp(first, tz="UTC") if first else pd.NaT,
        "origin_text": text,
    }


def history(rows: list[tuple]) -> pd.DataFrame:
    """History rows from ``(path, commit, seq, date, status, blob)`` tuples in repo a/r."""
    frame = pd.DataFrame(rows, columns=["path", "commit", "commit_seq", "date", "status", "blob"])
    frame["repo"] = "a/r"
    frame["skill_folder"] = frame["path"].map(stage.skill_folder_of)
    frame["commit_date"] = pd.to_datetime(frame.pop("date"), utc=True)
    frame["history_role"] = "own"
    frame["inherited"] = False
    frame["repo_created_at"] = frame["commit_date"].min()
    texts = frame.loc[frame["status"] != "D"].drop_duplicates(["path", "blob"]).copy()
    texts["version_seq"] = texts.groupby("path").cumcount() + 1
    frame = frame.merge(
        texts.loc[:, ["path", "blob", "version_seq"]], on=["path", "blob"], how="left"
    )
    frame.loc[frame["status"] == "D", "version_seq"] = pd.NA
    return frame


# A33: origin version by date


def test_origin_is_newest_version_released_before_first_commit():
    files = pd.DataFrame(
        [
            copy_row("feb/SKILL.md", "near", 3, "2026-02-01"),
            copy_row("jan/SKILL.md", "near", 3, "2026-01-01"),
        ]
    )
    books = {"copy": empty_book(), "registry": empty_book()}
    origin = stage.choose_origin_version(files, registry_versions(), books)
    feb, jan = origin.iloc[0], origin.iloc[1]
    assert int(feb["origin_seq"]) == 1
    assert feb["origin_basis"] == "similar_before_date"
    assert feb["origin_date"] == RELEASES[0]
    assert not feb["copied_before_release"]
    assert pd.isna(jan["origin_seq"]) and jan["origin_basis"] is None
    assert jan["copied_before_release"]


def test_origin_prefers_the_most_similar_eligible_version():
    # The copy started from v1's text in April; v2 is newer but less similar.
    v1 = np.full(settings.MINHASH_NUM_PERM, 1)
    v2 = np.concatenate([np.full(64, 1), np.full(64, 2)])
    registry = stage.signature_book(["L#1", "L#2", "L#3"], np.vstack([v1, v2, v2]), np.ones(3))
    copy = stage.signature_book(["t"], v1.reshape(1, -1), np.ones(1))
    files = pd.DataFrame([copy_row("apr/SKILL.md", "near", 2, "2026-04-20")])
    origin = stage.choose_origin_version(
        files, registry_versions(), {"copy": copy, "registry": registry}
    )
    assert int(origin.loc[0, "origin_seq"]) == 1


def test_no_date_keeps_family_but_no_origin_and_exact_keeps_identical_version():
    files = pd.DataFrame(
        [copy_row("near/SKILL.md", "near", 2, None), copy_row("exact/SKILL.md", "exact", 2, None)]
    )
    origin = stage.choose_origin_version(
        files, registry_versions(), {"copy": empty_book(), "registry": empty_book()}
    )
    near, exact = origin.iloc[0], origin.iloc[1]
    assert near["origin_basis"] == "no_date" and pd.isna(near["origin_seq"])
    assert not near["copied_before_release"]
    assert exact["origin_basis"] == "exact" and int(exact["origin_seq"]) == 2
    assert exact["origin_date"] == RELEASES[1]


def test_exact_origin_released_after_first_commit_is_before_release():
    files = pd.DataFrame([copy_row("early/SKILL.md", "exact", 3, "2026-04-01")])
    origin = stage.choose_origin_version(
        files, registry_versions(), {"copy": empty_book(), "registry": empty_book()}
    )
    assert int(origin.loc[0, "origin_seq"]) == 3
    assert origin.loc[0, "copied_before_release"]


# A30 + A32: history evidence beats a near text link


def test_history_through_v2_blob_is_history_exact_and_beats_near():
    rows = history(
        [
            ("s/SKILL.md", "c1", 1, "2026-03-10", "A", "v2blob"),
            ("s/SKILL.md", "c2", 2, "2026-04-01", "M", "edited"),
        ]
    )
    exact = stage.find_history_exact_links(rows, registry_versions())
    assert exact[["lineage_id", "version_seq", "link"]].values.tolist() == [
        ["L", 2, "history_exact"]
    ]
    near = pd.DataFrame(
        {
            "repo": ["a/r"],
            "path": ["s/SKILL.md"],
            "lineage_id": ["L"],
            "version_seq": [3],
            "release_date": [RELEASES[2]],
            "link": ["near"],
            "link_similarity": [0.85],
        }
    )
    winner = stage.pick_strongest_link(pd.concat([near, exact], ignore_index=True))
    assert winner.loc[0, "link"] == "history_exact"
    files = winner.assign(first_commit_date=pd.Timestamp("2026-03-10", tz="UTC"), origin_text="x")
    origin = stage.choose_origin_version(
        files, registry_versions(), {"copy": empty_book(), "registry": empty_book()}
    )
    assert int(origin.loc[0, "origin_seq"]) == 2 and origin.loc[0, "origin_basis"] == "exact"


def test_history_exact_takes_the_earliest_matching_row():
    rows = history(
        [
            ("s/SKILL.md", "c1", 1, "2026-02-01", "A", "v1blob"),
            ("s/SKILL.md", "c2", 2, "2026-03-05", "M", "v2blob"),
        ]
    )
    links = stage.find_history_exact_links(rows, registry_versions())
    assert links["version_seq"].tolist() == [1]


def test_strongest_link_order_with_all_six_links():
    kinds = ["cluster", "history_near", "near_contained", "near", "history_exact", "exact"]
    links = pd.DataFrame(
        {
            "repo": ["a/r"] * 6,
            "path": ["SKILL.md"] * 6,
            "lineage_id": [f"L{kind}" for kind in kinds],
            "version_seq": [1] * 6,
            "release_date": pd.to_datetime(["2026-01-01"] * 6, utc=True),
            "link": kinds,
            "link_similarity": [0.6, 0.9, 0.9, 0.9, 1.0, 1.0],
        }
    )
    order = []
    remaining = links
    while len(remaining):
        best = stage.pick_strongest_link(remaining).loc[0, "link"]
        order.append(best)
        remaining = remaining.loc[remaining["link"] != best]
    assert tuple(order) == stage.LINK_ORDER


def test_strongest_link_tie_goes_to_oldest_version():
    links = pd.DataFrame(
        {
            "repo": ["a/r"] * 2,
            "path": ["SKILL.md"] * 2,
            "lineage_id": ["New", "Old"],
            "version_seq": [1, 1],
            "release_date": pd.to_datetime(["2026-05-01", "2026-01-01"], utc=True),
            "link": ["near", "near"],
            "link_similarity": [0.9, 0.9],
        }
    )
    assert stage.pick_strongest_link(links).loc[0, "lineage_id"] == "Old"


# A31: history_near scores only the first downloaded version


def test_history_near_links_the_first_version_only():
    registry_text = " ".join(f"word{n}" for n in range(80))
    far_text = " ".join(f"other{n}" for n in range(80))
    sig, size = signature_for_text(registry_text, set())
    far_sig, far_size = signature_for_text(far_text, set())
    versions = registry_versions().iloc[:1]
    registry = registry_signature_table(versions, ["L#1"], sig.reshape(1, -1), np.array([size]))
    registry["index"] = build_copy_band_index(registry["signatures"])
    rows = history(
        [
            ("near/SKILL.md", "c1", 1, "2026-02-01", "A", "first_near"),
            ("late/SKILL.md", "c2", 2, "2026-02-01", "A", "first_far"),
            ("late/SKILL.md", "c3", 3, "2026-03-01", "M", "later_near"),
        ]
    )
    text_sigs = stage.signature_book(
        ["first_near", "first_far", "later_near"],
        np.vstack([sig, far_sig, sig]),
        np.array([size, far_size, size]),
    )
    files = rows.loc[:, ["repo", "path"]].drop_duplicates()
    links = stage.find_history_near_links(rows, registry, files, text_sigs)
    assert links[["path", "lineage_id", "link"]].values.tolist() == [
        ["near/SKILL.md", "L", "history_near"]
    ]
    assert links.loc[0, "link_similarity"] == 1.0


# A34c: arrival groups and removed copies


def test_one_folder_commit_is_chosen_and_25_folder_commit_is_bundle():
    rows = [("s0/SKILL.md", "c1", 1, "2026-01-01", "A", "b0")]
    rows += [(f"s{n}/SKILL.md", "c2", 2, "2026-02-01", "A", f"b{n}") for n in range(1, 26)]
    histories = history(rows)
    groups = stage.assign_arrival_groups(histories, histories.loc[:, ["repo", "path"]])
    first = groups.loc[groups["path"] == "s0/SKILL.md"].iloc[0]
    later = groups.loc[groups["path"] == "s5/SKILL.md"].iloc[0]
    assert (first["arrival_commit"], int(first["batch_size"]), first["arrival_group"]) == (
        "c1",
        1,
        "chosen",
    )
    assert first["arrival_in_first_commit"]
    assert (int(later["batch_size"]), later["arrival_group"]) == (25, "bundle")
    assert not later["arrival_in_first_commit"]


def test_own_unlinked_skills_in_the_same_commit_do_not_count():
    rows = [(f"copy{n}/SKILL.md", "c1", 1, "2026-01-01", "A", f"c{n}") for n in range(2)]
    rows += [(f"own{n}/SKILL.md", "c1", 1, "2026-01-01", "A", f"o{n}") for n in range(30)]
    histories = history(rows)
    copies = pd.DataFrame({"repo": ["a/r"] * 2, "path": ["copy0/SKILL.md", "copy1/SKILL.md"]})
    groups = stage.assign_arrival_groups(histories, copies)
    assert groups["batch_size"].tolist() == [2, 2]
    assert set(groups["arrival_group"]) == {"chosen"}


def test_renamed_copy_keeps_its_first_commit():
    histories = history(
        [
            ("new/SKILL.md", "c1", 1, "2026-01-01", "A", "x"),
            ("new/SKILL.md", "c3", 3, "2026-03-01", "R", "x"),
        ]
    )
    histories.loc[0, "history_role"] = "former_folder"
    groups = stage.assign_arrival_groups(histories, histories.loc[:1, ["repo", "path"]])
    assert groups.loc[0, "arrival_commit"] == "c1"
    assert groups.loc[0, "arrival_date"] == pd.Timestamp("2026-01-01", tz="UTC")


def test_folder_without_history_is_unknown_and_last_delete_is_removed():
    histories = history(
        [
            ("kept/SKILL.md", "c1", 1, "2026-01-01", "A", "x"),
            ("gone/SKILL.md", "c1", 1, "2026-01-01", "A", "y"),
            ("gone/SKILL.md", "c2", 2, "2026-04-02", "D", None),
        ]
    )
    copies = pd.DataFrame(
        {"repo": ["a/r", "a/r", "z/gone"], "path": ["kept/SKILL.md", "gone/SKILL.md", "SKILL.md"]}
    )
    groups = stage.assign_arrival_groups(histories, copies).set_index("path")
    assert not groups.loc["kept/SKILL.md", "removed"]
    assert groups.loc["gone/SKILL.md", "removed"]
    assert groups.loc["gone/SKILL.md", "removed_date"] == pd.Timestamp("2026-04-02", tz="UTC")
    assert groups.loc["SKILL.md", "arrival_group"] == "unknown"
    assert pd.isna(groups.loc["SKILL.md", "batch_size"])


# A34: family kinds


def family_connection() -> object:
    """DuckDB tables for ``build_text_families``.

    Texts: ``linked`` (a copy of L), ``crawled`` (unlinked, held by a copier,
    also in cluster 7), ``twin_a`` and ``twin_b`` (unlinked, cluster 7),
    ``alone`` (unlinked, no cluster), ``regtext`` (only in the registry, v1 of L).
    """
    con = connect_duckdb()
    tables = {
        "artifacts": pd.DataFrame(
            {
                "repo_full_name": [
                    "u/one",
                    "u/two",
                    "fork/one",
                    "u/three",
                    "u/four",
                    "u/five",
                    "reg/one",
                ],
                "path": ["SKILL.md"] * 7,
                "file_sha": [
                    "linked",
                    "linked",
                    "linked",
                    "crawled",
                    "twin_a",
                    "twin_b",
                    "regtext",
                ],
                "first_commit_at": [None] * 7,
            }
        ),
        "skill_language": pd.DataFrame(
            {
                "file_sha": ["linked", "crawled", "twin_a", "twin_b", "alone", "regtext"],
                "is_english": [True] * 6,
            }
        ),
        "repos": pd.DataFrame({"full_name": ["fork/one"], "is_fork": [1]}),
        "registry_repos": pd.DataFrame({"repo": ["reg/one", "copier/one"]}),
        "copier_repos": pd.DataFrame({"repo": ["copier/one"]}),
        "clusters": pd.DataFrame(
            {
                "file_sha": ["crawled", "twin_a", "twin_b", "alone"],
                "cluster_id": [7, 7, 7, 9],
                "threshold": [0.8] * 4,
            }
        ),
    }
    for name, frame in tables.items():
        stage._create_table(con, name, frame)
    lineage_tables = {
        "copier_seen": pd.DataFrame({"file_sha": ["crawled"]}),
        "roots": pd.DataFrame({"lineage_id": ["L"], "root_lineage": ["L"]}),
        "versions": registry_versions().assign(blob=["regtext", "v2blob", "v3blob"]),
        "lineages": pd.DataFrame({"lineage_id": ["L"], "repo": ["reg/one"]}),
    }
    stage.register_family_inputs(con, lineage_tables)
    return con


def test_family_kinds():
    con = family_connection()
    copy_files = pd.DataFrame(
        {
            "file_sha": ["linked", "linked"],
            "root_lineage": ["L", "L"],
            "link": ["near", "history_exact"],
        }
    )
    families = stage.build_text_families(con, copy_files, dev=True).set_index("file_sha")
    assert families.loc["linked", ["family_id", "family_kind", "best_link"]].tolist() == [
        "L",
        "source",
        "history_exact",
    ]
    assert families.loc["crawled", "family_kind"] == "copier_seen"
    assert families.loc["crawled", "family_id"] == "copier:crawled"
    assert families.loc["twin_a", ["family_id", "family_kind"]].tolist() == [
        "cluster:7",
        "independent",
    ]
    assert families.loc["alone", ["family_id", "family_kind"]].tolist() == [
        "single:alone",
        "singleton",
    ]
    assert families.loc["regtext", ["family_id", "family_kind"]].tolist() == ["L", "source"]
    assert families.loc["regtext", "registry_self"]
    # Family L: 3 copy files + 1 registry file; without the fork and the
    # registry owner's own file, 2 files in 2 repos.
    assert families.loc["linked", ["files", "repos"]].tolist() == [4, 4]
    assert families.loc["linked", ["files_without_forks", "repos_without_forks"]].tolist() == [2, 2]


# A34b: copier result


def test_copier_result_by_day():
    files = pd.DataFrame(
        {
            "first_commit_date": utc("2026-01-01", "2026-03-01", None, "2026-02-01T23:00"),
            "copier_first_date": utc("2026-02-01", "2026-02-01", "2026-02-01", "2026-02-01T01:00"),
        }
    )
    result = stage.compare_with_copier(files)["copier_result"].tolist()
    assert result == ["repo_first", "copier_first", "no_date", "repo_first"]


def test_history_link_counts_per_link_and_source():
    files = pd.DataFrame(
        {
            "link": ["cluster", "history_near", "exact", "exact"],
            "history_source": ["copied", "copied", "none", "copied"],
        }
    )
    counts = stage.count_history_links(files)
    assert counts.values.tolist() == [
        ["exact", "copied", 1],
        ["exact", "none", 1],
        ["history_near", "copied", 1],
        ["cluster", "copied", 1],
    ]


# A34d: history-proven test set, recall per text method

WORDS = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()


def numbered_lines(prefix: str, count: int) -> list[str]:
    """``count`` distinct lines of nine words each, all starting with ``prefix``."""
    return [f"{prefix} {n} {' '.join(WORDS[(n + k) % 12] for k in range(7))}" for n in range(count)]


REGISTRY_TEXT = "\n".join(numbered_lines("step", 40))
UNRELATED_TEXT = "\n".join(numbered_lines("other", 40))
COPY_TEXTS = {
    "same": REGISTRY_TEXT,
    "grown": REGISTRY_TEXT + "\n" + "\n".join(numbered_lines("added", 60)),
    "reworded": "\n".join(numbered_lines("rewritten", 40)),
    "fork": REGISTRY_TEXT,
}


def proven_world() -> dict[str, object]:
    """Four history-proven copies of lineage L, its versions, texts and signature books.

    L v1 is the registry text, v2 an unrelated later version. ``fork`` is an
    unchanged copy in a fork. Each copy's newest history blob is its current
    text; ``grown`` also has an older blob equal to v1.
    """
    files = pd.DataFrame(
        {
            "repo": ["a/r", "a/r", "a/r", "f/k", "a/r"],
            "path": [f"{name}/SKILL.md" for name in ("same", "grown", "reworded", "fork", "near")],
            "file_sha": ["same", "crawled", "reworded", "fork", "near"],
            "lineage_id": ["L"] * 5,
            "link": ["history_exact"] * 4 + ["near"],
            "is_fork": [False, False, False, True, False],
        }
    )
    rows = [
        ("same/SKILL.md", "c1", 1, "2026-02-01", "A", "same"),
        ("grown/SKILL.md", "c1", 1, "2026-02-01", "A", "v1blob"),
        ("grown/SKILL.md", "c2", 2, "2026-03-01", "M", "grown"),
        ("reworded/SKILL.md", "c3", 3, "2026-04-01", "A", "reworded"),
    ]
    histories = history(rows)
    fork_row = history([("fork/SKILL.md", "c4", 1, "2026-02-01", "A", "fork")]).assign(repo="f/k")
    histories = pd.concat([histories, fork_row], ignore_index=True)
    versions = pd.DataFrame(
        {
            "lineage_id": ["L", "L"],
            "version_seq": [1, 2],
            "version_id": ["L#1", "L#2"],
            "blob": ["v1blob", "v2blob"],
            "normalized_sha": [
                stage.normalized_sha(REGISTRY_TEXT),
                stage.normalized_sha(UNRELATED_TEXT),
            ],
        }
    )
    texts = {"copies": COPY_TEXTS, "versions": {"v1blob": REGISTRY_TEXT, "v2blob": UNRELATED_TEXT}}
    return {
        "files": files,
        "histories": histories,
        "versions": versions,
        "texts": texts,
        "books": {
            "copy": book_of(COPY_TEXTS),
            "registry": book_of({"L#1": REGISTRY_TEXT, "L#2": UNRELATED_TEXT}),
        },
    }


def book_of(texts: dict[str, str]) -> dict[str, object]:
    """A signature book of these texts, signed like stage 5a (no boilerplate)."""
    signed = {key: signature_for_text(text, set()) for key, text in texts.items()}
    keys = [key for key, (_values, size) in signed.items() if size > 0]
    return stage.signature_book(
        keys,
        np.vstack([signed[key][0] for key in keys]).astype(np.uint32),
        np.asarray([signed[key][1] for key in keys]),
    )


def scored_world() -> pd.DataFrame:
    """The proven pairs of ``proven_world``, scored by the fast path, keyed by path."""
    world = proven_world()
    pairs = stage.build_proven_pairs(world["files"], world["histories"])
    scored = stage.score_proven_pairs(pairs, world["versions"], world["books"], world["texts"])
    return scored.set_index("path")


def test_link_method_recall_on_three_proven_pairs():
    scored = scored_world()
    assert "near/SKILL.md" not in scored.index
    flags = list(stage.METHOD_COLUMNS.values())
    assert scored.loc["same/SKILL.md", flags].tolist() == [True, True, True, True]
    assert scored.loc["grown/SKILL.md", flags].tolist() == [False, False, True, True]
    assert scored.loc["reworded/SKILL.md", flags].tolist() == [False, False, False, False]
    # The later version v2 matches nothing; v1 still counts.
    assert scored.loc["same/SKILL.md", "best_line_share"] == 1.0
    no_fork = scored.loc[~scored["is_fork"]].reset_index()
    recall = stage.measure_link_method_recall(no_fork).set_index("method")["recall"]
    assert recall.to_dict() == {
        "exact": 0.3333,
        "minhash_jaccard": 0.3333,
        "containment": 0.6667,
        "line_check": 0.6667,
        "any_text_method": 0.6667,
    }


def test_plain_scorer_agrees_with_the_fast_path():
    scored = scored_world()
    for path, name in (("same", "same"), ("grown", "grown"), ("reworded", "reworded")):
        plain = stage.score_methods_on_pair([REGISTRY_TEXT, UNRELATED_TEXT], COPY_TEXTS[name])
        fast = scored.loc[f"{path}/SKILL.md"]
        for method, column in stage.METHOD_COLUMNS.items():
            assert plain[method] == bool(fast[column]), (name, method)
        assert plain["any_text_method"] == bool(fast[list(stage.METHOD_COLUMNS.values())].any())


def test_forks_inflate_recall_and_the_newest_blob_is_scored():
    scored = scored_world()
    # grown's older blob is v1 itself; the scored text is the newer, grown one.
    assert scored.loc["grown/SKILL.md", "current_file_sha"] == "grown"
    assert not scored.loc["grown/SKILL.md", "current_is_dataset_text"]
    recall = stage.measure_link_method_recall(scored.reset_index()).set_index("method")
    assert recall.loc["exact", ["found", "recall", "recall_without_forks"]].tolist() == [
        2,
        0.5,
        0.3333,
    ]
    # Only grown changed since the crawl, and only containment and the line check find it.
    assert recall.loc["containment", "recall_changed_since_crawl"] == 1.0
    assert recall.loc["minhash_jaccard", "recall_changed_since_crawl"] == 0.0


def test_proven_pair_edges():
    files = pd.DataFrame(
        {
            "repo": ["a/r"],
            "path": ["x/SKILL.md"],
            "file_sha": ["crawled"],
            "lineage_id": ["L"],
            "link": ["history_exact"],
            "is_fork": [None],
        }
    )
    # No history row: the dataset text is the current text.
    pairs = stage.build_proven_pairs(files, history([]))
    assert pairs[["current_file_sha", "current_is_dataset_text", "is_fork"]].values.tolist() == [
        ["crawled", True, False]
    ]
    # Two empty texts are the same text, have no shingles and no lines to keep.
    empty = stage.score_methods_on_pair([""], "")
    assert empty == {
        "exact": True,
        "minhash_jaccard": False,
        "containment": False,
        "line_check": False,
        "any_text_method": True,
    }
    # An empty test set has recall 0 for every method; a tfidf column adds a row.
    none = stage.measure_link_method_recall(
        pd.DataFrame(columns=[*stage.METHOD_COLUMNS.values(), "is_fork", "current_is_dataset_text"])
    )
    assert none["recall"].tolist() == [0.0] * 5
    flagged = scored_world().reset_index().assign(tfidf_found=[True, False, False, False])
    assert stage.measure_link_method_recall(flagged)["method"].tolist()[-1] == "tfidf"
