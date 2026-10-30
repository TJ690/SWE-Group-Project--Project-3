"""Tests for settings paths and the shared file helpers.

Uses tiny frames and temp folders. No dataset files.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import skill_genealogy.settings as settings
from skill_genealogy.file_helpers import (
    check_inputs_exist,
    outputs_exist,
    print_sample_records,
    read_parquet,
    write_parquet,
)


def test_data_dir_dev_and_full_differ(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Dev and full folders differ and end in the names from the plan."""
    monkeypatch.setattr(settings, "REPO_ROOT", tmp_path)
    dev = settings.data_dir(True)
    full = settings.data_dir(False)
    assert dev != full
    assert dev.name == "skill_genealogy_dev"
    assert full.name == "skill_genealogy"
    assert dev == tmp_path / "src" / "data" / "skill_genealogy_dev"
    assert full == tmp_path / "src" / "data" / "skill_genealogy"
    assert dev.is_dir()
    assert full.is_dir()
    assert settings.results_dir(True) == tmp_path / "results" / "skill_genealogy_dev"
    assert settings.results_dir(False) == tmp_path / "results" / "skill_genealogy"


def test_check_inputs_exist_names_missing_path_and_builder(tmp_path: Path) -> None:
    """A missing input raises, and the message names the path and the builder."""
    missing = tmp_path / "registries.parquet"
    command = "python -m skill_genealogy.select_registries"
    with pytest.raises(FileNotFoundError) as caught:
        check_inputs_exist({"registries": missing}, {"registries": command})
    message = str(caught.value)
    assert str(missing) in message
    assert command in message


def test_check_inputs_exist_accepts_a_glob_that_matches_a_file(tmp_path: Path) -> None:
    """A glob counts as present when it matches at least one file."""
    folder = tmp_path / "artifacts"
    folder.mkdir()
    (folder / "part-0.parquet").write_bytes(b"")
    check_inputs_exist(
        {"artifacts": str(folder / "*.parquet")},
        {"artifacts": "python src/data/download_data.py"},
    )


def test_write_parquet_round_trip_sorted(tmp_path: Path) -> None:
    """Three rows written with a sort column come back in that order."""
    frame = pd.DataFrame({"skill_id": ["c", "a", "b"], "n": [3, 1, 2]})
    path = tmp_path / "out" / "rows.parquet"
    write_parquet(frame, path, sort_by=["skill_id"])
    loaded = read_parquet(path)
    assert list(loaded["skill_id"]) == ["a", "b", "c"]
    assert list(loaded["n"]) == [1, 2, 3]
    assert len(loaded) == 3


def test_print_sample_records_cuts_long_strings(capsys: pytest.CaptureFixture[str]) -> None:
    """Sample lines keep the title and cut a long string to 80 characters."""
    frame = pd.DataFrame({"body": ["x" * 100, "short"], "n": [1, 2]})
    print_sample_records(frame, "sample rows", count=2)
    printed = capsys.readouterr().out
    assert printed.startswith("sample rows\n")
    assert f"body: {'x' * 80}" in printed
    assert "x" * 81 not in printed
    assert "body: short" in printed


def test_outputs_exist_false_when_one_file_is_missing(tmp_path: Path) -> None:
    """One missing file makes the whole output list incomplete."""
    present = tmp_path / "families.parquet"
    present.write_bytes(b"not read")
    missing = tmp_path / "trees.parquet"
    assert outputs_exist([present, missing]) is False
