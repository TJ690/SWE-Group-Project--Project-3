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
To be added once the pipeline is implemented

## Outputs
To be added

## Current Status
Pre-sprint: repository skeleton, finalized research question, pulled initial data sample

## Limitations
See THREATS_TO_VALIDITY.md for full report. Key limitation: There is no way to be sure of the authorship label for GitSkills artifacts, so all classifications are heuristic signals, not facts. 
