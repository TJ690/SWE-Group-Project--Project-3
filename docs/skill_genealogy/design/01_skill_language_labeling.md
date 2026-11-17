# Design: language labels (stage 2a)

Card G07 · Code: `select_registries.py`, `text_helpers.detect_language` · [Code flow](../code_flow/01_skill_language_labeling.md) · [Report](../report/01_skill_language_report.md)

## Problem

The study compares skill texts to find copies. A German translation of an English skill shares almost no words with it, so it would look like a new skill, and a "same skill" or "copy" decision across languages cannot be checked line by line. Every distinct text (1,877,981) therefore needs one language label, made once and used by every later stage.

## Design at a glance

Each distinct `file_sha` gets `language`, `is_english` and `body_chars`. The body is cleaned (no front matter, no code blocks), and only its first 2,000 characters are read. A published detector (lingua) chooses among 21 candidate languages. A script share rule decides mixed texts. `en` and `unknown` count as English.

## Decisions

**A published detector, not a stop-word test.** Skill texts are short and full of code names. A stop-word count labelled a German paragraph English and terse English steps as another language. Lingua is built for short texts and returns a confidence per language. The version is pinned (2.2.0) so labels do not drift between runs.

**Read the body, not the header or code.** Front matter is YAML keys (`name:`, `description:`) and code blocks are program text. Both are mostly English words whatever the skill's language, so they would pull every text toward English.

**First 2,000 characters only.** The language of a skill is clear from its start; reading the whole text (some are over 300,000 characters) adds time and no information.

**A 75% Latin-letter rule for mixed texts.** Many English skills contain a Chinese or Korean section. Lingua alone calls such a text Chinese as soon as it has Han characters. So a text can be English only when at least 75% of its letters are Latin, and lingua must call that Latin part English. Letters are counted by script, not with lingua's per-span labels: on 1,299 English skills those spans put 11% below 75% by mistake, because code names and short lines get mislabelled. On a sample of 1,491 texts, the rule changed 4 decisions compared with plain lingua.

**`unknown` counts as English.** A body under 20 letters, or one where lingua's confidence is below 0.50, is `unknown`. These are mostly near-empty texts (8,362, median 19 characters). Dropping them would remove skills for the wrong reason; keeping them costs nothing, because a near-empty text only matches when it is identical.

**One row per distinct text.** The same text in 1,000 repos is labelled once. This is what later stages join on (`file_sha`).

**A hand-check sheet, not a claimed accuracy.** The step writes 120 texts (40 `en`, 40 `unknown`, 40 other) for a person to label, and a command scores the sheet. A filled sheet is never overwritten. Until it is scored, the accuracy is unknown and is reported that way.

## Alternatives rejected

- **Keep every language and compare across languages.** Needs translation; a copy decision could not be checked line by line.
- **Label by repository, not by text.** 15,215 repos hold both English and non-English texts.

## Effect

87.0% of texts (1,633,285) are English, 91% of files. The English filter changes the registry list: 3,968 repos pass the 5-skill rule in any language, 3,566 in English.

## Limits

- Translated copies are outside the study. The non-English share (13.0%, mostly Chinese, Japanese, German) is reported.
- A skill with an English header and a mostly non-English body is not English.
- The detector's accuracy on this data is not measured until the hand-check sheet is scored.
