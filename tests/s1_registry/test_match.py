"""Registry blob matches: origin, mirrors, and lineage copies."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import manifest_path
from skillsig.s1_registry.match import assign_origin, build_lineages, match_exact, run

_SNAPSHOT = date(2026, 7, 20)
_BLOB = "b" * 40
_LATER = "c" * 40
_SOLO = "d" * 40
_MATCH_COLUMNS = [
    "blob",
    "files",
    "repos",
    "owners",
    "origin_repo",
    "origin_skill",
    "origin_date",
    "tier",
    "redistributors",
]
_LINEAGE_COLUMNS = [
    "lineage_id",
    "versions",
    "versions_seen",
    "copies",
    "owners",
    "first_date",
    "last_date",
    "span_days",
]


def test_origin_is_earliest_source() -> None:
    versions = pd.DataFrame(
        [
            _version("alpha/X", "early", _BLOB, "2025-10-01", tier="A"),
            _version("alpha/X", "other", _BLOB, "2025-11-01", tier="A"),
            _version("beta/Y", "early", _BLOB, "2026-04-01", tier="B"),
        ]
    )
    artifacts = [
        ("downstream/one", _BLOB),
        ("downstream/two", _BLOB),
        ("alpha/X", _BLOB),
    ]
    assigned = assign_origin(match_exact(_connect(artifacts), versions))
    assert list(assigned.columns) == _MATCH_COLUMNS
    assert len(assigned) == 1
    row = assigned.iloc[0]
    assert row["origin_repo"] == "alpha/X"
    assert row["origin_skill"] == "early"
    assert row["origin_date"] == "2025-10-01"
    assert row["tier"] == "A"
    assert list(row["redistributors"]) == ["beta/Y"]
    assert int(row["files"]) == 3
    assert int(row["repos"]) == 3
    assert int(row["owners"]) == 2


def test_mirror_counted_once(tmp_cfg: Config) -> None:
    _write_sources(tmp_cfg.root)
    _write_mining_run(tmp_cfg)
    _write_versions(
        tmp_cfg,
        [
            _version("acme/skills", "demo", _BLOB, "2025-10-01", tier="A"),
            _version("acme/mirror", "demo", _BLOB, "2025-10-01", tier="A"),
        ],
    )
    _write_artifacts(
        tmp_cfg,
        [
            ("acme/skills", _BLOB),
            ("acme/mirror", _BLOB),
            ("downstream/app", _BLOB),
        ],
    )
    result = run(tmp_cfg, force=True)
    derived = resolve(tmp_cfg, tmp_cfg.paths.derived)
    matches = pd.read_parquet(derived / "s1_registry_matches.parquet")
    lineages = pd.read_parquet(derived / "s1_lineages.parquet")
    assert result.rows == {"s1_registry_matches": 1, "s1_lineages": 1}
    assert len(matches) == 1
    row = matches.iloc[0]
    assert int(row["files"]) == 3
    assert row["origin_repo"] == "acme/skills"
    assert list(row["redistributors"]) == []
    assert list(lineages["lineage_id"]) == ["acme/skills::demo"]
    assert int(lineages.iloc[0]["copies"]) == 2
    assert manifest_path(result.outputs[0]).is_file()
    assert manifest_path(result.outputs[1]).is_file()

    again = run(tmp_cfg)
    assert again.skipped
    assert again.rows == result.rows


def test_lineage_copies_exclude_source_and_ignore_later_versions() -> None:
    versions = pd.DataFrame(
        [
            _version("alpha/X", "early", _BLOB, "2025-10-01", last="2025-12-01", tier="A"),
            _version("alpha/X", "other", _BLOB, "2025-10-01", last="2025-12-01", tier="A"),
            _version("alpha/X", "early", _LATER, "2026-08-01", last="2026-08-15", tier="A"),
            _version("beta/Y", "solo", _SOLO, "2026-08-01", tier="B"),
        ]
    )
    artifacts = [
        ("alpha/X", _BLOB),
        ("downstream/one", _BLOB),
        ("downstream/two", _BLOB),
        ("other/three", _BLOB),
        ("later/one", _LATER),
        ("reader/app", _SOLO),
    ]
    matched = match_exact(_connect(artifacts), versions)
    early = matched[(matched["repo"] == "alpha/X") & (matched["blob"] == _BLOB)]
    assert len(early) == 2
    assert set(early["source_files"].astype(int)) == {1}
    lineages = build_lineages(versions, matched, _SNAPSHOT)
    assert list(lineages.columns) == _LINEAGE_COLUMNS
    by_id = lineages.set_index("lineage_id")
    kept = by_id.loc["alpha/X::early"]
    assert int(kept["versions"]) == 1
    assert int(kept["versions_seen"]) == 1
    assert int(kept["copies"]) == 3
    assert int(kept["owners"]) == 3
    assert kept["first_date"] == "2025-10-01"
    assert kept["last_date"] == "2025-12-01"
    assert int(kept["span_days"]) == (date(2025, 12, 1) - date(2025, 10, 1)).days
    other = by_id.loc["alpha/X::other"]
    assert int(other["copies"]) == 3
    solo = by_id.loc["beta/Y::solo"]
    assert int(solo["versions"]) == 1
    assert int(solo["versions_seen"]) == 1
    assert int(solo["copies"]) == 1
    assert int(solo["owners"]) == 1
    assert int(solo["span_days"]) == 0


def test_dev_sample_keeps_full_copy_count(tmp_cfg: Config) -> None:
    _write_sources(tmp_cfg.root)
    _write_mining_run(tmp_cfg)
    _write_versions(
        tmp_cfg,
        [
            _version("acme/skills", "kept", _BLOB, "2025-10-01"),
            _version("acme/skills", "dropped", _LATER, "2025-11-01"),
        ],
    )
    _write_artifacts(
        tmp_cfg,
        [("downstream/one", _BLOB), ("downstream/two", _BLOB), ("elsewhere/app", _LATER)],
    )
    sample = resolve(tmp_cfg, tmp_cfg.paths.derived) / "s0_devsample_ids.parquet"
    sample.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"file_sha": [_BLOB], "stratum": ["x"]}).to_parquet(sample, index=False)
    result = run(tmp_cfg, dev=True, force=True)
    matches = pd.read_parquet(result.outputs[0])
    assert result.rows["s1_registry_matches"] == 1
    assert list(matches["blob"]) == [_BLOB]
    assert int(matches.iloc[0]["files"]) == 2


def test_missing_versions_names_the_command(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match="registry --only versions"):
        run(tmp_cfg, force=True)


def _connect(rows: list[tuple[str, str]]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.register("artifacts", pd.DataFrame(rows, columns=["repo_full_name", "file_sha"]))
    return con


def _version(
    repo: str,
    skill: str,
    blob: str,
    first: str,
    *,
    last: str | None = None,
    tier: str = "A",
) -> dict[str, str]:
    return {
        "repo": repo,
        "skill": skill,
        "blob": blob,
        "first_date": first,
        "last_date": last or first,
        "first_commit": "a" * 40,
        "tier": tier,
        "lineage_id": f"{repo}::{skill}",
    }


def _write_sources(root: Path) -> None:
    path = root / "config" / "registry_sources.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "sources:",
                "  - {repo: acme/skills, tier: A, group: acme, status: mined}",
                "  - {repo: acme/mirror, tier: A, group: acme, status: mined, "
                "mirror_of: acme/skills}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_versions(cfg: Config, rows: list[dict[str, str]]) -> None:
    path = resolve(cfg, cfg.paths.derived) / "s1_registry_versions.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)


def _write_artifacts(cfg: Config, rows: list[tuple[str, str]]) -> None:
    directory = resolve(cfg, cfg.paths.raw) / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows, columns=["repo_full_name", "file_sha"])
    frame.to_parquet(directory / "part.parquet", index=False)


def _write_mining_run(cfg: Config) -> None:
    directory = resolve(cfg, cfg.paths.raw) / "mining_runs"
    directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "run_id": ["r"],
            "artifact_type": ["skill"],
            "query": ["q"],
            "started_at": ["2026-07-01T00:00:00"],
            "finished_at": ["2026-07-20T22:26:56"],
            "discovered": [1],
            "note": ["t"],
        }
    ).to_parquet(directory / "runs.parquet", index=False)
