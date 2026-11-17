# Sprint 3 Report: Where Do Skills Come From?

This report follows the [Sprint 2 report](sprint2_report.md). Sprint 2 built the authorship pipeline and found that copied content is a large part of the signal: 16% of skills are highly templated, and 36% of skills with a disclosed agent also show reuse. Sprint 3 asks where those copies come from. It builds a skill genealogy: which registry skill each copy started from, what the copier changed, and whether the copy kept up with later updates.

## 1. The question in plain words

A **skill** is a `SKILL.md` file, often with scripts next to it, that tells an AI coding agent how to do a task. A **registry** is a popular repository that publishes many skills. People copy skills from registries into their own projects.

Our genealogy questions:

> **RQ1, origin.** Is a skill a copy of a registry skill, and of which version?
>
> **RQ2, change.** What do people change when they copy a skill?
>
> **RQ3, staleness.** Do copies fall behind the registry they came from?

The full statement is in [RESEARCH_QUESTION_GENEALOGY.md](../RESEARCH_QUESTION_GENEALOGY.md).

**This sprint:** link every copy in the dataset to its source, download the history of both sides, and measure change and lag.

## 2. Background

### 2.1 What Sprint 2 left open

- Templated labels find reuse but cannot say who copied whom. Exact sharing does not give a direction.
- The 31 hand-picked registries covered only part of the copied skills.
- A reader of a single file cannot see that 500 other files share its text.

### 2.2 What other work tells us

- **Code is copied across projects at scale.** Large studies of GitHub found that much code is duplicated across repositories (Lopes et al., DéjàVu, OOPSLA 2017) and that reuse often happens by copying whole files (Gharehyazie et al., MSR 2017).
- **Copied snippets go stale.** Stack Overflow code copied into projects is often outdated or unattributed, and copies rarely receive later fixes (Ragkhitwetsagul et al., TSE 2019; Baltes and Diehl, EMSE 2019).
- **Technical lag** measures how far a dependency is behind its newest release (Zerouali et al., 2018). We apply the same idea to a copied skill and the registry it came from.
- **What is new for skills.** A skill is executed by an agent with little review, and it can carry scripts and permissions (`allowed-tools`). A missed update is a behaviour the agent still follows.

## 3. The data

| What | Count |
|---|---:|
| Distinct skill texts in GitSkills | 1,877,981 |
| English texts (used) | 1,633,285 (87.0%) |
| Registries chosen by rule | 4,806 |
| – source registries | 4,060 |
| – copier registries (crawlers and catalogues) | 702 |
| English registry skills (lineages) | 188,787 |
| Registry text versions | 817,485 |
| Copy files found in the dataset | 1,414,080 |
| Repositories holding a copy | 60,958 |

**New data.** We downloaded the git history of every registry (7.9 million file changes) and of every repository holding a copy (12.1 million file changes). The clones are from November 2026; GitSkills was captured in July 2026.

## 4. Method

### 4.1 Pipeline

| Step | What it does |
|---|---|
| Language labels, registry tree check, registry selection | Label each text's language; choose registries by a written rule (100+ stars, 5+ English skills); check the file trees of under-counted repos, which added 1,240 registries such as `anthropics/skills` |
| Registry history, READMEs, kinds | Download each registry's history and README; label each registry a source or a copier |
| Registry lineages, package versions | Build the versions of each source skill, for the text and for the whole folder |
| Text signatures, re-hosts, dataset copies | Remove boilerplate lines, sign every text with MinHash, find registries that re-host each other, and link dataset texts to registry versions |
| Copied-skill history | Download the history of every copied folder |
| Copy families, family trees | Give each copy an origin version and a family; build a tree for each family |
| Copy changes | Compare each copy with its origin: text, every version, and the folder's scripts |
| Staleness | Measure how far each copy is behind, and when copies took updates |

### 4.2 Key decisions

- **A copier is never an origin.** Crawler registries such as `majiayu000/claude-skill-registry` (312,153 folders) hold other people's skills. They are labelled once by the registry-kinds step and left out of the lineages.
- **Every fuzzy link must pass a line check.** A link is kept only when at least 50% of the registry version's lines are in the copy. This dropped 7,017 of 294,596 candidate links.
- **The same code measures registries and copies.** A registry update and a copier's edit are described by the same functions, so their shares can be compared.
- **Held version, not origin, for staleness.** A copy that synced later is measured from the version it holds now.
- **Each copy is measured at a date it could have known.** Copies with history are compared on the registry snapshot day; dataset-only copies on the dataset capture day; deleted copies on the day they were deleted.

### 4.3 Validation design

- **Link recall.** 42,554 copies whose git history holds an exact registry blob are a test set. Each text method is scored on whether it finds the proven source.
- **Sensitivity.** Headline numbers with and without the largest hub, same-owner copies, the largest copier, and copies dated before their release; stale cutoffs of 30, 90 and 180 days.
- **Hand checks.** A registry-kind review sheet and a language sample sheet are written. Neither is filled yet.

## 5. Implementation

The genealogy is a separate Python package, `src/skill_genealogy/`, with one module per stage.

```bash
python -m skill_genealogy.<module> [--dev] [--force]
```

- **Settings in one file.** Every cutoff is a named value in `settings.py`, with a comment saying where it comes from.
- **Network only in four steps.** The registry tree check, registry history, README and copied-skill history downloads use GitHub; every other step reads saved tables and can be rerun offline.
- **Resumable downloads.** Each repository is stored when it finishes, so a stop costs only the repos in flight.
- **Dev mode is a sample of registries.** 30 registries drawn with a fixed seed, so families are not cut apart. Dev and full outputs go to different folders.
- **Memory.** DuckDB with a memory cap for large tables, and texts in batches: the registry history alone is 20 GB of text on a 16 GB laptop.
- **Tests.** 334 genealogy tests pass (`python -m pytest -q tests/skill_genealogy`).

Commands are in [RUNBOOK_GENEALOGY.md](../RUNBOOK_GENEALOGY.md), tables in [DATA_DICTIONARY_GENEALOGY.md](../DATA_DICTIONARY_GENEALOGY.md), and design notes in [docs/skill_genealogy/design](../docs/skill_genealogy/design/README.md).

## 6. Preliminary results

### 6.1 About one in five English texts outside the registries is a copy (RQ1)

287,579 of the 1,576,434 English texts outside the registries (18.2%) copy a registry version. They sit in 1,414,080 files.

| Link | Files | Share |
|---|---:|---:|
| exact | 1,121,631 | 79.3% |
| near (Jaccard ≥ 0.80) | 168,774 | 11.9% |
| history_exact (proved by the copy's history) | 42,554 | 3.0% |
| near_contained | 41,324 | 2.9% |
| cluster | 39,035 | 2.8% |
| history_near | 762 | 0.1% |

Exact copies are 39% of linked texts but 79% of copy files: an unchanged skill is often in many repositories. The most copied skills are from `anthropics/skills` (`skill-creator` in 3,142 repos, `frontend-design` in 3,478).

Registries also copy each other: 17,415 registry skills (9.2%) are re-hosts of an older registry skill. 6,718 of those links join two repos of the same owner (templates, not copying).

### 6.2 Most copies arrive in bundles

Of the copies with history, 78% arrived in a commit that added 20 or more skill folders at once. Only 9% were chosen one to four at a time. About half of each group arrived within a day of the repository's creation.

### 6.3 Line checks find edited copies that MinHash misses

On the 42,554 copies proven by git history:

| Method | Recall | Recall on texts changed since the crawl |
|---|---:|---:|
| exact (normalised text) | 16.9% | 24.7% |
| MinHash Jaccard ≥ 0.80 | 70.6% | 59.8% |
| containment ≥ 0.80 | 88.8% | 81.8% |
| line check ≥ 0.50 | 98.4% | 93.0% |

A copy that grows keeps the registry's lines but drops under a Jaccard of 0.80. The near-duplicate clusters used for the Sprint 2 templated label miss many of these.

### 6.4 Copiers rarely change what they copy (RQ2)

Against the version it started from:

| State | Copies | Share |
|---|---:|---:|
| unchanged body | 1,139,332 | 89.3% |
| cosmetic (under 5% of lines) | 49,707 | 3.9% |
| substantive | 86,854 | 6.8% |

The share depends on the link: exact links are 0.6% substantive, cluster links 84.0%. 12,379 copies (1.0%) changed `allowed-tools`. When a copy adds a section, it is seldom one its registry adds later (mean heading overlap 0.097).

Measured the same way, a copier's own edit is smaller than a registry update:

| Population | Changes | Substantive | Adds a command | Adds a network reference |
|---|---:|---:|---:|---:|
| Registry updates (of copied skills) | 277,763 | 39.1% | 11.7% | 6.4% |
| Copy syncs to a registry version | 20,985 | 48.1% | 19.0% | 8.5% |
| Copy local edits | 128,285 | 31.5% | 9.0% | 4.6% |
| Copy against origin | 1,275,893 | 6.8% | 1.6% | 1.0% |

The folder tells a similar story. 20.4% of copies have a folder that differs from the origin package, mostly because aggregators keep `SKILL.md` and drop the scripts. 6,105 copies (0.59%) hold a script that gained a capability the registry's file did not have (network access, running commands, reading secrets).

### 6.5 Copies fall behind and stay there (RQ3)

| State | Files | Share |
|---|---:|---:|
| stale (missed a release over 30 days old) | 803,168 | 56.8% |
| behind (within 30 days) | 60,426 | 4.3% |
| up to date | 391,756 | 27.7% |
| orphaned (registry removed the skill) | 72,578 | 5.1% |
| removed (copy deleted) | 66,148 | 4.7% |
| unknown | 20,004 | 1.4% |

- A stale copy's first missed release is a median 174 days old.
- Bundle copies are 65.1% stale; copies chosen on their own 50.8%.
- Copies rarely update. Of 1,096,620 copies with a history, 25,906 (2.4%) ever took a later registry version.
- When a copy does update, it is a median 8 days after the release, leaving out 672 auto-syncing collections that update on the release day.
- 36% of behind or stale copies missed an update that added a command, a network reference or a script. This is a pattern rule, not a judgement that the update was a fix.

### 6.6 The stale share depends on choices we report

| Variant | Copy files | Stale | Up to date |
|---|---:|---:|---:|
| All copies | 1,414,080 | 56.8% | 27.7% |
| Without lineages of `sickn33/agentic-awesome-skills` | 1,045,744 | 49.2% | 33.5% |
| Without same-owner copies | 1,400,135 | 57.1% | 27.4% |
| Without copies in `majiayu000/*` repos | 1,361,695 | 57.0% | 27.3% |
| Without copies dated before their release | 1,316,241 | 58.3% | 27.9% |
| Compared on the registry snapshot day | 1,032,108 | 65.7% | |
| Compared on the dataset capture day | 317,178 | 39.3% | |

At a 90-day cutoff the stale share is 49.4%; at 180 days, 27.2%. One hub carries a quarter of the copies and moves the result by 7.6 points, so it is reported both ways.

## 7. What this means for the research questions

| Question | Where we stand after Sprint 3 |
|---|---|
| **RQ1**: Where do skills come from? | **Measured.** 18.2% of English texts outside the registries copy a registry version, mostly unchanged and mostly in bundles. Link recall is measured (98.4% for the line check); precision is not. |
| **RQ2**: What do copiers change? | **Measured.** Almost nothing: 89% of copies keep the body as it was. Copier edits are smaller than registry updates. |
| **RQ3**: Do copies fall behind? | **Measured, with a caveat.** 57% are stale and only 2.4% ever update. Whether the missed updates matter is not yet known. |
| Link to the authorship study | **Not done yet.** Copies should not count as independent evidence of how a text was written. |

## 8. Limits and open questions

- The origin is the earliest version among the selected registries, not the author.
- Link precision, registry kinds and change kinds have not been checked by hand.
- "Stale" counts every missed release; a typo fix counts like a fixed command.
- Most lineages are only months old, so lag depends on the snapshot date.
- 22% of copy files have no downloaded history and are measured from their dataset text only.
- One hub (`sickn33/agentic-awesome-skills`) carries 26% of copy files.
- The genealogy is English only.

The full list is in [THREATS_TO_VALIDITY_GENEALOGY.md](../THREATS_TO_VALIDITY_GENEALOGY.md).

## 9. Items pending

1. **Hand-label registry changes.** Two raters label about 300 registry version changes as security, correctness, capability, wording or cosmetic, with Cohen's kappa. If fixes are a meaningful share (about 15–20% or more), lead with "copies keep defects their source fixed"; otherwise report that what copies miss is mostly cosmetic. See [paper direction](../docs/skill_genealogy/paper_direction.md).
2. **Hand-check links and registry kinds.** A blind sample of near, contained and cluster links for precision, and the 487 low-confidence registry kinds.
3. **Summary tables.** One table per RQ, split by link kind, arrival group and origin role, with and without the largest hub.
4. **Time to update.** Survival curves for taking an update, with copies not yet updated counted as censored.
5. **Attribution.** Check whether copies keep the source's licence, author and source fields.
6. **Link back to authorship.** Report how much of the Sprint 2 labelled set is copies, and whether the labels change when copies are removed.

## 10. Where to find things

| Item | Location |
|---|---|
| Research questions | [RESEARCH_QUESTION_GENEALOGY.md](../RESEARCH_QUESTION_GENEALOGY.md) |
| How to run it | [RUNBOOK_GENEALOGY.md](../RUNBOOK_GENEALOGY.md) |
| Tables and columns | [DATA_DICTIONARY_GENEALOGY.md](../DATA_DICTIONARY_GENEALOGY.md) |
| Threats to validity | [THREATS_TO_VALIDITY_GENEALOGY.md](../THREATS_TO_VALIDITY_GENEALOGY.md) |
| Stage design notes | [docs/skill_genealogy/design/](../docs/skill_genealogy/design/README.md) |
| Stage reports (numbers) | [docs/skill_genealogy/report/](../docs/skill_genealogy/report/) |
| Paper direction | [docs/skill_genealogy/paper_direction.md](../docs/skill_genealogy/paper_direction.md) |
| Pipeline code | `src/skill_genealogy/` |
| Tests | `tests/skill_genealogy/` |
| Run scripts | `scripts/skill_genealogy/` |
| Result tables | `src/data/skill_genealogy/` |
