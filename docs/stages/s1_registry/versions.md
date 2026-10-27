# 1.2 Collapse versions (`versions.py`)

```bash
python -m skillsig registry --only versions
```

[Back to s1](README.md)

## What it does

Turns the raw commit log into a catalog with **one row per distinct version** of each registry skill: one row per (repository, skill, blob).

## How it works

`build_versions` runs five steps:

| Function | Adds |
|---|---|
| `_collapse` | One row per (repo, skill, blob), with first commit, `first_date`, `last_date`, `n_commits`, subject and release label |
| `_intro_flags` | `ai_trailer_at_introduction`: a commit on the version's first day had an AI trailer |
| `_assign_sequence` | `version_seq` 1, 2, 3… by date; `lineage_id = repo::skill` |
| `_flag_snapshot` | `post_snapshot`: first seen after the dataset snapshot (2026-07-20) |
| `_assign_renames` | `renamed_group`: the same blob under two skill names in one repository |

It also reads each version's text from the clone for `size_bytes` and `desc_idiom` (how the description opens). Texts missing from a clone are downloaded by hash; `SKILLSIG_NO_FETCH` does not stop this, but a hash names exact bytes, so it cannot change which versions exist.

`desc_idiom` categories, checked in this order:

| Value | Description starts with |
|---|---|
| `use_this_skill_whenever` | "Use this skill whenever…" (the idiom Anthropic's skill-creator teaches) |
| `use_when` | "Use when…", "Use this skill when…", "This skill should be used when…" |
| `guide_for` | "guide for" within the first few words |
| `capability_summary` | An action verb ("Analyze…") or "This skill helps / creates…", "Use this skill to…" |
| `other` | Anything else |

## Output

`s1_registry_versions.parquet`: 15,254 versions of 2,377 skills. Columns: `lineage_id, repo, skill, blob, version_seq, first_commit, first_date, last_date, n_commits, subject, release_label, size_bytes, ai_trailer_at_introduction, desc_idiom, post_snapshot, renamed_group`.

## Design decisions

- **One row per blob, not per commit:** a version is a distinct text, and several commits can produce the same one (revert, re-add).
- **Flag post-snapshot versions, don't drop them:** they can't be in the dataset, but they are part of the history. Step 1.3 applies the cut-off.

## Tests

`test_versions.py` (10): one row per blob, version order, same-day ordering, post-snapshot flag, rename groups, incomplete commits dropped, description idioms, size and idiom read from the clone.
