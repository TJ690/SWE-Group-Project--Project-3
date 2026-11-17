# Copy families report

Results of stages 7a and 7b, read from `src/data/skill_genealogy/` on 2026-11-10. How the step runs is in [copy families](../code_flow/13_copy_families.md); why it is built this way is in the [design note](../design/13_copy_families.md).

## How the tables were built

| Date | Run | What it did |
|---|---|---|
| 2026-11-10 | Full run (`python -m skill_genealogy.assign_families`), 172 seconds | Read 1,637,779 own `SKILL.md` history rows of 1,096,902 copy files, signed 33,210 history texts, wrote the four tables |
| 2026-11-10 | `--force`, 172 seconds | Same numbers; every date column of `copy_files` is now stored in microseconds |
| 2026-11-10 | `--force` with stage 7b, 611 seconds | Same stage 7a numbers; scored 42,554 proven pairs (about 7 minutes of line diffs) and wrote `proven_pairs` and `link_method_recall` |

## Links per copy file

1,414,080 copy files in 60,958 repos. Each keeps its strongest link.

| Link | Files | Share |
|---|---:|---:|
| exact | 1,121,631 | 79.3% |
| history_exact | 42,554 | 3.0% |
| near | 168,774 | 11.9% |
| near_contained | 41,324 | 2.9% |
| history_near | 762 | 0.1% |
| cluster | 39,035 | 2.8% |

890,526 files have a history row equal to a registry blob. Most of them already have an exact text link, which ranks higher. For the 42,554 files where `history_exact` wins, the history proves a copy that the current text shows only as a near, contained or cluster match. 5,943 of the winning history links rest only on commits that a registry history also holds (inherited).

`history_near` was tried on the 33,737 files with history and no link stronger than cluster. It linked 762 of them. A first version that already matched would mostly have been found by stage 5c, so the yield is small.

1,096,902 files (77.6%) have their own `SKILL.md` history; 317,178 have none.

## Origin version

| Link | exact origin | similar before date | no date | copied before release |
|---|---:|---:|---:|---:|
| exact | 1,121,631 | | | 71,122 (origin kept) |
| history_exact | 42,554 | | | 2,797 (origin kept) |
| near | | 129,219 | 27,647 | 11,908 |
| near_contained | | 30,197 | 7,256 | 3,871 |
| history_near | | 677 | | 85 |
| cluster | | 25,534 | 5,445 | 8,056 |

- `no_date`: 40,348 files, **16.2% of the non-exact links**. Those files have no downloaded history and no dataset `first_commit_at`; they keep their link and family.
- `copied_before_release`: 97,839 files (6.9%). For 73,919 exact and history_exact files, the identical registry version was released after the copy's first commit: the repo had the text first, or one of the two git dates was rewritten. For the other 23,920, no version of the linked lineage existed yet, so they have no origin.

## Arrival groups

| Group | Folders | Files | Repos | Arrived with the repo's first upload | Removed |
|---|---:|---:|---:|---:|---:|
| chosen (1–4) | 103,419 | 103,420 | 40,993 | 50,461 (48.8%) | 7.8% |
| small_set (5–19) | 139,470 | 139,470 | 12,909 | 65,641 (47.1%) | 7.9% |
| bundle (20+) | 853,690 | 853,730 | 5,510 | 433,444 (50.8%) | 5.5% |
| unknown | 317,459 | 317,460 | 6,756 | | |

Of the copy folders with history, 78% arrived as bundle copies. 65,866 of them (6.0%) were deleted before the download (`removed`). The `unknown` files are in repos that are `gone` (169,878 files) or `too_large` (131,522, the three mirrors). The other 16,060 are in `ok`, `error` or `empty` repos whose folder has no own `SKILL.md` history. 282 `unknown` files do have history rows, but only deletes: the folder's `SKILL.md` was removed, so there is no arrival commit, and they are marked `removed`.

About half of each group arrived within a day of the repo's creation. For chosen copies this is the expected case: a new project started with one or two skills in it. For a bundle it is the case where a squashed project upload can hide copies that were chosen one by one.

**Hand check.** In the 8 repos the user labelled stored, 99.2% of the 3,847 copy folders with history are bundle copies. The 18 repos labelled used are close behind at 97.4% of 7,048 folders. As expected, stored repos hold bundles, but the arrival group does not separate stored from used among repos of this size: all 30 checked repos have 100 or more copies, and both kinds install them in large commits. The group describes how a copy arrived, not whether the repo uses it.

## Families

| Family kind | Texts | Families | Files |
|---|---:|---:|---:|
| source | 335,976 | 108,779 | 1,568,751 |
| copier_seen | 89,212 | 89,212 | 313,347 |
| independent | 269,847 | 65,935 | 436,999 |
| singleton | 938,250 | 938,250 | 1,131,907 |

Of the 1,633,285 English texts, 56,851 live only inside registry repos (`registry_self`). Source families count the registry's own files too.

GitSkills marks only 3 of its 282,200 repos as forks, so the totals with and without forks differ only by files whose owner owns the family's root registry.

## Texts a copier holds

282,429 files outside the registries hold a `copier_seen` text.

| Result | Files | Share |
|---|---:|---:|
| repo_first | 16,712 | 5.9% |
| copier_first | 4,908 | 1.7% |
| no_date | 260,809 | 92.3% |

12,549 files hold texts that only weak copiers (stage 3c confidence below 0.5) hold. **Most files cannot be dated**, because these files were not cloned in stage 6 and the dataset `first_commit_at` is empty for most rows. Among the dated files, the dataset repo had the text first more than three times as often as the copier. That fits copiers being crawlers.

## Link method recall (stage 7b)

The test set is the 42,554 copy files whose winning link is `history_exact`. Each file's newest history text is scored against every version of its linked lineage. 6,230 of these texts changed after the crawl. GitSkills marks almost no repo as a fork, so recall without forks is the same.

| Method | Found | Recall | Recall on the 6,230 texts changed since the crawl |
|---|---:|---:|---:|
| exact (same normalised text) | 7,180 | 0.169 | 0.247 |
| MinHash Jaccard ≥ 0.80 | 30,028 | 0.706 | 0.598 |
| containment ≥ 0.80 | 37,794 | 0.888 | 0.818 |
| line check ≥ 0.50 | 41,892 | 0.984 | 0.930 |
| any text method | 41,900 | 0.985 | 0.931 |

Split by the file's stage 5c link and by whether the text changed since the crawl:

| Stage 5c link | Changed | Pairs | exact | Jaccard | containment | line check | missed by all |
|---|---|---:|---:|---:|---:|---:|---:|
| near | no | 26,918 | 0.208 | 0.977 | 0.988 | 0.994 | 157 |
| near | yes | 4,122 | 0.269 | 0.779 | 0.896 | 0.954 | 188 |
| near_contained | no | 5,508 | 0.000 | 0.000 | 0.965 | 0.994 | 33 |
| near_contained | yes | 1,288 | 0.162 | 0.214 | 0.773 | 0.877 | 158 |
| cluster | no | 3,898 | 0.010 | 0.000 | 0.202 | 0.992 | 31 |
| cluster | yes | 820 | 0.271 | 0.293 | 0.495 | 0.894 | 87 |

What the numbers say:

- **The line check finds almost every proven copy, and MinHash Jaccard misses most edited ones.** A copy that grows keeps the registry lines but falls under a Jaccard of 0.80. Containment sits in between. On texts that changed after the crawl, Jaccard finds 60% and the line check 93%.
- **Exact finds 17%, even though no file in the set has an exact blob link.** These texts equal a registry version after normalisation (spaces, line endings, blank lines) but not byte for byte, so stage 5c, which compares blobs, called them near.
- **654 proven copies (1.5%) are missed by every method.** Most of them changed after the crawl (433). The 221 unchanged ones were linked by stage 5c to another lineage than the one their history proves, so the text matches a different registry skill. Example: `phatblat/dotfiles` `.claude/skills/brainstorm/SKILL.md` once held a blob of `Aedelon/claude-code-blueprint::skills/brainstorm`, and its current text shares no shingle and no line with any version of it.
- **The set is not a random sample.** Stage 6 cloned only copies stage 5c had linked, so every pair was found by some text method on its crawled text. The first group (near, unchanged) is close to 1 by construction. Use the changed-since-crawl column as the method comparison.

## For later stages

- Stage 8 (trees) and stages 9–10 read `copy_files.parquet`. The 97,839 `copied_before_release` files and the 40,348 `no_date` files have no `similar_before_date` origin. Exact ones keep their identical version.
- Every RQ table should be split by `arrival_group`. The 317,460 `unknown` files have no history, so stages 9–10 can measure them only from their current text.
- Removed copies (65,866 folders) are measured only up to `removed_date`.
