# Execution steps

The full `skillsig` pipeline, in the order to run it, starting from data that is already on disk. Nothing here downloads the dataset or fetches from GitHub.

For each stage: what the stage does, then each step's command, what it does, its input, its output, and how much data it processes.

**Design notes:** how each step works and why it was built that way (design decisions, trade-offs, known limits, key results) are in [`docs/stages/`](stages/README.md), one file per stage with the same step numbers as here. Each stage section below links to its file.

## How to run a step

Run every command from the repository root with the project environment active:

```bash
source .venv/bin/activate
```

```bash
python -m skillsig <stage> [--only <module>] [--dev-sample] [--force]
```

| Flag | Effect |
|---|---|
| *(no flag)* | **Full data**: all **3,797,117** `SKILL.md` files in the dataset. Identical copies share one `file_sha`, so most steps work on the 1,877,981 distinct skills (or the 1,840,872 readable ones for text steps); copy counts still use every file. This is the default; see [Run on the full data](#run-on-the-full-data). |
| `--only <module>` | Run one module of the stage. Most steps below use it. |
| `--dev-sample` | Run on the 50,000-skill dev sample instead of the full data (for testing). |
| `--force` | Rebuild even if the inputs and config are unchanged. Add it to every line to rebuild the whole pipeline. |

Each output gets a `<file>.manifest.json` beside it (input fingerprints, config hash, code commit, row count). A step whose inputs and config have not changed is skipped. **Code changes are not tracked**: after editing code, add `--force`.

## Before you start: data that must already exist

| Path | What | Size |
|---|---|---|
| `src/data/gitskills_data/data/{artifacts,artifact_siblings,repos,mining_runs}/` | Raw GitSkills Parquet | 12.5 GB |
| `src/data/registry_repos/` | Local git clones of the 31 registry collections | 271 MB |
| `src/data/generated_data/` | Tables built by the SQL scripts (step P.1) | 477 MB |

| Raw table | Rows | Meaning |
|---|---|---|
| `artifacts` | 3,797,117 | One row per `SKILL.md` file found on GitHub |
| ↳ distinct skills | **1,877,981** | One representative per distinct text (`file_sha`); the unit for labels |
| ↳ readable distinct skills | **1,840,872** | Distinct skills with text, ≥ 200 body characters, not a redirect stub; the unit for text features |
| ↳ with commit history | 458,137 | 24.4% of distinct skills |
| `artifact_siblings` | 7,264,865 | Files and folders stored next to each skill |
| `repos` | 282,200 | Repository metadata |
| `mining_runs` | 7 | Collection runs (gives the snapshot date) |

## Sequence at a glance

```
P    Prepare        P.1 SQL build scripts                          (once)
s0   Intake         0.1 data
s1   Registry       1.1 mine_git → 1.2 versions → 1.3 match → 1.4 changes → 1.5 discover_popular
s2   Features       2.1 structure → 2.2 commit → 2.3 repo_context → 2.4 skill_type → 2.5 wording
                    2.6 dedup → 2.7 template_sim → 2.8 specificity → 2.9 scaffold
s3   Labels         3.1 rules_v1 → 3.2 rules_v2 → 3.3 rules_v2_1 → 3.4 baselines
                    3.5 style_model → 3.6 text_methods → (3.7 text_score, not run yet)
                    3.8 evidence
s4   Validation     4.1 disclosure_recall → 4.2 template_provenance → 4.3 edit_robustness
                    4.4 stability → 4.5 scaffold_check
                    4.6 sample (draw sheets) → 4.7 people annotate → 4.8 spot_check (score)
s6   Report         6.1 export (report figures)
```

## Run on the full data

Full data is the default: every command runs on the whole dataset, 3,797,117 `SKILL.md` files (1,877,981 distinct skills), unless `--dev-sample` is added. To run the whole pipeline, run these in order (each one is explained in its stage section below). Add `--force` to each line to rebuild everything even when it looks up to date.

```bash
source .venv/bin/activate
python -m skillsig data
SKILLSIG_NO_FETCH=1 python -m skillsig registry --only mine_git
python -m skillsig registry --only versions
python -m skillsig registry --only match
python -m skillsig registry --only changes
python -m skillsig registry --only discover_popular
python -m skillsig features --only structure
python -m skillsig features --only commit
python -m skillsig features --only repo_context
python -m skillsig features --only skill_type
python -m skillsig features --only wording
python -m skillsig dedup
python -m skillsig features --only template_sim
python -m skillsig features --only specificity
python -m skillsig features --only scaffold
python -m skillsig label --only rules_v1
python -m skillsig label --only rules_v2
python -m skillsig label --only rules_v2_1
python -m skillsig label --only baselines
python -m skillsig label --only style_model
python -m skillsig label --only text_methods
python -m skillsig label --only evidence
python -m skillsig evaluate --only disclosure_recall
python -m skillsig evaluate --only template_provenance
python -m skillsig evaluate --only edit_robustness
python -m skillsig evaluate --only stability
python -m skillsig evaluate --only scaffold_check
python -m skillsig sample --only spot_refresh
python -m skillsig export
```

To run it unattended on a Mac, save the lines above as `run.sh` and keep the machine awake while it logs:

```bash
caffeinate -i bash run.sh > logs/full_run.log 2>&1
```

Before you start:

- **Keep `SKILLSIG_NO_FETCH=1`** on the `mine_git` line. Without it the step pulls new commits from GitHub.
- **Protect the annotation.** The last line is `spot_refresh`, not `sample`: it keeps the same 120 skills, so finished sheets (`spot_A_done.xlsx`) stay valid. Copy `results/annotation/spot_A_done.xlsx` somewhere safe first; that folder is not in git.
- **Rescore after relabelling.** If labels changed, finish with `python -m skillsig sample --only spot_check` to score the finished sheets against the new labels.
- **Expect rebuilds without `--force` too.** A config change (for example `minhash.body_max_chars` or `sample.per_label`) marks every output as out of date, so every step rebuilds; steps whose logic does not use the changed setting give the same result.
- **Do not use `make all` or `python -m skillsig all`.** Both stop at the registry stage, which has no default module.

---

## P Prepare generated tables

**Code:** `db/*.sql` (run once)

**What it does:** builds four summary tables from the raw Parquet with DuckDB SQL. Three later steps read them, and so do the EDA notebooks. This is a local build, not a download.

**Design notes:** [stages/P_prepare.md](stages/P_prepare.md)

### P.1 SQL build scripts

```bash
mkdir -p src/data/generated_data
python -c "import duckdb; duckdb.connect().execute(open('db/content_and_structure.sql').read())"
python -c "import duckdb; duckdb.connect().execute(open('db/agent_ecosystem.sql').read())"
python -c "import duckdb; duckdb.connect().execute(open('db/repos.sql').read())"
python -c "import duckdb; duckdb.connect().execute(open('db/siblings_data.sql').read())"
```

| Script | Input | Output (`src/data/generated_data/`) | Size | Read by |
|---|---|---|---|---|
| `content_and_structure.sql` | `artifacts` | `skill_features.parquet`: structure counts per skill | 63 MB | 2.1 |
| `agent_ecosystem.sql` | `artifacts` | `file_agents.parquet`: which agent folder each file sits in | 209 MB | 3.4 |
| `repos.sql` | `repos`, `artifacts` | `repo_profile.parquet`: one row per repository | 16 MB | 2.3 |
| `siblings_data.sql` | `artifact_siblings` | `sibling_files.parquet`: one row per bundled file | 189 MB | notebooks only |

Skip P.1 if these four files already exist.

---

## s0 Intake stage

**Code:** `src/skillsig/s0_intake/`

**What it does:** opens the raw Parquet as DuckDB views, checks that every expected column and row count is there, and draws a seeded, stratified **dev sample** of 50,000 skills that every later step can use through `--dev-sample`. No labels or features are made here.

**Design notes:** [stages/s0_intake/](stages/s0_intake/README.md)

### 0.1 Data check and dev sample

```bash
python -m skillsig data
```

| Item | Details |
|---|---|
| **Does** | Schema check (stops on missing columns); draws the dev sample |
| **Input** | Raw Parquet (all four tables) |
| **Output** | `src/data/derived/s0_devsample_ids.parquet` (`file_sha`, `stratum`) |
| **Data** | Reads 3,797,117 rows; writes 50,000 ids |

**Check:** `python -m pytest -q tests/s0_intake`

---

## s1 Registry stage

**Code:** `src/skillsig/s1_registry/`

**What it does:** rebuilds where skills came from. The dataset is one snapshot and does not record origins, so this stage reads the full git history of 31 well-known skill collections. It records every version they ever published, finds dataset skills that are byte-for-byte copies (a git blob hash equals the dataset's `file_sha`), links copies into lineages, classifies edits between versions as substantive or cosmetic, and lists widely copied skills that come from elsewhere. It also collects AI co-author lines from the registries' commits, for check 4.1.

`python -m skillsig registry` on its own does nothing; run each module with `--only`, in this order.

**Design notes:** [stages/s1_registry/](stages/s1_registry/README.md) (one file per step)

### 1.1 Mine registry history

```bash
SKILLSIG_NO_FETCH=1 python -m skillsig registry --only mine_git
```

**Keep `SKILLSIG_NO_FETCH=1`.** Without it, the step fetches new commits from GitHub into the clones, which changes the registry snapshot every label is built on.

| Item | Details |
|---|---|
| **Does** | Walks the git history of each local clone; records every `SKILL.md` version and every AI co-author trailer |
| **Input** | `config/registry_sources.yaml`; clones in `src/data/registry_repos/` |
| **Output** | `src/data/derived/s1_versions_raw.parquet`; `s1_commit_trailers.parquet` |
| **Data** | 31 repositories → 16,485 version rows; 50,941 trailer rows |

### 1.2 Collapse versions

```bash
python -m skillsig registry --only versions
```

| Item | Details |
|---|---|
| **Does** | One ordered row per distinct version (blob), with intro date and trailer |
| **Input** | 1.1 outputs; mining snapshot date |
| **Output** | `src/data/derived/s1_registry_versions.parquet` |
| **Data** | 16,485 → 15,254 rows |

### 1.3 Match registry versions to the dataset

```bash
python -m skillsig registry --only match
```

| Item | Details |
|---|---|
| **Does** | Joins registry blob hashes to dataset `file_sha` (exact copies); marks the registry's own files as origin; builds lineages |
| **Input** | 1.2; `artifacts` (`repo_full_name`, `file_sha`) |
| **Output** | `src/data/derived/s1_registry_matches.parquet`; `s1_lineages.parquet` |
| **Data** | 3,797,117 artifact rows scanned → 6,719 matches; 2,274 lineages |

### 1.4 Classify registry changes

```bash
python -m skillsig registry --only changes
```

| Item | Details |
|---|---|
| **Does** | Pairs consecutive versions of each registry skill; classifies each change as substantive or cosmetic |
| **Input** | 1.2; file contents from the clones |
| **Output** | `src/data/derived/s1_registry_changes.parquet` |
| **Data** | 12,877 version pairs |

### 1.5 Find popular skills outside the registry

```bash
python -m skillsig registry --only discover_popular
```

| Item | Details |
|---|---|
| **Does** | Ranks the most widely copied skills (by distinct owners) that no mined registry explains, with a likely origin repository |
| **Input** | `artifacts`, `repos`, 1.3 |
| **Output** | `results/s1_popular_candidates.csv` |
| **Data** | 1,877,981 skills ranked → top 200 |

**Check:** `python -m pytest -q tests/s1_registry`

---

## s2 Features stage

**Code:** `src/skillsig/s2_features/`

**What it does:** measures each skill, without deciding anything. It produces one table per kind of evidence: structure (length, headings, lists), commit history (AI trailers, bots, bulk commits), repository context (collection or project), skill type, wording, near-duplicate clusters (MinHash), similarity to registry versions, specificity of identifiers, and structural scaffolding. The labels in s3 read these tables.

Order matters within this stage: 2.7 and 2.8 read the clusters and signatures from 2.6.

**Design notes:** [stages/s2_features/](stages/s2_features/README.md) (one file per step)

### 2.1 Structure

```bash
python -m skillsig features --only structure
```

| Item | Details |
|---|---|
| **Does** | Copies the SQL-built structure counts: body length, headings, lists, code blocks, tables, links, front-matter fields |
| **Input** | `src/data/generated_data/skill_features.parquet` |
| **Output** | `src/data/derived/s2_structure.parquet` |
| **Data** | 1,840,872 readable skills |

### 2.2 Commit history

```bash
python -m skillsig features --only commit
```

| Item | Details |
|---|---|
| **Does** | From first and last commit: AI co-author trailers (and which tool), bot class, how many skills one commit added (batch size), message class |
| **Input** | `artifacts` commit columns |
| **Output** | `src/data/derived/s2_commit.parquet` |
| **Data** | 1,877,981 distinct skills (458,137 with history) |

### 2.3 Repository context

```bash
python -m skillsig features --only repo_context
```

| Item | Details |
|---|---|
| **Does** | Per repository: size, share of copied files, and kind (`A_collection`, `B_agent_tooling`, `C_project`) |
| **Input** | `generated_data/repo_profile.parquet`; `artifacts`; 1.3 |
| **Output** | `src/data/derived/s2_repo.parquet` |
| **Data** | 282,200 repositories |

### 2.4 Skill type

```bash
python -m skillsig features --only skill_type
```

| Item | Details |
|---|---|
| **Does** | Keyword type of each skill (from name and description; body as fallback) — context only, no label reads it. |
| **Input** | `artifacts` name, description, body; 2.3 |
| **Output** | `src/data/derived/s2_types.parquet`; `results/s2_skill_type_by_project.csv` |
| **Data** | 1,877,981 skills; 4,136 summary rows |

### 2.5 Wording

```bash
python -m skillsig features --only wording
```

| Item | Details |
|---|---|
| **Does** | Wording features W1–W12 (vocabulary richness, sentence shape, marker words) and description idiom. |
| **Input** | `artifacts` text and description |
| **Output** | `src/data/derived/s2_wording.parquet` |
| **Data** | 1,840,872 readable skills |

### 2.6 Near-duplicate clusters

```bash
python -m skillsig dedup
```

| Item | Details |
|---|---|
| **Does** | MinHash signature of each skill's full body (5-word shingles, 128 permutations); LSH groups near-duplicates into clusters at similarity 0.8 and 0.6. |
| **Input** | `artifacts` text of readable skills |
| **Output** | `src/data/derived/s2_clusters.parquet`; `s2_minhash_shards/` (signatures, 1.3 GB; resumable) |
| **Data** | 1,840,872 skills → 3,681,744 rows (one per threshold) |

### 2.7 Registry similarity

```bash
python -m skillsig features --only template_sim
```

| Item | Details |
|---|---|
| **Does** | For each skill, the nearest registry version and the estimated similarity (exact, near ≥ 0.8, or none) |
| **Input** | 1.2, 1.3, the clones, 2.6 signatures |
| **Output** | `src/data/derived/s2_template.parquet` |
| **Data** | 1,840,872 skills against 15,254 registry versions |

### 2.8 Specificity

```bash
rm -rf src/data/derived/s2_specificity_identifiers src/data/derived/s2_specificity_buckets_c*
python -m skillsig features --only specificity
```

The `rm` clears resumable parts; leave it out to resume an interrupted run.

| Item | Details |
|---|---|
| **Does** | Share of a skill's identifiers (paths, flags, code names) that almost no other skill uses. |
| **Input** | `artifacts` text (first 50,000 characters); 2.6 |
| **Output** | `src/data/derived/s2_specificity.parquet` |
| **Data** | 1,734,331 skills |

### 2.9 Scaffold signals

```bash
python -m skillsig features --only scaffold
```

| Item | Details |
|---|---|
| **Does** | Structural templating: leftover placeholders, skill-creator template text, heading-outline families, front-matter keys, folder layout, name-masked body hash, shared-line fraction and customization score; one `scaffold_basis` per skill. |
| **Input** | `artifacts` text and owners; `artifact_siblings` |
| **Output** | `src/data/derived/s2_scaffold.parquet`; `results/s2_scaffold_top_outlines.csv` |
| **Data** | 1,840,872 skills; 7,264,865 sibling rows; 200 outline rows |

**Check:** `python -m pytest -q tests/s2_features`

---

## s3 Labeling stage

**Code:** `src/skillsig/s3_labeling/`

**What it does:** turns the s2 evidence into one authorship label per distinct skill: `highly_templated`, `agent_assisted`, `likely_human` or `insufficient_evidence`. The rules run in versions, each building on the last: v1 (copy count, trailer, bot, batch), v2 (adds registry, cluster and confidence), v2.1 (trailer and human-confidence overrides, independent flags). v2.1 is the current label. The stage also builds single-signal baselines to compare against, and two text-only models that ask whether the text alone looks like disclosed agent skills.

**Design notes:** [stages/s3_labeling/](stages/s3_labeling/README.md) (one file per step)

### 3.1 Labels v1

```bash
python -m skillsig label --only rules_v1
```

| Item | Details |
|---|---|
| **Does** | Sprint 1 rules, first match wins: copy count, AI trailer, bot class, batch size |
| **Input** | `artifacts`; 2.2 |
| **Output** | `src/data/derived/s3_labels_v1.parquet` |
| **Data** | 1,877,981 skills |

### 3.2 Labels v2

```bash
python -m skillsig label --only rules_v2
```

| Item | Details |
|---|---|
| **Does** | Adds registry matches and clusters as templated bases; gives a confidence and reasons per skill |
| **Input** | 3.1; 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7 |
| **Output** | `src/data/derived/s3_labels_v2.parquet`; `results/s3_v1_v2_crosstab.csv` |
| **Data** | 1,877,981 skills; 11 crosstab rows |

### 3.3 Labels v2.1 (current label)

```bash
python -m skillsig label --only rules_v2_1
```

| Item | Details |
|---|---|
| **Does** | Trailer override for bulk installs, human confidence only with revision history, and independent flags (templated, disclosed agent, revised, conflict) |
| **Input** | 3.1, 3.2, 2.2, `artifacts` |
| **Output** | `src/data/derived/s3_labels_v2_1.parquet`; `results/s3_v2_v21_crosstab.csv` |
| **Data** | 1,877,981 skills; 10 crosstab rows |

### 3.4 Baselines

```bash
python -m skillsig label --only baselines
```

| Item | Details |
|---|---|
| **Does** | Single-signal labels (B0 majority, B1 copy count, B2 agent folder, B3 trailer, B3b trailer without bulk) to show what the rule set adds |
| **Input** | 3.1, 3.2, 2.2, `generated_data/file_agents.parquet` |
| **Output** | `src/data/derived/s3_baseline_labels.parquet`; `results/s3_baseline_summary.csv` |
| **Data** | 1,877,981 skills; 9 summary rows |

### 3.5 Style model (E4)

```bash
python -m skillsig label --only style_model
```

| Item | Details |
|---|---|
| **Does** | Positive-unlabelled text model: does the text alone resemble skills whose commits disclosed an AI tool? Cross-validated, grouped by owner and cluster. |
| **Input** | 3.3, 2.1, 2.2, 2.5, 2.6, 2.8, `artifacts` text |
| **Output** | `src/data/derived/s3_style_scores.parquet`; `results/s3_style_model.csv`, `.json`, `s3_style_top_words.csv` |
| **Data** | 380,023 skills scored; 30 result rows |

### 3.6 Text methods (E5)

```bash
python -m skillsig label --only text_methods
```

| Item | Details |
|---|---|
| **Does** | Compares text-only methods (length, phrase rule, feature models, TF-IDF, ensemble) for agent-assisted vs. human on skills with history. |
| **Input** | 3.3, 2.1, 2.2, 2.5, 2.6, 2.8, `artifacts` text (first 6,000 characters) |
| **Output** | `src/data/derived/s3_text_methods_oof.parquet`; `results/s3_text_methods.csv`, `.json`, `s3_text_methods_top_words.csv` |
| **Data** | 40,000 skills (20,000 agent + 20,000 human); 21 result rows |

### 3.7 Text score (E5b) — not run yet

```bash
python -m skillsig label --only text_score
```

| Item | Details |
|---|---|
| **Does** | Applies the method chosen in 3.6 to skills without history, as a text-based lean |
| **Input** | 3.6, 3.3, s2 features, `artifacts` text |
| **Output** | `src/data/derived/s3_text_scores.parquet`; `results/s3_text_score_summary.csv`, `.json` |
| **Data** | Non-templated skills without history |

### 3.8 Evidence dimensions

```bash
python -m skillsig label --only evidence
```

| Item | Details |
|---|---|
| **Does** | Reports reuse evidence and disclosed agent involvement as two separate dimensions per skill, keeping the overlapping flags and the historical v2.1 label. Assigns no human-authorship verdict and does not penalize overlap |
| **Input** | 3.3, 3.1 (copy counts), 2.2, 2.6, 2.7, 2.9 |
| **Output** | `src/data/derived/s3_evidence.parquet`; `results/s3_evidence_matrix.csv`, `s3_evidence_signals.csv`, `s3_evidence_legacy_crosstab.csv`, `s3_evidence_summary.json` |
| **Data** | 1,877,981 skills; 15-cell matrix (5 reuse × 3 agent signals) |

Step 6.1 draws the matrix as `figures/report/f0_evidence_dimensions.png`. See [the evidence definitions](stages/s3_labeling/evidence.md).

**Check:** `python -m pytest -q tests/s3_labeling`

---

## s4 Validation stage

**Code:** `src/skillsig/s4_validation/`

**What it does:** measures how far the labels can be trusted. Checks E1–E3 compare signals against known answers, such as copies of registry skills and edits of a known size. E5 shows how label shares move when one threshold changes. The scaffold check tests the structural signals. E6 is the human spot-check: the pipeline draws a blind sample, two people label it, and the pipeline scores its labels against theirs.

**Design notes:** [stages/s4_validation/](stages/s4_validation/README.md) (one file per check)

### 4.1 Disclosure recall (E1)

```bash
python -m skillsig evaluate --only disclosure_recall
```

| Item | Details |
|---|---|
| **Does** | Of all AI disclosures in registry history, the share visible from only the first or last commit, which is all the dataset shows |
| **Input** | 1.1 outputs |
| **Output** | `results/s4_disclosure_recall.csv`, `.json` |
| **Data** | 50,941 registry trailers → 8 rows |

### 4.2 Template provenance (E2)

```bash
python -m skillsig evaluate --only template_provenance
```

| Item | Details |
|---|---|
| **Does** | How many registry copies the copy-count and cluster rules mark: recovery on exact registry matches, and agreement on near candidates (reported separately, never pooled) |
| **Input** | 2.7, 3.1, 2.6, 3.3 |
| **Output** | `results/s4_template_provenance.csv`; `s4_template_unexplained.csv` |
| **Data** | 6,481 exact registry matches and 42,104 near candidates; 3 + 3 rows |

### 4.3 Edit robustness (E3)

```bash
python -m skillsig evaluate --only edit_robustness
```

| Item | Details |
|---|---|
| **Does** | Edits registry skills by known amounts and checks how often the template detector still matches |
| **Input** | 2.7, `artifacts` text |
| **Output** | `results/s4_edit_robustness.csv`; `figures/12_edit_robustness.png` |
| **Data** | 10 rows |

### 4.4 Stability (E5)

```bash
python -m skillsig evaluate --only stability
```

| Item | Details |
|---|---|
| **Does** | Changes one threshold at a time and reports how the v2.1 label shares move |
| **Input** | 3.1, 2.2, 2.7, 2.6, 3.3, `artifacts` |
| **Output** | `results/s4_stability.csv`, `.json` |
| **Data** | 1,877,981 skills per setting → 84 rows |

### 4.5 Scaffold check

```bash
python -m skillsig evaluate --only scaffold_check
```

| Item | Details |
|---|---|
| **Does** | What the scaffold signals add beyond the old rules, their recall on registry copies, whether they track agent style, and a 150-row spot sheet with shared-passage evidence |
| **Input** | 2.9, 3.3, 2.7, 3.1, 2.6; text of sampled rows |
| **Output** | `results/s4_scaffold_overlap.csv`, `_recall.csv`, `_agent_confound.csv`, `_spot.csv` |
| **Data** | 1,840,872 skills → 6, 3, 2 and 150 rows |

`python -m skillsig evaluate` without `--only` runs 4.1–4.4, then scores E6 (4.8) if `spot_A_done.xlsx` exists. It does not run 4.5.

### 4.6 Draw the spot-check sheets (E6)

```bash
python -m skillsig sample
```

| Item | Details |
|---|---|
| **Does** | Draws a blind, stratified sample: 30 skills per v2.1 label, at most one per repository and per cluster. Writes Excel sheets with dropdowns, with full skill text up to 32,000 characters |
| **Input** | 3.3, 3.2, 3.4, 2.2, 2.6, 2.3, `artifacts` |
| **Output** | `results/annotation/spot_A.xlsx` (person A), `spot_B.xlsx` (person B), `trailer_check.csv`, `key.parquet` (answer key; never shown to annotators), `trailer_key.parquet`, `codebook.md`; `results/s4_spot_design.json` |
| **Data** | 120 rows (A); 40 rows (B, a subset of A); 50 trailer rows |

`sample` never draws over an existing `spot_A.xlsx`; move it aside to redraw. After relabelling, use this instead, to keep the same skills and refresh only the key:

```bash
python -m skillsig sample --only spot_refresh
```

### 4.7 Annotate (people)

Follow `docs/ANNOTATION_GUIDELINES.md` (also `.docx`).

| Who | Fills | Saves as (`results/annotation/`) |
|---|---|---|
| Person A | `spot_A.xlsx`: `your_label`, `your_confidence`, `notes` | `spot_A_done.xlsx` |
| Person B | `spot_B.xlsx` (same columns), without talking to person A | `spot_B_done.xlsx` |
| Person C (optional) | a copy of `spot_A.xlsx`, any subset of rows, without talking to A or B | `spot_C_done.xlsx` |
| Anyone | `trailer_check.csv`: `is_ai_disclosure` = `yes` / `no` / `unsure` | `trailer_check_done.csv` |

`results/annotation/` is git-ignored: back up finished files yourself.

### 4.8 Score the spot-check (E6)

Both independent annotations are available as of 2026-10-27. Preserve the submitted
workbooks; `spot_B_done.xlsx` is a byte-identical copy of `spot_B_Terry.xlsx`.
Scoring now also writes `s4_spot_rater_confusion.csv`, `s4_spot_overlap_precision.csv`
and private `annotation/rater_disagreements.csv`. A and B agree on 21/40 (52.5%),
kappa 0.288. A third rater, C (60 of the 120 skills, `spot_C_done.xlsx`), is scored in
separate files; C agrees with A on 33/60 (kappa 0.31) and with B on 14/21 (kappa 0.47).
Original A-based scores are retained separately from overlap comparisons.
See [the annotation review](report/ANNOTATION_REVIEW.md) for interpretation and procedure.

```bash
python -m skillsig sample --only spot_check
```

| Item | Details |
|---|---|
| **Does** | Compares the human labels with the pipeline's: precision per label (v2.1, v2 and baselines) with 95% intervals, precision per reason, confusion table; agreement between A and B (Cohen's kappa); trailer precision |
| **Input** | `spot_A_done.xlsx` (required); `spot_B_done.xlsx`, `spot_C_done.xlsx`, `trailer_check_done.csv` (if present); `key.parquet` |
| **Output** | `results/s4_spot_precision.csv`; `s4_spot_reason_precision.csv`; `s4_spot_confusion.csv`; `s4_spot_agreement.json`; `s4_trailer_precision.json`; with B's sheet also `s4_spot_rater_confusion.csv`, `s4_spot_overlap_precision.csv` and the private `annotation/rater_disagreements.csv`; with C's sheet also `s4_spot_rater_pairs.csv`, `s4_spot_rater_pair_confusion.csv`, `s4_spot_rater_c_precision.csv` and the private `annotation/rater_c_disagreements.csv` |
| **Data** | 120 labelled rows → 28, 55 and 68 rows |

**Check:** `python -m pytest -q tests/s4_validation`

---

## s6 Report stage

**Code:** `src/skillsig/s6_report/`

**What it does:** draws the charts of [docs/report/FINDINGS.md](report/FINDINGS.md) from the finished outputs of s0–s4. It measures nothing new: each chart reads a table an earlier step wrote. A chart whose inputs are missing is skipped with a warning, so it also runs on a partial pipeline.

**Design notes:** [stages/s6_report/](stages/s6_report/README.md)

### 6.1 Report figures

```bash
python -m skillsig export
```

| Item | Details |
|---|---|
| **Does** | Draws 17 charts, including the independent evidence matrix, finding and appendix figures. The edit-robustness chart comes from step 4.3 |
| **Input** | `results/s3_*.csv`, `results/s4_*.csv`, `s2_repo`, `s2_types`, `s2_clusters`, `s3_text_methods_oof`; `s0_devsample_ids` and `artifacts` for the dev-sample chart; MinHash settings from the config |
| **Output** | `figures/report/*.png`, each with a manifest |
| **Data** | 17 figures |

Code changes are not tracked: after editing a chart, run `python -m skillsig export --force`.

**Check:** `python -m pytest -q tests/s6_report`

## s5: not implemented

The CLI lists `analyze` and `rq2` (`s5_analysis`), but that package does not exist yet. Running them prints `not implemented`.

## Checks for the whole pipeline

| Command | What it checks |
|---|---|
| `python -m pytest -q` | All 411 unit tests, on small fixtures (no dataset needed) |
| `python -m pytest -q -m data tests/regression` | Known counts on the full data (v1 label counts, registry coverage) |

## Notes

- **One-shot runner:** `scripts/run_full.sh` runs every step above except P.1 and 3.7 with `--force`, then draws the report figures, and keeps clones unfetched unless `FETCH=1`. Once `spot_A_done.xlsx` exists it scores the existing sample (`spot_refresh`, then `spot_check`) instead of drawing new sheets.
- **`python -m skillsig all` and `make smoke`** stop at the registry stage, which has no default module. Run the steps one by one.
- **Output folders:** `src/data/derived/` holds every intermediate table (not in git). `results/` holds result tables and their manifests (in git), except `results/annotation/`, which is git-ignored. `figures/` holds figures.
