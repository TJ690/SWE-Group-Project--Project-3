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
| `copy_share` | `copy_rows / files`. Near 1: installer, mirror, or aggregator. Near 0: origin or unique collection. |
| `name_has_skill` | Repository name contains `skill`. |
| `name_is_collection` | Name matches awesome, collection, library, marketplace, registry, or hub. |
| `name_has_agent` | Name matches claude, agent, codex, cursor, copilot, or gemini. |
| `size_tier` | `files` bucket: `1`, `2-5`, `6-20`, `21-100`, `101-1000`, `>1000`. |

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

### Authorship label (first matching rule wins)

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

## 4. Known data caveats

- Commit fields describe the representative's repository, not its copies.
- The dataset holds public repositories only and is a lower bound (GitHub code search limits apply: default branch, files under 384 KB, and others).
- The format launched in October 2025. Commits before 2025-10-01 are files that existed before becoming a `SKILL.md`.
- Author accounts are anonymised with a key that is not distributed.
- `content` text keeps the license of its origin repository. Check `repos.license` before reusing it.
