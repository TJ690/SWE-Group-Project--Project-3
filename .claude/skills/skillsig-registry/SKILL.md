---
name: skillsig-registry
description: Run skillsig pipeline stage s1 registry, runbook steps 3a-3e - mine the history of the 31 registry skill collections (mine_git), build distinct versions (versions), match registry texts to GitSkills (match), compare consecutive versions (changes), and list popular texts outside the registry (discover_popular). Use for any registry, provenance-source or mine_git run.
---

# Registry: history of the 31 skill collections (steps 3a–3e)

Follow the run rules and guardrails in `AGENTS.md`. The user names a step or module, or `all`, and a mode (`dev` or `full`; ask if missing). `all` runs the modules in table order, stopping at the first failure.

| Step | Module | Command | Output |
|---|---|---|---|
| 3a | `mine_git` | `SKILLSIG_NO_FETCH=1 .venv/bin/python -m skillsig registry --only mine_git` | `src/data/derived/s1_versions_raw.parquet`, `s1_commit_trailers.parquet` |
| 3b | `versions` | `.venv/bin/python -m skillsig registry --only versions` | `src/data/derived/s1_registry_versions.parquet` |
| 3c | `match` | `... registry --only match` | `src/data/derived/s1_registry_matches.parquet`, `s1_lineages.parquet` |
| 3d | `changes` | `... registry --only changes` | `src/data/derived/s1_registry_changes.parquet` |
| 3e | `discover_popular` | `... registry --only discover_popular` | `results/s1_popular_candidates.csv` |

Add `--dev-sample` for `dev` and `--force` for `force`.

- **Fetching:** always keep `SKILLSIG_NO_FETCH=1` on 3a unless the user explicitly asks to update the registry snapshot. Fetching pulls new commits and changes every label built on the registry. Missing clones in `src/data/registry_repos/` may still download; say so if that happens.
- `python -m skillsig registry` with no `--only` has no default runner; always run modules one by one.
- Sources are listed in `config/registry_sources.yaml`.

Details: `docs/PIPELINE_RUNBOOK.md` steps 3a–3e; design notes: `docs/stages/s1_registry/` (one file per module).
