---
name: skillsig-features
description: Run skillsig pipeline stage s2 features, runbook steps 4a-4e and 5b-5d - structure, commit-history signals (commit), repository context (repo_context), skill topics (skill_type), wording, registry similarity (template_sim), project-specific identifiers (specificity) and scaffolds (scaffold). Use for any per-skill feature build. Near-duplicate clusters (5a) are the separate skillsig-dedup skill.
---

# Features: per-skill evidence (steps 4a–4e, 5b–5d)

Follow the run rules and guardrails in `AGENTS.md`. The user names a step or module, or `all`, and a mode (`dev` or `full`; ask if missing). `all` runs 4a–4e, then 5b–5d, in table order; 5b–5d need the clusters from step 5a (`skillsig-dedup`), so check `src/data/derived/s2_clusters.parquet` first.

| Step | Module | Output | Notes |
|---|---|---|---|
| 4a | `structure` | `src/data/derived/s2_structure.parquet` | |
| 4b | `commit` | `src/data/derived/s2_commit.parquet` | |
| 4c | `repo_context` | `src/data/derived/s2_repo.parquet` | |
| 4d | `skill_type` | `src/data/derived/s2_types.parquet`, `results/s2_skill_type_by_project.csv` | |
| 4e | `wording` | `src/data/derived/s2_wording.parquet` | |
| 5b | `template_sim` | `src/data/derived/s2_template.parquet` | needs 5a and registry 3b–3c |
| 5c | `specificity` | `src/data/derived/s2_specificity.parquet` | long; resumable |
| 5d | `scaffold` | `src/data/derived/s2_scaffold.parquet`, `results/s2_scaffold_top_outlines.csv` | long |

Command: `.venv/bin/python -m skillsig features --only <module>`, plus `--dev-sample` for `dev` and `--force` for `force`.

- **Specificity rebuild:** 5c keeps resumable parts in `src/data/derived/s2_specificity_identifiers` and `s2_specificity_buckets_c*`. For a true forced rebuild (as `scripts/run_full.sh` does), remove those first; confirm with the user before deleting. Without `force`, keep them so an interrupted run resumes.
- In `dev` mode most population counts (copy counts, batch sizes) are still computed on all data; only scaffold spread counts are sample-only. See `docs/stages/s2_features/README.md`.

Details: `docs/PIPELINE_RUNBOOK.md` steps 4a–5d; design notes: `docs/stages/s2_features/<module>.md`.
