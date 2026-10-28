# 1.1 Mine registry history (`mine_git.py`)

```bash
SKILLSIG_NO_FETCH=1 python -m skillsig registry --only mine_git
```

[Back to s1](README.md)

## What it does

For each of the 30 registry clones, records every commit that added or changed a `SKILL.md`, and which AI tools each commit names as co-author.

## How it works

**Clones.** A missing clone is created with `git clone --filter=blob:none --no-checkout`: full history, no file contents. An existing clone is reused as it is (see [Fetching](#fetching)).

**Versions.** One `git log` per clone:

```
git log --no-renames --raw --no-abbrev --format=@@%H|%ad|%s --date=short -- *SKILL.md
```

Each (commit, path) becomes a row; deleted files are skipped. The **skill name** is the parent folder (`skills/pdf/SKILL.md` → `pdf`; a root `SKILL.md` takes the repository name). A **release label** is read from subjects like `Release v1.2.0`.

**AI trailers.** A second `git log` lists each commit's `Co-authored-by` values. Each value is checked with the same detector as step 2.2: `claude`/`anthropic`, `cursor`, `copilot`, `codex`/`openai`, `gemini` (case-insensitive). `ai_tools` lists the tools found; `has_ai_trailer` says whether there were any.

**Mirrors** are not cloned; the origin's rows are copied under the mirror's name.

`--only mine` also works: `mine.py` is an alias of this module.

## Outputs

| File | Rows | One row per | Columns |
|---|---|---|---|
| `s1_versions_raw.parquet` | 16,485 | Commit × `SKILL.md` path | `repo, commit, commit_date, subject, path, skill, blob, status, release_label` |
| `s1_commit_trailers.parquet` | 50,941 | Commit | `repo, commit, ai_tools, has_ai_trailer` |

## Fetching

The step's manifest checks only `config/registry_sources.yaml`, not the clones:

| Situation | What happens |
|---|---|
| YAML unchanged, no `--force` | Skipped; clones untouched |
| YAML changed or `--force`, with `SKILLSIG_NO_FETCH=1` | Rebuilt from the clones as they are |
| YAML changed or `--force`, without it | Every clone is **fetched from GitHub first** |

Fetching adds new registry versions and so changes matches and templated labels downstream. Keep `SKILLSIG_NO_FETCH=1` unless refreshing is deliberate.

## Design decisions

- **Local git, not the GitHub API:** one `git log` returns a whole history in seconds with no rate limit. (`github_client.py`, a cached API client, exists but no stage uses it yet.)
- **No file contents in the clones:** this step needs history only; the 30 clones take 271 MB.
- **`--no-renames`:** git's rename guessing can link unrelated files. Without it, a moved file starts a new history; histories may split but never join the wrong files.
- **Same trailer detector as 2.2:** step 4.1 compares the two views of history, so they must detect trailers the same way.

## Limits

- Only `Co-authored-by` lines and five tools are read. "Generated with Claude Code" footers and tools like Devin or Aider are missed (step 2.2's extended detector covers them; s1 does not use it).
- Substring matching: a person named "Cursor…" or an `@openai.com` address counts as AI.
- Git reads trailers only from the last paragraph of a message; step 2.2 reads any line, so the two can differ slightly.

## Tests

`test_sources.py` (8): the YAML loads 31 mined sources; bad entries raise. `test_mine_git.py` (12): added and changed rows, release labels, root and non-ASCII paths, Claude co-author detected and a human not, mirrors copied, skip when unchanged, `SKILLSIG_NO_FETCH` respected. `test_github_client.py` (9) covers the unused API client.
