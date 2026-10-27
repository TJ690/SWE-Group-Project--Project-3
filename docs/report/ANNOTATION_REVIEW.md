# Independent annotation review — 2026-10-27

## Scope and preservation

A supplied 120 labelled skills; B supplied 40 of the same skills. C (Jayden) later
labelled 60 of A's 120 skills on an untouched copy of the blind A sheet; those 60 are
final. All IDs are unique, all labels use the four-category taxonomy, and B's IDs match
the assigned overlap. The scoring key matches the current v2.1 labels.

`results/annotation/spot_B_Terry.xlsx` was copied byte for byte to
`spot_B_done.xlsx`, and `spot_A_Jayden.xlsx` to `spot_C_done.xlsx`, the filenames the
scorer accepts. No annotator's original answers were edited, and A's and B's scored
outputs are unchanged by adding C. SHA-256 hashes of the original workbooks and the
canonical B and C copies are recorded in the private `results/annotation/independent_annotation_provenance.json`.
These are submitted independent answers under the annotation protocol; file inspection
cannot establish whether the annotators followed every part of that protocol.

## Inter-rater agreement

Source: [saved agreement](../../results/s4_spot_agreement.json).

| Measure | Result |
|---|---:|
| Skills labelled by A | 120 |
| Skills labelled by B | 40 |
| Paired, independently labelled skills | 40 |
| Agreements | 21 |
| Disagreements | 19 |
| Raw agreement | 52.5% |
| Wilson 95% interval for raw agreement | 37.5–67.1% |
| Cohen's kappa | 0.288 |

The interval describes raw agreement, not kappa. The small stratified overlap is
not a representative population sample. These numbers measure agreement on the
visible evidence, not agreement with verified authorship. Neither rater is treated
as established ground truth, and no adjudication has been performed.

### With a third rater

Source: [pairwise agreement](../../results/s4_spot_rater_pairs.csv). Each pair is
compared only on the skills both labelled.

| Pair | Shared skills | Agreements | Raw agreement (Wilson 95%) | Cohen's kappa |
|---|---:|---:|---:|---:|
| A and B | 40 | 21 | 52.5% (37.5–67.1%) | 0.288 |
| A and C | 60 | 33 | 55.0% (42.5–66.9%) | 0.308 |
| B and C | 21 | 14 | 66.7% (45.4–82.8%) | 0.465 |

C's 60 skills were not a designed subset: they are spread unevenly across labels (21
agent, 15 templated, 15 insufficient, 9 human), so C's results describe those skills,
not the whole sample. On the 21 skills all three labelled, all three agree on 10, and
at least two agree on 19. A third rater does not make any one reference correct; it
shows how much the reference itself varies.

## Which labels disagree?

Rows are A, columns are B. Source:
[rater confusion](../../results/s4_spot_rater_confusion.csv).

| A / B | Highly templated | Agent-assisted | Likely human | Insufficient evidence |
|---|---:|---:|---:|---:|
| Highly templated | 0 | 0 | 0 | 0 |
| Agent-assisted | 1 | 13 | 2 | 4 |
| Likely human | 1 | 0 | 3 | 3 |
| Insufficient evidence | 0 | 2 | 6 | 5 |

A assigned no templated labels on the overlap; B assigned two. This does not prove
that the pipeline's copied skills are incorrect. The blind sheets omit copy counts
and paired source texts, so some copy-based decisions cannot be verified from them.

Review of the submitted notes suggests five discussion groups. These are descriptions
of the disagreements, not adjudicated explanations or error assignments:

- **7 cases:** B chose insufficient evidence while A chose agent-assisted (4) or
  likely human (3). B's notes cite unavailable commit history.
- **6 cases:** A chose insufficient evidence and B chose likely human. B's notes
  cite project-specific text, commit messages and absence of AI attribution.
- **2 cases:** A chose agent-assisted and B chose likely human. B's notes again
  cite project-specific content and a workflow without explicit AI attribution.
- **2 cases:** B chose highly templated while A chose agent-assisted or likely human.
  B's notes cite generic content and large collection repositories.
- **2 cases:** A chose insufficient evidence and B chose agent-assisted. B's notes
  cite an explicit Claude trailer or Replit Agent metadata.

A explicitly noted a language problem on two of the 19 disagreements. Missing notes
elsewhere prevent determining A's reasoning. Confidence ratings are self-reports and
cannot settle a disagreement.

A against C on their 60 shared skills (rows are A, columns are C; source:
[pairwise confusion](../../results/s4_spot_rater_pair_confusion.csv)):

| A / C | Highly templated | Agent-assisted | Likely human | Insufficient evidence |
|---|---:|---:|---:|---:|
| Highly templated | 0 | 2 | 1 | 0 |
| Agent-assisted | 1 | 20 | 4 | 4 |
| Likely human | 0 | 3 | 3 | 3 |
| Insufficient evidence | 1 | 3 | 5 | 10 |

As with A and B, agent-assisted is the label the readers share most; likely human and
insufficient evidence are where they split.

## Pipeline agreement depends on the reference

The original `s4_spot_precision.csv`, `s4_spot_reason_precision.csv` and
`s4_spot_confusion.csv` remain comparisons against **A's 120 answers**. They are not
pooled across annotators or replaced by a consensus.

The new [overlap precision table](../../results/s4_spot_overlap_precision.csv) compares
each pipeline version and baseline against A and B separately on the **same 40 items**.
For v2.1:

| Pipeline label | Agrees with A | Agrees with B |
|---|---:|---:|
| Highly templated | 0/10 | 0/10 |
| Agent-assisted | 9/10 | 10/10 |
| Likely human | 2/10 | 6/10 |
| Insufficient evidence | 4/10 | 3/10 |

Against C, on C's 60 skills ([C precision](../../results/s4_spot_rater_c_precision.csv)),
with A on the same skills for comparison:

| Pipeline label | Agrees with C | Agrees with A (same skills) |
|---|---:|---:|
| Highly templated | 0/15 | 1/15 |
| Agent-assisted | 20/21 | 18/21 |
| Likely human | 3/9 | 2/9 |
| Insufficient evidence | 4/15 | 7/15 |

Wilson intervals are included in the CSVs. Ten to twenty predicted cases per category
give wide intervals. The likely-human comparison changes substantially with the rater; agent
disclosures are more consistently recognized on this overlap. These are descriptive
sample results, not verified authorship accuracy or statistically established rankings.

## Review procedure

1. Keep the original `_done.xlsx` workbooks unchanged. The scorer validates IDs and
   labels, and rejects duplicate IDs, unassigned rows and unknown nonblank labels.
2. Review the 19 A–B disagreements in the private
   `results/annotation/rater_disagreements.csv`, and the 34 involving C (27 with A, 7
   with B) in `rater_c_disagreements.csv`. Both contain pipeline labels and original
   notes, and must not be presented as a new blind annotation exercise.
3. If the team discusses cases, save decisions in a separate adjudication file with
   `sample_id`, `adjudicated_label`, `rationale`, `reviewer` and `review_date`. Do not
   overwrite either independent answer or report post-discussion agreement as independent.
4. Separate disagreements about reading visible evidence from disagreements caused
   by missing evidence or ambiguous category definitions. Any revised protocol needs
   a fresh evaluation; it does not retroactively validate these annotations.
5. The 50-message trailer check remains outstanding. The scorer reports no trailer
   precision until its completed sheet is available.

## Reproduce

From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m skillsig evaluate --only spot_check
```

The canonical completed workbooks (`spot_A_done.xlsx`, and `spot_B_done.xlsx` and
`spot_C_done.xlsx` when available) and `key.parquet` must be present in
`results/annotation/`. Annotation sources and the row-level review stay git-ignored;
back them up with access appropriate for the research data. Aggregate result tables
and their manifests can be versioned with this report.
