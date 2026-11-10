# Copies in the dataset

Stage 5c. Most copies of a registry skill do not sit in another registry: they sit in ordinary repos of the dataset. This stage links each English dataset text that has a file outside the registries to **one registry lineage version**, checks every fuzzy link with a line diff, lists the copy files with flags, and records which unlinked texts a copier registry holds. It reads saved tables only; no network. Card G14.

The code is `src/skill_genealogy/find_copies.py`, steps 10–18 (steps 0–3 are stage 5a, [boilerplate lines and MinHash signatures](09_boilerplate_signatures.md); steps 4–9 are stage 5b, [registries copying each other](10_registry_rehosts.md)).

**Two passes.** Pass 1 (steps 11–15) links texts to **source** registries only, because only sources have lineages ([stage 4a](07_registry_lineages.md) leaves copiers out). Pass 2 (step 16) looks at the texts that are still unlinked and asks whether a **copier** registry holds the same blob. A copier match is not a link: it never enters `text_links` or `copy_candidates`. It is evidence for stage 7a, which decides whether the copier or the dataset repo had the text first. "No registry match" therefore never means "self-written".

## Scripts

Run these from the repo root.

```bash
bash scripts/skill_genealogy/dev_mode/11_dataset_copies.sh [--force]
```

`find_copies --dev --only copies` in the foreground (about 4 minutes), log in `logs/find_copies_copies_dev_<date>.log`. Needs the dev `registry_signatures.npz` and `lineage_roots.parquet`, and the shared dataset signatures.

```bash
bash scripts/skill_genealogy/full_run/11_dataset_copies.sh [--force]
```

`find_copies --only copies` in the background with `caffeinate`, log in `logs/find_copies_copies_<date>.log`. The stage skips when its four outputs exist; `--force` deletes only those four files.

```bash
bash scripts/skill_genealogy/tests/11_dataset_copies.sh
```

Runs `tests/skill_genealogy/test_find_copies.py` and the doctests of `find_copies.py` (stages 5a, 5b and 5c share the file).

## Inputs

- GitSkills `artifacts` (`repo_full_name`, `path`, `file_sha`, `content`) and `repos` (`full_name`, `is_fork`).
- `skill_language.parquet` (stage 2a, full data folder): `is_english`.
- `registry_kinds.parquet` (stage 3c, full data folder): every registry (4,806), its `registry_kind` and `confidence`.
- `registry_history/registry_history_changes.parquet` and `registry_history_texts.parquet` (stage 3): copier `SKILL.md` rows for Pass 2; registry texts for the line check.
- `lineages.parquet`, `lineage_versions.parquet` (stage 4a), `registry_signatures.npz` (stage 5a) and `lineage_roots.parquet` (stage 5b), dev or full data folder.
- `dataset_signatures/` (stage 5a, full data folder): one signature and shingle count per English dataset text.
- `src/data/derived/s2_clusters.parquet` (main pipeline, read only): near-duplicate clusters at 0.60 and 0.80.

## Steps

**Step 10: Read the inputs and list the texts to link (query Q5.4).** Check the files (a missing one names the command that builds it). `registry_repos` is every registry of stage 3c, copiers included. `copy_texts` is every distinct English `file_sha` with at least one file outside `registry_repos`. Only these texts are linked: a text found only inside registries has no dataset copy.

**Step 11: Exact links (query Q5.5).** A text whose `file_sha` equals a version `blob` is the same bytes (both are git blob ids). Among several such versions the oldest `release_date` wins, then `lineage_id`, then `version_seq`. This is SQL, so a blob held by thousands of versions costs nothing in Python. Exact texts skip steps 12 and 13.

**Steps 12 and 13: Near and contained links (Algorithms A25, A26).** The other texts are looked up in one LSH index of the registry signatures, one dataset part (about 48,000 texts) at a time. See the cards below.

**Step 14: Cluster links (Algorithm A27, query Q5.6).**

**Step 15: Line-diff check (Algorithm A28, query Q5.7).** Every `near`, `near_contained` and `cluster` link is checked; a link that fails is dropped and stays dropped (the text does not try a later method).

**Step 16: Copier pass (Algorithm A28b, query Q5.8).**

**Step 17: Copy files and flags (queries Q5.9, Q5.10).** Each kept text is joined to its dataset files. A file in any registry repo (copiers included) is the registry's own file, not a copy, and is left out. Each file gets `root_lineage` from stage 5b (the lineage itself when it has no row), and two flags that drop nothing:

- `is_fork`: `repos.is_fork = 1`. A fork shares the registry's git history, so later stages report with and without forks. A repo missing from `repos` is not a fork.
- `same_owner_as_registry`: the copy's owner (text before the first `/`, any letter case) owns the linked lineage's registry: someone moving their own skill.

**Step 18: Save and print.** The four outputs are written. The run prints the texts outside the registries, candidates and kept links per kind, links dropped per kind, copy files per kind, copy repos (the repos stage 6 clones), fork and same-owner files, the copier-seen texts and files (and how many only in weak copiers), and two sample rows of each table.

```text
link order for one text (Pass 1, first match wins)

  exact blob ── yes ──────────────────────────────> exact (1.0, no line check)
      │ no
  band index: Jaccard >= 0.80 ── yes ─> near ─────┐
      │ no                                        │
  containment >= 0.80, source >= 20 shingles      │
      ── yes ─> near_contained ───────────────────┤
      │ no                                        │
  0.80 cluster with linked texts ── yes ─> cluster┤
      │ no                                        v
      v                              line check: share of registry lines kept
  unlinked ──────────────────> Pass 2     >= 0.50 kept │ < 0.50 dropped -> Pass 2
```

## Algorithm A25: near links

**Goal.** Find the registry version a dataset text is a light edit of.

**Rule.** A candidate counts when the estimated Jaccard of the two shingle sets (equal MinHash values / 128) is at least `NEAR_COPY_MIN_JACCARD` (0.80). The highest Jaccard wins; ties go to the oldest release date, then `lineage_id`, then `version_seq`. Any version counts, not only the first: a repo often copies a skill as it was when it copied it. There is no date rule, because 88% of dataset texts have no reliable first-commit date; stage 7a dates the copies.

**Candidates.** Comparing 1.5 million texts with 816,534 versions pair by pair is far too slow, so the registry signatures go into an LSH index of **64 bands of 2 values**, the layout datasketch's `MinHashLSH` picks for `COPY_SEARCH_LSH_THRESHOLD` (0.1). A text and a version are candidates when one whole band is equal, which happens with probability `1 − (1 − j²)⁶⁴` for Jaccard *j*: 0.93 at 0.2, 0.997 at 0.3, practically 1 from 0.4. Each candidate is then scored with the exact count of equal values. Stage 5b uses 25 bands of 5 values; that is too coarse here, because a contained copy can have a low Jaccard (A26).

**Speed.** Each band's registry keys are sorted once (the two values of a band folded into one 64-bit number), so a lookup is a binary search. Two different bands can rarely get the same key; that only adds a candidate, which scoring then rejects. On 20,000 full-data texts the index gave 581 band hits per text (25 × 5 bands: 97; one value per band: 2,917). `build_registry_lsh` builds the same index with datasketch, and a test checks that both give the same candidates.

## Algorithm A26: near_contained links

**Goal.** Find a dataset text that kept a registry text and added its own sections. Its Jaccard falls, but the registry text is still inside it.

**Rule.** For texts with no near link: containment, the share of the registry text's shingles inside the dataset text, estimated as `jaccard × (registry + copy) / ((1 + jaccard) × registry)` from the two shingle counts, is at least `NEAR_COPY_MIN_CONTAINMENT` (0.80). The registry text must keep at least `REHOST_CONTAINED_MIN_SHINGLES` (20) shingles after boilerplate removal: a smaller rest is often just headings such as `## Instructions`, `## Safety`, which are inside many unrelated skills (see the [stage 5b report](../report/10_registry_rehosts_report.md)), and the line check cannot catch it, because a text with those headings keeps every one of its lines.

**Weak sources.** A small registry text inside a large copy reaches containment 1.0 from one or two equal values by chance. When a text has a strong candidate (at least `STRONG_MATCH_MIN_VALUES` = 20 equal values), candidates with at most `WEAK_MATCH_MAX_VALUES` = 5 equal values are left out.

**Choice.** Highest containment, then highest Jaccard, then oldest date, `lineage_id`, `version_seq`. Containment is clipped to 1.0, so several versions of one skill often tie at 1.0; the higher Jaccard picks the version that covers more of the copy, for example the later, longer version the copy grew from.

**Worked example** (`test_contained_link_when_the_copy_adds_lines`): a registry text of 40 lines (316 shingles) and a copy with the same 40 lines and 60 new ones (796 shingles). Jaccard about 0.40, containment 1.0: no near link, one `near_contained` link.

## Algorithm A27: cluster links

**Goal.** Link a text that MinHash missed but that the main pipeline's near-duplicate clusters put next to linked texts.

**Rule.** Only `s2_clusters` rows at `CLUSTER_LINK_THRESHOLD` (0.80) are used; the 0.60 clusters group looser neighbours. In each cluster, count the texts linked in steps 11–13 per lineage. An unlinked text of `copy_texts` in the cluster takes the lineage with the most linked texts; ties go to the lineage whose linked version is oldest, then `lineage_id`. The version is that lineage's oldest linked version in the cluster. Jaccard and containment stay null, because the pair was never scored; the line check decides whether the link stays. The votes are counted before the line check, so a link that step 15 later drops still votes.

## Algorithm A28: line-diff check

MinHash compares sets of 5-word phrases, not lines, and one standard deviation of its estimate is about 0.04 near 0.80. A cluster link is not scored at all. So every non-exact link is compared line by line (PAN text alignment: find candidates, then align):

```text
similarity_verified = share_of_registry_lines_kept(registry_text, copy_text)
                    = registry body lines that the copy still has unchanged
                      / registry body lines         (diff after normalize_text)
```

A link below `MIN_VERIFIED_LINE_SHARE` (0.50) goes to `links_dropped` with `reason = line_share_below_min`; exactly 0.50 stays. A missing text gives `reason = missing_text`. Exact links get 1.0 without reading any text. Example: the same words with other line breaks have Jaccard 1.0 but share 0.0, and are dropped.

The registry texts come from the stage 3 texts table (query Q4.2), the dataset texts from one scan of the artifacts into a temporary file (Q5.7). Links are sorted by registry blob and checked `LINE_CHECK_BATCH_LINKS` (50,000) at a time, the diffs in `DOWNLOAD_WORKERS` processes.

## Algorithm A28b: copier pass (Pass 2)

For each text of `copy_texts` with no kept link (never linked, or dropped by A28), look for an **exact** match among the `SKILL.md` change rows (`status` not `D`) of copier registries (`registry_kind = copier`): `new_sha = file_sha`. Copier texts have no signatures, so the match is exact only. Per text:

- `copier_first_date`: the earliest commit date of those rows, in UTC;
- `copier_first_repo`: the copier with that date (ties: repo name);
- `copier_repos`: every copier that holds it, sorted;
- `weak_copiers_only`: every one of them has stage 3c confidence below `WEAK_COPIER_BELOW` (0.5), so the copier label itself is uncertain.

In dev only the copiers among the dev registries are checked, the same way only the dev lineages are indexed.

## Queries

- **Q5.4** `COPY_TEXTS_SQL`: distinct English `file_sha` of `artifacts`, anti-joined on `repo_full_name` to `registry_repos`.
- **Q5.5** `EXACT_LINKS_SQL`: `copy_texts` joined to the version table on `file_sha = blob`; `QUALIFY row_number() = 1` over release date, `lineage_id`, `version_seq`.
- **Q5.6** `CLUSTER_LINKS_SQL`: 0.80 cluster members, votes per cluster and lineage, the winner per cluster, its oldest linked version, applied to unlinked members of `copy_texts`.
- **Q5.7** `LINE_CHECK_TEXTS_SQL`: one `content` per needed `file_sha`, written to a temporary parquet file.
- **Q5.8** `COPIER_SEEN_SQL`: copier `SKILL.md` rows (not deletes) grouped per blob and copier, then per blob over the unlinked texts: `min` date, `first(repo ORDER BY date, repo)`, `list(repo ORDER BY repo)`, `bool_and(confidence < 0.5)`.
- **Q5.9** `COPY_FILES_SQL`: artifacts of the kept texts, anti-joined to `registry_repos`, with `root_lineage`.
- **Q5.10** `FLAG_COPIES_SQL`: the copy rows with `repos.is_fork` and the lineage's registry owner.

## Outputs

All four in the dev or full data folder.

`text_links.parquet`, one row per linked text: `file_sha`, `lineage_id`, `version_seq`, `link` (`exact`, `near`, `near_contained`, `cluster`), `jaccard`, `containment` (1.0 for exact, null for cluster), `similarity_verified`.

`copy_candidates.parquet`, one row per copy file: `repo`, `path`, `file_sha`, `lineage_id`, `root_lineage`, `version_seq`, `link`, `jaccard`, `containment`, `similarity_verified`, `is_fork`, `same_owner_as_registry`.

`links_dropped.parquet`: `file_sha`, `lineage_id`, `link`, `similarity_verified`, `reason`.

`copier_seen.parquet`, one row per unlinked text a copier holds: `file_sha`, `copier_first_date`, `copier_first_repo`, `copier_repos` (list), `weak_copiers_only`.

**Copy history comes from stage 6 only.** The registry side already has its full history (stage 3). The copy side, the dataset repos, keeps only a first and a last commit in GitSkills, so stage 6 clones every copy repo. The main pipeline's `wild_history` (the `SKILL.md` history of busy dataset files, mined on 2026-10-29) is not used: it covers 1,230 of the 1,414,080 copy files and would save about 21 of 60,958 clones, while it holds only `SKILL.md` and not the skill folder, is an older snapshot, and its code cannot be rebuilt from this repository. One history source keeps stages 7 to 10 simple.

## Limits

- **Only source registries are linked.** A text copied from a small repo that is not a registry is unlinked here; stage 7a finds such copies from history.
- **A copy much larger than its source.** With containment 0.80, a dataset text more than about 5 times the size of the registry text has a Jaccard under 0.15 and is often not a candidate.
- **Exact copier matches only.** A copier text edited by the dataset repo is not seen by Pass 2.
- **Dev mode.** Only the dev registries are indexed and only their copiers checked, so copies of other registries are not found.

The full-run numbers are in the [dataset copies report](../report/11_dataset_copies_report.md).
