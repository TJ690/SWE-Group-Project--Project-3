# Why we built it this way: the choices behind T00 to T10

A plain-language guide for readers who are not programmers. It explains **why** each choice was made, not how to run anything.

**Covers:** T00 (scaffold), T01 (data loading, checking, sampling), T02 (structure features), T03 (commit-history features), T04 (repository context), T05 (label rules v1), T06 (registry sources), T07 (GitHub client), T08 (mining git history), T09 (registry versions), T10 (matching registry to the dataset).

---

## The big picture

The project asks one question: **can we tell whether a skill file (SKILL.md) was written by a human, written with an AI agent's help, or copied from a template?**

The dataset has about 3.8 million skill files. That is far too many to read by hand, so we build a small **assembly line** (a "pipeline"). Each station does one job and hands its result to the next. T00 to T05 are the first stations: build the factory floor (T00), receive and inspect the raw material (T01), take three sets of measurements (T02 file shape, T03 commit history, T04 repository context), and apply the first labeling rules (T05).

T00 to T04 only gather **evidence**. T05 applies the first simple rules that turn that evidence into labels, and it is deliberately a faithful copy of the Sprint 1 rules. Better rules come later.

---

## T00: The factory floor

### Why an assembly line of separate stages, instead of one big program?
- **Easier to fix.** If one stage is wrong, we fix that stage and leave the others alone.
- **Easier to rerun.** If only the labeling rules change, we do not re-download or re-measure everything.
- **Easier to build in pieces.** Each task is a small, reviewable chunk of work, which suits a team project with a deadline.

### Why one settings file (the config)?
Some numbers are judgment calls. Examples: "6 or more skills added together counts as a bulk install," or "10 or more copies counts as a template."

If those numbers were written inside the code in many places, changing our mind would mean hunting for every copy, and we could miss one. With one settings file:
- There is **one place** to change a number.
- A reader (a professor, a teammate) can see **every judgment call** in a single page.
- We can re-run the analysis with different numbers to show the result is not fragile.

### Why does the settings loader refuse unknown or missing settings?
A typo such as `bulk_batch_mim` could otherwise be ignored, and the program would quietly use a wrong value. A loud error at the start is much cheaper than a wrong result discovered weeks later.

### Why is the loaded configuration "frozen" (read-only)?
So no part of the program can quietly change a threshold halfway through a run. Everyone sees the same numbers from start to finish.

### Why a project definition file (`pyproject.toml`) with exact version numbers?
- It lets anyone install the project with one command instead of a long checklist.
- **Pinning exact versions** of every library means a teammate (or the instructor) gets the same behavior we got. Without pinning, a library update next month could change a result and nobody would know why.

### Why "receipts" (provenance) next to every output?
Each output file gets a small record: what the inputs looked like, what settings were used, which version of the code ran, and when. This gives us two benefits:
1. **Skipping needless work.** If nothing changed since last time, the stage skips itself. Some stages take hours, so this saves a lot of time.
2. **Defending our numbers.** We can show exactly how any number in the report was produced. This matters for a research project.

### Why a few big "stage names" (data, features, label, ...) and a Makefile of shortcuts?
One short command per stage means nobody has to remember long, error-prone commands. A stage that is not built yet says so clearly and stops, instead of failing mysteriously or silently doing nothing.

### Why keep downloaded data and secrets out of version control?
The data is several gigabytes and can be re-downloaded. The GitHub access token is a password. Neither belongs in a shared code history.

---

## T01: Receiving and inspecting the raw material

### Why use a database engine (DuckDB) on Parquet files?
Parquet is a compact table format designed for large data. DuckDB can ask questions of those files directly. We chose this because:
- It handles millions of rows quickly on an ordinary laptop.
- We can use SQL, which is well suited to counting and grouping.
- No database server needs to be set up.

### Why "views" instead of copying the data?
A view is like a **window onto the files**, not a photocopy. We never duplicate the multi-gigabyte data, so:
- It saves disk space.
- There is no risk of a stale copy that disagrees with the original.
- Setup is instant.

### Why a "schema check" at the start?
Every later stage assumes certain columns exist and the table is the size we expect. If a download was cut short, or the dataset version changed, we want to find out **immediately and clearly**, not hours later as a confusing crash or, worse, as a silently wrong answer.

The check looks at column names and four row counts (3,797,117 artifacts; 1,877,981 primary rows; 282,200 repositories; 7 mining runs). It is a quick delivery inspection. It does not check data types, empty values or value ranges.

### Why skip the row counts in "dev" mode?
In dev mode we deliberately work with a smaller set, so the full-dataset totals would not match. The column check still runs, because the structure must be right either way.

### Why a "snapshot date"?
The dataset was collected over time. We need a single "today" for questions like "how long ago was this file last changed?" We use the latest collection date in the data, so every calculation agrees on what "now" means.

### Why take a subset (the "dev sample")?
This is the most common question, so here is the full reasoning.

- **Speed of learning.** Running every step on 3.8 million files can take hours. While we are still writing and fixing code, we need to try things many times a day. A 50,000-file sample runs in minutes.
- **Cheaper mistakes.** If there is a bug, we find it in minutes instead of after an overnight run.
- **Same shape, smaller size.** A good sample behaves like a miniature of the whole, so if the code works on it, it very likely works on everything.
- **Full runs happen at the end.** The plan says to use the sample while building and to do the full run only at the end.

The sample is a **development tool**, not a research finding. The final results come from the full data.

### Why is the sample "stratified" and not just random?
Imagine the data is a school with a few huge classes and a few tiny ones. A plain random pick would almost never choose students from the tiny classes, so we could not test our code on them. Rare cases are often exactly where bugs hide.

So we first sort the files into groups ("strata") using three properties, then sample from every group:
- **Where the file sits** in its repository.
- **Whether commit history was collected** for it (yes or no).
- **How many copies exist:** 1, 2 to 9, or 10 or more.

This guarantees that unusual groups, such as heavily copied templates, are present in the sample.

### Why is the sample "seeded" (given a fixed starting number)?
Computers produce "random" numbers from a starting value. By fixing it, we get the **same sample every time**. This means:
- Two teammates test on the same files.
- A bug can be reproduced.
- Results in the report can be re-created.

### Why allow certain files to be always included?
Sometimes we are chasing a bug in one specific tricky file. A "force include" list guarantees that file is always in the sample, no matter what the random pick would have done. It is empty by default.

### Why do later stages restrict themselves to the sample in dev mode?
If the stages used different subsets, their outputs would not line up when combined. One shared list of file ids keeps every stage looking at the same files.

### Why a clear message when the dataset is missing?
A new teammate who has not downloaded the data should be told exactly what to do ("run the download script"), not shown a cryptic error.

---

## T02: Measuring the shape of each file

### Why measure structure at all?
Hand-written files, AI-generated files and copied templates often **look different**: length, how many headings, how many bullet lists, whether there are code blocks, whether the header has a license, and so on. These numbers are fingerprints of style. They are evidence, not proof.

The 15 measurements are: description length, word count, headings, second-level headings, numbered items, bullet items, code blocks, table rows, links, has an "examples" section, has a "when to use" section, header has allowed-tools, header has license, header has version or metadata, and the share of non-plain-English characters.

### Why count things instead of judging quality?
Counting is **objective and repeatable**. Two people (or one program on two days) get the same answer. Judging "is this well written?" would be subjective and could not be defended in a research report.

### Why were these numbers computed twice, once in SQL and once in Python?
- The SQL version already exists and is fast for the whole dataset, so the full run **reuses it** and does not recompute everything in Python.
- But later tasks need the same measurements for files that are **not** in the dataset table, such as historical versions from the 31 reference repositories, and small test examples. Those need a Python version.

### Why must the two versions give identical answers ("parity")?
If the Python version counted a "heading" slightly differently from the SQL version, then comparing a dataset file with a reference file would be like comparing inches with centimeters. Any difference we found might just be a measurement difference, not a real one.

So the Python code deliberately copies the SQL definitions (even small details such as how spaces are trimmed), and a test checks 500 random real files to confirm every number matches.

### Why separate the header (front matter) from the body?
A skill file has a settings header at the top and the instructions below. Some facts live in the header (license, allowed tools) and some in the body (headings, lists). Measuring each in the right place avoids mixing them up.

### Why must the code never crash on a messy file?
Real-world data is untidy: empty files, missing headers, broken formatting. If one bad file crashed the program, we would lose a long run. Instead, a bad header is simply recorded as "not valid" and the run continues. Whether the header was valid is itself a useful fact.

### Why ignore headings inside code blocks?
A line starting with `#` inside a code example is usually a **comment in code**, not a section heading. Counting it would inflate the heading count.

### Why define a "readable" skill (at least 200 characters, not a stub, not a redirect page)?
Some files are not real skills: a one-line pointer to another file, or a "Redirecting..." page. Measuring them would add noise, so the card defines them out. (Note: the copy step in `run()` takes whatever the existing SQL table contains. We have not verified in the code that this filter is applied there, so confirm before citing it in the report.)

---

## T03: Reading the clues in the commit history

If T02 asks "what does the file look like?", T03 asks "**how did it arrive?**"

### Why look at commit history?
Because AI coding tools often leave **traces in the history**, and bulk tools leave a pattern. This is some of the most direct evidence we have.

### Why count only the "Co-authored-by" line as an AI trailer?
Tools like Claude Code add a line such as `Co-Authored-By: Claude ... <noreply@anthropic.com>` to the commit message. That is the tool **declaring itself as a co-author**.

A message that merely mentions Copilot ("fix copilot docs") says nothing about who wrote the commit. If we counted every mention, we would label ordinary human work as AI-assisted. Counting only the declared co-author line keeps the signal **precise**, even if it misses some cases.

### Why check both the first and the last commit?
The first commit shows how the file was **created**. The last shows who **touched it most recently**. A file can start human and later be edited by an AI tool, or the reverse. Recording both preserves that story.

### Why separate "coding-agent bots" from "other bots"?
Not every bot is an author.
- A **coding-agent bot** (names like claude, cursor, copilot, devin, jules, gemini) writes content. That is an authorship signal.
- A **plumbing bot** (github-actions, dependabot, release, sync, publish) just moves or updates files that someone else wrote. Treating it as an AI author would be wrong.

The plumbing check runs first, so a bot called something like `claude-sync-bot` is treated as plumbing. A bot we do not recognize defaults to "plumbing," because we do not assume AI authorship without evidence.

### Why track batch size and "bulk"?
If one commit adds many skills to the same repository at the same moment, a person almost certainly did not write them one by one. It looks like an **installer, a sync, or an import**. We count how many skills share the same repository and first-commit time, and flag 6 or more as "bulk" (the threshold lives in the settings file).

### Why count batches on the whole population, even in dev mode?
If we counted batches only inside the 50,000-file sample, big batches would look smaller than they are, and real bulk installs could be missed. The batch size is therefore measured on the full set and then attached to the sampled files.

### Why classify commit messages?
Messages such as "install", "sync" or "import" suggest a tool moved the file, "initial commit" suggests a repository being set up, and a "feat:" prefix suggests a developer following a convention. We record this as **context**. It is deliberately **not** used as an input to the labels, because it is too weak to decide authorship on its own.

### Why use only the first matching message class?
So each message gets exactly one class, in a fixed order of priority. That keeps the result simple and repeatable.

### Why is a missing message "other" and not "short"?
"We have no message" and "the message was very short" are different facts. Mixing them would invent information we do not have.

### Why leave blanks when there is no history ("never guess")?
For some files the commit history was not collected. We do not know who wrote them, so we record **unknown**, not "no AI" or "human." Filling in a guess would create false confidence and could quietly bias the results. Later steps can then honestly report "insufficient evidence."

### Why no labels yet?
Gathering evidence and interpreting it are separate jobs. Keeping them apart means we can change the interpretation rules later without re-collecting everything, and we can show a reader exactly which evidence led to which label.

---

## T04: Looking at the neighborhood, not just the house

If T02 asks "what does the file look like?" and T03 asks "how did it arrive?", T04 asks "**what kind of place does it live in?**"

### Why look at the repository at all?
The same poster means something different in a print shop's sample wall than in a child's bedroom. A skill inside a repository with 500 other skills is probably part of a collection or template library. The same skill in a one-file personal project is more likely someone's own work. The repository does not say who wrote the skill, but it changes how much weight the other evidence deserves.

### Why sort repositories into three kinds?
Each repository is put into exactly one type, and the first matching rule wins:
- **A, collection:** the name looks like a collection, or the repository is large (more than 100 skill files), or at least half its files are copies.
- **B, agent tooling:** not a collection, but the name mentions "skill" or an AI agent.
- **C, ordinary project:** everything else.

Grouping like this lets the report say things like "among ordinary projects, X% showed AI signals" instead of mixing very different settings into one number.

### Why "first match wins" and only one kind per repository?
It keeps the result simple and repeatable. Every repository gets exactly one answer, so nothing is counted twice.

### Why flag names that look like tests, demos or benchmarks?
Repositories named `sample`, `bench`, `test`, `demo`, `mirror` or `collection` are often built *from* other people's skills, or filled with AI-generated examples on purpose. They would distort the picture of how people really write skills. The flag lets us see them, filter them out, or re-run the analysis without them. The known case `MalSkillBench` must be flagged.

### Why measure the repository's age up to the "snapshot date" and not today?
The dataset was collected on a certain date. Using that date as "now" means the numbers come out identical no matter when anyone runs the code. If the creation date is missing or unreadable, the age is left blank rather than guessed.

### Where do the columns come from?
- Some are **copied** from the raw repository table: owner, stars, language, created and last-updated dates.
- Some are **calculated earlier** by `db/repos.sql`: the number of skill files, the share that are copies, the size bucket, and three yes/no flags from keyword searches on the repository name (mentions "skill", looks like a collection, mentions an agent).
- Three are **calculated in T04**: the repository kind, the benchmark-like-name flag, and the age.

### Why does dev mode keep every repository, not just a sample?
The 50,000-file sample picks *files*, but a repository's profile (for example, its copy share) describes the *whole* repository. Cutting repositories down would make those numbers wrong. The table is also small, so there is no speed reason to cut it.

### Limits to be honest about
- **Keyword searches on names are rough.** A search for "hub" also matches "github", so repositories under the `github` owner (for example `github/spec-kit`) look like collections no matter how small they are. A search for "test" also matches "latest" and "contest". We have not measured how many repositories this affects.
- The name searches look at the **whole `owner/repo` text**, so an owner called `agent-labs` makes all its repositories count as "mentions an agent".
- The size cutoffs and the 0.5 copy-share cutoff are fixed in the code, not in the settings file, so they cannot be changed for a sensitivity test without editing code.

---

## T05: Turning the evidence into labels (version 1)

T02 to T04 collected evidence. T05 applies the first set of rules that turn that evidence into one of four labels per skill. It is called "v1" because it is a faithful copy of the Sprint 1 rules. Better rules (v2) come later in T17.

### Why four labels and not a simple yes or no?
The data cannot prove who wrote a skill. Many skills have no history, arrive in bulk, or show no clear sign either way. A fourth label, "insufficient evidence," lets the study say "we do not know" honestly. About 90% of skills end up there. That is a finding about the limits of the evidence, not a failure.

The four labels and their Sprint 1 counts:

| Label | Plain meaning | Count |
|---|---|---|
| highly templated | Same text appears in many places | 35,768 |
| agent assisted | Signs an AI coding agent was involved | 80,114 |
| likely human | Everything points to one person adding it alone | 62,798 |
| insufficient evidence | Not enough clear signals | 1,699,301 |

They add up to 1,877,981, the number of unique skills. Every skill gets exactly one label.

### Why are the rules checked in a fixed order?
Like a triage desk with colored lanes, the first rule that fits decides the lane. The order is: templated, then agent assisted, then likely human, then everything else.

### Why does "templated" come first?
When the same text appears 10 or more times, that fact says more than who happened to commit one copy. So a heavily copied skill is "templated" even if one copy also carries an AI signature. A test checks that templated beats agent assisted.

### Why is "agent assisted" limited to small batches?
A bot that installs 30 skills in one second is not evidence of an AI *writing* them. So the rule requires fewer than 6 skills in the batch, that the first committer is not a plumbing bot, and that there is either a coding-agent author or an AI co-author line.

### Why is "likely human" so strict?
It requires a lone commit, an ordinary person's account, no AI marks on the first or last commit, and exactly one copy. We want this to be a *confident* human bucket, not a catch-all for "not obviously AI." Anything that fails any condition falls to "insufficient evidence."

### Why does every label come with reason codes?
Codes such as `copies>=10`, `ai_trailer_first:claude`, `bulk_batch>=6` or `no_history` show **why** a skill got its label. This makes errors easy to investigate and lets the report say, for example, how many "insufficient evidence" skills are there simply because history was never collected.

### Why port the old rules exactly, even if newer definitions exist?
Version 1 must reproduce the published Sprint 1 counts to the digit. If we improved a rule while porting it, we could not tell whether a changed count came from a bug or from the improvement. So v1 is a faithful copy that acts as a **regression anchor**: a fixed reference we can always compare against. Improvements are made later, in v2, one at a time.

For this reason T05 does not simply reuse T03's columns. It recomputes its own evidence from the raw data using the Sprint 1 definitions, because T03's cleaner definitions would not reproduce the published counts. In v1:
- An AI co-author match can span lines of the message, where T03 requires the tool name to be on the same line.
- The list of coding-agent bot names is longer.
- Plumbing bots are recognized slightly differently.

### Why does the regression test compare four exact numbers?
If the full run gives 35,768 / 80,114 / 62,798 / 1,699,301 and they add up to 1,877,981, the port is faithful. If any count differs, something changed and we investigate before moving on. This test is slow, so it is tagged and only runs when asked.

### Why no confidence score in v1?
Sprint 1 did not have one. Adding it now would break the exact match. It arrives in v2.

### Why does T05 not use the repository kind from T04?
The v1 rules only use copy count, history, batch size, author type and AI trailers. Repository kind becomes useful in v2 and in how we build and report the validation sample.

### Limits to be honest about
- These are **rule-of-thumb labels, not ground truth.** The 240-skill human validation is what measures how accurate they are.
- AI use that leaves no trailer and no bot author looks like human work, so some "likely human" labels will be wrong.
- Because v1 trailers can match across lines, an unrelated later mention of a tool in a commit message may be counted as a signal.
- Copy count is global: it counts every row in the dataset with the same content, including copies in other repositories.

---

## T06: The guest list of reference repositories

### Why a "registry" at all?
The dataset has 282,200 repositories. Some of its skills are copies or edits of skills published by well-known sources, such as Anthropic's official `anthropics/skills`. If we hold the **originals with their full version history**, we can ask questions the dataset alone cannot answer: Is this skill a copy of an official one? Which version was copied? Is that version out of date now? The registry is our set of 31 reference sources to compare against.

### Why write the list down as a settings file and check it?
It is cheaper to catch a typo in a list of 31 names at the start than after a long download fails hours later. The loader refuses a bad tier (only A, B or G are allowed), a name that is not in `owner/name` form, and a "mirror" that points to a repository not on the list.

### Why three tiers (A, B, G)?
So results can be compared by kind of source: **A** is provider-official (Anthropic, OpenAI), **B** is famous community maintainers, **G** is Google/Gemini and GitHub/Copilot. A finding such as "copies of official skills go stale faster than copies of community skills" needs this split.

### Why mark mirrors?
`flutter/agent-plugins` is a copy of `flutter/skills`. Counting both would count the same skills twice and make reuse look larger than it is. The loader checks that every mirror marker points to a real repository, and later steps skip mirrors.

### Why does T06 use no network?
It only reads a local file and works out folder names (`owner__repo`). That keeps it fast, easy to test, and free of any rate-limit worries.

---

## T07: A careful way to ask GitHub questions

### Why a separate "client" and not just ask GitHub when needed?
Several later tasks need to ask GitHub's web service for things a clone does not hold: a repository's stars and license, and the history of a live sample of about 300 other repositories. Doing this carefully takes a lot of small rules. Writing them once means no other task has to remember them.

### Why a token?
Without one, GitHub allows 60 questions an hour. With a free read-only token it allows 5,000. The live sample needs up to 2,000, so a token is required for it. The token is never written to files, logs or test data.

### Why a cache?
The first answer to a question is saved with a label from GitHub. Next time the client asks "has this changed since that label?" and GitHub replies "no" for free, without using the quota. Re-running the project costs nothing for questions already answered.

### Why wait, retry and stop?
- When fewer than 50 questions remain, the client **waits until the quota resets**, instead of being blocked.
- On a temporary GitHub error it **retries up to 5 times**, waiting longer each time.
- A "not found" answer returns nothing rather than crashing the run.
- A **hard budget of 2,000 calls** stops a bug that loops from burning the whole quota.

### Why does T07 produce no results of its own?
It builds a tool, not data. The only file it creates is a small cache database, which can be deleted at no loss. Later tasks use the tool and save what they learn.

### Why is git preferred over the API whenever possible?
Cloning a repository's history has no rate limit and needs no token. The API is used only for repository facts and for the small live sample. This is a standing project rule.

---

## T08: Reading the full history of the reference skills

### Why clone with "no file contents"?
A "treeless" clone downloads the **list of commits and files but not the file text**. That keeps all 31 repositories to about 1 GB and a few minutes, instead of many gigabytes. History is all we need at this stage.

### Why can we compare files without having their text?
Git gives every file content a fingerprint (a "blob" ID) computed from the exact contents. The dataset's `file_sha` column is the same kind of fingerprint. If a registry fingerprint equals a dataset fingerprint, the two files are **identical, byte for byte**, and no text is needed to know that.

### Why record a fingerprint per commit?
It tells us when a skill changed (the fingerprint changed), when an old text came back (an old fingerprint reappeared), and which dataset files are exact copies. The commit message and date come along with it, and a second table records AI co-author lines.

### Why store full commit IDs?
Short IDs can change length as a repository grows. Full IDs never change, so results stay valid.

### Why ignore renames and skip deleted files?
Renames are turned off so that a move is simply a delete plus an add, which is simple and predictable. Delete rows are dropped from the output. This keeps the first version small, but it has a cost, described under T10's limits.

### Why skip mirrors?
They share their origin's history, and cloning them would only duplicate rows.

### Why is the history of a skill still found when its folder no longer exists?
The mining asks git for every commit that touched any file named `SKILL.md`, at any path, at any point in time. It does not look at today's file tree. For example, `GoogleCloudPlatform/gke-mcp` removed its whole `skills/` folder on 2026-08-14, but its 25 skills still appear in the history.

### Why can big repositories be run one at a time?
Two repositories are large (about 18,700 and 3,900 commits). Running them separately, and being able to resume, avoids losing hours of work to an interruption.

### Three places commit history comes from
1. The 31 registry repositories: **our own git mining** (this task).
2. The 1.9 million dataset skills: **already in the dataset**, collected by its authors. We only read it.
3. The live sample of about 300 repositories: the **GitHub API**, with a budget.

---

## T09: Turning a raw diary into a catalog of versions

### Why collapse to one row per distinct fingerprint?
T08 produces a diary with one row per commit. What we actually want is a catalog with one row per **distinct version** of each skill. Rows are grouped by repository, skill name and fingerprint. A commit that touches a file without changing it, or a text that is restored later, then adds nothing new.

### Why does `n_commits` sometimes exceed 1?
It counts how many commits showed that exact text. Two examples from the real repository `anthropics/skills`, checked on 2026-10-23:
- **`claude-api`:** 26 commits, 26 different fingerprints, so 26 versions with `n_commits = 1` each. It was first added on 2026-03-04 and grew from about 18 KB to about 105 KB.
- **`algorithmic-art`:** 1 version with `n_commits = 3`. The text never changed. The file was **moved twice** (a top-level folder, then `examples/`, then `skills/`), and each move was a commit showing the same text.

This is also why the pipeline follows fingerprints and not file paths. A path changes when a repository is reorganized, but the fingerprint follows the content.

### Why number the versions?
`version_seq` (1, 2, 3...) puts versions in order by date, with commit order as the tie-break. Later steps need "the next version" and "the latest version" to measure whether a copy is out of date.

### Why record whether an AI co-author line appeared when a version was introduced?
It lets us ask whether visible AI involvement relates to how a skill evolves. In `claude-api`, three of the 26 versions came in with an AI co-author line (from a quick check, not the project's exact rule).

### Why flag versions after the snapshot date?
The dataset was collected on a certain date. A registry version first published **after** that date cannot have been copied into the dataset. Without the flag, such versions would look like missed matches.

### Why look at how the description starts?
Descriptions follow recognizable opening formulas ("Use this skill whenever...", "Use when...", "Guide for...", or a plain summary of what the skill does). The first words are a possible style signature, so each version gets a short class label.

### Why keep both rows when the same text appears under two skill names?
It signals a rename or copy inside a repository. Both rows are kept and share a group ID, so no information is thrown away.

---

## T10: Which dataset skills are copies of the reference skills?

### Why match by fingerprint?
Because it is fast, exact and needs no text. A registry fingerprint equal to a dataset `file_sha` means an identical file. Across the whole dataset, about 6,719 distinct registry fingerprints match 216,440 dataset files, roughly 5.7% of the 3.8 million.

### Why that number is a lower bound
A copy with even one changed character gets a different fingerprint and is missed. Edited copies are handled later (T15) with a similarity score. So 5.7% is the share of **exact** copies only.

### Why decide who is the "origin" by earliest date?
The same text can appear in several registry sources. We call the source that published it first the origin and the others redistributors. This is a reasonable rule, though not proof: the text may have been created elsewhere and published first in a registry we happen to track.

### Why summarise "lineages"?
A lineage is all the versions of one skill. For each, we count how many versions exist, how many of them appear in the dataset, how many copies there are, and how many owners hold them. This is the material for asking whether people copy the latest version or old ones.

### Why count each distinct fingerprint once, and ignore the source repository itself?
The same text listed in two sources must not be counted twice, and a repository that publishes a skill obviously contains it. Neither should count as a downstream copy.

### Limits to be honest about
- **Deleted skills are not recorded.** T08 drops delete rows, and no later table has a "retired" flag. So copies of skills that their source later removed (for example, the 25 `gke-mcp` skills) still match, and the 5.7% includes them. A retired skill's last version looks like the "latest" version, even though nobody maintains it any more. This can make copies look more current than they are.
- Moves and deletions cannot be told apart in the raw log. Moves work correctly by accident because the fingerprint stays the same.
- The registry is 31 chosen sources, not all of GitHub.
- "Origin = earliest date" is a convention, not proof of authorship.
- A fix would be to keep delete events in T08 and add a "retired" date to each lineage, so later steps can treat retired lineages separately.

---

## Common themes across T00 to T10

| Theme | What it means in practice |
|---|---|
| **Reproducible** | Fixed seed, pinned versions, receipts. Anyone can get the same numbers. |
| **Honest about the unknown** | Blanks instead of guesses. Strict config. Early failure instead of silent errors. |
| **Evidence before judgment** | T00 to T04 measure. T05 applies simple, explainable rules, and every label carries its reasons. |
| **Fingerprints over file paths** | Files are identified by a content fingerprint, so moves and renames do not create fake new skills (T08 to T10). |
| **Git before the web service** | History comes from cloning wherever possible. The GitHub API is kept for repository facts and a small, budgeted sample. |
| **Faithful before better** | v1 copies Sprint 1 exactly, so later improvements can be measured against a fixed reference. |
| **Small and fast while building** | Dev sample, skipped reruns, views instead of copies. |
| **One place for each decision** | Settings in one file, definitions shared between SQL and Python. |

## Limits to be honest about

- The dev sample is for building, not for reporting final results.
- The schema check verifies column names and four counts only. It does not check data types or empty values.
- AI trailers only catch tools that **declare themselves**. An AI-assisted commit with no trailer looks like a human commit, so the signal has good precision but limited coverage.
- A bot's login name is a rough guide, not proof of what the bot does.
- Structure measurements (T02) are style hints, not proof of authorship.
- Repository kind (T04) depends on keyword searches of names, which can misfire (for example "hub" inside "github").
- Registry matching (T10) finds exact copies only, so its 5.7% is a lower bound. Deleted skills are not recorded, so copies of retired skills still match and can look current.
- The v1 labels (T05) are rules of thumb. About 90% of skills are "insufficient evidence," and the human validation sample is what measures accuracy.
