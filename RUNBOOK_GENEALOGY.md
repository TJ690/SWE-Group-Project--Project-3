# Genealogy runbook

Run commands from the repository root with Python 3.13. Run the stages in order; each reads the tables of the stages before it. The design of each stage is in [docs/skill_genealogy/design](docs/skill_genealogy/design/README.md), how its code runs is in [code_flow](docs/skill_genealogy/code_flow/), and the numbers of the full run are in [report](docs/skill_genealogy/report/).

```bash
source .venv/bin/activate
```

The genealogy package is `src/skill_genealogy/`. It is separate from the `skillsig` pipeline in [docs/PIPELINE_RUNBOOK.md](docs/PIPELINE_RUNBOOK.md), and uses only two of its outputs: the GitSkills Parquet files (dataset download) and the near-duplicate clusters `src/data/derived/s2_clusters.parquet`.

## Modes and common flags

| Flag | Meaning |
|---|---|
| `--dev` | Use the fixed 30-registry sample (`dev_registries.parquet`) and write to `src/data/skill_genealogy_dev/`. A smoke test; its numbers are not evidence. |
| `--force` | Rebuild even if the output exists. Use it after editing code. |

Full outputs go to `src/data/skill_genealogy/`. Dev and full outputs never share a folder.

Every stage has three scripts in `scripts/skill_genealogy/`, numbered like the design pages:

| Folder | Use |
|---|---|
| `tests/NN_<stage>.sh` | The stage's unit tests and doctests |
| `dev_mode/NN_<stage>.sh` | A dev run, output shown and logged |
| `full_run/NN_<stage>.sh` | A full run in the background, logged to `logs/`, with `caffeinate` keeping the Mac awake for long stages |

Extra arguments to a script are passed to the module, for example `scripts/skill_genealogy/full_run/16_staleness.sh --force`.

**Network.** Only the registry tree check, registry history, registry READMEs and copied-skill history use GitHub. Every other stage reads saved tables. Running a download again changes the snapshot that every later table is built on, so do it only on purpose. A stopped download resumes; `--retry-failed` retries only `error` and `timeout` repos.

**Tests.**

```bash
python -m pytest -q tests/skill_genealogy
```

<a id="language-labels"></a>

## Language labels: label the language of every skill text

```bash
python -m skill_genealogy.select_registries --force-languages
```

**Input:** `src/data/gitskills_data/data/artifacts/*.parquet` ([dataset download](docs/PIPELINE_RUNBOOK.md#step-0)).

**Output:** `skill_language.parquet`; `language_check_sample.csv` (a hand-check sheet, never overwritten once filled).

[Design](docs/skill_genealogy/design/01_skill_language_labeling.md) · [Report](docs/skill_genealogy/report/01_skill_language_report.md).

<a id="registry-tree-check"></a>

## Registry tree check: count the skills of starred repos (network)

```bash
python -m skill_genealogy.check_registry_trees            # --retry-failed, --workers N, --limit N
```

**Input:** [`skill_language.parquet`](#language-labels); `src/data/gitskills_data/data/{artifacts,repos}/*.parquet`.

**Output:** `registry_tree_counts.parquet`.

**Time:** about 56 minutes of git time, 8 repos at once. [Design](docs/skill_genealogy/design/02_registry_tree_check.md) · [Report](docs/skill_genealogy/report/02_registry_tree_check_report.md).

<a id="registry-selection"></a>

## Registry selection: choose the registries

```bash
python -m skill_genealogy.select_registries --force
```

**Input:** [`skill_language.parquet`](#language-labels); [`registry_tree_counts.parquet`](#registry-tree-check); `src/data/gitskills_data/data/{artifacts,repos}/*.parquet`.

**Output:** `registries.parquet`; `repo_roles.parquet`; `dev_registries.parquet`.

[Design](docs/skill_genealogy/design/03_registry_selection.md) · [Report](docs/skill_genealogy/report/03_registry_selection_report.md).

<a id="registry-history"></a>

## Registry history: download the registry histories (network)

```bash
python -m skill_genealogy.download_registry_history       # --retry-failed, --merge-only, --deleted-folders, --since DATE
```

**Input:** [`registries.parquet`](#registry-selection) (or `dev_registries.parquet` with `--dev`).

**Output:** `registry_history/registry_history_changes.parquet`; `registry_history_texts.parquet`; `registry_history_status.parquet`.

**Time:** many hours on full data; the slowest repo (`chromium/chromium`) takes over 2 hours. Run with `full_run/04_registry_history.sh`. [Design](docs/skill_genealogy/design/04_registry_history.md) · [Report](docs/skill_genealogy/report/04_registry_history_report.md).

<a id="registry-readmes"></a>

## Registry READMEs: download the README of every registry (network)

```bash
python -m skill_genealogy.download_registry_readmes       # --retry-failed
```

**Input:** [`registries.parquet`](#registry-selection); [`registry_history_status.parquet`](#registry-history).

**Output:** `registry_readmes.parquet`.

**Time:** about 10 minutes. [Design](docs/skill_genealogy/design/05_registry_readmes.md) · [Report](docs/skill_genealogy/report/05_registry_readmes_report.md).

<a id="registry-kinds"></a>

## Registry kinds: label each registry a source or a copier

```bash
python -m skill_genealogy.label_registry_kinds --force
```

**Input:** [`registries.parquet`](#registry-selection); [`registry_history/*`](#registry-history); [`registry_readmes.parquet`](#registry-readmes).

**Output:** `registry_kinds.parquet`; `registry_kind_review.csv` (hand-review sheet). After filling `hand_label` in the sheet, run the command again with `--force` to apply it.

**Time:** under a minute. [Design](docs/skill_genealogy/design/06_registry_kinds.md) · [Report](docs/skill_genealogy/report/06_registry_kinds_report.md).

<a id="registry-lineages"></a>

## Registry lineages: build the versions of each source skill

```bash
python -m skill_genealogy.build_registry_lineages --force
```

**Input:** [`registry_history/*`](#registry-history); [`registry_kinds.parquet`](#registry-kinds); `registry_kind_review.csv`.

**Output:** `lineages.parquet`; `lineage_versions.parquet`; `lineage_changes.parquet`; `lineages_dropped.parquet`.

**Time:** about 3 hours, peak 3 GB. [Design](docs/skill_genealogy/design/07_registry_lineages.md) · [Report](docs/skill_genealogy/report/07_registry_lineages_report.md).

<a id="package-versions"></a>

## Package versions: build the versions of each whole skill folder

```bash
python -m skill_genealogy.build_registry_lineages --packages-only --force
```

**Input:** [`lineages.parquet`, `lineage_versions.parquet`](#registry-lineages); [`registry_history/*`](#registry-history).

**Output:** `lineage_package_versions.parquet`; `lineage_folder_manifests.parquet`.

**Time:** about 6 minutes. [Design](docs/skill_genealogy/design/08_package_versions.md) · [Report](docs/skill_genealogy/report/08_package_versions_report.md).

<a id="text-signatures"></a>

## Text signatures: find boilerplate lines and sign every text

```bash
python -m skill_genealogy.find_copies --only signatures
```

**Input:** [`skill_language.parquet`](#language-labels); [`lineage_versions.parquet`](#registry-lineages); [`registry_history_texts.parquet`](#registry-history).

**Output:** `boilerplate_lines.parquet`; `dataset_signatures/part_*.npz`; `registry_signatures.npz`.

**Time:** about 17 minutes for boilerplate and registry signatures, plus about 80 minutes to sign the dataset the first time. [Design](docs/skill_genealogy/design/09_boilerplate_signatures.md) · [Report](docs/skill_genealogy/report/09_boilerplate_signatures_report.md).

<a id="registry-rehosts"></a>

## Registry re-hosts: find registries that copy from other registries

```bash
python -m skill_genealogy.find_copies --only rehost
```

**Input:** [`registry_signatures.npz`](#text-signatures); [`lineages.parquet`, `lineage_versions.parquet`](#registry-lineages); [`registry_kinds.parquet`](#registry-kinds); [`repo_roles.parquet`](#registry-selection).

**Output:** `lineage_roots.parquet`; `registry_hubs.parquet`; `repo_roles_final.parquet`.

**Time:** under a minute. [Design](docs/skill_genealogy/design/10_registry_rehosts.md) · [Report](docs/skill_genealogy/report/10_registry_rehosts_report.md).

<a id="dataset-copies"></a>

## Dataset copies: find copies of registry skills in the dataset

```bash
python -m skill_genealogy.find_copies --only copies
```

**Input:** [`dataset_signatures/`, `registry_signatures.npz`](#text-signatures); [`lineage_roots.parquet`](#registry-rehosts); [registry lineage tables](#registry-lineages); [`registry_kinds.parquet`](#registry-kinds); [`skill_language.parquet`](#language-labels); `src/data/derived/s2_clusters.parquet` ([near-duplicate clusters](docs/PIPELINE_RUNBOOK.md)).

**Output:** `text_links.parquet`; `copy_candidates.parquet`; `links_dropped.parquet`; `copier_seen.parquet`.

**Time:** about 5 minutes. [Design](docs/skill_genealogy/design/11_dataset_copies.md) · [Report](docs/skill_genealogy/report/11_dataset_copies_report.md).

<a id="copied-skill-history"></a>

## Copied-skill history: download the history of copied skills (network)

```bash
python -m skill_genealogy.download_copied_skill_history --yes    # --retry-failed, --merge-only, --limit N
```

**Input:** [`copy_candidates.parquet`](#dataset-copies).

**Output:** `copied_skill_history/copied_skill_history_changes.parquet`; `copied_skill_history_texts.parquet`; `copied_skill_history_status.parquet`; `download_targets.parquet`; `copied_skill_history.duckdb` (the resumable store).

**Time:** about 10.5 hours for 60,958 repos, 8 at a time. `--yes` skips the confirmation prompt. If the export runs out of memory, run again with `--merge-only`. [Design](docs/skill_genealogy/design/12_copied_skill_history.md) · [Report](docs/skill_genealogy/report/12_copied_skill_history_report.md).

<a id="copy-families"></a>

## Copy families and link recall: assign origins and families, and score each text method

```bash
python -m skill_genealogy.assign_families --force
```

**Input:** [`copy_candidates.parquet`, `text_links.parquet`, `copier_seen.parquet`](#dataset-copies); [`copied_skill_history/`](#copied-skill-history); [`lineage_roots.parquet`, `repo_roles_final.parquet`](#registry-rehosts); [signatures and boilerplate lines](#text-signatures); [registry lineage tables](#registry-lineages); [`registry_kinds.parquet`](#registry-kinds); [`skill_language.parquet`](#language-labels); `src/data/derived/s2_clusters.parquet`.

**Output:** `copy_files.parquet`; `families.parquet`; `copier_seen_files.parquet`; `history_link_counts.parquet`; `proven_pairs.parquet`; `link_method_recall.parquet`.

**Time:** about 10 minutes. [Design](docs/skill_genealogy/design/13_copy_families.md) · [Report](docs/skill_genealogy/report/13_copy_families_report.md).

<a id="family-trees"></a>

## Family trees: build a tree for each family

```bash
python -m skill_genealogy.build_family_trees --force     # --skip-families-over N
```

**Input:** [`copy_files.parquet`, `families.parquet`](#copy-families); [`lineage_roots.parquet`](#registry-rehosts); [`lineage_versions.parquet`](#registry-lineages); [signatures](#text-signatures).

**Output:** `family_tree.parquet`; `family_summary.parquet`.

**Time:** about 13 minutes. [Design](docs/skill_genealogy/design/14_family_trees.md) · [Report](docs/skill_genealogy/report/14_family_trees_report.md).

<a id="copy-changes"></a>

## Copy changes: compare copies with their origin

```bash
python -m skill_genealogy.measure_copy_changes --force               # all three parts
python -m skill_genealogy.measure_copy_changes --only origin         # copy against origin
python -m skill_genealogy.measure_copy_changes --only versions       # version scores
python -m skill_genealogy.measure_copy_changes --only folders        # folder comparison, needs version scores
```

**Input:** [`copy_files.parquet`, `families.parquet`](#copy-families); [`copied_skill_history/`](#copied-skill-history); [registry lineage tables](#registry-lineages); [package version tables](#package-versions); [`registry_history_texts.parquet`](#registry-history); [`skill_language.parquet`](#language-labels).

**Output:** copy against origin `copy_changes.parquet`, `copy_history_edits.parquet`, `change_profile.parquet`; version scores `copy_version_similarity.parquet`; folder comparison `copy_folder_changes.parquet`, `copy_version_folder_summary.parquet`.

**Time:** about 2 hours (version scores 65 minutes, folder comparison 35 minutes). [Design](docs/skill_genealogy/design/15_copy_changes.md) · [Code flow](docs/skill_genealogy/code_flow/15_copy_changes.md).

<a id="staleness"></a>

## Staleness: measure how far each copy is behind

```bash
python -m skill_genealogy.measure_staleness --force       # --stale-days N (default 30)
```

**Input:** [`copy_files.parquet`, `families.parquet`](#copy-families); [`copied_skill_history/`](#copied-skill-history); [registry lineage tables](#registry-lineages); [package version tables](#package-versions); [`registry_history/*`](#registry-history); [`skill_language.parquet`](#language-labels).

**Output:** `copy_staleness.parquet`; `sync_events.parquet`. The cutoff is stored in every row (`stale_after_days`).

**Time:** about 70 minutes. [Design](docs/skill_genealogy/design/16_staleness.md) · [Code flow](docs/skill_genealogy/code_flow/16_staleness.md).

## Not built yet

| Module | Card | Does |
|---|---|---|
| `summarize_results` | G23 | Summary tables for RQ1–RQ3 |
| `export_tree_viewer`, `export_staleness_viewer` | G24, G25, G32, G34 | Tree and staleness viewers |
| `repo_report` | G26 | Terminal report for one repository |

## Checking outputs

Genealogy stages write no `.manifest.json`, so `scripts/output_status.py` only shows whether a file exists. Each stage prints a summary at the end of its log in `logs/`. To count rows:

```bash
python -c "import duckdb; print(duckdb.sql(\"select count(*) from 'src/data/skill_genealogy/copy_staleness.parquet'\"))"
```

The expected full-run row counts are in [DATA_DICTIONARY_GENEALOGY.md](DATA_DICTIONARY_GENEALOGY.md).
