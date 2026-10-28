# 3.7 Text score, E5b (`text_score.py`): not run yet

```bash
python -m skillsig label --only text_score
```

[Back to s3](README.md)

## What it will do

Give skills **without** commit history a text-based lean, using the method chosen in 3.6.

1. Train that method on all labelled skills with history (as in 3.6).
2. Score every non-templated skill without history.
3. Assign `text_lean_agent` or `text_lean_human` where 3.6's held-out precision reaches the target, and `text_uncertain` in between.
4. `label_with_text` replaces `insufficient_evidence` only for skills without history; every other v2.1 label stays.

## Outputs

`s3_text_scores.parquet`; `results/s3_text_score_summary.csv` and `results/s3_text_score.json`.

## Design decisions

- **A "lean", not a label:** precision is estimated with equal class sizes and noisy humans, so it is a tendency with a measured error rate, kept apart from the commit-based label.
- **Depends on 3.6:** with the current results (a few percent decided at a nominal 90% precision against proxy labels), most skills without history would stay `text_uncertain`.

## Tests

`test_text_score.py` (3): cut-offs separate clean classes; with no signal nothing is decided; lean bands.
