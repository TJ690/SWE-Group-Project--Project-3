# Research Question

## Question 
Can we distinguish likely human-authored, agent-assisted, and highly templated skills by observing structural, linguistic, and commit-metadata patterns in GitSkills artifacts, and how reliable is that signal?

## Background/Motivation
We chose this question because skills place a software engineering concern directly in an agent's workflow: how to understand the origin of instructions that software follows. A skill can guide how an agent approaches a task, applies a development practice, or uses a tool. Understanding how those instructions were produced is therefore relevant to the developers who adopt, review, and maintain them.

Skills may be written by developers, drafted with agent assistance, or adapted from reusable templates. These processes raise different review questions. A reviewer may want to understand the intent behind a manually written instruction, check whether an agent-assisted draft fits the project, or determine whether a template has been meaningfully customized. Knowing the production context could help focus that review and trace assumptions inherited from another source. Each process can produce useful or flawed instructions, so authorship is context for evaluation rather than a quality judgment.

The difficulty is that a finished skill does not necessarily reveal how it was created. Similar wording could come from a shared template, common writing conventions, or agent assistance. Human revision can also obscure the distinction between an initial draft and the final artifact. This creates an open question about whether visible characteristics of a skill and its repository history can provide meaningful evidence of its production process, and how much confidence that evidence deserves.

This uncertainty motivated our research question. Establishing whether these distinctions can be made reliably would help developers interpret authorship claims and make more informed decisions about reviewing and reusing skills. Establishing where the distinctions cannot be supported would be equally valuable, because it would clarify the limits of inferring authorship from public software artifacts.

## Historical taxonomy and independent evidence
The original classification used three categories:
1. Human-Authored: Written primarily by a person with little to no AI assistance.
2. Agent-Assisted: Co-produced with an AI tool, with human revision/editing.
3. Highly Templated: Follows a standard scaffolding with little to no customization.

These categories represent the best available proxy given observable signals (see DATA_DICTIONARY.md and THREATS_TO_VALIDITY.md).

The primary evidence report now separates two dimensions because copied content and
agent involvement can coexist:

- **Reuse evidence:** exact content sharing, registry near-matches or near-duplicate
  candidates, scaffold signals, no signal observed, or reuse not assessed. The output
  retains overlapping flags and does not establish copying direction.
- **Agent-involvement evidence:** disclosed, no disclosure observed in the available
  commit view, or history unavailable. Missing disclosure does not establish human
  authorship; disclosure can concern installation or maintenance.

Historical labels and independent annotations remain available for comparisons. They
are not rewritten into a new reference set. The independent report assigns no human
verdict or confidence penalty when reuse and disclosure coexist. See
[the evidence definitions](docs/stages/s3_labeling/evidence.md).

## Unit of Analysis
An individual GitSkill artifact -- one SKILL.md file, removing duplicated copies through content hash.

## Population and Sample
The GitSkills dataset (Hugging Face: `mvaccargiu/gitskills`) has 3,797,117 SKILL.md files across 282,200 repositories, but once duplicate content is removed, we have 1,877,981 distinct contents. The exploratory analysis uses all distinct contents. Only the near-duplicate similarity check (TF-IDF) uses a 20,000-skill sample, because the full comparison does not fit in memory.

## Variables
Features (independent variables):
- Structural: heading/pattern, section consistency, document length
- Linguistic: imperative-language density
- Commit metadata: single-commit vs. iterative editing, commit message patterns
- Repository context: repository size, age, stars, tool-family indicators (the dataset has no contributor count)

## Outcome (dependent variable)
- Independent reuse and disclosed-involvement evidence, with coverage and overlap.
  Historical heuristic labels remain comparison outcomes, not verified authorship.

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

