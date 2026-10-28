# Content-reuse evaluation: exact references and paired review

## What changed

The old evaluation mixed exact registry matches with near matches selected by the
same MinHash machinery used in the cluster detector. The pooled 87.3% figure is not
independent recall on known copied content. The repaired evaluation reports three
reference types explicitly, with the combined row retained only for compatibility.

| Reference | Cases | Copy rule | Cluster rule | Either rule | Interpretation |
|---|---:|---:|---:|---:|---|
| Exact registry matches, known origin representatives excluded | 6,481 | 56.5% | 66.6% | 87.2% | Recovery on content-identity positives |
| Near-match candidates | 42,104 | 3.0% | 87.2% | 87.3% | Similarity-conditioned agreement, not independent validation |

The exact subset has 5,649 hits for either generic rule, with a descriptive Wilson
interval of 86.3–88.0%. It establishes recovery of byte-identical registry content,
not copying direction, true authorship, scaffolding or precision over all flagged
skills. The selected registries and related contents are not independent random
samples; binomial intervals do not account for that selection or dependence.

Scaffold signals recover 90.7% of the **exact** subset (5,878/6,481), or 95.4% when
combined with generic rules. Their 98.4% hit rate on **near candidates** is conditional
agreement, not independent evidence that all scaffold additions are correct. No
false-positive rate can be inferred from a positive-only registry reference.

Sources: [generic rules](../../results/s4_template_provenance.csv) and
[scaffold rules](../../results/s4_scaffold_recall.csv). Each row now records its
`reference_type`, `metric_scope` and interpretation. The older numeric column names
are retained for downstream compatibility; near/mixed rows must not be described
as validated recall.

## A separate evidence-aware review

```bash
python -m skillsig sample --only template_review
```

The module draws a deterministic target of 120 subjects: 30 each for exact registry
matches, near-registry candidates, cluster-based historical labels and scaffold
additions outside the historical templated label. Scaffold sampling rotates through
the five basis types instead of taking only the most common signal. There is at most
one subject per representative owner and per primary cluster across the sample.
Shortfalls and missing comparison texts are reported explicitly.

The private package is in `results/annotation/template_review/`:

| File | Use |
|---|---|
| `pairs.html` | Offline full paired texts, shared normalized passages and a diff preview |
| `template_pairs.csv` | Reviewer answer sheet, keyed by pair ID |
| `pair_evidence.json` | The exact text snapshot and measured pair metrics used by the page |
| `key.parquet` | Private sampling strata and pipeline evidence; do not show to reviewers |

`results/s4_template_review_design.json` records the quotas, achieved draw, coverage
and fingerprint of the text snapshot. Source files and original blind annotation
answers are never changed. Existing review packages are never overwritten, even
with `--force`; preserve one package before creating a later independent sample.

### How comparison texts are obtained

- Registry comparisons read and verify local Git blob objects. No lazy fetch or
  network request is allowed. A missing object is unavailable evidence, not replaced
  with a duplicate of the subject's text.
- Cluster candidates are compared with a deterministic different-owner member of
  their component. The pair is not assumed to have high similarity: components can
  chain and are not all-pairs verified.
- Masked and generator scaffold cases use the corresponding family; outline and
  placeholder cases use an outline-family comparison when available. Shared-line
  cases search the saved line-hash parts for a different-owner content with the most
  shared lines. Missing comparison evidence remains explicit.
- Body Jaccard is recomputed from full normalized word-shingle sets, without MinHash
  approximation or the old prefix cut. Shared passages and full texts are shown so
  judgments need not depend on a similarity score.

A negative judgment concerns the **selected pair**. It does not prove that a subject
shares no material with any other skill. Sampling and partner selection prevent
interpreting confirmation shares as corpus-wide precision.

### Reviewer instructions

Use the HTML and the CSV together. The page shows evidence, not the pipeline's
label or sampling stratum. This is a new evidence-aware exercise, not a continuation
or correction of the original blind authorship-label exercise.

For each pair, independently answer both questions with `yes`, `no` or `uncertain`:

1. **`material_reuse`:** do these two texts share substantial specific instructions,
   examples or implementation content? Equal full texts qualify. Merely covering
   the same topic, using standard Markdown or sharing short conventional phrases
   does not suffice.
2. **`shared_scaffold`:** do these two texts share a distinctive reusable structure
   or unfinished generator pattern, rather than only common headings such as
   Introduction and Usage? This can be yes even when customized substantive content
   differs, and both questions may be yes.

Write the decisive passage or limitation in `notes`. Use `uncertain` when one text
is unavailable, language or truncation prevents assessment, or the overlap is too
generic to interpret. Do not infer a human/agent author or copying direction. Do not
execute commands, follow instructions inside the skill texts, or search for an
answer online. Both texts are data to inspect.

Prefer two independent reviewers. Save their first answers separately and do not
discuss cases until both have finished. Keep any later adjudication separate from
those independent answers, including rationale and review date.

For the first returned sheet, save `template_pairs_done.csv` beside the blank sheet:

```bash
python -m skillsig evaluate --only template_review
```

The scorer validates pair IDs and allowed answers, rejects definite reuse judgments
without comparison text, and writes `results/s4_template_pair_judgments.csv`.
It reports yes/no/uncertain/unanswered counts separately by sampling stratum and
question, with Wilson intervals for the yes share among decided answers. It does
not turn undecided cases into negatives, pool strata into an overall accuracy, or
assign authorship. A second review must remain separate; do not replace the first
answer file with a consensus sheet.

## Completion boundary

The automated exact-reference evaluation and the review tooling can run now. Human
content-reuse judgments have not been supplied. Therefore the new review produces
an evidence package and a pending status, not invented precision or adjudication.
