"""Tests for stage 1 check_data.

Tiny parquet files and a fake plan.json. No full dataset.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import skill_genealogy.settings as settings
from skill_genealogy.check_data import (
    CheckResult,
    check_counts_and_nulls,
    check_date_ranges,
    check_files_exist,
    check_join_coverage,
    check_minhash_settings,
    check_required_columns,
    main,
    preview_copy_download_size,
    preview_non_english_share,
    preview_registry_rule,
    save_report,
)
from skill_genealogy.file_helpers import connect_duckdb

ENGLISH = (
    "This skill tells you how to use the tool when you need it. "
    "Follow the steps in order and check the result."
)
GERMAN = (
    "Dieser Absatz beschreibt eine deutsche Fertigkeit zum Prüfen von Dateien.\n"
    "Er bleibt durchgehend auf Deutsch und nennt keine englischen Befehle."
)
PLAN = {
    "body_max_chars": 1000000,
    "chunk_rows": 50000,
    "dev": False,
    "input_stamp": ["a.parquet", "b.parquet"],
    "limit": None,
    "num_perm": 128,
    "sample_fraction": None,
    "seed": 20261015,
    "shingle_words": 5,
}


def _write(frame: pd.DataFrame, path: Path) -> None:
    """Write one parquet file, creating the parent folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def _patch_paths(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point every stage-1 input at files under ``root``."""
    monkeypatch.setattr(settings, "ARTIFACTS_GLOB", str(root / "artifacts" / "*.parquet"))
    monkeypatch.setattr(settings, "REPOS_GLOB", str(root / "repos" / "*.parquet"))
    monkeypatch.setattr(settings, "MINING_RUNS_GLOB", str(root / "mining_runs" / "*.parquet"))
    monkeypatch.setattr(settings, "SIBLINGS_GLOB", str(root / "siblings" / "*.parquet"))
    monkeypatch.setattr(settings, "WILD_HISTORY_DIR", root / "wild")
    monkeypatch.setattr(settings, "MINHASH_SHARDS_DIR", root / "shards")
    monkeypatch.setattr(settings, "CLUSTERS_PATH", root / "clusters.parquet")


def _artifact_row(
    repo: str,
    name: str,
    file_sha: str | None,
    content: str = "skill text for the check",
) -> dict[str, object]:
    """One artifact row with every column the check requires."""
    return {
        "repo_full_name": repo,
        "path": f"skills/{name}/SKILL.md",
        "file_sha": file_sha,
        "content": content,
        "first_commit_at": "2024-01-15T00:00:00Z",
        "last_commit_at": "2024-02-01T00:00:00Z",
        "commit_count": 1,
        "content_sha_ok": 1,
        "name": name,
        "description": "A skill",
        "filename": "SKILL.md",
    }


def _repo_row(full_name: str, stars: int, is_fork: int) -> dict[str, object]:
    """One repos row with every column the check requires."""
    owner = full_name.split("/")[0]
    return {
        "full_name": full_name,
        "owner": owner,
        "stars": stars,
        "is_fork": is_fork,
        "license": "MIT",
        "created_at": "2020-01-01T00:00:00Z",
    }


def _write_world(root: Path) -> None:
    """A complete tiny input set. One registry, one fork, one exact copy."""
    artifacts = [
        *[_artifact_row("acme/registry", f"s{index}", f"s{index}") for index in range(5)],
        *[_artifact_row("acme/fork", f"f{index}", f"f{index}") for index in range(5)],
        _artifact_row("acme/copy", "s0", "s0"),
        _artifact_row("acme/copy", "s1", "s1"),
        _artifact_row("acme/other", "o0", "o0"),
    ]
    repos = [
        _repo_row("acme/registry", 200, 0),
        _repo_row("acme/fork", 500, 1),
        _repo_row("acme/copy", 1, 0),
        _repo_row("acme/other", 1, 0),
    ]
    _write(pd.DataFrame(artifacts), root / "artifacts" / "part.parquet")
    _write(pd.DataFrame(repos), root / "repos" / "part.parquet")
    _write(
        pd.DataFrame(
            {
                "run_id": [1, 2],
                "started_at": ["2024-06-01T00:00:00+00:00", "2024-06-02T00:00:00+00:00"],
                "finished_at": ["2024-06-01T12:00:00+00:00", "2024-06-02T12:00:00+00:00"],
            }
        ),
        root / "mining_runs" / "part.parquet",
    )
    _write(
        pd.DataFrame(
            {
                "repo_full_name": ["acme/registry"],
                "artifact_path": ["skills/s0/SKILL.md"],
                "entry_name": ["SKILL.md"],
                "entry_type": ["file"],
                "entry_size": [5],
                "entry_sha": ["abc"],
                "content": ["hello"],
            }
        ),
        root / "siblings" / "part.parquet",
    )
    _write(
        pd.DataFrame(
            {
                "repo": ["acme/registry", "acme/copy"],
                "target_path": ["skills/s0/SKILL.md", "skills/s0/SKILL.md"],
            }
        ),
        root / "wild" / "versions.parquet",
    )
    _write(pd.DataFrame({"blob": ["abc"]}), root / "wild" / "blobs.parquet")
    _write(
        pd.DataFrame({"repo": ["acme/registry"], "status": ["ok"]}),
        root / "wild" / "repo_status.parquet",
    )
    shas = [f"s{index}" for index in range(5)] + [f"f{index}" for index in range(5)] + ["o0"]
    shards = root / "shards"
    shards.mkdir(parents=True, exist_ok=True)
    np.save(shards / "shard_00000.ids.npy", np.array(shas, dtype="U64"))
    np.save(shards / "shard_00000.npy", np.zeros((len(shas), 128), dtype=np.uint32))
    (shards / "plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
    _write(
        pd.DataFrame({"file_sha": shas, "cluster_id": list(range(len(shas)))}),
        root / "clusters.parquet",
    )


def _by_name(results: list[CheckResult]) -> dict[str, CheckResult]:
    """Map check name to result."""
    return {item.name: item for item in results}


def test_missing_column_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A parquet file without description is FAIL. The other tables still pass."""
    _write_world(tmp_path)
    frame = pd.read_parquet(tmp_path / "artifacts" / "part.parquet")
    _write(frame.drop(columns=["description"]), tmp_path / "artifacts" / "part.parquet")
    _patch_paths(monkeypatch, tmp_path)
    con = connect_duckdb()
    try:
        results = _by_name(check_required_columns(con))
    finally:
        con.close()
    assert results["artifacts_columns"].status == "FAIL"
    assert "description" in results["artifacts_columns"].message
    assert results["repos_columns"].status == "PASS"
    assert results["artifact_siblings_columns"].status == "PASS"
    assert results["wild_history_versions_columns"].status == "PASS"


def test_null_file_sha_warns_above_one_percent_and_passes_at_one_percent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2 of 100 null file_sha values is WARN. 1 of 100 is exactly 1% and passes."""
    _patch_paths(monkeypatch, tmp_path)
    repos = pd.DataFrame([_repo_row("acme/one", 10, 0)])
    _write(repos, tmp_path / "repos" / "part.parquet")
    con = connect_duckdb()
    try:
        warned = [
            _artifact_row("acme/one", f"n{index}", None if index < 2 else f"sha{index}")
            for index in range(100)
        ]
        _write(pd.DataFrame(warned), tmp_path / "artifacts" / "part.parquet")
        warn_result = _by_name(check_counts_and_nulls(con))["artifact_counts_and_nulls"]
        passed = [
            _artifact_row("acme/one", f"n{index}", None if index < 1 else f"sha{index}")
            for index in range(100)
        ]
        _write(pd.DataFrame(passed), tmp_path / "artifacts" / "part.parquet")
        pass_result = _by_name(check_counts_and_nulls(con))["artifact_counts_and_nulls"]
    finally:
        con.close()
    assert warn_result.status == "WARN"
    assert warn_result.numbers["null_file_sha_share"] == 0.02
    assert pass_result.status == "PASS"
    assert pass_result.numbers["null_file_sha_share"] == 0.01


def test_full_tiny_world_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every check passes on a hand-built set, and the previews match the rows."""
    _write_world(tmp_path)
    _patch_paths(monkeypatch, tmp_path)
    con = connect_duckdb()
    try:
        groups = [
            check_files_exist(),
            check_required_columns(con),
            check_counts_and_nulls(con),
            check_date_ranges(con),
            check_join_coverage(con),
            [check_minhash_settings()],
            [preview_registry_rule(con)],
            [preview_copy_download_size(con)],
        ]
    finally:
        con.close()
    flat = [item for group in groups for item in group]
    assert {item.status for item in flat} == {"PASS"}
    counts = _by_name(groups[2])["artifact_counts_and_nulls"]
    assert counts.numbers["file_sha_without_content"] == 0
    registry = _by_name(groups[6])["registry_rule_preview"]
    assert registry.numbers["repos"] == 1
    assert registry.numbers["skills"] == 5
    assert registry.numbers["files"] == 5
    copies = _by_name(groups[7])["copy_download_size"]
    assert copies.numbers["copy_files"] == 2
    assert copies.numbers["copy_paths"] == 2
    assert copies.numbers["paths_with_wild_history"] == 1
    assert copies.numbers["repos_to_clone"] == 1
    versions = _by_name(groups[1])["wild_history_versions_columns"]
    assert versions.numbers["columns"] == ["repo", "target_path"]
    settings_result = groups[5][0]
    assert settings_result.numbers["seed"] == 20261015
    assert settings_result.numbers["signature_dtype"] == "uint32"
    assert settings_result.numbers["signature_columns"] == 128


def test_file_sha_without_content_ignores_a_sha_that_has_content_elsewhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A text with content on any row counts as present. A text with none does not."""
    _patch_paths(monkeypatch, tmp_path)
    _write(pd.DataFrame([_repo_row("acme/one", 10, 0)]), tmp_path / "repos" / "part.parquet")
    kept_with = _artifact_row("acme/one", "kept-a", "kept", content="skill text for the check")
    kept_empty = _artifact_row("acme/one", "kept-b", "kept")
    kept_empty["content"] = None
    missing = _artifact_row("acme/one", "missing-a", "missing")
    missing["content"] = None
    missing["first_commit_at"] = None
    rows = [kept_with, kept_empty, missing]
    _write(pd.DataFrame(rows), tmp_path / "artifacts" / "part.parquet")
    con = connect_duckdb()
    try:
        result = _by_name(check_counts_and_nulls(con))["artifact_counts_and_nulls"]
    finally:
        con.close()
    dated = {row["file_sha"] for row in rows if row["first_commit_at"] is not None}
    assert result.numbers["file_sha_without_content"] == 1
    assert result.numbers["file_sha_with_first_commit_at"] == len(dated)


def test_minhash_plan_match_and_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan that matches settings passes. A different num_perm fails."""
    folder = tmp_path / "shards"
    folder.mkdir()
    monkeypatch.setattr(settings, "MINHASH_SHARDS_DIR", folder)
    (folder / "plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
    matched = check_minhash_settings()
    assert matched.status == "PASS"
    assert matched.numbers["shingle_words"] == 5
    assert matched.numbers["input_stamp"] == {"length": 2}
    assert "limit" in matched.numbers
    changed = dict(PLAN)
    changed["num_perm"] = 64
    (folder / "plan.json").write_text(json.dumps(changed), encoding="utf-8")
    mismatched = check_minhash_settings()
    assert mismatched.status == "FAIL"
    assert "num_perm" in mismatched.message


def test_minhash_seed_mismatch_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The size fields can match while a different seed is only a warning."""
    folder = tmp_path / "shards"
    folder.mkdir()
    monkeypatch.setattr(settings, "MINHASH_SHARDS_DIR", folder)
    changed = dict(PLAN)
    changed["seed"] = 1
    (folder / "plan.json").write_text(json.dumps(changed), encoding="utf-8")
    result = check_minhash_settings()
    assert result.status == "WARN"
    assert result.numbers["seed_matches"] is False


def test_missing_files_name_the_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every missing input FAILs, and the message names the command that builds it."""
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "shards").mkdir()
    np.save(tmp_path / "shards" / "shard_00000.ids.npy", np.array(["s0"], dtype="U64"))
    results = check_files_exist()
    by_name = _by_name(results)
    assert by_name["minhash_shard_ids"].status == "PASS"
    assert by_name["minhash_signatures"].status == "FAIL"
    text = " ".join(item.message for item in results)
    assert "python src/data/download_data.py" in text
    assert "main pipeline step 3f" in text
    assert "python -m skillsig dedup" in text


def test_future_and_unparsed_dates_warn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A commit after today, and a value that is not a timestamp, are WARN."""
    _patch_paths(monkeypatch, tmp_path)
    future = _artifact_row("acme/one", "soon", "sha")
    future["first_commit_at"] = "2099-01-01T00:00:00Z"
    _write(pd.DataFrame([future]), tmp_path / "artifacts" / "future.parquet")
    _write(
        pd.DataFrame(
            {
                "started_at": ["2024-06-01T00:00:00Z"],
                "finished_at": ["2024-06-02T00:00:00Z"],
            }
        ),
        tmp_path / "mining_runs" / "part.parquet",
    )
    con = connect_duckdb()
    try:
        future_result = check_date_ranges(con)[0]
        broken = _artifact_row("acme/one", "bad", "sha")
        broken["last_commit_at"] = "not-a-date"
        _write(pd.DataFrame([broken]), tmp_path / "artifacts" / "future.parquet")
        broken_result = check_date_ranges(con)[0]
    finally:
        con.close()
    assert future_result.status == "WARN"
    assert future_result.numbers["commits_after_today"] == 1
    assert broken_result.status == "WARN"
    assert broken_result.numbers["unparsed_dates"] == 1


def test_empty_versions_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """versions.parquet with columns and no rows is WARN, and the columns are listed."""
    _write_world(tmp_path)
    _write(
        pd.DataFrame({"repo": pd.Series(dtype="object"), "target_path": pd.Series(dtype="object")}),
        tmp_path / "wild" / "versions.parquet",
    )
    _patch_paths(monkeypatch, tmp_path)
    con = connect_duckdb()
    try:
        result = _by_name(check_required_columns(con))["wild_history_versions_columns"]
    finally:
        con.close()
    assert result.status == "WARN"
    assert result.numbers["rows"] == 0
    assert result.numbers["columns"] == ["repo", "target_path"]


def test_join_coverage_warns_below_99_percent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One cluster id missing from artifacts is 50 percent, which is WARN."""
    _write_world(tmp_path)
    _write(
        pd.DataFrame({"file_sha": ["s0", "missing-sha"], "cluster_id": [1, 2]}),
        tmp_path / "clusters.parquet",
    )
    _patch_paths(monkeypatch, tmp_path)
    con = connect_duckdb()
    try:
        results = _by_name(check_join_coverage(con))
    finally:
        con.close()
    assert results["cluster_id_join"].status == "WARN"
    assert results["cluster_id_join"].numbers["share"] == 0.5
    assert results["minhash_id_join"].status == "PASS"
    assert results["artifacts_repos_join"].status == "PASS"
    assert results["wild_history_join"].status == "PASS"


def test_non_english_share_on_three_texts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """English, German and a two-letter text produce three language shares."""
    _patch_paths(monkeypatch, tmp_path)
    rows = [
        _artifact_row("acme/one", "en", "sha-en", ENGLISH),
        _artifact_row("acme/one", "de", "sha-de", GERMAN),
        _artifact_row("acme/one", "short", "sha-short", "ok"),
    ]
    _write(pd.DataFrame(rows), tmp_path / "artifacts" / "part.parquet")
    con = connect_duckdb()
    try:
        result = preview_non_english_share(con, sample_size=10)
    finally:
        con.close()
    assert result.status == "PASS"
    assert result.numbers["sampled"] == 3
    shares = result.numbers["share_by_language"]
    assert isinstance(shares, dict)
    assert shares["en"] == pytest.approx(1 / 3)
    assert shares["de"] == pytest.approx(1 / 3)
    assert shares["unknown"] == pytest.approx(1 / 3)
    assert result.numbers["non_english_share"] == pytest.approx(1 / 3)


def test_save_report_writes_run_date_and_checks(tmp_path: Path) -> None:
    """The report is a JSON object with the run date and the check list."""
    path = tmp_path / "out" / "data_check_report.json"
    save_report([CheckResult("example", "PASS", {"rows": 2}, "ok")], path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "run_date" in saved
    assert saved["checks"] == [
        {"name": "example", "status": "PASS", "numbers": {"rows": 2}, "message": "ok"}
    ]


def test_main_returns_1_when_a_file_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing artifacts glob is FAIL, the report is written, and main returns 1."""
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "data_dir", lambda dev: tmp_path)
    monkeypatch.setattr(sys, "argv", ["check_data"])
    assert main() == 1
    saved = json.loads((tmp_path / "data_check_report.json").read_text(encoding="utf-8"))
    artifacts = next(item for item in saved["checks"] if item["name"] == "artifacts")
    assert artifacts["status"] == "FAIL"
    assert "python src/data/download_data.py" in artifacts["message"]


def test_main_passes_and_skips_the_second_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete tiny world exits 0. A second run skips and still exits 0."""
    _write_world(tmp_path)
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "data_dir", lambda dev: tmp_path / "out")
    monkeypatch.setattr(sys, "argv", ["check_data"])
    assert main() == 0
    report = tmp_path / "out" / "data_check_report.json"
    assert report.is_file()
    assert main() == 0
    text = report.read_text(encoding="utf-8")
    assert "registry_rule_preview" in text
