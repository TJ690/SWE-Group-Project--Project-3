# Design: copies in the dataset (stage 5c)

Card G14 · Code: `find_copies.py` steps 10–18 · [Code flow](../code_flow/11_dataset_copies.md) · [Report](../report/11_dataset_copies_report.md)

## Problem

Most copies of a registry skill are not in another registry; they sit in ordinary repos of the dataset. RQ1 needs to know which dataset texts come from a registry, RQ2 needs the exact version each came from, and RQ3 needs that version to say how far behind the copy is. Copies are rarely byte-identical: people edit names, add sections, or keep a skill inside a larger file.

## Design at a glance

Link each English dataset text found outside the registries to **one registry lineage version**, in a fixed order: `exact`, `near`, `near_contained`, `cluster`. Check every link that is not exact with a line diff. Then list every copy file, with fork and same-owner flags. A second pass records which unlinked texts a copier registry holds, as evidence only.

## Decisions

**Compare with every registry version, not the newest.** The registry side is the full history from stage 3, so a text copied a year ago still meets the version it was copied from. This is also why an old dataset snapshot is fine for linking: a link says where a text came from, which does not change with time (see [code flow](../code_flow/11_dataset_copies.md), "Why an old dataset snapshot is fine for linking").

**A fixed link order, one link per text.** The order goes from strongest to weakest evidence: the same blob, then nearly the same text, then the registry text inside a larger one, then a near-duplicate cluster vote. A text keeps the first link it gets, so a weaker method never overrides a stronger one.

**Ties go to the oldest version.** Several versions can equal a text (a revert, or a skill re-hosted by several registries). The oldest is the earliest possible source.

**One finer LSH index.** A contained copy can have a low Jaccard: 13,078 kept contained links share less than half of their phrases with the registry text. The stage 5b index (bands of 5) rarely finds those, so this stage uses 64 bands of 2 values. It costs about 581 band hits per text, still a few seconds per part.

**Cluster links borrow the main pipeline's near-duplicate clusters.** A text MinHash missed, sitting in a 0.80 cluster whose linked texts mostly point to one lineage, takes that lineage. Only the 0.80 clusters are used; the 0.60 clusters are too loose.

**A line diff checks every fuzzy link.** MinHash compares sets of phrases, not lines, and cluster links are never scored. A link is kept only when the copy still has at least half of the registry text's body lines (`MIN_VERIFIED_LINE_SHARE`). Same words with other line breaks have Jaccard 1.0 and line share 0, and are dropped. Every dropped link is saved with its reason.

**Files inside any registry are not copies.** A registry's own files, copiers included, are its history, not a dataset copy.

**A copier match is evidence, not a link.** A copier is not an author. For texts still unlinked, the step records the copiers that hold the exact blob and which had it first. Stage 7a decides whether the copier or the dataset repo had the text first. "No registry match" therefore never means "self-written".

**Flags, not filters.** Forks and repos of the registry's own owner are flagged and kept, so later stages can report with and without them.

**No dataset history column.** The main pipeline's `wild_history` covers only 1,230 of 1.4 million copy files, holds only `SKILL.md`, and is an older snapshot. Copy history comes from stage 6 only.

## Alternatives rejected

- **Link to the newest registry version.** Would date every copy to the latest release and make staleness zero by construction.
- **Accept MinHash links without a line check.** 7,017 links fail the check (3,977 near, 329 near_contained, 2,711 cluster); cluster links fail most often for their number, because they were never scored.
- **Treat a copier as the source when no registry matches.** Would credit crawlers with authorship.

## Effect

287,579 of 1,576,434 English texts outside the registries are linked (112,463 exact, 119,445 near, 32,158 near_contained, 23,513 cluster). 1,414,080 copy files in 60,958 repos. 83,594 unlinked texts are held by a copier.

## Limits

- Only source registries are linked; a copy of a small non-registry repo is unlinked here (stage 7a finds it from history).
- A copy more than about 5 times the size of its source is often missed.
- The copier pass is exact only.
- Copies made after the dataset crawl are outside the study.
