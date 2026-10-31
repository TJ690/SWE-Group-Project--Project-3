"""Tests for stage 2: language labels, the registry rule, the hub test and roles.

Tiny frames and parquet files. No full dataset. The pool test labels two texts.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import skill_genealogy.settings as settings
from skill_genealogy.file_helpers import connect_duckdb
from skill_genealogy.select_registries import (
    _register_language,
    _should_write_sample,
    assign_repo_roles,
    compute_hub_shares,
    count_english_skills_per_repo,
    draw_language_check_sample,
    label_all_skill_languages,
    label_languages,
    pick_dev_registries,
    read_distinct_texts_in_chunks,
    read_inputs,
    score_language_check,
    select_registry_repos,
    summarize_languages,
)
from skill_genealogy.text_helpers import normalize_text, split_front_matter

ENGLISH = (
    "This skill tells you how to use the tool when you need it. "
    "Follow the steps in order and check the result."
)
CHINESE = "这是一个中文技能，用来检查文件并写出简短报告。\n它说明每个步骤，并且不使用英文句子。"
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


def test_missing_language_file_names_the_rebuild_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Steps 2–6 stop and name ``--force-languages`` when the label file is missing."""
    monkeypatch.setattr(settings, "REPO_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError, match="--force-languages"):
        _register_language(connect_duckdb())


def _hub_fixture() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Three repos. B has four dated texts, two of them older in A, plus one undated text.

    t4 has the same time in B and C. A non-English text is older in Z and must be ignored.
    """
    artifacts = pd.DataFrame(
        {
            "repo_full_name": [
                "A",
                "B",
                "A",
                "B",
                "B",
                "C",
                "B",
                "C",
                "B",
                "Z",
                "B",
            ],
            "file_sha": [
                "t1",
                "t1",
                "t2",
                "t2",
                "t3",
                "t3",
                "t4",
                "t4",
                "t_null",
                "t_zh",
                "t_zh",
            ],
            "first_commit_at": [
                "2020-01-01",
                "2020-06-01",
                "2020-02-01",
                "2020-07-01",
                "2020-03-01",
                "2020-08-01",
                "2020-04-01",
                "2020-04-01",
                None,
                "2019-01-01",
                "2021-01-01",
            ],
        }
    )
    labels = pd.DataFrame(
        {
            "file_sha": ["t1", "t2", "t3", "t4", "t_null", "t_zh"],
            "is_english": [True, True, True, True, True, False],
        }
    )
    return artifacts, labels


def test_hub_share_for_texts_first_seen_in_an_older_repo() -> None:
    """B's four dated texts include two that are older in A, so the share is 0.5.

    The undated text is left out. The non-English text is left out. A is 0.
    The default cutoff keeps B (five English texts) and drops A and C.
    """
    artifacts, labels = _hub_fixture()
    con = connect_duckdb()
    con.register("artifacts", artifacts)
    con.register("skill_language", labels)
    named = compute_hub_shares(con, repos=["A", "B", "C"]).set_index("repo")
    default = compute_hub_shares(con).set_index("repo")
    con.close()
    assert int(named.loc["B", "skills_tested"]) == 4
    assert int(named.loc["B", "first_seen_elsewhere"]) == 2
    assert float(named.loc["B", "hub_share"]) == 0.5
    assert float(named.loc["A", "hub_share"]) == 0.0
    assert int(named.loc["C", "first_seen_elsewhere"]) == 1
    assert bool(named.loc["B", "hub_share"] >= settings.HUB_MIN_SHARE) is True
    assert list(default.index) == ["B"]
    assert int(default.loc["B", "skills_tested"]) == 4


def test_tie_on_earliest_date_is_not_elsewhere() -> None:
    """The same earliest time is not elsewhere for either tied repo.

    The smaller name is the recorded winner, and the later repo C still counts.
    """
    artifacts = pd.DataFrame(
        {
            "repo_full_name": ["B", "A", "C"],
            "file_sha": ["t", "t", "t"],
            "first_commit_at": ["2020-05-01", "2020-05-01", "2020-06-01"],
        }
    )
    labels = pd.DataFrame({"file_sha": ["t"], "is_english": [True]})
    con = connect_duckdb()
    con.register("artifacts", artifacts)
    con.register("skill_language", labels)
    shares = compute_hub_shares(con, repos=["A", "B", "C"]).set_index("repo")
    con.close()
    assert int(shares.loc["A", "first_seen_elsewhere"]) == 0
    assert int(shares.loc["B", "first_seen_elsewhere"]) == 0
    assert int(shares.loc["C", "first_seen_elsewhere"]) == 1
    assert float(shares.loc["A", "hub_share"]) == 0.0
    assert float(shares.loc["B", "hub_share"]) == 0.0


def test_repo_roles_for_hub_registry_and_fork() -> None:
    """A 50-star hub stays a hub. A 200-star non-hub is a registry. A fork is not."""
    skills = pd.DataFrame(
        {
            "repo": ["a/hub", "b/reg", "c/fork", "d/tiny", "e/both"],
            "owner": ["a", "b", "c", "d", "e"],
            "stars": [50, 200, 1000, 500, 150],
            "is_fork": [0, 0, 1, 0, 0],
            "english_skills": [10, 10, 20, 1, 8],
            "files": [10, 12, 20, 1, 8],
        }
    )
    hubs = pd.DataFrame(
        {
            "repo": ["a/hub", "b/reg", "c/fork", "e/both"],
            "skills_tested": [10, 10, 20, 8],
            "first_seen_elsewhere": [5, 0, 0, 2],
            "hub_share": [0.5, 0.0, 0.0, 0.25],
        }
    )
    roles = assign_repo_roles(skills, hubs).set_index("repo")
    assert roles.loc["a/hub", "repo_role"] == "hub"
    assert bool(roles.loc["a/hub", "is_hub"]) is True
    assert bool(roles.loc["a/hub", "is_registry"]) is False
    assert roles.loc["b/reg", "repo_role"] == "registry"
    assert roles.loc["c/fork", "repo_role"] == "repository"
    assert roles.loc["d/tiny", "repo_role"] == "repository"
    assert roles.loc["e/both", "repo_role"] == "registry_hub"
    assert bool(roles.loc["a/hub", "hub_tested"]) is True
    assert bool(roles.loc["e/both", "hub_tested"]) is True
    chosen = select_registry_repos(skills)
    assert list(chosen["repo"]) == ["b/reg", "e/both"]


def test_one_dated_text_is_not_a_hub() -> None:
    """A share of 1.0 on one dated text stays a repository. The share is kept."""
    skills = pd.DataFrame(
        {
            "repo": ["thin/one"],
            "owner": ["thin"],
            "stars": [50],
            "is_fork": [0],
            "english_skills": [10],
            "files": [10],
        }
    )
    hubs = pd.DataFrame(
        {
            "repo": ["thin/one"],
            "skills_tested": [1],
            "first_seen_elsewhere": [1],
            "hub_share": [1.0],
        }
    )
    roles = assign_repo_roles(skills, hubs).iloc[0]
    assert float(roles["hub_share"]) == 1.0
    assert bool(roles["is_hub"]) is False
    assert bool(roles["hub_tested"]) is False
    assert roles["repo_role"] != "hub"
    assert roles["repo_role"] == "repository"


def test_pick_dev_registries_is_stable_and_keeps_hubs() -> None:
    """Thirty rows, ten in each size group, at least three hubs, same rows on two draws.

    A frame with no hubs still returns thirty rows and does not invent hubs.
    """
    rows = [
        {
            "repo": f"owner/r{index:03d}",
            "english_skills": index + 1,
            "is_hub": index in {5, 40, 80},
        }
        for index in range(90)
    ]
    frame = pd.DataFrame(rows)
    first = pick_dev_registries(frame, count=30, min_hubs=3, seed=settings.RANDOM_SEED)
    second = pick_dev_registries(frame, count=30, min_hubs=3, seed=settings.RANDOM_SEED)
    assert len(first) == 30
    assert int(first["is_hub"].sum()) >= 3
    assert first.equals(second)
    assert first["size_group"].value_counts().to_dict() == {
        "small": 10,
        "medium": 10,
        "large": 10,
    }
    hubs = set(first.loc[first["is_hub"], "repo"])
    assert hubs == {"owner/r005", "owner/r040", "owner/r080"}
    plain = frame.copy()
    plain["is_hub"] = False
    none = pick_dev_registries(plain, count=30, min_hubs=3, seed=settings.RANDOM_SEED)
    assert len(none) == 30
    assert int(none["is_hub"].sum()) == 0


def test_count_english_skills_keeps_empty_repos() -> None:
    """English texts are distinct shas. A repo with only a non-English text counts as zero."""
    artifacts = pd.DataFrame(
        {
            "repo_full_name": ["acme/a", "acme/a", "acme/b"],
            "file_sha": ["en", "en", "zh"],
            "first_commit_at": ["2020-01-01", "2020-01-01", "2020-01-01"],
        }
    )
    labels = pd.DataFrame({"file_sha": ["en", "zh"], "is_english": [True, False]})
    repos = pd.DataFrame(
        {
            "full_name": ["acme/a", "acme/b", "acme/c"],
            "owner": ["acme", "acme", "acme"],
            "stars": [10, 20, 30],
            "is_fork": [0, 0, 1],
        }
    )
    con = connect_duckdb()
    con.register("artifacts", artifacts)
    con.register("skill_language", labels)
    con.register("repos", repos)
    counted = count_english_skills_per_repo(con).set_index("repo")
    con.close()
    assert int(counted.loc["acme/a", "english_skills"]) == 1
    assert int(counted.loc["acme/a", "files"]) == 2
    assert int(counted.loc["acme/b", "english_skills"]) == 0
    assert int(counted.loc["acme/c", "english_skills"]) == 0
    assert list(counted.index) == ["acme/a", "acme/b", "acme/c"]
