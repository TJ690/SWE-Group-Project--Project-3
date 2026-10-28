"""Sprint 1 label rules: precedence, reason codes, and parquet output."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import duckdb
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s0_intake.devsample import output_path as devsample_path
from skillsig.s3_labeling import LABELS as PACKAGE_LABELS
from skillsig.s3_labeling import run as package_run
from skillsig.s3_labeling.rules_v1 import LABELS, label_v1, run

_CLAUDE = "Co-authored-by: Claude Sonnet <noreply@anthropic.com>\n"
_COLUMNS = (
    "file_sha",
    "dedup_primary",
    "history_fetched",
    "repo_full_name",
    "first_commit_at",
    "first_commit_author",
    "first_commit_author_type",
    "first_commit_message",
    "last_commit_message",
)


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "copies": 1,
        "history_fetched": True,
        "batch_size": 1,
        "noncoding_bot": False,
        "coding_agent_bot": False,
        "ai_trailer_first": False,
        "ai_trailer_last": False,
        "first_author_class": "user",
        "ai_tool_first": None,
        "ai_tool_last": None,
    }
    base.update(overrides)
    return base


def test_labels_constant() -> None:
    assert LABELS == (
        "highly_templated",
        "agent_assisted",
        "likely_human",
        "insufficient_evidence",
    )
    assert PACKAGE_LABELS is LABELS
    assert package_run is run


@pytest.mark.parametrize(
    ("row", "label", "reasons"),
    [
        (_row(copies=10, history_fetched=False), "highly_templated", ["copies>=10"]),
        (
            _row(ai_trailer_first=True, ai_tool_first="claude"),
            "agent_assisted",
            ["ai_trailer_first:claude"],
        ),
        (
            _row(),
            "likely_human",
            ["batch_size=1", "author=user", "copies=1", "no_ai_trailer"],
        ),
        (_row(history_fetched=False), "insufficient_evidence", ["no_history"]),
    ],
)
def test_one_fixture_row_per_label(
    cfg: Config, row: dict[str, object], label: str, reasons: list[str]
) -> None:
    assert label in LABELS
    assert label_v1(row, cfg) == (label, reasons)


def test_templated_beats_agent_assisted(cfg: Config) -> None:
    row = _row(
        copies=12,
        ai_trailer_first=True,
        ai_tool_first="claude",
        coding_agent_bot=True,
        first_author_class="coding_agent_bot",
    )
    assert label_v1(row, cfg) == ("highly_templated", ["copies>=10"])


def test_coding_agent_and_trailer_reasons(cfg: Config) -> None:
    row = _row(
        copies=3,
        batch_size=2,
        coding_agent_bot=True,
        first_author_class="coding_agent_bot",
        ai_trailer_first=True,
        ai_tool_first="codex",
    )
    assert label_v1(row, cfg) == (
        "agent_assisted",
        ["coding_agent_bot", "ai_trailer_first:codex"],
    )


def test_bulk_batch_blocks_trailer(cfg: Config) -> None:
    row = _row(batch_size=5, ai_trailer_first=True, ai_tool_first="gemini")
    assert label_v1(row, cfg)[0] == "agent_assisted"
    blocked = _row(batch_size=6, ai_trailer_first=True, ai_tool_first="claude")
    label, reasons = label_v1(blocked, cfg)
    assert label == "insufficient_evidence"
    assert reasons == ["bulk_batch>=6", "ai_trailer_first:claude"]


def test_human_blockers(cfg: Config) -> None:
    last = _row(ai_trailer_last=True, ai_tool_last="claude")
    assert label_v1(last, cfg) == (
        "insufficient_evidence",
        ["no_agent_signal", "ai_trailer_last:claude"],
    )
    copies = _row(copies=2)
    assert label_v1(copies, cfg) == ("insufficient_evidence", ["no_agent_signal", "copies=2"])
    org = _row(first_author_class="organization")
    assert label_v1(org, cfg) == (
        "insufficient_evidence",
        ["no_agent_signal", "author=organization"],
    )
    noncoding = _row(
        noncoding_bot=True,
        first_author_class="noncoding_bot",
        ai_trailer_first=True,
        ai_tool_first="claude",
    )
    assert label_v1(noncoding, cfg) == (
        "insufficient_evidence",
        ["noncoding_bot", "ai_trailer_first:claude"],
    )


def test_thresholds_come_from_config(cfg: Config) -> None:
    tight = dataclasses.replace(
        cfg,
        thresholds=dataclasses.replace(
            cfg.thresholds, templated_copies_min=3, bulk_batch_min=4
        ),
    )
    assert label_v1(_row(copies=3, history_fetched=False), tight)[1] == ["copies>=3"]
    label, reasons = label_v1(
        _row(batch_size=4, ai_trailer_first=True, ai_tool_first="claude"), tight
    )
    assert label == "insufficient_evidence"
    assert reasons[0] == "bulk_batch>=4"


def test_run_writes_sprint1_labels(tmp_cfg: Config) -> None:
    _write_inputs(tmp_cfg, _population())
    result = run(tmp_cfg)
    assert result.skipped is False
    assert result.rows == {"s3_labels_v1": len(_expected_labels())}
    assert result.outputs[0] == resolve(tmp_cfg, tmp_cfg.paths.derived) / "s3_labels_v1.parquet"
    got = _read_labels(result.outputs[0])
    assert list(got) == sorted(got)
    assert got == _expected_labels()
    assert _columns(result.outputs[0]) == ["file_sha", "label", "copies", "reasons"]

    again = run(tmp_cfg)
    assert again.skipped is True
    assert again.rows == {"s3_labels_v1": len(_expected_labels())}
    forced = run(tmp_cfg, force=True)
    assert forced.skipped is False


def test_run_dev_keeps_sampled_ids(tmp_cfg: Config) -> None:
    _write_inputs(tmp_cfg, _population())
    _write_sample(devsample_path(tmp_cfg), ["h_human", "missing"])
    result = run(tmp_cfg, dev=True)
    assert result.rows == {"s3_labels_v1": 1}
    got = _read_labels(result.outputs[0])
    assert list(got) == ["h_human"]
    assert got["h_human"][0] == "likely_human"


def test_missing_commit_names_command(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match=r"features --only commit"):
        run(tmp_cfg)


def test_missing_artifacts_names_download(tmp_cfg: Config) -> None:
    _write_commit(resolve(tmp_cfg, tmp_cfg.paths.derived) / "s2_commit.parquet", ["h_human"])
    with pytest.raises(FileNotFoundError, match=r"download_data\.py"):
        run(tmp_cfg)


def test_missing_dev_sample_names_data_command(tmp_cfg: Config) -> None:
    _write_inputs(tmp_cfg, _population()[:1])
    with pytest.raises(FileNotFoundError, match=r"python -m skillsig data --dev-sample"):
        run(tmp_cfg, dev=True)


def _population() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    rows.extend(
        _copies(
            "t_templated",
            10,
            repo="owner/templated",
            author="Ada",
            author_type="User",
            message=_CLAUDE,
        )
    )
    rows.append(_primary("a_agent", "owner/agent", "Ada", "User", _CLAUDE))
    rows.append(_primary("h_human", "owner/human", "Ada", "User", "add a skill file\n"))
    rows.append(
        _primary(
            "u_loose",
            "owner/loose",
            "Ada",
            "User",
            "docs\n\nCo-authored-by: Ada <a@b>\nGenerated with Claude\n",
        )
    )
    rows.append(_primary("s_subject", "owner/subject", "Ada", "User", "Add Copilot support\n"))
    rows.append(
        _primary(
            "m_multi",
            "owner/multi",
            "Ada",
            "User",
            "Co-authored-by: Claude <a@b>\nCo-authored-by: Cursor <c@d>\n",
        )
    )
    rows.append(_primary("c_both", "owner/both", "Copilot", "Bot", _CLAUDE))
    rows.append(_primary("o_opencode", "owner/open", "opencode-agent[bot]", "Bot", "add skill\n"))
    rows.append(_primary("w_actions", "owner/actions", "github-actions[bot]", "Bot", _CLAUDE))
    rows.append(
        _primary("j_jessica", "owner/jessica", "jessica-claude[bot]", "Bot", "add skill\n")
    )
    rows.append(_primary("g_org", "owner/org", "acme", "Organization", "add skill\n"))
    rows.append(_primary("e_empty", "owner/empty", "", "", "add skill\n"))
    rows.append(
        _primary("l_last", "owner/last", "Ada", "User", "add a skill file\n", last=_CLAUDE)
    )
    rows.append(
        _primary("z_nulltime", "owner/null", "Ada", "User", "add a skill file\n", when=None)
    )
    lone = _primary("n_nohist", "owner/none", "Ada", "User", _CLAUDE)
    lone["history_fetched"] = 0
    rows.append(lone)
    rows.extend(
        _copies("x_copies2", 2, repo="owner/two", author="Ada", author_type="User", message="add\n")
    )
    rows.extend(
        _copies(
            "p_partial",
            4,
            repo="owner/partial",
            author="Ada",
            author_type="User",
            message=_CLAUDE,
            history=0,
        )
    )
    for index in range(6):
        message = _CLAUDE if index == 0 else "add skill\n"
        rows.append(
            _primary(
                f"b_bulk{index}",
                "owner/bulk",
                "Ada",
                "User",
                message,
                when="2026-02-02T00:00:00Z",
            )
        )
    return rows


def _expected_labels() -> dict[str, tuple[str, int, list[str]]]:
    human = ("likely_human", 1, ["batch_size=1", "author=user", "copies=1", "no_ai_trailer"])
    bulk = ("insufficient_evidence", 1, ["bulk_batch>=6"])
    expected = {
        "a_agent": ("agent_assisted", 1, ["ai_trailer_first:claude"]),
        "b_bulk0": ("insufficient_evidence", 1, ["bulk_batch>=6", "ai_trailer_first:claude"]),
        "c_both": ("agent_assisted", 1, ["coding_agent_bot", "ai_trailer_first:claude"]),
        "e_empty": ("insufficient_evidence", 1, ["no_agent_signal", "author=unknown"]),
        "g_org": ("insufficient_evidence", 1, ["no_agent_signal", "author=organization"]),
        "h_human": human,
        "j_jessica": ("insufficient_evidence", 1, ["noncoding_bot"]),
        "l_last": ("insufficient_evidence", 1, ["no_agent_signal", "ai_trailer_last:claude"]),
        "m_multi": ("agent_assisted", 1, ["ai_trailer_first:claude,cursor"]),
        "n_nohist": ("insufficient_evidence", 1, ["no_history"]),
        "o_opencode": ("agent_assisted", 1, ["coding_agent_bot"]),
        "p_partial": ("insufficient_evidence", 4, ["no_history", "copies=4"]),
        "s_subject": human,
        "t_templated": ("highly_templated", 10, ["copies>=10"]),
        "u_loose": ("agent_assisted", 1, ["ai_trailer_first:claude"]),
        "w_actions": ("insufficient_evidence", 1, ["noncoding_bot", "ai_trailer_first:claude"]),
        "x_copies2": ("insufficient_evidence", 2, ["no_agent_signal", "copies=2"]),
        "z_nulltime": ("insufficient_evidence", 1, ["batch_size=0", "no_agent_signal"]),
    }
    for index in range(1, 6):
        expected[f"b_bulk{index}"] = bulk
    return expected


def _primary(
    file_sha: str,
    repo: str,
    author: str,
    author_type: str,
    message: str,
    *,
    when: str | None = "2026-01-01T00:00:00Z",
    last: str | None = None,
) -> dict[str, object]:
    return {
        "file_sha": file_sha,
        "dedup_primary": 1,
        "history_fetched": 1,
        "repo_full_name": repo,
        "first_commit_at": when,
        "first_commit_author": author,
        "first_commit_author_type": author_type,
        "first_commit_message": message,
        "last_commit_message": "add skill\n" if last is None else last,
    }


def _copies(
    file_sha: str,
    n: int,
    *,
    repo: str,
    author: str,
    author_type: str,
    message: str,
    history: int = 1,
) -> list[dict[str, object]]:
    rows = []
    for index in range(n):
        if index == 0:
            row = _primary(file_sha, repo, author, author_type, message)
            row["history_fetched"] = history
            rows.append(row)
            continue
        rows.append(
            {
                "file_sha": file_sha,
                "dedup_primary": 0,
                "history_fetched": 0,
                "repo_full_name": f"copy/{file_sha}-{index}",
                "first_commit_at": None,
                "first_commit_author": None,
                "first_commit_author_type": None,
                "first_commit_message": None,
                "last_commit_message": None,
            }
        )
    return rows


def _write_inputs(cfg: Config, rows: list[dict[str, object]]) -> None:
    directory = resolve(cfg, cfg.paths.raw) / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    _write_table(directory / "artifacts.parquet", _COLUMNS, rows)
    shas = [str(row["file_sha"]) for row in rows if row["dedup_primary"] == 1]
    shas.append("only-in-commit")
    _write_commit(resolve(cfg, cfg.paths.derived) / "s2_commit.parquet", shas)


def _write_commit(path: Path, file_shas: list[str]) -> None:
    _write_table(path, ("file_sha",), [{"file_sha": sha} for sha in file_shas])


def _write_sample(path: Path, file_shas: list[str]) -> None:
    _write_table(
        path,
        ("file_sha", "stratum"),
        [{"file_sha": sha, "stratum": "canonical|history=1|copies=1"} for sha in file_shas],
    )


def _write_table(path: Path, columns: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        specs = ", ".join(f"{name} VARCHAR" for name in columns)
        if columns == _COLUMNS:
            specs = (
                "file_sha VARCHAR, dedup_primary INTEGER, history_fetched INTEGER, "
                "repo_full_name VARCHAR, first_commit_at VARCHAR, first_commit_author VARCHAR, "
                "first_commit_author_type VARCHAR, first_commit_message VARCHAR, "
                "last_commit_message VARCHAR"
            )
        con.execute(f"CREATE TABLE t ({specs})")
        holders = ", ".join("?" for _ in columns)
        con.executemany(
            f"INSERT INTO t VALUES ({holders})",
            [tuple(row[name] for name in columns) for row in rows],
        )
        dest = "'" + path.as_posix().replace("'", "''") + "'"
        con.execute(f"COPY t TO {dest} (FORMAT PARQUET, COMPRESSION ZSTD)")
    finally:
        con.close()


def _read_labels(path: Path) -> dict[str, tuple[str, int, list[str]]]:
    con = duckdb.connect()
    try:
        fetched = con.execute(
            """
            SELECT file_sha, label, copies, reasons
            FROM read_parquet(?)
            ORDER BY file_sha
            """,
            [path.as_posix()],
        ).fetchall()
    finally:
        con.close()
    return {
        sha: (label, int(copies), list(reasons))
        for sha, label, copies, reasons in fetched
    }


def _columns(path: Path) -> list[str]:
    con = duckdb.connect()
    try:
        names = con.execute(
            "SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet(?))",
            [path.as_posix()],
        ).fetchall()
    finally:
        con.close()
    return [name for (name,) in names]
