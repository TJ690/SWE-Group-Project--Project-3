# Registry READMEs

Downloads the README of every registry. This is stage 3b. It uses the network. [Registry history](04_registry_history.md) has to have run first, because this step reads the commit that stage 3 mined. Card G37.

A README often says what a repo is. `majiayu000/claude-skill-registry` says "This is an aggregate catalog, not a single plugin or a first-party skill pack" and "The maintainer of this mirror is not necessarily the author of a listed skill." Stage 3 keeps only skill files, so it has no README. A later step reads the README text next to the stage 3 history to tell a repo that copies skills from one that writes them.

## Scripts

Run these from the repo root.

```bash
bash scripts/skill_genealogy/dev_mode/05_registry_readmes.sh
```

Fetches the READMEs of the 30 registries in `dev_registries.parquet` and stays in the terminal. The command is `download_registry_readmes --dev`. The table goes to `src/data/skill_genealogy_dev/registry_readmes.parquet`.

```bash
bash scripts/skill_genealogy/full_run/05_registry_readmes.sh
```

Fetches every registry in `registries.parquet` in the background. The log is `logs/download_registry_readmes.log`. The table goes to `src/data/skill_genealogy/registry_readmes.parquet`.

```bash
bash scripts/skill_genealogy/full_run/05_registry_readmes_retry.sh
```

Runs `download_registry_readmes --retry-failed`.

```bash
bash scripts/skill_genealogy/tests/05_registry_readmes.sh
```

Runs the tests.

## Inputs

- `src/data/skill_genealogy/registries.parquet` (or `dev_registries.parquet` with `--dev`), the same list stage 3 reads
- `registry_history/registry_history_status.parquet` from stage 3, in the dev or full data folder. Only `repo` and `head_commit` are read. A missing file stops the run and names `python -m skill_genealogy.download_registry_history`.

`--limit N` fetches at most N registries. `--workers` sets how many run at once (default 8). `--timeout` sets the seconds for each git command (default 600).

## Steps

**Read the registry list and the stage 3 commits.** Each registry gets the `head_commit` its stage 3 history was mined at, or none when stage 3 has no commit for it (`gone`, `timeout`, `error`, or a repo added to the list after stage 3 ran).

**Skip finished repos.** A row whose status is `ok`, `no_readme`, `gone` or `empty` is finished. `timeout` and `error` are fetched again only with `--retry-failed`. `--force` fetches every registry again.

**Fetch one commit, without file contents.** This is the stage 3 method on a small scale. With a stage 3 commit, git starts an empty repo and fetches only that commit and its trees (`git fetch --depth 1 --filter=blob:none origin <commit>`, GIT10). This reads the same snapshot as the history, not the repo as it is today. If that commit cannot be fetched, for example after a force push, the newest commit is cloned instead (`git clone --depth 1 --filter=blob:none`, GIT9). The newest commit is also used when stage 3 has no commit. `commit_basis` says which one was used: `stage3_head` or `current_head`. Eight repos run at a time.

**Find the README.** One `git ls-tree` lists the files directly in `.github/`, the repo root and `docs/` (GIT11). GitHub shows the first README it finds in that folder order, so this step does too. Inside a folder, the names are tried in this order, in any letter case: `readme.md`, `readme.markdown`, `readme.mdx`, `readme.rst`, `readme.txt`, `readme.org`, `readme.adoc`, `readme`. A link or a submodule is not a README. A repo with none is `no_readme`.

**Fetch only the README.** The README blob is fetched by its id, the same call stage 3 uses for `SKILL.md` (GIT8). A README larger than 200,000 bytes keeps its row and path, but not its text, and the message says so. The text is stored raw. The clone is then deleted.

**Save.** The table is written every 200 repos with a `progress readmes i/N` line, and once at the end. A new row replaces an older row for the same repo.

## Re-running

- A plain re-run skips finished rows and fetches the rest.
- `--retry-failed` fetches `timeout` and `error` rows again.
- `--force` fetches every registry again.

## Output

`registry_readmes.parquet`, one row per registry:

- `repo`: `owner/repo`
- `status`: `ok`, `no_readme`, `gone`, `empty`, `timeout` or `error`
- `message`: the reason when a fetch fails, a note when the newest commit was used, or a note when the README was too large
- `commit`: the commit the README was read from
- `commit_basis`: `stage3_head` (the stage 3 commit) or `current_head` (the newest commit)
- `readme_path`, `readme_sha`: the README file and its blob id
- `readme_bytes`, `readme_text`: its size in UTF-8 bytes and its raw text, or null when it was too large
- `seconds`, `fetched_at`: how long the fetch took, and when it ran (UTC)

The run prints the counts by status and by commit basis, how many texts were kept, the median README size, and two sample rows.

## Dev check

On 2026-11-07, `--dev --limit 5` fetched 5 READMEs in 1.0 second. 3 used the stage 3 commit. 2 used the newest commit because the dev stage 3 status had no row for them. The median README was 14,376 bytes.
