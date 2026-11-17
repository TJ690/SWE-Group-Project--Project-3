"""Stage 3c: label each registry as a source or a copier.

What: a source writes its own skills. A copier collects skills from other
repositories (an aggregate catalog, a mirror, a marketplace). Later stages must
not take a copier as the origin of a skill. Each registry gets two votes and a
confidence:

- The date vote reads the stage 3 history. For each ``SKILL.md`` text a
  registry shares with registries of other owners, it asks who had that exact
  text first. Nobody can copy a text before it exists, so a registry where most
  shared texts appeared elsewhere first is a copier (Algorithm A60).
- The README vote reads the stage 3b README and looks for fixed phrases, such
  as "aggregate catalog", "this mirror" or "our skills" (Algorithm A61).
- The two votes are combined into ``registry_kind`` (``source``, ``copier`` or
  ``unclear``) and a confidence from 0 to 1 (Algorithm A62). The confidence is
  rule-based: it ranks registries from doubtful to clear. It is not a measured
  probability.

Registries below ``KIND_REVIEW_BELOW`` go to a review sheet, largest first, so a
person can check the ones that weigh most. A label a person writes there is used
on the next run (``--force``). A filled sheet is never overwritten.

Inputs: ``registries.parquet`` (or ``dev_registries.parquet`` with ``--dev``),
``registry_history/registry_history_changes.parquet`` and
``registry_history_status.parquet`` from stage 3, and ``registry_readmes.parquet``
from stage 3b (optional: without it there is no README vote). The date vote
always compares with every registry, also in a dev run.
Outputs: ``registry_kinds.parquet`` and ``registry_kind_review.csv`` in the dev
or full data folder.

Run (no network):
    python -m skill_genealogy.label_registry_kinds
    python -m skill_genealogy.label_registry_kinds --force    # after filling the sheet
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pandas as pd

from skill_genealogy import settings
from skill_genealogy.download_registry_history import read_registry_list
from skill_genealogy.file_helpers import (
    build_arg_parser,
    connect_duckdb,
    outputs_exist,
    print_sample_records,
    print_summary,
    read_parquet,
    write_parquet,
)

KIND_COLUMNS: tuple[str, ...] = (
    "repo",
    "registry_kind",
    "confidence",
    "needs_review",
    "kind_basis",
    "skill_folders",
    "shared_texts",
    "elsewhere_first",
    "upstream_share",
    "own_texts",
    "copied_share",
    "top_upstream",
    "date_vote",
    "date_strength",
    "readme_vote",
    "readme_strength",
    "readme_phrase",
    "hand_label",
)
REVIEW_COLUMNS: tuple[str, ...] = (
    "repo",
    "skill_folders",
    "confidence",
    "registry_kind",
    "kind_basis",
    "upstream_share",
    "shared_texts",
    "own_texts",
    "copied_share",
    "top_upstream",
    "readme_phrase",
    "readme_start",
    "your_label",
    "note",
)
# Hand labels the review sheet accepts.
_HAND_LABELS = frozenset({"source", "copier", "unclear"})
# Registries checked by hand in the G38 analysis (README, metadata, history).
# Printed as a check after each run; they are not used by the rule.
KNOWN_KINDS: dict[str, str] = {
    "majiayu000/claude-skill-registry": "copier",
    "lingxling/awesome-skills-cn": "copier",
    "aiskillstore/marketplace": "copier",
    "anthropics/skills": "source",
    "obra/superpowers": "source",
    "github/awesome-copilot": "source",
}
_README_START_CHARS = 500
# A README is split into sentences at . ! ? and blank lines; only sentences with
# a skill word are read. A negation within three words before a phrase cancels it.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n\s*\n|\n#+ ")
_SKILL_WORD = re.compile(r"skill")
_NEGATIONS = frozenset({"not", "no", "never", "isn't", "isnt"})

# Query Q3c.1, see QUERIES.md
# For each SKILL.md text (blob), the first time each registry had it. Per text,
# the earliest owner and the second earliest owner. A registry's "other first"
# time is the earliest time of any other owner. Strictly earlier counts.
SHARED_TEXTS_SQL = """
CREATE OR REPLACE TEMP TABLE first_seen AS
SELECT repo, split_part(repo, '/', 1) AS owner, new_sha AS sha,
       min(try_cast(commit_date AS TIMESTAMPTZ)) AS seen_at
FROM read_parquet(?)
WHERE is_skill_md AND status <> 'D' AND new_sha IS NOT NULL
  AND new_sha NOT LIKE '0000000%'
  AND repo IN (SELECT repo FROM compared_repos)
GROUP BY 1, 2, 3;

CREATE OR REPLACE TEMP TABLE owner_first AS
SELECT sha, owner, min(seen_at) AS seen_at, arg_min(repo, seen_at) AS first_repo
FROM first_seen WHERE seen_at IS NOT NULL
GROUP BY 1, 2;

CREATE OR REPLACE TEMP TABLE text_order AS
SELECT sha,
       max(owner) FILTER (WHERE rank = 1) AS owner_1,
       max(seen_at) FILTER (WHERE rank = 1) AS seen_1,
       max(first_repo) FILTER (WHERE rank = 1) AS repo_1,
       max(seen_at) FILTER (WHERE rank = 2) AS seen_2,
       max(first_repo) FILTER (WHERE rank = 2) AS repo_2
FROM (
    SELECT *, row_number() OVER (PARTITION BY sha ORDER BY seen_at, owner) AS rank,
              count(*) OVER (PARTITION BY sha) AS owners
    FROM owner_first
)
WHERE owners > 1
GROUP BY 1;

CREATE OR REPLACE TEMP TABLE shared AS
SELECT f.repo, f.sha, f.seen_at,
       CASE WHEN f.owner = o.owner_1 THEN o.seen_2 ELSE o.seen_1 END AS other_seen,
       CASE WHEN f.owner = o.owner_1 THEN o.repo_2 ELSE o.repo_1 END AS other_repo
FROM first_seen AS f JOIN text_order AS o USING (sha)
WHERE f.seen_at IS NOT NULL;
"""
# Query Q3c.2, see QUERIES.md
SHARE_PER_REPO_SQL = """
SELECT repo,
       count(*) AS shared_texts,
       count(*) FILTER (WHERE other_seen < seen_at) AS elsewhere_first
FROM shared GROUP BY repo
"""
# Query Q3c.4, see QUERIES.md
# All distinct SKILL.md texts a registry had, shared or not.
OWN_TEXTS_SQL = """
SELECT repo, count(*) AS own_texts
FROM first_seen WHERE seen_at IS NOT NULL GROUP BY repo
"""
# Query Q3c.3, see QUERIES.md
# The three registries that had this registry's texts first most often.
TOP_UPSTREAM_SQL = """
SELECT repo,
       string_agg(other_repo || ' ' || texts, ', ' ORDER BY texts DESC, other_repo)
           AS top_upstream
FROM (
    SELECT repo, other_repo, count(*) AS texts,
           row_number() OVER (PARTITION BY repo ORDER BY count(*) DESC, other_repo) AS rank
    FROM shared WHERE other_seen < seen_at
    GROUP BY repo, other_repo
)
WHERE rank <= 3
GROUP BY repo
"""


def kinds_path(dev: bool) -> Path:
    """``registry_kinds.parquet`` in the dev or full data folder."""
    return settings.data_dir(dev) / "registry_kinds.parquet"


def review_path(dev: bool) -> Path:
    """``registry_kind_review.csv`` in the dev or full data folder."""
    return settings.data_dir(dev) / "registry_kind_review.csv"


def upstream_shares(changes_path: Path, compared: list[str]) -> pd.DataFrame:
    """Shared texts and who had them first, for every registry in ``compared``.

    Algorithm A60, queries Q3c.1–Q3c.3. A text is shared when registries of at
    least two owners have it. ``elsewhere_first`` counts the shared texts that a
    registry of another owner had strictly earlier. Registries of one owner
    count as one, so a sibling repo is not an upstream.

    ``own_texts`` counts all distinct ``SKILL.md`` texts of the registry, and
    ``copied_share`` is ``elsewhere_first / own_texts``: the part of everything
    it holds that another owner had first.

    Inputs: the stage 3 changes parquet and the registries to compare with.
    Outputs: ``repo``, ``shared_texts``, ``elsewhere_first``, ``upstream_share``,
    ``own_texts``, ``copied_share``, ``top_upstream`` for registries that share
    at least one text.
    """
    con = connect_duckdb()
    try:
        con.execute(f"SET memory_limit = '{settings.HISTORY_DUCKDB_MEMORY_LIMIT}'")
        con.register("compared_repos", pd.DataFrame({"repo": pd.Series(compared, dtype="string")}))
        for statement in SHARED_TEXTS_SQL.split(";"):
            if statement.strip():
                con.execute(
                    statement, [str(changes_path)] if "read_parquet(?)" in statement else []
                )
        shares = con.execute(SHARE_PER_REPO_SQL).df()
        top = con.execute(TOP_UPSTREAM_SQL).df()
        own = con.execute(OWN_TEXTS_SQL).df()
    finally:
        con.close()
    shares = shares.merge(top, on="repo", how="left").merge(own, on="repo", how="left")
    shares["upstream_share"] = shares["elsewhere_first"] / shares["shared_texts"]
    shares["copied_share"] = shares["elsewhere_first"] / shares["own_texts"]
    return shares


def date_vote(
    shared_texts: int, upstream_share: float | None, copied_share: float | None = None
) -> tuple[str | None, float]:
    """The date vote: direction and strength (Algorithm A60).

    No shared text: ``source`` with ``KIND_NO_SHARED_SOURCE_STRENGTH``.
    Otherwise the direction is ``copier`` above 0.5 and ``source`` below, and the
    strength is ``|share - 0.5| / 0.5 * min(1, shared / KIND_FULL_EVIDENCE_TEXTS)``.
    A share at or below ``KIND_LOW_SHARE_SOURCE_MAX`` is a source vote of at
    least ``KIND_NO_SHARED_SOURCE_STRENGTH``. Exactly 0.5 gives no direction.
    With fewer than ``KIND_FULL_EVIDENCE_TEXTS`` shared texts, a copier
    direction whose ``copied_share`` (texts first seen elsewhere over all own
    texts) is below ``KIND_COPIED_SHARE_SOURCE_MAX`` becomes a source vote of
    ``KIND_NO_SHARED_SOURCE_STRENGTH``: it borrowed a few texts and wrote the
    rest. With 20 or more shared texts the rule does not apply: a crawler of
    repos outside the registries also has many unshared texts.

    Example:
    >>> date_vote(1, 1.0, copied_share=1 / 600)
    ('source', 0.7)
    >>> date_vote(1, 1.0, copied_share=1.0)
    ('copier', 0.05)
    >>> date_vote(49661, 0.955, copied_share=0.094)
    ('copier', 0.91)
    >>> date_vote(0, None)
    ('source', 0.7)
    >>> date_vote(45781, 0.946)
    ('copier', 0.892)
    >>> date_vote(5, 0.9)
    ('copier', 0.2)
    >>> date_vote(63, 0.095)
    ('source', 0.81)
    >>> date_vote(10, 0.5)
    (None, 0.0)
    """
    if not shared_texts:
        return "source", settings.KIND_NO_SHARED_SOURCE_STRENGTH
    share = float(upstream_share or 0.0)
    evidence = min(1.0, shared_texts / settings.KIND_FULL_EVIDENCE_TEXTS)
    strength = round(abs(share - 0.5) / 0.5 * evidence, 3)
    if share > 0.5:
        if (
            shared_texts < settings.KIND_FULL_EVIDENCE_TEXTS
            and copied_share is not None
            and copied_share < settings.KIND_COPIED_SHARE_SOURCE_MAX
        ):
            return "source", settings.KIND_NO_SHARED_SOURCE_STRENGTH
        return "copier", strength
    if share == 0.5:
        return None, 0.0
    if share <= settings.KIND_LOW_SHARE_SOURCE_MAX:
        strength = max(strength, settings.KIND_NO_SHARED_SOURCE_STRENGTH)
    return "source", round(strength, 3)


def readme_vote(text: str | None) -> tuple[str | None, float, str]:
    """The README vote: direction, strength and the phrase that matched (Algorithm A61).

    The README is split into sentences. A phrase counts only as whole words,
    only in a sentence that mentions a skill, and not right after "not", "no"
    or "never". A copier phrase (``README_COPIER_STRONG``) wins over a source
    phrase (``README_SOURCE``). No phrase, or no README, is no vote.

    Example:
    >>> readme_vote("This is an aggregate catalog, not a single first-party skill pack.")
    ('copier', 0.8, 'aggregate catalog')
    >>> readme_vote("# Skills\\nOfficial skills for our database.")
    ('source', 0.6, 'official skills')
    >>> readme_vote("This skill pack is a curated distribution, not a mirror of our workspace.")
    (None, 0.0, '')
    >>> readme_vote("This mirrors the skill layout of the server.")
    (None, 0.0, '')
    >>> readme_vote("Commits need a vote from a committer who is not the author.")
    (None, 0.0, '')
    >>> readme_vote(None)
    (None, 0.0, '')
    """
    if not isinstance(text, str) or not text.strip():
        return None, 0.0, ""
    sentences = [
        part for part in _SENTENCE_BREAK.split(text.lower()) if _SKILL_WORD.search(part)
    ]
    for phrases, kind, strength in (
        (settings.README_COPIER_STRONG, "copier", settings.README_STRONG_STRENGTH),
        (settings.README_SOURCE, "source", settings.README_SOURCE_STRENGTH),
    ):
        for phrase in phrases:
            if any(_phrase_said(phrase, sentence) for sentence in sentences):
                return kind, strength, phrase
    return None, 0.0, ""


def _phrase_said(phrase: str, sentence: str) -> bool:
    """True when ``phrase`` is in ``sentence`` as whole words and not negated.

    Example:
    >>> _phrase_said("a mirror of", "this is a mirror of other skills")
    True
    >>> _phrase_said("a mirror of", "this is not a mirror of other skills")
    False
    >>> _phrase_said("this mirror", "this mirrors the skill tree")
    False
    """
    flat = re.sub(r"\s+", " ", sentence)
    words = r"\s+".join(re.escape(word) for word in phrase.split())
    for match in re.finditer(rf"(?<![\w-]){words}(?![\w-])", flat):
        before = flat[max(0, match.start() - 30) : match.start()].split()
        if not set(before[-3:]) & _NEGATIONS:
            return True
    return False


def combine_votes(
    date: tuple[str | None, float],
    readme: tuple[str | None, float],
) -> tuple[str, float, str]:
    """Kind, confidence and basis from the two votes (Algorithm A62).

    Both agree: ``1 - (1 - date) * (1 - readme)``. One vote: its strength.
    They disagree: the stronger direction, with confidence ``|date - readme|``.
    No vote: ``unclear`` with 0.

    Example:
    >>> combine_votes(("copier", 0.892), ("copier", 0.8))
    ('copier', 0.978, 'date+readme')
    >>> combine_votes(("source", 0.81), (None, 0.0))
    ('source', 0.81, 'date')
    >>> combine_votes(("source", 0.7), ("copier", 0.5))
    ('source', 0.2, 'date_vs_readme')
    >>> combine_votes((None, 0.0), (None, 0.0))
    ('unclear', 0.0, 'none')
    """
    date_dir, date_strength = date
    readme_dir, readme_strength = readme
    if date_dir and readme_dir:
        if date_dir == readme_dir:
            confidence = 1 - (1 - date_strength) * (1 - readme_strength)
            return date_dir, round(confidence, 3), "date+readme"
        winner = date_dir if date_strength >= readme_strength else readme_dir
        return winner, round(abs(date_strength - readme_strength), 3), "date_vs_readme"
    if date_dir:
        return date_dir, round(date_strength, 3), "date"
    if readme_dir:
        return readme_dir, round(readme_strength, 3), "readme"
    return "unclear", 0.0, "none"


def label_registries(
    repos: list[str],
    shares: pd.DataFrame,
    readmes: pd.DataFrame,
    folders: dict[str, int],
) -> pd.DataFrame:
    """One row per registry with both votes, the kind and the confidence.

    Inputs: the registries to label, ``upstream_shares`` rows, README rows
    (``repo``, ``status``, ``readme_text``), and skill folders per repo.
    Outputs: a frame with the ``KIND_COLUMNS``, sorted by repo.
    """
    by_repo = shares.set_index("repo") if len(shares) else pd.DataFrame()
    texts = _readme_texts(readmes)
    rows: list[dict] = []
    for repo in sorted(set(repos)):
        share = by_repo.loc[repo] if repo in by_repo.index else None
        shared = int(share["shared_texts"]) if share is not None else 0
        upstream = float(share["upstream_share"]) if share is not None else None
        own = int(share["own_texts"]) if share is not None and pd.notna(share["own_texts"]) else 0
        copied = float(share["copied_share"]) if share is not None and own else None
        date = date_vote(shared, upstream, copied)
        readme_dir, readme_strength, phrase = readme_vote(texts.get(repo))
        kind, confidence, basis = combine_votes(date, (readme_dir, readme_strength))
        rows.append(
            {
                "repo": repo,
                "registry_kind": kind,
                "confidence": confidence,
                "needs_review": confidence < settings.KIND_REVIEW_BELOW,
                "kind_basis": basis,
                "skill_folders": int(folders.get(repo, 0)),
                "shared_texts": shared,
                "elsewhere_first": int(share["elsewhere_first"]) if share is not None else 0,
                "upstream_share": upstream,
                "own_texts": own,
                "copied_share": copied,
                "top_upstream": share["top_upstream"] if share is not None else None,
                "date_vote": date[0],
                "date_strength": date[1],
                "readme_vote": readme_dir,
                "readme_strength": readme_strength,
                "readme_phrase": phrase,
                "hand_label": None,
            }
        )
    return pd.DataFrame(rows, columns=list(KIND_COLUMNS))


def apply_hand_labels(kinds: pd.DataFrame, review: pd.DataFrame | None) -> pd.DataFrame:
    """Use the labels a person wrote in the review sheet.

    A ``your_label`` of ``source``, ``copier`` or ``unclear`` replaces
    ``registry_kind``, sets ``kind_basis`` to ``hand`` and ``needs_review`` to
    false. The computed confidence is kept. Other values are ignored.

    Example:
    >>> kinds = pd.DataFrame({"repo": ["a/x", "b/y"], "registry_kind": ["copier", "source"],
    ...     "kind_basis": ["date", "date"], "needs_review": [True, True],
    ...     "hand_label": [None, None]})
    >>> review = pd.DataFrame({"repo": ["a/x", "b/y"], "your_label": ["Source", "maybe"]})
    >>> apply_hand_labels(kinds, review)[["registry_kind", "kind_basis"]].values.tolist()
    [['source', 'hand'], ['source', 'date']]
    """
    if review is None or review.empty or "your_label" not in review.columns:
        return kinds
    labels = {
        str(row["repo"]): str(row["your_label"]).strip().lower()
        for row in review.to_dict("records")
        if str(row["your_label"]).strip().lower() in _HAND_LABELS
    }
    out = kinds.copy()
    hand = out["repo"].map(labels)
    chosen = hand.notna()
    out.loc[chosen, "registry_kind"] = hand[chosen]
    out.loc[chosen, "hand_label"] = hand[chosen]
    out.loc[chosen, "kind_basis"] = "hand"
    out.loc[chosen, "needs_review"] = False
    return out


def review_rows(kinds: pd.DataFrame, readmes: pd.DataFrame) -> pd.DataFrame:
    """The review sheet: registries that need review, most skill folders first.

    ``readme_start`` is the first ``_README_START_CHARS`` characters of the
    README on one line. ``your_label`` and ``note`` are empty for a person.
    """
    texts = _readme_texts(readmes)
    chosen = kinds.loc[kinds["needs_review"].astype(bool)].copy()
    chosen = chosen.sort_values(
        ["skill_folders", "repo"], ascending=[False, True], kind="mergesort"
    )
    chosen["readme_start"] = [
        re.sub(r"\s+", " ", texts.get(repo) or "")[:_README_START_CHARS] for repo in chosen["repo"]
    ]
    chosen["your_label"] = ""
    chosen["note"] = ""
    return chosen.loc[:, list(REVIEW_COLUMNS)].reset_index(drop=True)


def summarize_kinds(kinds: pd.DataFrame) -> dict[str, object]:
    """Counts by kind and basis, the review count, and folders per kind.

    Example:
    >>> kinds = pd.DataFrame({"registry_kind": ["copier", "source", "source"],
    ...     "kind_basis": ["date+readme", "date", "date"], "needs_review": [False, True, False],
    ...     "skill_folders": [100, 5, 20]})
    >>> summarize_kinds(kinds)["folders_copier"]
    100
    """
    numbers: dict[str, object] = {"registries": len(kinds)}
    for kind in ("source", "copier", "unclear"):
        mask = kinds["registry_kind"] == kind
        numbers[f"kind_{kind}"] = int(mask.sum())
        numbers[f"folders_{kind}"] = int(kinds.loc[mask, "skill_folders"].sum())
    for basis, count in kinds["kind_basis"].value_counts().sort_index().items():
        numbers[f"basis_{basis}"] = int(count)
    numbers["needs_review"] = int(kinds["needs_review"].astype(bool).sum())
    return numbers


def agreement_table(kinds: pd.DataFrame) -> pd.DataFrame:
    """Date vote against README vote, for registries where the README voted."""
    voted = kinds.loc[kinds["readme_vote"].notna()]
    if voted.empty:
        return pd.DataFrame(columns=["date_vote", "readme_vote", "registries"])
    table = voted.groupby([voted["date_vote"].fillna("none"), "readme_vote"]).size()
    return table.rename("registries").reset_index()


def known_case_check(kinds: pd.DataFrame) -> list[str]:
    """One line per ``KNOWN_KINDS`` registry: expected, got, ok or MISMATCH."""
    by_repo = kinds.set_index("repo")
    lines: list[str] = []
    for repo, expected in KNOWN_KINDS.items():
        if repo not in by_repo.index:
            lines.append(f"{repo}: expected {expected}, not in this run")
            continue
        got = by_repo.loc[repo, "registry_kind"]
        mark = "ok" if got == expected else "MISMATCH"
        lines.append(f"{repo}: expected {expected}, got {got} ({mark})")
    return lines


def share_sensitivity(kinds: pd.DataFrame) -> pd.DataFrame:
    """Registries and folders with a share at or above 0.7, 0.8 and 0.9 (20+ shared texts)."""
    enough = kinds.loc[kinds["shared_texts"] >= settings.KIND_FULL_EVIDENCE_TEXTS]
    rows = []
    for cutoff in (0.7, 0.8, 0.9):
        hit = enough.loc[enough["upstream_share"] >= cutoff]
        rows.append(
            {
                "share_at_least": cutoff,
                "registries": len(hit),
                "skill_folders": int(hit["skill_folders"].sum()),
            }
        )
    return pd.DataFrame(rows)


def build_registry_kinds(
    repos: list[str],
    compared: list[str],
    history_dir: Path,
    readmes_file: Path,
    out_path: Path,
    sheet_path: Path,
) -> pd.DataFrame:
    """Steps 2–5 on explicit paths (``main`` and the tests call this).

    Inputs: the registries to label, the registries to compare with, the stage 3
    folder, the stage 3b table (may be missing), and the two output paths.
    Outputs: the kinds frame, also written to ``out_path``; the review sheet,
    written to ``sheet_path`` unless a filled sheet is already there.
    """
    # Step 2: Date vote from the stage 3 history (Algorithm A60)
    shares = upstream_shares(history_dir / "registry_history_changes.parquet", compared)
    status = read_parquet(history_dir / "registry_history_status.parquet")
    folders = dict(
        zip(status["repo"].astype(str), status["skill_folders"].astype(int), strict=True)
    )
    # Step 3: README vote from stage 3b (Algorithm A61)
    readmes = read_parquet(readmes_file) if readmes_file.is_file() else pd.DataFrame()
    if readmes.empty:
        print(f"no README table at {readmes_file}: the README vote is empty", flush=True)
    # Step 4: Combine the votes (Algorithm A62), then the hand labels
    kinds = label_registries(repos, shares, readmes, folders)
    previous = _read_sheet(sheet_path)
    kinds = apply_hand_labels(kinds, previous)
    write_parquet(kinds, out_path, sort_by=["repo"])
    # Step 5: Review sheet, unless a person already filled it
    if _sheet_has_labels(previous):
        print(f"kept {sheet_path}: it has labels; they were applied", flush=True)
    else:
        review_rows(kinds, readmes).to_csv(sheet_path, index=False)
    return kinds


def main() -> None:
    """Read the inputs, vote, combine, write the kinds and the review sheet, print checks."""
    args = build_arg_parser(
        "Label each registry as a source or a copier (no network)."
    ).parse_args()
    dev = bool(args.dev)
    out_path, sheet_path = kinds_path(dev), review_path(dev)
    if outputs_exist([out_path]) and not args.force:
        print(
            f"skip: {out_path} exists (pass --force to rebuild and apply review labels)", flush=True
        )
        return
    started = time.perf_counter()
    # Step 1: Read the registries to label and to compare with
    repos = read_registry_list(dev)
    compared = read_registry_list(False)
    history_dir = settings.data_dir(dev) / "registry_history"
    if not (history_dir / "registry_history_changes.parquet").is_file():
        flag = " --dev" if dev else ""
        raise SystemExit(
            f"missing stage 3 output in {history_dir}; "
            f"run: python -m skill_genealogy.download_registry_history{flag}"
        )
    readmes_file = settings.data_dir(dev) / "registry_readmes.parquet"
    kinds = build_registry_kinds(repos, compared, history_dir, readmes_file, out_path, sheet_path)
    # Step 6: Summary and checks
    numbers = summarize_kinds(kinds)
    numbers["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    numbers["output"] = str(out_path)
    numbers["review_sheet"] = str(sheet_path)
    print_summary("Stage 3c summary", numbers)
    print("Date vote against README vote (registries with a README vote)")
    print(agreement_table(kinds).to_string(index=False))
    print("Known cases")
    for line in known_case_check(kinds):
        print(line)
    print("Copiers by share cutoff (20+ shared texts)")
    print(share_sensitivity(kinds).to_string(index=False))
    print_sample_records(
        kinds.sort_values("skill_folders", ascending=False), "Largest registries", 2
    )


def _readme_texts(readmes: pd.DataFrame) -> dict[str, str]:
    """README text per repo, for rows with status ``ok`` and a text."""
    if readmes.empty or "readme_text" not in readmes.columns:
        return {}
    ok = readmes.loc[(readmes["status"] == "ok") & readmes["readme_text"].notna()]
    return dict(zip(ok["repo"].astype(str), ok["readme_text"].astype(str), strict=True))


def _read_sheet(path: Path) -> pd.DataFrame | None:
    """The review sheet as text, or None when there is none."""
    if not path.is_file():
        return None
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _sheet_has_labels(sheet: pd.DataFrame | None) -> bool:
    """True when a person wrote any ``your_label`` or ``note``."""
    if sheet is None:
        return False
    for column in ("your_label", "note"):
        if column in sheet.columns and bool((sheet[column].astype(str).str.strip() != "").any()):
            return True
    return False


if __name__ == "__main__":
    main()
