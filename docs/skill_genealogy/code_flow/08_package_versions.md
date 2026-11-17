# Package versions and folder manifests

Stage 4b. A registry skill is a package: `SKILL.md` plus the other files in its folder (scripts, references, config, assets). [Registry lineages](07_registry_lineages.md) (stage 4a) numbers a new version only when the `SKILL.md` text changes, but a script can change while the text stays the same. This stage numbers **package versions**, a new one whenever the folder's files or the text version change, and stores the folder listing (the *manifest*) of each one. It does not use the network. Card G11.

The code is steps 8–10 of `build_registry_lineages.py`, the same module as stage 4a. Stage 4a must have run first.

## Scripts

Run these from the repo root.

```bash
bash scripts/skill_genealogy/dev_mode/08_package_versions.sh
```

Steps 8–10 only (`build_registry_lineages --dev --packages-only --force`), from the dev lineage tables. It reads the full stage 3 changes table, limited to the dev lineages, and writes to `src/data/skill_genealogy_dev/`. It takes about 4 seconds.

```bash
bash scripts/skill_genealogy/full_run/08_package_versions.sh
```

Steps 8–10 on the full lineage tables, in the background, with `caffeinate`. The log is `logs/build_package_versions_<date>.log`.

```bash
bash scripts/skill_genealogy/tests/08_package_versions.sh
```

Runs the tests and doctests of `build_registry_lineages.py` (stages 4a and 4b share the file).

**How the two stages run together.** `build_registry_lineages` without `--packages-only` runs stage 4a, then stage 4b. Each part skips when its own outputs exist. A stage 4a rebuild always rebuilds stage 4b too, because the package versions point to its `version_seq`. `--packages-only` skips stage 4a, so a 4b fix does not repeat the 3-hour 4a run.

## Inputs

- `lineages.parquet` and `lineage_versions.parquet` from stage 4a, in the dev or full data folder. Only kept (English, non-copier) lineages are in them, so nothing is filtered again here.
- `registry_history/registry_history_changes.parquet` and `registry_history_texts.parquet` (stage 3), always from the full data folder.

## Size

The kept lineages have about 4.4 million `own` and `former_folder` rows (`SKILL.md` and folder files). Query Q4.3 counts them per registry, and the registries are split into batches of about `PACKAGE_ROW_BATCH` (400,000) rows. A registry is never split. Each batch is read (Q4.4), replayed, numbered and written, then freed. Manifest rows are appended to one file batch by batch.

## Steps

**Step 8: Rebuild the folder manifests (Algorithm A21).** For each lineage, walk its commits and keep the folder's file listing after each commit. See the card below.

**Step 9: Number the package versions (Algorithm A21).** A new `package_version_seq` at each commit where the manifest or the text version changes. Record what changed. Store the manifest only at package versions.

**Step 10: Summary.** The run prints the counts listed below, two package versions of the lineage with the most, and two manifest rows of its newest package version.

## Algorithm A21: manifests and package versions

**Replay.** Rows of one lineage are ordered by UTC commit time, then `commit_seq`; inside one commit, deletes go first, then renames, then adds and edits (so a delete plus an add of the same path keeps the new file). Each row updates a running map `repo_path -> (sha, mode)`:

- `A`, `M`, `T` (type change) set the path;
- `D` removes it;
- `R` removes `old_repo_path` and sets `repo_path`.

The map is keyed by the **full repo path**, never by `file_path`. Since stage 3 attaches moved folders, a skill can live in two places at once (parallel copies), and both copies have the same relative paths. Keyed by the full path, deleting the old copy's `scripts/a.py` does not remove the own copy's `scripts/a.py`. `copy_source` rows are the history of another skill and are never replayed.

**Which folder is the skill.** The skill folder after a commit is the folder of the `SKILL.md` text row that A20 reads: in a commit with several, the `own` row first, else the `former_folder` row whose `repo_path` comes first alphabetically (`skill_folder_after_commit`). A commit without a `SKILL.md` text row keeps the folder. The folder comes from `repo_path`, not from the HEAD folder (the lineage's `skill_folder`), because the skill was not always there:

- stage 3 can file a skill's older history at the repo root as `own` rows (`repo_path` is `SKILL.md`), for example `aboul3ata/lazyweb-skill` before it moved to `skills/lazyweb`;
- a skill can move to another folder (a `former_folder` rename) and come back later, for example `TentacleOpera/switchboard` `.agents/skills/switchboard-contracts`.

**The manifest** is every path under that folder, relative to it, except:

- the folder's own `SKILL.md`, in any letter case: it is the text version. A nested `cn/SKILL.md` stays, as a reference file;
- paths inside `SKIPPED_FOLDERS` (`node_modules/`, `.venv/`, `venv/`, `__pycache__/`, `dist/`, `build/`, `.git/`);
- at a **repo-root skill** (`skill_folder` is `.`), every path outside `ROOT_SKILL_PACKAGE_FOLDERS` (`scripts/`, `references/`, `assets/`). Without this rule the whole repository would be the package: the 75 root lineages have 1.07 million file rows, 395,000 of them in `github/gh-aw`.

Each manifest row has `path`, `sha`, `mode`, `size` and `file_category` (`script`, `reference`, `config` or `asset`, from `compare_texts.file_category`: a script by extension or mode `100755`).

**Numbering.** `version_seq` after a commit is the text version whose first commit is the newest one seen so far (from `lineage_versions.parquet`). A commit becomes a package version when all of these hold:

1. text version 1 has been reached (files added before the first `SKILL.md` join package version 1);
2. the skill folder has its `SKILL.md` after the commit (a removed skill has no package version at or after its delete);
3. the manifest or `version_seq` differs from the previous package version.

`changed_skill_md` is True when `version_seq` changed. `changed_scripts` and `changed_other_files` are True when a script, or another file, was added, removed, or got a new sha or mode. A rename is a remove plus an add. Version 1 compares with an empty folder.

**Worked example** (the test `test_folder_edits_make_five_package_versions`):

```text
commit  change                                   pkg  text  manifest                              changed
c1      A SKILL.md, A references/x.md             1    1     x.md                                  skill_md, other
c2      A scripts/a.py                            2    1     x.md, a.py                            scripts
c3      M scripts/a.py                            3    1     x.md, a.py (new sha)                  scripts
c4      R scripts/a.py -> scripts/b.py            4    1     x.md, b.py                            scripts
c5      D references/x.md                         5    1     b.py                                  other
```

A commit that only adds `node_modules/left-pad/index.js` makes no package version. A whitespace-only `SKILL.md` edit makes none either: A20 gives it no new `version_seq`, and `SKILL.md` is not in the manifest.

## Queries

- **Q4.3** `FOLDER_ROW_COUNTS_SQL`: own and former-folder rows per registry, for the kept lineages in the registered table `kept_lineages`. Sizes the batches.
- **Q4.4** `FOLDER_CHANGES_SQL`: every own and former-folder row (`SKILL.md` and folder files) of the kept lineages of the registries in `batch_repos`.
- **Q4.5** `BLOB_SIZES_SQL`: byte length of the stored text of the blobs in `wanted_blobs`.

## Outputs

`lineage_package_versions.parquet`, one row per package version:

- `lineage_id`, `package_version_seq` (1, 2, … per lineage), `version_seq` (the `SKILL.md` text version at that point)
- `commit`, `commit_date` (UTC)
- `changed_skill_md`, `changed_scripts`, `changed_other_files`
- `file_count`, `script_count` (manifest files, `SKILL.md` not counted)

`lineage_folder_manifests.parquet`, one row per file per package version: `lineage_id`, `package_version_seq`, `path` (relative to the skill folder), `sha`, `mode`, `size`, `file_category`. A package version with no folder file has no manifest row.

## Summary

The run prints: lineages and root lineages, change rows read and batches, lineages with package versions, package versions and text versions, package versions per lineage (median, maximum), lineages with more package versions than text versions, text versions whose commit has no package version (expected 0), how many package versions changed the text, scripts or other files and how many changed only folder files, lineages with folder files and with scripts in their newest package version (count and share) and ever, manifest rows, rows with a size, and rows per `file_category`.

## Limits

- **`size` is known only for stored texts.** `git log --raw` gives the blob id and mode, not the size, and this stage uses no network. Stage 3 stored the text of `SKILL.md` files and of scripts up to `MAX_SCRIPT_BYTES`, so `size` is the byte length of that stored text (98% of script blobs) and null for most other files.
- A repo-root skill counts only `scripts/`, `references/` and `assets/`. A root skill that keeps its files elsewhere (for example `src/`) shows fewer files than it has.
- A skill folder that holds another skill (`skills/a` and `skills/a/sub/SKILL.md`) lists the inner skill's files too: they are in its folder.
- The folder follows the `SKILL.md` that A20 reads. A commit that edits only a parallel copy's `SKILL.md` moves the folder to that copy until the next own edit, so its files can show up as a folder change.
- Two renames in one commit that swap names (`a -> b`, `b -> a`) are applied one after the other, so one file can be lost. Git rarely reports that.

The full-run numbers are in the [package versions report](../report/08_package_versions_report.md).
