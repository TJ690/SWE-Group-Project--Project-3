"""Tests for stage 5a, template lines and MinHash signatures.

Tiny hand-made artifacts, language labels and registry texts. No dataset files.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from skill_genealogy import find_copies as stage
from skill_genealogy import settings
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


# Stage 5b: registries copying each other (card G13)

WORDS = (
    "review the pull request and list every risky change with a short reason "
    "then propose a safer patch that keeps the public interface the same and "
    "explain how to test each fix with a small example before you merge it "
    "check the error paths the logging and the configuration defaults as well "
    "and write the findings as a numbered list that a new teammate can follow"
).split()


def skill_text(count: int = 60, change: str | None = None) -> str:
    """A skill body of ``count`` words; ``change`` replaces the last word."""
    words = [WORDS[index % len(WORDS)] + str(index // len(WORDS)) for index in range(count)]
    if change:
        words[-1] = change
    return "---\nname: demo\n---\n" + " ".join(words)


def rehost_inputs(rows: list[tuple[str, str, int, str, str, str]]) -> dict[str, object]:
    """Stage 5b inputs from ``(lineage_id, repo, version_seq, date, blob, text)`` rows.

    Each version is signed with ``signature_for_text`` and no boilerplate.
    """
    versions = pd.DataFrame(
        [
            (lineage, seq, blob, pd.Timestamp(date, tz="UTC"))
            for lineage, _r, seq, date, blob, _t in rows
        ],
        columns=["lineage_id", "version_seq", "blob", "release_date"],
    )
    lineages = (
        pd.DataFrame(
            [(lineage, repo) for lineage, repo, *_rest in rows], columns=["lineage_id", "repo"]
        )
        .drop_duplicates("lineage_id")
        .assign(is_english=True)
    )
    signed = [stage.signature_for_text(text, set()) for *_rest, text in rows]
    return {
        "lineages": lineages,
        "versions": versions,
        "signature_ids": [f"{lineage}#{seq}" for lineage, _r, seq, *_rest in rows],
        "signatures": np.vstack([values for values, _size in signed]),
        "sizes": np.asarray([size for _values, size in signed], dtype=np.int32),
    }


def root_of(roots: pd.DataFrame, lineage_id: str) -> dict[str, object]:
    """The ``lineage_roots`` row of one lineage as a dict."""
    return roots.loc[roots["lineage_id"] == lineage_id].iloc[0].to_dict()


def test_exact_then_near_chain_to_root() -> None:
    inputs = rehost_inputs(
        [
            ("A", "a/reg", 1, "2026-01-01", "blob1", skill_text()),
            ("B", "b/reg", 1, "2026-03-01", "blob1", skill_text()),
            ("C", "c/reg", 1, "2026-04-01", "blob2", skill_text(change="different")),
        ]
    )
    roots = stage._find_roots(inputs)
    b_row, c_row, a_row = root_of(roots, "B"), root_of(roots, "C"), root_of(roots, "A")
    assert (b_row["copied_from_lineage"], b_row["rehost_link"], b_row["root_lineage"]) == (
        "A",
        "exact",
        "A",
    )
    assert b_row["chain_length"] == 1 and not b_row["is_original"]
    assert c_row["rehost_link"] == "near" and c_row["rehost_similarity"] >= 0.8
    # C is nearer to A and B equally; the tie goes to the older date, A.
    assert c_row["root_lineage"] == "A"
    assert a_row["is_original"] and a_row["chain_length"] == 0 and a_row["root_lineage"] == "A"


def test_near_copy_of_a_copy_has_chain_length_two() -> None:
    inputs = rehost_inputs(
        [
            ("A", "a/reg", 1, "2026-01-01", "blob1", skill_text()),
            ("B", "b/reg", 1, "2026-03-01", "blob1", skill_text()),
            ("B", "b/reg", 2, "2026-03-15", "blob3", skill_text(change="extra")),
            ("C", "c/reg", 1, "2026-04-01", "blob3", skill_text(change="extra")),
        ]
    )
    c_row = root_of(stage._find_roots(inputs), "C")
    assert (c_row["copied_from_lineage"], c_row["copied_from_seq"]) == ("B", 2)
    assert (c_row["root_lineage"], c_row["chain_length"]) == ("A", 2)


def test_same_date_is_no_link() -> None:
    inputs = rehost_inputs(
        [
            ("A", "a/reg", 1, "2026-01-01", "blob1", skill_text()),
            ("B", "b/reg", 1, "2026-01-01", "blob1", skill_text()),
        ]
    )
    roots = stage._find_roots(inputs)
    assert roots["is_original"].all() and roots["copied_from_lineage"].isna().all()


def test_same_repo_two_folders_is_no_link() -> None:
    inputs = rehost_inputs(
        [
            ("A1", "a/reg", 1, "2026-01-01", "blob1", skill_text()),
            ("A2", "a/reg", 1, "2026-03-01", "blob1", skill_text()),
        ]
    )
    assert stage._find_roots(inputs)["is_original"].all()


def test_small_source_inside_larger_copy_is_near_contained() -> None:
    inputs = rehost_inputs(
        [
            ("A", "a/reg", 1, "2026-01-01", "blob1", skill_text(60)),
            ("B", "b/reg", 1, "2026-03-01", "blob2", skill_text(90)),
        ]
    )
    b_row = root_of(stage._find_roots(inputs), "B")
    assert b_row["rehost_link"] == "near_contained" and b_row["copied_from_lineage"] == "A"
    assert b_row["rehost_similarity"] >= 0.8


def test_tiny_source_inside_larger_copy_is_no_link() -> None:
    # 12 words leave 8 shingles, under REHOST_CONTAINED_MIN_SHINGLES (20).
    inputs = rehost_inputs(
        [
            ("A", "a/reg", 1, "2026-01-01", "blob1", skill_text(12)),
            ("B", "b/reg", 1, "2026-03-01", "blob2", skill_text(30)),
        ]
    )
    assert stage._find_roots(inputs)["is_original"].all()


def test_cycle_by_bad_dates_ends() -> None:
    rows = pd.DataFrame(
        {
            "lineage_id": ["A", "B"],
            "copied_from_lineage": ["B", "A"],
            "copied_from_seq": [1, 1],
            "rehost_link": ["exact", "exact"],
            "rehost_similarity": [1.0, 1.0],
            "first_date": pd.to_datetime(["2026-02-01", "2026-01-01"], utc=True),
        }
    )
    roots = stage.follow_to_root(rows)
    assert roots["chain_cycle"].all()
    assert roots["root_lineage"].tolist() == ["B", "B"]
    assert roots["chain_length"].tolist() == [1, 0]
    assert not roots["is_original"].any()


def test_band_index_matches_datasketch_lsh() -> None:
    texts = [
        skill_text(60),
        skill_text(60, change="x"),
        skill_text(90),
        skill_text(30),
        "other words " * 20,
    ]
    signatures = np.vstack([stage.signature_for_text(text, set())[0] for text in texts])
    ids = [f"L{index}#1" for index in range(len(texts))]
    lsh = stage.build_registry_lsh(ids, signatures, 0.5)
    bands = stage.build_band_index(signatures, 0.5)
    for row in range(len(texts)):
        expected = {
            ids.index(found) for found in lsh.query(stage.minhash_from_array(signatures[row]))
        }
        _query, others = stage.band_candidate_pairs(bands, np.array([row]))
        assert set(others.tolist()) == expected - {row}


def test_containment_estimates_match_scalar() -> None:
    from skill_genealogy.text_helpers import estimate_containment

    cases = [(0.5, 4, 8), (0.0, 4, 8), (0.3, 10, 50), (0.9, 100, 90), (0.2, 0, 5)]
    vector = stage.containment_estimates(*[np.array(column) for column in zip(*cases, strict=True)])
    assert vector.tolist() == pytest.approx([estimate_containment(*case) for case in cases])


def test_vectorised_sources_match_plain_rule() -> None:
    rows = [
        ("A", "a/reg", 1, "2026-01-01", "b1", skill_text(60)),
        ("B", "b/reg", 1, "2026-02-01", "b2", skill_text(60, change="one")),
        ("C", "c/reg", 1, "2026-03-01", "b3", skill_text(90)),
        ("D", "d/reg", 1, "2026-04-01", "b1", skill_text(60)),
        ("E", "e/reg", 1, "2026-05-01", "b4", skill_text(60, change="two")),
        ("F", "a/reg", 1, "2026-06-01", "b5", skill_text(30)),
    ]
    inputs = rehost_inputs(rows)
    roots = stage._find_roots(inputs).set_index("lineage_id")
    table = stage.version_table(inputs["versions"], inputs["lineages"])
    signatures, sizes = inputs["signatures"], inputs["sizes"]
    for index, (lineage, *_rest) in enumerate(rows):
        agree = (signatures == signatures[index]).sum(axis=1) / 128
        candidates = table.assign(
            jaccard=agree,
            containment=stage.containment_estimates(agree, sizes, np.full(len(rows), sizes[index])),
            shingles=sizes,
        )
        plain = stage.find_rehost_source(lineage, f"{lineage}#1", candidates)
        found = roots.loc[lineage]
        if plain is None:
            assert found["is_original"]
        else:
            assert (found["copied_from_lineage"], found["rehost_link"]) == (
                plain["copied_from_lineage"],
                plain["rehost_link"],
            )


def kinds_table(rows: list[tuple[str, str, float | None, int]]) -> pd.DataFrame:
    """A small ``registry_kinds`` table from ``(repo, kind, upstream_share, shared_texts)``."""
    return pd.DataFrame(
        [
            {
                "repo": repo,
                "registry_kind": kind,
                "confidence": 0.7,
                "kind_basis": "date",
                "upstream_share": share,
                "shared_texts": shared,
                "readme_phrase": "",
            }
            for repo, kind, share, shared in rows
        ]
    )


def test_dated_hub_share_is_a_cross_check_only() -> None:
    lineages = pd.DataFrame(
        {
            "lineage_id": [f"s{index}" for index in range(14)],
            "repo": ["big/src"] * 10 + ["small/src"] * 4,
            "is_english": True,
        }
    )
    roots = pd.DataFrame(
        {"lineage_id": lineages["lineage_id"], "is_original": [False] * 3 + [True] * 11}
    )
    shares = stage.compute_dated_hub_shares(lineages, roots).set_index("repo")
    assert shares.loc["big/src", "dated_hub_share"] == pytest.approx(0.3)
    assert pd.isna(shares.loc["small/src", "dated_hub_share"])
    roles = pd.DataFrame(
        {"repo": ["big/src", "small/src", "hub/copy"], "hub_share": [0.5, None, 0.9]}
    )
    kinds = kinds_table(
        [
            ("big/src", "source", 0.01, 384),
            ("small/src", "source", None, 0),
            ("hub/copy", "copier", 0.95, 49661),
        ]
    )
    hubs = stage.build_registry_hubs(kinds, shares.reset_index(), roles).set_index("repo")
    assert not hubs.loc["big/src", "is_hub"]
    assert (
        hubs.loc["big/src", "hub_reason"] == "source: 1% of 384 shared texts first seen elsewhere"
    )
    assert hubs.loc["big/src", "preview_hub_share"] == pytest.approx(0.5)
    copier = hubs.loc["hub/copy"]
    assert (
        copier["is_hub"] and copier["english_lineages"] == 0 and pd.isna(copier["dated_hub_share"])
    )
    assert copier["hub_reason"].startswith("copier: 95% of 49,661")


def test_update_repo_roles_uses_g38_kinds() -> None:
    roles = pd.DataFrame(
        {
            "repo": ["hub/copy", "src/reg", "app/one", "app/hub"],
            "is_registry": [True, True, False, False],
            "is_hub": [False, False, False, True],
            "hub_tested": [True, True, False, True],
            "hub_share": [0.1, 0.0, None, 0.6],
            "repo_role": ["registry", "registry", "repository", "hub"],
        }
    )
    hubs = pd.DataFrame(
        {
            "repo": ["hub/copy", "src/reg"],
            "registry_kind": ["copier", "source"],
            "is_hub": [True, False],
            "upstream_share": [0.95, 0.02],
        }
    )
    final = stage.update_repo_roles(roles, hubs).set_index("repo")
    assert len(final) == len(roles)
    assert (
        final.loc["hub/copy", "repo_role"] == "registry_copier" and final.loc["hub/copy", "is_hub"]
    )
    assert final.loc["hub/copy", "hub_share"] == pytest.approx(0.95)
    assert final.loc["src/reg", "repo_role"] == "registry"
    assert final.loc["app/hub", "repo_role"] == "hub" and final.loc["app/hub", "is_hub"]
    assert final.loc["app/hub", "hub_evidence"] == "dataset_dates"
    assert final.loc["app/one", "hub_evidence"] is None
    assert set(final.loc[["hub/copy", "src/reg"], "hub_evidence"]) == {"registry_kinds"}


# Stage 5c: find copies in the dataset (card G14)

FRONT = "---\nname: demo\n---\n"


def text_lines(tag: str, count: int) -> list[str]:
    """``count`` lines of 8 words; every word is unique to ``tag`` and the line."""
    return [
        " ".join(f"{WORDS[(line * 8 + word) % len(WORDS)]}{tag}{line}" for word in range(8))
        for line in range(count)
    ]


def skill(lines: list[str]) -> str:
    """A SKILL.md text with front matter and these body lines."""
    return FRONT + "\n".join(lines) + "\n"


def signed(texts: dict[str, str]) -> tuple[list[str], np.ndarray, np.ndarray]:
    """``(ids, signatures, sizes)`` of texts signed with no boilerplate."""
    pairs = [stage.signature_for_text(text, set()) for text in texts.values()]
    return (
        list(texts),
        np.vstack([values for values, _size in pairs]),
        np.asarray([size for _values, size in pairs], dtype=np.int32),
    )


def registry_bundle(versions: dict[str, tuple[str, str]]) -> dict[str, object]:
    """Registry signatures from ``{"L#seq": (release_date, text)}``."""
    ids, signatures, sizes = signed({key: text for key, (_date, text) in versions.items()})
    table = pd.DataFrame(
        {
            "lineage_id": [key.split("#")[0] for key in ids],
            "version_seq": [int(key.split("#")[1]) for key in ids],
            "release_date": pd.to_datetime([date for date, _t in versions.values()], utc=True),
            "version_id": ids,
        }
    )
    return stage.registry_signature_table(table, ids, signatures, sizes)


A_LINES = text_lines("a", 40)
A2_LINES = A_LINES[:20] + text_lines("a2", 1) + A_LINES[21:]


def test_copy_band_index_matches_datasketch_lsh() -> None:
    texts = {
        "full": skill(A_LINES),
        "edited": skill(A2_LINES),
        "half": skill(A_LINES[:20] + text_lines("h", 20)),
        "grown": skill(A_LINES + text_lines("g", 60)),
        "other": skill(text_lines("o", 40)),
    }
    ids, signatures, _sizes = signed(texts)
    lsh = stage.build_registry_lsh(ids, signatures, settings.COPY_SEARCH_LSH_THRESHOLD)
    index = stage.build_copy_band_index(signatures)
    query, other = stage.copy_candidate_pairs(index, signatures)
    for row in range(len(ids)):
        expected = {
            ids.index(found) for found in lsh.query(stage.minhash_from_array(signatures[row]))
        }
        assert set(other[query == row].tolist()) == expected
    assert (index["bands"], index["rows_per_band"]) == (64, 2)


def test_near_link_at_high_jaccard_and_none_at_half() -> None:
    registry = registry_bundle({"A#1": ("2026-01-01", skill(A_LINES))})
    index = stage.build_copy_band_index(registry["signatures"])
    near_copy = skill(A_LINES[:38] + text_lines("n", 2))  # Jaccard about 0.9
    half_copy = skill(A_LINES[:20] + text_lines("h", 20))  # Jaccard about 0.3, containment 0.5
    dataset = signed({"near": near_copy, "half": half_copy})
    near = stage.find_near_links(dataset, index, registry)
    assert near[["file_sha", "lineage_id", "link"]].values.tolist() == [["near", "A", "near"]]
    assert 0.8 <= near.loc[0, "jaccard"] < 1.0
    assert stage.find_contained_links(dataset, index, registry).empty


def test_contained_link_when_the_copy_adds_lines() -> None:
    # Registry text of 40 lines; the copy keeps them and adds 60 new lines.
    registry = registry_bundle({"A#1": ("2026-01-01", skill(A_LINES))})
    index = stage.build_copy_band_index(registry["signatures"])
    dataset = signed({"grown": skill(A_LINES + text_lines("g", 60))})
    assert stage.find_near_links(dataset, index, registry).empty
    found = stage.find_contained_links(dataset, index, registry)
    assert found["link"].tolist() == ["near_contained"]
    assert found.loc[0, "jaccard"] < 0.8 and found.loc[0, "containment"] >= 0.8


def test_near_link_prefers_higher_jaccard_then_older_version() -> None:
    registry = registry_bundle(
        {
            "New#1": ("2026-05-01", skill(A_LINES)),
            "Old#1": ("2026-01-01", skill(A_LINES)),
            "Edit#1": ("2025-01-01", skill(A2_LINES)),
        }
    )
    index = stage.build_copy_band_index(registry["signatures"])
    dataset = signed({"copy": skill(A_LINES[:39] + text_lines("z", 1))})
    assert stage.find_near_links(dataset, index, registry)["lineage_id"].tolist() == ["Old"]


def test_line_check_drops_a_link_whose_lines_were_rewritten() -> None:
    # Same words, other line breaks: MinHash sees the same text, the line diff does not.
    rejoined = [f"{A_LINES[index]} {A_LINES[index + 1]}" for index in range(0, 40, 2)]
    half_kept = A_LINES[:20] + text_lines("k", 20)
    links = stage._as_links(
        pd.DataFrame(
            {
                "file_sha": ["rejoined", "half", "same"],
                "lineage_id": ["A", "A", "A"],
                "version_seq": [1, 1, 1],
                "release_date": ["2026-01-01"] * 3,
            }
        ),
        "near",
    )
    links.loc[2, "link"] = "exact"
    lookup = {
        "copies": {"rejoined": skill(rejoined), "half": skill(half_kept)},
        "versions": {"A#1": skill(A_LINES)},
    }
    checked = stage.verify_links_with_line_diff(links, lookup).set_index("file_sha")
    assert checked.loc["rejoined", "similarity_verified"] == 0.0
    assert checked.loc["rejoined", "reason"] == "line_share_below_min"
    assert checked.loc["half", "similarity_verified"] == pytest.approx(0.5)
    assert checked.loc["half", "reason"] is None
    assert checked.loc["same", "similarity_verified"] == 1.0
    kept, dropped = stage.split_checked_links(checked.reset_index())
    assert sorted(kept["file_sha"]) == ["half", "same"]
    assert dropped["file_sha"].tolist() == ["rejoined"]


def write_copy_world(folder: Path) -> None:
    """Every stage 5c input file for one small world under ``folder``.

    Registries a/reg and b/reg are sources; h/copier and w/weak are copiers.
    Lineage A (a/reg) has versions A#1 (blob ba1) and A#2 (ba2); B (b/reg)
    re-hosts ba1 later; C (b/reg) is a 30-line skill.
    """
    full = folder / "full"
    (full / "registry_history").mkdir(parents=True)
    (full / "dataset_signatures").mkdir()
    texts = {
        "ba1": skill(A_LINES),
        "ba2": skill(A2_LINES),
        "bc1": skill(text_lines("c", 30)),
    }
    dataset_texts = {
        "ba1": texts["ba1"],
        "near1": skill(A2_LINES[:39] + text_lines("n", 1)),
        "cont1": skill(text_lines("c", 30) + text_lines("cx", 60)),
        "clus1": skill(A_LINES[:25] + text_lines("u", 25)),
        "reword": skill([f"{A_LINES[i]} {A_LINES[i + 1]}" for i in range(0, 40, 2)]),
        "only_copier": skill(text_lines("v", 20)),
    }
    artifacts = [
        ("x/app", "s/SKILL.md", "ba1"),
        ("f/fork", "s/SKILL.md", "ba1"),
        ("a/other", "s/SKILL.md", "ba1"),
        ("a/reg", "skills/alpha/SKILL.md", "ba1"),
        ("h/copier", "skills/alpha/SKILL.md", "ba1"),
        ("y/app", "s/SKILL.md", "near1"),
        ("z/app", "s/SKILL.md", "cont1"),
        ("c/app", "s/SKILL.md", "clus1"),
        ("q/app", "s/SKILL.md", "reword"),
        ("k/app", "s/SKILL.md", "only_copier"),
        ("n/app", "s/SKILL.md", "ba2"),  # A#2's blob, but not English
    ]
    contents = {**dataset_texts, "ba2": texts["ba2"]}
    pd.DataFrame(
        [(repo, path, sha, contents[sha]) for repo, path, sha in artifacts],
        columns=["repo_full_name", "path", "file_sha", "content"],
    ).to_parquet(folder / "artifacts.parquet")
    english = {sha: sha != "ba2" for _r, _p, sha in artifacts}
    pd.DataFrame({"file_sha": list(english), "is_english": list(english.values())}).to_parquet(
        full / "skill_language.parquet"
    )
    pd.DataFrame({"full_name": ["x/app", "f/fork"], "is_fork": [0, 1]}).to_parquet(
        folder / "repos.parquet"
    )
    cluster_rows = [(sha, 5, 0.8) for sha in ("ba1", "near1", "cont1", "clus1", "reword")]
    pd.DataFrame(
        cluster_rows + [("only_copier", 9, 0.6), ("ba1", 9, 0.6)],
        columns=["file_sha", "cluster_id", "threshold"],
    ).to_parquet(folder / "clusters.parquet")
    pd.DataFrame(
        [
            ("h/copier", "only_copier", "2026-04-01T10:00:00+02:00", "A", True),
            ("w/weak", "only_copier", "2026-03-01T10:00:00+00:00", "A", True),
            ("w/weak", "reword", "2026-03-05T10:00:00+00:00", "A", True),
            ("h/copier", "ba1", "2026-02-01T10:00:00+00:00", "A", True),
            ("h/copier", "cont1", "2026-02-01T10:00:00+00:00", "D", True),
        ],
        columns=["repo", "new_sha", "commit_date", "status", "is_skill_md"],
    ).to_parquet(full / "registry_history" / "registry_history_changes.parquet")
    pd.DataFrame({"sha": list(texts), "text": list(texts.values())}).to_parquet(
        full / "registry_history" / "registry_history_texts.parquet"
    )
    pd.DataFrame(
        {
            "repo": ["a/reg", "b/reg", "h/copier", "w/weak"],
            "registry_kind": ["source", "source", "copier", "copier"],
            "confidence": [0.9, 0.9, 0.9, 0.3],
        }
    ).to_parquet(full / "registry_kinds.parquet")
    pd.DataFrame(
        {"lineage_id": ["A", "B", "C"], "repo": ["a/reg", "b/reg", "b/reg"], "is_english": True}
    ).to_parquet(full / "lineages.parquet")
    versions = pd.DataFrame(
        {
            "lineage_id": ["A", "A", "B", "C"],
            "version_seq": [1, 2, 1, 1],
            "blob": ["ba1", "ba2", "ba1", "bc1"],
            "release_date": pd.to_datetime(
                ["2026-01-01", "2026-02-01", "2026-03-01", "2026-01-15"], utc=True
            ),
        }
    )
    versions.to_parquet(full / "lineage_versions.parquet")
    pd.DataFrame({"lineage_id": ["A", "B", "C"], "root_lineage": ["A", "A", "C"]}).to_parquet(
        full / "lineage_roots.parquet"
    )
    version_ids = [f"{row.lineage_id}#{row.version_seq}" for row in versions.itertuples()]
    _ids, signatures, sizes = signed(
        {key: texts[blob] for key, blob in zip(version_ids, versions["blob"], strict=True)}
    )
    stage._write_signature_file(
        full / "registry_signatures.npz", version_ids, signatures, sizes, 0, []
    )
    english_texts = {sha: text for sha, text in dataset_texts.items()}
    ids, signatures, sizes = signed(dict(sorted(english_texts.items())))
    stage._write_signature_file(
        full / "dataset_signatures" / "part_s00_c00000.npz", ids, signatures, sizes, 0, []
    )


@pytest.fixture
def copy_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every stage 5c input path at a small world in ``tmp_path``."""
    write_copy_world(tmp_path)

    def data_dir(dev: bool) -> Path:
        folder = tmp_path / ("dev" if dev else "full")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    monkeypatch.setattr(settings, "data_dir", data_dir)
    monkeypatch.setattr(settings, "ARTIFACTS_GLOB", str(tmp_path / "artifacts.parquet"))
    monkeypatch.setattr(settings, "REPOS_GLOB", str(tmp_path / "repos.parquet"))
    monkeypatch.setattr(settings, "CLUSTERS_PATH", tmp_path / "clusters.parquet")
    return tmp_path


def test_copy_world_links_files_and_copier_pass(copy_world: Path) -> None:
    stage.run_copies(dev=False, force=False)
    full = copy_world / "full"
    links = pd.read_parquet(full / "text_links.parquet").set_index("file_sha")
    # Exact: ba1 equals A#1 and B#1; the older A#1 wins. ba2 is not English.
    assert links.loc["ba1", ["lineage_id", "version_seq", "link"]].tolist() == ["A", 1, "exact"]
    assert links.loc["ba1", "similarity_verified"] == 1.0
    assert "ba2" not in links.index
    assert links.loc["near1", ["lineage_id", "version_seq", "link"]].tolist() == ["A", 2, "near"]
    assert links.loc["cont1", ["lineage_id", "link"]].tolist() == ["C", "near_contained"]
    # Cluster: three linked texts vote A, one votes C; A's oldest linked version is A#1.
    assert links.loc["clus1", ["lineage_id", "version_seq", "link"]].tolist() == ["A", 1, "cluster"]
    assert pd.isna(links.loc["clus1", "jaccard"])
    assert links.loc["clus1", "similarity_verified"] == pytest.approx(25 / 40)
    assert "reword" not in links.index and "only_copier" not in links.index
    assert list(links.reset_index().columns) == list(stage.TEXT_LINK_COLUMNS)

    dropped = pd.read_parquet(full / "links_dropped.parquet")
    assert dropped[["file_sha", "link", "reason"]].values.tolist() == [
        ["reword", "near", "line_share_below_min"]
    ]

    copies = pd.read_parquet(full / "copy_candidates.parquet").set_index("repo")
    # Files in a/reg (source) and h/copier (copier) are registry files, not copies.
    assert sorted(copies.index) == ["a/other", "c/app", "f/fork", "x/app", "y/app", "z/app"]
    assert copies.loc["f/fork", "is_fork"] and not copies.loc["x/app", "is_fork"]
    assert copies.loc["a/other", "same_owner_as_registry"]
    assert not copies.loc["x/app", "same_owner_as_registry"]
    assert copies.loc["z/app", "root_lineage"] == "C" and copies.loc["x/app", "root_lineage"] == "A"
    assert list(copies.reset_index().columns) == list(stage.COPY_COLUMNS)

    seen = pd.read_parquet(full / "copier_seen.parquet").set_index("file_sha")
    # ba1 is linked in Pass 1 and cont1's copier row is a delete: no Pass 2 row.
    assert sorted(seen.index) == ["only_copier", "reword"]
    only = seen.loc["only_copier"]
    assert only["copier_first_repo"] == "w/weak"
    assert only["copier_first_date"] == pd.Timestamp("2026-03-01T10:00:00", tz="UTC")
    assert list(only["copier_repos"]) == ["h/copier", "w/weak"]
    assert not only["weak_copiers_only"]
    assert seen.loc["reword", "weak_copiers_only"]
    assert set(seen.index).isdisjoint(set(links.index))
