"""Wording features W1-W10 and description idiom."""

from __future__ import annotations

import math

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s0_data.devsample import output_path as devsample_path
from skillsig.s1_registry.versions import description_idiom as registry_idiom
from skillsig.s2_features.wording import description_idiom, run, wording_features

# 3 non-blank lines. First words: Run (verb), You (not), Write (verb).
# Tokens: run your tests you must write now write hi (9; write twice; you/your twice).
# Sentence ends: "tests." and "now." Syllables: 1 each (silent e drops the extra group in "write").
# One MUST, one `$ ` prompt, 50 characters. One bullet and one heading level -> uniformity 0.
_THREE = "# Run your tests.\n- You MUST write now.\n$ Write hi"
_KEYS = (
    "w_imperative_ratio",
    "w_you_per_100w",
    "w_command_density",
    "w_fk_grade",
    "w_mattr500",
    "w_emphasis_per_k",
    "w_uniformity_bullet_cv",
    "w_uniformity_heading_entropy",
    "w_phrase_hits",
    "w_non_english",
)
_COLUMNS = ("file_sha", *_KEYS, "desc_idiom")


def test_three_line_fixture_matches_hand_computed_values() -> None:
    assert len(_THREE) == 50
    assert _THREE.count("\n") == 2
    expected = {
        "w_imperative_ratio": 2 / 3,
        "w_you_per_100w": 200 / 9,
        "w_command_density": 1 * 1000 / 50,
        "w_fk_grade": 0.39 * (9 / 2) + 11.8 * (9 / 9) - 15.59,
        "w_mattr500": 8 / 9,
        "w_emphasis_per_k": 1 * 1000 / 50,
        "w_uniformity_bullet_cv": 0.0,
        "w_uniformity_heading_entropy": 0.0,
        "w_phrase_hits": 0.0,
        "w_non_english": 0.0,
    }
    got = wording_features(_THREE)
    assert tuple(got) == _KEYS
    assert got == pytest.approx(expected)

    bullets = "- aa\n- aaaa\n- aaaaaa"
    lengths = [2, 4, 6]
    mean = sum(lengths) / len(lengths)
    variance = sum((length - mean) ** 2 for length in lengths) / len(lengths)
    assert wording_features(bullets)["w_uniformity_bullet_cv"] == pytest.approx(
        math.sqrt(variance) / mean
    )

    headings = "# A\n## B\n### C"
    assert wording_features(headings)["w_uniformity_heading_entropy"] == pytest.approx(math.log2(3))

    shell = "```bash\necho hi\n```"
    assert wording_features(shell)["w_command_density"] == pytest.approx(1 * 1000 / len(shell))
    inside = "```bash\n$ echo hi\n```"
    assert wording_features(inside)["w_command_density"] == pytest.approx(1 * 1000 / len(inside))
    python = "```python\necho hi\n```"
    assert wording_features(python)["w_command_density"] == 0.0


def test_mattr_averages_each_500_token_window() -> None:
    # Window 0..499 is all "a" (TTR 1/500). Window 1..500 is 499 "a" plus "b" (TTR 2/500).
    text = ("a " * 500) + "b"
    assert wording_features(text)["w_mattr500"] == pytest.approx((1 / 500 + 2 / 500) / 2)


def test_empty_body_returns_zeros_not_nan() -> None:
    for text in ("", "\n\n", "   \n\t"):
        got = wording_features(text)
        assert tuple(got) == _KEYS
        for value in got.values():
            assert type(value) is float
            assert value == 0.0
            assert not math.isnan(value)


def test_non_english_flag_is_ratio_above_point_two() -> None:
    assert wording_features("aaaaá")["w_non_english"] == 0.0
    assert wording_features("aaaáá")["w_non_english"] == 1.0


@pytest.mark.parametrize(
    ("desc", "idiom"),
    [
        ("Use this skill whenever the user wants to...", "use_this_skill_whenever"),
        ("Use when the user asks for a code review.", "use_when"),
        ("Guide for creating effective skills.", "guide_for"),
        (
            "Create distinctive, production-grade frontend interfaces. "
            "Use this skill when the user asks to build web components.",
            "capability_summary",
        ),
        ("Notes from the planning meeting.", "other"),
        ("Use this skill when the user wants a spreadsheet.", "use_when"),
        (None, "other"),
        ("   ", "other"),
    ],
)
def test_description_idiom_matches_versions(desc: str | None, idiom: str) -> None:
    assert description_idiom is registry_idiom
    assert description_idiom(desc) == registry_idiom(desc) == idiom


def test_run_keeps_readable_sample_rows_and_skips(tmp_cfg: Config) -> None:
    review = "Use when the user asks for a review."
    notes = "Notes from the planning meeting."
    _write_artifacts(
        tmp_cfg,
        [
            _row("bbb", _THREE, notes, 250, 1),
            _row("aaa", _front(review) + _THREE, review, 250, 1),
            _row("ccc", "Hello body\n", None, 250, 1),
            _row("mmm", "See SKILL.md\nKeep going.\n", notes, 250, 1),
            _row("zzz", _THREE, notes, 250, 1),
            _row("sss", "skills/SKILL.md", notes, 250, 1),
            _row("rrr", "Redirecting to https://example.com", notes, 250, 1),
            _row("ppp", _THREE, notes, 250, 0),
            _row("lll", _THREE, notes, 10, 1),
        ],
    )
    _write_devsample(tmp_cfg, ["bbb", "aaa", "ccc", "mmm", "sss", "rrr", "ppp", "lll"])

    output = resolve(tmp_cfg, tmp_cfg.paths.derived) / "s2_wording.parquet"
    first = run(tmp_cfg, dev=True)
    assert first.skipped is False
    assert first.rows == {"s2_wording": 4}
    assert first.outputs == [output]

    table = pq.read_table(output)
    assert table.column_names == list(_COLUMNS)
    got = table.to_pylist()
    assert [row["file_sha"] for row in got] == ["aaa", "bbb", "ccc", "mmm"]
    assert got[0]["desc_idiom"] == "use_when"
    assert got[1]["desc_idiom"] == "other"
    assert got[2]["desc_idiom"] == "other"
    _assert_features(got[0], _THREE)
    _assert_features(got[1], _THREE)
    _assert_features(got[2], "Hello body\n")
    _assert_features(got[3], "See SKILL.md\nKeep going.\n")

    second = run(tmp_cfg, dev=True)
    assert second.skipped is True
    assert second.rows == {"s2_wording": 4}
    assert output.with_name(output.name + ".manifest.json").is_file()


def test_missing_artifacts_names_download_command(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match=r"download_data.py"):
        run(tmp_cfg, dev=True)


def test_missing_dev_sample_names_command(tmp_cfg: Config) -> None:
    _write_artifacts(tmp_cfg, [_row("aaa", "hello\n", "x", 250, 1)])
    with pytest.raises(FileNotFoundError, match=r"data --dev-sample"):
        run(tmp_cfg, dev=True)


def _assert_features(row: dict[str, object], body: str) -> None:
    expected = wording_features(body)
    for key, value in expected.items():
        assert row[key] == pytest.approx(value)


def _front(description: str) -> str:
    return f"---\nname: demo\ndescription: {description}\n---\n"


def _row(
    file_sha: str,
    content: str,
    description: str | None,
    body_chars: int,
    dedup_primary: int,
) -> dict[str, object]:
    return {
        "file_sha": file_sha,
        "content": content,
        "description": description,
        "body_chars": body_chars,
        "dedup_primary": dedup_primary,
    }


def _write_artifacts(cfg: Config, rows: list[dict[str, object]]) -> None:
    path = resolve(cfg, cfg.paths.raw) / "artifacts" / "part-00000.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "file_sha": pa.array([row["file_sha"] for row in rows], type=pa.string()),
            "content": pa.array([row["content"] for row in rows], type=pa.string()),
            "description": pa.array([row["description"] for row in rows], type=pa.string()),
            "body_chars": pa.array([row["body_chars"] for row in rows], type=pa.int64()),
            "dedup_primary": pa.array([row["dedup_primary"] for row in rows], type=pa.int64()),
        }
    )
    pq.write_table(table, path, compression="zstd")


def _write_devsample(cfg: Config, file_shas: list[str]) -> None:
    path = devsample_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "file_sha": pa.array(file_shas, type=pa.string()),
            "stratum": pa.array(["x"] * len(file_shas), type=pa.string()),
        }
    )
    pq.write_table(table, path, compression="zstd")
