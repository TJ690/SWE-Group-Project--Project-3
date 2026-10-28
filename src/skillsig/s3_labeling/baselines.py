"""
STAGE: s3_labeling
MODULE: baselines
PURPOSE: One-signal rule baselines (B0-B3b) and the owner + cluster grouped split.
INPUTS: s3_labels_v2, s3_labels_v1 (copies), s2_commit, file_agents, artifacts primary rows
OUTPUTS: src/data/derived/s3_baseline_labels.parquet, results/s3_baseline_summary.csv
RQ LINK: RQ1 (yardstick: does the multi-signal rule set beat single obvious rules?)

A baseline agreeing with the rules is not evidence that either is correct; only the annotated
sample (s4_validation) can say which is closer to what a careful reader sees.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from sklearn.model_selection import GroupKFold

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_intake.devsample import output_path as devsample_path

log = logging.getLogger(__name__)

BASELINES = ("B0_majority", "B1_copy_count", "B2_agent_folder", "B3_trailer", "B3b_trailer_no_bulk")
# Tool-specific folders only; shared (.agents/.agent), other dot-folders and none do not count.
TOOL_FOLDERS = frozenset(
    {
        "Claude Code",
        "GitHub Copilot",
        "Cursor",
        "Codex",
        "Gemini CLI",
        "Kiro",
        "opencode",
        "Antigravity",
    }
)
_NONE = "insufficient_evidence"
FEATURE_COLUMNS = ("label_v2", "copies", "agent_folder", "ai_trailer_first", "batch_size")

_V2 = "python -m skillsig label --only rules_v2"
_V1 = "python -m skillsig label --only rules_v1"
_COMMIT = "python -m skillsig features --only commit"
_DOWNLOAD = "python src/data/download_data.py"
_DEV_SAMPLE = "python -m skillsig data --dev-sample"


def predict_rules(features: pd.DataFrame, which: str, cfg: Config) -> pd.Series:
    """Labels from one baseline rule. `features` needs the FEATURE_COLUMNS."""
    index = features.index
    if which == "B0_majority":
        counts = features["label_v2"].value_counts()
        top = sorted(counts[counts == counts.max()].index)[0]
        return pd.Series(top, index=index, name=which, dtype="string")
    if which == "B1_copy_count":
        copies = pd.to_numeric(features["copies"], errors="coerce").fillna(0)
        hit = copies >= int(cfg.thresholds.templated_copies_min)
        return _where(hit, "highly_templated", which)
    if which == "B2_agent_folder":
        return _where(features["agent_folder"].isin(TOOL_FOLDERS), "agent_assisted", which)
    trailer = features["ai_trailer_first"].fillna(False).astype(bool)
    if which == "B3_trailer":
        return _where(trailer, "agent_assisted", which)
    if which == "B3b_trailer_no_bulk":
        batch = pd.to_numeric(features["batch_size"], errors="coerce")
        small = batch.notna() & (batch < int(cfg.thresholds.bulk_batch_min))
        return _where(trailer & small, "agent_assisted", which)
    raise ValueError(f"unknown baseline {which!r}; expected one of {BASELINES}")


def group_keys(owner: pd.Series, cluster_id: pd.Series) -> pd.Series:
    """Connected components over owner and cluster_id; one integer group per component.

    Two skills share a group when they share an owner or a near-duplicate cluster, directly or
    through a chain. A skill without a cluster is grouped with its owner only.
    """
    owners, _ = pd.factorize(owner.fillna("").astype(str), sort=True)
    has_cluster = cluster_id.notna().to_numpy()
    clusters, _ = pd.factorize(cluster_id[has_cluster], sort=True)
    n_owner = int(owners.max()) + 1 if len(owners) else 0
    n_nodes = n_owner + (int(clusters.max()) + 1 if len(clusters) else 0)
    rows = owners[has_cluster]
    cols = clusters + n_owner
    graph = coo_matrix((np.ones(len(rows), dtype=np.int8), (rows, cols)), shape=(n_nodes, n_nodes))
    _, component = connected_components(graph, directed=False)
    raw = pd.Series(component[owners], index=owner.index)
    codes, _ = pd.factorize(raw)  # renumber by first appearance -> deterministic
    return pd.Series(codes, index=owner.index, name="group")


def group_split(
    df: pd.DataFrame, *, seed: int, n_splits: int = 5
) -> list[tuple[np.ndarray, np.ndarray]]:
    """GroupKFold over group_keys(df.owner, df.cluster_id); no group spans train and test."""
    groups = group_keys(df["owner"], df["cluster_id"])
    folds = GroupKFold(n_splits=n_splits, shuffle=True, random_state=seed)
    return [(train, test) for train, test in folds.split(df, groups=groups)]


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write s3_baseline_labels.parquet and the summary CSV. Dev mode keeps dev-sample ids."""
    outputs = _output_paths(cfg)
    inputs = _inputs(cfg, dev=dev)
    _require_inputs(cfg, dev=dev)
    if not force and all(inputs_unchanged(p, cfg=cfg, inputs=inputs) for p in outputs):
        log.info("baselines up to date: %s", outputs[0])
        return StageResult(
            stage="s3_labeling", outputs=outputs, skipped=True, rows=_saved_rows(outputs)
        )
    features = _features(cfg, dev=dev)
    labels = pd.DataFrame({"file_sha": features["file_sha"]})
    for name in BASELINES:
        labels[name] = predict_rules(features, name, cfg).astype("string")
    labels_path, summary_path = outputs
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    labels.to_parquet(labels_path, index=False, compression="zstd")
    summary = _summary(labels)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(summary_path, index=False)
    written = (len(labels), len(summary))
    for path, count in zip(outputs, written, strict=True):
        write_manifest(path, cfg=cfg, inputs=inputs, rows=count)
    log.info("wrote %s (%s rows) and %s", labels_path, written[0], summary_path)
    return StageResult(
        stage="s3_labeling",
        outputs=outputs,
        skipped=False,
        rows={"s3_baseline_labels": written[0], "s3_baseline_summary": written[1]},
    )


def _where(mask: pd.Series, label: str, name: str) -> pd.Series:
    out = pd.Series(_NONE, index=mask.index, name=name, dtype="string")
    out[mask.to_numpy(dtype=bool)] = label
    return out


def _summary(labels: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for name in BASELINES:
        counts = labels[name].value_counts().rename_axis("label").reset_index(name="n")
        counts.insert(0, "baseline", name)
        counts["share"] = (counts["n"] / counts["n"].sum()).round(6)
        parts.append(counts.sort_values("label"))
    return pd.concat(parts, ignore_index=True)


def _features(cfg: Config, *, dev: bool) -> pd.DataFrame:
    derived = resolve(cfg, cfg.paths.derived)
    sample_join = ""
    if dev:
        sample_join = (
            "JOIN (SELECT DISTINCT file_sha FROM read_parquet("
            f"{_sql(devsample_path(cfg))})) d USING (file_sha)"
        )
    query = f"""
        WITH v1 AS (
            SELECT file_sha, max(copies) AS copies
            FROM read_parquet({_sql(derived / "s3_labels_v1.parquet")}) GROUP BY file_sha
        ),
        prim AS (
            SELECT file_sha, min(repo_full_name) AS repo_full_name, min(path) AS path
            FROM read_parquet({_sql(_artifacts_dir(cfg) / "*.parquet")})
            WHERE coalesce(TRY_CAST(dedup_primary AS INTEGER), 0) = 1
            GROUP BY file_sha
        ),
        folders AS (
            SELECT repo_full_name, path, any_value(agent_folder) AS agent_folder
            FROM read_parquet({_sql(_file_agents(cfg))}) GROUP BY 1, 2
        )
        SELECT l.file_sha, l.label AS label_v2, v1.copies, f.agent_folder,
               k.ai_trailer_first, k.batch_size
        FROM read_parquet({_sql(derived / "s3_labels_v2.parquet")}) l
        {sample_join}
        LEFT JOIN v1 USING (file_sha)
        LEFT JOIN read_parquet({_sql(derived / "s2_commit.parquet")}) k USING (file_sha)
        LEFT JOIN prim p USING (file_sha)
        LEFT JOIN folders f ON f.repo_full_name = p.repo_full_name AND f.path = p.path
        ORDER BY l.file_sha
    """
    con = duckdb.connect()
    try:
        return con.execute(query).fetchdf()
    finally:
        con.close()


def _sql(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _artifacts_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "artifacts"


def _file_agents(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.generated) / "file_agents.parquet"


def _output_paths(cfg: Config) -> list[Path]:
    derived = resolve(cfg, cfg.paths.derived)
    results = resolve(cfg, cfg.paths.results)
    return [derived / "s3_baseline_labels.parquet", results / "s3_baseline_summary.csv"]


def _inputs(cfg: Config, *, dev: bool) -> list[Path]:
    derived = resolve(cfg, cfg.paths.derived)
    paths = [
        derived / "s3_labels_v2.parquet",
        derived / "s3_labels_v1.parquet",
        derived / "s2_commit.parquet",
        _file_agents(cfg),
        _artifacts_dir(cfg),
    ]
    if dev:
        paths.append(devsample_path(cfg))
    return paths


def _require_inputs(cfg: Config, *, dev: bool) -> None:
    derived = resolve(cfg, cfg.paths.derived)
    for path, command in (
        (derived / "s3_labels_v2.parquet", _V2),
        (derived / "s3_labels_v1.parquet", _V1),
        (derived / "s2_commit.parquet", _COMMIT),
        (_file_agents(cfg), _DOWNLOAD),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"missing {path}; run: {command}")
    directory = _artifacts_dir(cfg)
    if not directory.is_dir() or not any(directory.glob("*.parquet")):
        raise FileNotFoundError(f"missing {directory}; run: {_DOWNLOAD}")
    if dev and not devsample_path(cfg).is_file():
        raise FileNotFoundError(f"missing {devsample_path(cfg)}; run: {_DEV_SAMPLE}")


def _saved_rows(outputs: list[Path]) -> dict[str, int]:
    keys = ("s3_baseline_labels", "s3_baseline_summary")
    return {
        key: int(json.loads(manifest_path(path).read_text(encoding="utf-8"))["rows"])
        for key, path in zip(keys, outputs, strict=True)
    }
