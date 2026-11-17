# Design: registries copying each other (stage 5b)

Card G13 · Code: `find_copies.py` steps 4–9 · [Code flow](../code_flow/10_registry_rehosts.md) · [Report](../report/10_registry_rehosts_report.md)

## Problem

Stage 3c removed copier registries, but source registries also re-host each other's skills: `sickn33/agentic-awesome-skills` took 2,842 of its 8,871 skills from older registries. If each lineage were its own origin, one skill would have many roots. Each lineage needs a **root**: the oldest lineage its first text came from.

## Design at a glance

For each lineage's **first version**, look for an older version in **another repo** with the same blob (`exact`), a Jaccard of at least 0.80 (`near`), or at least 80% of the older text inside it (`near_contained`). Keep one source per lineage, then follow the sources to the root. Then build the final registry roles from stage 3c.

## Decisions

**Only the first version is tested.** A lineage that started as a copy is a re-host, whatever it did later. Later versions are the lineage's own edits (stage 4a). The **source**, though, can be any version of the older lineage, because a registry copies a skill as it is that day.

**Strictly older, other repo.** The same commit time is no evidence of who copied whom, and two folders of one registry with the same text are the registry's own business. Two repos of one owner do count: a template repo and a project built from it are different repos.

**Exact first, in SQL.** The same git blob is the strongest evidence and costs nothing to find with a join, even for a blob held by thousands of versions. Exact lineages skip the MinHash search.

**Containment catches copies that grew.** A registry that copied a skill and added sections has a lower Jaccard, but the whole older text is still inside. Containment is computed from the Jaccard estimate and the two shingle counts.

**A contained source needs at least 20 shingles.** A text of a few headings (`## Instructions`, `## Safety`, `## Resources`) is "contained" in many unrelated skills. Below 20 shingles, contained links joined unrelated skills; above it, the folder names agree most of the time. `near` needs no minimum, because both texts must be of similar size.

**Weak sources lose to strong ones.** Containment is capped at 1.0, so a tiny text inside a large one can reach 1.0 from one or two equal values by chance. When a candidate agrees on 20+ values, candidates with 5 or fewer are dropped.

**One LSH index of 25 bands of 5.** Pairs are found with locality-sensitive hashing: two signatures are candidates when one band of 5 values is equal. This catches practically every pair at Jaccard 0.8 and about half at 0.5, which serves both rules from one index. The index is stored as sorted numpy arrays (about 250 MB instead of several GB); a test checks it gives the same candidates as datasketch.

**Choice and chain.** Exact first, then the highest similarity, then the oldest date. The chain is followed to a lineage with no source. Every link goes to a strictly older version, so a cycle can only come from wrong dates; the walk stops, marks it, and takes the oldest lineage.

**Hub labels come from stage 3c, not from the dated share.** The share of re-hosted lineages per registry is computed as a cross-check, but it only sees re-hosts from other source registries: copiers have no lineages, and a copy from a small repo has no older lineage. Stage 3c compares every shared text and is the better label. `repo_roles_final.parquet` takes registry roles from stage 3c, and every later stage reads it.

## Alternatives rejected

- **Compare every version, not only the first.** Would treat a registry that later synced from another as a re-host.
- **A 0.80 LSH for near and a separate index for contained.** Two indexes, and the 0.80 index misses near pairs close to the cutoff.
- **The dataset's `first_commit_at` for dates.** Empty for 88% of registry texts.

## Effect

Of 188,787 lineages, 17,415 are re-hosted (8,972 exact, 7,001 near, 1,442 near_contained), with chains up to 5 steps and no cycle. 976 registries re-host at least one skill.

## Limits

- Only copies between source registries are seen; a registry that copied a small repo looks original here (stage 7a counts that case).
- A short skill pasted into a much longer one rarely shares a band and is missed.
- There is no line check here; MinHash close calls can fall either side.
