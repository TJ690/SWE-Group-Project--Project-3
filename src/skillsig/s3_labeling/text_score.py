"""
STAGE: s3_labeling
MODULE: text_score
PURPOSE: E5b - text-based lean for no-history skills, using the method chosen in E5 (text_methods).
INPUTS: s3_text_methods_oof.parquet, s3_labels_v2_1, s2_* features, artifacts (content)
OUTPUTS: s3_text_scores.parquet, results/s3_text_score_summary.csv, results/s3_text_score.json
RQ LINK: RQ1 (text-only evidence where commit history is missing)

Fits METHOD on every labelled history skill (as in E5), then scores every non-templated skill
with no history. Cutoffs come from the E5 out-of-fold scores: text_lean_agent where held-out
precision for agent reaches PRECISION, text_lean_human likewise for human, text_uncertain
between. Precision assumes a 50/50 prior and noisy negatives (see text_methods), so these are
text leans with a measured error rate, not authorship. label_with_text replaces
insufficient_evidence only for no-history skills; every other v2.1 label is unchanged.
"""

from __future__ import annotations

import json
import logging

import duckdb
import numpy as np
import pandas as pd

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s3_labeling import text_methods as tm
from skillsig.s3_labeling.style_model import STRUCTURE, WORDING, prepare

log = logging.getLogger(__name__)

METHOD = "ensemble"
PRECISION = 0.80
_BATCH = 50_000


def cutoffs(oof: pd.DataFrame, method: str, precision: float) -> tuple[float, float]:
    """(hi, lo) score cutoffs reaching `precision` for agent above hi and human below lo."""
    y = oof["y"].to_numpy()
    score = oof[method].to_numpy()
    w = np.where(y == 1, 0.5 / y.sum(), 0.5 / (y == 0).sum())
    hi = _cut(score, y == 1, w, precision, descending=True)
    lo = _cut(score, y == 0, w, precision, descending=False)
    return hi, lo


def _cut(score, hit, w, precision, *, descending: bool) -> float:
    order = np.argsort(-score if descending else score, kind="stable")
    s, h, ww = score[order], hit[order], w[order]
    ok = np.append(s[1:] != s[:-1], True) & (np.cumsum(ww * h) / np.cumsum(ww) >= precision)
    idx = np.flatnonzero(ok)
    if len(idx) == 0:
        return np.inf if descending else -np.inf
    return float(s[idx.max()])


class Scorer:
    """Fitted members of METHOD; ensemble members map to their out-of-fold percentile."""

    def __init__(self, frame: pd.DataFrame, oof: pd.DataFrame, method: str, seed: int):
        self.method = method
        self.members = tm.ENSEMBLE if method == "ensemble" else (method,)
        self.ref = {m: np.sort(oof[m].to_numpy()) for m in self.members}
        self.predict = {m: tm.METHODS[m](frame, seed) for m in self.members}

    def score(self, batch: pd.DataFrame) -> np.ndarray:
        raw = {m: self.predict[m](batch) for m in self.members}
        if self.method != "ensemble":
            return raw[self.method]
        pct = [
            np.searchsorted(self.ref[m], raw[m], side="right") / len(self.ref[m])
            for m in self.members
        ]
        return np.mean(pct, axis=0)


def pool_query(cfg: Config) -> str:
    """Non-templated skills without history, with features and primary content."""
    derived = resolve(cfg, cfg.paths.derived)
    art = resolve(cfg, cfg.paths.raw) / "artifacts" / "*.parquet"
    cols_s = ", ".join(f"st.{c}" for c in STRUCTURE)
    cols_w = ", ".join(f"w.{c}" for c in WORDING)
    return f"""
        WITH pool AS (
            SELECT l.file_sha, l.label
            FROM read_parquet({_sql(derived / "s3_labels_v2_1.parquet")}) l
            JOIN read_parquet({_sql(derived / "s2_commit.parquet")}) k USING (file_sha)
            WHERE NOT coalesce(k.history_fetched, false) AND NOT l.flag_templated
        ),
        prim AS (
            SELECT a.file_sha, a.repo_full_name, a.content AS text
            FROM read_parquet({_sql(art)}) a
            WHERE coalesce(TRY_CAST(a.dedup_primary AS INTEGER), 0) = 1
              AND a.file_sha IN (SELECT file_sha FROM pool)
            QUALIFY row_number() OVER (PARTITION BY a.file_sha) = 1
        )
        SELECT p.file_sha, p.label, split_part(pr.repo_full_name, '/', 1) AS owner, pr.text,
               NULL::VARCHAR AS first_commit_at, 0 AS s,
               {cols_s}, {cols_w}, sp.specificity, sp.n_identifiers
        FROM pool p
        JOIN prim pr USING (file_sha)
        JOIN read_parquet({_sql(derived / "s2_wording.parquet")}) w USING (file_sha)
        JOIN read_parquet({_sql(derived / "s2_structure.parquet")}) st USING (file_sha)
        LEFT JOIN read_parquet({_sql(derived / "s2_specificity.parquet")}) sp USING (file_sha)
    """


def lean(score: np.ndarray, hi: float, lo: float) -> np.ndarray:
    return np.where(
        score >= hi, "text_lean_agent", np.where(score <= lo, "text_lean_human", "text_uncertain")
    )


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Score every no-history, non-templated skill and write leans and a summary."""
    derived = resolve(cfg, cfg.paths.derived)
    results = resolve(cfg, cfg.paths.results)
    oof_path = derived / "s3_text_methods_oof.parquet"
    inputs = [
        oof_path,
        *(
            derived / n
            for n in (
                "s3_labels_v2_1.parquet",
                "s2_structure.parquet",
                "s2_wording.parquet",
                "s2_commit.parquet",
                "s2_specificity.parquet",
            )
        ),
    ]
    for path in inputs:
        if not path.is_file():
            raise FileNotFoundError(f"missing {path}; run `label --only text_methods` first")
    outputs = [
        derived / "s3_text_scores.parquet",
        results / "s3_text_score_summary.csv",
        results / "s3_text_score.json",
    ]
    if not force and all(inputs_unchanged(p, cfg=cfg, inputs=inputs) for p in outputs):
        rows = {p.name: json.loads(manifest_path(p).read_text())["rows"] for p in outputs}
        return StageResult("s3_labeling", outputs, skipped=True, rows=rows)
    seed = int(cfg.seed)
    oof = pd.read_parquet(oof_path)
    hi, lo = cutoffs(oof, METHOD, PRECISION)
    con = duckdb.connect()
    try:
        frame = tm.labelled_frame(con, cfg)
        frame = pd.concat(
            [frame, pd.DataFrame([tm.rule_scores(t) for t in frame["clean"]])], axis=1
        )
        scorer = Scorer(frame, oof, METHOD, seed)
        del frame
        parts = []
        reader = con.execute(pool_query(cfg)).fetch_record_batch(_BATCH)
        for batch in reader:
            df = prepare(batch.to_pandas())
            df["text"] = df["text"].fillna("")
            df["clean"] = [tm.clean_text(t) for t in df["text"]]
            df = pd.concat([df, pd.DataFrame([tm.rule_scores(t) for t in df["clean"]])], axis=1)
            score = scorer.score(df)
            parts.append(
                pd.DataFrame(
                    {
                        "file_sha": df["file_sha"],
                        "label_v2_1": df["label"],
                        "words": np.expm1(df["body_words"]).round().astype(int),
                        "text_score": score,
                        "text_lean": lean(score, hi, lo),
                        "in_text_disclosure": df["text"].map(tm.has_disclosure),
                    }
                )
            )
            log.info("E5b scored %d rows", sum(len(p) for p in parts))
    finally:
        con.close()
    scores = pd.concat(parts, ignore_index=True)
    # An explicit AI attribution written into the text overrides the style lean.
    scores.loc[scores["in_text_disclosure"], "text_lean"] = "text_disclosed_agent"
    scores["label_with_text"] = np.where(
        scores["label_v2_1"] == "insufficient_evidence", scores["text_lean"], scores["label_v2_1"]
    )
    band = pd.cut(
        scores["words"], [-1, 149, 399, 999, np.inf], labels=["<150", "150-399", "400-999", "1000+"]
    )
    summary = (
        pd.concat(
            [
                scores.assign(group="all", value="all"),
                scores.assign(group="length", value=band.astype(str)),
            ]
        )
        .groupby(["group", "value", "text_lean"])
        .size()
        .rename("n")
        .reset_index()
    )
    summary["pct"] = 100 * summary["n"] / summary.groupby(["group", "value"])["n"].transform("sum")
    meta = {
        "method": METHOD,
        "precision_target": PRECISION,
        "cutoff_agent": hi,
        "cutoff_human": lo,
        "n_scored": len(scores),
        "lean_counts": scores["text_lean"].value_counts().to_dict(),
        "note": "precision measured on history skills at a 50/50 prior with likely_human as "
        "negatives; it is not authorship ground truth",
    }
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
    scores.to_parquet(outputs[0], index=False)
    summary.round(2).to_csv(outputs[1], index=False)
    outputs[2].write_text(json.dumps(meta, indent=2, sort_keys=True, default=float) + "\n")
    counts = [len(scores), len(summary), 1]
    for path, count in zip(outputs, counts, strict=True):
        write_manifest(path, cfg=cfg, inputs=inputs, rows=count)
    return StageResult(
        "s3_labeling", outputs, rows=dict(zip([p.name for p in outputs], counts, strict=True))
    )


def _sql(path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"
