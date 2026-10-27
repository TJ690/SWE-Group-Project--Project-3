# 2.3 Repository context (`repo_context.py`)

**Command:** `python -m skillsig features --only repo_context` · **Tests:** `tests/s2_features/test_repo_context.py` · [Back to s2](README.md)

## What it does

One row per repository: size, how much of it is shared or copied from a registry, age at the snapshot, name flags, and a **kind**: collection, agent tooling, or ordinary project.

## How it works

Starts from `generated_data/repo_profile.parquet` (P.1) and adds, in one DuckDB query:

| Column | How |
|---|---|
| `shared_share` | Share of the repository's files whose `file_sha` is held by more than one repository |
| `copied_in_share` | Share whose registry origin (1.3) is another repository |
| `registry_origin_share` | Share whose registry origin is this repository |
| `age_days_at_snapshot` | Days from `created_at` to the snapshot date (2026-07-20) |
| `name_is_collection` | Name (after `owner/`) matches `awesome`, `collection`, `library`, `marketplace`, `registry` or `hub` (but not `github`) |
| `name_has_skill`, `name_has_agent` | Name contains `skill`; or `claude`, `agent`, `codex`, `cursor`, `copilot`, `gemini` |
| `benchmark_like_name` | Name contains `sample`, `bench`, `test` (at a word start), `demo`, `mirror` or `collection` |

**Kind** (`repo_kind`), first match wins:

1. `A_collection`: collection name, **or** `files ≥ 101` (`thresholds.collection_files_min`), **or** `shared_share ≥ 0.5` (`thresholds.collection_copy_share_min`).
2. `B_agent_tooling`: name has a skill or agent word.
3. `C_project`: everything else, an ordinary codebase that happens to contain skills.

## Output

`src/data/derived/s2_repo.parquet`: 282,200 rows, columns `repo_full_name, owner, stars, language, created_at, pushed_at, files, copy_share, size_tier, repo_kind, benchmark_like_name, age_days_at_snapshot, repo_name, is_fork, distinct_skills, name_is_collection, name_has_skill, name_has_agent, shared_share, copied_in_share, registry_origin_share`.

| Kind | Repositories | `SKILL.md` files |
|---|---|---|
| `A_collection` | 82,923 (29%) | 2,802,429 (74%) |
| `B_agent_tooling` | 50,313 (18%) | 295,013 (8%) |
| `C_project` | 148,964 (53%) | 699,675 (18%) |

Most repositories are ordinary projects, but most files sit in collections.

## Design decisions

- **Why classify the repository?** A skill inside a 500-skill "awesome" list and a skill inside a web app are different situations: the first is likely gathered, the second likely written for the project. RQ2 compares skill types and labels by kind.
- **Why one kind per repository, first match wins?** Clear, countable categories; collection evidence is the strongest, so it is checked first.
- **Why 101 files and 50% shared?** A repository with more than 100 skills or mostly shared files behaves like a collection whatever its name. Both are in config and tested as parameters.
- **Why look at the name without the owner?** An owner called `awesome-dev` says nothing about one of their repositories.
- **Why age to the snapshot, not today?** Every date calculation shares one "now" (s0).

## Full data vs dev sample

Dev mode keeps **every repository**: size and shares describe the whole repository, and the sample is file-level. Values are the same as in a full run.

## Tests (15)

- Name helpers drop the owner; `hub` inside `github` is not a collection word; benchmark-like names (`malskillbench` yes; `test` only at a word start).
- `shared_share` 0.7 → collection; a one-file personal Python repo → project; kind priority; cutoffs come from config.
- A run writes kinds and shares; missing inputs (`repo_profile`, `artifacts`, s1 matches, `mining_runs`) each name their command.

## Limits

- Name words are English and fixed; `hub` and `library` also match ordinary projects.
- Not read by any label rule; it is context for analysis and the spot-check sheets.
