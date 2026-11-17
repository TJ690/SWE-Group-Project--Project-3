# Dataset copies report

Results of stage 5c, read from `src/data/skill_genealogy/text_links.parquet`, `copy_candidates.parquet`, `links_dropped.parquet` and `copier_seen.parquet` on 2026-11-10. How the step runs is in [copies in the dataset](../code_flow/11_dataset_copies.md). The logs are `logs/find_copies_copies_dev_20261109_2357.log` (dev) and `logs/find_copies_copies_20261110_0001.log` (full).

## Run

| Run | Registry versions indexed | Time | Peak memory |
|---|---:|---:|---:|
| Dev (26 source registries, 4 copiers) | 2,054 | 211 s | 2.3 GB |
| Full (4,104 source and unclear registries, 702 copiers) | 816,534 | 288 s | not measured |

1,576,434 distinct English texts have at least one file outside the 4,806 registries. 112,463 of them are exact copies; the other 1,463,971 were searched in the band index, 36 parts of about 45,000 texts, 6 seconds per part.

## Links

| Link | Candidates | Dropped by line check | Kept texts | Copy files |
|---|---:|---:|---:|---:|
| `exact` | 112,463 | 0 | 112,463 | 1,121,631 |
| `near` | 123,422 | 3,977 | 119,445 | 199,814 |
| `near_contained` | 32,487 | 329 | 32,158 | 48,120 |
| `cluster` | 26,224 | 2,711 | 23,513 | 44,515 |
| **all** | 294,596 | 7,017 | 287,579 | 1,414,080 |

18.2% of the English texts outside the registries copy a source registry version. Exact copies are 39% of the linked texts but 79% of the copy files: an unchanged skill is often in many repos. No link had a missing text.

`near` Jaccard: lowest 0.805, median 1.00. A near link at 1.00 with a different blob is the same text after normalisation and boilerplate removal (front matter, whitespace or a template line changed).

**Why 64 bands of 2 values.** 13,078 of the 32,158 kept `near_contained` links have a Jaccard under 0.5 (5,436 under 0.3). With stage 5b's 25 bands of 5 values a pair at Jaccard 0.4 shares a band only about 1 time in 4, so most of these would not have been candidates.

**The line check.** It drops 2.4% of the fuzzy links: 3.2% of `near`, 1.0% of `near_contained`, 10.3% of `cluster`. Cluster links fail most often because they were never scored: the main pipeline's 0.80 clusters group texts whose nearest neighbour is similar, so a member can be several edits away from the version its cluster voted for. Example dropped links: `anthropics/claude-plugins-official::plugins/hookify/skills/writing-rules` (cluster, 0.35 of the lines kept), `iterate/iterate::.agents/skills/shadcn` (cluster, 0.38).

**Folder names as a rough check.** The copy file's folder has the same name as the linked lineage's folder in 89% of exact files, 77% of near, 71% of near_contained and 70% of cluster files. A different name is not a wrong link (repos rename skills), but the order follows how strict each method is.

## Copy files

| | Files | Repos |
|---|---:|---:|
| Copy files | 1,414,080 | 60,958 |
| Fork | 1 | |
| Same owner as the registry | 13,926 (exact 11,081, near 2,282, near_contained 542, cluster 21) | |

Forks are almost absent because the dataset itself has only 3 fork repos among 282,200. Stage 6 clones all 60,958 copy repos. The main pipeline's `wild_history` would have covered 1,230 of these files (21 repos completely), so it is not used; see the [code flow](../code_flow/11_dataset_copies.md).

Roots with the most copy files:

| Root lineage | Files | Repos |
|---|---:|---:|
| `anthropics/skills::skills/skill-creator` | 7,085 | 3,142 |
| `anthropics/skills::skills/frontend-design` | 4,635 | 3,478 |
| `anthropics/skills::skills/pdf` | 3,378 | 1,882 |
| `anthropics/skills::skills/xlsx` | 3,134 | 1,604 |
| `nextlevelbuilder/ui-ux-pro-max-skill::.claude/skills/ui-ux-pro-max` | 2,486 | 1,671 |

## Copier pass (Pass 2)

| | Texts | Files outside the registries |
|---|---:|---:|
| Unlinked texts held by a copier | 83,594 | 282,429 (184,956 repo-text pairs) |
| – held only by weak copiers (confidence < 0.5) | 1,749 | 12,549 |

`majiayu000/claude-skill-registry` had the text first for 74,894 of the 83,594 texts, then `aiskillstore/marketplace` (1,273), `LeoYeAI/openclaw-master-skills` (873) and `hashgraph-online/awesome-codex-plugins` (859). 216 of these texts had a Pass 1 link that the line check dropped.

The stage 3c revision measured 215,460 repo-text pairs held only by copiers. Here there are 184,956, because Pass 1 now also links near, contained and cluster copies of source registries that a copier holds as an exact blob.

## Dev run

2,406 linked texts (exact 147, near 1,168, near_contained 97, cluster 994), 8 dropped by the line check, 10,679 copy files in 3,969 repos, 0 forks, 23 same-owner files. 115,210 unlinked texts are held by one of the 4 dev copiers, almost all by `majiayu000/claude-skill-registry`. Dev indexes only the 26 dev source registries, so most copies are not found and the copier pass sees many more unlinked texts than in the full run.

## Checks

- One row per text in `text_links` (287,579) and per `(repo, path)` in `copy_candidates` (1,414,080).
- Every kept non-exact link has `similarity_verified >= 0.50`; every exact link's version has `blob = file_sha`.
- No copy file is in a registry repo (copiers included).
- No `copier_seen` text is in `text_links`.

## Sample rows

```text
file_sha: 3e5b7e8911695898592eb417537f734c24fd0668
lineage_id: jeremylongshore/claude-code-plugins-plus-skills::plugins/testing/load-balancer-t…
version_seq: 4, link: near, jaccard: 1.0, containment: 1.0, similarity_verified: 1.0

repo: 0-CYBERDYNE-SYSTEMS-0/FFT_nano
path: .claude/skills/add-telegram/SKILL.md
lineage_id: nanocoai/nanoclaw::.claude/skills/add-telegram (root the same)
version_seq: 1, link: near, jaccard: 0.961, containment: 1.0, similarity_verified: 1.0
is_fork: False, same_owner_as_registry: False

file_sha: 00003364786d9725bbf2e157cbf6b9ba09df1218
copier_first_date: 2026-01-24 16:18:26+00:00
copier_first_repo: majiayu000/claude-skill-registry
copier_repos: ['majiayu000/claude-skill-registry'], weak_copiers_only: False
```
