"""Skill-type taxonomy v0: keyword rules and file counts by project kind."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s0_data.devsample import output_path as devsample_path
from skillsig.s2_features.skill_type import RULES, SKILL_TYPES, run, skill_type

_SHA = {
    "meta": "a" * 40,
    "office": "b" * 40,
    "security": "c" * 40,
    "blank": "d" * 40,
    "orphan": "e" * 40,
}


def test_skill_creator_is_meta() -> None:
    assert skill_type("skill-creator", None) == "meta"
    assert skill_type("Skill-Creator", "writes a template") == "meta"


def test_pdf_toolkit_is_office_documents() -> None:
    text = "Comprehensive PDF manipulation toolkit"
    assert skill_type(None, text) == "office_documents"
    assert skill_type(text, None) == "office_documents"


def test_empty_name_and_description_are_unclassified() -> None:
    assert skill_type(None, None) == "unclassified"
    assert skill_type("", "") == "unclassified"
    assert skill_type("  ", None) == "unclassified"


def test_security_review_of_tests_is_security() -> None:
    assert skill_type("security review of tests", None) == "security"
    assert skill_type(None, "Security review of tests") == "security"


def test_word_test_beats_later_review_rule() -> None:
    """v0 known weakness: the word 'test' wins over a later category."""
    assert skill_type("review of tests", None) == "testing_debugging"


def test_meta_beats_a_later_office_hit() -> None:
    assert skill_type("skill-creator", "Comprehensive PDF manipulation toolkit") == "meta"


def test_rules_follow_skill_type_order() -> None:
    assert SKILL_TYPES[-1] == "unclassified"
    assert tuple(label for label, _pattern in RULES) == SKILL_TYPES[:-1]
    assert all(
        skill_type(name, description) in SKILL_TYPES
        for name, description in (
            ("skill-creator", None),
            (None, "Comprehensive PDF manipulation toolkit"),
            ("security review of tests", None),
            (None, None),
        )
    )


def test_run_counts_files_and_skips_when_unchanged(tmp_cfg: Config) -> None:
    _write_artifacts(tmp_cfg, _artifact_rows())
    _write_repos(tmp_cfg, _repo_rows())
    result = run(tmp_cfg)
    types_path, csv_path = result.outputs
    assert result.skipped is False
    assert result.rows == {"s2_types": 5, "s2_skill_type_by_project": 5}
    assert types_path == resolve(tmp_cfg, tmp_cfg.paths.derived) / "s2_types.parquet"
    assert csv_path == resolve(tmp_cfg, tmp_cfg.paths.results) / "s2_skill_type_by_project.csv"

    typed = _read_types(types_path)
    assert list(typed) == [
        _SHA["meta"],
        _SHA["office"],
        _SHA["security"],
        _SHA["blank"],
        _SHA["orphan"],
    ]
    assert typed[_SHA["meta"]] == "meta"
    assert typed[_SHA["office"]] == "office_documents"
    assert typed[_SHA["security"]] == "security"
    assert typed[_SHA["blank"]] == "unclassified"
    assert typed[_SHA["orphan"]] == "unclassified"

    counts = _read_counts(csv_path)
    assert counts[("meta", "C_project", "Python")] == 2
    assert counts[("office_documents", "A_collection", None)] == 1
    assert counts[("security", "B_agent_tooling", "Python")] == 1
    assert counts[("unclassified", "C_project", "Python")] == 1
    assert counts[("unclassified", None, None)] == 1

    again = run(tmp_cfg)
    assert again.skipped is True
    assert again.rows == {"s2_types": 5, "s2_skill_type_by_project": 5}
    forced = run(tmp_cfg, force=True)
    assert forced.skipped is False


def test_dev_keeps_sample_skills_and_their_copies(tmp_cfg: Config) -> None:
    _write_artifacts(tmp_cfg, _artifact_rows())
    _write_repos(tmp_cfg, _repo_rows())
    _write_sample(tmp_cfg, [_SHA["meta"]])
    result = run(tmp_cfg, dev=True)
    assert result.skipped is False
    assert result.rows == {"s2_types": 1, "s2_skill_type_by_project": 1}
    typed = _read_types(result.outputs[0])
    assert typed == {_SHA["meta"]: "meta"}
    counts = _read_counts(result.outputs[1])
    assert counts == {("meta", "C_project", "Python"): 2}
    assert run(tmp_cfg, dev=True).skipped is True


def test_missing_repo_names_repo_context(tmp_cfg: Config) -> None:
    _write_artifacts(tmp_cfg, _artifact_rows()[:1])
    with pytest.raises(FileNotFoundError, match="repo_context"):
        run(tmp_cfg)


def test_missing_artifacts_names_the_download_command(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match="download_data.py"):
        run(tmp_cfg)


def test_dev_missing_sample_names_dev_sample_command(tmp_cfg: Config) -> None:
    _write_artifacts(tmp_cfg, _artifact_rows()[:1])
    _write_repos(tmp_cfg, _repo_rows()[:1])
    with pytest.raises(FileNotFoundError, match="dev-sample"):
        run(tmp_cfg, dev=True)


def _artifact_rows() -> list[dict[str, object]]:
    return [
        _artifact(_SHA["meta"], "ada/notes", 1, "skill-creator", None),
        _artifact(_SHA["meta"], "ada/notes", 0, None, None),
        _artifact(
            _SHA["office"],
            "some/copies",
            1,
            "pdf-tools",
            "Comprehensive PDF manipulation toolkit",
        ),
        _artifact(
            _SHA["security"],
            "x/MalSkillBench",
            1,
            "security-review",
            "security review of tests",
        ),
        _artifact(_SHA["blank"], "ada/notes", 1, "", ""),
        _artifact(_SHA["orphan"], "missing/repo", 1, "cooking", "Bake bread"),
    ]


def _artifact(
    file_sha: str,
    repo: str,
    primary: int,
    name: str | None,
    description: str | None,
) -> dict[str, object]:
    return {
        "file_sha": file_sha,
        "repo_full_name": repo,
        "dedup_primary": primary,
        "name": name,
        "description": description,
    }


def _repo_rows() -> list[dict[str, object]]:
    return [
        {"repo_full_name": "ada/notes", "repo_kind": "C_project", "language": "Python"},
        {"repo_full_name": "some/copies", "repo_kind": "A_collection", "language": None},
        {"repo_full_name": "x/MalSkillBench", "repo_kind": "B_agent_tooling", "language": "Python"},
    ]


def _write_artifacts(cfg: Config, rows: list[dict[str, object]]) -> None:
    directory = resolve(cfg, cfg.paths.raw) / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    _write_parquet(directory / "part.parquet", rows)


def _write_repos(cfg: Config, rows: list[dict[str, object]]) -> None:
    path = resolve(cfg, cfg.paths.derived) / "s2_repo.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_parquet(path, rows)


def _write_sample(cfg: Config, file_shas: list[str]) -> None:
    path = devsample_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"file_sha": sha, "stratum": "1"} for sha in file_shas]
    _write_parquet(path, rows)


def _write_parquet(path: Path, rows: list[dict[str, object]]) -> None:
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path, compression="zstd")


def _read_types(path: Path) -> dict[str, str]:
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT file_sha, skill_type FROM read_parquet(?) ORDER BY file_sha",
            [path.as_posix()],
        ).fetchall()
    finally:
        con.close()
    return {str(sha): str(label) for sha, label in rows}


def _read_counts(path: Path) -> dict[tuple[str, str | None, str | None], int]:
    con = duckdb.connect()
    try:
        rows = con.execute(
            """
            SELECT skill_type, repo_kind, language, files
            FROM read_csv(?, header = true)
            """,
            [path.as_posix()],
        ).fetchall()
    finally:
        con.close()
    return {(str(label), kind, language): int(files) for label, kind, language, files in rows}
