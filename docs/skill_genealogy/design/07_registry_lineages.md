# Design: registry lineages (stage 4a)

Card G10 · Code: `build_registry_lineages.py` steps 0–7 · [Code flow](../code_flow/07_registry_lineages.md) · [Report](../report/07_registry_lineages_report.md)

## Problem

Stage 3 gives file changes. Later stages need **skills with numbered versions**: "this copy holds version 3 of `anthropics/skills::skills/pdf`, released on this date". A skill can be renamed, copied inside its registry, deleted, or edited only in whitespace, and each case has to give the right versions.

## Design at a glance

A lineage is one skill folder of one source registry, followed through renames. Its `SKILL.md` texts are numbered in time order; a new version starts only when the normalised text changes. Copies inside a registry are their own lineage and name their source. Deleted skills are marked removed. Lineages whose newest text is not English are dropped. Each consecutive pair of versions is compared.

## Decisions

**Copiers are left out here, once.** Only `source` and `unclear` registries become lineages. A copier is never a lineage, root or origin, and no later stage has to filter it again. This drops 702 registries and 385,400 folders.

**The id is the folder at HEAD.** `lineage_id = repo::skill_folder`. Stage 3 already files a renamed skill's rows under its current folder, so the id is stable. Older folders are listed in `former_paths`, taken from the full `repo_path`, never from `file_path` (stage 3 strips that to the folder).

**A copy inside a registry is a new lineage.** Stage 3 shows a copy as the source's rows (`copy_source`) up to the copy, then the destination's own add. The destination starts its own versions and `copied_from` names the source. The source keeps its versions. Copies between registries are stage 5b.

**A version is a change of meaning, not of bytes.** The version key is the SHA-1 of `normalize_text(text)` with blank lines removed. A whitespace-only commit, including one that only adds or removes blank lines, is not a new version (that rule removes 5,064 versions on full data). A text that comes back gets a new number and `same_as_seq` points to its first appearance.

**Front-matter changes are versions, and are labelled.** A `version:` bump or a new `description:` changes what an agent reads, so it is a new version. The step's `state` is `front_matter_only` and `fm_keys_changed` names the keys, so a later stage can leave such steps out.

**One version per commit.** When a commit touches parallel copies of a skill (the same skill in two places, 9,611 commits), the `own` row gives the version.

**The `old_sha` chain is not followed.** In a merge, `old_sha` can name an older blob. Versions are ordered by UTC commit time and `commit_seq`, which is always defined.

**Removed means the last own row is a delete.** A re-add in the same commit wins. A former folder's delete does not count: a moved skill lives on.

**English by the newest version.** A lineage is English when its newest text is. One label per lineage keeps all its versions together; a lineage is never split by language.

**Batches for 9.4 GB of text.** Lineages are split so each batch needs at most 40,000 texts; texts are read, used and freed. Dates are parsed as UTC.

## Alternatives rejected

- **A new version for every commit that touches `SKILL.md`.** Counts whitespace and line-ending commits as edits.
- **Id from the first folder name.** Changes meaning when a skill is renamed twice; the HEAD folder is where users find it.
- **Merging separate folders that hold the same skill into one lineage** (for example the example projects of `TencentCloudBase/awesome-cloudbase-examples`, each with its own copy of the platform skills). They are separate folders that users install separately; stages 5b and 7a group them by text.

## Effect

188,787 English lineages from 4,104 registries, 817,485 versions (median 2 per lineage, max 357). 35,234 dropped as not English, two thirds of them from one German legal registry.

## Limits

- Repos with more than 200 folders were mined without rename following.
- An English skill later translated is dropped with all its English versions.
- A `version:` bump alone is a new version (labelled `front_matter_only`).
