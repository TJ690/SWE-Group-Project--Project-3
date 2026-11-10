# Copied-skill history report

Results of the copied-skill history download (stage 6), read from `src/data/skill_genealogy/copied_skill_history/` on 2026-11-10. How the step runs is in [copied-skill history](../code_flow/12_copied_skill_history.md); why it is built this way is in the [design note](../design/12_copied_skill_history.md).

Every repository that holds a copy of a registry skill (stage 5c, `copy_candidates.parquet`) was cloned without file contents. For each copied skill folder, every file change was kept, along with the text of `SKILL.md` and of small scripts.

## How the tables were built

| Date | Run | What it did |
|---|---|---|
| 2026-11-10 | Full run (`full_run/12_copied_skill_history.sh`), 10 h 35 min | Cloned 60,958 repos; the export of the texts table ran out of memory |
| 2026-11-10 | `--retry-failed` | Cloned the 12 `error` and `timeout` repos again; exported all three tables |
| 2026-11-10 | `--retry-failed`, after the clone folder fix | Cloned the 4 repos that share a name up to letter case; 7 blocked repos stayed `error` |
| 2026-11-10 | `--merge-only` | Exported again and printed the summary with the stage 4a version rule |

All clones are from 2026-11-10. The store and the parquet files hold the same rows.

## What finished

| Status | Repos | Share | What it means |
|---|---:|---:|---|
| ok | 55,296 | 90.7% | History of the copied folders was saved |
| gone | 5,646 | 9.3% | Git reports the repository was not found |
| error | 7 | 0.0% | GitHub answers HTTP 403 (disabled or blocked) |
| empty | 6 | 0.0% | The repository has no commits |
| too_large | 3 | 0.0% | Over 20,000 copied folders; not cloned |
| timeout | 0 | | |

The three `too_large` repos are mirrors: `majiayu000/claude-skill-registry-data` (52,358 copied folders), `gabrielmoreira/agent-skills-mirror` (50,481) and `David-Li0406/meta-skill-evloving` (28,683). Together they hold 131,522 of the 1,414,038 target folders (9.3%).

The 7 blocked repos are `D2758695161/bounty-hunter-kit`, `QDaria/zipminator`, `btcfoxman/litellm`, `davebirr/Fabrikam-Project`, `effinrich/rh-nx-monorepo`, `eusouandersonluiz/mmos` and `theAbyssOfTime2004/myVault`.

An `ok` repo took a median of 2.4 seconds (90th percentile 10 seconds). The slowest were Firefox mirrors: `mozilla/enterprise-firefox` (1,630 s), `mozilla-releng/staging-firefox` (1,328 s) and `mozilla-releng/git-backing` (1,250 s). Clone time summed over repos is 84 hours; 8 at a time, the run took 10.6 hours.

## Copied folders per repo

| Copied folders | Repos | Folders |
|---|---:|---:|
| 1 | 22,630 | 22,630 |
| 2–4 | 16,306 | 44,268 |
| 5–19 | 14,636 | 136,055 |
| 20–99 | 5,692 | 227,865 |
| 100–999 | 1,505 | 425,495 |
| 1,000 or more | 189 | 557,725 |

Most repos hold one or a few copies. Most folders sit in a few large repos: the 189 repos with 1,000 folders or more hold 39% of all target folders. Rename following is on for the 54,536 `ok` repos with at most 200 folders (479,783 folders). It is off for the 760 repos above that line (617,119 folders).

## Folders without history

Of the 1,112,615 target folders in `ok` repos, **16,036 (1.4%)** have no version of their own `SKILL.md`.

- **901 repos (12,923 folders)** cloned without error but have no change row at all. Their default branch never held the folder under that path. Either the dataset saw the copy on another branch, or the repo's history was replaced after the snapshot (a force push, or a repo deleted and created again under the same name).
- The other 3,113 folders have no own `SKILL.md` version although their repo has history: other copied folders of the repo are kept, or the folder's rows are only other files or nested skills.

## Versions per copied skill

A version is a distinct blob of the folder's own `SKILL.md` that is not a delete (the stage 4a rule; nested `x/SKILL.md` files of other skills are not counted).

| Versions | Folders | Share of folders with history |
|---|---:|---:|
| 1 | 955,314 | 87.1% |
| 2 | 72,121 | 6.6% |
| 3–5 | 49,909 | 4.6% |
| 6–20 | 17,718 | 1.6% |
| 21 or more | 1,517 | 0.1% |

The median is 1 and the 90th percentile 2; **12.9%** of copied skills were changed after they were first added. The most-changed copies are `vercel/turborepo` `skills/turborepo` (410 versions), `getsentry/cli` `plugins/sentry-cli/skills/sentry-cli` (180) and `kdeldycke/repomatic` `.claude/skills/repomatic-ship` (162). These are active project repos; stage 7a decides from the dates whether such a folder is a copy or the origin.

Whitespace-only edits are separate blobs here. Stage 7 counts versions after `normalize_text`, so its counts can only be lower.

## When copies first appear

The first `SKILL.md` commit of each copied folder, in UTC:

| Quarter | Folders |
|---|---:|
| before 2025 | 8 |
| 2025 Q1–Q3 | 105 |
| 2025 Q4 | 12,647 |
| 2026 Q1 | 351,797 |
| 2026 Q2 | 643,300 |
| 2026 Q3 | 88,681 |
| 2026 Q4 | 323 |

91% of copied folders get their first `SKILL.md` between January and June 2026. The 8 dates before 2025 were not checked. Stage 7a compares each copy date with the registry version's date, so a wrong commit date shows up there as a copy older than its source. The first commit is 2015-04-17 and the last 2026-11-10.

## What the changes are

The store holds **12,079,171** change rows.

| Role | Rows | Share |
|---|---:|---:|
| own | 11,665,849 | 96.6% |
| former_folder | 241,277 | 2.0% |
| copy_source | 172,045 | 1.4% |

| File | Rows |
|---|---:|
| `SKILL.md` | 1,742,558 |
| Script, not `SKILL.md` | 1,366,835 |
| Other file | 8,969,778 |

Following a rename also sees folders that are not the copy. Those rows were dropped and counted on the status rows: 78,259 `not_ours`, 102,202 `after_copy` and 30 `twin`.

**Parent targets.** 736 targets are a repo root (`.`) and 6,789 targets contain another target folder. Their logs include the nested skills' files: 64,020 `SKILL.md` rows in 4,744 folders are a nested `x/SKILL.md`. They are kept in the changes table and are not counted as the parent's versions.

**Repeated keys.** 109 keys of (`repo`, `skill_folder`, `commit`, `file_path`, `status`) appear twice. Each is one skill folder that lives at two paths in the same commit. Keyed by `repo_path`, as stage 3 states the unique key, there are none.

## Texts and size

`copied_skill_history_texts.parquet` has **635,021** texts (8.0 GB of UTF-8 before compression), one row per sha, sorted by sha. Every non-delete `SKILL.md` change has its text. On disk, the store is 3.7 GB, the texts file 2.9 GB, the changes file 423 MB and the status file 2.7 MB.

## For later stages

- Read `commit_date` as UTC.
- Use `file_path = SKILL.md` (any case) with role `own` or `former_folder` for a skill's own versions.
- Leave out the files of a nested skill folder when reading a parent target.
- A `gone`, `too_large` or blocked repo, or a folder without history, has no copy date. Report these copies as undated rather than dropping them.
- Compare a copy with the registry at the same date. Both histories were downloaded in November 2026; the dataset snapshot is older.
