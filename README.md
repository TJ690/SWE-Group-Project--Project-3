# SWE-Group-Project--Project-3 - GitSkills Authorship Signal Detection

## Overview
This research studies authorship signals in the GitSkills dataset. It addresses the following question, inspired by the MSR 2027 Mining Challenge:

> Can we distinguish likely human-authored, agent-assisted, and highly templated skills using their structure, wording, and commit history, and how reliable are these signals?

The analysis explores the data and applies simple label rules. Wording features and checks of label reliability are planned. The labels are clues about authorship, not confirmed facts or measures of skill quality. See [the research question](RESEARCH_QUESTION.md), [Sprint 1 report](report/sprint1_report.md), and [limits of the study](THREATS_TO_VALIDITY.md).

## Team

- Product Owner

- Scrum Master

- Developers/Researchers

## Repository Structure

- `src/data/download_data.py` - downloads the raw dataset

- `src/notebooks/` - nine exploratory analysis notebooks

- `db/` - four SQL build scripts and other exploratory SQL queries

- `figures/` - charts used in the research report

- `report/` - research report drafts

- `docs/` - challenge paper, work progress, and retrospectives

- `elicitation/` - notebook interview notes

- `RESEARCH_QUESTION.md` - research question, competing explanations, and planned checks

- `DATA_DICTIONARY.md` - data fields used in the analysis

- `THREATS_TO_VALIDITY.md` - current limits of the study

## Setup
Use Python 3.13. Commands below are run from the repository root.

1. Clone the repository:
   ```bash
   git clone https://github.com/TJ690/SWE-Group-Project--Project-3
   cd SWE-Group-Project--Project-3
   ```

2. Create and activate a virtual environment.

   macOS and Linux:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   Windows (PowerShell):
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```
   The shell prompt shows `(.venv)` while the environment is active. Leave it with `deactivate`.

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Point Jupyter at this environment before opening notebooks in `src/notebooks/`:
   ```bash
   python -m ipykernel install --user --name gitskills --display-name "GitSkills (.venv)"
   ```
   Select the **GitSkills (.venv)** kernel in each notebook.

## Dataset Acquisition
The analysis uses the full GitSkills dataset (Hugging Face: [mvaccargiu/gitskills](https://huggingface.co/datasets/mvaccargiu/gitskills)). It is about 13 GB, so it is never committed to git.

### Where the data lives
Both folders sit under `src/data/` and are listed in `.gitignore`:

```
src/data/
  download_data.py          tracked: downloads the dataset
  gitskills_data/           ignored: raw dataset (download)
    data/
      artifacts/*.parquet
      artifact_siblings/*.parquet
      mining_runs/*.parquet
      repos/*.parquet
  generated_data/           ignored: derived files built from the raw data
    skill_features.parquet
    file_agents.parquet
    repo_profile.parquet
    sibling_files.parquet
```

Keep these exact paths. The notebooks in `src/notebooks/` and the SQL in `db/` read from them. Run `git status` after downloading. It should list no files under `src/data/` other than `download_data.py`.

### 1. Download the raw data
With the virtual environment active (see Setup), run the script from `src/data/`. It writes to `./gitskills_data`, relative to the current folder:

```bash
cd src/data
python download_data.py
cd ../..
```

The download is large and can take a long time. Re-running the script resumes and skips files that are already complete. Check that `src/data/gitskills_data/data/artifacts/` contains `part-*.parquet` files.

### 2. Build the generated files
The EDA notebooks also read four derived Parquet files. Each is built by one SQL script in `db/`. Run these from the repository root. DuckDB is already installed by `requirements.txt`:

```bash
mkdir -p src/data/generated_data
python -c "import duckdb; duckdb.connect().execute(open('db/content_and_structure.sql').read())"
python -c "import duckdb; duckdb.connect().execute(open('db/agent_ecosystem.sql').read())"
python -c "import duckdb; duckdb.connect().execute(open('db/repos.sql').read())"
python -c "import duckdb; duckdb.connect().execute(open('db/siblings_data.sql').read())"
```

| Script | Writes |
|---|---|
| `db/content_and_structure.sql` | `generated_data/skill_features.parquet` |
| `db/agent_ecosystem.sql` | `generated_data/file_agents.parquet` |
| `db/repos.sql` | `generated_data/repo_profile.parquet` |
| `db/siblings_data.sql` | `generated_data/sibling_files.parquet` |

On Windows, use `mkdir src\data\generated_data` (PowerShell: `New-Item -ItemType Directory -Force src/data/generated_data`).

### 3. How the paths resolve
Keep the raw data in `src/data/gitskills_data/` and the generated files in `src/data/generated_data/`.

- **Notebooks:** Run from the repository root or a folder inside it. The notebooks find the data folders automatically. If a required file is missing, download the raw data or build the generated files first.

- **SQL scripts:** Run from the repository root. Their paths start with `src/data/`, as shown in the build commands above.

## Running the Pipeline
After completing the Setup and Dataset Acquisition above:

1. Build all four generated files first, since even the shared notebook setup checks for generated files.

2. Open any notebook in `src/notebooks/` using **GitSkills (.venv)** kernel.

3. Run all cells top to bottom.

Each notebook covers one topic and runs independently after the data files are built:

| Notebook | Topic |
|---|---|
| `EDA_integrity` | Row counts, key integrity, mining runs, adoption over time |
| `EDA_copies` | Exact duplicates, most-copied skills, in-repo vs. cross-owner reuse |
| `EDA_near_duplicates` | Near-duplicate detection via whitespace merges and TF-IDF similarity |
| `EDA_names` | Skill-name distribution |
| `EDA_content` | Body length, structure, and front-matter features |
| `EDA_agents` | Agent folders (`.claude`, `.cursor`, etc.) and cross-agent mirroring |
| `EDA_authorship` | Commit history, bots, AI trailers, Claude models, revisions, bulk commits |
| `EDA_repos` | Repository size, age, stars, and context by label |
| `EDA_siblings` | Extra files bundled with skills |

The pipeline is: **download raw Parquet files → build four derived files with DuckDB → run the notebooks → review tables and plots**. The notebooks group skills using copy counts and commit history.

## Outputs
Each notebook in `src/notebooks/` contains its own tables and plots as saved cell outputs. Open the notebook directly to view the results. The headline findings so far:

- 3,797,117 `SKILL.md` files, but only 1,877,981 distinct contents. This means 50.5% of files are redundant copies.

- Removing front matter, ignoring case, and normalizing spacing merges 198,374 additional contents (10.6% of distinct contents). This groups similar bodies even when their headers differ.

- In a 20,000-skill sample with bodies of at least 200 characters, 12.1% have a near-twin within that sample at cosine similarity >= 0.8. The comparison uses the first 5,000 characters of each normalized text. The share ranges from 5.3% at a .95 threshold to 15.4% at a .60 threshold.

**Adoption over time**

- Among distinct skills with history, first commits grew from 1,265 in October 2025 to a peak of 86,254 in May 2026. June stayed at a similar level. July is only partly collected, so its lower count does not show a confirmed decline.

- Creation dates of repositories that host skills peak at 48,669 in April 2026. These dates show when the repositories were created, not when they first added a skill.

**Data Integrity**

- 0 malformed deduplication groups and 0 representative rows missing content.

Derived data lives in `src/data/generated_data/` and is ignored by git. Report charts are stored in `figures/`.

## GitHub Project
Our backlog and sprint tasks are tracked on the [GitHub project board](https://github.com/users/TJ690/projects/2).

## Sprint 1 Report
The [Sprint 1 report](report/sprint1_report.md) summarizes the dataset exploration, initial findings, and label rules developed during Sprint 1.

## Work Progress and Retrospectives
See [work progress and retrospectives](docs/sprints/README.md) for completed work, lessons learned, and planned next steps.

## Limitations
Only 24.4% of distinct skills have commit history. An AI co-author note can record an installation or sync rather than writing, and a missing note does not prove human authorship. The highly templated label currently means the same text appears in at least 10 files; it does not prove that a template was used. See [THREATS_TO_VALIDITY.md](THREATS_TO_VALIDITY.md).
