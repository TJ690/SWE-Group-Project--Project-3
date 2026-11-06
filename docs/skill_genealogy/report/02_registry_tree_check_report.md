# Registry tree check report

Results of the tree check, read from `src/data/skill_genealogy/registry_tree_counts.parquet` on 2026-11-06. How the step runs is in [registry tree check](../code_flow/02_registry_tree_check.md). The queries are in `db/genealogy/registry_tree_counts.sql`.

One row is one candidate repository. A candidate is a non-fork with at least 100 stars and 1–4 English skills in the dataset. The check clones only the newest commit and counts `SKILL.md` files. It does not open the files, so the count includes every language.

## What was checked

The file holds **6,834** candidates. Every candidate has a row. None are still waiting.

| Status | Repos | What it means |
|---|---:|---|
| ok | 6,777 | The `SKILL.md` count is filled in |
| gone | 57 | The repository is deleted or private |
| empty | 0 | The default branch has no commit |
| timeout | 0 | The git command ran out of time |
| error | 0 | The clone failed for another reason |

A check took 0.44 seconds at the median. The longest took 9.7 seconds. Added together, the checks took 56 minutes of git time. Eight repositories were checked at once, so the wall-clock time was shorter.

## How many skills the repos actually hold

Of the 6,777 repositories that answered, **1,240** have 5 or more `SKILL.md` files. Stage 2c promotes those. **5,537** have fewer than 5, so they stay out.

| `SKILL.md` files | Repos |
|---|---:|
| 0–4 | 5,537 |
| 5–19 | 1,076 |
| 20–49 | 135 |
| 50 or more | 29 |

The two largest trees have 246 files each: `Snowflake-Labs/cocoplus` and `makenotion/notion-cookbook`. Next are `ai4nucleome/BioMaster` (189), `xu-xiang/everything-claude-code-zh` (185), and `zhou210712/claude-for-legal-ZH` (150).

Two widely copied repositories the dataset under-counted were promoted. `obra/superpowers` has 1 English skill in the dataset and 15 `SKILL.md` files in the repo. `anthropics/skills` has 3 in the dataset and 20 in the repo.

## Stars

Most candidates are in the 100–999 star band. The share that reaches 5 `SKILL.md` files rises with stars: 16.7% of the 100–999 band, 20.5% of the 1,000–9,999 band, and 27.9% of the 10,000 or more band.

| Stars | Candidates | 5 or more `SKILL.md` files |
|---|---:|---:|
| 100–999 | 4,952 | 827 |
| 1,000–9,999 | 1,517 | 311 |
| 10,000 or more | 365 | 102 |
