---
name: pipeline
description: Run or inspect the whole skillsig pipeline - preview the step list (dry run), run every stage in order on the full data (scripts/run_full.sh) or as a quick dev-sample smoke run, check the progress of a running chain, or show which outputs exist and in which mode. Use when asked to run everything, rebuild all tables and figures, or check pipeline status. For a single step use the matching skillsig-* stage skill.
---

# Pipeline: the whole chain

Follow the run rules and guardrails in `AGENTS.md`. The user asks for one of the actions below. If unclear, start with `status`.

## `plan` (dry run)
```bash
DRY_RUN=1 bash scripts/run_full.sh
```
Prints every step in order without running anything.

## `full`
Full forced rebuild on all data: every table in `results/` and every figure in `figures/report/`.
```bash
caffeinate -i bash scripts/run_full.sh      # macOS; elsewhere: bash scripts/run_full.sh
```
- Takes hours: confirm first, then run in the background. The script writes `logs/run_full_<timestamp>.log`.
- It does **not** re-fetch the registry; add `FETCH=1` only if the user asks for a new registry snapshot.
- If `results/annotation/spot_A_done.xlsx` exists it keeps the sample, refreshes the key and scores it; otherwise it draws fresh sheets and moves unannotated old sheets aside. Tell the user which case applies before starting.
- It needs steps 0 and 1a–1d done first (`skillsig-prepare`).

## `dev`
Quick smoke run on the 50,000-skill sample. `scripts/run_full.sh` has no dev mode, so run the same steps in the same order as in `scripts/run_full.sh`, each as `.venv/bin/python -m skillsig <stage> --only <module> --dev-sample --force` (registry `mine_git` with `SKILLSIG_NO_FETCH=1`), stopping at the first failure. Skip step 8 (`sample`) unless the user asks, so real review sheets are not replaced by dev ones. Warn the user that dev outputs overwrite full outputs at the same paths, and that a `full` run is needed afterwards for reported numbers.

## `status`
- Running chain: show the last ~20 lines of the newest `logs/run_full_*.log` (`ls -t logs/run_full_*.log | head -1`), the current step, and any `FAILED`/`WARN` lines.
- Outputs: `python scripts/output_status.py 'src/data/derived/*.parquet' 'results/*.csv' 'results/*.json'` and summarise: what is missing, which outputs are `dev`, and which were built from an older commit than `git log -1`.

## After a run
Report the total time, any failed or warned step with its log lines, and the result of the regression tests (the script runs them and logs a warning if they fail). Point to `docs/report/FINDINGS.md` for the results.

Single steps: `skillsig-prepare`, `skillsig-intake`, `skillsig-registry`, `skillsig-features`, `skillsig-dedup`, `skillsig-label`, `skillsig-evaluate`, `skillsig-spotcheck`, `skillsig-export`.
