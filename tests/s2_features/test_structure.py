"""Parity of structure_features with db/content_and_structure.sql."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s0_intake.devsample import output_path as devsample_path
from skillsig.s0_intake.loader import connect
from skillsig.s2_features import run, split_front_matter, structure_features

_COLUMNS: tuple[str, ...] = (
    "desc_chars",
    "body_words",
    "headings",
    "h2",
    "numbered_items",
    "bullet_items",
    "code_blocks",
    "table_rows",
    "links",
    "has_examples_section",
    "has_when_to_use",
    "fm_allowed_tools",
    "fm_license",
    "fm_version_or_metadata",
    "non_ascii_ratio",
)
_TABLE_COLUMNS: tuple[str, ...] = (
    "file_sha",
    "location_class",
    "frontmatter_valid",
    "body_chars",
    *_COLUMNS,
)

# Folded description, a column-0 heading inside a fence, and one non-ASCII letter.
_FIXTURE = """\
---
name: demo
description: >
  Hello
  there.
allowed-tools: Read
license: Apache-2.0
metadata:
  origin: example
---

# Title

```
# inside fence
```

## Examples

- bullet
1. step

| h |
| - |

See [docs](https://example.com) and café.
"""

_SQL_FEATURES = """
WITH s AS (
  SELECT
    ? AS description,
    regexp_replace(?, '^\\s*---.*?\\n---\\s*\\n?', '', 's') AS body,
    regexp_extract(?, '^\\s*---(.*?)\\n---', 1, 's') AS fm
)
SELECT
  length(description) AS desc_chars,
  length(regexp_split_to_array(trim(body), '\\s+')) AS body_words,
  len(regexp_extract_all(body, '(?m)^#{1,6}\\s')) AS headings,
  len(regexp_extract_all(body, '(?m)^##\\s')) AS h2,
  len(regexp_extract_all(body, '(?m)^\\s*\\d+[.)]\\s')) AS numbered_items,
  len(regexp_extract_all(body, '(?m)^\\s*[-*+]\\s')) AS bullet_items,
  len(regexp_extract_all(body, '(?m)^\\s*```')) // 2 AS code_blocks,
  len(regexp_extract_all(body, '(?m)^\\|.*\\|\\s*$')) AS table_rows,
  len(regexp_extract_all(body, '\\[[^\\]]+\\]\\([^)]+\\)')) AS links,
  regexp_matches(lower(body), '(?m)^#{1,6}\\s*(examples?|usage|quick start)')
    AS has_examples_section,
  regexp_matches(lower(body), '(?m)^#{1,6}\\s*(when to use|use when|triggers?)')
    AS has_when_to_use,
  regexp_matches(lower(fm), '(?m)^allowed-tools\\s*:') AS fm_allowed_tools,
  regexp_matches(lower(fm), '(?m)^license\\s*:') AS fm_license,
  regexp_matches(lower(fm), '(?m)^(version|metadata)\\s*:') AS fm_version_or_metadata,
  length(regexp_replace(body, '[\\x00-\\x7F]', '', 'g')) * 1.0
    / greatest(length(body), 1) AS non_ascii_ratio
FROM s
"""


def test_empty_no_front_matter_and_malformed_yaml_do_not_raise() -> None:
    empty = structure_features("")
    assert list(empty) == list(_COLUMNS)
    assert empty["non_ascii_ratio"] == 0.0
    assert split_front_matter("") == ({}, "", False)

    plain = "No front matter, just words.\n"
    assert split_front_matter(plain) == ({}, plain, False)
    assert structure_features(plain)["desc_chars"] == 0

    broken = "---\nname: [unterminated\n---\nStill a body that is fine.\n"
    matter, body, valid = split_front_matter(broken)
    assert valid is False
    assert matter == {}
    assert body == "Still a body that is fine.\n"
    structure_features(broken)


def test_headings_inside_fenced_code_blocks_are_not_counted() -> None:
    text = (
        "# Visible\n\n"
        "```python\n"
        "def demo():\n"
        "    # not a heading\n"
        "    ## also not a heading\n"
        "    return 1\n"
        "```\n\n"
        "## Also visible\n"
    )
    features = structure_features(text)
    assert features["headings"] == 2
    assert features["h2"] == 1
    assert features["code_blocks"] == 1


def test_structure_features_match_sql_on_a_fixture() -> None:
    loaded, _body, valid = split_front_matter(_FIXTURE)
    assert valid is True
    assert loaded["description"] == "Hello there.\n"
    description = loaded["description"]
    assert isinstance(description, str)
    assert structure_features(_FIXTURE) == _sql_features(_FIXTURE, description)


@pytest.mark.data
def test_parity_on_500_primary_rows(cfg: Config) -> None:
    con = connect(cfg)
    try:
        selected = ", ".join(f"s.{name}" for name in _COLUMNS)
        rows = con.execute(
            f"""
            SELECT a.file_sha, a.content, {selected}
            FROM artifacts a
            JOIN skill_features s USING (file_sha)
            WHERE a.dedup_primary = 1 AND a.content IS NOT NULL
            ORDER BY hash(a.file_sha || ?)
            LIMIT 500
            """,
            [str(cfg.seed)],
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 500
    for sha, content, *expected in rows:
        got = structure_features(content)
        assert list(got) == list(_COLUMNS)
        for name, value in zip(_COLUMNS, expected, strict=True):
            assert got[name] == value, f"{sha} {name}: {got[name]!r} != {value!r}"


def test_run_copies_skill_features_and_skips_when_unchanged(tmp_cfg: Config) -> None:
    source = resolve(tmp_cfg, tmp_cfg.paths.generated) / "skill_features.parquet"
    rows = [_feature_row("c", 1), _feature_row("a", 3), _feature_row("b", 9)]
    _write_skill_features(source, rows)
    first = run(tmp_cfg)
    assert first.skipped is False
    assert first.rows == {"s2_structure": 3}
    assert first.outputs[0] == resolve(tmp_cfg, tmp_cfg.paths.derived) / "s2_structure.parquet"
    shas, headings = _read_columns(first.outputs[0])
    assert shas == ["a", "b", "c"]
    assert headings == [3, 9, 1]

    second = run(tmp_cfg)
    assert second.skipped is True
    assert second.rows == {"s2_structure": 3}

    forced = run(tmp_cfg, force=True)
    assert forced.skipped is False


def test_run_dev_keeps_sampled_ids(tmp_cfg: Config) -> None:
    source = resolve(tmp_cfg, tmp_cfg.paths.generated) / "skill_features.parquet"
    _write_skill_features(source, [_feature_row("a", 3), _feature_row("b", 9)])
    _write_devsample(devsample_path(tmp_cfg), ["b", "missing"])
    result = run(tmp_cfg, dev=True)
    assert result.rows == {"s2_structure": 1}
    shas, headings = _read_columns(result.outputs[0])
    assert shas == ["b"]
    assert headings == [9]


def test_missing_skill_features_names_build_command(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match=r"content_and_structure\.sql"):
        run(tmp_cfg)


def test_missing_dev_sample_names_data_command(tmp_cfg: Config) -> None:
    source = resolve(tmp_cfg, tmp_cfg.paths.generated) / "skill_features.parquet"
    _write_skill_features(source, [_feature_row("a", 1)])
    with pytest.raises(FileNotFoundError, match=r"python -m skillsig data --dev-sample"):
        run(tmp_cfg, dev=True)


def _sql_features(text: str, description: str) -> dict[str, float | int | bool]:
    con = duckdb.connect()
    try:
        cursor = con.execute(_SQL_FEATURES, [description, text, text])
        names = [column[0] for column in cursor.description]
        row = cursor.fetchone()
    finally:
        con.close()
    assert row is not None
    return dict(zip(names, row, strict=True))


def _feature_row(file_sha: str, headings: int) -> tuple[object, ...]:
    return (
        file_sha,
        "canonical",
        1,
        200,
        10,
        40,
        headings,
        1,
        0,
        2,
        1,
        0,
        3,
        True,
        False,
        True,
        False,
        False,
        0.0,
    )


def _write_skill_features(path: Path, rows: list[tuple[object, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ", ".join(_TABLE_COLUMNS)
    placeholders = ", ".join("?" for _ in _TABLE_COLUMNS)
    con = duckdb.connect()
    try:
        con.execute(
            """
            CREATE TABLE skill_features (
                file_sha VARCHAR,
                location_class VARCHAR,
                frontmatter_valid BIGINT,
                body_chars BIGINT,
                desc_chars BIGINT,
                body_words BIGINT,
                headings BIGINT,
                h2 BIGINT,
                numbered_items BIGINT,
                bullet_items BIGINT,
                code_blocks BIGINT,
                table_rows BIGINT,
                links BIGINT,
                has_examples_section BOOLEAN,
                has_when_to_use BOOLEAN,
                fm_allowed_tools BOOLEAN,
                fm_license BOOLEAN,
                fm_version_or_metadata BOOLEAN,
                non_ascii_ratio DOUBLE
            )
            """
        )
        con.executemany(f"INSERT INTO skill_features ({columns}) VALUES ({placeholders})", rows)
        dest = "'" + path.as_posix().replace("'", "''") + "'"
        con.execute(f"COPY skill_features TO {dest} (FORMAT PARQUET, COMPRESSION ZSTD)")
    finally:
        con.close()


def _write_devsample(path: Path, file_shas: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE sample (file_sha VARCHAR, stratum VARCHAR)")
        con.executemany(
            "INSERT INTO sample VALUES (?, 'canonical|history=0|copies=1')",
            [(sha,) for sha in file_shas],
        )
        dest = "'" + path.as_posix().replace("'", "''") + "'"
        con.execute(f"COPY sample TO {dest} (FORMAT PARQUET, COMPRESSION ZSTD)")
    finally:
        con.close()


def _read_columns(path: Path) -> tuple[list[str], list[int]]:
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT file_sha, headings FROM read_parquet(?) ORDER BY file_sha",
            [path.as_posix()],
        ).fetchall()
        names = con.execute(
            "SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet(?))",
            [path.as_posix()],
        ).fetchall()
    finally:
        con.close()
    assert [name for (name,) in names] == list(_TABLE_COLUMNS)
    return [sha for sha, _ in rows], [int(headings) for _, headings in rows]
