# 4.3 Edit robustness, E3 (`edit_robustness.py`)

```bash
python -m skillsig evaluate --only edit_robustness
```

[Back to s4](README.md)

## Question

The near-copy detector (2.7) uses a similarity threshold of 0.8. How much can someone edit a registry skill before it stops being detected?

## How it works

500 seeded registry skills are edited at five levels (5, 10, 20, 40, 60% of non-blank body lines) in two ways: lines **replaced** with lines from unrelated skills, or lines **deleted**. Each edited text is compared with its original using the same settings as 2.6. Detected = similarity ≥ 0.8.

## Result

| Lines changed | Replaced: still detected | Deleted: still detected |
|---|---|---|
| 5% | 78% | 96% |
| 10% | 16% | 55% |
| 20% | 0% | 3% |
| 40%, 60% | 0% | 0% |

So "near copy" means a **lightly** edited copy: about one line in twenty changed. A copy with a rewritten section is not "near" and must be caught by clusters or the scaffold signals.

## Outputs

`results/s4_edit_robustness.csv` and the chart `figures/12_edit_robustness.png`.

## Design decisions

- **Why synthetic edits?** Real edits have unknown size; controlled edits show exactly what 0.8 means.
- **Why two kinds of edit?** Replacing both adds and removes text; deleting only removes. Real customisation is somewhere in between.

## Tests

`test_edit_robustness.py` (6): editing 0% changes nothing; exact edit counts, front matter kept; similarity is symmetric; results deterministic and falling as edits grow; chart written.
