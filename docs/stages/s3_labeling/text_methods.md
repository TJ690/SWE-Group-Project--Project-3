# 3.6 Text methods, E5 (`text_methods.py`)

```bash
python -m skillsig label --only text_methods
```

[Back to s3](README.md)

## What it does

Compares text-only methods against two proxy groups: disclosure-positive and historical
`likely_human`. This does not provide verified human/agent authorship labels. Results
describe discrimination and high-precision coverage on those proxies; transfer to
skills without history has not been validated.

## How it works

- **Positive proxy:** non-templated skills labelled agent-assisted in v2.1 with a
  disclosure. **Negative proxy:** v2.1 `likely_human`. 20,000 of each, drawn with the seed.
- **Text:** the first 6,000 characters, front matter and tool names removed.
- **Methods:**

| Method | What it is |
|---|---|
| `length_only` | Body length alone |
| `ai_phrase_rule` | A fixed list of "AI-sounding" words (*comprehensive, robust, seamless, leverage, delve*, …) minus human habits; no training |
| `features_lr`, `features_gbm` | The 2.1 / 2.5 / 2.8 features with logistic regression or gradient boosting |
| `tfidf_words`, `tfidf_chars` | Word and character patterns with logistic regression |
| `ensemble` | Average of `features_gbm` and both TF-IDF models |

- 5 folds grouped by representative repository owner and detected cluster, scored on
  all rows, **matched** (positive proxies excluding bulk commits), and **strong human**
  (the historical name for negative proxies with revision evidence). These names do
  not establish actual authorship or remove every workflow difference.
- **Decided share:** the fraction of skills a method can label at 80% or 90% precision, assuming equal numbers of each class.

## Result

| Method | AUC (all) | AUC (matched) | Agent decided at 90% | Human decided at 90% |
|---|---|---|---|---|
| `length_only` | 0.53 | 0.54 | 0% | 0% |
| `ai_phrase_rule` | 0.52 | 0.51 | 0% | 0% |
| `features_gbm` | 0.65 | 0.62 | 0.0% | 0.3% |
| `tfidf_chars` | 0.70 | 0.67 | 0.4% | 1.4% |
| `ensemble` | **0.71** | 0.68 | 0.9% | 7.1% |

The tested phrase rule has AUC near 0.5; the ensemble reaches 0.711, which is some
proxy-label discrimination. At a nominal 90% precision against those proxies under
the assumed 50/50 prior, reported coverage is 0.9% of positives and 7.1% of negatives.
This neither establishes a practical true-authorship classifier nor proves that
text cannot work. Column names containing Agent/Human refer to these proxy groups.

## Outputs

- `s3_text_methods_oof.parquet`: out-of-fold score of every method for the 40,000 skills.
- `results/s3_text_methods.csv` and `.json`: AUC and decided share per method and subset.
- `results/s3_text_methods_top_words.csv`: the words that lean most each way.

## Design decisions

- **Why a second experiment after 3.5?** This evaluates different methods and a
  high-precision coverage target against a narrower negative proxy, not a verified
  human class. The population and target differ from the positive-unlabelled experiment.
- **Why the matched and strong-human subsets?** They examine selected workflow
  restrictions. They do not establish that workflow confounding has been eliminated.
- **Why also score within length bands?** The reported length-stratified AUC is
  about 0.708 for the ensemble. This checks a length-related explanation within the
  evaluated proxies, without establishing an authorship-specific style signal.
- **Noisy proxy labels:** undisclosed agent use can enter the negative group.
  Nonrandom disclosure and other selection can bias scores upward or downward;
  no guaranteed lower bound on true-authorship discrimination is established.

## Limits

Reads `s2_specificity`, which is currently older than the clusters (see [2.8](../s2_features/specificity.md)).

## Tests

`test_text_methods.py` (6): commit and date columns are not features; front matter and tool names are removed; the phrase rule ranks LLM-style text above casual text; decided share is right for perfect and random scores, and reweights unbalanced classes.
