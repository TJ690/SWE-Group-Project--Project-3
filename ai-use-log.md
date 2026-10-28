# AI Use Log

This log discloses how the team used AI tools on this project, as the course requires. Each entry records the tool, its use, and the available verification. Historical records do not identify the initiating team member for every AI session; a commit author alone does not establish who operated the tool.

The study is about AI involvement in `SKILL.md` files, so two rules apply throughout:

- **No AI labels.** No AI tool assigned a label to any skill in the hand-labelling sheets (spot-check, trailer check, template-pair review). All labels come from people.
- **Answer keys stay separate from annotation.** Automated sampling and scoring code uses private answer keys to join submitted answers to pipeline predictions. Keys must not be shown to annotators during independent review or used to generate their answers. This log does not claim that automated tools never accessed a key.

## Entries

### Pipeline implementation with Cursor

**Tool:** Cursor agent

**What it was used for**
- Implementing the `skillsig` package from the task cards T00–T32 in `docs/CODING_PLAN.md`. The local Cursor setup holds project rules (labels are heuristics, the exact label names, no personal names in results, testing and data conventions), task cards and run commands.
- Writing the stage design notes in `docs/stages/` and keeping `docs/EXECUTION_STEPS.md` up to date.

**How it was checked**
- The Sprint 1 notebook counts are fixed regression anchors (35,768 / 80,114 / 62,798 / 1,699,301), and the v1 label port is tested against them.
- 437 automated tests run with `python -m pytest -q`.
- Every output has a manifest recording its inputs, settings and git commit.

**Attribution limit:** The available record does not establish a complete module-by-module split between handwritten and AI-generated code, or every human rewrite. Tests establish checked behavior, not authorship. No such split is claimed here.

### Sprint 2 documentation with Claude Code

**Tool:** Claude Code (Claude Opus 5.5)

**What it was used for**
- Drafting `docs/sprints/sprint2/progress.md` and `retrospective.md`. The team then edited the retrospective: the "What was challenging" points and the RQ2 explanation are the team's own wording.
- Drafting `report/sprint2_report.md` from the existing results tables, `docs/report/FINDINGS.md` and `docs/report/ANNOTATION_REVIEW.md`.
- Writing `SKILLS_RUNBOOK.md`, a guide to running the pipeline through the agent skills, and adding README links to the new documents.
- Drafting this log.

**How it was checked**
- Every number in the report and progress notes was read from the current `results/` files, not typed from memory. Two values that had changed during the sprint were updated to the latest results.
- All relative links in the new documents were checked to point at existing files.
- The related-work citations in the report come from the Sprint 2 planning notes and still need a final check before the final report.
- The team reviewed the drafts before committing them.

### Sprint 2 planning research

**Tool:** Claude Code

**What it was used for**
- The existing disclosure attributes the planning work to Claude Code. `reports/GitSkills provenance sprint 2 plan.md` and `research_notes/GitSkills provenance sprint 2 plan/` contain research notes on provenance signals, datasets and the backlog.

**How it was checked**
- The notes were compared with the local implementation and results during Sprint 2 reviews; planning estimates are not treated as measured findings.
- The available record does not establish which research or browsing tools produced each note, or that every external citation was independently checked. Related-work citations require source verification before being presented as verified evidence in the final report.

### Sprint 2 implementation and research review with OpenAI Codex

**Tool:** OpenAI Codex.

**Period:** Sprint 2 work recorded in this chat through 2026-10-28.

**What it was used for**
- Reviewing code, Markdown documentation, saved results and the course assignment against the Sprint 2 requirements.
- Implementing and testing independent-rater agreement and scoring support, preserving submitted annotations and recording their hashes.
- Implementing the separate reuse and disclosed-agent-involvement evidence report, without replacing the historical labels or human answers.
- Separating exact registry recovery from similarity-conditioned evaluation, and building the paired-text review package and its scoring code. Codex did not supply the human pair-review judgments.
- Revising conclusions, model interpretation notes and figure captions to distinguish proxy-label discrimination, visible disclosure, reuse evidence and unverified authorship.
- Updating the README team documentation and this AI-use disclosure on 2026-10-28.

**How it was checked**
- Tests exercised agreement calculations, same-item comparisons, validation of submitted IDs and labels, evidence dimensions, paired-review handling and preservation of existing answers.
- Original workbook hashes and canonical copies were checked against the recorded preservation hashes during the review.
- Revised research wording was compared with saved numerical outputs. Tests and interpretation edits do not verify true authorship or replace independent human review.
- The requirement audit did not execute a fresh end-to-end pipeline rebuild. That verification remains separate from the passing tests.

**Remaining disclosure limits:** This entry records work visible in the current chat. It does not establish complete AI-session history, independently verify earlier tool/model claims, or certify that all generated work has received a separate human review.

## How to add an entry

Add a new section under **Entries** for each piece of AI-assisted work, with:

1. **Tool**, with the model if known.
2. **What it was used for:** the files or tasks.
3. **How it was checked:** tests, reruns, comparison with results, or review by another team member.

Keep entries factual.
