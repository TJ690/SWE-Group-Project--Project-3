"""
STAGE: s3_labeling
MODULE: style_model
PURPOSE: E4 - PU style model: does text alone resemble skills whose commits disclosed an AI tool?
INPUTS: s3_labels_v2_1, s2_structure, s2_wording, s2_commit, s2_clusters, artifacts (content)
OUTPUTS: s3_style_scores.parquet, results/s3_style_model.csv|json, results/s3_style_top_words.csv
RQ LINK: RQ1 (tier C: style resemblance; evaluation E4 - disclosed hold-out without commit data)

Positive-unlabelled learning (Elkan & Noto, KDD 2008). Positives are skills with a disclosed AI
trailer or agent bot; every other skill with history is unlabelled, not human. Commit, repo and
date columns never enter the text model. Folds group by owner and near-duplicate cluster, and the
marker lexicon is fitted inside each training fold. A text model that does not beat the
date-only baseline is a valid, reportable result.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

import duckdb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import StandardScaler

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s2_features.lexicons import (
    VENDOR_WORDS,
    doc_sets,
    fightin_words,
    fit_lexicon,
    rate_from_tokens,
    tokens,
)
from skillsig.s3_labeling.baselines import group_keys, group_split

log = logging.getLogger(__name__)

STRUCTURE = (
    "frontmatter_valid",
    "body_chars",
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
_COUNTS = (
    "body_chars",
    "desc_chars",
    "body_words",
    "headings",
    "h2",
    "numbered_items",
    "bullet_items",
    "code_blocks",
    "table_rows",
    "links",
)
WORDING = (
    "w_imperative_ratio",
    "w_you_per_100w",
    "w_command_density",
    "w_fk_grade",
    "w_mattr500",
    "w_emphasis_per_k",
    "w_uniformity_bullet_cv",
    "w_uniformity_heading_entropy",
    "w_non_english",
    "w_sentence_cv",
    "w_prose_sentences",
    "w_emdash_per_1kw",
)
SPECIFICITY = ("specificity", "has_specificity", "n_identifiers")
# Added 2026-10-25; "text_without_new" drops them to measure what they add.
NEW_FEATURES = ("w_sentence_cv", "w_prose_sentences", "w_emdash_per_1kw", *SPECIFICITY)
TEXT_FEATURES: tuple[str, ...] = (*STRUCTURE, *WORDING, *SPECIFICITY, "marker_rate")
DATE_FEATURES: tuple[str, ...] = ("months_since_format",)
MODELS: dict[str, tuple[str, ...]] = {
    "text_only": TEXT_FEATURES,
    "date_only": DATE_FEATURES,
    "text_plus_date": (*TEXT_FEATURES, *DATE_FEATURES),
    "text_without_new": tuple(f for f in TEXT_FEATURES if f not in NEW_FEATURES),
    "text_no_markers": tuple(
        f for f in TEXT_FEATURES if f not in ("marker_rate", "w_emphasis_per_k")
    ),
}
_NOTE = (
    "AUC measures disclosed-vs-unlabelled proxy discrimination, not verified authorship. "
    "Disclosure selection and label noise can bias it in either direction; no guaranteed "
    "lower bound on true-authorship AUC is established. PU rescaling assumes random "
    "disclosure among agent skills, which is not established."
)
_N_SPLITS = 5
_BOOT = 1000


def training_frame(con: duckdb.DuckDBPyConnection, cfg: Config) -> pd.DataFrame:
    """History rows with wording features, not templated: s, owner, cluster, text, features."""
    derived = resolve(cfg, cfg.paths.derived)
    art = resolve(cfg, cfg.paths.raw) / "artifacts" / "*.parquet"
    cols_s = ", ".join(f"st.{c}" for c in STRUCTURE)
    cols_w = ", ".join(f"w.{c}" for c in WORDING)
    threshold = float(cfg.minhash.lsh_threshold)
    query = f"""
        WITH base AS (
            SELECT l.file_sha, l.flag_disclosed_agent AS s,
                   k.first_commit_at, k.ai_tool_first_ext, k.ai_tool_last_ext
            FROM read_parquet({_sql(derived / "s2_wording.parquet")}) w
            JOIN read_parquet({_sql(derived / "s3_labels_v2_1.parquet")}) l USING (file_sha)
            JOIN read_parquet({_sql(derived / "s2_commit.parquet")}) k USING (file_sha)
            WHERE k.history_fetched AND NOT l.flag_templated
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
        SELECT b.file_sha, b.s, split_part(p.repo_full_name, '/', 1) AS owner, cl.cluster_id,
               p.text, b.first_commit_at, b.ai_tool_first_ext, b.ai_tool_last_ext,
               {cols_s}, {cols_w}, sp.specificity, sp.n_identifiers
        FROM base b
        JOIN read_parquet({_sql(derived / "s2_wording.parquet")}) w USING (file_sha)
        JOIN read_parquet({_sql(derived / "s2_structure.parquet")}) st USING (file_sha)
        JOIN prim p USING (file_sha)
        LEFT JOIN cl USING (file_sha)
        LEFT JOIN read_parquet({_sql(derived / "s2_specificity.parquet")}) sp USING (file_sha)
        ORDER BY b.file_sha
    """
    frame = con.execute(query).fetchdf()
    return prepare(frame)


def prepare(frame: pd.DataFrame) -> pd.DataFrame:
    """Numeric features, log1p on counts, and months since the format launched."""
    out = frame.copy()
    out["s"] = out["s"].fillna(False).astype(bool).astype(int)
    for col in (*STRUCTURE, *WORDING):
        out[col] = pd.to_numeric(out[col], errors="coerce").astype(float).fillna(0.0)
    out["specificity"] = pd.to_numeric(out["specificity"], errors="coerce")
    out["has_specificity"] = out["specificity"].notna().astype(float)
    out["specificity"] = out["specificity"].fillna(0.0)
    identifiers = pd.to_numeric(out["n_identifiers"], errors="coerce").fillna(0.0)
    out["n_identifiers"] = np.log1p(identifiers.clip(lower=0))
    for col in _COUNTS:
        out[col] = np.log1p(out[col].clip(lower=0))
    when = pd.to_datetime(out["first_commit_at"], errors="coerce", utc=True)
    launch = pd.Timestamp("2025-10-01", tz="UTC")
    out["months_since_format"] = ((when - launch).dt.days / 30.44).fillna(0.0)
    return out.reset_index(drop=True)


def fit_fold(train: pd.DataFrame, features: Sequence[str], seed: int) -> Pipeline:
    model = make_pipeline(
        StandardScaler(), LogisticRegression(C=1.0, max_iter=2000, random_state=seed)
    )
    model.fit(train[list(features)].to_numpy(), train["s"].to_numpy())
    return model


def elkan_noto_c(model: Pipeline, held_out_pos: pd.DataFrame, features: Sequence[str]) -> float:
    """Label frequency c = mean P(s=1|x) on held-out positives."""
    if held_out_pos.empty:
        return float("nan")
    return float(model.predict_proba(held_out_pos[list(features)].to_numpy())[:, 1].mean())


def cross_validate(frame: pd.DataFrame, cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    """-> (out-of-fold scores, per-fold metrics) for every model in MODELS."""
    data = frame.copy()
    sets = doc_sets(data["text"].fillna(""))
    toks = [tokens(t) for t in data["text"].fillna("")]
    oof = pd.DataFrame({"file_sha": data["file_sha"], "s": data["s"], "fold": -1})
    metrics = []
    folds = group_split(data, seed=int(cfg.seed), n_splits=_N_SPLITS)
    for fold, (train_idx, test_idx) in enumerate(folds):
        s = data["s"].to_numpy()
        pos = [sets[i] for i in train_idx if s[i] == 1]
        neg = [sets[i] for i in train_idx if s[i] == 0]
        lexicon = frozenset(fit_lexicon(pos, neg))
        data.loc[:, "marker_rate"] = [rate_from_tokens(t, lexicon) for t in toks]
        train, test = data.iloc[train_idx], data.iloc[test_idx]
        oof.loc[test_idx, "fold"] = fold
        for name, features in MODELS.items():
            model = fit_fold(train, features, int(cfg.seed))
            score = model.predict_proba(test[list(features)].to_numpy())[:, 1]
            c = elkan_noto_c(model, test[test["s"] == 1], features)
            oof.loc[test_idx, f"g_{name}"] = score
            oof.loc[test_idx, f"c_{name}"] = c
            metrics.append(
                {
                    "model": name,
                    "fold": str(fold),
                    "auc_pu": _auc(test["s"], score),
                    "n_pos": int(test["s"].sum()),
                    "n_unl": int((test["s"] == 0).sum()),
                    "c": c,
                    "lexicon_size": len(lexicon),
                }
            )
    oof["style_score"] = np.minimum(1.0, oof["g_text_only"] / oof["c_text_only"])
    return oof, pd.DataFrame(metrics)


def summarise(
    frame: pd.DataFrame, oof: pd.DataFrame, folds: pd.DataFrame, cfg: Config
) -> tuple[pd.DataFrame, dict]:
    """Mean AUC per model with a group-bootstrap CI, the usefulness test and PU estimates."""
    rng = np.random.default_rng(int(cfg.seed))
    groups = group_keys(frame["owner"], frame["cluster_id"]).to_numpy()
    unique = np.unique(groups)
    index_by_group = {g: np.flatnonzero(groups == g) for g in unique}
    s = oof["s"].to_numpy()
    boot: dict[str, list[float]] = {name: [] for name in MODELS}
    diff: list[float] = []
    prevalence: list[float] = []
    for _ in range(_BOOT):
        pick = rng.choice(unique, size=len(unique), replace=True)
        idx = np.concatenate([index_by_group[g] for g in pick])
        if s[idx].min() == s[idx].max():
            continue
        aucs = {name: _auc(s[idx], oof[f"g_{name}"].to_numpy()[idx]) for name in MODELS}
        for name, value in aucs.items():
            boot[name].append(value)
        diff.append(aucs["text_plus_date"] - max(aucs["date_only"], 0.5))
        unl = idx[s[idx] == 0]
        prevalence.append(float(oof["style_score"].to_numpy()[unl].mean()))
    rows = []
    for name in MODELS:
        auc = _auc(s, oof[f"g_{name}"].to_numpy())
        low, high = np.percentile(boot[name], [2.5, 97.5])
        part = folds[folds["model"] == name]
        rows.append(
            {
                "model": name,
                "fold": "pooled",
                "auc_pu": auc,
                "n_pos": int(s.sum()),
                "n_unl": int((s == 0).sum()),
                "c": float(part["c"].mean()),
                "lexicon_size": float(part["lexicon_size"].mean()),
                "ci_low": low,
                "ci_high": high,
            }
        )
    table = pd.concat([folds, pd.DataFrame(rows)], ignore_index=True)
    d_low, d_high = np.percentile(diff, [2.5, 97.5])
    pooled = {r["model"]: r["auc_pu"] for r in rows}
    d_mean = pooled["text_plus_date"] - max(pooled["date_only"], 0.5)
    pos_scores = oof.loc[oof["s"] == 1, "style_score"]
    theta_hi = float(np.percentile(pos_scores, 20))  # keeps 80% recall
    theta_lo = float(np.percentile(pos_scores, 5))
    unl_scores = oof.loc[oof["s"] == 0, "style_score"]
    summary = {
        "n_rows": len(frame),
        "n_pos": int(s.sum()),
        "auc_pooled": pooled,
        "auc_ci": {r["model"]: [r["ci_low"], r["ci_high"]] for r in rows},
        "text_plus_date_minus_best_of_date_or_chance": {"mean": d_mean, "ci": [d_low, d_high]},
        "style_useful": bool(d_mean >= 0.02 and d_low > 0),
        "theta_hi": theta_hi,
        "theta_lo": theta_lo,
        "recall_at_theta_hi": float((pos_scores >= theta_hi).mean()),
        "share_unlabelled_above_theta_hi": float((unl_scores >= theta_hi).mean()),
        "prevalence_estimate": {
            "mean": float(unl_scores.mean()),
            "ci": list(np.percentile(prevalence, [2.5, 97.5])),
            "reliable": bool(pooled["text_only"] >= 0.7),
            "note": "Elkan-Noto rescaling saturates when the classifier is weak; do not "
            "report as a finding unless reliable is true",
        },
        "auc_by_tool": _auc_by_tool(frame, oof),
        "single_feature_auc": _single_feature_auc(frame),
        "features": {name: list(features) for name, features in MODELS.items()},
        "seed": int(cfg.seed),
        "note": _NOTE,
    }
    return table, summary


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Fit and evaluate the PU style models. Wording exists for the dev sample only."""
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
        derived / "s3_style_scores.parquet",
        results / "s3_style_model.csv",
        results / "s3_style_model.json",
        results / "s3_style_top_words.csv",
    ]
    if not force and all(inputs_unchanged(p, cfg=cfg, inputs=inputs) for p in outputs):
        rows = {p.name: json.loads(manifest_path(p).read_text())["rows"] for p in outputs}
        return StageResult("s3_labeling", outputs, skipped=True, rows=rows)
    con = duckdb.connect()
    try:
        frame = training_frame(con, cfg)
    finally:
        con.close()
    if frame["s"].nunique() < 2:
        raise ValueError("style model needs both disclosed and unlabelled skills")
    oof, folds = cross_validate(frame, cfg)
    table, summary = summarise(frame, oof, folds, cfg)
    sets = doc_sets(frame["text"].fillna(""))
    s = frame["s"].to_numpy()
    top = fightin_words(
        [d for d, y in zip(sets, s, strict=True) if y == 1],
        [d for d, y in zip(sets, s, strict=True) if y == 0],
    )
    top = top[~top["word"].isin(VENDOR_WORDS)].head(30)
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
    scores = oof[["file_sha", "fold", "s", "g_text_only", "g_text_plus_date", "style_score"]]
    scores.to_parquet(outputs[0], index=False)
    table.to_csv(outputs[1], index=False)
    outputs[2].write_text(json.dumps(_jsonable(summary), indent=2, sort_keys=True) + "\n")
    top.to_csv(outputs[3], index=False)
    counts = [len(scores), len(table), 1, len(top)]
    for path, count in zip(outputs, counts, strict=True):
        write_manifest(path, cfg=cfg, inputs=inputs, rows=count)
    log.info(
        "E4: AUC text_only=%.3f date_only=%.3f text_plus_date=%.3f useful=%s",
        summary["auc_pooled"]["text_only"],
        summary["auc_pooled"]["date_only"],
        summary["auc_pooled"]["text_plus_date"],
        summary["style_useful"],
    )
    rows = {p.name: c for p, c in zip(outputs, counts, strict=True)}
    return StageResult("s3_labeling", outputs, rows=rows)


def _auc(y: Sequence[int] | np.ndarray, score: Sequence[float] | np.ndarray) -> float:
    y = np.asarray(y)
    if y.min() == y.max():
        return float("nan")
    return float(roc_auc_score(y, score))


def _single_feature_auc(frame: pd.DataFrame) -> dict[str, dict[str, float | str]]:
    """In-sample AUC of each new feature alone, with the direction that favours positives."""
    out: dict[str, dict[str, float | str]] = {}
    for col in NEW_FEATURES:
        auc = _auc(frame["s"], frame[col])
        out[col] = {
            "auc": max(auc, 1 - auc),
            "higher_in": "disclosed" if auc >= 0.5 else "unlabelled",
            "mean_disclosed": float(frame.loc[frame["s"] == 1, col].mean()),
            "mean_unlabelled": float(frame.loc[frame["s"] == 0, col].mean()),
        }
    return out


def _auc_by_tool(frame: pd.DataFrame, oof: pd.DataFrame) -> dict[str, dict[str, float]]:
    tools = frame["ai_tool_first_ext"].fillna("") + "," + frame["ai_tool_last_ext"].fillna("")
    claude = tools.str.contains("claude")
    out = {}
    for name, mask in (("claude_positives", claude), ("other_positives", ~claude)):
        keep = (frame["s"] == 0) | ((frame["s"] == 1) & mask)
        n_pos = int(((frame["s"] == 1) & mask).sum())
        if n_pos < 30:
            out[name] = {"n_pos": n_pos, "auc_text_only": float("nan")}
            continue
        out[name] = {
            "n_pos": n_pos,
            "auc_text_only": _auc(frame.loc[keep, "s"], oof.loc[keep, "g_text_only"]),
        }
    return out


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.floating, np.integer)):
        value = value.item()
    if isinstance(value, float) and np.isnan(value):
        return None
    return value


def _sql(path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"
