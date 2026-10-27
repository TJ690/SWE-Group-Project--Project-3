# Annotation guidelines: spot-check of authorship labels

These guidelines explain how to label the skills in `results/annotation/spot_A.xlsx` and `spot_B.xlsx`. They expand the short codebook on each workbook's Instructions sheet. Where the two differ, follow this document.

## 1. What you are doing and why

The pipeline gives every skill one of four labels. To learn how often those labels are right, two people label a sample of skills **by hand, without seeing the pipeline's answer**. The scoring script then compares your labels with the pipeline's and reports precision per label, plus agreement between the two of you.

Your labels are the reference the pipeline is measured against. Consistency matters more than speed. Two careful people applying the same rules should reach the same answer.

**Label the visible evidence, not the true author.** Nobody can know for certain who wrote a file. The question is never "who really wrote this?". It is always "what does the evidence on this row show?".

### How the sample was chosen, and why this size

`python -m skillsig sample` (step 4.6) draws the rows. You don't need this to label, but it explains what the scores can and cannot say.

- **30 skills per pipeline label, 120 in all.** Precision is wanted for each label. A plain random sample would be 73% `insufficient_evidence` and hold only a handful of the rarer labels.
- **Two groups of 15 inside each label**, aimed at the parts most likely to be wrong: registry copies vs. cluster-or-copy-count templated; agent labels added by v2.1 vs. already agent in v2; revised vs. unrevised human; insufficient with vs. without history.
- **At most one skill per repository and per near-duplicate cluster**, and a body of at least 200 characters. Otherwise ten near-identical skills from one collection would count as ten judgements.
- **Person B labels 40 of the 120** (10 per label, the same rows as A) so agreement between two people can be measured on the same items.
- **An optional person C** labels any subset of a fresh copy of A's sheet and saves it as `spot_C_done.xlsx`. C is scored in separate files, against A and B on the skills they share; C's answers never replace A's or B's. *(Added 2026-10-27, after a third rater joined once A and B had finished; not part of the original sampling design.)*
- **Why 30 per label.** With 30 rows, a 95% interval on precision is about ±16 points. That is enough to tell a rule that works (around 80%) from one that doesn't (around 20%), which is the decision the scores support; narrowing it to ±10 points would need roughly 60 to 100 rows per label, depending on the rate. The scores rank rules; they are not population estimates.
- **Precision, not recall.** Rows are drawn by pipeline label, so the sample answers "when the pipeline says X, is it X?" but not "how many X does it miss?".

Full design notes: [docs/stages/s4_validation/spot_check.md](stages/s4_validation/spot_check.md).

## 2. The workflow

| Step | Person A | Person B |
|---|---|---|
| Open | `results/annotation/spot_A.xlsx` (120 rows) | `results/annotation/spot_B.xlsx` (40 rows) |
| Fill in | `your_label`, `your_confidence`, `notes` (the last three columns) | same |
| Save as | `results/annotation/spot_A_done.xlsx` | `results/annotation/spot_B_done.xlsx` |

- B's 40 rows are also in A's sheet, in a different order. **Do not discuss any row with the other person until both sheets are saved as `_done`.** Agreement between you is one of the results.
- **Do not search GitHub, the web, or the dataset** for a repository, skill, or author. Judge from the row only. Looking things up breaks the blind design and makes the two of you inconsistent.
- You may sort or filter rows while working; scoring matches rows by `sample_id`. Do not add, delete, or rename rows or columns, and do not edit any column other than the last three.
- Plan for roughly 2 to 3 minutes per row (about 4 to 6 hours for A, 1.5 to 2 hours for B). Work in sessions of an hour or less. Labels drift when you are tired.
- After both sheets are saved, run `python -m skillsig evaluate`. If you disagree on rows afterwards, discuss them, but **do not change the `_done` files**. The first, independent answers are the data.
- Scoring writes a private `results/annotation/rater_disagreements.csv` with both original
  answers and notes. Any later adjudication belongs in a separate file with the sample ID,
  adjudicated label, rationale, reviewer and date. Do not replace independent agreement
  with agreement after discussion. The [annotation review](report/ANNOTATION_REVIEW.md)
  records the current results and the limits of the reference labels.

## 3. What each column tells you

| Column | What it shows | How to use it |
|---|---|---|
| `file_sha` | The skill's content hash (its identity in the dataset) | **Not evidence.** It lets the team check a row against the full text in the dataset after labelling. Do not use it to look anything up while labelling. |
| `repo_full_name` | `owner/repo` | The repo name can hint at a collection (`awesome-skills`, `agent-skills`). |
| `repo_context` | stars; language; repo creation date; number of `SKILL.md` files in the repo | Hundreds or thousands of `SKILL.md` files usually means a collection or mirror, not a project's own skill. |
| `skill_name`, `description` | Front-matter fields | Check whether the description is specific to a project or generic. |
| `body_excerpt` | The full skill text after the front matter, up to 32,000 characters (Excel's cell limit) | The main evidence for templating and project specificity. Click the cell and widen the row or use the formula bar to read long skills. |
| `body_chars` | The full length of the skill text, in characters | If it is above 32,000, `body_excerpt` shows only the first 32,000 characters. |
| `commit_count`, `first_commit_at`, `last_commit_at` | How many commits touched the file, and when | Several commits over days or weeks suggest ongoing human work. **Blank means no history was collected. It does not mean no history exists.** |
| `first_commit_message`, `last_commit_message` | Messages, with emails and personal names redacted | AI co-author lines (`Co-Authored-By: Claude …`) and "Generated with …" lines are kept and are the strongest agent evidence. |

About a third of the rows have no commit information. For those, only the text and repo context are available.

Two things can look like missing content but are expected:
- **Very long skills are cut at 32,000 characters.** About 2 in 100 skills are longer; `body_chars` shows when this happened. Judge from what is shown; if the cut-off part might change your label, lower your confidence and say so in `notes`.
- **An empty `skill_name` or `description` is empty in the source too.** Some skills have no front matter, or front matter without these fields. When there is no front matter, the excerpt starts with the skill's first line.

## 4. The four labels

### `highly_templated`
The text is a **near-copy** of a skill you recognise, or it clearly comes from a **published collection or scaffold**, with **little project-specific content**.

Signs:
- You recognise it as a well-known skill (for example a popular debugging, TDD, or "skill-creator" skill), possibly with the name changed.
- Unfilled template text: `[TODO: …]`, "Replace with …", "Delete this section when done", lorem ipsum.
- Generic content that would fit any project, in a repo with very many `SKILL.md` files.
- Commit messages that say the file was installed, synced, imported, or copied (`sync skills from …`, `install pack`).

### `agent_assisted`
There is **visible evidence that an AI tool took part**:
- An AI co-author or generator line in either commit message: `Co-Authored-By: Claude …`, `Co-authored-by: Cursor …`, `Generated with [Claude Code]`, `Copilot`, `Codex`, and similar. Either the first or the last commit counts.
- A commit message that explicitly says an agent produced it ("generated by Claude", "created with Codex").
- A **bulk commit** that adds many skills at once ("add 25 skills"), **together with** uniform, freshly written text. Bulk alone is weak (see the edge cases).

### `likely_human`
**All three** must hold:
1. The text is **specific to its project**: real paths, commands, team conventions, domain details.
2. The history shows **ordinary human work**: commit messages that read like a person's, ideally more than one commit spread over days.
3. There is **no AI marker** in either commit message.

`likely_human` means "the evidence looks like human work and shows no AI sign". It does not mean proven human.

### `insufficient_evidence`
The evidence is **missing or points both ways**. This is a valid, expected answer, not a failure. Use it whenever none of the three above clearly fits. A large share of rows will end up here, and that is fine.

## 5. How to decide (follow this order)

Go down the list and stop at the first answer that fits.

1. **Is the text a near-copy, an unfilled scaffold, or generic collection material?** Then `highly_templated`.
   *This wins even if a commit has an AI trailer.* The label describes where the text came from. Mention the trailer in `notes`.
2. **Is there a visible AI marker in a commit message, or an explicit agent statement?** Then `agent_assisted`.
3. **Is the text project-specific, with human-looking history and no AI marker?** Then `likely_human`.
4. **Otherwise:** `insufficient_evidence`.

## 6. Confidence (1 to 5)

Confidence is about how clearly the evidence supports **your** label, not how sure you are about the true author.

| Score | Meaning | Typical case |
|---|---|---|
| 5 | You would bet on it | A `Co-Authored-By: Claude` line; or a skill you recognise word for word |
| 4 | Fairly sure | Clear evidence with one small doubt (a trailer only on the last commit; a near-copy with a few edits) |
| 3 | More likely than not | The evidence leans one way but is indirect (generic text in a large collection, no message) |
| 2 | Leaning | One weak cue (a bulk "add skills" commit with no trailer; a single human-looking commit) |
| 1 | A guess | You chose a label but could argue for another |

For `insufficient_evidence`, use confidence to say how sure you are that the evidence really is insufficient: 5 = nothing to go on, 1 = it almost fit another label.

## 7. Edge cases

| Situation | Label | Confidence | Why |
|---|---|---|---|
| AI trailer on the **last** commit only | `agent_assisted` | 3–4 | An agent took part in at least one edit |
| AI trailer, but the text is a copy of a known skill | `highly_templated` | as usual | Rule 1 wins; write "trailer: Claude" in notes |
| Bulk commit ("add 40 skills"), no trailer, fresh uniform text | `agent_assisted` | 1–2 | Bulk alone is weak evidence |
| Bulk commit that says install / sync / import / copy | `highly_templated` if the text is generic, else `insufficient_evidence` | 2–3 | Copied in, not written there |
| Commits by an automation bot (`github-actions`, `dependabot`, a sync bot) | not agent evidence; decide from the text | — | Bots that move files are not coding agents |
| Text reads "AI-like" (emoji headings, "comprehensive", tidy bullet lists) but no marker | `insufficient_evidence` | 2–3 | **Writing style alone is never enough** for `agent_assisted`; mention it in notes |
| Project-specific text, **one** commit with a normal human message, no marker | `likely_human` | 1–2 | One commit is thin history |
| Project-specific text, **no** commit information | `insufficient_evidence` | 3–4 | Rule 3 needs history |
| Runtime fill-ins like `[WHAT]`, `{{project_name}}`, `<skill-name>` | not template residue | — | They are meant to be filled at use time; judge the rest of the text |
| Unfilled `[TODO: …]` or "Replace with …" in prose | `highly_templated` | 3–5 | Scaffold left unfinished |
| Very short skill (a few lines) with no history | `insufficient_evidence` | 4–5 | Too little to judge |
| Text not in English | Apply the same rules | lower by 1 if you cannot read it well | Note the language |
| "Initial commit" / "first commit" covering the whole repo | No history evidence either way | — | Treat like a single commit |

## 8. Writing notes

Notes are optional but make disagreements easy to resolve later. Keep them short and name the evidence you used:

- `trailer: Claude Opus in first commit`
- `near-copy of the popular systematic-debugging skill`
- `unfilled [TODO] in Overview`
- `project-specific paths; 4 commits over 3 weeks`
- `no history; generic text; 900 SKILL.md files in repo`
- `AI-like style but no marker`

## 9. Trailer check (`trailer_check.csv`)

A separate 50-row sheet. For each commit message, decide whether the AI line in it is **a real disclosure that an AI tool took part**, and fill `is_ai_disclosure` with:

- `yes`: a co-author or "Generated with" line naming an AI tool (`Co-Authored-By: Claude …`).
- `no`: the AI name appears for another reason, for example the commit adds a skill *about* Claude, or mentions a model in passing.
- `unsure`: you cannot tell.

Save it as `results/annotation/trailer_check_done.csv`.

## 10. Quick reference

```
1. Near-copy / unfilled scaffold / generic collection text?  -> highly_templated
2. AI co-author or "Generated with" line, or explicit agent?  -> agent_assisted
3. Project-specific + human-looking history + no AI marker?  -> likely_human
4. Otherwise                                                 -> insufficient_evidence

Style alone is never agent evidence.  Blank history is not "no history".
Do not look anything up.  Do not discuss rows until both sheets are done.
```
