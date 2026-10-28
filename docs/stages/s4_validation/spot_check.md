# 4.6 – 4.8 Human spot-check, E6 (`spot_check.py`)

[Back to s4](README.md)

Three steps: the pipeline draws a blind sample (4.6), people label it (4.7), the pipeline scores itself against them (4.8).

---

## 4.6 Draw the sample

```bash
python -m skillsig sample
```

**Who is drawn.** 30 skills per v2.1 label, 120 in all. Each label is split into two groups of 15, aimed at the parts most likely to be wrong:

| Label | Group 1 | Group 2 |
|---|---|---|
| `highly_templated` | Registry copies | Cluster or copy-count only |
| `agent_assisted` | Added by v2.1 (bulk or last-commit trailer) | Already agent in v2 |
| `likely_human` | Not revised | Revised |
| `insufficient_evidence` | Has history | No history |

Rules: body of at least 200 characters; **at most one skill per repository and per near-duplicate cluster** across the whole sample; seeded order.

**What people get** (in `results/annotation/`):

| File | For | Contents |
|---|---|---|
| `spot_A.xlsx` | Person A | All 120 skills |
| `spot_B.xlsx` | Person B | 40 of them (a third per label) |
| `trailer_check.csv` | Anyone | 50 commit messages the extended trailer detector flagged |
| `codebook.md` | Both | The label definitions |
| `key.parquet` | Nobody | The pipeline's answers, kept for scoring |

Each sheet row shows the repository, its kind, the skill name, description, body (up to 32,000 characters, Excel's cell limit), commit count, and first/last commit dates and messages, with dropdowns for the label and a confidence. **No pipeline output** (label, reasons, copies, cluster size, …) ever reaches a sheet.

**Safety:** `sample` never overwrites an existing `spot_A.xlsx`. After relabelling, use this instead; it keeps the same skills and only refreshes the key:

```bash
python -m skillsig sample --only spot_refresh
```

## 4.7 Label (people)

Follow [ANNOTATION_GUIDELINES.md](../../ANNOTATION_GUIDELINES.md). Person A and person B work separately and save `spot_A_done.xlsx` and `spot_B_done.xlsx`; the trailer check is saved as `trailer_check_done.csv`. An optional third reader, person C, labels any subset of a fresh copy of `spot_A.xlsx` and saves it as `spot_C_done.xlsx`.

## 4.8 Score

```bash
python -m skillsig sample --only spot_check
```

Compares the people's labels with the key. Needs `spot_A_done.xlsx`; uses B's sheet and the trailer check if present.

| Output | Contents |
|---|---|
| `s4_spot_precision.csv` | Per label, for v2.1, v2 and every baseline: of the skills the pipeline gave this label, how many the person agreed with |
| `s4_spot_reason_precision.csv` | The same per reason code |
| `s4_spot_confusion.csv` | Pipeline label × person's label |
| `s4_spot_agreement.json` | Agreement between A and B (Cohen's kappa) |
| `s4_spot_rater_confusion.csv` | A label × B label on the independently labelled overlap |
| `s4_spot_overlap_precision.csv` | Pipeline and baselines scored against each rater separately on the same overlap |
| `annotation/rater_disagreements.csv` | Private row-level review with original answers and notes; not adjudicated labels |
| `s4_spot_rater_pairs.csv` | With C's sheet: agreement, Wilson interval and kappa for every pair of raters, each on the skills both labelled |
| `s4_spot_rater_pair_confusion.csv` | With C's sheet: label × label counts for every pair |
| `s4_spot_rater_c_precision.csv` | With C's sheet: pipeline and baselines scored against C, on C's skills |
| `annotation/rater_c_disagreements.csv` | With C's sheet: private A–C and B–C disagreements with original answers and notes |
| `s4_trailer_precision.json` | Share of flagged trailers people confirmed |

## Result so far (person A only, scored 2026-10-27)

| v2.1 label | Person agreed | Share (95% interval) |
|---|---|---|
| `agent_assisted` | 25 of 30 | 83% (66–93) |
| `insufficient_evidence` | 15 of 29 | 52% (34–69) |
| `likely_human` | 6 of 30 | 20% (10–37) |
| `highly_templated` | 1 of 31 | 3% (1–16) |

- **Disclosure agreement:** A agreed with 22/27 labels carrying a first-commit Claude
  trailer. This measures agreement on visible evidence, not verified agent authorship.
- **Human is weak.** The person called 11 of the 30 "human" skills agent-assisted and 12 insufficient.
- **Templated is the biggest problem, but partly by design.** Most of these skills were templated through the `cluster` reason (confirmed 1 of 26). The sheet shows one skill at a time and hides copy counts, so a person can only recognise a copy if they already know the original. Low agreement here partly measures what the sheet can show, not only rule errors.

Person B's 40 answers are now included: 21 agreements and 19 disagreements, raw agreement
52.5% (Wilson 95% interval 37.5–67.1%), Cohen's kappa 0.288. The original scores above
still use A's 120 answers. On the common 40 items, v2.1 likely-human agreement is 2/10
against A and 6/10 against B. See [the annotation review](../../report/ANNOTATION_REVIEW.md).
Person C (Jayden) labelled 60 of the 120 skills, a spread that was not designed (21 agent,
15 templated, 15 insufficient, 9 human). C agrees with A on 33/60 (kappa 0.31) and with B on
14/21 (kappa 0.47). Against C, v2.1 agent-assisted holds 20/21, highly templated 0/15,
likely human 3/9 and insufficient evidence 4/15. Adding C leaves every A and B output
unchanged. No rater is ground truth, and no adjudication has been performed. The trailer
check remains outstanding.

## Design decisions

- **Split by label:** precision is wanted per label. A random sample would be 73% insufficient.
- **One per repository and cluster:** otherwise ten near-identical skills from one collection would count as ten judgements.
- **Blind:** people who see the pipeline's label tend to agree with it.
- **A second person:** if two careful readers disagree, the pipeline's score can't be read as accuracy.
- **A third person scored apart:** C's answers get their own files instead of being pooled or turned into a majority vote, so A's and B's results stay reproducible and nobody's answers are overwritten.
- **Precision, not recall:** the sample is drawn by pipeline label, so it can answer "when the pipeline says X, is it X?" but not "how many X does it miss?".
- **"Indicative":** 30 per label gives intervals about ±16 points wide. The numbers rank rules; they are not population estimates.

## Tests

`test_spot_check.py` (14): quotas, caps and determinism; one per repository and cluster; shortfalls move to the sibling group; sheets are blind and B is a subset of A; full bodies up to the Excel limit; scoring (precision, kappa, trailers); never overwrites; refresh keeps the sample. `test_rater_agreement.py` (8): pairs are matched by sample id, with shuffled rows and partial answers; blank, duplicate or unknown ids and invalid labels are rejected; scoring writes the overlap outputs and the private disagreement file; every pair of three raters is compared; adding C writes its own files and leaves A's and B's outputs byte-identical. `test_stats.py` (5): Wilson intervals and kappa.
