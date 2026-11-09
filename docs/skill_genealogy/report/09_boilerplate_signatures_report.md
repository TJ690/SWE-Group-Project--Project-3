# Boilerplate lines and signatures report

Results of stage 5a, read from `src/data/skill_genealogy/boilerplate_lines.parquet`, `dataset_signatures/` and `registry_signatures.npz` on 2026-11-08. How the step runs is in [boilerplate lines and MinHash signatures](../code_flow/09_boilerplate_signatures.md). The logs are `logs/find_copies_signatures_dev_20261108_1753.log` (dev, which built the shared files) and `logs/find_copies_signatures_20261108_1813.log` (full).

## Run

| Part | Time |
|---|---|
| Step 1, owner count over 1,633,285 English texts (817 batches of 2,000) and rollup | 17 minutes |
| Step 2, dataset signatures | see below |
| Step 2, dev registry signatures (2,054 versions) | under 1 minute |
| Full run, registry signatures (817,485 versions, 21 batches of 40,000 texts) | 17 minutes (1,027 seconds) |

**Dataset signatures were copied from the 2026-11-01 run of the same code.** That run (in the `SWE-Group-Project--Project-3` checkout) used the same signing code (`normalize_text`, `body_lines`, shingles and MinHash are byte-identical), the same chunk layout, and an English label table with exactly the same `is_english` value for every `file_sha`. The new `boilerplate_lines.parquet` is byte-identical to that run's. The 36 part files were copied in while the dev run was signing; the run then skipped them. Checks:

- 1,500 random signatures re-signed with the current code: 0 differences in hash values or shingle count.
- The one chunk the dev run signed itself (`part_s00_c00000`, 50,000 texts) is equal to the copied one in ids, all hash values and sizes.
- Signed plus empty is 1,633,285, every distinct English text.

Signing the dataset from scratch takes about 80 minutes (4,765 seconds on 2026-11-01, 8 workers).

## Boilerplate lines

**547,906** lines have at least 50 owners. Median 115 owners; 307,716 lines have 100 or more, 3,799 have 1,000 or more (863 of those are headings).

Top 30 (owners, line):

| Owners | Line | Owners | Line |
|---:|---|---:|---|
| 37,564 | `## when to use` | 9,017 | `## how to use` |
| 24,709 | `## quick reference` | 8,641 | `## core workflow` |
| 23,769 | `## output format` | 8,488 | `## when to apply` |
| 22,515 | `## quick start` | 8,276 | `## core concepts` |
| 20,111 | `## best practices` | 7,911 | `-h "content-type: application/json" \` |
| 19,426 | `## when to use this skill` | 7,586 | `## when not to use` |
| 17,949 | `## error handling` | 7,506 | `use this skill when:` |
| 13,370 | `## core principles` | 6,966 | `## next steps` |
| 12,482 | `## common patterns` | 6,958 | `} catch (error) {` |
| 12,445 | `## reference files` | 6,777 | `useeffect(() => {` |
| 11,164 | `## common pitfalls` | 6,770 | `## critical rules` |
| 11,122 | `## common mistakes` | 6,392 | `## key principles` |
| 10,782 | `## related skills` | 6,289 | `## important notes` |
| 9,968 | `} else {` | 6,207 | `\| --- \| --- \|` |
| 9,418 | `## how it works` | 6,137 | `## additional resources` |

All 30 are section headings of the skill format, code scaffolding or a table separator: template text, not the content of one skill. **The cutoff stays at 50.**

Lines at exactly 50 owners are different. They are the wording of one skill that 50 owners copied, for example `🔧 code implementation complete. ready to deploy:` and `📋 experiment plan loaded:`. They are removed too. This costs little: a copy of that skill still shares its other lines with the source, and a line held by 50 owners says little about which of them copied from which.

## Dataset signatures

| | Count |
|---|---:|
| Distinct English texts | 1,633,285 |
| Signed | **1,629,358** |
| Empty after boilerplate removal (no signature) | 3,927 (0.24%) |
| Part files (4 slices × 9 chunks) | 36, 872 MB |

Slices hold 408,027, 408,188, 407,754 and 409,316 texts. The empty texts are front matter with no body, or a body made only of boilerplate, for example `00076ce27b9f…` (`name: react-best-practices`, front matter only).

## Registry signatures

| | Dev | Full |
|---|---:|---:|
| Lineage versions | 2,054 | 817,485 |
| Signed | 2,054 | **816,534** |
| Empty | 0 | 951 (0.12%) |

Checks: ids are in the order of `lineage_versions.parquet` with no duplicate, in dev and full. Re-signed from their stored text, 200 dev and 300 full versions gave 0 differences. In dev, versions with the same normalised text share one signature (23 groups). Median shingle count: 1,816 (dev), 803 (full).

Every one of the 951 empty versions has its text stored. 487 have an empty body (front matter only, e.g. `AkaraChen/aghub::fixtures/.agents/skills/vercel-react-best-practices#1`); the other 464 have a body made only of boilerplate lines. **562 lineages** have no signed version at all. Stage 5c can still link them by exact text, not by similarity.

## Sample records

```text
dataset   id: 000009fe40ffb92ccb4350995d114f838be78237   shingle_count: 1001   hash0: 763516
registry  id: 0sec-labs/foxguard::plugins/claude-code/skills/diff-scan#1  shingle_count: 123   hash0: 40174584
```

## Open points

- Lines at the 50-owner cutoff include copied skill wording (see above). If stage 5c finds copies missed because of it, a higher cutoff for lines with an emoji or a full sentence is the place to look.
