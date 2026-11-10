# Registries copying each other

Stage 5b. Many registries publish a skill that another registry published first. This stage finds, for each registry lineage, whether its **first version** was copied from an older version in another registry, and follows those links to the **root lineage**, the oldest registry copy of the skill. It also writes the final registry hub table and repo roles that every later stage reads. It reads saved tables only; no network. Card G13.

The code is `src/skill_genealogy/find_copies.py`, steps 4–9 (steps 0–3 are stage 5a, [boilerplate lines and MinHash signatures](09_boilerplate_signatures.md)).

**Registry kinds come from stage 3c, not from this stage.** [Stage 3c](06_registry_kinds.md) labels every registry `source`, `copier` or `unclear` from who had each shared text first and from the README, and [stage 4a](07_registry_lineages.md) leaves copiers out. So every lineage here belongs to a source or an unclear registry, and a link found here is one source registry copying another. This stage still counts, per registry, the share of lineages copied from an older registry (the **dated hub share**), but only as a printed cross-check next to the stage 3c label. It labels nothing.

## Scripts

Run these from the repo root.

```bash
bash scripts/skill_genealogy/dev_mode/10_registry_rehosts.sh [--force]
```

`find_copies --dev --only rehost` in the foreground (a few seconds), log in `logs/find_copies_rehost_dev_<date>.log`. Needs the dev `registry_signatures.npz`.

```bash
bash scripts/skill_genealogy/full_run/10_registry_rehosts.sh [--force]
```

`find_copies --only rehost` in the background with `caffeinate`, log in `logs/find_copies_rehost_<date>.log`. The stage skips when its three outputs exist; `--force` deletes only those three files, never the signatures.

```bash
bash scripts/skill_genealogy/tests/10_registry_rehosts.sh
```

Runs `tests/skill_genealogy/test_find_copies.py` and the doctests of `find_copies.py` (stages 5a and 5b share the file).

`find_copies` without `--only` runs stages 5a, 5b and then [5c](11_dataset_copies.md).

## Inputs

- `lineages.parquet`, `lineage_versions.parquet` (stage 4a, dev or full data folder): `lineage_id`, `repo`, `is_english`; `version_seq`, `blob`, `release_date`.
- `registry_signatures.npz` (stage 5a, dev or full): one MinHash signature and shingle count per version, id `lineage_id#version_seq`.
- `repo_roles.parquet` (stage 2c, full data folder): the stage-2 roles and hub preview.
- `registry_kinds.parquet` (stage 3c, full data folder): `registry_kind`, `confidence`, `kind_basis`, `upstream_share`, `shared_texts`, `readme_phrase`.

## Steps

**Step 4: Read the inputs.** Check the five files (a missing one names the command that builds it) and load them. Each version gets its registry `repo` and its signature id.

**Step 5: Exact re-hosts (query Q5.3).** For each first version, look for a version of **another repo** with the **same blob**, released **strictly before** it. The oldest one wins (then `lineage_id`, then `version_seq`). This is SQL over the version table, so a blob shared by thousands of versions costs nothing in Python. An exact link always beats a near one, so lineages found here skip step 6.

**Step 6: Near and contained re-hosts (Algorithm A23).** The other first versions are compared by MinHash with every version of other registries. See the card below.

**Step 7: One source per lineage, then the chain (Algorithms A23, A24).** All candidates of a lineage from steps 5 and 6 are sorted by the A23 order and the first is kept. Then A24 follows the links to the root.

**Step 8: Dated hub shares, registry hubs, final roles (Algorithm A24b).** See the card below.

**Step 9: Save and print.** The three outputs are written. The run prints the counts per link kind, the longest chain, cycles, the registries with the most re-hosted lineages, the registries by stage 3c kind, the repo roles, every non-copier registry whose dated hub share is at least `HUB_MIN_SHARE` (0.25), and two sample rows of each table.

## Algorithm A23: the source of a first version

**Goal.** Decide whether a lineage's first text already existed, earlier, in another registry.

**Why only the first version.** A lineage that started as a copy is a re-host, whatever it did later. Later versions of the same lineage are compared with their own earlier versions in stage 4a, not here. The *source*, though, can be any version of the older lineage: a registry often copies the newest version of a skill, not its first.

**Candidates.** A version of another lineage is a candidate when:

1. its repo is different (two folders of one registry with the same text are the registry's own business, not a copy between registries);
2. its `release_date` is **strictly before** the first version's date (the same day and time is no evidence of who copied whom);
3. one of the three links holds:
   - `exact`: the same git blob;
   - `near`: estimated Jaccard of the two shingle sets ≥ `NEAR_COPY_MIN_JACCARD` (0.80);
   - `near_contained`: estimated containment of the candidate inside the first version ≥ `NEAR_COPY_MIN_CONTAINMENT` (0.80). Containment is `jaccard × (source + copy) / ((1 + jaccard) × source)`, from the Jaccard and the two shingle counts stored with the signatures. It catches a registry that copied a skill and added sections: the Jaccard falls, but all of the older text is still inside. The candidate must keep at least `REHOST_CONTAINED_MIN_SHINGLES` (20) shingles after boilerplate removal. A smaller rest is often only headings such as `## Instructions`, `## Safety`, `## Resources`, which sit inside many unrelated skills; the [report](../report/10_registry_rehosts_report.md) shows how often such links were wrong. A `near` link needs no minimum, because both texts must be of similar size.

**Choice.** Exact first; otherwise the highest similarity (Jaccard for `near`, containment for `near_contained`); ties go to the oldest release date, then the smallest `lineage_id`.

**Finding the MinHash candidates.** Comparing 188,073 signed first versions with 816,534 versions pair by pair is 150 billion comparisons. So the signatures are put into an LSH index (locality-sensitive hashing): the 128 values are cut into 25 **bands** of 5, and two signatures are candidates when one whole band is equal. These are the band sizes datasketch's `MinHashLSH` picks for threshold `REHOST_CONTAINMENT_LSH_THRESHOLD` (0.5). A pair with Jaccard *j* shares at least one band with probability `1 − (1 − j⁵)²⁵`: 0.99995 at 0.8, 0.55 at 0.5, 0.06 at 0.3. One index at 0.5 serves both rules: practically every `near` pair is a candidate, and a contained copy whose Jaccard fell to about 0.5 is still found about half the time. For each candidate pair the exact count of equal values gives the Jaccard estimate.

`build_registry_lsh` builds the datasketch index itself; the tests use it. A datasketch index of 816,534 versions needs several GB, so the run uses `build_band_index`, which keeps the same 25 bands as sorted numpy arrays (about 250 MB). A test checks that both give the same candidates. In the same way `find_rehost_source` is the plain rule for one lineage, and a test checks that the run's vectorised path (`find_exact_sources`, `find_near_sources`, `choose_rehost_sources`) picks the same source.

**Worked example** (the tests `test_exact_then_near_chain_to_root` and `test_near_copy_of_a_copy_has_chain_length_two`):

```text
lineage  repo    version  date        blob   text
A        a/reg   1        2026-01-01  blob1  T
B        b/reg   1        2026-03-01  blob1  T
B        b/reg   2        2026-03-15  blob3  T'   (last word changed)
C        c/reg   1        2026-04-01  blob3  T'

B#1: same blob as A#1, older, other repo  -> exact from A#1
C#1: same blob as B#2, older, other repo  -> exact from B#2
```

## Algorithm A24: follow the chain to the root

`root_lineage` is found by following `copied_from_lineage` until a lineage with no source. `is_original` is true when the lineage itself has no source. `chain_length` is the number of steps. In the example: A is original (root A, length 0), B has root A, length 1, C has root A, length 2.

**Cycles.** Every link goes to a strictly older version, and a lineage's versions are never older than its first version, so a chain's first dates fall at every step and cannot come back. A cycle can still appear if dates are wrong, so the walk stops at a lineage it has already seen, sets `chain_cycle`, and takes the cycle's oldest lineage (first date, then id) as the root. That root still has a source, so it is not `is_original`.

## Algorithm A24b: dated hub share (a cross-check)

Per registry that has lineages:

```text
english_lineages  = lineages with is_english
rehosted_lineages = English lineages that are not is_original
dated_hub_share   = rehosted_lineages / english_lineages
                    (null below HUB_TEST_MIN_SKILLS = 5 English lineages)
```

Dates are the stage 3 commit dates of version 1, not the dataset's `first_commit_at`, which is empty for 88% of registry texts.

`registry_hubs.parquet` has one row for **every** registry in `registry_kinds.parquet`, copiers included. `registry_kind`, `kind_confidence`, `kind_basis` and `upstream_share` are copied from stage 3c. `is_hub` is `registry_kind == "copier"`. `hub_reason` puts the stage 3c evidence in words, e.g. `copier: 100% of 7 shared texts first seen elsewhere` or `source: 0% of 4 shared texts first seen elsewhere; README: official skills`. A copier has no lineages, so it has 0 English lineages and a null `dated_hub_share`. `preview_hub_share` is the stage-2 `hub_share`, kept for comparison.

**Why the dated share labels nothing.** It sees only skills re-hosted from **another source registry**: copiers are not lineages, and a registry that copied from a small repo that is not a registry has no older lineage to link to. A registry that copies mostly from copier registries or from small repos has a low dated share. Stage 3c compares every shared text, so it is the better label. Stage 7a counts copies of non-registry repos (`copied_before_release`).

**Final roles.** `repo_roles_final.parquet` is `repo_roles.parquet` with the registry rows taken from stage 3c: `is_hub` as above, `hub_share` = `upstream_share`, `repo_role` = `registry_copier` for a copier and `registry` for a source or unclear registry, `hub_evidence = "registry_kinds"`. Other repos keep their stage-2 values; their `hub_evidence` is `dataset_dates` when they have a stage-2 `hub_share`, else null. **Every later stage reads this file, not `repo_roles.parquet`.**

## Queries

- **Q5.3** `EXACT_REHOST_SQL`: `versions` (the version table with each version's repo) joined to itself on `blob`, other repo, strictly earlier `release_date`, first versions only; `QUALIFY row_number() = 1` over the oldest date, then `lineage_id`, then `version_seq`.

## Outputs

All three in the dev or full data folder.

`lineage_roots.parquet`, one row per lineage: `lineage_id`, `copied_from_lineage`, `copied_from_seq`, `rehost_link` (`exact`, `near`, `near_contained` or null), `rehost_similarity`, `root_lineage`, `is_original`, `chain_length`, `chain_cycle`.

`registry_hubs.parquet`, one row per registry: `repo`, `registry_kind`, `kind_confidence`, `kind_basis`, `upstream_share`, `english_lineages`, `rehosted_lineages`, `dated_hub_share`, `is_hub`, `hub_reason`, `preview_hub_share`.

`repo_roles_final.parquet`: the columns of `repo_roles.parquet` plus `hub_evidence`; `repo_role` is `registry`, `registry_copier`, `hub` or `repository`.

## Limits

- **Only copies between registries are seen.** A registry that copied from a non-registry repo looks original here. Stage 7a counts those as `copied_before_release`.
- **A missed band.** A contained copy whose Jaccard is well under 0.5, for example a short skill pasted into a much longer one, rarely shares a whole band and is not found.
- **MinHash is an estimate.** With 128 values one standard deviation of the Jaccard estimate is at most about 0.044, so pairs near 0.80 can fall on either side. Stage 5c checks its close calls with a line diff; this stage does not.
- **Dev mode.** Only the dev registries have lineages, so a skill re-hosted from a registry outside the sample counts as original, and dev hub shares are too low. The kinds come from the full stage 3c run, so dev and full agree on them.

The full-run numbers are in the [registry re-hosts report](../report/10_registry_rehosts_report.md).
