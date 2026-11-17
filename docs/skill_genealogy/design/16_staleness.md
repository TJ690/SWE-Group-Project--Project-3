# Design: staleness (stage 10)

Card G22 · Code: `measure_staleness.py` · [Code flow](../code_flow/16_staleness.md)

## Problem

RQ3 asks whether copies fall behind the registry they came from. Stage 9 says what a copier changed against the version the copy started from. It does not say whether the registry moved on, what the copy missed, or whether a missed update mattered. A copy whose `SKILL.md` is current can still run an old script the registry has since fixed, and a copy that was deleted or whose registry skill was removed has nothing to be behind.

## Design at a glance

Each copy is placed on its lineage's timeline at two points: the version its text holds now, and the as-of date it is measured at. The releases between them are what it missed. The same is done for the folder with package versions. Each copy gets one state, and its history gives the moments it did take a registry version.

## Decisions

**Held version, not origin.** The origin (stage 7) is where the copy started. A copy that synced later holds a newer version and is not as far behind as its origin says. So the held version is the version the current text is, exactly or up to whitespace; only a modified copy is matched by similarity.

**A modified copy is matched only against versions that existed.** The most similar version released on or before the copy's date. A copy edited in March cannot hold a version released in May, even when the edit happens to look like it.

**An exact match needs no date.** Git dates can be set freely. A copy whose text is a registry version holds that version whatever its date says. When a registry went back to an older text, the newest version with that text is held, because that is the release the copy is equal to.

**Copies before every release are counted, not placed.** A modified copy dated before the lineage's first release has no held version and the state `unknown`. It stays in the table so a share has the right denominator. Stage 9 left these copies out because it needs an origin; staleness does not.

**The as-of date depends on the copy.** A deleted copy is measured to the day it was deleted: after that it cannot fall further behind. A copy with history is measured at its registry's snapshot, because releases after the snapshot are not in the data. A dataset-only copy is measured at the day the dataset captured it, because that is the text the dataset saw.

**A registry's snapshot has a floor.** Registries were mined on three days (2026-11-02, 11-06, 11-07), and no table records which day each one was. The newest commit of a registry's history is a lower bound, but a registry that went quiet in May would then be measured in May, and every copy of it would look months less stale than it is. The snapshot is the later of the newest commit and the first mining day, so it is at most 5 days early and never before a release the history holds.

**Versions behind are counted by number.** Stage 4a numbers versions in release order, and two versions can share a commit date. Counting by date would drop the second one.

**The package counts, but only for copies that kept the files.** The held package is the package version sharing the most files with the copy's folder, `SKILL.md` included, so a copy with the current text and an old script holds the older package. A copy that kept none of the package's files besides `SKILL.md` took the text only; it is `files_not_kept` and has no package lag. Without this rule every aggregator that drops `scripts/` would be "behind" on every script change it never had.

**`days_stale` starts at the first miss, text or package.** A copy behind only on its scripts is stale once the script update is older than the cutoff. Otherwise a missed script fix would leave a copy `behind` forever and never `stale`.

**Missed content is counted per update.** `missed_commands` is the number of missed updates that added a command, not the number of commands, so an update that adds ten lines of one example does not outweigh ten updates. `missed_risky` is true when any missed update added a command, a network reference or a script, or changed a script's risk flags (A16).

**Orphaned and removed are their own states.** A deleted copy (`removed`) and a copy of a skill the registry removed (`orphaned`) are not stale: one is gone, the other has no current version. A removal after the copy's as-of date does not orphan it.

**Sync events by exact blob.** As in stage 9a, a copy took a registry version only when its text is that version byte for byte. Inherited commits set where a fork starts but are not events, because they are the registry's releases, not the copier taking them.

**The stale cutoff is a run setting.** `--stale-days` changes it for one run, and the value is stored in every row, so two outputs with different cutoffs cannot be mixed up.

## Alternatives rejected

- **Measure from the origin.** Counts releases a copy later synced to as missed.
- **One global as-of date.** Either counts releases a registry history cannot hold or ignores ones it does.
- **The registry's newest commit as the snapshot.** Shortens the lag of copies of quiet registries by months.
- **The newest package by date as the held package.** Hides the old-script case the package measure exists for.
- **Package lag for every copy.** Makes text-only copies behind on scripts they never had.
- **`days_stale` from the text only.** A missed script fix never makes a copy stale.
- **Guess a version for copies dated before every release.** Hides a data problem (git dates) behind a made-up number.

## Effect (full run)

The full run measures 1,414,080 copy files in 71 minutes, none left out: 1,153,768 hold a registry version exactly, 236,431 are matched by similarity, 22,806 are dated before every release of their lineage and 1,075 share no 5-word phrase with any version.

| State | Files | Share |
|---|---|---|
| `stale` | 803,168 | 56.8% |
| `behind` | 60,426 | 4.3% |
| `up_to_date` | 391,756 | 27.7% |
| `orphaned` | 72,578 | 5.1% |
| `removed` | 66,148 | 4.7% |
| `unknown` | 20,004 | 1.4% |

Copies fall behind and stay there. 61.1% of copy files are behind or stale; a stale copy's first missed release is a median 174 days old (90th percentile 249), and a behind copy missed a median 3 releases. The share hardly depends on how the copy was linked: stale is 59.2% for `exact` links and 40–54% for the looser ones, all with a median of about 160–180 days. Counting distinct texts by their worst file, 149,739 of 300,124 texts are stale.

How a copy arrived matters more. Copies that came in a bundle (`arrival_group = bundle`, 853,730 files) are 65.1% stale; copies chosen on their own are 50.8%. Files whose arrival is unknown (mostly dataset-only copies, measured at the July capture date) are 51.9% up to date, because a shorter window leaves less to miss.

What was missed: 312,295 behind or stale copies (36% of them) missed an update that added a command, a network reference or a script, or changed a script's risk flags; 162,532 missed a new network reference and 90,526 missed a registry script change. 15,519 copies are behind only on their package: their `SKILL.md` is current but a script or other file is not. The package lag is measured for 1,009,703 copies; 90,319 took only the text (`files_not_kept`), 271,716 have no known folder and 42,342 are older than every package.

Copies rarely update. Of 1,096,620 copies with a history, 25,906 (2.4%) ever took a later registry version. The history holds 829,998 `copy` events, 61,424 `update` events and 1,137 `downgrade` events. A copy took its version a median 28 days after the release. 33,951 updates came on the day of the release, from 672 repos that sync automatically (skill collections such as `boisenoise/skills-collections` and `TuYv/ccpm`); the other updates came a median 8 days after the release. 23,141 `copy` events are dated before the release they took, from copies whose git date is earlier than the registry's.

## Effect (dev run)

10,679 copies in 54 seconds: 53.9% stale, 8.7% up to date, 34.5% unknown. The dev sample is mostly `cluster` and `near` links whose copies are dated before the registry's first release, so it is a smoke test, not an estimate.

## Limits

- A registry's snapshot can be up to 5 days early.
- `days_stale` is time since the release, not since the copier could have noticed it.
- A modified copy's held version is the most similar version by its date, which can differ from the one it was made from.
- Package lag includes changes to references and assets; `missed_script_updates` isolates scripts.
- A dataset copy whose folder GitSkills never listed has text lag only, and no copy without history has sync events.
- 882 copies hold a text the registry released only after the copy's as-of date (the registry took the text later, or git dates disagree). They are 0 behind and `up_to_date`.
- A sync event's lag is negative when the copy's git date is earlier than the release (23,141 `copy` events). Medians include them.
- The same-day updates of auto-syncing collections pull the median update lag to 0; read the update lag without them too.
