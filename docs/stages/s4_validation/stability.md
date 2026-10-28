# 4.4 Stability, E5 (`stability.py`)

```bash
python -m skillsig evaluate --only stability
```

[Back to s4](README.md)

## Question

How do aggregate label and confidence shares change across the tested threshold values?
This is descriptive sensitivity analysis, not proof of individual decision stability.

## How it works

On a seeded sample of 200,000 skills, the v2.1 labels are recomputed with the real rule functions, changing **one** threshold at a time:

| Threshold | Default | Tried |
|---|---|---|
| Copies / cluster size for templated | 10 | 8, 12 |
| Near-copy similarity | 0.8 | 0.7, 0.9 |
| Bulk batch size | 6 | 4, 8 |
| Commits needed for "revised" | 3 | 2, 4 |
| Days needed for "revised" | 7 | 3, 14 |
| Cluster similarity | 0.8 | 0.6 |

For each setting it reports the four label shares, and three confidence shares that some thresholds affect without changing labels. At the default setting the recomputed labels must equal the stored v2.1 labels; that agreement is reported as a self-check.

## Result

Example: changing the copy threshold from 8 to 12 moves `highly_templated` from 17.4% to 15.5% of skills. Full results are in `results/s4_stability.csv`.

## Outputs

`results/s4_stability.csv` (share and change in percentage points per setting) and `.json`.

## Design decisions

- **One threshold at a time:** each row answers one question. Interactions between thresholds are not explored.
- **Confidence shares too:** the "revised" thresholds only change confidence, so they would look perfectly stable otherwise.
- **The self-check:** if the default setting didn't reproduce the stored labels, every other row would be measuring a different rule set.

## Limits

The current output reports shares, not per-item transitions under each setting.
Opposing label changes can cancel, so similar aggregate shares do not establish
stable individual decisions. One-at-a-time settings do not test interactions or
all plausible threshold choices. Default agreement with stored labels is a
reproduction check, not evidence of stability under changed thresholds.

**The 0.7 similarity setting has no effect.** Step 2.7 marks a skill `near` only at 0.8 or above, and the rules accept only rows already marked `near`, so lowering the threshold here adds nothing. Its rows show exactly zero change; only the stricter 0.9 setting is really tested.

## Tests

`test_stability.py` (5): settings include the default and skip unchanged values; the copy threshold moves the templated share; the 0.6 cluster column is used; missing values are handled; shares sum to one.
