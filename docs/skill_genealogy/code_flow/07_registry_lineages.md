# Registry lineages

Turns the registry `SKILL.md` history into lineages. This is stage 4a. It does not use the network. [Registry history](04_registry_history.md) (stage 3) and [registry kinds](06_registry_kinds.md) (stage 3c) have to have run first. Card G10.

A lineage is one skill folder in one registry, followed through renames. Its versions are numbered, and a new version starts only when the text really changes. A copy inside a registry is its own lineage and names its source. A skill whose folder was deleted is marked removed. A lineage whose newest text is not English is dropped.

**Copiers are left out here, once.** Only registries whose stage 3c `registry_kind` is `source` or `unclear` become lineages. A copier is never a lineage, a root or an origin. Every later stage reads these tables, so no later stage filters copiers again.

## Scripts

Run these from the repo root.

```bash
bash scripts/skill_genealogy/dev_mode/07_registry_lineages.sh
```

Builds the lineages of the 30 dev registries and stays in the terminal. The command is `build_registry_lineages --dev --force`. There is no dev registry history folder: the run reads the **full** stage 3 tables, limited to the dev registries. The tables go to `src/data/skill_genealogy_dev/`. It takes about 35 seconds.

```bash
bash scripts/skill_genealogy/full_run/07_registry_lineages.sh
```

Builds every lineage in the background. It takes about 3 hours; keep the machine awake (`caffeinate -i -w <pid>`). The log is `logs/build_registry_lineages_<date>.log`. Follow it with `tail -f`. The tables go to `src/data/skill_genealogy/`.

```bash
bash scripts/skill_genealogy/tests/07_registry_lineages.sh
```

Runs the tests, with the tests of `compare_texts.py`.

## Inputs

All in the full data folder `src/data/skill_genealogy/`, also with `--dev`:

- `registry_history/registry_history_changes.parquet`, `registry_history_texts.parquet`, `registry_history_status.parquet` (stage 3)
- `registry_kinds.parquet` (stage 3c). The hand labels from `registry_kind_review.csv` are already in it after a `--force` run of stage 3c; the review is optional.
- `registries.parquet`, or `dev_registries.parquet` with `--dev` (stage 2c)

A run skips when the four outputs exist. `--force` builds them again.

## Size

Without copiers, the full run reads 1,314,549 `SKILL.md` rows of 4,053 registries and 224,028 folders, and 933,117 `SKILL.md` texts with 9.4 GB of text. The stage 3 tables have 7.9 million rows and 20 GB of text, so:

- The rows are read in DuckDB with the memory cap `HISTORY_DUCKDB_MEMORY_LIMIT` (4 GB). Only `SKILL.md` rows of kept registries reach pandas.
- Texts are never loaded all at once. Lineages are split into batches that need at most `LINEAGE_TEXT_BATCH` (40,000) texts. Each batch reads only its texts (query Q4.2), numbers the versions, labels the language and compares the versions, then frees the texts. `lineage_changes.parquet` is written batch by batch.
- `commit_date` is text with an offset (`+08:00`, `Z`). It is parsed as a UTC timestamp and never compared as text.

## Steps

**Step 0: Copier filter.** `registry_scope` joins the registry list with `registry_kinds.parquet`. A registry is kept when its kind is in `LINEAGE_REGISTRY_KINDS` (`source`, `unclear`). The run prints how many registries and skill folders were skipped, and the ten largest skipped registries.

**Step 1: Read the `SKILL.md` rows.** Query Q4.1 reads every `is_skill_md` row of the kept registries, with `repo_path`, `old_repo_path` and `history_role`. Three kinds of rows are in it:

- the folder's own `SKILL.md` (`file_path` is `SKILL.md`, in any letter case), role `own` or `former_folder`. Only these rows can be versions;
- `copy_source` rows, the source's `SKILL.md` before a copy. They are *outside rows*: step 2 reads them, and they never become versions;
- nested files such as `cn/SKILL.md`. They belong to another skill and are ignored.

The key of a change is `repo`, `skill_folder`, `commit`, `repo_path`, never `file_path`: since stage 3 attaches moved folders, one skill can have parallel copies (the same skill in two places at once) with the same `file_path`.

**Step 2: Join renames and copies (Algorithm A50).** See the card below.

**Step 3: Number the text versions (Algorithm A20).** See the card below.

**Step 4: Mark removed lineages.** A lineage is removed when its last own `SKILL.md` row (role `own`) is a delete (`D`). In one commit a delete sorts before an add, so a re-add wins. `removed_at` is the time of that delete. Stage 3 keeps 35,208 deleted skill folders; they end with that delete. `former_folder` rows do not decide: a moved skill lives on in the folder it moved to, and a deleted parallel copy is not the skill.

**Step 5: Language.** Each version gets `detect_language` (Algorithm A8, the same rule as stage 2a), once per normalised text. The rule reads the body without front matter and code fences. A lineage is English when its newest version is `en` or `unknown`. Other lineages go to `lineages_dropped.parquet` with reason `not_english`, and leave every other output. A lineage with no version (only deletes, or no stored text) is dropped with reason `no_text`.

**Step 6: Compare consecutive versions.** For each pair of versions n and n+1, `compare_texts` gives the body fields (lines added and removed, share of old lines kept, headings, commands, scripts, network references, steps, project names and the change `state`) and `compare_front_matter` gives the header fields (name, description, keys added, removed or with a new value (`fm_keys_changed`), `allowed-tools`). `compare_texts` reads the body only, so a step whose body is the same would be `unchanged` there; when its front matter changed (for example a `version:` bump), `step_state` sets `state` to `front_matter_only`. The four states are `substantive`, `cosmetic`, `front_matter_only` and `unchanged`.

**Step 7: Lineage table, save, summary.** One row per kept lineage. The run prints the counts below and two sample records of each output.

## Algorithm A50: renames and copies

`lineage_id` is `repo::skill_folder`, the folder at HEAD, the one stage 3 filed the rows under.

**Renames.** `former_paths` lists the older folders of the skill, oldest first, each once:

- the folder of `repo_path` of each `former_folder` row of the folder's own `SKILL.md`;
- the folder of `old_repo_path` of each rename (`R`) of that `SKILL.md`, own or former. This follows chains: `skills/a` → `skills/b` → `skills/new` gives `[skills/a, skills/b]`.

The folders come from `repo_path` and `old_repo_path`, never from `file_path`, which stage 3 strips to the folder. The HEAD folder itself is not listed.

**Copies.** Git status `C` is never stored. Stage 3 shows a copy in the destination folder as `copy_source` rows (the source's `SKILL.md`) up to the copy commit, then an add of the folder's own `SKILL.md`:

```text
folder skills/dst     c1 skills/src/SKILL.md A  (copy_source)   ┐ outside rows, not versions
                      c2 skills/src/SKILL.md M  (copy_source)   ┘ the newest one names the source
                      c3 skills/dst/SKILL.md A  (own)           → version 1 of skills/dst
                      c4 skills/dst/SKILL.md M  (own)           → version 2
folder skills/src     c1 A → v1    c2 M → v2    c5 M → v3        the source keeps its own versions
```

The destination is its own lineage. `copied_from` is the lineage of the newest `copy_source` row strictly before the destination's first text row, from its `repo_path`. Stage 3 marks a row `copy_source` only when that folder still exists at HEAD, so the source is a real copy even when it was never edited again. A former folder that no longer exists is `former_folder` and is a rename.

## Algorithm A20: versions

1. **One row per commit.** The rows that can give a version are the folder's own `SKILL.md` rows, role `own` or `former_folder`, that carry a text (not a delete). When one commit has several of them (parallel copies), the `own` row is kept; if there is none, the `former_folder` row whose `repo_path` comes first alphabetically.
2. **Order.** UTC commit time, then `commit_seq`.
3. **Number.** The text is the `new_sha` blob. The `old_sha` chain is not followed: in a merge, `old_sha` can name an older blob, and that is not an error. The first row with a stored text is version 1, whatever its status (`M` or `R` can come first). `normalized_sha` is the SHA-1 of `normalize_text(text)` with blank lines removed. `version_seq` goes up only when it differs from the previous version, so a whitespace-only commit, one that only adds or removes blank lines included, is not a version. A front-matter change (a new `version:` value) is a new version. A text that comes back gets a new `version_seq`, and `same_as_seq` is the first version with that text. `release_date` is the time of the first commit with that version.

A lineage timeline, from the dev run (`Rain-kl/OpenFlare`, a folder renamed from `.agent/` to `.agents/`):

```text
.agent/skills/cache-framework   ──R──>  .agents/skills/cache-framework   (HEAD)
2026-06-20 v1 (6,667 chars)     2026-06-27 v2 (6,712)     2026-07-24 v3 (6,737)
lineage_id     Rain-kl/OpenFlare::.agents/skills/cache-framework
former_paths   [".agent/skills/cache-framework"]
```

## Queries

- **Q4.1** `SKILL_MD_CHANGES_SQL`: every `is_skill_md` row of the registries in the registered table `kept_repos`.
- **Q4.2** `SKILL_TEXTS_SQL`: `sha, text` of the blobs in the registered table `wanted_blobs`, one batch at a time.

## Outputs

All in the dev or full data folder.

`lineages.parquet`, one row per kept lineage:

- `lineage_id`, `repo`, `skill_folder`, `former_paths` (list), `copied_from` (null when not a copy)
- `first_date` (release of version 1), `last_date` (release of the newest version, or `removed_at` when the skill was removed later), `version_count`
- `registry_removed`, `removed_at`, `newest_language`, `is_english`

There is no hub column. The registry hub tag is decided later with real dates, in G13.

`lineage_versions.parquet`: `lineage_id`, `version_seq`, `same_as_seq`, `blob`, `normalized_sha`, `commit`, `commit_date`, `release_date`, `body_chars` (characters of the normalised body without front matter, as in stage 2a), `language`.

`lineage_changes.parquet`: `lineage_id`, `from_seq`, `to_seq`, `release_date`, every `TextChange` field and every `compare_front_matter` field.

`lineages_dropped.parquet`: `lineage_id`, `reason` (`not_english`, `no_text`).

Sample lineage from the dev run:

```text
lineage_id: GMGNAI/gmgn-skills::skills/gmgn-swap     former_paths: []     copied_from: None
first_date: 2026-03-13 10:57:50+00:00                last_date: 2026-10-29 08:17:13+00:00
version_count: 74    registry_removed: False    newest_language: en    is_english: True
```

## Summary

The run prints: registries kept and skipped as copiers (with their skill folders), `SKILL.md` rows per `history_role` (and rows with no or another role), lineages read and kept, lineages per registry (median, maximum), versions per lineage (median, maximum), commits whose parallel copies were collapsed to one version, versions with `same_as_seq`, lineages with former paths and with `copied_from`, outside rows used for `copied_from` and ignored, removed lineages, dropped lineages by reason, the newest language, and the share of kept registries mined with `follow_renames = false`.

## Limits

- Registries with more than 200 skill folders were mined without `--follow` (`follow_renames = false`). A rename there is found only when git reports it as `R`, or when stage 3 attached the moved folder from shared versions.
- A lineage is English by its newest version only. An English skill translated to Chinese is dropped, with all its English versions.
- `copied_from` names a lineage of the same registry. Copies between registries are G13.
- The language rule reads the body only. A skill whose front matter is English and whose body is mostly Chinese is `not_english`.
- `same_as_seq` compares normalised texts, so a revert with other whitespace still counts as the same text.
- A front-matter change, even only a `version:` bump, is a new version. The step says so (`front_matter_only`, `fm_keys_changed`), so a later stage can leave such steps out.

The full-run numbers are in the [registry lineages report](../report/07_registry_lineages_report.md).
