# SWE-Group-Project--Project-3 - GitSkills Authorship Signal Detection

## Overview
This project aims to investigate whether we can distinguish likely human-authored, agent-assisted, and highly templated skills by observing structural, linguistic, and commit-metadata patterns in GitSkills artifacts. This is a class project for SWE-380/CSC-580, inspired by the MSR 2027 Mining Challenge, focusing on research question #6. Results are framed as heuristic signals and not confirmed authorship claims - See THREATS_TO_VALIDITY.md.

## Team
 - Product Owner
 - Scrum Master
 - Developers/Researchers

## Repository Structure
- src/ - pipeline code
- src/data/ - data download script; holds the git-ignored dataset and generated files (see Dataset Acquisition)
- src/notebooks/ - EDA notebooks
- db/ - DuckDB SQL that builds the generated data and runs exploratory queries
- tests/ - unit tests
- results/ - generated output tables
- figures/ - generated plots
- report/ - research report drafts
- docs/decisions/ - decision log
- docs/meeting-notes/ - sprint meeting notes
- ai-use-log.md - AI usage disclosure

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
The notebooks and SQL use relative paths, so nothing needs editing after you clone. Relative paths depend on the folder the code runs from:

- **Notebooks** in `src/notebooks/` read `../data/gitskills_data/data/...` and `../data/generated_data/...`. Jupyter and VS Code/Cursor use the notebook's own folder as the working directory. If you run a notebook's code from another folder, change the `*_path` variables in its first cell.
- **SQL** in `db/` reads and writes `src/data/...`. Run it from the repository root, as in step 2.

## Running the Pipeline
After completing the Setup and Dataset Acquisition above:
1. Open any notebook in `src/notebooks/` using **GitSkills (.venv)** kernel.
2. Run all cells top to bottom.

Each notebook covers one topic and runs independently:

| Notebook | Topic |
|---|---|
| `EDA_integrity` | Row counts, key integrity, mining runs, adoption over time |
| `EDA_copies` | Exact duplicates, most-copied skills, in-repo vs. cross-owner reuse |
| `EDA_near_duplicates` | Near-duplicate detection via whitespace merges and TF-IDF similarity |
| `EDA_names` | Skill-name distribution |
| `EDA_content` | Body length, structure, and front-matter features |
| `EDA_agents` | Agent folders (`.claude`, `.cursor`, etc.) and cross-agent mirroring |
| `EDA_authorship` | Commit history, bots, AI trailers, Claude models, revisions, bulk commits |
| `EDA_repos`, `EDA_siblings` | Repository profile and sibling-file analysis |

## Outputs
Each notebook in `src/notebooks/` contains its own tables and plots as saved cell outputs. Open the notebook directly to view the results. The headline findings so far:
- 3,797,117 `SKILL.md` files, but only 1,877,981 distinct contents. This means 50.5% of files are redundant copies.
- Whitespace/formatting normalization merges 198,374 additional contents, about 10.65% of distinct contents that exact-hashing misses.
- On a 20,000-skill sample, 12.1% have a near-twin at cosine similarity >= 0.8. This ranges from 5.3% at a .95 threshold to 15.4% at a .60 threshold.

**Adoption over time**
- Skill creation closely tracks the agent-skill format's launch (Oct 2025): new skills grew from 1,265/month at launch and peaked at 86,000/month by April 2026. It holds at a similar level through June before dropping off in the partial July 2026 snapshot month.
- New repository adoption follows a similar curve, peaking around 48,700 new repos/month in April 2026.

**Data Integrity**
- 0 malformed deduplication groups and 0 representative rows missing content.

  Generated derived data lives in `src/data/generate_data/` (git-ignored, and built with the `db/*.sql` scripts described above).

## Current Status
Sprint 1 complete: research questions finalized, full dataset acquisition pipeline built, 8 exploratory-analysis notebooks covering integrity, duplication, content structure, agent ecosystems, authorship signals, and repository/sibling-file profiles. Initial Sprint 1 report and figures added. See Retrospective-report for full breakdown of what was learned and how we will be moving forward.

## Limitations
See THREATS_TO_VALIDITY.md for full report. Key limitation: There is no way to be sure of the authorship label for GitSkills artifacts, so all classifications are heuristic signals, not facts. 
