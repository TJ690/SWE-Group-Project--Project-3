# Design: copied-skill history (stage 6)

Card G15 · Code: `download_copied_skill_history.py` · [Code flow](../code_flow/12_copied_skill_history.md) · [Report](../report/12_copied_skill_history_report.md)

## Problem

The dataset keeps one snapshot of each copy. That is enough to say a copy exists (stage 5c), but not when it was copied, what the repo changed afterwards, or whether it ever took a registry update. RQ1 needs the copy date, RQ2 the copy's edits, RQ3 its updates. Only the copy repo's git history has them.

## Design at a glance

Turn every copy file into a target (repo, skill folder). Clone each of the 60,958 copy repos without file contents and keep the history of the target folders only, with the same git code as stage 3. Store each repo in one DuckDB transaction. Export changes, texts and status with the stage 3 columns.

## Decisions

**Every copy repo, the copied folder only.** The target is the parent folder of each copy file, so later stages can compare the copy's whole folder with the registry package (stage 9c), not only `SKILL.md`. Other folders of the repo are not the copy and are not logged.

**Reuse the stage 3 git code.** The clone, log, rename following, large-repo pathspec handling and text fetching are the same functions, so copy history and registry history mean the same thing column by column. The deleted-folder search of stage 3 does not run, because the folders are given: a copy is a folder the dataset saw.

**One source of copy history.** The main pipeline's `wild_history` is not used: it covers 1,230 of 1.4 million copy files (about 21 repos), holds only `SKILL.md`, is an older snapshot, and its code is not in this repository. One source keeps stages 7–10 simple.

**Copier repos are never cloned.** Stage 5c links copies to source lineages only and leaves out files inside any registry, so a copier never appears as a target.

**A DuckDB store, one transaction per repo.** Stage 3 writes three files per repo; for 60,958 repos that is about 180,000 files. One database file, where each repo is saved whole or not at all, resumes just as safely. Git runs in 8 threads; only the main thread writes to DuckDB.

**The same resume rule as stage 3.** `ok`, `gone`, `empty` and `too_large` are finished; `timeout` and `error` are retried with `--retry-failed`.

**Mirrors over 20,000 folders are not cloned.** Three repos hold 28,000–52,000 copied folders each. Logging that many folders in one repo used more memory than the machine has in stage 3, and a mirror's history shows when it mirrored, not how a copy was edited. They are saved as `too_large` with the folder count.

**A full run asks first.** Above 2,000 pending repos a full run stops unless `--yes` is passed, and prints a time estimate from the dev run (a lower bound, because fast `gone` repos pull the median down).

**A version is the folder's own `SKILL.md`, as in stage 4a.** A copy file at `skills/SKILL.md` or at the repo root makes a target that contains other skills, and its log holds their `SKILL.md` files too. Only `file_path = SKILL.md` with role `own` or `former_folder` counts; counting every `SKILL.md` would give one parent folder over 6,000 versions.

**Sorting in parts.** The texts table (7.9 GB) is sorted by sha in 16 sha ranges, each under the 4 GB memory cap, and written in order into one file. The file is the same as one sort would give.

**One clone folder per exact repo name.** The dataset lists a few repos under two spellings. On a case-insensitive disk they would clone into one folder and both fail, so each clone folder is named by a hash of the exact name.

**Dates stay as git gives them, and are read as UTC.** `commit_date` keeps its offset, as in stage 3; every comparison casts it to UTC.

## Alternatives rejected

- **Shards like stage 3.** Too many files at this scale.
- **Clone only repos without `wild_history`.** Would mix two sources for a saving of about 21 clones.
- **Log every skill folder of each copy repo.** Most are not copies; the history would be much larger for nothing.

## Effect

55,296 of 60,958 copy repos `ok`, 5,646 `gone` (9.3%), 7 blocked, 3 `too_large`, 6 `empty`. 1,112,615 copied folders in `ok` repos, 1,096,579 of them with `SKILL.md` history; 12.9% have more than one version (median 1, 90th percentile 2). 12.1 million change rows, 635,021 texts. 91% of copied folders get their first `SKILL.md` between January and June 2026.

## Limits

- Default branch only: a copy the dataset saw on another branch, or in a repo rewritten since, has no history (16,036 folders, 1.4%; 901 repos have none of their folders).
- The clone is newer than the dataset snapshot; repos deleted since are `gone` (9.3%), and a folder moved since keeps only its rows up to the move.
- Staleness must compare copy and registry at the same date (both at the snapshot, or both at the download), never the snapshot copy with today's registry.
- A parent target's change rows include its nested skills' files; later stages separate them.
