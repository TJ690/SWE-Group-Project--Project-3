"""Tests for stage 5 steps 1–2: boilerplate lines and MinHash signatures.

Tiny tables and a few hand-written texts. No full dataset.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from skill_genealogy.file_helpers import connect_duckdb
from skill_genealogy.find_copies import (
    _sign_many,
    build_boilerplate_set,
    build_dataset_signatures,
    build_registry_signatures,
    count_owners_per_line,
    load_signatures,
    signature_for_text,
)
from skill_genealogy.settings import MINHASH_NUM_PERM

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
