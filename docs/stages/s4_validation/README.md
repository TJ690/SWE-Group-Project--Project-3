# s4. Validation: how far can the labels be trusted?

**Code:** `src/skillsig/s4_validation/` · **Tests:** `tests/s4_validation` · **Run:** [EXECUTION_STEPS.md § s4](../../EXECUTION_STEPS.md#s4-validation-stage)

There is no ground truth for who wrote a skill, so this stage checks the labels in three indirect ways:

| Kind | Idea | Checks |
|---|---|---|
| **Known answers** | Cases where the right answer is known for another reason | 4.1, 4.2, 4.3, 4.5 |
| **Robustness** | Does a label share move a lot when a threshold changes a little? | 4.4 |
| **People** | Two people label a blind sample; the pipeline is scored against them | 4.6 – 4.8 |

(E4, the text-only check, is step [3.5](../s3_labeling/style_model.md).)

---

## Checks

| Step | File | Question | Command |
|---|---|---|---|
| 4.1 | [disclosure_recall.md](disclosure_recall.md) | How many AI disclosures does first/last-commit history miss? | `evaluate --only disclosure_recall` |
| 4.2 | [template_provenance.md](template_provenance.md) | Do the copy and cluster rules find skills known to copy a registry? | `evaluate --only template_provenance` |
| 4.3 | [edit_robustness.md](edit_robustness.md) | How much editing does the near-copy detector tolerate? | `evaluate --only edit_robustness` |
| 4.4 | [stability.md](stability.md) | How do label shares move when one threshold changes? | `evaluate --only stability` |
| 4.5 | [scaffold_check.md](scaffold_check.md) | Are the 2.9 scaffold signals ready to join the label? | `evaluate --only scaffold_check` |
| 4.6 – 4.8 | [spot_check.md](spot_check.md) | Draw a blind sample, have people label it, score the pipeline | `sample`, then `sample --only spot_check` |
| Paired reuse review | [Protocol](../../report/TEMPLATE_EVALUATION.md) | Show full paired texts and assess material reuse/scaffold separately | `sample --only template_review`, then `evaluate --only template_review` after answers |

All commands start with `python -m skillsig`. `python -m skillsig evaluate` on its own runs 4.1–4.4, then 4.8 if person A's finished sheet exists; it does **not** run 4.5.

Results go to `results/s4_*.csv|json` (in git). The annotation sheets go to `results/annotation/` (**not** in git; back them up by hand).

---

## How results are reported

- **Every share has a 95% interval** (Wilson), which behaves well for small samples and for shares near 0% or 100%.
- **Small samples are marked "indicative".** With 30 skills per label, an interval is about ±16 points wide.
- **Samples are seeded**, so a rerun draws the same skills.

---

## Main findings so far

- **First/last-commit history misses about 8% of AI disclosures** (17% for skills edited 3+ times). [4.1]
- **The copy and cluster rules recover 87.2% of 6,481 exact registry matches**;
  copy count alone recovers 56.5%. Near candidates are similarity-conditioned
  agreement, not independent validation. Paired content-reuse review is pending. [4.2]
- **Synthetic edit retention:** 77.6% of pairs remain above the estimated similarity
  cutoff after 5% line replacement, versus 16.4% after 10%. This is not end-to-end
  corpus-search recall or a statement about all natural edits. [4.3]
- **A agrees with 25/30 `agent_assisted`, 1/31 `highly_templated` and 6/30
  `likely_human` labels.** B's overlap agrees with A on 21/40 (kappa 0.29), and a
  third reader, C, agrees with A on 33/60 (kappa 0.31) and with B on 14/21 (kappa 0.47), so
  these are comparisons against uncertain readers, not verified authorship accuracy. [4.8]

---

## Tests

```bash
python -m pytest -q tests/s4_validation
```

42 unit tests on small fixtures; each check's file lists its own.
