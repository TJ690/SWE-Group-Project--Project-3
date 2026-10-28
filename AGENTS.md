# Agent guide: running the skillsig pipeline

Instructions for coding agents (Claude Code, Codex and others) working in this repository. People should start with [README.md](README.md) and [docs/PIPELINE_RUNBOOK.md](docs/PIPELINE_RUNBOOK.md).

## Pipeline skills

Each pipeline stage has a skill in `.claude/skills/` (Codex reads the same folders through `.agents/skills/`):

| Skill | Runbook steps | Stage |
|---|---|---|
| `skillsig-prepare` | 0, 1a–1d | Python setup, dataset download, SQL-built tables |
| `skillsig-intake` | 2 | Schema check and dev sample |
| `skillsig-registry` | 3a–3e | History of the 31 registry collections |
| `skillsig-features` | 4a–4e, 5b–5d | Per-skill features |
| `skillsig-dedup` | 5a | Near-duplicate clusters (MinHash) |
| `skillsig-label` | 6a–6h | Label rules, baselines, text models, evidence |
| `skillsig-evaluate` | 7a–7e, 10p | Validation checks |
| `skillsig-spotcheck` | 8, 8r, 9, 10, 8p, 9p | Blind human review sheets and scoring |
| `skillsig-export` | 11 | Report figures |
| `pipeline` | all | Whole chain, dry run, progress of a running chain |

A step is named by its runbook id (`4b`) or its module (`commit`).

## Run rules (every skill follows these)

1. **Mode: `dev` or `full`.** `dev` adds `--dev-sample` (50,000-skill sample, quick). `full` runs on all 1,877,981 distinct skills. Dev and full runs write to the **same paths**, so a chain must not mix them. If the user did not say which, ask before running, and show the mode of the existing outputs with `python scripts/output_status.py <outputs>` (`mode=dev` is recorded only by modules that filter by the sample; others show `full/unrecorded`, and a row count near 50,000 then suggests dev).
2. **Force.** Add `--force` when the user says `force` or code has changed since the output was built (compare the output's `commit=` with `git log -1`). Without it, a step whose inputs and config are unchanged is skipped.
3. **Python.** Run from the repository root with `.venv/bin/python` if it exists, otherwise `python3`.
4. **Preflight.** Before running, read the step's section in `docs/PIPELINE_RUNBOOK.md` and check its **Input** files exist (`python scripts/output_status.py <inputs>`). If one is missing, name the step that builds it and stop.
5. **Long steps.** Steps that take more than a few minutes (`dedup`, `specificity`, `scaffold`, the full chain, most full-data label steps) run in the background with output written to `logs/<step>_<timestamp>.log`; report when they finish.
6. **After the run.** Run `python scripts/output_status.py <outputs>` on the step's **Output** files and report rows, mode and build time. Report failures with the last lines of the log. Link the design note in `docs/stages/`.

## Guardrails

- Never overwrite, move or delete completed annotations (`results/annotation/*_done.*`). Step 8 (`sample`) is only for a fresh draw; after annotation use 8r (`spot_refresh`).
- Never print, copy or share `results/annotation/key.parquet`, `trailer_key.parquet` or `template_review/key.parquet`: they are the answer keys for the blind review.
- Never re-fetch the registry clones (`FETCH=1`, or `mine_git` without `SKILLSIG_NO_FETCH=1`) unless the user asks: a fetch changes the snapshot every label is built on.
- Steps 9 and 9p are done by people; agents only explain what to do.
- Tests: `python -m pytest -q`; full-data regression checks: `python -m pytest -q -m data tests/regression`.
