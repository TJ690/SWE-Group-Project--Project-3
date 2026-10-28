# GitSkills provenance study: repository audit for Sprint 2 planning (as of 2026-10-17)

Repo root: `/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3` (branch `sprint1/exploratory-analysis`). Paths below are relative to that root. "Source" links are local files. Live DuckDB queries were run with the `duckdb` CLI (`/opt/homebrew/bin/duckdb`) against the local parquet. Note: neither the system Python nor anaconda has the `duckdb` Python module, and there is no `.venv` in the repo, so `requirements.txt` has not been installed in any interpreter found.

## Q1. Research question, sub-questions, competing explanations, planned checks, and the existing coding plan

### Takeaway
RQ1 asks whether likely-human, agent-assisted and highly templated skills can be told apart using structure, wording and commit history, and how reliable that signal is. The team has 5 competing explanations, each with a simple baseline. An untracked `docs/CODING_PLAN.md` (dated 2026-10-15, Sprint 2 due **2026-10-28**) plus 33 Cursor task cards (T00–T32) already set out a full `skillsig` Python package. That plan widens the scope to an **RQ2 (maintenance/update propagation)** that RESEARCH_QUESTION.md does not mention. None of that code exists yet.

### Cited Findings
- RQ text: "Can we distinguish likely human-authored, agent-assisted, and highly templated skills by observing structural, linguistic, and commit-metadata patterns in GitSkills artifacts, and how reliable is that signal?" — [RESEARCH_QUESTION.md](RESEARCH_QUESTION.md)
- Taxonomy: Human-Authored, Agent-Assisted, Highly Templated. Unit of analysis is one SKILL.md, deduplicated by content hash (1,877,981 distinct contents). Outcome is a heuristic label, "not a 100% verifiable truth." — [RESEARCH_QUESTION.md](RESEARCH_QUESTION.md)
- Variables: structural (headings, section consistency, length); linguistic (imperative-language density); commit metadata (single vs iterative commits, message patterns); repo context (size, age, stars, tool-family). The dataset has no contributor count. — [RESEARCH_QUESTION.md](RESEARCH_QUESTION.md)
- Competing explanations, each paired with a baseline the method must beat:
  1. Location/tool family (72% of agent-assisted vs 43% of likely-human sit under `.claude/`). Baseline: agent folder alone.
  2. Duplication (templated is defined by 10+ copies). Baseline: copy count alone.
  3. Commit workflow, not writing (a trailer on a bulk install). Baseline: trailer alone, with and without the bulk exclusion.
  4. Adoption timing. Baseline: repo creation date alone.
  5. No separable signal (a valid result).
  — [RESEARCH_QUESTION.md](RESEARCH_QUESTION.md)
- Expected contributions:
  1. A reproducible rule set (likely human / agent-assisted / highly templated / insufficient evidence), with each rule traceable to a DATA_DICTIONARY column.
  2. A reliability estimate: per-rule precision against a hand-labelled sample, sensitivity to the bulk cutoff (6) and copy threshold (10), and comparison with the baselines.
  3. A record of non-separating signals.
  4. A threats account.
  — [RESEARCH_QUESTION.md](RESEARCH_QUESTION.md)
- Threats listed (5):
  1. Authorship is unobservable ("likely human" means no AI sign found).
  2. A trailer may record an install, and some tools leave none.
  3. Many copies does not mean a template.
  4. Only about 1 in 4 skills has history, biased to early adopters and Claude Code users.
  5. Labels are not validated yet.
  — [THREATS_TO_VALIDITY.md](THREATS_TO_VALIDITY.md)
- Sprint 2 action items (repeated in progress.md, retrospective.md and the report §9):
  1. Wording features (command-style language, tone, typical AI phrasing) for all skills, including the 76% without history.
  2. Hand-label about 200–300 skills and report per-rule precision.
  3. Sweep the cutoffs (batch 6, copies 10).
  4. Near-duplicate detection on the full population.
  — [docs/sprints/sprint1/progress.md](docs/sprints/sprint1/progress.md); [docs/sprints/sprint1/retrospective.md](docs/sprints/sprint1/retrospective.md); [report/sprint1_report.md](report/sprint1_report.md)
- **docs/CODING_PLAN.md (untracked) summary.** It describes a CLI pipeline `skillsig`: `python -m skillsig <stage> [--dev-sample] [--force] [--config PATH] [--only MODULE]`.
  - Make targets: `data, registry, features, dedup, label, sample, evaluate, analyze, rq2, export, all, smoke, test`.
  - Stage→module map: data→`s0_data`; registry→`s1_registry`; features+dedup→`s2_features`; label→`s3_labeling`; sample+evaluate→`s4_validation`; analyze+rq2→`s5_analysis`; export→`s6_report`.
  - Every module exposes `run(cfg, *, dev=False, force=False) -> StageResult`. Stages are idempotent and write a manifest per output.
  - Dev runs use a 50k-skill dev sample.
  — [docs/CODING_PLAN.md](docs/CODING_PLAN.md)
- CODING_PLAN data downloads:
  - D1: the HF dataset.
  - **D2: treeless git clones of 31 "registry" repos** (`git clone --filter=blob:none --no-checkout`, then `git log --no-renames --raw --no-abbrev`), about 1 GB.
  - D3: REST repo metadata for those 31 repos.
  - D4: tags.
  - **D5: live-history sample of about 300 copying repos via REST** `commits?path=` and `contents?ref=` (budget 2,000 calls; GITHUB_TOKEN required).
  - D6: candidate origins for the top 50 popular families.
  - D7 (optional): Microsoft repos. D8 (optional): `openai/plugins`.
  — [docs/CODING_PLAN.md](docs/CODING_PLAN.md)
- CODING_PLAN task table: 33 tasks over 7 phases.
  - Phase 0: scaffold/data/features/v1 labels (T00–T05).
  - Phase 1: registry mining/matching/changes (T06–T12).
  - Phase 2: wording, MinHash, template similarity, skill types (T13–T16).
  - Phase 3: labels v2 and baselines (T17–T18).
  - Phase 4: validation sample, annotation sheets, kappa/precision, similarity validation, error analysis (T19–T23).
  - Phase 5: adoption lag, live sample, propagation patterns, RQ2 models, assistance probe, sensitivity/subgroup/ablation (T24–T29).
  - Phase 6: figures, export/HTML explorer, run_all/smoke (T30–T32).
  - Target dates run 10-15 to 10-27. Critical path: T00→T01→T03/T04→T05, and T06→T08→T09→T10→T15→T17→T19→T20→(annotation)→T21.
  — [docs/CODING_PLAN.md](docs/CODING_PLAN.md)
- CODING_PLAN human gates and open decisions:
  - Two people annotate 240 skills (about 2 days after T20).
  - Two reviewers judge 120 similarity pairs (after T22).
  - Oct 22 go/no-go on RQ2 (T24–T27 become an appendix if annotation is behind).
  - Open: mine Microsoft (2,154 files)? Who are the annotators (an LLM as third annotator needs an `ai-use-log.md`)? Who hand-codes 100 skills for the skill-type taxonomy (32% unclassified in the pilot)? Does the instructor allow a secondary question?
  — [docs/CODING_PLAN.md](docs/CODING_PLAN.md)
- CODING_PLAN regression anchors:
  - v1 label counts 35,768 / 80,114 / 62,798 / 1,699,301.
  - Registry union 6,719 blobs / 216,440 files.
  - Pilot lag 22% on latest / 29% stale at birth.
  — [docs/CODING_PLAN.md](docs/CODING_PLAN.md)
- RQ2 is defined only in the Cursor rules: "RQ2 (extension): do those signals go with maintenance and update propagation?" — [.cursor/rules/00-project-context.mdc](.cursor/rules/00-project-context.mdc)

### Inferences
- The plan is ambitious (33 cards, 13 days, two human annotation gates, a GitHub-API live sample, git clones of 31 repos). RQ2 (T24–T27, T25 live API, T26 patterns) and the registry provenance track (T06–T12, T15) add the most scope beyond what RESEARCH_QUESTION.md commits to. A "finish all code in one sprint" plan should treat Phase 0, T13, T14, T17–T23 and T29–T32 as the RQ1 core and the registry/RQ2 cards as stretch. The alternative is to formally amend RESEARCH_QUESTION.md to make provenance (registry origin and lineage) central.
- "Provenance" in the requested study title maps to the T06–T12/T15/T24–T27 registry-lineage track. The Sprint 1 docs never use the word.

### Gaps
- `CODING_PLAN.md` says "Design background lives in `ARCHITECTURE_PLAN_RQ1.md`". That file is not in the repo or the parent folder.
- The pilot that produced the anchors (31 registry repos mined "in the 2026-10-15 pilot", 6,719 blobs, lag 22%/29%, assist-probe AUC 0.52/0.64, 1,374 lineages, 32% unclassified skill types) has no code, data or notebook in the repo. Those numbers cannot be verified here.
- Instructor approval for RQ2 is undocumented.

## Q2. Dataset schema and provenance-bearing fields

### Takeaway
Four raw tables: artifacts 3,797,117 rows; artifact_siblings 7,264,865; repos 282,200; mining_runs 7. All were verified locally with DuckDB.

Provenance signals:
- `file_sha` (a git blob SHA-1, 40 hex, which joins to `git log --raw` blob IDs).
- Per-occurrence `repo_full_name`/`path`/`location_class` for every copy.
- First/last commit timestamps, anonymised author codes, author types and full redacted messages with `Co-authored-by` AI names kept. These exist **only for representatives with history**.
- Sibling `entry_sha` and registry metadata files (`_meta.json`, `.clawhub/origin.json`).
- Front-matter `author`/`source`/`version`/`metadata` keys.
- `repos.created_at`/`pushed_at`/`stars`/`license`.

### Cited Findings
- `artifacts` columns (live DESCRIBE; varchar unless noted):
  - Location and identity: `repo_full_name, path, filename, location_class, file_sha, discovered_at`.
  - Content and front matter: `content, content_fetched(bigint), frontmatter_valid(bigint), name, description, body_chars(bigint)`.
  - Fetch flags: `history_fetched, composition_fetched, dedup_primary` (bigint).
  - History: `first_commit_at, last_commit_at, commit_count(bigint)`.
  - Composition: `sibling_count, sibling_bytes, has_scripts, has_references, content_sha_ok, composition_truncated` (bigint).
  - Authors and messages: `first_commit_author, first_commit_author_type, first_commit_message, last_commit_author, last_commit_author_type, last_commit_message`.
  — local DuckDB `describe` on `src/data/gitskills_data/data/artifacts/*.parquet`
- `artifact_siblings`: `repo_full_name, artifact_path, entry_name, entry_type, entry_size, entry_sha, content, content_fetched, skipped_reason`. `repos`: `full_name, owner, stars, forks, is_fork, language, license, description, created_at, pushed_at, metadata_fetched`. `mining_runs`: `run_id, artifact_type, query, started_at, finished_at, discovered, note`. Dates are stored as ISO strings, not timestamps. — local DuckDB describe
- Row counts: artifacts 3,797,117; siblings 7,264,865; repos 282,200; mining_runs 7. `file_sha` length is 40 for every row. — local DuckDB count
- mining_runs, all with query `filename:SKILL.md`:
  - run 1 (2026-07-09, 11,796 discovered)
  - run 2 (07-09→07-12, 1,328,473)
  - run 5 (07-14→07-20, 1,657,948)
  - runs 3, 4, 6, 7: 0, "closed retroactively: process crashed before finishing (repair-runs)"
  - max `finished_at` = 2026-07-20T22:26:56 (the snapshot date used by the plan)
  — local DuckDB select
- `location_class` × `dedup_primary`: canonical 273,721 reps / 99,931 copies; skills-dir 1,037,938 / 1,064,115; other 566,322 / 755,090. — local DuckDB
- `content_sha_ok` among representatives: 1 = 1,851,135; 2 (repaired) = 26,804; 0 = 42. Data dictionary describes it as 0/1 only, which is incomplete. — local DuckDB; [DATA_DICTIONARY.md](DATA_DICTIONARY.md)
- History/composition among representatives:
  - history_fetched=1: 458,137 (424,983 untruncated + 33,153 truncated + 1 without composition).
  - composition_fetched=0: 1,544.
  - composition_truncated=1: 252,250.
  - The docs and paper say 458,548 with history; the local data shows 458,137 (EDA_authorship also prints 458,137).
  — local DuckDB; [src/notebooks/EDA_authorship.ipynb](src/notebooks/EDA_authorship.ipynb); [docs/Git-skills.pdf](docs/Git-skills.pdf)
- first_commit_at ranges 2014-06-30 to **2026-07-23**, which is later than the last discovered_at (2026-07-20T22:08). Some histories were fetched after discovery. — local DuckDB
- Filenames: SKILL.md 3,742,828; skill.md 49,314; Skill.md 3,530; SKILL.MD 1,426; other variants 10. — local DuckDB
- first_commit_author_type (reps with history): User 390,600; empty 62,750; Bot 4,630; Organization 153; Mannequin 3; ProgrammaticAccessBot 1. User author codes are 16-hex keyed hashes, stable across the dataset; bot logins are kept. — local DuckDB; [src/data/gitskills_data/README.md](src/data/gitskills_data/README.md)
- Messages containing "co-authored-by:" among reps with history: first 144,308; last 147,676. "Generated with … Claude Code" in first message: 7,753. "Signed-off-by": 3,071.
  - Top first-commit co-author names: Claude Opus 4.6 (20,397), Claude Opus 4.6 (1M context) (17,231), Claude Sonnet 4.6 (17,176), Claude Opus 4.7 (1M) (16,153), Claude Opus 4.8 (1M) (11,576), Claude Opus 4.8 (11,518), Claude Opus 4.5 (9,261), Claude Fable 5 (7,182), redacted/blank (6,576), Cursor (3,515), Copilot (2,403), claude-flow (245), Codex (235), github-actions[bot] (136), coderabbitai[bot] (122), factory-droid[bot] (115).
  — local DuckDB regexp on `first_commit_message`
- Front-matter provenance keys among the 1,877,981 representatives: `author:` 192,132; `source|origin|repository|homepage|url:` 122,803; `version:` 341,645; `metadata:` 264,088; `license:` 215,357; any "generated/generator" string in front matter 54,112. — local DuckDB regex over `content`
- Registry/installer metadata siblings: `_meta.json` 23,747 rows (e.g. `{"ownerId":…,"slug":"grok","version":"1.0.3","publishedAt":…}`); `skill.json` 6,126; `manifest.json` 3,823; `.clawhub/origin.json` 2,649 (e.g. `{"registry":"https://clawhub.ai","slug":"grok","installedVersion":"1.0.3","installedAt":…}`); `_download_meta.json` 738; `SKILL.json` 738. — local DuckDB on artifact_siblings
- `skipped_reason` is NULL for every sibling row; no files were skipped. — local DuckDB; [src/notebooks/EDA_siblings.ipynb](src/notebooks/EDA_siblings.ipynb)
- Key caveats:
  - Content, name, description and history are filled only for `dedup_primary=1`. A copy's text or name needs a join on `file_sha`.
  - Commit fields describe the representative's repository only.
  - The representative is NOT assumed to be the origin (the selection prefers `.claude/skills/` with a deterministic tie-break).
  - History follows the current path only, so renames lose earlier commits.
  - `name` is not an identity.
  - Forks are almost absent (3 in total).
  — [.cursor/rules/20-data-and-schemas.mdc](.cursor/rules/20-data-and-schemas.mdc); [docs/Git-skills.pdf](docs/Git-skills.pdf); [DATA_DICTIONARY.md](DATA_DICTIONARY.md)
- Generated tables (local):
  - `skill_features` 1,840,872 rows (cols: file_sha, location_class, frontmatter_valid, body_chars, desc_chars, body_words, headings, h2, numbered_items, bullet_items, code_blocks, table_rows, links, has_examples_section, has_when_to_use, fm_allowed_tools, fm_license, fm_version_or_metadata, non_ascii_ratio).
  - `file_agents` 3,797,117.
  - `repo_profile` 282,200 (repo cols + files, distinct_skills, copy_rows, copy_share, name_has_skill, name_is_collection, name_has_agent, size_tier).
  - `sibling_files` 5,858,945 (files only, + top_folder, subfolder, ext, file_category).
  — local DuckDB; [DATA_DICTIONARY.md](DATA_DICTIONARY.md)

### Inferences
- Provenance can be built at three levels:
  - (a) Exact copy genealogy: `file_sha` across all 3.8M occurrences, plus first_commit_at for the one representative.
  - (b) Registry lineage: dataset `file_sha` = blob SHAs from git history of known source repos (T08–T10). This is the only way to get version sequences.
  - (c) Installer metadata: `_meta.json` / `.clawhub/origin.json` / front-matter `source`/`version`. Sprint 1 and the plan do not use these. They are cheap, deterministic templated/installed signals that would strengthen the `highly_templated` label for skills with no history.
- Because copies have no history, "earliest copy" ordering is impossible from the dataset alone. Only `repos.created_at` and `discovered_at` exist per copy.
- Front-matter `author:` (192k) and model names in trailers could serve as features or audit fields. `author:` may hold personal names, which the responsible-label rules forbid storing.

### Gaps
- Whether `location_class` "canonical" exactly equals `.claude/skills/` was not checked (the data dictionary says the authors define it).
- No check was made on what fraction of `Co-authored-by` blanks (6,576) are redacted human names versus empty values.

## Q3. Existing code, configs, and label rules

### Takeaway
The only executable code is SQL scripts, 9 EDA notebooks and a 7-line download script. There is **no `src/skillsig/`, `tests/`, `pyproject.toml`, `Makefile` or `results/`**. `config/*.yaml`, `.env.example` and the `.cursor/` rules and task cards are untracked scaffolding written for the planned package. Label rules v1 exist only as inline DuckDB SQL inside `EDA_authorship.ipynb`, re-implemented in `EDA_content` and `EDA_repos`.

### Cited Findings
- `src/data/download_data.py`: `huggingface_hub.snapshot_download(repo_id="mvaccargiu/gitskills", repo_type="dataset", local_dir="./gitskills_data")`. It must be run from `src/data/`. The CODING_PLAN instead says to run it from the repo root, which would write `./gitskills_data` at the root. — [src/data/download_data.py](src/data/download_data.py); [docs/CODING_PLAN.md](docs/CODING_PLAN.md)
- `requirements.txt`: duckdb 1.5.5, huggingface_hub 2.0.0, ipykernel 7.3.0, matplotlib 3.11.1, numpy 2.5.2, pandas 3.0.5, scikit-learn 1.9.0, seaborn 0.13.2. Not present: pyarrow, pyyaml, scipy, statsmodels, datasketch, httpx, pytest (all needed by T00). — [requirements.txt](requirements.txt); [.cursor/tasks/T00-scaffold-the-package-config-cli-and-makefile.md](.cursor/tasks/T00-scaffold-the-package-config-cli-and-makefile.md)
- `.env.example`: `GITHUB_TOKEN=`, `SKILLSIG_CONFIG=config/default.yaml`. — [.env.example](.env.example)
- `config/default.yaml`:
  - seed 20261015; chunk_rows 50000; paths (raw, generated, registry_repos, derived, cache, results, figures).
  - thresholds: bulk_batch_min 6, templated_copies_min 10, templated_jaccard_min 0.80, insufficient_conf_floor low.
  - minhash: num_perm 128, shingle_words 5, body_max_chars 5000, lsh_threshold 0.80, lsh_threshold_low 0.60.
  - rq2: substantive_body_change_pct 5, grace_days 30, sensitivity [0,30,90], snapshot_date auto.
  - sample: per_label 60, boundary_oversample, similarity_pairs 120.
  - github: user_agent, per_page 100, max_calls 2000, live_sample_repos 300, checkpoint_every 25.
  — [config/default.yaml](config/default.yaml)
- `config/registry_sources.yaml`: 32 entries (31 `mined` + `openai/plugins` candidate).
  - Tier A (anthropics/skills, openai/skills [deprecated 2026-06-22]).
  - Tier B community: obra/superpowers, affaan-m/ECC, mattpocock/skills, garrytan/gstack, nextlevelbuilder/ui-ux-pro-max-skill, addyosmani/agent-skills, kepano/obsidian-skills, coreyhaines31/marketingskills, wshobson/agents, phuryn/pm-skills, blader/humanizer, vercel-labs/agent-skills, bmad-code-org/BMAD-METHOD.
  - Tier G Google/GitHub: google/skills, gemini-cli, gemini-managed-agents-templates, stitch-skills, googleworkspace/cli, adk-python, adk-samples, agents-cli, flutter/skills, flutter/agent-plugins [mirror_of flutter/skills], gke-mcp, github/awesome-copilot, spec-kit, gh-aw, copilot-cli-for-beginners, actions-migrations-via-copilot.
  — [config/registry_sources.yaml](config/registry_sources.yaml)
- `.cursor/rules`:
  - 00 project context: taxonomy strings `highly_templated, agent_assisted, likely_human, insufficient_evidence`; module header; stage contract; manifest.
  - 10 Python style: DuckDB for over 100k rows, chunked processing, zstd parquet, deterministic.
  - 20 data/schema and v1 rules.
  - 30 GitHub access: git over the API, ETag cache, 2,000-call budget, never log the token, metadata only.
  - 40 testing: pytest markers `data`/`network`/`slow`, regression anchors including lag pilot 186,975 / 41,590 / 145,385 / 53,661 / 109,137.
  - 50 responsible labels: no `is_ai`/`written_by` names, confidence + reasons, blind sheets, report with and without registry-matched files.
  - 60 task workflow.
  — [.cursor/rules/](.cursor/rules/00-project-context.mdc)
- db SQL scripts:
  - `content_and_structure.sql` writes `skill_features.parquet`. Readable filter: dedup_primary=1, no symlink stub, not 'Redirecting', body_chars ≥ 200. Regex structural features. Summary queries.
  - `agent_ecosystem.sql` writes `file_agents.parquet`: first dot-folder mapped to 16 tool families.
  - `repos.sql` writes `repo_profile.parquet`.
  - `siblings_data.sql` writes `sibling_files.parquet`: ext → file_category.
  - `authorship.sql`: ad-hoc queries for history coverage, author types, bot list, trailer tool counts, Claude model names, commit-count buckets, batches, AI share by month/location/stars, population filter. It has a missing semicolon after the batch query, so it is not runnable as one script.
  - `gitskills.sql`: exploratory schema, count and copy queries (missing semicolon after the owners query).
  - `skills_queries.sql`: queries against the 29,786-row sample DB (bare table names).
  — [db/](db/content_and_structure.sql)
- Notebooks (all read parquet via DuckDB, outputs saved in cells):
  - `EDA_integrity`: 0 bad dedup groups, 0 reps without text, 2,348 copies with text, mining runs, monthly adoption.
  - `EDA_copies`: copy distribution, top skills by owners, in-repo vs cross-owner, reuse by location/flags/stars, templated-threshold sensitivity (files ≥ 10: 35,768; > 10: 31,735; ≥ 10 & owners ≥ 2: 33,719; ≥ 10 owners: 24,555).
  - `EDA_near_duplicates`: normalised-hash merges 198,374; TF-IDF on a 20k sample; near-twins vs trailers.
  - `EDA_names`: 162,855 shared names cover 950,937 skills; code-review 3,690 texts; skill-creator 2,765.
  - `EDA_content`: population filters, structure features, structure by label and by label×location.
  - `EDA_agents`: agent-folder counts, combos, mirroring.
  - `EDA_authorship`: history coverage, author types, bot groups, trailer tools, Claude models, revision activity, trailer transitions, bulk batches, install messages, **v1 label SQL**, trailer disposition, cutoff sensitivity, AI share by month/location/stars, commit patterns by label.
  - `EDA_repos`: licenses, owner concentration, size tiers, large-repo copy share, age, language, name flags, context by label.
  - `EDA_siblings`: bundle coverage, kinds, script languages, folder conventions, bundled-file reuse (27.5% duplicate).
  — [src/notebooks/](src/notebooks/EDA_authorship.ipynb)
- **Exact v1 label definition (EDA_authorship SQL).**
  - Inputs:
    - `copies` = count(*) per file_sha over all files.
    - `batch_n` = count of reps with history sharing (repo_full_name, first_commit_at).
    - `bot_group`: not a bot unless type in (Bot, ProgrammaticAccessBot). Then `CI, release and sync` (github-actions/dependabot/renovate, or login contains release or sync), else `Coding agents` (regex `copilot|devin|jules|opencode|^claude|lovable|kilo-code|codex|factory-droid|amazon-q|codegen|capy-ai|warp-dev|cursor`), else `Skill distribution` (`clawdhub|txtskills|skill`), else `Other project bots`.
    - `ai_first`/`ai_last` = `lower(message) LIKE '%co-authored-by:%<tool>%'` for claude, anthropic, cursor, copilot, codex, openai, gemini.
  - Rules, first match wins:
    - templated = copies ≥ 10.
    - agent = history AND batch_n < 6 AND bot_group not in the non-coding groups AND (Coding agents OR ai_first).
    - human = history AND batch_n = 1 AND type='User' AND NOT ai_first AND NOT ai_last AND copies = 1 AND bot_group = 'not a bot'.
    - else insufficient.
  - Result: 35,768 / 80,114 / 62,798 / 1,699,301.
  — [src/notebooks/EDA_authorship.ipynb](src/notebooks/EDA_authorship.ipynb)
- Commit message classes (not label inputs): install/sync/import, repository scaffold, conventional prefix, add/upload, short generic (≤ 12 chars), other. Trailer lines are stripped first. — [DATA_DICTIONARY.md](DATA_DICTIONARY.md)

### Inferences
- **Regression risk for T03/T05.** The notebook's trailer test is a substring LIKE that matches "co-authored-by:" followed anywhere later in the message by the tool name. That includes a non-AI co-author line followed later by "Generated with Claude Code"-style text. T03's spec says "only Co-authored-by trailers count." A strict trailer parser will probably NOT reproduce 80,114 exactly. The port must replicate the LIKE semantics for v1 (and include `ProgrammaticAccessBot` and the `bot_group='not a bot'` clause for likely_human), then improve in v2.
- The Cursor rules' v1 summary omits the `bot_group = 'not a bot'` condition for likely_human and the "Skill distribution" bot group. The card's `classify_author` classes (coding_agent_bot | noncoding_bot | user | organization | unknown) must still map distribution bots to non-coding.
- The 4 generated parquet files exist locally and are current, so T02's parity test can run.

### Gaps
- The notebooks' plotting code that saved `figures/*.png` was not traced cell by cell. The figures appear to be exported manually.

## Q4. Sprint 1 findings and figures

### Takeaway
Sprint 1 delivered descriptive EDA and v1 labels.

- Highly templated skills are separable (but by construction).
- Human vs agent-assisted are nearly indistinguishable on structure. Location (`.claude/`) explains much of the gap.
- 90.5% of skills are "insufficient evidence" because of history coverage.
- Reliability has not been measured.

### Cited Findings
- Figures (9 PNGs, ~18–43 KB): `01_adoption_over_time`, `02_files_per_repo`, `03_copies_per_skill`, `04_where_copies_live`, `05_skill_length`, `06_revision_activity`, `07_bulk_commit_batches`, `08_authorship_label_counts`, `09_structure_by_label`. — [figures/](figures/)
- Scale: 3,797,117 files; 282,200 repos; 195,841 owners; 1,877,981 distinct; 50.5% of files are redundant copies; 79.3% of distinct skills are singletons; max copies 1,409. — [report/sprint1_report.md](report/sprint1_report.md)
- Copied contents (388,501): within one repo 82,413 (21.2%); multi-repo one owner 75,733 (19.5%); multi-owner 230,355 (59.3%). — [report/sprint1_report.md](report/sprint1_report.md)
- Adoption: first commits peak at 86,254 in 2026-05. Repo creation peaks at 48,669 in 2026-04. 86.2% of repos were created ≥ Oct 2025. 59.3% have 0 stars. 312 repos with more than 1,000 files hold 36.7% of files. 1,565 large repos are mostly copies (≥ 80%) and hold 28.9% of files. — [report/sprint1_report.md](report/sprint1_report.md); [src/notebooks/EDA_repos.ipynb](src/notebooks/EDA_repos.ipynb)
- Content: median 639 words; 36.3% bundle files; 11.4% ship scripts (Python 63%); 13.4% have invalid front matter; 66.3% of skills with history have 1 commit. — [report/sprint1_report.md](report/sprint1_report.md)
- Trailers: 30.3% (138,670) of skills with history have an AI trailer on the first commit; 95.2% of those are Claude. Of the 138,670: 79,607 (57.4%) stay agent-assisted; 53,705 (38.7%) are dropped as bulk; 5,242 (3.8%) are templated; 116 are non-coding bots. Agent-assisted count would be 109,423 with a batch < 21 cutoff and 134,028 with no cutoff. — [report/sprint1_report.md](report/sprint1_report.md)
- Bulk: 54,362 bulk commits; 326,343 skills in bulk commits; 42,437 repos. — [src/notebooks/EDA_authorship.ipynb](src/notebooks/EDA_authorship.ipynb)
- Trailer transitions among revised skills (154,254): never 56.3%; throughout 23.4%; gained 10.8%; lost 9.5%. Same author first and last: 85.6%. — [src/notebooks/EDA_authorship.ipynb](src/notebooks/EDA_authorship.ipynb)
- Bots: 4,630 skills first committed by bots across 140 accounts; github-actions[bot] 2,997 (65%); coding agents about 926 (Copilot 714, Devin 81, Jules 48). — [src/notebooks/EDA_authorship.ipynb](src/notebooks/EDA_authorship.ipynb)
- Label comparison (human / agent / templated):
  - Median words 589 / 661 / 854.
  - Median headings 10 / 11 / 18.
  - Examples section 12.9 / 12.3 / 28.5%.
  - When-to-use 14.1 / 13.5 / 35.5%.
  - License field 5.2 / 3.6 / 20.8%.
  - One commit 57.8 / 54.6 / 94.4%.
  - Median repo files 5 / 8 / 238.
  - Under `.claude/`: 43.0 / 72.0 / 57.7%.
  - Repo created before 2025: 15.3 / 9.0 / 4.5%.
  - Code block 70.2% vs 76.6% (human vs agent).
  - Install/sync wording 8.7% vs 19.6%.
  - Inside canonical folders, median words 694 vs 736.
  — [report/sprint1_report.md](report/sprint1_report.md)
- Templated: 1.9% of distinct contents but 31.4% of files (1,192,572). — [report/sprint1_report.md](report/sprint1_report.md)
- Near-duplicates:
  - Normalising merges 198,374 more contents (10.6%).
  - TF-IDF 20k sample, first 5,000 chars: 12.1% have a twin at ≥ 0.8 (5.3% at 0.95; 15.4% at 0.6). 45.5% of twins in the 0.7–0.95 band have a different name. Same owner 1,274 vs different owner 442.
  - Formatting twins carry AI trailers less often (21.5% vs 31.8%).
  — [src/notebooks/EDA_near_duplicates.ipynb](src/notebooks/EDA_near_duplicates.ipynb)
- Top-adopted skills come from Anthropic official skills or obra/superpowers (frontend-design: 897 owners / 951 repos / 1,245 files). — [src/notebooks/EDA_copies.ipynb](src/notebooks/EDA_copies.ipynb)

### Inferences
- The strongest current evidence is (a) explicit trailers and coding-agent bot authors, and (b) copy count. Structure barely separates human from agent. This makes competing explanation 5 ("no separable signal") a plausible headline, so the Sprint 2 code must produce baselines B0–B6 and an ablation to test it formally.

### Gaps
- No inferential statistics (CIs, tests) exist in Sprint 1. All numbers are descriptive.

## Q5. Planned but unimplemented work: concrete gaps

### Takeaway
Everything in Sprint 2 is unimplemented. The package, tests and results directories do not exist. Even T00 (target 10-15) and T01–T05 (targets 10-16/17) are not started, so the plan is already about 2–3 days behind as of 10-17.

### Cited Findings
- Missing files: `pyproject.toml`, `Makefile`, `src/skillsig/`, `tests/`, `results/` (verified with `ls`). `reports/` and `research_notes/` are empty new folders. Untracked: `.cursor/`, `.env.example`, `config/`, `docs/CODING_PLAN.md`. — local `ls`; git status
- `.gitignore` lacks the T00 additions (`src/data/registry_repos/`, `src/data/derived/`, `src/data/cache/`, `.env`, `results/export/*.parquet`, `results/annotation/`). — [.gitignore](.gitignore); [.cursor/tasks/T00-scaffold-the-package-config-cli-and-makefile.md](.cursor/tasks/T00-scaffold-the-package-config-cli-and-makefile.md)
- Specified but unbuilt items and their outputs:
  - **Wording W1–W10** (T13): imperative-line ratio, you/your per 100 words, command density, Flesch-Kincaid, MATTR-500, emphasis markers (MUST/NEVER/…), bullet-length CV, heading-level entropy, phrase-lexicon hits (lexicon from training split only), non-English flag, desc_idiom. Output `s2_wording.parquet`.
  - **MinHash/LSH full-population clusters** (T14): 128 perms, 5-word shingles, thresholds 0.8/0.6, memory-mapped signatures about 0.9 GB.
  - **Template similarity to registry** (T15).
  - **Skill-type taxonomy** (T16): 14 types.
  - **Labels v2** (T17): templated basis registry_exact > registry_near > cluster > copy_count; confidence high/medium/low; reasons.
  - **Baselines** B0 majority, B1 copy count, B2 agent folder, B3/B3b trailer ± bulk, B4 repo date, B5 text-only model, B6 full model (T18), with GroupKFold by owner + cluster component.
  - **240-skill stratified sample** (T19): 60 per label, half near thresholds, ≤ 2 per repo, ≤ 1 per cluster.
  - **Blind sheets** (T20).
  - **Cohen's kappa + Wilson CI per-rule precision + confusion** (T21).
  - **Similarity validation** of 120 pairs in 5 bands (T22).
  - **Error analysis** (T23).
  - **Cutoff sweep** bulk 2–12 × copies 5–20, subgroups, ablation vs hand labels (T29).
  - **Figures 10–23** (T30).
  - **Export + HTML explorer** (T31).
  - **Smoke test** (T32).
  — [.cursor/tasks/](.cursor/tasks/T13-wording-features-w1-w10.md)
- Registry/RQ2 track:
  - T06 sources loader; T07 GitHub client (ETag sqlite cache).
  - T08 mine git versions + trailers. Large repos: gh-aw has 18,746 commits, ECC 3,931.
  - T09 versions/idioms; T10 match to dataset (anchors 6,719 blobs / 216,440 files; tiers A 140/23,686, B 5,047/155,526, G 1,535/38,631); T11 change classifier; T12 popular families.
  - T24 adoption lag; T25 live sample; T26 patterns P1–P6; T27 NB-GLM/logit/KM; T28 assistance probe (leave-one-repo-out AUC; pilot text 0.52 vs date 0.64).
  — [.cursor/tasks/](.cursor/tasks/T10-match-registry-to-the-dataset-provenance-mirrors.md)
- Docs not yet updated for Sprint 2: THREATS item 5 "We haven't yet checked how often our labels are right." The README says "Wording features and checks of label reliability are planned." — [THREATS_TO_VALIDITY.md](THREATS_TO_VALIDITY.md); [README.md](README.md)

### Inferences
- Gaps in the plan itself:
  - (1) Installer-metadata signals (`_meta.json`, `.clawhub/origin.json`, front-matter source/version) are not used anywhere. They are a cheap addition to `templated_basis`.
  - (2) T13's W9 lexicon depends on a T18 training split, which creates a circular dependency (T13→T17→T18→lexicon→T13). It needs an explicit two-pass plan, or W9 should be dropped.
  - (3) T03's `classify_author` lacks a "distribution bot" class that v1 needs.
  - (4) The v1 trailer semantics mismatch (Q3).
  - (5) No dependency installs or venv are set up locally.
  - (6) T14 MinHash on 1.84M texts in pure-Python `datasketch` will take hours. Start it early or use a stratified fallback.
  - (7) Human annotation (480 judgments plus 240 pair judgments) is the true critical path and cannot be compressed by code.
- Feasible minimum "all code done" scope for RQ1 alone: T00–T05, T13, T14, T16 (optional), T17 (without registry basis, or with T10 exact only), T18, T19–T23, T29, T30–T32. T06–T12 and T24–T28 belong to the provenance/RQ2 extension.

### Gaps
- Whether the GitHub project board (https://github.com/users/TJ690/projects/2) holds backlog items GS-x-y matching the cards was not checked (it is a network resource).

## Q6. Challenge/dataset paper (docs/Git-skills.pdf)

### Takeaway
This is a 3-page MSR '27 data-showcase paper (arXiv:2608.10906v3, 11 Sep 2026; Destefanis, Graziotin, Vaccargiu, Ortu). It describes the collection pipeline and lists 5 research-direction families. "How many skills do agents themselves create or maintain" (1d) and "genealogy patterns of clones" (3c) map directly onto RQ1 and the provenance/RQ2 track.

### Cited Findings
- Pipeline:
  - Discovery: code search for `SKILL.md`, partitioned by file size to beat the 1,000-result cap. The total_count said ~349k; more than 3.8M were retrieved.
  - Dedup: by content hash, representative prefers `.claude/skills/`, deterministic tie-break, "We do not assume that the representative is the original source."
  - Enrichment: content, front matter, folder contents, repo metadata, and SKILL.md commit history for standard locations plus a size-stratified sample.
  — [docs/Git-skills.pdf](docs/Git-skills.pdf)
- Coverage notes:
  - Folder listings missing for 1,544 reps.
  - History missing for 1,166 in standard locations.
  - 42 reps changed between discovery and fetch (`content_sha_ok`).
  - Case variants such as skill.md: 54,289 files (1.4%), including pre-format files.
  - Public, default-branch, under-384 KB, active repos only, so a lower bound.
  — [docs/Git-skills.pdf](docs/Git-skills.pdf)
- Anonymisation: keyed one-way author codes, stable across the dataset; bot logins kept; emails and personal names redacted; AI assistant names in Co-authored-by kept. — [docs/Git-skills.pdf](docs/Git-skills.pdf)
- History table count is 458,548 in the paper (local data: 458,137). — [docs/Git-skills.pdf](docs/Git-skills.pdf)
- Suggested research questions:
  - 1) Adoption and linguistic evolution: (a) spread speed and first adopters; (b) skill-purpose taxonomy, which "does not yet exist"; (c) cohort linguistic change, "Convergence toward formulaic templates would indicate an emerging genre; shrinking diversity may also reflect rising machine authorship"; (d) "How many skills do agents themselves create or maintain", and in which natural languages.
  - 2) Shared format: vendor-neutral vs tool-specific locations; single-tool vs spec-wide text.
  - 3) Reuse without a package manager: concentration; mechanisms (direct addition, catalogs, scaffolding); "genealogy patterns known from code clones, such as consistent and inconsistent propagation of changes."
  - 4) NL software metrics: size, churn, age, clone coverage, readability; quality indicators vs copy count and edits; description properties vs reuse and maintenance.
  - 5) Maintenance and trust: outdated skills; modified copies introducing command execution or network access; bundled executables.
  — [docs/Git-skills.pdf](docs/Git-skills.pdf)
- Ref [4]: G. Destefanis, "Authoring Agent Skills: A Software-Engineering Approach," arXiv:2607.25032 (2026). — [docs/Git-skills.pdf](docs/Git-skills.pdf)
- Dataset README: HF Parquet mirror; Zenodo DOI 10.5281/zenodo.21875637 (SQLite); sample repo giuseppedestefanis/gitskills-sample; licence CC-BY-4.0, with content under the origin repo licence. — [src/data/gitskills_data/README.md](src/data/gitskills_data/README.md)

### Inferences
- The project's RQ is the paper's 1d/1c (machine authorship). The planned RQ2 (staleness, propagation, change classifier with network/command deltas) is the paper's 3c/5a/5b. T11's `content_signals` (commands_added, network_added, scripts_added) directly addresses 5b.

### Gaps
- MSR 2027 Mining Challenge submission rules and deadlines were not looked up. The elicitation notes mention a "Bring Your Own Data" policy, but no primary source was checked here.

## Q7. Git history and team progress

### Takeaway
54 commits from 2026-09-14 to 2026-10-07, all Sprint 1 EDA and documentation. Nothing has been committed since 10-07. The Sprint 2 scaffolding (`.cursor/`, `config/`, `docs/CODING_PLAN.md`, `.env.example`, dated 10-15) is uncommitted.

### Cited Findings
- `git log --oneline` (latest first): b5713af "Add GitHub project board link to README"; f7d5ef7/8451b0b merges; daeac0c "Revise README and documentation … add Sprint 1 progress and retrospective notes …"; f8af046 retrospective; ee37ff0 merge main; meeting-notes updates (fba7f22, c68f4cb, 37a2255); 5c02234 Sprint 1 retrospective; ff6e33a PR #13 merge (sprint1/exploratory-analysis); 7b3d344 PR #31 (readme-status-fix); f0f05e0 initial figures and Sprint 1 report; 4b1b9a2 DATA_DICTIONARY/RESEARCH_QUESTION; then notebook refactors/path fixes (e6dea08, 2492d99 added Git-skills PDF, 5fd13c5, 75438b7, c3b942a, bde8c92, 4744006, 995139b, 04a65d3, 7d245b0, dea87fa, 2ddc272, 6f49c22). — local `git log --oneline -30`
- Branches: local `assignments/requirements-engineering`, `main`, `sprint1/exploratory-analysis` (current). Remotes: origin/main, origin/assignments/requirements-engineering (adds a3921b9 "Update RESEARCH_QUESTION.md with detailed analysis info"), origin/readme-status-fix, origin/sprint1/exploratory-analysis. Remote: github.com/TJ690/SWE-Group-Project--Project-3. — local `git branch -a`
- Commit authors (all branches): venkatrepaka-umich 30, Jayden-Cathey 17, Terry 5, Venkat Repaka 2. — local `git shortlog -sn --all`
- Several "meeting-notes" commits exist, but no meeting-notes file is on this branch (it may be on main or the assignments branch). — local git log

### Inferences
- Sprint 2 should begin by committing the untracked scaffolding on a `sprint2/...` branch so the team shares the config and task cards.

### Gaps
- Commit dates on other branches and the contents of the meeting notes were not inspected.
