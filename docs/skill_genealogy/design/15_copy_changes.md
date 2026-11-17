# Design: copy against origin (stages 9a, 9b and 9c)

Cards G19 (9a), G20 (9b), G21 (9c) · Code: `measure_copy_changes.py` · [Code flow](../code_flow/15_copy_changes.md)

## Problem

RQ2 asks what people change when they copy a skill. Stage 7 says which registry version a copy started from, but not what became of the copy. The answer needs a measure that is the same for a copy and for the registry's own updates, otherwise "copies change a lot" has nothing to be compared with.

## Design at a glance

Each copy's current text is compared with its origin version with the same functions that describe a registry update (`compare_texts`, `compare_front_matter`). The edits inside each copy's history are labelled by whether the new text is a known registry text. A profile table reports the same six shares for four populations: registry updates, local edits, syncs and copy against origin.

## Decisions

**The target is the origin, not the newest registry version.** The question is what the copier changed, not how far behind the copy is. Against the newest version, every release the registry made after the copy would count as a change by the copier. Lag is RQ3 and stage 10.

**The same measures for the registry and for copies.** The profile uses the shares that `lineage_changes` already has, so a registry update and a copy edit are measured by the same code and the shares can be set next to each other.

**The current text is the newest history text, then the dataset text.** The history is newer than the crawl, so it is the better "current". When the newest blob has no stored text the dataset text is used and `current_basis` says so; an older history row is never substituted, because that would report an outdated state as current.

**Front matter on its own.** A copy can keep the body and widen `allowed-tools`. That is a security-relevant change a body diff cannot see, so it has its own flag and is kept in the profile.

**`customized` is a list rule, not a size rule.** Substantive is how much of the body changed. Customised is what was added: a project name, a script, a step, a command or a heading. A big rewrite with none of these is substantive and not customised; a small edit that adds a heading is customised and cosmetic. The two answer different questions.

**A project name counts as customisation only when it is added to the body.** `names_added` of `compare_texts` reads the whole file, front matter included. Many copies keep the instructions word for word and only add front-matter keys that carry their project's name (`metadata: jarvis:`, `catalogSource: luokai`). These copies are `unchanged` (the body is identical), and counting them as customised would make one copy both unchanged and customised. They are catalogue labels added by the tool that imports the skill, not changes to what the skill tells the agent to do. So `customized` uses the names added to the body (`body_names_added`), and the front-matter edit stays visible in `fm_keys_added`. `names_added` keeps the whole-file list, so the two can be compared. On full data this moved 16,048 copies (1.3%) from customised to not customised. The rule is local to stage 9a; `compare_texts` is unchanged, so stage 4a and the registry rows of the profile are not affected.

**Heading overlap measures direction.** The overlap of "headings the copy added" and "headings the registry added later" says whether copies move the way the registry moves. It is null when the copy added no heading, whatever the registry added later. Such a copy did not move, so it has no direction; scoring it 0 would read "left as it was" as "went the other way". The card first set null only when both sides were empty. On full data that gave 578,186 zeros from copies that added nothing while their registry added headings, out of 643,732 scored copies, and a mean of 0.0099; over the 65,546 copies that did add headings the mean was 0.097. The mean now answers the question asked: when a copy adds a section, is it one the registry also adds?

**An edit is `sync` only by exact blob.** `sync` means the new text is a version of the copy's own lineage, `other_source` a version of another lineage, `local` anything else. Exactness keeps `local` honest: it means "not a known registry text", not "a person wrote this". A rewrite that is nearly a registry version is `local`.

**Inherited commits are dropped.** A fork or imported history holds commits that belong to the registry's history. They are not the copier's edits and would turn the registry's own updates into `sync` rows for every fork. An inherited commit still counts as the text before the next edit. If it were removed before pairing, a fork's first own edit would have no earlier text and would be lost, or would be compared with an older text it never changed.

**A whitespace-only change is not an edit.** Stage 4a does not count a commit that only changes spaces, line endings, quotes or blank lines as a registry version (same `normalized_sha`). A copy edit is held to the same rule, otherwise the profile would set copy edits that include such commits next to registry updates that do not. On the full data before this rule, 11,190 of 161,847 copy edits (7%) were whitespace only, and they lowered the substantive share of local edits from 31.5% to 29.1%.

**The registry population is limited to lineages that have copies.** Comparing copies with all registry updates would compare them with skills nobody copied.

**Batches by file, dataset text only for its own batches.** Histories are read in batches that never split a file, so edits are in order. Copies with no stored history text (about a fifth of the full set) are batched apart from the others, and only their batches read the dataset. A scan of the dataset for every batch would cost more than the comparison, and one scan held in memory would be several GB.

## Alternatives rejected

- **Compare with the newest registry version.** Counts the registry's updates as the copier's changes.
- **Treat a near match of a registry version as a sync.** Makes `local` mean "different enough", which depends on a threshold that is arbitrary here.
- **Use only the dataset text as the current text.** Ignores the history that stage 6 downloaded, and the newest text.
- **A single `modified` flag instead of state and `customized`.** Cannot tell a typo fix from a rewrite.

## Effect (full run)

The full run compares 1,275,893 copies, none left out: 999,864 current texts come from history and 276,029 from the dataset. 89.3% are unchanged, 3.9% cosmetic and 6.8% substantive; 6.8% (86,389) are customised. Most copies are left as they were copied.

The shares depend on the link kind. `exact` links (1,050,509 copies, 82%) are 0.6% substantive; `near` (129,219) 18.7%; `history_exact` (39,757) 36.3%; `near_contained` (30,197) 66.8%; `cluster` (25,534) 84.0%. A looser link admits copies that differ more, so the RQ2 shares are reported by link kind.

Heading overlap averages 0.097 over the 65,546 copies that added a heading: when a copy adds a section, it is seldom one its registry adds later. 12,379 copies (1.0%) changed `allowed-tools`.

Inside copy histories there are 128,285 `local` edits, 20,985 `sync` and 1,387 `other_source`; 11,190 whitespace-only pairs are not edits. Substantive share by population: registry updates of the copied lineages 39.1% (277,763), syncs 48.1%, local edits 31.5%, copy against origin 6.8%. A copier's own edit is smaller than a registry update.

The dev sample (6,170 copies) is mostly `cluster` links (87%) and reads 89% substantive; it is not representative of the full set and is only a smoke test.

## Limits

- A headline share over all copies mixes link kinds of very different looseness. Read the shares by link kind.
- Only `SKILL.md`; scripts and references are stage 9c.
- A history shows only commits that touched the skill; an edit git did not record is invisible.
- `local` is "not a registry text", not authorship.
- `sync` means the new text is a version of the copy's lineage, not that it is newer. On full data 6,418 of 20,985 syncs land on the origin or an older version. Of these, 1,043 are reverts to a registry text the file held before (a local edit undone); the rest are copies whose first downloaded text was not a registry version and that later took the origin text.
- One commit can hold two history rows for the same file, which pairs A->B and B->A inside that commit; 3 such edits in full data.

## Stage 9b: every copy version against four texts

### Problem

9a compares one text per copy, the current one, with one target, the origin. The drift chart and the version player need a score at every version, and a reader asks more than one question of each: how big was this step, how far is it from where the copy started, did it keep up with the registry at the time, and how far is it from the registry today. Later pages must read the same numbers, not recompute them with slightly different rules.

### Decisions

**Four targets, chosen by date (A40).** `prev`, `origin`, `at_date` and `newest` answer the four questions above. `at_date` is the newest release on or before the copy version's date, so a copy is never measured against a text that did not yet exist.

**Exact shingle sets of the whole body.** Skill texts are small, so exact Jaccard and containment are cheap. MinHash would give an estimate that differs from the exact numbers other stages show for the same pair.

**No boilerplate line is removed.** Stage 5a removes lines that 50 or more owners share, so that two unrelated texts are not linked through an install line. 9b compares texts already known to be related, and in a popular skill nearly every line is shared by 50 owners because so many people copied it. With the lines removed, `frontend-design` kept 1 of its 25 body lines, and a copy that changed that one line scored Jaccard 0 while keeping 96% of the lines; a `grill-me` copy identical to its origin also scored 0, because nothing was left on either side. On the first full run 14,677 versions identical to their origin scored 0. The line scores (`verified`, line counts) never removed boilerplate, so the whole body now feeds every score.

**Identical texts score 1; nothing to measure is null.** The same blob scores 1, 1, 1, 0, 0, 0 without a comparison, so a perfect copy of a short or empty skill is not 0. Null is kept for a score with nothing to measure: Jaccard when neither body has a word, containment, `verified` and `body_change_pct` when the target has no body. A 0 there would read as "completely different", and `body_change_pct` divided by 1 would show adding 5 lines to an empty template as 500%. A copy that deleted its whole body is different: everything was removed, so it scores 0 and stays in the drift numbers.

**`verified` and the line counts come from one line diff.** They are the same numbers as `compare_texts` and `share_of_registry_lines_kept`, which a test checks, so a version's origin scores at its last version agree with the 9a row of the copy.

**A version is what A39 calls a step.** The history is ordered as A39 orders it, and a whitespace-only change, a same-blob row or a delete is not a new version. So every 9a edit is exactly one 9b step with the same line counts and label. On dev data all 689 9a edits match a 9b step with the same kind and line counts, and every copy's last version has the same origin line counts as its 9a row.

**Inherited commits stay as versions, marked `inherited`.** 9a leaves them out as edits but keeps them as the text before the next edit. 9b does the same by keeping them in the list, so the next own edit is scored against the text it changed. They are labelled, so a chart can leave them out.

**A copy with no stored history text is one dataset version.** It is dated `last_commit_at` of its own dataset row, and its text is any dataset row of the same blob, as in 9a. A file's own row can lack content that another repo's row of the blob has; requiring the own row left out 57 of 109 such copies in dev.

**Change notes are a short list, one kind per line (A41).** The version player needs "what did this step add that matters" without a diff. Network references come first because they are the risk to flag. One kind per line keeps the note counts additive.

### Alternatives rejected

- **Remove boilerplate, and fall back to the whole body when nothing is left.** Fixes an empty side, not a popular text with one line left (the `frontend-design` case), and mixes two definitions in one column.
- **Remove boilerplate, and leave the score null when nothing is left.** Drops the most-copied skills from every Jaccard number.
- **Only the origin as target.** Cannot say whether a copy kept up with its registry or how far it is from today's text.
- **The newest registry version as the only target.** Counts every later registry release as distance the copier made.
- **MinHash scores.** An estimate that would not match the exact numbers of 9a and stage 5.
- **Every history row as a version.** Whitespace-only commits would be versions with no change and would break the one-to-one match with 9a edits.

### Effect (full run)

The full run scores 1,453,150 versions of 1,275,893 copies, none left out: 1,177,121 versions from history and 276,029 from the dataset. 76,984 copies (6.0%) have more than one version. Kinds: `copy` 1,275,893, `local` 128,285, `inherited` 26,600, `sync` 20,985, `other_source` 1,387. The `local`, `sync` and `other_source` counts equal the 9a edit counts, and every 9a edit is a 9b step with the same new text, kind and line counts. In 1,402 of them the step starts from a whitespace variant of 9a's old text, because 9b keeps the first of two texts that differ only in whitespace. 11,663 whitespace-only history rows are not versions. That is more than the 11,190 whitespace-only edits in 9a, because 9b also counts the rows that pair inherited commits.

Among copies with two or more versions, the median origin Jaccard is 1.0 at the first version and 0.968 at the last (drift −0.032). A copy that changes at all starts as the origin text and moves away from it slowly. Median Jaccard of a `local` step against its previous version is 0.99, and of the copy against the newest registry text 0.76, so copies stay close to their origin while the registry moves on. 22.8% of dated versions are behind at-date. 268,031 versions (18.4%) have no at-date. All of them are dataset versions whose file has no `last_commit_at`, and every dated version has an at-date.

66,829 versions have change notes: command 245,854, heading 238,271, network 66,260, script 42,018, name 44,677, permission 3,612.

The version shares depend on the link kind as in 9a. `exact`, `near` and `history_exact` versions have median origin Jaccard 1.0, `history_near` 0.94, `cluster` 0.88 and `near_contained` 0.87. 173 versions have a null origin containment and `verified`, all against an origin with no body. The run takes about 65 minutes.

### Effect (dev run)

The dev run (6,170 copies, 27 seconds) scores 6,940 versions: 6,831 from history, 109 from the dataset. 470 copies have more than one version. Kinds: `copy` 6,170, `local` 684, `inherited` 81, `sync` 5. 57 whitespace-only history rows are not versions. Among copies with two or more versions, the median origin Jaccard is 0.784 at the first version and 0.778 at the last (drift −0.006). 32.9% of dated versions are behind at-date (the registry had released a newer version than the origin and the copy text is not it). 79 versions have no at-date, all dataset versions without `last_commit_at`. 371 versions have change notes (heading 1,483, script 986, command 471, name 269, network 47, permission 9). The dev sample is mostly `cluster` links and is only a smoke test.

### Limits

- An inherited commit carries the registry's date, not the date the fork took it.
- A dataset version without `last_commit_at` has no at-date (18.4% of versions on full data), so a share behind at-date is over dated versions only. Stage 7's first-commit date is not used instead: it dates the copy's arrival, not the text the crawl saw.
- A target body under 5 words is one short shingle, which a longer copy never contains, so its Jaccard is 0 even when the line is kept (2 origin rows on full data).
- `behind` compares blobs, so a copy equal to the at-date text except for whitespace counts as behind.
- A copy whose newest history blob has no stored text would end on an older stored version, and a history row without a stored text is skipped rather than ending a step. Neither occurs in the current data (0 history rows without text), so the rule is not built.
- `behind` in the summary is a rough count; stage 10 measures staleness.
- Notes list added lines only; a removed command or URL has no note.

## Stage 9c: the skill folder next to the text

### Problem

A skill is a folder, not only a `SKILL.md`. Scripts in `scripts/` run on the user's machine when an agent follows the skill, so a copier who edits or adds a script changes what the skill does even when the text is the same. 9a and 9b read the text only. 9c asks, for every copy version: which files differ from the registry package the copy started from, from the package current that day, from today's package and from the copy's previous folder; and did an added or edited script gain a capability (network, running commands, deleting files, reading secrets, installing packages, obfuscated code) that the registry's file did not have?

### Decisions

**The same versions and targets as 9b.** 9c reads 9b's version rows and uses the same four targets, so a version's text scores and folder counts sit side by side. The registry targets are package versions (stage 4b), the folder that existed at a date.

**The copy folder is built with the registry's walk (A42).** The copy's stage 6 rows are replayed with the A21 helpers of stage 4b, and a test checks the result equals `rebuild_folder_manifests`. A copy file and a registry file are then the same kind of row, and a git blob id matches across repos.

**The last version runs to the newest commit.** A version's folder is the folder after its commit, except the last version, which takes the folder after the newest commit of the folder. Copiers edit scripts without touching `SKILL.md`; on full data 24,884 copy folders have such later changes, and the copy's current folder is what a user installs.

**The origin package stays inside the origin text version (A43).** It is the newest package version of the origin text version committed by the copy's first date. A registry that added a script after a release without editing `SKILL.md` would otherwise make every later copy look as if it added the registry's own script.

**A copy with no history uses the dataset's file listing.** `artifact_siblings` lists the folder at crawl time with git blob ids, so its files match registry files by sha. It has no file mode, so a script is found by its extension only. GitSkills listed only some folders (`composition_fetched`). Of the 276,029 copies with no history, 248,779 were never listed; they have no folder rather than an empty one. Read as empty, every registry file of those copies showed as removed, and the share of copies whose folder differs from the origin was 24.1% instead of 20.4%.

**Only differing files are stored.** Most copies keep most files as they were, so storing `same` rows would multiply the table for no reader. The summary has one row per compared pair, zeros included, so shares are computed over all pairs.

**A flag is a capability, and unknown is null.** `new_risk_flags` lists flags the new script has and the target's file did not. A script whose text is not stored, a script over `MAX_SCRIPT_BYTES`, and a modified script whose old text is missing get null, not an empty list, so "no flag" is never read where nothing was checked.

### Alternatives rejected

- **The folder at each version's own commit only.** Loses the scripts a copier added after the last `SKILL.md` edit.
- **The origin package at the origin text's release date.** Counts a registry's later script as added by the copier.
- **An unlisted dataset folder read as empty.** Turns a gap in the crawl into removed files.
- **An empty folder when no package existed at a date.** Every copy file would read as added; no row is clearer.
- **Flags on every script in the folder.** A script the registry already had is the registry's capability; `risk_flags` alone cannot say what the copier added. Both lists are kept for the scripts that changed.

### Effect (full run)

The full run compares 1,453,150 versions of 1,275,893 copies in 35 minutes. 248,779 dataset versions have no folder (never listed by GitSkills). The rest give 3,771,118 compared pairs (origin 1,204,371, newest 1,204,371, at-date 1,185,119, prev 177,257) and 9,151,147 differing file rows: removed 4,334,740, modified 2,247,807, added 2,154,702, renamed 148,697, and 265,201 files with the same sha but a new mode, 238,673 of them scripts.

At each copy's last version, against its origin package (1,027,114 copies):

| Link | Copies | Folder differs | Scripts added or edited | New risky script | A file removed |
|---|---|---|---|---|---|
| `exact` | 801,730 | 14.0% | 1.3% | 0.27% | 6.3% |
| `near` | 129,219 | 30.6% | 5.1% | 1.1% | 14.4% |
| `history_exact` | 39,757 | 28.6% | 7.6% | 1.6% | 7.8% |
| `near_contained` | 30,197 | 52.2% | 10.5% | 4.4% | 26.1% |
| `cluster` | 25,534 | 42.7% | 7.9% | 2.4% | 18.9% |
| `history_near` | 677 | 44.8% | 9.8% | 3.1% | 17.7% |

Over all of them, 209,325 copies (20.4%, mode-only changes included) have a folder that differs from the origin package; 24,817 added or edited a script, and 6,105 (0.59%) have a script that gained a flag the registry file did not have: network 3,688, reads_secrets 3,093, runs_commands 2,936, installs_packages 1,250, deletes_files 1,154, obfuscated 94 copies. Most differences are files the copy does not have: aggregator copies keep `SKILL.md` and drop the registry's scripts and references (26,122 copies lack a script of their origin package). 249,558 copies name a script in `SKILL.md` that their folder does not hold, and 98,590 hold a script their `SKILL.md` never names. Missing scripts are often examples in the text (`skill-creator` names `scripts/rotate_pdf.py`), so that count is an upper bound.

### Effect (dev run)

6,940 versions, 69 without a folder, 219,485 differing file rows in 25 seconds. 89.1% of 6,101 copies differ from their origin package and 14.5% have a new risky script; the dev sample is mostly `cluster` links and is only a smoke test.

### Limits

- A flag is a regex match on the script text, comments included. It is a capability, not a verdict.
- A dataset folder without a history has no file mode, so an executable without a script extension is not a script there; snapshot copies cannot show a mode change.
- 248,779 dataset copies have no folder at all; shares are over the copies that have one.
- A version's folder is the folder after its commit; the folder is not split when scripts change between two `SKILL.md` versions, except that the last version runs to the newest commit.
- `missing_scripts` uses A17's mention finder, which also finds example paths in prose.
- The registry side has the same limits as stage 4b: a file without a stored text has no size, so its `size_change` is null.

