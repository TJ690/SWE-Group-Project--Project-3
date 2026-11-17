# Copied-skill history

Stage 6. The dataset keeps one snapshot of each skill, so it cannot show how a copy changed after it was copied. This stage clones every dataset repo that holds a copy of a registry skill and keeps the file history of the copied skill folders only. It is the only source of copy history: registry history comes from [stage 3](04_registry_history.md), copy history from here. It uses the network. Card G15.

The code is `src/skill_genealogy/download_copied_skill_history.py`. The clone and log commands are the stage 3 ones in `git_history.py` (GIT1–GIT8); this module chooses the folders, runs the clones in parallel and stores each repo.

## Scripts

Run these from the repo root.

```bash
bash scripts/skill_genealogy/dev_mode/12_copied_skill_history.sh [--limit 5]
```

`download_copied_skill_history --dev` in the background with `caffeinate`; the log is `logs/skill_genealogy/download_copied_dev_<date>.log`. The dev copy table has 3,969 repos. `--limit 5` clones only the first five repos (a quick check, a few seconds).

```bash
bash scripts/skill_genealogy/full_run/12_copied_skill_history.sh
```

The full copy table (60,958 repos) in the background, log `logs/skill_genealogy/download_copied_<date>.log`. A full run with more than 2,000 pending repos stops unless `--yes` is passed; the script passes it. Follow it with `tail -f` on the log.

```bash
bash scripts/skill_genealogy/full_run/12_copied_skill_history_retry.sh
```

Clones the `timeout` and `error` repos again (`--retry-failed`), in the foreground under `caffeinate`. All three run scripts keep the Mac awake (`caffeinate -i -s -m`) until the download ends, because a sleeping machine stalls every open clone into a timeout.

```bash
bash scripts/skill_genealogy/tests/12_copied_skill_history.sh
```

Runs the tests and doctests. No network: the clone tests use local `file://` repos.

## Inputs

- `copy_candidates.parquet` (stage 5c) in the dev or full data folder: only `repo` and `path` are read. A missing file stops the run and names `python -m skill_genealogy.find_copies [--dev] --only copies`.

## Steps

**Step 1: Pick download targets (Algorithm A29).** Every copy file becomes one target `(repo, skill_folder)`, where the skill folder is the parent folder of the file path (`.` for a file at the repo root). Two files in one folder are one target. Every copy file is a target. Stage 5c links copies to source lineages only, so a copier registry is never a target, and a text that only a copier holds (`copier_seen`) is not in the table.

**Step 2: Group by repo and skip finished repos.** Targets are grouped per repo, folders sorted. `--limit N` keeps the first N repos by name. A repo whose status in the store is `ok`, `gone`, `empty` or `too_large` is finished and is not cloned again. `timeout` and `error` are cloned again only with `--retry-failed`. This is the stage 3 resume rule; a repo with no status row is cloned. Before cloning, the run prints the planned repos and folders, the pending repos, and an hour estimate: the median seconds per repo of the dev run, times the pending repos, over the 8 workers. Fast `gone` repos pull that median down, so the estimate is printed as a lower bound.

**Step 3: Clone and store each repo.** Eight repos are cloned at a time (`DOWNLOAD_WORKERS`); each git command may run 600 seconds (`DOWNLOAD_TIMEOUT_SECONDS`, or `--timeout`). Each repo goes through `git_history.mine_one_repo` with its folder list, so only those folders are logged, exactly as stage 3 logs a registry: a blobless clone of the default branch, `--follow` across renames when the repo has at most 200 folders (`MANY_PATHS_LIMIT`), one log through covering parent folders above 1,000 folders (`GIT6_MAX_PATHSPECS`), texts of `SKILL.md` and scripts read in batches (`BLOB_READ_BATCH`), and progress lines every 30 seconds (`PROGRESS_SECONDS`) inside a long repo. Because the folders are given, the search for deleted skill folders of stage 3 does not run: a copy is a folder the dataset saw, not one the history may hold.

A repo with more than `COPY_HISTORY_MAX_FOLDERS` (20,000) target folders is not cloned. Its status is `too_large` and the message gives the folder count. Only mirrors reach that size: on the full table `majiayu000/claude-skill-registry-data` (52,358 folders), `gabrielmoreira/agent-skills-mirror` (50,481) and `David-Li0406/meta-skill-evloving` (28,683); the next repo has 17,740. Logging that many folders in one repo used more memory than the machine has in stage 3, and a mirror's history shows when it mirrored, not how a copy was edited.

Each repo clones into its own folder under `work/`, named by a hash of the exact repo name, and the folder is deleted when the repo is done. The dataset can list one GitHub repo under two spellings (`JubileeZ/Alpha-Zero-G` and `JubileeZ/alpha-zero-g`); on a case-insensitive disk, folders named after the repo would collide. Both spellings are kept as separate repos, because the copy table names both.

The git work runs in threads (each clone is a subprocess). The main thread saves each finished repo in one DuckDB transaction (`save_repo_to_store`): it deletes that repo's old rows, inserts its change rows, inserts each text whose blob sha is not stored yet, and writes the status row last. A failure rolls the transaction back, so a crash leaves either the whole repo or nothing, and an unfinished repo is cloned on the next run. One line is printed per repo: `repo status seconds changes`.

**Step 4: Export the store.** The three tables are written to parquet with DuckDB (memory cap `HISTORY_DUCKDB_MEMORY_LIMIT`, spill folder `duckdb_tmp/`). Texts are only the blobs a change row names. They are sorted by sha in 16 sha ranges and written in order into one file, so the sort fits under the memory cap (Q6.3). `download_targets.parquet` lists this run's targets. The temporary `work/` clone folder is deleted.

**Step 5: Summary.** For the repos of this run's targets: repos and folders targeted, repos per status, the folders of `ok` repos and how many of them have no change row (`folders_without_history`), versions per copied skill (median, 90th percentile, share with more than one), change rows, text rows and bytes, the first and last commit date in UTC, repeated change keys, disk bytes, run time, and two sample rows of changes and status.

## Algorithm A29: download targets

**Rule.** `skill_folder = parent(path)`, `.` for a root file; one row per distinct `(repo, skill_folder)`, sorted.

**Why the folder, not the file.** Later stages compare the copy's whole folder with the registry package (stage 9c), so the history of every file in the folder is kept, not only `SKILL.md`.

**Example** (doctest of `targets_from_copies`): `a/r skills/pdf/SKILL.md`, `a/r skills/pdf/skill.md`, `a/r skills/other/SKILL.md`, `b/r SKILL.md` give three targets: `a/r skills/other`, `a/r skills/pdf`, `b/r .`.

## Versions in the summary

A version here is a distinct blob of the folder's own `SKILL.md` (`file_path` is `SKILL.md` in any letter case, role `own` or `former_folder`) that is not a delete, counted per target folder of an `ok` repo. This is the stage 4a rule. A target can be a parent of other skills (a copy file at `skills/SKILL.md` or at the repo root): its log then also holds `skills/pdf/SKILL.md` and the like, which belong to other skills and are not its versions. A whitespace-only edit is a new blob, so this is an upper bound on the text versions that stage 7 counts after `normalize_text`. A folder with no change row has 0 versions: the folder is not on the default branch's history under that path (the dataset snapshot came from another branch, or the repo was rewritten). It is counted in `folders_without_history` and left out of the median.

## Queries

- **Q6.1** `_CREATE_STORE_SQL`: the `changes` (stage 3 columns), `texts` (`sha` primary key) and `status` tables, and an index on `changes(repo)`.
- **Q6.2** `_EXPORT_CHANGES_SQL`: every change row, sorted by repo, folder, `commit_seq`, file, status, `new_sha`.
- **Q6.3** `_KEEP_SHA_SQL`, `_EXPORT_TEXTS_PART_SQL`: texts whose sha is a `new_sha` or `old_sha` of a change row, sorted by sha, in row groups of `TEXT_ROW_GROUP`. They are sorted in 16 sha ranges with no gaps, split at the hex digits, and written in order into one file, so the sort fits under `HISTORY_DUCKDB_MEMORY_LIMIT`; the full run holds 7.9 GB of text.
- **Q6.4** `_EXPORT_STATUS_SQL`: the status table sorted by repo.
- **Q6.5** `_INSERT_TEXTS_SQL`: new texts only (`ANTI JOIN texts USING (sha)`), one per sha.
- **Q6.6** `_VERSIONS_SQL`: per target folder of an `ok` repo, distinct blobs of the folder's own `SKILL.md` (`file_path` is `SKILL.md` in any case, role `own` or `former_folder`, not a delete), the stage 4a rule. A nested `x/SKILL.md` is another skill and is not counted.
- **Q6.7** `_REPORT_CHANGES_SQL`, `_STORE_TOTALS_SQL`: this run's change rows (key and date columns only) in a temp table, then one statement each for change rows, texts and bytes, first and last commit (UTC), and repeated change keys. One statement for all of them does not fit under `HISTORY_DUCKDB_MEMORY_LIMIT` on full data.
- **Q6.8** `_STATUS_BY_REPO_SQL`: the stored status per repo, for the resume rule.

## Re-running

- A plain re-run clones only repos without a finished status. When nothing is pending and the four parquet files exist, it prints `skip` and the summary.
- `--force` exports again; it never clones a finished repo again.
- `--retry-failed` clones `timeout` and `error` repos again.
- `--merge-only` does not clone; it exports the store on disk.
- Stopping the run (Ctrl-C, sleep, a crash) loses at most the repos still being cloned.

## Outputs

All under `copied_skill_history/` in the dev or full data folder, apart from `registry_history/`.

`copied_skill_history.duckdb`: the store with the three tables. There is no shards folder: 60,958 repos would mean about 180,000 shard files.

`copied_skill_history_changes.parquet`: one row per file change in a copied skill folder, with the same columns as `registry_history_changes.parquet` (`repo`, `skill_folder`, `file_path`, `commit`, `commit_date`, `author_key`, `subject`, `status`, `old_file_path`, `old_sha`, `new_sha`, `old_mode`, `new_mode`, `is_skill_md`, `is_script`, `commit_seq`, `repo_path`, `old_repo_path`, `history_role`). `commit_date` is git's ISO date with its offset; read it as UTC (`CAST(commit_date AS TIMESTAMPTZ)`, or `pd.to_datetime(..., utc=True)`). `author_key` is the SHA-1 of the lowercased email; the email is not kept.

`copied_skill_history_texts.parquet`: `sha`, `text`, one row per blob, raw UTF-8.

`copied_skill_history_status.parquet`: `repo`, `status` (`ok`, `gone`, `empty`, `too_large`, `timeout`, `error`), `message`, `head_commit`, `seconds`, `follow_renames`, `not_ours`, `after_copy`, `twin` (rows dropped because a followed rename was another skill, as in stage 3), `skill_folders` (folders with at least one change row), `changes`.

`download_targets.parquet`: `repo`, `skill_folder` for the repos of the last run (all repos unless `--limit`).

## Limits

- **Default branch only.** A copy that the dataset saw on another branch has no history here (`folders_without_history`).
- **Today's history, not the dataset's.** The clone is newer than the GitSkills snapshot. A repo deleted since then is `gone`. The folder is logged by the path the dataset saw, so for a folder moved since then the rows end at the move; edits under the new name are not kept.
- **Mirrors over 20,000 folders** are `too_large` and have no copy history.
- **Blocked repos.** A repo GitHub answers with HTTP 403 (disabled or blocked) is `error`; retrying does not help.
- **Parent targets hold nested skills.** The change rows of a target that contains other skill folders include those folders' files. Later stages that read a skill's own files keep `file_path` rows that are not inside a nested folder with its own `SKILL.md`.
- **Big texts.** A script over 200,000 bytes is not stored (stage 3 rule).
