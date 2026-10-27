# 4.1 Disclosure recall, E1 (`disclosure_recall.py`)

```bash
python -m skillsig evaluate --only disclosure_recall
```

[Back to s4](README.md)

## Question

The dataset keeps only each skill's **first and last** commit message. An AI trailer on a commit in between is invisible to the labels. How many disclosures does that miss?

## How it works

The registry clones (s1) keep **every** commit, so for registry skills the answer can be counted. For each registry skill: did any commit carry an AI trailer, did the first or last one, or only a middle one? Recall = disclosures visible in the first or last commit ÷ all disclosures.

## Result

| Skills | With any AI trailer | Visible in first/last | Recall (95% interval) |
|---|---|---|---|
| All registry skills | 1,033 | 948 | 91.8% (89.9–93.3) |
| Skills with 3+ commits | 506 | 421 | 83.2% (79.7–86.2) |

Even when a tool discloses itself, the labels miss about 8% of disclosures overall and 17% for skills edited three or more times.

## Outputs

`results/s4_disclosure_recall.csv` (overall, 3+ commits, and per publisher group) and `.json`.

## Design decisions

- **Why report 3+ commits separately?** Skills with one or two commits can't have a middle commit, so including them inflates recall.
- **Limit:** registries are popular, maintained collections, not a random sample of the dataset.

## Tests

`test_disclosure_recall.py` (4): middle-only and single-commit cases; recall matches a hand count; outputs written; missing inputs name their command.
