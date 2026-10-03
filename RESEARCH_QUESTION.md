# Research Question

## Question 
Can we distinguish likely human-authored, agent-assisted, and highly templated skills by observing structural, linguistic, and commit-metadata patterns in GitSkills artifacts, and how reliable is that signal?

## Background/Motivation
As AI coding assistants and agent frameworks become more common, an increasing share of software artifacts, including skills, are being written, co-written, or templated with AI assistance. Understanding whether the different modes of authorship leave observable traces matters for trust and quality assessment in the growing AI-native tooling ecosystem. This project investigates whether such traces exist and how reliably we can detect them using observable repository data.

The question matters for skills in particular. A skill is natural-language instructions that an agent loads and follows, and 36.3% bundle extra files (11.4% ship scripts the agent may run). Half of all collected files (50.5%) are verbatim copies, and most distinct skills sit in small, unstarred repositories created after the format launched in October 2025. Readers and tools therefore have little to go on when deciding whether to trust a skill. Knowing whether a skill was written by a person, drafted by an agent, or copied from a template is one input to that decision, and it is unclear whether the available signals carry it.

## Taxonomy
We classify GitSkills artifacts into three categories:
1. Human-Authored: Written primarily by a person with little to no AI assistance.
2. Agent-Assisted: Co-produced with an AI tool, with human revision/editing.
3. Highly Templated: Follows a standard scaffolding with little to no customization.

These categories represent the best available proxy given observable signals (see DATA_DICTIONARY.md and THREATS_TO_VALIDITY.md).

## Unit of Analysis
An individual GitSkill artifact -- one SKILL.md file, removing duplicated copies through content hash.

## Population and Sample
The GitSkills dataset (Hugging Face: `mvaccargiu/gitskills`) has 3,797,117 SKILL.md files across 282,200 repositories, but once duplicate content is removed, we have 1,877,981 distinct contents. We work from a sample for development and reproducibility.

## Variables
Features (independent variables):
- Structural: heading/pattern, section consistency, document length
- Linguistic: imperative-language density
- Commit metadata: single-commit vs. iterative editing, commit message patterns
- Repository context: repository size, contributor count, tool-family indicators

## Outcome (dependent variable)
- Heuristic authorship-signal label produced by our classification method -- not a 100% verifiable truth.

## Competing and Simpler Explanations
The label may track something other than authorship. Each explanation below makes a prediction that the analysis can check, and the full method has to beat the simple baseline to count as evidence of an authorship signal.

1. **Location and tool family.** Agent-assisted skills sit under `.claude/` far more often than likely-human skills (72% vs 43%), and inside canonical agent folders the two classes match on length, code blocks, Examples and "When to use" sections. If so, "agent-assisted" only means "uses Claude Code". Baseline: agent folder alone.
2. **Duplication.** Highly templated is defined by 10 or more copies, so it may be a copy count and nothing more. If so, copy count or repository size alone reproduces the class. Baseline: copy count alone.
3. **Commit workflow, not writing.** An AI trailer on a bulk install records who ran the install. If so, most trailer-based agent-assisted labels are distribution events, and a missing trailer says nothing about human authorship. Baseline: trailer alone, with and without the bulk-install exclusion.
4. **Adoption timing.** Most skills date from October 2025 or later, and likely-human skills are more often in older repositories. If so, the classes reflect when and why a repository adopted skills. Baseline: repository creation date alone.
5. **No separable signal.** Likely-human and agent-assisted skills may be indistinguishable on text and structure, so only the commit trailer separates them. This is a possible result, and reporting it is a valid answer to the question.

## Expected Contribution
1. A documented, reproducible rule set that labels skills as likely human, agent-assisted, highly templated, or insufficient evidence, with every rule traceable to a column in DATA_DICTIONARY.md.
2. A reliability estimate for that method: per-rule precision against a hand-labelled sample, sensitivity to the bulk-install cutoff (6) and copy threshold (10), and comparison with the simple baselines above.
3. A record of which signals do not separate the classes (for example length, front-matter validity, commit count), so later work does not repeat them.
4. An account of where and why the signal is unreliable, in THREATS_TO_VALIDITY.md.

This is a starting point for future work on trust in AI-generated development artifacts, not a verified authorship classifier.

