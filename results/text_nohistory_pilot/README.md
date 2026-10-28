# Text analysis of skills with no commit history (pilot, dev sample)

Run 2026-10-25. Compare against `results/base_before_text/` (the frozen commit-only labels).
Pilot only: wording features (`s2_wording`) exist for the dev sample, so this covers 30,952 of the 1,186,459 no-history `insufficient_evidence` skills. Card T52 is the full run.

## Method
- **Train** the T42 text-only PU model (logistic regression on structure + wording + marker-word rate) on all 10,129 dev skills **with** history that are not templated. Positives (`s = 1`) = 3,740 skills with a disclosed AI trailer or agent bot (`flag_disclosed_agent`); the rest are unlabelled, not human.
- **Score** the 30,952 dev skills **without** history whose v2.1 label is `insufficient_evidence`. None of them is in training, so no leakage.
- `style_score = min(1, P(s=1 | text) / c)`, with `c = 0.391` (mean of the T42 out-of-fold `c`). Bands use the T42 cutoffs: `lean_agent` if score >= 0.779, `lean_human` if <= 0.592, else `undecided`.
- Skill type is the T46 v1 dry run; the agent folder comes from `generated_data/file_agents.parquet`.
- Per-skill scores: `src/data/derived/text_nohistory_pilot/nohist_scores.parquet`.

## Findings
1. **No-history skills score like history skills without a trailer** (`score_by_group.csv`): median 0.898 vs 0.899; lean_agent 69.9% vs 68.4%; disclosed-trailer skills 80.0%. The text does not single out the no-history population.
2. **The bands mostly measure length** (`nohist_by_length.csv`, `model_weights.csv`). `body_words` has the largest weight (+0.66). Under 150 words: 21% lean_agent, 48% lean_human. Over 1,000 words: 92% lean_agent, 0.5% lean_human.
3. **But the model is more than length.** On the history skills, length alone gives AUC 0.561; the text model 0.595; and within each length quartile the text model still gives 0.57-0.59. So there is a weak style signal on top of length; the absolute cutoffs hide it.
4. **Claude Code folders lean least toward agent** (`nohist_by_agent_folder.csv`): skills under `.claude/` are 47% lean_agent and 35% lean_human, the lowest agent share of any folder (Codex 56%, no folder 71%). If the model captured "written with Claude", this would go the other way. It suggests the signal is about long, generic, collection-style skills rather than Claude-specific writing.
5. **By skill type** (`nohist_by_skill_type.csv`): review_git (79%) and planning_agent_workflow (75%) lean most to agent; integrations_tools (64%) and backend (65%) least. Consistent with T42's marker words (gate, backlog, spawn, verdict), which are workflow topic words.
6. Copies barely matter (1 copy 70%, 4-9 copies 66%).

## What this supports in the paper
- "For skills with no commit history, text is the only evidence. A text model trained on disclosed-agent skills (AUC 0.60) rates no-history skills the same way as history skills without a trailer; its scores are driven mostly by length and workflow vocabulary, and Claude Code skills score lowest. We therefore do not assign authorship to no-history skills from text."
- Report shares by group, never a per-skill "agent" label.

## Do not
- Do not add `lean_agent` to individual no-history skills with these cutoffs: 70% would be flagged, mostly because they are long.
- Do not compare these numbers with labels rebuilt after the base freeze.
