# 2.5 Wording (`wording.py`)

**Command:** `python -m skillsig features --only wording` · **Tests:** `tests/s2_features/test_wording.py` · [Back to s2](README.md)

## What it does

Measures how each readable skill is written: instruction style, address, readability, vocabulary, emphasis, uniformity and rhythm. Also tags how the description opens.

## How it works

Front matter is removed with the same pattern as the SQL in P.1, then each body is measured in a process pool (one worker per CPU). Words are letter/digit tokens (`it's` is one word), lower-cased.

| Column | Measure | Definition |
|---|---|---|
| `w_imperative_ratio` | Instruction style | Share of non-empty lines whose first word (after a list marker) is one of ~130 instruction verbs (*add, check, create, run, use, verify, …*) |
| `w_you_per_100w` | Second person | The words "you" and "your" per 100 words |
| `w_command_density` | Shell commands | Non-empty lines inside shell-language fences (`bash`, `sh`, `zsh`, `shell`, `console`, `powershell`, …) plus `$ ` prompt lines outside them, per 1,000 characters |
| `w_fk_grade` | Readability | Flesch–Kincaid grade: `0.39 × words/sentences + 11.8 × syllables/word − 15.59` (syllables = vowel groups, silent final *e* dropped) |
| `w_mattr500` | Vocabulary variety | Moving-average type–token ratio over 500-word windows (one window for shorter texts) |
| `w_emphasis_per_k` | Emphasis | `MUST`, `NEVER`, `ALWAYS`, `IMPORTANT`, `CRITICAL`, `REQUIRED` (upper case) per 1,000 characters |
| `w_uniformity_bullet_cv` | Bullet uniformity | Coefficient of variation of bullet lengths; 0 with fewer than 2 bullets |
| `w_uniformity_heading_entropy` | Heading spread | Shannon entropy (bits) of heading-level counts |
| `w_phrase_hits` | Phrase lexicon | Always 0 here: the lexicon is fitted inside training folds in 3.5 |
| `w_non_english` | Language flag | 1 when more than 20% of characters are non-ASCII |
| `w_sentence_cv` | Burstiness | Coefficient of variation of sentence lengths in **prose** (code, tables, headings and list markers removed); 0 below 5 sentences |
| `w_prose_sentences` | Prose amount | Number of prose sentences |
| `w_emdash_per_1kw` | Em-dashes | `—` per 1,000 prose words |
| `desc_idiom` | Description opening | `use_this_skill_whenever`, `use_when`, `guide_for`, `capability_summary`, `other` (same function as 1.2) |

## Output

`src/data/derived/s2_wording.parquet`: 1,840,872 rows (readable skills), the columns above plus `file_sha`.

**What a typical skill looks like.** The median is the middle value across the 1,840,872 readable skills: half of skills score higher, half lower.

| Measure (column) | Median | In plain words |
|---|---|---|
| Instruction style (`w_imperative_ratio`) | 0.097 | About 1 line in 10 starts with an instruction verb |
| Second person (`w_you_per_100w`) | 0.07 | "You" is rare: well under 1 per 100 words |
| Readability (`w_fk_grade`) | 9.5 | Readable at about US school grade 9–10 |
| Vocabulary variety (`w_mattr500`) | 0.512 | In any 500-word stretch, about half the words are different words |

How many skills have each property:

| Property (column) | Skills | Share |
|---|---|---|
| Flagged non-English (`w_non_english = 1`) | 135,960 | 7.4% |
| Enough prose to measure burstiness (`w_prose_sentences ≥ 5`) | 1,790,676 | 97.3% |

How the descriptions open (`desc_idiom`, same categories as [step 1.2](../s1_registry/versions.md), checked in this order):

| Opening | Skills | Share |
|---|---|---|
| `other` | 1,270,420 | 69.0% |
| `capability_summary` | 377,236 | 20.5% |
| `use_when` | 176,439 | 9.6% |
| `guide_for` | 13,139 | 0.7% |
| `use_this_skill_whenever` | 3,638 | 0.2% |

## Design decisions

- **Why these measures?** Each targets a claim made about LLM-written instructions: more imperatives and second person, uniform bullet lengths, low sentence-length variation, emphasis words, em-dashes. Measuring them lets 3.5 and 3.6 test the claims instead of assuming them. (3.6 finds that a fixed "AI phrase" list scores no better than chance.)
- **Why rates, not raw counts?** Long skills have more of everything; rates make short and long skills comparable.
- **Why measure rhythm and em-dashes on prose only?** Code, tables and headings are not sentences; including them would make every code-heavy skill look "bursty".
- **Why zeros instead of NaN for empty input?** Models need complete rows; a body with no bullets has zero bullet variation by definition.
- **Why no fitted lexicon here?** A word list fitted on all skills and then used to evaluate them would leak labels into the feature. 3.5 fits it inside each training fold.
- **Why share `desc_idiom` with 1.2?** One definition lets registry versions and dataset skills be compared directly.

## Full data vs dev sample

Dev mode measures only the sampled readable skills; each skill's values are the same as in a full run.

## Tests (11)

- A three-line fixture matches hand-computed values; MATTR averages each 500-word window; an empty body gives zeros, not NaN; the non-English flag is "ratio above 0.2".
- Burstiness is the variation of prose sentence lengths; uniform sentences give 0; the em-dash rate counts prose only.
- The description idiom matches the 1.2 function (parametrised).
- A run keeps readable sampled rows and skips when unchanged; missing inputs name their commands.

## Limits

- English-centred: the verb list, syllable rule and readability grade assume English; 7.4% of skills are flagged non-English.
- The instruction-verb list is fixed; verbs outside it are not counted.
- Not used by the v2.1 label, only by the text models.
