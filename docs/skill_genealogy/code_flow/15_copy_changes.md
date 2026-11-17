# Copy changes

Stages 9a, 9b and 9c. [Copy families](13_copy_families.md) (stage 7) gave every dated copy an origin, one registry version. This stage answers RQ2: is a copy left as it was, or changed, and are its changes the kind a registry makes itself? Part `origin` (9a, card G19) compares each copy's current text with its origin, checks whether the headings the copy added are headings the registry added later (A38), labels each edit inside the copy's own history `sync`, `other_source` or `local` (A39), and puts four kinds of change side by side in a profile. Part `versions` (9b, card G20) scores every version of every copy against four texts chosen by date (A40) and lists the notable lines each step added (A41); the drift chart, the version player and stage 10 read these scores and do not recompute them. Part `folders` (9c, card G21) does the same for the rest of the skill folder: it rebuilds each copy version's folder (A42), compares it with the copy's previous folder and with the registry package current at the origin, at the version's date and today (A43), flags what added or edited scripts can do (A16), and counts scripts the `SKILL.md` names but the folder lacks, or the reverse (A17). All parts read saved tables only; no network.

The code is `src/skill_genealogy/measure_copy_changes.py`. A label describes a text. It does not say who typed it: a `local` edit is "not a registry version", not "a person wrote this". A risk flag says what a script can do, not that it is malicious.

## Scripts

```bash
bash scripts/skill_genealogy/dev_mode/15_copy_changes.sh [--force]
bash scripts/skill_genealogy/full_run/15_copy_changes.sh [--force]
bash scripts/skill_genealogy/tests/15_copy_changes.sh
```

Dev runs in the foreground, log in `logs/measure_copy_changes_dev_<date>.log`. Full runs in the background with `caffeinate`. Without `--only` the three parts run in order, `origin`, `versions`, then `folders`, and each skips when its outputs exist. `--only origin`, `--only versions` or `--only folders` runs one part (`bash scripts/skill_genealogy/dev_mode/15_copy_changes.sh --only folders`). `folders` reads the `versions` output, so it runs after it. `--force` rebuilds and replaces a part's files only after the new build has finished, so a failed run keeps the old files.

## Inputs

- Dev or full data folder: `copy_files.parquet`, `families.parquet` (stage 7), `lineage_versions.parquet`, `lineage_changes.parquet` (4a), `copied_skill_history/` (6).
- Shared full folder: `skill_language.parquet` (2a), `registry_history/registry_history_texts.parquet` (3).
- GitSkills `artifacts` (`file_sha`, `content`, and for 9b `last_commit_at`), only for a copy with no stored history text.
- 9c also: `copy_version_similarity.parquet` (9b), `lineage_package_versions.parquet` and `lineage_folder_manifests.parquet` (4b), and GitSkills `artifact_siblings` (the file listing of a copy folder with no history).

A missing file names the command that builds it.

## Steps of part `origin` (9a)

**Step 1: The copies.** A copy file is compared when it has an origin version (`origin_seq`), was not copied before the registry release (`copied_before_release`), its text is English, and it is not a registry's own file (`registry_self`). The copy histories come from stage 7's `read_copy_histories`, so version numbers match stage 7, and every row carries the copy's `lineage_id`.

**Step 2: The current text.** The newest history version of the file; when it has no stored text, the dataset text of `file_sha` (query Q9.3), never an older history row. `current_basis` says which one was used. A copy with neither text has no row; the run counts them.

**Step 3: Copy against origin.** `compare_texts(origin, current, repo, path)` gives the body change (`unchanged`, `cosmetic` or `substantive`, with 5% of the old body lines as the line) and what was added. `compare_front_matter` compares `name`, `description` and `allowed-tools` on their own, so a permission change shows even when the body is unchanged. `customized` is true when a project name, script mention, step, command or heading was added. A removed step is not customisation. The project name must be added to the body (`body_names_added`, front matter left out): a name added only as a front-matter key, with the body unchanged, is not customisation. `names_added` in the output still reads the whole file. Heading overlap is algorithm A38.

**Step 4: Edits inside a history.** Algorithm A39. Files are processed in batches (`COPY_CHANGE_FILE_BATCH`, never splitting a history), and each batch loads only its own texts (query Q9.1).

**Step 5: The profile.** Four populations with the same shares: `registry_update` (a registry's own version-to-version change, only for lineages that have copies), `copy_local_edit`, `copy_sync` and `copy_vs_origin`. `other_source` edits are not a population.

**Step 6: Save and print.** Sorted files; the run prints the counts, the shares, a table by link kind, and two sample records of each output.

## Algorithm A38: heading overlap with the registry

**Goal.** Tell whether a copy moves the same way the registry moves.

**Rule.** The set of headings the copy added against its origin, and the set of headings the registry added in versions after the origin (`lineage_changes.headings_added` where `to_seq` is greater than `origin_seq`). The score is the number of shared headings divided by the number of all headings. Headings are trimmed and case-folded, and each counts once. A copy that added no heading is null, whatever the registry added. A copy that added headings while the registry added none is 0.

**Why.** A copy that adds `## Deploy` while the registry also adds `## Deploy` later was heading the same way. The update that made the origin is not later, because the copy already has it. A copy that added no heading did not move, so it has no direction to compare; scoring it 0 would read "left as it was" as "went the other way". Most copies add no heading while their registry does, so those zeros would swamp the mean: on full data, 578,186 of 643,732 scored copies were such zeros, and the mean was 0.0099 instead of 0.097 over the copies that added a heading. With the null rule the mean is over copies that added headings only.

## Algorithm A39: the label of an edit

**Goal.** Separate an edit that re-syncs with the registry from one that is the copier's own.

**Rule.** Take the copy's history rows, drop deletes, and pair the remaining rows of a file in date order, inherited rows included. A pair whose new commit is inherited (a registry history also holds it) is left out. Inherited rows stay in the pairing so that a fork's first own edit is compared with the inherited text it changed. A pair with the same blob is not an edit. A pair whose texts differ only in spaces, line endings, quotes or blank lines (the same `normalized_sha`, algorithm A20 of stage 4a) is not an edit either, and is counted as `edits_left_out_whitespace_only`. A pair whose text is not stored is left out and counted. The new blob is `sync` when it is a version of the copy's own lineage, `other_source` when it is a version of another lineage, `local` otherwise. Each edit also has its `TextChange` and `permissions_changed`.

**Why.** Blob equality is exact, so a rewrite that equals a registry version byte for byte is a sync and a near match is not. That makes `local` mean "not a known registry text", which is the claim the data supports.

## Steps of part `versions` (9b)

**Step 1: The copies.** The same copies as 9a (`select_copies`), with the same histories (`read_copy_histories`).

**Step 2: Targets and registry texts.** The registry versions of each lineage are indexed by release date (`index_lineage_releases`). The registry texts every history row can need (origin, newest, at-date of the row's date) are read once (Q9.1).

**Step 3: The versions of each copy** (`list_copy_versions`). A copy's versions are its history rows in the order A39 pairs them (date, then `commit_seq`), deletes left out. A row is not a new version when it has the same blob as the last kept version, or the same `normalized_sha` (a whitespace-only change, counted as `history_rows_left_out_whitespace_only`), or when its text is not stored (`history_rows_left_out_no_text`). Versions are numbered 1, 2, ... in that order; this `version_seq` is the step number, not stage 7's blob number. Inherited rows (a fork's commits that a registry history also holds) stay, so each step is the same pair as an A39 edit. A copy with no kept history version gets one row: the dataset text of `file_sha`, dated `last_commit_at` of its own dataset row (Q9.4), `basis = dataset`. Copies with no stored history text are batched apart from the others, as in 9a.

**Step 4: Scores and notes.** For each version: the four targets (A40), the six scores against each (`score_pair`), and the change notes of the step from the previous version (A41). A target whose text is missing has null scores. Each pair of blobs is scored once per batch (`PairScorer`), because many copies are the same text as their origin.

**Step 5: Save and print.** `copy_version_similarity.parquet`, sorted by `repo, path, version_seq`. The run prints the counts, the version kinds, the drift, the share behind at-date, the note kinds and two sample records.

### The scores of a pair

`score_pair(target_text, copy_text)` gives six numbers. The target is the older side: the previous version, or a registry text. Three rules come first:

1. **Same text, full marks.** The same blob (or the same text) scores 1, 1, 1, 0, 0, 0 without a comparison (`IDENTICAL_SCORES`).
2. **The whole body is compared.** No boilerplate line is removed (see the design note).
3. **Nothing to measure is null, not 0.** `jaccard` is null when neither body has a word, `containment` when the target body has none, `verified` and `body_change_pct` when the target has no body line. A copy that deleted its whole body is measurable and scores 0.

| Score | Rule |
|---|---|
| `jaccard` | Exact Jaccard of the 5-word shingle sets of the two bodies (A4, A6). A body under 5 words is one shingle. |
| `containment` | Share of the target's shingles the copy holds (A7). |
| `verified` | Share of the target's body lines the copy kept unchanged (A10). |
| `lines_added`, `lines_removed` | Body lines from the line diff (A9). |
| `body_change_pct` | (added + removed) / target lines * 100, as in `compare_texts`. |

One line diff gives `verified` and the line counts, so they equal `compare_texts` and `share_of_registry_lines_kept` on the same pair. Exact sets are used, not MinHash: the texts are small, and an estimate would differ from the numbers other stages show.

## Algorithm A40: the four comparison targets

**Goal.** Score a copy version against the texts a reader asks about: what it was a moment ago, what it started from, what the registry offered that day, and what the registry offers now.

**Rule.** For a copy version with date `d` and step number `n`:

| Target | Text |
|---|---|
| `prev` | The copy's version `n - 1`; none for version 1. |
| `origin` | The registry version stage 7 picked (`origin_seq`), the same for every version of the copy. |
| `at_date` | The lineage version with the latest `release_date` on or before `d` (then the highest `version_seq`); none when nothing was released by `d` or `d` is missing. |
| `newest` | The lineage's highest `version_seq`. |

```text
registry   v1 ─────────── v2 ──────────────────── v3 ──────────
           10 Jan         1 Mar                   15 May
copy           c1 ───────────── c2 ───── c3 ───────────────── c4
               1 Feb            10 Mar   20 Apr               30 Jun

c1: prev none  origin v1  at_date v1  newest v3
c2: prev c1    origin v1  at_date v2  newest v3
c3: prev c2    origin v1  at_date v2  newest v3
c4: prev c3    origin v1  at_date v3  newest v3
```

**Why.** One target cannot answer all questions. `origin` measures what the copier changed (RQ2), `at_date` whether the copy kept up with the registry at that time, `newest` how far it is from today's registry (RQ3), and `prev` how big each step was. `pick_comparison_targets` is the plain rule; the run uses `targets_from_index`, a binary search over release dates stored as nanoseconds, and a test checks that both agree. An `exact` copy can predate the release it matches (stage 8), so `at_date` can be null while `origin` is set; a null `at_date` is not "behind".

## Algorithm A41: change notes of a step

**Goal.** Tell a reader of the version player what a step added that matters, without reading the diff.

**Rule.** Diff the previous version's body with this version's body (A9). Each added body line gets at most one note, of the first kind that matches in this order:

| Kind | Added line |
|---|---|
| `network` | Has a URL, `curl` or `wget`; the note text starts with `⚠`. |
| `script` | Mentions a script path (`find_script_mentions`). |
| `command` | Is inside a bash/sh/shell fence, or starts with `$ ` outside a fence. |
| `heading` | Is a markdown heading outside a fence. |
| `name` | Has a project word of the repo or path that the previous body did not have (A11, `body_names_added`, as in 9a's `customized`). |

A changed `allowed-tools` adds one `permission` note on the line of that key (line 1 when it was removed). `line` is 1-based in the normalised new text, front matter included. The notes are stored as a JSON list of `{line, kind, text}`; version 1 has `[]`.

**Why.** These are the additions that change what an agent does or reaches: network access, scripts, commands, new sections, the copier's own project. One kind per line keeps the counts additive; network comes first because it is the risk a reader must not miss. An ordinary sentence edit has scores and no note.

## Steps of part `folders` (9c)

**Step 1: The versions and their commits.** The copy versions are the rows of `copy_version_similarity.parquet` (9b), so 9c has exactly the versions 9b scored. 9b keeps a version's blob and date but not its commit. `version_commits` walks the copy's history rows (stage 7's `read_copy_histories`) in the A39 order next to its versions and gives each history version the first row at or after the previous match with the same blob and date.

**Step 2: The copy folder of each version.** A history version's folder is algorithm A42. A dataset version has no history, so its folder is the dataset's file listing of the `SKILL.md` folder (`artifact_siblings`, `entry_type = 'file'`, query Q9.7), `folder_source = dataset_snapshot`. GitSkills listed only some folders (`artifacts.composition_fetched = 1`, query Q9.10). A listed folder with no sibling row holds only its `SKILL.md`, so it is an empty folder. A folder that was never listed is unknown: the version has no folder and no comparison rows, and the run counts it as `versions_without_folder`. Reading it as empty would show every registry file as removed. The listing's `entry_sha` is the git blob id, so its files match registry files by sha. It has no file mode, so a snapshot script is found by its extension only. The A21 package rule applies to both: vendored folders and the folder's own `SKILL.md` are left out, and a repo-root skill keeps only `scripts/`, `references/` and `assets/`. A file's size is the byte length of its stored text (query Q9.6), or the listing's `entry_size`; a file with no stored text has no size.

**Step 3: The registry targets.** Algorithm A43 gives the package version of `origin`, `at_date` and `newest`; `prev` is the copy's own previous folder. The files of every package version any version needs are read once (query Q9.8).

**Step 4: Compare.** `compare_folders` (A15) with the target as the old side and the copy as the new side. A file is `same`, `modified`, `renamed` (a sha that moved), `added` (only in the copy) or `removed` (only in the target). Only differing files are stored; a `same` file is stored only when its mode changed. A pair with no target folder (`prev` of version 1, a date older than every package) is not compared. Copies are processed in batches of `COPY_CHANGE_FILE_BATCH`, and a pair of folders that occurs twice in a batch is compared once.

**Step 5: Risk flags and mentions.** Every added, modified or renamed script gets `risk_flags`, the A16 flags of its new text, and `new_risk_flags`, the flags the target's file did not have. An added script has no target file, so all its flags are new. Text is looked up by sha in the copied-skill history, the registry history, then the dataset listing (Q9.1, Q9.9), in chunks of `SCRIPT_TEXT_BATCH` shas; only the flags are kept. A script with no text, or over `MAX_SCRIPT_BYTES`, has null flags; a modified script whose old text is missing has null `new_risk_flags`. Null means unknown, not safe. `check_script_mentions` (A17) on the version's `SKILL.md` text and its folder gives `missing_scripts` (named in the text, absent from the folder) and `hidden_scripts` (in the folder, not named).

**Step 6: Summary.** One row per compared pair, with the counts of each file result, scripts added and modified, `new_risky_scripts` (added or modified scripts with a non-empty `new_risk_flags`), the version's missing and hidden script counts and its `folder_source`. A pair with the same two folders has a row of zeros, so a share over the summary is a share of compared pairs.

**Step 7: Save and print.** Sorted files; the run prints the counts, the headline numbers read at each copy's last version against its origin package, and two sample records of each output.

## Algorithm A42: the folder of a copy version

**Goal.** Know which files sat next to the copy's `SKILL.md` at each version, with the same rules stage 4b uses for the registry package.

**Rule.** The copy folder's stage 6 rows (`own` and `former_folder`, query Q9.5) are replayed with the A21 walk: in commit order (UTC date, then `commit_seq`), deletes before renames before the rest; `A`, `M` and `T` set a path, `D` drops it, `R` moves it; the skill folder follows the folder of the `SKILL.md` text row; the manifest is the files under that folder, relative to it, with the A21 package rule. A version's folder is the folder after its own commit. The last version of a copy takes the folder after the folder's newest commit instead, as long as its `SKILL.md` is still there; a copy whose `SKILL.md` was deleted keeps the folder of its last version's commit.

**Why.** The walk reuses the stage 4b helpers, and a test checks it gives the same folder as `rebuild_folder_manifests` after every commit, so a copy folder and a registry package are built the same way. The last version runs to the newest commit because a copier often adds or edits a script without touching `SKILL.md`; on full data 24,884 copy folders have file changes after their last `SKILL.md` commit, and those changes would be lost if the last version kept the folder of its own commit. Only the folders at the version commits are built, so a large folder with many commits is not rebuilt after every commit.

## Algorithm A43: the registry package of a target

**Goal.** Compare a copy folder with the registry folder that existed at the right moment.

**Rule.** The lineage's package versions (stage 4b) are sorted by commit date, then package number.

| Target | Package version |
|---|---|
| `origin` | Among the package versions whose `SKILL.md` is the origin text version (`version_seq = origin_seq`), the newest one committed on or before the copy's first version; the first one when the copy date is missing or older than all of them. |
| `at_date` | The newest package version committed on or before the version's date (`package_at_date`); none when the date is missing or older than every package version. |
| `newest` | The lineage's last package version. |

Two package versions with the same date keep the higher number.

**Why.** A registry can add a script after a release without editing `SKILL.md`. A copy made after that took the script, so comparing it with the folder at the release would count the registry's script as added by the copier. Keeping the origin inside the origin text version keeps 9c's origin the version 9a and 9b compare with. A date older than every package gives no `at_date` row rather than an empty folder, because an empty folder would show every copy file as added. `package_at_date` is a binary search over the sorted dates; `registry_manifest_for` is the plain form on the two stage 4b tables, used in the tests.

## Outputs

`copy_folder_changes.parquet` (9c), one row per file that differs between a copy version's folder and a target folder: `repo`, `path`, `version_seq`, `target` (`prev`, `origin`, `at_date`, `newest`), `file_path` (the copy's path; the target's path for a removed file), `old_file_path` (the target's path of a renamed file), `result` (`added`, `removed`, `modified`, `renamed`, or `same` with a changed mode), `old_sha`, `new_sha`, `mode_changed`, `category` (`script`, `reference`, `config`, `asset`), `size_change` (bytes, null when a size is unknown), `risk_flags` and `new_risk_flags` (lists, only for added, modified and renamed scripts; null when the text is missing).

`copy_version_folder_summary.parquet` (9c), one row per compared copy version and target: `repo`, `path`, `version_seq`, `target`, `target_package_seq` (null for `prev`), `files_added`, `files_removed`, `files_modified`, `files_renamed`, `scripts_added`, `scripts_modified`, `new_risky_scripts`, `missing_scripts`, `hidden_scripts` (null when the `SKILL.md` text is not stored), `folder_source` (`history` or `dataset_snapshot`). It is a file of its own so that 9b's file is not rewritten.

`copy_version_similarity.parquet` (9b), one row per copy version: `repo`, `path`, `lineage_id`, `link`, `origin_seq`, `basis` (`history` or `dataset`), `version_seq`, `commit_date`, `blob`, `change_kind` (`copy` for version 1, `inherited` for a registry-history commit, else the A39 label `sync`, `other_source` or `local`), then for each target `prev`, `origin`, `at_date`, `newest` the six scores `<target>_jaccard`, `_containment`, `_verified`, `_lines_added`, `_lines_removed`, `_body_change_pct`, plus `at_date_seq` and `newest_seq`, and `change_notes` (JSON).

`copy_changes.parquet`, one row per compared copy file: `repo`, `path`, `lineage_id`, `link`, `origin_seq`, `current_basis` (`latest_history` or `dataset`), every `TextChange` field (`lines_added`, `lines_removed`, `body_change_pct`, `share_of_old_lines_kept`, `headings_added`, `headings_removed`, `commands_added`, `scripts_mentioned_added`, `network_refs_added`, `steps_added`, `names_added`, `state`), every front-matter field (`name_changed`, `description_changed`, `fm_keys_added`, `fm_keys_removed`, `fm_keys_changed`, `allowed_tools_added`, `allowed_tools_removed`, `permissions_changed`), `customized`, `heading_overlap`.

`copy_history_edits.parquet`, one row per edit: `repo`, `path`, `lineage_id`, `from_blob`, `to_blob`, `commit`, `commit_date`, `change_kind`, `inherited` (always false: an edit whose commit is inherited is left out), the `TextChange` fields and `permissions_changed`.

`change_profile.parquet`, one row per population: `population`, `changes`, `share_substantive`, `share_headings_added`, `share_commands_added`, `share_network_added`, `share_scripts_added`, `share_permissions_changed`. An empty population has 0 changes and null shares.

## Settings

`COPY_CHANGE_FILE_BATCH` (20,000) in `settings.py`, for all three parts. Part `folders` adds `SCRIPT_TEXT_BATCH` (50,000 script shas per text read, a memory bound) and uses `MAX_SCRIPT_BYTES` (200,000: a larger script is compared by sha only and has null flags), `SCRIPT_RISK_PATTERNS` (A16), `SKIPPED_FOLDERS` and `ROOT_SKILL_PACKAGE_FOLDERS` (A21). Part `versions` also uses `SHINGLE_WORDS` (5); it adds no constant. The body threshold `SUBSTANTIVE_BODY_CHANGE_PCT` (5) is used inside `compare_texts`. DuckDB uses `HISTORY_DUCKDB_MEMORY_LIMIT` for the text queries and `DUCKDB_MEMORY_LIMIT` for the dataset text.

## Queries

**Q9.1.** The stored text of the wanted shas in a text parquet (registry or copied-skill history), joined with a `SEMI JOIN` to a registered table of shas.

**Q9.2.** Which of the wanted shas have a stored, non-null text.

**Q9.3.** The dataset `content` of the wanted `file_sha`, for copies with no stored history text. Copies that need it are batched apart from the rest, and the text is read per batch. One read for all of them would hold several GB (about a fifth of the 1.3 million full copies have no stored history text), and a read of the dataset for every batch of the others would be wasted.

**Q9.4.** For 9b: the `last_commit_at` of each wanted copy file from its own dataset row (`repo_full_name`, `path`, `file_sha`), and its text from any dataset row with the same blob. The file's own row can lack the `content` that another repo's row of the same blob has, as Q9.3 already allows by joining on `file_sha` alone.

**Q9.5.** For 9c: every `own` and `former_folder` change row (`SKILL.md` and folder files) of the copy folders of one batch, from `copied_skill_history_changes`. `copy_source` rows are the source repo's history and are left out, as in A21.

**Q9.6.** For 9c: the byte length of every stored copied-skill text, read once, for the file sizes of the copy folders.

**Q9.7.** For 9c: the dataset file listing (`artifact_siblings`, files only) of the `SKILL.md` folders of dataset versions.

**Q9.8.** For 9c: the files of the wanted package versions from `lineage_folder_manifests`.

**Q9.10.** For 9c: the wanted `SKILL.md` files whose folder GitSkills listed (`artifacts.composition_fetched = 1`). `artifact_siblings` has rows only for listed folders that hold other files, so a missing listing and an empty folder look the same there.

**Q9.9.** For 9c: script text from the dataset file listing (`content`), for snapshot scripts whose sha is in neither history.

## Reading the result

The `link` column matters for every share. A `cluster` link is a loose text match (stage 7), so its copies differ from the origin far more than an `exact` or `near` link, and they are most of the copies. The run prints the shares by link kind. A headline share over all copies mixes a copy that is the skill with a copy that is only like it.

## Tests

`tests/skill_genealogy/test_measure_copy_changes.py`, hand-made texts, no dataset.

| Test | What it proves |
|---|---|
| Unchanged, front-matter only, cosmetic, substantive | State, customisation and permission flag follow the rules |
| Removed step | Not customisation |
| Project name added only in front matter | In `names_added`, not customisation |
| Missing text | A copy without an origin or current text has no row |
| Heading overlap | 0.5, duplicates and case count once, registry side empty is 0, copy side empty is null |
| Later headings | The update that made the origin is not later |
| Current text | Newest history, then dataset, then missing |
| History edits | Local then sync; another lineage is `other_source`; inherited, same blob, whitespace only, deleted and unstored are not edits; a fork's first own edit starts from the inherited text |
| Profile | Hand-checked shares; `other_source` is not a population; empty population has null shares |
| Eligible copies | Early, originless, non-English and registry-only files are left out |
| Null text | A null text cell is a missing text; Q9.1 and Q9.2 skip it |
| Dataset batches | A copy whose newest blob has no stored text is batched with the dataset reads |
| Save | A failed write keeps the old files and leaves no temporary file |
| A40 timeline | c1–c4 get the targets of the diagram; a date before every release, or none, has no at-date; the fast lookup agrees with the plain one on microsecond dates |
| Versions | Whitespace-only steps and deletes are not versions; an out-of-order history is numbered by date; a copy with no history is one dataset row; a copy with no text at all has none |
| Scores | Identical text is 1, 1, 1, 0, 0, 0 even with no body; line counts and `verified` equal `compare_texts`; a line many owners share still counts; an empty target gives null, a deleted body 0; a one-line edit of a 25-line text keeps Jaccard above 0.8 |
| Copy without history | One row, `prev_*` null, notes `[]`, kind `copy`; a later local step scores against the previous version |
| A41 | `## Deploy` and `curl https://x` are notes on lines 2 and 3; a fenced heading is not; line numbers count the front matter; permission, command and name notes |
| Summary | Drift and share behind at-date on hand-made rows |
| 9c: modified and added script | Registry `scripts/a.py`; the copy edits it to call `curl` and adds `scripts/deploy.sh`: `modified` with `new_risk_flags` `[network]` and size change 20, `added`; summary 1 added, 1 modified, 1 new risky script; version 1 has no `prev` row |
| 9c: unknown old text | A modified script whose old text is missing keeps `risk_flags`, has null `new_risk_flags` and is not counted risky; a script over `MAX_SCRIPT_BYTES` has null flags |
| 9c: dataset snapshot | A copy with no history takes the sibling listing; `SKILL.md` and `node_modules/` are dropped, mode is null; a root skill keeps only package folders; a listed folder with no siblings is empty, an unlisted one has no folder |
| 9c: A43 | Package current at a date: none before the first, p1 on its own date, p2 between; the origin of a copy made after a script-only package is that package; an undated copy starts from the first package of its origin version; same-date packages keep the higher number |
| 9c: A42 | The copy walk gives the same folder as `rebuild_folder_manifests` after every commit (add, vendored file, edit, rename, delete); the last version keeps a script added after its commit; a deleted copy keeps the folder of its own commit; a returning text (A -> B -> A) gets the later commit |
| 9c: summary | Same folders give a row of zeros; `prev` compares two versions; missing and hidden scripts; the headline reads the last version; save replaces both files |

## Limits

- A label describes the text, not the author. `local` means "not a registry version".
- The origin is the version stage 7 picked. A copy with a loose `cluster` link may be only similar to it, so a high substantive share there is partly the link, not the copier.
- A history shows only commits that touched the skill folder; a copy edited in a way git did not record has no edit.
- Heading overlap is exact text; a renamed heading counts as a different one.
- 9b: a copy version is dated by its commit; an inherited commit carries the registry's date, not the date the fork took it. A dataset version has the dataset's `last_commit_at`, which GitSkills leaves empty for some files; those versions have no at-date.
- 9b: a history row whose text is not stored is not a version, so the next step is scored against the last stored version.
