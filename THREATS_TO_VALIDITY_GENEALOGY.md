# Threats to Validity: Skill Genealogy

Here is what limits the genealogy results. A link says two texts are the same skill; it does not prove who copied whom. Numbers are from the full run in `src/data/skill_genealogy/` (November 2026). The design pages in [docs/skill_genealogy/design](docs/skill_genealogy/design/README.md) list the limits of each stage.

## Construct validity

1. **Origin is not authorship.** The origin is the earliest version found among the 4,806 selected registries. The true author may not be a registry, may not be in the dataset, or may have rewritten the git dates. 97,839 copy files (6.9%) are dated before the release they match: either the repository had the text first, or a date is wrong. Their link is read as "same skill as", not "copied from".
2. **"Stale" counts every missed release, whatever it changed.** A typo fix counts as much as a fixed command. 36% of behind or stale copies missed an update that added a command, a network reference or a script, but that is a pattern rule, not a hand label, and it does not identify a security or correctness fix. No registry change has been hand-labelled yet, so the claim that copies keep defects their source fixed is not supported.
3. **Change labels are rules.** `substantive` means at least 5% of body lines differ; `cosmetic` means fewer. `local` means "not a known registry text", not "a person wrote this". A script risk flag is a regex match on the script text and can match a comment.
4. **Families of independent texts are similarity chains.** In families with no registry source, the parent is the closest earlier text by MinHash. The largest such families (`cluster:38`, 59,851 texts in 3 repos) are near-identical texts inside a few repos, not people copying from each other.

## Internal validity

5. **Links have measured recall, not precision.** On 42,554 copies proven by git history, the line check finds 98.4%, containment 88.8%, MinHash Jaccard 70.6% and exact match 16.9%. The proven set was drawn from copies that the dataset-copy search had already linked, so these are upper bounds for unchanged texts; the column for texts changed since the crawl (93.0% for the line check) is the fairer comparison. Precision of `near`, `near_contained` and `cluster` links was not measured by hand.
6. **The share of unchanged copies depends on how copies are found.** 79% of copy files are exact links, which are 0.6% substantive. `cluster` links are 84.0% substantive. A pooled share mixes links of very different looseness, so RQ2 is reported by link kind.
7. **Registry kinds are rule-based and unchecked.** The source or copier label comes from a date vote and a README vote. 487 registries have a confidence below 0.5, and none has a hand label yet. A copier mislabelled as a source would become the origin of copies it took from elsewhere.
8. **Git dates can be wrong.** Dates are the commit dates git stores, which authors can set. A squashed or re-created history moves a copy's first date. 23,141 `copy` sync events are dated before the release they took.
9. **Comparison dates differ.** Copies with history are compared on the registry snapshot day (November 2026; 65.7% stale). Dataset-only copies are compared on the July 2026 capture day (39.3% stale). Snapshots are up to 5 days early, because the mining day of each registry was not recorded. The pooled 56.8% mixes the two.
10. **The stale cutoff moves the result.** The stale share is 56.8% at 30 days, 49.4% at 90 days and 27.2% at 180 days. The up-to-date share (27.7%) does not depend on the cutoff.
11. **Auto-syncing repos move the update lag.** 33,951 of 61,424 `update` events come on the release day from 672 repos that sync automatically, which pulls the median update lag to 0 days. Without them the median is 8 days.

## External validity

12. **GitSkills is a code-search sample.** It holds only part of each repository's skills. The tree check promoted 1,240 under-counted repositories, including `anthropics/skills` and `obra/superpowers`, but other sources may still be missing.
13. **One hub carries a quarter of the copies.** 368,336 copy files (26.0%) are linked to lineages of `sickn33/agentic-awesome-skills`. Without them the stale share falls from 56.8% to 49.2% and the up-to-date share rises from 27.7% to 33.5%. Leaving out same-owner copies (57.1%), copies in `majiayu000/*` repos (57.0%) or copies dated before their release (58.3%) changes little.
14. **One copier is most copier folders.** `majiayu000/claude-skill-registry` holds 312,153 of the 385,400 copier folders. Copiers are never origins, so it changes RQ1 counts (`copier_seen` texts) more than RQ2 and RQ3.
15. **The time window is short.** 91% of copied folders first appear between January and June 2026. Most lineages are months old, and a copy has had little time to fall behind or update.
16. **English only.** 244,696 texts (13.0%) are not English and are left out; a translated copy cannot be matched line by line.
17. **Forks are absent.** GitSkills holds 3 fork repositories, so results with and without forks are the same.

## Coverage

18. **Not every copy has a history.** 5,646 copy repositories (9.3%) were gone by the download, 3 mirrors were too large to clone, and 16,036 folders in cloned repos have no `SKILL.md` history. 317,460 copy files (22%) are measured from their dataset text only, with no sync events.
19. **Not every folder is known.** 248,779 dataset-only copies were never listed by GitSkills, so they have no folder comparison and no package lag.
20. **Registry histories are incomplete.** Of the 4,104 source and unclear registries, 4,045 have an English lineage; 59 have none. 33 registries were gone, and `ClickHouse/ClickHouse` and `Expensify/App` failed to download. A skill deleted in a way git does not show has no history.

## Reliability

21. **Dev runs are not samples of the full run.** The 30-registry dev sample is mostly `cluster` and `near` links and gives very different shares (for example 89% substantive). Dev numbers are smoke tests only.
22. **Older review numbers are from a different run.** The reviews in `docs/skill_genealogy/reviews/` used tables built on 2026-11-04 with 3,566 registries, before the origin fix (B01). Use the numbers here and in the stage reports.
