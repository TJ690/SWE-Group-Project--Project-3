---
name: skillsig-export
description: Run skillsig pipeline step 11 (stage s6 report) - draw the report figures in figures/report/*.png from the results tables. Use when asked to export, redraw, or regenerate the report charts or figures.
---

# Export: report figures (step 11)

Follow the run rules and guardrails in `AGENTS.md`. The user gives a mode (`dev` or `full`; ask if missing; the report figures are normally drawn from `full` results).

```bash
.venv/bin/python -m skillsig export [--dev-sample] [--force]
```

- **Input:** the `results/` tables from steps 6–10 (evidence matrix, template provenance, scaffold overlap, spot precision, disclosure recall, crosstabs, text methods, stability) plus clusters, repo and type features. Check them with `python scripts/output_status.py` first. A figure whose table is missing is skipped or fails; name the step that builds it.
- **Output:** `figures/report/*.png`, each with a `.manifest.json`.
- After the run, list the figures that changed (`git status figures/report`) and offer to show them. Figures are tracked in git; do not commit unless asked.
- The findings that cite these charts are in `docs/report/FINDINGS.md`.

Details: `docs/PIPELINE_RUNBOOK.md` step 11; design note: `docs/stages/s6_report/README.md`.
