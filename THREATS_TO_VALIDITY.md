# Threats to Validity

Here is what limits our study. Our labels are clues about who wrote a skill, not confirmed facts.

1. We can't see who actually wrote a skill, so our labels are only clues, and "likely human" just means we didn't spot any sign of AI.
2. An AI note in a commit might only mean someone installed the skill, and some tools don't leave a note at all.
3. Finding the same skill in lots of places doesn't mean it came from a template.
4. Only about one in four skills has commit history, and most of those come from early adopters and Claude Code users.
5. A 120-skill blind spot-check, a second rater on 40 overlapping skills and a third
   rater on 60 skills now exist. A and B agree on 21/40 (52.5%, kappa 0.288), A and C on
   33/60 (55.0%, kappa 0.308), and B and C on 14/21 (66.7%, kappa 0.465). These are judgments
   of visible evidence, not verified authorship. Precision against either rater is
   indicative, and a single-rater reference is uncertain. The blind sheets omit copy
   counts and paired source texts, limiting validation of copy-based templated labels.
   See [the annotation review](docs/report/ANNOTATION_REVIEW.md).
6. A shared heading outline may come from AI writing style or from best-practice guides rather than a template, so the scaffold outline signal needs a stoplist and the agent-confound check.
7. Copying and agent involvement are not mutually exclusive. The historical label
   precedence hides some disclosures in templated content. The independent evidence
   report retains both dimensions and treats co-occurrence as expected, not a conflict.
   Exact sharing does not identify copying direction; near-duplicate components may
   chain through candidate links; scaffold signals remain incompletely validated.
   The old annotation scores do not validate the new two-dimensional evidence report.
8. Near registry matches and generic clusters share a similarity mechanism. Agreement
   on near matches is not independent recall on verified copies. Exact-match recovery
   is reported separately, and neither positive-only reference establishes overall
   precision. The evidence-aware paired review supplies comparison texts and allows
   uncertain judgments; it remains separate from the original blind annotations.
9. Text-model AUC measures proxy-label discrimination. Nonrandom disclosure and
   label noise can bias it in either direction; no guaranteed lower bound on true
   authorship performance is established. Precision and coverage depend on the
   assumed class prior and have not been validated in the no-history population.
10. Similar agreement estimates for full and trailer-only rules do not establish
    equivalence or show whether a trailer describes writing, installation, or maintenance.
    The predicted subsets differ, and true workflow roles have not been independently verified.
11. Threshold sensitivity currently measures aggregate shares. Opposing item-level
    changes can cancel; individual stability and threshold interactions are unmeasured.
12. A 12-skill pilot, labeled from skill text alone and without written rules, had an agreement of 5 of 12 (41.7%, kappa 0.18). It tested the labeling approach and cannot be compared with the spot check, which used different rules, evidence, and samples.
