# 2.7 Registry similarity (`template_sim.py`)

**Command:** `python -m skillsig features --only template_sim` · **Tests:** `tests/s2_features/test_template_sim.py` · [Back to s2](README.md)

## What it does

For each readable skill, finds the nearest version published by any mined registry (s1) and says how close it is: an exact copy, a near copy, or neither.

## How it works

1. **Registry index.** Reads every registry version (1.2), takes its text from the clone (mirrors resolve to their origin's clone; missing blobs are downloaded by hash), and builds a MinHash LSH index with the **same** normalisation, shingle width, permutations and seed as 2.6.
2. **Candidates.** The index is built at `min(templated_jaccard_min, lsh_threshold_low)` = min(0.8, 0.6) = **0.6**, so boundary pairs are still retrieved.
3. **Skill signatures** are read from the 2.6 shards, not recomputed.
4. **Classify** each skill:

| `match_kind` | Rule |
|---|---|
| `exact` | `file_sha` is a matched registry blob (1.3); `est_jaccard = 1.0` |
| `near` | Not exact, and the best candidate's estimated similarity is ≥ `templated_jaccard_min` (0.8) |
| `none` | Otherwise; the best candidate and its score are still recorded if one was found |

5. **Lineage** of the nearest blob: the earliest lineage that published it, overridden by the origin lineage from 1.3 when the blob was matched.

## Output

`src/data/derived/s2_template.parquet`: 1,840,872 rows, columns `file_sha, match_kind, nearest_blob, nearest_lineage_id, est_jaccard`.

| `match_kind` | Skills | Median `est_jaccard` |
|---|---|---|
| `exact` | 6,599 | 1.0 |
| `near` | 42,104 | 0.992 |
| `none` | 1,792,169 | — |

Leaving out the 118 that are the registries' own files gives the two references that 4.2 (E2) and 4.5 use, reported separately: **6,481 exact registry matches**, an identity reference, and **42,104 near candidates**. The near candidates are chosen by MinHash similarity, the same mechanism the cluster rule uses, so they are not an independent check of that rule. Pooling the two into one set of 48,585, as earlier versions did, is not a valid validation denominator. (`exact` has 6,599 readable skills, fewer than the 6,719 matched blobs, because some matched blobs are not readable.) Near copies are mostly very close: the median near match shares about 99% of its shingles with the registry version.

**How 3.2 uses it.** `exact` and `near` become the `registry_exact` and `registry_near` templated bases. 3.2 also computes `registry_origin`: true when the skill's own repository is the repository of `nearest_lineage_id`, i.e. the registry's own file, which is then not called templated.

## Design decisions

- **Why reuse 2.6's signatures and settings?** Same numbers, no second signature pass, and registry similarity is directly comparable with cluster similarity.
- **Why search at 0.6 and decide at 0.8?** A looser search misses fewer real matches; the decision still uses the configured threshold, and the score is kept so near misses can be inspected and 4.4 can test the stricter 0.9 setting.
- **What does 0.8 mean in practice?** 4.3 shows it survives about 5% of lines replaced (78% still detected) but not 10% (16%). "Near" means "lightly edited copy", not "inspired by".
- **Why keep the nearest blob for `none`?** It shows how close the best registry version was, so near misses can be inspected.

## Full data vs dev sample

Dev mode scores only the sampled skills against **all** registry versions; each skill's result is the same as in a full run, provided the 2.6 shards were built in the same mode.

## Tests (6)

- An exact blob is `exact`; a skill with one edited paragraph is `near`; an unrelated skill is `none`.
- Missing registry versions, matches or shards each name the command to build them.

## Limits

- Only the 31 mined registries; a copy of an unlisted publisher is `none` here (2.6 clusters still catch widely copied ones).
- A copy with a rewritten section falls below 0.8 and is `none`.
- The first run needs network access to download registry file contents into the blobless clones.
- **The 0.8 cut is applied here, at build time.** 3.2 only accepts rows already marked `near`, so lowering `templated_jaccard_min` later has no effect unless this step is rerun. In particular, 4.4's "Jaccard 0.7" setting changes nothing (its rows in `results/s4_stability.csv` show 0.0 movement): only the stricter 0.9 setting is really tested.
