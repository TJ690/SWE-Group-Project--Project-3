# Design: registry tree check (stage 2b)

Card G36 (bug B01) · Code: `check_registry_trees.py` · [Code flow](../code_flow/02_registry_tree_check.md) · [Report](../report/02_registry_tree_check_report.md)

## Problem

A registry is a repo with at least 5 English skills (stage 2c). The dataset comes from GitHub code search and holds only some files of each repo, so it under-counts some of the most copied repos: `obra/superpowers` has 1 skill in the dataset and 15 in the repo, `anthropics/skills` has 3 and 20. Hundreds of their copies are in the dataset. If they are not registries, the first repo that re-hosted one of their skills becomes its origin, and RQ1 credits the wrong author.

## Design at a glance

For every non-fork with at least 100 stars and 1–4 English skills in the dataset, clone only the newest commit, without file contents, and count the `SKILL.md` paths in the tree. Stage 2c promotes a repo with 5 or more.

## Decisions

**Count from the repo's own tree.** The tree at HEAD is the ground truth the dataset sampled from. A shallow, blobless clone (`--depth 1 --filter=blob:none --no-checkout`) brings the file listing and nothing else, so a check takes 0.44 seconds at the median.

**Only repos that could change the answer.** A repo with 5 or more dataset skills already passes; a repo with 0 English skills in the dataset has no copies to explain. The candidates are the 6,834 non-forks with 100+ stars and 1–4 English skills.

**Why not lower the skill cutoff instead.** 365 repos with 10,000+ stars and about 6,800 with 100+ stars have 1–4 dataset skills, and most of them, such as `fastapi/fastapi`, really hold only a few. Lowering the cutoff would make them all registries. Counting the tree separates the under-counted from the small.

**Count paths, not texts.** The check does not open files, so it counts skills in any language. That is a small over-count that stage 4a corrects: a lineage whose text is not English is dropped there.

**Save as it goes, resume.** The table is written every 200 repos; a rerun skips `ok`, `gone` and `empty`. The order is by stars, then name, so `--limit` always checks the same repos.

**Same star cutoff as the registry rule (100).** The team chose 100 stars as the bar for a source; the tree check does not change that bar.

## Effect

All 6,834 candidates were checked: 6,777 `ok`, 57 `gone`. 1,240 have 5 or more `SKILL.md` files and become registries in stage 2c, including `obra/superpowers` and `anthropics/skills`. The share that passes rises with stars: 16.7% at 100–999 stars, 27.9% at 10,000+.

## Limits

- The tree is today's HEAD, not the dataset's snapshot. A repo that grew after the crawl can pass.
- The count includes non-English skills (corrected in stage 4a).
- A repo under 100 stars is never checked, even if it is widely copied. Stage 7a still finds copies of such repos from history.
