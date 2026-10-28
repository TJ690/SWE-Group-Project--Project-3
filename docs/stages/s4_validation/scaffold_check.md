# 4.5 Scaffold check (`scaffold_check.py`)

```bash
python -m skillsig evaluate --only scaffold_check
```

[Back to s4](README.md)

Not run by `python -m skillsig evaluate` on its own; run it with `--only`.

## Question

Step 2.9 measures structural templating (shared lines, outlines, placeholders), but no label uses it yet. Is it ready to?

## How it works

Four checks:

| Check | Asks |
|---|---|
| **Overlap** | Which skills do the scaffold signals mark that the current templated rules don't? |
| **Reference coverage** | Recovery of 6,481 exact matches, separately from conditional agreement on 42,104 near candidates |
| **Agent confound** | Do outline matches occur more often in skills whose commits disclosed an agent? If so, the outline rule partly detects AI style. |
| **Spot sheet** | 150 marked skills for a person to check, each shown with the passages it shares with a skill from another owner |

## Result

- **Exact-reference recovery:** 90.7% of 6,481 exact registry matches (95.4% with
  generic rules). On 42,104 similarity-selected near candidates, 98.4% is conditional
  agreement, not independent validation. The older pooled 97.4% is exploratory.
- **New coverage:** about 113,000 skills that the current rules don't call templated, mostly via shared lines (85,356) and outlines (19,989).
- **Descriptive association:** outline matches occur in 0.89% of disclosed-agent
  skills versus 1.17% of the rest. This does not show a higher raw rate in the
  disclosed group, but it does not rule out style, topic or workflow confounding.

The old spot sheet has not been reviewed, so whether the additional candidates are
correct remains open. A new [paired review](../../report/TEMPLATE_EVALUATION.md)
shows full texts and shared passages, recomputes exact body-shingle Jaccard, and
keeps material reuse separate from shared-scaffold judgments. It does not alter
the original blind annotations or infer authorship.

## Outputs

`results/s4_scaffold_overlap.csv`, `s4_scaffold_recall.csv`, `s4_scaffold_agent_confound.csv`, `s4_scaffold_spot.csv`.

## Design decisions

- **Check before using:** high recall doesn't show the additions are right; the spot sheet and the confound test do.
- **Shared passages on the sheet:** a reviewer sees the actual shared text instead of trusting a score.
- **Skill texts are not fingerprinted:** only the 150 spot rows read them, and hashing the whole raw table on every run would be slow.

## Tests

`test_scaffold_check.py` (5): new hits counted; recall on known copies; confound rates; spot partners come from another owner; tables written with evidence.
