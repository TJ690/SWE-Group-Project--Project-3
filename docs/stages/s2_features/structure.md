# 2.1 Structure (`structure.py`)

**Command:** `python -m skillsig features --only structure` · **Tests:** `tests/s2_features/test_structure.py` · [Back to s2](README.md)

## What it does

Gives each readable skill a set of structure counts: how long it is, how it is organised, and which front-matter fields it declares.

## How it works

No recomputation. A DuckDB `COPY` takes `src/data/generated_data/skill_features.parquet` (built by `db/content_and_structure.sql` in [P.1](../P_prepare.md)) and writes it as `s2_structure.parquet`, keeping only sampled ids in dev mode.

The module also has two Python functions used by other steps that measure one text at a time:

- `split_front_matter(text)` → (front-matter mapping, body, valid flag). It never raises on bad YAML.
- `structure_features(text)` → the same columns as the SQL, for a single text. Used where the SQL table does not apply (registry versions in 1.4).

## Output

`src/data/derived/s2_structure.parquet`: 1,840,872 rows (readable skills).

| Column | Meaning |
|---|---|
| `file_sha`, `location_class` | Skill id and folder class |
| `frontmatter_valid` | YAML front matter parsed |
| `body_chars`, `desc_chars`, `body_words` | Body length, description length, word count |
| `headings`, `h2` | Lines starting `#`–`######` / `##` |
| `numbered_items`, `bullet_items` | Lines starting `1.` / `1)` and `-` `*` `+` |
| `code_blocks` | Fence lines divided by 2 |
| `table_rows`, `links` | Markdown table rows; `[text](url)` links |
| `has_examples_section`, `has_when_to_use` | A heading named "Examples / Usage / Quick start", or "When to use / Use when / Triggers" |
| `fm_allowed_tools`, `fm_license`, `fm_version_or_metadata` | Front matter declares that key |
| `non_ascii_ratio` | Share of non-ASCII characters in the body |

## Design decisions

- **Why counts, not quality judgments?** Counts are objective and reproducible. Whether "many headings" means templated or careful is for the text models to learn, not for the measuring step to assume.
- **Why copy the SQL table instead of recomputing?** Regex counts over 1.8M texts are much faster in SQL than in Python, and the EDA notebooks already read the SQL table, so both share one definition.
- **Why a Python twin with a parity test?** Steps that measure single texts (registry versions) need identical definitions. The parity test proves both give the same counts, including DuckDB's specific `trim()` and whitespace rules.
- **Why must parsing never crash?** Real files have broken YAML, tabs in front matter, unclosed fences. One bad file must not stop a 1.8M-row run; invalid front matter is recorded as `frontmatter_valid = false` (13.4% of distinct skills).

## Full data vs dev sample

Dev mode keeps the sampled ids; each skill's values are the same as in a full run.

## Tests (8)

- Empty text, missing and malformed front matter do not raise.
- Indented `#` lines inside a code fence are not counted as headings.
- Python matches the SQL on a fixture; **parity on 500 real rows** (`-m data`).
- Run copies the table, skips when unchanged, keeps sampled ids in dev mode; missing inputs name the command to build them.

## Limits

- An **unindented** `# comment` inside a shell code fence is counted as a heading by both the SQL and Python (the regex has no fence state). The test with this name covers only indented lines.
- `code_blocks` rounds an unclosed fence down.
