# s3. Labels and independent evidence dimensions

**Code:** `src/skillsig/s3_labeling/` · **Tests:** `tests/s3_labeling` · **Run:** [EXECUTION_STEPS.md § s3](../../EXECUTION_STEPS.md#s3-labeling-stage)

Keeps the historical labels for comparison and now reports reuse evidence separately
from disclosed agent involvement for each of the 1,877,981 distinct skills. Both can
be present. The [independent evidence output](evidence.md) assigns no human-authorship
verdict and does not penalize overlap. Baselines, text methods and existing annotation
scores still use the historical taxonomy.

---

## The four labels

| Label | Meaning | Typical evidence |
|---|---|---|
| `highly_templated` | The text is a copy or near-copy of widely shared content, so who committed *this* file says little about who wrote it | Registry match, near-duplicate cluster of 10+, 10+ copies |
| `agent_assisted` | The commit history discloses an AI coding agent | AI co-author trailer, coding-agent bot as author |
| `likely_human` | A person committed it alone, and nothing points to an agent | One skill per commit by a user account, one copy, no trailer |
| `insufficient_evidence` | Missing or conflicting signals | No commit history, bulk installs, non-coding bots |

Every label comes with **reason codes** and (from v2) a **confidence** (`high`, `medium`, `low`), so step 4.8 can check accuracy per reason.

**Why four labels, not "human vs. AI"?** Copied content is a third origin: a copied Anthropic skill was not written by the person who committed it, with or without an agent. And with three quarters of skills lacking history, a yes/no answer would mostly be a guess.

**Why versions (v1 → v2 → v2.1)?** Each version is kept, so its effect can be measured and the human spot-check (4.8) can score all of them on the same sample. **v2.1 is the current label.**

---

## Steps

Run in order, each with `python -m skillsig label --only <module>`:

| Step | File | Does | Output |
|---|---|---|---|
| 3.1 | [rules_v1.md](rules_v1.md) | Sprint 1 rules: copy count, trailer, bot, batch size | `s3_labels_v1` |
| 3.2 | [rules_v2.md](rules_v2.md) | Adds registry and cluster evidence, confidence | `s3_labels_v2` |
| 3.3 | [rules_v2_1.md](rules_v2_1.md) | Two corrections and independent flags: **the current label** | `s3_labels_v2_1` |
| 3.4 | [baselines.md](baselines.md) | One-signal labels to compare against | `s3_baseline_labels` |
| 3.5 | [style_model.md](style_model.md) | Does text alone look like disclosed-agent skills? | `s3_style_scores` |
| 3.6 | [text_methods.md](text_methods.md) | Which text-only method best separates agent from human? | `s3_text_methods_oof` |
| 3.7 | [text_score.md](text_score.md) | Text-based lean for skills without history (**not run yet**) | `s3_text_scores` |
| 3.8 | [evidence.md](evidence.md) | Reuse × agent disclosure, retaining overlapping flags and historical labels | `s3_evidence` |

Label tables go to `src/data/derived/`; summaries go to `results/`.

---

## Current result (v2.1, full data)

| Label | Skills | Share |
|---|---|---|
| `highly_templated` | 303,517 | 16.2% |
| `agent_assisted` | 142,667 | 7.6% |
| `likely_human` | 60,930 | 3.2% |
| `insufficient_evidence` | 1,370,867 | 73.0% |

---

## Who uses the outputs

| Output | Read by |
|---|---|
| `s3_labels_v2_1` | 3.4–3.6, and every s4 check (4.2, 4.4, 4.5, 4.6, 4.8) |
| `s3_labels_v1`, `s3_labels_v2` | 3.2, 3.3, 3.4; 4.6/4.8 score v2 alongside v2.1 |
| `s3_baseline_labels` | 4.6/4.8 score the baselines on the same sample |
| `s3_style_scores`, `s3_text_methods_oof` | 3.7 (planned); reported results |
| `s3_evidence` (and its `results/s3_evidence_*` tables) | 6.1 draws `f0_evidence_dimensions.png`; reported results |

---

## Full data vs dev sample

With `--dev-sample`, every step labels only the 50,000 sampled skills. Copy counts and batch sizes are still counted over the whole dataset, but cluster sizes come from 2.6, which in dev mode clusters only within the sample. So the `cluster` reason rarely fires in a dev run, and dev label shares are not a small copy of the full ones.

---

## Tests

```bash
python -m pytest -q tests/s3_labeling
```

94 unit tests on small fixtures; each step file lists its own. One regression test checks the v1 label counts on the real data (`python -m pytest -q -m data tests/regression/test_rules_v1_counts.py`).
