"""
STAGE: s3_labeling
MODULE: text_methods
PURPOSE: E5 - compare text-only methods against disclosure/likely-human proxy labels.
INPUTS: s3_labels_v2_1, s2_structure, s2_wording, s2_commit, s2_clusters, s2_specificity, artifacts
OUTPUTS: s3_text_methods_oof.parquet, results/s3_text_methods.csv|json,
         results/s3_text_methods_top_words.csv
RQ LINK: RQ1 (proxy discrimination and high-precision coverage; authorship transfer unvalidated)

Unlike E4 (PU), this is two-class. Positives: non-templated skills whose commits disclosed an AI
tool or agent bot. Negatives: v2.1 likely_human (user author, single copy, no AI trailer). The
negatives are noisy: absence of a trailer is not verified human authorship. Disclosure selection
and label noise can bias apparent AUC in either direction; no lower bound is established.
Commit, repo and date columns never enter a model; they only define the
evaluation subsets. Vendor words (claude, cursor, ...) are removed from the text so a model cannot
learn the tool name instead of the writing.

Evaluation subsets:
- all: every sampled row.
- matched: positives that were not bulk installs, so both classes are single-skill commits.
- strong_human: negatives revised in >= human_min_commits commits over >= human_min_span_days.
Length-controlled AUC is the mean AUC within body-length quartiles; it shows the signal that is
left once length is held fixed. "decided" is the share of rows a method can label at the target
precision on each side (classes reweighted to 50/50), i.e. what could leave insufficient_evidence.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence

import duckdb
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s2_features.lexicons import VENDOR_WORDS
from skillsig.s3_labeling.baselines import group_split
from skillsig.s3_labeling.style_model import STRUCTURE, TEXT_FEATURES, WORDING, prepare

log = logging.getLogger(__name__)
INTERPRETATION_NOTE = (
    "Positives are disclosure-based and negatives are historical likely_human proxies, "
    "not verified authorship. Disclosure selection and label noise can bias AUC in either "
    "direction; no guaranteed lower bound is established. decided_* assume a 50/50 prior "
    "and precision against those proxy labels, not the no-history population."
)

N_PER_CLASS = 20_000
TEXT_CHARS = 6_000
TARGET_PRECISION = (0.80, 0.90)
_N_SPLITS = 5
FEATURES = tuple(f for f in TEXT_FEATURES if f != "marker_rate")

# Phrases often said to mark LLM-written prose, and habits more common in human writing.
# Fixed in advance (not fitted), so the rule score needs no training data.
AI_PHRASES = (
    "comprehensive",
    "robust",
    "seamless",
    "seamlessly",
    "leverage",
    "leverages",
    "ensure",
    "ensures",
    "crucial",
    "essential",
    "streamline",
    "delve",
    "utilize",
    "facilitate",
    "best practices",
    "key principles",
    "core principles",
    "overview",
    "workflow",
    "systematic",
    "systematically",
    "actionable",
    "critical",
    "proactively",
    "holistic",
)
_EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")
_CHECK = re.compile("[✅❌✔✖]")
_BOLD_LEAD = re.compile(r"^\s*[-*\d.]+\s+\*\*[^*\n]{1,60}\*\*\s*[:—-]", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*]|\d+\.)\s+", re.MULTILINE)
_CONTRACTION = re.compile(r"\b\w+'(?:t|s|re|ve|ll|d|m)\b", re.IGNORECASE)
_FIRST_PERSON = re.compile(r"\b(?:I|I'm|I've|my|we|our)\b")
_LOWER_START = re.compile(r"(?:^|[.!?]\s+)[a-z]", re.MULTILINE)
_WORDS = re.compile(r"[A-Za-z']+")
_VENDOR = re.compile(r"\b(?:" + "|".join(sorted(VENDOR_WORDS)) + r")\b", re.IGNORECASE)
_FRONT = re.compile(r"^\s*---.*?\n---\s*\n?", re.DOTALL)
_DISCLOSURE = re.compile(
    r"generated (?:with|by) \[?(?:claude|cursor|copilot|codex|chatgpt|gemini)"
    r"|co-authored-by:\s*(?:claude|cursor|copilot|codex|gemini)"
    r"|\U0001f916 generated|written (?:with|by) (?:claude|chatgpt|an ai|ai)",
    re.IGNORECASE,
)


def labelled_frame(con: duckdb.DuckDBPyConnection, cfg: Config) -> pd.DataFrame:
    """Up to N_PER_CLASS rows per class: y, owner, cluster, text, subset columns, features."""
    derived = resolve(cfg, cfg.paths.derived)
    art = resolve(cfg, cfg.paths.raw) / "artifacts" / "*.parquet"
    cols_s = ", ".join(f"st.{c}" for c in STRUCTURE)
    cols_w = ", ".join(f"w.{c}" for c in WORDING)
    threshold = float(cfg.minhash.lsh_threshold)
    seed = int(cfg.seed)
    query = f"""
        WITH lab AS (
            SELECT l.file_sha,
                   CASE WHEN l.label = 'agent_assisted' AND l.flag_disclosed_agent THEN 1
                        WHEN l.label = 'likely_human' AND NOT l.flag_disclosed_agent THEN 0
                   END AS y
            FROM read_parquet({_sql(derived / "s3_labels_v2_1.parquet")}) l
            JOIN read_parquet({_sql(derived / "s2_wording.parquet")}) w USING (file_sha)
            WHERE NOT l.flag_templated
        ),
        base AS (
            SELECT file_sha, y FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY y ORDER BY hash(file_sha || '{seed}')) AS rk
                FROM lab WHERE y IS NOT NULL
            ) WHERE rk <= {N_PER_CLASS}
        ),
        prim AS (
            SELECT a.file_sha, any_value(a.repo_full_name) AS repo_full_name,
                   any_value(a.content) AS text
            FROM read_parquet({_sql(art)}) a
            WHERE coalesce(TRY_CAST(a.dedup_primary AS INTEGER), 0) = 1
              AND a.file_sha IN (SELECT file_sha FROM base)
            GROUP BY a.file_sha
        ),
        cl AS (
            SELECT file_sha, any_value(cluster_id) AS cluster_id
            FROM read_parquet({_sql(derived / "s2_clusters.parquet")})
            WHERE abs(threshold - {threshold!r}) < 1e-9
              AND file_sha IN (SELECT file_sha FROM base)
            GROUP BY file_sha
        )
        SELECT b.file_sha, b.y, b.y AS s, split_part(p.repo_full_name, '/', 1) AS owner,
               cl.cluster_id, p.text, k.first_commit_at, k.is_bulk, k.commit_count, k.span_days,
               {cols_s}, {cols_w}, sp.specificity, sp.n_identifiers
        FROM base b
        JOIN read_parquet({_sql(derived / "s2_commit.parquet")}) k USING (file_sha)
        JOIN read_parquet({_sql(derived / "s2_wording.parquet")}) w USING (file_sha)
        JOIN read_parquet({_sql(derived / "s2_structure.parquet")}) st USING (file_sha)
        JOIN prim p USING (file_sha)
        LEFT JOIN cl USING (file_sha)
        LEFT JOIN read_parquet({_sql(derived / "s2_specificity.parquet")}) sp USING (file_sha)
        ORDER BY b.file_sha
    """
    frame = prepare(con.execute(query).fetchdf())
    frame["text"] = frame["text"].fillna("")
    frame["clean"] = [clean_text(t) for t in frame["text"]]
    frame["words_raw"] = np.expm1(frame["body_words"])
    return frame


def clean_text(text: str) -> str:
    """Body without front matter or vendor names, cut to TEXT_CHARS."""
    body = _FRONT.sub("", text or "", count=1)
    return _VENDOR.sub(" ", body)[:TEXT_CHARS]


def rule_scores(text: str) -> dict[str, float]:
    """Untrained style rates per 1,000 words; higher ai_minus_human = more LLM-like."""
    words = _WORDS.findall(text)
    n = max(len(words), 1)
    lower = text.lower()
    per_k = 1000.0 / n
    bullets = max(len(_BULLET.findall(text)), 1)
    ai = (
        sum(lower.count(p) for p in AI_PHRASES) * per_k
        + text.count("—") * per_k
        + len(_EMOJI.findall(text)) * per_k
        + len(_CHECK.findall(text)) * per_k
        + 10.0 * len(_BOLD_LEAD.findall(text)) / bullets
    )
    human = (
        len(_CONTRACTION.findall(text)) * per_k
        + len(_FIRST_PERSON.findall(text)) * per_k
        + len(_LOWER_START.findall(text)) * per_k
        + text.count("!") * per_k
    )
    return {"rule_ai": ai, "rule_human": human, "ai_minus_human": ai - human}


def has_disclosure(text: str) -> bool:
    """An AI attribution line written into the skill text itself."""
    return bool(_DISCLOSURE.search(text or ""))


# --- methods: fit(train, seed) -> predict(frame) -> scores (higher = agent) ---------------

Predictor = Callable[[pd.DataFrame], np.ndarray]


def _column(name: str) -> Callable[[pd.DataFrame, int], Predictor]:
    def fit(train: pd.DataFrame, seed: int) -> Predictor:
        return lambda frame: frame[name].to_numpy()

    return fit


def _features_lr(train: pd.DataFrame, seed: int) -> Predictor:
    model = make_pipeline(
        StandardScaler(), LogisticRegression(C=1.0, max_iter=2000, random_state=seed)
    )
    model.fit(train[list(FEATURES)].to_numpy(), train["y"].to_numpy())
    return lambda frame: model.predict_proba(frame[list(FEATURES)].to_numpy())[:, 1]


def _features_gbm(train: pd.DataFrame, seed: int) -> Predictor:
    cols = [*FEATURES, "rule_ai", "rule_human"]
    model = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, random_state=seed)
    model.fit(train[cols].to_numpy(), train["y"].to_numpy())
    return lambda frame: model.predict_proba(frame[cols].to_numpy())[:, 1]


def _tfidf(analyzer: str, ngram: tuple[int, int], max_features: int) -> Callable:
    def fit(train: pd.DataFrame, seed: int) -> Predictor:
        vec = TfidfVectorizer(
            analyzer=analyzer,
            ngram_range=ngram,
            min_df=5,
            max_df=0.9,
            max_features=max_features,
            sublinear_tf=True,
            lowercase=True,
            dtype=np.float32,
        )
        x_train = vec.fit_transform(train["clean"])
        model = LogisticRegression(C=2.0, max_iter=3000, random_state=seed)
        model.fit(x_train, train["y"].to_numpy())
        return lambda frame: model.predict_proba(vec.transform(frame["clean"]))[:, 1]

    return fit


METHODS: dict[str, Callable[[pd.DataFrame, int], Predictor]] = {
    "length_only": _column("body_words"),
    "ai_phrase_rule": _column("ai_minus_human"),
    "features_lr": _features_lr,
    "features_gbm": _features_gbm,
    "tfidf_words": _tfidf("word", (1, 2), 200_000),
    "tfidf_chars": _tfidf("char_wb", (3, 5), 300_000),
}
ENSEMBLE = ("features_gbm", "tfidf_words", "tfidf_chars")


def cross_validate(frame: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Out-of-fold score per method; folds group by owner and near-duplicate cluster."""
    seed = int(cfg.seed)
    keep = ["file_sha", "y", "owner", "is_bulk", "commit_count", "span_days", "words_raw"]
    oof = frame[keep].copy()
    oof["fold"] = -1
    for fold, (train_idx, test_idx) in enumerate(group_split(frame, seed=seed, n_splits=_N_SPLITS)):
        train, test = frame.iloc[train_idx], frame.iloc[test_idx]
        oof.loc[test_idx, "fold"] = fold
        for name, method in METHODS.items():
            oof.loc[test_idx, name] = method(train, seed)(test)
            log.info("E5 fold %d %s done", fold, name)
    ranks = [oof[m].rank(pct=True) for m in ENSEMBLE]
    oof["ensemble"] = np.mean(ranks, axis=0)
    return oof


def evaluate(frame: pd.DataFrame, oof: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """One row per method x subset: AUC, length-controlled AUC, share decided at each precision."""
    subsets = {
        "all": np.ones(len(frame), dtype=bool),
        "matched": ((frame["y"] == 0) | ~frame["is_bulk"].fillna(False).astype(bool)).to_numpy(),
        "strong_human": (
            (frame["y"] == 1)
            | (
                (frame["commit_count"].fillna(0) >= int(cfg.thresholds.human_min_commits))
                & (frame["span_days"].fillna(0) >= int(cfg.thresholds.human_min_span_days))
            )
        ).to_numpy(),
    }
    quartile = pd.qcut(frame["words_raw"], 4, labels=False, duplicates="drop").to_numpy()
    rows = []
    y_all = oof["y"].to_numpy()
    for name in [*METHODS, "ensemble"]:
        score = oof[name].to_numpy()
        for subset, mask in subsets.items():
            y, s = y_all[mask], score[mask]
            within = [_auc(y[quartile[mask] == q], s[quartile[mask] == q]) for q in range(4)]
            row = {
                "method": name,
                "subset": subset,
                "n_agent": int(y.sum()),
                "n_human": int((y == 0).sum()),
                "auc": _auc(y, s),
                "auc_within_length": float(np.nanmean(within)),
            }
            for p in TARGET_PRECISION:
                agent, human = decided_share(y, s, p)
                row[f"agent_decided_p{int(p * 100)}"] = agent
                row[f"human_decided_p{int(p * 100)}"] = human
            rows.append(row)
    return pd.DataFrame(rows)


def decided_share(y: np.ndarray, score: np.ndarray, precision: float) -> tuple[float, float]:
    """Largest share of agent (top) and human (bottom) rows labelled at >= precision (50/50)."""
    w = np.where(y == 1, 0.5 / max(y.sum(), 1), 0.5 / max((y == 0).sum(), 1))
    order = np.argsort(-score, kind="stable")
    back = order[::-1]
    return (
        _best_cover(y[order] == 1, w[order], score[order], precision),
        _best_cover(y[back] == 0, w[back], score[back], precision),
    )


def _best_cover(
    hit: np.ndarray, w: np.ndarray, sorted_score: np.ndarray, precision: float
) -> float:
    tp = np.cumsum(w * hit)
    total = np.cumsum(w)
    # A threshold can only cut between different scores, never inside a run of ties.
    cut = np.append(sorted_score[1:] != sorted_score[:-1], True)
    ok = np.flatnonzero(cut & (tp / total >= precision))
    if len(ok) == 0:
        return 0.0
    k = ok.max()
    return float(min(1.0, tp[k] / max(w[hit].sum(), 1e-12)))


def top_words(frame: pd.DataFrame, cfg: Config, k: int = 40) -> pd.DataFrame:
    """Largest word-model coefficients on the full sample, both directions."""
    vec = TfidfVectorizer(
        ngram_range=(1, 2),
        min_df=5,
        max_df=0.9,
        max_features=200_000,
        sublinear_tf=True,
        dtype=np.float32,
    )
    x = vec.fit_transform(frame["clean"])
    model = LogisticRegression(C=2.0, max_iter=3000, random_state=int(cfg.seed))
    model.fit(x, frame["y"].to_numpy())
    names = vec.get_feature_names_out()
    coef = model.coef_[0]
    order = np.argsort(coef)
    pick = [*order[-k:][::-1], *order[:k]]
    return pd.DataFrame(
        {
            "term": names[pick],
            "coef": coef[pick],
            "direction": ["agent"] * k + ["human"] * k,
        }
    )


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Fit and compare the text methods on labelled history skills."""
    derived = resolve(cfg, cfg.paths.derived)
    results = resolve(cfg, cfg.paths.results)
    inputs = [
        derived / name
        for name in (
            "s3_labels_v2_1.parquet",
            "s2_structure.parquet",
            "s2_wording.parquet",
            "s2_commit.parquet",
            "s2_clusters.parquet",
            "s2_specificity.parquet",
        )
    ]
    for path in inputs:
        if not path.is_file():
            raise FileNotFoundError(f"missing {path}; run the stage that builds it")
    outputs = [
        derived / "s3_text_methods_oof.parquet",
        results / "s3_text_methods.csv",
        results / "s3_text_methods.json",
        results / "s3_text_methods_top_words.csv",
    ]
    if not force and all(inputs_unchanged(p, cfg=cfg, inputs=inputs) for p in outputs):
        rows = {p.name: json.loads(manifest_path(p).read_text())["rows"] for p in outputs}
        return StageResult("s3_labeling", outputs, skipped=True, rows=rows)
    con = duckdb.connect()
    try:
        frame = labelled_frame(con, cfg)
    finally:
        con.close()
    if frame["y"].nunique() < 2:
        raise ValueError("text methods need both agent and human skills")
    rules = pd.DataFrame([rule_scores(t) for t in frame["clean"]])
    frame = pd.concat([frame, rules], axis=1)
    disclosed = frame["text"].map(has_disclosure)
    oof = cross_validate(frame, cfg)
    table = evaluate(frame, oof, cfg)
    words = top_words(frame, cfg)
    best = table[table["subset"] == "all"].sort_values("auc", ascending=False).iloc[0]
    summary = {
        "n_agent": int(frame["y"].sum()),
        "n_human": int((frame["y"] == 0).sum()),
        "best_method_all": best["method"],
        "best_auc_all": float(best["auc"]),
        "in_text_disclosure": {
            "agent_rows": int(disclosed[frame["y"] == 1].sum()),
            "human_rows": int(disclosed[frame["y"] == 0].sum()),
        },
        "ensemble_of": list(ENSEMBLE),
        "target_precision": list(TARGET_PRECISION),
        "text_chars": TEXT_CHARS,
        "seed": int(cfg.seed),
        "note": INTERPRETATION_NOTE,
    }
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
    oof.to_parquet(outputs[0], index=False)
    table.to_csv(outputs[1], index=False)
    outputs[2].write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    words.to_csv(outputs[3], index=False)
    counts = [len(oof), len(table), 1, len(words)]
    for path, count in zip(outputs, counts, strict=True):
        write_manifest(path, cfg=cfg, inputs=inputs, rows=count)
    log.info("E5: best=%s AUC=%.3f", summary["best_method_all"], summary["best_auc_all"])
    return StageResult(
        "s3_labeling", outputs, rows=dict(zip([p.name for p in outputs], counts, strict=True))
    )


def _auc(y: Sequence[int] | np.ndarray, score: Sequence[float] | np.ndarray) -> float:
    y = np.asarray(y)
    if len(y) == 0 or y.min() == y.max():
        return float("nan")
    return float(roc_auc_score(y, score))


def _sql(path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"
