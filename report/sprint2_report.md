# Sprint 2 Report: Can We Tell Who Wrote a Skill?

This report updates the [Sprint 1 report](sprint1_report.md). Sprint 1 explored the data in notebooks. Sprint 2 turned that work into a tested pipeline, added new signals, and checked the labels against people. The full result tables and charts are in [FINDINGS.md](../docs/report/FINDINGS.md).

## 1. The question in plain words

A **skill** is a `SKILL.md` text file that gives an AI coding agent (such as Claude Code, Cursor or Copilot) step-by-step instructions for a task.

Our research question is unchanged:

> Can we tell apart skills that look **human-written**, **agent-assisted**, or **highly templated** (copied from a standard template), using the file's structure, its wording, and its commit history? And how far can we trust that signal?

We still call every result a **heuristic label**: a clue, not proof of who wrote the file.

**This sprint:** build the labelling method as code, test it, and measure how often it agrees with people.

## 2. Background

### 2.1 What Sprint 1 found

- Half of all `SKILL.md` files (50.5%) are exact copies of another file.
- Only one in four distinct skills (24.4%) has commit history, so commit-based rules cannot label most skills.
- An AI co-author line on a commit is a real clue, but a bulk install of many skills also carries one. We dropped commit-based agent labels for batches of 6 or more skills.
- Templated skills look clearly different from the rest. Human and agent-assisted skills look almost the same, and much of the gap is explained by where the file is stored (for example under `.claude/`).

### 2.2 What other work tells us

- **AI co-author lines are strong positives but weak negatives.** Claude Code adds "Co-Authored-By: Claude" by default, but users can turn it off ([DeployHQ](https://www.deployhq.com/blog/how-to-use-git-with-claude-code-understanding-the-co-authored-by-attribution)). Codex adds no attribution at all ([AIDev](https://arxiv.org/html/2507.15003v1)). A study of agent use found commit-level clues in only 58% of projects that visibly use agents at the file level ([Agentic Much?](https://arxiv.org/html/2601.18341v1)). So a missing line never proves a person wrote the file.
- **Text-only AI detectors are unreliable on short text.** Their true-positive rate at 1% false positives can fall close to 0% under practical conditions ([arXiv 2412.05139](https://arxiv.org/abs/2412.05139)), and code-like text is harder still ([arXiv 2310.05103](https://ar5iv.labs.arxiv.org/html/2310.05103)). A skill is short, templated markdown with code blocks.
- **Some words became much more common after ChatGPT.** We use the published excess-vocabulary list from [Kobak et al.](https://arxiv.org/abs/2406.07016v5) as a fixed word list. It is decided in advance, so it is not tuned on our labels.
- **Near-duplicate search can be done at scale** with MinHash and locality-sensitive hashing ([datasketch](https://ekzhu.com/datasketch/lsh.html)). It is approximate, so we treat its matches as candidates.

## 3. The data

We use the same public **GitSkills** dataset (Hugging Face: `mvaccargiu/gitskills`, snapshot ending 2026-07-20).

| What | Count |
|---|---:|
| `SKILL.md` files | 3,797,117 |
| Repositories | 282,200 |
| **Distinct skills** (unit of analysis) | **1,877,981** |
| Readable distinct skills (200+ body characters) | 1,840,872 |
| Distinct skills with commit history | 458,137 (24.4%) |

The Sprint 1 report gave 458,548 skills with history. Our local copy of the data has 458,137, and all Sprint 2 tests are anchored to the local count. The difference is recorded in [THREATS_TO_VALIDITY.md](../THREATS_TO_VALIDITY.md).

**New data source.** We mined the git history of 31 public skill collections (registries), such as the official Anthropic skills repository and large community collections. 6,719 of their skill versions appear unchanged in 216,440 dataset files. This gives us a set of skills whose source we know.

## 4. Method

### 4.1 Signals

Each signal is measured on its own before any label is decided.

| Signal | What it shows | How we measure it |
|---|---|---|
| Exact copies | Reuse | Number of files and owners that share the same text |
| Registry match | Shared content with a registry version (exact); similarity candidates (near). Copying direction not established | Exact or near (similarity 0.8+) match to a registry version |
| Near-duplicate cluster | A family of similar texts | MinHash (128 numbers, 5-word pieces) grouped at similarity 0.8 |
| Scaffold leftovers | Built from a template generator | Unfilled `TODO` placeholders and empty example folders |
| AI co-author line or agent bot | Disclosed agent involvement | First and last commit messages and author accounts |
| Bulk batch | Skills added together (install, import, generation or mixed work) | Number of skills added to a repository at the same time |
| Revision | Repeated changes over time; does not identify who made them | 3 or more commits over 7 or more days |
| Repository context | Background only | Agent folder, repository age and size |
| Wording and specificity | Weak clue | Readability, tone, excess-vocabulary words, rare identifiers |

Wording, specificity, skill type and scaffold signals are measured but **not used by the current label**. This lets us study them without moving the labels.

### 4.2 Labels

The labels are built in three steps. Each version is kept so changes can be compared.

| Version | What it adds |
|---|---|
| **v1** | The Sprint 1 notebook rules, ported exactly. A test checks that the counts match. |
| **v2** | Templated now also covers registry copies and near-duplicate clusters of 10 or more. Every label gets a confidence (high, medium or low) and reason codes. |
| **v2.1** (current) | A disclosed agent inside a bulk install becomes agent-assisted with low confidence, instead of insufficient. Likely human is medium confidence only if the skill was revised; otherwise low. A skill that is both templated and disclosed stays templated, is flagged as a conflict, and drops one confidence level. |

The templated reasons are checked in order of strength: exact registry copy, near registry copy, cluster of 10+, then 10+ identical copies.

Because reuse and agent use can both be true, we also report an **evidence view** that shows them side by side instead of forcing one label.

### 4.3 Baselines

The labels must beat simpler explanations. We compare them with:

| Baseline | Rule |
|---|---|
| B0 | Everything is insufficient evidence |
| B1 | Templated if 10+ identical copies |
| B2 | Agent-assisted if the skill sits in an agent folder |
| B3 | Agent-assisted if the first commit has an AI line |
| B3b | B3, but not for bulk batches of 6+ |

### 4.4 Validation design

- **Known copies.** Check how many of the 6,481 exact registry copies (excluding the registries' own files) the templated rules find.
- **People.** Draw 120 skills, about 30 per v2.1 label, at most a few per repository. Readers see the text, repository facts and commit messages, but not the pipeline's label. They follow the [annotation guidelines](../docs/ANNOTATION_GUIDELINES.md). Reader A labelled all 120, B labelled a 40-skill overlap, and C labelled 60.
- **Agreement.** Cohen's kappa between readers, and the share of each pipeline label a reader agrees with, with Wilson 95% intervals.
- **Sensitivity.** Change one cutoff at a time on 200,000 skills and see how label shares move.
- **Edit robustness.** Edit 500 registry skills by replacing or deleting lines, and check whether they still match their original.
- **Disclosure coverage.** In registry histories, check how many AI lines sit only in middle commits that our first/last view cannot see.

## 5. Implementation

The notebooks became a Python package, `skillsig`, run with:

```bash
python -m skillsig <stage>
```

| Stage | Does |
|---|---|
| Prepare and s0 Intake | Builds summary tables, checks the dataset, draws a 50,000-skill dev sample |
| s1 Registry | Clones the 31 collections, lists every skill version, matches them to the dataset |
| s2 Features | Measures each skill: commits, repository, type, wording, specificity, near-duplicates, scaffold |
| s3 Labels | Applies v1, v2 and v2.1, the baselines and the text models |
| s4 Validation | Checks the labels against known copies and people |
| s6 Report | Draws the report charts from saved results (`python -m skillsig export`) |

Design choices that make the results reproducible:

- **One config file.** Every cutoff (batch size 6, copy minimum 10, similarity 0.8, revision 3 commits over 7 days) lives in `config/default.yaml`. Unknown or missing keys fail loudly.
- **Manifests.** Every output has a `.manifest.json` that records its input fingerprints, the config hash, the git commit and the row count. Unchanged steps are skipped on rerun.
- **Fixed seed.** Every sample and model uses seed `20261015`.
- **DuckDB over Parquet.** The full dataset runs on a laptop with no database server.
- **Tests.** 437 automated tests cover parsing, features, rules, validation and the regression anchors from Sprint 1.

Setup and commands are in the [pipeline runbook](../docs/PIPELINE_RUNBOOK.md). The reasons behind each step are in the [stage design notes](../docs/stages/README.md).

## 6. Preliminary results

### 6.1 Labels moved a lot from v1 to v2.1

| Label | Sprint 1 (v1) | Sprint 2 (v2.1) | Share |
|---|---:|---:|---:|
| Highly templated | 35,768 | 303,517 | 16.2% |
| Agent-assisted | 80,114 | 142,667 | 7.6% |
| Likely human | 62,798 | 60,930 | 3.2% |
| Insufficient evidence | 1,699,301 | 1,370,867 | 73.0% |

v2 moved about 268,000 skills into templated, mostly through near-duplicate clusters. v2.1 moved about 65,000 disclosed bulk-install skills into agent-assisted.

![v2 moved 268,000 skills into templated; v2.1 moved 65,000 into agent-assisted](../figures/report/a_label_versions.png)

The templated reasons read only the text, so they also reach skills with no history. 236,495 of the templated skills have no commit history.

### 6.2 Reuse and agent use overlap

Of the 157,774 skills with a disclosed agent, **57,027 (36.1%) also show reuse**. A single label hides this, so the evidence view shows both.

![Reuse and disclosed agent involvement can coexist](../figures/report/f0_evidence_dimensions.png)

### 6.3 The templated rules find known copies

On 6,481 exact registry copies:

| Rule | Found | Share |
|---|---:|---:|
| 10+ identical copies (B1) | 3,661 | 56.5% |
| Near-duplicate cluster | 4,316 | 66.6% |
| Either | 5,649 | 87.2% |

This measures how many known copies the rules find, not how often a templated label is right. Registry copies are only positives, so this check cannot count false alarms.

![Exact registry recovery](../figures/report/f1_template_provenance.png)

Near-duplicate matching survives small edits but not large ones. When 5% of a skill's lines are replaced, 77.6% of edited copies still match their original. At 10%, only 16.4% do.

### 6.4 People agree with agent labels, not with human labels

Reader A's agreement with each v2.1 label on the 120-skill sample:

| v2.1 label | A agrees | Share | Wilson 95% |
|---|---:|---:|---|
| Agent-assisted | 25 / 30 | 83% | 66–93% |
| Insufficient evidence | 15 / 29 | 52% | 34–69% |
| Likely human | 6 / 30 | 20% | 10–37% |
| Highly templated | 1 / 31 | 3% | 1–16% |

The baselines on the same sample:

| Baseline agent label | A agrees | Share |
|---|---:|---:|
| B3 (any first-commit AI line) | 23 / 28 | 82% |
| B3b (AI line, no bulk) | 11 / 16 | 69% |
| B2 (agent folder) | 32 / 57 | 56% |

![Agreement with A for v2.1 and baselines](../figures/report/f2_rules_vs_baselines.png)

What this shows:

- **Agent labels hold up.** C also agreed with 20 of 21. Against the simple trailer baseline the estimates are similar (25/30 against 23/28, on different subsets of the sample); the evaluation establishes neither superiority nor equivalence.
- **Likely human is not validated.** A labelled 11 of the 30 likely-human skills as agent-assisted.
- **Templated looks wrong to readers, but the sheet cannot judge it.** The blind sheet shows one file, not its copies or its source. A reader cannot see that 500 other files share the text. A separate paired review, which shows both texts side by side, has been built but not yet completed.

### 6.5 Readers often disagree with each other

| Readers | Shared skills | Agree | Raw agreement | Cohen's kappa |
|---|---:|---:|---:|---:|
| A and B | 40 | 21 | 52.5% | 0.29 |
| A and C | 60 | 33 | 55.0% | 0.31 |
| B and C | 21 | 14 | 66.7% | 0.47 |

Readers mostly agree on agent-assisted skills. They split on likely human and insufficient evidence. In several cases one reader judged the evidence missing while another read the same text as project-specific human work. Because the readers themselves vary, every agreement number above has a wide margin. See the [annotation review](../docs/report/ANNOTATION_REVIEW.md).

### 6.6 Text alone gives only a moderate signal

We trained text models on 20,000 disclosed-agent and 20,000 likely-human skills. Folds keep each repository owner and detected near-duplicate family on one side, which reduces leakage through them, though it does not remove every route.

| Method | AUC |
|---|---:|
| Fixed AI-phrase list | 0.52 |
| Length only | 0.53 |
| Feature model (logistic regression) | 0.60 |
| Ensemble (feature model + word and character TF-IDF) | **0.71** |

An AUC of 0.5 is guessing. 0.71 shows some signal, so "no separable signal" is not supported. But at a nominal 90% precision against these proxy labels, under a 50/50 class prior, the best model decides only 0.9% of disclosure-positive and 7.1% of likely-human proxies, so it cannot label most skills. Performance on true authorship remains unestablished.

![Text-only methods reach AUC 0.71](../figures/report/f4_text_methods_auc.png)

These are proxy labels, not verified authors. The "human" group can include undisclosed agent use.

### 6.7 Cutoffs move label shares a little

Changing one cutoff at a time on 200,000 skills, the largest change in any label share is **+3.3 percentage points** (templated, when the cluster similarity drops to 0.6). The copy-count cutoff moves the templated share by about +1.1 or −0.7 points.

![Only the cluster and copy cutoffs move label shares](../figures/report/f5_stability.png)

This is about totals. We have not yet checked how many individual skills change label.

### 6.8 Our commit view misses some disclosures

In registry histories, 92% of skills with an AI line show it on the first or last commit, which is all GitSkills records. For skills with 3 or more commits, this drops to 83%. The rest are only in middle commits.

![The labels see 92% of AI disclosures](../figures/report/f2_disclosure_recall.png)

## 7. What this means for the research question

| Part of the question | Where we stand after Sprint 2 |
|---|---|
| Can we spot **highly templated** skills? | **We can find reuse.** The rules find 87.2% of known registry copies. Whether a reader would call them templated is still being tested with the paired review. |
| Can we spot **agent-assisted** skills? | **Yes, when disclosed.** Readers agree with 83% of agent labels. The simple trailer rule does about as well. |
| Can we spot **human-written** skills? | **Not reliably.** Readers agree with only 20% of likely-human labels. |
| How reliable is the signal? | **Partly measured.** Agent labels are fairly reliable. Readers disagree often (kappa about 0.3), so the reference itself is uncertain. |

Commit and copy evidence carry most of the signal. Wording adds a moderate amount but cannot label most skills by itself.

## 8. Limits and open questions

- We still cannot see who wrote a skill. All labels are clues.
- Readers disagree, so their answers are not ground truth. The 120-skill sample gives wide intervals.
- The blind sheet cannot show copies, so it cannot validate the templated label.
- Registry copies are only positives, so we cannot count false templated labels from them.
- GitSkills records only the first and last commit. Some AI lines are in middle commits.
- A co-author line may describe an install or later maintenance, not the original writing.
- Repository creation date, one of our planned simpler explanations, has not been tested as a baseline yet.
- Only label totals were checked under changed cutoffs, not individual skills.

The full list is in [THREATS_TO_VALIDITY.md](../THREATS_TO_VALIDITY.md).

## 9. Next steps (Sprint 3)

1. **Finish the hand labels:** the trailer check (50 messages), the paired template review (120 pairs) and C's remaining 60 skills.
2. **Review disagreements** between readers and store any agreed answers separately from the original ones.
3. **More baselines and checks:** add repository creation date, run the ablation, compare subgroups, and count individual label changes under different cutoffs.
4. **Error analysis** with examples checked by hand.
5. **Complete the report draft** with all sections, and expand the threats to validity.
6. **Release:** export tables, test the README from a clean checkout, and tag a version.

## 10. Where to find things

| Item | Location |
|---|---|
| Full findings and charts | [docs/report/FINDINGS.md](../docs/report/FINDINGS.md) |
| Annotation review | [docs/report/ANNOTATION_REVIEW.md](../docs/report/ANNOTATION_REVIEW.md) |
| Template evaluation | [docs/report/TEMPLATE_EVALUATION.md](../docs/report/TEMPLATE_EVALUATION.md) |
| Pipeline code | `src/skillsig/` |
| Tests | `tests/` |
| How to run it | [docs/PIPELINE_RUNBOOK.md](../docs/PIPELINE_RUNBOOK.md) |
| Stage design notes | [docs/stages/](../docs/stages/README.md) |
| Settings | `config/default.yaml` |
| Result tables | `results/` |
| Charts | `figures/report/` |
| Sprint 2 progress and retrospective | [docs/sprints/sprint2/](../docs/sprints/sprint2/progress.md) |
