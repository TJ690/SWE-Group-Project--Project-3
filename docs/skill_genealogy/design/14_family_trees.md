# Design: family trees (stage 8)

Card G18 · Code: `build_family_trees.py` · [Code flow](../code_flow/14_family_trees.md) · [Report](../report/14_family_trees_report.md)

## Problem

Stage 7 says which family a text belongs to, but a family is a bag of texts. RQ1 asks how large and how deep a family is: how many texts and repos a skill reached, and how many copy steps lie between a registry version and its farthest copy. A bag cannot give a depth, and the viewers need an ordered structure to draw. The structure has to be built without knowing who copied whom, because git does not record it.

## Design at a glance

One tree per family. A source family starts with its registry versions at generation 0. Every other text hangs under the most similar text that is older than it, so a generation is the number of steps from the registry. An independent family has no registry, so its oldest dated text is the root. Singletons and copier-only texts are one-node trees. A summary table has one row per family.

## Decisions

**The parent is the most similar older text, and it is called that.** Two texts with a high MinHash similarity and an earlier date are the best evidence the data offers for "copied from". It is not proof: the real source may be a text that was never published or crawled. Every figure, viewer label and thesis sentence calls the link a parent or an ancestor candidate, never a source.

**Only older nodes can be parents.** A text cannot come from one that did not exist yet, so a parent must have an earlier date. An equal timestamp is not earlier, which also makes cycles impossible: a parent is always before its child in the processing order, and generations come out in one pass.

**Registry versions are candidates by release date.** A copy can only come from a version that was released before it. Candidates are ordered by release date, so a tie, including a text that a registry and its re-host both hold, keeps the earliest release. Ordering them by lineage id would have picked the re-host whenever its name sorted first.

**A copy older than every release hangs on the earliest version.** A copy whose first commit is before the registry's first release has no registry version it could have come from. Leaving it parentless would give it generation 0 in the same column as the registry versions and understate depth. It is attached to the earliest registry version, which is the closest registry anchor in the family. Its parent is then newer than the copy, and the card text calls that out: the registry published a skill that already existed elsewhere, so the link says "same skill", not "copied from".

**Registry versions have generation 0, copies start at 1.** Generation counts copy steps. A re-hosted lineage stays at 0, and the version it was copied from is kept as its parent so the chain is visible.

**An independent family is rooted on its oldest dated text.** With no registry to anchor it, the oldest text is the best guess for the start. When no text has a date, the text held by the most files is taken, and `root_basis` says so. Most independent families have no dated text, so most roots are `most_copied`; a reader of any depth statistic of independent families should know that.

**Undated texts attach but never become parents.** An undated text may be older than anything, so letting it be a parent would put an arbitrary order into the tree. It is attached to the nearest node so it is not lost, and has no children.

**Dates come from the best source each text has.** A copy file has a history-based first commit from stage 6. Any other text has only the dataset `first_commit_at`. The two are both git dates. They are not identical in meaning, and the design accepts that rather than leaving the second group undated.

**A cap of the latest 2,000 earlier texts.** The largest family has 59,851 texts, and comparing every pair is quadratic. The latest 2,000 are the most plausible parents. A text that lost candidates is marked `approx`, and the summary says which families contain one.

**Leaves are not compared.** A singleton or copier-only text has no one to compare with, and about a million of them (1,019,658 in the full run) would only slow the run. They are written in one frame.

**A summary row says what was and was not built.** `status` is `built`, `registry_only` or `skipped`, and `root_basis` is `none` when no node exists. A family with no nodes must not claim a root, because a count of roots by basis would then include families that have no tree.

**A failed run keeps the old outputs.** `--force` replaces the files only when the new build has finished, because a full run is long and a crash should not delete a good result.

## Alternatives rejected

- **A parent without the date rule (the most similar text of the family).** Two near copies would point at each other, and a registry version could have a copy as its parent.
- **Attaching an old copy to the most similar registry version, whatever its release date.** Better numbers for the similarity, but it places a copy under a version it cannot descend from.
- **Alphabetical order of lineages as the tie rule.** Arbitrary, and wrong for re-hosts.
- **Comparing every earlier text.** Exact, but unusable on the largest family.
- **Giving a copy older than every release no parent.** It looks like an independent origin, and it drops out of the depth of its family.
- **Rooting an independent family on the text with the most similar neighbours (a medoid).** A better centre, but it is not a time order, so it would not read as "older".

## Effect

The full run builds 136,608 trees and 1,019,658 one-node leaves: 2,022,027 nodes in 1,202,176 families (766 seconds). Among the 104,882 built families of more than one text, the median is 2 texts and 2 repos and the depth is 1 at the median and the 90th percentile (maximum 13). 490,974 text nodes have a parent, 49% of them the identical blob. 9,654 copies are older than every registry version of their family and hang on the earliest one. Only `cluster:38` reaches the cap. The details are in the [report](../report/14_family_trees_report.md).

## Limits

- The tree is a set of closest-earlier links, not a copy history. The real source may be missing from the data, and two texts with the same date are never linked.
- The two kinds of date (history-based, dataset) are not the same measurement, so an ordering between a copy file and another text of the same family can be off.
- Most independent roots are chosen without a date.
- A copy older than every registry version has a parent that is newer than it.
- The 2,000-text cap makes the largest family approximate.
- Families made only of registry texts have no tree.
