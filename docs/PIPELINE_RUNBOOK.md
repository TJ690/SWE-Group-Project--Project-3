# Pipeline runbook

Run commands from the repository root with Python 3.13. Use the steps in order. Step 8r is optional. The [coding plan](CODING_PLAN.md) explains each step and shows two saved records per available table.

```bash
source .venv/bin/activate
```

Run step 0 first if the environment does not exist. Pipeline commands also read `config/default.yaml` by default (`SKILLSIG_CONFIG` or `--config PATH` can change this). All pipeline commands accept `--force`, `--dev-sample` and `--config PATH`. Use `--force` after editing code. A dev run uses the [step 2 sample](#step-2) and shares output paths with full runs; keep the whole chain in the same mode.

<a id="step-0"></a>

## Step 0: Set up Python and download the dataset

```bash
make setup
source .venv/bin/activate
(cd src/data && python download_data.py)
```

**Input:** `pyproject.toml`; `Hugging Face: mvaccargiu/gitskills`.

**Output:** `src/data/gitskills_data/data/artifacts/*.parquet`; `src/data/gitskills_data/data/artifact_siblings/*.parquet`; `src/data/gitskills_data/data/repos/*.parquet`; `src/data/gitskills_data/data/mining_runs/*.parquet`.

[Explanation and saved examples](CODING_PLAN.md#step-0).

<a id="step-1a"></a>

## Step 1a: Count content and structure

```bash
mkdir -p src/data/generated_data
python -c "import duckdb; duckdb.connect().execute(open('db/content_and_structure.sql').read())"
```

**Input:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Output:** `src/data/generated_data/skill_features.parquet`.

[Explanation and saved examples](CODING_PLAN.md#step-1a).

<a id="step-1b"></a>

## Step 1b: Record agent folders

```bash
mkdir -p src/data/generated_data
python -c "import duckdb; duckdb.connect().execute(open('db/agent_ecosystem.sql').read())"
```

**Input:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Output:** `src/data/generated_data/file_agents.parquet`.

[Explanation and saved examples](CODING_PLAN.md#step-1b).

<a id="step-1c"></a>

## Step 1c: Build repository summaries

```bash
mkdir -p src/data/generated_data
python -c "import duckdb; duckdb.connect().execute(open('db/repos.sql').read())"
```

**Input:** [`src/data/gitskills_data/data/repos/*.parquet`](#step-0); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Output:** `src/data/generated_data/repo_profile.parquet`.

[Explanation and saved examples](CODING_PLAN.md#step-1c).

<a id="step-1d"></a>

## Step 1d: Describe bundled files

```bash
mkdir -p src/data/generated_data
python -c "import duckdb; duckdb.connect().execute(open('db/siblings_data.sql').read())"
```

**Input:** [`src/data/gitskills_data/data/artifact_siblings/*.parquet`](#step-0).

**Output:** `src/data/generated_data/sibling_files.parquet`.

[Explanation and saved examples](CODING_PLAN.md#step-1d).

<a id="step-2"></a>

## Step 2: Check the data and make a development sample

```bash
python -m skillsig data
```

**Input:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/gitskills_data/data/artifact_siblings/*.parquet`](#step-0); [`src/data/gitskills_data/data/repos/*.parquet`](#step-0); [`src/data/gitskills_data/data/mining_runs/*.parquet`](#step-0); [`src/data/generated_data/skill_features.parquet`](#step-1a); [`src/data/generated_data/file_agents.parquet`](#step-1b); [`src/data/generated_data/repo_profile.parquet`](#step-1c); [`src/data/generated_data/sibling_files.parquet`](#step-1d).

**Output:** `src/data/derived/s0_devsample_ids.parquet`.

[Explanation and saved examples](CODING_PLAN.md#step-2).

<a id="step-3a"></a>

## Step 3a: Read the history of the registry collections

```bash
SKILLSIG_NO_FETCH=1 python -m skillsig registry --only mine_git
```

**Input:** `config/registry_sources.yaml`; `src/data/registry_repos/`.

**Output:** `src/data/derived/s1_versions_raw.parquet`; `src/data/derived/s1_commit_trailers.parquet`.

`SKILLSIG_NO_FETCH=1` keeps existing branch history. Missing clones or file objects may still download.

[Explanation and saved examples](CODING_PLAN.md#step-3a).

<a id="step-3b"></a>

## Step 3b: Build the distinct-version table

```bash
python -m skillsig registry --only versions
```

**Input:** [`src/data/derived/s1_versions_raw.parquet`](#step-3a); [`src/data/derived/s1_commit_trailers.parquet`](#step-3a); [`src/data/gitskills_data/data/mining_runs/*.parquet`](#step-0); [`src/data/registry_repos/`](#step-3a).

**Output:** `src/data/derived/s1_registry_versions.parquet`.

[Explanation and saved examples](CODING_PLAN.md#step-3b).

<a id="step-3c"></a>

## Step 3c: Match registry texts and summarize their history

```bash
python -m skillsig registry --only match
```

**Input:** [`src/data/derived/s1_registry_versions.parquet`](#step-3b); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/gitskills_data/data/mining_runs/*.parquet`](#step-0); `config/registry_sources.yaml`.

**Output:** `src/data/derived/s1_registry_matches.parquet`; `src/data/derived/s1_lineages.parquet`.

[Explanation and saved examples](CODING_PLAN.md#step-3c).

<a id="step-3d"></a>

## Step 3d: Compare consecutive versions

```bash
python -m skillsig registry --only changes
```

**Input:** [`src/data/derived/s1_registry_versions.parquet`](#step-3b); [`src/data/registry_repos/`](#step-3a).

**Output:** `src/data/derived/s1_registry_changes.parquet`.

[Explanation and saved examples](CODING_PLAN.md#step-3d).

<a id="step-3e"></a>

## Step 3e: Find popular texts outside the registry

```bash
python -m skillsig registry --only discover_popular
```

**Input:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/gitskills_data/data/repos/*.parquet`](#step-0); [`src/data/derived/s1_registry_matches.parquet`](#step-3c).

**Output:** `results/s1_popular_candidates.csv`.

[Explanation and saved examples](CODING_PLAN.md#step-3e).

<a id="step-4a"></a>

## Step 4a: Load structure features

```bash
python -m skillsig features --only structure
```

**Input:** [`src/data/generated_data/skill_features.parquet`](#step-1a).

**Output:** `src/data/derived/s2_structure.parquet`.

[Explanation and saved examples](CODING_PLAN.md#step-4a).

<a id="step-4b"></a>

## Step 4b: Measure commit-history signals

```bash
python -m skillsig features --only commit
```

**Input:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Output:** `src/data/derived/s2_commit.parquet`.

[Explanation and saved examples](CODING_PLAN.md#step-4b).

<a id="step-4c"></a>

## Step 4c: Describe repository context

```bash
python -m skillsig features --only repo_context
```

**Input:** [`src/data/generated_data/repo_profile.parquet`](#step-1c); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/gitskills_data/data/mining_runs/*.parquet`](#step-0); [`src/data/derived/s1_registry_matches.parquet`](#step-3c).

**Output:** `src/data/derived/s2_repo.parquet`.

[Explanation and saved examples](CODING_PLAN.md#step-4c).

<a id="step-4d"></a>

## Step 4d: Assign skill topics

```bash
python -m skillsig features --only skill_type
```

**Input:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/derived/s2_repo.parquet`](#step-4c).

**Output:** `src/data/derived/s2_types.parquet`; `results/s2_skill_type_by_project.csv`.

[Explanation and saved examples](CODING_PLAN.md#step-4d).

<a id="step-4e"></a>

## Step 4e: Measure wording

```bash
python -m skillsig features --only wording
```

**Input:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Output:** `src/data/derived/s2_wording.parquet`.

[Explanation and saved examples](CODING_PLAN.md#step-4e).

<a id="step-5a"></a>

## Step 5a: Group near-duplicate texts

```bash
python -m skillsig dedup
```

**Input:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Output:** `src/data/derived/s2_clusters.parquet`; `src/data/derived/s2_minhash_shards/`.

[Explanation and saved examples](CODING_PLAN.md#step-5a).

<a id="step-5b"></a>

## Step 5b: Find registry similarity

```bash
python -m skillsig features --only template_sim
```

**Input:** [`src/data/derived/s1_registry_versions.parquet`](#step-3b); [`src/data/derived/s1_registry_matches.parquet`](#step-3c); [`src/data/registry_repos/`](#step-3a); [`src/data/derived/s2_minhash_shards/`](#step-5a); `config/registry_sources.yaml`.

**Output:** `src/data/derived/s2_template.parquet`.

[Explanation and saved examples](CODING_PLAN.md#step-5b).

<a id="step-5c"></a>

## Step 5c: Measure project-specific identifiers

```bash
python -m skillsig features --only specificity
```

**Input:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/derived/s2_clusters.parquet`](#step-5a).

**Output:** `src/data/derived/s2_specificity.parquet`.

For a clean rebuild, move existing `s2_specificity_identifiers/` and `s2_specificity_buckets_c*` parts aside before using `--force`; keep them to resume.

[Explanation and saved examples](CODING_PLAN.md#step-5c).

<a id="step-5d"></a>

## Step 5d: Find shared structure and scaffolds

```bash
python -m skillsig features --only scaffold
```

**Input:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/gitskills_data/data/artifact_siblings/*.parquet`](#step-0).

**Output:** `src/data/derived/s2_scaffold.parquet`; `results/s2_scaffold_top_outlines.csv`.

[Explanation and saved examples](CODING_PLAN.md#step-5d).

<a id="step-6a"></a>

## Step 6a: Reproduce the first label rules

```bash
python -m skillsig label --only rules_v1
```

**Input:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/derived/s2_commit.parquet`](#step-4b).

**Output:** `src/data/derived/s3_labels_v1.parquet`.

[Explanation and saved examples](CODING_PLAN.md#step-6a).

<a id="step-6b"></a>

## Step 6b: Add registry and cluster evidence

```bash
python -m skillsig label --only rules_v2
```

**Input:** [`src/data/derived/s3_labels_v1.parquet`](#step-6a); [`src/data/derived/s2_structure.parquet`](#step-4a); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/derived/s2_repo.parquet`](#step-4c); [`src/data/derived/s2_types.parquet`](#step-4d); [`src/data/derived/s2_wording.parquet`](#step-4e); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s2_template.parquet`](#step-5b); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Output:** `src/data/derived/s3_labels_v2.parquet`; `results/s3_v1_v2_crosstab.csv`.

[Explanation and saved examples](CODING_PLAN.md#step-6b).

<a id="step-6c"></a>

## Step 6c: Add disclosure and revision flags

```bash
python -m skillsig label --only rules_v2_1
```

**Input:** [`src/data/derived/s3_labels_v1.parquet`](#step-6a); [`src/data/derived/s3_labels_v2.parquet`](#step-6b); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Output:** `src/data/derived/s3_labels_v2_1.parquet`; `results/s3_v2_v21_crosstab.csv`.

[Explanation and saved examples](CODING_PLAN.md#step-6c).

<a id="step-6d"></a>

## Step 6d: Build simple comparison rules

```bash
python -m skillsig label --only baselines
```

**Input:** [`src/data/derived/s3_labels_v1.parquet`](#step-6a); [`src/data/derived/s3_labels_v2.parquet`](#step-6b); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/generated_data/file_agents.parquet`](#step-1b); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Output:** `src/data/derived/s3_baseline_labels.parquet`; `results/s3_baseline_summary.csv`.

[Explanation and saved examples](CODING_PLAN.md#step-6d).

<a id="step-6e"></a>

## Step 6e: Test text style against disclosure

```bash
python -m skillsig label --only style_model
```

**Input:** [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/derived/s2_structure.parquet`](#step-4a); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/derived/s2_wording.parquet`](#step-4e); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s2_specificity.parquet`](#step-5c); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Output:** `src/data/derived/s3_style_scores.parquet`; `results/s3_style_model.csv`; `results/s3_style_model.json`; `results/s3_style_top_words.csv`.

[Explanation and saved examples](CODING_PLAN.md#step-6e).

<a id="step-6f"></a>

## Step 6f: Compare several text methods

```bash
python -m skillsig label --only text_methods
```

**Input:** [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/derived/s2_structure.parquet`](#step-4a); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/derived/s2_wording.parquet`](#step-4e); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s2_specificity.parquet`](#step-5c); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Output:** `src/data/derived/s3_text_methods_oof.parquet`; `results/s3_text_methods.csv`; `results/s3_text_methods.json`; `results/s3_text_methods_top_words.csv`.

[Explanation and saved examples](CODING_PLAN.md#step-6f).

<a id="step-6g"></a>

## Step 6g: Report reuse and disclosure separately

```bash
python -m skillsig label --only evidence
```

**Input:** [`src/data/derived/s3_labels_v1.parquet`](#step-6a); [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s2_template.parquet`](#step-5b); [`src/data/derived/s2_scaffold.parquet`](#step-5d).

**Output:** `src/data/derived/s3_evidence.parquet`; `results/s3_evidence_matrix.csv`; `results/s3_evidence_signals.csv`; `results/s3_evidence_legacy_crosstab.csv`; `results/s3_evidence_summary.json`.

[Explanation and saved examples](CODING_PLAN.md#step-6g).

<a id="step-7a"></a>

## Step 7a: Check how many disclosures first and last commits reveal

```bash
python -m skillsig evaluate --only disclosure_recall
```

**Input:** [`src/data/derived/s1_versions_raw.parquet`](#step-3a); [`src/data/derived/s1_commit_trailers.parquet`](#step-3a).

**Output:** `results/s4_disclosure_recall.csv`; `results/s4_disclosure_recall.json`.

[Explanation and saved examples](CODING_PLAN.md#step-7a).

<a id="step-7b"></a>

## Step 7b: Check recovery of exact registry texts

```bash
python -m skillsig evaluate --only template_provenance
```

**Input:** [`src/data/derived/s2_template.parquet`](#step-5b); [`src/data/derived/s3_labels_v1.parquet`](#step-6a); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c).

**Output:** `results/s4_template_provenance.csv`; `results/s4_template_unexplained.csv`.

[Explanation and saved examples](CODING_PLAN.md#step-7b).

<a id="step-7c"></a>

## Step 7c: Test similarity after controlled edits

```bash
python -m skillsig evaluate --only edit_robustness
```

**Input:** [`src/data/derived/s2_template.parquet`](#step-5b); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Output:** `results/s4_edit_robustness.csv`; `figures/12_edit_robustness.png`.

[Explanation and saved examples](CODING_PLAN.md#step-7c).

<a id="step-7d"></a>

## Step 7d: Vary one rule threshold at a time

```bash
python -m skillsig evaluate --only stability
```

**Input:** [`src/data/derived/s3_labels_v1.parquet`](#step-6a); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/derived/s2_template.parquet`](#step-5b); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Output:** `results/s4_stability.csv`; `results/s4_stability.json`.

[Explanation and saved examples](CODING_PLAN.md#step-7d).

<a id="step-7e"></a>

## Step 7e: Check scaffold overlap and prepare examples

```bash
python -m skillsig evaluate --only scaffold_check
```

**Input:** [`src/data/derived/s2_scaffold.parquet`](#step-5d); [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/derived/s2_template.parquet`](#step-5b); [`src/data/derived/s3_labels_v1.parquet`](#step-6a); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Output:** `results/s4_scaffold_overlap.csv`; `results/s4_scaffold_recall.csv`; `results/s4_scaffold_agent_confound.csv`; `results/s4_scaffold_spot.csv`.

[Explanation and saved examples](CODING_PLAN.md#step-7e).

<a id="step-8"></a>

## Step 8: Draw the blind authorship review sample

```bash
python -m skillsig sample
```

**Input:** [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/derived/s3_labels_v2.parquet`](#step-6b); [`src/data/derived/s3_baseline_labels.parquet`](#step-6d); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s2_repo.parquet`](#step-4c); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Output:** `results/annotation/spot_A.xlsx`; `results/annotation/spot_B.xlsx`; `results/annotation/trailer_check.csv`; `results/annotation/key.parquet`; `results/annotation/trailer_key.parquet`; `results/annotation/codebook.md`; `results/s4_spot_design.json`.

New draw only; existing `spot_A.xlsx` is kept. To keep IDs after label changes, use [8r](#step-8r). Keep `key.parquet` and `trailer_key.parquet` private.

[Explanation and saved examples](CODING_PLAN.md#step-8).

<a id="step-8r"></a>

## Step 8r: Refresh the same sample after label changes

```bash
python -m skillsig sample --only spot_refresh
```

**Input:** [`results/annotation/key.parquet`](#step-8); [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/derived/s3_labels_v2.parquet`](#step-6b); [`src/data/derived/s3_baseline_labels.parquet`](#step-6d); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s2_repo.parquet`](#step-4c); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`results/annotation/spot_A.xlsx`](#step-8); [`results/annotation/spot_B.xlsx`](#step-8).

**Output:** `results/annotation/key.parquet`; conditionally `results/annotation/spot_A.xlsx`, `results/annotation/spot_B.xlsx` and `results/annotation/codebook.md`.

Keeps the same IDs. If either A/B workbook has responses, updates only the key; otherwise rewrites both workbooks and the codebook. Input key and sheets originate in [8](#step-8).

[Explanation and saved examples](CODING_PLAN.md#step-8r).

<a id="step-9"></a>

## Step 9: People complete the blind review

Manual: follow docs/ANNOTATION_GUIDELINES.md.

**Input:** [`results/annotation/spot_A.xlsx`](#step-8); [`results/annotation/spot_B.xlsx`](#step-8); [`results/annotation/trailer_check.csv`](#step-8); [`results/annotation/codebook.md`](#step-8); `docs/ANNOTATION_GUIDELINES.md`.

**Output:** `results/annotation/spot_A_done.xlsx`; `results/annotation/spot_B_done.xlsx`; `results/annotation/spot_C_done.xlsx`; `results/annotation/trailer_check_done.csv`.

A is required for scoring. B, C and trailer answers are optional. Back up completed files; `results/annotation/` is ignored by Git.

[Explanation and saved examples](CODING_PLAN.md#step-9).

<a id="step-10"></a>

## Step 10: Score the completed blind review

```bash
python -m skillsig sample --only spot_check
```

**Input:** [`results/annotation/spot_A_done.xlsx`](#step-9); [`results/annotation/spot_B_done.xlsx`](#step-9); [`results/annotation/spot_C_done.xlsx`](#step-9); [`results/annotation/trailer_check_done.csv`](#step-9); [`results/annotation/key.parquet`](#step-8r).

**Output:** `results/s4_spot_precision.csv`; `results/s4_spot_reason_precision.csv`; `results/s4_spot_confusion.csv`; `results/s4_spot_agreement.json`; `results/s4_spot_rater_confusion.csv`; `results/s4_spot_overlap_precision.csv`; `results/s4_spot_rater_pairs.csv`; `results/s4_spot_rater_pair_confusion.csv`; `results/s4_spot_rater_c_precision.csv`; `results/s4_trailer_precision.json`; `results/annotation/rater_disagreements.csv`; `results/annotation/rater_c_disagreements.csv`.

B outputs require B’s sheet; C outputs require C’s sheet; trailer precision requires the completed trailer CSV. Use the original [step 8 key](#step-8) if no refresh was needed.

[Explanation and saved examples](CODING_PLAN.md#step-10).

## Run the main chain with one command

After steps 0 and 1a–1d:

```bash
bash scripts/run_full.sh
```

**Inputs:** files from [0](#step-0), [1a](#step-1a), [1b](#step-1b), [1c](#step-1c), [1d](#step-1d), registry config and local clones from [3a](#step-3a); completed annotations from [9](#step-9), if present.

**Outputs:** the main-chain files listed in steps 2–8 and 10; `logs/run_full_<timestamp>.log`. Uses `--force`. If `spot_A_done.xlsx` exists, keeps sample IDs, refreshes the key and scores; otherwise draws sheets, moving earlier uncompleted sheets aside. Does not run step 9 for people.

```bash
DRY_RUN=1 bash scripts/run_full.sh
```

**Input:** runner script. **Output:** printed command list and log; no stage execution.

Use `FETCH=1 bash scripts/run_full.sh` only when updating the registry snapshot is intended. The runner records regression-test failures as warnings and continues; inspect the log before using results.

## Checks

| Run | Input | Output |
| --- | --- | --- |
| `python -m pytest -q` | Code and small test fixtures | Unit-test pass/fail |
| `python -m pytest -q -m data tests/regression` | Full dataset and generated tables from the main chain | Known-count regression pass/fail |

`make all`, `make smoke` and `python -m skillsig all` stop at the registry stage because it has no default runner. Run the listed modules or the full script.
