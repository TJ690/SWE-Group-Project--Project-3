# Copy families

Stage 7a. Stage 5c linked each dataset text to a registry lineage by its **current** text. A copy's own git history (stage 6) is stronger evidence: a history that passes through a registry blob proves the copy, even when the current text has drifted away. This stage adds those history links, keeps the strongest link of each copy file, picks the registry version the copy started from **by date**, records when and in which batch each copy arrived in its repo, and gives every English text one family. It reads saved tables only; no network. Card G16.

The code is `src/skill_genealogy/assign_families.py`. It reuses the stage 5 search code in `find_copies.py` (the LSH band index, the near-link choice, the signer) and the folder rule of stage 6 (`skill_folder_of`).

## Scripts

Run these from the repo root.

```bash
bash scripts/skill_genealogy/dev_mode/13_copy_families.sh [--force]
```

`assign_families --dev` in the foreground, log in `logs/assign_families_dev_<date>.log`. Needs the dev stage 5c tables (`11_dataset_copies.sh`) and the dev copy history (`12_copied_skill_history.sh`). Families cover every English dataset text, also in dev, so it takes several minutes.

```bash
bash scripts/skill_genealogy/full_run/13_copy_families.sh [--force]
```

`assign_families` in the background with `caffeinate`, log in `logs/assign_families_<date>.log`. The stage skips when its four outputs exist; `--force` deletes them first.

```bash
bash scripts/skill_genealogy/tests/13_copy_families.sh
```

Runs `tests/skill_genealogy/test_assign_families.py` and the doctests of `assign_families.py`.

## Inputs

- Stage 5c, dev or full data folder: `copy_candidates.parquet` (one row per copy file with its text link), `copier_seen.parquet` (unlinked texts a copier holds). `text_links.parquet` is checked to exist.
- Stage 4a and 5b, dev or full data folder: `lineage_versions.parquet`, `lineages.parquet`, `lineage_roots.parquet`, `repo_roles_final.parquet`; stage 5a `registry_signatures.npz`.
- Stage 6, dev or full data folder: `copied_skill_history/` (changes, texts, status).
- Full data folder: `skill_language.parquet` (2a), `registry_kinds.parquet` (3c), the stage 3 change rows (for inherited commits), `boilerplate_lines.parquet` and `dataset_signatures/` (5a).
- GitSkills `artifacts` (`repo_full_name`, `path`, `file_sha`, `first_commit_at`) and `repos` (`is_fork`, `created_at`); `src/data/derived/s2_clusters.parquet` (read only).

## Steps

**Step 1: Read the inputs.** Check the files (a missing one names the command that builds it), register the large tables as DuckDB views, and build `registry_repos` (every stage 3c registry) and `copier_repos` (copiers; in dev only the dev registries' copiers, as in stage 5c).

**Step 2: Read the copy histories (queries Q7.1–Q7.3).** Rule 1. The history of a copy file is the history of its folder (the parent of `path`, the stage 6 target). Only the folder's own top-level `SKILL.md` counts (`lower(file_path) = 'skill.md'`, role `own` or `former_folder`): a nested `x/SKILL.md` is another skill with its own folder, and a `copy_source` row is the history of another folder of the same repo before this one existed (the stage 4a rule). Dates are read as UTC. Each file's distinct blobs are numbered `version_seq` 1, 2, … in order of first appearance (UTC date, then `commit_seq`); a delete has no version. A commit that a registry history also holds (a fork, or an imported registry history) is marked `inherited` (Q7.2) and kept as evidence. Q7.3 reads when GitHub created each copy repo (`repos.created_at`).

**Step 3: `history_exact` links (Algorithm A30).**

**Step 4: Signatures of the copy texts.** Steps 5 and 7 compare copy texts with registry versions by MinHash. The texts needed are the first downloaded version of every file and the current text of every non-exact file. A text that is a dataset text already has a stage 5a signature and is read from `dataset_signatures/`. An older history version is read from the stage 6 texts table and signed the stage 5a way (normalised, front matter and boilerplate lines removed, 5-word shingles, 128 values), `HISTORY_SIGN_BATCH` texts at a time, so both sides of every comparison are built the same way.

**Step 5: `history_near` links (Algorithm A31).**

**Step 6: The strongest link of each file (Algorithm A32).** The stage 5c text link, the history_exact link and the history_near link of each file compete. The winner's lineage resolves to its `root_lineage` (stage 5b): the family of a copy is the first registry that published the skill. Each file also gets:

- `link_similarity`: the score that admitted the link: 1.0 for exact and history_exact, the Jaccard for near and history_near, the containment for near_contained, the line-check share for cluster (a cluster pair was never scored);
- `same_owner_as_registry`, recomputed for the winning lineage, and `is_fork` from stage 5c;
- `repo_role` from `repo_roles_final.parquet`;
- `history_source`: `copied` when the folder has own `SKILL.md` rows, else `none`;
- `first_commit_date`: the first commit of the file's history, else the dataset `first_commit_at` (query Q7.4). `discovered_at` is never used: it is the crawl date, not a commit date.

**Step 7: The origin version, by date (Algorithm A33).**

**Step 8: Arrival groups and removed copies (Algorithm A34c).**

**Step 9: Families (Algorithm A34, queries Q7.6–Q7.9) and the copier result (Algorithm A34b, query Q7.5).**

**Step 10: Save and print.** The four outputs are written, sorted so a rerun gives the same files. The run prints copy files per link and per history source, `copied_before_release` and `no_date` counts (with the `no_date` share of non-exact links), the fork share, folders, files, repos and first-commit arrivals per arrival group, removed folders and their share, the bundle share of the hand-labelled stored and used repos, texts and families per family kind, `registry_self` texts, copier-seen files per result (and how many only in weak copiers), and two sample rows of each output.

```text
link order for one copy file (first in the list wins)

  current text (stage 5c)          own history (stage 6)
  ───────────────────────          ─────────────────────
  1 exact                          
                                   2 history_exact   a version blob = a registry blob (A30)
  3 near
  4 near_contained
                                   5 history_near    first version near a registry text (A31),
                                                     only when 1-4 found nothing
  6 cluster

  winner -> lineage -> root_lineage (stage 5b) = family of the copy
```

## Algorithm A30: history_exact links

**Goal.** Prove a copy from its own history, even when its current text has drifted.

**Rule.** A history row of the file (not a delete) whose blob equals a lineage version blob links the file to that version. When several rows match, the **earliest** row of the copy's history wins (UTC date, then `commit_seq`): that is when the copy took the registry text. A blob held by several lineages takes the oldest release, then `lineage_id`, then `version_seq`. Inherited commits count: they are part of the repo's history.

**Why it is weaker than `exact`.** An exact link says the current text is a registry blob; nothing is stronger. A history_exact link wins over every fuzzy text link: a hash match in the history is proof, a MinHash estimate on the current text is not.

## Algorithm A31: history_near links

**Goal.** Catch a copy whose first version was a light edit of a registry text, when the current text has drifted too far for stage 5c.

**Rule.** Only files with history and no exact, history_exact, near or near_contained link are scored (cluster is last in the order, so a cluster-only file is scored). Only the **first** downloaded version is compared: it is the closest the history gets to the moment of copying, and searching every later version would link files that converged on a registry text by chance. The search is the stage 5c near search (A25): registry versions sharing one band of the 64 × 2 LSH index, then the highest estimated Jaccard at least `NEAR_COPY_MIN_JACCARD` (0.80), ties to the oldest release.

## Algorithm A32: strongest link

**Rule.** One link per copy file, the first in this order: `exact` → `history_exact` → `near` → `near_contained` → `history_near` → `cluster`. A tie inside one kind goes to the oldest release, then `lineage_id`, then `version_seq`. Copiers have no lineages ([stage 4a](07_registry_lineages.md) leaves them out), so a copier can never win, and no hub tie rule is needed.

## Algorithm A33: origin version, by date

**Goal.** Name the registry version a copy started from, without pointing at a version released after the copy existed.

**Rule.**

- `exact` / `history_exact`: the identical version (`origin_basis = exact`), with or without a date. When that version was released after the copy's first commit, the copy also gets `copied_before_release = True`: the repo had the text before the registry published it.
- other links with a `first_commit_date`: among the versions of the linked lineage with `release_date <= first_commit_date`, the one whose signature is most similar to the copy's **first** text (the first downloaded version, else the current text), by estimated Jaccard. Ties, and texts without a signature, go to the newest such version (`origin_basis = similar_before_date`).
- no version released on or before that date: `copied_before_release = True`, origin empty, `origin_basis` null. Either the repo was the real origin, or its git date is wrong (a rebased or squashed history). Counted, not fixed.
- no date at all (no downloaded history and no dataset `first_commit_at`, for example a repo deleted since the crawl): `origin_basis = no_date`, origin empty, `copied_before_release = False`. The file keeps its link and family. No date is guessed.

**Worked example** (doctest of `choose_origin_version`, tests `test_origin_*`):

```text
registry:  v1 (Jan 10) ------ v2 (Mar 01) ------------- v3 (May 15)
copy:            c1 (Feb 01)                c3 (Apr 20)
c1, near:          only v1 is released -> origin v1, similar_before_date
c3, near to v1:    v1 and v2 released, v1 more similar -> origin v1
copy first committed Jan 01, near:  nothing released -> copied_before_release
history passes through v2's blob:   history_exact -> origin v2 (exact)
```

**Speed.** Each distinct (copy text, lineage) pair is scored once against every version of the lineage (equal MinHash values / 128, in blocks of `ORIGIN_SCORE_BLOCK` pairs); the date filter and the pick per file run in DuckDB (Q7.10).

## Algorithm A34c: arrival group and removal

**Goal.** Tell a skill picked by hand from one that came in a bundle. Most copies arrive many at a time, and bundle copies are edited far less often, so every later table is reported per group.

**Rule, per copy folder** (every copy file of the folder gets the folder's values):

- `arrival_commit` / `arrival_date`: the first commit (UTC date, then `commit_seq`) with a non-delete row of the folder's own `SKILL.md`. `former_folder` rows count, so a renamed copy keeps its first name's commit.
- `batch_size`: the number of **copy folders** of the same repo that arrived in the same commit. The repo's own unlinked skills added in that commit do not count: a project that adds two copies and thirty of its own skills chose the two copies.
- `arrival_group`: `chosen` for a batch of at most `ARRIVAL_CHOSEN_MAX` (4), `bundle` from `ARRIVAL_BUNDLE_MIN` (20), `small_set` in between, `unknown` when the folder has no own `SKILL.md` history (the repo is `gone`, `too_large`, `error` or `empty`, or the folder is not on the default branch).
- `arrival_in_first_commit`: the copy arrived with the repo's first upload: its arrival date is at most `ARRIVAL_REPO_START_DAYS` (1) after GitHub created the repo, or earlier for an imported history (Q7.3). A project uploaded in one squashed commit makes copies chosen one by one look like a bundle; the summary counts these. The repo's own first commit is not used, because stage 6 logs only the copy folders: for a repo with one copy folder, its "first commit in the history" is always the copy's own arrival.
- `removed` / `removed_date`: the folder's last commit only deletes its `SKILL.md`. Stage 10 measures staleness of a removed copy only up to that date.

No repo-level "collection" label is used. A real app that installs a 1,000-skill bundle is a project, and its copies are still bundle copies.

## Algorithm A34: families

**Rule, per English text, in this order:**

1. a text whose copy files link to a lineage: `family_id = root_lineage`, `family_kind = source`. When its files carry different links (their histories differ), the text's `best_link` is the strongest of them;
2. a text only inside registry repos (`registry_self = True`) that is a lineage version: the root of the oldest lineage version with that blob, `source`;
3. an unlinked text that a copier holds (`copier_seen` of stage 5c, or a registry-only text inside a copier): `copier:<file_sha>`, `copier_seen`. This is checked **before** clusters: a crawled text is not independent work. No registry match never means self-written;
4. an unclaimed text (none of the above) in an `s2_clusters` cluster at `CLUSTER_LINK_THRESHOLD` (0.80) with at least one other unclaimed English text: `cluster:<id>`, `independent`;
5. else `single:<file_sha>`, `singleton`.

Every root is a source registry, because stage 4a left copiers out. Family totals are counted over every English dataset file of the family's texts, twice: all files, and without forks and without files whose owner owns the family's root registry (`files_without_forks`, `repos_without_forks`).

## Algorithm A34b: copier result (Pass 2)

For each file outside the registries of a `copier_seen` text, compare the file's first commit with the copier's first date for the text (stage 5c `copier_first_date`). These files were not cloned in stage 6 (only linked copies were), so the date is the dataset `first_commit_at`. Both are compared as UTC **days**:

- `repo_first`: the file's first commit is on or before the copier's first date (the same day counts): the copier crawled it, and the earliest such repo among the text's files is the origin;
- `copier_first`: the copier had the text strictly earlier: copied, origin unknown (the repo may have installed it from the copier, or from an author outside the registries);
- `no_date`: either date is missing. No date is guessed.

`weak_copiers_only` is carried from stage 5c: every copier that holds the text has a stage 3c confidence below 0.5.

## Queries

- **Q7.1** `COPY_HISTORY_SQL`: stage 6 change rows of the copy folders, top-level `SKILL.md`, role `own` or `former_folder`, date cast to `TIMESTAMPTZ`, blob null on a delete.
- **Q7.2** `INHERITED_COMMITS_SQL`: distinct stage 3 commits that are also commits of the copy histories.
- **Q7.3** `REPO_CREATED_SQL`: `min(created_at)` as UTC per copy repo from the GitSkills `repos` table.
- **Q7.4** `DATASET_FIRST_COMMIT_SQL`: `min(first_commit_at)` as UTC per `(repo, path)` of the copy files.
- **Q7.5** `COPIER_SEEN_FILES_SQL`: artifacts of the `copier_seen` texts, anti-joined to `registry_repos`, with `min(first_commit_at)`.
- **Q7.6** `ENGLISH_FILES_SQL`: every distinct `(repo, path, file_sha)` of an English text, with `in_registry`, `in_copier` and `is_fork`.
- **Q7.7** `REGISTRY_TEXTS_SQL`: texts whose files are all in registries, with the root of the oldest lineage version that has the blob, and `in_copier`.
- **Q7.8** `ASSIGN_FAMILIES_SQL`: the five family rules; the cluster count is taken over unclaimed English members only.
- **Q7.9** `FAMILY_COUNTS_SQL`: family totals, with and without forks and root-owner files.
- **Q7.10** `ORIGIN_PICK_SQL`: per dated non-exact file, the scored versions released on or before its first commit; `QUALIFY row_number() = 1` over Jaccard (nulls last), newest release, highest `version_seq`.

## Outputs

All four in the dev or full data folder.

`copy_files.parquet`, one row per copy file: `repo`, `path`, `file_sha`, `lineage_id`, `root_lineage`, `link`, `link_similarity`, `history_source` (`copied` | `none`), `first_commit_date`, `origin_seq`, `origin_date`, `origin_basis` (`exact` | `similar_before_date` | `no_date` | null), `copied_before_release`, `is_fork`, `same_owner_as_registry`, `repo_role`, `arrival_commit`, `arrival_date`, `batch_size`, `arrival_group` (`chosen` | `small_set` | `bundle` | `unknown`), `arrival_in_first_commit`, `removed`, `removed_date`.

`families.parquet`, one row per English text: `file_sha`, `family_id`, `family_kind` (`source` | `copier_seen` | `independent` | `singleton`), `best_link`, `registry_self`, and the family totals `files`, `repos`, `owners`, `files_without_forks`, `repos_without_forks` (the same on every text of a family).

`copier_seen_files.parquet`, one row per file of a `copier_seen` text outside the registries: `repo`, `path`, `file_sha`, `first_commit_date`, `copier_first_date`, `copier_first_repo`, `copier_result` (`repo_first` | `copier_first` | `no_date`), `weak_copiers_only`.

`history_link_counts.parquet`: `link`, `history_source`, `files`: copy files per winning link and history download.

## Limits

- **History only for linked copies.** Stage 6 cloned the repos of linked copies only, so an unlinked text cannot gain a history link, and `copier_seen` files are dated by the dataset `first_commit_at` alone.
- **Default branch, today's history.** A copy the dataset saw on another branch, or in a repo deleted or rewritten since, has no history: it keeps its text link, and its arrival group is `unknown`.
- **Only the first version is searched for `history_near`.** A history that drifts towards a registry text later is not linked.
- **Git dates can lie.** A rebase or a squashed upload moves the first commit; `copied_before_release` and `arrival_in_first_commit` count the visible cases.
- **The origin comes from the linked lineage, not its root.** When the best text match is a later re-host whose root stage 5b did not find, every version of the linked lineage can be newer than the copy, and the copy is `copied_before_release`. The dev sample shows this strongly: 916 shadcn copies from January link to a June re-host, because the original registry is not among the 30 dev registries.
- **Dev mode.** Only the dev lineages and their copies are linked, but families cover every English text, so most dev texts fall into `independent` or `singleton`.

The full-run numbers are in the [copy families report](../report/13_copy_families_report.md).
