# Research Question

## Question 
Can we distinguish likely human-authored, agent-assisted, and highly templated skills by observing structural, linguistic, and commit-metadata patterns in GitSkills artifacts, and how reliable is that signal?

## Background/Motivation
As AI coding assistants and agent frameworks become more common, an increasing share of software artifacts, including skills, are being written, co-written, or templated with AI assistance. Understanding whether the different modes of authorship leave observable traces matters for trust and quality assessment in the growing AI-native tooling ecosystem. This project investigates whether such traces exist and how reliably they can be detected using observable repository data.

## Taxonomy
We classify GitSkills artifacts into three categories:
1. Human-Authored: Written primarily by a person with little to no AI assistance.
2. Agent-Assisted: Co-produced with an AI tool, with human revision/editing.
3. Highly Templated: Follows a standard scaffolding with little to no customization.

These categories represent the best available proxy given observable signals (see DATA_DICTIONARY.md and THREATS_TO_VALIDITY.md).

## Unit of Analysis
An individual GitSkill artifact -- one SKILL.md file, removing duplicated copies through content hash.

## Population and Sample
The GitSkills dataset (Hugging Face: `mvaccargiu/gitskills`) has 3,797,117 SKILL.md files across 282,200 repositories, deduplicated to 1,877,981 distinct contents. We work from a sample for development and reproducibility.

## Variables
Features (independent variables):
- Structural: heading/patterm, section consistency, document length
- Linguistic: imperative-language density
- Commit metadata: single-commit vs. iterative-edit flag, commit message patterns
- Repository context: repository size, contributor count, tool-family indicators

## Outcome (dependent variable)
- Heuristic authorship-signal label produced by our classification method -- not a ground truth label.

## Expected Contribution
A documented, reproducible heuristic method for surfacing likely 
authorship patterns in AI-native software artifacts, along with an 
honest accounting of where and why that signal is unreliable. This is useful 
as a starting point for future work on trust in AI-generated development artifacts, without claiming unfounded certainty.

