# New data sources to extend the GitSkills provenance/authorship study (Sprint 2 acquisition plan)

Baseline (what we already have, from local repo files, so it is NOT recommended as new data):
- GitSkills (`mvaccargiu/gitskills`, July 2026): 3,797,117 SKILL.md rows / 1,877,981 distinct `file_sha`; for representatives only: content, front matter, siblings, and for 458,548 (24.4%) the **first and last commit only** (timestamp, anonymised author code, author type, redacted message), `commit_count`. `repos`: stars, forks, is_fork, language, license, description, created_at, pushed_at. No contributor counts, no topics, no PR linkage, no full commit list, no committer vs author split, no copies' history, no fork parent, no data for AGENTS.md/CLAUDE.md/.cursor rules. Author keys are not distributed, so we cannot link anonymised codes to real accounts. (Local: `DATA_DICTIONARY.md`, `src/data/gitskills_data/README.md`.)
- Already planned locally: `config/registry_sources.yaml` lists ~32 provider/community skill repos (anthropics/skills, openai/skills -> openai/plugins, obra/superpowers, google/skills, github/awesome-copilot, etc.), most `status: mined` in a 2026-10-15 pilot; candidate: openai/plugins and Microsoft skill repos. `.env.example` has only `GITHUB_TOKEN`. So GitHub REST via a PAT is already the planned mechanism; new recommendations below focus on fields/sources beyond that.

---

## Q1. GitHub REST & GraphQL APIs (per-file history, trailers, identities, PRs, repo metadata, contributors, code search), limits, tooling

### Takeaway
The GitHub API is the cheapest way to fill GitSkills' biggest gaps (full per-file commit list instead of first/last only, committer vs author, PR linkage, bot PRs, topics/fork parent/contributors) for a targeted sample (tens of thousands of skills), but code search is far too throttled (10 req/min) to rediscover at scale; for the deep history of many skills, a blobless `git clone` plus `git log --follow` is faster and costs no API quota.

### Cited Findings
- REST primary limits: 60 req/h unauthenticated (per IP); 5,000 req/h per user for a PAT; GitHub App installation tokens at least 5,000/h, scaling +50/h per repo or org user beyond 20, capped at 12,500/h; `GITHUB_TOKEN` in Actions 1,000/h per repo. — [GitHub REST rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
- Secondary limits: at most 100 concurrent requests (REST+GraphQL shared); 900 points/min for REST (GET = 1 point) and 2,000 points/min for GraphQL; 90 s CPU per 60 s real time (max 60 s for GraphQL). — [GitHub REST rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
- GraphQL: 5,000 points/h per user; cost = (sum of requests needed per connection assuming `first`/`last` fully used) / 100, rounded, minimum 1; every connection needs `first`/`last` 1–100; max 500,000 nodes per call; requests over 10 s time out (502/504) and deduct extra points. — [GitHub GraphQL rate limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api)
- Code search requires authentication and is limited to **10 requests/minute**; other search endpoints 30/min authenticated; max 1,000 results per search; only the default branch and files < 384 KB are indexed; queries max 256 chars and ≤5 AND/OR/NOT operators. — [GitHub Search REST API](https://docs.github.com/en/rest/search/search)
- GitSkills authors confirm the 1,000-result cap forced partitioning by file size and that `total_count` was unreliable (~349k reported vs 3.8M retrieved). — local `src/data/gitskills_data/README.md` (dataset card; also [arXiv:2608.10906](https://arxiv.org/abs/2608.10906))
- `GET /repos/{owner}/{repo}/commits` supports `sha`, `path` (only commits touching that file), `author`, `committer`, `since`, `until`, `per_page` (max 100), `page`. Responses include `commit.author` / `commit.committer` git identities **with email**, plus top-level linked GitHub `author`/`committer` users (can be null). — [GitHub Commits REST API](https://docs.github.com/en/rest/commits/commits)
- `GET /repos/{owner}/{repo}/commits/{commit_sha}/pulls` returns PRs associated with a commit (answers "was the skill added via a PR, by whom, was the PR opened by a bot/agent"). — [GitHub Commits REST API](https://docs.github.com/en/rest/commits/commits)

### Inferences
- **New fields obtainable per skill (representatives with known repo+path):**
  - Full commit list touching the file (all commits, not only first/last) -> edit-churn, number of distinct human vs bot editors, time to first human edit after an agent-authored commit. Endpoint: `GET /repos/{o}/{r}/commits?path=.claude/skills/x/SKILL.md&per_page=100`. One call per skill for ≤100 commits (most skills have few commits).
  - Full raw commit message incl. all trailers (`Co-authored-by`, `Generated-by`, `Signed-off-by`, Claude Code's "Generated with Claude Code" footer) of every commit, not just first/last; we must redact emails ourselves.
  - **Author vs committer split** (GitSkills keeps one "author"): agent workflows often show committer = `GitHub` (web-flow) or bot, author = human, or vice versa; a `[bot]` committer such as `copilot-swe-agent[bot]`, `claude[bot]` is an additional agent signal.
  - PR linkage per first commit: `commits/{sha}/pulls` -> PR author login & type, `user.type == "Bot"`, PR body (agent PRs often carry tool footers), labels, merged_by.
  - Repo metadata missing from GitSkills: `topics`, `parent`/`source` (fork genealogy), `has_discussions`, `archived`, `subscribers_count`, contributor list (`GET /repos/{o}/{r}/contributors?per_page=1&anon=1` and read the `Link` header last page for a cheap contributor count), and owner type/created_at (`GET /users/{login}`) for account-age features.
- **GraphQL is more quota-efficient for batched repo metadata**: one query can alias ~50–100 repositories (`repository(owner:,name:){stargazerCount forkCount repositoryTopics(first:20){...} parent{nameWithOwner} isArchived createdAt}`) for roughly 1 point; file history via `object(expression:"HEAD"){... on Commit{history(path:"...", first:100){nodes{oid committedDate message author{user{login} email name} committer{...} associatedPullRequests(first:1){nodes{number author{login __typename}}}}}}}` gets commits + PR linkage in one call. (Point formula from GitHub docs above; the specific query shape is from prior knowledge of the GraphQL schema, verify in the GitHub GraphQL Explorer.)
- **Throughput estimate (PAT, 5,000/h):** ~120k REST calls/day per token if run continuously. Enriching e.g. a stratified sample of 30k skills x (1 history + 1 PR call) = 60k calls = ~12 h of quota for one token; 3-4 student tokens finish in an evening. The 282,200 repos' extra metadata via GraphQL batches of ~50 = ~5,600 queries = ~1–2 h.
- **Conditional requests**: send `If-None-Match` with the stored ETag; GitHub has historically documented that 304 responses do not count against the primary limit (prior knowledge; the rate-limit page fetched this session does not state it — verify on "Best practices for using the REST API"). Useful for re-crawls in Sprint 3.
- **Code search** at 10 req/min x 100 results = at most 1,000 results/min, and only for partitioned queries; re-discovering SKILL.md at scale is infeasible in one sprint. Use it only for *new artifact types* in a small targeted way: e.g. `filename:AGENTS.md`, `filename:CLAUDE.md`, `path:.cursor/rules`, `filename:.cursorrules` restricted to the 282,200 GitSkills repos (`repo:` qualifier) or size-partitioned (`size:1000..1200`), to learn whether a repo with skills also has agent instruction files (a strong "agent-native repo" covariate).
- **Clone alternative (no API quota, best for deep history):** `git clone --filter=blob:none --no-checkout https://github.com/o/r` fetches all commits/trees but no blobs; `git log --follow --format='%H%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%cI%x1f%B%x1e' -- path/SKILL.md` then gives full history incl. renames (`--follow`) and trailers (`git log --format='%(trailers:key=Co-authored-by)'`). Blob contents for each historical version can be pulled lazily with `git show <sha>:path`. This is the right tool to measure *how the SKILL.md text evolved* (did a human rewrite an agent draft?), which GitSkills cannot answer. Clones go through git protocol, not the REST quota (prior knowledge; GitHub may still throttle very aggressive cloning).
- **Libraries:** PyGithub (simple, sync, handles pagination), ghapi (thin, auto-generated from OpenAPI), or `httpx`/`aiohttp` with an async semaphore ≤ ~20 concurrent and backoff on `403/429` + `Retry-After` / `x-ratelimit-reset` headers; `gql` or plain httpx POST for GraphQL. Recommended: httpx + cache responses to disk (SQLite/parquet) keyed by URL so the team never pays twice.
- **Effort:** 3–5 person-days for a sampled REST/GraphQL enricher (history + PR + repo extras) with caching and redaction; +2 days for the clone-based history miner.

### Gaps
- I did not verify in this session whether 304 conditional responses are exempt from the primary limit (page fetched did not state it).
- Exact GraphQL point cost for `history(path:)` + nested `associatedPullRequests` must be measured with the `rateLimit { cost remaining }` field.
- Whether GitSkills' `file_sha` is the git blob SHA-1 (as code search returns) is not stated in the dataset card; it is very likely (code search returns blob `sha`) but should be verified by recomputing `git hash-object` on a few `content` values. This matters for Software Heritage/WoC joins (Q2).

---

## Q2. Large-scale sources (GH Archive, GHTorrent, Software Heritage, World of Code, BigQuery github_repos, AIDev, other HF agent datasets)

### Takeaway
Software Heritage (lookup by content hash -> origins/provenance) and World of Code (blob -> first commit/author across all of open source) are the only sources that can answer "who published this exact text first" across *all* repos, including copies GitSkills did not enrich; GH Archive adds cheap event-level context (PR events by bots, push timing). AIDev and AgentPack are labelled agent-activity datasets but their cutoffs (mid-Aug 2025) predate the SKILL.md format (Oct 2025), so they help mainly as ground-truth for agent commit/PR style and for AGENTS.md/CLAUDE.md, not for skills directly.

### Cited Findings
- **Software Heritage provenance**: `whereis` takes a core SWHID and returns a qualified SWHID with an anchor (release/revision) and origin URL, or None; `whereare` is a POST batch variant. The docs explicitly say it does **not** guarantee the earliest occurrence ("best answer" = oldest revision by commit date, but commit dates cannot be fully trusted; "quality of the result is not guaranteed"). The endpoint **is not publicly available and requires authentication and special user permission**. — [swh.provenance interface](https://docs.softwareheritage.org/devel/apidoc/swh.provenance.interface.html); [swh-web provenance API](https://docs.softwareheritage.org/devel/swh-web/uri-scheme-api-provenance.html); [SWH Provenance overview](https://docs.softwareheritage.org/devel/swh-provenance/index.html) (summary via search results; the API pages themselves returned an Anubis bot-block when fetched)
- SWH Web API authentication: OpenID Connect bearer token from an account on the SWH Identity Provider (`swh auth generate-token`); authentication "can notably allow to lift API rate limiting". — [swh-web-client docs](https://docs.softwareheritage.org/devel/swh-web-client/index.html)
- SWH rate limit figures: a 2021 Guix mailing-list post states an account raises the limit to 1,200 (vs 120) requests per hour; no official current figure found. — [Guix list, 2021](https://yhetil.org/guix/87sfx3l1sr.fsf@gnu.org/) (secondary, dated)
- **World of Code**: commit-centred maps; studies chain project->commit (p2c), commit->blob (c2b), and blob->author (b2a) to find the **first commit/author introducing each blob** and the project of that first commit. — [Hackathon code reuse study, arXiv:2103.10167](https://arxiv.org/pdf/2103.10167); [WoC research workflow paper, arXiv:2010.16196](https://arxiv.org/pdf/2010.16196)
- WoC release V2604 includes a global author-identity map: 62.7M developer identities from 106.8M author strings over 5.87B commits. — [arXiv:2607.06183](https://arxiv.org/pdf/2607.06183)
- **GH Archive**: hourly public GitHub event archive, mirrored to BigQuery as year/month/day tables (e.g. `githubarchive.day.20150101`). — [gharchive.org](https://gharchive.org/)
- BigQuery on-demand pricing $6.25/TiB scanned with 1 TiB/month free; cost is driven by columns selected (LIMIT does not reduce cost); a narrowed 2025 PushEvent query was estimated at ~50 GB (~$0.31) vs >1 TiB for `SELECT *`. — third-party guide [github-archive-guide (via tessl registry)](https://tessl.io/registry/skills/github/gadievron/raptor/github-archive) (non-authoritative; confirm in Google's pricing page and with a dry run)
- **AIDev**: ~932,791 agent-authored PRs (OpenAI Codex, Devin, GitHub Copilot, Cursor, Claude Code) across 116,211 repos and 72,189 developers, cutoff **2025-08-01**; curated subset of 33,596 PRs from 2,807 repos (>100 stars) with comments, reviews, commits, related issues. HF `hao-li/AIDev`, Zenodo 10.5281/zenodo.16899501; paper arXiv:2602.09185 (MSR '26). — [AIDev paper](https://arxiv.org/html/2602.09185v1); [HF papers page](https://huggingface.co/papers/2602.09185)
- AIDev table lists 5,137 Claude Code PRs from 1,643 developers across 1,915 repos. — [SAILResearch/AI_Teammates_in_SE3](https://github.com/SAILResearch/AI_Teammates_in_SE3)
- **AgentPack** (nuprl, HF, gated): ~1.3M commits co-authored by Claude Code, OpenAI Codex, Cursor Agent in public GitHub projects, April–mid-August 2025. — [HF nuprl/AgentPack](https://huggingface.co/datasets/nuprl/AgentPack)
- A widely shared SemiAnalysis estimate says Claude Code authors ~4% of public GitHub commits (secondary summary, unverified). — [thenote.app summary](https://thenote.app/post/en/analysis-claude-code-currently-authors-4-of-all-public-github-commits-and-is-on-mdhhnbu3jf)

### Inferences
- **Software Heritage, what to use in practice:** the public archive API (prior knowledge, verify): `GET https://archive.softwareheritage.org/api/1/content/sha1_git:{file_sha}/` (does SWH have this blob? returns sha1/sha256/length), `GET /api/1/origin/{url}/visits/` (first archival visit date of a repo, an independent timestamp not controlled by commit dates), and the SWH graph / `whereis` provenance (needs approved access — request it by email early, it may not arrive within the sprint). SWHID for a blob is `swh:1:cnt:<git blob sha1>`, so if `file_sha` is the git blob SHA it joins directly. Main added value: **first archival visit timestamp** per origin as a tamper-resistant lower bound on "when did this text exist publicly", and existence of the same blob in non-GitHub forges (GitLab, Codeberg). Rate limit (~120/h anonymous, ~1,200/h with token, unverified) means sample ≤ ~10–20k blobs per sprint.
- **World of Code** is the strongest "who published first" source (b2fa-style blob->first author/commit maps across all forges), but access requires requesting an account from the WoC team (UTK) and running queries on their servers; turnaround may exceed one sprint. Also WoC version cadence: V2604 (April 2026) would cover skills only up to ~early 2026. Recommend: apply for access in week 1; treat as stretch.
- **GH Archive (BigQuery)**: new fields: `PullRequestEvent` with `actor.login` (bot actors such as `Copilot`, `claude[bot]`, `devin-ai-integration[bot]`), `PushEvent` commit messages (first ~20 commits per push in payload, prior knowledge) for the GitSkills repos, `CreateEvent` (repo creation) and `ForkEvent` timestamps, `WatchEvent` star timing (star growth after skill publication). Filter `repo.name IN UNNEST(@gitskills_repos)` on 2025-10..2026-07 month tables and select only needed columns; expect tens to hundreds of GB -> within the 1 TiB free tier if careful. Effort: 1–2 person-days.
- **BigQuery `bigquery-public-data.github_repos`** (prior knowledge): last substantially refreshed years ago and is a ~3M-repo snapshot; it will not contain SKILL.md (post-Oct-2025). Useful only as a **known-human pre-LLM text corpus** (README/markdown `contents` from before late 2022), see Q4. Its `contents` table is ~2–3 TB, so a single unfiltered scan costs >$10; query `files` first and join narrowly.
- **GHTorrent** (prior knowledge): discontinued around 2019–2020; no data for the skill era. Do not use.
- **AIDev / AgentPack**: no overlap in time with SKILL.md (cutoffs Aug 2025). Use for (a) labelled agent PR/commit messages to train/validate the commit-message classifier, (b) the list of agent bot logins and trailer formats per tool, (c) repos in AIDev that later added skills (join on repo name with GitSkills -> "agent-using repos" covariate). Effort: 0.5–1 day each (HF `datasets.load_dataset`, AgentPack requires accepting terms on HF).

### Gaps
- Could not read SWH API pages directly (Anubis bot protection); endpoint paths for the public content/origin APIs are from prior knowledge.
- No official current SWH rate limits found.
- WoC access procedure and turnaround time not confirmed this session.
- I did not find a HF dataset of agent commits/PRs covering Oct 2025–2026 (the skill era); a newer one may exist (e.g. an AIDev update) but none surfaced.

---

## Q3. Skill registries / marketplaces as sources

### Takeaway
skills.sh (Vercel) is the richest new source: a documented JSON API giving per-skill **install counts** (all-time, trending, hot), source repo, content SHA-256 + files snapshot, duplicate/fork flags, and third-party **security audit** results — none of which GitSkills has. Claude Code plugin marketplaces (`.claude-plugin/marketplace.json`) give declared upstream source repos/owners for skills bundled in plugins, which helps resolve copies to origins. SkillsMP gives only a heavily throttled search.

### Cited Findings
- skills.sh API (base `https://skills.sh/api/v1/`): `GET /skills` leaderboard (`view=all-time|trending|hot`, `page`, `per_page` ≤500, `source=owner/repo`; unfiltered all-time window 100,000 rows; paginate while `pagination.hasMore`), `GET /skills/search?q=&owner=&limit≤200`, `GET /skills/curated`, `GET /skills/{source}/{skill}` (fields `installs`, `hash` = SHA-256 of file contents, `files[{path,contents}]`), `GET /skills/audit/{source}/{skill}` (partners Gen Agent Trust Hub, Socket, Snyk, Runlayer, ZeroLeaks; `status` pass/warn/fail, `riskLevel`, `auditedAt`). Skill objects: `id`, `slug`, `name`, `source`, `installs`, `sourceType` (github / well-known), `installUrl`, `isDuplicate` (detected forks/copies). Auth via a Vercel OIDC token (free Vercel project), 600 req/min per (team, project), 429 + `Retry-After`. — [skills.sh API docs](https://skills.sh/docs/api)
- Install counts are anonymous, deduplicated, opt-in telemetry from the `skills` CLI (`npx skills add <owner>/<skill>`); each skill page shows source repo, install count, origin org, agents most used on, security audits. — [skills.sh docs](https://www.skills.sh/docs); [skills.sh about](https://skills.sh/about)
- SkillsMP indexes millions of SKILL.md files from public GitHub; 50 anonymous searches/day, 500/day with a free key; results carry `repoStars` (repo-level, not skill-level). — third-party connector description surfaced by search ([anythingmcp agent-skills](https://anythingmcp.com/marketplace/agent-skills)); not verified against SkillsMP's own docs
- Claude Code plugin marketplace = repo with `.claude-plugin/marketplace.json` requiring `name`, `owner`, `plugins[]`; each entry has `name`, `source` (relative path, `{source:"github", repo}`, `{source:"git-subdir", url, path}`, `url`, `archive`, `npm`, `command`), `description`; sources can pin `ref`/`sha`; names like `claude-plugins-official` are reserved for official Anthropic marketplaces. — [Claude Code docs: Create a marketplace](https://code.claude.com/docs/en/plugin-marketplaces)
- Third-party aggregators re-host skills under their own paths (e.g. skillsmp.com `creators/.../skillshub/skills-gadievron-raptor-github-archive`, skill-gallery.jp, mcp.directory, smithery.ai, tessl.io all re-list the same `gadievron/raptor` skill). — search results above ([skillsmp](https://skillsmp.com/creators/comeonoliver/skillshub/skills-gadievron-raptor-github-archive), [tessl](https://tessl.io/registry/skills/github/gadievron/raptor/github-archive), [smithery](https://smithery.ai/skills/gadievron/github-archive))
- A Skillselion "agent skill author census 2026-08" page exists (not reviewed). — [skillselion](https://www.skillselion.com/research/agent-skill-author-census-2026-08)

### Inferences
- **skills.sh plan:** page the all-time leaderboard (100k rows / 500 per page = 200 calls) + `hot`/`trending`; then detail calls for skills whose `source` maps to a GitSkills repo. Join key: `source` (owner/repo) + slug -> GitSkills `repo_full_name` + folder name; secondary join: SHA-256 of the GitSkills `content` vs `hash`. New fields: installs (a real-usage/popularity outcome far better than repo stars), `isDuplicate` (an independent copy/origin label to validate our `copy_share` heuristic), audit risk. At 600/min the full crawl takes < 1 hour. Effort: 1–2 person-days. Check the skills.sh Terms page before redistributing install counts; publish only aggregates.
- **Marketplaces:** code-search `filename:marketplace.json path:.claude-plugin` (small result set, fits 10 req/min) then fetch raw JSON; plugin entries with `github`/`git-subdir` sources give **declared upstream repo** for vendored skills, i.e. explicit copy->origin edges. Also `anthropics/claude-plugins-official` (reserved official marketplace name; repo name per prior knowledge, verify) and `openai/plugins` (already candidate in registry_sources.yaml). Effort: 1 day.
- **awesome-claude-skills lists, cursor.directory, agents.md**: curated lists give a human-curated "notable origin" label (repo URL + often author attribution) cheaply by parsing README links (1 day). cursor.directory and agents.md are about Cursor rules / AGENTS.md, a different artifact; useful only if the study expands beyond SKILL.md. I did not verify their APIs this session.
- SkillsMP (50–500 searches/day) and HTML-only directories are not worth scraping in one sprint; their ToS on scraping were not checked.

### Gaps
- skills.sh Terms of Service text on data reuse not read.
- No verified API/metadata for cursor.directory, agents.md, awesome lists, OpenAI Codex skills directory, or skillselion.
- Whether skills.sh `hash` uses the same normalisation as raw file bytes (to match GitSkills content) is unverified.

---

## Q4. Ground-truth / labelling data (known-human, known-LLM, explicit agent trailers)

### Takeaway
Three cheap ground-truth sources: (1) explicit agent signals from full commit histories and PR metadata (trailers, bot committers, agent PR authors) mined via Q1 — far larger than GitSkills' first/last-commit-only signal; (2) labelled agent commits/PRs from AIDev and AgentPack; (3) synthetic LLM-generated SKILL.md files as positives and pre-Nov-2022 markdown docs as known-human negatives.

### Cited Findings
- AgentPack: 1.3M commits explicitly co-authored by Claude Code, Codex, Cursor Agent (Apr–Aug 2025). — [HF nuprl/AgentPack](https://huggingface.co/datasets/nuprl/AgentPack)
- AIDev: 932,791 agent-authored PRs with agent identity known per PR. — [AIDev paper](https://arxiv.org/html/2602.09185v1)
- GitSkills keeps AI names in `Co-authored-by` trailers but only for first/last commit messages of representatives. — local `DATA_DICTIONARY.md`
- ChatGPT's public launch was 30 Nov 2022 (prior knowledge, the conventional cutoff for "pre-LLM" text).

### Inferences
- **Known-human corpus:** markdown docs (README.md, CONTRIBUTING.md, docs/*.md, and especially *instructional/how-to* markdown) with git commit date before 2022-11-30 from (a) BigQuery `github_repos.contents` (old snapshot, pre-LLM by construction), or (b) `git log --before=2022-11-30` on cloned repos. Caveat: genre mismatch — SKILL.md is a new genre; detectors trained on READMEs may learn genre not authorship. Better: human-written *prompt/instruction* text from pre-2023 (e.g. GitHub issue templates, `.github/` docs, Ansible/Docker how-tos).
- **Known-LLM corpus:** generate synthetic SKILL.md with several models (Claude, GPT, Gemini, open models) from the `name` + `description` of real skills, plus the official `skill-creator` style prompts; ~2k-5k samples. Cost estimate: ~1.5k tokens out per skill x 5k ≈ 7.5M output tokens; with a small/cheap model this is on the order of tens of US dollars (check current pricing; not verified this session). Effort: 1–2 days.
- **Explicit trailers at scale:** from the full per-file history (Q1) extract all trailer lines and tool footers (`Generated with Claude Code`, `Co-Authored-By: Claude <noreply@anthropic.com>`, Copilot coding-agent bot commits, `cursoragent`, Codex). This turns the current 4.3% `agent_assisted` label into a richer per-commit timeline (agent drafted then human edited, etc.).
- Treat trailer-positive skills as *agent-assisted*, not *agent-written*; trailer-negative is not human (many tools allow disabling trailers), as the project's DATA_DICTIONARY already notes.

### Gaps
- No public, verified dataset of human-written vs LLM-written *SKILL.md* exists that I found.
- Current LLM API prices not verified this session.

---

## Q5. Legal / ethical (GitHub ToS, PII, anonymisation)

### Takeaway
GitHub's Acceptable Use Policies allow research use of public, **non-personal** information (API collection is not "scraping") on condition that resulting publications are open access; personal data (emails, names) must be handled per GitHub's Privacy Statement, secured, and honoured on removal requests — so hash or drop identities at ingestion, mirroring GitSkills' anonymisation.

### Cited Findings
- AUP §7: researchers may use public, non-personal information for research only if resulting publications are open access; scraping = automated extraction (bots/crawlers); API collection is not scraping; info may not be used for spam or sold to recruiters; use must comply with the GitHub Privacy Statement. §8: misuse of personal information prohibited; collectors must use personal info only for the purpose authorised, secure it, and respond promptly to complaints, removal and do-not-contact requests. — [GitHub Acceptable Use Policies](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies)
- Commit API responses include author/committer emails. — [GitHub Commits REST API](https://docs.github.com/en/rest/commits/commits)
- GitSkills replaces accounts with keyed one-way codes (key not distributed), keeps bot logins, redacts emails and personal names in messages, keeps AI names in `Co-authored-by`. — local `src/data/gitskills_data/README.md`
- GitSkills content keeps the license of the origin repo. — local `DATA_DICTIONARY.md`

### Inferences
- Recommended handling: at ingestion compute `HMAC-SHA256(team_secret, login_or_email)` and store only the code; keep `[bot]` logins and AI trailer names in clear; regex-redact emails (`\S+@\S+`) and `Co-authored-by: <Human Name>` names; never commit raw API dumps to the repo (keep under git-ignored `data/raw/` with the secret in `.env`); publish only aggregates and anonymised codes. Our codes will not match GitSkills' codes (their key is private), so cross-dataset author identity linkage is impossible — join on repo+path+commit SHA instead.
- University of Michigan IRB: mining public metadata with anonymisation typically qualifies as not human-subjects research or exempt, but the team should confirm with the course instructor (not verified).
- Use a token tied to a real account, respect `Retry-After`, set a descriptive `User-Agent` with a contact.
- Do not redistribute fetched SKILL.md text from repos without licenses; share SHAs + URLs instead.

### Gaps
- GitHub ToS Section H (API terms) not read in this session.
- skills.sh, SkillsMP, SWH, WoC terms of use not read.

---

## Summary acquisition plan (priority order for one sprint, ~2 weeks, 4 students)

| # | Source | New fields vs GitSkills | Access | Limits / cost | Effort |
|---|---|---|---|---|---|
| 1 | GitHub REST/GraphQL on a stratified sample (~20–50k skills) | full per-file commit list, all trailers, author vs committer, PR linkage + PR author type/body, topics, fork parent, contributor count, owner account age | PAT (`GITHUB_TOKEN` already in .env.example); `/commits?path=`, `/commits/{sha}/pulls`, GraphQL `history(path:)` | 5,000 req/h per token; 900 REST pts/min; GraphQL 5,000 pts/h | 3–5 pd |
| 2 | Blobless clones + `git log --follow` (subset of repos) | complete history incl. renames, every historical SKILL.md version (human edits of agent drafts) | `git clone --filter=blob:none` | no API quota; disk/time | 2 pd |
| 3 | skills.sh API | installs (all-time/trending/hot), isDuplicate, content hash, security audits | `https://skills.sh/api/v1/skills…`, Vercel OIDC token | 600 req/min | 1–2 pd |
| 4 | GH Archive on BigQuery | bot PR events, push commit messages, star timing, repo creation for GitSkills repos | `githubarchive.month.2025*/2026*` | 1 TiB/month free, $6.25/TiB after (verify) | 1–2 pd |
| 5 | Claude plugin marketplaces + awesome lists | declared upstream repo/owner for vendored skills (copy->origin edges), curated "notable" label | code search `path:.claude-plugin filename:marketplace.json`, raw JSON | 10 code-search req/min | 1 pd |
| 6 | AIDev, AgentPack (HF) | labelled agent PR/commit text and bot logins (pre-skill era) | `datasets.load_dataset("hao-li/AIDev")`, gated `nuprl/AgentPack` | free | 1 pd |
| 7 | Synthetic LLM skills + pre-2022 human markdown | known-positive / known-negative text for detector validation | LLM API; BigQuery `github_repos` or clones | API cost (small); BigQuery scans | 2 pd |
| 8 (stretch) | Software Heritage | first archival visit time, cross-forge existence of the same blob, provenance (`whereis`, needs special permission) | archive.softwareheritage.org API, OIDC token | ~120/h anon, ~1,200/h token (unverified) | 2 pd + access wait |
| 9 (stretch) | World of Code | blob -> first commit/author/project across all open source | account on WoC servers | access request | 3+ pd + access wait |

Not recommended: GHTorrent (dead), re-running GitHub code search for SKILL.md at scale (10 req/min), SkillsMP (50–500 searches/day), BigQuery `github_repos` for skills (stale snapshot).
