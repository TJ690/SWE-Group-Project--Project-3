"""
STAGE: s4_validation
MODULE: spot_check
PURPOSE: E6 - blind spot-check (sample.per_label per label), overlap, trailer checks, scoring.
INPUTS: s3_labels_v2_1, s3_labels_v2, s3_baseline_labels, s2_commit, s2_clusters, s2_repo, artifacts
OUTPUTS: results/annotation/* (xlsx sheets, keys, codebook); results/s4_spot_*.csv|json
RQ LINK: RQ1 (indicative precision of each label; agreement on the visible signal)

Person A labels sample.per_label skills per v2.1 label (30 -> 120 rows) without seeing any
pipeline output; person B labels a third of them per label (10 -> 40); an optional person C
labels any subset of A's sheet (spot_C_done.xlsx) and is scored separately; anyone marks 50
detected trailer lines as real or not. Each label's quota is split between its two strata
(larger half to the first). Precision per label is indicative (n = 30 is about +/-16 points)
and every output says so. Drawing never overwrites existing sheets: move
results/annotation/spot_A.xlsx aside to redraw.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from skillsig.config import Config
from skillsig.provenance import StageResult
from skillsig.s3_labeling.baselines import BASELINES
from skillsig.s4_validation import _io, _sheets, rater_agreement
from skillsig.s4_validation.stats import cohens_kappa, wilson_interval

log = logging.getLogger(__name__)

TAXONOMY = ("highly_templated", "agent_assisted", "likely_human", "insufficient_evidence")
SHEET_COLUMNS = (
    "sample_id",
    "file_sha",  # content hash, for checking the sheet against the dataset; carries no label
    "repo_full_name",
    "repo_context",
    "skill_name",
    "description",
    "body_excerpt",
    "body_chars",  # full body length; above EXCERPT_CHARS the excerpt is cut
    "commit_count",
    "first_commit_at",
    "last_commit_at",
    "first_commit_message",
    "last_commit_message",
    "your_label",
    "your_confidence",
    "notes",
)
FORBIDDEN = frozenset(
    {
        "label",
        "label_v21",
        "label_v2",
        "confidence",
        "confidence_v21",
        "reasons",
        "reasons_v21",
        "stratum",
        "copies",
        "cluster_id",
        "cluster_size",
        "est_jaccard",
        "batch_size",
        "flag_templated",
        "flag_disclosed_agent",
        "style_score",
        *BASELINES,
    }
)
# stratum -> (v2.1 label, sibling stratum); the first stratum of each label gets the larger half
STRATA: dict[str, tuple[str, str]] = {
    "templated_registry": ("highly_templated", "templated_generic"),
    "templated_generic": ("highly_templated", "templated_registry"),
    "agent_v21_new": ("agent_assisted", "agent_v2"),
    "agent_v2": ("agent_assisted", "agent_v21_new"),
    "human_not_revised": ("likely_human", "human_revised"),
    "human_revised": ("likely_human", "human_not_revised"),
    "insufficient_history": ("insufficient_evidence", "insufficient_no_history"),
    "insufficient_no_history": ("insufficient_evidence", "insufficient_history"),
}
FILL_COLUMNS = ("your_label", "your_confidence", "notes")
_EXAMPLE = {
    "sample_id": "EXAMPLE",
    "your_label": "agent_assisted",
    "your_confidence": "4",
    "notes": 'first commit message ends with "Co-Authored-By: Claude"',
}
N_TRAILER = 50
EXCERPT_CHARS = 32_000  # just under Excel's 32,767-character cell limit
_CANDIDATES = 400


def quotas(per_label: int) -> dict[str, int]:
    """Split each label's per_label quota over its two strata (15 -> 8 + 7, 30 -> 15 + 15)."""
    if per_label < 1:
        raise ValueError(f"per_label must be >= 1, got {per_label}")
    out: dict[str, int] = {}
    for name, (label, _sibling) in STRATA.items():
        first = not any(STRATA[other][0] == label for other in out)
        out[name] = (per_label + 1) // 2 if first else per_label // 2
    return out


def b_per_label(per_label: int) -> int:
    """Rows per label that person B also labels: a third of the A quota (15 -> 5, 30 -> 10)."""
    return max(1, per_label // 3)


_FRONT = re.compile(r"^\s*---.*?\n---\s*\n?", re.DOTALL)


def codebook_text(n_a: int, n_b: int) -> str:
    return "\n".join(
        [
            "# Spot-check codebook",
            "",
            "Label the visible evidence, not the true author. Nobody can know who wrote a file.",
            "",
            "Full rules, decision order and edge cases: docs/ANNOTATION_GUIDELINES.md.",
            "Read it before you start; where it differs from this summary, follow it.",
            "",
            "- **highly_templated**: the text is a near-copy of a skill you recognise or that "
            "clearly comes from a published collection or scaffold, with little "
            "project-specific content.",
            "- **agent_assisted**: there is visible evidence of tool involvement: an AI "
            'co-author or "Generated with" line, an agent bot account, or many skills added in '
            "one commit with uniform structure.",
            "- **likely_human**: the text is specific to its project and the history shows "
            "ordinary human work (revisions over days, a user account), with no AI marker.",
            "- **insufficient_evidence**: the evidence is missing or points both ways. Use this "
            "freely; it is a valid answer.",
            "",
            "Confidence (1-5): 1 = a guess; 2 = leaning; 3 = more likely than not; "
            "4 = fairly sure; 5 = you would bet on it.",
            "",
            "Do not search GitHub for the repository or its author. Judge from the sheet only.",
            f"Person A fills spot_A.xlsx ({n_a} rows); person B fills spot_B.xlsx ({n_b} rows)",
            "without talking about the rows. Use the dropdowns in the last columns. Save as",
            "spot_A_done.xlsx and spot_B_done.xlsx.",
            "Trailer check: for each message, is the AI line a real disclosure that an AI tool",
            "took part (yes), not one (no), or unclear (unsure)? Save as trailer_check_done.csv.",
            "",
        ]
    )


def candidates(con: duckdb.DuckDBPyConnection, cfg: Config) -> pd.DataFrame:
    """Up to _CANDIDATES seeded candidates per stratum: file_sha, stratum, repo, cluster."""
    seed = int(cfg.seed)
    threshold = float(cfg.minhash.lsh_threshold)
    min_body = int(cfg.thresholds.min_body_chars)
    d = lambda name: _io.sql(_io.derived(cfg, name))  # noqa: E731
    query = f"""
        WITH prim AS (
            SELECT file_sha, any_value(repo_full_name) AS repo_full_name,
                   any_value(body_chars) AS body_chars
            FROM read_parquet({_io.sql(_io.artifacts_glob(cfg))})
            WHERE coalesce(TRY_CAST(dedup_primary AS INTEGER), 0) = 1
            GROUP BY file_sha
        ),
        cl AS (
            SELECT file_sha, any_value(cluster_id) AS cluster_id
            FROM read_parquet({d("s2_clusters.parquet")})
            WHERE abs(threshold - {threshold!r}) < 1e-9 GROUP BY file_sha
        ),
        base AS (
            SELECT l.file_sha, p.repo_full_name, cl.cluster_id,
                CASE
                  WHEN l.label = 'highly_templated' AND l.templated_basis IN
                       ('registry_exact', 'registry_near') THEN 'templated_registry'
                  WHEN l.label = 'highly_templated' THEN 'templated_generic'
                  WHEN l.label = 'agent_assisted' AND l.reasons[1] IN
                       ('trailer_in_bulk', 'trailer_last_only') THEN 'agent_v21_new'
                  WHEN l.label = 'agent_assisted' THEN 'agent_v2'
                  WHEN l.label = 'likely_human' AND list_contains(l.reasons, 'not_revised')
                       THEN 'human_not_revised'
                  WHEN l.label = 'likely_human' THEN 'human_revised'
                  WHEN k.history_fetched THEN 'insufficient_history'
                  ELSE 'insufficient_no_history'
                END AS stratum
            FROM read_parquet({d("s3_labels_v2_1.parquet")}) l
            JOIN prim p USING (file_sha)
            LEFT JOIN read_parquet({d("s2_commit.parquet")}) k USING (file_sha)
            LEFT JOIN cl USING (file_sha)
            WHERE p.body_chars >= {min_body}
        )
        SELECT file_sha, stratum, repo_full_name, cluster_id FROM (
            SELECT *, row_number() OVER (
                PARTITION BY stratum ORDER BY md5(file_sha || '{seed}')) AS rn
            FROM base
        ) WHERE rn <= {_CANDIDATES}
        ORDER BY stratum, rn
    """
    return con.execute(query).fetchdf()


def draw(pool: pd.DataFrame, cfg: Config) -> tuple[pd.DataFrame, dict[str, dict[str, int]]]:
    """Greedy seeded draw with quotas, sibling top-up, and 1-per-repo / 1-per-cluster caps."""
    repos: set[str] = set()
    clusters: set[float] = set()
    taken: dict[str, list[str]] = {name: [] for name in STRATA}
    by_stratum = {name: pool[pool["stratum"] == name] for name in STRATA}

    def take(name: str, k: int) -> int:
        got = 0
        for row in by_stratum[name].itertuples(index=False):
            if got >= k:
                break
            if row.file_sha in taken[name] or row.repo_full_name in repos:
                continue
            if row.cluster_id is not None and not pd.isna(row.cluster_id):
                if row.cluster_id in clusters:
                    continue
                clusters.add(row.cluster_id)
            repos.add(row.repo_full_name)
            taken[name].append(row.file_sha)
            got += 1
        return got

    design: dict[str, dict[str, int]] = {}
    quota = quotas(int(cfg.sample.per_label))
    for name in STRATA:
        design[name] = {
            "quota": quota[name],
            "drawn": take(name, quota[name]),
            "candidates": len(by_stratum[name]),
        }
    for name, (_, sibling) in STRATA.items():
        short = quota[name] - design[name]["drawn"]
        if short > 0:
            extra = take(sibling, short)
            design[sibling]["topped_up"] = design[sibling].get("topped_up", 0) + extra
    rows = [
        {"file_sha": sha, "stratum": name, "label_v21": STRATA[name][0]}
        for name, shas in taken.items()
        for sha in shas
    ]
    sample = pd.DataFrame(rows)
    rng = np.random.default_rng(int(cfg.seed))
    sample = sample.iloc[rng.permutation(len(sample))].reset_index(drop=True)
    width = max(2, len(str(len(sample))))
    sample.insert(0, "sample_id", [f"S{i + 1:0{width}d}" for i in range(len(sample))])
    return sample, design


def details(con: duckdb.DuckDBPyConnection, cfg: Config, shas: Sequence[str]) -> pd.DataFrame:
    """Everything the sheets and the key need for the drawn skills."""
    listed = ", ".join("'" + s.replace("'", "''") + "'" for s in shas) or "''"
    d = lambda name: _io.sql(_io.derived(cfg, name))  # noqa: E731
    query = f"""
        WITH prim AS (
            SELECT file_sha, any_value(repo_full_name) AS repo_full_name,
                   any_value(name) AS skill_name, any_value(description) AS description,
                   any_value(content) AS content, any_value(commit_count) AS commit_count,
                   any_value(first_commit_at) AS first_commit_at,
                   any_value(last_commit_at) AS last_commit_at,
                   any_value(first_commit_message) AS first_commit_message,
                   any_value(last_commit_message) AS last_commit_message
            FROM read_parquet({_io.sql(_io.artifacts_glob(cfg))})
            WHERE coalesce(TRY_CAST(dedup_primary AS INTEGER), 0) = 1 AND file_sha IN ({listed})
            GROUP BY file_sha
        )
        SELECT p.*, r.stars, r.language, r.created_at AS repo_created_at, r.files AS repo_files,
               l.label AS label_v21, l.confidence AS confidence_v21, l.reasons AS reasons_v21,
               v2.label AS label_v2, v2.reasons AS reasons_v2,
               k.ai_tool_first_ext, k.ai_tool_last_ext, b.* EXCLUDE (file_sha)
        FROM prim p
        LEFT JOIN read_parquet({d("s2_repo.parquet")}) r USING (repo_full_name)
        LEFT JOIN read_parquet({d("s3_labels_v2_1.parquet")}) l USING (file_sha)
        LEFT JOIN read_parquet({d("s3_labels_v2.parquet")}) v2 USING (file_sha)
        LEFT JOIN read_parquet({d("s2_commit.parquet")}) k USING (file_sha)
        LEFT JOIN read_parquet({d("s3_baseline_labels.parquet")}) b USING (file_sha)
    """
    return con.execute(query).fetchdf()


def build_sheets(sample: pd.DataFrame, info: pd.DataFrame, cfg: Config) -> dict[str, pd.DataFrame]:
    """Sheets A (all) and B (a third per label, other order), plus the answer key."""
    merged = sample.merge(info.drop(columns=["label_v21"]), on="file_sha", how="left")
    sheet = pd.DataFrame(
        {
            "sample_id": merged["sample_id"],
            "file_sha": merged["file_sha"],
            "repo_full_name": merged["repo_full_name"],
            "repo_context": [_context(r) for r in merged.itertuples(index=False)],
            "skill_name": merged["skill_name"],
            "description": merged["description"],
            "body_excerpt": [_excerpt(t) for t in merged["content"]],
            "body_chars": [len(_body(t)) for t in merged["content"]],
            "commit_count": merged["commit_count"],
            "first_commit_at": merged["first_commit_at"],
            "last_commit_at": merged["last_commit_at"],
            "first_commit_message": merged["first_commit_message"],
            "last_commit_message": merged["last_commit_message"],
            "your_label": "",
            "your_confidence": "",
            "notes": "",
        },
        columns=list(SHEET_COLUMNS),
    )
    rng = np.random.default_rng(int(cfg.seed) + 1)
    picks = []
    per_b = b_per_label(int(cfg.sample.per_label))
    for label in TAXONOMY:
        ids = merged.loc[merged["label_v21"] == label, "sample_id"].tolist()
        picks += list(rng.choice(ids, size=min(per_b, len(ids)), replace=False))
    sheet_b = sheet[sheet["sample_id"].isin(picks)]
    sheet_b = sheet_b.iloc[rng.permutation(len(sheet_b))].reset_index(drop=True)
    key_cols = [
        "sample_id",
        "file_sha",
        "stratum",
        "label_v21",
        "confidence_v21",
        "reasons_v21",
        "label_v2",
        "reasons_v2",
        *[b for b in BASELINES if b in merged.columns],
    ]
    key = merged[key_cols].copy()
    key["in_b"] = key["sample_id"].isin(picks)
    return {"A": sheet, "B": sheet_b, "key": key}


def trailer_rows(con: duckdb.DuckDBPyConnection, cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    """50 seeded messages that the extended trailer match flagged: sheet and key."""
    seed = int(cfg.seed)
    commit = _io.sql(_io.derived(cfg, "s2_commit.parquet"))
    query = f"""
        SELECT k.file_sha, k.ai_trailer_first_ext AS first_hit, k.ai_tool_first_ext,
               k.ai_tool_last_ext, a.first_commit_message, a.last_commit_message
        FROM read_parquet({_io.sql(_io.derived(cfg, "s2_commit.parquet"))}) k
        JOIN (
            SELECT file_sha, any_value(first_commit_message) AS first_commit_message,
                   any_value(last_commit_message) AS last_commit_message
            FROM read_parquet({_io.sql(_io.artifacts_glob(cfg))})
            WHERE coalesce(TRY_CAST(dedup_primary AS INTEGER), 0) = 1
              AND file_sha IN (
                  SELECT file_sha FROM read_parquet({commit})
                  WHERE ai_trailer_first_ext OR ai_trailer_last_ext
              )
            GROUP BY file_sha
        ) a USING (file_sha)
        WHERE k.ai_trailer_first_ext OR k.ai_trailer_last_ext
        ORDER BY md5(k.file_sha || '{seed}-trailer') LIMIT {N_TRAILER}
    """
    rows = con.execute(query).fetchdf()
    first = rows["first_hit"].fillna(False).astype(bool)
    ids = [f"T{i + 1:02d}" for i in range(len(rows))]
    sheet = pd.DataFrame(
        {
            "check_id": ids,
            "commit_message": np.where(
                first, rows["first_commit_message"], rows["last_commit_message"]
            ),
            "is_ai_disclosure": "",
        }
    )
    key = pd.DataFrame(
        {
            "check_id": ids,
            "file_sha": rows["file_sha"],
            "which": np.where(first, "first", "last"),
            "tool_ext": np.where(first, rows["ai_tool_first_ext"], rows["ai_tool_last_ext"]),
        }
    )
    return sheet, key


def score(
    key: pd.DataFrame,
    a: pd.DataFrame,
    b: pd.DataFrame | None = None,
    trailer: pd.DataFrame | None = None,
) -> dict[str, object]:
    """Precision per version/label and per reason, confusion, kappa on the overlap, trailers."""
    largest = int(key.groupby("label_v21").size().max()) if len(key) else 0
    note = f"indicative, n<={largest} per label"
    human = _labels(a).rename(columns={"your_label": "human_label"})
    frame = key.merge(human, on="sample_id", how="inner")
    versions = ["label_v21", "label_v2", *[c for c in BASELINES if c in frame.columns]]
    precision_rows, confusion_rows, reason_rows = [], [], []
    for version in versions:
        for label in TAXONOMY:
            predicted = frame[frame[version] == label]
            agree = int((predicted["human_label"] == label).sum())
            low, high = wilson_interval(agree, len(predicted))
            precision_rows.append(
                {
                    "version": version,
                    "label": label,
                    "n_predicted": len(predicted),
                    "agree": agree,
                    "precision": agree / len(predicted) if len(predicted) else np.nan,
                    "ci_low": low,
                    "ci_high": high,
                    "note": note,
                }
            )
        counts = frame.groupby([version, "human_label"]).size().reset_index(name="n")
        for row in counts.itertuples(index=False):
            confusion_rows.append(
                {"version": version, "pipeline_label": row[0], "human_label": row[1], "n": row[2]}
            )
    for version, reason_col in (("label_v21", "reasons_v21"), ("label_v2", "reasons_v2")):
        exploded = frame[[version, reason_col, "human_label"]].explode(reason_col)
        exploded = exploded.dropna(subset=[reason_col])
        for reason, part in exploded.groupby(reason_col):
            agree = int((part[version] == part["human_label"]).sum())
            low, high = wilson_interval(agree, len(part))
            reason_rows.append(
                {
                    "version": version,
                    "reason": reason,
                    "n": len(part),
                    "agree": agree,
                    "precision": agree / len(part),
                    "ci_low": low,
                    "ci_high": high,
                    "note": note,
                }
            )
    agreement: dict[str, object] = {
        "n_scored": len(frame),
        "partial": len(frame) < len(key),
        "note": "agreement on the visible signal, not on true authorship",
    }
    if b is not None:
        both = _labels(a).merge(_labels(b), on="sample_id", suffixes=("_a", "_b"))
        agreement.update(
            {
                "kappa": cohens_kappa(both["your_label_a"].tolist(), both["your_label_b"].tolist()),
                "n_overlap": len(both),
                "raw_agreement": float((both["your_label_a"] == both["your_label_b"]).mean())
                if len(both)
                else np.nan,
            }
        )
    trailers: dict[str, object] = {}
    if trailer is not None:
        answers = trailer["is_ai_disclosure"].fillna("").str.strip().str.lower()
        yes, no = int((answers == "yes").sum()), int((answers == "no").sum())
        low, high = wilson_interval(yes, yes + no)
        trailers = {
            "yes": yes,
            "no": no,
            "unsure": int((answers == "unsure").sum()),
            "precision": yes / (yes + no) if yes + no else np.nan,
            "ci_low": low,
            "ci_high": high,
        }
    return {
        "precision": pd.DataFrame(precision_rows),
        "reasons": pd.DataFrame(reason_rows),
        "confusion": pd.DataFrame(confusion_rows),
        "agreement": agreement,
        "trailers": trailers,
    }


def rebuild(old_key: pd.DataFrame, info: pd.DataFrame, cfg: Config) -> dict[str, pd.DataFrame]:
    """Sheets and key for an already drawn sample: same sample_ids, B rows and strata.

    The sheets are rebuilt from the current artifacts (text, commits); the key takes the current
    labels, and label_v21_at_draw keeps the label the sample was stratified on.
    """
    sample = old_key[["sample_id", "file_sha", "stratum", "label_v21"]].reset_index(drop=True)
    sheets = build_sheets(sample, info, cfg)
    b_ids = set(old_key.loc[old_key["in_b"].astype(bool), "sample_id"])
    if set(sheets["B"]["sample_id"]) != b_ids:
        sheets["B"] = sheets["A"][sheets["A"]["sample_id"].isin(b_ids)].reset_index(drop=True)
    key = sheets["key"]
    current = info.set_index("file_sha")["label_v21"]
    key["label_v21_at_draw"] = key["label_v21"]
    key["label_v21"] = key["file_sha"].map(current)
    key["in_b"] = key["sample_id"].isin(b_ids)
    return {"A": sheets["A"], "B": sheets["B"], "key": key}


def run_refresh(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Keep the drawn sample; refresh its key (current labels) and blank sheets (current text).

    Sheets that already hold any answer are left untouched; only the key is refreshed then.
    """
    key_path = _io.annotation(cfg, "key.parquet")
    if not key_path.exists():
        raise FileNotFoundError(f"missing {key_path}; draw first: python -m skillsig sample")
    old_key = pd.read_parquet(key_path)
    inputs = _inputs(cfg)
    _io.require([(p, "python -m skillsig label --only rules_v2_1") for p in inputs[:3]])
    con = duckdb.connect()
    try:
        info = details(con, cfg, old_key["file_sha"].tolist())
    finally:
        con.close()
    sheets = rebuild(old_key, info, cfg)
    _check_blind(sheets["A"])
    outputs = [key_path]
    sheets["key"].to_parquet(key_path, index=False)
    paths = [_io.annotation(cfg, "spot_A.xlsx"), _io.annotation(cfg, "spot_B.xlsx")]
    answered = [p for p in paths if p.exists() and _has_answers(p)]
    if answered:
        log.warning("E6 refresh: %s already has answers; refreshed the key only", answered[0])
    else:
        codebook = codebook_text(len(sheets["A"]), len(sheets["B"]))
        for sheet, path in ((sheets["A"], paths[0]), (sheets["B"], paths[1])):
            _sheets.write_annotation_xlsx(
                sheet,
                path,
                fill_columns=FILL_COLUMNS,
                labels=TAXONOMY,
                instructions=codebook,
                example=_EXAMPLE,
            )
        _io.annotation(cfg, "codebook.md").write_text(codebook, encoding="utf-8")
        outputs += paths
    moved = int((sheets["key"]["label_v21"] != sheets["key"]["label_v21_at_draw"]).sum())
    log.info("E6 refresh: %s rows, %s with a new v2.1 label since the draw", len(old_key), moved)
    return StageResult("s4_validation", outputs, rows={"spot_A": len(sheets["A"])})


def _has_answers(path: Path) -> bool:
    sheet = _sheets.read_annotation(path)
    columns = [c for c in FILL_COLUMNS if c in sheet.columns]
    return bool(sheet[columns].apply(lambda s: s.str.strip().ne("")).to_numpy().any())


def run_draw(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Draw the sample and write sheets, keys and codebook (never overwrites existing sheets)."""
    sheet_a = _io.annotation(cfg, "spot_A.xlsx")
    outputs = [
        sheet_a,
        _io.annotation(cfg, "spot_B.xlsx"),
        _io.annotation(cfg, "trailer_check.csv"),
        _io.annotation(cfg, "key.parquet"),
        _io.annotation(cfg, "trailer_key.parquet"),
        _io.annotation(cfg, "codebook.md"),
        _io.results(cfg, "s4_spot_design.json"),
    ]
    existing = [p for p in (sheet_a, _io.annotation(cfg, "spot_A.csv")) if p.exists()]
    if existing:
        log.warning("spot-check sheets already exist (%s); move it aside to redraw", existing[0])
        return StageResult("s4_validation", outputs, skipped=True)
    inputs = _inputs(cfg)
    _io.require([(p, "python -m skillsig label --only rules_v2_1") for p in inputs[:3]])
    con = duckdb.connect()
    try:
        pool = candidates(con, cfg)
        sample, design = draw(pool, cfg)
        info = details(con, cfg, sample["file_sha"].tolist())
        sheets = build_sheets(sample, info, cfg)
        trailer_sheet, trailer_key = trailer_rows(con, cfg)
    finally:
        con.close()
    _check_blind(sheets["A"])
    sheet_a.parent.mkdir(parents=True, exist_ok=True)
    codebook = codebook_text(len(sheets["A"]), len(sheets["B"]))
    for sheet, path in ((sheets["A"], outputs[0]), (sheets["B"], outputs[1])):
        _sheets.write_annotation_xlsx(
            sheet,
            path,
            fill_columns=FILL_COLUMNS,
            labels=TAXONOMY,
            instructions=codebook,
            example=_EXAMPLE,
        )
    trailer_sheet.to_csv(outputs[2], index=False)
    sheets["key"].to_parquet(outputs[3], index=False)
    trailer_key.to_parquet(outputs[4], index=False)
    outputs[5].write_text(codebook, encoding="utf-8")
    _io.write_json(
        {
            "seed": int(cfg.seed),
            "strata": design,
            "n_a": len(sheets["A"]),
            "n_b": len(sheets["B"]),
            "n_trailer": len(trailer_sheet),
        },
        outputs[6],
        cfg,
        inputs,
    )
    log.info(
        "E6: wrote %s (A=%s, B=%s, trailers=%s)",
        sheet_a.parent,
        len(sheets["A"]),
        len(sheets["B"]),
        len(trailer_sheet),
    )
    return StageResult("s4_validation", outputs, rows={"spot_A": len(sheets["A"])})


def run_score(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Score the returned sheets. Raises FileNotFoundError when spot_A_done.xlsx is missing."""
    done_a = done_path(cfg, "A")
    if done_a is None:
        missing = _io.annotation(cfg, "spot_A_done.xlsx")
        raise FileNotFoundError(f"missing {missing}; annotate results/annotation/spot_A.xlsx first")
    key = pd.read_parquet(_io.annotation(cfg, "key.parquet"))
    if key["sample_id"].duplicated().any():
        raise ValueError("spot-check key has duplicate sample_ids")
    a = rater_agreement.validate(_sheets.read_annotation(done_a), set(key["sample_id"]), "A")
    b_path = done_path(cfg, "B")
    t_path = _io.annotation(cfg, "trailer_check_done.csv")
    b = None
    if b_path is not None:
        b_ids = (
            set(key.loc[key["in_b"].astype(bool), "sample_id"])
            if "in_b" in key
            else set(key["sample_id"])
        )
        b = rater_agreement.validate(_sheets.read_annotation(b_path), b_ids, "B")
    # Person C is an optional third independent rater of any subset of A's sheet.
    c_path = done_path(cfg, "C")
    c = None
    if c_path is not None:
        c = rater_agreement.validate(_sheets.read_annotation(c_path), set(key["sample_id"]), "C")
    trailer = pd.read_csv(t_path, dtype=str) if t_path.exists() else None
    result = score(key, a, b, trailer)
    if b is not None:
        summary, rater_confusion, pairs = rater_agreement.compare(a, b)
        result["agreement"].update(summary)
    candidates_in = (done_a, b_path, c_path, t_path, _io.annotation(cfg, "key.parquet"))
    inputs = [p for p in candidates_in if p is not None and p.exists()]
    outputs = [
        _io.results(cfg, "s4_spot_precision.csv"),
        _io.results(cfg, "s4_spot_reason_precision.csv"),
        _io.results(cfg, "s4_spot_confusion.csv"),
        _io.results(cfg, "s4_spot_agreement.json"),
    ]
    _io.write_csv(result["precision"], outputs[0], cfg, inputs)
    _io.write_csv(result["reasons"], outputs[1], cfg, inputs)
    _io.write_csv(result["confusion"], outputs[2], cfg, inputs)
    _io.write_json(result["agreement"], outputs[3], cfg, inputs)
    if b is not None:
        overlap_ids = set(pairs["sample_id"])
        overlap_precision = []
        for name, sheet in (("A", a), ("B", b)):
            scored = score(
                key[key["sample_id"].isin(overlap_ids)],
                sheet[sheet["sample_id"].isin(overlap_ids)],
            )
            precision = scored["precision"].copy()
            precision.insert(0, "rater", name)
            precision["evaluation_scope"] = "same independently labelled overlap"
            overlap_precision.append(precision)
        for name, frame in (
            ("s4_spot_rater_confusion.csv", rater_confusion),
            ("s4_spot_overlap_precision.csv", pd.concat(overlap_precision, ignore_index=True)),
        ):
            path = _io.results(cfg, name)
            _io.write_csv(frame, path, cfg, inputs)
            outputs.append(path)
        # Notes and item IDs stay private. This is a review aid, never an adjudicated reference.
        path = _io.annotation(cfg, "rater_disagreements.csv")
        review = pairs.loc[~pairs["agree"]].merge(
            key[["sample_id", "file_sha", "label_v21", "label_v2"]],
            on="sample_id",
            validate="one_to_one",
        )
        _io.write_csv(review, path, cfg, inputs)
        outputs.append(path)
    if c is not None:
        outputs.extend(_score_rater_c(cfg, key, {"A": a, "B": b, "C": c}, inputs))
    if result["trailers"]:
        outputs.append(_io.results(cfg, "s4_trailer_precision.json"))
        _io.write_json(result["trailers"], outputs[-1], cfg, inputs)
    return StageResult(
        "s4_validation", outputs, rows={"s4_spot_precision": len(result["precision"])}
    )


def _score_rater_c(
    cfg: Config,
    key: pd.DataFrame,
    raters: dict[str, pd.DataFrame | None],
    inputs: list[Path],
) -> list[Path]:
    """Third-rater outputs; A's and B's outputs and original answers are left untouched."""
    present = {name: sheet for name, sheet in raters.items() if sheet is not None}
    summaries, confusion, review = rater_agreement.pairwise(present)
    precision = score(key, present["C"])["precision"]
    precision.insert(0, "rater", "C")
    precision["evaluation_scope"] = "items labelled by C"
    review = review.merge(
        key[["sample_id", "file_sha", "label_v21", "label_v2"]], on="sample_id", how="left"
    )
    outputs = []
    for path, frame in (
        (_io.results(cfg, "s4_spot_rater_pairs.csv"), summaries),
        (_io.results(cfg, "s4_spot_rater_pair_confusion.csv"), confusion),
        (_io.results(cfg, "s4_spot_rater_c_precision.csv"), precision),
        # Private review aid like rater_disagreements.csv, covering every pair with C.
        (_io.annotation(cfg, "rater_c_disagreements.csv"), review[review["rater_y"] == "C"]),
    ):
        _io.write_csv(frame, path, cfg, inputs)
        outputs.append(path)
    return outputs


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Draw when spot_A_done.xlsx (or .csv) is missing; score when it exists."""
    if done_path(cfg, "A") is not None:
        return run_score(cfg, dev=dev, force=force)
    return run_draw(cfg, dev=dev, force=force)


def done_path(cfg: Config, person: str) -> Path | None:
    """The returned sheet for person A, B or C: spot_X_done.xlsx, else .csv, else None."""
    for suffix in (".xlsx", ".csv"):
        path = _io.annotation(cfg, f"spot_{person}_done{suffix}")
        if path.exists():
            return path
    return None


def _inputs(cfg: Config) -> list[Path]:
    return [
        _io.derived(cfg, "s3_labels_v2_1.parquet"),
        _io.derived(cfg, "s3_labels_v2.parquet"),
        _io.derived(cfg, "s3_baseline_labels.parquet"),
        _io.derived(cfg, "s2_commit.parquet"),
        _io.derived(cfg, "s2_clusters.parquet"),
        _io.derived(cfg, "s2_repo.parquet"),
    ]


def _labels(sheet: pd.DataFrame) -> pd.DataFrame:
    out = sheet[["sample_id", "your_label"]].copy()
    out["your_label"] = out["your_label"].fillna("").str.strip().str.lower()
    return out[out["your_label"].isin(TAXONOMY)]


def _body(text: object) -> str:
    return _FRONT.sub("", text, count=1) if isinstance(text, str) else ""


def _excerpt(text: object) -> str:
    return _body(text)[:EXCERPT_CHARS]


def _context(row) -> str:
    stars = "unknown" if pd.isna(row.stars) else int(row.stars)
    language = row.language if isinstance(row.language, str) and row.language else "no language"
    created = str(row.repo_created_at)[:10] if isinstance(row.repo_created_at, str) else "unknown"
    files = "unknown" if pd.isna(row.repo_files) else int(row.repo_files)
    return f"{stars} stars; {language}; created {created}; {files} SKILL.md files in the repo"


def _check_blind(sheet: pd.DataFrame) -> None:
    leaked = FORBIDDEN & set(sheet.columns)
    if leaked:
        raise ValueError(f"annotation sheet leaks pipeline columns: {sorted(leaked)}")
