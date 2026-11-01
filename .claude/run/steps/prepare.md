---
name: skillsig-prepare
description: Set up the GitSkills project before the skillsig pipeline - runbook steps 0 and 1a-1d. Creates the Python environment, downloads the 13 GB Hugging Face dataset, and builds the four SQL tables in src/data/generated_data/ (skill_features, file_agents, repo_profile, sibling_files). Use when asked to set up the project, download the data, or rebuild the generated/SQL tables.
---

# Prepare: environment, dataset, SQL tables (steps 0, 1a–1d)

Follow the run rules and guardrails in `AGENTS.md`. The user names a step (`0`, `1a`–`1d`, or `all`). If none is given, run `python scripts/output_status.py` on the outputs below and offer to build only what is missing. These steps always use the full data: `dev`/`full` does not apply.

| Step | Command | Output |
|---|---|---|
| 0 | `make setup`, then `(cd src/data && ../../.venv/bin/python download_data.py)` | `src/data/gitskills_data/data/{artifacts,artifact_siblings,repos,mining_runs}/*.parquet` |
| 1a | `.venv/bin/python -c "import duckdb; duckdb.connect().execute(open('db/content_and_structure.sql').read())"` | `src/data/generated_data/skill_features.parquet` |
| 1b | same, with `db/agent_ecosystem.sql` | `src/data/generated_data/file_agents.parquet` |
| 1c | same, with `db/repos.sql` | `src/data/generated_data/repo_profile.parquet` |
| 1d | same, with `db/siblings_data.sql` | `src/data/generated_data/sibling_files.parquet` |

- Run `mkdir -p src/data/generated_data` before 1a–1d. Steps 1a–1d need step 0's Parquet files.
- The download is about 13 GB: confirm with the user before starting it, and run it in the background. Re-running resumes and skips finished files.
- Building a table again overwrites it. If it already exists, say so and confirm first.
- Afterwards run `git status` and check that nothing under `src/data/` is listed except `download_data.py`.

Details: `docs/PIPELINE_RUNBOOK.md` steps 0–1d; design note: `docs/stages/P_prepare.md`.
