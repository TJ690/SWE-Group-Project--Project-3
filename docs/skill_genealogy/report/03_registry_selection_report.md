# Registry selection report

Results of registry selection, read from `registries.parquet`, `repo_roles.parquet`, and `dev_registries.parquet` on 2026-11-06. How the step runs is in [registry selection](../code_flow/03_registry_selection.md). The queries are in `db/genealogy/registry_selection.sql`.

The step does not use the network. It reads the tree counts from `registry_tree_counts.parquet`, which the [tree check report](02_registry_tree_check_report.md) describes. A repository is a registry when it is not a fork, has at least 100 stars, and either has 5 or more English skills in the dataset or was promoted by the tree check.

## Registries

The file holds **4,806** registries. The dataset rule alone, from the language report, is 3,566. The tree check adds **1,240**.

| How it passed | Registries | Median stars | Median English skills in the dataset | Median `SKILL.md` files in the repo |
|---|---:|---:|---:|---:|
| dataset | 3,566 | 434 | 12 | — |
| repo_tree | 1,240 | 438 | 3 | 8 |

The two groups have about the same number of stars. A tree promotion has only 1–4 English skills in the dataset, which is why its median there is 3, and a median of 8 `SKILL.md` files in the repo itself.

The largest tree promotions by stars are `obra/superpowers` (255,386 stars, 1 dataset skill, 15 files in the repo) and `anthropics/skills` (163,055 stars, 3 dataset skills, 20 files in the repo). The largest registry overall is `openclaw/openclaw`, which passed on the dataset count (383,040 stars, 44 English skills).

## Roles

Every repository in the dataset has one role. The hub flag is a preview from the dates already in the dataset. A hub needs a `hub_share` of at least 0.25 and at least 5 dated English texts. Only repositories with at least 5 English skills are scored, so a tree-promoted registry is not scored here.

| Role | Repositories | Rule |
|---|---:|---|
| repository | 277,389 | Not a registry and not a hub |
| registry | 4,806 | Passes the registry rule and is not a hub |
| hub | 5 | A hub that does not pass the registry rule |
| registry_hub | 0 | Passes the registry rule and is a hub |

The five hubs each have fewer than 100 stars, which is why they are not registries.

| Repository | Stars | English skills | `hub_share` |
|---|---:|---:|---:|
| aydabd/cacad | 0 | 39 | 0.972 |
| PrimeBrains/moira | 0 | 5 | 0.600 |
| santapong/Cooker | 0 | 8 | 0.375 |
| outwebfeng/text-format | 4 | 9 | 0.286 |
| franreke05/RequenaDesk | 0 | 17 | 0.250 |

Of the English texts in registries, **135,825 of 153,761 (88.3%)** have no `first_commit_at`. Those texts stay out of this hub test. `discovered_at` is not used.

## Fixed sample

`dev_registries.parquet` holds 30 registries, 10 from each size group. The groups are cut by English-skill count across all 4,806 registries. A count on a boundary can fall in either group, because ties are split by repo name. The same seed returns this list. The draw asks for at least 3 hubs. No registry is a hub, and the five hubs above are not registries, so no swap was possible. The sample has **0** hubs.

| Size group | Registries in the group | English skills in the group | Drawn | English skills in the sample |
|---|---:|---|---:|---|
| small | 1,602 | 1–5 | 10 | 2–5 |
| medium | 1,602 | 5–13 | 10 | 5–12 |
| large | 1,602 | 13–13,799 | 10 | 13–13,799 |
