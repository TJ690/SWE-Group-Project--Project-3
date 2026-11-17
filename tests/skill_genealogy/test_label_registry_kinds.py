"""Tests for stage 3c, registry kinds (source or copier).

Tiny hand-made stage 3 tables. No network and no dataset files.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from skill_genealogy.label_registry_kinds import (
    build_registry_kinds,
    known_case_check,
    share_sensitivity,
    upstream_shares,
)

_T1, _T2, _T3, _T4 = ("1" * 40, "2" * 40, "3" * 40, "4" * 40)


def _row(repo: str, sha: str, date: str, status: str = "A") -> dict:
    """One SKILL.md change row with the columns stage 3c reads."""
    return {
        "repo": repo,
        "skill_folder": "skills/x",
        "is_skill_md": True,
        "status": status,
        "new_sha": "0" * 40 if status == "D" else sha,
        "commit_date": date,
    }


def _history(folder: Path) -> Path:
    """``a/orig`` writes T1. ``b/mirror`` copies T1 and T2 later. ``c/other`` writes T2.

    ``a/sibling`` (same owner as ``a/orig``) has T1 last. ``d/alone`` shares
    nothing. A delete row of T4 in ``b/mirror`` must not count as a text.
    """
    rows = [
        _row("a/orig", _T1, "2026-01-01T00:00:00+00:00"),
        _row("b/mirror", _T1, "2026-02-01T00:00:00+00:00"),
        _row("b/mirror", _T2, "2026-02-01T09:00:00+09:00"),
        _row("c/other", _T2, "2026-01-15T00:00:00Z"),
        _row("a/sibling", _T1, "2026-03-01T00:00:00+00:00"),
        _row("d/alone", _T3, "2026-01-01T00:00:00+00:00"),
        _row("b/mirror", _T4, "2026-04-01T00:00:00+00:00", status="D"),
    ]
    folder.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(folder / "registry_history_changes.parquet", index=False)
    status = pd.DataFrame(
        {
            "repo": ["a/orig", "b/mirror", "c/other", "a/sibling", "d/alone"],
            "skill_folders": [3, 900, 2, 1, 4],
        }
    )
    status.to_parquet(folder / "registry_history_status.parquet", index=False)
    return folder


_REPOS = ["a/orig", "a/sibling", "b/mirror", "c/other", "d/alone"]


def test_upstream_share_counts_who_had_each_text_first(tmp_path: Path) -> None:
    """The mirror had both texts after another owner; owners count as one; deletes are ignored."""
    history = _history(tmp_path / "registry_history")
    shares = upstream_shares(history / "registry_history_changes.parquet", _REPOS).set_index("repo")
    assert shares.loc["b/mirror", "shared_texts"] == 2
    assert shares.loc["b/mirror", "elsewhere_first"] == 2
    assert shares.loc["b/mirror", "upstream_share"] == 1.0
    assert shares.loc["a/orig", "elsewhere_first"] == 0
    assert shares.loc["c/other", "elsewhere_first"] == 0
    # The sibling had T1 after b/mirror (another owner), so that counts as elsewhere first.
    assert shares.loc["a/sibling", "elsewhere_first"] == 1
    assert "d/alone" not in shares.index
    assert shares.loc["b/mirror", "top_upstream"] in {"a/orig 1, c/other 1", "c/other 1, a/orig 1"}


def test_build_labels_votes_and_writes_a_review_sheet(tmp_path: Path) -> None:
    """Date and README votes combine; a lone registry is a source; doubtful rows go to the sheet."""
    history = _history(tmp_path / "registry_history")
    readmes = tmp_path / "registry_readmes.parquet"
    pd.DataFrame(
        {
            "repo": ["b/mirror", "c/other"],
            "status": ["ok", "ok"],
            "readme_text": [
                "This is an aggregate catalog of agent skills.",
                "Official skills for our tool.",
            ],
        }
    ).to_parquet(readmes, index=False)
    out, sheet = tmp_path / "registry_kinds.parquet", tmp_path / "registry_kind_review.csv"
    kinds = build_registry_kinds(_REPOS, _REPOS, history, readmes, out, sheet).set_index("repo")
    assert kinds.loc["b/mirror", "registry_kind"] == "copier"
    assert kinds.loc["b/mirror", "kind_basis"] == "date+readme"
    assert kinds.loc["d/alone", "registry_kind"] == "source"
    assert kinds.loc["d/alone", "confidence"] == 0.7
    assert kinds.loc["c/other", "kind_basis"] == "date+readme"
    # Two shared texts are weak date evidence, so the mirror alone (no README) would be doubtful.
    assert bool(kinds.loc["a/sibling", "needs_review"]) is True
    review = pd.read_csv(sheet, dtype=str, keep_default_na=False)
    assert "a/sibling" in set(review["repo"])
    assert list(review["your_label"].unique()) == [""]
    assert pd.read_parquet(out).shape[0] == len(_REPOS)


def test_hand_labels_are_used_and_a_filled_sheet_is_kept(tmp_path: Path) -> None:
    """A label written in the sheet replaces the kind on the next run, and the sheet stays."""
    history = _history(tmp_path / "registry_history")
    out, sheet = tmp_path / "registry_kinds.parquet", tmp_path / "registry_kind_review.csv"
    missing = tmp_path / "no_readmes.parquet"
    build_registry_kinds(_REPOS, _REPOS, history, missing, out, sheet)
    review = pd.read_csv(sheet, dtype=str, keep_default_na=False)
    review.loc[review["repo"] == "a/sibling", "your_label"] = "source"
    review.loc[review["repo"] == "a/sibling", "note"] = "same team as a/orig"
    review.to_csv(sheet, index=False)
    kinds = build_registry_kinds(_REPOS, _REPOS, history, missing, out, sheet).set_index("repo")
    assert kinds.loc["a/sibling", "registry_kind"] == "source"
    assert kinds.loc["a/sibling", "kind_basis"] == "hand"
    assert bool(kinds.loc["a/sibling", "needs_review"]) is False
    kept = pd.read_csv(sheet, dtype=str, keep_default_na=False)
    assert kept.loc[kept["repo"] == "a/sibling", "note"].item() == "same team as a/orig"


def test_known_cases_and_sensitivity_report(tmp_path: Path) -> None:
    """Known registries missing from a run are reported, and cutoffs need 20+ shared texts."""
    kinds = pd.DataFrame(
        {
            "repo": ["anthropics/skills", "x/big"],
            "registry_kind": ["source", "copier"],
            "shared_texts": [63, 40],
            "upstream_share": [0.095, 0.85],
            "skill_folders": [20, 500],
        }
    )
    lines = known_case_check(kinds)
    assert "anthropics/skills: expected source, got source (ok)" in lines
    missing = "obra/superpowers: expected source, not in this run"
    assert any(line.startswith(missing) for line in lines)
    table = share_sensitivity(kinds).set_index("share_at_least")
    assert table.loc[0.8, "registries"] == 1 and table.loc[0.9, "registries"] == 0


def test_a_registry_that_borrowed_a_few_texts_is_a_source(tmp_path: Path) -> None:
    """One borrowed text out of 40 own texts: 1 of 1 shared came first elsewhere, still a source."""
    history = _history(tmp_path / "registry_history")
    changes = pd.read_parquet(history / "registry_history_changes.parquet")
    day = "2026-01-01T00:00:00+00:00"
    own = [_row("e/author", "e" * 37 + f"{n:03d}", day) for n in range(100, 140)]
    borrowed = _row("e/author", _T1, "2026-05-01T00:00:00+00:00")
    changes = pd.concat([changes, pd.DataFrame([*own, borrowed])], ignore_index=True)
    changes.to_parquet(history / "registry_history_changes.parquet", index=False)
    repos = [*_REPOS, "e/author"]
    out, sheet = tmp_path / "registry_kinds.parquet", tmp_path / "registry_kind_review.csv"
    kinds = build_registry_kinds(repos, repos, history, tmp_path / "none.parquet", out, sheet)
    kinds = kinds.set_index("repo")
    assert kinds.loc["e/author", "upstream_share"] == 1.0
    assert kinds.loc["e/author", "own_texts"] == 41
    assert kinds.loc["e/author", "registry_kind"] == "source"
    assert kinds.loc["e/author", "confidence"] == 0.7
    # The mirror copied everything it holds, so the new rule leaves it a copier.
    assert kinds.loc["b/mirror", "copied_share"] == 1.0
    assert kinds.loc["b/mirror", "registry_kind"] == "copier"
