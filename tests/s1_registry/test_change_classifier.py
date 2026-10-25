"""Version-to-version change metrics: cosmetic edits, description, network, scripts."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pandas as pd
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import manifest_path
from skillsig.s1_registry.change_classifier import (
    classify_change,
    content_signals,
    diff_versions,
)
from skillsig.s1_registry.changes import run
from skillsig.s1_registry.sources import Source, clone_dir

_REPO = "acme/skills"


def _skill(description: str, body: str) -> str:
    return f"---\nname: demo\ndescription: {description}\n---\n\n{body}"


def test_whitespace_only_change_is_cosmetic(cfg: Config) -> None:
    old = _skill("Do the thing.", "Hello   world\n")
    new = _skill("Do the thing.", "Hello world\n\n")
    metrics = diff_versions(old, new, ["SKILL.md"], ["SKILL.md"])
    assert metrics.body_change_pct == 0.0
    assert classify_change(metrics, cfg) == "cosmetic"


def test_rewritten_description_is_substantive(cfg: Config) -> None:
    old = _skill("Summarize a paper.", "Body\n")
    new = _skill("Translate legal contracts into plain language.", "Body\n")
    metrics = diff_versions(old, new, ["SKILL.md"], ["SKILL.md"])
    assert metrics.body_change_pct == 0.0
    assert classify_change(metrics, cfg) == "substantive"


def test_added_curl_line_counts_one_network_ref() -> None:
    old = "Read the docs.\n"
    new = "Read the docs.\ncurl https://x\n"
    metrics = diff_versions(old, new, [], [])
    assert metrics.network_added == 1
    assert content_signals(new, [])["network"] == content_signals(old, [])["network"] + 1


def test_added_bundle_script_is_substantive(cfg: Config) -> None:
    metrics = diff_versions("Body\n", "Body\n", ["SKILL.md"], ["SKILL.md", "scripts/run.sh"])
    assert metrics.scripts_added == 1
    assert content_signals("Body\n", ["SKILL.md", "scripts/run.sh"])["scripts"] == 1
    assert classify_change(metrics, cfg) == "substantive"


def test_missing_versions_names_the_versions_command(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match="registry --only versions"):
        run(tmp_cfg)


def test_run_records_a_substantive_version_step(tmp_cfg: Config) -> None:
    _write_sources(tmp_cfg.root)
    origin = Source(_REPO, "A", "acme", "mined", None, None)
    clone = clone_dir(tmp_cfg, origin)
    first, second = _two_versions(clone)
    _write_versions(tmp_cfg, first, second)

    result = run(tmp_cfg, force=True)
    path = resolve(tmp_cfg, tmp_cfg.paths.derived) / "s1_registry_changes.parquet"
    frame = pd.read_parquet(path)
    assert not result.skipped
    assert result.rows == {"s1_registry_changes": 1}
    assert manifest_path(path).is_file()
    assert list(frame.columns) == [
        "lineage_id",
        "from_blob",
        "to_blob",
        "from_date",
        "to_date",
        "body_change_pct",
        "desc_similarity",
        "headings_added",
        "headings_removed",
        "files_delta",
        "change_type",
        "commands_added",
        "commands_removed",
        "network_added",
        "network_removed",
        "scripts_added",
        "scripts_removed",
        "steps_delta",
    ]
    row = frame.iloc[0]
    assert row["lineage_id"] == f"{_REPO}::demo"
    assert row["from_blob"] == first["blob"]
    assert row["to_blob"] == second["blob"]
    assert row["from_date"] == "2026-01-01"
    assert row["to_date"] == "2026-02-01"
    assert row["change_type"] == "substantive"
    assert int(row["network_added"]) == 1
    assert int(row["scripts_added"]) == 1
    assert int(row["files_delta"]) == 1

    again = run(tmp_cfg)
    assert again.skipped
    assert again.rows["s1_registry_changes"] == 1


def _write_sources(root: Path) -> None:
    path = root / "config" / "registry_sources.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "sources:\n  - {repo: acme/skills, tier: A, group: acme, status: mined}\n",
        encoding="utf-8",
    )


def _write_versions(cfg: Config, first: dict[str, str], second: dict[str, str]) -> None:
    derived = resolve(cfg, cfg.paths.derived)
    derived.mkdir(parents=True, exist_ok=True)
    rows = [
        _version(first, seq=1, day="2026-01-01"),
        _version(second, seq=2, day="2026-02-01"),
    ]
    pd.DataFrame(rows).to_parquet(derived / "s1_registry_versions.parquet", index=False)


def _version(item: dict[str, str], *, seq: int, day: str) -> dict[str, object]:
    return {
        "lineage_id": f"{_REPO}::demo",
        "repo": _REPO,
        "skill": "demo",
        "blob": item["blob"],
        "version_seq": seq,
        "first_commit": item["commit"],
        "first_date": day,
    }


def _two_versions(clone: Path) -> tuple[dict[str, str], dict[str, str]]:
    _init_repo(clone)
    skill = clone / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(_skill("Do the thing.", "# Use\n\nHello world\n"), encoding="utf-8")
    first = _commit(clone, "Add skill")
    skill.write_text(
        _skill("Do the thing.", "# Use\n\nHello world\ncurl https://x\n"),
        encoding="utf-8",
    )
    script = clone / "skills" / "demo" / "scripts" / "run.sh"
    script.parent.mkdir(parents=True)
    script.write_text("echo hi\n", encoding="utf-8")
    outside = clone / "tools" / "run.sh"
    outside.parent.mkdir(parents=True)
    outside.write_text("echo no\n", encoding="utf-8")
    second = _commit(clone, "Add network and script")
    return first, second


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    for args in (
        ["config", "user.email", "dev@example.com"],
        ["config", "user.name", "Dev"],
        ["config", "commit.gpgsign", "false"],
        ["config", "core.autocrlf", "false"],
    ):
        _git(path, *args)


def _commit(path: Path, message: str) -> dict[str, str]:
    _git(path, "add", "-A")
    _git(path, "commit", "-m", message)
    commit = _git(path, "rev-parse", "HEAD").strip()
    blob = _git(path, "rev-parse", f"{commit}:skills/demo/SKILL.md").strip()
    return {"commit": commit, "blob": blob}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout
