"""E1 disclosure recall on full registry histories."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s4_validation.disclosure_recall import run, skill_histories, summarise


def _versions() -> pd.DataFrame:
    rows = [
        # middle-only: trailer on 2nd of 3 commits
        ("o/r", "c1", "2026-01-01", "a/SKILL.md"),
        ("o/r", "c2", "2026-01-02", "a/SKILL.md"),
        ("o/r", "c3", "2026-01-03", "a/SKILL.md"),
        # single commit with trailer
        ("o/r", "c4", "2026-01-01", "b/SKILL.md"),
        # tie on date: order by commit sha -> c5 first, c6 last (trailer on c6)
        ("o/r", "c6", "2026-02-01", "c/SKILL.md"),
        ("o/r", "c5", "2026-02-01", "c/SKILL.md"),
        # no trailer at all
        ("g/x", "c7", "2026-03-01", "d/SKILL.md"),
    ]
    return pd.DataFrame(rows, columns=["repo", "commit", "commit_date", "path"])


def _trailers() -> pd.DataFrame:
    rows = [("o/r", "c2", True), ("o/r", "c4", True), ("o/r", "c6", True), ("g/x", "c7", False)]
    return pd.DataFrame(rows, columns=["repo", "commit", "has_ai_trailer"])


def test_middle_only_and_single_commit() -> None:
    hist = skill_histories(_versions(), _trailers()).set_index("path")
    assert bool(hist.loc["a/SKILL.md", "middle_only"]) is True
    assert bool(hist.loc["b/SKILL.md", "first_ai"]) and bool(hist.loc["b/SKILL.md", "last_ai"])
    assert bool(hist.loc["c/SKILL.md", "last_ai"]) and not bool(hist.loc["c/SKILL.md", "first_ai"])
    assert not bool(hist.loc["d/SKILL.md", "any_ai"])


def test_summary_recall_matches_hand_count() -> None:
    table = summarise(skill_histories(_versions(), _trailers()), {"o/r": "community"})
    overall = table.set_index("scope").loc["all"]
    assert overall["any_ai"] == 3
    assert overall["visible_first_or_last"] == 2
    assert overall["recall"] == pytest.approx(2 / 3)
    assert "group:community" in set(table["scope"])
    assert "group:other" in set(table["scope"])


def test_run_writes_outputs(tmp_cfg: Config) -> None:
    derived = resolve(tmp_cfg, tmp_cfg.paths.derived)
    derived.mkdir(parents=True)
    pq.write_table(pa.Table.from_pandas(_versions()), derived / "s1_versions_raw.parquet")
    pq.write_table(pa.Table.from_pandas(_trailers()), derived / "s1_commit_trailers.parquet")
    result = run(tmp_cfg)
    table = pd.read_csv(result.outputs[0])
    assert table.loc[table["scope"] == "all", "middle_only"].item() == 1
    payload = json.loads(Path(result.outputs[1]).read_text())
    assert "not a random sample" in payload["note"]
    assert run(tmp_cfg).skipped is True


def test_missing_inputs_name_command(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match="registry --only mine_git"):
        run(tmp_cfg)
