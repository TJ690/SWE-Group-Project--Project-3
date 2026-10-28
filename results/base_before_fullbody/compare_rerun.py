"""Before (5,000-char MinHash) vs after (full body): clusters, registry matches, labels, checks.

Reads src/data/derived/base_before_fullbody/ (before) and src/data/derived/ (after); writes CSVs
to results/base_before_fullbody/comparison/ and prints a summary.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path("/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3")
BEFORE = ROOT / "src/data/derived/base_before_fullbody"
AFTER = ROOT / "src/data/derived"
RES_BEFORE = ROOT / "results/base_before_fullbody"
OUT = RES_BEFORE / "comparison"
OUT.mkdir(parents=True, exist_ok=True)
con = duckdb.connect()
summary: dict[str, object] = {}


def q(sql: str) -> pd.DataFrame:
    return con.execute(sql).fetchdf()


def p(base: Path, name: str) -> str:
    return "'" + (base / f"{name}.parquet").as_posix() + "'"


# 1. clusters, per threshold
rows = []
for threshold in (0.6, 0.8):
    f = q(f"""
        WITH b AS (SELECT file_sha, cluster_id la, cluster_size sa FROM read_parquet({p(BEFORE, 's2_clusters')})
                   WHERE abs(threshold - {threshold}) < 1e-9),
             a AS (SELECT file_sha, cluster_id lb, cluster_size sb FROM read_parquet({p(AFTER, 's2_clusters')})
                   WHERE abs(threshold - {threshold}) < 1e-9)
        SELECT * FROM b JOIN a USING (file_sha)
    """)
    both = f.groupby(["la", "lb"]).size().rename("both")
    f = f.join(both, on=["la", "lb"])
    same = (f["both"] == f["sa"]) & (f["both"] == f["sb"])
    rows.append(
        {
            "threshold": threshold,
            "skills": len(f),
            "changed_membership": int((~same).sum()),
            "changed_share": float((~same).mean()),
            "near_dup_before": int((f.sa >= 2).sum()),
            "near_dup_after": int((f.sb >= 2).sum()),
            "near_dup_lost": int(((f.sa >= 2) & (f.sb < 2)).sum()),
            "near_dup_gained": int(((f.sa < 2) & (f.sb >= 2)).sum()),
            "cluster10_before": int((f.sa >= 10).sum()),
            "cluster10_after": int((f.sb >= 10).sum()),
            "cluster10_lost": int(((f.sa >= 10) & (f.sb < 10)).sum()),
            "cluster10_gained": int(((f.sa < 10) & (f.sb >= 10)).sum()),
            "clusters_ge2_before": int(f.loc[f.sa >= 2, "la"].nunique()),
            "clusters_ge2_after": int(f.loc[f.sb >= 2, "lb"].nunique()),
        }
    )
clusters = pd.DataFrame(rows)
clusters.to_csv(OUT / "clusters.csv", index=False)

# 2. registry matches
tmpl = q(f"""
    WITH b AS (SELECT file_sha, any_value(match_kind) kb, max(est_jaccard) jb FROM read_parquet({p(BEFORE, 's2_template')}) GROUP BY 1),
         a AS (SELECT file_sha, any_value(match_kind) ka, max(est_jaccard) ja FROM read_parquet({p(AFTER, 's2_template')}) GROUP BY 1)
    SELECT coalesce(kb, 'missing') AS before, coalesce(ka, 'missing') AS after, count(*) n
    FROM b FULL JOIN a USING (file_sha) GROUP BY 1, 2 ORDER BY 1, 2
""")
tmpl.to_csv(OUT / "template_match_kind.csv", index=False)

# 3. labels v2.1
lab = q(f"""
    SELECT b.label AS before, a.label AS after, count(*) n
    FROM read_parquet({p(BEFORE, 's3_labels_v2_1')}) b JOIN read_parquet({p(AFTER, 's3_labels_v2_1')}) a USING (file_sha)
    GROUP BY 1, 2 ORDER BY 1, 2
""")
lab.to_csv(OUT / "labels_v21_transitions.csv", index=False)
counts = q(f"""
    WITH b AS (SELECT label, count(*) n FROM read_parquet({p(BEFORE, 's3_labels_v2_1')}) GROUP BY 1),
         a AS (SELECT label, count(*) n FROM read_parquet({p(AFTER, 's3_labels_v2_1')}) GROUP BY 1)
    SELECT label, b.n AS before, a.n AS after, a.n - b.n AS change FROM b FULL JOIN a USING (label) ORDER BY label
""")
counts.to_csv(OUT / "labels_v21_counts.csv", index=False)
basis = q(f"""
    WITH b AS (SELECT coalesce(templated_basis, 'none') basis, count(*) n FROM read_parquet({p(BEFORE, 's3_labels_v2_1')}) GROUP BY 1),
         a AS (SELECT coalesce(templated_basis, 'none') basis, count(*) n FROM read_parquet({p(AFTER, 's3_labels_v2_1')}) GROUP BY 1)
    SELECT basis, b.n AS before, a.n AS after, a.n - b.n AS change FROM b FULL JOIN a USING (basis) ORDER BY basis
""")
basis.to_csv(OUT / "templated_basis_counts.csv", index=False)
conf = q(f"""
    SELECT b.label = a.label AS same_label, b.confidence = a.confidence AS same_confidence, count(*) n
    FROM read_parquet({p(BEFORE, 's3_labels_v2_1')}) b JOIN read_parquet({p(AFTER, 's3_labels_v2_1')}) a USING (file_sha)
    GROUP BY 1, 2 ORDER BY 1, 2
""")
conf.to_csv(OUT / "labels_v21_confidence.csv", index=False)


# 4. result tables, side by side
def side(name: str, key: str) -> pd.DataFrame | None:
    before, after = RES_BEFORE / name, ROOT / "results" / name
    if not (before.exists() and after.exists()):
        return None
    b, a = pd.read_csv(before), pd.read_csv(after)
    merged = b.merge(a, on=key, suffixes=("_before", "_after"), how="outer")
    merged.to_csv(OUT / f"side_{name}", index=False)
    return merged


for name, key in (
    ("s4_template_provenance.csv", "known_set"),
    ("s4_scaffold_overlap.csv", "scaffold_basis"),
    ("s4_scaffold_recall.csv", "known_set"),
    ("s3_baseline_summary.csv", None),
    ("s3_style_model.csv", None),
    ("s3_text_methods.csv", None),
    ("s4_stability.csv", None),
):
    if key is None:
        for which, base in (("before", RES_BEFORE), ("after", ROOT / "results")):
            path = base / name
            if path.exists():
                pd.read_csv(path).to_csv(OUT / f"{which}_{name}", index=False)
        continue
    side(name, key)

summary["clusters"] = clusters.to_dict(orient="records")
summary["label_counts"] = counts.to_dict(orient="records")
summary["templated_basis"] = basis.to_dict(orient="records")
changed = int(lab.loc[lab.before != lab.after, "n"].sum())
summary["labels_changed"] = changed
summary["labels_total"] = int(lab["n"].sum())
(OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

pd.set_option("display.width", 200)
print(clusters.T.to_string(header=False))
print("\nregistry match kind before -> after\n", tmpl.to_string(index=False))
print("\nv2.1 label counts\n", counts.to_string(index=False))
print("\ntemplated basis\n", basis.to_string(index=False))
print("\nlabel transitions (changed only)\n", lab[lab.before != lab.after].to_string(index=False))
print(f"\nlabels changed: {changed} of {int(lab['n'].sum())}")
print("\nlabel/confidence agreement\n", conf.to_string(index=False))
