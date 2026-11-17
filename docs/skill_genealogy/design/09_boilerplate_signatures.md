# Design: boilerplate lines and signatures (stage 5a)

Card G12 · Code: `find_copies.py` steps 0–3 · [Code flow](../code_flow/09_boilerplate_signatures.md) · [Report](../report/09_boilerplate_signatures_report.md)

## Problem

Stage 5 looks for copies among 1.6 million English dataset texts and 817,485 registry versions. Comparing every pair is impossible, and comparing raw texts is unfair: thousands of unrelated skills share template lines (`## when to use this skill`, stock sentences from a skill generator), so two skills from one template look like copies of each other.

## Design at a glance

Find the lines that many unrelated owners share (**boilerplate**) and remove them. Then summarise each text as a **MinHash signature** of its 5-word shingles (128 numbers plus the shingle count), once, so stages 5b and 5c can estimate similarity quickly.

## Decisions

**Boilerplate is a line many owners share.** A line is boilerplate when at least 50 distinct GitHub owners have it in an English text (`BOILERPLATE_MIN_OWNERS`). Owners, not files or repos, are counted, because one author pastes the same line into many of their own skills; that is not a shared template. Fifty independent owners writing the same line of at least 3 words is far beyond chance. The top 30 lines and the lines at the cutoff are printed so a person can check that they are template text.

**Lines of fewer than 3 words are never boilerplate.** A heading such as `## usage` says nothing about a template and adds few shingles.

**Owners come from every file of a text.** The dataset stores `content` once per distinct text, but the same text sits in many repos. The owners of a text come from every artifact row with its sha; counting only rows with content finds about a third of the owners.

**The line key is the one the signature removes.** Both use the same code (`normalize_text`, body without front matter, stripped, lowercase), so a line counted is exactly a line removed.

**MinHash with 128 values and 5-word shingles.** A 5-word phrase is specific enough that sharing many of them means shared text. 128 values give a Jaccard estimate within about ±0.09 (two standard deviations), and close calls are checked with a line diff in stage 5c. The shingle count is stored with each signature, because stage 5c needs it to turn Jaccard into **containment** (how much of a registry text is inside a copy).

**Sign each distinct text once.** The same text in 1,000 repos has one signature, keyed by `file_sha`; stage 5c maps it back to files.

**An empty text has no signature.** A text with no shingle left after boilerplate removal is counted, not stored. Two empty sets would have Jaccard 1.0 and look like perfect copies.

**Resumable chunks, fixed order.** Dataset texts are signed in 4 hash slices and chunks of 50,000, each written to its own file under a temporary name and then renamed. A stopped run resumes at the next missing chunk. The dataset signatures and boilerplate list do not depend on the registry list, so dev and full share them.

**Memory: never explode texts in SQL.** Exploding every text into lines in one query spilled more than 100 GB. Owner pairs are built in batches of 2,000 texts and counted on a fresh connection.

## Alternatives rejected

- **TF-IDF over whole texts.** Needs a vocabulary of 2.4 million texts, and template words still weigh in. (An optional per-part TF-IDF is card G30.)
- **Removing lines by file count.** One prolific author would make their own lines boilerplate.
- **Embeddings.** Measure topic, not copying: two different PDF skills are close in meaning without sharing text.

## Effect

547,906 boilerplate lines (median 115 owners). 1,629,358 dataset signatures (3,927 empty) and 816,534 registry signatures (951 empty).

## Limits

- A line copied into 50 owners' skills is removed even when it is real content of one popular skill; the copies still match on their other lines.
- The cutoff is a count over this dataset, not a share; a larger dataset needs it checked again.
- MinHash is an estimate (one standard deviation about 0.044).
