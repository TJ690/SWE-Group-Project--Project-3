# Skill Provenance App: Architecture and Tech-Stack Design (Sprint 2, Python 3.13)

Scope: a small app that takes a GitHub repo URL or a URL to one SKILL.md / agent-instruction file and returns a provenance report: (a) likely human-authored / agent-assisted / templated-or-copied / insufficient evidence, (b) where else copies exist and who published first, (c) the evidence behind each verdict. It reuses the team's GitSkills research assets (DuckDB SQL in `db/`, parquet in `src/data/`, label rules from `EDA_authorship.ipynb`, the planned `skillsig` package in `docs/CODING_PLAN.md`).

Local project facts used below (from the repo, not the web): GitSkills has 3,797,117 SKILL.md rows, 1,877,981 distinct `file_sha`, 282,200 repos; commit history exists only for representatives (`history_fetched = 1` for 458,548, 24.4%); `first_commit_message` keeps AI names in `Co-authored-by` trailers; Sprint 1 found 30.3% of distinct skills with history have an AI co-author line in their first commit (95.2% of those name Claude) and that 38.7% of commit-based agent candidates were dropped as bulk installs (batch >= 6). `config/default.yaml` already fixes MinHash params (`num_perm: 128`, `shingle_words: 5`, `lsh_threshold: 0.80`, `lsh_threshold_low: 0.60`) and thresholds (`bulk_batch_min: 6`, `templated_copies_min: 10`). Sources: `DATA_DICTIONARY.md`, `report/sprint1_report.md`, `config/default.yaml`, `docs/CODING_PLAN.md`.

## Q1. Existing tools / prior art: what each provides and whether to reuse it

### Takeaway
No existing tool answers "who wrote this markdown file and was an LLM involved". The useful reusable pieces are (1) GitHub's own APIs (GraphQL `Commit.authors` already parses `Co-authored-by` trailers; REST/GraphQL file history), (2) git-mining libraries (PyDriller/GitPython) for clone-based history, (3) datasketch for near-duplicate lookup against the GitSkills corpus, and (4) Software Heritage for an independent "earliest-seen" check by git blob hash. Supply-chain tools (Scorecard, attestations, Sigstore/SLSA, deps.dev) verify builds and repo hygiene, not text authorship, so they are at most optional context signals.

### Cited Findings
**GitHub GraphQL (primary data source for per-file history)**
- `Commit.authors` is "based on the git author and the Co-authored-by message trailer", git author first, paginated, so no custom trailer parser is strictly needed for GitHub-hosted repos — [GitHub GraphQL Commit reference](https://docs.github.com/en/graphql/reference/commits)
- `Commit.history` returns linear history in `git log` order, filterable by `path`, `since`, `until`, `author`; `Commit` also exposes `authoredDate`, `committedDate`, `signature` (signing info if present), `associatedPullRequests`, and `file(path)` — [GitHub GraphQL Commit reference](https://docs.github.com/en/graphql/reference/commits)
- GraphQL primary limit: 5,000 points/hour per user (PAT); `GITHUB_TOKEN` in Actions gets 1,000 points/hour per repo — [GitHub GraphQL rate limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api)
- Point cost = requests needed per connection assuming every `first`/`last` is filled, divided by 100, minimum 1; actual cost readable from `rateLimit { cost }`. Every connection needs `first`/`last` in 1..100; max 500,000 nodes per call; requests over 10 s are killed (502/504) and cost extra points — [GitHub GraphQL rate limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api)
- Secondary limits: max 100 concurrent requests (shared REST+GraphQL); 2,000 points/minute for GraphQL, 900/minute for REST endpoints — [GitHub GraphQL rate limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api)

**GitHub REST**
- `GET /repos/{owner}/{repo}/commits` supports `path`, `sha`, `since`, `until`, `author`, `per_page` (max 100); response has `commit.author`/`commit.committer` (name, email, date) and `commit.verification` (`verified`, `reason`, ...). The docs do not state that `path` follows renames — [GitHub REST commits](https://docs.github.com/en/rest/commits/commits)

**PyDriller / GitPython**
- PyDriller `Repository` accepts a remote URL and "will automatically create a temporary folder, clone the repository"; filters include `filepath` ("only commits that modified this file will be analyzed"), `only_modifications_with_file_types`, `since`, `to`, and `clone_repo_to` — [PyDriller docs](https://pydriller.readthedocs.io/en/latest/repository.html)
- The team's CODING_PLAN already uses treeless clones (`git clone --filter=blob:none --no-checkout`) plus `git log --no-renames --raw --no-abbrev` for 31 registry repos, about 1 GB total — local `docs/CODING_PLAN.md` (D2)

**datasketch (near-duplicate index)**
- `MinHashLSH(threshold, num_perm)` returns approximate candidates (false positives and negatives possible); threshold and `num_perm` are fixed at construction; candidates are unranked, so verify with `MinHash.jaccard`; no top-k (use LSH Forest for that); `insertion_session()` for bulk load; `merge()` combines indexes — [datasketch MinHashLSH docs](https://ekzhu.com/datasketch/lsh.html)
- The LSH object is picklable and the docs call pickling the recommended way to save/move it; Redis or Cassandra storage via `storage_config`, with a fixed `basename` needed to reconnect to the same data — [datasketch MinHashLSH docs](https://ekzhu.com/datasketch/lsh.html); a GitHub issue thread confirms random basenames break Redis reuse across runs — [lightrun summary of datasketch issue](https://lightrun.com/answers/ekzhu-datasketch-redis-storage-question)

**Software Heritage (independent first-seen / where-else evidence)**
- Content lookup: `GET https://archive.softwareheritage.org/api/1/content/sha1_git:<40-hex>/`; hash type may be `sha1`, `sha1_git`, `sha256`, `blake2s256`; 404 if not archived; a batch "known" route also exists — [swh-web API URLs](https://docs.softwareheritage.org/devel/swh-web/uri-scheme-api); [SwhFS tutorial](https://docs.softwareheritage.org/devel/swh-fuse/tutorial.html)
- A content SWHID `swh:1:cnt:<X>` uses the git blob `sha1_git` as `<X>`, so a GitHub blob SHA maps directly — [SWHID spec](https://docs.softwareheritage.org/devel/swh-model/persistent-identifiers)
- The API docs page returned an Anubis bot-challenge "Access Denied" to an automated fetch during this research, so scripted access may hit bot protection — observed fetch of [swh-web API URLs](https://docs.softwareheritage.org/devel/swh-web/uri-scheme-api.html)

**Supply-chain verification tools (low relevance)**
- `gh attestation verify` checks attestation signatures and that the attestation refers to the provided artifact, requires `--repo` or `--owner`, defaults to SLSA provenance predicates, supports `--bundle` offline and `--format=json` — [gh attestation verify manual](https://cli.github.com/manual/gh_attestation_verify)
- Artifact attestations sign with a temporary keypair tied to GitHub account identity; verification compares owner/repo to the signing certificate — [GitHub blog launch post](https://github.blog/news-insights/product-news/introducing-artifact-attestations-now-in-public-beta); [Ian Lewis analysis](https://www.ianlewis.org/en/understanding-github-artifact-attestations)
- OpenSSF Scorecard scores repos 0-10 across independent checks (branch protection, pinned deps, token permissions, SAST...); pre-computed results via `/projects/github.com/<org>/<repo>`; only GitHub projects; hostnames `api.scorecard.dev` and `api.securityscorecards.dev` both appear in docs — [ossf/scorecard](https://github.com/ossf/scorecard); [Scorecard swagger](https://api.securityscorecards.dev/swagger.json); [DefectDojo Scorecard parser docs](https://docs.defectdojo.com/supported_tools/parsers/file/ossf_scorecard/)
- deps.dev: no primary doc found in this session for the v3 project endpoint; a GUAC issue discusses v3 vs v3alpha batch APIs — [guac issue #1947](https://github.com/guacsec/guac/issues/1947)

**AI-text detectors**
- University of Chicago/NBER study (1,992 human passages): Pangram had lowest error rates and was the only tool keeping high recall at a 0.5% FPR cap; GPTZero and Originality.ai were a second tier that struggled on very short text; open-source RoBERTa detector mislabeled 30-69% of human text as AI — [NBER w34223](https://www.nber.org/papers/w34223); [the-decoder summary](https://the-decoder.com/pangram-achieves-near-perfect-results-in-ai-text-detection-tests-study-reveals/)
- Epoch AI (2026): Pangram and GPTZero flagged no human passages; Originality.ai flagged 19/495 (3.8%); ~13% of style-imitating AI passages and ~26% of scientific writing went undetected — [Epoch AI data insight](https://epoch.ai/data-insights/ai-detectors-false-negatives)
- GPTZero's own benchmark claims 99.6% accuracy / 0.13% FPR vs Pangram 97.5% / 0.20% (vendor-published) — [GPTZero vs Pangram](https://gptzero.me/news/gptzero-vs-pangram/)

**Claude Code attribution trailers (key "agent-assisted" signal and its weakness)**
- Claude Code's commit `Co-Authored-By` line and PR footer are controlled by an `attribution` setting (`attribution.commit`, `attribution.pr`), replacing deprecated `includeCoAuthoredBy`; setting them to "" is reported to remove them — [deku.posstree guide](https://deku.posstree.com/en/generative_ai/claude-code-attribution/); [TIL magmalabs](https://til.magmalabs.io/posts/9d0f1a16a1-you-can-disable-ai-attribution-in-claude-code-commits.md)
- User-reported issues: a `Claude-Session` trailer still appended with attribution disabled, and earlier bugs where `includeCoAuthoredBy: false` was ignored — [claude-code #14241](https://github.com/anthropics/claude-code/issues/14241); [claudeissues #53259](https://claudeissues.com/issue/53259-co-authored-by-system-prompt-overrides-claude-md-settings-json-and-skill-instruc)

### Inferences
- Reuse map: GitHub GraphQL (history, co-authors, PR link, signature) = core; PyDriller/GitPython = fallback for non-GitHub or large repos and for offline golden tests; datasketch = corpus matching; DuckDB = exact-hash and metadata lookups against GitSkills parquet; Software Heritage = optional "earliest archived" corroboration; Scorecard/attestations/gitsign/SLSA = out of scope for authorship (show at most "commit signed: yes/no" from `Commit.signature`). PyGithub/ghapi are unnecessary if the team writes ~3 GraphQL queries with `httpx`; PyGithub is fine for REST convenience if someone already knows it.
- Absence of a trailer is weak evidence: trailers can be disabled, and non-Claude tools often add none. Presence is strong evidence of tool involvement in that commit but, per the Sprint 1 bulk-install finding, not of authorship of the file content.
- Commercial AI-text detectors are not recommended as a core signal: short SKILL.md bodies, heavy templating, markdown/code structure, paid APIs, and the policy cost of sending third-party text to a vendor. If included, make it an opt-in, clearly labeled "external detector score" with low weight.

### Gaps
- Whether GitSkills `file_sha` is the git blob SHA-1 (`sha1("blob <len>\0" + bytes)`) could not be verified (DuckDB not installed in the shell used). This determines whether exact matching works with blob SHAs straight from the GitHub tree API. Verification snippet: `hashlib.sha1(b"blob %d\x00" % len(b) + b).hexdigest() == file_sha` on rows with `content_sha_ok = 1`. Encoding/line-ending normalization by the collector could also break equality.
- Software Heritage anonymous/authenticated rate limits and an endpoint for "origins containing this content" were not retrieved (docs page blocked); treat SWH as optional.
- deps.dev v3 project schema not confirmed; GitHub official limitations doc for artifact attestations not retrieved.
- No found tool specifically detects LLM-written SKILL.md / agent-instruction files.

## Q2. Pipeline design: URL parsing, discovery, history fetch, features, corpus matching, scoring, output

### Takeaway
A linear, cacheable pipeline: `parse_url -> resolve (repo, ref, paths) -> discover skill files via one tree call -> fetch per-file history via GraphQL (clone fallback) -> compute features -> match against a prebuilt GitSkills index (exact blob hash in DuckDB + MinHash LSH pickle) -> rules engine producing signals with weights and evidence -> Report (pydantic) -> JSON / Markdown / Rich / web`. Build the corpus index offline once from the existing parquet; at query time only the small index files are loaded.

### Cited Findings
- GraphQL `history(path:)` gives per-file commits with `authors` (incl. co-authors), dates, `signature`, `associatedPullRequests` in one query; 100 nodes per page — [GitHub GraphQL Commit reference](https://docs.github.com/en/graphql/reference/commits); [GraphQL rate limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api)
- REST commits `path` filter is an alternative but rename-following is not documented — [GitHub REST commits](https://docs.github.com/en/rest/commits/commits)
- PyDriller `filepath` filter + remote URL cloning gives an offline-capable fallback — [PyDriller docs](https://pydriller.readthedocs.io/en/latest/repository.html)
- MinHash LSH returns unranked approximate candidates, so re-score with exact/estimated Jaccard before reporting — [datasketch MinHashLSH docs](https://ekzhu.com/datasketch/lsh.html)
- Software Heritage content lookup by `sha1_git` can confirm whether the blob was archived elsewhere — [swh-web API URLs](https://docs.softwareheritage.org/devel/swh-web/uri-scheme-api)

### Inferences
**Stage 1: URL parsing (`urls.py`, pure function, heavily unit-tested)**
Accept and normalize to `Target(owner, repo, ref|None, path|None, kind)`:
- `https://github.com/o/r` and `.git` suffix, trailing slash -> kind=repo
- `https://github.com/o/r/tree/<ref>/<dir>` -> kind=tree (scan under dir)
- `https://github.com/o/r/blob/<ref>/<path>/SKILL.md` -> kind=blob
- `https://raw.githubusercontent.com/o/r/<ref>/<path>` -> kind=blob
- Ambiguity: refs may contain `/` (e.g. `feature/x`). Resolve by trying the longest prefix that is a valid ref (GraphQL `repository.ref(qualifiedName:)` or `object(expression:"<ref>:<path>")`). Pin the resolved commit SHA immediately so the whole report refers to one snapshot.
- Reject non-GitHub hosts with a clear error in v1 (optionally allow any git URL via the clone path).

**Stage 2: Discovery (`discover.py`)**
- One call: `GET /repos/{o}/{r}/git/trees/{sha}?recursive=1` (or GraphQL tree) yields every path and blob SHA without downloading content. Match globs: `**/SKILL.md`, `.claude/skills/**/SKILL.md`, `.agents/**`, `AGENTS.md`, `CLAUDE.md`, `.cursor/rules/**/*.mdc`, `.github/copilot-instructions.md`, `GEMINI.md`. Reuse the agent-folder mapping from `db/agent_ecosystem.sql` to tag the tool family.
- Cap at e.g. 200 files per run (configurable) and warn; huge registries are themselves a signal (aggregator/mirror), mirroring `repo_profile.copy_share`.

**Stage 3: Content + history (`github_client.py`, `history.py`)**
- Content: fetch raw bytes per file (raw URL or GraphQL `Blob.text`); compute blob SHA locally and check it equals the tree SHA.
- History: GraphQL query per file (batch several files with aliases in one query to save points):
  `repository{ object(oid:$sha){ ... on Commit { history(first:100, path:$p){ totalCount nodes{ oid authoredDate committedDate messageHeadline message authors(first:5){nodes{name email user{login}}} author{user{login}} signature{isValid state} associatedPullRequests(first:1){nodes{number}} } } } } }`
  plus, for bulk-install detection, the file count changed in the first commit (REST `GET /commits/{sha}` gives `files[]`; or count how many discovered skill paths share that first-commit oid, which needs no extra calls).
- Fallback (`--clone`): `git clone --filter=blob:none` + PyDriller `Repository(path, filepath=p)` when the token is missing, the repo is huge, or GraphQL errors. Also used for offline golden tests.

**Stage 4: Features (`features/`)**: reuse code, do not rewrite
- `structure.py`: port the SQL in `db/content_and_structure.sql` (headings, h2, bullets, numbered items, code blocks, table rows, links, examples/when-to-use sections, front-matter keys, non_ascii_ratio) to a Python function over one string; assert parity on 50 corpus rows.
- `commit.py`: AI trailer regex set from `EDA_authorship.ipynb` (Claude/Anthropic, Cursor, Copilot, Codex/OpenAI, Gemini) + "Generated with Claude Code" footer + bot-author patterns (coding-agent bots vs release/sync bots); first-commit batch size; commit count; days between first and last commit; number of distinct human authors; edit count after creation.
- `wording.py` (optional, low weight): stylometric markers (em-dash rate, "comprehensive/robust/leverage"-type lexicon rate, sentence length variance). Treat as exploratory only.

**Stage 5: Corpus matching (`corpus/`)**
- Exact: DuckDB query over a slim `skill_index.parquet` (`file_sha, repo_full_name, path, first_commit_at, discovered_at, stars, location_class`) built once from `artifacts` + `repos`; returns all repos with the identical blob and the earliest `first_commit_at` (only representatives have history, so "earliest known" is best-effort; fall back to `repos.created_at` / `discovered_at` and flag it).
- Near-dup: build MinHash (128 perm, 5-word shingles on body up to 5,000 chars, per config) for the 1.84M readable distinct skills offline, insert into `MinHashLSH(threshold=0.6)`, pickle to `data/index/lsh.pkl` + `minhashes.npy` keyed by `file_sha`. Query, then re-rank by estimated Jaccard and report >= 0.8 as "near copy", 0.6-0.8 as "similar/template family".
- Size management: if the full LSH pickle is too large for the host (check: 1.84M x 128 x 8 bytes ~ 1.9 GB of hash values alone, an estimate), ship a reduced index: only skill families with >= 2 copies or bodies from known registries (the 31 repos in `config/registry_sources.yaml`), or use `num_perm=64`. Keep full index for local CLI use.
- "Who published first": order matches by earliest evidence date with provenance of each date (git first commit in GitSkills / live GitHub history / repo created_at / SWH archive); state when dates are not comparable (e.g. history missing, rebased/squashed imports, backdated commits since `authoredDate` is author-controlled).

**Stage 6: Scoring (`scoring/rules.py`)**
- Signals as data: `Signal(id, label_target, weight, fired: bool, value, evidence: list[EvidenceItem], caveat)`. Rules mirror Sprint 1 v1 labels so results are consistent with the research: templated if exact copies >= 10 (config `templated_copies_min`) or near-match to a registry version >= 0.8; agent-assisted if first-commit AI trailer/coding-agent bot AND batch < 6; human-likely only when history exists and no AI signal and not templated; otherwise insufficient evidence. Precedence: templated > agent-assisted > human-likely > insufficient (as in Sprint 1).
- Confidence from evidence strength, not a probability: e.g. High (direct trailer on a single-file commit; exact copy of an older registry file), Medium, Low (only stylistic/wording signals). Optional log-odds sum with hand-set weights for ordering.
- Optional ML classifier (stretch): logistic regression (scikit-learn already in requirements) on structure+commit features trained on the Sprint 1 heuristic labels; only report it as "model agrees/disagrees with rules", since it is trained on heuristic labels, not ground truth.

**Stage 7: Output (`report/`)**
- `Report` pydantic model -> `model_dump_json()`; Jinja2 Markdown template; Rich tables for CLI; same object rendered by the web UI. Include `schema_version`, `tool_version`, `resolved_commit`, `generated_at`, `index_version` (GitSkills snapshot date July 2026) for reproducibility.

### Gaps
- The exact size of a 1.84M-entry LSH pickle and its load time were not measured; must be benchmarked in week 1.
- Whether GraphQL `history(path:)` follows renames was not confirmed (git log without `--follow` does not, so assume no).

## Q3. UI options, hosting, token handling, rate limits, caching

### Takeaway
Build the core as a library + Typer/Rich CLI first (needed for tests and batch runs), then a thin Streamlit app over the same `analyze()` function. Host the Streamlit app on Streamlit Community Cloud (free) or run locally; Hugging Face Spaces Gradio/Docker now needs a paid plan to create. FastAPI is only worth it if another client needs a REST API. Cache all GitHub calls with requests-cache (SQLite) or an equivalent, with conditional revalidation, and keep the token in `.env` / host secrets.

### Cited Findings
- Hugging Face Spaces: "Static Spaces are free for everyone. Gradio and Docker Spaces run on compute and require a paid plan to create: PRO for personal accounts..." with an exception for up to 2 ZeroGPU Gradio Spaces for free personal accounts; SDK choices listed are Gradio, Docker, static HTML (Streamlit is not listed as its own SDK) — [HF Spaces overview](https://huggingface.co/docs/hub/en/spaces-overview)
- HF CPU Basic: 2 vCPU, 16 GB RAM, 50 GB non-persistent disk, no hourly cost; free hardware sleeps when unused; secrets are exposed as environment variables and not copied on duplication — [HF Spaces overview](https://huggingface.co/docs/hub/en/spaces-overview)
- Streamlit Community Cloud limits (stated as of Feb 2024): memory min 690 MB, max 2.7 GB; apps without traffic for 12 hours sleep; over-limit apps may throttle or fail — [Streamlit manage your app](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app); older sources cite ~1 GB — [Streamlit blog](https://blog.streamlit.io/common-app-problems-resource-limits)
- requests-cache: expiration precedence is Cache-Control response headers > request headers > per-request `expire_after` > `urls_expire_after` > session `expire_after`; `always_revalidate=True` sends a conditional request when a validator exists; `refresh=True` revalidates; `stale_if_error` serves expired data on failures — [requests-cache expiration guide](https://requests-cache.readthedocs.io/en/stable/user_guide/expiration.html)
- GitHub secondary limits (100 concurrent, 2,000 GraphQL points/min) and 10 s timeouts — [GitHub GraphQL rate limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api)
- The team's plan already specifies: read `X-RateLimit-*`, sleep when remaining < 50, retry 5xx with backoff, ETag conditional requests — local `docs/CODING_PLAN.md` section 2

### Inferences
- Option comparison:
  - CLI (Typer + Rich): cheapest, testable with `typer.testing.CliRunner`, fits `python -m skillsig provenance <url> --json out.json`. Must-have.
  - Streamlit: one-file UI (`app/streamlit_app.py`), `st.cache_data` around `analyze(url)`, `st.secrets`/env for token, good tables/expanders for evidence. Recommended web UI. Index size must fit 2.7 GB on Community Cloud, so ship the reduced LSH index there.
  - Gradio: similar effort; but HF hosting for Gradio now requires a paid plan (except ZeroGPU), removing its main advantage.
  - FastAPI + minimal HTML: most flexible, more code (routing, templates, async, CORS, deploy). Only if time remains; could reuse the same `Report` model as the response schema.
- Token handling: `GITHUB_TOKEN` from `.env` via `python-dotenv` or `pydantic-settings`; `.env` in `.gitignore`, `.env.example` committed (already untracked in repo); a fine-grained PAT with public-repo read-only access is enough. In the hosted app use host secrets and do not let users paste tokens (or if allowed, never cache/log them). Without a token, fall back to clone mode and corpus-only results.
- Caching layers: (1) HTTP cache: requests-cache `CachedSession('cache/http.sqlite')` for REST; GraphQL is POST, so cache by hashing (query, variables) in a small SQLite table or use `allowable_methods=('GET','POST')` with `match_headers` off and a short expiry; (2) result cache keyed by `(owner, repo, resolved_commit_sha, tool_version, index_version)` since a commit SHA is immutable. If the team uses `httpx` instead of `requests`, `hishel` is the equivalent cache (not researched here).
- Rate-limit budget per analysis: 1 tree call + ~1 GraphQL point per 1-5 files (aliases) + per-commit file counts only when needed: a typical repo with < 20 skills costs well under 50 points, so a 5,000/hour PAT supports ~100+ analyses/hour (estimate).

### Gaps
- Current (2026) Streamlit Community Cloud limits not confirmed beyond the Feb-2024 figures.
- Whether HF Spaces Docker running Streamlit is free for an existing (already-created) Space was not checked; the docs say creation needs a paid plan.
- hishel/httpx caching was not researched.

## Q4. Explainability: presenting confidence and evidence without over-claiming

### Takeaway
Report a categorical verdict plus a coarse confidence level, a signal table showing every rule that fired or was checked, the concrete evidence (commit oid + trailer text, matching repos + dates, Jaccard score), and fixed caveats. Language must say "signals consistent with" rather than "written by".

### Cited Findings
- Even the best AI-text detectors miss ~13% of style-imitating text and ~26% of scientific writing, and weaker open-source detectors flag 30-69% of human text, so text-only verdicts must be hedged — [Epoch AI](https://epoch.ai/data-insights/ai-detectors-false-negatives); [NBER w34223](https://www.nber.org/papers/w34223)
- Commit trailers can be disabled or appear inconsistently by configuration — [deku.posstree guide](https://deku.posstree.com/en/generative_ai/claude-code-attribution/); [claude-code #14241](https://github.com/anthropics/claude-code/issues/14241)
- MinHash LSH is approximate (false positives/negatives), so similarity claims need the verified Jaccard value next to them — [datasketch docs](https://ekzhu.com/datasketch/lsh.html)
- Sprint 1 already documents that labels are heuristic, that bulk installs carry AI trailers for files the AI did not write, and that label precision is not yet measured — local `report/sprint1_report.md` sections 4 and 7

### Inferences
- Report layout (same in JSON, Markdown, Streamlit):
  1. Header: file, repo, resolved commit, verdict, confidence (High/Medium/Low/Insufficient), one-sentence reason.
  2. Signal table: `signal | observed value | points toward | weight | fired? | evidence link`. Include non-fired checks ("No AI trailer found in 3 commits") so absence is visible.
  3. Copies: table of exact and near copies (repo, path, stars, earliest date + date source, similarity), sorted by date, with "earliest known publisher" and a caveat when dates come from different sources.
  4. Caveats block (static + conditional): heuristic labels; trailer absence is not proof of human authorship; bulk-commit warning when batch >= 6; corpus snapshot date (July 2026) so newer copies are missing; history only for 24.4% of corpus representatives; author dates are author-controlled.
  5. "How to dispute/verify": link to commits and the matching files.
- Per-signal contribution: if using additive weights, show a horizontal bar per signal (contribution to each label); never show more than one decimal or call it a probability unless calibrated against the planned 240-skill human annotation (CODING_PLAN `make sample` / `make evaluate`); once that exists, show per-rule precision next to each rule ("this rule was correct in X of Y annotated cases").
- Label wording: "Likely templated / copied", "Agent-assisted signals present", "No agent signals found (likely human)", "Insufficient evidence". Avoid "AI-generated".

### Gaps
- No external guidance on provenance-report UX was researched; the layout is a design proposal.

## Q5. Testing and CI

### Takeaway
Three test layers: pure unit tests (URL parser, feature extractors, rules) with no network; recorded-HTTP integration tests via pytest-recording/VCR.py cassettes with token headers filtered; and a small set of golden repos with expected verdicts, run against cassettes in CI and live on a manual/weekly workflow.

### Cited Findings
- pytest-recording is a pytest plugin over VCR.py, provides a `vcr` marker, can block network access, uses VCR record mode `none` by default to prevent unintended requests, and takes a `vcr_config` fixture passed to VCR — [pytest-recording on PyPI](https://pypi.org/project/pytest-recording); [VCR.py docs](https://vcrpy.readthedocs.io/)
- `GITHUB_TOKEN` in Actions has a 1,000 GraphQL points/hour per repo limit, enough for a small live golden run — [GitHub GraphQL rate limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api)
- The team's plan already defines `make smoke` (tiny fixture, no network) and `make test` (data-marked tests excluded) — local `docs/CODING_PLAN.md`

### Inferences
- Tools: pytest, pytest-recording (if `requests`/`httpx` via VCR.py; VCR.py supports httpx in recent versions, verify) or respx for hand-written `httpx` mocks; hypothesis for URL-parser fuzzing (optional); `filter_headers=["authorization"]` in `vcr_config` so tokens never land in cassettes; a pre-commit/CI grep for `ghp_`/`github_pat_` in `tests/cassettes`.
- Golden set (record once, assert verdict + key evidence, not exact scores):
  - `anthropics/skills` (known official origin): expect "templated/original source" with itself as earliest publisher for its skills, and many corpus copies.
  - A repo whose skill commit carries `Co-Authored-By: Claude` / "Generated with Claude Code" on a single-skill commit: expect agent-assisted. Pick from GitSkills rows where `first_commit_message` matches and batch size = 1.
  - A bulk-install repo (same first commit adds >= 6 skills with an AI trailer): expect NOT agent-assisted, bulk-install caveat present.
  - A known mirror/aggregator (high `copy_share`): expect templated with origin elsewhere.
  - A personal single-skill repo with several human edits and no trailers: expect likely human / low confidence.
  - Edge cases: blob URL with slash-containing branch, raw URL, tree URL to subfolder, repo with zero skill files, private/404 repo, rate-limit 403 response (synthetic cassette).
- Corpus-index tests: build a 1,000-row fixture index from parquet (committed small parquet, or generated synthetic texts) so CI never needs the 13 GB dataset; mark full-data tests `@pytest.mark.data`.
- Parity test: Python structure features == SQL features on a fixture sample.
- CI (GitHub Actions): `actions/setup-python` 3.13 (or `astral-sh/setup-uv`), `ruff check`, `ruff format --check`, `pytest -m "not data and not live"`; optional weekly `live` job with `GITHUB_TOKEN` re-recording cassettes.

### Gaps
- VCR.py httpx support version and respx behavior on Python 3.13 not verified in this session.

## Q6. Project structure, pyproject, and task breakdown (one sprint, ~2 weeks)

### Takeaway
Add a `provenance` subpackage inside the planned `src/skillsig/` (reusing `config.py`, `github_client.py`, `s2_features/structure.py`, `s2_features/minhash_cluster.py`, `s3_labeling/rules_v1.py`) rather than a separate repo. Roughly 60-75 person-hours of work, feasible for 3-4 students in two weeks if the corpus index build starts on day 2.

### Cited Findings
- Existing planned layout: `src/skillsig/{cli.py, config.py, s0_data, s1_registry/github_client.py, s2_features/{structure,commit,wording,minhash_cluster,template_sim}.py, s3_labeling/rules_v1.py, ...}`, tests at repo-root `tests/`, stage contract `run(cfg, *, dev=False, force=False) -> StageResult`, config in `config/default.yaml` — local `docs/CODING_PLAN.md`
- Current `requirements.txt` pins duckdb 1.5.5, pandas 3.0.5, numpy 2.5.2, scikit-learn 1.9.0, huggingface_hub 2.0.0 — local `requirements.txt`
- datasketch pickled LSH is the documented persistence path — [datasketch docs](https://ekzhu.com/datasketch/lsh.html)

### Inferences
Proposed layout (additions in **bold** concept):
```
pyproject.toml                 # [project] requires-python=">=3.13"; scripts: skillsig = "skillsig.cli:app"
.env.example                   # GITHUB_TOKEN=
config/default.yaml            # + provenance: {max_files, lsh_index_path, weights, cache_path}
src/skillsig/
  cli.py                       # Typer app; adds `provenance` command
  provenance/
    __init__.py                # analyze(url, *, cfg, offline=False) -> Report
    urls.py                    # parse/normalize GitHub URLs -> Target
    discover.py                # tree listing + glob patterns + agent family tag
    github.py                  # GraphQL/REST client: token, retries, rate-limit sleep, cache
    history.py                 # GraphQL history(path:) + PyDriller fallback
    features.py                # wraps s2_features.structure/commit for one file
    corpus.py                  # DuckDB exact match + LSH near-dup + earliest publisher
    swh.py                     # optional Software Heritage lookup
    rules.py                   # Signal definitions, weights, precedence, confidence
    models.py                  # pydantic: Target, FileEvidence, Signal, Match, Report
    render.py                  # Rich tables, Markdown (Jinja2), JSON
  index/build_index.py         # offline: parquet -> skill_index.parquet + lsh.pkl (+ reduced variant)
app/streamlit_app.py           # thin UI calling provenance.analyze
tests/provenance/{test_urls,test_discover,test_rules,test_corpus,test_cli,test_golden}.py
tests/cassettes/  tests/fixtures/mini_index/
.github/workflows/ci.yml
```
Dependencies: `httpx` (or `requests` + `requests-cache`), `pydantic>=2`, `typer`, `rich`, `jinja2`, `python-dotenv`, `datasketch`, `duckdb`, `pyarrow`, `pydriller` (fallback), `streamlit` (extra `[web]`); dev: `pytest`, `pytest-recording`/`respx`, `ruff`. Use `uv` or pip + `pip install -e .[web,dev]`.

Task breakdown (estimates are person-hours; dependencies in parentheses):
| # | Task | Est. |
|---|---|---|
| P1 | pyproject, package skeleton, config section, CI workflow (ruff + pytest) | 4 |
| P2 | `urls.py` + exhaustive tests (repo/tree/blob/raw, slash refs) | 4 |
| P3 | `github.py` client: token, GraphQL+REST, retries, rate-limit sleep, SQLite cache | 6 |
| P4 | `discover.py` tree listing + patterns + agent tag (P2,P3) | 4 |
| P5 | `history.py` GraphQL history w/ aliases + co-author extraction + batch size; PyDriller fallback (P3) | 8 |
| P6 | Verify `file_sha` == git blob SHA; `build_index.py` exact-match parquet (DuckDB) | 4 |
| P7 | MinHash index build (full + reduced), pickle, load benchmark (P6; start early, long run) | 8 |
| P8 | `features.py` port SQL structure features + parity test; commit features from Sprint 1 regexes | 6 |
| P9 | `corpus.py` exact/near matches, earliest-publisher logic with date provenance (P6,P7) | 6 |
| P10 | `rules.py` signals, precedence, confidence, caveats; unit tests per rule (P8,P9) | 6 |
| P11 | `models.py` + `render.py` JSON/Markdown/Rich + Typer command (P10) | 5 |
| P12 | Golden repos + cassettes + integration tests (P11) | 6 |
| P13 | Streamlit app + deploy to Community Cloud (reduced index) (P11) | 5 |
| P14 | Optional: SWH lookup, LR classifier agreement, FastAPI endpoint | 4-8 |
| | Total core (P1-P13) | ~74 h |

Suggested split for 4 people: A = P1-P4 (client & discovery), B = P5+P8 (history & features), C = P6,P7,P9 (corpus index), D = P10-P13 (rules, output, UI, golden tests); integration checkpoint at end of week 1 with the CLI working on one golden repo end-to-end.

Deployment options summary: (1) local CLI with full index (primary, demo-ready); (2) Streamlit Community Cloud with reduced index and token in secrets (free, sleeps after 12 h idle, 2.7 GB memory cap); (3) HF Spaces only if someone has PRO (Docker/Gradio creation requires paid plan); (4) Docker image + FastAPI on any VM (stretch).

### Gaps
- Effort estimates are judgment-based, not sourced.
- Whether the reduced index preserves enough recall for "who published first" must be measured against the full index.
