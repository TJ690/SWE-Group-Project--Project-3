# Stage design notes

One file per pipeline stage. Each explains, step by step, **what** the step computes, **how** it computes it, and **why** it was built that way (design decisions, trade-offs, known limits). For the commands to run and the inputs and outputs, see [EXECUTION_STEPS.md](../EXECUTION_STEPS.md); the step numbers here match the step numbers there.

| File | Stage | Steps |
|---|---|---|
| [P_prepare.md](P_prepare.md) | Prepare: SQL-built tables | P.1 |
| [s0_intake/](s0_intake/README.md) | Intake: open the dataset, verify it, draw the dev sample | 0.1 |
| [s1_registry/](s1_registry/README.md) | Registry: where skills came from, from the history of 31 skill collections | 1.1 – 1.5 |
| [s2_features/](s2_features/README.md) | Features: per-skill evidence, one file per step | 2.1 – 2.9 |
| [s3_labeling/](s3_labeling/README.md) | Labels: rules v1 → v2 → v2.1, baselines, text models, one file per step | 3.1 – 3.7 |
| [s4_validation/](s4_validation/README.md) | Validation: known-answer checks and the human spot-check, one file per check | 4.1 – 4.8 |
| [s6_report/](s6_report/README.md) | Report: the charts of the findings report | 6.1 |

Related documents:

- [WHY_WE_BUILT_IT_THIS_WAY_T00-T10.md](../WHY_WE_BUILT_IT_THIS_WAY_T00-T10.md): plain-language reasons for tasks T00 to T10.
- [ANNOTATION_GUIDELINES.md](../ANNOTATION_GUIDELINES.md): how people label the spot-check sheets (step 4.7).

---

## Decisions that apply to every stage

### 1 A chain of small stages, each with one job

The pipeline is `P → s0 → s1 → s2 → s3 → s4`. Each module reads finished tables from earlier steps and writes its own table under `src/data/derived/` (intermediate) or `results/` (reported). Nothing is passed in memory between steps.

- **Why:** a step can be rerun, tested and reviewed on its own. Changing a label rule reruns s3 and s4, not the slow MinHash step.
- **Trade-off:** the same columns are sometimes re-read from the raw Parquet by several steps. That is cheap with DuckDB and keeps each step independent.

### 2 Evidence (s1, s2) is kept apart from decisions (s3)

s1 and s2 only **measure** (copy counts, trailers, clusters, wording). Only s3 turns measurements into a label. s4 only **checks** labels.

- **Why:** a threshold change touches one rule file, not the measurement code. A reader can audit each signal separately from the rules that combine them. Several signals (skill type, wording, specificity, scaffold) are measured but deliberately not used by the current label, so they can be studied without moving the label.

### 3 The unit is the distinct skill text (`file_sha`)

3,797,117 `SKILL.md` files hold 1,877,981 distinct texts. Labels are given once per distinct text (the dataset's `dedup_primary = 1` row); copy counts still use every file.

- **Why:** identical bytes cannot have two different authorship stories in the text itself, and giving a label per copy would let one popular skill dominate every share. The number of copies is itself evidence (templated), so it is kept as a feature instead of being thrown away.
- **Consequence:** commit metadata (first/last commit, author) comes from the representative row. Other copies may have different histories that the label does not see.

### 4 One readable filter defined once

Text features use the 1,840,872 distinct skills that have text, at least 200 body characters, and are not a one-line `…SKILL.md` pointer or a `Redirecting…` stub. The same SQL predicate (`_READABLE` in `minhash_cluster.py`, mirrored in `db/content_and_structure.sql`) is reused everywhere.

- **Why:** stubs and pointers have no wording to measure, and they would form giant fake near-duplicate clusters. Using one definition means every text feature covers the same population.

### 5 One config file, frozen at load

Every judgment call (bulk batch size 6, copy minimum 10, Jaccard 0.8, human revision 3 commits over 7 days, MinHash settings, scaffold settings) lives in `config/default.yaml`. The loader rejects unknown or missing keys and returns a read-only object.

- **Why:** one page lists every threshold a reader may question; s4 stability (4.4) can vary them one at a time; typos fail loudly instead of silently using a default.

### 6 Manifests and skip-if-unchanged

Each output gets `<file>.manifest.json`: input fingerprints (sha256, or size + mtime above 1 GB), the config hash, the git commit, the time and the row count. A step whose inputs and config hash match its manifest is skipped.

- **Why:** several steps are slow on the full data; reruns must be cheap. The manifest also answers "which code and settings produced this number?" for every reported table.
- **Known gap:** code changes are not fingerprinted (only the commit is recorded). After editing code, run with `--force`. Any config change, even an unrelated one, marks every output stale.

### 7 DuckDB over Parquet, no database server

Raw tables are registered as DuckDB views over `read_parquet(...)`. Heavy joins and group-bys stay in SQL; Python UDFs are registered into DuckDB where a rule is easier to express in Python (for example the v1 label and the repository kind) so the same function is used in tests and on the full data.

- **Why:** millions of rows run on a laptop with no setup and no copy of the 12.5 GB dataset.

### 8 Determinism

Every random choice is seeded from `seed: 20261015`: the dev sample, MinHash permutations, the stability sample, the spot-check draw, cross-validation folds. Samples are drawn by ordering on `hash(file_sha || seed)` rather than by a random generator, so the same ids are drawn regardless of row order or thread count.

### 9 Dev mode restricts which skills are scored

`--dev-sample` limits outputs to 50,000 skills. Most population counts are still computed over everything: batch size, copy count, identifier frequency, repository size.

- **Why:** a batch size or copy count computed inside a sample would be wrong (a skill copied 500 times might have one copy in the sample).
- **Exceptions:** near-duplicate clusters (2.6) and scaffold spread counts (2.9) are computed **inside the sample only**, so their dev values are far smaller than the full ones, and the `cluster` templated rule rarely fires in dev runs. See [s2 § Full data vs. dev sample](s2_features/README.md#full-data-vs-dev-sample).

### 10 Insufficient evidence is a real answer

Every label rule ends in `insufficient_evidence` when its signals are missing or conflicting, and every label carries reason codes.

- **Why:** most skills (75.6%) have no commit history, and the project would rather report "we cannot tell" than guess. The reason codes let s4 score precision per reason, not only per label.
