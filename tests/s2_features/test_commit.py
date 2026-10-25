"""Commit-history features: AI trailers, bot class, batches, message class."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s0_data.devsample import output_path as devsample_path
from skillsig.s2_features.commit import (
    AI_TOOLS,
    ai_tools_in_message,
    classify_author,
    classify_message,
    run,
    strip_trailers,
)

_WHEN = "2026-01-01T00:00:00Z"
_LATER = "2026-01-08T00:00:00Z"
_CLAUDE = "Co-Authored-By: Claude Sonnet <noreply@anthropic.com>"
_INT = frozenset({"dedup_primary", "history_fetched", "commit_count"})


def test_ai_tools_constant() -> None:
    assert AI_TOOLS == ("claude", "cursor", "copilot", "codex", "gemini")


def test_claude_coauthor_trailer() -> None:
    assert ai_tools_in_message(_CLAUDE) == frozenset({"claude"})


def test_copilot_in_subject_only() -> None:
    message = "Add Copilot support for the skill"
    assert ai_tools_in_message(message) == frozenset()
    assert classify_message(message) == "add_upload"


def test_null_message() -> None:
    assert ai_tools_in_message(None) == frozenset()
    assert classify_message(None) == "other"


def test_tool_outside_coauthor_line_does_not_count() -> None:
    human = (
        "feat: add skill\n\n"
        "Co-authored-by: Ada Lovelace <ada@example.com>\n"
        "Generated with Claude Code\n"
    )
    assert ai_tools_in_message(human) == frozenset()
    assert ai_tools_in_message("Signed-off-by: Claude <claude@example.com>\n") == frozenset()
    assert ai_tools_in_message("Generated-by: Cursor <cursor@example.com>\n") == frozenset()
    both = (
        "subject\n\n"
        "Co-authored-by: Cursor <cursoragent@cursor.com>\n"
        "Co-authored-by: OpenAI Codex <noreply@openai.com>\n"
    )
    assert ai_tools_in_message(both) == frozenset({"cursor", "codex"})
    assert ai_tools_in_message(
        "Co-authored-by: Someone <noreply@anthropic.com>"
    ) == frozenset({"claude"})


def test_strip_trailers_removes_trailer_lines_only() -> None:
    text = "feat: add skill\n\nCo-authored-by: Claude <a@b>\nSigned-off-by: X\nGenerated-by: Y\n"
    stripped = strip_trailers(text)
    assert stripped.startswith("feat: add skill")
    assert "co-authored-by" not in stripped.lower()
    assert "signed-off-by" not in stripped.lower()
    assert "generated-by" not in stripped.lower()
    assert strip_trailers(None) == ""


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("sync skills from the registry", "install_sync_import"),
        ("initial commit", "scaffold"),
        ("feat: add a widget", "conventional"),
        ("Add Copilot support", "add_upload"),
        ("skills via upload from the archive", "add_upload"),
        ("wip", "short_generic"),
        ("", "short_generic"),
        ("rewrote the onboarding guide", "other"),
        (None, "other"),
        (f"initial commit\n\n{_CLAUDE}\n", "scaffold"),
    ],
)
def test_classify_message(message: str | None, expected: str) -> None:
    assert classify_message(message) == expected


@pytest.mark.parametrize(
    ("login", "author_type", "expected"),
    [
        ("claude[bot]", "Bot", "coding_agent_bot"),
        ("cursor[bot]", "Bot", "coding_agent_bot"),
        ("copilot-swe-agent[bot]", "Bot", "coding_agent_bot"),
        ("codex[bot]", "Bot", "coding_agent_bot"),
        ("devin[bot]", "Bot", "coding_agent_bot"),
        ("jules[bot]", "Bot", "coding_agent_bot"),
        ("gemini[bot]", "Bot", "coding_agent_bot"),
        ("github-actions[bot]", "Bot", "noncoding_bot"),
        ("dependabot[bot]", "Bot", "noncoding_bot"),
        ("release-please[bot]", "Bot", "noncoding_bot"),
        ("skills-sync[bot]", "Bot", "noncoding_bot"),
        ("publisher[bot]", "Bot", "noncoding_bot"),
        ("cursor-sync[bot]", "Bot", "noncoding_bot"),
        ("clawdhub[bot]", "Bot", "noncoding_bot"),
        ("opencode[bot]", "Bot", "noncoding_bot"),
        ("some-project[bot]", "Bot", "noncoding_bot"),
        (None, "ProgrammaticAccessBot", "noncoding_bot"),
        ("cursor-fan", "User", "user"),
        ("acme", "Organization", "organization"),
        (None, None, "unknown"),
        (None, "Mannequin", "unknown"),
    ],
)
def test_classify_author(login: str | None, author_type: str | None, expected: str) -> None:
    assert classify_author(login, author_type) == expected


def test_run_batch_null_history_and_skip(tmp_cfg: Config) -> None:
    rows = _sample_rows()
    _write_artifacts(tmp_cfg, rows)
    result = run(tmp_cfg)
    assert result.skipped is False
    assert result.rows == {"s2_commit": 13}

    by_sha = _read(result.outputs[0])
    assert list(by_sha) == sorted(by_sha)
    bulk = [by_sha[f"{i:040x}"] for i in range(7)]
    assert all(row["batch_size"] == 7 and row["is_bulk"] is True for row in bulk)
    assert [by_sha[f"{i:040x}"]["batch_size"] for i in range(7, 12)] == [5, 5, 5, 5, 5]
    assert all(by_sha[f"{i:040x}"]["is_bulk"] is False for i in range(7, 12))

    claude = by_sha[f"{0:040x}"]
    assert claude["ai_tool_first"] == "claude"
    assert claude["ai_trailer_first"] is True
    assert claude["ai_tool_last"] == "gemini"
    assert claude["ai_trailer_last"] is True
    assert claude["span_days"] == 7
    assert claude["commit_count"] == 4
    assert claude["msg_class_first"] == "scaffold"
    assert claude["first_author_class"] == "user"
    assert claude["coding_agent_bot"] is False
    assert claude["noncoding_bot"] is False

    copilot_subject = by_sha[f"{1:040x}"]
    assert copilot_subject["ai_tool_first"] is None
    assert copilot_subject["ai_trailer_first"] is False
    assert copilot_subject["msg_class_first"] == "add_upload"

    missing = by_sha[f"{2:040x}"]
    assert missing["msg_class_first"] == "other"
    assert missing["ai_tool_first"] is None
    assert missing["ai_trailer_first"] is False

    assert by_sha[f"{3:040x}"]["first_author_class"] == "coding_agent_bot"
    assert by_sha[f"{3:040x}"]["coding_agent_bot"] is True
    assert by_sha[f"{4:040x}"]["first_author_class"] == "noncoding_bot"
    assert by_sha[f"{4:040x}"]["noncoding_bot"] is True
    assert by_sha[f"{5:040x}"]["ai_tool_first"] == "claude,cursor"

    no_history = by_sha[f"{12:040x}"]
    assert no_history["history_fetched"] is False
    for column in (
        "commit_count",
        "first_commit_at",
        "last_commit_at",
        "span_days",
        "ai_tool_first",
        "ai_tool_last",
        "ai_trailer_first",
        "ai_trailer_last",
        "first_author_class",
        "coding_agent_bot",
        "noncoding_bot",
        "batch_size",
        "is_bulk",
        "msg_class_first",
    ):
        assert no_history[column] is None

    again = run(tmp_cfg)
    assert again.skipped is True
    assert again.rows == {"s2_commit": 13}


def test_dev_sample_keeps_full_batch_size(tmp_cfg: Config) -> None:
    _write_artifacts(tmp_cfg, _sample_rows())
    sample = devsample_path(tmp_cfg)
    sample.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"file_sha": [f"{0:040x}"], "stratum": ["test"]}),
        sample,
        compression="zstd",
    )
    result = run(tmp_cfg, dev=True)
    by_sha = _read(result.outputs[0])
    assert list(by_sha) == [f"{0:040x}"]
    assert by_sha[f"{0:040x}"]["batch_size"] == 7
    assert by_sha[f"{0:040x}"]["is_bulk"] is True


def test_missing_artifacts_names_the_download_command(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match="download_data.py"):
        run(tmp_cfg)


def test_missing_dev_sample_names_the_data_command(tmp_cfg: Config) -> None:
    _write_artifacts(tmp_cfg, _sample_rows()[:1])
    with pytest.raises(FileNotFoundError, match="skillsig data --dev-sample"):
        run(tmp_cfg, dev=True)


def _sample_rows() -> list[dict[str, object]]:
    bulk = [
        _row(
            0,
            first_commit_message=f"initial commit\n\n{_CLAUDE}\n",
            last_commit_message="Co-authored-by: Gemini <gemini@example.com>\n",
            last_commit_at=_LATER,
            commit_count=4,
        ),
        _row(1, first_commit_message="Add Copilot support"),
        _row(2, first_commit_message=None, last_commit_message=None),
        _row(
            3,
            first_commit_author="copilot-swe-agent[bot]",
            first_commit_author_type="Bot",
            first_commit_message="feat: implement the skill",
        ),
        _row(
            4,
            first_commit_author="github-actions[bot]",
            first_commit_author_type="Bot",
            first_commit_message="sync skills",
        ),
        _row(
            5,
            first_commit_message=(
                "subject\n\n"
                "Co-authored-by: Cursor <cursoragent@cursor.com>\n"
                "Co-authored-by: Claude <noreply@anthropic.com>\n"
            ),
        ),
        _row(6, first_commit_message="feat: add a widget"),
    ]
    small = [_row(i, repo_full_name="o/other") for i in range(7, 12)]
    no_history = _row(
        12,
        history_fetched=0,
        commit_count=99,
        first_commit_at=_WHEN,
        last_commit_at=_WHEN,
        first_commit_author="copilot-swe-agent[bot]",
        first_commit_author_type="Bot",
        first_commit_message=_CLAUDE,
        last_commit_message=_CLAUDE,
    )
    copy = _row(13, dedup_primary=0, first_commit_message=_CLAUDE)
    return [*bulk, *small, no_history, copy]


def _row(index: int, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "file_sha": f"{index:040x}",
        "repo_full_name": "o/bulk",
        "dedup_primary": 1,
        "history_fetched": 1,
        "commit_count": 1,
        "first_commit_at": _WHEN,
        "last_commit_at": _WHEN,
        "first_commit_author": f"user{index}",
        "first_commit_author_type": "User",
        "first_commit_message": "feat: add skill",
        "last_commit_message": "feat: add skill",
    }
    row.update(overrides)
    return row


def _write_artifacts(cfg: Config, rows: list[dict[str, object]]) -> None:
    path = resolve(cfg, cfg.paths.raw) / "artifacts" / "part-00000.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0])
    arrays = {}
    for name in columns:
        values = [row[name] for row in rows]
        kind = pa.int64() if name in _INT else pa.string()
        arrays[name] = pa.array(values, type=kind)
    pq.write_table(pa.table(arrays), path, compression="zstd")


def _read(path: Path) -> dict[str, dict[str, object]]:
    con = duckdb.connect()
    try:
        described = con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [path.as_posix()])
        names = [row[0] for row in described.fetchall()]
        assert names == [
            "file_sha",
            "history_fetched",
            "commit_count",
            "first_commit_at",
            "last_commit_at",
            "span_days",
            "ai_tool_first",
            "ai_tool_last",
            "ai_trailer_first",
            "ai_trailer_last",
            "first_author_class",
            "coding_agent_bot",
            "noncoding_bot",
            "batch_size",
            "is_bulk",
            "msg_class_first",
        ]
        table = con.execute("SELECT * FROM read_parquet(?)", [path.as_posix()])
        header = [item[0] for item in table.description]
        return {row[0]: dict(zip(header, row, strict=True)) for row in table.fetchall()}
    finally:
        con.close()
