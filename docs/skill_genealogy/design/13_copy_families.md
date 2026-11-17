# Design: copy families (stage 7a) and link method recall (stage 7b)

Cards G16, G17 · Code: `assign_families.py` · [Code flow](../code_flow/13_copy_families.md) · [Report](../report/13_copy_families_report.md)

## Problem

Stage 5c links a dataset text to a registry skill by its current text alone. Three things are still missing for RQ1. A copy whose text drifted after copying may have no text link, or a weak one, although its own history proves it. A link names a lineage, but not the version the copy started from, and a version released after the copy existed cannot be its source. And the texts with no link at all still need a label: copied from a crawler, written twice independently, or found once.

## Design at a glance

Read each copy's own `SKILL.md` history (stage 6). Add two history links: a history blob equal to a registry blob (`history_exact`), and a first version near a registry text (`history_near`). Keep the strongest of the text and history links per file and resolve it to the root lineage. Pick the origin version among the versions released on or before the copy's first commit. Group copies by how many copies arrived in the same commit. Give every English text one family, in a fixed rule order.

## Decisions

**History evidence ranks right below an exact text match.** A history row whose blob is a registry blob is a hash match: proof, not an estimate. It beats every MinHash link on the current text. Only an exact current text ranks higher, because it needs no history at all. `history_near` is a MinHash estimate on an older text, so it ranks below the text-based near links and above the cluster vote.

**Only the first downloaded version is searched for `history_near`.** The first version is the closest the history gets to the moment of copying. Searching every version would link files that later converged on a registry text, and it would cost a band lookup per version.

**The family is the root lineage.** A copy of a re-hosted skill belongs to the registry that first published it (stage 5b), so one skill is one family however many registries re-host it.

**The origin is chosen by date, and never guessed.** Without the date limit, a near match could name a version released after the copy existed. A copy whose linked lineage has no version by its first commit is `copied_before_release`, with no origin: either the repo had the text first, or its git date is wrong. A copy with no date at all gets `no_date`; `discovered_at` is the crawl date and is never used as a commit date. An exact link keeps its identical version in every case, because no date can make a hash match wrong.

**The origin is compared with the copy's first text.** The version a copy started from is the one closest to what it first committed, not to what it holds today. The current text is used only when there is no history.

**Arrival group per copy, not a collection label per repo.** Most copies arrive many at a time, and those are edited far less afterwards. Repo size and registry count did not separate repos that store copies from repos that use them (30 hand-checked repos), while one app can install a 1,000-skill bundle and still be a project. So each copy folder gets the size of its own arrival batch, counted over copy folders only, and every later table can be split by it.

**"Arrived with the first upload" uses the repo's creation date.** Stage 6 logs only the copy folders, so the first commit in that history is the first copy commit, not the repo's first commit. GitHub's `created_at` is known for every repo. A copy that arrived within a day of it came with the initial upload, which is the case where a squashed project upload can make hand-picked copies look like a bundle.

**A copier-held text is never independent.** A text that a copier registry holds came from somewhere, even if no source registry has it. It is labelled `copier_seen` before clusters are looked at, and each of its files is dated against the copier: the earlier side is the likely origin.

**Clusters count only unclaimed texts.** An independent family is two or more texts that no source or copier explains. A cluster whose other members are all linked would otherwise make a one-text "independent" family.

**Family totals are given with and without forks and the registry owner's own repos.** A fork shares the registry's history, and an owner moving their own skill is not reuse; both inflate spread.

**Method recall is measured on copies proven by their own history (stage 7b).** A `history_exact` file held a registry blob at some commit, so it is a copy without a human label. Its newest text is scored against every version of its lineage by each stage 5 text method, with the same cutoffs and signatures as stage 5c. The matching blob itself is not scored, because every method would find it. Every version counts, because a copy may follow a newer or older registry text than the one it took. The table is evidence for which method stage 5c should trust for edited copies.

**The bias of the test set is reported, not removed.** Stage 6 cloned only copies that stage 5c had linked, and an exact copy wins `exact` rather than `history_exact`. So the set holds proven copies that had already drifted from every exact registry text. They were found by some text method on their crawled text. Copies that no method found at all were never cloned, so they cannot be in the set. `recall_changed_since_crawl` keeps only texts stage 5c never saw, and is the least circular number.

## Alternatives rejected

- **The newest version before the date as the origin, without similarity.** A copy often starts from an older version that a repo had lying around; the newest released version would overstate how current copies are.
- **Search every history version for `history_near`.** More links, but many from late convergence, and much slower.
- **The repo's first commit in the stage 6 history for `arrival_in_first_commit`.** True for almost every repo with one copy folder, so it says nothing.
- **A repo-level "collection" flag.** Did not match the hand labels, and would mislabel projects that install bundles.
- **All files with any `history_exact` row as the recall test set.** Most of them have an exact current text, so exact recall would be near 1 by construction and say nothing about edited copies.
- **Scoring the dataset (crawled) text of a proven copy.** Stage 5c linked exactly that text, so `any_text_method` would be close to 1 by construction.

## Effect

History links add 42,554 `history_exact` and 762 `history_near` winners to the 1,414,080 copy files. The origin is exact for 82% of files and `similar_before_date` for 13%; 2.9% have `no_date` and 6.9% are `copied_before_release`. Of the copy folders with history, 78% arrived as bundles. Among the 1,633,285 English texts: 335,976 are in source families, 89,212 are `copier_seen`, 269,847 are independent and 938,250 are singletons. On the 42,554 history-proven copies, recall is 0.169 for exact, 0.706 for MinHash Jaccard, 0.888 for containment and 0.984 for the line check. On the 6,230 texts changed since the crawl it is 0.247, 0.598, 0.818 and 0.930: the line check is the method that keeps finding edited copies. The full-run numbers are in the [report](../report/13_copy_families_report.md).

## Limits

- Only linked copies were cloned in stage 6, so an unlinked text cannot gain a history link, and `copier_seen` files are dated only by the dataset `first_commit_at`, which most files lack.
- The origin is taken from the linked lineage. When that lineage is a later re-host stage 5b did not trace to its root, the copy looks `copied_before_release`.
- History is today's default branch; copies the dataset saw elsewhere, or in deleted repos, have no history and an `unknown` arrival group.
- Git dates can be rewritten by a rebase or squash.
