# Sprint 1 Retrospective

## What went well
- We finalized a feasible research question(RQ #6: human/agent authorship signals).
- We built a reproducible, working pipeline: dataset acquisition script, four DuckDB SQL build scripts, and 9 topic-focused EDA notebooks covering integrity, duplication and near-duplicates, skill names, content structure, agent ecosystems, authorship, and repository/sibling profiles.
- Data integrity checks came back clean. There were 0 malformed deduplication groups and 0 representative rows missing content across the full 1,877,981-skill population.
- We got findings early: 50.5% file-level duplication, an additional 10.6% of distinct contents merge under near-duplicate detection, and a clear adoption curve tracking the agent-skill format's launch in October 2025.

## What was challenging
- The one source of direct authorship-adjacent signals is only available for a minority of skills, which limits how far commit-based classification alone can go.
- Several of our candidate signals turned out to be location not authorship. Location correlates with tool family, not necessarily authorship mode; our templated-category threshold, which is 10+ copies, could be re-measuring duplication. And adoption timing could explain class differences on its own. This information shaped how we scoped our project.
- No field in the dataset labels authorship directly.

## What we learned about the data/problem
- Commit metadata is not a clean authorship signal. An AI co-author trailer on a bulk install records who ran the install, not who wrote the skill. This became one of our 5 competing explanations, with an exclusion-cutoff baseline to test against.
- Highly templated skills are clearly different from the others: longer, more often have Examples and "When to use" sections, rarely edited, and live in large repositories.
- A real AI-authorship signal exists in the data in the form of explicit `Co-authored-by` trailers, dominated by Claude, but only covers skills where someone discloses it.
- Deduplication by content hash is clean and reliable, but a large amount of additional duplication (whitespace/formatting variants) is invisible to exact-hash matching and needs near-duplicate detection to be found.

## What changed in our back log/plan as a result
- The backlog started with stories to list basic queries on the dataset (counts, distributions, simple summaries).
- As the analysis progressed, we added a story for each analysis, which became the topic-focused EDA notebooks: integrity, copies, near-duplicates, names, content structure, agent ecosystems, authorship, repositories, and siblings.

## Action items for Sprint 2
- Compute wording features (command-style language, tone, typical AI phrasing) for all skills, including the ones without commit history.
- Hand-label a sample of about 200 to 300 skills and report how often each labelling rule is right.
- Test how the labels change when the cutoffs (6 skills in a batch, 10 copies) move.
- Run near-duplicate detection on the full population.
