# Registry history report

Results of the registry history download, read from `src/data/skill_genealogy/registry_history/` on 2026-11-07. How the step runs is in [registry history](../code_flow/04_registry_history.md). The queries are in `db/genealogy/registry_history.sql`.

The download covers all **4,806** registries: the **3,566** that passed on the dataset count and the **1,240** promoted by the tree check. Each repository is cloned without file contents. The run keeps every file change in each skill folder, and the text of `SKILL.md` and of small scripts.

## How the tables were built

The tables are the result of several runs. Each run added to the shards of the one before, and the last merge joined them.

| Date | Run | What it added |
|---|---|---|
| 2026-11-02 | First full run, in the Project-3 checkout | The 3,566 dataset registries |
| 2026-11-06 | Plain run | The 1,240 tree-promoted registries |
| 2026-11-06 | `--retry-failed`, twice | 157 repos that failed during a network outage, and most earlier errors |
| 2026-11-06 | `--retry-failed --since 2025-09-01` | `majiayu000/claude-skill-registry`, after the large-registry fix (bug 3) |
| 2026-11-07 | `--deleted-folders`, then a local repair | Skills deleted before the download, and the earlier history of moved skills (bug 2), in 1,009 registries |

Every shard has a status file, and no shard changed after the last merge.

## What finished

Every registry has a status row.

| Status | Dataset basis | Tree basis | What it means |
|---|---:|---:|---|
| ok | 3,531 | 1,240 | Skill-folder history was saved |
| gone | 33 | 0 | Git reports the repository was not found |
| timeout | 1 | 0 | A git command ran longer than 600 seconds |
| error | 1 | 0 | The mine failed for another reason |

The timeout is `ClickHouse/ClickHouse`, also with `--since 2025-09-01`. The error is `Expensify/App`: `fatal: error in object: unshallow`, from the shallow clone. Together they hold 27 English dataset skills. A finished mine took 9.8 seconds at the median. The longest took 8,104 seconds (`chromium/chromium`).

**16** `ok` registries have no rows. Their skills were deleted in a way git does not show.

## Skill folders

The 4,771 finished registries hold **609,442** skill folders and **7,926,625** file changes.

| Skill folders | Repos | Folders | Changes |
|---|---:|---:|---:|
| 0 | 16 | 0 | 0 |
| 1–4 | 9 | 22 | 33,558 |
| 5–19 | 2,864 | 28,351 | 896,406 |
| 20–49 | 1,130 | 34,590 | 852,418 |
| 50–199 | 578 | 53,355 | 1,810,282 |
| 200–1,000 | 139 | 55,420 | 890,251 |
| over 1,000 | 35 | 437,704 | 3,443,710 |

Rename following is on for the 4,628 repositories with 200 skill folders or fewer. It is off for the 143 above that line. Those 143 hold 483,200 folders (79%) and 4,050,406 changes.

The largest are `majiayu000/claude-skill-registry` (312,153 folders), `Klotzkette/claude-fuer-deutsches-recht` (23,399), `lingxling/awesome-skills-cn` (9,687), `sickn33/agentic-awesome-skills` (9,044), and `ECNU-ICALK/AutoSkill` (8,195).

**One registry is 51% of all folders.** `majiayu000/claude-skill-registry` is a crawler mirror, not a registry of authored skills. Its README says "This is an aggregate catalog, not a single plugin or a first-party skill pack" and "The maintainer of this mirror is not necessarily the author of a listed skill." 98.8% of its `SKILL.md` history comes from bulk commits such as `Discover 34337 new skills`. On 2026-10-08 it deleted all 312,160 `SKILL.md` files, and on 2026-10-21 it added them back as pointer pages ("It is not an installable skill"). Its commit dates are crawl dates. Stage 3c labels it a copier.

## Deleted and moved skills

Stage 3 first chose skill folders from the newest commit only, so a skill whose folder was gone by the download had no history (bug 2). The fix reads every `SKILL.md` path in the history and logs each history-only folder by its exact path. Each one is then one of three things:

| History-only folders | Folders | Registries | What happens |
|---|---:|---:|---|
| Deleted: the delete is visible and the text lives nowhere else | **35,208** | 929 | Kept as its own skill, ending with the delete |
| Moved: a `SKILL.md` version also lives in a folder that still exists | **38,285** | 487 | Kept as `former_folder` history of that folder |
| No visible delete, or newer than the snapshot | 7,633 | | Not kept |

A first cleanup dropped the moved folders. That removed the earliest history of moved skills: `gooseworks-ai/goose-skills` then seemed to get its own texts after a crawler had copied them. The moved folders are now attached to the folder the skill moved to, and the delete plus add of a move is one rename row, as `--follow` shows it.

Of the English `SKILL.md` folders the dataset saw in finished registries, **185,901 of 187,786 (99.0%)** now have history, against 88.6% before the fix. `chromium/chromium` (timeout), `shobcoder/shob` (deleted from GitHub) and `majiayu000/claude-skill-registry` (old layouts, not deletions) were not checked.

## What the changes are

A change row is one file in one commit. `history_role` says how that file belongs to the skill.

| Role | Rows | Share | Meaning |
|---|---:|---:|---|
| own | 7,399,530 | 93.4% | The skill folder itself |
| former_folder | 422,790 | 5.3% | A folder this skill used to be in |
| copy_source | 104,305 | 1.3% | The folder this skill was copied from, before the copy |

Following a rename also sees other folders. Those rows are dropped and counted on the status row: **65,896** `not_ours`, **109,081** `after_copy`, and **2** `twin`.

| Git status | Rows | Meaning |
|---|---:|---|
| M | 3,409,319 | The file was modified |
| A | 2,984,051 | The file was added |
| D | 1,220,712 | The file was deleted |
| R | 311,961 | The file was renamed |
| T | 582 | The file type changed |

`SKILL.md` is 2,570,814 of the rows and scripts are 859,398. The two groups overlap by 2,349 rows: a `SKILL.md` with mode `100755` also counts as a script. The other 4,498,762 rows are the remaining files in the skill folders.

## Versions of SKILL.md

**609,410** skill folders have at least one `SKILL.md` change. The median folder has 3 such commits. The 90th percentile has 6. **104,178** folders (17.1%) have one `SKILL.md` commit, and **131,535** (21.6%) have 5 or more. Bulk commits in large registries count as one commit for each folder they touch.

The commit dates run from 2016-02-26 to 2026-11-07. The earliest dates belong to files that were in a folder before it became a skill. The folder with the most `SKILL.md` commits is `kortix-ai/suna` `.claude/skills/learnings` (358). Next are `TencentCloudBase/CloudBase-MCP` `config/source/skills` (354) and `mvanhorn/cli-printing-press` `skills/printing-press` (310).

## Texts

`registry_history_texts.parquet` holds **1,992,550** blobs. The text is 20.0 GB, stored in a 5.51 GB file. The median text is 5,116 characters. The longest is 1,025,262 characters.

Every `SKILL.md` blob is stored: 1,448,364 blobs, before and after each change. Of 546,683 script blobs, 537,708 are stored. The other 8,975 scripts have no text, because a script larger than 200,000 bytes is left out. 6,487 stored blobs are neither a `SKILL.md` nor a script's new version: 6,486 are the old version of a script, and 1 is not used by any change row.

## Repeated change keys

A change key of `repo`, `skill_folder`, `commit` and `file_path` repeats on **44,537** keys (125,000 rows) in 235 repositories. The query in `registry_history.sql` expects 0, but the rows are not copies. A repeat happens when one skill's file lives in two places at once. Some repositories kept parallel copies of a skill: `lingxling/awesome-skills-cn` had the same skill in `plugins/…-claude/skills/<name>`, `plugins/…/skills/<name>` and `skills/<name>`, and one commit changed all three. After the move fix, all three belong to the surviving folder, and each change keeps its own `repo_path`.

With `repo_path` in place of `file_path`, only **750** keys repeat (19 repositories). Later stages should use `repo`, `skill_folder`, `commit` and `repo_path` as the key.

## Known limits

- `ClickHouse/ClickHouse` and `Expensify/App` have no history (27 English dataset skills).
- A deleted skill at the repo root is not mined, because it would mean logging every file of the repo.
- A deleted folder is logged by its exact path, so a rename before its deletion is not followed.
- `majiayu000/claude-skill-registry` has no deleted-skill check. Its history-only paths are old layouts.
- In the 143 registries with more than 200 folders, renames are not followed by git. A move is found from shared `SKILL.md` versions instead.
- The tables mix two snapshot dates. The first 3,566 registries were mined on 2026-11-02; the others on 2026-11-06 and 2026-11-07.
