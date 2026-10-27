# 3.5 Style model, E4 (`style_model.py`)

```bash
python -m skillsig label --only style_model
```

[Back to s3](README.md)

## What it does

Asks: **does the text alone look like skills whose commits disclosed an AI tool?** It does not change any label.

## How it works

- **Skills used:** those with history that are not templated, 380,023 in all.
- **Positives:** skills whose commits disclosed an agent (`flag_disclosed_agent`). **Everything else is "unlabelled", not "human"**: a model trained this way is called positive-unlabelled (PU) learning.
- **Text-only features:** structure (2.1), wording (2.5), specificity (2.8), and
  `marker_rate`, the rate of words that disclosed skills overuse. Commit and repository
  metadata are not text-model features; the explicitly named date variants use date.
- **Marker words** are chosen inside each training fold only, with tool names (`claude`, `cursor`, `gpt`, `agent`, …) removed.
- **Model:** logistic regression, 5 folds grouped by owner and cluster (see [3.4](baselines.md)).
- **Comparisons:** text only; date only (months since the format launched); text plus date; text without the newest features; text without marker words.

## Result

How well each model separates disclosed from unlabelled skills (AUC: 0.5 is chance, 1.0 is perfect):

| Model | AUC |
|---|---|
| Text only | 0.60 |
| Date only | 0.48 |
| Text plus date | 0.61 |
| Text without marker words | 0.59 |

The tested text features distinguish disclosed from unlabelled proxies weakly in
this experiment. The date-only baseline performs near chance, but that does not
exclude other temporal, tool-family or workflow confounding. This is not a direct
human-versus-agent authorship evaluation.

## Outputs

- `s3_style_scores.parquet`: out-of-fold score per skill.
- `results/s3_style_model.csv` and `.json`: AUC per model and fold, with intervals.
- `results/s3_style_top_words.csv`: the marker words.

## Design decisions

- **Why PU, not human vs. agent?** Missing disclosure does not establish human
  authorship, so the negative pool is unlabelled. Selection into disclosure and
  label noise can bias apparent discrimination in either direction. No guaranteed
  lower bound on true-authorship AUC is established. The PU rescaling assumption
  of random disclosure among agent-assisted skills is not established either.
- **Why leave out historically templated skills?** Reuse patterns could teach the
  model registry/collection style. This filter reduces one concern but does not
  establish authorship or remove all topic, tool or workflow confounding.
- **Why a date-only model?** It checks one possible temporal proxy. Its AUC near
  0.48 is descriptive for this population; it does not rule out temporal confounding
  or substitute for the untested repository-creation-date baseline.
- **Why choose marker words per fold and drop tool names?** Otherwise the model learns "mentions Claude", or sees the test data while picking words.

## Limits

- Reads `s2_specificity`, which is currently older than the clusters (see [2.8](../s2_features/specificity.md)).
- Reads the skill texts from `artifacts`, but its manifest does not fingerprint them.

## Tests

`test_style_model.py` (7): commit and date columns never enter the text model; a planted separable feature gives a high score and random ones give chance; marker words fitted on training rows only.
