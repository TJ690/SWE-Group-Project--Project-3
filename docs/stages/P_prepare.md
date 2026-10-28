# P. Prepare: generated tables

**Code:** `db/*.sql` · **Output folder:** `src/data/generated_data/` · **Run:** [EXECUTION_STEPS.md § P](../EXECUTION_STEPS.md#p-prepare-generated-tables)

A one-off local build of four summary tables from the raw Parquet. It downloads nothing.

---

## P.1 SQL build scripts

### What it does

| Script | Builds | Content |
|---|---|---|
| `content_and_structure.sql` | `skill_features.parquet` | One row per readable distinct skill: body length, description length, word count, headings, H2s, numbered and bullet items, code blocks, table rows, links, "Examples"/"When to use" sections, front-matter fields (`allowed-tools`, `license`, `version`/`metadata`), non-ASCII ratio |
| `agent_ecosystem.sql` | `file_agents.parquet` | One row per file: which agent folder the path sits in (`.claude` → Claude Code, `.github` → GitHub Copilot, `.cursor` → Cursor, `.codex`, `.gemini`, `.kiro`, `.agents`, …) |
| `repos.sql` | `repo_profile.parquet` | One row per repository: GitHub metadata plus `files`, `distinct_skills`, `copy_rows`, `copy_share`, name flags and a size tier |
| `siblings_data.sql` | `sibling_files.parquet` | One row per bundled file next to a skill (used by the EDA notebooks only) |

### How it works

Each script is a single `COPY (SELECT …) TO '…parquet'` over `read_parquet` of the raw tables.

- **Body vs. front matter.** `regexp_replace(content, '^\s*---.*?\n---\s*\n?', '', 's')` removes the YAML block; `regexp_extract(…, 1, 's')` keeps it for the front-matter flags. The same regular expression is reused in Python (`structure.py`, `wording.py`, `minhash_cluster.py`) so every step strips front matter the same way.
- **Readable filter.** `dedup_primary = 1`, not a one-line pointer to another `SKILL.md`, not `Redirecting…`, `body_chars >= 200` (see [README § 4](README.md#4-one-readable-filter-defined-once)).
- **Agent folder.** The first dot-folder in the path decides the tool. No dot-folder is `No agent folder`; an unknown dot-folder is `Other dot-folder`.

### Design decisions

- **Why SQL, before the Python pipeline existed?** These tables came from Sprint 1 exploration and feed the EDA notebooks. Keeping them in SQL lets the notebooks and the pipeline read one shared definition.
- **Why does step 2.1 copy this table instead of recomputing it?** Regex counts over 1.8M texts are much faster in SQL than in Python. `structure.py` still has a Python twin, `structure_features(text)`, that is tested for **parity** with the SQL (the same counts on the same text). The Python version is used where a single text is measured (for example s1 change classification); the SQL table is used for the population.
- **Why classify agent folders by path, not by content?** The folder is where a tool installs or reads skills (`.claude/skills/…`). It says which agent the repository is set up for, which is weaker than authorship; baseline B2 (step 3.4) measures how far that weak signal goes on its own.
- **Why keep `sibling_files` even though no step reads it?** Step 2.9 reads siblings straight from the raw `artifact_siblings` table (it needs the representative row's folder). The generated table stays for the notebooks.

### Limits

- `code_blocks` is fence lines divided by 2, so an unclosed fence rounds down.
- Headings inside code fences are counted by the SQL (regex has no fence state). The scaffold outline in 2.9 does skip fenced lines.
- The agent-folder mapping is a fixed list; new tools fall into `Other dot-folder` until the list is extended.
