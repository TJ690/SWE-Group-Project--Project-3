# Pipeline runbook: every step, its input, output, data size and run time

This is the full run of the `skillsig` pipeline, from downloading GitSkills to scoring the spot-check. Each step lists the command, what it reads, what it writes, how much data it processes, and how long it took on the last full run (a MacBook, 2026-10-25 and 2026-10-26).

All commands run from the repository root with the project environment active (`source .venv/bin/activate`, or prefix with `.venv/bin/`).

## How to run any step

```bash
python -m skillsig <stage> [--only <module>] [--dev-sample] [--force]
```

| Flag | Effect |
|---|---|
| `--only <module>` | Run one module of the stage (most stages need this; see the tables) |
| `--dev-sample` | Run on the 50,000-skill dev sample instead of the full data (about 2.6% of skills; useful for testing) |
| `--force` | Rebuild even if the inputs and config are unchanged |

Every output gets a `<file>.manifest.json` next to it with its inputs' fingerprints, the config hash, the code commit and the row count. A step is skipped when nothing it depends on changed. **Code changes do not count**: after editing code, use `--force`.

## The data

| Table | Rows | Size on disk | What it is |
|---|---|---|---|
| `artifacts` | 3,797,117 | 6.0 GB | One row per `SKILL.md` file found on GitHub |
| ↳ distinct skills (`dedup_primary = 1`) | **1,877,981** | | One representative per distinct text (`file_sha`); the unit of analysis |
| ↳ readable distinct skills | **1,840,872** | | Distinct skills with text, at least 200 body characters, not a redirect stub |
| ↳ with commit history | 458,137 | | 24.4% of distinct skills |
| `artifact_siblings` | 7,264,865 | 6.5 GB | Files and folders stored next to each skill |
| `repos` | 282,200 | 19 MB | Repository metadata |
| `mining_runs` | 7 | | Collection runs (gives the snapshot date) |
| Registry clones | 31 repositories | 271 MB | Git history of 31 well-known skill collections |

Most steps run on the distinct skills (1,877,981) or the readable ones (1,840,872).

## Overview

```
 0  Setup and download ─► raw Parquet (12.5 GB)
 1  SQL build scripts  ─► generated_data/ (4 tables)
 2  data               ─► schema check, dev sample
 3  registry           ─► versions, matches, lineages of 31 registry repos
 4  features           ─► per-skill signals (structure, commit, repo, type, wording)
 5  dedup + template   ─► near-duplicate clusters, registry near-matches, specificity, scaffold
 6  label              ─► v1 → v2 → v2.1 labels, baselines, style and text models
 7  evaluate           ─► E1–E5 checks, scaffold check
 8  sample             ─► E6 spot-check sheets (spot_A.xlsx, spot_B.xlsx, trailer_check.csv)
 9  (people)           ─► annotate, save as *_done.xlsx
10  score              ─► E6 precision, confusion, agreement
```

---

## Step 0: setup and download

| | |
|---|---|
| **Commands** | `make setup` (creates `.venv`, installs the package with dev tools), then `cd src/data && python download_data.py && cd ../..` |
| **Input** | Hugging Face dataset `mvaccargiu/gitskills` |
| **Output** | `src/data/gitskills_data/data/{artifacts,artifact_siblings,repos,mining_runs}/*.parquet` (12.5 GB; never committed) |
| **Time** | Long (network bound); re-running resumes |

## Step 1: SQL build scripts (generated tables)

These build the tables the EDA notebooks and three feature steps read. Run each once from the repository root after `mkdir -p src/data/generated_data`:

```bash
python -c "import duckdb; duckdb.connect().execute(open('db/content_and_structure.sql').read())"
python -c "import duckdb; duckdb.connect().execute(open('db/agent_ecosystem.sql').read())"
python -c "import duckdb; duckdb.connect().execute(open('db/repos.sql').read())"
python -c "import duckdb; duckdb.connect().execute(open('db/siblings_data.sql').read())"
```

| Script | Output (in `src/data/generated_data/`) | Size | Read by |
|---|---|---|---|
| `content_and_structure.sql` | `skill_features.parquet` | 63 MB | Step 4 `structure` |
| `agent_ecosystem.sql` | `file_agents.parquet` | 209 MB | Step 6 `baselines` |
| `repos.sql` | `repo_profile.parquet` | 16 MB | Step 4 `repo_context` |
| `siblings_data.sql` | `sibling_files.parquet` | 189 MB | EDA notebooks only |

Input for all four: the raw Parquet from step 0.

## Step 2: data

| Command | Input | Output | Rows | Time |
|---|---|---|---|---|
| `python -m skillsig data` | Raw Parquet | Schema check (fails on missing columns); `src/data/derived/s0_devsample_ids.parquet`, the stratified dev sample used by `--dev-sample` | 50,000 | 9 s |

## Step 3: registry (31 skill collections)

`python -m skillsig registry` alone does nothing; run each module with `--only`, in this order. The clones are not re-fetched unless `FETCH=1` is passed to `scripts/run_full.sh` (fetching would change the registry snapshot every label is built on).

| # | Command | Input | Output (`src/data/derived/` unless shown) | Rows | Time |
|---|---|---|---|---|---|
| 3a | `python -m skillsig registry --only mine_git` | `config/registry_sources.yaml`; clones in `src/data/registry_repos/` | `s1_versions_raw.parquet` (every SKILL.md version in git history); `s1_commit_trailers.parquet` (AI co-author lines in registry commits) | 16,485; 50,941 | 8 s |
| 3b | `python -m skillsig registry --only versions` | 3a outputs | `s1_registry_versions.parquet` (one row per distinct version, ordered) | 15,254 | 9 s |
| 3c | `python -m skillsig registry --only match` | 3b; `artifacts` | `s1_registry_matches.parquet` (dataset skills byte-identical to a registry version); `s1_lineages.parquet` | 6,719; 2,274 | 9 s |
| 3d | `python -m skillsig registry --only changes` | 3b; clone file contents | `s1_registry_changes.parquet` (substantive vs cosmetic edits between versions) | 12,877 | 4.4 min |
| 3e | `python -m skillsig registry --only discover_popular` | `artifacts`, `repos`, 3c | `results/s1_popular_candidates.csv` (most widely copied skills outside the registry) | 200 | 7 s |

## Step 4: features (per-skill signals)

| # | Command | Input | Output (`src/data/derived/` unless shown) | Rows (data processed) | Time |
|---|---|---|---|---|---|
| 4a | `python -m skillsig features --only structure` | `generated_data/skill_features.parquet` | `s2_structure.parquet`: length, headings, lists, code blocks, links, front-matter fields | 1,840,872 readable skills | 1 s |
| 4b | `python -m skillsig features --only commit` | `artifacts` first/last commit columns | `s2_commit.parquet`: AI trailers, bot class, batch size, message class | 1,877,981 distinct skills | 33 s |
| 4c | `python -m skillsig features --only repo_context` | `generated_data/repo_profile.parquet`, `artifacts`, 3c | `s2_repo.parquet`: repo kind (collection / agent tooling / project), share measures | 282,200 repos | 7 s |
| 4d | `python -m skillsig features --only skill_type` | `artifacts` name, description, body; 4c | `s2_types.parquet` (keyword skill type); `results/s2_skill_type_by_project.csv` | 1,877,981; 4,136 | 8.3 min |
| 4e | `python -m skillsig features --only wording` | `artifacts` text | `s2_wording.parquet`: wording features W1–W12, description idiom | 1,840,872 | 13.4 min |

## Step 5: near-duplicates, registry similarity, specificity, scaffold

Order matters: `template_sim` and `specificity` read the clusters and signatures that `dedup` writes.

| # | Command | Input | Output (`src/data/derived/` unless shown) | Rows (data processed) | Time |
|---|---|---|---|---|---|
| 5a | `python -m skillsig dedup` | `artifacts` text of readable skills (**full body** since 2026-10-26; was the first 5,000 characters) | `s2_clusters.parquet`: near-duplicate cluster per skill at MinHash thresholds 0.8 and 0.6; `s2_minhash_shards/` (signatures, 1.3 GB) | 3,681,744 (1,840,872 × 2 thresholds) | 28 min (5,000-character cut); about 40 min (full body) |
| 5b | `python -m skillsig features --only template_sim` | 3b, 3c, clones, 5a signatures | `s2_template.parquet`: nearest registry version and estimated similarity per skill | 1,840,872 | 3.6–4.3 min |
| 5c | `python -m skillsig features --only specificity` | `artifacts` text (first 50,000 characters), 5a | `s2_specificity.parquet`: share of a skill's identifiers almost no other skill uses | 1,734,331 | 5 min |
| 5d | `python -m skillsig features --only scaffold` | `artifacts` text and owners, `artifact_siblings` | `s2_scaffold.parquet`: placeholders, outline families, front-matter keys, folder layout, masked body hash, shared-line fraction, customization, scaffold basis; `results/s2_scaffold_top_outlines.csv` | 1,840,872; 200 | 75 min |

Before a full `specificity` rebuild, delete its resumable parts: `rm -rf src/data/derived/s2_specificity_identifiers src/data/derived/s2_specificity_buckets_c*`.

## Step 6: labels and models

| # | Command | Input | Output (`src/data/derived/` unless shown) | Rows | Time |
|---|---|---|---|---|---|
| 6a | `python -m skillsig label --only rules_v1` | `artifacts`, 4b | `s3_labels_v1.parquet`: Sprint 1 label (copy count, trailer, bot, batch) | 1,877,981 | 9 s |
| 6b | `python -m skillsig label --only rules_v2` | 6a; 4a, 4b, 4c, 4d, 4e; 5a, 5b | `s3_labels_v2.parquet`: label, templated basis, confidence, reasons; `results/s3_v1_v2_crosstab.csv` | 1,877,981; 11 | 18–20 s |
| 6c | `python -m skillsig label --only rules_v2_1` | 6a, 6b, 4b, `artifacts` | `s3_labels_v2_1.parquet`: **the current label** plus independent flags; `results/s3_v2_v21_crosstab.csv` | 1,877,981; 10 | 17 s |
| 6d | `python -m skillsig label --only baselines` | 6a, 6b, 4b, `generated_data/file_agents.parquet` | `s3_baseline_labels.parquet` (single-signal baselines B0–B3b); `results/s3_baseline_summary.csv` | 1,877,981; 9 | 11 s |
| 6e | `python -m skillsig label --only style_model` | 6c, 4a, 4b, 4e, 5a, 5c, `artifacts` text | E4 style model: `s3_style_scores.parquet`; `results/s3_style_model.csv`, `.json`, `s3_style_top_words.csv` | 380,023 scored; 30 | 21–25 min |
| 6f | `python -m skillsig label --only text_methods` | 6c, 4a, 4b, 4e, 5a, 5c, `artifacts` text | E5 text-only methods: `s3_text_methods_oof.parquet`; `results/s3_text_methods.csv`, `.json`, `_top_words.csv` | 40,000 (20,000 agent + 20,000 human); 21 | 17 min |
| 6g | `python -m skillsig label --only text_score` | 6f, 6c, features | E5b text lean for no-history skills: `s3_text_scores.parquet` and summaries | **not run yet** | — |
| 6h | `python -m skillsig label --only evidence` | 6a, 6c, 4b, 5a, 5b, 5d | `s3_evidence.parquet`; `results/s3_evidence_{matrix,signals,legacy_crosstab}.csv`, `s3_evidence_summary.json` | 1,877,981 | command log |

Step 6h reports reuse and disclosed agent involvement independently; both may coexist.
It preserves historical labels, their confidence, and independent annotations. This
report assigns no human-authorship verdict. Its reuse evidence is broader than the
old templated threshold: sharing and cluster candidates start at two. See
[definitions](stages/s3_labeling/evidence.md). Run `python -m skillsig export`
afterward to generate `figures/report/f0_evidence_dimensions.png` with the other figures.

## Step 7: evaluations

| # | Command | Input | Output (`results/`) | Rows | Time |
|---|---|---|---|---|---|
| 7a | `python -m skillsig evaluate --only disclosure_recall` | 3a outputs | E1: `s4_disclosure_recall.csv`, `.json` (share of AI disclosures visible from first/last commit) | 8 | 2 s |
| 7b | `python -m skillsig evaluate --only template_provenance` | 5b, 6a, 5a, 6c | E2: exact-match recovery (6,481), near-candidate agreement (42,104), mixed row marked exploratory; `s4_template_provenance.csv`, `s4_template_unexplained.csv` | 3; 3 | 2–3 s |
| 7c | `python -m skillsig evaluate --only edit_robustness` | 5b, `artifacts` text | E3: `s4_edit_robustness.csv`, `figures/12_edit_robustness.png` | 10 | 23–35 s |
| 7d | `python -m skillsig evaluate --only stability` | 6a, 4b, 5b, 5a, 6c, `artifacts` | E5: `s4_stability.csv`, `.json` (label shares when one threshold moves) | 84 | 53 s |
| 7e | `python -m skillsig evaluate --only scaffold_check` | 5d, 6c, 5b, 6a, 5a; text of sampled rows | `s4_scaffold_overlap.csv`, `_recall.csv`, `_agent_confound.csv`, `_spot.csv` | 6; 3; 2; 150 | 14 s |

`python -m skillsig evaluate` without `--only` runs 7a–7d, then scores E6 if `spot_A_done.xlsx` exists. It does not run 7e.

## Step 8: spot-check sheets (E6)

### Separate paired content-reuse review

```bash
python -m skillsig sample --only template_review
```

Builds an evidence-aware review package in `results/annotation/template_review/`:
full paired texts, shared passages, direct body-shingle Jaccard and diffs in
`pairs.html`; answer sheet `template_pairs.csv`; private `key.parquet` and
`pair_evidence.json`. The target is 30 subjects per exact/near/cluster/scaffold
stratum, with subject owner/cluster caps and explicit shortfalls. Registry text is
read from verified local Git objects only. Existing packages are never overwritten.
Sampling and text coverage are recorded in `results/s4_template_review_design.json`.

This is separate from the original blind authorship annotations. Reviewers assess
material reuse and shared scaffold, with uncertainty kept explicit. After a returned
`template_pairs_done.csv` is saved beside the sheet, run
`python -m skillsig evaluate --only template_review` for stratified judgment counts
and intervals. See [the review protocol](report/TEMPLATE_EVALUATION.md). Human
judgments are required; the tool does not invent a precision result.

| Command | Input | Output (`results/annotation/`) | Rows | Time |
|---|---|---|---|---|
| `python -m skillsig sample` | 6c, 6b, 6d, 4b, 5a, 4c, `artifacts` | `spot_A.xlsx` (person A), `spot_B.xlsx` (person B, a subset), `trailer_check.csv`, `key.parquet` (answer key, never shown to annotators), `trailer_key.parquet`, `codebook.md`; `results/s4_spot_design.json` | 120 (30 per label); 40; 50 | 7–8 s |
| `python -m skillsig sample --only spot_refresh` | `key.parquet` and the same inputs | Keeps the drawn skills; rewrites the key with current labels, and the sheets with current text if they are still blank | 120 | 8 s |

`sample` never draws over existing sheets: move `spot_A.xlsx` aside to redraw. Use `spot_refresh` after relabelling, so annotation already started stays valid.

## Step 9: annotation (people)

Follow `docs/ANNOTATION_GUIDELINES.md` (also `.docx`).

| Who | Fills | Saves as (`results/annotation/`) |
|---|---|---|
| Person A | `spot_A.xlsx`: `your_label`, `your_confidence`, `notes` | `spot_A_done.xlsx` |
| Person B | `spot_B.xlsx` | `spot_B_done.xlsx` |
| Person C (optional) | a copy of `spot_A.xlsx`, any subset of rows | `spot_C_done.xlsx` |
| Anyone | `trailer_check.csv`: `is_ai_disclosure` (yes / no / unsure) | `trailer_check_done.csv` |

## Step 10: scoring (E6)

Both independent sheets are now included (2026-10-27). B's submitted workbook,
`spot_B_Terry.xlsx`, is preserved and has a byte-identical `spot_B_done.xlsx` copy
for scorer compatibility. The 40-item overlap has 21 agreements (52.5%; kappa 0.288).
See [the annotation review](report/ANNOTATION_REVIEW.md). The existing precision and
pipeline-confusion tables use A's 120 answers; they are not consensus labels.

Additional outputs are `results/s4_spot_rater_confusion.csv` (A × B),
`results/s4_spot_overlap_precision.csv` (each rater on the same overlap), and the private
`results/annotation/rater_disagreements.csv` (original answers and notes, no adjudication).
With `spot_C_done.xlsx` (Jayden, 60 of 120 skills, added 2026-10-27; a byte-identical copy of
`spot_A_Jayden.xlsx`) the scorer also writes `s4_spot_rater_pairs.csv`,
`s4_spot_rater_pair_confusion.csv`, `s4_spot_rater_c_precision.csv` and the private
`rater_c_disagreements.csv`; A's and B's outputs do not change. C agrees with A on 33/60
(kappa 0.31) and with B on 14/21 (kappa 0.47).
The scorer rejects duplicate IDs, unassigned rows and unknown nonblank labels.

| Command | Input | Output (`results/`) | Rows |
|---|---|---|---|
| `python -m skillsig sample --only spot_check` (or `python -m skillsig evaluate`) | `spot_A_done.xlsx` (required); `spot_B_done.xlsx`, `spot_C_done.xlsx` and `trailer_check_done.csv` if present; `key.parquet` | `s4_spot_precision.csv` (precision per label for v2.1, v2 and the baselines, with Wilson intervals); `s4_spot_reason_precision.csv`; `s4_spot_confusion.csv`; `s4_spot_agreement.json` (Cohen's kappa once B is done); `s4_trailer_precision.json` (once the trailer check is done) | 28; 55; 68; 1 |

## Checks

| Command | What it checks | Time |
|---|---|---|
| `make test` (`python -m pytest -q`) | 397 unit tests on small fixtures; no dataset needed | about 20 s |
| `python -m pytest -q -m data tests/regression` | Known counts on the full data (v1 labels, registry coverage) | short |
| `make smoke` (`python -m skillsig all --dev-sample`) | Meant to run every stage on the dev sample, but **stops at `registry`**: that stage has no default module, and `all` stops at the first stage that is not implemented. Run the steps one by one with `--dev-sample` instead. | |

## Total run time

About **2.5 to 3 hours** for steps 2–8 on the full data: roughly 88 minutes for the run of 2026-10-25, plus `text_methods` (17 min), the scaffold step (75 min) and the longer full-body `dedup`. Steps 0 and 1 are one-off.

## One-shot runner

`scripts/run_full.sh` (or double-click `run_full.command` on a Mac) runs steps 2–8 and 10 with `--force`, then draws the report figures, and logs to `logs/run_full_<timestamp>.log`. `DRY_RUN=1` prints the steps only; `FETCH=1` also re-fetches the registry clones.

- **Annotation is safe.** Before annotation it draws fresh sheets, moving an unannotated `spot_A.xlsx` aside first. Once `spot_A_done.xlsx` exists it never redraws: it runs `spot_refresh` (same sample, new answer key) and then scores with `spot_check`.
- **Not included:** step 1 (SQL build scripts, run once by hand), 6g `text_score` (not run yet) and the separate paired content-reuse review.

## Where to look for what

| Folder | Contents | In git? |
|---|---|---|
| `src/data/gitskills_data/` | Raw dataset | No |
| `src/data/generated_data/` | SQL-built tables (step 1) | No |
| `src/data/derived/` | Every intermediate table (steps 2–6) | No |
| `results/` | Result tables and their manifests (steps 3e–10) | Yes |
| `results/annotation/` | Sheets, keys, codebook, finished annotations | **No** (git-ignored): back up finished `*_done.xlsx` files yourself |
| `figures/` | Figures | Yes |
| `results/base_before_text/`, `results/base_before_fullbody/` | Frozen comparison points and their write-ups | Yes |
| `logs/` | Full-run logs | No |
