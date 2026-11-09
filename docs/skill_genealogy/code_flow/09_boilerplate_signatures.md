# Boilerplate lines and MinHash signatures

Stage 5a. Stage 5 finds copies of registry skills: in other registries (5b, card G13) and in the dataset (5c, card G14). Both compare **MinHash signatures**, a short summary of each text's 5-word shingles from which the Jaccard similarity of two texts can be estimated. This stage builds the signatures, once, for every English dataset text and every registry lineage version. It does not compare them. It does not use the network. Card G12.

Many unrelated skills share template lines: headings such as `## when to use this skill`, or stock sentences from a skill generator. If those lines stayed in, two unrelated skills built from one template would look alike. So the stage first finds the lines shared by many GitHub owners (the **boilerplate**) and leaves them out of every signature.

The code is `src/skill_genealogy/find_copies.py`, steps 0–3. Cards G13 and G14 add later steps to the same file.

## Scripts

Run these from the repo root.

```bash
bash scripts/skill_genealogy/dev_mode/09_boilerplate_signatures.sh
```

`find_copies --dev --only signatures` in the background, log in `logs/find_copies_signatures_dev_<date>.log`. `--dev` changes only the registry file: it signs the dev lineage versions into `src/data/skill_genealogy_dev/`. The boilerplate list and the dataset signatures do not depend on the registries, so they are shared with the full run and always go to the full data folder. The first run (dev or full) builds them, which takes more than an hour; later runs skip them. The dev script never passes `--force`, so it cannot delete the shared files.

```bash
bash scripts/skill_genealogy/full_run/09_boilerplate_signatures.sh [--force]
```

`find_copies --only signatures` in the background with `caffeinate`, log in `logs/find_copies_signatures_<date>.log`. Each output is skipped when it exists, and the dataset is signed in chunk files, so a stopped run resumes at the next missing chunk. `--force` deletes the boilerplate list, every dataset part and this mode's registry file, and builds them again.

```bash
bash scripts/skill_genealogy/tests/09_boilerplate_signatures.sh
```

Runs `tests/skill_genealogy/test_find_copies.py` and the doctests of `find_copies.py`.

`--only rehost` and `--only copies` are the parts of stages 5b and 5c. Until G13 and G14 exist they print a skip line.

## Inputs

- The GitSkills artifacts (`settings.ARTIFACTS_GLOB`): `repo_full_name`, `file_sha`, `content`.
- `skill_language.parquet` (stage 2a, full data folder). Only `is_english` texts are read.
- `lineage_versions.parquet` (stage 4a, dev or full data folder): `lineage_id`, `version_seq`, `blob`. Copier registries are already left out there, so they get no signature.
- `registry_history/registry_history_texts.parquet` (stage 3, full data folder; there is no dev registry history).

## Steps

**Step 0: Read the inputs.** Check that the four inputs exist (a missing one names the command that builds it), and register the views `artifacts` and `skill_language`. DuckDB gets an 8 GB memory cap and a temporary spill folder of at most 40 GB.

**Step 1: Count owners per line and keep the boilerplate (Algorithm A22, query Q5.1).** See the card below. Lines with at least `BOILERPLATE_MIN_OWNERS` (50) owners are written to `boilerplate_lines.parquet`. The run prints the top 30 lines and the 5 kept lines nearest the cutoff, so a person can check by eye that they are template text.

**Step 2: Build MinHash signatures (query Q5.2 for the dataset, Q4.2 for the registry).** Each text is signed by `signature_for_text`:

```text
normalize_text -> body (front matter removed) -> drop boilerplate lines
  -> 5-word shingles -> MinHash, 128 values, seed MINHASH_SEED
```

The result is the 128 hash values (`uint64`) and the **shingle count** (the size of the shingle set). Stage 5c needs the count to turn a Jaccard estimate into containment (how much of a registry text is inside a copy). A text with no shingle left, for example front matter with an empty body, has no signature: it is counted as empty and not stored.

- **Dataset.** Every distinct English `file_sha` is signed once, even when many repos hold the same text. Query Q5.2 reads the texts in 4 hash slices, sorted by `file_sha`, and they are signed in chunks of 50,000 by 8 worker processes. Each chunk is one file `part_sXX_cYYYYY.npz` (slice, chunk). Because the order is fixed, chunk *n* always holds the same texts, so a chunk file that exists is skipped and a stopped run resumes. A chunk is written under a hidden name and then renamed, so a crash never leaves a file that looks finished.
- **Registry.** Every row of `lineage_versions.parquet` is signed, with id `lineage_id#version_seq`, in the order of that table. The full registry history has about 9 GB of `SKILL.md` text, more than the machine holds as Python strings, so the texts are read `LINEAGE_TEXT_BATCH` (40,000) blobs at a time with the stage 4a query Q4.2 and freed after each batch. A version whose blob has no stored text is counted as empty.

**Step 3: Summary.** The run prints the owner cutoff, lines kept, dataset texts signed and empty, registry versions signed and empty, run time, two empty examples of each kind (id and the start of the text), and two signature records of each kind.

## Algorithm A22: template lines by owner count

**Goal.** Find body lines that many unrelated authors share, so that a signature describes what a skill says, not the template it was made from.

**Line key.** For each English text: `normalize_text`, drop the front matter, split into lines (`body_lines`), and key each line by `line.strip().lower()`. Lines with fewer than `BOILERPLATE_MIN_WORDS` (3) words are dropped: a two-word heading such as `## usage` is too short to say anything about a template. The key is built by the same Python code that `signature_for_text` uses to remove a line, so a line counted here is exactly a line removed there. `normalize_text` makes a curly apostrophe and a straight one the same character, so `Don’t edit this file` and `Don't edit this file` are one key.

**Owner count.** The owner is the text before the first `/` of `repo_full_name`. For each key, count the **distinct owners** whose English texts contain it. Owners are counted, not files or repos, because one author often pastes the same line into many of their own skills and repos; that is not a shared template. An owner who has a line twice, in one file or in many, counts once.

The owners of a text come from **every** artifact row with its `file_sha`, not only the row that holds `content`. The artifacts table has 3,797,117 rows but stores `content` on only 1,880,329 of them, one per distinct text; the other rows of the same sha are the same file in other repos. A check that counts only rows with content finds about a third of the owners. The owner name is used as it is: only 1 of 195,841 owners differs from another by letter case alone.

**Cutoff.** A line with at least `BOILERPLATE_MIN_OWNERS` (50) owners is boilerplate. 50 independent owners writing the same line of at least 3 words is far more than chance agreement. The value is checked against the printed top 30 and the lines at the cutoff; see the [report](../report/09_boilerplate_signatures_report.md).

**How it runs in memory.** Exploding every text into lines in one SQL query spilled more than 100 GB, because each line row kept a reference to its whole text. So:

1. Q5.1 writes the `(file_sha, owner)` pairs of the English texts to a temporary parquet, without the text.
2. Q5.2 streams the texts of each hash slice, 2,000 texts at a time (`LINE_BATCH_ROWS`). Python builds the line keys of the batch; Q5.1 joins them to the owners on a second connection (a second query on the scan connection would cancel the scan) and writes the distinct `(owner, line)` pairs of the batch to a parquet file.
3. A fresh connection (two threads, its own spill folder) counts distinct owners per line over all pair files and keeps the lines at or above the cutoff. A fresh connection is needed because the scan connection's memory is full of cached skill text.

**Worked example** (the test `test_count_owners_per_line_counts_distinct_owners`):

```text
repo    file  English  body lines
a/one   s1    yes      "use this shared skill", "hi there", "one two three"
a/two   s2    yes      "use this shared skill" (twice)
b/one   s3    yes      "use this shared skill"
c/one   s4    yes      "use this shared skill"
d/one   s5    no       "use this shared skill"

line                     owners
use this shared skill    3        a (s1, s2 count once), b, c; d is not English
one two three            1
hi there                 -        two words, dropped
```

With a cutoff of 3, only `use this shared skill` is boilerplate. Then `alpha beta gamma delta epsilon` with and without that line has the same signature and the same shingle count (`test_signature_ignores_a_boilerplate_line`).

## Queries

- **Q5.1** `FILE_OWNERS_SQL`, `LINE_BATCH_SQL`, `LINE_OWNER_ROLLUP_SQL`: the owner of each English `file_sha`; the distinct `(owner, line)` pairs of one batch (`batch_lines` is a registered frame of `file_sha, line`); distinct owners per line over the pair files, `HAVING owners >= min_owners`.
- **Q5.2** `ENGLISH_SLICE_TEXTS_SQL`, `ENGLISH_SLICE_COUNT_SQL`: the distinct English texts of one hash slice (`hash(file_sha) % 4`), one `content` per `file_sha`; sorted by `file_sha` for signing, unsorted for the owner count. The count sizes the chunks.
- **Q4.2** `SKILL_TEXTS_SQL` (in `build_registry_lineages.py`, see [registry lineages](07_registry_lineages.md)): the texts of one batch of registry blobs.

## Outputs

`boilerplate_lines.parquet` (full data folder, shared by dev and full): `line` (the lowercase key), `owners`. Only lines with `owners >= BOILERPLATE_MIN_OWNERS`, highest count first.

`dataset_signatures/part_sXX_cYYYYY.npz` (full data folder, shared) and `registry_signatures.npz` (dev or full data folder). Each `.npz` has:

- `ids`: `file_sha` for the dataset, `lineage_id#version_seq` for the registry, e.g. `acme/app::skills/demo#1`
- `signatures`: `uint64 [n, 128]`
- `sizes`: `int32 [n]`, the shingle count of each text
- `empty_count` and `empty_examples`: texts with no signature, and up to two of them as `id | start of text`

`load_signatures(path)` returns `(ids, signatures, sizes)` from one file or from every part file of a folder in name order. The full dataset folder holds about 1.7 GB of hash values; G14 can read it part by part.

## Limits

- **A short line is never boilerplate.** Two-word headings stay in every signature. They add few shingles, so they move a Jaccard estimate little.
- **Real skill text can be removed.** A line copied into 50 owners' skills is removed even when it is real content of one popular skill, because its copies are what made it common. The copies still match on their other lines.
- **The cutoff is an owner count over the whole dataset**, not a share. A future dataset with many more owners would need the value checked again.
- A dataset text is identified by its `file_sha`, so two different repos with the same text share one signature. Stage 5c maps the sha back to its repos.
- MinHash only estimates Jaccard. With 128 values one standard deviation is at most `0.5 / sqrt(128)`, about 0.044, so an estimate can be off by about ±0.09. Stage 5c checks close calls with a line diff.
- A body of fewer than 5 words is one shingle (all its words), so very short texts match only when they are identical.

The full-run numbers are in the [boilerplate and signatures report](../report/09_boilerplate_signatures_report.md).
