# Sprint 2 Retrospective

## What went well
- We turned the Sprint 1 notebooks into a tested, staged Python package (`skillsig`). It reproduces the Sprint 1 label counts exactly, and every output records the inputs and settings that made it.
- Near-duplicate detection ran on the full population of 1,840,872 readable skills, not just a sample.
- Mining 31 skill collections gave us a set of known copies to check the templated rules against. Copy count or cluster membership recovers 87.2% of 6,481 exact registry copies.
- Three people labelled a blind sample, so we measured agreement between readers instead of trusting a single one.
- The report figures are drawn by code from saved results, so they can be regenerated.

## What was challenging
- We had to find the signals step by step. Each round of results showed which clues were missing, so we added them one at a time: registry matches, near-duplicate clusters, then scaffold leftovers.
- Turning what we recognise as a template in a SKILL.md into code was hard. A person can see that a skill was built from a template at a glance, but writing rules that catch the same thing without flagging ordinary skills took several attempts.
- Hand labelling was slower than coding and became the bottleneck. The trailer check and the template-pair review are still not done.
- The readers often disagreed (kappa 0.29 for A and B). The blind sheet hides copy counts and source texts, so some copy-based labels cannot be judged from it.
- The near-duplicate run over 1.8 million texts took hours, and many commits went in at once instead of through smaller reviewed pull requests.

## What we learned about the data/problem
- Agent disclosures are the most dependable signal. Readers agreed with most agent-assisted labels.
- "No AI trailer" is not evidence of human authorship. The likely-human label was rarely confirmed by readers, so it stays unvalidated.
- Reuse and agent use are not exclusive. A copied skill can also carry an agent trailer, and 36.1% of skills with a disclosed agent also show reuse.
- Text-only methods find some signal (AUC 0.71 against proxy labels), but not enough to label most skills with high precision.
- Label shares are fairly stable when one cutoff changes, but we have not yet checked whether individual skills change label.

## What changed in our back log/plan as a result
- The plan grew from "hand-label 200–300 skills" to a 120-skill blind sample with three readers, an agreement review, and a separate paired review for template copies.
- We added a registry-mining stage so the templated rules could be checked against known copies.
- The single-label view was joined by an evidence view that reports reuse and agent disclosure separately.
- We planned RQ2 (how skills are maintained and how updates spread to copies) as a logical extension of RQ1, since it builds on the same registry matches. It was a stretch goal and we could not complete it this sprint. The provenance web app also moved to stretch work so that validation could come first.

## Action items for Sprint 3
- Finish the trailer check, the template-pair review and C's remaining 60 skills.
- Review the reader disagreements and store any agreed decisions separately from the original answers.
- Add repository creation date as a baseline, and run the ablation and a subgroup comparison.
- Write the complete report draft and expand the threats to validity.
- Use smaller pull requests, each reviewed by another team member, and tag a release.
