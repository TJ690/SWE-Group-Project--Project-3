# 2.6 Near-duplicate clusters (`minhash_cluster.py`)

**Command:** `python -m skillsig dedup` · **Tests:** `tests/s2_features/test_minhash_cluster.py` · [Back to s2](README.md)

## What it does

Groups readable skills whose bodies are nearly identical into **clusters**, at two similarity thresholds: 0.8 (used by the label) and 0.6 (used to check sensitivity). Keeps each skill's signature for 2.7 and 2.8.

## How it works

1. **Normalise** each body: remove front matter, lower-case, collapse whitespace, cut at `minhash.body_max_chars` (1,000,000, above the longest body of 355,781 characters, so the whole body is used).
2. **Shingles:** overlapping 5-word windows (`minhash.shingle_words`).
3. **Signature:** 128 MinHash values (`minhash.num_perm`), seeded by `seed`. The permutations are the same as the `datasketch` library's, computed in NumPy for speed.
4. **Shards:** signatures are written in blocks to `s2_minhash_shards/` with a `plan.json` recording the settings. A rerun with the same plan and inputs reuses finished shards, so an interrupted run resumes.
5. **LSH:** the 128 values are split into bands (sizes chosen by `datasketch` for each threshold). Skills whose values agree on a whole band are candidates; identical signatures are joined first.
6. **Union–find** joins candidates into clusters. `cluster_id` is the smallest member's index; `nn_sim_est` is the best estimated similarity to any neighbour.

The estimated similarity is the share of the 128 values two signatures have in common, which estimates the Jaccard similarity of their shingle sets.

## Outputs

`src/data/derived/s2_clusters.parquet`: 3,681,744 rows, one per readable skill per threshold, columns `file_sha, cluster_id, cluster_size, threshold, nn_sim_est`.

| | Threshold 0.8 | Threshold 0.6 |
|---|---|---|
| Skills in a cluster of 2+ | 602,662 (32.7%) | 693,250 (37.7%) |
| Skills in a cluster of 10+ (the `cluster` templated rule) | 277,852 (15.1%) | 344,328 (18.7%) |
| Clusters of 2+ | 130,255 | 138,731 |
| Largest cluster | 59,851 | 59,858 |

`src/data/derived/s2_minhash_shards/`: 1.3 GB of signatures, read by 2.7. (4.3 recomputes its own signatures with the same settings.)

## Design decisions

- **Why MinHash + LSH?** Comparing every pair of 1.8M texts is about 1.7 × 10¹² comparisons. MinHash estimates similarity from 128 numbers, and LSH only compares skills that collide in a band: close to linear time.
- **Why 5-word shingles?** Short enough that a small edit changes few shingles, long enough that common phrases ("use this skill when") do not make unrelated skills look alike.
- **Why the full body?** Until 2026-10-26 MinHash read only the first 5,000 characters, and about 48% of skills are longer. Skills sharing an opening but differing later looked identical, and late edits were invisible. Reading the full body moved clusters of 10+ at 0.8 from 274,085 to 277,852 skills (8,403 lost that status, 12,170 gained it) and grew the 4.2 known-copy set from 45,928 to 48,585. Before/after tables: `results/base_before_fullbody/`.
- **Why two thresholds?** 0.8 is the label threshold; 0.6 lets 4.4 show how much the templated share depends on the choice.
- **Why union–find?** A family of lightly edited copies is one family even if the first and last members differ more: A≈B and B≈C put A, B, C together. The cost is **chaining**: one cluster has 59,851 members, far more than any single template could explain. `nn_sim_est` is stored so a reader can see how close each member actually is.
- **Why resumable shards?** A long step should survive a crash or a sleeping laptop.

## Full data vs dev sample

In dev mode **clusters are formed only among the sampled skills**. A skill with 500 near-copies in the full data may have none in the sample, so dev cluster sizes are far smaller and the `cluster` templated rule (size ≥ 10) rarely fires. Dev mode also overwrites the shards, so the next full run recomputes every signature.

The module also accepts `limit` and `sample_fraction` (a share per location class) for experiments from Python; the CLI does not expose them.

## Tests (8)

- Near-identical texts cluster at 0.8; unrelated texts do not; texts with identical front matter but different bodies do not (front matter is removed).
- Cluster ids are deterministic for the same input order.
- A run writes both thresholds and the shards, and skips when unchanged; `limit`, `sample_fraction` and shard resume work.
- Missing inputs name their commands.

## Limits

- **Chaining** makes the largest clusters very large; `cluster_size` overstates how many skills share one template.
- LSH is probabilistic: pairs near the threshold are sometimes missed or included.
- Front matter is ignored, so two skills with the same body and different names are one cluster (intended).
