# Coding flow and algorithm design

This document describes the pipeline flow, the algorithms used in each step, the reasons for choosing them, and the generated outputs. Commands, inputs and outputs are listed in the shorter [pipeline runbook](PIPELINE_RUNBOOK.md).

## Purpose

The analysis examines text, reuse patterns and commit records in `SKILL.md` instruction files to identify evidence about how skills were produced. We can observe identical text and recorded AI disclosures. We usually cannot verify who wrote the original text.

The flow is: **download → prepare tables → check data → read registry history → measure features → apply rules and test text methods → evaluate → human review → report**. The paired reuse review is a separate branch after the features and labels exist.

## Pipeline flow

```text
Step 0: Raw dataset
    |
    +--> Steps 1a-1d: SQL summary tables
    |          |
    |          v
    |      Step 2: Schema checks + development sample
    |
    +--> Steps 3a-3e: Registry history, versions and matches
    |          |
    |          v
    +--> Steps 4a-4e: Structure, commits, repository, topic, wording
    |
    +--> Steps 5a-5d: Clusters, registry similarity, specificity, scaffold
               |
               v
         Steps 6a-6h: Label rules, text models, evidence tables
               |
               +--> Steps 7a-7e: Automated evaluations
               |
               +--> Step 8 -> Step 9 -> Step 10
               |    Draw      Human     Score
               |    sample    review    answers
               |
               +--> Step 8p -> Step 9p -> Step 10p
                    Paired      Human      Summarize
                    texts       review     judgments

Saved feature and result tables -> Step 11: Report figures
```

The diagram groups the stages. The input links in each step show its exact file dependencies. Step 8r refreshes an existing sample after label changes. Step 6g is an optional experiment; it is excluded from the main runner.

## Key terms

| Word | Simple meaning |
| --- | --- |
| Skill | A set of instructions an AI assistant can follow, stored in `SKILL.md` |
| Artifact | One file at one repository path in the raw dataset |
| Distinct text | One unique file content; identical copies share the same `file_sha` |
| Representative | The one raw row with `dedup_primary = 1` chosen for a distinct text |
| Registry | One of the selected repositories that publishes a collection of skills |
| Blob | Git’s content hash for one file version; used here to match file contents |
| Feature | A measured property, such as body length or an AI trailer flag |
| Trailer / disclosure | A commit note that names an AI co-author; it may describe writing, installation or maintenance |
| Label | A decision made by rules; it is not automatically a verified answer |
| Threshold | A chosen boundary, such as at least ten copies |
| Parquet | A compact file format for tables; it is not a separate database server |
| Front matter | The YAML header between `---` lines, usually containing the skill name and description |
| SQL | A language for selecting, joining and counting table records |
| NULL | A missing or unavailable value; it does not automatically mean zero or false |

## Shared choices

Most label tables have one row per distinct text, not per file copy. The raw dataset has 3,797,117 file rows and 1,877,981 distinct texts. Text features use 1,840,872 readable distinct texts: at least 200 body characters and no pointer or redirect stub. Copy counts still use all file rows. Commit evidence usually comes from the representative row, so histories of other copies may differ.

DuckDB reads Parquet directly and runs SQL joins and counts on this computer. Each stage writes a table that later stages can read without rerunning earlier work. Settings live in [`config/default.yaml`](../config/default.yaml), including the random seed `20261015`. Manifests beside pipeline outputs record inputs, config, code commit and row count. SQL preparation files and hand-completed annotations do not use that same automatic manifest process. Inputs and config control skipping; code edits require `--force`.

**Output examples:** the tables below contain two records from each available saved table. Only selected columns are shown. Numbers are rounded for display and long text cells are shortened. Hash IDs are kept in full. Rows are examples, not random samples or population summaries; different tables may show different skills. JSON examples show two saved fields, not database rows. Missing outputs are clearly marked.

## Step index

- [Step 0: Set up Python and download the dataset](#step-0)
- [Step 1a: Count content and structure](#step-1a)
- [Step 1b: Record agent folders](#step-1b)
- [Step 1c: Build repository summaries](#step-1c)
- [Step 1d: Describe bundled files](#step-1d)
- [Step 2: Check the data and make a development sample](#step-2)
- [Step 3a: Read the history of the registry collections](#step-3a)
- [Step 3b: Build the distinct-version table](#step-3b)
- [Step 3c: Match registry texts and summarize their history](#step-3c)
- [Step 3d: Compare consecutive versions](#step-3d)
- [Step 3e: Find popular texts outside the registry](#step-3e)
- [Step 4a: Load structure features](#step-4a)
- [Step 4b: Measure commit-history signals](#step-4b)
- [Step 4c: Describe repository context](#step-4c)
- [Step 4d: Assign skill topics](#step-4d)
- [Step 4e: Measure wording](#step-4e)
- [Step 5a: Group near-duplicate texts](#step-5a)
- [Step 5b: Find registry similarity](#step-5b)
- [Step 5c: Measure project-specific identifiers](#step-5c)
- [Step 5d: Find shared structure and scaffolds](#step-5d)
- [Step 6a: Reproduce the first label rules](#step-6a)
- [Step 6b: Add registry and cluster evidence](#step-6b)
- [Step 6c: Add disclosure and revision flags](#step-6c)
- [Step 6d: Build simple comparison rules](#step-6d)
- [Step 6e: Test text style against disclosure](#step-6e)
- [Step 6f: Compare several text methods](#step-6f)
- [Step 6g: Optional: score skills without history](#step-6g)
- [Step 6h: Report reuse and disclosure separately](#step-6h)
- [Step 7a: Check how many disclosures first and last commits reveal](#step-7a)
- [Step 7b: Check recovery of exact registry texts](#step-7b)
- [Step 7c: Test similarity after controlled edits](#step-7c)
- [Step 7d: Vary one rule threshold at a time](#step-7d)
- [Step 7e: Check scaffold overlap and prepare examples](#step-7e)
- [Step 8: Draw the blind authorship review sample](#step-8)
- [Step 8r: Refresh the same sample after label changes](#step-8r)
- [Step 9: People complete the blind review](#step-9)
- [Step 10: Score the completed blind review](#step-10)
- [Step 8p: Separate branch: prepare paired reuse review](#step-8p)
- [Step 9p: Separate branch: people review paired texts](#step-9p)
- [Step 10p: Separate branch: summarize paired judgments](#step-10p)
- [Step 11: Draw the report figures](#step-11)

<a id="step-0"></a>

## Step 0: Set up Python and download the dataset

**Run:** see [runbook step 0](PIPELINE_RUNBOOK.md#step-0). **Code:** [`src/data/download_data.py`](../src/data/download_data.py).

**Inputs and source steps:** `pyproject.toml`; `Hugging Face: mvaccargiu/gitskills`.

**Outputs:** `src/data/gitskills_data/data/artifacts/*.parquet`; `src/data/gitskills_data/data/artifact_siblings/*.parquet`; `src/data/gitskills_data/data/repos/*.parquet`; `src/data/gitskills_data/data/mining_runs/*.parquet`.

**Processing and algorithm**

The download script copies the existing GitSkills dataset to this computer. It does not collect GitHub files itself. The dataset has four tables: skill files, files stored beside them, repositories, and collection runs. The script writes relative to its working folder, so run it inside `src/data/`.

**Why this approach**

Using one saved dataset gives every later step the same starting point. Parquet stores tables in a compact form and lets DuckDB read only the columns a query needs.

**Output meaning and limits**

`artifacts` has one row per file location. Different rows can hold the same text. `dedup_primary = 1` selects one representative of each distinct text. Repository dates describe the repository, not the date a skill was written.

**Saved output examples**

`src/data/gitskills_data/data/artifacts/*.parquet`

| `repo_full_name` | `name` | `file_sha` | `dedup_primary` | `history_fetched` |
| --- | --- | --- | --- | --- |
| williamfiset/algorithms | NULL | 13a96ab46007faee7b417f18c56feef5ff2867c3 | 1 | 0 |
| alirezarezvani/claude-skills | NULL | 12f4e1a936b32578173e1fba0cffb93d1b6e265a | 0 | 0 |

`src/data/gitskills_data/data/artifact_siblings/*.parquet`

| `repo_full_name` | `entry_name` | `entry_type` | `entry_size` |
| --- | --- | --- | --- |
| 0-AI-UG/cate | examples | dir | 0 |
| 0-AI-UG/cate | examples/midnight-ember.cate-theme.json | file | 3292 |

`src/data/gitskills_data/data/repos/*.parquet`

| `full_name` | `stars` | `language` | `created_at` |
| --- | --- | --- | --- |
| williamfiset/algorithms | 18673 | Java | 2017-03-18T02:50:46Z |
| alirezarezvani/claude-skills | 22655 | Python | 2025-10-19T04:04:05Z |

`src/data/gitskills_data/data/mining_runs/*.parquet`

| `run_id` | `artifact_type` | `finished_at` | `discovered` |
| --- | --- | --- | --- |
| 1 | agent-skills | 2026-07-09T18:04:34.751737+00:00 | 11796 |
| 2 | agent-skills | 2026-07-12T06:54:51.308396+00:00 | 1328473 |

<a id="step-1a"></a>

## Step 1a: Count content and structure

**Run:** see [runbook step 1a](PIPELINE_RUNBOOK.md#step-1a). **Code:** [`db/content_and_structure.sql`](../db/content_and_structure.sql).

**Inputs and source steps:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `src/data/generated_data/skill_features.parquet`.

**Processing and algorithm**

Select distinct, readable skills. Remove the YAML header when measuring the body. Count words, headings, lists, code blocks, links and selected header fields.

**Why this approach**

SQL counts are fast and easy to inspect. Building this table once also lets the old notebooks and the new pipeline use the same measures.

**Output meaning and limits**

One row describes one readable text. These counts describe its format; they do not identify its author.

**Saved output examples**

`src/data/generated_data/skill_features.parquet`

| `file_sha` | `body_chars` | `body_words` | `headings` | `code_blocks` |
| --- | --- | --- | --- | --- |
| 56253f7a907b0bdb23268362fa91c8a31229a523 | 201 | 17 | 0 | 0 |
| 38fbebeecc866431802dda3c0bf0ffafa71ca88d | 201 | 28 | 0 | 0 |

<a id="step-1b"></a>

## Step 1b: Record agent folders

**Run:** see [runbook step 1b](PIPELINE_RUNBOOK.md#step-1b). **Code:** [`db/agent_ecosystem.sql`](../db/agent_ecosystem.sql).

**Inputs and source steps:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `src/data/generated_data/file_agents.parquet`.

**Processing and algorithm**

Read each file path and record which agent folder it belongs to, such as `.claude` or `.cursor`. Keep every file location, including identical copies.

**Why this approach**

A path rule is simple and repeatable. Keeping locations is necessary because the same text can be installed for several tools.

**Output meaning and limits**

The folder shows where a file is used. It does not prove that the named tool wrote it.

**Saved output examples**

`src/data/generated_data/file_agents.parquet`

| `repo_full_name` | `path` | `file_sha` | `agent_folder` |
| --- | --- | --- | --- |
| williamfiset/algorithms | .gemini/SKILL.md | 13a96ab46007faee7b417f18c56feef5ff2867c3 | Gemini CLI |
| alirezarezvani/claude-skills | .gemini/skills/tc/SKILL.md | 12f4e1a936b32578173e1fba0cffb93d1b6e265a | Gemini CLI |

<a id="step-1c"></a>

## Step 1c: Build repository summaries

**Run:** see [runbook step 1c](PIPELINE_RUNBOOK.md#step-1c). **Code:** [`db/repos.sql`](../db/repos.sql).

**Inputs and source steps:** [`src/data/gitskills_data/data/repos/*.parquet`](#step-0); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `src/data/generated_data/repo_profile.parquet`.

**Purpose**

Create one summary row per repository containing skill files. This provides context: a repository with thousands of skills may be a collection, while one with a few skills may be an application project. Later steps use these counts and metadata to examine that difference.

**Data flow**

```text
artifacts (step 0)                 repos (step 0)
    |                                  |
    v                                  |
Group and count by repository          |
    |                                  |
    +---------- join by name ----------+
                     |
                     v
           Add name flags and size group
                     |
                     v
           repo_profile.parquet
           One row per repository
```

**Processing and algorithm**

Use SQL `GROUP BY` to collect artifact rows by repository name. Calculate `files` (all skill file locations), `distinct_skills` (different `file_sha` values), and `copy_rows` (rows with `dedup_primary = 0`). Calculate `copy_share = copy_rows / files`, rounded to three decimal places.

Join these counts to repository metadata using `repo_full_name = full_name`. This adds owner, stars, forks, language, license and dates. Only repositories present in both inputs enter the output. Check the full repository name for skill, collection and agent-related words to create name flags. Assign a `size_tier` based on the number of skill files: `1`, `2-5`, `6-20`, `21-100`, `101-1000`, or `>1000`.

**Why this approach**

Grouping, counting and joining use information already present in the dataset, so no trained model is needed. Saving one repository-level table avoids repeating these calculations in later steps.

**Where it is used later**

- [Step 4c](#step-4c) reads this table, adds sharing measures, and assigns a repository kind: collection, agent tooling or general project.
- [Step 4d](#step-4d) uses the step 4c output to summarize skill topics by repository kind and language.

These values provide context rather than directly deciding authorship. The [v2 label step](#step-6b) requires the step 4c file as a dependency, but its values do not choose the label.

**Output meaning and limits**

`files` counts file locations; `distinct_skills` counts different contents. Three files containing texts A, A and B therefore give three files and two distinct skills.

`copy_rows` counts non-representative occurrences in the whole dataset. Each distinct text has one selected representative (`dedup_primary = 1`); other occurrences have `0`. A high `copy_share` does not prove that this repository copied from another repository or identify the original author. Name flags are rough text matches, not confirmed repository categories.

**Saved output examples**

`src/data/generated_data/repo_profile.parquet`

| `full_name` | `files` | `distinct_skills` | `copy_share` | `size_tier` |
| --- | --- | --- | --- | --- |
| tomharris/engineer-agent | 17 | 17 | 0 | 6-20 |
| navikt/dialogmote-frontend | 16 | 16 | 0.938 | 6-20 |

<a id="step-1d"></a>

## Step 1d: Describe bundled files

**Run:** see [runbook step 1d](PIPELINE_RUNBOOK.md#step-1d). **Code:** [`db/siblings_data.sql`](../db/siblings_data.sql).

**Inputs and source steps:** [`src/data/gitskills_data/data/artifact_siblings/*.parquet`](#step-0).

**Outputs:** `src/data/generated_data/sibling_files.parquet`.

**Purpose**

Describe the extra files bundled with each skill, such as scripts, reference documents, templates and configuration. A skill package may contain `SKILL.md`, `scripts/fetch-docs.js` and `references/guide.md`. This step makes the supporting files easier to count and compare.

**Data flow**

```text
artifact_siblings (step 0)
          |
          v
Keep entries where entry_type = 'file'
          |
          v
Extract folder and file extension
          |
          v
Assign category using extension rules
          |
          v
sibling_files.parquet
One row per bundled file occurrence
```

**Processing and algorithm**

The input already has one row per bundled entry. SQL filters and adds fields to these records; it does not download or open the bundled files.

1. Keep only rows with `entry_type = 'file'`. Directory entries are excluded, but their files remain.
2. Set `top_folder` to the first part of `entry_name`, before `/`. Set `subfolder` to that same part when the path contains `/`; otherwise use `(skill root)`.
3. Extract the final file extension and convert it to lowercase.
4. Use an ordered SQL `CASE` expression to assign a category from the extension.

| Extension examples | Category |
| --- | --- |
| `py`, `js`, `sh`, `ts` | `script / code` |
| `md`, `txt`, `rst` | `documentation` |
| `json`, `yaml`, `csv` | `data / config` |
| `png`, `pdf`, `mp4` | `media / asset` |
| `html`, `css`, `tsx` | `web / template` |
| No extension | `no extension` |
| Unlisted extension | `other` |

Preserve the repository name, associated skill path, entry name, file size, content hash and recorded reason for skipped collection.

**Why this approach**

File extensions provide a simple way to classify the supporting files in a skill package. Fixed rules make this classification fast and repeatable without reading file contents or training a model. The resulting table supports counts and comparisons of bundled file types and folder patterns.

**Where it is used later**

- [Step 2](#step-2) opens this generated table as part of the data checks.
- [`EDA_siblings.ipynb`](../src/notebooks/EDA_siblings.ipynb) uses it to examine bundled file types and folder patterns.

[Step 5d](#step-5d) reads the raw `artifact_siblings` table from [step 0](#step-0) directly, rather than this generated summary. This step does not assign an authorship label.

**Output meaning and limits**

One row describes one bundled file occurrence, not one unique file content. Identical supporting files bundled with different skills can appear more than once.

`subfolder` stores only the first folder, not the complete nested path. For a root-level file such as `README.md`, `top_folder` contains the filename and `subfolder` contains `(skill root)`.

Categories depend on filenames rather than actual behavior. A `.js` file is classified as code, but this does not establish what it does or whether it works.

**Saved output examples**

`src/data/generated_data/sibling_files.parquet`

| `entry_name` | `top_folder` | `subfolder` | `ext` | `file_category` |
| --- | --- | --- | --- | --- |
| scripts/fetch-docs.js | scripts | scripts | js | script / code |
| scripts/tests/run-tests.js | scripts | scripts | js | script / code |
| README.md | README.md | (skill root) | md | documentation |

<a id="step-2"></a>

## Step 2: Check the data and make a development sample

**Run:** see [runbook step 2](PIPELINE_RUNBOOK.md#step-2). **Code:** [`src/skillsig/s0_intake/`](../src/skillsig/s0_intake/).

**Inputs and source steps:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/gitskills_data/data/artifact_siblings/*.parquet`](#step-0); [`src/data/gitskills_data/data/repos/*.parquet`](#step-0); [`src/data/gitskills_data/data/mining_runs/*.parquet`](#step-0); [`src/data/generated_data/skill_features.parquet`](#step-1a); [`src/data/generated_data/file_agents.parquet`](#step-1b); [`src/data/generated_data/repo_profile.parquet`](#step-1c); [`src/data/generated_data/sibling_files.parquet`](#step-1d).

**Outputs:** `src/data/derived/s0_devsample_ids.parquet`.

**Purpose**

Check that the expected dataset is available, then select 50,000 distinct skills for faster development runs.

**Data flow**

```text
Raw tables (step 0) + generated tables (steps 1a-1d)
                         |
                         v
                Open eight DuckDB views
                         |
                         v
              Check columns and row counts
              Stop if a check fails
                         |
                         v
        Group by location, history and copy count
                         |
                         v
       Allocate places and select IDs with fixed seed
                         |
                         v
              s0_devsample_ids.parquet
```

**Processing and algorithm**

1. Open the eight tables as DuckDB views. Views let SQL read the existing Parquet files without copying the dataset.
2. Check that all views and specified required columns exist. In full mode, also check these exact counts:

| Check | Expected count |
| --- | ---: |
| Artifact file rows | 3,797,117 |
| Representative rows (`dedup_primary = 1`) | 1,877,981 |
| Repositories | 282,200 |
| Collection runs | 7 |

3. Use **stratified sampling**: divide distinct skills into groups by location class, available history (`0` or `1`), and exact-copy count (`1`, `2-9`, or `>=10`). Location and history come from the representative row; copy counts use all occurrences.
4. Give every group at least one place, then allocate the remaining places approximately in proportion to group size. Within each group, order IDs by `hash(file_sha + seed)` and take the allocated number. The configured seed is `20261015`.

**Why this approach**

Data checks catch missing columns and unexpected dataset sizes before expensive processing. Stratified sampling keeps small groups present while preserving the main mix of locations, history and copies. These properties affect which label rules can apply. A fixed seed makes selection repeatable, and the smaller sample speeds up development.

**Where it is used later**

Later steps supporting `--dev-sample` read these IDs to restrict processing. Without the flag, they normally process the full population. Many counts, such as exact-copy counts and commit batch sizes, still use the full population so sampling does not change their meaning. Near-duplicate clusters are formed within the sample in development mode.

**Output meaning and limits**

The output stores IDs and group names, not copied skill texts. For example, `skills-dir|history=0|copies=2-9` means a representative in the `skills-dir` location class, unavailable history, and two to nine exact file occurrences across the dataset.

The checks cover table availability, specified column names and selected row counts, not every value or data type. Development mode skips the exact row-count checks. An unchanged input/config manifest can skip the step entirely; use `--force` to rerun it.

Development and full runs share output paths. Keep a complete chain in one mode and rebuild the full chain after a development run before using full-data results.

**Saved output examples**

`src/data/derived/s0_devsample_ids.parquet`

| `file_sha` | `stratum` |
| --- | --- |
| 000050a706b4f628641f2990ede1a4424c3bcbb2 | skills-dir&#124;history=0&#124;copies=2-9 |
| 0001f6b93877d9809d0127c5d9aa3048911ceb66 | skills-dir&#124;history=0&#124;copies=2-9 |

<a id="step-3a"></a>

## Step 3a: Read the history of the registry collections

**Run:** see [runbook step 3a](PIPELINE_RUNBOOK.md#step-3a). **Code:** [`src/skillsig/s1_registry/mine_git.py`](../src/skillsig/s1_registry/mine_git.py).

**Inputs and source steps:** `config/registry_sources.yaml`; `src/data/registry_repos/`.

**Outputs:** `src/data/derived/s1_versions_raw.parquet`; `src/data/derived/s1_commit_trailers.parquet`.

**Purpose**

Collect earlier skill versions and commit disclosures from selected registry repositories. The raw dataset is a snapshot; Git history shows changes that happened before and after that snapshot.

**Data flow**

```text
registry_sources.yaml
          |
          v
Select repositories marked "mined"
          |
          v
Open local clones; download missing clones
          |
          +-------------------------+
          |                         |
          v                         v
Read SKILL.md changes       Read commit co-author lines
          |                         |
          v                         v
s1_versions_raw.parquet     s1_commit_trailers.parquet
```

**Processing and algorithm**

1. Load sources marked `mined` from the configuration; exclude `candidate` sources. Configured mirrors reuse their source repository's mined records.
2. Open local clones, downloading missing ones with `--filter=blob:none --no-checkout`. These options defer file-content downloads and avoid creating working copies.
3. Parse Git logs for additions and changes to `SKILL.md`. Save repository, commit ID, date, path, skill name, content hash (`blob`) and change status. Exclude deletion records because they have no resulting file version.
4. Separately parse `Co-authored-by` trailers for recognized AI tools. Save one row per commit, including commits with no detected AI trailer.

A **commit hash** identifies a repository change; a **blob hash** identifies one file's exact content. One commit can change several skills, and several commits can contain the same file content.

**Why this approach**

Git directly provides version hashes, dates and commit records, so no trained model is needed. Local clones make extraction repeatable, while deferred file downloads reduce initial storage. Separate version and disclosure tables keep file changes distinct from repository-wide commit evidence.

**Where it is used later**

- [Step 3b](#step-3b) groups repeated records into distinct versions and adds introduction-time disclosure information.
- [Step 3c](#step-3c) uses the step 3b versions to match registry content hashes to dataset files.
- [Step 7a](#step-7a) uses both outputs to compare disclosures across recorded skill history with those visible in first and last commits.

**Output meaning and limits**

A version row is a recorded file change, not yet a distinct text. Status `A` means added and `M` means modified. A trailer row describes a commit: an empty `ai_tools` list means no recognized disclosure was found, not confirmed human authorship. A disclosure may concern writing, installation or maintenance. This step does not assign authorship labels or confirm original sources.

The command keeps existing clones from fetching new branch history. Missing clones are still downloaded. Later steps can download missing file objects by exact hash; this flag does not make the whole pipeline offline.

**Saved output examples**

`src/data/derived/s1_versions_raw.parquet`

| `repo` | `skill` | `commit_date` | `blob` | `status` |
| --- | --- | --- | --- | --- |
| GoogleCloudPlatform/gke-mcp | gke-cluster-creator | 2026-01-20 | a18b723fb522a9e4c442e9240af4d0b52e88d587 | A |
| GoogleCloudPlatform/gke-mcp | gke-inference-quickstart | 2026-01-20 | 458a7401f244603749938d7f6f5ada8a4cdb26a1 | A |

`src/data/derived/s1_commit_trailers.parquet`

| `repo` | `commit` | `ai_tools` | `has_ai_trailer` |
| --- | --- | --- | --- |
| GoogleCloudPlatform/gke-mcp | 00031ffe19e52ddfe701501305cfb8f7fc24f5b2 | [] | false |
| GoogleCloudPlatform/gke-mcp | 004e535836a9f0ebcf12cae290b153a0886624e6 | [] | false |

<a id="step-3b"></a>

## Step 3b: Build the distinct-version table

**Run:** see [runbook step 3b](PIPELINE_RUNBOOK.md#step-3b). **Code:** [`src/skillsig/s1_registry/versions.py`](../src/skillsig/s1_registry/versions.py).

**Inputs and source steps:** [`src/data/derived/s1_versions_raw.parquet`](#step-3a); [`src/data/derived/s1_commit_trailers.parquet`](#step-3a); [`src/data/gitskills_data/data/mining_runs/*.parquet`](#step-0); [`src/data/registry_repos/`](#step-3a).

**Outputs:** `src/data/derived/s1_registry_versions.parquet`.

**Purpose**

Turn raw change records into one row per distinct version of each registry skill. A commit is a change event; a version here is distinct file content. For example, a history of A → B → A has three changes but only two distinct versions.

**Data flow**

```text
Raw versions + commit trailers (step 3a)
                    |
                    v
    Group by repository + skill + blob
                    |
                    v
    First/last dates + commit counts
                    |
                    v
    Assign lineage ID and version order
                    |
                    v
    Add disclosure and snapshot flags
                    |
                    v
    Read file size and description opening
                    |
                    v
       s1_registry_versions.parquet
```

**Processing and algorithm**

1. Remove incomplete records and join trailer flags using repository and commit ID.
2. Group by `(repo, skill, blob)`. Keep the earliest recorded commit, first and last recorded dates, and count of distinct commits for each content version.
3. Order distinct versions by first recorded date, using commit and blob hashes to break ties. Assign `version_seq` values of 1, 2, 3… and a `lineage_id` such as `anthropics/skills::pdf`.
4. Add the fields below. Read file size and description text from the registry clones.

| Field | Meaning |
| --- | --- |
| `ai_trailer_at_introduction` | A recorded commit on the version's first day had a recognized AI trailer |
| `post_snapshot` | The version first appeared after the latest collection date in `mining_runs` from [step 0](#step-0) |
| `size_bytes` | File-content size |
| `desc_idiom` | Description-opening category, such as “Use when…” or a capability summary |
| `renamed_group` | Shared identifier for identical content under multiple skill names in one repository |

**Why this approach**

Grouping by content hash avoids counting repeated content as new versions. Dates and sequence numbers support version comparisons. Flagging later versions instead of deleting them preserves history while keeping the dataset's time boundary visible.

**Where it is used later**

- [Step 3c](#step-3c) matches version hashes to dataset files and builds lineage summaries.
- [Step 3d](#step-3d) compares consecutive distinct versions to measure changes.
- [Step 5b](#step-5b) uses registry versions as reference texts for similarity matching.

**Output meaning and limits**

`version_seq` follows first appearances of distinct contents, not every transition. In A → B → A, the return to A is combined with its earlier occurrence. A lineage groups a skill's recorded versions, not confirmed authors. `renamed_group` shows shared content rather than proving a rename. An introduction-day trailer records commit involvement, not original authorship.

**Saved output examples**

`src/data/derived/s1_registry_versions.parquet`

| `lineage_id` | `blob` | `version_seq` | `first_date` | `post_snapshot` |
| --- | --- | --- | --- | --- |
| GoogleCloudPlatform/gke-mcp::custom-golden-image-discovery | bad33b53d33f140792def6ff4c86d899a27b286a | 1 | 2026-03-17 | false |
| GoogleCloudPlatform/gke-mcp::gke-ai-troubleshooting-handle-disruption-gpu-tpu | b13a4cec5c3a867a9b10c3ce56d04748af5b869c | 1 | 2026-06-29 | false |

<a id="step-3c"></a>

## Step 3c: Match registry texts and summarize their history

**Run:** see [runbook step 3c](PIPELINE_RUNBOOK.md#step-3c). **Code:** [`src/skillsig/s1_registry/match.py`](../src/skillsig/s1_registry/match.py).

**Inputs and source steps:** [`src/data/derived/s1_registry_versions.parquet`](#step-3b); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/gitskills_data/data/mining_runs/*.parquet`](#step-0); `config/registry_sources.yaml`.

**Outputs:** `src/data/derived/s1_registry_matches.parquet`; `src/data/derived/s1_lineages.parquet`.

**Data flow**

```text
Registry versions (step 3b)       Dataset files (step 0)
          blob                        file_sha
            \                          /
             +---- join equal hashes --+
                           |
                           v
                 Exact-match counts
                           |
               +-----------+-----------+
               |                       |
               v                       v
       Registry matches          Lineage summaries
```

**Processing and algorithm**

Join registry blob hashes to dataset `file_sha` values. Count files, repositories and owners with each text. If several listed registries contain it, assign the earliest recorded registry as the origin candidate. Build a history summary for each lineage, including versions and copies outside its source repository.

**What the counts mean**

The two outputs use different units. `s1_registry_matches.parquet` has one row per matched content hash. `s1_lineages.parquet` has one row per registry skill, combining its retained versions. The column name is `versions_seen`, not `version_seen`.

| Field | Calculation and meaning | What it helps measure |
| --- | --- | --- |
| `files` | Count every dataset file location holding one exact content hash, including occurrences in the source repository. | How widely one version's exact text appears. Several paths in one repository count separately. |
| `owners` in matches | Count distinct accounts holding that exact text. The owner is the part before `/` in `owner/repository`. | Breadth across accounts: 100 files under one account differ from 100 files across 100 accounts. It counts accounts, not individual authors. |
| `versions` | Count distinct content hashes retained for a lineage from [step 3b](#step-3b). Normally exclude versions introduced after the snapshot; if a lineage has no earlier versions, retain its later versions. | How many different texts were recorded for a skill, rather than how many commits occurred. |
| `versions_seen` | Count retained versions whose exact hash appears anywhere in the dataset, including the source repository. | Dataset coverage of the recorded versions. A version can be seen without any external copies. |
| `copies` | For each retained version, subtract its occurrences inside the lineage's source repository from `files`, then sum across versions. | Exact file occurrences outside that source repository. This counts files, not distinct owners or repositories. |
| `owners` in lineages | Take the union of accounts holding any retained, matched version, then count each account once. Include the source account if its files appear in the dataset. | Reach across accounts for the skill as a whole, without counting the same account again for each version. |
| `span_days` | Days between the earliest `first_date` and latest `last_date` among retained version records. | The observed time span of recorded changes. It is not the skill's age, update frequency or time since its last change. |

**Count example**

The following is an illustrative calculation, not a saved database sample. Suppose a lineage has three retained versions:

| Version | Files in source repository | Files elsewhere | Seen in dataset? |
| --- | ---: | ---: | --- |
| A | 1 | 3 | Yes |
| B | 1 | 1 | Yes |
| C | 0 | 0 | No |

Version A has `files = 4`. Across the lineage, `versions = 3`, `versions_seen = 2`, and `copies = (4 - 1) + (2 - 1) = 4`. If the same outside account holds A and B, it counts only once in the lineage's `owners`. If the earliest and latest retained change dates are 30 days apart, `span_days = 30`.

**Why this approach**

Hash matching finds byte-identical text without comparing long documents. It is a strong check of content identity. Dates provide a clear rule for choosing among the listed sources.

**Where it is used later**

- [Step 3e](#step-3e) uses matched hashes to exclude already-covered texts from popular-source discovery. It calculates popularity counts separately from the raw dataset.
- [Step 4c](#step-4c) uses matched hashes and origin repositories to calculate repository-level origin and copied-in shares.
- [Step 5b](#step-5b) uses matched hashes to recognize exact registry matches and assign their origin lineage before considering approximate similarity.

The lineage counts (`versions`, `versions_seen`, `copies`, `owners`, `span_days`) summarize version coverage, external occurrences and change history. No implemented downstream stage currently reads `s1_lineages.parquet` directly; these fields are not inputs to the current authorship rules. Likewise, the later consumers of the match table mainly use hash and origin fields rather than its saved `files` and `owners` totals.

**Output meaning and limits**

Equal hashes support equal content, not the direction of copying or original authorship. The earliest listed registry may not be the real first publisher. Copy totals can overlap across lineages sharing the same content, so do not add them together. These matches count exact content only; edited copies need similarity analysis.

Here, `copies` excludes the source repository. The `copies` field calculated separately in [step 6a](#step-6a) counts all dataset occurrences of a text, so the two fields are not interchangeable. `span_days` can be positive even with one distinct version if that same content was recorded again later. The snapshot filter checks version introduction dates; it does not cap every retained `last_date` at the snapshot.

**Saved output examples**

`src/data/derived/s1_registry_matches.parquet`

| `blob` | `files` | `owners` | `origin_repo` | `origin_skill` |
| --- | --- | --- | --- | --- |
| 000432afe96102f283e5b82b0fa755f8d2d32c19 | 83 | 28 | wshobson/agents | bats-testing-patterns |
| 002c2daede7d36a96a3fd566b2119682194f27d3 | 8 | 8 | nextlevelbuilder/ui-ux-pro-max-skill | banner-design |

`src/data/derived/s1_lineages.parquet`

| `lineage_id` | `versions` | `versions_seen` | `copies` | `span_days` |
| --- | --- | --- | --- | --- |
| GoogleCloudPlatform/gke-mcp::custom-golden-image-discovery | 1 | 1 | 2 | 0 |
| GoogleCloudPlatform/gke-mcp::gke-ai-troubleshooting-handle-disruption-gpu-tpu | 2 | 1 | 0 | 14 |

<a id="step-3d"></a>

## Step 3d: Compare consecutive versions

**Run:** see [runbook step 3d](PIPELINE_RUNBOOK.md#step-3d). **Code:** [`src/skillsig/s1_registry/changes.py`](../src/skillsig/s1_registry/changes.py).

**Inputs and source steps:** [`src/data/derived/s1_registry_versions.parquet`](#step-3b); [`src/data/registry_repos/`](#step-3a).

**Outputs:** `src/data/derived/s1_registry_changes.parquet`.

**Processing and algorithm**

Pair each version with the next version in its lineage. Compare body lines after normalizing case and spaces. Measure changed lines, description similarity, headings, commands and bundled files. Mark a change `substantive` if at least 5% of body lines changed, the description changed, or bundled files changed. Otherwise mark it `cosmetic`.

**Why this approach**

A line comparison fits Markdown and is easy to explain. Ignoring spacing avoids treating indentation fixes as major edits. A description change matters because the description helps an agent decide when to use a skill.

**Output meaning and limits**

The 5% boundary is a chosen rule, stored in config. A substantive change is not necessarily a quality improvement. This table supports change analysis; it does not decide authorship.

**Saved output examples**

`src/data/derived/s1_registry_changes.parquet`

| `lineage_id` | `from_date` | `to_date` | `body_change_pct` | `change_type` |
| --- | --- | --- | --- | --- |
| GoogleCloudPlatform/gke-mcp::gke-ai-troubleshooting-handle-disruption-gpu-tpu | 2026-06-29 | 2026-07-13 | 5.4054 | substantive |
| GoogleCloudPlatform/gke-mcp::gke-ai-troubleshooting-handle-disruption-gpu-tpu | 2026-07-13 | 2026-07-23 | 46.835 | substantive |

<a id="step-3e"></a>

## Step 3e: Find popular texts outside the registry

**Run:** see [runbook step 3e](PIPELINE_RUNBOOK.md#step-3e). **Code:** [`src/skillsig/s1_registry/discover_popular.py`](../src/skillsig/s1_registry/discover_popular.py).

**Inputs and source steps:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/gitskills_data/data/repos/*.parquet`](#step-0); [`src/data/derived/s1_registry_matches.parquet`](#step-3c).

**Outputs:** `results/s1_popular_candidates.csv`.

**Processing and algorithm**

Count the different owners that hold each distinct text. Exclude texts already covered by exact registry matches. Rank the remaining texts and save the top 200, with a candidate source repository.

**Why this approach**

Owner counts reduce the effect of one account publishing many copies. The list helps find important sources missing from the selected registry collections.

**Output meaning and limits**

A candidate source is a lead to investigate. Popularity and early dates do not prove who first wrote the text.

**Saved output examples**

`results/s1_popular_candidates.csv`

| `file_sha` | `name` | `files` | `owners` | `candidate_repo` |
| --- | --- | --- | --- | --- |
| 5be498e2585843c7137bf9a74e262f57415de5ce | frontend-design | 1245 | 897 | Ali-Dakheel/Souq |
| 2f14076e59e6ce5cd6f88007421a85f0bd772520 | verification-before-completion | 1252 | 866 | 1203992808/ChatGPTCaricature |

<a id="step-4a"></a>

## Step 4a: Load structure features

**Run:** see [runbook step 4a](PIPELINE_RUNBOOK.md#step-4a). **Code:** [`src/skillsig/s2_features/structure.py`](../src/skillsig/s2_features/structure.py).

**Inputs and source steps:** [`src/data/generated_data/skill_features.parquet`](#step-1a).

**Outputs:** `src/data/derived/s2_structure.parquet`.

**Processing and algorithm**

Copy the prepared content and structure columns into the pipeline feature table. In development mode, keep only the IDs from [step 2](#step-2).

**Why this approach**

Reusing the SQL output keeps notebook and pipeline measurements consistent instead of implementing the same counts twice.

**Output meaning and limits**

A feature is a measured property. More headings or longer text does not by itself mean AI involvement.

**Saved output examples**

`src/data/derived/s2_structure.parquet`

| `file_sha` | `body_words` | `headings` | `bullet_items` | `code_blocks` |
| --- | --- | --- | --- | --- |
| 00000818feedfd1e1d22f6bb42362f0c4e5213e4 | 1234 | 15 | 4 | 11 |
| 000009fe40ffb92ccb4350995d114f838be78237 | 1029 | 17 | 40 | 1 |

<a id="step-4b"></a>

## Step 4b: Measure commit-history signals

**Run:** see [runbook step 4b](PIPELINE_RUNBOOK.md#step-4b). **Code:** [`src/skillsig/s2_features/commit.py`](../src/skillsig/s2_features/commit.py).

**Inputs and source steps:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `src/data/derived/s2_commit.parquet`.

**Processing and algorithm**

For each representative text, read the first and last commit information. Detect AI co-author notes using fixed text patterns. Classify the first author as a user, coding-agent bot or other bot. Count how many skills share its first commit; six or more is a bulk batch. Also record commit count, time span and message class.

**Why this approach**

Fixed rules are easy to audit and do not need training labels. Counting batches across all files helps distinguish a single-file change from a large installation.

**Output meaning and limits**

`history_fetched = false` means history is unavailable. It is different from having history with no disclosure. A trailer records involvement in a commit; it does not prove who wrote the original skill.

**Saved output examples**

`src/data/derived/s2_commit.parquet`

| `file_sha` | `history_fetched` | `ai_trailer_first` | `coding_agent_bot` | `batch_size` | `commit_count` |
| --- | --- | --- | --- | --- | --- |
| 00000818feedfd1e1d22f6bb42362f0c4e5213e4 | true | false | false | 5 | 1 |
| 000009fe40ffb92ccb4350995d114f838be78237 | true | false | false | 2 | 1 |

<a id="step-4c"></a>

## Step 4c: Describe repository context

**Run:** see [runbook step 4c](PIPELINE_RUNBOOK.md#step-4c). **Code:** [`src/skillsig/s2_features/repo_context.py`](../src/skillsig/s2_features/repo_context.py).

**Inputs and source steps:** [`src/data/generated_data/repo_profile.parquet`](#step-1c); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/gitskills_data/data/mining_runs/*.parquet`](#step-0); [`src/data/derived/s1_registry_matches.parquet`](#step-3c).

**Outputs:** `src/data/derived/s2_repo.parquet`.

**Processing and algorithm**

Add shared-content and registry-origin shares to each repository. Apply rules in order: a collection-like name, at least 101 skill files, or at least 50% shared content gives `A_collection`. Otherwise a skill/tool name gives `B_agent_tooling`. Remaining repositories get `C_project`. Compute age against the dataset snapshot.

**Why this approach**

A clear rule set gives useful context without requiring people to classify hundreds of thousands of repositories.

**Output meaning and limits**

Repository kind is approximate context. `shared_share` and copying direction are not the same thing. The age is measured at collection time, not today.

**Saved output examples**

`src/data/derived/s2_repo.parquet`

| `repo_full_name` | `files` | `shared_share` | `repo_kind` | `age_days_at_snapshot` |
| --- | --- | --- | --- | --- |
| 0-AI-UG/cate | 3 | 0.66667 | A_collection | 113 |
| 0-AI-UG/zero-agent | 1 | 0 | B_agent_tooling | 128 |

<a id="step-4d"></a>

## Step 4d: Assign skill topics

**Run:** see [runbook step 4d](PIPELINE_RUNBOOK.md#step-4d). **Code:** [`src/skillsig/s2_features/skill_type.py`](../src/skillsig/s2_features/skill_type.py).

**Inputs and source steps:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/derived/s2_repo.parquet`](#step-4c).

**Outputs:** `src/data/derived/s2_types.parquet`; `results/s2_skill_type_by_project.csv`.

**Processing and algorithm**

Match keyword lists against the skill name and description. Use the body as a fallback when needed. Record the selected type, all matching types and where the match came from. Count types by repository kind and language.

**Why this approach**

Keyword rules are a small, readable starting point. They give topic context without needing a hand-labelled training set.

**Output meaning and limits**

A topic is not an authorship label. Several topics may match; some skills remain unclassified. The summary counts file occurrences rather than just distinct texts.

**Saved output examples**

`src/data/derived/s2_types.parquet`

| `file_sha` | `skill_type` | `type_source` | `skill_types_all` |
| --- | --- | --- | --- |
| 00000818feedfd1e1d22f6bb42362f0c4e5213e4 | backend_api_languages | body | ["backend_api_languages", "integrations_tools"] |
| 000009fe40ffb92ccb4350995d114f838be78237 | testing_debugging | front_matter | ["testing_debugging"] |

`results/s2_skill_type_by_project.csv`

| `skill_type` | `repo_kind` | `language` | `files` |
| --- | --- | --- | --- |
| backend_api_languages | A_collection | NULL | 14075 |
| backend_api_languages | A_collection | 1C Enterprise | 3 |

<a id="step-4e"></a>

## Step 4e: Measure wording

**Run:** see [runbook step 4e](PIPELINE_RUNBOOK.md#step-4e). **Code:** [`src/skillsig/s2_features/wording.py`](../src/skillsig/s2_features/wording.py).

**Inputs and source steps:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `src/data/derived/s2_wording.parquet`.

**Purpose**

Convert writing patterns into numeric features for the text-model experiments in [step 6e](#step-6e) and [step 6f](#step-6f).

**Processing and algorithm**

Select readable, distinct skills from the raw artifacts, remove the YAML header, and process text in batches. `wording.py` calculates the following features with custom Python functions:

| Algorithm | Calculation and output |
| --- | --- |
| Pattern matching and counting | Regular expressions and fixed word lists identify instruction verbs, “you/your,” shell-command lines and uppercase emphasis. Save instruction-line shares and rates per 100 words or 1,000 characters. |
| Flesch–Kincaid readability | `0.39 × words/sentences + 11.8 × syllables/words − 15.59` gives `w_fk_grade`. Syllables are estimated from vowel groups, with a trailing silent-e adjustment. |
| Moving-average type–token ratio (MATTR) | Slide a 500-word window through the body, track word frequencies in a dictionary, and average `different words / window words` as `w_mattr500`. Shorter texts use one window. |
| Coefficient of variation | Divide standard deviation by mean length for bullets (`w_uniformity_bullet_cv`) and prose sentences (`w_sentence_cv`). Larger values indicate more varied lengths. Bullet lengths use characters; sentence lengths use word counts. |
| Shannon entropy | Count headings at each level and calculate `−Σ p × log2(p)` as `w_uniformity_heading_entropy`. Higher values mean heading levels are more evenly represented. |
| Prose extraction and counting | Remove fenced code, tables, headings and list markers before measuring sentence variation and em-dashes per 1,000 prose words. Also save `w_prose_sentences`. |
| Character threshold | Set `w_non_english = 1` when more than 20% of body characters are non-ASCII. |

`w_phrase_hits` is currently zero because its configured phrase list is empty. The description-opening category (`desc_idiom`) reuses the pattern rules defined in [step 3b](#step-3b).

**Libraries used**

| Library/module | Role |
| --- | --- |
| `re` | Text patterns, token extraction and header removal |
| `math` | Square roots for variation and base-two logarithms for entropy |
| DuckDB | Read Parquet, filter readable distinct skills and select development-sample IDs |
| PyArrow | Define output types and write compressed Parquet batches |
| `multiprocessing` | Calculate features across CPU worker processes |

**Why this approach**

These measures turn claims about writing style into values that can be tested. Rates, such as words per 100 words, make texts of different lengths easier to compare.

**Where it is used later**

Later steps join wording rows to other feature tables using `file_sha`. Each selected wording value becomes a numeric input column for a model.

- [Step 6e](#step-6e) combines wording, structure and specificity in logistic regression. It learns feature weights to distinguish skills with observed AI disclosure from unlabelled skills. Grouped held-out evaluation checks whether those patterns carry over to other owners and text families. The `text_without_new` comparison removes sentence variation, prose-sentence count, em-dash rate and specificity features to measure their combined contribution.
- [Step 6f](#step-6f) uses the wording values in feature-based logistic regression and gradient boosting. It compares their held-out scores with length, phrase and TF-IDF methods. This tests whether measured writing patterns add useful information compared with simpler rules or learned word patterns.
- [Step 6g](#step-6g), the optional no-history experiment, reads the same wording columns when applying the selected ensemble to skills without commit history. Its feature-based member uses these values alongside the text-based members.

The models select the numeric wording columns listed in `WORDING` in `style_model.py`. `desc_idiom` and `w_phrase_hits` remain in the saved table but are outside that feature list. The historical v2.1 rules do not use wording values to choose labels; these features support the separate text experiments.

**Output meaning and limits**

A style value is not proof of AI writing. Readability and word patterns assume English; the non-ASCII threshold is only a rough language flag. Sentence variation is zero below five prose sentences, and bullet variation is zero below two bullets. The later text experiments test whether these measures separate the selected evidence groups.

**Saved output examples**

`src/data/derived/s2_wording.parquet`

| `file_sha` | `w_imperative_ratio` | `w_fk_grade` | `w_mattr500` | `w_sentence_cv` | `desc_idiom` |
| --- | --- | --- | --- | --- | --- |
| 00000818feedfd1e1d22f6bb42362f0c4e5213e4 | 0.055838 | 11.71 | 0.52823 | 0.54571 | other |
| 000009fe40ffb92ccb4350995d114f838be78237 | 0.076923 | 12.472 | 0.46284 | 0.6616 | use_when |

<a id="step-5a"></a>

## Step 5a: Group near-duplicate texts

**Run:** see [runbook step 5a](PIPELINE_RUNBOOK.md#step-5a). **Code:** [`src/skillsig/s2_features/minhash_cluster.py`](../src/skillsig/s2_features/minhash_cluster.py).

**Inputs and source steps:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `src/data/derived/s2_clusters.parquet`; `src/data/derived/s2_minhash_shards/`.

**Data flow**

```text
Skill body
    |
    v
Remove header -> lower-case -> normalize spaces
    |
    v
Overlapping 5-word shingles
    |
    v
128-number MinHash signature -----> saved signature shards
    |
    v
LSH band matches -> candidate links -> union-find -> cluster table

Example of a connected cluster:
A --- B --- C
A and C belong to the same group even without a direct link.
```

**Processing and algorithm**

1. Remove the header, lower-case the body and collapse repeated spaces. The current one-million-character limit covers the full bodies in this dataset.
2. Split the words into overlapping groups of five, called **shingles**. For example, six words give two five-word shingles.
3. Build a **MinHash signature**: 128 numbers that summarize the shingle set. The fraction of matching numbers estimates **Jaccard similarity**: shared shingles divided by all different shingles. For example, 80 shared shingles out of a union of 100 gives 0.8.
4. Use **locality-sensitive hashing (LSH)** to find candidates. It splits signatures into bands and looks for identical bands, avoiding a comparison of every pair of documents.
5. Use **union-find** to join candidate links into connected groups. Save groups at settings 0.8 and 0.6, plus signature shards that later steps can reuse.

**Why this approach**

Comparing every pair of about 1.8 million texts would require about 1.7 trillion comparisons. Compact signatures and candidate search make the task practical. Five-word groups retain local wording while tolerating small edits. Saving shards lets interrupted work resume.

**Output meaning and limits**

A cluster is a connected candidate group. If A links to B and B links to C, all three join even when A and C are less alike. The threshold is an LSH search setting, not a guarantee that every pair in a cluster meets it. `nn_sim_est` is an estimated neighbour score.

In dev mode, clusters form only within the [step 2 sample](#step-2). Their sizes can be much smaller than in full mode. Signature shards use the same output folder in both modes.

**Saved output examples**

`src/data/derived/s2_clusters.parquet`

| `file_sha` | `cluster_id` | `cluster_size` | `threshold` | `nn_sim_est` |
| --- | --- | --- | --- | --- |
| 00000818feedfd1e1d22f6bb42362f0c4e5213e4 | 0 | 1 | 0.6 | NULL |
| 000009fe40ffb92ccb4350995d114f838be78237 | 1 | 2 | 0.6 | 0.98438 |

`src/data/derived/s2_minhash_shards/`

Two real signature records from `shard_00000`; only the first four of 128 signature numbers are shown. These are arrays stored beside the cluster table.

| `file_sha` | `signature_1` | `signature_2` | `signature_3` | `signature_4` |
| --- | --- | --- | --- | --- |
| 00000818feedfd1e1d22f6bb42362f0c4e5213e4 | 14092054 | 233092 | 82132 | 11441542 |
| 000009fe40ffb92ccb4350995d114f838be78237 | 763516 | 1059454 | 104329 | 5600000 |

<a id="step-5b"></a>

## Step 5b: Find registry similarity

**Run:** see [runbook step 5b](PIPELINE_RUNBOOK.md#step-5b). **Code:** [`src/skillsig/s2_features/template_sim.py`](../src/skillsig/s2_features/template_sim.py).

**Inputs and source steps:** [`src/data/derived/s1_registry_versions.parquet`](#step-3b); [`src/data/derived/s1_registry_matches.parquet`](#step-3c); [`src/data/registry_repos/`](#step-3a); [`src/data/derived/s2_minhash_shards/`](#step-5a); `config/registry_sources.yaml`.

**Outputs:** `src/data/derived/s2_template.parquet`.

**Processing and algorithm**

Build signatures for registry versions with the same settings as [step 5a](#step-5a). Search the registry index at 0.6 to find possible matches. Reuse dataset signatures from the shards. An exact hash match gets `exact`; otherwise the best retrieved candidate gets `near` if estimated Jaccard is at least 0.8. Other rows get `none`.

**Why this approach**

Reusing signatures saves time and keeps comparisons consistent. Searching more broadly than the final 0.8 rule helps retrieve candidates near the boundary.

**Output meaning and limits**

The nearest version is the best candidate found by the search, not a guaranteed global nearest neighbour. `none` means no qualifying match was found among the selected registries. It does not establish an original work.

**Saved output examples**

`src/data/derived/s2_template.parquet`

| `file_sha` | `match_kind` | `nearest_blob` | `nearest_lineage_id` | `est_jaccard` |
| --- | --- | --- | --- | --- |
| 00000818feedfd1e1d22f6bb42362f0c4e5213e4 | none | NULL | NULL | 0 |
| 000009fe40ffb92ccb4350995d114f838be78237 | none | NULL | NULL | 0 |

<a id="step-5c"></a>

## Step 5c: Measure project-specific identifiers

**Run:** see [runbook step 5c](PIPELINE_RUNBOOK.md#step-5c). **Code:** [`src/skillsig/s2_features/specificity.py`](../src/skillsig/s2_features/specificity.py).

**Inputs and source steps:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/derived/s2_clusters.parquet`](#step-5a).

**Outputs:** `src/data/derived/s2_specificity.parquet`.

**Data flow**

```text
Body text -> extract distinct identifiers
                         |
                         v
             Count groups using each identifier
                         ^
                         |
             Near-duplicate clusters (step 5a)
                         |
                         v
             Rare if used by fewer than 5 groups
                         |
                         v
             specificity = rare / all identifiers
```

**Processing and algorithm**

Read up to 50,000 body characters and collect distinct code names, file paths, flags and inline code. Count how many near-duplicate groups use each identifier. An identifier is rare if fewer than five groups use it. Compute `specificity = n_rare / n_identifiers`. With fewer than three identifiers, save a missing score; skills with no identifiers have no row.

**Why this approach**

Identifier rarity measures project detail rather than generic advice. Counting groups instead of copies prevents a widely copied skill from making its own identifiers seem common. Buckets and saved parts keep memory use manageable.

**Output meaning and limits**

A high score suggests specific content. It does not establish human authorship: an agent can also read a project. `NULL` means too little evidence for this score.

The step reuses saved identifier parts. Leave them in place to resume. For a clean rebuild after input or extraction changes, move `src/data/derived/s2_specificity_identifiers` and `src/data/derived/s2_specificity_buckets_c*` aside, then run with `--force`.

**Saved output examples**

`src/data/derived/s2_specificity.parquet`

| `file_sha` | `n_identifiers` | `n_rare` | `specificity` |
| --- | --- | --- | --- |
| 00000818feedfd1e1d22f6bb42362f0c4e5213e4 | 117 | 65 | 0.55556 |
| 000009fe40ffb92ccb4350995d114f838be78237 | 15 | 7 | 0.46667 |

The same step also saves resumable identifier and bucket parts under `src/data/derived/s2_specificity_identifiers/` and `src/data/derived/s2_specificity_buckets_c*`. They support this score table; they are not separate authorship results.

<a id="step-5d"></a>

## Step 5d: Find shared structure and scaffolds

**Run:** see [runbook step 5d](PIPELINE_RUNBOOK.md#step-5d). **Code:** [`src/skillsig/s2_features/scaffold.py`](../src/skillsig/s2_features/scaffold.py).

**Inputs and source steps:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/gitskills_data/data/artifact_siblings/*.parquet`](#step-0).

**Outputs:** `src/data/derived/s2_scaffold.parquet`; `results/s2_scaffold_top_outlines.csv`.

**Data flow**

```text
Body + header + sibling files (step 0)
                  |
                  v
    +-------------+---------------+----------------+
    |             |               |                |
    v             v               v                v
Placeholders   Heading         Masked body      Shared lines
               outlines        and layout
    |             |               |                |
    +-------------+---------------+----------------+
                  |
                  v
       Count spread across distinct owners
                  |
                  v
       Apply ordered scaffold-basis rules
                  |
                  v
       Per-skill signals + outline summary
```

**Processing and algorithm**

Measure unfinished placeholders, heading outlines, ordered header keys, folder layouts, masked bodies and shared lines. Masking replaces changing details such as names, URLs and numbers before comparing bodies. Count spread across different owners.

For similar outlines, use MinHash LSH to find possible heading-set matches, then compare heading order using edit distance: the insertions, removals and replacements needed to turn one outline into another. Attach an outline to the first matching group leader. This avoids joining every outline through long chains. Outlines under four headings remain separate, and very large candidate buckets are skipped.

Assign the first matching `scaffold_basis`: generator marker, placeholder, masked copy across at least ten owners, at least 60% shared lines with three or more lines, or a shared outline supported by shared header keys or folder layout. A line is shared when at least five owners use it.

**Why this approach**

Text-copy methods miss common structures filled with different wording. Several supporting signals are used because common headings alone are weak evidence. Owner counts reduce the effect of one person publishing many variants.

**Output meaning and limits**

A scaffold signal is a candidate for review. Common good practice can also create similar structure. `customization = 1 - shared_line_frac` measures unshared lines, not quality or originality.

**Saved output examples**

`src/data/derived/s2_scaffold.parquet`

| `file_sha` | `placeholder_count` | `outline_family_owners` | `shared_line_frac` | `customization` | `scaffold_basis` |
| --- | --- | --- | --- | --- | --- |
| 00000818feedfd1e1d22f6bb42362f0c4e5213e4 | 0 | 1 | 0.035714 | 0.96429 | NULL |
| 000009fe40ffb92ccb4350995d114f838be78237 | 0 | 1 | 0.01087 | 0.98913 | NULL |

`results/s2_scaffold_top_outlines.csv`

| `outline_text` | `outline_len` | `owners` | `skills` |
| --- | --- | --- | --- |
| 1:&lt;name&gt; | 1 | 7091 | 21916 |
| 1:&lt;name&gt; skill | 1 | 874 | 1895 |

The same step keeps intermediate signals and line hashes in `src/data/derived/s2_scaffold_parts/`. The paired review in [step 8p](#step-8p) can read these parts.

<a id="step-6a"></a>

## Step 6a: Reproduce the first label rules

**Run:** see [runbook step 6a](PIPELINE_RUNBOOK.md#step-6a). **Code:** [`src/skillsig/s3_labeling/rules_v1.py`](../src/skillsig/s3_labeling/rules_v1.py).

**Inputs and source steps:** [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/derived/s2_commit.parquet`](#step-4b).

**Outputs:** `src/data/derived/s3_labels_v1.parquet`.

**Processing and algorithm**

Apply the first rule that matches:

1. At least ten byte-identical file copies: `highly_templated`.
2. Available history, fewer than six skills in the first-commit batch, no non-coding bot, and a coding-agent bot or first-commit AI trailer: `agent_assisted`.
3. Available history, one skill in the batch, user author, no first/last AI trailer and one copy: `likely_human`.
4. Otherwise: `insufficient_evidence`, with reason codes.

**Why this approach**

Keeping the old rules gives a fixed comparison with Sprint 1 and a regression check before later rules change behavior. First-match order makes decisions traceable.

**Output meaning and limits**

These are historical rule names. Ten copies show reuse; they do not prove a template was used. The `likely_human` rule depends partly on missing disclosure, which does not establish human authorship.

**Saved output examples**

`src/data/derived/s3_labels_v1.parquet`

| `file_sha` | `label` | `copies` | `reasons` |
| --- | --- | --- | --- |
| 00000818feedfd1e1d22f6bb42362f0c4e5213e4 | insufficient_evidence | 1 | ["batch_size=5", "no_agent_signal"] |
| 000009fe40ffb92ccb4350995d114f838be78237 | insufficient_evidence | 2 | ["batch_size=2", "no_agent_signal", "copies=2"] |

<a id="step-6b"></a>

## Step 6b: Add registry and cluster evidence

**Run:** see [runbook step 6b](PIPELINE_RUNBOOK.md#step-6b). **Code:** [`src/skillsig/s3_labeling/rules_v2.py`](../src/skillsig/s3_labeling/rules_v2.py).

**Inputs and source steps:** [`src/data/derived/s3_labels_v1.parquet`](#step-6a); [`src/data/derived/s2_structure.parquet`](#step-4a); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/derived/s2_repo.parquet`](#step-4c); [`src/data/derived/s2_types.parquet`](#step-4d); [`src/data/derived/s2_wording.parquet`](#step-4e); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s2_template.parquet`](#step-5b); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `src/data/derived/s3_labels_v2.parquet`; `results/s3_v1_v2_crosstab.csv`.

**Processing and algorithm**

Choose a templated basis in this order: exact registry match, near registry match, cluster of at least ten, or at least ten exact copies. Skip the two registry rules for a representative stored in its matched origin repository. Other skills keep their v1 label. Add rule-based confidence and reasons; cases near cut-offs receive lower confidence.

**Why this approach**

Exact-copy counts miss edited copies and families of similar texts. Adding the basis shows which evidence changed the decision.

**Output meaning and limits**

Confidence is a rule category, not a calibrated probability. Structure, repository, type and wording files are required and fingerprinted by this step, but their values do not decide the label.

**Saved output examples**

`src/data/derived/s3_labels_v2.parquet`

| `file_sha` | `label` | `templated_basis` | `confidence` | `reasons` |
| --- | --- | --- | --- | --- |
| 00000818feedfd1e1d22f6bb42362f0c4e5213e4 | insufficient_evidence | NULL | low | ["batch_size=5", "no_agent_signal"] |
| 000009fe40ffb92ccb4350995d114f838be78237 | insufficient_evidence | NULL | low | ["batch_size=2", "no_agent_signal", "copies=2"] |

`results/s3_v1_v2_crosstab.csv`

| `v1_label` | `v2_label` | `n` |
| --- | --- | --- |
| agent_assisted | agent_assisted | 78049 |
| agent_assisted | highly_templated | 1882 |

<a id="step-6c"></a>

## Step 6c: Add disclosure and revision flags

**Run:** see [runbook step 6c](PIPELINE_RUNBOOK.md#step-6c). **Code:** [`src/skillsig/s3_labeling/rules_v2_1.py`](../src/skillsig/s3_labeling/rules_v2_1.py).

**Inputs and source steps:** [`src/data/derived/s3_labels_v1.parquet`](#step-6a); [`src/data/derived/s3_labels_v2.parquet`](#step-6b); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `src/data/derived/s3_labels_v2_1.parquet`; `results/s3_v2_v21_crosstab.csv`.

**Processing and algorithm**

Start with v2. Where it said insufficient evidence but disclosure is present and the author is not a non-coding bot, assign low-confidence `agent_assisted`. Keep `likely_human` at medium confidence only with at least three commits over seven days; otherwise lower it. If a templated skill also has disclosure, keep its label and reduce confidence. Save disclosure, reuse, revision and filter flags separately.

**Why this approach**

The changes expose cases missed by first-commit-only or small-batch rules. Separate flags let later reports show more than one kind of evidence at once.

**Output meaning and limits**

Repeated revision shows maintenance, not proof of human writing. The old conflict penalty is part of this historical label system. [Step 6h](#step-6h) reports reuse and disclosure separately without treating them as a contradiction.

**Saved output examples**

`src/data/derived/s3_labels_v2_1.parquet`

| `file_sha` | `label` | `confidence` | `flag_disclosed_agent` | `revised` | `conflict` |
| --- | --- | --- | --- | --- | --- |
| 00000818feedfd1e1d22f6bb42362f0c4e5213e4 | insufficient_evidence | low | false | false | false |
| 000009fe40ffb92ccb4350995d114f838be78237 | insufficient_evidence | low | false | false | false |

`results/s3_v2_v21_crosstab.csv`

| `label_v2` | `label_v21` | `confidence_v21` | `n` |
| --- | --- | --- | --- |
| agent_assisted | agent_assisted | high | 117 |
| agent_assisted | agent_assisted | low | 13212 |

<a id="step-6d"></a>

## Step 6d: Build simple comparison rules

**Run:** see [runbook step 6d](PIPELINE_RUNBOOK.md#step-6d). **Code:** [`src/skillsig/s3_labeling/baselines.py`](../src/skillsig/s3_labeling/baselines.py).

**Inputs and source steps:** [`src/data/derived/s3_labels_v1.parquet`](#step-6a); [`src/data/derived/s3_labels_v2.parquet`](#step-6b); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/generated_data/file_agents.parquet`](#step-1b); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `src/data/derived/s3_baseline_labels.parquet`; `results/s3_baseline_summary.csv`.

**Processing and algorithm**

Build five baselines: always choose the most common v2 label; call ten-copy texts templated; call tool-folder files agent-assisted; call first-trailer files agent-assisted; repeat the trailer rule after excluding bulk batches. Other cases get insufficient evidence.

**Why this approach**

A baseline is a simple method used for comparison. It checks whether the larger rule system adds value over one-signal shortcuts.

**Output meaning and limits**

Agreement between two rules does not prove either is correct. The agent-folder baseline deliberately tests a weak shortcut. The later review compares all methods on the same sampled skills.

**Saved output examples**

`src/data/derived/s3_baseline_labels.parquet`

| `file_sha` | `B0_majority` | `B1_copy_count` | `B2_agent_folder` | `B3_trailer` |
| --- | --- | --- | --- | --- |
| 00000818feedfd1e1d22f6bb42362f0c4e5213e4 | insufficient_evidence | insufficient_evidence | agent_assisted | insufficient_evidence |
| 000009fe40ffb92ccb4350995d114f838be78237 | insufficient_evidence | insufficient_evidence | insufficient_evidence | insufficient_evidence |

`results/s3_baseline_summary.csv`

| `baseline` | `label` | `n` | `share` |
| --- | --- | --- | --- |
| B0_majority | insufficient_evidence | 1877981 | 1 |
| B1_copy_count | highly_templated | 35768 | 0.019046 |

<a id="step-6e"></a>

## Step 6e: Test text style against disclosure

**Run:** see [runbook step 6e](PIPELINE_RUNBOOK.md#step-6e). **Code:** [`src/skillsig/s3_labeling/style_model.py`](../src/skillsig/s3_labeling/style_model.py).

**Inputs and source steps:** [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/derived/s2_structure.parquet`](#step-4a); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/derived/s2_wording.parquet`](#step-4e); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s2_specificity.parquet`](#step-5c); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `src/data/derived/s3_style_scores.parquet`; `results/s3_style_model.csv`; `results/s3_style_model.json`; `results/s3_style_top_words.csv`.

**Data flow**

```text
Features + disclosure flags + owner/cluster groups
                         |
                         v
                  Five grouped folds
                         |
             +-----------+------------+
             |                        |
             v                        v
       Four training folds      One held-out fold
             |                        |
             v                        |
       Select marker words            |
       Fit scaling + model            |
             |                        |
             +---- score held-out ----+
                         |
                         v
        Repeat until every fold has been held out
                         |
                         v
             Per-skill scores + AUC tables
```

**Processing and algorithm**

Use non-templated skills with history. Disclosed skills form the positive group; other skills are unlabelled, because missing disclosure does not mean human writing. Fit **logistic regression**, which learns a weighted sum of features and turns it into a score between zero and one.

Use five-fold cross-validation: train on four parts and score the held-out part, repeating until each row has a score from a model that did not train on it. Group skills connected by owner or cluster into the same part. Choose marker words inside training parts only and remove tool names. Compare text-only, date-only and combined versions.

For each fold, calculate `c`, the mean disclosure score among its held-out disclosed skills. The code saves `style_score = min(1, g_text_only / c)`. This positive-unlabelled correction tries to account for undisclosed involvement. It assumes disclosure is selected in a suitable way; that assumption has not been established here. A saved value of 1 is therefore not proof of agent authorship.

**Why this approach**

Logistic regression is a simple, readable reference model. Grouped splits reduce memorization of an owner or near-copy family. Selecting words only from training data prevents the held-out answers from influencing training.

**Output meaning and limits**

`s` marks observed disclosure. `style_score` is an experimental score, not an authorship probability. AUC measures ranking: 0.5 is chance ranking and 1 is perfect ranking of these selected groups. Disclosure is a proxy, meaning an indirect target rather than a verified answer.

**Saved output examples**

`src/data/derived/s3_style_scores.parquet`

| `file_sha` | `fold` | `s` | `g_text_only` | `style_score` |
| --- | --- | --- | --- | --- |
| 00000818feedfd1e1d22f6bb42362f0c4e5213e4 | 2 | 0 | 0.46655 | 1 |
| 000009fe40ffb92ccb4350995d114f838be78237 | 4 | 0 | 0.34316 | 0.90484 |

`results/s3_style_model.csv`

| `model` | `fold` | `auc_pu` | `n_pos` | `n_unl` |
| --- | --- | --- | --- | --- |
| text_only | 0 | 0.61522 | 24021 | 39395 |
| date_only | 0 | 0.49607 | 24021 | 39395 |

`results/s3_style_model.json`

| `field` | `saved_value` |
| --- | --- |
| auc_by_tool | {"claude_positives": {"auc_text_only": 0.6079069103565957, "n_pos": 132975}, "other_positives": {… |
| auc_ci | {"date_only": [0.469489363993772, 0.49904786556440894], "text_no_markers": [0.5690244965852749, 0… |

`results/s3_style_top_words.csv`

| `word` | `z` | `delta` | `df_pos` | `df_neg` |
| --- | --- | --- | --- | --- |
| gate | 31.566 | 0.30827 | 20051 | 21977 |
| carries | 26.633 | 0.46159 | 6870 | 6459 |

<a id="step-6f"></a>

## Step 6f: Compare several text methods

**Run:** see [runbook step 6f](PIPELINE_RUNBOOK.md#step-6f). **Code:** [`src/skillsig/s3_labeling/text_methods.py`](../src/skillsig/s3_labeling/text_methods.py).

**Inputs and source steps:** [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/derived/s2_structure.parquet`](#step-4a); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/derived/s2_wording.parquet`](#step-4e); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s2_specificity.parquet`](#step-5c); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `src/data/derived/s3_text_methods_oof.parquet`; `results/s3_text_methods.csv`; `results/s3_text_methods.json`; `results/s3_text_methods_top_words.csv`.

**Processing and algorithm**

Draw 20,000 disclosed agent-assisted and 20,000 historical `likely_human` skills. Remove headers and tool names and read up to 6,000 characters. Use the same owner/cluster grouping for five-fold evaluation.

Compare length alone, a fixed phrase rule, logistic regression on measured features, gradient boosting on features, word TF-IDF, character TF-IDF, and an ensemble. **Gradient boosting** adds small decision trees that correct earlier errors. **TF-IDF** gives more weight to patterns frequent in one document but uncommon across documents; logistic regression then learns which patterns separate the groups. The **ensemble** averages the boosting and two TF-IDF scores.

Report ranking and the share reaching 80% or 90% precision against the proxy labels. Also report selected restricted groups and length-based checks.

**Why this approach**

Length and phrase rules test simple explanations first. Feature models can combine measured patterns. TF-IDF can learn wording patterns outside the fixed feature list. Comparing all methods under the same split makes the comparison fairer.

**Output meaning and limits**

`oof` means out-of-fold: each score comes from a model trained on other groups. `y = 1` is the disclosure-positive proxy, not verified AI authorship. Precision targets assume equal class sizes here; they do not establish performance on skills without history.

**Saved output examples**

`src/data/derived/s3_text_methods_oof.parquet`

| `file_sha` | `y` | `fold` | `tfidf_words` | `tfidf_chars` | `ensemble` |
| --- | --- | --- | --- | --- | --- |
| 00025728e4e0f1431826156dc6784b7747e46ca8 | 1 | 4 | 0.66117 | 0.74869 | 0.6332 |
| 0003928a4a066f30f964bb81a3e37caa3f7795b8 | 0 | 3 | 0.70056 | 0.63623 | 0.65319 |

`results/s3_text_methods.csv`

| `method` | `subset` | `auc` | `agent_decided_p90` | `human_decided_p90` |
| --- | --- | --- | --- | --- |
| length_only | all | 0.53373 | 0 | 0.0001 |
| length_only | matched | 0.53916 | 0 | 0.0001 |

`results/s3_text_methods.json`

| `field` | `saved_value` |
| --- | --- |
| best_auc_all | 0.71067 |
| best_method_all | ensemble |

`results/s3_text_methods_top_words.csv`

| `term` | `coef` | `direction` |
| --- | --- | --- |
| arguments | 4.2426 | agent |
| noreply | 3.7282 | agent |

<a id="step-6g"></a>

## Step 6g: Optional: score skills without history

**Run:** see [runbook step 6g](PIPELINE_RUNBOOK.md#step-6g). **Code:** [`src/skillsig/s3_labeling/text_score.py`](../src/skillsig/s3_labeling/text_score.py).

**Inputs and source steps:** [`src/data/derived/s3_text_methods_oof.parquet`](#step-6f); [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/derived/s2_structure.parquet`](#step-4a); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/derived/s2_wording.parquet`](#step-4e); [`src/data/derived/s2_specificity.parquet`](#step-5c); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `src/data/derived/s3_text_scores.parquet`; `results/s3_text_score_summary.csv`; `results/s3_text_score.json`.

**Processing and algorithm**

Use held-out scores from step 6f to choose cut-offs for a 90% proxy precision target. Train the selected ensemble on the history groups, then score non-templated texts without history. Save an agent lean, human lean or uncertainty. An explicit AI disclosure inside the text overrides the style lean. Save `label_with_text` separately; do not rewrite v2.1.

**Why this approach**

This explores whether a model trained where history exists can provide a useful signal where history is missing. Separate outputs keep that experiment apart from the saved rule labels.

**Output meaning and limits**

The target population differs from the training population. Proxy precision does not validate transfer to no-history texts. The production output files are absent in this checkout, so no real sample rows can be shown.

Optional; not included in `scripts/run_full.sh`. The separate `results/text_nohistory_pilot/` experiment is not the output of this command.

**Saved output examples**

`src/data/derived/s3_text_scores.parquet`

_Output is not present in this checkout. No sample records are available._

`results/s3_text_score_summary.csv`

_Output is not present in this checkout. No sample records are available._

`results/s3_text_score.json`

_Output is not present in this checkout. No sample records are available._

<a id="step-6h"></a>

## Step 6h: Report reuse and disclosure separately

**Run:** see [runbook step 6h](PIPELINE_RUNBOOK.md#step-6h). **Code:** [`src/skillsig/s3_labeling/evidence.py`](../src/skillsig/s3_labeling/evidence.py).

**Inputs and source steps:** [`src/data/derived/s3_labels_v1.parquet`](#step-6a); [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s2_template.parquet`](#step-5b); [`src/data/derived/s2_scaffold.parquet`](#step-5d).

**Outputs:** `src/data/derived/s3_evidence.parquet`; `results/s3_evidence_matrix.csv`; `results/s3_evidence_signals.csv`; `results/s3_evidence_legacy_crosstab.csv`; `results/s3_evidence_summary.json`.

**Data flow**

```text
Copy counts + registry matches + clusters + scaffold
                          |
                          v
                  Reuse evidence --------+
                                         |
Commit records -> disclosure flags ------+-> per-skill evidence table
                                         |
                                         v
                              Reuse x disclosure matrix

A skill can have both reuse evidence and disclosed agent involvement.
```

**Processing and algorithm**

Keep five overlapping reuse flags: exact content shared by at least two files, registry exact match, registry near-match, cluster with at least two members, and a scaffold signal. Make a display category from these flags. Separately report disclosed involvement, history with no observed disclosure, or unavailable history. Keep the older labels for comparison and count all reuse-by-disclosure combinations.

**Why this approach**

Reuse and disclosed agent involvement can both be present. Two reporting dimensions show that directly and avoid forcing unlike evidence into one authorship label.

**Output meaning and limits**

The lower two-copy threshold describes observed sharing, not additional validated templated labels. Flags overlap and must not be summed. No disclosure is not proof of human writing. These dimensions need their own evaluation; the old annotation scores do not validate them.

**Saved output examples**

`src/data/derived/s3_evidence.parquet`

| `file_sha` | `copies` | `reuse_signal` | `agent_signal` | `reuse_and_disclosure` |
| --- | --- | --- | --- | --- |
| 00000818feedfd1e1d22f6bb42362f0c4e5213e4 | 1 | no_reuse_signal_observed | no_disclosure_observed | false |
| 000009fe40ffb92ccb4350995d114f838be78237 | 2 | exact_content_shared | no_disclosure_observed | false |

`results/s3_evidence_matrix.csv`

| `reuse_signal` | `agent_signal` | `n` | `share_all` | `share_within_agent_signal` |
| --- | --- | --- | --- | --- |
| exact_content_shared | disclosed | 35655 | 0.018986 | 0.22599 |
| exact_content_shared | history_unavailable | 268472 | 0.14296 | 0.18909 |

`results/s3_evidence_signals.csv`

| `signal` | `agent_signal` | `n` |
| --- | --- | --- |
| flag_exact_content_shared | disclosed | 35629 |
| flag_exact_content_shared | history_unavailable | 267960 |

`results/s3_evidence_legacy_crosstab.csv`

| `legacy_label` | `reuse_signal` | `agent_signal` | `n` |
| --- | --- | --- | --- |
| agent_assisted | exact_content_shared | disclosed | 27153 |
| agent_assisted | near_duplicate_signal | disclosed | 12879 |

`results/s3_evidence_summary.json`

| `field` | `saved_value` |
| --- | --- |
| n | 1877981 |
| dev | false |

<a id="step-7a"></a>

## Step 7a: Check how many disclosures first and last commits reveal

**Run:** see [runbook step 7a](PIPELINE_RUNBOOK.md#step-7a). **Code:** [`src/skillsig/s4_validation/disclosure_recall.py`](../src/skillsig/s4_validation/disclosure_recall.py).

**Inputs and source steps:** [`src/data/derived/s1_versions_raw.parquet`](#step-3a); [`src/data/derived/s1_commit_trailers.parquet`](#step-3a).

**Outputs:** `results/s4_disclosure_recall.csv`; `results/s4_disclosure_recall.json`.

**Processing and algorithm**

For each registry skill, check whether any commit has an AI trailer. Compare that with looking only at the first and last commits. Recall equals visible disclosures divided by all observed disclosures. Report skills with at least three commits separately.

**Why this approach**

The dataset usually exposes first and last commits only. Full registry histories let us estimate what this limited view misses. Skills with one or two commits cannot hide a middle disclosure.

**Output meaning and limits**

This checks disclosure coverage within the chosen registries. It does not measure all AI use or establish coverage in every dataset repository.

**Saved output examples**

`results/s4_disclosure_recall.csv`

| `scope` | `any_ai` | `visible_first_or_last` | `middle_only` | `recall` |
| --- | --- | --- | --- | --- |
| all | 1033 | 948 | 85 | 0.91772 |
| all_3plus_commits | 506 | 421 | 85 | 0.83202 |

`results/s4_disclosure_recall.json`

| `field` | `saved_value` |
| --- | --- |
| any_ai | 1033 |
| ci_high | 0.93297 |

<a id="step-7b"></a>

## Step 7b: Check recovery of exact registry texts

**Run:** see [runbook step 7b](PIPELINE_RUNBOOK.md#step-7b). **Code:** [`src/skillsig/s4_validation/template_provenance.py`](../src/skillsig/s4_validation/template_provenance.py).

**Inputs and source steps:** [`src/data/derived/s2_template.parquet`](#step-5b); [`src/data/derived/s3_labels_v1.parquet`](#step-6a); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c).

**Outputs:** `results/s4_template_provenance.csv`; `results/s4_template_unexplained.csv`.

**Processing and algorithm**

Take exact registry matches after excluding known origin representatives. Count how many also meet the copy-count rule, cluster rule or either rule. Report near-match candidates separately. Also count templated labels without a registry match.

**Why this approach**

Equal hashes provide a content-identity reference separate from approximate clustering. Keeping near matches separate avoids calling agreement between related similarity methods independent validation.

**Output meaning and limits**

Exact recovery is recall on this reference set, not precision or overall accuracy. Near-match rows show conditional agreement. An unexplained text may come from a source outside the registry list.

**Saved output examples**

`results/s4_template_provenance.csv`

| `known_set` | `n` | `copy_recall` | `cluster_recall` | `either_recall` | `metric_scope` |
| --- | --- | --- | --- | --- | --- |
| exact | 6481 | 0.56488 | 0.66595 | 0.87162 | recovery_on_exact_registry_matches |
| near | 42104 | 0.029878 | 0.87213 | 0.87319 | agreement_on_near_match_candidates |

`results/s4_template_unexplained.csv`

| `rule` | `n` | `registry_matched` | `share_registry` | `note` |
| --- | --- | --- | --- | --- |
| copy_rule | 35768 | 4919 | 0.13753 | agreement, not precision: sources outside the 31 mined repos are not known |
| cluster_rule | 277852 | 41048 | 0.14773 | agreement, not precision: sources outside the 31 mined repos are not known |

<a id="step-7c"></a>

## Step 7c: Test similarity after controlled edits

**Run:** see [runbook step 7c](PIPELINE_RUNBOOK.md#step-7c). **Code:** [`src/skillsig/s4_validation/edit_robustness.py`](../src/skillsig/s4_validation/edit_robustness.py).

**Inputs and source steps:** [`src/data/derived/s2_template.parquet`](#step-5b); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `results/s4_edit_robustness.csv`; `figures/12_edit_robustness.png`.

**Processing and algorithm**

Select 500 registry-matched texts with the fixed seed. At five levels, replace or delete 5%, 10%, 20%, 40% or 60% of nonblank body lines. Compare edited text with its original using the same MinHash settings as [step 5a](#step-5a). Count scores at or above 0.8.

**Why this approach**

Controlled edits make the amount of change known. Testing replacement and deletion shows how the detector responds to two different kinds of edits.

**Output meaning and limits**

The detection rate applies to these synthetic edits. Real edits can behave differently. This step tests resilience of the similarity measure, not authorship.

**Saved output examples**

`results/s4_edit_robustness.csv`

| `mode` | `level` | `n` | `mean_jaccard` | `detected` | `detection_rate` |
| --- | --- | --- | --- | --- | --- |
| replace | 0.05 | 500 | 0.84484 | 388 | 0.776 |
| replace | 0.1 | 500 | 0.71466 | 82 | 0.164 |

`figures/12_edit_robustness.png`

This is a chart, so it has no database records.

<a id="step-7d"></a>

## Step 7d: Vary one rule threshold at a time

**Run:** see [runbook step 7d](PIPELINE_RUNBOOK.md#step-7d). **Code:** [`src/skillsig/s4_validation/stability.py`](../src/skillsig/s4_validation/stability.py).

**Inputs and source steps:** [`src/data/derived/s3_labels_v1.parquet`](#step-6a); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/derived/s2_template.parquet`](#step-5b); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `results/s4_stability.csv`; `results/s4_stability.json`.

**Processing and algorithm**

Draw a seeded 200,000-skill sample. Recompute the rules with the default settings to check reproduction. Then vary one copy, batch, similarity or revision threshold while holding the others fixed. Save label and confidence shares and their change in percentage points.

**Why this approach**

Sensitivity analysis shows which chosen boundaries affect the reported shares. One-at-a-time changes make each comparison easier to understand.

**Output meaning and limits**

Similar overall shares can hide opposite changes in individual skills. Lowering the near-match rule to 0.7 cannot add candidates already marked `none` at 0.8 in [step 5b](#step-5b). That setting does not test a full 0.7 similarity search.

**Saved output examples**

`results/s4_stability.csv`

| `parameter` | `value` | `label` | `share` | `delta_pp` |
| --- | --- | --- | --- | --- |
| default | 0 | highly_templated | 0.16257 | 0 |
| default | 0 | agent_assisted | 0.0752 | 0 |

`results/s4_stability.json`

| `field` | `saved_value` |
| --- | --- |
| default_shares | {"agent_assisted": 0.0752, "agent_assisted:low": 0.5422872340425532, "highly_templated": 0.16257,… |
| largest_change | {"delta_pp": 3.3024999999999998, "label": "highly_templated", "parameter": "cluster_threshold", "… |

<a id="step-7e"></a>

## Step 7e: Check scaffold overlap and prepare examples

**Run:** see [runbook step 7e](PIPELINE_RUNBOOK.md#step-7e). **Code:** [`src/skillsig/s4_validation/scaffold_check.py`](../src/skillsig/s4_validation/scaffold_check.py).

**Inputs and source steps:** [`src/data/derived/s2_scaffold.parquet`](#step-5d); [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/derived/s2_template.parquet`](#step-5b); [`src/data/derived/s3_labels_v1.parquet`](#step-6a); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `results/s4_scaffold_overlap.csv`; `results/s4_scaffold_recall.csv`; `results/s4_scaffold_agent_confound.csv`; `results/s4_scaffold_spot.csv`.

**Processing and algorithm**

Cross-tabulate scaffold bases with older reuse bases. Measure overlap with reference sets and rates in disclosure groups. Draw 150 scaffold examples with possible partners and shared passages. Passage matching uses winnowing: hash short word groups and keep selected hashes from sliding windows to find local shared passages efficiently.

**Why this approach**

Overlap shows whether structural signals add candidates beyond text similarity. Paired passages give reviewers concrete evidence to inspect.

**Output meaning and limits**

The spot CSV has blank review fields until people fill them. Generated pairs and overlap are not completed human validation. Shared structure may reflect common practice rather than material copying.

**Saved output examples**

`results/s4_scaffold_overlap.csv`

| `scaffold_basis` | `registry_exact` | `registry_near` | `cluster` | `copy_count` | `none` |
| --- | --- | --- | --- | --- | --- |
| generator | 0 | 0 | 129 | 0 | 62 |
| placeholder | 218 | 406 | 1012 | 94 | 7528 |

`results/s4_scaffold_recall.csv`

| `known_set` | `n` | `scaffold_hits` | `scaffold_recall` | `ci_low` | `ci_high` |
| --- | --- | --- | --- | --- | --- |
| exact | 6481 | 5878 | 0.90696 | 0.89964 | 0.91379 |
| near | 42104 | 41429 | 0.98397 | 0.98272 | 0.98512 |

`results/s4_scaffold_agent_confound.csv`

| `group` | `n` | `scaffold_hits` | `scaffold_rate` | `outline_only_hits` | `outline_rate` |
| --- | --- | --- | --- | --- | --- |
| disclosed_agent | 154851 | 18818 | 0.12152 | 1371 | 0.0088537 |
| not_disclosed | 1686021 | 273085 | 0.16197 | 19696 | 0.011682 |

`results/s4_scaffold_spot.csv`

| `file_sha` | `scaffold_basis` | `existing_basis` | `owner` | `winnow_overlap` | `annotator_label` |
| --- | --- | --- | --- | --- | --- |
| 330c5b61b25c4a0b47072ca25637aed68c2fc335 | generator | cluster | gvidstv | 0.801 | NULL |
| 306f74522f750ec6c0ba825ebe795461af3f406c | generator | cluster | majiayu000 | 0.801 | NULL |

<a id="step-8"></a>

## Step 8: Draw the blind authorship review sample

**Run:** see [runbook step 8](PIPELINE_RUNBOOK.md#step-8). **Code:** [`src/skillsig/s4_validation/spot_check.py`](../src/skillsig/s4_validation/spot_check.py).

**Inputs and source steps:** [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/derived/s3_labels_v2.parquet`](#step-6b); [`src/data/derived/s3_baseline_labels.parquet`](#step-6d); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s2_repo.parquet`](#step-4c); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0).

**Outputs:** `results/annotation/spot_A.xlsx`; `results/annotation/spot_B.xlsx`; `results/annotation/trailer_check.csv`; `results/annotation/key.parquet`; `results/annotation/trailer_key.parquet`; `results/annotation/codebook.md`; `results/s4_spot_design.json`.

**Processing and algorithm**

Draw 30 skills from each of four v2.1 label groups, with limits on repeated repositories and clusters. Person A gets 120 skills. Person B gets a 40-skill subset. Create a separate 50-row trailer review. Write pipeline labels and sampling groups to separate scoring keys; exclude them from the blind workbooks. Excel text is limited to 32,000 characters.

**Why this approach**

Sampling by predicted label gives enough cases to inspect each label. Limiting related texts avoids counting many copies as independent judgments. Hiding model answers reduces pressure to agree with them.

**Output meaning and limits**

The sample checks agreement or precision by predicted label. It cannot directly estimate recall or population label shares. The workbooks contain review fields; separate scoring keys contain the pipeline labels and sampling groups.

An existing `spot_A.xlsx` prevents a new draw. For the same sample with updated model answers, use [step 8r](#step-8r). The annotation folder is ignored by Git; keep copies of completed files.

**Saved output examples**

`results/annotation/spot_A.xlsx`

| `sample_id` | `skill_name` | `your_label` | `your_confidence` |
| --- | --- | --- | --- |
| S001 | humanize-chinese-v2 | NULL | NULL |
| S002 | gws-keep | NULL | NULL |

`results/annotation/spot_B.xlsx`

| `sample_id` | `skill_name` | `your_label` | `your_confidence` |
| --- | --- | --- | --- |
| S034 | wcf-ui-builder | NULL | NULL |
| S119 | vscode-snippets | NULL | NULL |

`results/annotation/trailer_check.csv`

| `check_id` | `is_ai_disclosure` |
| --- | --- |
| T01 | NULL |
| T02 | NULL |

`results/annotation/key.parquet`

| `sample_id` | `file_sha` | `stratum` | `label_v21` | `in_b` |
| --- | --- | --- | --- | --- |
| S001 | 31ddcacf4b4642484f29d5e96b758e9fc14a37bf | insufficient_no_history | insufficient_evidence | false |
| S002 | 6ca36cad67b43e992f66d3aa97f8db3b546c1a09 | templated_registry | highly_templated | false |

`results/annotation/trailer_key.parquet`

| `check_id` | `file_sha` | `which` | `tool_ext` |
| --- | --- | --- | --- |
| T01 | 965fcee787933a7bb5b0d798f2f694da2c32c35c | first | claude |
| T02 | 0300c05eb35c2d471740f3d5bfb2ab49523fcff1 | first | claude |

`results/annotation/codebook.md`

This is a written guide, so it has no database records.

`results/s4_spot_design.json`

| `field` | `saved_value` |
| --- | --- |
| n_a | 120 |
| n_b | 40 |

<a id="step-8r"></a>

## Step 8r: Refresh the same sample after label changes

**Run:** see [runbook step 8r](PIPELINE_RUNBOOK.md#step-8r). **Code:** [`src/skillsig/s4_validation/spot_refresh.py`](../src/skillsig/s4_validation/spot_refresh.py).

**Inputs and source steps:** [`results/annotation/key.parquet`](#step-8); [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/derived/s3_labels_v2.parquet`](#step-6b); [`src/data/derived/s3_baseline_labels.parquet`](#step-6d); [`src/data/derived/s2_commit.parquet`](#step-4b); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s2_repo.parquet`](#step-4c); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`results/annotation/spot_A.xlsx`](#step-8); [`results/annotation/spot_B.xlsx`](#step-8).

**Outputs:** `results/annotation/key.parquet`; `results/annotation/spot_A.xlsx`; `results/annotation/spot_B.xlsx`.

**Processing and algorithm**

Load the saved sample IDs and look up the current model labels. Rewrite the key for those same IDs. Refresh workbook content only when its response fields are still blank. Preserve completed answers.

**Why this approach**

Keeping the same IDs allows existing human answers to be compared with changed rules. Drawing a new sample would break that comparison.

**Output meaning and limits**

The scoring key may change while the reviewed subjects stay fixed. This is an alternative to drawing a new sample, not a required fresh draw.

**Saved output examples**

`results/annotation/key.parquet`

| `sample_id` | `file_sha` | `label_v21` | `label_v21_at_draw` |
| --- | --- | --- | --- |
| S001 | 31ddcacf4b4642484f29d5e96b758e9fc14a37bf | insufficient_evidence | insufficient_evidence |
| S002 | 6ca36cad67b43e992f66d3aa97f8db3b546c1a09 | highly_templated | highly_templated |

`results/annotation/spot_A.xlsx`

| `sample_id` | `your_label` | `your_confidence` |
| --- | --- | --- |
| S001 | NULL | NULL |
| S002 | NULL | NULL |

`results/annotation/spot_B.xlsx`

| `sample_id` | `your_label` | `your_confidence` |
| --- | --- | --- |
| S034 | NULL | NULL |
| S119 | NULL | NULL |

<a id="step-9"></a>

## Step 9: People complete the blind review

**Run:** see [runbook step 9](PIPELINE_RUNBOOK.md#step-9). **Code:** [`docs/ANNOTATION_GUIDELINES.md`](../docs/ANNOTATION_GUIDELINES.md).

**Inputs and source steps:** [`results/annotation/spot_A.xlsx`](#step-8); [`results/annotation/spot_B.xlsx`](#step-8); [`results/annotation/trailer_check.csv`](#step-8); [`results/annotation/codebook.md`](#step-8); `docs/ANNOTATION_GUIDELINES.md`.

**Outputs:** `results/annotation/spot_A_done.xlsx`; `results/annotation/spot_B_done.xlsx`; `results/annotation/spot_C_done.xlsx`; `results/annotation/trailer_check_done.csv`.

**Processing and algorithm**

Each person independently reads the assigned text and fills `your_label`, `your_confidence` and notes. A third person may review a subset in a copy of A’s sheet. Trailer reviewers answer yes, no or unsure. Save completed files with the exact names shown in the outputs.

**Why this approach**

Independent judgments reveal whether people can apply the definitions consistently. Allowing uncertainty is better than forcing a claim that the visible evidence cannot support.

**Output meaning and limits**

These records are people’s judgments, not verified authorship. The current files have A, B and C answers; the completed trailer sheet is absent. Blank or unassigned answers do not count as completed judgments.

B, C and the trailer review are optional inputs to scoring; A is required. Preserve original submissions as well as scorer-compatible file names.

**Saved output examples**

`results/annotation/spot_A_done.xlsx`

| `sample_id` | `your_label` | `your_confidence` |
| --- | --- | --- |
| S001 | highly_templated | 4 |
| S002 | highly_templated | 4 |

`results/annotation/spot_B_done.xlsx`

| `sample_id` | `your_label` | `your_confidence` |
| --- | --- | --- |
| S034 | agent_assisted | 5 |
| S119 | likely_human | 4 |

`results/annotation/spot_C_done.xlsx`

| `sample_id` | `your_label` | `your_confidence` |
| --- | --- | --- |
| S001 | likely_human | 4 |
| S002 | agent_assisted | 5 |

`results/annotation/trailer_check_done.csv`

_Output is not present in this checkout. No sample records are available._

<a id="step-10"></a>

## Step 10: Score the completed blind review

**Run:** see [runbook step 10](PIPELINE_RUNBOOK.md#step-10). **Code:** [`src/skillsig/s4_validation/spot_check.py`](../src/skillsig/s4_validation/spot_check.py).

**Inputs and source steps:** [`results/annotation/spot_A_done.xlsx`](#step-9); [`results/annotation/spot_B_done.xlsx`](#step-9); [`results/annotation/spot_C_done.xlsx`](#step-9); [`results/annotation/trailer_check_done.csv`](#step-9); [`results/annotation/key.parquet`](#step-8r).

**Outputs:** `results/s4_spot_precision.csv`; `results/s4_spot_reason_precision.csv`; `results/s4_spot_confusion.csv`; `results/s4_spot_agreement.json`; `results/s4_spot_rater_confusion.csv`; `results/s4_spot_overlap_precision.csv`; `results/s4_spot_rater_pairs.csv`; `results/s4_spot_rater_pair_confusion.csv`; `results/s4_spot_rater_c_precision.csv`; `results/s4_trailer_precision.json`; `results/annotation/rater_disagreements.csv`; `results/annotation/rater_c_disagreements.csv`.

**Data flow**

```text
Saved sample key (step 8 or 8r)    Completed answers (step 9)
               |                             |
               +------ join sample_id -------+
                              |
                              v
                    Validate IDs and labels
                              |
             +----------------+----------------+
             |                                 |
             v                                 v
   Pipeline vs each reviewer           Reviewer vs reviewer
             |                                 |
             v                                 v
   Precision + confusion tables        Agreement + kappa
   + Wilson intervals                  + disagreement records
```

**Processing and algorithm**

Match answers by sample ID and reject duplicate IDs, unexpected IDs and unknown labels. Compare A’s answers with each pipeline method. Precision here is agreeing answers divided by predictions of that label. Save confusion tables showing which labels differ. Compare B and C separately on shared items. Use Wilson 95% intervals for proportions. Compute Cohen’s kappa: `(observed agreement - expected agreement) / (1 - expected agreement)`, where expected agreement comes from each person’s label frequencies.

**Why this approach**

ID matching prevents row-order mistakes. Wilson intervals behave better than simple symmetric intervals with small samples or proportions near zero. Kappa accounts for agreement that can occur from frequent labels alone. Keeping raters separate preserves disagreements rather than hiding them in a majority vote.

**Output meaning and limits**

The saved precision measures agreement with a particular reviewer, not true-authorship accuracy. A-based scores and pairwise overlap scores have different denominators. No consensus labels are produced. Trailer precision is written only when the completed trailer sheet exists.

The answer key originates in [step 8](#step-8); step 8r refreshes it only if labels changed. B-based outputs require B’s sheet; C-based outputs require C’s sheet.

**Saved output examples**

`results/s4_spot_precision.csv`

| `version` | `label` | `n_predicted` | `agree` | `precision` | `ci_low` |
| --- | --- | --- | --- | --- | --- |
| label_v21 | highly_templated | 31 | 1 | 0.032258 | 0.0057171 |
| label_v21 | agent_assisted | 30 | 25 | 0.83333 | 0.66435 |

`results/s4_spot_reason_precision.csv`

| `version` | `reason` | `n` | `agree` | `precision` | `ci_low` |
| --- | --- | --- | --- | --- | --- |
| label_v21 | ai_trailer_first:claude | 27 | 22 | 0.81481 | 0.63301 |
| label_v21 | ai_trailer_last:claude | 15 | 15 | 1 | 0.79611 |

`results/s4_spot_confusion.csv`

| `version` | `pipeline_label` | `human_label` | `n` |
| --- | --- | --- | --- |
| label_v21 | agent_assisted | agent_assisted | 25 |
| label_v21 | agent_assisted | highly_templated | 1 |

`results/s4_spot_agreement.json`

| `field` | `saved_value` |
| --- | --- |
| kappa | 0.28772 |
| n_a_labelled | 120 |

`results/s4_spot_rater_confusion.csv`

| `label_a` | `label_b` | `n` |
| --- | --- | --- |
| highly_templated | highly_templated | 0 |
| highly_templated | agent_assisted | 0 |

`results/s4_spot_overlap_precision.csv`

| `rater` | `version` | `label` | `n_predicted` | `agree` | `precision` |
| --- | --- | --- | --- | --- | --- |
| A | label_v21 | highly_templated | 10 | 0 | 0 |
| A | label_v21 | agent_assisted | 10 | 9 | 0.9 |

`results/s4_spot_rater_pairs.csv`

| `rater_x` | `rater_y` | `n_overlap` | `n_agree` | `n_disagree` | `raw_agreement` |
| --- | --- | --- | --- | --- | --- |
| A | B | 40 | 21 | 19 | 0.525 |
| A | C | 60 | 33 | 27 | 0.55 |

`results/s4_spot_rater_pair_confusion.csv`

| `rater_x` | `rater_y` | `label_x` | `label_y` | `n` |
| --- | --- | --- | --- | --- |
| A | B | highly_templated | highly_templated | 0 |
| A | B | highly_templated | agent_assisted | 0 |

`results/s4_spot_rater_c_precision.csv`

| `rater` | `version` | `label` | `n_predicted` | `agree` | `precision` |
| --- | --- | --- | --- | --- | --- |
| C | label_v21 | highly_templated | 15 | 0 | 0 |
| C | label_v21 | agent_assisted | 21 | 20 | 0.95238 |

`results/s4_trailer_precision.json`

_Output is not present in this checkout. No sample records are available._

`results/annotation/rater_disagreements.csv`

| `sample_id` |
| --- |
| S005 |
| S012 |

`results/annotation/rater_c_disagreements.csv`

| `sample_id` | `rater_x` | `rater_y` |
| --- | --- | --- |
| S001 | A | C |
| S002 | A | C |

<a id="step-8p"></a>

## Step 8p: Separate branch: prepare paired reuse review

**Run:** see [runbook step 8p](PIPELINE_RUNBOOK.md#step-8p). **Code:** [`src/skillsig/s4_validation/template_review.py`](../src/skillsig/s4_validation/template_review.py).

**Inputs and source steps:** [`src/data/derived/s3_labels_v2_1.parquet`](#step-6c); [`src/data/derived/s3_labels_v1.parquet`](#step-6a); [`src/data/derived/s2_template.parquet`](#step-5b); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s2_scaffold.parquet`](#step-5d); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); [`src/data/derived/s2_scaffold_parts/`](#step-5d); [`src/data/registry_repos/`](#step-3a).

**Outputs:** `results/annotation/template_review/template_pairs.csv`; `results/annotation/template_review/key.parquet`; `results/annotation/template_review/pair_evidence.json`; `results/annotation/template_review/pairs.html`; `results/s4_template_review_design.json`.

**Processing and algorithm**

Aim for 30 subjects in each exact-registry, near-registry, cluster and scaffold group, while limiting repeated owners and clusters. Find a partner text for each subject. Show full paired texts, differences and shared passages without the historical label. Compute full-body shingle Jaccard directly for each pair: intersection size divided by union size. Record missing partners and sampling shortfalls.

**Why this approach**

People cannot judge copying well from one isolated text. Paired evidence makes the review fit the reuse question. Direct Jaccard is affordable for a small review set and checks the actual pair rather than another signature estimate.

**Output meaning and limits**

The package is a review design, not finished validation. A partner may be missing. Missing evidence must remain visible. This branch is separate from the blind authorship review and does not replace it.

Existing review packages are not overwritten. The answer sheet records `material_reuse` and `shared_scaffold` as yes, no or uncertain; the scoring key is stored separately.

**Saved output examples**

`results/annotation/template_review/template_pairs.csv`

| `pair_id` | `pair_available` | `material_reuse` | `shared_scaffold` | `notes` |
| --- | --- | --- | --- | --- |
| P001 | true | NULL | NULL | NULL |
| P002 | false | NULL | NULL | NULL |

`results/annotation/template_review/key.parquet`

| `file_sha` | `templated_basis` | `legacy_label` | `registry_origin` | `copies` | `owner` |
| --- | --- | --- | --- | --- | --- |
| 24e7543de7737499fbcda022e4baae0c9e81abd7 | registry_exact | highly_templated | false | 45 | akhil-lokesh |
| 0b18a6ea851bfec56b24ebb29d03cb380c758f41 | NULL | agent_assisted | false | 1 | metedata |

`results/annotation/template_review/pair_evidence.json`

| `pair_id` | `left_sha` | `right_sha` | `left_text` | `right_text` |
| --- | --- | --- | --- | --- |
| P001 | 24e7543de7737499fbcda022e4baae0c9e81abd7 | 24e7543de7737499fbcda022e4baae0c9e81abd7 | --- name: autonomous-loops description: "Patterns and architectures for autonomous Claude Code lo… | --- name: autonomous-loops description: "Patterns and architectures for autonomous Claude Code lo… |
| P002 | 0b18a6ea851bfec56b24ebb29d03cb380c758f41 | NULL | --- name: crit-typography description: Use when evaluating font selection, type scale, typographi… | NULL |

`results/annotation/template_review/pairs.html`

This is a review page, so it has no database records.

`results/s4_template_review_design.json`

| `field` | `saved_value` |
| --- | --- |
| note | Separate evidence-aware review; original blind annotations unchanged. |
| pairs | 120 |

<a id="step-9p"></a>

## Step 9p: Separate branch: people review paired texts

**Run:** see [runbook step 9p](PIPELINE_RUNBOOK.md#step-9p). **Code:** [`docs/report/TEMPLATE_EVALUATION.md`](../docs/report/TEMPLATE_EVALUATION.md).

**Inputs and source steps:** [`results/annotation/template_review/pairs.html`](#step-8p); [`results/annotation/template_review/template_pairs.csv`](#step-8p); `docs/report/TEMPLATE_EVALUATION.md`.

**Outputs:** `results/annotation/template_review/template_pairs_done.csv`.

**Processing and algorithm**

Read both texts and the shown evidence. Fill the reuse and scaffold judgments, keeping uncertainty where the evidence is insufficient. Save the completed CSV beside the original sheet.

**Why this approach**

The task asks about visible material reuse and shared structure, which is better aligned with these signals than guessing original authorship.

**Output meaning and limits**

The completed answer file is absent in this checkout. Judgment counts and precision results are unavailable until the completed answer file is returned.

**Saved output examples**

`results/annotation/template_review/template_pairs_done.csv`

_Output is not present in this checkout. No sample records are available._

<a id="step-10p"></a>

## Step 10p: Separate branch: summarize paired judgments

**Run:** see [runbook step 10p](PIPELINE_RUNBOOK.md#step-10p). **Code:** [`src/skillsig/s4_validation/template_review.py`](../src/skillsig/s4_validation/template_review.py).

**Inputs and source steps:** [`results/annotation/template_review/template_pairs_done.csv`](#step-9p); [`results/annotation/template_review/key.parquet`](#step-8p).

**Outputs:** `results/s4_template_pair_judgments.csv`.

**Processing and algorithm**

Validate submitted pair IDs and answer values. Group judgments by sampling stratum and review question. Count yes, no and uncertain answers and report intervals for the supported proportions.

**Why this approach**

Separate strata show whether exact, near, cluster and scaffold candidates behave differently. Keeping uncertainty visible avoids silently turning unclear cases into negative answers.

**Output meaning and limits**

This command needs actual returned judgments. Its output is absent here. Sampling limits and missing text can restrict what the result supports.

**Saved output examples**

`results/s4_template_pair_judgments.csv`

_Output is not present in this checkout. No sample records are available._

<a id="step-11"></a>

## Step 11: Draw the report figures

**Run:** see [runbook step 11](PIPELINE_RUNBOOK.md#step-11). **Code:** [`src/skillsig/s6_report/figures.py`](../src/skillsig/s6_report/figures.py).

**Inputs and source steps:** [`results/s3_evidence_matrix.csv`](#step-6h); [`results/s4_template_provenance.csv`](#step-7b); [`results/s4_scaffold_overlap.csv`](#step-7e); [`results/s4_spot_precision.csv`](#step-10); [`results/s4_disclosure_recall.csv`](#step-7a); [`results/s3_v1_v2_crosstab.csv`](#step-6b); [`results/s3_v2_v21_crosstab.csv`](#step-6c); [`results/s3_text_methods.csv`](#step-6f); [`results/s4_stability.csv`](#step-7d); [`src/data/derived/s2_clusters.parquet`](#step-5a); [`src/data/derived/s3_text_methods_oof.parquet`](#step-6f); [`src/data/derived/s0_devsample_ids.parquet`](#step-2); [`src/data/derived/s2_repo.parquet`](#step-4c); [`src/data/derived/s2_types.parquet`](#step-4d); [`src/data/gitskills_data/data/artifacts/*.parquet`](#step-0); `config/default.yaml`.

**Outputs:** `figures/report/*.png`.

**Processing and algorithm**

Read completed result tables and draw the configured 17 report figures. Some charts group existing rows, such as repository kinds or skill types. The ROC chart varies a score cut-off and plots true-positive against false-positive rates for the proxy groups. The LSH curve uses configured signature settings. Skip charts whose required inputs are absent.

**Why this approach**

Drawing charts from saved tables keeps reports tied to pipeline results. Grouped counts show distributions, while curves show how model results change across score cut-offs.

**Output meaning and limits**

This step produces pictures, not new record tables. Examples below show actual input rows and generated file names. The edit-robustness chart is generated earlier in [step 7c](#step-7c). A successful partial export does not mean every chart was produced.

**Saved output examples**

`figures/report/*.png`

Generated file examples: `figures/report/a_label_versions.png`, `figures/report/b_devsample_groups.png`. Charts contain no database rows. Two source rows from the [step 6h evidence matrix](#step-6h):

| `reuse_signal` | `agent_signal` | `n` | `share_all` | `share_within_agent_signal` |
| --- | --- | --- | --- | --- |
| exact_content_shared | disclosed | 35655 | 0.018986 | 0.22599 |
| exact_content_shared | history_unavailable | 268472 | 0.14296 | 0.18909 |

## Current implementation limits

The current code does not contain `s5_analysis`. The CLI names `analyze` and `rq2`, but both return `not implemented`. Registry change tables exist; the proposed adoption, propagation and maintenance-model stages are not a completed pipeline. The proposed table exporter and HTML explorer are also absent; `export` currently draws figures.

The no-history text score in [step 6g](#step-6g) has code but no production output here. The paired reuse review has a generated package, but no returned answer file or scored judgments. These are different states and should not be described as finished results.
