# Data Dictionary: Skill Genealogy

Describes every table, column and derived label of the `skill_genealogy` package. Counts are for the full run in `src/data/skill_genealogy/` (built 2026-11-06 to 2026-11-17). Dev runs write the same tables to `src/data/skill_genealogy_dev/`. The source dataset is described in [DATA_DICTIONARY.md](DATA_DICTIONARY.md), section 1. How each table is built is in [RUNBOOK_GENEALOGY.md](RUNBOOK_GENEALOGY.md).

## 1. Keys and conventions

| Key | Meaning |
|---|---|
| `file_sha` | Git blob id of a dataset text. One distinct skill text. |
| `repo`, `path` | One dataset file (`owner/repo` and path of `SKILL.md`). A copy is one file, so the same text in two repos is two copies. |
| `lineage_id` | `owner/repo::skill_folder` of a source registry skill. |
| `version_seq`, `*_seq` | Version number inside a lineage, 1 = first, in release order. |
| `package_version_seq` | Version number of the whole skill folder inside a lineage. |
| `blob`, `sha` | Git blob id of a text or file. |
| `family_id` | A family: the root `lineage_id` (source), `cluster:N` (independent), `copier:<file_sha>` (copier_seen) or `single:<file_sha>` (singleton). |

- **English only.** Every table from registry selection on holds only texts labelled `en` or `unknown`.
- **Normalised text.** Texts are compared after `normalize_text()` (NFKC, LF line endings, no trailing spaces, unified quotes and dashes). A change that only touches these is not a new version.
- **Dates are UTC.** Git dates are stored as given and compared in UTC.
- **Null means not measured.** A null score or flag means there was nothing to measure, never 0.

## 2. Registry side

### 2.1 `skill_language.parquet` (language labels)

One row per distinct dataset text. 1,877,981 rows.

| Column | Meaning |
|---|---|
| `file_sha` | Text id. |
| `language` | ISO code from the lingua detector, `unknown` (under 20 letters, or Latin but not confident) or `other` (non-Latin, not confident). |
| `is_english` | True for `en` and `unknown`. 1,633,285 texts. |
| `body_chars` | Characters after the front matter. |

### 2.2 `registry_tree_counts.parquet` (registry tree check)

One row per checked candidate: a non-fork with 100+ stars and 1–4 English skills in the dataset. 6,834 rows.

| Column | Meaning |
|---|---|
| `repo` | Candidate repository. |
| `status`, `message` | `ok`, `gone`, `empty`, `timeout` or `error`, and the reason. |
| `head_commit` | Commit whose tree was listed. |
| `tree_skill_md` | `SKILL.md` files in the repository's own tree (any language). |
| `seconds`, `checked_at` | Check time and date. |

### 2.3 `registries.parquet`, `repo_roles.parquet`, `dev_registries.parquet` (registry selection)

`registries`: one row per registry. 4,806 rows.

| Column | Meaning |
|---|---|
| `repo`, `owner`, `stars` | Repository facts from GitSkills. |
| `english_skills`, `files` | Distinct English texts and files in the dataset. |
| `tree_skill_md` | Count from the registry tree check, if checked. |
| `registry_basis` | `dataset` (5+ English skills in the dataset, 3,566) or `repo_tree` (promoted by the registry tree check, 1,240). |
| `hub_share`, `is_hub`, `hub_tested` | Preview hub test from dataset dates: share of dated texts another repo had first, the flag (`hub_share >= 0.25` with 5+ dated texts), and whether it could be scored. |

`repo_roles`: one row per dataset repository (282,200), with `repo_role` = `registry`, `hub`, `registry_hub` or `repository`, plus the columns above.

`dev_registries`: the fixed 30-registry dev sample, 10 per `size_group`, with `english_skills` and `is_hub`.

### 2.4 `registry_history/` (registry history)

`registry_history_changes.parquet`: one row per file change in a registry skill folder. 7,926,625 rows.

| Column | Meaning |
|---|---|
| `repo`, `skill_folder` | Registry and the skill folder followed. |
| `file_path`, `old_file_path` | Path inside the folder, and the old path of a rename. |
| `repo_path`, `old_repo_path` | The same paths from the repository root. `(repo, repo_path, commit, status)` is unique. |
| `commit`, `commit_seq`, `commit_date` | Commit, its order in the history, and its date (string, read as UTC). |
| `author_key` | Anonymised author. |
| `subject` | First line of the commit message. |
| `status` | Git status: `A` added, `M` modified, `D` deleted, `R` renamed, `T` type change. |
| `old_sha`, `new_sha`, `old_mode`, `new_mode` | Blob ids and file modes before and after. |
| `is_skill_md`, `is_script` | The file is the folder's `SKILL.md` / a script. |
| `history_role` | `own` (the folder's own history), `former_folder` (history at an older path), `copy_source` (the folder it was copied from inside the repo). |

`registry_history_texts.parquet`: one row per stored text (`sha`, `text`). `SKILL.md` and small scripts. 1,992,550 rows.

`registry_history_status.parquet`: one row per registry (4,806): `status` (`ok`, `gone`, `timeout`, `error`), `message`, `head_commit`, `seconds`, `follow_renames`, `history_since`, `skill_folders`, `changes`, and counts of rows dropped while following renames (`not_ours`, `after_copy`, `twin`), deleted and moved folders (`deleted_folders`, `moved_folders`, `history_folders_dropped`).

### 2.5 `registry_readmes.parquet` (registry READMEs)

One row per registry. 4,806 rows. `status` (`ok`, `no_readme`, `gone`), `commit` and `commit_basis` (`stage3_head` = the commit the registry history download mined, `current_head`), `readme_path`, `readme_sha`, `readme_bytes`, `readme_text` (null over 200,000 bytes), `seconds`, `fetched_at`.

### 2.6 `registry_kinds.parquet` and `registry_kind_review.csv` (registry kinds)

One row per registry. 4,806 rows.

| Column | Meaning |
|---|---|
| `registry_kind` | `source` (4,060), `copier` (702) or `unclear` (44). Copiers are never origins. |
| `confidence` | Rule-based rank from 0 to 1, not a probability. |
| `needs_review` | Confidence below 0.5 (487 registries). |
| `kind_basis` | `date`, `date+readme` (agree), `date_vs_readme` (disagree), `readme`, `none`. |
| `skill_folders` | Skill folders in the history. |
| `shared_texts`, `elsewhere_first` | `SKILL.md` texts the registry shares with other owners, and how many of those another owner had first. |
| `upstream_share` | `elsewhere_first / shared_texts`. |
| `own_texts`, `copied_share` | All distinct texts of the registry, and the share another owner had first. |
| `top_upstream` | The owner it shares most first-seen-elsewhere texts with. |
| `date_vote`, `date_strength` | Vote and strength from dates. |
| `readme_vote`, `readme_strength`, `readme_phrase` | Vote from a README phrase, and the phrase. |
| `hand_label` | Filled from the review sheet. Empty in the current run. |

The CSV has the same rows for hand review; a filled `hand_label` overrides the rule on the next run.

### 2.7 `lineages.parquet`, `lineage_versions.parquet`, `lineage_changes.parquet`, `lineages_dropped.parquet` (registry lineages)

`lineages`: one row per English source skill. 188,787 rows.

| Column | Meaning |
|---|---|
| `lineage_id`, `repo`, `skill_folder` | The skill. |
| `former_paths` | Older folder paths it moved from. |
| `copied_from` | Folder inside the same repo it was copied from, if any. |
| `first_date`, `last_date` | First and last version dates. |
| `version_count` | Text versions. |
| `registry_removed`, `removed_at` | The registry deleted the skill, and when. |
| `newest_language`, `is_english` | Language of the newest version. |

`lineage_versions`: one row per distinct normalised text of a lineage. 817,485 rows. `version_seq`, `blob`, `normalized_sha`, `commit`, `commit_date`, `release_date` (the date the version was released; equal to `commit_date` in the current run), `body_chars`, `language`, and `same_as_seq` (an earlier version with the same normalised text, when the registry went back to it).

`lineage_changes`: one row per step between consecutive versions. 628,698 rows. The change columns are shared with `copy_changes` (section 3.6). `state` here is `substantive` (245,066), `front_matter_only` (209,300) or `cosmetic` (174,332).

`lineages_dropped`: `lineage_id` and `reason` (`not_english` 35,234, `no_text` 7).

### 2.8 `lineage_package_versions.parquet`, `lineage_folder_manifests.parquet` (package versions)

`lineage_package_versions`: one row per version of the whole skill folder. 970,430 rows. `package_version_seq`, the text `version_seq` it contains, `commit`, `commit_date`, `changed_skill_md`, `changed_scripts`, `changed_other_files`, `file_count`, `script_count`.

`lineage_folder_manifests`: one row per file in each package version. 22,803,855 rows. `path`, `sha`, `mode`, `size`, `file_category` (`script`, `reference`, `asset`, `config`).

### 2.9 `boilerplate_lines.parquet`, signatures (text signatures)

`boilerplate_lines`: lines held by 50 or more owners (`line`, `owners`). 547,906 rows. They are removed before signing, so a shared install line does not link two unrelated skills.

`dataset_signatures/part_*.npz` and `registry_signatures.npz`: 128-value MinHash signatures of every English dataset text and every registry version, with shingle counts. Binary files read only by registry re-hosts, dataset copies, copy families and family trees.

### 2.10 `lineage_roots.parquet`, `registry_hubs.parquet`, `repo_roles_final.parquet` (registry re-hosts)

`lineage_roots`: one row per lineage. 188,787 rows.

| Column | Meaning |
|---|---|
| `copied_from_lineage`, `copied_from_seq` | The older registry lineage and version this one re-hosts. Null for originals. |
| `rehost_link`, `rehost_similarity` | `exact`, `near` (Jaccard ≥ 0.80) or `near_contained` (containment ≥ 0.80), and the score. 17,415 re-hosted. |
| `root_lineage`, `chain_length`, `chain_cycle` | The original at the end of the chain, steps to reach it, and a cycle flag (never true). |
| `is_original` | No older source in another registry (171,372). |

`registry_hubs`: one row per registry: kind and confidence from registry kinds, `english_lineages`, `rehosted_lineages`, `upstream_share`, `dated_hub_share`, `preview_hub_share`, `is_hub`, and `hub_reason` (a readable sentence).

`repo_roles_final`: `repo_roles` with the final `repo_role` (`repository` 277,389, `registry` 4,104, `registry_copier` 702, `hub` 5) and `hub_evidence`.

## 3. Copy side

### 3.1 `text_links.parquet`, `copy_candidates.parquet`, `links_dropped.parquet`, `copier_seen.parquet` (dataset copies)

`text_links`: one row per linked English text outside the registries. 287,579 rows. `file_sha`, `lineage_id`, `version_seq`, `link`, `jaccard`, `containment`, `similarity_verified` (share of the registry version's lines kept, from a line diff).

| `link` | Rule |
|---|---|
| `exact` | Same blob as a registry version. |
| `near` | MinHash Jaccard ≥ 0.80. |
| `near_contained` | Containment ≥ 0.80 of the registry version in the text. |
| `cluster` | Same near-duplicate cluster (`s2_clusters`) as a registry version. |

Every non-exact link keeps at least 50% of the registry lines (`similarity_verified >= 0.50`).

`copy_candidates`: one row per dataset file holding a linked text. 1,414,080 rows. `repo`, `path`, `file_sha`, `lineage_id`, `root_lineage`, `version_seq`, `link`, scores, `is_fork`, `same_owner_as_registry`.

`links_dropped`: 7,017 fuzzy links that failed the line check (`reason = line_share_below_min`).

`copier_seen`: 83,594 unlinked texts that a copier registry holds. `copier_first_date`, `copier_first_repo`, `copier_repos`, `weak_copiers_only` (only copiers with confidence below 0.5).

### 3.2 `copied_skill_history/` (copied-skill history)

The same layout as `registry_history/` (section 2.4) for the 60,958 repositories that hold a copy.

| File | Rows |
|---|---:|
| `copied_skill_history_changes.parquet` | 12,079,171 |
| `copied_skill_history_texts.parquet` | 635,021 |
| `copied_skill_history_status.parquet` | 60,958 (`ok` 55,296, `gone` 5,646, `error` 7, `empty` 6, `too_large` 3) |
| `download_targets.parquet` | 1,414,038 (`repo`, `skill_folder`) |
| `copied_skill_history.duckdb` | The resumable store the parquet files are exported from |

### 3.3 `copy_files.parquet` and helpers (copy families)

`copy_files`: one row per copy file. 1,414,080 rows.

| Column | Meaning |
|---|---|
| `repo`, `path`, `file_sha`, `lineage_id`, `root_lineage` | The copy and its lineage. |
| `link`, `link_similarity` | Strongest link: dataset-copy links plus `history_exact` (a history text equals a registry blob, 42,554) and `history_near` (762). |
| `history_source` | `copied` (has a downloaded history) or `none`. |
| `first_commit_date` | First `SKILL.md` commit of the copy. |
| `origin_seq`, `origin_date`, `origin_basis` | Origin version: `exact`, `similar_before_date` (most similar version released by the copy's date), `no_date`, or null. |
| `copied_before_release` | The copy is dated before the matching release (97,839). |
| `is_fork`, `same_owner_as_registry`, `repo_role` | Repository facts. |
| `arrival_commit`, `arrival_date`, `batch_size` | Commit that added the folder, and how many copy folders it added. |
| `arrival_group` | `chosen` (1–4), `small_set` (5–19), `bundle` (20+), `unknown` (no history). |
| `arrival_in_first_commit` | Arrived at most one day after GitHub created the repository (or earlier, for an imported history). |
| `removed`, `removed_date` | The copy was deleted before the download. |

`families`: one row per English text. 1,633,285 rows. `family_id`, `family_kind` (`source`, `independent`, `copier_seen`, `singleton`), `best_link`, `registry_self` (text only inside registries), `files`, `repos`, `owners`, and `files_without_forks`, `repos_without_forks` (which also leave out same-owner copies).

`copier_seen_files`: files holding a `copier_seen` text (282,429), with `copier_result` (`repo_first`, `copier_first`, `no_date`).

`history_link_counts`: files by `link` and `history_source`.

### 3.4 `proven_pairs.parquet`, `link_method_recall.parquet` (link recall)

`proven_pairs`: the 42,554 `history_exact` copies, each scored against its proven lineage: `exact`, `jaccard_found`, `containment_found`, `verified_found`, the best scores, `stage5_link`, and `current_is_dataset_text` (false when the text changed after the crawl).

`link_method_recall`: one row per method (`exact`, `minhash_jaccard`, `containment`, `line_check`, `any_text_method`): `proven_pairs`, `found`, `recall`, `recall_without_forks`, `changed_pairs`, `recall_changed_since_crawl`.

### 3.5 `family_tree.parquet`, `family_summary.parquet` (family trees)

`family_tree`: one row per node. 2,022,027 rows (445,593 registry versions, 1,576,434 texts).

| Column | Meaning |
|---|---|
| `node_id`, `node_kind` | `registry` (a lineage version) or `copy` (a dataset text). |
| `blob`, `repo`, `first_date`, `files` | The node's text, a repository holding it, its first date and file count. |
| `parent_id`, `parent_similarity` | The closest earlier node (MinHash), and the similarity. |
| `generation` | Steps from the registry trunk: a copy of a registry version is 1, a copy of a copy 2. Registry versions stay at 0. |
| `approx` | The family hit the 2,000-text cap (only `cluster:38`). |

`family_summary`: one row per family. 1,202,176 rows. `texts`, `member_nodes`, `registry_nodes`, `dated_nodes`, `depth`, `mean_generation`, `repos`, `approx`, `status` (`built`, `registry_only`), `root_basis` (`single`, `trunk`, `most_copied`, `oldest_dated`, `none`).

### 3.6 `copy_changes.parquet` (copy against origin)

One row per copy with an origin. 1,275,893 rows. The current text (`current_basis` = `latest_history` or `dataset`) is compared with the origin version (`origin_seq`).

| Column | Meaning |
|---|---|
| `lines_added`, `lines_removed` | Body lines from a line diff. |
| `body_change_pct` | Changed body lines as a share of the origin's lines. |
| `share_of_old_lines_kept` | Origin lines still present. |
| `headings_added`, `headings_removed`, `commands_added`, `scripts_mentioned_added`, `network_refs_added`, `names_added` | Lists of what was added or removed. `names_added` reads the whole file. |
| `steps_added` | Numbered steps added. |
| `state` | `unchanged` (same body), `cosmetic` (under 5% of body lines), `substantive` (5% or more). |
| `name_changed`, `description_changed` | Front-matter `name` / `description` changed. |
| `fm_keys_added`, `fm_keys_removed`, `fm_keys_changed` | Front-matter keys. |
| `allowed_tools_added`, `allowed_tools_removed`, `permissions_changed` | `allowed-tools` changes. |
| `customized` | Added a project name to the body, a script, a step, a command or a heading. |
| `heading_overlap` | Share of headings the copy added that the registry also added later. Null when the copy added none. |

### 3.7 `copy_history_edits.parquet`, `change_profile.parquet` (copy against origin)

`copy_history_edits`: one row per edit inside a copy's history. 150,657 rows. `from_blob`, `to_blob`, `commit`, `commit_date`, `inherited`, the change columns of 3.6, and `change_kind`:

| `change_kind` | Meaning |
|---|---|
| `sync` | The new text is a version of the copy's own lineage, byte for byte. |
| `other_source` | The new text is a version of another lineage. |
| `local` | Not a known registry text. Not proof that a person wrote it. |

`change_profile`: four rows, one per population (`registry_update`, `copy_local_edit`, `copy_sync`, `copy_vs_origin`), with `changes` and the shares `share_substantive`, `share_headings_added`, `share_commands_added`, `share_network_added`, `share_scripts_added`, `share_permissions_changed`.

### 3.8 `copy_version_similarity.parquet` (version scores)

One row per version of each copy. 1,453,150 rows. `version_seq`, `commit_date`, `blob`, `basis` (`history`, `dataset`), `change_kind` (`copy` = the first version, `local`, `sync`, `other_source`, `inherited`), `change_notes`, and for each of four targets the columns `<t>_jaccard`, `<t>_containment`, `<t>_verified`, `<t>_lines_added`, `<t>_lines_removed`, `<t>_body_change_pct`:

| Target | Compared with |
|---|---|
| `prev` | The copy's previous version |
| `origin` | The origin version |
| `at_date` | The newest registry version on or before this version's date (`at_date_seq`) |
| `newest` | The newest registry version (`newest_seq`) |

Scores use exact shingle sets of the whole body, with no boilerplate removed. Identical texts score 1.

### 3.9 `copy_folder_changes.parquet`, `copy_version_folder_summary.parquet` (folder comparison)

`copy_folder_changes`: one row per file that differs between a copy version's folder and a target package. 9,151,147 rows. `target` (`prev`, `origin`, `at_date`, `newest`), `file_path`, `old_file_path`, `result` (`added`, `removed`, `modified`, `renamed`, `same` = same sha, new mode), `old_sha`, `new_sha`, `mode_changed`, `category`, `size_change`, `risk_flags`, and `new_risk_flags` (flags the new script has and the target's file did not: `network`, `runs_commands`, `deletes_files`, `reads_secrets`, `installs_packages`, `obfuscated`; null when not checked).

`copy_version_folder_summary`: one row per compared pair, zeros included. 3,771,118 rows. `target_package_seq`, `files_added`, `files_removed`, `files_modified`, `files_renamed`, `scripts_added`, `scripts_modified`, `new_risky_scripts`, `missing_scripts` (named in `SKILL.md`, not in the folder), `hidden_scripts` (in the folder, never named), `folder_source` (`history`, `dataset_snapshot`).

### 3.10 `copy_staleness.parquet` (staleness)

One row per copy file. 1,414,080 rows.

| Column | Meaning |
|---|---|
| `current_basis`, `current_blob` | Where the current text came from (`history`, `dataset`) and its blob. |
| `held_seq`, `held_basis`, `held_similarity`, `held_release_date` | Version the copy holds now: `exact_current`, `similar_before_date`, `copied_before_release` (none), `no_overlap` (none). |
| `as_of_date`, `as_of_basis` | Date the copy is measured at: `registry_snapshot`, `dataset_capture` or `copy_removed`. |
| `latest_seq` | Newest version released by the as-of date. |
| `skill_md_versions_behind`, `first_missed_release` | Text versions missed, and the first one's date. |
| `package_held_seq`, `package_basis`, `package_versions_behind`, `first_missed_package` | The same for the folder. `package_basis`: `measured`, `files_not_kept` (took the text only), `no_folder`, `no_package`. |
| `missed_script_updates` | Missed package versions that changed a script. |
| `days_stale` | Days from the first missed release (text or package) to the as-of date. |
| `missed_substantive`, `missed_commands`, `missed_network`, `missed_scripts` | Number of missed updates of each kind (per update, not per line). |
| `missed_risky` | Any missed update added a command, a network reference or a script, or changed a script's risk flags. A rule, not a hand label. |
| `stopped_changing_at` | Last change of the copy. |
| `copied_before_release` | As in `copy_files`. |
| `registry_removed`, `days_since_removed` | The registry deleted the skill. |
| `copy_state` | See below. |
| `stale_after_days` | The cutoff used (30). |

| `copy_state` | Rule | Files |
|---|---|---:|
| `up_to_date` | Behind on neither text nor folder | 391,756 |
| `behind` | Behind, first miss within the cutoff | 60,426 |
| `stale` | Behind, first miss older than the cutoff | 803,168 |
| `orphaned` | The registry removed the skill | 72,578 |
| `removed` | The copy was deleted | 66,148 |
| `unknown` | No held version | 20,004 |

### 3.11 `sync_events.parquet` (staleness)

One row per moment a copy took a registry version, byte for byte. 892,559 rows. `commit`, `commit_date`, `event_kind` (`copy` 829,998 = first arrival, `update` 61,424, `downgrade` 1,137), `from_seq`, `to_seq`, `versions_skipped`, `release_date`, `lag_days` (negative when the copy's git date is before the release).

## 4. Settings

Every cutoff is a named value in `src/skill_genealogy/settings.py`.

| Setting | Value | Used for |
|---|---:|---|
| `TREE_CHECK_MIN_STARS` | 100 | Registry and tree-check star floor |
| `MIN_ENGLISH_LETTER_SHARE` | 0.75 | A text below this Latin-letter share cannot be English |
| `BOILERPLATE_MIN_OWNERS` | 50 | A line held by this many owners is boilerplate |
| `NEAR_COPY_MIN_JACCARD` | 0.80 | `near` links |
| `NEAR_COPY_MIN_CONTAINMENT` | 0.80 | `near_contained` links |
| `MIN_VERIFIED_LINE_SHARE` | 0.50 | Line check for every fuzzy link |
| `SUBSTANTIVE_BODY_CHANGE_PCT` | 5 | `cosmetic` versus `substantive` |
| `ARRIVAL_CHOSEN_MAX`, `ARRIVAL_BUNDLE_MIN` | 4, 20 | Arrival groups |
| `STALE_AFTER_DAYS` | 30 | `behind` versus `stale` (override with `--stale-days`) |

## 5. Known data caveats

- Origin is the earliest version among the selected registries, not the author.
- 22% of copy files have no downloaded history and are measured from their dataset text only.
- No registry kind, link or change label has been checked by hand.
- See [THREATS_TO_VALIDITY_GENEALOGY.md](THREATS_TO_VALIDITY_GENEALOGY.md).
