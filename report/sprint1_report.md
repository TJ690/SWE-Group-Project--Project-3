# Sprint 1 Report: Can We Tell Who Wrote a Skill?

## 1. The question in plain words

AI coding tools such as Claude Code, Cursor and Copilot now help write a lot of software. One kind of file they use is a **skill**: a `SKILL.md` text file that gives an AI agent step-by-step instructions for a task, such as "how to review code" or "how to make a PDF".

Our research question:

> Can we tell apart skills that look **human-written**, **agent-assisted**, or **highly templated** (copied from a standard template), using the file's structure, its wording, and its commit history? And how far can we trust that signal?

Why it matters: an agent follows a skill's instructions, and about one in nine skills ships scripts the agent may run. Understanding how a skill may have been made can help people review it. Authorship alone does not tell us whether it is safe or useful.

**This sprint:** explore the data to learn what signals exist and how good they are. The wording analysis (such as how many commands a skill gives) is planned for the next phase.

## 2. The data

We use the public **GitSkills** dataset (Hugging Face: `mvaccargiu/gitskills`).

| What | Count |
|---|---:|
| `SKILL.md` files found on GitHub | 3,797,117 |
| Repositories that hold them | 282,200 |
| Accounts that own those repositories | 195,841 |
| **Distinct skills** (after removing exact copies) | **1,877,981** |
| Distinct skills that have commit history | 458,548 (24.4%) |

We count each distinct content hash once (the unit of analysis), so a skill copied 500 times counts as one. The main counts use the full dataset. The TF-IDF similarity check uses a 20,000-skill sample.

### How the pipeline works

1. Download the raw Parquet files with `src/data/download_data.py`.

2. Run four DuckDB scripts in `db/` to build content features, agent-folder mappings, repository profiles, and sibling-file data.

3. Run the nine notebooks in `src/notebooks/`. They check data integrity, explore the dataset, apply label rules, and compare the groups.

4. Read the saved tables and plots in the notebooks. The charts used in this report are stored in `figures/`.

The notebooks group skills using copy counts and commit history. See [the README](../README.md) for the commands.

## 3. What the skill world looks like

### 3.1 The format is new and grew fast

The chart tracks first commits for distinct skills with history from October 2025 onward. Their monthly count peaks at 86,254 in May 2026. It also shows creation dates of repositories that host skills, which are not the dates those repositories first added skills. July 2026 is only partly collected, so its lower count does not show a confirmed decline.

![New skills and host repositories per month](../figures/01_adoption_over_time.png)

Most repositories are small, personal projects:

- 86.2% of repositories were created in October 2025 or later.

- 59.3% have no stars. Only 4.1% have 100 or more.

- The most common languages are Python (25.4%) and TypeScript (23.8%).

### 3.2 A few big repositories hold a huge share of the files

43% of repositories hold just one skill file. Yet a small group of very large repositories (312 of them) holds 37% of all files. Many of those are registries or mirrors that copy skills in bulk.

![Repositories by number of skill files](../figures/02_files_per_repo.png)

### 3.3 Skills are copied a lot

- 79.3% of distinct skills exist as a single file.

- But **50.5% of all files are exact copies** of a skill that exists elsewhere.

- The most copied skill appears in 1,409 files.

The chart uses log scales: most skills have one copy, and very few have hundreds.

![How many copies each skill has](../figures/03_copies_per_skill.png)

Where do the copies live? Of the 388,501 skills that are copied at all:

![Where copied skills appear](../figures/04_where_copies_live.png)

| Copied... | Skills | Share |
|---|---:|---:|
| inside one repository only | 82,413 | 21.2% |
| across repositories of one owner | 75,733 | 19.5% |
| across different owners | 230,355 | 59.3% |

### 3.4 What a typical skill looks like

The typical skill is short: a median of **639 words**. Seven in ten have between 250 and 2,000 words.

![Skill length](../figures/05_skill_length.png)

Other facts:

- 36.3% of skills include extra files besides `SKILL.md`. 11.4% include scripts, mostly Python (63% of those) and shell.

- 13.4% have broken or invalid front matter (the header block at the top).

- Among distinct skills with collected history, **66.3% have one recorded commit**. This does not tell us whether they were edited elsewhere.

![Number of commits per skill](../figures/06_revision_activity.png)

## 4. How we label authorship

We cannot see who typed a skill, so we use clues and call the result a **heuristic label**, not a fact.

The rules in `EDA_authorship.ipynb` are applied in this order:

| Label | Rule used in Sprint 1 |
|---|---|
| Highly templated | The same content hash appears in at least 10 files, with or without commit history. |
| Agent-assisted | History is available, the first-commit batch has fewer than 6 skills, and the first commit has a recognized AI co-author note or a coding-agent bot author. Release, sync, distribution, and other non-coding bots are excluded. |
| Likely human | History is available, a `User` account added the skill alone, neither the first nor last commit has a recognized AI note, and the content appears in only one file. |
| Insufficient evidence | None of the rules above apply. |

The AI note check looks for Claude/Anthropic, Cursor, Copilot, Codex/OpenAI, and Gemini. Bot groups are based on account-name patterns. These rules can miss tools or misread account names. A highly templated label takes priority even if the skill also has an AI note. Copy count is a sign of reuse, not proof that a template was used.

**Important:** 30.3% of distinct skills with history have a recognized AI co-author line in their first commit, and 95.2% of those name Claude.

### The bulk-install problem

If someone asks an AI tool to copy 1,000 skills into a project, all 1,000 get the AI co-author line, but the AI wrote none of them. So we exclude commit-based agent labels when the estimated batch contains 6 or more skills. The notebook estimates a batch by grouping representative skills with history by repository and first-commit timestamp, rather than by commit ID. This can miss copies or group separate commits that share a timestamp. A large batch is a warning sign, not proof of an install.

![How many skills are added in the same commit](../figures/07_bulk_commit_batches.png)

Of the 138,670 skills whose first commit names an AI tool:

| What happens to them | Skills | Share |
|---|---:|---:|
| Stay "agent-assisted" | 79,607 | 57.4% |
| Dropped as bulk installs | 53,705 | 38.7% |
| Already counted as templated | 5,242 | 3.8% |
| Made by a non-coding bot | 116 | 0.1% |

The cutoff choice matters. Agent-assisted would be 109,423 skills with a cutoff of 21, and 134,028 with no cutoff at all.

### The resulting labels

The rules are applied in order: templated first, then agent-assisted, then likely human.

![Authorship labels](../figures/08_authorship_label_counts.png)

| Label | Skills | Share |
|---|---:|---:|
| Likely human | 62,798 | 3.3% |
| Agent-assisted | 80,114 | 4.3% |
| Highly templated | 35,768 | 1.9% |
| Insufficient evidence | 1,699,301 | 90.5% |

Two things to keep in mind:

- "Likely human" only means we found no sign of AI. A missing AI note does not prove a person wrote it.

- 90.5% is "insufficient evidence" mostly because three out of four skills have no commit history. It does not mean a fourth kind of author.

## 5. Do the three groups look different?

### 5.1 Templated skills: yes

Highly templated skills are only 1.9% of distinct skills, but they make up **31.4% of all files** (1,192,572 files). They stand out:

| | Likely human | Agent-assisted | Highly templated |
|---|---:|---:|---:|
| Median words | 589 | 661 | 854 |
| Median headings | 10 | 11 | 18 |
| Has an "Examples" section | 12.9% | 12.3% | 28.5% |
| Has a "When to use" section | 14.1% | 13.5% | 35.5% |
| Has a `license` field | 5.2% | 3.6% | 20.8% |
| One recorded commit (among skills with history) | 57.8% | 54.6% | 94.4% |
| Median skill files in its repository | 5 | 8 | 238 |
| Lives in a repository with 100+ skill files | 4.0% | 5.4% | 61.3% |

![Structure by authorship label](../figures/09_structure_by_label.png)

The highly templated group has longer text, more sections, fewer recorded edits, and larger skill collections. Because copy count defines this group, these differences do not independently prove template-based authorship.

### 5.2 Human vs agent-assisted: barely

These two groups look almost the same on everything we measured:

| | Likely human | Agent-assisted |
|---|---:|---:|
| Mean length (characters) | 6,095 | 6,619 |
| Valid front matter | 89.4% | 89.3% |
| Mean commits | 2.69 | 2.68 |
| Has an "Examples" section | 12.9% | 12.3% |
| Includes a code block | 70.2% | 76.6% |
| Median repository size (skill files) | 5 | 8 |
| Zero-star repository | 49.3% | 52.2% |

The comparisons suggest that **where the skill is stored** may explain some differences. 71.8% of agent-assisted skills sit in a standard agent folder, against 42.8% of likely-human skills. Inside those folders, the two groups match almost exactly (median 694 vs 736 words).

Commit messages show one gap: 19.6% of agent-assisted skills have "install / sync / import" wording, against 8.7% for likely human. This is consistent with an install effect, but does not establish the cause.

### 5.3 Repository clues

| | Likely human | Agent-assisted | Highly templated |
|---|---:|---:|---:|
| Repository created before 2025 | 15.3% | 9.0% | 4.5% |
| Skill is under `.claude/` | 43.0% | 72.0% | 57.7% |
| Only skill file in its repository | 22.8% | 10.7% | 1.4% |

The `.claude/` number is partly circular. Claude Code both stores skills in `.claude/` and adds the co-author line our label reads.

## 6. Other findings that affect later work

- **Names are not identities.** The same front-matter `name` is used by many different texts. For example, `skill-creator` covers 2,765 different texts. We use the text hash, not the name.

- **Exact-copy removal misses some copies.** Removing front matter, ignoring case, and normalizing spacing merges 198,374 additional contents (10.6%). These bodies match under that rule even if their headers differ. In a 20,000-skill sample with bodies of at least 200 characters, 12.1% have a near-twin within the sample (similarity of 0.8 or higher). The TF-IDF check uses the first 5,000 normalized characters; it is not a full-dataset estimate. Most sampled near-twin pairs belong to the same owner.

- **Bots are rare as authors.** Only 4,630 skills were first committed by a bot. Of those, 65% came from `github-actions[bot]`, which syncs or publishes files and does not write them.

## 7. What this means for the research question

| Part of the question | Where we stand |
|---|---|
| Can we spot **highly templated** skills? | **We can identify highly copied text.** This group differs in structure, repository size and recorded edits, but copy count alone does not prove template use. |
| Can we spot **agent-assisted** skills? | **We can identify disclosed AI involvement.** Commit notes and coding-agent bot names provide clues. Structure shows limited differences so far. Wording has not been compared yet. |
| Can we spot **human-written** skills? | **Weak.** We can only say "no sign of AI". |
| How reliable is the signal? | **Not measured yet.** We have no ground truth to check the labels against. |

## 8. Limits and open questions

Here is what limits our results. We still need to check these in Sprint 2, and `THREATS_TO_VALIDITY.md` has the same list.

- We can't see who actually wrote a skill, so our labels are only clues, and "likely human" just means we didn't spot any sign of AI.
- An AI note in a commit might only mean someone installed the skill, and some tools don't leave a note at all.
- Finding the same skill in lots of places doesn't mean it came from a template.
- Only about one in four skills has commit history, and most of those come from early adopters and Claude Code users.
- We haven't yet checked how often our labels are right.

## 9. Next steps (Sprint 2 / Phase 2)

1. **Wording features.** Compute command-style language, tone and typical AI phrasing for all skills, including the 76% with no commit history.

2. **Hand-label a sample** of about 200 to 300 skills and report how often each labelling rule is right.

3. **Check the cutoffs.** Test how the labels change when the cutoffs (6 skills in a batch, 10 copies) move.

4. **Near-duplicates.** Run near-duplicate detection on the full population.

## 10. Where to find things

| Item | Location |
|---|---|
| Research question | `RESEARCH_QUESTION.md` |
| Data description | `DATA_DICTIONARY.md` |
| Analysis notebooks | `src/notebooks/` (`EDA_authorship`, `EDA_content`, `EDA_copies`, `EDA_repos`, and others) |
| SQL that builds the data files | `db/` |
| Charts used here | `figures/` |
