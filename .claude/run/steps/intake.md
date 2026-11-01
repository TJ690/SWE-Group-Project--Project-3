---
name: skillsig-intake
description: Run skillsig pipeline step 2 (stage s0 intake) - register the GitSkills Parquet files as DuckDB views, check the schema, and draw the seeded 50,000-skill dev sample (src/data/derived/s0_devsample_ids.parquet). Use when asked to check the data, run intake, or make or rebuild the dev sample.
---

# Intake: check the data and draw the dev sample (step 2)

Follow the run rules and guardrails in `AGENTS.md`.

```bash
.venv/bin/python -m skillsig data [--force]
```

- **Input:** the raw dataset and the four SQL tables from `skillsig-prepare` (steps 0, 1a–1d). If any is missing, stop and point to that skill.
- **Output:** `src/data/derived/s0_devsample_ids.parquet` (50,000 rows).
- This step builds the dev sample that every later `dev` run uses, so `dev`/`full` does not apply here. The sample is seeded (`seed` in `config/default.yaml`), so a rebuild draws the same ids unless the data or config changed.
- If a schema check fails, report the failing column or table from the output and stop; later steps would fail on the same data.

Details: `docs/PIPELINE_RUNBOOK.md` step 2; design note: `docs/stages/s0_intake/README.md`.
