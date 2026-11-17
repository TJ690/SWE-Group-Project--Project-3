# Registry re-hosts report

Results of stage 5b, read from `src/data/skill_genealogy/lineage_roots.parquet`, `registry_hubs.parquet` and `repo_roles_final.parquet` on 2026-11-09. How the step runs is in [registries copying each other](../code_flow/10_registry_rehosts.md). The logs are `logs/find_copies_rehost_dev_20261109_1633.log` (dev) and `logs/find_copies_rehost_20261109_1633.log` (full).

## Run

| Run | Lineages | Time | Peak memory |
|---|---:|---:|---:|
| Dev (26 registries) | 943 | 0.4 s | 0.4 GB |
| Full (4,045 registries with lineages) | 188,787 | 21.7 s | 2.6 GB |

The full run read 816,534 signed versions and searched 179,211 first versions in the band index (the 8,972 with an exact source and the first versions without a signature were not searched).

## Re-hosted lineages

| | Lineages | Share |
|---|---:|---:|
| Original (no older source in another registry) | 171,372 | 90.8% |
| Re-hosted | 17,415 | 9.2% |
| – `exact` (same blob) | 8,972 | |
| – `near` (Jaccard ≥ 0.80) | 7,001 | |
| – `near_contained` (containment ≥ 0.80, source ≥ 20 shingles) | 1,442 | |

`near` similarities: median 1.00, lowest 0.805. A `near` link with Jaccard 1.00 and a different blob is the same text after normalisation and boilerplate removal (front matter, whitespace or a template line changed).

**The source is often a later version.** 7,701 of the 17,415 links (exact 3,555, near 3,329, contained 817) point to a version after the source lineage's first: registries copy the skill as it was when they copied it.

**Same owner.** 6,718 links (exact 5,481, near 836, contained 401) join two repos of the same GitHub owner, for example `cyanheads/mcp-ts-core` → `cyanheads/pubmed-mcp-server` (34 exact copies) and `paralleldrive/aidd` → `paralleldrive/riteway`. These are template or sibling repos. The stage keeps them, because the rule is "another repo". Stage 3c counts one owner's registries as one, so for these registries it sees no shared text.

**Chains** (steps to the root): 1 step 14,745, 2 steps 2,205, 3 steps 420, 4 steps 33, 5 steps 12. No cycles: every link goes to a strictly older version, so first dates fall along a chain.

Roots with the most re-hosted lineages:

| Root lineage | Re-hosts |
|---|---:|
| `TencentCloudBase/CloudBase-MCP::config/source/skills` | 439 |
| `mvanhorn/cli-printing-press::testdata/golden/expected/generate-golden-api-oauth2-device-code/printing-press-oauth2-device-code` | 241 |
| `TencentCloudBase/CloudBase-MCP::config/source/skills/cloudbase-agent/py` | 135 |
| `anthropics/skills::skills/skill-creator` | 125 |
| `TencentCloudBase/CloudBase-MCP::config/.codebuddy/skills/relational-database-mcp` | 99 |

The TencentCloudBase and printing-press roots are templates copied into many folders of another repo of the same owner (`TencentCloudBase/awesome-cloudbase-examples`, `mvanhorn/printing-press-library`).

## Registries with the most re-hosted lineages

| Registry | Re-hosted | Lineages |
|---|---:|---:|
| `sickn33/agentic-awesome-skills` | 2,842 | 8,871 |
| `TencentCloudBase/awesome-cloudbase-examples` | 1,975 | 2,131 |
| `Dokhacgiakhoa/Agent-Skills-4-Vibe-Coding-CLI` | 1,278 | 1,758 |
| `Dokhacgiakhoa/Agent-skills-setup-for-AntiGravity` | 1,278 | 1,758 |
| `PKU-YuanGroup/OpenAI4S` | 584 | 612 |
| `InternScience/DrClaw` | 417 | 519 |
| `mvanhorn/printing-press-library` | 311 | 1,205 |
| `synthetic-sciences/openscience` | 271 | 400 |
| `vibeeval/vibecosystem` | 185 | 307 |
| `makenotion/notion-cookbook` | 180 | 256 |

976 of the 4,045 registries with lineages have at least one re-hosted lineage.

## Minimum source size for a contained link

A text that keeps only a few shingles after boilerplate removal is often just its headings. Example: `sickn33/agentic-awesome-skills::…/auth-implementation-patterns` and `rmyndharis/antigravity-skills::skills/airflow-dag-patterns` both keep only `## Instructions`, `## Safety`, `## Resources` (2 and 4 shingles), so the smaller one is fully "contained" in the other although the skills are unrelated. Without a minimum, 436 contained links had a source under 20 shingles. Whether the copy keeps the source's folder name is a rough sign of a real copy:

| Source shingles | Contained links | Same folder name |
|---|---:|---:|
| under 5 | 214 | 26% |
| 5–9 | 160 | 38% |
| 10–19 | 62 | 48% |
| 20–29 | 107 | 89% |
| 30–49 | 93 | 78% |
| 50–99 | 155 | 81% |
| 100–199 | 162 | 81% |

So a contained link needs a source of at least `REHOST_CONTAINED_MIN_SHINGLES` (20) shingles. That removes 434 links (1,876 → 1,442); the other counts do not change. `near` links are not limited: both sides must be of similar size, and small `near` pairs mostly keep the folder name (55% under 5 shingles, 74–76% from 5 to 19), for example `JuliusBrussee/caveman::skills/caveman-review` → `debuglebowski/slayzone::.claude/skills/caveman-review`.

## Registry kinds and the dated hub share

| Kind (stage 3c) | Registries | With lineages | Share scored (≥ 5 English lineages) | Share ≥ 0.25 | `is_hub` |
|---|---:|---:|---:|---:|---:|
| copier | 702 | 0 | 0 | 0 | 702 |
| source | 4,060 | 4,001 | 3,837 | 277 | 0 |
| unclear | 44 | 44 | 44 | 18 | 0 |

295 non-copier registries have a dated hub share of at least `HUB_MIN_SHARE` (0.25). They stay sources: the share labels nothing. 60 of them have re-hosts only from registries of their own owner (the case above). The others disagree with stage 3c for a reason not checked here. Example: `ContextLab/storytelling-with-data` re-hosts all 14 skills from `serverless-dna/tendril` (`near`, Jaccard 1.00, different blobs), while stage 3c finds 0% of its 13 shared texts first seen elsewhere. These registries are a review list for stage 3c, not a label change.

## Roles

`repo_roles_final.parquet` has 282,200 rows, the same as `repo_roles.parquet`.

| `repo_role` | `hub_evidence` | Repos |
|---|---|---:|
| registry | registry_kinds | 4,104 |
| registry_copier | registry_kinds | 702 |
| hub | dataset_dates | 5 |
| repository | dataset_dates | 44,833 |
| repository | null | 232,556 |

## Checks

- One root row per lineage (188,787), no duplicates.
- Every `copied_from_lineage` is a kept lineage in another repo, and its version `copied_from_seq` was released strictly before the first version: 0 violations.
- Every `exact` link has the same blob on both sides: 0 violations.
- Every `root_lineage` is an original lineage.
- Dev: 943 lineages, 0 re-hosts. The 26 dev registries were drawn independently in stage 2, and a source outside the sample cannot be seen in dev.

## Sample rows

```text
lineage_id: 0xroylee/Omniskills::examples/teams/startup-team/skills/unslop
copied_from_lineage: cursor/plugins::pstack/skills/unslop
copied_from_seq: 3
rehost_link: exact
rehost_similarity: 1.0
root_lineage: cursor/plugins::pstack/skills/unslop
is_original: False, chain_length: 1, chain_cycle: False

repo: 2SSK/dot-files
registry_kind: copier, kind_confidence: 0.35, kind_basis: date
upstream_share: 1.0, english_lineages: 0, dated_hub_share: null
is_hub: True
hub_reason: copier: 100% of 7 shared texts first seen elsewhere
```
