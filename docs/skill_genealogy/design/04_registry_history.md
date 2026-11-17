# Design: registry history (stage 3)

Card G09 · Code: `download_registry_history.py`, `git_history.py` · [Code flow](../code_flow/04_registry_history.md) · [Report](../report/04_registry_history_report.md)

## Problem

The dataset keeps one snapshot of each skill. To number a registry skill's versions, date them, and see what a copy was copied from, the study needs every change of every skill folder in the 4,806 registries: `SKILL.md`, scripts and the other files. That is 609,442 folders and about 8 million file changes, from repos as large as `chromium/chromium`.

## Design at a glance

Clone each registry's default branch without file contents, log each skill folder (following renames when the repo is small enough), fetch only the texts of `SKILL.md` and small scripts, store one shard per repo, and merge the shards into three tables: changes, texts, status.

## Decisions

**Blobless clone of the default branch.** `--filter=blob:none --no-checkout --single-branch` brings commits and trees, not file contents. The log needs only trees; texts are fetched afterwards for the few files that matter. The default branch is what users install.

**Skill folders, not single files.** A skill is a folder (`SKILL.md` plus scripts and references). Stage 4b compares whole folders, and a script can change while the text stays the same. Every file change in the folder is kept; only `SKILL.md` and scripts get their text.

**Follow renames for small repos, one log for large ones.** With up to 200 skill folders (`MANY_PATHS_LIMIT`), each folder is followed with `git log --follow` on its `SKILL.md`, and then every folder name it lived in is logged, so a renamed skill keeps its whole past. Above 200 folders, one log reads all folders: following 300,000 folders one by one would take days. Above 1,000 folders that log names their parent folders instead (`GIT6_MAX_PATHSPECS`), because git's pathspec matching grows much faster than the folder count, and the rows are cut back to the folders afterwards.

**Keep deleted and moved skills.** Folders are chosen at HEAD, but a skill deleted before the download is still in the history. Every `SKILL.md` path that ever existed is listed, and each history-only folder becomes a **deleted skill** (a visible delete, its text lives nowhere else), a **moved skill** (one of its versions lives in a folder that still exists; its rows are attached there as `former_folder`, so the skill keeps its earliest dates), or is dropped (no visible delete, as after a layout rebuild). Without the moved history, a crawler that copied a skill looks older than the skill's author. A repo with more than 20,000 history-only folders (`DELETED_FOLDERS_MAX`) rebuilt its layout and is not searched.

**Drop history that belongs to another skill.** `--follow` also sees copies and unrelated folders of the same name. A folder that never became this skill is `not_ours`; a copy source keeps only the rows before the copy (`copy_source`); rows after it are `after_copy`. Each kept row says its role (`own`, `former_folder`, `copy_source`), and the dropped rows are counted on the status row.

**Text of `SKILL.md` and scripts up to 200 KB.** Scripts carry the risky capabilities RQ2 looks for. 200 KB is about three times the 99th-percentile script size and covers 99.8% of scripts; larger files are mostly bundled or generated code. Larger scripts are still listed with path, sha and mode.

**One shard per repo, status written last.** A crash before the status file leaves a repo that the next run mines again. A rerun skips `ok`, `gone` and `empty`; `--retry-failed` retries `timeout` and `error`. Eight repos run at a time, each git command up to 600 seconds.

**The author is a hash.** `author_key` is the SHA-1 of the lowercased email. Later stages can tell authors apart without storing personal data.

**Merge in DuckDB.** A pandas merge of 5 million change rows peaked near 15 GB; DuckDB with a 4 GB cap spills to disk.

## Alternatives rejected

- **The GitHub API per file.** Rate limits make millions of calls impossible.
- **Full clones.** Hundreds of GB of file contents the study never reads.
- **Only `SKILL.md` history.** Misses script changes, which are a core RQ2 signal.

## Effect

4,771 of 4,806 registries `ok`, 33 `gone`, 1 timeout, 1 error. 609,442 skill folders, 7.9 million changes, 2.0 million texts (20 GB). 99.0% of the English skill folders the dataset saw in finished registries have history.

## Limits

- Repos with more than 200 folders (143 repos, 79% of folders) are not followed across renames; a move is found from shared versions instead.
- A history rewrite (squash, force push) loses the old dates.
- The tables mix clone dates (2026-11-02 to 2026-11-07).
- The change key that is unique is `repo`, `skill_folder`, `commit`, `repo_path`: one skill can live in two places at once.
