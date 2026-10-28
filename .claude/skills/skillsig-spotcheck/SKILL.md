---
name: skillsig-spotcheck
description: Run the skillsig blind human review (stage s4 spot-check), runbook steps 8, 8r, 9, 10 and the paired reuse branch 8p, 9p - draw review sheets (sample), refresh the same sample after label changes (spot_refresh), explain how people annotate, score completed sheets (spot_check), and prepare paired template review packages (template_review). Use for annotation sheets, rater agreement, spot-check precision or the template pair review.
---

# Spot-check: blind human review (steps 8, 8r, 9, 10, 8p, 9p)

Follow the run rules and guardrails in `AGENTS.md`. The user names a step or module and a mode (`dev` or `full`; ask if missing). With no step, check `results/annotation/` and suggest the next one: no sheets → 8; sheets but no `spot_A_done.xlsx` → 9; `spot_A_done.xlsx` present → 8r if labels changed since the key was built, then 10.

| Step | Module | Command | Output |
|---|---|---|---|
| 8 | (default) | `.venv/bin/python -m skillsig sample` | `results/annotation/spot_A.xlsx`, `spot_B.xlsx`, `trailer_check.csv`, `key.parquet`, `trailer_key.parquet`, `codebook.md`; `results/s4_spot_design.json` |
| 8r | `spot_refresh` | `... sample --only spot_refresh` | refreshed `key.parquet`, blank `spot_A.xlsx`/`spot_B.xlsx` |
| 9 | manual | people follow `docs/ANNOTATION_GUIDELINES.md` | `results/annotation/spot_{A,B,C}_done.xlsx`, `trailer_check_done.csv` |
| 10 | `spot_check` | `... sample --only spot_check` | `results/s4_spot_*.csv/json`, `results/s4_trailer_precision.json`, `results/annotation/rater_*disagreements.csv` |
| 8p | `template_review` | `... sample --only template_review` | `results/annotation/template_review/` (pairs CSV, `pairs.html`, key) |
| 9p | manual | people review `results/annotation/template_review/pairs.html` | `template_pairs_done.csv` |

Add `--dev-sample` for `dev` and `--force` for `force`. Scoring the paired review (10p) is in `skillsig-evaluate`.

- **Protect annotations.** Never overwrite, move or delete `*_done.*` files. Before step 8, if `spot_A_done.xlsx` exists, stop and use 8r + 10 instead. If an unannotated `spot_A.xlsx` exists, step 8 keeps it; to draw fresh sheets, move the old ones into `results/annotation/previous_<timestamp>/` (as `scripts/run_full.sh` does) only after the user agrees.
- **Keep keys private.** Do not open, print or summarise `key.parquet`, `trailer_key.parquet` or `template_review/key.parquet` for the user: raters must stay blind. Report only aggregate scores from step 10.
- `results/annotation/` is ignored by git; remind the user to back up completed sheets.
- Step 10 needs A; B, C and the trailer CSV are optional and add agreement outputs.
- For step 9, explain the sheets and point to `docs/ANNOTATION_GUIDELINES.md`; do not fill in answers.

Details: `docs/PIPELINE_RUNBOOK.md` steps 8–10p; design note: `docs/stages/s4_validation/spot_check.md`.
