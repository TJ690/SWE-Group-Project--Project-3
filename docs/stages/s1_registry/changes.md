# 1.4 Classify registry changes (`changes.py`, `change_classifier.py`)

```bash
python -m skillsig registry --only changes
```

[Back to s1](README.md)

## What it does

For each pair of consecutive versions of a registry skill (v1→v2, v2→v3, …), measures what changed and labels the change `substantive` or `cosmetic`. This is for RQ2 (how skills evolve), not for the authorship label.

## How it works

Reads both versions' texts and bundled file lists from the clone, splits off the front matter, and measures:

| Column | Measures |
|---|---|
| `body_change_pct` | Share of body lines that changed (lower-cased, whitespace collapsed) |
| `desc_similarity` | How similar the two descriptions are |
| `headings_added/removed` | Headings gained or lost |
| `commands_added/removed` | Shell commands gained or lost (prompt lines, or lines starting with a known CLI such as `git`, `npm`, `docker`) |
| `network_added/removed` | Lines with URLs, `curl`, `wget`, `fetch(`, `requests.get` |
| `scripts_added/removed` | Bundled `.py .sh .js .ts .ps1` files gained or lost |
| `steps_delta`, `files_delta` | Change in numbered steps; in bundled files |

**Rule:** `substantive` if the body changed by 5% or more (`rq2.substantive_body_change_pct`), or the description changed, or the bundled files changed. Otherwise `cosmetic`.

## Output

`s1_registry_changes.parquet`: 12,877 version pairs. Columns: `lineage_id, from_blob, to_blob, from_date, to_date, change_type`, plus the measures above.

## Design decisions

- **Line-level diff:** Markdown edits are line-shaped, and collapsing whitespace makes re-indenting count as no change.
- **Any description change is substantive:** the description is what an agent reads to decide whether to load the skill.
- **Commands, network lines and scripts counted separately:** adding `curl … | bash` is a different kind of change from fixing a typo, even at the same percentage.
- **5%:** a judgment call for "more than a typo fix", kept in config.

## Tests

`test_change_classifier.py` (6): whitespace-only edit is cosmetic; a rewritten description or added script is substantive; an added `curl` line counts once.
