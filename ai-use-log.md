# AI Use Log

This log discloses how the team used AI tools on this project, as the course requires. Each entry says who used which tool, what it was used for, and how the output was checked.

The study is about AI involvement in `SKILL.md` files, so two rules apply throughout:

- **No AI labels.** No AI tool assigned a label to any skill in the hand-labelling sheets (spot-check, trailer check, template-pair review). All labels come from people.
- **Answer keys stay blind.** AI tools were not used to open or summarise the review answer keys (`key.parquet`, `trailer_key.parquet`, `template_review/key.parquet`).

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

*To complete:* list any modules written mainly by hand, and any AI-generated code that was rewritten after review.

### Sprint 2 documentation with Claude Code

**Tool:** Claude Code (Claude Opus 5.5)

**What it was used for**
- Comparing the Sprint 2 plan and the course specification with the repository, and listing the work still pending for Sprints 2 and 3.
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

`reports/GitSkills provenance sprint 2 plan.md` and the notes in `research_notes/GitSkills provenance sprint 2 plan/` collect web research on provenance signals, datasets and the backlog. *To complete:* say whether an AI research tool produced these, and how the cited sources were checked.

## How to add an entry

Add a new section under **Entries** for each piece of AI-assisted work, with:

1. **Tool**, with the model if known.
2. **What it was used for:** the files or tasks.
3. **How it was checked:** tests, reruns, comparison with results, or review by another team member.

Keep entries factual.
