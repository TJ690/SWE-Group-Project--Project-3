# Sprint 1 Progress

This document records work completed in Sprint 1 and the next steps for Sprint 2. See the [Sprint 1 retrospective](retrospective.md) for lessons learned and changes to the plan.

## Completed Work

- Finalized the [research question](../../../RESEARCH_QUESTION.md).
- Set up the dataset download script and four DuckDB scripts that build the generated data files.
- Created nine exploratory notebooks covering data integrity, exact and near duplicates, skill names, content features, agent folders, commit history, repositories, and bundled files.
- Applied label rules using copy counts and commit history.
- Prepared an initial [research report](../../../report/sprint1_report.md) with figures and documented the [limits of the study](../../../THREATS_TO_VALIDITY.md).

## Initial Label Counts

The rules assign one label to each distinct skill content:

| Label | Distinct skills | Share |
|---|---:|---:|
| Likely human | 62,798 | 3.3% |
| Agent-assisted | 80,114 | 4.3% |
| Highly templated | 35,768 | 1.9% |
| Insufficient evidence | 1,699,301 | 90.5% |

These labels are clues, not confirmed authorship. Only 24.4% of distinct skills have collected commit history. Copy counts can still support a highly templated label without history.

## Planned Work for Sprint 2

- Compute wording features (command-style language, tone, typical AI phrasing) for all skills, including the ones without commit history.
- Hand-label a sample of about 200 to 300 skills and report how often each labelling rule is right.
- Test how the labels change when the cutoffs (6 skills in a batch, 10 copies) move.
- Run near-duplicate detection on the full population.
