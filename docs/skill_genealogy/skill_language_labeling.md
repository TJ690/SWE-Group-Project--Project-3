# Skill language labeling

Labels every distinct skill text with a language code and an English flag. Run commands from the repo root with `.venv/bin/python`.

## How to run

The full-run script starts in the background and writes a log under `logs/skill_genealogy/`. The dev script stays in the terminal.

```bash
bash scripts/skill_genealogy/full_run/language_labels.sh
bash scripts/skill_genealogy/dev_mode/language_labels.sh
```

Both run:

```bash
.venv/bin/python -m skill_genealogy.select_registries --force-languages
```

`--dev` is accepted and ignored. The language file always goes to `src/data/skill_genealogy/`. After the language file is written, the same command continues into registry selection. That part is not covered here.

| Command | What it does |
|---|---|
| `--force-languages` | Rebuild `skill_language.parquet` and the hand-check CSV |
| `--score-language-check` | Score a filled `language_check_sample.csv` and stop |
| (no flags, file already exists) | Keep the language file |

Tests:

```bash
bash scripts/skill_genealogy/tests/language_labels.sh
```

## Inputs

| File | Reads |
|---|---|
| `settings.py` | Its own path, then the nearest `pyproject.toml` above it. No dataset. |
| `file_helpers.py` | Paths, frames, and titles the caller passes in. |
| `text_helpers.py` | One skill text string per call. Cutoffs come from `settings.py`. |
| `select_registries.py` | `src/data/gitskills_data/data/artifacts/*.parquet`, from `python src/data/download_data.py`. Columns used: `file_sha`, `content`, and `repo_full_name` for the summary. `--score-language-check` reads `src/data/skill_genealogy/language_check_sample.csv` instead. |

Rows with a null `file_sha` or null `content` are skipped. One distinct `file_sha` becomes one row.

## What the code does

Eight worker processes label chunks of 10,000 texts. This process keeps the full text only long enough to cut a 2,000-character sample. DuckDB reads the texts in 8 slices, with a cap of 8 GB and 2 threads. The file is sorted by `file_sha`.

| Output | Contents |
|---|---|
| `src/data/skill_genealogy/skill_language.parquet` | `file_sha`, `language`, `is_english`, `body_chars` |
| `language_check_sample.csv` | 40 texts each from `en`, `unknown`, and other languages. `human_language` and `human_is_english` stay empty for a person to fill |

A check sheet that already has a human label is kept. `--score-language-check` compares `is_english` with `human_is_english`. Blank human cells are skipped.

**`settings.py`** — cutoffs for this step. Fewer than 20 letters is `unknown`. A text needs 75% Latin letters before it can be English. Lingua confidence below 0.50 is `unknown`. Only the first 2,000 characters of the body are read, against 21 candidate languages. `DOWNLOAD_WORKERS` is 8. The check sample uses seed `20261015`.

**`file_helpers.py`** — `check_inputs_exist` stops when the artifacts glob matches nothing. `connect_duckdb` opens the in-memory database that reads the parquet. `print_summary` and `print_sample_records` print the language counts and two sample rows.

**`text_helpers.py`** — `detect_language` normalises the text, drops the YAML front matter and fenced code blocks, then asks lingua for a code.

Language detection uses [lingua-language-detector](https://github.com/pemistahl/lingua-py) 2.2.0. It scores the body with n-gram models for the 21 candidate languages and returns a confidence for each. The models load once per process.

- Under 20 letters → `unknown`.
- At least 75% Latin letters → lingua may return `en`, another code, or `unknown`.
- Below that share → English is blocked. The label is another code, or `other` when nothing is confident.
- `is_english` is true for `en` and `unknown`. `body_chars` is the length of the normalised body, so front matter is not counted.

**`select_registries.py`** — `label_languages` applies that rule to one chunk. `label_all_skill_languages` streams the distinct texts, writes the parquet, and draws the check sample. `summarize_languages` counts English and non-English texts, files, and repos, and lists the ten most common language codes.
