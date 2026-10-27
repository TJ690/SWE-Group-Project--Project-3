# s0. Intake: open, verify, sample

**Code:** `src/skillsig/s0_intake/` (`loader.py`, `schema_check.py`, `devsample.py`) · **Tests:** `tests/s0_intake` · **Run:** [EXECUTION_STEPS.md § s0](../../EXECUTION_STEPS.md#s0-intake-stage)

The pipeline's receiving dock: it opens the delivered dataset, refuses to continue if it is not the dataset the pipeline was written for, and draws the 50,000-skill dev sample. It makes no features and no labels.

**Name:** the package is `skillsig.s0_intake` (renamed from `s0_data`). The CLI command is still `python -m skillsig data`, and its output keeps the name `s0_devsample_ids.parquet`.

---

## At a glance

| | |
|---|---|
| **Reads** | The whole dataset: raw `artifacts`, `repos`, `artifact_siblings`, `mining_runs`, and the four generated tables from [P.1](../P_prepare.md) |
| **Checks** | Every file exists, every expected column exists, and (full data) four exact row counts |
| **Writes** | One file: `src/data/derived/s0_devsample_ids.parquet` (50,000 rows) and its manifest |
| **Provides to later stages** | The sample's path (`output_path`, imported by 14 modules in s1–s3) and the snapshot date (`snapshot_date()`, imported by `s1_registry/versions.py`, `s1_registry/match.py`, `s2_features/repo_context.py`) |
| **Does not** | Copy data, compute features, give labels, or filter what full runs process |

The stage always works on the **whole** dataset. Only its output is a sample. With `--dev-sample` the stage itself changes very little: it skips the four row-count checks (the column check still runs) and still draws the sample from the full population.

---

## 0.1 Data check and dev sample

### What it does

1. **Opens the dataset.** Registers eight DuckDB views: the four raw tables and the four generated ones.
2. **Verifies it.** Checks that every expected column is present and, on the full data, four row counts. Any problem stops the run.
3. **Draws the dev sample.** A seeded sample of 50,000 distinct skills, grouped by location, history and copy count, written to `s0_devsample_ids.parquet` (`file_sha`, `stratum`).

It also exposes `snapshot_date(con)` = `max(mining_runs.finished_at)` (2026-07-20), the single "today" used by later steps.

---

### Step 1: open the dataset

`connect()` runs `CREATE VIEW <name> AS SELECT * FROM read_parquet('<glob>')` for each table. A view is a window onto the Parquet files, not a copy, so nothing of the 12.5 GB is duplicated and setup is instant. If any file is missing it stops with:

```
missing dataset at …; run: python src/data/download_data.py
```

---

### Step 2: verify the dataset

`schema_check.VIEW_COLUMNS` lists the columns each view must have (30 for `artifacts`, 19 for `skill_features`, …). On the full data it also checks these counts with `==`:

| Check | Expected |
|---|---|
| `artifacts` rows (all `SKILL.md` files) | 3,797,117 |
| `artifacts` with `dedup_primary = 1` (distinct skills) | 1,877,981 |
| `repos` rows | 282,200 |
| `mining_runs` rows | 7 |

Any problem raises `schema check failed:` followed by the full list, for example `artifacts missing column body_chars` or `repos row count 281900 != 282200`.

This is the stage's gatekeeping job and the reason to run it even when nobody uses the sample: it is the one place that confirms the dataset before the long later steps.

---

### Step 3: draw the dev sample

#### Why there is a dev sample

The pipeline does not depend on it. Full data is the default: every step processes everything unless `--dev-sample` is added, and in a full run no step reads the sample file. The sample exists only to make development fast:

- **A full run is slow.** The longest steps are scaffold signals, MinHash clustering, the style model and wording.
- **Rule and threshold changes need quick feedback.** On 50,000 skills every step finishes quickly, so a mistake shows up immediately instead of after a full run.
- **It sits between unit tests and the full run.** Unit tests use tiny fake Parquet files; the sample is real data, small enough to iterate on.

50,000 skills is about 2.7% of the 1,877,981 distinct skills.

#### The three properties the sample is grouped by

Each distinct skill gets a **stratum** (group) key built from three properties:

```
<location>|history=<0|1>|copies=<1 | 2-9 | >=10>
```

Location and history come from the skill's representative row (`dedup_primary = 1`); copies counts every file with that `file_sha`.

**1. Location** (`location_class`): where the `SKILL.md` sits in its repository. The value comes from the dataset authors; the pipeline does not compute it.

| Value | Meaning | Example path | Distinct skills |
|---|---|---|---|
| `canonical` | A standard agent folder | `.claude/skills/commit/skill.md` | 273,721 (14.6%) |
| `skills-dir` | Inside some `skills/` folder that is not an agent's standard one | `wahlkampfrecht-praxis/skills/…/SKILL.md` | 1,037,938 (55.3%) |
| `other` | Anywhere else | `project-management/strategy-frameworks/…/SKILL.md` | 566,322 (30.2%) |

**2. History** (`history_fetched`): whether the dataset includes commit information for the file: first and last commit author, message and date, and a commit count. 458,137 distinct skills (24.4%) have it. History matters most for this project because almost every agent and human signal comes from commits (AI trailers, bot authors, batch size, revisions). It is tied closely to location: nearly every `canonical` skill has history and most others do not, which suggests the dataset authors fetched history mainly for canonical files.

**3. Copies**: how many files in the whole dataset have exactly the same text (the same `file_sha`).

| Value | Meaning | Distinct skills |
|---|---|---|
| `1` | The text exists only here | 1,489,480 (79.3%) |
| `2-9` | Copied a few times | 352,733 (18.8%) |
| `>=10` | Widely copied; the bar for the `highly_templated` label | 35,768 (1.9%) |

**Why these three?** Together they decide which label rule a skill can reach. Copies drive `highly_templated`; history is required for `agent_assisted` and `likely_human`; location separates installed agent skills from collections and other files. A sample that keeps all three in proportion exercises every rule path.

#### The 18 groups: full data vs. sample

3 locations × 2 history values × 3 copy bands = 18 groups. Current sample, largest group first:

| Group | Full data | Share | Sample | Share |
|---|---|---|---|---|
| skills-dir, no history, 1 copy | 731,472 | 38.95% | 19,469 | 38.94% |
| other, no history, 1 copy | 419,709 | 22.35% | 11,171 | 22.34% |
| canonical, history, 1 copy | 186,664 | 9.94% | 4,969 | 9.94% |
| skills-dir, no history, 2–9 copies | 172,940 | 9.21% | 4,604 | 9.21% |
| skills-dir, history, 1 copy | 96,586 | 5.14% | 2,572 | 5.14% |
| other, no history, 2–9 copies | 80,285 | 4.28% | 2,138 | 4.28% |
| canonical, history, 2–9 copies | 66,341 | 3.53% | 1,767 | 3.53% |
| other, history, 1 copy | 54,346 | 2.89% | 1,447 | 2.89% |
| skills-dir, history, 2–9 copies | 22,924 | 1.22% | 611 | 1.22% |
| canonical, history, 10+ copies | 19,550 | 1.04% | 521 | 1.04% |
| skills-dir, no history, 10+ copies | 12,438 | 0.66% | 332 | 0.66% |
| other, history, 2–9 copies | 9,924 | 0.53% | 265 | 0.53% |
| other, no history, 10+ copies | 1,834 | 0.10% | 50 | 0.10% |
| skills-dir, history, 10+ copies | 1,578 | 0.08% | 43 | 0.09% |
| canonical, no history, 1 copy | 703 | 0.04% | 20 | 0.04% |
| canonical, no history, 2–9 copies | 319 | 0.02% | 9 | 0.02% |
| other, history, 10+ copies | 224 | 0.01% | 7 | 0.01% |
| canonical, no history, 10+ copies | 144 | 0.01% | 5 | 0.01% |
| **Total** | **1,877,981** | | **50,000** | |

Every group's share in the sample matches the full data to within 0.01 percentage points. At 50,000 seats even the smallest group (144 skills) earns about 4 seats by proportion alone, so the "one seat per group" guarantee below makes almost no difference at this size; it matters only for much smaller samples (as in the unit tests, which draw 6).

#### How seats are shared out (`_allocate`, `_spread`)

1. If the population is no larger than `n`, take everything.
2. If there are more groups than seats, give one seat to each of the `n` largest groups.
3. Otherwise give **one seat to every group**, then share the remaining seats in proportion to each group's remaining size. Fractions are settled by largest remainder, and no group gets more seats than it has skills.

#### How skills are picked inside a group

Rows are ordered by `hash(file_sha || seed)` (seed `20261015` from config) and the first `need` are taken. Optional `dev_force_include` ids are inserted first and count toward their group's quota; an id that is not in the dataset is kept with stratum `absent`.

#### When the sample file is created and when it is read

**s0 always creates it.** `python -m skillsig data` writes `s0_devsample_ids.parquet` on every run (unless skipped because nothing changed), with or without `--dev-sample`. The flag only decides whether the row counts are checked:

| Command | Schema check | Writes `s0_devsample_ids.parquet` |
|---|---|---|
| `python -m skillsig data` | Columns + row counts | Yes |
| `python -m skillsig data --dev-sample` | Columns only | Yes, the same 50,000 ids |

So the file exists after any s0 run, even if no dev run is ever planned.

**Later steps read it only when they get `--dev-sample`.** The flag is passed to each step's `run(cfg, dev=..., force=...)`; the mode comes from the command line, never from the files.

| Command | Reads the sample? | Rows processed | Manifest `inputs` include `s0_devsample_ids.parquet`? |
|---|---|---|---|
| `python -m skillsig registry --only match` | No | All skills | No |
| `python -m skillsig registry --only match --dev-sample` | Yes | Sampled skills only | Yes |

In dev mode a step semi-joins its rows on the sample's `file_sha` list. Most counts that describe the whole population are still computed over everything: batch size, copy count, identifier document frequency, repository size. A copy count computed inside the sample would be wrong (a skill copied 500 times might have one copy in the sample), so for those features dev values match full-run values for the same skill.

Two exceptions: near-duplicate clusters (2.6) and scaffold spread counts (2.9) are computed **inside the sample only**, so their dev values are far smaller than full-run values. See [s2 § Full data vs. dev sample](../s2_features/README.md#full-data-vs-dev-sample).

A later step never creates the sample file. Run with `--dev-sample` while it is missing, it stops with:

```
missing src/data/derived/s0_devsample_ids.parquet; run: python -m skillsig data --dev-sample
```

Plain `python -m skillsig data` creates the same file.

#### Dev and full outputs share one set of file names

A dev run and a full run write to the **same** paths (for example `src/data/derived/s1_registry_matches.parquet`). No step chooses between a "dev file" and a "full file": it always reads the one file at the fixed path, and the `--dev-sample` flag only decides which rows it processes.

- **How to tell which mode built a file:** open its `.manifest.json`. If `s0_devsample_ids.parquet` is among the `inputs`, the file was built in dev mode; otherwise on full data.
- **Switching modes rebuilds automatically.** The manifest's input list differs between modes, so the next run in the other mode sees a mismatch and rebuilds; `--force` is needed only after code changes.
- **Mixing modes is not detected.** A step does not check whether its upstream files were built in the same mode it is running in. Running `registry --only match --dev-sample` overwrites the full `s1_registry_matches.parquet`, and a later full run of `features --only template_sim` then silently scores all skills against dev-sized matches.
- **Safe practice:** run a whole chain either with `--dev-sample` or without it. After any dev run, rerun the full chain without the flag from the first step that ran in dev mode before reporting anything.

---

### Design decisions

- **Why check exact row counts?** Every number in the report assumes this dataset version. A truncated download or a new release should fail at step 0.1, not show up weeks later as slightly different shares. The check is skipped in dev mode, where the totals intentionally differ; the column check still runs.
- **Why views instead of copying the data?** No duplicate of 12.5 GB, no stale copy that disagrees with the original, instant setup.
- **Why a dev sample at all?** Full runs are slow. A 50,000-skill sample (about 2.7% of distinct skills) runs every step quickly, so rule changes can be tried quickly and tests on real data stay fast.
- **Why group the sample instead of a simple random draw?** A random 50,000 would match these shares only on average; each run would wobble, and the smallest groups (144 to 703 skills, expected 4 to 19 seats) could come out at zero or double. Grouping fixes every share to the population's, so a dev run always has the same mix of copy bands and history, and both the templated and the commit-based rules always get exercised.
- **Why "one seat per group, then proportional"?** Pure proportional allocation can round a small group to zero when the sample is small; equal allocation would distort every share. One guaranteed seat plus proportional fill keeps coverage and keeps the sample representative.
- **Why order by `hash(file_sha || seed)`?** It is deterministic across machines, DuckDB versions and thread counts, and does not depend on file order. Changing `seed` draws a different but equally valid sample.
- **Why store the stratum with each id?** So it is visible why a skill is in the sample, and so results can be reweighted to the population if a future sample is not proportional.
- **Why a single snapshot date?** The dataset was mined over several runs. "Days since last commit", "post-snapshot registry version" and repository age must agree on what "now" is, so they all use `max(finished_at)` (config `rq2.snapshot_date: auto`).

---

### Output

`src/data/derived/s0_devsample_ids.parquet`: 50,000 rows, columns `file_sha`, `stratum`, sorted by `file_sha`, with `s0_devsample_ids.parquet.manifest.json` beside it. The step is skipped when the dataset directories and the config are unchanged; `--force` rebuilds.

---

## Testing

### Checks the stage runs on the data

Every `python -m skillsig data` run performs these checks:

| Check | When | On failure |
|---|---|---|
| Every Parquet file exists | Always | Stops and names the download command |
| All eight views exist, with every expected column | Always | Stops and lists each missing column |
| Four exact row counts (table in Step 2) | Full data only | Stops and shows actual vs. expected |

### Unit tests (`tests/s0_intake/`)

Eleven tests. Ten run on small fake Parquet files and need no dataset; one needs the real dataset.

**`test_loader.py`: opening, snapshot date, stage run**

| Test | Checks |
|---|---|
| `test_views_register_and_join_on_file_sha` | Views register and can be joined on `file_sha` |
| `test_views_read_parquet_without_copying` | `artifacts` is a VIEW over `read_parquet`, not a table copy |
| `test_missing_dataset_names_download_command` | A missing dataset raises an error naming `python src/data/download_data.py` |
| `test_snapshot_date_is_max_finished_at` | The snapshot date is the latest `finished_at` |
| `test_run_writes_sample_and_skips_when_unchanged` | A run writes the file with columns `file_sha, stratum` sorted by `file_sha`; a second run is skipped; `--force` rebuilds |
| `test_run_full_mode_rejects_fixture_counts` | A full-data run on the fake data fails on row count, proving the count check stops the stage |

**`test_schema_check.py`: the schema check**

| Test | Checks |
|---|---|
| `test_missing_column_is_named` | A dropped column (`body_chars`) is named in the problem list |
| `test_dev_skips_count_checks` | Dev mode skips the counts; full mode reports the mismatch |
| `test_full_counts_match_data_dictionary` (`@pytest.mark.data`) | On the real dataset: the schema check passes, the snapshot date is 2026-07-20, 1,877,981 distinct skills, and six table sizes match the data dictionary (3,797,117 artifacts, 7,264,865 siblings, 282,200 repos, 7 mining runs, 1,840,872 `skill_features`, 3,797,117 `file_agents`) |

**`test_devsample.py`: the dev sample**

| Test | Checks |
|---|---|
| `test_sample_is_deterministic_and_covers_strata` | Two draws give the same sample; every group is covered; each skill has the right group key; a different seed gives a different sample |
| `test_force_include_always_kept` | `dev_force_include` ids are kept even outside the quota; an unknown id is kept as `absent` |

Run them with:

```bash
python -m pytest -q tests/s0_intake
```

```bash
python -m pytest -q -m data tests/s0_intake
```

The second command runs the real-data test and needs the dataset on disk.

### Testing gaps

- The proportional seat-sharing (`_spread`) is only exercised indirectly, with 6 seats.
- No regression test checks the real 50,000-row sample (its size or group mix).
- Neither the stage nor the tests check column types, empty values or value ranges.

---

## Limits

- The schema check covers names and counts only, not types, empty values or value ranges.
- **Later stages do not require that s0 passed.** In a full run they neither read its output nor check its manifest (they read the snapshot date straight from `mining_runs`), so skipping `python -m skillsig data` lets a full run start on a damaged or different dataset.
- The sample is representative only on the three grouping properties. Shares of other things (labels, skill types, repository kinds) on the sample are close to, but not guaranteed to equal, the full-data shares; the full run is what gets reported.
