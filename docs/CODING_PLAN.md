# Coding plan — `skillsig` (RQ1 + RQ2)

Scope: code only. No documentation tasks. Plan date 2026-10-15; Sprint 2 due 2026-10-28.
Design background lives in `ARCHITECTURE_PLAN_RQ1.md`; this file is the build order, the run flow and the data to fetch.

## 1. How the application runs

`skillsig` is a command-line pipeline, not a server. Each stage reads files, writes Parquet or CSV, and records a manifest next to each output. A stage is skipped when its inputs and config are unchanged.

```
pip install -e .                        # Python 3.13
cp .env.example .env                    # put GITHUB_TOKEN in it (only needed for the registry clones' REST metadata and the live sample)

python src/data/download_data.py        # existing script: dataset from Hugging Face (once)
make data                               # build/verify views, schema check, dev sample
make registry                           # clone 31 repos, mine versions, match to dataset, changes
make features                           # structure, commit, repo, wording, skill type
make dedup                              # MinHash clusters + template similarity (long run, start overnight)
make label                              # rules v1 (regression), v2, baselines
make sample                             # 240-skill sample, blind annotation sheets, similarity pairs
# ---- humans annotate sheets in results/annotation/ ----
make evaluate                           # kappa, per-rule precision, confusion, errors, similarity precision
make analyze                            # adoption, patterns, sensitivity, subgroups, ablation, assist probe
make rq2                                # lineage/occurrence models, Kaplan-Meier (live sample if token set)
make export                             # figures, CSV/Parquet tables, HTML explorer
make all                                # everything that can run without humans
make smoke                              # tiny fixture, no network
make test                               # unit tests (data-marked tests excluded)
```

Equivalent CLI: `python -m skillsig <stage> [--dev-sample] [--force] [--config PATH] [--only MODULE]`.

- **Dev vs full:** always build with `--dev-sample` (50k skills, minutes). Full runs only at the end.
- **Environment:** `GITHUB_TOKEN` (optional but recommended), `SKILLSIG_CONFIG` (default `config/default.yaml`).
- **Stage to module map:** data -> `s0_data`; registry -> `s1_registry`; features and dedup -> `s2_features`; label -> `s3_labeling`; sample and evaluate -> `s4_validation`; analyze and rq2 -> `s5_analysis`; export -> `s6_report`.
- **Stage contract:** every module exposes `run(cfg, *, dev=False, force=False) -> StageResult`.

## 2. Data to download

| # | What | How | Size / calls | Needed by | Token |
|---|---|---|---|---|---|
| D1 | GitSkills dataset (3.8M SKILL.md, 282k repos) | `python src/data/download_data.py` (existing; Hugging Face `mvaccargiu/gitskills`) | several GB | T01 onward | no |
| D2 | 31 registry repos, **treeless clones** | `git clone --filter=blob:none --no-checkout https://github.com/<owner>/<repo>.git src/data/registry_repos/<owner>__<repo>` then `git log --no-renames --raw --no-abbrev` (no file downloads, no API) | ~1 GB total, a few minutes | T08, T09 | no |
| D3 | Repo metadata (stars, created, pushed, default branch, archived, license) for the 31 repos | REST `GET /repos/{owner}/{repo}` | 31 calls | T04 cross-check, T06 | recommended |
| D4 | Release labels (tags) for repos that use them | REST `GET /repos/{owner}/{repo}/tags` or `git tag` in the clone (prefer the clone) | 0 calls if from clone | T08 `release_label` | no |
| D5 | Live-history sample: ~300 copying repos | REST `GET /repos/{r}/commits?path=<SKILL.md path>&per_page=100`; `GET /repos/{r}/contents/<path>?ref=<sha>` (read `sha` only, no file content stored) | budget **2,000 calls** (`github.max_calls`), ETag cached | T25 | **required** (60 calls/h unauthenticated is too few) |
| D6 | Candidate origins for popular skill families | REST `GET /repos/{owner}/{repo}` for the top 50 candidates | ~50 calls | T12 | recommended |
| D7 (optional) | Microsoft skills repos | discover with `python -m skillsig registry discover --owner microsoft` (lists repos in the dataset), then add rows to `config/registry_sources.yaml` and clone as D2 | per repo | later | no |
| D8 (optional) | `openai/plugins` | already in the sources file as `candidate`; flip status to `mined` and clone | 1 clone | T08 | no |

Rate limits: authenticated REST is 5,000 calls/hour; the 2,000-call budget fits in one run. The client reads `X-RateLimit-*`, sleeps until reset when remaining < 50, and retries 5xx with backoff. Use ETag conditional requests so repeated runs cost nothing.

Git clone etiquette: sequential clones, no parallel flood; re-run does `git fetch` not a new clone.

## 3. Final repository tree

```
.cursor/
  rules/                 00-project-context.mdc ... 60-task-workflow.mdc
  tasks/                 _TEMPLATE.md, T00-*.md ... T32-*.md
config/                  default.yaml, registry_sources.yaml
docs/CODING_PLAN.md
db/                      (existing, untouched)
pyproject.toml, Makefile, .env.example
src/
  data/                  download_data.py (existing)
                         gitskills_data/ generated_data/ registry_repos/ derived/ cache/   (git-ignored)
  notebooks/             (existing, untouched)
  skillsig/
    __init__.py __main__.py cli.py config.py paths.py provenance.py pipeline.py
    s0_data/             loader.py schema_check.py devsample.py
    s1_registry/         sources.py github_client.py mine_git.py versions.py changes.py
                         change_classifier.py match.py discover_popular.py adoption.py adoption_live.py
    s2_features/         structure.py commit.py repo_context.py wording.py minhash_cluster.py
                         template_sim.py skill_type.py
    s3_labeling/         rules_v1.py rules_v2.py baselines.py
    s4_validation/       sampler.py annotation_sheets.py agreement.py rule_precision.py
                         similarity_validation.py error_analysis.py
    s5_analysis/         patterns.py rq2_maintenance.py assist_probe.py sensitivity.py subgroups.py ablation.py
    s6_report/           figures.py export_tables.py explorer_template.html
tests/                   mirrors src/skillsig (+ fixtures/, regression/)
results/                 csv/json outputs (tracked); results/annotation/ and results/export/ rules in .gitignore
figures/                 existing 01-09, new 10-23
```

Changes against the earlier architecture plan: tests live at repo-root `tests/`; data lives under `src/data/`; `evolution.py` is replaced by `adoption.py`, `change_classifier.py` and `rq2_maintenance.py`.

## 4. Task order

Each row is one Cursor task card in `.cursor/tasks/`. Work top to bottom; a task starts only when its dependencies are done.

| ID | Phase | Task | Depends on | Target | Backlog |
|---|---|---|---|---|---|
| T00 | 0 | Scaffold the package, config, CLI and Makefile | - | 10-15 | GS-0-1 |
| T01 | 0 | Data loader, schema check and dev sample | T00 | 10-16 | GS-1-2 |
| T02 | 0 | Structure features as pure functions, parity with the SQL build | T01 | 10-16 | GS-2-2 |
| T03 | 0 | Commit-history features: AI trailers, bot classes, batches, message classes | T01 | 10-16 | GS-2-3 |
| T04 | 0 | Repository-context features and project kind | T01 | 10-17 | GS-2-4 |
| T05 | 0 | Label rules v1 (port of Sprint 1) with exact regression | T03, T04 | 10-17 | GS-2-5 |
| T06 | 1 | Registry sources loader | T00 | 10-16 | new |
| T07 | 1 | GitHub client with cache and rate-limit handling | T00 | 10-16 | new |
| T08 | 1 | Mine git history: SKILL.md versions and commit trailers | T06 | 10-17 | GS-1-4 |
| T09 | 1 | Registry versions: distinct blobs, sequence, idioms, snapshot flag | T08, T01 | 10-17 | GS-1-4 |
| T10 | 1 | Match registry to the dataset: provenance, mirrors, lineages | T09, T01 | 10-18 | GS-1-4 |
| T11 | 1 | Version-to-version changes and the substantive/cosmetic + preserve/remove/introduce classifier | T09 | 10-19 | GS-5-1, GS-5-7 |
| T12 | 1 | Discover popular skill families and candidate origins | T10 | 10-19 | new |
| T13 | 2 | Wording features W1-W10 | T02 | 10-19 | GS-2-2 |
| T14 | 2 | MinHash/LSH near-duplicate clusters on the full population | T01 | 10-19 | new |
| T15 | 2 | Template similarity: nearest registry version per skill | T10, T14 | 10-20 | new |
| T16 | 2 | Skill-type and project-type taxonomy (v0 rules) | T04 | 10-20 | new |
| T17 | 3 | Label rules v2 with basis, confidence and reasons | T05, T13, T15, T16 | 10-21 | GS-2-5 |
| T18 | 3 | Baselines B0-B6 | T17 | 10-21 | GS-3-3 |
| T19 | 4 | Stratified validation sample (240 skills) | T17 | 10-21 | GS-3-1 |
| T20 | 4 | Blind annotation sheets and answer key | T19 | 10-22 | GS-3-1 |
| T21 | 4 | Annotator agreement and per-rule precision | T20 | 10-23 | GS-3-1 |
| T22 | 4 | Similarity-method validation (pairs by similarity band) | T14, T15 | 10-22 | GS-5-6 |
| T23 | 4 | Error analysis and failure-mode examples | T21 | 10-24 | GS-3-5 |
| T24 | 5 | Adoption metrics: update lag from the dataset | T10, T11 | 10-20 | GS-5-1 |
| T25 | 5 | Live-history sample via the GitHub API | T07, T24 | 10-24 | GS-5-4 |
| T26 | 5 | Propagation patterns P1-P6 | T24, T15, T11 | 10-24 | GS-5-6 |
| T27 | 5 | RQ2 maintenance models: lineage and occurrence level | T24, T26, T17, T08 | 10-25 | GS-5-2, GS-5-5 |
| T28 | 5 | Assistance probe: does content reveal AI involvement within a maintainer? | T13, T02, T08 | 10-25 | new |
| T29 | 5 | Sensitivity, subgroup and ablation analyses for RQ1 | T18, T21 | 10-25 | GS-3-4 |
| T30 | 6 | Figures | T29, T27, T26, T22 | 10-26 | GS-4-2 |
| T31 | 6 | Exportable result tables and single-file HTML explorer | T26, T24, T17, T16 | 10-26 | GS-5-8 |
| T32 | 6 | run_all, smoke test and Makefile wiring | T31, T30, T23, T28 | 10-27 | GS-4-1 |

Critical path: T00 -> T01 -> T03/T04 -> T05 (v1 regression) and T06 -> T08 -> T09 -> T10 -> T15 -> T17 -> T19 -> T20 -> (annotation) -> T21. Long compute to start early: T14 (MinHash) and T08 (clones).

Human gates: annotation of 240 skills by two people (after T20, about 2 days), judging 120 similarity pairs (after T22), the Oct 22 go/no-go on RQ2 as a full question (T24 to T27 become an appendix if annotation is behind).

## 5. Using the task cards with Cursor

1. Open the repo in Cursor. The rules in `.cursor/rules/` load automatically; `00-project-context.mdc` is always on.
2. In Agent chat, attach one card: type `@.cursor/tasks/T05-...md` (pick the file starting with the task id) (or drag the file in) and say: **"Implement this task card. Follow the rules. Stop when the acceptance command passes."**
3. Review the diff, run the acceptance command yourself, commit with message `T05: <title>`.
4. Next card. Do not batch cards: one card per chat keeps context small and diffs reviewable.
5. To make a new card, copy `.cursor/tasks/_TEMPLATE.md`.

Regression anchors Cursor must hit: v1 label counts 35,768 / 80,114 / 62,798 / 1,699,301 (T05); registry union 6,719 blobs and 216,440 files (T10); pilot lag 22% on latest, 29% stale at birth (T24).

## 6. Open decisions

1. Mine Microsoft next (largest owner by file count, 2,154 files)? Adds D7.
2. Who are the two annotators? Optional LLM as third annotator needs an `ai-use-log.md` entry and does not replace the human pair.
3. Skill-type taxonomy is keyword v0 (32% unclassified in the pilot): who hand-codes the 100-skill validation set?
4. Does the instructor allow a secondary question? Decide at the Oct 22 gate.
