# Registry kinds report

Results of the source or copier labels, read from `src/data/skill_genealogy/registry_kinds.parquet` and `registry_kind_review.csv` on 2026-11-07. How the step runs is in [registry kinds](../code_flow/06_registry_kinds.md). The run took 14 seconds and used no network.

A source writes its own skills. A copier collects skills of other authors. The label combines a date vote (who had each shared `SKILL.md` text first) and a README vote (phrases such as "aggregate catalog"). The confidence is rule-based: it ranks registries from doubtful to clear. It is not a measured probability. No registry has been labelled by hand yet.

## Labels

| Kind | Registries | Skill folders |
|---|---:|---:|
| source | 4,060 | 222,746 |
| copier | 702 | 385,400 |
| unclear | 44 | 1,296 |

`majiayu000/claude-skill-registry` alone is 312,153 of the copier folders (confidence 0.982, date and README agree). Without it, copiers hold 73,247 folders.

Of the 702 copiers, **241** share 20 or more texts with other owners. The other **461** rest on 1 to 19 shared texts.

| Confidence | Copier | Source | Unclear |
|---|---:|---:|---:|
| below 0.5 (review) | 359 | 84 | 44 |
| 0.5 to 0.8 | 147 | 3,456 | 0 |
| 0.8 or more | 196 | 520 | 0 |

The 343 copiers at 0.5 or more hold 376,134 folders, 98% of all copier folders.

## Borrowed a few texts

The date vote looks only at shared texts. A registry that wrote hundreds of skills and borrowed one or two then shows "1 of 1 first seen elsewhere", a share of 1.0, and was called a copier. `FerroxLabs/wayland` (2,309 folders, 3 shared texts) and `smithersai/smithers` (585 folders, 1 shared text) were two of them.

The step now also counts all distinct `SKILL.md` texts of a registry (`own_texts`) and the part another owner had first (`copied_share`). With fewer than 20 shared texts and a `copied_share` below 0.1, the vote is a source vote of 0.7. This changed **341** registries (17,483 folders) from copier to source. 22 of them had been copiers at 0.5 or more, such as `nodetool-ai/nodetool` (638 texts, 16 first seen in `mattpocock/skills`) and `basicmachines-co/basic-memory`: projects with their own skills and a few bundled from others.

The rule needs fewer than 20 shared texts. Without that limit, `majiayu000/claude-skill-registry` became a source on the first trial: it shares 49,661 texts and 95% were first seen elsewhere, but most of what it crawled comes from repos outside the registries, so its `copied_share` is only 0.094.

## Where the labels come from

| Basis | Registries |
|---|---:|
| date vote only | 4,724 |
| date and README agree | 29 |
| date and README disagree | 8 |
| README vote only | 1 |
| no vote | 44 |

Most labels come from the date vote alone. Only **38** of the 4,718 README texts have a phrase the README vote accepts, and only 2 of those are copier phrases:

| Phrase | Vote | Registries |
|---|---|---:|
| official skills | source | 28 |
| our skills | source | 6 |
| skills built by | source | 2 |
| aggregate catalog | copier | 1 |
| aggregated from | copier | 1 |

READMEs rarely say where their skills came from. The first trial matched phrases anywhere in the README and gave 178 conflicts with the date vote. "A curated collection of skills" is used by original authors (`MicrosoftDocs/Agent-Skills`). Strong phrases matched in other contexts, such as "not a mirror of", "This mirrors the build" and "a committer who is not the author". "Copied from" described install steps, file trees or one borrowed plugin (`microsoft/skills`), in 7 repositories the date vote called sources. The vote now reads only sentences that mention a skill, needs whole words, ignores a phrase after "not", "no" or "never", and uses neither "collection" words nor "copied from". The conflicts fell to 8.

| Date vote | README vote | Registries |
|---|---|---:|
| copier | copier | 1 |
| copier | source | 7 |
| source | copier | 1 |
| source | source | 28 |
| none | source | 1 |

## Moved skills and the date vote

The date vote needs each skill's earliest appearance. A first version of the stage 3 deleted-folder fix dropped moved folders, which held that earliest history. Keeping that history changed 12 labels: 6 copiers became sources, 3 sources became copiers, and 3 unclear registries got a label. `gooseworks-ai/goose-skills` was a copier, because its own texts seemed to appear after `majiayu000/claude-skill-registry` had crawled them. With the moved history attached (see the [registry history report](04_registry_history_report.md)), it is a source with confidence 0.979, and only 1% of its 384 shared texts were first seen elsewhere.

## Known cases

All six registries checked by hand get the expected label: `majiayu000/claude-skill-registry`, `lingxling/awesome-skills-cn` and `aiskillstore/marketplace` are copiers; `anthropics/skills`, `obra/superpowers` and `github/awesome-copilot` are sources.

## Cutoffs

Among registries with 20 or more shared texts, the copiers change little with the share cutoff:

| Share first seen elsewhere | Registries | Skill folders |
|---|---:|---:|
| 0.7 or more | 217 | 373,146 |
| 0.8 or more | 194 | 367,802 |
| 0.9 or more | 165 | 356,373 |

## Review sheet

`registry_kind_review.csv` has **487** registries below 0.5 confidence, most skill folders first. They hold 17,462 skill folders. Reviewing the first 25 covers 51% of those folders, the first 50 covers 63%, and the first 100 covers 74%. The largest is `a5c-ai/babysitter` (2,118 folders, 816 shared texts, a weak source). Labels written in `your_label` are used on the next run with `--force`. The review is optional: an unreviewed row keeps its computed label.

## Limits

- A history rewrite makes an original look younger than its copies.
- Only registries are compared, so a text whose origin is not a registry counts as first seen in the earliest registry.
- A small crawler of repos outside the registries, with fewer than 20 shared texts, looks like a source under the `copied_share` rule.
- The README vote is literal and English only, and almost no README states that it copies.
- 461 copier labels rest on fewer than 20 shared texts.
