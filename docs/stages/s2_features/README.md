# s2. Features: per-skill evidence

**Code:** `src/skillsig/s2_features/` · **Tests:** `tests/s2_features` · **Run:** [EXECUTION_STEPS.md § s2](../../EXECUTION_STEPS.md#s2-features-stage)

Measures each skill without deciding anything. Each step writes one table of one kind of evidence, keyed by `file_sha` (or by repository for 2.3). The labels in s3 read these tables; several are measured only for analysis and the text models, and are not used by the current label.

This folder has one file per step, named after its `--only` module.

---

## The nine steps

| Step | File | Command | Measures | Used by the v2.1 label? |
|---|---|---|---|---|
| 2.1 | [structure.md](structure.md) | `features --only structure` | Length, headings, lists, code, tables, links, front-matter fields | No (text models in 3.5 / 3.6) |
| 2.2 | [commit.md](commit.md) | `features --only commit` | AI trailers, bot authors, batch size, message kind, revision history | **Yes** |
| 2.3 | [repo_context.md](repo_context.md) | `features --only repo_context` | Repository size, shared and registry shares, kind (collection / agent tooling / project) | No (context, spot-check sheets) |
| 2.4 | [skill_type.md](skill_type.md) | `features --only skill_type` | Topic of the skill (14 keyword types) | No (context only) |
| 2.5 | [wording.md](wording.md) | `features --only wording` | 13 wording measures and the description idiom | No (text models) |
| 2.6 | [dedup.md](dedup.md) | `dedup` | Near-duplicate clusters (MinHash) at similarity 0.8 and 0.6 | **Yes**: cluster size |
| 2.7 | [template_sim.md](template_sim.md) | `features --only template_sim` | Nearest registry version: exact, near or none | **Yes**: registry bases |
| 2.8 | [specificity.md](specificity.md) | `features --only specificity` | Share of rare identifiers (project-specific writing) | No (text models) |
| 2.9 | [scaffold.md](scaffold.md) | `features --only scaffold` | Structural templating: placeholders, outlines, masked copies, shared lines | No (checked in 4.5 first) |

---

## Order and dependencies

```
P.1 skill_features ─────► 2.1 structure
artifacts ──────────────► 2.2 commit
P.1 repo_profile, 1.3 ──► 2.3 repo_context ─► 2.4 skill_type
artifacts ──────────────► 2.5 wording
artifacts ──────────────► 2.6 dedup ─┬─► 2.7 template_sim  (+ 1.2, 1.3, clones)
                                     └─► 2.8 specificity
artifacts, siblings ────► 2.9 scaffold
```

- 2.3 needs s1 step 1.3; 2.4 needs 2.3.
- 2.7 needs s1 steps 1.2 and 1.3 and the 2.6 signatures.
- 2.8 needs the 2.6 clusters. **Rerun 2.8 whenever 2.6 is rebuilt** (see [Rebuilding specificity](#rebuilding-specificity)).
- 2.1, 2.2, 2.5 and 2.9 depend only on the dataset and can run in any order.

---

## Populations

| Population | Rows | Steps |
|---|---|---|
| All distinct skills | 1,877,981 | 2.2 commit, 2.4 skill_type |
| Readable distinct skills (text, ≥ 200 body characters, not a pointer or redirect stub) | 1,840,872 | 2.1, 2.5, 2.6 (×2 thresholds), 2.7, 2.9 |
| Readable skills with at least one identifier | 1,734,331 | 2.8 |
| Repositories | 282,200 | 2.3 |

The readable filter is defined once ([stages README § 4](../README.md#4-one-readable-filter-defined-once)) and reused by every text step.

---

## Outputs

| Step | File | Rows | Size / notes |
|---|---|---|---|
| 2.1 | `src/data/derived/s2_structure.parquet` | 1,840,872 | |
| 2.2 | `src/data/derived/s2_commit.parquet` | 1,877,981 | |
| 2.3 | `src/data/derived/s2_repo.parquet` | 282,200 | |
| 2.4 | `src/data/derived/s2_types.parquet` | 1,877,981 | |
| 2.4 | `results/s2_skill_type_by_project.csv` | 4,136 | In git |
| 2.5 | `src/data/derived/s2_wording.parquet` | 1,840,872 | |
| 2.6 | `src/data/derived/s2_clusters.parquet` | 3,681,744 | One row per skill per threshold |
| 2.6 | `src/data/derived/s2_minhash_shards/` | | Signatures, 1.3 GB, resumable |
| 2.7 | `src/data/derived/s2_template.parquet` | 1,840,872 | |
| 2.8 | `src/data/derived/s2_specificity.parquet` | 1,734,331 | |
| 2.8 | `src/data/derived/s2_specificity_identifiers/` | | Pass-1 parts, 738 MB, resumable |
| 2.9 | `src/data/derived/s2_scaffold.parquet` | 1,840,872 | |
| 2.9 | `results/s2_scaffold_top_outlines.csv` | 200 | In git |

Each output has a `.manifest.json` beside it. A step is skipped when the inputs its manifest lists, and the config, are unchanged:

| Step | Inputs fingerprinted |
|---|---|
| 2.1 | `generated_data/skill_features.parquet` |
| 2.2 | `artifacts` |
| 2.3 | `generated_data/repo_profile.parquet`, `artifacts`, `mining_runs`, `s1_registry_matches` |
| 2.4 | `artifacts`, `s2_repo` |
| 2.5 | `artifacts` |
| 2.6 | `artifacts` (plus the run settings: sample, limit, fraction) |
| 2.7 | `s1_registry_versions`, `s1_registry_matches`, the 2.6 shards, the clones, the registry YAML |
| 2.8 | `artifacts`, `s2_clusters` |
| 2.9 | `artifacts`, `artifact_siblings` |

Every step also adds `s0_devsample_ids.parquet` in dev mode.

---

## Full data vs dev sample

Every step keeps only the 50,000 sampled skills in dev mode, but they differ in what they **count across**. Where a feature is a count over other skills, dev mode can either keep counting the whole population or count only inside the sample:

| Step | Rows kept in dev mode | Counts across | Dev values equal full values for the same skill? |
|---|---|---|---|
| 2.1 structure | Sampled | (per skill) | Yes |
| 2.2 commit | Sampled | **Whole population** (batch size) | Yes |
| 2.3 repo_context | **Every repository** | Whole population | Yes |
| 2.4 skill_type | Sampled (summary counts all their copies) | (per skill) | Yes |
| 2.5 wording | Sampled | (per skill) | Yes |
| 2.6 dedup | Sampled | **Sample only** | **No**: clusters form only among sampled skills, so sizes are far smaller and the `cluster` rule (≥ 10) rarely fires |
| 2.7 template_sim | Sampled | Registry versions (not sampled) | Yes, given the same signatures |
| 2.8 specificity | Sampled | **Whole population** for identifier frequency, but grouped by the 2.6 clusters, which are sample-only in dev | Approximately |
| 2.9 scaffold | Sampled | **Sample only** (owners per outline, front-matter keys, layout, masked body; shared-line frequency) | **No**: spread counts are far smaller, so most bases fire less |

So a dev run is a fast check that the code works and the rules behave sensibly, not a small-scale copy of the full result for clusters and scaffold signals.

Dev and full runs write the **same file names**. A dev run of 2.6 also overwrites the 1.3 GB signature shards, so the next full run of 2.6 has to recompute every signature. See [s0 § Dev and full outputs share one set of file names](../s0_intake/README.md#dev-and-full-outputs-share-one-set-of-file-names).

---

## Who uses s2 outputs

| Table | s3 labels | s3 text models (3.5 / 3.6) | s4 validation |
|---|---|---|---|
| `s2_structure` | — | Features | — |
| `s2_commit` | 3.1 v1, 3.2 v2, 3.3 v2.1, 3.4 baselines | Defines positives / negatives and subsets | 4.4 stability, 4.6 spot-check sheets |
| `s2_repo` | — (3.2 requires it but does not read it) | — | 4.6 spot-check sheets |
| `s2_types` | — (3.2 requires it but does not read it) | — | — |
| `s2_wording` | — (3.2 requires it but does not read it) | Features | — |
| `s2_clusters` | 3.2 v2 `cluster` basis | Cross-validation groups | 4.2, 4.4, 4.5, 4.6 (one per cluster) |
| `s2_template` | 3.2 v2 `registry_exact` / `registry_near` | — | 4.2 E2, 4.3 E3, 4.4, 4.5 |
| `s2_specificity` | — | Features | — |
| `s2_scaffold` | — | — | 4.5 scaffold check |

---

## Testing

| File | Tests | Covers |
|---|---|---|
| `test_structure.py` | 8 | Bad or missing front matter never raises; indented `#` lines in code are not headings; Python matches the SQL on a fixture; **parity on 500 real rows** (`-m data`); run, skip and dev filtering |
| `test_commit.py` | 17 | Trailer detection (only on `Co-authored-by`; subject mentions don't count); extended detector is a superset and leaves the original unchanged; message and author classes; batch size, NULLs without history, full-population batches in dev mode |
| `test_repo_context.py` | 15 | Name flags ignore the owner; `hub` inside `github` is not a collection word; benchmark-like names; kind priority and config cutoffs; output shares |
| `test_skill_type.py` | 20 | Rule order cases (meta beats office, testing beats review); `stress test` is not testing; bare `design` is not frontend; body fallback reads only 500 characters; all-matches list; old v0 rules kept for comparison |
| `test_wording.py` | 11 | Hand-computed values on a fixture; MATTR windows; empty body gives zeros; non-English threshold; burstiness and em-dash rate count prose only; idiom matches 1.2 |
| `test_minhash_cluster.py` | 8 | Near-identical texts cluster at 0.8; unrelated texts and same-front-matter/different-body texts do not; deterministic ids; both thresholds written; shard resume |
| `test_template_sim.py` | 6 | Exact blob → exact; edited paragraph → near; unrelated → none; missing inputs name their commands |
| `test_specificity.py` | 6 | Each identifier kind extracted; null below 3 identifiers; frequency counts clusters, not copies; dev scores only the sample but frequency uses everything |
| `test_scaffold.py` | 14 | Placeholders skip code and links; generator markers; outline canonicalisation; masked hash matches renamed copies; outline families join near and keep far apart, skip oversized buckets; winnowing finds planted text; basis order |
| `test_lexicons.py` | 5 | Fightin' Words marker lexicon used by 3.5 (planted word ranks first, vendor words excluded) |

110 tests; all run on small fixtures except the structure parity test.

```bash
python -m pytest -q tests/s2_features
```

**Gaps:** no full-data regression test for any s2 table (row counts, share of clusters, template split); no test that dev-mode cluster and scaffold counts differ from full; the "headings inside code fences" test only covers indented `#` lines.

---

## Rebuilding specificity

Specificity counts how many 2.6 clusters use each identifier, so it must be rebuilt whenever 2.6 is. Clear the resumable parts first so nothing from the earlier run is reused, then rerun 3.5 and 3.6, which read it:

```bash
rm -rf src/data/derived/s2_specificity_identifiers src/data/derived/s2_specificity_buckets_c*
python -m skillsig features --only specificity --force
python -m skillsig label --only style_model --force
python -m skillsig label --only text_methods --force
```

This was last done on 2026-10-27, after the full-body cluster rebuild; see [specificity.md](specificity.md#rebuild-history) for what changed.
