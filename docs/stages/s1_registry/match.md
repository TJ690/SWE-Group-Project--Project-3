# 1.3 Match registry versions to the dataset (`match.py`)

```bash
python -m skillsig registry --only match
```

[Back to s1](README.md)

## What it does

1. Finds every dataset file whose `file_sha` equals a registry version's blob.
2. Decides which registry each matched file **originally** came from.
3. Summarises each registry skill's history (its **lineage**, `repo::skill`).

## How it works

```python
versions = _annotate(_read_versions(cfg), load_sources(cfg))      # A. prepare
con      = _connect_artifacts(cfg, dev=dev)                        # B. open dataset
matched  = match_exact(con, versions)                              # C. match (in memory)
assign_origin(matched)                     → s1_registry_matches   # D. origin
build_lineages(versions, matched, snapshot) → s1_lineages          # E. lineages
```

**A. Prepare.** Drop mirrors (so files aren't counted twice), attach tiers, drop rows missing a key or duplicated.

**B. Open the dataset.** A DuckDB view of `artifacts(repo_full_name, file_sha)`. In dev mode only sampled skills are kept, with every copy of each.

**C. Match.** Three queries per (blob, registry repository):

| Query | Gives |
|---|---|
| `_TOTALS_SQL` | `files`, `repos`, `owners`: how many dataset files, repositories and accounts hold this exact text |
| `_OWNERS_SQL` | `holder_owners`: the list of those accounts |
| `_SOURCE_SQL` | `source_files`: copies inside the registry repository itself |

The result, `matched`, has one row per registry version found in the dataset. Versions with no copy are left out.

**D. Origin.** For each blob, the registry with the earliest `first_date` is the origin; any other registry holding it is a `redistributor`.

**E. Lineages.**
1. Drop versions dated after the snapshot, unless the skill has no earlier version.
2. Per lineage: `versions` (distinct texts), `first_date`, `last_date`, `span_days`.
3. Join the versions with their dataset counts (a left join, so never-copied versions count as unseen):

| Column | Formula | Meaning |
|---|---|---|
| `versions_seen` | count of versions with a copy | Versions found in the dataset |
| `copies` | Σ (`files − source_files`) | Dataset files outside the registry, added up over versions |
| `owners` | size of the union of `holder_owners` | Distinct accounts, including the registry's own |

## Outputs

| File | Rows | One row per | Columns |
|---|---|---|---|
| `s1_registry_matches.parquet` | 6,719 | Registry blob found in the dataset | `blob, files, repos, owners, origin_repo, origin_skill, origin_date, tier, redistributors` |
| `s1_lineages.parquet` | 2,274 | Registry skill (mirrors excluded) | `lineage_id, versions, versions_seen, copies, owners, first_date, last_date, span_days` |

## Worked example

Snapshot 2026-07-20 (from the unit test `test_lineage_copies_exclude_source_and_ignore_later_versions`).

| Registry version | blob | first_date | Dataset files holding it |
|---|---|---|---|
| alpha/X :: early | B | 2025-10-01 | alpha/X, downstream/one, downstream/two, other/three |
| alpha/X :: other | B | 2025-10-01 | (same blob) |
| alpha/X :: early | L | 2026-08-01 | later/one |
| beta/Y :: solo | S | 2026-08-01 | reader/app |

| Lineage | versions | copies | owners | span_days | Why |
|---|---|---|---|---|---|
| alpha/X::early | 1 | 3 | 3 | 61 | L is after the snapshot and an earlier version exists, so L is dropped. copies = 4 files − 1 in alpha/X |
| alpha/X::other | 1 | 3 | 3 | 61 | Same blob B, so the same counts |
| beta/Y::solo | 1 | 1 | 1 | 0 | Its only version is after the snapshot, so it is kept |

## Design decisions

- **Exact hash matching only:** equal hashes mean equal bytes, so a match is never wrong. Edited copies are found by similarity in 2.6 / 2.7.
- **Earliest registry is the origin:** community collections often re-host official skills; ordering by date credits the official one.
- **The registry's own files are not copies:** they are excluded from `copies`, and 3.2 does not call them templated.

## Limits

- **Only untouched copies are found.** Any edit, even one byte, changes `file_sha`. A skill copied from a registry and then edited does not match, though it clearly came from there. "Not matched" means "not an exact copy", not "not from a registry". *Example:* the October 2025 `pdf` skill from `anthropics/skills` appears unchanged in 1,409 dataset files, and all match. Change one sentence and the copy no longer matches here, but 2.7 still finds it as a `near` copy if about 95% is unchanged.
- **The origin can only be a listed registry.** If the real publisher isn't in the YAML, the earliest listed one gets the credit (step 1.5 helps find missing ones).
- **Skill name = folder name.** `skills/review/` and `legacy/review/` in one repository become one lineage; a renamed folder starts a new one.
- **Reading `copies` and `owners`:**
  - `copies` counts files, not repositories, and is repeated in every lineage that shares a blob: don't add it up across lineages.
  - `owners` includes the registry's own account; outside owners ≈ `owners − 1`.
  - Files held by another registry count as copies.
- **`span_days` is time between edits, not lifetime.** A skill with one version has span 0.
- **Post-snapshot versions can appear in `s1_registry_matches`**, because the cut-off is applied only to lineages.

## Tests

- `test_match.py` (5): origin is the earliest registry; a mirror is counted once; the worked example above; dev mode keeps every copy of sampled blobs.
- Regression test on the real data (`-m data`): 6,719 matched blobs, 216,440 dataset files, per-tier totals, and the October 2025 `pdf` version in exactly 1,409 files.

## For developers

**Naming trap.** In `_build()`, `matches` and `lineages` are row counts. The parameter named `matches` in `assign_origin`, `build_lineages`, `_usage` and `_link_matches` is the in-memory `matched` table, not the Parquet file.

**Open fixes:**
- Make the join keys explicit in `_link_matches`: `keys.merge(slim, on=["repo", "blob"], how="left")`.
- Rename those `matches` parameters to `matched`, and the counts in `_build()` to `n_matches` / `n_lineages`.
- Optionally save `matched` to disk, so lineage counts can be checked without rerunning the join.

**Inspect `matched` in a notebook:**

```python
from skillsig.config import load_config
from skillsig.s1_registry import match as m
from skillsig.s1_registry.sources import load_sources

cfg = load_config(None)
versions = m._annotate(m._read_versions(cfg), load_sources(cfg))
con = m._connect_artifacts(cfg, dev=True)   # dev=False for the full dataset
matched = m.match_exact(con, versions)
con.close()
```
