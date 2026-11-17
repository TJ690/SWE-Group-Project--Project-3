# Paper direction: from description to consequence

How the `skill_genealogy` work becomes a publishable result: the problem, an honest review of its weaknesses, the claims worth making, and the pipeline changes that test them. Stage designs are in [design](design/README.md).

## The problem

Agent skills (`SKILL.md` folders) spread by copy-paste. GitSkills is one snapshot and records no provenance, so nobody can tell which skills are copies, where they came from, or whether they kept up with their source. Large copier registries (for example `majiayu000/claude-skill-registry`, 312k folders) also hide the true origin.

Current research questions:

- **RQ1, origin.** Where do skills come from: a registry, an independent author, or nobody else?
- **RQ2, change.** What do people change when they copy a skill?
- **RQ3, staleness.** Do copies fall behind the registry they came from?

## Weaknesses a reviewer will raise

1. **The method is not new.** Clone genealogy, cross-project copy reuse (DéjàVu, Gharehyazie et al.), outdated Stack Overflow snippets (Ragkhitwetsagul et al., Baltes & Diehl) and technical lag in dependencies are well studied. MinHash plus git history is standard. The paper must say what skills show that code clones did not.
2. **The questions describe, they do not show consequences.** "18% are copies" or "copies are stale" says nothing unless the missed updates matter. Staleness counts every change, so typo fixes count as much as real fixes.
3. **The time window is short.** Skills spread from late 2025, so most lineages are months old and staleness depends on the snapshot date.
4. **Data limits.** GitSkills is a code-search sample (the tree check had to add 1,240 registries). One aggregator holds most copier folders. 6,718 of 17,415 re-host links join repos of the same owner (templates, not copying). Registry kinds are rule-based and not yet checked by hand.
5. **Engineering ahead of findings.** Many stages, no RQ answered yet; the risk is a pipeline paper.
6. **Drift from the project question.** The repo's main question is authorship signals; the genealogy must feed it or stand as its own paper.

| Venue | Assessment |
|---|---|
| MSR 2027 Mining Challenge | Plausible, if it leads with one consequence finding. |
| MSR or ICSE main track | Not as-is: needs a consequence claim, validation and the skills-versus-clones argument. |

## Why skills differ from code clones

A skill is instructions plus scripts plus permissions (`allowed-tools`), and an agent carries it out with little review. A stale instruction is executed, not just left uncompiled. Permission changes are therefore measured as their own category.

## Claims worth making

### Claim A: copies keep defects their source already fixed (lead claim)

Headline form: "X% of copies hold a version the source later fixed; the median time to adopt a security fix is Y days, and fixes are taken up no faster than cosmetic edits."

### Claim B: copies drop attribution and licence notices

Headline form: "Of copies whose source licence requires a notice, X% keep any notice." Phrased as attribution retention, never as a legal violation.

### Optional link to the authorship study

A copied skill's wording is not evidence of how it was written. Report how much of the authorship-labelled set is copies, and whether labels change when copies are removed.

## Step 0: the go/no-go pilot

The claims rest on one question: are registry updates real fixes or mostly cosmetic? Answer it before building more.

1. Sample about 300 version transitions from the stage 4a lineage changes, weighted toward lineages with dataset copies, same-owner pairs left out.
2. Show each with its commit subject (stored by stage 3) and its diff.
3. Two raters label each with the codebook below; report Cohen's kappa and the share of each kind.

| Kind | Examples |
|---|---|
| `security` | Dangerous command removed, `allowed-tools` tightened, secret leak fixed, URL pinned |
| `correctness` | Wrong command or path, renamed tool or API, broken step, outdated model name |
| `capability` | New step, script or section |
| `wording` | Rephrasing, clarification, examples |
| `cosmetic` | Formatting, typos, front-matter metadata |

**Gate.** If `security` plus `correctness` is a meaningful share (roughly 15–20% or more), Claim A leads. If not, RQ3 is reported as "copies drift, but what they miss is mostly cosmetic", and Claim B leads. The labelled sample is reused as validation data either way.

## Pipeline changes

| Change | Where | Why |
|---|---|---|
| Label each registry change by kind (rules on commit subject and diff: command or URL changed, risk flag removed, `allowed-tools` changed, tool or model names, whitespace only), with a confidence | New stage 4c, `lineage_change_kinds.parquet`, no network | Separates real fixes from cosmetic edits |
| Missed updates by kind (`missed_security`, `missed_correctness`, ...) and fix exposure (holds a version later fixed; days exposed) | G22 `find_missed_updates` | Turns staleness into risk |
| Time to adopt each fix, with Kaplan–Meier curves; copies not yet updated are censored | G22 sync events (A48), stage 11 | Handles the short time window |
| Fetch `LICENSE*` at the mined commit | Stage 3b (same sparse fetch as the README) | Source licence for Claim B |
| Fetch the copy repo's root `LICENSE*` | Stage 6 | Copy licence for Claim B |
| Kept `author`, `license`, `source` front-matter fields; mentions of the source owner, repo or URL | G19 (`compare_front_matter`) | Attribution retention |
| `same_owner` flag on every link; headline numbers leave these out | Stages 5b, 5c | Template reuse is not copying |

## Validation

- Every copier statistic reported with and without `majiayu000/claude-skill-registry`.
- Blind hand samples, two raters, agreement reported: copy-link precision (G17 gives recall), registry kinds (`registry_kind_review.csv`), change kinds.
- Headline numbers at Jaccard 0.70, 0.80 and 0.90.
- Copy and registry compared at the same date, never the snapshot copy against today's registry.

## Order of work

```text
stage 6 (download) ─┐
pilot ──────────────┴─> gate ─> 4c change kinds ─> 7a/7b families + recall
                                                 ─> 9a/9c copy vs origin, scripts, attribution
                                                 ─> 10 staleness + exposure + survival
                                                 ─> 11 summary tables
       Claim B in parallel: 3b LICENSE fetch, stage 6 root LICENSE
```

Put off until after the paper: G18 trees, the viewers (G24, G25, G32, G34) and the repo report (G26). G20 keeps only the origin and latest targets unless a result needs the others.
