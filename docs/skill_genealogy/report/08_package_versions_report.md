# Package versions report

Results of stage 4b, read from `src/data/skill_genealogy/lineage_package_versions.parquet` and `lineage_folder_manifests.parquet` on 2026-11-08. How the step runs is in [package versions](../code_flow/08_package_versions.md). The log is `logs/build_package_versions_20261108_0114.log`.

The run read the stage 4a tables of the second full run ([registry lineages report](07_registry_lineages_report.md)) and the stage 3 changes table as it is. It used no network. `--packages-only --force` took 6.2 minutes (373 seconds) in 13 batches, at a peak of 3.4 GB. The outputs are 12 MB (package versions) and 62 MB (manifests).

The skill folder at each commit is the folder of the `SKILL.md` that A20 reads, not the HEAD folder. This matters for 502 lineages (2,148 text versions): skills whose older root history stage 3 files as `own` rows (`aboul3ata/lazyweb-skill`), and skills that moved away and came back (`TentacleOpera/switchboard`). With the HEAD folder, those versions would have no package version.

## Rows read

The 188,787 kept lineages (75 of them at a repo root) have **4,399,980** `own` and `former_folder` rows, both `SKILL.md` and folder files. `copy_source` rows are not read. `github/gh-aw` (a root skill) is a batch of its own with 398,418 rows, of which only `scripts/`, `references/` and `assets/` count.

## Package versions

| | Count |
|---|---:|
| Lineages with package versions | 188,787 (all) |
| Package versions | **970,430** |
| Text versions (stage 4a) | 817,485 |
| Text versions without a package version | **0** |
| Package versions that change the text (`changed_skill_md`) | 817,485 |
| Package versions that change only folder files | **152,945** (15.8%) |
| Lineages with more package versions than text versions | 27,501 (14.6%) |

Per lineage: median **2**, 90th percentile 10, 99th percentile 53, maximum **3,132** (`topoteretes/cognee::cognee`). 69,533 lineages have one package version, and 20,205 have 10 or more.

What each package version changed:

| Text | Scripts | Other files | Package versions |
|:-:|:-:|:-:|---:|
| yes | – | – | 642,117 |
| yes | – | yes | 132,438 |
| – | – | yes | 107,143 |
| – | yes | – | 35,923 |
| yes | yes | yes | 27,658 |
| yes | yes | – | 15,272 |
| – | yes | yes | 9,879 |

A script changed in **88,732** package versions; 45,802 of them did not change the text. These are the changes that a text-only comparison (G10) cannot see.

## Folder files

| | Lineages | Share |
|---|---:|---:|
| Folder files in the newest package version | 75,492 | 40.0% |
| A script in the newest package version | 21,518 | 11.4% |
| Folder files in any package version | 76,644 | |
| A script in any package version | 22,207 | |

Of the lineages with folder files now, the median has **3** files, the 90th percentile 14 and the 99th percentile 117. 889 lineages have 100 or more files, and 21 have 1,000 or more (the most is 12,011).

## Manifests

**22,803,855** manifest rows (one per file per package version):

| `file_category` | Rows | With `size` |
|---|---:|---:|
| script | 13,097,303 | 13,071,174 (99.8%) |
| reference | 5,228,447 | 87,309 |
| asset | 3,614,914 | 62,419 |
| config | 863,191 | 817 |

`size` is known for 13.2 million rows, almost all scripts: stage 3 stored script texts, not other files.

**A few lineages hold most rows.** The top 100 lineages hold 73% of the manifest rows. They are `SKILL.md` files placed inside a project's source folder, so the "package" is that code:

| Lineage | Manifest rows | Package versions |
|---|---:|---:|
| `topoteretes/cognee::cognee` (a Python package, 1,240 files) | 6,617,160 | 3,132 |
| `HKUDS/Vibe-Trading::agent` | 3,237,077 | 2,054 |
| `ai-shifu/ai-shifu::src/api` | 884,443 | 982 |
| `BuilderIO/agent-native::packages/core/docs` | 610,148 | 449 |
| `ai-shifu/ai-shifu::src/web` | 588,213 | 684 |

## Root skills

The 75 repo-root lineages have 6,310 package versions (2,257 change the text). The most is 963 (`Ancienttwo/repo-harness::.`), then `aigorahub/elves::.` (543) and `github/gh-aw-firewall::.` (505). Only `scripts/`, `references/` and `assets/` count there.

## Sample records

```text
lineage_id: topoteretes/cognee::cognee      package_version_seq: 2    version_seq: 1
commit: d33c3efad6ec…  commit_date: 2026-03-10 02:29:49+00:00
changed_skill_md: False   changed_scripts: True   changed_other_files: True
file_count: 1240   script_count: 1127

lineage_id: topoteretes/cognee::cognee      package_version_seq: 3132
path: __init__.py   sha: fd117b0460e4…   mode: 100644   size: 4420   file_category: script
```

## Open points

- Lineages whose folder is a project's source tree (`cognee`, `src/api`, `agent`) count source edits as package versions. G21 and G22 may want to leave out or flag lineages with many files (889 have 100 or more).
- `size` is null for most non-script files (no network in this stage).
