# Registry history

Downloads the git history of every selected registry's skill folders. This is stage 3. It uses the network. [Registry selection](03_registry_selection.md) has to have written the registry list first. This step leaves that list as it is.

The dataset holds one snapshot of each skill. This step keeps every file change in each skill folder, and the text of `SKILL.md` and of small scripts, so later stages can number versions.

## Scripts

Run these from the repo root.

```bash
bash scripts/skill_genealogy/dev_mode/04_registry_history.sh
```

Mines the 30 registries in `dev_registries.parquet` and stays in the terminal. The command is `download_registry_history --dev`. The tables go to `src/data/skill_genealogy_dev/registry_history/`.

```bash
bash scripts/skill_genealogy/full_run/04_registry_history.sh
```

Mines every registry in `registries.parquet` in the background. The log is `logs/download_registry_history.log`. Follow it with `tail -f logs/download_registry_history.log`. The tables go to `src/data/skill_genealogy/registry_history/`.

```bash
bash scripts/skill_genealogy/full_run/04_registry_history_retry.sh
```

Runs `download_registry_history --retry-failed`.

```bash
bash scripts/skill_genealogy/tests/04_registry_history.sh
```

Runs the tests.

## Inputs

- `src/data/skill_genealogy/registries.parquet` for a full run
- `src/data/skill_genealogy/dev_registries.parquet` for `--dev`

Both lists are the stage 2c files. `--dev` does not look for a second copy under the dev folder. A missing list stops the run and names `python -m skill_genealogy.select_registries`.

`--limit N` keeps the first N names after sorting. `--timeout` sets the seconds for each git command. The default is 600. `--since YYYY-MM-DD` clones only history from that date on, for the repos mined in that run (see **Clone without file contents**).

## Steps

**Read the registry list.** Names are sorted and duplicates are dropped. `--dev` reads the 30-registry sample. A full run reads every registry.

**Skip finished repos.** Each repo has a shard under `registry_history/shards/`. A shard is finished when its `.status.json` says `ok`, `gone`, or `empty`. `timeout` and `error` are left alone unless `--retry-failed` is set. A missing shard, or a status file that cannot be read, is mined. The status file is written last, so a crash before it finishes is mined again.

**Clone without file contents.** For each pending repo, git clones the default branch (`--filter=blob:none`, `--no-checkout`, `--single-branch`). This is the whole history of that branch, not one commit. It brings commits and trees, and no file contents. Prompts are off. Eight repos run at a time. Each git command gets 600 seconds.

With `--since 2025-09-01` the clone adds `--shallow-since=2025-09-01`, so a large old repo downloads only recent commits and trees. Git shows the oldest commit of a shallow clone as adding every file it holds. To keep that false add out, the clone then fetches one more commit (`git fetch --deepen=1`). Every commit on or after the date then has its true changes, and the log readers drop the rows of the commits listed in `.git/shallow`, which are all older than the date. A file that existed before the date has no add row; its first row is its first change after the date. When no commit is that new, only the newest commit is cloned and the repo is `ok` with no changes. A repo whose whole history is newer than the date is not shallow, and nothing is dropped.

**List the skill folders.** It reads the default-branch tip, then lists every path whose file name is `SKILL.md` in any letter case. The parent of each path is a skill folder. A skill at the repo root uses `.`. Paths inside `node_modules/`, `.venv/`, `venv/`, `__pycache__/`, `dist/`, `build/`, and `.git/` are left out. A branch with no commits is `empty` and is not mined further.

**Find deleted skill folders.** A skill whose folder was deleted before the download is not at the tip, but its history is in the clone (bug 2 in [bugs](../bugs.md)). After the tip folders are logged, `git log --format= --name-only --no-renames -- ':(icase,glob)**/skill.md'` lists every `SKILL.md` path that existed in any commit (GIT12). Its parent folders that were not mined yet are logged by their exact path, without `--follow`, because a deleted `SKILL.md` cannot be followed from the tip. A folder is left out when it is already mined, sits inside a mined folder (its files are already in that folder's rows), is the old name of a skill whose rows were kept through a rename, is vendored, or is the repo root (a deleted root skill would mean logging every file of the repo). Each such folder is then one of three things. It is a **deleted skill** (`keep_deleted_folders`) when none of its `SKILL.md` versions is also a version in a folder that still exists, and its newest `SKILL.md` change is a delete or a rename out of the folder; it is kept as its own skill. It is a **moved skill** (`attach_moved_folders`) when one of its `SKILL.md` versions also lives in a folder that still exists; its rows are kept as `former_folder` history of that folder (the one holding its last version, else the one sharing most versions), so the skill keeps its earliest dates. In the commit of the move, the delete in the old folder and the add in the new one become one rename row (`collapse_move_rows`), and the folder's commits are numbered again by time (`renumber_commit_seq`). Otherwise git shows no delete, as after a large restructure, and the folder is not kept. A repo with more than 20,000 history-only folders (`DELETED_FOLDERS_MAX`) is not searched, because it rebuilt or moved its whole layout; the status message says so. `deleted_folders`, `moved_folders` and `history_folders_dropped` on the status row count the three groups. A deleted skill keeps its add, edit and delete rows.

**Log every file change.** When the repo has at most 200 skill folders, each folder is followed across renames. `git log --follow` walks `SKILL.md`. A second log then reads every folder name that file has lived in, so a renamed skill keeps the files it had before the rename. A repo with more than 200 skill folders skips `--follow` and reads all its folders in one log. Up to 1,000 folders are named in that log. Git compares every changed file with every named folder, so with more folders than that it is very slow: 1,000 folders inside `skills/<category>/` with 100,000 skills took up to 2 minutes per log. Above 1,000 folders, the log names their parent folders instead (at most 1,000, for example `skills/development/`), and the rows are cut back to what a log of the folders alone shows. A file outside every skill folder is dropped, a rename from outside into a skill folder is an add, and a rename out of one is a delete. `majiayu000/claude-skill-registry` (312,153 folders) takes about 1.5 minutes this way. Git still skips the search for edited renames when one commit changes too many files, so a few of those can read as a delete and an add.

One line is printed per repo: `repo status seconds changes`. Long steps inside a repo also print `progress owner/repo ...` lines, at most every 30 seconds and once at the end: `folder i/N` while following folders, the start and end of a log through parent folders, and `texts batch (of B blobs) i/N` while fetching texts in batches of 500.

**Drop history that belongs to another skill.** Following a rename also sees copies and other folders. A folder that never became this skill is dropped (`not_ours`). A folder that was copied into this skill keeps only the commits before the copy (`after_copy` counts the rows after that). A copy source with no older rows is dropped (`twin`). Kept rows are marked `own`, `former_folder`, or `copy_source`.

**Fetch the texts.** The clone has no file contents yet. It fetches the blob for each `SKILL.md` and for each script, in batches of 500. A script is a `.py`, `.sh`, `.bash`, `.js`, `.mjs`, `.ts`, `.ps1`, or `.rb` file, or any file with mode `100755`. A script larger than 200,000 bytes is left out. Texts are stored raw, as UTF-8, and are not cleaned. Each blob is stored once. The clone is then deleted.

**Record the result.** The shard stores:

- `repo`: `owner/repo`
- `status`: `ok`, `gone` (deleted, private, or an auth prompt), `empty`, `timeout`, or `error`
- `message`: the reason when a check fails, or a note when blobs were read one by one
- `head_commit`: the default-branch tip, or null when there is no commit
- `seconds`: how long the mine took
- `follow_renames`: false when the repo has more than 200 skill folders
- `history_since`: the `--since` date used for this repo, or null for the whole history
- `not_ours`, `after_copy`, `twin`: change rows dropped because another folder was not this skill
- `deleted_folders`, `moved_folders`, `history_folders_dropped`: history-only folders kept as deleted skills, attached as earlier history, or not kept; null when the history was not searched

The author address is not kept. `author_key` is the SHA-1 of the lowercased, trimmed email.

**Merge the shards.** After the pending repos finish, every shard that has a status file is joined into the three parquet files. Repos that are not on the current registry list are removed from those files. Their shard files stay on disk. A re-run prints how many of those leftover shards were left out.

## Re-running

- A plain re-run skips repos marked `ok`, `gone`, or `empty`. It also skips `timeout` and `error`.
- When the three output files already exist and nothing is pending, it prints the saved summary and skips the merge.
- `--retry-failed` mines the `timeout` and `error` repos again.
- `--force` merges again. It does not clone a finished repo again.
- `--merge-only` skips the clones and only merges the shards already on disk.
- `--deleted-folders` adds deleted skill folders to `ok` shards that were mined before that step existed. Each repo is cloned again, its history is compared with its shard, only the missing folders are logged, and their rows are added to the shard. The status file then gets `deleted_folders_checked: true`, so a re-run skips it. A failed repo keeps its old shard and is tried again. `--repos-file PATH` limits the check to the repos listed there, one per line. The merge runs at the end.
- `--since` applies only to the repos mined in that run. Finished shards keep the history they were mined with, so the `history_since` column shows which repos have a cut history.

## Outputs

All three files are under `registry_history/` in the dev or full data folder.

`registry_history_changes.parquet` — one row per file change in a skill folder:

- `repo`, `skill_folder`, `file_path`, `old_file_path`
- `commit`, `commit_date`, `commit_seq` (`1` is the oldest kept commit in that folder), `author_key`, `subject`
- `status` (the first letter of the git status, so `R100` is `R`), `old_sha`, `new_sha`, `old_mode`, `new_mode`
- `is_skill_md`, `is_script`, `repo_path`, `old_repo_path`, `history_role`

`registry_history_texts.parquet` — one row per blob: `sha`, `text`. The same blob is stored once.

`registry_history_status.parquet` — one row per repo, with the fields under **Record the result**, plus `skill_folders` and `changes`.

The run prints the status counts, skill folders, change rows, `SKILL.md` change rows, dropped-row counts, text rows, and two sample rows from each table. A repeated change key (`repo`, `skill_folder`, `commit`, `file_path`) is printed as a warning.

The queries that read these three tables are in `db/genealogy/registry_history.sql`. Run `db/genealogy/views.sql` first. The views read the full-run folder. For a `--dev` run, point them at `src/data/skill_genealogy_dev/registry_history/`.
