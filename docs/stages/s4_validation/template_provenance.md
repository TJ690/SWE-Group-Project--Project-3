# 4.2 Template provenance, E2 (`template_provenance.py`)

```bash
python -m skillsig evaluate --only template_provenance
```

[Back to s4](README.md)

## Question

How many exact registry matches do the generic copy-count and cluster rules recover?
How often do those rules agree on separately selected near-match candidates?

## How it works

The exact reference consists of 6,481 exact registry matches after known origin
representatives are excluded. Equal blob hashes support content identity, not
authorship or copying direction. The 42,104 near matches are similarity-conditioned
candidates sharing the cluster detector's MinHash mechanism. They are reported
separately, not treated as independent known-copy positives. A near match with a
missing similarity is excluded. Each row records its reference type and scope.

## Result

| Reference | n | Copy rule | Cluster rule | Either |
|---|---|---|---|---|
| Exact content-identity positives | 6,481 | 56.5% | 66.6% | 87.2% |
| Near candidates (conditional agreement) | 42,104 | 3.0% | 87.2% | 87.3% |

The mixed `exact_or_near` CSV row remains for compatibility and is explicitly marked
exploratory. It must not be used as independent validation. Exact recovery is not a
false-positive rate or a measure of overall templating precision. Wilson intervals
are descriptive and do not account for registry selection and related contents.

For near candidates and scaffold additions, use the new
[paired evidence-aware review](../../report/TEMPLATE_EVALUATION.md):

```bash
python -m skillsig sample --only template_review
```

It shows full paired texts, shared passages and a diff, without the pipeline label.
This is separate from the original blind authorship annotations, which are preserved.

## Outputs

- `results/s4_template_provenance.csv`: the table above.
- `results/s4_template_unexplained.csv`: templated skills **without** a registry match, by reason.

## Design decisions

- **Why exact registry matches?** They establish content identity independently of
  similarity clustering. They do not prove "little customization" for the entire
  templated category, and the positive-only reference cannot establish precision.
- **Why "unexplained" is not called an error:** a templated skill with no registry match may copy a source that isn't on the registry list.

## Tests

`test_template_provenance.py` (3): recall on known copies; unexplained share; both tables written.
