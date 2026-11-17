# Research Question: Skill Genealogy

## Question
Where do agent skills come from, what do people change when they copy one, and do copies keep up with the skill they were copied from?

- **RQ1, origin.** Is a skill a copy of a registry skill, a member of a family with no registry source, or a text nobody else holds?
- **RQ2, change.** When a skill is copied, what does the copier change against the version they started from, and how does that compare with the changes the registry itself makes?
- **RQ3, staleness.** Do copies fall behind the registry they came from, how far, and do the updates they miss touch commands, network access or scripts?

## Background/Motivation
A skill (`SKILL.md` plus the scripts and references in its folder) tells an AI coding agent how to do a task. Skills spread by copy and paste: a developer finds a skill in a popular collection and drops the folder into their own repository. GitSkills records one snapshot of where each text sits, but not where it came from, so nobody can tell which skills are copies, which collection they came from, or whether they were updated after they were copied.

This matters more for skills than for ordinary code clones. An agent carries out a skill with little review. A stale instruction is executed, not just left uncompiled. When a registry fixes a wrong command, removes a risky step or tightens `allowed-tools`, a copy made before the fix keeps the old behaviour until someone updates it.

The genealogy work also feeds the authorship question in [RESEARCH_QUESTION.md](RESEARCH_QUESTION.md). A copied skill's wording shows how its source was written, not how the copy was made. Knowing which skills are copies, and of what, tells the authorship study which texts it should not read as independent evidence.

## Definitions
- **Registry.** A non-fork repository with 100 or more stars and 5 or more English skills, counted in the dataset or in the repository's own file tree. 4,806 repositories qualify.
- **Source or copier.** A registry that writes its own skills is a source. One that collects other people's skills is a copier. Copiers are never origins.
- **Lineage.** The versions of one skill folder in one source registry, taken from its git history.
- **Copy.** A dataset file outside the registries whose text matches a registry version: exactly, by near-duplicate similarity, by containment, through a near-duplicate cluster, or through its own git history.
- **Origin.** The registry version a copy started from. It is the earliest version found among the selected registries, not necessarily the author.
- **Stale.** A copy that has not taken a registry update released more than 30 days before its comparison date.

## Unit of Analysis
- RQ1: one distinct English skill text (`file_sha`).
- RQ2 and RQ3: one copy file (repository plus path), because the same text in two repositories can be edited or updated differently.

## Population and Sample
The GitSkills snapshot (Hugging Face `mvaccargiu/gitskills`, July 2026) holds 1,877,981 distinct skill texts. The genealogy uses the 1,633,285 English texts (87.0%). Registry histories and the histories of copied skills were downloaded from GitHub in November 2026.

| Population | Count |
|---|---:|
| Registries (source 4,060, copier 702, unclear 44) | 4,806 |
| English registry lineages | 188,787 |
| Registry text versions | 817,485 |
| Copy files | 1,414,080 |
| Repositories holding a copy | 60,958 |

No sample is drawn for the results. Dev runs use a fixed 30-registry sample as a smoke test, and their numbers are not evidence.

## Variables
- **Link:** how a copy was matched (`exact`, `history_exact`, `near`, `near_contained`, `history_near`, `cluster`) and its similarity.
- **Origin and family:** origin version, family kind (`source`, `independent`, `copier_seen`, `singleton`), generation in the family tree.
- **Arrival:** how many skill folders arrived in the same commit (`chosen` 1–4, `small_set` 5–19, `bundle` 20+), and whether the copy was later removed.
- **Change against origin:** body lines added and removed, share of body changed, headings, commands, network references, scripts, project names, front-matter and `allowed-tools` changes, folder file changes, new risk flags in scripts.
- **Lag:** held version, versions behind (text and folder), days since the first missed release, what the missed updates added, and sync events (when a copy took a registry version).

## Outcome (dependent variable)
- RQ1: share of English texts and files that are copies, by link kind and family kind.
- RQ2: state of each copy against its origin (`unchanged`, `cosmetic` under 5% of body lines, `substantive`), customisation, and the same change shares for registry updates, copy syncs and local copy edits.
- RQ3: copy state (`up_to_date`, `behind`, `stale`, `orphaned`, `removed`, `unknown`), days stale, missed updates by kind, and time to take an update.

## Competing and Simpler Explanations
1. **Template reuse, not copying.** Two repositories of one owner can share a skill because one is a template of the other. Check: report results with and without same-owner copies.
2. **One collection drives the numbers.** A single registry hub (`sickn33/agentic-awesome-skills`) is linked to 26% of copy files. Check: report results with and without it, and split by registry and hub origins.
3. **Copies are unchanged because only unchanged copies are found.** 79% of copy files are exact links, and looser links admit more edited copies. Check: report RQ2 shares by link kind, and measure recall against copies proven by git history.
4. **Staleness is a product of the comparison date.** A copy compared on the registry snapshot day has had more time to fall behind than one compared on the dataset capture day. Check: split by comparison basis and report several stale cutoffs (30, 90, 180 days).
5. **What copies miss is cosmetic.** If most registry updates are typo fixes, being behind does not matter. Check: report missed updates by what they added, and hand-label a sample of registry changes before claiming that copies miss fixes.
6. **Git dates are wrong.** A copy dated before its registry's first release is either the true original or has a rewritten date. Check: count these copies, keep them out of the origin step, and report results without them.

## Expected Contribution
1. A reproducible pipeline that links 1.4 million dataset copies to the registry versions they came from, with every threshold named in `settings.py`.
2. A measured comparison of text-matching methods against copies proven by git history: which methods find edited copies and which miss them.
3. A description of what copiers change, set beside what registries change, measured by the same code.
4. A measure of how far copies fall behind, what the missed updates contain, and how rarely copies update.
5. An account of where these measures are unreliable, in [THREATS_TO_VALIDITY_GENEALOGY.md](THREATS_TO_VALIDITY_GENEALOGY.md).

The claim that copies keep defects their source has fixed is not yet supported. It needs a hand-labelled sample of registry changes (security, correctness, capability, wording, cosmetic) first; see [paper direction](docs/skill_genealogy/paper_direction.md).
