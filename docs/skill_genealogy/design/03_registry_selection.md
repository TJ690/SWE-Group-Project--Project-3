# Design: registry selection (stage 2c)

Card G08 · Code: `select_registries.py` · [Code flow](../code_flow/03_registry_selection.md) · [Report](../report/03_registry_selection_report.md)

## Problem

A skill family needs a root: the repo where the skill was written. The study cannot clone all 282,200 dataset repos, so it picks the repos that are likely to be sources (**registries**), downloads their history, and treats every other repo as a possible copy. The stage also needs a small, fixed set of registries for dev runs.

## Design at a glance

A registry is a non-fork with at least 100 stars and either 5+ English skills in the dataset (`dataset`) or 1–4 in the dataset and 5+ `SKILL.md` files in its tree (`repo_tree`, stage 2b). Every repo gets one role. A hub flag is a preview. Thirty registries, 10 per size group, form the dev sample.

## Decisions

**Stars plus a minimum number of English skills.** A family tree needs a source large enough to study, and stars show that other people use it. Five distinct English skills separates a skill collection from a project that ships one skill. Counting English skills only (not every language) removes 402 repos that reached 5 only with non-English texts.

**Distinct texts, not files.** `english_skills` counts distinct texts, so one skill stored in two folders counts once.

**A fork is never a registry.** A fork is a copy. If it were a source, the same skill would seem to start in two places.

**The tree count promotes, never demotes.** A repo that passes on the dataset count stays a registry. The tree count only adds the under-counted (1,240 repos).

**The hub flag here is only a preview.** A hub is a repo that collects other people's skills. Testing it needs dates, and the dataset's `first_commit_at` is empty for 88% of registry texts. `discovered_at` is not used: it is when the crawler saw the file, not when anyone wrote it. So stage 2c computes a share from the few dated texts, keeps it as a preview, and the real label comes later from downloaded history (stage 3c, used by stage 5b).

**One role per repo.** `registry`, `registry_hub`, `hub` or `repository`. Later stages read one column to know what a repo is. The registry list and the registry rows of the role table must name the same repos, or the run stops before writing.

**Dev sample of registries, stratified by size.** Registries are cut into three equal groups by English-skill count and 10 are drawn from each with a fixed seed. Size matters: a 5-skill registry and a 13,000-skill registry stress the code differently. Every dataset file that matches a dev registry stays in the dev run, so families are whole.

## Alternatives rejected

- **Top N registries by stars.** Misses large skill collections with few stars, and includes famous repos with one skill.
- **A lower skill cutoff instead of the tree check.** See [02](02_registry_tree_check.md): thousands of small, popular repos would pass.
- **Hub test from `discovered_at`.** It dates the crawl, not the authoring.

## Effect

4,806 registries: 3,566 by the dataset count, 1,240 by the tree count. 277,389 repos are `repository`; 5 are preview hubs, all under 100 stars. The dev sample has 30 registries and no hub, because no registry is a preview hub.

## Limits

- A source under 100 stars is not a registry; its copies are found later from history (stage 7a), not here.
- A copier with 100+ stars passes the rule. Stage 3c finds it and stage 4a leaves it out.
- The dev sample is fixed; a bug that only shows on a very large registry can still pass dev.
