---
name: skillsig-evaluate
description: Run skillsig pipeline stage s4 validation checks, runbook steps 7a-7e and 10p - disclosure recall from first and last commits (disclosure_recall), recovery of exact registry texts (template_provenance), similarity after controlled edits (edit_robustness), one-threshold-at-a-time stability (stability), scaffold overlap (scaffold_check), and the paired reuse review summary (template_review). Use when asked to evaluate or validate labels.
---

# Evaluate: known-answer checks (steps 7a–7e, 10p)

Follow the run rules and guardrails in `AGENTS.md`. The user names a step or module, or `all`, and a mode (`dev` or `full`; ask if missing). `all` runs 7a–7e in table order; 10p runs only when asked.

| Step | Module | Output |
|---|---|---|
| 7a | `disclosure_recall` | `results/s4_disclosure_recall.{csv,json}` |
| 7b | `template_provenance` | `results/s4_template_provenance.csv`, `results/s4_template_unexplained.csv` |
| 7c | `edit_robustness` | `results/s4_edit_robustness.csv`, `figures/12_edit_robustness.png` |
| 7d | `stability` | `results/s4_stability.{csv,json}` |
| 7e | `scaffold_check` | `results/s4_scaffold_{overlap,recall,agent_confound,spot}.csv` |
| 10p | `template_review` | `results/s4_template_pair_judgments.csv` |

Command: `.venv/bin/python -m skillsig evaluate --only <module>`, plus `--dev-sample` for `dev` and `--force` for `force`.

- These read the s3 labels and s2 features; check that the labels exist and were built in the same mode.
- 10p needs the people's judgments in `results/annotation/template_review/template_pairs_done.csv` (step 9p). If it is missing, say so and stop.
- `python -m skillsig evaluate` with no `--only` runs only the default set; prefer named modules.
- After the run, summarise the main CSV in a few lines (for example recall per commit position, or which thresholds moved labels most).
- Scoring the blind review (step 10) is in `skillsig-spotcheck`.

Details: `docs/PIPELINE_RUNBOOK.md` steps 7a–7e, 10p; design notes: `docs/stages/s4_validation/<module>.md`.
