"""Scaffold (structural templating) signals."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.cli import resolve_runner
from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s2_features.scaffold import (
    layout_key,
    line_hashes,
    masked_sha,
    outline,
    outline_distance,
    outline_families,
    outline_similarity,
    placeholders,
    run,
    scaffold_basis,
    shared_passages,
    winnow,
)

_FENCE = "```"


def _scaffold(cfg: Config, **changes: object) -> Config:
    return dataclasses.replace(cfg, scaffold=dataclasses.replace(cfg.scaffold, **changes))


def test_placeholders_skip_code_fences_and_links() -> None:
    text = (
        "---\nname: x\ndescription: [TODO: describe]\n---\n"
        "TODO: finish this\n"
        "See [todo](https://example.com) and [TBD later].\n"
        f"{_FENCE}\n[TODO: inside a fence]\nTODO: also inside\n{_FENCE}\n"
        "Use {{project_name}} and skills/<skill-name>/SKILL.md\n"
    )
    count, generator = placeholders(text)
    assert count == 3  # description, line-start TODO:, [TBD later]; not the link or the fence
    assert generator is None


def test_placeholders_name_the_generator() -> None:
    text = "## Overview\n\n[TODO: 1-2 sentences explaining what this skill enables]\n"
    assert placeholders(text)[1] == "skill-creator"


def test_outline_canonicalises_headings() -> None:
    body = (
        "# PDF Tools\n## 1. Think Before Coding\n### Step 2: Setup\n### 3\n"
        "## [Docs](http://x)\n"
        f"{_FENCE}\n# not a heading\n{_FENCE}\n"
    )
    assert outline(body, "pdf-tools") == ["1:<name>", "2:think before coding", "3:setup", "2:docs"]
    assert outline(body.upper(), "pdf-tools") == outline(body, "pdf-tools")


def test_masked_sha_matches_renamed_copies() -> None:
    a = "Use pdf-tools to merge 3 files. See https://a.example/x and ./scripts/run.py."
    b = "Use doc-merger to merge 12 files. See https://b.example/y and ./bin/go.sh."
    assert masked_sha(a, "pdf-tools") == masked_sha(b, "doc-merger")
    assert masked_sha(a, "pdf-tools") != masked_sha("Something else entirely.", "pdf-tools")


def test_line_hashes_mask_names_and_skip_short_lines(cfg: Config) -> None:
    a = "- Run the pdf-tools helper before every release\nok\n" + f"{_FENCE}\n"
    b = "* Run the doc-merger helper before every release\n"
    assert line_hashes(a, cfg, "pdf-tools") == line_hashes(b, cfg, "doc-merger")
    assert len(line_hashes(a, cfg, "pdf-tools")) == 1


def test_layout_key() -> None:
    entries = [
        ("scripts", "dir"),
        ("scripts/example.py", "file"),
        ("references/api.md", "file"),
        ("SKILL.md", "file"),
        ("README.md", "file"),
    ]
    assert layout_key(entries) == "readme.md|references/|scripts/"
    assert layout_key([("SKILL.md", "file")]) is None


def test_outline_distance_and_similarity() -> None:
    base = ["2:a", "2:b", "2:c", "2:d", "2:e"]
    assert outline_distance(base, ["2:a", "2:b", "2:x", "2:c", "2:d", "2:e"]) == 1
    assert outline_similarity(base, ["2:a", "2:b", "2:x", "2:d", "2:e"]) == pytest.approx(0.8)
    assert outline_distance([], base) == 5


def test_outline_families_join_near_and_keep_far_apart(cfg: Config) -> None:
    cfg = _scaffold(cfg, outline_len_min=4)
    base = [f"2:section {i}" for i in range(8)]
    near = [*base[:4], "2:extra", *base[4:]]
    far = [f"2:other {i}" for i in range(8)]
    short = base[:2]
    labels = outline_families([base, near, far, short, list(base)], cfg)
    assert labels[0] == labels[1] == labels[4]
    assert labels[2] != labels[0]
    assert labels[3] == 3


def test_outline_families_skip_oversized_buckets(cfg: Config) -> None:
    cfg = _scaffold(cfg, outline_len_min=4, outline_bucket_max=1)
    base = [f"2:section {i}" for i in range(8)]
    labels = outline_families([base, list(base)], cfg)
    assert list(labels) == [0, 1]


def test_winnow_shared_run_guarantee() -> None:
    k, w = 5, 4
    shared = [f"s{i}" for i in range(k + w - 1)]
    a = [f"a{i}" for i in range(40)] + shared + [f"a{i}" for i in range(40, 60)]
    b = [f"b{i}" for i in range(17)] + shared + [f"b{i}" for i in range(17, 50)]
    common = {h for h, _ in winnow(a, k, w)} & {h for h, _ in winnow(b, k, w)}
    assert common
    assert winnow(["x"] * 3, k, w) == []
    with pytest.raises(ValueError):
        winnow(a, 0, w)


def test_shared_passages_returns_planted_text(cfg: Config) -> None:
    planted = "always run the validation script before you publish the skill to the registry"
    a = " ".join(f"alpha{i}" for i in range(30)) + f" {planted} " + "tail words here"
    b = "intro words " + planted + " " + " ".join(f"bravo{i}" for i in range(30))
    overlap, passages = shared_passages(a, b, cfg)
    assert overlap > 0
    assert planted in passages[0]
    assert shared_passages("short", "text", cfg) == (0.0, [])


def test_scaffold_basis_first_match(cfg: Config) -> None:
    cfg = _scaffold(cfg, owners_min=5, outline_len_min=4)
    outline_feat = {
        "outline_len": 5,
        "outline_key": "k",
        "outline_family_owners": 9,
        "fm_key": "name|description|license",
        "fm_key_owners": 6,
    }
    assert (
        scaffold_basis({"placeholder_generator": "skill-creator", "placeholder_count": 4}, cfg)
        == "generator"
    )
    assert scaffold_basis({"placeholder_count": 1, "masked_owners": 50}, cfg) == "placeholder"
    assert (
        scaffold_basis({"masked_owners": 10, "line_count": 9, "shared_line_frac": 0.9}, cfg)
        == "masked_copy"
    )
    assert (
        scaffold_basis({"line_count": 9, "shared_line_frac": 0.6, **outline_feat}, cfg)
        == "shared_lines"
    )
    assert scaffold_basis({"line_count": 2, "shared_line_frac": 1.0}, cfg) is None
    assert scaffold_basis(outline_feat, cfg) == "outline"
    assert scaffold_basis({**outline_feat, "fm_key": "name|description"}, cfg) is None
    assert (
        scaffold_basis(
            {**outline_feat, "fm_key": None, "layout_key": "scripts/", "layout_owners": 5}, cfg
        )
        == "outline"
    )
    stop = _scaffold(cfg, outline_stoplist=("k",))
    assert scaffold_basis(outline_feat, stop) is None
    assert scaffold_basis({"placeholder_generator": float("nan")}, cfg) is None


_BOILER = [
    "Always read the whole repository before you start any change.",
    "Write tests for every behaviour you add or modify here.",
    "Keep each commit small and describe why the change matters.",
    "Ask the user before deleting files or rewriting history.",
]


def _write_raw(cfg: Config) -> None:
    raw = resolve(cfg, cfg.paths.raw)
    rows = []
    for i in range(6):
        body = "\n".join(
            [f"# Helper {i}", "## Rules", *_BOILER, f"Unique guidance number {i} for team {i}x."]
        )
        rows.append(
            {
                "file_sha": f"s{i}",
                "repo_full_name": f"owner{i}/repo",
                "path": "skills/x/SKILL.md",
                "content": f"---\nname: helper-{i}\ndescription: d\n---\n{body}\n",
                "dedup_primary": 1,
                "body_chars": 500,
                "composition_fetched": 1,
            }
        )
    rows.append(
        {
            "file_sha": "solo",
            "repo_full_name": "loner/repo",
            "path": "SKILL.md",
            "content": "---\nname: solo\ndescription: d\n---\n# Solo\nNothing here is shared.\n",
            "dedup_primary": 1,
            "body_chars": 500,
            "composition_fetched": 0,
        }
    )
    _pq(raw / "artifacts" / "part-00000.parquet", rows)
    siblings = [
        {
            "repo_full_name": f"owner{i}/repo",
            "artifact_path": "skills/x/SKILL.md",
            "entry_name": "scripts/a.py",
            "entry_type": "file",
        }
        for i in range(6)
    ]
    _pq(raw / "artifact_siblings" / "part-00000.parquet", siblings)


def _pq(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def test_run_writes_features_and_skips_rerun(tmp_cfg: Config) -> None:
    cfg = _scaffold(tmp_cfg, owners_min=5, line_owners_min=5)
    _write_raw(cfg)
    result = run(cfg)
    frame = pq.read_table(result.outputs[0]).to_pandas().set_index("file_sha")
    assert result.rows["s2_scaffold"] == 7
    assert frame.loc["s0", "shared_line_frac"] == pytest.approx(0.8)  # 4 of 5 lines in 6 owners
    assert frame.loc["s0", "customization"] == pytest.approx(0.2)
    assert frame.loc["s0", "scaffold_basis"] == "shared_lines"
    assert frame.loc["s0", "layout_key"] == "scripts/"
    assert frame.loc["s0", "layout_owners"] == 6
    assert frame.loc["s0", "outline_family_owners"] == 6
    assert frame.loc["solo", "shared_line_frac"] == 0.0
    assert pd.isna(frame.loc["solo", "scaffold_basis"])
    assert result.outputs[1].name == "s2_scaffold_top_outlines.csv"
    assert run(cfg).skipped is True


def test_runner_resolves() -> None:
    assert resolve_runner("features", "scaffold") is run
