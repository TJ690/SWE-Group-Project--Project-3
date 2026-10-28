# Before full-body MinHash (5,000-character cut vs full body)

Frozen 2026-10-26. Until then, MinHash read only the first 5,000 characters of each skill body (`minhash.body_max_chars: 5000`). It now reads the full body (`body_max_chars: 1000000`; the longest body is 355,781 characters). About 48% of skills are longer than 5,000 characters.

## What is frozen

Copies of the outputs built with the cut, with their manifests, in `src/data/derived/base_before_fullbody/` (data folder, not in git):

| File | Built (UTC) | Code commit | Config hash |
|---|---|---|---|
| `s2_clusters.parquet`, `s2_template.parquet`, `s3_labels_v2.parquet`, `s3_labels_v2_1.parquet`, `s3_baseline_labels.parquet`, `s3_style_scores.parquet`, `s3_text_methods_oof.parquet` | 2026-10-25 | `ee67f60` | `4848949d138c` |

The result tables built with the cut are in this folder. The rebuilt outputs (2026-10-26, config hash `9c1fdd28dfc4`) are in their usual places. Rerun order: `dedup`, `features --only template_sim`, `label --only rules_v2`, `rules_v2_1`, `baselines`, `style_model`, `text_methods`, `evaluate --only template_provenance`, `stability`, `edit_robustness`, `scaffold_check`, `sample --only spot_refresh`.

`compare_rerun.py` rebuilds every table in `comparison/`.

## Results

### Near-duplicate clusters (1,840,872 skills)

| | Threshold 0.8 | Threshold 0.6 |
|---|---|---|
| Near-duplicate (cluster of 2+): before → after | 595,911 → 602,662 | 686,254 → 693,250 |
| Lost / gained near-duplicate status | 14,001 / 20,752 | 9,549 / 16,545 |
| Cluster of 10+ (the `cluster` templated rule): before → after | 274,085 → 277,852 | 341,569 → 344,328 |
| Lost / gained cluster-of-10 status | 8,403 / 12,170 | 7,318 / 10,077 |
| Skills whose cluster gained or lost any member | 174,329 (9.5%) | 178,873 (9.7%) |

The last row counts every member of a cluster that changed at all, so one skill joining a cluster of 500 counts 501 times. The status rows are the ones that matter for labels.

### Registry matches (`s2_template`)

| Before → after | Skills |
|---|---|
| exact → exact | 6,599 |
| near → near | 38,097 |
| near → none | 1,350 |
| none → near | 4,007 |

The known-copy set used for recall (E2) grows from 45,928 to 48,585 skills.

### Labels (v2.1, 1,877,981 skills)

| Label | Before | After | Change |
|---|---|---|---|
| highly_templated | 299,673 | 303,517 | +3,844 |
| agent_assisted | 142,795 | 142,667 | −128 |
| likely_human | 60,960 | 60,930 | −30 |
| insufficient_evidence | 1,374,553 | 1,370,867 | −3,686 |

18,866 skills (1.0%) change label. The largest moves are insufficient_evidence → highly_templated (10,776) and highly_templated → insufficient_evidence (7,090). Templated basis: `registry_near` +2,657, `cluster` +1,415, `copy_count` −228.

### Checks and models

| Check | Before | After |
|---|---|---|
| E2 recall of copy + cluster rules on known registry copies | 87.27% (n = 45,928) | 87.30% (n = 48,585) |
| Scaffold recall on known registry copies | 97.07% | 97.37% |
| Scaffold flags not caught by any old rule | 116,992 | 113,073 |
| Style model mean AUC (text only) | 0.606 | 0.604 |
| Text methods AUC (ensemble) | 0.711 | 0.710 |
| Default highly_templated share (stability) | 16.06% | 16.26% |

## Reading

- The cut mattered at the margin. About 1% of labels and about 1% of cluster-of-10 decisions change, in both directions, with a small net gain for highly_templated (+0.2 points).
- Changes go both ways, as the dev-sample check predicted. Long skills with a shared opening but different bodies drop out of clusters; copies with a rewritten opening join them.
- Recall, the stability shares and the text models are unchanged within noise. No earlier conclusion depends on the cut.
- The E6 spot-check sample was kept (same 120 skills). Its key now holds the new labels; one sampled skill's v2.1 label changed (`label_v21_at_draw` keeps the old one).
