# Registry tree check

Counts `SKILL.md` files in the repo itself for popular repos the dataset under-counts. This is stage 2b. It uses the network. [Registry selection](03_registry_selection.md) reads the saved table and does not clone anything. [Skill language labeling](01_skill_language_labeling.md) has to have written `skill_language.parquet` first.

## Scripts

Run these from the repo root.

```bash
bash scripts/skill_genealogy/dev_mode/02_registry_tree_check.sh
```

Checks 20 candidates in the terminal. The command is `check_registry_trees --limit 20`. The counts still go into the one table below.

```bash
bash scripts/skill_genealogy/full_run/02_registry_tree_check.sh
```

Checks every unfinished candidate in the background. The log is `logs/check_registry_trees.log`. Follow it with `tail -f logs/check_registry_trees.log`.

```bash
bash scripts/skill_genealogy/full_run/02_registry_tree_check_retry.sh
```

Runs `check_registry_trees --retry-failed`.

```bash
bash scripts/skill_genealogy/tests/02_registry_tree_check.sh
```

Runs the tests.

## Inputs

- `src/data/gitskills_data/data/artifacts/*.parquet` and `repos/*.parquet`
- `src/data/skill_genealogy/skill_language.parquet` from stage 2a
- `src/data/skill_genealogy/registry_tree_counts.parquet`, when a previous run left one

The English-skill count is query Q2.4, the same count stage 2c uses.

## Steps

**Pick candidates.** The step counts each repo’s English skills in the dataset. It keeps repos that:

- are not forks,
- have 100+ stars, and
- have 1–4 English skills in the dataset.

Repos with 5 or more already pass the registry rule, and repos with 0 are not checked. A missing star count is not a candidate. Candidates run in order of stars, most first. Ties on stars go by repo name, so `--limit` always takes the same repos.

**Clone just the newest commit.** For each candidate, git copies only the newest commit of the default branch (`--depth 1`, `--single-branch`, `--filter=blob:none`, `--no-checkout`). It brings the folder and file listing, but no file contents and no history, so it’s quick and small.

**Count the skills.** It lists the repo’s files with `git ls-tree` and counts the paths whose file name is `SKILL.md` in any letter case. Paths inside `node_modules/`, `.venv/`, `venv/`, `__pycache__/`, `dist/`, `build/`, and `.git/` are left out. It never opens the files, so this count includes skills in any language.

**Record the result.** It saves:

- `repo`: `owner/repo`
- `status`: `ok`, `gone` (deleted or private), `empty`, `timeout`, or `error`
- `message`: the reason when a check fails
- `head_commit`: the commit it read
- `tree_skill_md`: the `SKILL.md` count (`ok`), 0 (`empty`), or null when the check failed
- `seconds`: how long the check took
- `checked_at`: when it ran

Then it deletes the clone.

**Save as it goes.** It writes `registry_tree_counts.parquet` every 200 repos and checks 8 repos at a time, giving each git command up to 600 seconds. The printout says how many candidates have 5 or more `SKILL.md` files. Turning those into registries is stage 2c.

## Re-running

- A plain re-run skips repos marked `ok`, `gone`, or `empty`.
- `--retry-failed` checks the `timeout` and `error` repos again.
- `--force` checks every candidate again.

## Output

`src/data/skill_genealogy/registry_tree_counts.parquet` — one row per checked repo, with the fields listed under **Record the result**.

The queries that read this table are in `db/genealogy/registry_tree_counts.sql`. Run `db/genealogy/views.sql` first.
