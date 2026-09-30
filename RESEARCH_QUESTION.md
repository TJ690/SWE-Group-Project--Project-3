# Research Question

## Question 
Can we distinguish likely human-authored, agent-assisted, and highly templated skills by observing structural, linguistic, and commit-metadata patterns in GitSkills artifacts, and how reliable is that signal?

## Background/Motivation
As AI coding assistants and agent frameworks become more common, an increasing share of software artifacts, including skills, are being written, co-written, or templated with AI assistance. Understanding whether the different modes of authorship leave observable traces matters for trust and quality assessment in the growing AI-native tooling ecosystem. This project investigates whether such traces exist and how reliably we can detect them using observable repository data.

## Taxonomy
We classify GitSkills artifacts into three categories:
1. Human-Authored: Written primarily by a person with little to no AI assistance.
2. Agent-Assisted: Co-produced with an AI tool, with human revision/editing.
3. Highly Templated: Follows a standard scaffolding with little to no customization.

These categories represent the best available proxy given observable signals (see DATA_DICTIONARY.md and THREATS_TO_VALIDITY.md).

## Unit of Analysis
An individual GitSkill artifact ('dedup_primary = 1' in the 'artifacts' table). One row per unique file content (by 'file_sha'), not per file occurrence. We analyze representative skills instead of every copy, since copies share the same content and don't offer additional authorship signal beyond what the representative already captures.

## Population and Sample
The GitSkills dataset (Hugging Face: `mvaccargiu/gitskills`) has 3,797,117 SKILL.md files across 282,200 repositories, but once duplicate content is removed, we have 1,877,981 distinct contents. We work from a sample for development and reproducibility.

- Full population: 1,877,981 distinct skills.
- Readable population (used for content/structure analysis): 1,840,872 skills, excluding symlink stubs, one saved redirect page, and bodies under 200 characters.
- Commit-history-eligible subset: 458,137 skills (24.4% of distinct skills)

## Key Known Constraints (from EDA)
Only 24.4% of distinct skills have any commit metadata. For the remaining ~76%, we can only draw on content/structural features instead of commit-based signals. Our classification method must work in two ways: a higher-confidence tier for skills with history, and a lower-confidence tier for the rest.

~28.8% of history-eligible skills arrive via bulk commits, which means multiple skills added in a single commit, typically from aggregator/installer repositories rather than individual authorship activity. These bulk-added skills' commit metadata reflects the committer, often the installer script or the person running the sync, not the original author. We exclude or separately flag bulk-commit skills when using commit-based signals as an authorship proxy.

## Variables
Features (independent variables):
- Structural: heading/pattern, section consistency, document length
- Linguistic: imperative-language density
- Commit metadata: single-commit vs. iterative editing, commit message patterns
- Repository context: repository size, contributor count, tool-family indicators

**Commit-based (24.4% of skills)**
- first_commit_author_type, last_commit_author_type (User/Bot/Organization)\
- Presence of an AI co-author trailer
- commit_count
- bulk-commit - multiple skills sharing one 'repo_full_name' + 'first_commit_at'

**Content-based (all readable skills)**
- Body length
- Structural completeness
- Front-matter completeness/validity
- Bundled-file presence and type
- File location class

## Outcome (dependent variable)
A three-way heuristic label per skill: AI-trailer confirmed, content suggests templates, or insufficient signal.

## Expected Contribution
A documented, reproducible heuristic method for finding likely 
authorship patterns in AI-native software artifacts, along with accounting for where and why that signal is unreliable. This is useful 
as a starting point for future work on trust in AI-generated development artifacts.

