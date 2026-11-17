# Staleness

Stage 10, card G22. [Copy changes](15_copy_changes.md) (stage 9) measures what a copier changed against the version the copy started from. This stage answers RQ3: does a copy fall behind the registry it came from? For every English copy file of stage 7 it finds the registry version the copy's text still holds (A44), counts the releases after it up to an as-of date (A45), finds the registry package version the copy's folder holds and the package versions after it (A46), counts what the missed updates touched, and gives each copy a state (A47). The copy's own history gives its sync events: the first registry version it took, later updates and downgrades, and the lag behind each release (A48). The stage reads saved tables only; no network.

The code is `src/skill_genealogy/measure_staleness.py`. A state describes the files at a date. It does not say who maintains the copy, or whether the copier should have updated.

## Scripts

```bash
bash scripts/skill_genealogy/dev_mode/16_staleness.sh [--force] [--stale-days N]
bash scripts/skill_genealogy/full_run/16_staleness.sh [--force] [--stale-days N]
bash scripts/skill_genealogy/tests/16_staleness.sh
```

Dev runs in the foreground, log in `logs/measure_staleness_dev_<date>.log`. Full runs in the background with `caffeinate`, log in `logs/measure_staleness_<date>.log`, progress in `logs/measure_staleness_full.progress.json`. The stage skips when both outputs exist, and then prints the summary of the saved files. `--stale-days N` sets the stale cutoff for this run (default `STALE_AFTER_DAYS`, 30); the value is stored in every row, and a run with a new cutoff needs `--force`.

## Inputs

- Dev or full data folder: `copy_files.parquet`, `families.parquet` (stage 7), `lineages.parquet`, `lineage_versions.parquet`, `lineage_changes.parquet` (4a), `lineage_package_versions.parquet`, `lineage_folder_manifests.parquet` (4b), `copied_skill_history/` (6).
- Shared full folder: `skill_language.parquet` (2a), `registry_history/registry_history_changes.parquet` and `registry_history_texts.parquet` (3).
- GitSkills `artifacts` (dates of every copy file, text of a copy with no history) and `artifact_siblings` (the folder of a copy with no history).

A missing file names the command that builds it.

## Steps

**Step 1: The copies, their current text and the as-of date.** A copy file is measured when it has a lineage, its text is English, and it is not a registry's own file (`registry_self`), as in stage 9. Unlike stage 9, copies dated before the registry release (`copied_before_release`) and copies without a date stay in the table, so they are counted rather than lost. The current text is the newest `SKILL.md` blob of the stage 6 history (deletes skipped), dated by its commit; a copy with no history row has its dataset text, dated `last_commit_at` (Q10.2). The as-of date is chosen per copy (rule below).

**Step 2: The held version (A44).** One pass over every copy. Texts are read only for copies whose blob is no version of their lineage: the copy's text (history store, else the dataset) and the lineage versions released by the copy's date (registry store), in batches of `COPY_CHANGE_FILE_BATCH` copies.

**Step 3: Releases missed (A45).** Counted from the held version to the as-of date; `latest_seq` is the newest version released by then.

**Step 4: The held package (A46).** In the same batches as step 2. The copy's current folder is rebuilt with the stage 9c helpers (A42): the folder after the newest commit while its `SKILL.md` is there, else after the last `SKILL.md` commit; a copy with no history takes its `artifact_siblings` listing when GitSkills listed the folder. Q10.4 counts the files the folder shares with each package version of the lineage. `stopped_changing_at` is the newest commit of the folder's own history.

**Step 5: What the missed updates touched.** Q10.3 lists, for every package version with `changed_scripts`, the scripts that differ from the package version before it. Their flags come from A16 (`load_script_flags` of stage 9c). For each copy, the missed text releases (`lineage_changes`) and the missed package versions with script changes are summed (`find_missed_updates`).

**Step 6: The state (A47).**

**Step 7: Sync events (A48).**

**Step 8: Save and print.** Both files sorted by repo and path. The run prints the states (files and texts), the states by `arrival_group`, the median days stale of exact copies, missed content by kind, the bases, the events by kind with their median lag, and two sample records of each output.

### The as-of date (rule 2)

| Copy | As-of date | `as_of_basis` |
|---|---|---|
| Deleted in its own repo (`removed`, stage 7) | its `removed_date` | `copy_removed` |
| Deleted after its registry's snapshot | the snapshot (later releases are not in the data); state still `removed` | `registry_snapshot` |
| Has a downloaded history | the snapshot of its lineage's registry | `registry_snapshot` |
| Dataset only | `discovered_at` of its dataset row | `dataset_capture` |

A registry's snapshot is the later of its newest downloaded commit (Q10.1) and `REGISTRY_MINED_FROM` (2026-11-02, the first day any registry history was mined). Registries were mined on 2026-11-02, 11-06 and 11-07, so one global date would either count releases a registry's history cannot hold or miss releases it does hold. The newest commit alone would put a registry that went quiet in May at May, and shorten the lag of every copy of it by months. The floor keeps the snapshot within 5 days of the real mining day and never before a release the history holds.

## Algorithm A44: the held version

**Goal.** Name the registry version a copy's text still is, as of now.

**Rule.**
1. A lineage version with the copy's blob, or the same text up to whitespace (the same `normalized_sha`, A20), is held exactly: `exact_current`, similarity 1. When several versions have that text (the registry went back to an older text), the newest released by the as-of date is held.
2. Else the most similar version released on or before the copy's date (`current_date`; for an undated copy, the as-of date): exact Jaccard of the 5-word shingles of the body, the same score as stage 9b (`comparable_shingles`). A tie goes to the newer version. `similar_before_date`, with the score in `held_similarity`.
3. No version released by the copy's date: `copied_before_release`, no held version. No text to compare: `no_text`. A best score of 0 (no shared 5-word shingle with any version): `no_overlap`, no held version, because the newest version would otherwise win the tie with no evidence.

**Why.** An exact match needs no date: whatever the copy's git date says, it holds that text. A modified copy is matched only against versions that existed when it was last changed, so a copy is never said to hold a version released after it. Git dates can be set freely, so a copy dated before every release is counted, not moved to a guessed version.

## Algorithm A45: versions behind

**Goal.** How many releases a copy missed, and since when.

**Rule.** The versions numbered after the held one and released on or before the as-of date. `skill_md_versions_behind` is their count, `first_missed_release` the release date of the first. Stage 4a numbers versions in release order (no release date decreases with the number), so "after" is a higher number; two versions released at the same moment are still counted in order.

**Why.** Counting by date alone would skip a version that has the same commit date as the held one (372 such pairs in the lineages).

## Algorithm A46: the held package

**Goal.** Name the registry package version (stage 4b) a copy's folder holds, so a copy whose `SKILL.md` is current but whose `scripts/deploy.sh` is the old one is still behind.

**Rule.** Among the lineage's package versions committed on or before the copy's date, the one that shares the most `(path, sha)` files with the copy's current folder; the copy's `SKILL.md` counts as a file, matched with the blob of the package's text version. A tie goes to the newest. `package_versions_behind` counts the package versions numbered after it and committed by the as-of date; `first_missed_package` is the first of them; `missed_script_updates` counts those with `changed_scripts`.

`package_basis` says when this is measured:

| `package_basis` | When | Package columns |
|---|---|---|
| `measured` | the copy keeps at least one file of the held package besides `SKILL.md`, or the package has no other file | filled |
| `files_not_kept` | the package has files besides `SKILL.md` and the copy keeps none of them | held package only |
| `no_folder` | the copy's folder is not known (dataset copy GitSkills never listed) | null |
| `no_package` | no package version was committed by the copy's date | null |

**Why.** A copy that took only the `SKILL.md` never had the registry's scripts, so a later script fix is not an update it missed. Counting it would make every aggregator that drops scripts "behind" on every script change. Q10.4 is the SQL form for full data; `choose_held_package_version` is the plain form, and a test checks they agree.

## Algorithm A47: the state

**Rule.** Checked in this order:

| State | When |
|---|---|
| `removed` | the copy was deleted in its own repo (behind and days stale are still measured up to `removed_date`) |
| `orphaned` | the registry removed the skill (`registry_removed`) on or before the as-of date; `days_since_removed` = as-of − `removed_at` |
| `unknown` | no held version (`copied_before_release`, `no_text` or `no_overlap`) |
| `up_to_date` | no missed text release and no missed package version |
| `stale` | `days_stale` > `stale_after_days` |
| `behind` | otherwise |

`days_stale` = as-of − the earlier of `first_missed_release` and `first_missed_package`. A copy behind only on its package is stale once the package update is older than the cutoff.

**Why.** A deleted copy is gone, not stale, and a copy of a removed skill has nothing current to be behind. `unknown` keeps copies the method cannot place in the denominator instead of guessing a version for them.

## Algorithm A48: sync events

**Rule.** Walk a copy's history rows in the A39 order (deletes skipped). A row whose blob is a version of the copy's own lineage is a registry text; its version number is chosen as in A44 rule 1, by the row's date. The first registry text is a `copy`; a later higher version is an `update` with `versions_skipped` = the versions between the two; a lower one a `downgrade`; the same one is no event. Other texts are local edits and are skipped. `lag_days` = commit date − release date of the version taken. An inherited commit (a registry history also holds it) sets the version the copy starts from but is not an event.

**Why.** Exact blobs, as in A39: a copy that is "nearly" a registry version did not sync. Inherited commits are the registry's own releases inside a fork, not the copier taking them.

## Outputs

`copy_staleness.parquet`, one row per copy file:

| Column | Meaning |
|---|---|
| `repo, path, lineage_id, link, arrival_group` | the copy (stage 7) |
| `current_basis, current_blob` | where the current text comes from (`history` or `dataset`) and its blob |
| `held_seq, held_basis, held_similarity, held_release_date` | A44 |
| `as_of_date, as_of_basis` | rule 2 |
| `latest_seq` | newest version released by the as-of date |
| `skill_md_versions_behind, first_missed_release` | A45 |
| `package_held_seq, package_basis, package_versions_behind, first_missed_package, missed_script_updates` | A46 |
| `days_stale` | as-of − the earlier first miss |
| `missed_substantive, missed_commands, missed_network, missed_scripts` | number of missed updates that were substantive, added a command, a network reference, or a script (named in `SKILL.md` or added to the package) |
| `missed_risky` | a missed update added a command, network reference or script, or changed a script's risk flags |
| `stopped_changing_at` | newest commit of the copy folder's history (null without history) |
| `copied_before_release` | from stage 7 |
| `registry_removed, days_since_removed` | the skill was removed by the as-of date |
| `copy_state, stale_after_days` | A47 and the cutoff used |

`sync_events.parquet`, one row per event: `repo, path, lineage_id, commit, commit_date, event_kind, from_seq, to_seq, versions_skipped, release_date, lag_days`.

## Settings

| Setting | Value | Meaning |
|---|---|---|
| `STALE_AFTER_DAYS` | 30 | days behind before a copy is stale; same as `rq2.grace_days`; `--stale-days` overrides |
| `REGISTRY_MINED_FROM` | 2026-11-02 | first day of the stage 3 mining; floor of a registry's snapshot |
| `COPY_CHANGE_FILE_BATCH` | 20,000 | copies per batch (shared with stage 9) |

## Queries

- **Q10.1** `REGISTRY_NEWEST_COMMIT_SQL`: newest commit date of each registry's downloaded history.
- **Q10.2** `DATASET_DATES_SQL`: `last_commit_at` and `discovered_at` of each copy file's dataset row.
- **Q10.3** `SCRIPT_DELTAS_SQL`: for every package version with `changed_scripts`, the scripts (`file_category = 'script'`) whose sha differs from the package version before it; an added script has no old sha, a removed one no new sha.
- **Q10.4** `SHARED_PACKAGE_FILES_SQL`: per copy and candidate package version (committed by the copy's date), the number of shared `(path, sha)` files, `SKILL.md` included, and whether the `SKILL.md` was one of them.

Stage 9 queries reused: Q9.1 (stored texts), Q9.3 (dataset texts), Q9.5 (copy folder rows), Q9.7 (sibling files), Q9.9 (sibling texts) and Q9.10 (listed folders).

## Tests

`tests/skill_genealogy/test_measure_staleness.py` uses one timeline: v1 on 10 January, v2 on 1 March, v3 on 15 May 2026, and a package 4 on 1 June that fixes `scripts/deploy.sh`.

- A copy holding v1 on 23 October is 2 behind, first miss 1 March, 236 days stale, `stale`; with `--stale-days 400` it is `behind`.
- A copy with v3's text and the old script holds package 3, is 1 package behind with 1 missed script update, and is not `up_to_date`. The SQL pick and the plain A46 agree.
- A copy that kept no registry file is `files_not_kept` and has no package lag.
- A removed lineage gives `orphaned` with `days_since_removed`; a removal after the as-of date does not.
- A copy deleted on 1 June holding v1, with v3 moved to 1 July, is as-of 1 June, 1 behind, `removed`.
- Sync events: copy v1, a local edit, update to v3 (1 skipped), downgrade to v2, with their lags; inherited commits set the start but are not events.
- A44: exact blob, whitespace-equal text, most similar before the copy date, a copy before every release, no shared shingle, a text the registry returned to.
- A copy deleted after the registry snapshot is measured at the snapshot and stays `removed`; missing flags read as false.

## Limits

- The snapshot of a registry can be up to 5 days before the day it was mined.
- `days_stale` counts from the first missed release, not from the day the copy could have seen it.
- A modified copy's held version is the most similar one, which can be a version it never held when its edits move it toward a later text.
- Package lag counts every later package version the copy could have taken, including changes to references and assets; `missed_script_updates` isolates scripts.
- A dataset copy without a folder listing (`no_folder`) has text lag only.
- Sync events need a history; a dataset copy has none.
- A copy can hold exactly a text the registry released after the copy's as-of date (882 on full data); it is 0 behind.
- A sync event's `lag_days` is negative when the copy's git date is before the release.
