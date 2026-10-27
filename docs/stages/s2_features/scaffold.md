# 2.9 Scaffold signals (`scaffold.py`)

**Command:** `python -m skillsig features --only scaffold` · **Tests:** `tests/s2_features/test_scaffold.py` · [Back to s2](README.md)

## What it does

Detects **structural** templating: skills generated from one scaffold and then filled with new wording. The copy, cluster and registry rules compare body text, so they miss these. This step compares structure instead, and gives each skill at most one `scaffold_basis`.

## How it works

Six signals, each counted in **distinct owners** (the part of `owner/repo` before the slash):

| Signal | How | Column(s) |
|---|---|---|
| **A. Placeholder residue** | Unfinished-authoring markers outside code fences: `[TODO …]`, `[TBD …]`, `[FIXME …]`, `[placeholder …]` (not links), lines starting `TODO:` / `TBD:` / `FIXME:`, `<placeholder>`, `replace this`, `lorem ipsum`, `skill-name-here`, `your-skill-name`, `my-skill-name` | `placeholder_count` |
| | Generator markers: seven phrases from Anthropic's `skill-creator/scripts/init_skill.py` template (commit `1ed29a03`, the last version before the script was removed), e.g. "[TODO: 1-2 sentences explaining what this skill enables]", "## Structuring This Skill" | `placeholder_generator` |
| **B. Heading outline** | The list of headings as `level:text`, lower-cased, with the skill's own name masked to `<name>`, then hashed | `outline_key`, `outline_len` |
| **B2. Outline family** | Near-identical outlines: MinHash LSH over each outline's heading set (threshold 0.6) gives candidates; inside each bucket, leader clustering joins an outline to the first leader with heading-sequence similarity ≥ 0.8 (1 − edit distance / longer length). Buckets larger than 5,000 are skipped as generic; outlines under 4 headings stay alone | `outline_family_id`, `outline_family_owners` |
| **C. Front-matter keys** | Ordered keys, e.g. `name\|description\|license` | `fm_key`, `fm_key_owners` |
| **D. Folder layout** | Sorted top-level siblings of the skill's folder, e.g. `references/ scripts/ readme.md` | `layout_key`, `layout_owners` |
| **E. Masked body** | The body with names, repository, URLs, paths and numbers masked, then hashed: the same text with different names ("Type-2 clone") | `masked_sha`, `masked_owners` |
| **F. Shared lines** | Every normalised body line of ≥ 20 characters is hashed; a line is **shared** when ≥ 5 owners have it. `shared_line_frac` = shared lines / lines; `customization = 1 − shared_line_frac` | `line_count`, `shared_line_frac`, `customization` |

**Basis**, first match wins:

| Order | `scaffold_basis` | Fires when |
|---|---|---|
| 1 | `generator` | A skill-creator marker is present |
| 2 | `placeholder` | ≥ 1 placeholder (`scaffold.placeholder_min`) |
| 3 | `masked_copy` | Masked body shared by ≥ 10 owners (`thresholds.templated_copies_min`) |
| 4 | `shared_lines` | ≥ 3 lines and `shared_line_frac ≥ 0.6` (`scaffold.shared_frac_min`) |
| 5 | `outline` | Outline of ≥ 4 headings, not on `scaffold.outline_stoplist`, family spans ≥ 5 owners, **and** either front-matter keys beyond `name`/`description` shared by ≥ 5 owners, or a folder layout shared by ≥ 5 owners |

The run has three passes: per-skill signals and line hashes written in chunks; owner counts per key, line frequencies and outline families; then the basis.

## Outputs

`src/data/derived/s2_scaffold.parquet`: 1,840,872 rows, columns `file_sha, owner, placeholder_count, placeholder_generator, outline_key, outline_len, outline_family_id, outline_family_owners, fm_key, fm_key_owners, layout_key, layout_owners, masked_sha, masked_owners, line_count, shared_line_frac, customization, scaffold_basis`.

| `scaffold_basis` | Skills |
|---|---|
| `shared_lines` | 224,893 |
| `masked_copy` | 36,494 |
| `outline` | 21,067 |
| `placeholder` | 9,258 |
| `generator` | 191 |
| none | 1,548,969 (84%) |

A typical skill is highly customised: median `shared_line_frac` 0.044 (customisation 0.956). 9,401 skills have at least one placeholder.

`results/s2_scaffold_top_outlines.csv`: the 200 most common outlines (`outline_key, outline_text, outline_len, owners, skills, outline_family_id, family_owners, example_file_sha`), for a person to review and add generic ones to `scaffold.outline_stoplist` (currently empty).

`winnow()` and `shared_passages()` (Schleimer, Wilkerson & Aiken 2003) find the passages two texts share; 4.5 uses them to show evidence on its spot-check sheet.

## Design decisions

- **Why owners, not files or repositories?** One author's house style repeated across their own repositories is not a template from elsewhere.
- **Why mask names before hashing?** A scaffold filled in with a different skill name is still the same scaffold.
- **Why line frequency?** It is the standard site-template detection idea (boilerplate lines recur across pages; Vieira et al. 2006; Kohlschütter et al. 2010), applied to skills.
- **Why does the outline rule need extra support?** Many independent authors write `## Overview / ## When to use / ## Examples`; an outline alone is weak. Requiring a shared non-generic front-matter key set or folder layout as well cuts generic outlines.
- **Why did the placeholder list drop `{{var}}`, `<skill-name>`, `[your …]` and "replace with"?** On the dev sample those were mostly runtime parameters the agent or user is meant to fill (`skills/<skill-name>/SKILL.md`), not unfilled scaffolds.
- **Why not in the label yet?** The signals would add about 113,000 templated skills. 4.5 first checks what they add, their recall on known copies (97.4%), and whether the outline rule secretly measures agent style (it does not: 0.89% outline hits among disclosed-agent skills vs. 1.17% among the rest).

## Full data vs dev sample

In dev mode every count is taken **inside the sample only**: owners per outline family, front-matter keys, layout and masked body, and shared-line frequency. Spread counts are far smaller than in the full data, so most bases fire less. A dev run checks that the code works, not what the full result will be.

## Tests (14)

- Placeholders skip code fences and links; generator markers are named.
- Outlines are canonicalised; the masked hash matches renamed copies; line hashes mask names and skip short lines; layout keys.
- Outline distance and similarity; families join near outlines, keep far ones apart, and skip oversized buckets.
- Winnowing finds a planted shared passage.
- Basis order is first match; a run writes features and skips when unchanged; the module is reachable from the CLI.

## Limits

- `shared_lines` dominates (225k) and overlaps heavily with existing cluster matches (see 4.5); its precision has not been checked by people yet.
- `outline_stoplist` is empty, so generic outlines are excluded only by the bucket-size cap and the support rule.
