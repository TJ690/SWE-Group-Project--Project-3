# 2.8 Specificity (`specificity.py`)

**Command:**

```bash
rm -rf src/data/derived/s2_specificity_identifiers src/data/derived/s2_specificity_buckets_c*
python -m skillsig features --only specificity
```

The `rm` clears resumable parts; leave it out to resume an interrupted run. · **Tests:** `tests/s2_features/test_specificity.py` · [Back to s2](README.md)

## What it does

Scores each readable skill by the share of its identifiers (paths, flags, code names) that almost no other skill uses. A high share is positive evidence of project-specific writing: the real file paths, flags and function names of one codebase.

## How it works

**1. Identifiers.** From the first 50,000 characters of the body, with front matter and URLs removed, collect distinct strings of up to 80 characters of these kinds:

| Kind | Example |
|---|---|
| Inline code span | `` `make test-e2e` `` |
| Path | `src/api/routes.ts`, `~/.config/app` |
| File name | `docker-compose.yml` |
| Command-line flag | `--dry-run` |
| camelCase name | `fetchUserProfile` |
| snake_case name | `max_retry_count` |

**2. Pass 1** reads each raw `artifacts` file in small batches and writes one `(file_sha, identifier)` part per file to `s2_specificity_identifiers/` (738 MB). Finished parts are reused, so an interrupted run resumes.

**3. Pass 2** (DuckDB) joins each skill to its 2.6 cluster at 0.8 and counts, for every identifier, the number of **distinct clusters** that use it (a skill without a cluster counts as its own group). To bound memory, identifiers are counted in 16 hash buckets.

**4. Score.** An identifier is **rare** if it appears in fewer than 5 clusters. `specificity = n_rare / n_identifiers`; `NULL` when the skill has fewer than 3 identifiers.

## Output

`src/data/derived/s2_specificity.parquet`: 1,734,331 rows (readable skills with at least one identifier), columns `file_sha, n_identifiers, n_rare, specificity`.

**What the table contains.** One row per readable skill with at least one identifier. The 106,541 readable skills with no identifiers at all have no row.

| Group | Skills | `specificity` |
|---|---|---|
| 3 or more identifiers | 1,652,438 | A score between 0 and 1 |
| 1 or 2 identifiers | 81,893 | `NULL` (too few to judge) |
| **Total rows** | **1,734,331** | |

A typical skill (median) has **30 identifiers**, and **0.318** of them are rare: about one in three is used in fewer than 5 near-duplicate clusters across the whole dataset.

## Design decisions

- **Why identifiers?** Generic advice ("write tests", "use clear names") can be written by anyone, including an agent working from general knowledge. Real paths and function names of one project are hard to produce without that project.
- **Why count clusters, not files?** A skill copied 900 times would otherwise make its own identifiers look common, and the original's specificity would drop because it is popular.
- **Why always over the full population?** Rarity is a population fact; dev mode only limits which skills get a score.
- **Why `NULL` below 3 identifiers?** One rare identifier out of one gives 100%, which says nothing.
- **Why the first 50,000 characters?** A few skills are hundreds of thousands of characters long; identifiers past 50k add memory cost but rarely change the share.
- **Why no leakage concern?** The feature reads text only, never labels or commits.

## Full data vs dev sample

Dev mode scores only sampled skills, but identifier frequencies still come from all readable skills. The grouping uses the 2.6 clusters, which are sample-only in dev mode, so skills outside the sample count as their own groups: dev scores approximate full scores.

## Tests (6)

- Each identifier kind is extracted; empty text gives none.
- Rare counts and `NULL` below 3 identifiers.
- A run counts clusters, not copies; dev mode scores only sampled skills while frequency uses everything.
- Missing clusters names `python -m skillsig dedup`.

## Rebuild history

The table was first built on 2026-10-25, before `s2_clusters` was rebuilt on full bodies, so its rarity counts used the old clusters. It was rebuilt on 2026-10-27 on the current clusters, and 3.5 and 3.6 were rerun. 171,399 skills got a different score (mean change 0.006; median 0.314 → 0.318). The text-model AUCs moved by less than 0.001, and the ensemble's coverage at a nominal 90% proxy precision went from 1.0% to 0.9% of positives.

## Limits

- Regex-based: some prose words in backticks count as identifiers, and identifiers in plain prose without code formatting are missed unless they look like paths, flags or code names.
- Not used by the v2.1 label, only by the text models.
