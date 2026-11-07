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
| 2026-11-07 | `--deleted-folders` | Skills deleted before the download (bug 2), in 1,009 registries |

Every shard has a status file, and no shard changed after the last merge.

## What finished

Every registry has a status row.

| Status | Dataset basis | Tree basis | What it means |
|---|---:|---:|---|
| ok | 3,531 | 1,240 | Skill-folder history was saved |
| gone | 33 | 0 | Git reports the repository was not found |
| timeout | 1 | 0 | A git command ran longer than 600 seconds |
| error | 1 | 0 | The mine failed for another reason |

The timeout is `ClickHouse/ClickHouse`, also with `--since 2025-09-01`. The error is `Expensify/App`: `fatal: error in object: unshallow`, from the shallow clone. Together they hold 27 English dataset skills. A finished mine took 9.8 seconds at the median. The longest took 8,104 seconds (`chromium/chromium`). Added together, the finished mines took 2,308 minutes of git time.

**16** `ok` registries have no rows. Their skills were moved, or deleted in a way git does not show.

## Skill folders

The 4,771 finished registries hold **609,442** skill folders and **7,691,573** file changes.

| Skill folders | Repos | Folders | Changes |
|---|---:|---:|---:|
| 0 | 16 | 0 | 0 |
| 1–4 | 9 | 22 | 33,502 |
| 5–19 | 2,864 | 28,351 | 891,168 |
| 20–49 | 1,130 | 34,590 | 841,048 |
| 50–199 | 578 | 53,355 | 1,779,784 |
| 200–1,000 | 139 | 55,420 | 816,174 |
| over 1,000 | 35 | 437,704 | 3,329,897 |

Rename following is on for the 4,628 repositories with 200 skill folders or fewer. It is off for the 143 above that line. Those 143 hold 483,200 folders (79%) and 3,891,314 changes.

The largest are `majiayu000/claude-skill-registry` (312,153 folders), `Klotzkette/claude-fuer-deutsches-recht` (23,399), `lingxling/awesome-skills-cn` (9,687), `sickn33/agentic-awesome-skills` (9,044), and `ECNU-ICALK/AutoSkill` (8,195).

**One registry is 51% of all folders.** `majiayu000/claude-skill-registry` is a crawler mirror, not a registry of authored skills. Its README says "This is an aggregate catalog, not a single plugin or a first-party skill pack" and "The maintainer of this mirror is not necessarily the author of a listed skill." 98.8% of its `SKILL.md` history comes from bulk commits such as `Discover 34337 new skills`. On 2026-10-08 it deleted all 312,160 `SKILL.md` files, and on 2026-10-21 it added them back as pointer pages ("It is not an installable skill"). Its commit dates are crawl dates. Later stages should not treat it as a source; stage 3c will label it.

## Deleted skills

Stage 3 first chose skill folders from the newest commit only, so a skill deleted before the download had no history (bug 2). The fix reads every `SKILL.md` path in the history and keeps a folder when its deletion is real.

| | Folders |
|---|---:|
| History-only folders logged in 1,009 registries | 81,126 |
| Moved: a `SKILL.md` version also lives in a current folder | 30,667 |
| No visible delete (for example after a large restructure) | 13,071 |
| Newer than the snapshot | 2,180 |
| **Kept as deleted skills** | **35,208**, in 929 registries |

`deleted_folders` and `history_folders_dropped` on the status row give the counts per registry. **35,234** folders now end with a `SKILL.md` delete. Of the English `SKILL.md` folders the dataset saw in finished registries, **175,904 of 187,786 (93.7%)** now have history, against 88.6% before the fix. `chromium/chromium` (timeout), `shobcoder/shob` (deleted from GitHub) and `majiayu000/claude-skill-registry` (old layouts, not deletions) were not checked.

## What the changes are

A change row is one file in one commit. `history_role` says how that file belongs to the skill.

| Role | Rows | Share | Meaning |
|---|---:|---:|---|
| own | 7,399,530 | 96.2% | The skill folder itself |
| former_folder | 187,738 | 2.4% | A name this skill used to have |
| copy_source | 104,305 | 1.4% | The folder this skill was copied from, before the copy |

Following a rename also sees other folders. Those rows are dropped and counted on the status row: **65,896** `not_ours`, **109,081** `after_copy`, and **2** `twin`.

| Git status | Rows | Meaning |
|---|---:|---|
| M | 3,353,556 | The file was modified |
| A | 2,901,134 | The file was added |
| D | 1,162,218 | The file was deleted |
| R | 274,117 | The file was renamed |
| T | 548 | The file type changed |

`SKILL.md` is 2,474,940 of the rows and scripts are 830,058. The two groups overlap by 2,062 rows: a `SKILL.md` with mode `100755` also counts as a script. The other 4,388,637 rows are the remaining files in the skill folders.

## Versions of SKILL.md

**609,410** skill folders have at least one `SKILL.md` change. The median folder has 3 such commits. The 90th percentile has 6. **111,019** folders (18.2%) have one `SKILL.md` commit, and **127,253** (20.9%) have 5 or more. Bulk commits in large registries count as one commit for each folder they touch.

The commit dates run from 2016-02-26 to 2026-11-07. The earliest dates belong to files that were in a folder before it became a skill. The folder with the most `SKILL.md` commits is `kortix-ai/suna` `.claude/skills/learnings`, with 358 commits from 2026-08-10 to 2026-09-25. Next are `mvanhorn/cli-printing-press` `skills/printing-press` (310) and `drandyhaas/KiCadRoutingTools` `.claude/skills/plan-pcb-routing` (272).

## Texts

`registry_history_texts.parquet` holds **1,972,770** blobs. The text is 20.2 GB, stored in a 5.44 GB file. The median text is 5,101 characters. The longest is 1,025,262 characters.

Every `SKILL.md` blob is stored: 1,433,870 blobs, before and after each change. Of 541,088 script blobs, 532,239 are stored. The other 8,849 scripts have no text, because a script larger than 200,000 bytes is left out. 6,669 stored blobs are neither a `SKILL.md` nor a script's new version: 6,598 are the old version of a script, and 71 are not used by any change row.

## Repeated change keys

A change key is `repo`, `skill_folder`, `commit`, and `file_path`. **655** keys are stored on more than one row (4,437 rows), in 35 repositories. The query in `registry_history.sql` expects 0.

The rows are not copies. They happen when one skill's `SKILL.md` lives in two places at once. In `serenakeyitan/tdoc`, the root `SKILL.md` (a former location of the skill) and `skills/tdoc/SKILL.md` both belong to `skills/tdoc`. A commit that changes both gives two rows with `file_path = SKILL.md` and different `repo_path` values. A key that later stages use should include `repo_path`.

## Known limits

- `ClickHouse/ClickHouse` and `Expensify/App` have no history (27 English dataset skills).
- A deleted skill at the repo root is not mined, because it would mean logging every file of the repo.
- A deleted folder is logged by its exact path, so a rename before its deletion is not followed.
- `majiayu000/claude-skill-registry` has no deleted-skill check. Its history-only paths are old layouts.
- In the 143 registries with more than 200 folders, renames are not followed. A rename appears as a delete and an add.
- The tables mix two snapshot dates. The first 3,566 registries were mined on 2026-11-02; the others on 2026-11-06 and 2026-11-07.
