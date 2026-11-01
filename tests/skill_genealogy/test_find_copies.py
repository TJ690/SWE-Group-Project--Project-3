"""Tests for stage 5: boilerplate lines, signatures, and registry re-hosts.

Tiny tables and a few hand-written texts. No full dataset.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from skill_genealogy.file_helpers import connect_duckdb
from skill_genealogy.find_copies import (
    _containment_array,
    _sign_many,
    build_boilerplate_set,
    build_dataset_signatures,
    build_registry_lsh,
    build_registry_signatures,
    compute_dated_hub_shares,
    count_owners_per_line,
    find_cluster_links,
    find_contained_links,
    find_exact_links,
    find_near_links,
    find_rehost_source,
    flag_forks_and_same_owner,
    follow_to_root,
    list_copy_files,
    load_signatures,
    signature_for_text,
    update_repo_roles,
    verify_links_with_line_diff,
)
from skill_genealogy.settings import (
    MINHASH_NUM_PERM,
    NEAR_COPY_MIN_CONTAINMENT,
    NEAR_COPY_MIN_JACCARD,
)
from skill_genealogy.text_helpers import (
    estimate_containment,
    estimate_jaccard,
    make_minhash,
    minhash_from_array,
    minhash_to_array,
)

BODY = "alpha beta gamma delta epsilon"
BOILERPLATE_LINE = "please install the tool"
SHARED_LINE = "use this shared skill"


def _register_skills(
    rows: list[tuple[str, str, str]],
    english: dict[str, bool],
) -> object:
    """Create artifacts and skill_language on a new connection.

    ``rows`` are ``(repo_full_name, file_sha, content)``.
    """
    con = connect_duckdb()
    con.execute(
        "CREATE TABLE artifacts (repo_full_name VARCHAR, file_sha VARCHAR, content VARCHAR)"
    )
    con.executemany("INSERT INTO artifacts VALUES (?, ?, ?)", rows)
    con.execute("CREATE TABLE skill_language (file_sha VARCHAR, is_english BOOLEAN)")
    language_rows = [(file_sha, flag) for file_sha, flag in english.items()]
    con.executemany("INSERT INTO skill_language VALUES (?, ?)", language_rows)
    return con


def test_count_owners_per_line_counts_distinct_owners() -> None:
    """One owner with the line twice still counts as one. A non-English text does not.

    The shared line is in three English owners. Owner ``a`` has it in two
    files, and twice inside the first file. Owner ``d`` is not English.
    ``hi there`` has two words, so it is not a row. ``one two three`` has
    three words and one owner.
    """
    con = _register_skills(
        [
            ("a/one", "s1", f"{SHARED_LINE}\n{SHARED_LINE}\n"),
            ("a/two", "s2", f"  {SHARED_LINE}  \n"),
            ("b/one", "s3", f"{SHARED_LINE}\n"),
            ("c/one", "s4", f"{SHARED_LINE}\nhi there\none two three\n"),
            ("d/one", "s5", f"{SHARED_LINE}\n"),
        ],
        {"s1": True, "s2": True, "s3": True, "s4": True, "s5": False},
    )
    frame = count_owners_per_line(con)
    counts = dict(zip(frame["line"], frame["owners"], strict=True))
    assert counts[SHARED_LINE] == 3
    assert "hi there" not in counts
    assert counts["one two three"] == 1
    assert build_boilerplate_set(frame, 3) == {SHARED_LINE}


def test_count_owners_per_line_respects_owner_floor() -> None:
    """line_owner_min drops lines below the cutoff and keeps the shared line."""
    con = _register_skills(
        [
            ("a/one", "s1", f"{SHARED_LINE}\nonly this owner wrote\n"),
            ("b/one", "s2", f"{SHARED_LINE}\n"),
            ("c/one", "s3", f"{SHARED_LINE}\n"),
        ],
        {"s1": True, "s2": True, "s3": True},
    )
    con.execute("CREATE TEMP TABLE line_owner_min (min_owners INTEGER)")
    con.execute("INSERT INTO line_owner_min VALUES (3)")
    frame = count_owners_per_line(con)
    assert list(frame["line"]) == [SHARED_LINE]
    assert int(frame.loc[0, "owners"]) == 3


def test_curly_apostrophe_line_is_one_key() -> None:
    """A curly apostrophe and a straight one are the same line, and it is removed.

    Three owners share ``don’t use this shared skill`` with a curly apostrophe.
    The stored key is the normalised line. ``signature_for_text`` drops that
    line, so the signature matches the body without it.
    """
    curly = "don\u2019t use this shared skill"
    con = _register_skills(
        [
            ("a/one", "s1", curly),
            ("b/one", "s2", curly),
            ("c/one", "s3", curly),
        ],
        {"s1": True, "s2": True, "s3": True},
    )
    frame = count_owners_per_line(con)
    boilerplate = build_boilerplate_set(frame, 3)
    assert boilerplate == {"don't use this shared skill"}
    with_line = f"{BODY}\n{curly}"
    left_values, left_size = signature_for_text(with_line, boilerplate)
    right_values, right_size = signature_for_text(BODY, boilerplate)
    assert left_size == right_size
    assert left_size > 0
    assert np.array_equal(left_values, right_values)


def test_signature_ignores_a_boilerplate_line() -> None:
    """Two bodies that differ only by a boilerplate line get the same signature."""
    boilerplate = {BOILERPLATE_LINE}
    with_line = f"{BODY}\nPlease install the tool\nzeta eta theta iota kappa"
    without_line = f"{BODY}\nzeta eta theta iota kappa"
    left_values, left_size = signature_for_text(with_line, boilerplate)
    right_values, right_size = signature_for_text(without_line, boilerplate)
    assert left_size == right_size
    assert left_size > 0
    assert left_values.dtype == np.uint64
    assert left_values.shape == (MINHASH_NUM_PERM,)
    assert np.array_equal(left_values, right_values)


def test_signature_of_only_boilerplate_is_empty() -> None:
    """A body that is only a boilerplate line has no shingles and is not stored."""
    values, size = signature_for_text("Please install the tool", {BOILERPLATE_LINE})
    assert size == 0
    assert np.array_equal(values, np.zeros(MINHASH_NUM_PERM, dtype=np.uint64))


def test_load_signatures_round_trip(tmp_path: Path) -> None:
    """A registry file and a folder of parts load back the same ids and hashes."""
    boilerplate = {BOILERPLATE_LINE}
    versions = pd.DataFrame(
        {
            "lineage_id": ["acme/app::skills/demo", "acme/app::skills/other"],
            "version_seq": [1, 2],
            "blob": ["aaa", "bbb"],
        }
    )
    texts = {
        "aaa": f"{BODY} zeta",
        "bbb": f"{BODY} zeta\nPlease install the tool",
    }
    registry_path = tmp_path / "registry_signatures.npz"
    written = build_registry_signatures(versions, texts, boilerplate, registry_path)
    assert written == 2
    ids, signatures, sizes = load_signatures(registry_path)
    assert ids == ["acme/app::skills/demo#1", "acme/app::skills/other#2"]
    assert signatures.shape == (2, MINHASH_NUM_PERM)
    assert signatures.dtype == np.uint64
    assert sizes.dtype == np.int32
    assert list(sizes) == [2, 2]
    expected, expected_size = signature_for_text(texts["aaa"], boilerplate)
    assert expected_size == 2
    assert np.array_equal(signatures[0], expected)
    assert np.array_equal(signatures[1], expected)

    folder = tmp_path / "parts"
    folder.mkdir()
    first = folder / "part_s00_c00001.npz"
    second = folder / "part_s00_c00000.npz"
    build_registry_signatures(versions.iloc[1:], texts, boilerplate, first)
    build_registry_signatures(versions.iloc[:1], texts, boilerplate, second)
    folder_ids, folder_signatures, folder_sizes = load_signatures(folder)
    assert folder_ids == [
        "acme/app::skills/demo#1",
        "acme/app::skills/other#2",
    ]
    assert np.array_equal(folder_signatures, signatures)
    assert np.array_equal(folder_sizes, sizes)

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    empty_ids, empty_signatures, empty_sizes = load_signatures(empty_dir)
    assert empty_ids == []
    assert empty_signatures.shape == (0, MINHASH_NUM_PERM)
    assert empty_sizes.shape == (0,)
    assert empty_sizes.dtype == np.int32


def test_registry_signature_skips_an_empty_body(tmp_path: Path) -> None:
    """A version whose body is only boilerplate is counted and not stored."""
    versions = pd.DataFrame(
        {
            "lineage_id": ["acme/app::skills/demo"],
            "version_seq": [1],
            "blob": ["aaa"],
        }
    )
    path = tmp_path / "registry_signatures.npz"
    written = build_registry_signatures(
        versions,
        {"aaa": "Please install the tool"},
        {BOILERPLATE_LINE},
        path,
    )
    assert written == 0
    ids, signatures, sizes = load_signatures(path)
    assert ids == []
    assert signatures.shape == (0, MINHASH_NUM_PERM)
    assert sizes.shape == (0,)


def test_dataset_signatures_round_trip_and_resume(tmp_path: Path) -> None:
    """English texts are signed, a non-English text is not, and a second run skips."""
    con = _register_skills(
        [
            ("a/one", "sha-en", f"{BODY} zeta"),
            ("b/one", "sha-other", "this file is not labelled english"),
            ("c/one", "sha-empty", "Please install the tool"),
        ],
        {"sha-en": True, "sha-other": False, "sha-empty": True},
    )
    out_dir = tmp_path / "dataset_signatures"
    signed = build_dataset_signatures(con, {BOILERPLATE_LINE}, out_dir)
    assert signed == 1
    ids, signatures, sizes = load_signatures(out_dir)
    assert ids == ["sha-en"]
    assert int(sizes[0]) == 2
    expected, _size = signature_for_text(f"{BODY} zeta", {BOILERPLATE_LINE})
    assert np.array_equal(signatures[0], expected)
    part = next(out_dir.glob("part_s??_c?????.npz"))
    first_write = part.stat().st_mtime_ns
    signed_again = build_dataset_signatures(con, {BOILERPLATE_LINE}, out_dir)
    assert signed_again == 1
    assert part.stat().st_mtime_ns == first_write


def test_pool_matches_plain_signature(monkeypatch) -> None:
    """The process pool returns the same hashes as signature_for_text."""
    monkeypatch.setattr("skill_genealogy.find_copies.POOL_MIN_TEXTS", 1)
    texts = [f"{BODY} zeta", "one two three four five six"]
    boilerplate = {BOILERPLATE_LINE}
    pooled = _sign_many(texts, boilerplate)
    for text, (values, size) in zip(texts, pooled, strict=True):
        plain_values, plain_size = signature_for_text(text, boilerplate)
        assert size == plain_size
        assert np.array_equal(values, plain_values)


def _version_row(
    lineage: str,
    repo: str,
    when: str,
    blob: str,
    *,
    jaccard: float = 0.0,
    containment: float = 0.0,
    seq: int = 1,
) -> dict[str, object]:
    """One candidate row for ``find_rehost_source``."""
    return {
        "version_id": f"{lineage}#{seq}",
        "lineage_id": lineage,
        "repo": repo,
        "version_seq": seq,
        "release_date": when,
        "blob": blob,
        "jaccard": jaccard,
        "containment": containment,
    }


def _rehost_row(lineage: str, when: str, source: dict | None) -> dict[str, object]:
    """One ``follow_to_root`` input row."""
    if source is None:
        return {
            "lineage_id": lineage,
            "copied_from_lineage": None,
            "copied_from_seq": None,
            "rehost_link": None,
            "rehost_similarity": None,
            "release_date": when,
        }
    return {
        "lineage_id": lineage,
        "copied_from_lineage": source["copied_from_lineage"],
        "copied_from_seq": source["copied_from_seq"],
        "rehost_link": source["rehost_link"],
        "rehost_similarity": source["rehost_similarity"],
        "release_date": when,
    }


def test_exact_then_near_chain_reaches_the_older_root() -> None:
    """B copies A's blob. C near-copies B. C's root is A, two steps away."""
    copied_b = find_rehost_source(
        "B",
        "B#1",
        pd.DataFrame(
            [
                _version_row("B", "b/repo", "2026-03-01", "blob-a"),
                _version_row("A", "a/repo", "2026-01-01", "blob-a"),
            ]
        ),
    )
    assert copied_b is not None
    assert copied_b["copied_from_lineage"] == "A"
    assert copied_b["copied_from_seq"] == 1
    assert copied_b["rehost_link"] == "exact"
    assert copied_b["rehost_similarity"] == 1.0
    copied_c = find_rehost_source(
        "C",
        "C#1",
        pd.DataFrame(
            [
                _version_row("C", "c/repo", "2026-04-01", "blob-c"),
                _version_row("B", "b/repo", "2026-03-01", "blob-b", jaccard=0.91, containment=0.95),
            ]
        ),
    )
    assert copied_c is not None
    assert copied_c["copied_from_lineage"] == "B"
    assert copied_c["rehost_link"] == "near"
    roots = follow_to_root(
        pd.DataFrame(
            [
                _rehost_row("A", "2026-01-01", None),
                _rehost_row("B", "2026-03-01", copied_b),
                _rehost_row("C", "2026-04-01", copied_c),
            ]
        )
    )
    copied = roots.loc[roots["lineage_id"] == "C"].iloc[0]
    assert copied["root_lineage"] == "A"
    assert int(copied["chain_length"]) == 2
    assert bool(copied["is_original"]) is False
    original = roots.loc[roots["lineage_id"] == "A"].iloc[0]
    assert original["root_lineage"] == "A"
    assert int(original["chain_length"]) == 0
    assert bool(original["is_original"]) is True


def test_same_date_is_not_a_rehost() -> None:
    """A shared blob on the same date is not strictly earlier."""
    found = find_rehost_source(
        "B",
        "B#1",
        pd.DataFrame(
            [
                _version_row("B", "b/repo", "2026-01-01", "blob-a"),
                _version_row("A", "a/repo", "2026-01-01", "blob-a"),
            ]
        ),
    )
    assert found is None


def test_same_repo_is_not_a_rehost() -> None:
    """Two folders in one registry are not a re-host of each other."""
    found = find_rehost_source(
        "B",
        "B#1",
        pd.DataFrame(
            [
                _version_row("B", "a/repo", "2026-03-01", "blob-a"),
                _version_row("A", "a/repo", "2026-01-01", "blob-a"),
            ]
        ),
    )
    assert found is None


def test_later_version_is_not_a_source() -> None:
    """A blob that appears later in another registry is not the source."""
    found = find_rehost_source(
        "A",
        "A#1",
        pd.DataFrame(
            [
                _version_row("A", "a/repo", "2026-01-01", "blob-a"),
                _version_row("B", "b/repo", "2026-03-01", "blob-a"),
            ]
        ),
    )
    assert found is None


def test_exact_beats_a_near_link_and_an_older_exact_wins() -> None:
    """Exact wins over a higher-looking near score. The oldest exact wins a tie."""
    found = find_rehost_source(
        "C",
        "C#1",
        pd.DataFrame(
            [
                _version_row("C", "c/repo", "2026-06-01", "blob"),
                _version_row("A", "a/repo", "2026-02-01", "blob"),
                _version_row("B", "b/repo", "2026-01-15", "blob"),
                _version_row("D", "d/repo", "2026-01-01", "other", jaccard=0.99, containment=0.99),
            ]
        ),
    )
    assert found is not None
    assert found["copied_from_lineage"] == "B"
    assert found["rehost_link"] == "exact"
    tied = find_rehost_source(
        "C",
        "C#1",
        pd.DataFrame(
            [
                _version_row("C", "c/repo", "2026-06-01", "blob"),
                _version_row("M", "m/repo", "2026-03-01", "blob"),
                _version_row("A", "a/repo", "2026-03-01", "blob"),
            ]
        ),
    )
    assert tied is not None
    assert tied["copied_from_lineage"] == "A"


def test_source_can_be_a_later_sequence_of_an_older_lineage() -> None:
    """The matching text can be version 3 when version 1 is a different blob."""
    found = find_rehost_source(
        "B",
        "B#1",
        pd.DataFrame(
            [
                _version_row("B", "b/repo", "2026-05-01", "blob"),
                _version_row("A", "a/repo", "2026-01-01", "other", seq=1, jaccard=0.1),
                _version_row("A", "a/repo", "2026-03-01", "blob", seq=3),
            ]
        ),
    )
    assert found is not None
    assert found["copied_from_lineage"] == "A"
    assert found["copied_from_seq"] == 3
    assert found["rehost_link"] == "exact"


def test_containment_link_when_jaccard_is_below_the_near_cutoff() -> None:
    """Jaccard under 0.80 with containment at 0.80 is a near-contained link."""
    contained = find_rehost_source(
        "C",
        "C#1",
        pd.DataFrame(
            [
                _version_row("C", "c/repo", "2026-04-01", "new"),
                _version_row("A", "a/repo", "2026-01-01", "old", jaccard=0.40, containment=0.80),
            ]
        ),
    )
    assert contained is not None
    assert contained["rehost_link"] == "near_contained"
    assert abs(float(contained["rehost_similarity"]) - 0.80) < 1e-9
    near = find_rehost_source(
        "C",
        "C#1",
        pd.DataFrame(
            [
                _version_row("C", "c/repo", "2026-04-01", "new"),
                _version_row("A", "a/repo", "2026-01-01", "old", jaccard=0.80, containment=0.99),
            ]
        ),
    )
    assert near is not None
    assert near["rehost_link"] == "near"
    assert abs(float(near["rehost_similarity"]) - 0.80) < 1e-9


def test_higher_similarity_wins_across_link_types() -> None:
    """A contained score of 0.95 beats a near score of 0.81."""
    found = find_rehost_source(
        "C",
        "C#1",
        pd.DataFrame(
            [
                _version_row("C", "c/repo", "2026-04-01", "new"),
                _version_row("Near", "n/repo", "2026-01-01", "n", jaccard=0.81, containment=0.81),
                _version_row("Wide", "w/repo", "2026-02-01", "w", jaccard=0.40, containment=0.95),
            ]
        ),
    )
    assert found is not None
    assert found["copied_from_lineage"] == "Wide"
    assert found["rehost_link"] == "near_contained"


def test_cycle_stops_at_the_oldest_lineage() -> None:
    """A date cycle ends. The root is the oldest member, and the walk stops."""
    roots = follow_to_root(
        pd.DataFrame(
            [
                _rehost_row(
                    "A",
                    "2026-01-01",
                    {
                        "copied_from_lineage": "B",
                        "copied_from_seq": 1,
                        "rehost_link": "exact",
                        "rehost_similarity": 1.0,
                    },
                ),
                _rehost_row(
                    "B",
                    "2026-05-01",
                    {
                        "copied_from_lineage": "A",
                        "copied_from_seq": 1,
                        "rehost_link": "exact",
                        "rehost_similarity": 1.0,
                    },
                ),
            ]
        )
    )
    assert set(roots["root_lineage"]) == {"A"}
    assert bool(roots["chain_cycle"].all())
    older = roots.loc[roots["lineage_id"] == "A"].iloc[0]
    newer = roots.loc[roots["lineage_id"] == "B"].iloc[0]
    assert int(older["chain_length"]) == 0
    assert bool(older["is_original"]) is False
    assert int(newer["chain_length"]) == 1
    tied = follow_to_root(
        pd.DataFrame(
            [
                _rehost_row(
                    "B",
                    "2026-01-01",
                    {
                        "copied_from_lineage": "A",
                        "copied_from_seq": 1,
                        "rehost_link": "exact",
                        "rehost_similarity": 1.0,
                    },
                ),
                _rehost_row(
                    "A",
                    "2026-01-01",
                    {
                        "copied_from_lineage": "B",
                        "copied_from_seq": 1,
                        "rehost_link": "exact",
                        "rehost_similarity": 1.0,
                    },
                ),
            ]
        )
    )
    assert set(tied["root_lineage"]) == {"A"}


def _hub_inputs(english: int, rehosted: int, other: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """English lineages, the first ``rehosted`` of them copied, plus non-English rows."""
    rows: list[dict[str, object]] = []
    roots: list[dict[str, object]] = []
    for index in range(english):
        lineage_id = f"e{index}"
        rows.append({"lineage_id": lineage_id, "repo": "r/one", "is_english": True})
        roots.append({"lineage_id": lineage_id, "is_original": index >= rehosted})
    for index in range(other):
        lineage_id = f"n{index}"
        rows.append({"lineage_id": lineage_id, "repo": "r/one", "is_english": False})
        roots.append({"lineage_id": lineage_id, "is_original": False})
    return pd.DataFrame(rows), pd.DataFrame(roots)


def test_dated_hub_share_thresholds() -> None:
    """3 of 10 is a hub, 2 of 10 is not, and 4 English lineages are not scored."""
    hub = compute_dated_hub_shares(*_hub_inputs(10, 3, other=2))
    assert abs(float(hub.loc[0, "hub_share"]) - 0.3) < 1e-9
    assert bool(hub.loc[0, "is_hub"]) is True
    assert int(hub.loc[0, "english_lineages"]) == 10
    assert int(hub.loc[0, "rehosted_lineages"]) == 3
    assert hub.loc[0, "hub_reason"] == "3 of 10 skills first appeared in older registries -> hub"
    low = compute_dated_hub_shares(*_hub_inputs(10, 2))
    assert abs(float(low.loc[0, "hub_share"]) - 0.2) < 1e-9
    assert bool(low.loc[0, "is_hub"]) is False
    assert low.loc[0, "hub_reason"] == (
        "2 of 10 skills first appeared in older registries -> not a hub"
    )
    small = compute_dated_hub_shares(*_hub_inputs(4, 4))
    assert pd.isna(small.loc[0, "hub_share"])
    assert bool(small.loc[0, "is_hub"]) is False
    assert "not scored" in str(small.loc[0, "hub_reason"])


def test_update_repo_roles_keeps_non_registry_hubs() -> None:
    """A dated registry becomes a hub. A stage-2 non-registry hub is unchanged."""
    roles = pd.DataFrame(
        [
            {
                "repo": "a/reg",
                "owner": "a",
                "stars": 100,
                "is_fork": False,
                "english_skills": 10,
                "is_registry": True,
                "is_hub": False,
                "hub_tested": True,
                "hub_share": 0.0,
                "repo_role": "registry",
            },
            {
                "repo": "kept/reg",
                "owner": "k",
                "stars": 200,
                "is_fork": False,
                "english_skills": 8,
                "is_registry": True,
                "is_hub": False,
                "hub_tested": True,
                "hub_share": 0.05,
                "repo_role": "registry",
            },
            {
                "repo": "b/hub",
                "owner": "b",
                "stars": 0,
                "is_fork": False,
                "english_skills": 5,
                "is_registry": False,
                "is_hub": True,
                "hub_tested": True,
                "hub_share": 0.6,
                "repo_role": "hub",
            },
            {
                "repo": "c/plain",
                "owner": "c",
                "stars": 1,
                "is_fork": False,
                "english_skills": 1,
                "is_registry": False,
                "is_hub": False,
                "hub_tested": False,
                "hub_share": None,
                "repo_role": "repository",
            },
        ]
    )
    hubs = pd.DataFrame(
        [
            {
                "repo": "a/reg",
                "english_lineages": 10,
                "rehosted_lineages": 3,
                "hub_share": 0.3,
                "is_hub": True,
                "hub_reason": "3 of 10 skills first appeared in older registries -> hub",
                "preview_hub_share": 0.0,
            }
        ]
    )
    updated = update_repo_roles(roles, hubs)
    assert len(updated) == len(roles)
    registry = updated.loc[updated["repo"] == "a/reg"].iloc[0]
    assert registry["repo_role"] == "registry_hub"
    assert bool(registry["is_hub"]) is True
    assert abs(float(registry["hub_share"]) - 0.3) < 1e-9
    assert registry["hub_evidence"] == "registry_history"
    assert int(registry["stars"]) == 100
    assert bool(registry["hub_tested"]) is True
    kept = updated.loc[updated["repo"] == "kept/reg"].iloc[0]
    assert kept["repo_role"] == "registry"
    assert abs(float(kept["hub_share"]) - 0.05) < 1e-9
    assert kept["hub_evidence"] == "dataset_dates"
    other = updated.loc[updated["repo"] == "b/hub"].iloc[0]
    assert other["repo_role"] == "hub"
    assert bool(other["is_hub"]) is True
    assert abs(float(other["hub_share"]) - 0.6) < 1e-9
    assert other["hub_evidence"] == "dataset_dates"
    plain = updated.loc[updated["repo"] == "c/plain"].iloc[0]
    assert plain["repo_role"] == "repository"
    assert pd.isna(plain["hub_evidence"])


def _signature_bundle(
    ids: list[str],
    matrix: np.ndarray,
    sizes: list[int],
    meta_rows: list[dict[str, object]],
) -> dict[str, object]:
    """The dict ``find_near_links`` and ``find_contained_links`` expect."""
    return {
        "ids": ids,
        "signatures": matrix,
        "sizes": np.asarray(sizes, dtype=np.int32),
        "meta": pd.DataFrame(meta_rows),
    }


def _meta_row(
    lineage: str,
    when: str,
    *,
    is_hub: bool,
    seq: int = 1,
    size: int = 1,
) -> dict[str, object]:
    """One registry version row for a signature bundle."""
    return {
        "version_id": f"{lineage}#{seq}",
        "lineage_id": lineage,
        "version_seq": seq,
        "release_date": when,
        "is_hub": is_hub,
        "repo": f"{lineage.lower()}/reg",
        "size": size,
    }


def test_exact_link_prefers_oldest_then_non_hub() -> None:
    """The oldest blob match wins. The same date prefers the non-hub lineage.

    ``sha-old`` is on a hub in January and a non-hub in May, so the hub wins
    because it is older. ``sha-tie`` is on a hub and a non-hub on the same
    day, so the non-hub wins. A text that lives only in a registry repo, and
    a non-English text, are not links.
    """
    con = connect_duckdb()
    con.execute("CREATE TABLE artifacts (repo_full_name VARCHAR, file_sha VARCHAR)")
    con.executemany(
        "INSERT INTO artifacts VALUES (?, ?)",
        [
            ("old/reg", "sha-old"),
            ("new/reg", "sha-old"),
            ("c/copy", "sha-old"),
            ("hub/reg", "sha-tie"),
            ("plain/reg", "sha-tie"),
            ("c/copy", "sha-tie"),
            ("old/reg", "sha-inside"),
            ("c/copy", "sha-fr"),
        ],
    )
    con.execute("CREATE TABLE skill_language (file_sha VARCHAR, is_english BOOLEAN)")
    con.executemany(
        "INSERT INTO skill_language VALUES (?, ?)",
        [
            ("sha-old", True),
            ("sha-tie", True),
            ("sha-inside", True),
            ("sha-fr", False),
        ],
    )
    con.execute(
        "CREATE TABLE lineage_versions "
        "(lineage_id VARCHAR, version_seq INTEGER, blob VARCHAR, release_date VARCHAR)"
    )
    con.executemany(
        "INSERT INTO lineage_versions VALUES (?, ?, ?, ?)",
        [
            ("Old", 1, "sha-old", "2026-01-01"),
            ("New", 1, "sha-old", "2026-05-01"),
            ("Hub", 1, "sha-tie", "2026-02-01"),
            ("Plain", 1, "sha-tie", "2026-02-01"),
        ],
    )
    con.execute("CREATE TABLE lineages (lineage_id VARCHAR, repo VARCHAR)")
    con.executemany(
        "INSERT INTO lineages VALUES (?, ?)",
        [("Old", "old/reg"), ("New", "new/reg"), ("Hub", "hub/reg"), ("Plain", "plain/reg")],
    )
    con.execute("CREATE TABLE registry_hubs (repo VARCHAR, is_hub BOOLEAN)")
    con.executemany(
        "INSERT INTO registry_hubs VALUES (?, ?)",
        [("old/reg", True), ("new/reg", False), ("hub/reg", True), ("plain/reg", False)],
    )
    con.execute("CREATE TABLE registry_repo_list (repo VARCHAR)")
    con.executemany(
        "INSERT INTO registry_repo_list VALUES (?)",
        [("old/reg",), ("new/reg",), ("hub/reg",), ("plain/reg",)],
    )
    found = find_exact_links(con, True)
    by_sha = dict(zip(found["file_sha"], found["lineage_id"], strict=True))
    assert by_sha == {"sha-old": "Old", "sha-tie": "Plain"}
    assert set(found["link"]) == {"exact"}
    assert float(found["jaccard"].iloc[0]) == 1.0


def _phrase_set(prefix: str, count: int) -> set[str]:
    """``count`` distinct shingles. The prefix keeps two sets from overlapping."""
    return {f"{prefix}-{index}" for index in range(count)}


def test_near_links_keep_high_jaccard_and_drop_half() -> None:
    """Jaccard about 0.91 links. Jaccard about 0.53 does not. A tie prefers non-hub.

    The copy shares 95 of 100 shingles with two identical registry versions
    (Jaccard 95/105). One is an older hub and one is a newer non-hub, so the
    non-hub wins. A third version shares 100 of 200 shingles and is not a link.
    """
    shared = _phrase_set("shared", 95)
    near_set = shared | _phrase_set("only-reg", 5)
    copy_set = shared | _phrase_set("only-copy", 5)
    half_reg = _phrase_set("half", 100)
    half_copy = half_reg | _phrase_set("added", 100)
    near_values = minhash_to_array(make_minhash(near_set))
    half_values = minhash_to_array(make_minhash(half_reg))
    copy_values = minhash_to_array(make_minhash(copy_set))
    half_copy_values = minhash_to_array(make_minhash(half_copy))
    assert estimate_jaccard(minhash_from_array(near_values), minhash_from_array(copy_values)) > 0.9
    half_jaccard = estimate_jaccard(
        minhash_from_array(half_values), minhash_from_array(half_copy_values)
    )
    assert 0.4 < half_jaccard < NEAR_COPY_MIN_JACCARD
    matrix = np.vstack([near_values, near_values, half_values])
    meta = [
        _meta_row("Hub", "2026-01-01", is_hub=True, size=100),
        _meta_row("Plain", "2026-06-01", is_hub=False, size=100),
        _meta_row("Half", "2026-01-01", is_hub=False, size=100),
    ]
    bundle = _signature_bundle(["Hub#1", "Plain#1", "Half#1"], matrix, [100, 100, 100], meta)
    index = build_registry_lsh(["Hub#1", "Plain#1", "Half#1"], matrix, NEAR_COPY_MIN_JACCARD)
    dataset = (
        ["near-copy", "half-copy"],
        np.vstack([copy_values, half_copy_values]),
        np.array([100, 200]),
    )
    found = find_near_links(dataset, index, bundle)
    assert list(found["file_sha"]) == ["near-copy"]
    assert found.loc[0, "lineage_id"] == "Plain"
    assert found.loc[0, "link"] == "near"
    assert float(found.loc[0, "jaccard"]) >= NEAR_COPY_MIN_JACCARD


def _word_line(index: int) -> str:
    """One body line of five words, unique to ``index``."""
    return f"w{index} alpha beta gamma delta"


def test_contained_link_when_the_copy_adds_lines() -> None:
    """Forty shared lines plus sixty new lines are contained, not a near copy.

    The registry body is 40 lines. The copy keeps those lines and adds 60.
    Jaccard stays under 0.80 and containment stays at least 0.80, so the
    link is ``near_contained``. The matrix formula matches ``estimate_containment``.
    """
    registry_text = "\n".join(_word_line(index) for index in range(40))
    copy_text = "\n".join(_word_line(index) for index in range(100))
    registry_values, registry_size = signature_for_text(registry_text, set())
    copy_values, copy_size = signature_for_text(copy_text, set())
    jaccard = estimate_jaccard(
        minhash_from_array(registry_values), minhash_from_array(copy_values)
    )
    containment = estimate_containment(jaccard, registry_size, copy_size)
    assert jaccard < NEAR_COPY_MIN_JACCARD
    assert containment >= NEAR_COPY_MIN_CONTAINMENT
    matrix_jaccard = float(
        np.count_nonzero(registry_values == copy_values) / MINHASH_NUM_PERM
    )
    matrix_containment = float(
        _containment_array(
            np.array([matrix_jaccard]),
            np.array([float(registry_size)]),
            copy_size,
        )[0]
    )
    assert abs(matrix_jaccard - jaccard) < 1e-12
    assert abs(matrix_containment - containment) < 1e-12
    bundle = _signature_bundle(
        ["Reg#1"],
        np.vstack([registry_values]),
        [registry_size],
        [_meta_row("Reg", "2026-01-01", is_hub=False, size=registry_size)],
    )
    found = find_contained_links(
        (["copy"], np.vstack([copy_values]), np.array([copy_size])),
        bundle,
    )
    assert len(found) == 1
    assert found.loc[0, "link"] == "near_contained"
    assert found.loc[0, "lineage_id"] == "Reg"
    assert float(found.loc[0, "jaccard"]) < NEAR_COPY_MIN_JACCARD
    assert float(found.loc[0, "containment"]) >= NEAR_COPY_MIN_CONTAINMENT
    other = minhash_to_array(make_minhash(_phrase_set("unrelated", 30)))
    missed = find_contained_links(
        (["other"], np.vstack([other]), np.array([30])),
        bundle,
    )
    assert missed.empty


def test_cluster_link_uses_the_majority_lineage() -> None:
    """The most common linked lineage wins. A tie uses the oldest. 0.60 is ignored.

    Cluster 1 has lineage A twice and B once, so the unlinked text takes A
    and A's oldest version. Cluster 2 has one each, and B is older, so the
    unlinked text takes B. A 0.60 cluster does not pass a lineage on. A
    non-English text in the majority cluster is not linked.
    """
    con = connect_duckdb()
    con.execute(
        "CREATE TABLE clusters (file_sha VARCHAR, cluster_id INTEGER, threshold DOUBLE)"
    )
    con.executemany(
        "INSERT INTO clusters VALUES (?, ?, ?)",
        [
            ("a1", 1, 0.8),
            ("a2", 1, 0.8),
            ("b1", 1, 0.8),
            ("u", 1, 0.8),
            ("fr", 1, 0.8),
            ("t1", 2, 0.8),
            ("t2", 2, 0.8),
            ("u2", 2, 0.8),
            ("a1", 9, 0.6),
            ("ignore", 9, 0.6),
        ],
    )
    con.execute("CREATE TABLE skill_language (file_sha VARCHAR, is_english BOOLEAN)")
    con.executemany(
        "INSERT INTO skill_language VALUES (?, ?)",
        [(sha, sha != "fr") for sha in ("a1", "a2", "b1", "u", "fr", "t1", "t2", "u2", "ignore")],
    )
    con.execute("CREATE TABLE copy_text_shas (file_sha VARCHAR)")
    con.executemany(
        "INSERT INTO copy_text_shas VALUES (?)",
        [(sha,) for sha in ("a1", "a2", "b1", "u", "fr", "t1", "t2", "u2", "ignore")],
    )
    linked = pd.DataFrame(
        [
            {"file_sha": "a1", "lineage_id": "A", "version_seq": 1, "release_date": "2026-01-01"},
            {"file_sha": "a2", "lineage_id": "A", "version_seq": 3, "release_date": "2026-04-01"},
            {"file_sha": "b1", "lineage_id": "B", "version_seq": 1, "release_date": "2026-02-01"},
            {"file_sha": "t1", "lineage_id": "A", "version_seq": 1, "release_date": "2026-06-01"},
            {"file_sha": "t2", "lineage_id": "B", "version_seq": 2, "release_date": "2026-01-01"},
        ]
    )
    found = find_cluster_links(con, linked)
    by_sha = {
        row.file_sha: (row.lineage_id, int(row.version_seq), row.link)
        for row in found.itertuples(index=False)
    }
    assert by_sha["u"] == ("A", 1, "cluster")
    assert by_sha["u2"] == ("B", 2, "cluster")
    assert "fr" not in by_sha
    assert "ignore" not in by_sha
    assert pd.isna(found.loc[found["file_sha"] == "u", "jaccard"]).all()


def test_line_check_drops_a_reworded_near_link() -> None:
    """A near link whose registry lines were all rewritten is dropped. Exact is 1.0.

    The kept near link still has one of the two registry lines, so the share
    is 0.5 and it stays. The reworded link has none, so the reason is
    ``line_share_below_min``. The exact link does not read the lookup.
    """
    links = pd.DataFrame(
        [
            {
                "file_sha": "reworded",
                "lineage_id": "A",
                "version_seq": 1,
                "link": "near",
                "jaccard": 0.9,
                "containment": 0.9,
                "release_date": "2026-01-01",
            },
            {
                "file_sha": "kept",
                "lineage_id": "A",
                "version_seq": 1,
                "link": "near",
                "jaccard": 0.9,
                "containment": 0.9,
                "release_date": "2026-01-01",
            },
            {
                "file_sha": "same",
                "lineage_id": "A",
                "version_seq": 1,
                "link": "exact",
                "jaccard": 1.0,
                "containment": 1.0,
                "release_date": "2026-01-01",
            },
        ]
    )
    checked = verify_links_with_line_diff(
        links,
        {
            "copies": {
                "reworded": "totally different words\nanother new line",
                "kept": "keep this line\nextra",
            },
            "versions": {"A#1": "keep this line\ndrop this line"},
        },
    )
    by_sha = {row.file_sha: row for row in checked.itertuples(index=False)}
    assert by_sha["reworded"].reason == "line_share_below_min"
    assert float(by_sha["reworded"].similarity_verified) == 0.0
    assert by_sha["kept"].reason is None
    assert float(by_sha["kept"].similarity_verified) == 0.5
    assert by_sha["same"].reason is None
    assert float(by_sha["same"].similarity_verified) == 1.0


def test_registry_files_are_not_copies() -> None:
    """A file inside a registry repo is left out. Forks and same-owner stay, flagged.

    ``sha1`` is in the registry and in two outside repos. Only the outside
    files are copies. ``c/copy`` is a fork. ``a/other`` has the same owner as
    the registry. The path that is in wild history is flagged. The path that
    is not is the one stage 6 still has to fetch.
    """
    con = connect_duckdb()
    con.execute(
        "CREATE TABLE artifacts (repo_full_name VARCHAR, path VARCHAR, file_sha VARCHAR)"
    )
    con.executemany(
        "INSERT INTO artifacts VALUES (?, ?, ?)",
        [
            ("a/reg", "skills/demo/SKILL.md", "sha1"),
            ("c/copy", "skills/demo/SKILL.md", "sha1"),
            ("c/copy", "skills/other/SKILL.md", "sha1"),
            ("a/other", "skills/demo/SKILL.md", "sha1"),
            ("a/reg", "skills/only/SKILL.md", "sha-inside"),
        ],
    )
    con.execute("CREATE TABLE registry_repo_list (repo VARCHAR)")
    con.execute("INSERT INTO registry_repo_list VALUES ('a/reg')")
    con.execute("CREATE TABLE lineage_roots (lineage_id VARCHAR, root_lineage VARCHAR)")
    con.execute("INSERT INTO lineage_roots VALUES ('A', 'Root')")
    con.execute("CREATE TABLE lineages (lineage_id VARCHAR, repo VARCHAR)")
    con.execute("INSERT INTO lineages VALUES ('A', 'a/reg')")
    con.execute("CREATE TABLE repos (full_name VARCHAR, is_fork BIGINT)")
    con.executemany(
        "INSERT INTO repos VALUES (?, ?)",
        [("c/copy", 1), ("a/other", 0)],
    )
    con.execute("CREATE TABLE wild_versions (repo VARCHAR, target_path VARCHAR)")
    con.execute("INSERT INTO wild_versions VALUES ('c/copy', 'skills/demo/SKILL.md')")
    links = pd.DataFrame(
        [
            {
                "file_sha": "sha1",
                "lineage_id": "A",
                "version_seq": 1,
                "link": "exact",
                "jaccard": 1.0,
                "containment": 1.0,
                "similarity_verified": 1.0,
            }
        ]
    )
    files = list_copy_files(con, links)
    assert set(files["repo"]) == {"c/copy", "a/other"}
    assert "a/reg" not in set(files["repo"])
    assert set(files["root_lineage"]) == {"Root"}
    flagged = flag_forks_and_same_owner(files, con)
    fork = flagged.loc[flagged["repo"] == "c/copy"].sort_values("path")
    assert bool(fork["is_fork"].all())
    assert not bool(fork["same_owner_as_registry"].any())
    demo = fork.loc[fork["path"] == "skills/demo/SKILL.md"].iloc[0]
    other = fork.loc[fork["path"] == "skills/other/SKILL.md"].iloc[0]
    assert bool(demo["has_wild_history"]) is True
    assert bool(other["has_wild_history"]) is False
    owned = flagged.loc[flagged["repo"] == "a/other"].iloc[0]
    assert bool(owned["same_owner_as_registry"]) is True
    assert bool(owned["is_fork"]) is False
    assert bool(owned["has_wild_history"]) is False


def test_registry_lsh_returns_a_copy_of_the_same_text() -> None:
    """Two identical sketches meet a 0.80 index. An unrelated sketch does not."""
    values = minhash_to_array(make_minhash({"alpha beta", "beta gamma"}))
    other = minhash_to_array(make_minhash({"totally different words here", "nothing in common"}))
    index = build_registry_lsh(["a#1", "b#1", "c#1"], np.vstack([values, values, other]), 0.8)
    hits = set(index.query(minhash_from_array(values)))
    assert hits == {"a#1", "b#1"}
    empty = build_registry_lsh([], np.zeros((0, MINHASH_NUM_PERM), dtype=np.uint64), 0.8)
    assert empty.query(minhash_from_array(values)) == []
