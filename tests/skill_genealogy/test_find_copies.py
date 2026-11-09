"""Tests for stage 5a, template lines and MinHash signatures.

Tiny hand-made artifacts, language labels and registry texts. No dataset files.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from skill_genealogy import find_copies as stage
from skill_genealogy.file_helpers import connect_duckdb

SHARED = "use this shared skill"
BODY = "alpha beta gamma delta epsilon zeta eta theta"


def make_connection(artifacts: list[tuple[str, str, str]], english: dict[str, bool]):
    """A DuckDB connection with tables ``artifacts`` and ``skill_language``."""
    con = connect_duckdb()
    con.register(
        "artifacts_frame",
        pd.DataFrame(artifacts, columns=["repo_full_name", "file_sha", "content"]),
    )
    con.execute("CREATE TABLE artifacts AS SELECT * FROM artifacts_frame")
    con.register(
        "language_frame",
        pd.DataFrame({"file_sha": list(english), "is_english": list(english.values())}),
    )
    con.execute("CREATE TABLE skill_language AS SELECT * FROM language_frame")
    return con


def owners_of(frame: pd.DataFrame, line: str) -> int:
    """The owner count of one line, 0 when absent."""
    found = frame.loc[frame["line"] == line, "owners"]
    return int(found.iloc[0]) if len(found) else 0


def test_count_owners_per_line_counts_distinct_owners() -> None:
    con = make_connection(
        [
            ("a/one", "s1", f"{SHARED}\nhi there\none two three"),
            ("a/two", "s2", f"{SHARED}\n{SHARED}"),  # same owner a, line twice
            ("b/one", "s3", SHARED),
            ("c/one", "s4", SHARED),
            ("d/one", "s5", SHARED),  # not English
        ],
        {"s1": True, "s2": True, "s3": True, "s4": True, "s5": False},
    )
    frame = stage.count_owners_per_line(con)
    assert owners_of(frame, SHARED) == 3
    assert owners_of(frame, "one two three") == 1
    assert owners_of(frame, "hi there") == 0  # two words: dropped
    assert list(frame.columns) == ["line", "owners"]
    assert frame["owners"].iloc[0] == 3  # highest first


def test_count_owners_per_line_respects_owner_floor() -> None:
    con = make_connection(
        [("a/x", "s1", f"{SHARED}\none two three"), ("b/x", "s2", SHARED), ("c/x", "s3", SHARED)],
        {"s1": True, "s2": True, "s3": True},
    )
    frame = stage.count_owners_per_line(con, min_owners=3)
    assert frame.to_dict("records") == [{"line": SHARED, "owners": 3}]


def test_curly_apostrophe_line_is_one_key() -> None:
    curly = "Don’t edit this file"
    con = make_connection(
        [("a/x", "s1", curly), ("b/x", "s2", "Don't edit this file"), ("c/x", "s3", curly)],
        {"s1": True, "s2": True, "s3": True},
    )
    frame = stage.count_owners_per_line(con)
    assert frame.to_dict("records") == [{"line": "don't edit this file", "owners": 3}]
    boilerplate = stage.build_boilerplate_set(frame, 3)
    with_line = stage.signature_for_text(f"{BODY}\n{curly}", boilerplate)
    without = stage.signature_for_text(BODY, boilerplate)
    assert with_line[1] == without[1] and np.array_equal(with_line[0], without[0])


def test_build_boilerplate_set_cutoff_and_empty() -> None:
    lines = pd.DataFrame({"line": ["kept line here", "dropped line here"], "owners": [50, 49]})
    assert stage.build_boilerplate_set(lines, 50) == {"kept line here"}
    assert stage.build_boilerplate_set(stage._empty_line_frame(), 50) == set()
    with pytest.raises(ValueError):
        stage.build_boilerplate_set(lines, 0)


def test_signature_ignores_a_boilerplate_line() -> None:
    boilerplate = {"please install the tool first"}
    left = stage.signature_for_text(
        f"alpha beta gamma\nPlease install the tool first\n{BODY}", boilerplate
    )
    right = stage.signature_for_text(f"alpha beta gamma\n{BODY}", boilerplate)
    assert left[1] == right[1] > 0
    assert left[0].dtype == np.uint64 and left[0].shape == (128,)
    assert np.array_equal(left[0], right[0])
    # Without the boilerplate set the line counts, so the signatures differ.
    assert not np.array_equal(
        stage.signature_for_text(f"alpha beta gamma\nPlease install the tool first\n{BODY}", set())[
            0
        ],
        right[0],
    )


def test_signature_of_only_boilerplate_is_empty() -> None:
    values, size = stage.signature_for_text(
        "---\nname: x\n---\nPlease install the tool", {"please install the tool"}
    )
    assert size == 0
    assert not values.any()
    assert stage.signature_for_text(None, set())[1] == 0  # type: ignore[arg-type]


def test_load_signatures_round_trip(tmp_path: Path) -> None:
    first = stage.signature_for_text(BODY, set())
    second = stage.signature_for_text("one two three four five six", set())
    single = tmp_path / "one.npz"
    stage._write_signature_file(
        single,
        ["x", "y"],
        np.vstack([first[0], second[0]]),
        np.array([first[1], second[1]]),
        1,
        ["z | text"],
    )
    ids, signatures, sizes = stage.load_signatures(single)
    assert ids == ["x", "y"]
    assert signatures.dtype == np.uint64 and signatures.shape == (2, 128)
    assert np.array_equal(signatures[0], first[0])
    assert sizes.tolist() == [first[1], second[1]]
    assert stage.read_signature_report(single) == (2, 1, ["z | text"])

    folder = tmp_path / "parts"
    stage._write_signature_file(
        stage._part_path(folder, 1, 0), ["y"], second[0][None, :], np.array([second[1]]), 0, []
    )
    stage._write_signature_file(
        stage._part_path(folder, 0, 0), ["x"], first[0][None, :], np.array([first[1]]), 0, []
    )
    ids, signatures, sizes = stage.load_signatures(folder)
    assert ids == ["x", "y"]  # part name order
    assert np.array_equal(signatures[1], second[0])

    empty = tmp_path / "empty"
    empty.mkdir()
    ids, signatures, sizes = stage.load_signatures(empty)
    assert ids == [] and signatures.shape == (0, 128) and sizes.shape == (0,)
    with pytest.raises(FileNotFoundError):
        stage.load_signatures(tmp_path / "missing")


def test_registry_signature_skips_an_empty_body(tmp_path: Path) -> None:
    versions = pd.DataFrame(
        {
            "lineage_id": [
                "acme/app::skills/demo",
                "acme/app::skills/demo",
                "acme/app::skills/gone",
            ],
            "version_seq": [1, 2, 1],
            "blob": ["b1", "b2", "b3"],
        }
    )
    texts = {"b1": BODY, "b2": f"{BODY}\nmore words make a new version"}  # b3 missing
    out = tmp_path / "registry_signatures.npz"
    assert stage.build_registry_signatures(versions, texts, set(), out) == 2
    ids, signatures, _sizes = stage.load_signatures(out)
    assert ids == ["acme/app::skills/demo#1", "acme/app::skills/demo#2"]
    assert np.array_equal(signatures[0], stage.signature_for_text(BODY, set())[0])
    signed, empty, examples = stage.read_signature_report(out)
    assert (signed, empty) == (2, 1)
    assert examples[0].startswith("acme/app::skills/gone#1 |")


def test_registry_batches_match_one_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    texts = {f"b{index}": f"{BODY} word{index} extra filler here" for index in range(5)}
    versions = pd.DataFrame(
        {"lineage_id": ["r::s"] * 5, "version_seq": range(1, 6), "blob": list(texts)}
    )
    monkeypatch.setattr(
        stage, "read_texts", lambda _path, blobs: {blob: texts[blob] for blob in blobs}
    )
    batched = tmp_path / "batched.npz"
    whole = tmp_path / "whole.npz"
    stage.sign_registry_in_batches(
        versions, tmp_path / "texts.parquet", set(), batched, batch_rows=2
    )
    stage.build_registry_signatures(versions, texts, set(), whole)
    left, right = stage.load_signatures(batched), stage.load_signatures(whole)
    assert left[0] == right[0]
    assert np.array_equal(left[1], right[1]) and np.array_equal(left[2], right[2])


def test_dataset_signatures_round_trip_and_resume(tmp_path: Path) -> None:
    con = make_connection(
        [
            ("a/x", "s1", BODY),
            ("b/x", "s1", BODY),  # same text in two repos: signed once
            ("c/x", "s2", "---\nname: x\n---\n"),  # empty body
            ("d/x", "s3", BODY),  # not English
            ("e/x", "s4", f"{BODY} iota kappa"),
        ],
        {"s1": True, "s2": True, "s3": False, "s4": True},
    )
    out = tmp_path / "dataset_signatures"
    assert stage.build_dataset_signatures(con, set(), out) == 2
    ids, _signatures, _sizes = stage.load_signatures(out)
    assert sorted(ids) == ["s1", "s4"]
    assert stage.read_signature_report(out)[1] == 1  # s2

    parts = sorted(out.glob(stage.PART_GLOB))
    times = [path.stat().st_mtime_ns for path in parts]
    assert stage.build_dataset_signatures(con, set(), out) == 2
    assert [path.stat().st_mtime_ns for path in parts] == times  # not rewritten


def test_pool_matches_plain_signature() -> None:
    texts = [f"{BODY} number {index} with words" for index in range(stage.POOL_MIN_TEXTS)]
    boilerplate = {"number 0 with words"}
    pooled = stage._sign_many(texts, boilerplate)
    plain = [stage.signature_for_text(text, boilerplate) for text in texts]
    assert [size for _values, size in pooled] == [size for _values, size in plain]
    assert all(
        np.array_equal(left, right) for (left, _l), (right, _r) in zip(pooled, plain, strict=True)
    )


def test_iter_text_chunks_splits_rows() -> None:
    con = connect_duckdb()
    result = con.execute("SELECT range AS file_sha, 'x' AS content FROM range(5000)")
    sizes = [len(frame) for frame in stage._iter_text_chunks(result, 2100)]
    assert sizes == [2100, 2100, 800]
