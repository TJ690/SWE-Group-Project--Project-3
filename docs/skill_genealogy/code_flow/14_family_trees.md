# Family trees

Stage 8. [Copy families](13_copy_families.md) (stage 7) gave every English text one family. This stage turns each family into a tree: a source family starts with its registry versions, every other text hangs under the most similar text that is older than it, and the generation counts the steps from the registry. RQ1 reads the summary for the size and depth of families; the tree viewer and the repo chain draw the rows. It reads saved tables only; no network. Card G18.

The code is `src/skill_genealogy/build_family_trees.py`. It reuses the signature loaders of `find_copies.py` (stage 5a/5c). The parent of a text is the closest earlier text, **not a proof of who copied whom**.

## Scripts

Run these from the repo root.

```bash
bash scripts/skill_genealogy/dev_mode/14_family_trees.sh [--force]
```

`build_family_trees --dev` in the foreground, log in `logs/build_family_trees_dev_<date>.log`. Needs the dev stage 7 tables (`13_copy_families.sh`). Families cover every English text, also in dev, so it takes several minutes.

```bash
bash scripts/skill_genealogy/full_run/14_family_trees.sh [--force]
```

The same in the background with `caffeinate`, log in `logs/build_family_trees_<date>.log`. The stage skips when both outputs exist. `--force` keeps the old files until the new build has finished and then replaces them, so a failed run leaves the previous outputs in place.

```bash
bash scripts/skill_genealogy/tests/14_family_trees.sh
```

Runs `tests/skill_genealogy/test_build_family_trees.py` and the doctests of `build_family_trees.py`.

**Progress.** A run prints a line at most every 30 seconds (families and texts done, the family being built, texts per second, time left). The same numbers are in `logs/build_family_trees_<dev|full>.progress.json`, replaced in one step, so `cat` shows a whole file at any time. A family of more than 1,000 texts also reports how far inside it the run is.

**Flag `--skip-families-over N`.** Leaves out every family with more than N texts. They stay in the summary as `skipped` and have no nodes. The default builds every family.

## Inputs

- Dev or full data folder: `families.parquet` and `copy_files.parquet` (stage 7), `lineage_versions.parquet` (4a), `lineage_roots.parquet` (5b), `registry_signatures.npz` (5a).
- Full data folder (shared by dev and full): `dataset_signatures/` (5a).
- GitSkills `artifacts` (`repo_full_name`, `path`, `file_sha`, `first_commit_at`), for the date of every text that is not a copy file.

A missing file names the command that builds it.

## Steps

**Step 1: Dates and repos of every text (query Q8.1).** A copy file already carries a history-based `first_commit_date` (stage 7). A text that is not a copy file gets the earliest dataset `first_commit_at` over its files, read in one DuckDB scan (dates as UTC). `earliest_files` reduces the files of each text to one row: the earliest dated file sets the repo and the date, a date tie takes the smaller repo and then the path, and `files` counts all files of the text. A text with no dated file is undated.

**Step 2: Leaves.** A singleton or copier-only family has one text and nothing to compare. Each text is one root node (generation 0, no parent), written in one frame.

**Step 3: Signatures.** The registry signatures plus the dataset signatures of the texts of the remaining families are stacked into one `uint32` matrix; a dictionary maps `lineage#version` or the blob sha to its row. Registry texts that are the registry's own (`registry_self`) are not nodes of the copy side.

**Step 4: One tree per family (A36, A37).** Families are built one by one, in id order.

**Step 5: Summary.** One row per family from the tree and the stage 7 table.

**Step 6: Save and print.** Both files are written under a temporary name, then moved into place, sorted so a rerun gives the same files. The run prints nodes, families per kind, skipped and approximate families, the depth median, 90th percentile and maximum, the dated copy nodes, and two sample rows of each output.

```text
a source family                      an independent family

 v1 ── v2 ── v3     registry          P (oldest dated = root, generation 0)
  │     │      generation 0           ├── Q (nearest earlier)  generation 1
  A     B                             │    └── R               generation 2
  └─ C                                └── S                    generation 1
 copies, generation = parent + 1      undated texts attach to the nearest
                                      node and are never parents
```

## Algorithm A36: the trunk of a source family

**Goal.** Give a source family its registry history as the first nodes, so every copy can be placed relative to a released version.

**Rule.** Every version of every lineage whose `root_lineage` is the family is a node (`lineage_id#version_seq`) with generation 0. Within a lineage the parent is the previous version. The first version of a re-hosted lineage has as parent the version it was copied from (`copied_from_lineage#copied_from_seq`) when that version is in the family; its generation stays 0, because it is a registry version, but the chain is kept. The registry node's date is its release date, its repo is the part of the lineage id before `::`.

**Why.** Generation counts copy steps. A registry version is the start of the count wherever it sits in the version chain, and a re-host is a registry copying a registry, which stage 5b already records.

## Algorithm A37: the parent of a text

**Goal.** Place each text under the closest earlier text, so the tree shows how a skill spread.

**Rule.**

1. **Order.** In a family with a trunk, texts are ordered by date, then id; undated texts come last. A family without a trunk (independent) has a **root**: the oldest dated text (ties take the smaller id), else the text with the most files. The root comes first and has no parent.
2. **Candidates of a dated text.** Every registry version released **before** its date, plus the dated texts strictly before it, at most the latest `TREE_MAX_EARLIER_MEMBERS` (2,000) of them. The root is always a candidate. An equal timestamp is not "before". `approx` is true when the cap dropped candidates.
3. **Candidates of an undated text.** Every registry version, plus the latest dated texts (same cap). An undated text is never a candidate, so it never has children.
4. **Choice.** The same blob scores 1. Otherwise the share of equal values in the two 128-value MinHash signatures (an estimate of the Jaccard similarity) is the score; the highest wins. A text or candidate with no signature scores nothing. When nothing scores, the first candidate is kept with similarity NaN. Candidates are ordered **registry versions by release date** (a tie keeps the lineage, then the version order), then texts by date, so a tie always keeps the earliest node. A copy of a text that a re-host and its original both hold therefore points at the original.
5. **Copies older than every version.** A text dated before every registry version and before every other text of the family has no candidate. It attaches to the **earliest registry version**. Its parent is then newer than the copy: the copy exists in a repo before the registry released it, and the registry version is the closest registry anchor, not a source.
6. **Generation** is the parent's generation plus one; registry versions are 0, a root is 0.

**Why the cap.** A family can hold tens of thousands of texts, and every text against every earlier one is quadratic. The latest 2,000 are the most plausible parents. Only the family that hit the cap is marked `approx`.

**Why a fixed order.** A parent is always earlier in the processing order, so one pass computes the generations and no cycle can form.

**Vectorised.** A text's candidates are compared in one numpy call, `(matrix[rows] == query).mean(axis=1)`. `score_candidates_simple` is the loop version used in the tests, and `pick_parent_for_member` is the single-text entry that uses the same rule (`_best_candidate`) as a run.

## Outputs

`family_tree.parquet`, one row per node.

| Column | Meaning |
|---|---|
| `family_id` | Stage 7 family |
| `node_id` | `lineage_id#version_seq` for a registry version, `text:<blob sha>` for a text |
| `node_kind` | `registry` or `copy` |
| `blob` | Blob sha of the text |
| `repo` | Repo of the earliest file (registry: the registry repo); empty if unknown |
| `first_date` | Earliest commit date (UTC); registry: release date; empty when undated |
| `files` | Files holding the text (registry nodes: 0) |
| `parent_id` | Parent node, empty for a root |
| `parent_similarity` | Share of equal MinHash values, 1.0 for the same blob; NaN when unsigned |
| `generation` | Steps from the registry (or the root) |
| `approx` | The cap dropped candidates |

`family_summary.parquet`, one row per family: `family_id`, `family_kind`, `texts` (all texts of the family), `member_nodes`, `registry_nodes`, `dated_nodes`, `depth` (largest generation), `approx`, `mean_generation` (copy nodes only), `repos` (distinct repos of copy nodes), `status`, `root_basis`.

- `status` is `built`, `registry_only` (every text is a registry text, so no node was built) or `skipped` (`--skip-families-over`).
- `root_basis` is `trunk` (a registry trunk), `single` (one text), `oldest_dated`, `most_copied`, or `none` (`registry_only` and `skipped` families: no node, so no root).

## Settings

`TREE_MAX_EARLIER_MEMBERS` (2,000) in `settings.py`. DuckDB uses the memory and temp caps of stage 5c for the date scan.

## Queries

**Q8.1.** For the wanted texts, `min(first_commit_at)` per `(repo_full_name, path, file_sha)` of the artifacts, as UTC. The wanted texts are registered as a table and joined with a `SEMI JOIN`.

## Tests

`tests/skill_genealogy/test_build_family_trees.py`, hand-made versions and copies, no dataset.

| Test | What it proves |
|---|---|
| Source chain and trunk | Parents v1, v2 and A at generations 1, 1 and 2; the trunk stays 0 |
| Undated | Attaches to the nearest node and has no children |
| Cap | With cap 1 a text sees only the latest earlier text and the registry; `approx` is set |
| Scorers | Vectorised and simple scorers agree; empty matrix scores nothing |
| Same blob, missing signature | Same blob scores 1; a missing signature keeps the earliest node |
| Re-host | First version points at the source version, generation 0 |
| Tie between original and re-host | The earlier release wins, for a similar and for an identical text |
| Older than every version | Attaches to the earliest registry version |
| Earliest file | Sets repo and date; a tie takes the smaller repo; no file is undated |
| Independent roots | Oldest dated wins; all undated: the most-copied text |
| Summary | Depth, mean generation; `registry_only` and `skipped` have `root_basis` `none` |
| Progress | The status file and the tick |
| Save and force | A failed write or a failed build leaves the old outputs |

## Limits

- A parent is the closest **earlier** text by MinHash, an estimate. Two texts with the same date are never parent and child.
- Dates mix two sources: a copy file's date is its history-based first commit, any other text's is the dataset `first_commit_at`. Both are git dates and can be rewritten by a rebase or squash.
- Most independent families have no dated text and root on the text with the most files.
- A registry version released after a copy is not its parent. A text identical to a later release is linked to the closest earlier version instead.
- A family whose texts are all registry texts has no nodes and is `registry_only`; an independent registry cluster has no trunk, so its texts are not shown.
- The cap makes the largest family approximate.
