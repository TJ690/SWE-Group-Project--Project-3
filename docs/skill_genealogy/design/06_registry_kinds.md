# Design: registry kinds (stage 3c)

Card G38 · Code: `label_registry_kinds.py` · [Code flow](../code_flow/06_registry_kinds.md) · [Report](../report/06_registry_kinds_report.md)

## Problem

The registry rule (stars and skill count) also selects collections that copy other people's skills. `majiayu000/claude-skill-registry` is a crawler mirror with 312,153 skill folders, half of all mined folders. If a copier is treated as a source, it becomes the origin of every skill it crawled first, and RQ1 and RQ3 measure the wrong thing. Each registry needs a label, `source` or `copier`, with a confidence a person can review.

## Design at a glance

Two votes. The **date vote**: for every `SKILL.md` text shared with registries of other owners, did another owner have it strictly earlier? The **README vote**: does the README say the skills are collected, or that they are the repo's own? The votes are combined into a kind and a rule-based confidence. Low-confidence registries go to a review sheet.

## Decisions

**Dates decide, because nobody can copy a text before it exists.** For each shared blob the step takes the first time each registry had it (from stage 3 history, not the dataset's mostly empty dates). `upstream_share` is the share of shared texts first seen in another owner's registry. Above 0.5 the vote is copier, below it source; the strength grows with distance from 0.5 and with the number of shared texts (full at 20).

**Owners, not repos.** Registries of one owner count as one. A sibling repo of the same author is not an upstream.

**A registry that shares nothing is a source.** Nothing in it came from another registry, so it gets a source vote of 0.7.

**Borrowing a few texts is not copying.** The share counts only shared texts, so a registry that wrote 600 skills and borrowed one shows "1 of 1 first seen elsewhere". The step also computes `copied_share` = texts first seen elsewhere / all its texts. With fewer than 20 shared texts and `copied_share` below 0.1, the vote is source. The 20-text limit keeps crawlers copiers: `majiayu000/claude-skill-registry` shares 49,661 texts and 95% were first seen elsewhere, but it crawled mostly non-registry repos, so its `copied_share` is only 0.094.

**README phrases, read narrowly.** Only sentences that mention a skill are read, a phrase must match whole words, and it does not count within three words after "not", "no" or "never" ("not a mirror of"). Collection words ("a curated collection of skills") are not used, because original authors describe their own skills that way. "Copied from" is not used either: it described install steps and file trees. A copier phrase is a 0.8 vote, a source phrase 0.6.

**Combine votes simply, and say it is not a probability.** Agreeing votes: `1 − (1 − a)(1 − b)`. Disagreeing votes: the stronger wins with `|a − b|`. No vote: `unclear`. The confidence ranks registries from doubtful to clear; it is not a measured probability, and the docs say so.

**A person can overrule.** Registries below 0.5 confidence go to `registry_kind_review.csv`, largest first, with the evidence. A label written there replaces the computed kind on the next run (`kind_basis = hand`). A filled sheet is never overwritten. The review is optional: the first 25 rows cover half the folders under review.

**Checked against known cases.** Three known copiers and three known sources (`anthropics/skills`, `obra/superpowers`, `github/awesome-copilot`) are printed on every run; all six get the expected label.

## Alternatives rejected

- **A share of all texts as the only rule.** Crawlers of non-registry repos have a low share and would be sources.
- **README only.** Only 38 of 4,718 READMEs have an accepted phrase, and only 2 of those are copier phrases.
- **A trained classifier.** No labelled set exists; a transparent rule with a review sheet can be checked by hand.

## Effect

4,060 sources, 702 copiers (385,400 folders, 312,153 of them in one crawler), 44 unclear. Most labels rest on the date vote alone.

## Limits

- A history rewrite makes an original look younger than its copies.
- Only registries are compared; a text from a non-registry repo counts as first seen in the earliest registry.
- 461 copier labels rest on fewer than 20 shared texts.
- A small crawler of non-registry repos (under 20 shared texts) looks like a source.
