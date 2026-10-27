# Data Dictionary

Describes every table, column, and derived label used in this project. Counts are for the full dataset.

## 1. Source dataset

GitSkills, Hugging Face `mvaccargiu/gitskills` (CC-BY-4.0; paper arXiv:2608.10906). Collected July 2026 from public GitHub repositories.

| Table | Rows | One row is |
|---|---:|---|
| `artifacts` | 3,797,117 | one `SKILL.md` occurrence (repository + path) |
| `artifact_siblings` | 7,264,865 | one file or folder stored next to a representative skill |
| `repos` | 282,200 | one repository |
| `mining_runs` | 7 | one collection run (provenance log) |

Distinct contents: 1,877,981 (grouped by `file_sha`). One representative per group is enriched (full text, front matter, siblings, and for part of them commit history). All copies are kept as their own rows.

Local layout (git-ignored, see README "Dataset Acquisition"):

```
src/data/gitskills_data/data/{artifacts,artifact_siblings,repos,mining_runs}/*.parquet   raw
src/data/generated_data/*.parquet                                                         derived
```

### 1.1 `artifacts`

| Column | Type | Meaning |
|---|---|---|
| `repo_full_name` | string | `owner/repo` that holds the file. Joins to `repos.full_name`. |
| `path` | string | Path of the file inside the repository. |
| `filename` | string | Exact basename of the file. |
| `location_class` | string | Where the file sits. Values seen: `canonical`, `skills-dir`, `other` (canonical = standard agent folder such as `.claude/skills/`; the exact rule is defined by the dataset authors). |
| `file_sha` | string | Content hash. Identical text has the same hash. This is the identity of a distinct skill. |
| `discovered_at` | string | When the collector found the file. |
| `content` | string | Full text. Filled only where `dedup_primary = 1` (a few duplicates also carry text; harmless, see `EDA_integrity`). |
| `content_fetched` | int (0/1) | Whether the text was retrieved. |
| `frontmatter_valid` | int (0/1) | YAML front matter parsed. Invalid for 13.4% of distinct skills. |
| `name`, `description` | string | Front-matter fields. `name` is not a unique identity (see `EDA_names`). |
| `body_chars` | int | Characters after the front matter. |
| `history_fetched` | int (0/1) | Commit history retrieved. 1 for 458,548 representatives (24.4%). |
| `composition_fetched` | int (0/1) | Folder contents (siblings) retrieved. |
| `dedup_primary` | int (0/1) | 1 for the one representative of each `file_sha`. Use this to count distinct skills. |
| `first_commit_at`, `last_commit_at` | string | Timestamps of the first and last commit touching the file. Only when `history_fetched = 1`. |
| `commit_count` | int | Commits touching the file. |
| `sibling_count`, `sibling_bytes` | int | Number and total size of files stored next to the skill. |
| `has_scripts`, `has_references` | int (0/1) | Skill bundles scripts / reference material. |
| `content_sha_ok` | int (0/1) | Fetched text matches `file_sha`. |
| `composition_truncated` | int (0/1) | Folder listing was truncated. |
| `first_commit_author`, `last_commit_author` | string | Anonymised author code. Bot accounts keep their login. |
| `first_commit_author_type`, `last_commit_author_type` | string | `User`, `Bot`, `Organization`, or empty. |
| `first_commit_message`, `last_commit_message` | string | Commit message. Emails and personal names are redacted; AI names in `Co-authored-by` trailers are kept. |

Important: history and commit columns exist only for representatives. A skill that was copied has no history on its other copies.

### 1.2 `artifact_siblings`

| Column | Meaning |
|---|---|
| `repo_full_name`, `artifact_path` | The skill the entry belongs to. |
| `entry_name` | Path of the entry relative to the skill folder. |
| `entry_type` | `file` or `dir`. |
| `entry_size`, `entry_sha` | Size in bytes and git hash. |
| `content`, `content_fetched` | Text of files under a size cap. |
| `skipped_reason` | Why a file was not fetched. |

### 1.3 `repos`

| Column | Meaning |
|---|---|
| `full_name`, `owner` | Repository and its owner account. |
| `stars`, `forks` | Counts. 59.3% of repositories have no stars. |
| `is_fork` | 1 if a fork. Only 3 are forks (forks are excluded by collection). |
| `language` | Primary language. |
| `license` | License; 44.4% declare one. |
| `description` | Repository description. |
| `created_at`, `pushed_at` | Creation and last-push dates. |
| `metadata_fetched` | 0 for 515 repositories whose metadata could not be fetched. |

There is no contributor-count column.

### 1.4 `mining_runs`

`run_id`, `artifact_type`, `query`, `started_at`, `finished_at`, `discovered`, `note`.

## 2. Generated tables (`src/data/generated_data/`)

Built by the SQL scripts in `db/`. See the README for run commands.

### 2.1 `skill_features.parquet` (`db/content_and_structure.sql`)

One row per readable distinct skill: `dedup_primary = 1`, body of at least 200 characters, no symlink stub, not a saved redirect page. 1,840,872 rows.

| Column | Meaning |
|---|---|
| `file_sha`, `location_class`, `frontmatter_valid`, `body_chars` | As in `artifacts`. |
| `desc_chars` | Length of the description. |
| `body_words` | Whitespace-separated words in the body. |
| `headings`, `h2` | Markdown headings (any level / level 2). |
| `numbered_items`, `bullet_items` | Lines starting with `1.` / `-`, `*`, `+`. |
| `code_blocks` | Fenced code blocks (fence lines divided by 2). |
| `table_rows`, `links` | Markdown table rows; `[text](url)` links. |
| `has_examples_section` | A heading named examples, usage, or quick start. |
| `has_when_to_use` | A heading named when to use, use when, or trigger(s). |
| `fm_allowed_tools`, `fm_license`, `fm_version_or_metadata` | Front-matter key present. |
| `non_ascii_ratio` | Share of non-ASCII characters. Above 0.2 is treated as "mostly non-English". |

### 2.2 `file_agents.parquet` (`db/agent_ecosystem.sql`)

One row per file (all 3,797,117): `repo_full_name`, `path`, `file_sha`, `location_class`, `agent_folder`.

`agent_folder` is the first dot-folder in the path: Claude Code (`.claude`), Shared (`.agents`, `.agent`), GitHub Copilot (`.github`), Cursor, Codex, opencode, Gemini CLI, Antigravity, Kiro, Windsurf, Trae, Qwen Code, Kilo Code, Continue, Factory, `Other dot-folder`, or `No agent folder`. This is a tool-family indicator from the path, not proof of which tool wrote the file.

### 2.3 `repo_profile.parquet` (`db/repos.sql`)

One row per repository that holds at least one skill file. All `repos` columns plus:

| Column | Meaning |
|---|---|
| `files` | `SKILL.md` files in the repository. |
| `distinct_skills` | Distinct `file_sha` in the repository. |
| `copy_rows` | Files whose text is represented elsewhere (`dedup_primary = 0`). |
| `copy_share` | `copy_rows / files`: share of rows whose content has its representative (`dedup_primary = 1`) in another row. It depends on which copy GitSkills picked as representative (99.3% of multi-copy contents: the first crawled copy), not on who wrote it, so an origin can score 1.0 (`mattpocock/skills`: 18 original skills, 1.000) and a copier 0.0. Kept for continuity; `s2_repo` uses `shared_share` instead (section 2.5). |
| `name_has_skill` | `owner/repo` contains `skill` (note: the owner is included, so `github` matches `hub`). `s2_repo` recomputes all three name flags on the repository name only. |
| `name_is_collection` | Name matches awesome, collection, library, marketplace, registry, or hub. |
| `name_has_agent` | Name matches claude, agent, codex, cursor, copilot, or gemini. |
| `size_tier` | `files` bucket: `1`, `2-5`, `6-20`, `21-100`, `101-1000`, `>1000`. |

### 2.5 `s2_repo.parquet` (`src/skillsig/s2_features/repo_context.py`, in `src/data/derived/`)

One row per repository in `repo_profile`. Copies `owner, stars, language, created_at, pushed_at, files, copy_share, size_tier, is_fork, distinct_skills`, plus:

| Column | Meaning |
|---|---|
| `repo_name` | Part after `owner/`. |
| `name_is_collection` | `repo_name` matches awesome, collection, library, marketplace, registry, or hub (not inside `github`). |
| `name_has_skill`, `name_has_agent` | `repo_name` has `skill`; or claude, agent, codex, cursor, copilot, gemini. |
| `shared_share` | Share of the repository's rows whose `file_sha` also appears in at least one other repository. Does not depend on the representative choice; does not say who copied whom. |
| `copied_in_share` | Share of rows whose `file_sha` is a known registry blob (`s1_registry_matches`) with an origin other than this repository. Covers registry content only. |
| `registry_origin_share` | Share of rows whose `file_sha` is a registry blob that this repository originated. |
| `repo_kind` | First match: `A_collection` (collection name, or `files >= thresholds.collection_files_min`, or `shared_share >= thresholds.collection_copy_share_min`), `B_agent_tooling` (skill or agent word in `repo_name`), else `C_project`. |
| `benchmark_like_name` | `repo_name` has sample, bench, demo, mirror, collection, or `test` at a word start (not `latest`). |
| `age_days_at_snapshot` | Snapshot date minus `created_at`. |

### 2.6 `s2_types.parquet` (`src/skillsig/s2_features/skill_type.py`, in `src/data/derived/`)

One row per distinct skill (`dedup_primary = 1`). Context only; no label reads it.

| Column | Meaning |
|---|---|
| `skill_type` | First matching keyword rule (v1) on name + description; the first 500 body characters when both are empty; else `unclassified` ("not covered by the keyword rules", not "unknown"). |
| `skill_types_all` | Every matching type in rule order (27% of skills match two or more). |
| `type_source` | `front_matter`, `body`, or `none`. |
| `type_version` | `v1`. The v0 rules stay in the code as `RULES_V0`. |

### 2.7 `s2_scaffold.parquet` (`src/skillsig/s2_features/scaffold.py`, in `src/data/derived/`)

One row per distinct readable skill (same filter as `s2_clusters`). Structural templating signals; no label reads it yet. "Owners" are distinct owners of each skill's representative repository, so one author's house style does not count as shared. Built by `python -m skillsig features --only scaffold`.

| Column | Meaning |
|---|---|
| `owner` | Owner of the representative repository (lower case). |
| `placeholder_count` | Unfinished-authoring markers outside code fences: `[TODO …]`/`[TBD …]`/`[FIXME …]`/`[placeholder …]`, line-start `TODO:`, `<placeholder>`, `replace this`, `lorem ipsum`, `your-skill-name`. Runtime parameters such as `{{var}}` or `<skill-name>` are not counted. |
| `placeholder_generator` | `skill-creator` when the text keeps any marker line from anthropics/skills `init_skill.py`; else null. |
| `outline_key`, `outline_len` | Hash of the canonical heading list (level, lower case, no numbering or punctuation, skill name as `<name>`), and its length. Null / 0 when there are no headings. |
| `outline_family_id`, `outline_family_owners` | Near-identical outlines (MinHash LSH candidates, heading-sequence edit similarity ≥ `scaffold.outline_sim_min`) and the owners using the family. |
| `fm_key`, `fm_key_owners` | Ordered front-matter keys (`name\|description\|license`) and owners sharing them. |
| `layout_key`, `layout_owners` | Sorted top-level folder entries (`references/\|scripts/`), `SKILL.md` excluded; null when folder contents were not fetched or empty. |
| `masked_sha`, `masked_owners` | Body hash after masking skill name, repo, URLs, paths and numbers; owners sharing it (renamed copies). |
| `line_count` | Distinct normalised body lines of at least `scaffold.line_min_chars` characters. |
| `shared_line_frac` | Share of those lines held by at least `scaffold.line_owners_min` owners. Null when `line_count = 0`. |
| `customization` | `1 - shared_line_frac`. |
| `scaffold_basis` | First match: `generator`, `placeholder` (≥ `placeholder_min`), `masked_copy` (owners ≥ `templated_copies_min`), `shared_lines` (≥ 3 lines and frac ≥ `shared_frac_min`), `outline` (length ≥ `outline_len_min`, family owners ≥ `owners_min`, not in `outline_stoplist`, and a non-generic front-matter key set or a folder layout shared by ≥ `owners_min` owners); else null. |

`results/s2_scaffold_top_outlines.csv` lists the 200 outlines with the most owners, for filling `scaffold.outline_stoplist`. `python -m skillsig evaluate --only scaffold_check` writes `results/s4_scaffold_{overlap,recall,agent_confound,spot}.csv`.

### 2.8 `s2_commit.parquet` (`src/skillsig/s2_features/commit.py`, in `src/data/derived/`)

One row per distinct skill (`dedup_primary = 1`). Commit fields describe the representative's repository. Every column except `file_sha` and `history_fetched` is null when `history_fetched` is false.

| Column | Meaning |
|---|---|
| `history_fetched` | GitSkills fetched commit history for the representative (`artifacts.history_fetched = 1`). |
| `commit_count`, `first_commit_at`, `last_commit_at` | Copied from `artifacts`. |
| `span_days` | Days from the first to the last commit. |
| `ai_trailer_first`, `ai_trailer_last` | A `Co-authored-by:` line on the first / last commit names an AI tool (Claude/Anthropic, Cursor, Copilot, Codex/OpenAI, Gemini). |
| `ai_tool_first`, `ai_tool_last` | The tools named, comma-separated in a fixed order (`claude,cursor,copilot,codex,gemini`). |
| `ai_trailer_first_ext`, `ai_trailer_last_ext`, `ai_tool_first_ext`, `ai_tool_last_ext` | Extended detector: also counts generator lines such as "Generated with Claude Code" and other coding agents (`other_agent`). |
| `first_author_class` | `user`, `organization`, `coding_agent_bot`, `noncoding_bot` (CI, release, sync bots) or `unknown`. |
| `coding_agent_bot`, `noncoding_bot` | `first_author_class` equals that class. |
| `batch_size` | Representatives with history sharing this skill's repository and `first_commit_at`. |
| `is_bulk` | `batch_size >= thresholds.bulk_batch_min` (6). |
| `msg_class_first` | First-commit message class, trailers removed: `install_sync_import`, `scaffold`, `conventional`, `add_upload`, `short_generic`, `other`. Context only; no label reads it. |

### 2.9 `s2_template.parquet` (`src/skillsig/s2_features/template_sim.py`, in `src/data/derived/`)

One row per distinct readable skill: its nearest registry version.

| Column | Meaning |
|---|---|
| `match_kind` | `exact` (same blob as a registry version), `near` (estimated Jaccard >= `thresholds.templated_jaccard_min`) or `none`. |
| `nearest_blob`, `nearest_lineage_id` | The nearest registry version and its lineage (`s1_registry`). |
| `est_jaccard` | MinHash estimate of 5-word-shingle Jaccard similarity to `nearest_blob`. |

### 2.10 `s2_clusters.parquet` (`src/skillsig/s2_features/minhash_cluster.py`, in `src/data/derived/`)

One row per distinct readable skill per threshold (`minhash.lsh_threshold` 0.8 and `minhash.lsh_threshold_low` 0.6).

| Column | Meaning |
|---|---|
| `threshold` | LSH similarity setting of the row. Labels read only the 0.8 rows. |
| `cluster_id`, `cluster_size` | Connected component of LSH candidate links and its size. Links can chain, so members need not be pairwise similar. |
| `nn_sim_est` | Best estimated similarity to any LSH candidate neighbour; null for a skill with none. |

### 2.4 `sibling_files.parquet` (`db/siblings_data.sql`)

One row per bundled file (`entry_type = 'file'`): `repo_full_name`, `artifact_path`, `entry_name`, `entry_size`, `entry_sha`, `skipped_reason`, plus:

| Column | Meaning |
|---|---|
| `top_folder`, `subfolder` | First path segment; `(skill root)` when the file sits next to `SKILL.md`. |
| `ext` | Lower-case file extension. |
| `file_category` | `script / code`, `documentation`, `data / config`, `media / asset`, `web / template`, `no extension`, or `other`. |

## 3. Derived signals and labels

Defined in `src/notebooks/EDA_authorship.ipynb`. `EDA_content` and `EDA_repos` rebuild the same rules so each notebook runs alone. They are heuristics, not verified authorship.

| Signal | Definition |
|---|---|
| AI trailer | `first_commit_message` has a `Co-authored-by:` line naming Claude, Anthropic, Cursor, Copilot, Codex, OpenAI, or Gemini. |
| Coding-agent bot | First commit authored by a bot account that is a coding agent. CI, release, and distribution bots (for example `github-actions[bot]`) are not an authorship signal. |
| Batch | Representative skills (`dedup_primary = 1`) that share a repository and `first_commit_at`. |
| Bulk install | Batch of 6 or more skills. Trailer and bot author then describe the install, not the writing. |
| Copy count | Number of files sharing the same `file_sha`. |

### Sprint 1 authorship label (first matching rule wins)

The notebook rule. The pipeline's `s3_labels_v1` applies the same rules (same counts); later versions are below.

| Label | Rule | Skills | Share |
|---|---|---:|---:|
| `highly_templated` | `file_sha` occurs in 10 or more files. | 35,768 | 1.9% |
| `agent_assisted` | History fetched, batch under 6, first author not a non-coding bot, and a coding agent authored the first commit or it has an AI trailer. | 80,114 | 4.3% |
| `likely_human` | History fetched, committed alone, first author type `User`, no AI trailer on first or last commit, text occurs in exactly one file. | 62,798 | 3.3% |
| `insufficient_evidence` | Everything else (no history, bulk install not already templated, non-coding bot, batch of 2-5 with no agent signal, organization or unlinked author, copy count 2-9). | 1,699,301 | 90.5% |

Important Notes:

- `likely_human` means "no AI signal found", not confirmed human writing.
- `insufficient_evidence` is a coverage limit, not a fourth kind of author.
- The label is the outcome variable of the research question.
- Commit message class (not an input to the label): install/sync/import, repository scaffold, conventional prefix, add/upload, short generic (12 characters or fewer), other. `Co-authored-by`, `Signed-off-by`, and `Generated-by` lines are removed before classifying.

### Pipeline label tables (`s3_labels_v1`, `s3_labels_v2`, `s3_labels_v2_1`)

Built by `python -m skillsig label --only rules_v1|rules_v2|rules_v2_1` into `src/data/derived/`. One row per distinct skill (1,877,981). `s3_labels_v2_1` is the current label. Rule details: [docs/stages/s3_labeling/](docs/stages/s3_labeling/README.md).

| Column | Table | Meaning |
|---|---|---|
| `label` | all | `highly_templated`, `agent_assisted`, `likely_human` or `insufficient_evidence`. |
| `reasons` | all | Reason codes of the rules that fired (list below). Never empty. |
| `copies` | v1 | Files in `artifacts` sharing this `file_sha`. |
| `templated_basis` | v2, v2.1 | First templated reason that applies: `registry_exact`, `registry_near`, `cluster`, `copy_count`; null when not templated. |
| `confidence` | v2, v2.1 | `high`, `medium` or `low` (heuristic, not a calibrated probability). |
| `v1_label` / `label_v1`, `label_v2` | v2 / v2.1 | Earlier labels, kept for comparison. |
| `flag_templated`, `flag_disclosed_agent` | v2.1 | Templated evidence and disclosed agent involvement, set whatever the label. |
| `revised` | v2.1 | `commit_count >= thresholds.human_min_commits` and `span_days >= thresholds.human_min_span_days`. |
| `human_signal_count` | v2.1 | 0-5: revised, batch of 1, one copy, `user` author, history without disclosure. |
| `conflict` | v2.1 | `flag_templated` and `flag_disclosed_agent` both true. |
| `filter_short`, `filter_case_variant`, `filter_pre_format` | v2.1 | Body under `thresholds.min_body_chars`; file not named exactly `SKILL.md`; first commit before 2025-10-01. Flags only; nothing is dropped. |

**Reason codes and the column each one reads:**

| Reason code | Column(s) | Fires when |
|---|---|---|
| `no_history` | `s2_commit.history_fetched` | No commit history. |
| `copies=N`, `copies>=10` | `s3_labels_v1.copies` | Copy count of the text. |
| `copy_count` | `s3_labels_v1.copies` | `copies >= thresholds.templated_copies_min` (10). |
| `registry_exact`, `registry_near` | `s2_template.match_kind`, `est_jaccard` | Exact or near match to a registry version. |
| `registry_origin` | `s2_template.nearest_lineage_id`, `artifacts.repo_full_name` | The skill is the registry's own file; registry reasons are skipped. |
| `cluster` | `s2_clusters.cluster_size` (threshold 0.8) | Component of `thresholds.templated_copies_min` (10) or more. |
| `ai_trailer_first:<tools>`, `ai_trailer_last:<tools>` | `s2_commit.ai_tool_first`, `ai_tool_last` | AI co-author trailer on the first / last commit. |
| `coding_agent_bot`, `noncoding_bot` | `s2_commit.first_author_class` | First commit by that kind of bot. |
| `author=user`, `author=organization`, `author=unknown` | `s2_commit.first_author_class` | Author class when it explains the label. |
| `batch_size=N`, `bulk_batch>=6` | `s2_commit.batch_size` | Batch size; bulk when at least `thresholds.bulk_batch_min`. |
| `no_ai_trailer`, `no_agent_signal` | `s2_commit.ai_trailer_first`, `ai_trailer_last`, `coding_agent_bot` | No disclosure on the commits read. |
| `trailer_in_bulk`, `trailer_last_only`, `trailer_ext_only`, `trailer_other` | `s2_commit.is_bulk`, `ai_trailer_*`, `ai_trailer_*_ext`, `coding_agent_bot` | v2.1: why v2 left a disclosed skill as insufficient (bulk batch; last commit only; extended detector only; other). |
| `revised`, `not_revised` | `s2_commit.commit_count`, `span_days` | v2.1 human confidence (see `revised`). |
| `conflict:templated+disclosed` | `flag_templated`, `flag_disclosed_agent` | v2.1: templated and disclosed; confidence drops one level. |

### Independent evidence dimensions (`s3_evidence.parquet`)

Built with `python -m skillsig label --only evidence`. This adds an evidence report;
historical labels and independent annotations are preserved. See
[the full schema definitions](docs/stages/s3_labeling/evidence.md).

| Field | Meaning |
|---|---|
| `reuse_signal` | Compact display category: `exact_content_shared`, `near_duplicate_signal`, `scaffold_signal`, `no_reuse_signal_observed`, `reuse_not_assessed`. |
| `flag_exact_content_shared` | Same bytes in at least two dataset files, without claiming copying direction. |
| `flag_registry_exact`, `flag_registry_near` | Registry match signals; known registry origin excluded; near requires measured similarity at the configured cutoff. |
| `flag_near_duplicate_cluster` | At least two members in the primary LSH component; candidate/transitive evidence, not all-pairs verification. |
| `flag_scaffold`, `scaffold_basis` | Existing structural candidate evidence and its basis; manual validation incomplete. |
| `agent_signal` | `disclosed`, `no_disclosure_observed`, or `history_unavailable`. No human-authorship verdict. |
| `disclosure_first`, `disclosure_last`, `coding_agent_bot` | Available disclosure evidence; may concern installation or maintenance. |
| `bulk_commit`, `batch_size` | Context retained without suppressing the observation of disclosure. |
| `reuse_and_disclosure` | Any reuse flag and disclosed involvement co-occur; not a contradiction or confidence penalty. |
| `legacy_label`, `legacy_templated_basis`, `legacy_conflict` | Unchanged historical v2.1 interpretation retained for comparison, not used as new authorship conclusions. |
| `copies`, `cluster_size`, `registry_origin`, `text_assessed`, `history_fetched`, `disclosed_agent` | Supporting counts/coverage/flags. Text not assessed does not mean original; no observed disclosure does not mean human. |

The reuse display category prioritizes exact over near over scaffold for a compact
matrix; underlying flags overlap and must not be summed. Evidence at two copies or
cluster members is intentionally broader than the old ten-copy templated rule.

## 4. Known data caveats

- Commit fields describe the representative's repository, not its copies.
- The dataset holds public repositories only and is a lower bound (GitHub code search limits apply: default branch, files under 384 KB, and others).
- The format launched in October 2025. Commits before 2025-10-01 are files that existed before becoming a `SKILL.md`.
- Author accounts are anonymised with a key that is not distributed.
- `content` text keeps the license of its origin repository. Check `repos.license` before reusing it.
