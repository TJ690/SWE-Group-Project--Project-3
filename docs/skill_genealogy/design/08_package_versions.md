# Design: package versions (stage 4b)

Card G11 · Code: `build_registry_lineages.py` steps 8–10 · [Code flow](../code_flow/08_package_versions.md) · [Report](../report/08_package_versions_report.md)

## Problem

A skill is a package: `SKILL.md` plus scripts, references, config and assets. Stage 4a numbers versions of the text only, but a registry can change a script while the text stays the same. RQ2 (did the copy change the scripts?) and RQ3 (did the copy miss a script update?) need versions of the whole folder and the file list of each.

## Design at a glance

Replay each lineage's file changes in order and keep the folder's file listing (the **manifest**) after each commit. A new **package version** starts whenever the manifest or the text version changes. Store the manifest only at package versions.

## Decisions

**A package version is the folder plus the text version.** Two signals start one: any file added, removed or changed (sha or mode), or a new `SKILL.md` text version from stage 4a. So package versions are always at least the text versions, and every text version has a package version (checked: 0 missing).

**Replay by full repo path.** The running map is keyed by `repo_path`, never by `file_path`. A skill can live in two places at once (parallel copies), with the same relative paths; keyed by the full path, deleting the old copy's `scripts/a.py` does not remove the own copy's.

**The folder follows the `SKILL.md` that stage 4a reads.** The skill folder after a commit is the folder of the text row that gives the version, not the HEAD folder. A skill can have its older history at the repo root, or move away and come back; using the HEAD folder would make those versions have no package.

**Order inside a commit: delete, rename, add.** A delete plus an add of the same path keeps the new file.

**The text is not in the manifest.** `SKILL.md` is the text version; listing it again would double-count text changes. A nested `cn/SKILL.md` stays as a reference file.

**Vendored folders are not the skill.** `node_modules/`, `.venv/`, `dist/`, `build/` and similar are left out, so installing dependencies is not a package change.

**A root skill keeps only `scripts/`, `references/`, `assets/`.** When `SKILL.md` sits at the repo root, the folder is the whole project. 75 root lineages had 1.07 million file rows (395,000 in one repo) that are the project, not the skill. The three Agent Skills folders are the package.

**Package versions start at text version 1 and stop at removal.** Files added before the first `SKILL.md` join package version 1; nothing after a delete of the skill.

**Size only from stored texts.** `git log --raw` gives sha and mode, not size, and this stage uses no network. `size` is the byte length of the stored text (scripts and `SKILL.md`), else null.

**Batches by registry, rerun alone.** About 4.4 million rows are read in batches of about 400,000 rows, never splitting a registry. `--packages-only` reruns this stage without the 3-hour stage 4a; a 4a rebuild always rebuilds 4b, because package versions point to its `version_seq`.

## Alternatives rejected

- **Text versions only.** Misses 152,945 folder-only versions on full data, and every "missed script update".
- **Hash of the whole folder as the version.** Cannot say what changed (`changed_scripts`, `changed_other_files`).
- **The whole repo for a root skill.** Explodes the manifest with the project's own files.

## Effect

970,430 package versions for 817,485 text versions; 152,945 change only folder files, 45,802 of them scripts. 22.8 million manifest rows.

## Limits

- `size` is null for most non-script files.
- A root skill that keeps files outside the three folders shows fewer files.
- Two renames that swap names in one commit can lose a file.
