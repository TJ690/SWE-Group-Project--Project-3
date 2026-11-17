# Family trees report

Results of stage 8, read from `src/data/skill_genealogy/` on 2026-11-14. How the step runs is in [family trees](../code_flow/14_family_trees.md); why it is built this way is in the [design note](../design/14_family_trees.md).

## How the tables were built

| Date | Run | What it did |
|---|---|---|
| 2026-11-14 | Full run (`python -m skill_genealogy.build_family_trees`), 766 seconds | Read the stage 7 tables for 1,633,285 English texts, dated them, loaded 1,371,883 signatures, built 136,608 trees and 1,019,658 one-node leaves, wrote `family_tree.parquet` and `family_summary.parquet` |

The trees took about 11 minutes. 1 family (`cluster:38`) hit the 2,000-text cap.

## Size of the tables

| Table | Rows | Meaning |
|---|---:|---|
| `family_tree` | 2,022,027 | 445,593 registry versions and 1,576,434 text nodes |
| `family_summary` | 1,202,176 | one per family |

Every node id is unique, every parent exists and belongs to the same family of its child, and no text node has a parent of the wrong generation. The 374,787 registry versions whose parent is the previous version stay at generation 0 by design.

## Families

| Family kind | Families | Trees built | Registry only |
|---|---:|---:|---:|
| source | 108,779 | 70,806 | 37,973 |
| independent | 65,935 | 65,802 | 133 |
| copier_seen | 89,212 | 83,594 | 5,618 |
| singleton | 938,250 | 936,064 | 2,186 |

`registry_only` families hold only texts that are a registry's own, so they have no text node to place. Their summary row has `status = registry_only` and `root_basis = none`. 45,910 families are in this state.

**Root basis of the 1,156,266 built families.** 1,019,658 have one text (`single`), 70,806 have a registry trunk, 39,803 are independent and rooted on the text with the most files (`most_copied`), and 25,999 on the oldest dated text (`oldest_dated`). 60% of the multi-text independent families have no dated text.

## Size and depth

Families with more than one text and a built tree: 104,882.

| | Texts median | Texts p90 | Texts max | Repos median | Repos p90 | Repos max | Depth median | Depth p90 | Depth max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| source (39,080) | 3 | 14 | 950 | 3 | 13 | 781 | 1 | 2 | 13 |
| independent (65,802) | 2 | 4 | 59,851 | 2 | 3 | 140 | 1 | 1 | 8 |
| all | 2 | | | | | | 1 | 1 | 13 |

8,313 families are at least 2 steps deep. The mean generation of a copy is 1.07 in source families and 0.58 in independent ones. Over all copies, 1,085,460 are at generation 0 (leaves and roots), 365,210 at 1, 50,991 at 2 and 13,977 at 3. Generation 4 holds 55,355 copies, 50,231 of them in `cluster:38`.

The biggest families:

| Family | Kind | Texts | Repos | Depth |
|---|---|---:|---:|---:|
| `cluster:38` | independent | 59,851 | 3 | 4 |
| `cluster:590` | independent | 3,935 | 4 | 2 |
| `cluster:580` | independent | 3,897 | 1 | 2 |
| `anthropics/skills::skills/skill-creator` | source | 950 | 781 | 10 |
| `anthropics/skills::skills/frontend-design` | source | 808 | 752 | 8 |

The largest independent families are clusters of near-identical texts, mostly inside one to four repos (`cluster:580` is in a single repo). Their depth is a chain of the closest earlier text, not evidence of people copying from each other, and for `cluster:38` it also depends on the 2,000-text cap.

## Parents

490,974 text nodes have a parent: 287,579 in source families (all of them) and 203,395 in independent families (all but the 65,802 roots). Singletons and copier-only texts are one-node trees.

| Parent | Text nodes |
|---|---:|
| a registry version | 248,744 |
| another text | 242,230 |

242,809 parents (49%) are the same blob (similarity 1.0). The median similarity is 0.99, the 10th percentile 0.57. 1,022 text nodes have a parent but no similarity, because a signature is missing; they keep the earliest candidate.

9,654 text nodes (all with a registry parent) were first committed before every release of their family. Their parent is the earliest registry version, and so it is newer than they are. Read these links as "same skill as", not "copied from".

Dated text nodes: 563,494 of 1,576,434. The 1,012,940 undated ones can have a parent but never a child.

## Check against the card

- A copy of version 1 is generation 1, a copy of a copy generation 2: shown by the tests and by the tables (no generation mismatch).
- A tie between an original and its re-host goes to the earlier release: covered by a test; the data gives no failing case.
- `cluster:38` is the only family with `approx = true`: 59,851 texts and a cap of 2,000.

## What the numbers cannot say

- The parent is the closest earlier text by MinHash, an estimate. The real source may not be in the dataset.
- Dates come from two sources (stage 6 history for copy files, the dataset `first_commit_at` for the rest). They are not the same measurement, and git dates can be rewritten.
- Most independent roots are chosen without a date.
- 37,973 source families (35%) are registry texts only and have no tree. Their registry history is in stage 4a.
