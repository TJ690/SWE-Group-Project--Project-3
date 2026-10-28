---
name: skillsig-label
description: Run skillsig pipeline stage s3 labeling, runbook steps 6a-6h - label rules v1, v2 and v2.1 (rules_v1, rules_v2, rules_v2_1), comparison baselines, the text style model (style_model), text method comparison (text_methods), optional text scores for skills without history (text_score), and the evidence matrix (evidence). Use when asked to build, rebuild or compare authorship labels.
---

# Labels: rules, baselines, text models, evidence (steps 6a–6h)

Follow the run rules and guardrails in `AGENTS.md`. The user names a step or module, or `all`, and a mode (`dev` or `full`; ask if missing). `all` runs 6a–6f and 6h in table order; 6g is optional and runs only when asked.

| Step | Module | Output |
|---|---|---|
| 6a | `rules_v1` | `src/data/derived/s3_labels_v1.parquet` |
| 6b | `rules_v2` | `src/data/derived/s3_labels_v2.parquet`, `results/s3_v1_v2_crosstab.csv` |
| 6c | `rules_v2_1` | `src/data/derived/s3_labels_v2_1.parquet`, `results/s3_v2_v21_crosstab.csv` |
| 6d | `baselines` | `src/data/derived/s3_baseline_labels.parquet`, `results/s3_baseline_summary.csv` |
| 6e | `style_model` | `src/data/derived/s3_style_scores.parquet`, `results/s3_style_model.{csv,json}`, `results/s3_style_top_words.csv` |
| 6f | `text_methods` | `src/data/derived/s3_text_methods_oof.parquet`, `results/s3_text_methods.{csv,json}`, `results/s3_text_methods_top_words.csv` |
| 6g | `text_score` (optional) | `src/data/derived/s3_text_scores.parquet`, `results/s3_text_score_summary.csv`, `results/s3_text_score.json` |
| 6h | `evidence` | `src/data/derived/s3_evidence.parquet`, `results/s3_evidence_{matrix,signals,legacy_crosstab}.csv`, `results/s3_evidence_summary.json` |

Command: `.venv/bin/python -m skillsig label --only <module>`, plus `--dev-sample` for `dev` and `--force` for `force`.

- Each rule version reads the one before it (6b needs 6a, 6c needs 6b), plus the s1 registry and s2 feature outputs. Check inputs from the runbook before running.
- After running 6a–6c, show the label counts from the crosstab CSVs, so changes are easy to spot.
- In `full` mode, 6a has known counts checked by `python -m pytest -q -m data tests/regression`; offer to run them after 6a–6c.
- Thresholds are in `config/default.yaml`; changing one marks every output stale.
- After a label change, the blind review sample is refreshed with step 8r (`skillsig-spotcheck spot_refresh`), not drawn again.

Details: `docs/PIPELINE_RUNBOOK.md` steps 6a–6h; design notes: `docs/stages/s3_labeling/<module>.md`.
