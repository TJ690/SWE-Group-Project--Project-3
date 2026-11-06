# Registry selection

Chooses the repositories treated as skill sources, gives every dataset repository one role, and draws a fixed sample of 30 registries. This is stage 2c. It does not use the network. It reads the counts from [registry tree check](02_registry_tree_check.md) when that file exists. [Skill language labeling](01_skill_language_labeling.md) has to have written `skill_language.parquet` first. This step leaves that file as it is.

## Scripts

Run these from the repo root.

```bash
bash scripts/skill_genealogy/dev_mode/03_registry_selection.sh
```

Rebuilds the three tables in the terminal. The command is `select_registries --dev --force`. `--dev` is accepted, and the tables still go to `src/data/skill_genealogy/`.

```bash
bash scripts/skill_genealogy/full_run/03_registry_selection.sh
```

Runs the same rebuild in the background, without `--dev`. The log is `logs/select_registries.log`.

## Tests

```bash
bash scripts/skill_genealogy/tests/03_registry_selection.sh
```

## Inputs

- `src/data/gitskills_data/data/artifacts/*.parquet` and `repos/*.parquet`
- `src/data/skill_genealogy/skill_language.parquet` from stage 2a. `en` and `unknown` count as English
- `src/data/skill_genealogy/registry_tree_counts.parquet` from stage 2b, when it exists

A missing tree-count file is allowed. Every repo is then judged on its dataset count only.

## Steps

**Count English skills.** The step counts each repo’s English skills in the dataset (query Q2.4, the same count stage 2b uses). `english_skills` is the number of distinct English texts. `files` counts English rows, so one text stored at two paths counts as two files. A repo with no English skill is kept, with both counts at 0.

**Attach the tree counts.** For a candidate whose stage 2b row is `ok`, it copies `tree_skill_md` onto that repo. Every other repo gets a null count: not checked, `gone`, `empty`, `timeout`, or `error`. Those repos are judged on the dataset count only.

**Apply the registry rule.** A repository is a registry when it is not a fork, has 100+ stars, and passes in one of two ways:

- `dataset`: 5 or more English skills in the dataset
- `repo_tree`: 1–4 English skills in the dataset, and 5 or more `SKILL.md` files in its own tree

Forks and repos with a missing star count are never registries. `registry_basis` is `dataset` when the dataset rule passes, and `repo_tree` when only the tree count passes.

**Test hubs.** The hub flag is separate from the registry rule, and it uses the dates already in the dataset. Only repositories with at least 5 English skills are scored. A tree-promoted registry has 1–4 English skills in the dataset, so this step does not score it.

`hub_share` is the share of a repository’s dated English texts that were first seen in an older repository:

```text
hub_share = texts first seen elsewhere / dated English texts in this repo
```

Only English texts are used. A text with no usable `first_commit_at` is left out of both counts. For each text, the earliest `first_commit_at` across repositories wins. The same time goes to the smaller repo name. The text counts as first seen elsewhere only when that winner is a different repository and its time is strictly earlier. A tie does not count. If this repository is the earliest, the text does not count either.

`is_hub` is true when `hub_share` is at least 0.25 and at least 5 texts had a usable date. A share of 1.0 on a single dated text is not a hub. A repository that was not scored gets a null share, `hub_tested` false, and `is_hub` false.

This stage uses the share in two places. It sets `is_hub`, which the next step turns into `repo_role`. The fixed sample then uses that flag: when the draw has fewer than 3 hubs, a non-hub is swapped for a hub in the same size group. The number is stored on `registries.parquet` and `repo_roles.parquet`. No later module reads it. The planned step G13 recomputes the hub tag from downloaded commit dates.

**Assign a role.** Every repository gets one role from those two flags:

| `is_registry` | `is_hub` | `repo_role` |
|---|---|---|
| yes | no | `registry` |
| yes | yes | `registry_hub` |
| no | yes | `hub` |
| no | no | `repository` |

A tree-promoted registry is not hub-tested here, so its role is `registry`. On the 2026-11-06 tables that is 4,806 `registry` rows, 5 `hub` rows (each has 5 or more English skills and fewer than 100 stars), and no `registry_hub`. These hub columns are a preview. A later stage recomputes the hub tag from downloaded commit dates.

The registry list and the registry rows inside the role table must name the same repos. A mismatch stops the run before anything is written.

**Draw a fixed sample.** Registries are sorted by English-skill count, then repo name, and cut into three equal groups: `small`, `medium`, and `large`. The draw takes 30 repos, 10 from each group, with seed `20261015`. When the draw has fewer than 3 hubs, non-hubs are swapped for hubs in the same group. The same seed returns the same list. The rows are written to `dev_registries.parquet`.

## Re-running

- A plain run, when the three output files already exist, prints the saved summary and skips the work.
- `--force` rebuilds the three files and keeps `skill_language.parquet`.

Run stage 2b first when the tree promotions should be included, then run `03_registry_selection.sh` again.

## Outputs

All three files are under `src/data/skill_genealogy/`.

`registries.parquet` — one row per registry:

- `repo`, `owner`, `stars`, `english_skills`, `files`
- `hub_share`, `is_hub`, `hub_tested`
- `tree_skill_md`, `registry_basis` (`dataset` or `repo_tree`)

`repo_roles.parquet` — one row per repository:

- `repo`, `owner`, `stars`, `is_fork`, `english_skills`
- `is_registry`, `is_hub`, `hub_tested`, `hub_share`, `repo_role`
- `tree_skill_md`, `registry_basis` (null when the repo is not a registry)

`dev_registries.parquet` — the 30-registry sample:

- `repo`, `english_skills`, `is_hub`, `size_group`

The run prints how many registries passed by `dataset` and by `repo_tree`, the hub counts, the stage 2b status counts, and two sample rows from each table.

The queries that read these three tables are in `db/genealogy/registry_selection.sql`. Run `db/genealogy/views.sql` first.
