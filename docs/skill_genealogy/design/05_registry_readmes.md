# Design: registry READMEs (stage 3b)

Card G37 · Code: `download_registry_readmes.py` · [Code flow](../code_flow/05_registry_readmes.md) · [Report](../report/05_registry_readmes_report.md)

## Problem

Stage 3c must tell a registry that writes its skills from one that collects them. Dates are the main evidence, but some repos say what they are: `majiayu000/claude-skill-registry` calls itself "an aggregate catalog, not a single plugin" and says "the maintainer of this mirror is not necessarily the author of a listed skill." Stage 3 keeps only skill folders, so it has no README.

## Design at a glance

For each registry, fetch only the commit stage 3 mined, without file contents, find the README the way GitHub does, fetch only that file, and store its raw text.

## Decisions

**The same commit as the history.** A README read today can describe a repo that changed after the history was mined. Fetching the stage 3 `head_commit` (`git fetch --depth 1 --filter=blob:none origin <commit>`) keeps both pieces of evidence from one snapshot. When that commit is gone (a force push) or stage 3 has no commit, the newest commit is used and `commit_basis` says so.

**GitHub's README order.** GitHub shows the first README in `.github/`, then the root, then `docs/`, with a fixed order of names (`readme.md` before `readme.rst`, and so on). Using the same order means the text is the one a visitor reads.

**Fetch one blob.** Listing three folders and fetching one file costs under a second per repo (median 0.9 s), so all 4,806 READMEs take 9 minutes.

**Raw text, 200 KB cap.** Stage 3c reads phrases; the text is not normalised here. A README over 200,000 bytes keeps its row and path but not its text, the same cap as scripts.

**Resume like stage 3.** `ok`, `no_readme`, `gone` and `empty` are finished; `timeout` and `error` are retried on request. The table is saved every 200 repos.

## Alternatives rejected

- **The GitHub API README endpoint.** Returns today's README, not the mined commit's, and has rate limits.
- **Read the README during stage 3.** Stage 3 is long and already finished; a separate small stage can be rerun alone.

## Effect

4,739 READMEs found, 33 repos without one, 34 gone. 4,769 were read at the stage 3 commit. Median size 11 KB.

## Limits

- A README rarely states where its skills came from (see [06](06_registry_kinds.md)): the README vote decides few labels.
- Non-English READMEs are stored but the stage 3c phrases are English.
