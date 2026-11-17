# Skill language report

Results of the language labelling step, read from `src/data/skill_genealogy/skill_language.parquet` on 2026-11-06. How the step runs is in [skill language labeling](../code_flow/01_skill_language_labeling.md). The queries are in `db/genealogy/language_labels.sql`.

One row is one distinct skill text (`file_sha`). The same text saved in many repositories is one text and many files. `en` and `unknown` count as English. Every other code does not.

## What was labelled

The file holds **1,877,981** texts, the full set of distinct skill texts.

| | Texts | Share of texts | Files | Repositories |
|---|---:|---:|---:|---:|
| English | 1,633,285 | 87.0% | 3,451,004 | 251,416 |
| Not English | 244,696 | 13.0% | 346,113 | 45,999 |

A repository is counted on both sides when it holds both kinds of text. **15,215** repositories do. **282,200** repositories hold at least one labelled text.

An English text appears in 2.1 files on average. A non-English text appears in 1.4. English is 87% of texts and 91% of files.

## Languages

`unknown` means the body has fewer than 20 letters, or it is at least 75% Latin letters and the detector was not confident. Those 8,362 texts have a median length of 19 characters, and they count as English. `other` means the body is under 75% Latin letters and the detector found no confident language. That includes scripts it does not cover, such as Greek or Hebrew.

| Language | English | Texts | Median body length |
|---|---|---:|---:|
| en | yes | 1,624,923 | 4,897 |
| zh | no | 105,142 | 2,962 |
| ja | no | 30,331 | 2,710 |
| de | no | 29,634 | 3,819 |
| pt | no | 18,889 | 5,001 |
| ko | no | 18,226 | 3,218 |
| es | no | 15,465 | 4,424 |
| unknown | yes | 8,362 | 19 |
| fr | no | 7,480 | 4,178.5 |
| ru | no | 4,863 | 3,805 |
| tr | no | 3,867 | 4,394 |
| vi | no | 2,982 | 4,180 |
| ar | no | 2,446 | 1,120 |
| nl | no | 1,377 | 4,467 |
| it | no | 1,233 | 4,258 |
| pl | no | 965 | 5,225 |
| id | no | 779 | 4,965 |
| th | no | 550 | 3,654.5 |
| other | no | 234 | 3,597 |
| uk | no | 189 | 4,140 |
| fa | no | 28 | 1,887.5 |
| hi | no | 16 | 5,248 |

Chinese is the largest non-English language, at 105,142 texts (5.6% of all texts). Japanese, German, Portuguese, Korean and Spanish are the next five.

## Effect on the registry rule

The registry rule keeps a repository with at least 100 stars that is not a fork and has at least 5 different skills. Counting every language gives **3,968** registries. Counting English skills only gives **3,566**. The English filter drops **402** repositories.

## Hand check

`language_check_sample.csv` has 120 rows: 40 labelled `en`, 40 labelled `unknown`, and 40 from other languages. None of the `human_is_english` cells are filled in, so the sheet has not been scored.
