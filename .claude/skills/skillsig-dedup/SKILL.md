---
name: skillsig-dedup
description: Run skillsig pipeline step 5a - group near-duplicate SKILL.md texts with MinHash/LSH into src/data/derived/s2_clusters.parquet. Takes about 30 minutes on full data. Use when asked to run dedup, near-duplicate clustering, MinHash, or rebuild clusters.
---

# Dedup: near-duplicate clusters (step 5a)

Follow the run rules and guardrails in `AGENTS.md`. The user gives a mode (`dev` or `full`; ask if missing).

```bash
.venv/bin/python -m skillsig dedup [--dev-sample] [--force] > logs/dedup_<timestamp>.log 2>&1
```

- **Input:** `src/data/gitskills_data/data/artifacts/*.parquet` (step 0); a `dev` run also needs the dev sample from step 2.
- **Output:** `src/data/derived/s2_clusters.parquet`, shards in `src/data/derived/s2_minhash_shards/`.
- Full data takes about 30 minutes and a lot of memory: run in the background. On machines with under 8 GB RAM set `SKILLSIG_WORKERS=2 SKILLSIG_DUCKDB_MEMORY=1500MB`.
- In `dev` mode clusters are built inside the sample only, so they are far smaller than full clusters and the `cluster` templated rule rarely fires.
- After a rebuild, steps 5b–5d (`skillsig-features`) and everything in `skillsig-label` read the new clusters and should be rerun with `force`. Tell the user.
- Settings (shingle size, permutations, bands, Jaccard 0.8) are in `config/default.yaml`.

Details: `docs/PIPELINE_RUNBOOK.md` step 5a; design note: `docs/stages/s2_features/dedup.md`.
