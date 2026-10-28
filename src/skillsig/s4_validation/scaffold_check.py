"""
STAGE: s4_validation
MODULE: scaffold_check
PURPOSE: Check the scaffold signals: what they add, recall on registry copies, the agent-style
         confound, and a spot-check sheet with winnowing evidence.
INPUTS: s2_scaffold, s3_labels_v2_1, and the E2 inputs (s2_template, s3_labels_v1, s2_clusters);
        artifacts text for the spot-check rows only
OUTPUTS: results/s4_scaffold_{overlap,recall,agent_confound,spot}.csv
RQ LINK: RQ1 (is highly templated more than a copy count?)

Overlap is scaffold basis x existing templated basis; the `none` column counts skills the copy,
cluster and registry rules miss. Recall uses the E2 known set (skills matching a mined registry
version). The confound table compares outline hits between skills whose commits disclosed an
agent and the rest: a much higher rate there means the outline rule partly measures LLM style.
Artifacts text is not a manifest input (only spot-check evidence reads it; hashing 13 GB on
every check would dominate the run).
"""

from __future__ import annotations

import hashlib
import logging

import duckdb
import pandas as pd

from skillsig.config import Config
from skillsig.provenance import StageResult
from skillsig.s2_features.scaffold import BASES, shared_passages
from skillsig.s4_validation import _io
from skillsig.s4_validation.stats import wilson_interval
from skillsig.s4_validation.template_provenance import provenance_frame, reference_notes

log = logging.getLogger(__name__)

EXISTING = ("registry_exact", "registry_near", "cluster", "copy_count", "none")
# Group that holds a spot-check row's partner, per basis.
_PARTNER_GROUP = {
    "generator": "placeholder_generator",
    "placeholder": "outline_family_id",
    "masked_copy": "masked_sha",
    "shared_lines": "outline_family_id",
    "outline": "outline_family_id",
}
_PASSAGES = 3


def scaffold_frame(con: duckdb.DuckDBPyConnection, cfg: Config) -> pd.DataFrame:
    """Scaffold columns joined to the v2.1 label, its templated basis and the agent flag."""
    query = f"""
        SELECT s.file_sha, s.owner, s.scaffold_basis, s.placeholder_generator,
               s.outline_family_id, s.masked_sha,
               coalesce(l.templated_basis, 'none') AS existing_basis,
               coalesce(l.flag_disclosed_agent, false) AS disclosed_agent,
               l.label AS label_v21
        FROM read_parquet({_io.sql(_io.derived(cfg, "s2_scaffold.parquet"))}) s
        JOIN read_parquet({_io.sql(_io.derived(cfg, "s3_labels_v2_1.parquet"))}) l
          USING (file_sha)
        ORDER BY s.file_sha
    """
    return con.execute(query).fetchdf()


def overlap_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Rows: scaffold basis (plus none); columns: existing templated basis and total."""
    basis = frame["scaffold_basis"].fillna("none")
    table = pd.crosstab(basis, frame["existing_basis"])
    table = table.reindex(index=[*BASES, "none"], columns=list(EXISTING), fill_value=0)
    table["total"] = table.sum(axis=1)
    return table.rename_axis("scaffold_basis").reset_index().rename_axis(None, axis=1)


def recall_table(known: pd.DataFrame) -> pd.DataFrame:
    """Exact recovery and conditional near agreement, with explicit reference scope."""
    known = known[(known["registry_kind"] != "none") & ~known["registry_origin"].astype(bool)]
    rows = []
    for name, part in (
        ("exact", known[known["registry_kind"] == "exact"]),
        ("near", known[known["registry_kind"] == "near"]),
        ("exact_or_near", known),
    ):
        n = len(part)
        scaffold = part["scaffold_basis"].notna()
        generic = part["copy_rule"] | part["cluster_rule"]
        hits = int(scaffold.sum())
        combined = int((scaffold | generic).sum())
        low, high = wilson_interval(hits, n)
        rows.append(
            {
                "known_set": name,
                "n": n,
                "scaffold_hits": hits,
                "scaffold_recall": hits / n if n else float("nan"),
                "ci_low": low,
                "ci_high": high,
                "scaffold_only_hits": int((scaffold & ~generic).sum()),
                "combined_hits": combined,
                "combined_recall": combined / n if n else float("nan"),
                **reference_notes(name),
            }
        )
    return pd.DataFrame(rows)


def confound_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Scaffold and outline-only hit rates for disclosed-agent skills vs the rest."""
    rows = []
    for group, part in (
        ("disclosed_agent", frame[frame["disclosed_agent"].astype(bool)]),
        ("not_disclosed", frame[~frame["disclosed_agent"].astype(bool)]),
    ):
        n = len(part)
        any_hits = int(part["scaffold_basis"].notna().sum())
        outline_hits = int((part["scaffold_basis"] == "outline").sum())
        low, high = wilson_interval(outline_hits, n)
        rows.append(
            {
                "group": group,
                "n": n,
                "scaffold_hits": any_hits,
                "scaffold_rate": any_hits / n if n else float("nan"),
                "outline_only_hits": outline_hits,
                "outline_rate": outline_hits / n if n else float("nan"),
                "ci_low": low,
                "ci_high": high,
            }
        )
    return pd.DataFrame(rows)


def spot_sample(frame: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Seeded sample of cfg.sample.per_label per basis, each with a partner from another owner."""
    frame = frame.assign(_order=[_order_key(sha, cfg.seed) for sha in frame["file_sha"]])
    picked = []
    for basis in BASES:
        part = frame[frame["scaffold_basis"] == basis].sort_values("_order")
        picked.append(part.head(int(cfg.sample.per_label)))
    sample = pd.concat(picked, ignore_index=True) if picked else frame.head(0)
    sample["partner_sha"] = [_partner(row, frame) for row in sample.to_dict(orient="records")]
    return sample.drop(columns="_order")


def evidence(sample: pd.DataFrame, texts: dict[str, str], cfg: Config) -> pd.DataFrame:
    """Spot-check sheet: winnowing overlap and shared passages with the partner skill."""
    rows = []
    for row in sample.to_dict(orient="records"):
        partner = row["partner_sha"]
        overlap, passages = (float("nan"), [])
        if isinstance(partner, str) and row["file_sha"] in texts and partner in texts:
            overlap, passages = shared_passages(texts[row["file_sha"]], texts[partner], cfg)
        passages = [*passages, *[""] * _PASSAGES][:_PASSAGES]
        rows.append(
            {
                "file_sha": row["file_sha"],
                "scaffold_basis": row["scaffold_basis"],
                "existing_basis": row["existing_basis"],
                "owner": row["owner"],
                "partner_sha": partner,
                "winnow_overlap": overlap,
                **{f"shared_passage_{i + 1}": text for i, text in enumerate(passages)},
                "annotator_label": "",
                "notes": "",
            }
        )
    return pd.DataFrame(rows)


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write the overlap, recall, confound and spot-check tables."""
    inputs = [
        _io.derived(cfg, "s2_scaffold.parquet"),
        _io.derived(cfg, "s3_labels_v2_1.parquet"),
        _io.derived(cfg, "s2_template.parquet"),
        _io.derived(cfg, "s3_labels_v1.parquet"),
        _io.derived(cfg, "s2_clusters.parquet"),
    ]
    commands = [
        "python -m skillsig features --only scaffold",
        "python -m skillsig label --only rules_v2_1",
        "python -m skillsig features --only template_sim",
        "python -m skillsig label --only rules_v1",
        "python -m skillsig dedup",
    ]
    _io.require(list(zip(inputs, commands, strict=True)))
    names = ("overlap", "recall", "agent_confound", "spot")
    outputs = [_io.results(cfg, f"s4_scaffold_{name}.csv") for name in names]
    if not force and _io.up_to_date(outputs, cfg, inputs):
        return StageResult("s4_validation", outputs, skipped=True, rows=_io.saved_rows(outputs))
    con = duckdb.connect()
    try:
        frame = scaffold_frame(con, cfg)
        known = provenance_frame(con, cfg).merge(
            frame[["file_sha", "scaffold_basis"]], on="file_sha", how="inner"
        )
        sample = spot_sample(frame, cfg)
        texts = _texts(con, cfg, sample)
    finally:
        con.close()
    tables = [
        overlap_table(frame),
        recall_table(known),
        confound_table(frame),
        evidence(sample, texts, cfg),
    ]
    for table, path in zip(tables, outputs, strict=True):
        _io.write_csv(table, path, cfg, inputs)
    new = int(tables[0].loc[tables[0]["scaffold_basis"] != "none", "none"].sum())
    log.info("scaffold: %s skills flagged that no existing templated rule catches", new)
    return StageResult(
        "s4_validation",
        outputs,
        rows={f"s4_scaffold_{name}": len(t) for name, t in zip(names, tables, strict=True)},
    )


def _order_key(sha: str, seed: int) -> str:
    return hashlib.sha1(f"{seed}-{sha}".encode()).hexdigest()


def _partner(row: dict, frame: pd.DataFrame) -> str | None:
    column = _PARTNER_GROUP.get(row["scaffold_basis"])
    value = row.get(column) if column else None
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    group = frame[(frame[column] == value) & (frame["owner"] != row["owner"])]
    if group.empty:
        return None
    return str(group.sort_values("_order")["file_sha"].iloc[0])


def _texts(con: duckdb.DuckDBPyConnection, cfg: Config, sample: pd.DataFrame) -> dict[str, str]:
    shas = sorted({*sample["file_sha"], *sample["partner_sha"].dropna()})
    if not shas:
        return {}
    con.register("spot_ids", pd.DataFrame({"file_sha": shas}))
    rows = con.execute(
        f"""
        SELECT file_sha, any_value(content)
        FROM read_parquet({_io.sql(_io.artifacts_glob(cfg))})
        WHERE dedup_primary = 1 AND content IS NOT NULL
          AND file_sha IN (SELECT file_sha FROM spot_ids)
        GROUP BY file_sha
        """
    ).fetchall()
    return {str(sha): text for sha, text in rows}
