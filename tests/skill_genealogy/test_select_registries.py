"""Tests for stage 2 step 1, language labels.

Tiny frames and parquet files. No full dataset. The pool test labels two texts.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import skill_genealogy.settings as settings
from skill_genealogy.file_helpers import connect_duckdb
from skill_genealogy.select_registries import (
    _should_write_sample,
    draw_language_check_sample,
    label_all_skill_languages,
    label_languages,
    read_distinct_texts_in_chunks,
    read_inputs,
    score_language_check,
    summarize_languages,
)
from skill_genealogy.text_helpers import normalize_text, split_front_matter

ENGLISH = (
    "This skill tells you how to use the tool when you need it. "
    "Follow the steps in order and check the result."
)
CHINESE = (
    "这是一个中文技能，用来检查文件并写出简短报告。\n"
    "它说明每个步骤，并且不使用英文句子。"
)
GERMAN = (
    "Dieser Absatz beschreibt eine deutsche Fertigkeit zum Prüfen von Dateien.\n"
    "Er bleibt durchgehend auf Deutsch und nennt keine englischen Befehle."
)
SHORT = "ok"


def _body_chars(text: str) -> int:
    """Length of the normalised body, the same rule as ``label_languages``."""
    return len(split_front_matter(normalize_text(text))[1])


def _register(con, frame: pd.DataFrame, folder: Path) -> None:
    """Write ``frame`` and register it as the artifacts view."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "artifacts.parquet"
    frame.to_parquet(path, index=False)
    quoted = "'" + str(path).replace("'", "''") + "'"
    con.execute(f"CREATE OR REPLACE VIEW artifacts AS SELECT * FROM read_parquet({quoted})")


def test_label_languages_four_texts() -> None:
    """English, Chinese, German and a very short text get the expected flags."""
    texts = pd.DataFrame(
        {
            "file_sha": ["en", "zh", "de", "short"],
            "content": [ENGLISH, CHINESE, GERMAN, SHORT],
        }
    )
    result = label_languages(texts)
    assert list(result["language"]) == ["en", "zh", "de", "unknown"]
    assert result["is_english"].tolist() == [True, False, False, True]
    assert result["body_chars"].tolist() == [
        _body_chars(ENGLISH),
        _body_chars(CHINESE),
        _body_chars(GERMAN),
        _body_chars(SHORT),
    ]


def test_label_languages_front_matter_is_not_counted() -> None:
    """A short body under front matter is unknown, and the header is not in the length."""
    text = "---\nname: demo\n---\nHi"
    result = label_languages(pd.DataFrame({"file_sha": ["a"], "content": [text]}))
    row = result.iloc[0]
    assert row["language"] == "unknown"
    assert bool(row["is_english"]) is True
    assert int(row["body_chars"]) == 2


def test_label_languages_empty() -> None:
    """No rows still returns the four label columns."""
    result = label_languages(pd.DataFrame(columns=["file_sha", "content"]))
    assert list(result.columns) == ["file_sha", "language", "is_english", "body_chars"]
    assert len(result) == 0


def test_read_distinct_texts_in_chunks_splits_and_dedupes(tmp_path: Path) -> None:
    """Duplicate shas collapse, and a small chunk size yields more than one frame."""
    frame = pd.DataFrame(
        {
            "file_sha": ["a", "b", "c", "d", "e", "a"],
            "content": ["one", "two", "three", "four", "five", "one"],
            "repo_full_name": ["r"] * 6,
        }
    )
    con = connect_duckdb()
    _register(con, frame, tmp_path)
    chunks = list(read_distinct_texts_in_chunks(con, chunk_rows=2))
    con.close()
    assert [len(chunk) for chunk in chunks] == [2, 2, 1]
    seen = [sha for chunk in chunks for sha in chunk["file_sha"].tolist()]
    assert sorted(seen) == ["a", "b", "c", "d", "e"]
    assert list(chunks[0].columns) == ["file_sha", "content"]


def test_label_all_skill_languages_one_row_per_sha(tmp_path: Path) -> None:
    """Two artifact rows with the same sha become one sorted label row."""
    frame = pd.DataFrame(
        {
            "file_sha": ["b", "a", "a"],
            "content": [SHORT, ENGLISH, ENGLISH],
            "repo_full_name": ["r", "r", "r"],
        }
    )
    con = connect_duckdb()
    _register(con, frame, tmp_path / "in")
    out = tmp_path / "skill_language.parquet"
    result = label_all_skill_languages(con, out)
    con.close()
    assert list(result["file_sha"]) == ["a", "b"]
    assert result["language"].tolist() == ["en", "unknown"]
    assert result["is_english"].tolist() == [True, True]
    assert out.is_file()
    assert not (tmp_path / "skill_language.unsorted.parquet").exists()


def test_summarize_languages_counts_files_and_repos(tmp_path: Path) -> None:
    """Texts are distinct shas. Files are artifact rows. A mixed repo counts on both sides."""
    artifacts = pd.DataFrame(
        {
            "file_sha": ["e1", "e1", "u1", "z1"],
            "content": ["english", "english", "ok", "chinese"],
            "repo_full_name": ["acme/a", "acme/b", "acme/a", "acme/c"],
        }
    )
    labels = pd.DataFrame(
        {
            "file_sha": ["e1", "u1", "z1"],
            "language": ["en", "unknown", "zh"],
            "is_english": [True, True, False],
            "body_chars": [100, 2, 40],
        }
    )
    labels_path = tmp_path / "skill_language.parquet"
    labels.to_parquet(labels_path, index=False)
    con = connect_duckdb()
    _register(con, artifacts, tmp_path / "in")
    summary = summarize_languages(con, labels_path)
    con.close()
    assert summary["english_texts"] == 2
    assert summary["non_english_texts"] == 1
    assert summary["english_files"] == 3
    assert summary["non_english_files"] == 1
    assert summary["english_repos"] == 2
    assert summary["non_english_repos"] == 1
    assert summary["unknown_texts"] == 1
    assert summary["unknown_median_body_chars"] == 2
    assert summary["top_languages"] == "en: 1, unknown: 1, zh: 1"


def _label_rows() -> pd.DataFrame:
    """50 English, 10 unknown, and 30 each of Chinese, German and French."""
    rows: list[tuple[str, str, bool, int]] = []
    for index in range(50):
        rows.append((f"e{index:02d}", "en", True, 100))
    for index in range(10):
        rows.append((f"u{index:02d}", "unknown", True, 2))
    for language in ("zh", "de", "fr"):
        for index in range(30):
            rows.append((f"{language}{index:02d}", language, False, 80))
    return pd.DataFrame(rows, columns=["file_sha", "language", "is_english", "body_chars"])


def test_language_check_sample_is_stable_and_mixed(tmp_path: Path) -> None:
    """40 per group, or all of a smaller group. The same seed returns the same rows."""
    labels = _label_rows()
    labels_path = tmp_path / "skill_language.parquet"
    labels.to_parquet(labels_path, index=False)
    long_text = ("word " * 80) + "\n" + ("tail " * 20)
    artifacts = pd.DataFrame(
        {
            "file_sha": labels["file_sha"],
            "content": [long_text] * len(labels),
            "repo_full_name": ["acme/a"] * len(labels),
        }
    )
    con = connect_duckdb()
    _register(con, artifacts, tmp_path / "in")
    first_path = tmp_path / "language_check_sample.csv"
    second_path = tmp_path / "language_check_sample_again.csv"
    first = draw_language_check_sample(con, labels_path, first_path)
    second = draw_language_check_sample(con, labels_path, second_path)
    con.close()
    assert list(first["file_sha"]) == list(second["file_sha"])
    assert len(first) == 90
    assert int((first["language"] == "en").sum()) == 40
    assert int((first["language"] == "unknown").sum()) == 10
    other = first.loc[~first["language"].isin(["en", "unknown"]), "language"]
    assert len(other) == 40
    assert set(other) == {"zh", "de", "fr"}
    assert first["body_start"].str.len().eq(300).all()
    assert not first["body_start"].str.contains(r"[\r\n]", regex=True).any()
    assert first["human_language"].eq("").all()
    assert first["human_is_english"].eq("").all()
    stored = pd.read_csv(first_path, dtype=str, keep_default_na=False)
    assert list(stored.columns) == [
        "file_sha",
        "language",
        "is_english",
        "body_chars",
        "body_start",
        "human_language",
        "human_is_english",
    ]


def test_score_language_check_accuracy(tmp_path: Path) -> None:
    """Three of four filled rows agree. The blank row is skipped. One disagreement is listed."""
    frame = pd.DataFrame(
        {
            "file_sha": ["a", "b", "c", "d", "e"],
            "language": ["en", "en", "unknown", "zh", "de"],
            "is_english": ["True", "True", "True", "False", "False"],
            "body_chars": ["10", "10", "2", "20", "20"],
            "body_start": ["a", "b", "c", "d", "e"],
            "human_language": ["en", "de", "en", "zh", ""],
            "human_is_english": ["true", "false", "true", "false", ""],
        }
    )
    path = tmp_path / "language_check_sample.csv"
    frame.to_csv(path, index=False)
    score = score_language_check(path)
    assert score["scored"] == 4
    assert score["correct"] == 3
    assert score["skipped_blank"] == 1
    assert score["accuracy"] == 0.75
    by_group = score["by_group"]
    assert by_group["en"]["accuracy"] == 0.5
    assert by_group["unknown"]["accuracy"] == 1.0
    assert by_group["non_english"]["accuracy"] == 1.0
    disagreements = score["disagreements"]
    assert len(disagreements) == 1
    assert disagreements[0]["file_sha"] == "b"


def test_filled_sheet_is_kept_after_rebuild(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A filled sheet stays, and the message says it came from the previous labels."""
    path = tmp_path / "language_check_sample.csv"
    pd.DataFrame(
        {
            "file_sha": ["a"],
            "language": ["en"],
            "is_english": ["True"],
            "body_chars": ["10"],
            "body_start": ["hello"],
            "human_language": ["en"],
            "human_is_english": ["true"],
        }
    ).to_csv(path, index=False)
    before = path.read_text(encoding="utf-8")
    assert _should_write_sample(path, force=True) is False
    assert path.read_text(encoding="utf-8") == before
    message = f"kept {path}: drawn from the previous labels; re-check before scoring"
    assert message in capsys.readouterr().out


def test_read_inputs_names_the_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing artifacts glob names the download command."""
    monkeypatch.setattr(settings, "ARTIFACTS_GLOB", str(tmp_path / "missing" / "*.parquet"))
    with pytest.raises(FileNotFoundError, match="download_data.py"):
        read_inputs(connect_duckdb())
