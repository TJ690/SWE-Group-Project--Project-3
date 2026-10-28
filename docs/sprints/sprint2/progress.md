# Sprint 2 Progress

This document records work completed in Sprint 2 and the next steps for Sprint 3. See the [Sprint 2 retrospective](retrospective.md) for lessons learned and changes to the plan. The full results are in [FINDINGS.md](../../report/FINDINGS.md).

## Completed Work

- Moved the Sprint 1 notebook logic into the `skillsig` Python package, run with `python -m skillsig <stage>`. The stages are intake, registry, features, labels, validation and report figures. Each output gets a manifest file that records its inputs and settings. See the [pipeline runbook](../../PIPELINE_RUNBOOK.md) and the [stage design notes](../../stages/README.md).
- Ported the Sprint 1 labels as v1 and added a regression test, so the package reproduces the notebook counts exactly.
- Mined the history of 31 public skill collections. 6,719 of their versions appear unchanged in 216,440 dataset files.
- Computed features for every distinct skill: commit metadata (with more AI tool and bot patterns), repository context, skill type, wording, specificity and scaffold leftovers.
- Ran near-duplicate detection (MinHash) on all 1,840,872 readable skills. 15.1% sit in a cluster of 10 or more at similarity 0.8.
- Built label rules v2 and v2.1, which record a reason and a confidence for each label.
- Compared the labels with simple baselines (majority class, copy count, agent folder, trailer, trailer without bulk installs) and tested text-only models.
- Wrote [annotation guidelines](../../ANNOTATION_GUIDELINES.md) and drew a blind 120-skill sample. Three people labelled it: A all 120, B a 40-skill overlap, and C 60.
- Checked how much the label shares move when one cutoff changes at a time, and how well near-duplicate detection survives small edits.
- Generated 18 report figures from saved stage outputs with `python -m skillsig export`.
- Grew the test suite to 437 passing tests and updated the [data dictionary](../../../DATA_DICTIONARY.md), [research question](../../../RESEARCH_QUESTION.md) and [threats to validity](../../../THREATS_TO_VALIDITY.md).

## Label Counts After Sprint 2

The current rules (v2.1) assign one label to each distinct skill content:

| Label | Sprint 1 (v1) | Sprint 2 (v2.1) | Share |
|---|---:|---:|---:|
| Highly templated | 35,768 | 303,517 | 16.2% |
| Agent-assisted | 80,114 | 142,667 | 7.6% |
| Likely human | 62,798 | 60,930 | 3.2% |
| Insufficient evidence | 1,699,301 | 1,370,867 | 73.0% |

Most of the change comes from two places. Near-duplicate clusters and registry matches moved about 268,000 skills into highly templated. Disclosed agent trailers on bulk commits moved about 65,000 into agent-assisted. These labels are still clues, not confirmed authorship.

## Preliminary Results

- **Agent labels agree well with readers.** Reader A agreed with 25 of 30 agent-assisted labels (83%), and C agreed with 20 of 21.
- **Likely-human labels do not.** A agreed with 6 of 30. On the shared 40 skills, A agreed with 2 of 10 and B with 6 of 10. The label is not validated.
- **Readers disagree with each other.** A and B agreed on 21 of 40 skills (Cohen's kappa 0.29). A and C agreed on 33 of 60 (kappa 0.31). See the [annotation review](../../report/ANNOTATION_REVIEW.md).
- **Copy rules recover known registry copies.** Copy count or cluster membership finds 87.2% of 6,481 exact registry copies. Copy count alone finds 56.5%.
- **Text alone separates the groups only moderately.** The best text model reaches AUC 0.71, compared with 0.52 for a phrase list. At a nominal 90% precision against proxy labels (50/50 prior) it decides fewer than 10% of skills.
- **Cutoffs matter a little.** The largest change in any label share is +3.3 percentage points, when the cluster similarity drops to 0.6.
- **Reuse and agent disclosure overlap.** 57,027 of the 157,774 skills with a disclosed agent (36.1%) also show reuse. The report now shows reuse and disclosure as two separate views instead of forcing one label.

## Planned Work for Sprint 3

- Finish the trailer check (50 messages), the template-pair review (120 pairs) and C's remaining 60 skills.
- Review the reader disagreements and record any agreed decisions in a separate file.
- Add repository creation date as a baseline, and run the ablation and a subgroup comparison.
- Write up the error analysis with examples checked by hand.
- Finish the export of result tables and a full-run smoke test.
- Write the complete report draft with citations, and expand the threats to validity to cover all six areas the course requires.
- Tag a release, test the README from a clean checkout, and prepare the demo.
