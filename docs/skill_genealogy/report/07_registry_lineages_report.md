# Registry lineages report

Results of stage 4a, read from `src/data/skill_genealogy/lineages.parquet`, `lineage_versions.parquet`, `lineage_changes.parquet` and `lineages_dropped.parquet` on 2026-11-07. How the step runs is in [registry lineages](../code_flow/07_registry_lineages.md). The log is `logs/build_registry_lineages_20261107_2135.log`.

The run read the stage 3 history as it is ([registry history report](04_registry_history_report.md)) and the stage 3c labels after G38 rev 2 ([registry kinds report](06_registry_kinds_report.md)). It used no network. This is the second full run (2026-11-07 21:35 to 2026-11-08 00:40), after two fixes: blank lines no longer make a version, and a front-matter-only step is `front_matter_only` with `fm_keys_changed`. It took 3.1 hours (11,100 seconds) at a peak of 2.9 GB, with `caffeinate` keeping the machine awake.

## Copiers left out

| | Registries | Skill folders |
|---|---:|---:|
| All registries | 4,806 | |
| Kept (`source` 4,060, `unclear` 44) | 4,104 | 224,028 with `SKILL.md` rows |
| Skipped as copiers | **702** | **385,400** |

`majiayu000/claude-skill-registry` is 312,153 of the skipped folders. The next largest are `lingxling/awesome-skills-cn` (9,687), `aiskillstore/marketplace` (6,899) and `hashgraph-online/awesome-codex-plugins` (3,569). Every registry had a stage 3c label.

## Rows read

The kept registries have **1,314,549** `SKILL.md` change rows. Every row has a `history_role`:

| Role | Rows |
|---|---:|
| own | 1,153,136 |
| former_folder | 128,773 |
| copy_source | 32,640 |
| no role, or another value | 0 |

**64,997** rows are outside rows (`copy_source`, or a nested `SKILL.md` of another skill). 27,338 of them name the source of a copy; 37,659 are ignored.

## Lineages

| | Lineages |
|---|---:|
| Read (skill folders) | 224,028 |
| Kept (English) | **188,787** |
| Dropped, `not_english` | 35,234 |
| Dropped, `no_text` | 7 |

**One registry is two thirds of the language drops.** `Klotzkette/claude-fuer-deutsches-recht`, a `source` registry of German legal skills, lost 23,388 of its 23,399 lineages. Next are `ECNU-ICALK/AutoSkill` (2,373), `affaan-m/ECC` (485), `brycewang-stanford/Awesome-Journal-Skills` (460) and `revfactory/harness-100` (312). 510 registries lost at least one lineage, and 59 kept registries have no English lineage at all.

Of the kept lineages, 187,588 have an `en` newest version and 1,199 an `unknown` one (too short or unclear, counted as English). 187,874 come from `source` registries and 913 from `unclear` ones.

Lineages per registry: median **13**, maximum **8,871** (`sickn33/agentic-awesome-skills`).

## Versions

**817,485** versions. Per lineage the median is **2**, the 90th percentile 9, and the maximum **357** (`kortix-ai/suna` `.claude/skills/learnings`). 73,428 lineages (39%) have one version, and 41,577 (22%) have 5 or more.

- A version is a new text after normalising and dropping blank lines. A commit that only adds or removes blank lines is not a version: on the first run, before that rule, there were 5,064 more versions (822,549).
- **9,611** commits had parallel copies of a skill (two or more text rows in one commit). Each gave one version, from the `own` row.
- **7,934** versions bring back an older text (`same_as_seq` is set). The most is 52, in `EverMind-AI/Raven` `raven/memory_engine/skills/subagent-dag-orchestration`.
- Version 1 dates run from 2024-03-19 to 2026-11-07. 96% of lineages start in 2026.

**Template folders.** `TencentCloudBase/awesome-cloudbase-examples` has about 70,000 versions in 2,131 lineages from only 2,570 texts. Its example projects each hold copies of the same platform skills (for example `.codebuddy/skills/cloudbase-platform`), and sync commits update them all at once: one lineage has 128 versions. They are separate folders, so each is its own lineage. G13 and G16 can group them by text.

## Renames, copies, removed

| | Lineages |
|---|---:|
| With `former_paths` | 29,817 |
| With 2 or more former folders | 8,461 |
| With `copied_from` | 5,210 |
| `copied_from` names a kept lineage | 4,296 |
| `copied_from` names a dropped or missing lineage | 914 |
| `registry_removed` | 25,228 |

A `copied_from` that is not a kept lineage still names the source: the source may have been dropped as not English, or the source folder may have no `SKILL.md` text of its own. A null would hide the copy.

103 of the kept registries (2.5%) were mined with `follow_renames = false`. In those, a rename is found only when git reported it as `R`, or when stage 3 attached the moved folder.

## Version steps

**628,698** steps from version n to n+1.

| State | Steps | Share | Meaning |
|---|---:|---:|---|
| substantive | 245,066 | 39.0% | The body changed by 5% or more of its lines |
| front_matter_only | 209,300 | 33.3% | The body is the same; only the front matter changed |
| cosmetic | 174,332 | 27.7% | The body changed a little |
| unchanged | 0 | 0% | |

`compare_texts` reads the body only. A step whose body is the same was `unchanged` on the first run; it is now `front_matter_only`, and `fm_keys_changed` names the keys whose value changed. No step is `unchanged`: with blank lines out of the version key, two versions always differ in the body or in the front matter. `TencentCloudBase/awesome-cloudbase-examples` has 58,322 `front_matter_only` steps, `jeremylongshore/claude-code-plugins-plus-skills` 19,268 and `sickn33/agentic-awesome-skills` 11,029.

Keys with a new value, over all steps (242,564 steps list at least one):

| Key | Steps |
|---|---:|
| version | 95,403 |
| description | 83,443 |
| metadata | 46,390 |
| compatibility | 9,433 |
| allowed-tools | 8,660 |
| name | 7,907 |
| tags | 4,464 |
| risk | 4,051 |

Header flags over all steps: `description` changed 91,382, `name` 16,904, `allowed-tools` (`permissions_changed`) 11,430. These count added and removed keys too, so they are higher than the value changes above.

## Known limits

- A front-matter change, even only a `version:` bump, is a new version. The step is `front_matter_only`, so a later stage can leave it out.
- A lineage is English by its newest text only. An English skill translated to German or Chinese is dropped with all its English versions.
- Registries with more than 200 folders were mined without `--follow`. A rename that git did not report is a delete in one folder and a new lineage in another.
- `copied_from` covers copies inside one registry. Copies between registries are G13.
