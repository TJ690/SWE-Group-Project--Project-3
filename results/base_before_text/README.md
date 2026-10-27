# Base analysis (before text)

Frozen 2026-10-25. This is the reference point for every text-based result that follows (T42 style model, T43 lean, T52 no-history text).
The labels here use **commit, copy, cluster and registry evidence only**; no wording or style feature decides a label.

## What is frozen

Copies of the label files, with their manifests, in `src/data/derived/base_before_text/` (data folder, not in git):

| File | Built (UTC) | Code commit | Config hash | Rows |
|---|---|---|---|---|
| `s3_labels_v1.parquet` | 2026-10-25T06:02:19+00:00 | `ee67f60` | `25fdf5268dc7` | 1,877,981 |
| `s3_labels_v2.parquet` | 2026-10-25T08:48:24+00:00 | `ee67f60` | `4848949d138c` | 1,877,981 |
| `s3_labels_v2_1.parquet` | 2026-10-25T08:49:09+00:00 | `ee67f60` | `4848949d138c` | 1,877,981 |

State of the code when frozen: T44 (v2 cluster basis uses the 0.80 threshold) was applied before v2 was built. T45 (repo context) and T46 (skill type v1) were **not** yet rebuilt, so `s2_repo` and `s2_types` are still the earlier versions; no label reads them.

## Summary tables (this folder)

| File | Content |
|---|---|
| `label_counts.csv` | label counts and % for v1, v2, v2.1 |
| `v2_1_label_by_confidence.csv` | v2.1 label x confidence |
| `v2_1_label_by_history.csv` | v2.1 label x `history_fetched` |
| `v2_1_templated_basis.csv` | basis of each `highly_templated` label |
| `v2_1_top_reasons.csv` | 8 most common reasons per v2.1 label |
| `v2_1_flags.csv` | v2.1 flag totals |
| `v1_v2_v2_1_transitions.csv` | how each skill moved across versions |

## Headline numbers (v2.1, 1,877,981 distinct skills)

| Label | Skills | % |
|---|---|---|
| insufficient_evidence | 1,374,553 | 73.19 |
| highly_templated | 299,673 | 15.96 |
| agent_assisted | 142,795 | 7.60 |
| likely_human | 60,960 | 3.25 |

- 1,186,459 of the `insufficient_evidence` skills (86%) have **no commit history** (`history_fetched = false`). Only text can say anything about them; that is the target of T52.
- v2.1 moved 64,662 skills from `insufficient_evidence` to `agent_assisted` (disclosed trailer inside a bulk install or on the last commit only), all at low confidence.
- Templated basis: cluster 235,401; registry_near 39,447; copy_count 18,344; registry_exact 6,481.

## Rules for later comparisons

- Compare any text result against these files, not against labels rebuilt later.
- If a label file is rebuilt with different code or config, re-freeze under a new dated folder; do not overwrite this one.
