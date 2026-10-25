# Finish GitSkills code in one sprint

The team can finish all Sprint 2 code by the **2026-10-28** deadline, but only by doing three things. First, put the hand annotation, not the code, on the critical path. Second, freeze an "RQ1 + provenance core" of about **95 person-hours of code plus about 30 hours of human labelling**. Third, push the RQ2 maintenance models (T26–T28) and the web front end to stretch. As of 10-17 none of the planned `skillsig` package exists: there is no `src/skillsig/`, `tests/`, `pyproject.toml` or `results/`, and the scaffolding is uncommitted. The plan is therefore already **2–3 days behind** its own dates ([project audit](../research_notes/GitSkills%20provenance%20sprint%202%20plan/project_audit.md); [CODING_PLAN](../docs/CODING_PLAN.md)). Most of the remaining work is already specified in the T00–T32 cards. The research changes the plan in six places:
- Port the v1 labels with the notebook's loose `LIKE` trailer test, so the 80,114 agent-assisted anchor holds, and add a strict trailer parser only as a v2 feature.
- Break the T13→T17→T18→T13 lexicon loop with a fixed external word list.
- Add the unused installer-metadata signals (`_meta.json`, `.clawhub/origin.json`, front-matter `source`/`version`) as the top-precision "templated" basis.
- Start the full-population MinHash run on day 2.
- Fix the Python environment before anything else.
- Draw the 240-skill annotation sample by 10-21 so two raters can label it while the registry-lineage and app code is written in parallel.

Provenance itself breaks down into about a dozen signals, ranked by precision: exact blob hash, installer metadata, registry blob lineage, agent trailers and bots, near-duplicate clusters, scaffold fingerprints, then stylometry. Text-only AI detectors are a last resort, because they fail on short, templated markdown. The payoff after provenance is a set of "so what" questions on diffusion, attribution, security drift from origin, and staleness. Together with a small `skillsig provenance <github-url>` CLI that reuses the same modules, these fit the MSR 2027 Mining Challenge's 4-page format (due Dec 23, 2026) ([MSR 2027 Mining Challenge](https://2027.msrconf.org/track/msr-2027-mining-challenge)).

## The repo has a plan, data and EDA, but no package yet

The research question asks whether **likely-human, agent-assisted and highly templated skills** can be told apart from structural, linguistic and commit-metadata patterns, and how reliable that signal is. The unit is one SKILL.md, deduplicated by content hash to **1,877,981 distinct contents**. Each explanation is tested against a simple baseline: agent folder, copy count, trailer with and without bulk exclusion, repo date, and "no separable signal" ([RESEARCH_QUESTION.md](../RESEARCH_QUESTION.md)). Sprint 1 delivered v1 labels as inline DuckDB SQL in `EDA_authorship.ipynb`: **35,768 templated / 80,114 agent-assisted / 62,798 likely-human / 1,699,301 insufficient**. It also found that structure barely separates human from agent. Median words are 589 vs 661, and median headings 10 vs 11. Location explains much of the gap: 43.0% vs 72.0% of skills sit under `.claude/` ([sprint1 report](../report/sprint1_report.md)). That makes "no separable signal" a credible headline. The Sprint 2 code therefore has to produce baselines and an ablation that test it formally, not just more descriptive tables.

`docs/CODING_PLAN.md` and 33 Cursor cards specify a stage-based package. The CLI is `python -m skillsig <stage>`, and every module exposes `run(cfg, *, dev, force) -> StageResult`. The plan also adds an RQ2 on maintenance and update propagation that RESEARCH_QUESTION.md never mentions and that the instructor has not documented approving ([CODING_PLAN](../docs/CODING_PLAN.md); [00-project-context rule](../.cursor/rules/00-project-context.mdc)). Its regression anchors come from a 10-15 "pilot": **6,719 registry blobs matching 216,440 files**, a 22% / 29% lag, and assist-probe AUC 0.52 vs 0.64. No code, data or notebook for that pilot exists in the repo, so treat these anchors as targets to reproduce, not facts. `ARCHITECTURE_PLAN_RQ1.md`, which the plan cites as its design background, is also missing ([project audit](../research_notes/GitSkills%20provenance%20sprint%202%20plan/project_audit.md)).

Four environment and data problems will block the first day unless they are fixed up front:
- **No interpreter has the `duckdb` Python module installed**, and there is no `.venv`. `requirements.txt` lacks pyarrow, pyyaml, scipy, statsmodels, datasketch, httpx and pytest, all of which T00 needs ([requirements.txt](../requirements.txt); [T00 card](../.cursor/tasks/T00-scaffold-the-package-config-cli-and-makefile.md)).
- `download_data.py` writes to `./gitskills_data` relative to the working directory. It must run from `src/data/`, but the plan says to run it from the repo root ([download_data.py](../src/data/download_data.py)).
- **The local data has 458,137 representatives with history, while the paper says 458,548.** `content_sha_ok` takes a value of 2 (26,804 repaired rows) that the data dictionary does not document. Some `first_commit_at` values (up to 2026-07-23) fall after the last discovery date ([project audit](../research_notes/GitSkills%20provenance%20sprint%202%20plan/project_audit.md); [DATA_DICTIONARY.md](../DATA_DICTIONARY.md)). Anchor all regression tests to the local counts and record the difference as a threat.
- No source has yet confirmed that `file_sha` is a git blob SHA-1 (`sha1("blob <len>\0"+bytes)`). The registry match (T10), the Software Heritage join and the app's exact lookup all depend on it. A one-hour check on rows with `content_sha_ok = 1` settles it ([provenance app design notes](../research_notes/GitSkills%20provenance%20sprint%202%20plan/provenance_app_design.md)).

## Twelve provenance signals, ranked by how much to trust them

No single signal decides provenance. The literature on agent attribution is consistent: explicit signatures are **strong positives and weak negatives**. Claude Code adds "Co-Authored-By: Claude" and "Generated with Claude Code" by default, but users can turn both off ([DeployHQ](https://www.deployhq.com/blog/how-to-use-git-with-claude-code-understanding-the-co-authored-by-attribution)). Cursor only began adding its trailer around late January 2026 ([Cursor forum](https://forum.cursor.com/t/co-author-added-without-consent-and-cant-be-turned-off/150096)). Codex adds no authorship attribution at all ([AIDev](https://arxiv.org/html/2507.15003v1)). Commit-level detection found only **58.29% of projects that visibly use agents at the file level** ([Agentic Much?](https://arxiv.org/html/2601.18341v1)). Text detectors are weaker still: TPR at 1% FPR falls to as low as 0% under practical conditions ([arXiv 2412.05139](https://arxiv.org/abs/2412.05139)). Detectors also struggle on code-like, low-entropy text ([arXiv 2310.05103](https://ar5iv.labs.arxiv.org/html/2310.05103)), and SKILL.md is short, templated markdown with code blocks. Neither AIDev nor "Agentic Much?" reports precision or recall for trailer heuristics, so a measured per-rule precision would itself be a contribution ([AIDev](https://arxiv.org/html/2507.15003v1)).

The table orders methods by expected precision. Each row gives the class it supports, how to implement it from the data in hand, and how to validate it.

| # | Signal (class it supports) | Implementation in `skillsig` | Validation |
|---|---|---|---|
| M1 | Exact content hash across 3.8M occurrences (copied) | `file_sha` group counts, owner counts, in-repo vs cross-owner (T04) | Already exact. Report sensitivity of the copy threshold 5–20 (T29) |
| M2 | Installer/registry metadata (templated/installed) | **New:** parse sibling `_meta.json` (23,747 rows), `.clawhub/origin.json` (2,649), `skill.json`, `_download_meta.json`, plus front-matter `source/origin/repository` (122,803 reps), `version`, `metadata` | Hand-check 50 positives. Cross-tab against M1 and the trailer-on-bulk-commit pattern |
| M3 | Registry blob lineage (copied from a known origin, with version) | T06–T10: treeless clones of the 31 registry repos, `git log --raw --no-abbrev`, join blob SHAs to `file_sha` | Reproduce the 6,719 / 216,440 anchors. Spot-check 30 matches by diffing |
| M4 | Agent trailers and coding-agent bot authors (agent-assisted) | T03: keep the v1 `LIKE` test as `ai_trailer_v1`, and add a line-anchored `ai_trailer_strict` plus the Cursor/Copilot/aider/Devin patterns ([provenance methods notes](../research_notes/GitSkills%20provenance%20sprint%202%20plan/provenance_methods.md)) | Per-rule precision on the 240 sample (T21). Prevalence by month, since recall depends on tool release dates |
| M5 | Bulk-batch exclusion (install, not authorship) | `batch_n` = reps sharing `(repo, first_commit_at)`, cutoff 6 | Sweep 2–12 (T29). Annotators judge "authored vs installed" |
| M6 | Near-duplicate clusters (templated family / derived) | T14: datasketch MinHash, 128 perms, 5-word shingles, LSH 0.8 / 0.6, union-find; verify candidates with exact Jaccard because LSH is approximate ([datasketch](https://ekzhu.com/datasketch/lsh.html)) | T22: 120 pairs in 5 similarity bands, two reviewers |
| M7 | Template similarity to the nearest registry version (templated) | T15: Jaccard of each skill to its nearest registry version | Same pair protocol as T22 |
| M8 | Scaffold fingerprint (templated, often also agent-assisted) | **New:** regex for leftover `init_skill.py` TODO placeholders and empty example `scripts/`/`references/`/`assets/` siblings ([wikidocs](https://wikidocs.net/381799)) | Fetch the verbatim scaffold from anthropics/skills, then hand-check hits. Allow multi-label |
| M9 | Repo context (prior only) | T04: agent folder family, repo age, size tier, `copy_share`, skill added on day 0 of repo | Baselines B2 and B4 show how much this alone explains |
| M10 | Wording/stylometry (weak agent/human) | T13 W1–W10. W9 uses the published excess-vocabulary list (~900 words) ([Kobak et al.](https://arxiv.org/abs/2406.07016v5); [llm-excess-vocab](https://github.com/berenslab/llm-excess-vocab)) | B5 text-only model under GroupKFold by owner and cluster. Ablation in T29 |
| M11 | Zero-shot detectors such as Binoculars (weak, sample only) | Stretch: prose body only, ≥200 tokens, 2–5k stratified sample, needs a GPU ([Binoculars](https://arxiv.org/html/2401.12070v3)) | TPR at 1% / 5% FPR on trailer positives vs pre-2022 markdown. Mark domain mismatch |
| M12 | Full per-file history, author/committer split, PR linkage (agent-assisted, edit timeline) | T25 widened: REST `commits?path=` and `commits/{sha}/pulls`, or GraphQL `history(path:)` with `authors`, which already parses Co-authored-by ([GraphQL Commit](https://docs.github.com/en/graphql/reference/commits)) | Estimate how many agent edits in middle commits the first/last-only data misses |

Two more methods are **out of scope for classification**. Commercial detectors bring cost, opacity and short-text failure: one study found open-source RoBERTa mislabelled **30–69% of human text** ([NBER w34223](https://www.nber.org/papers/w34223)). Supply-chain attestations (Sigstore, SLSA, gitsign) prove who signed a commit, not who wrote the text, and almost no skills carry them ([GitHub docs](https://docs.github.com/actions/security-guides/using-artifact-attestations-to-establish-provenance-for-builds)).

To combine the signals, T17 should keep the deterministic precedence the team already uses, with a recorded `basis`, `confidence` and `reasons`. On top of that it should run a Snorkel-style labelling-function analysis that reports coverage, overlaps and conflicts for each rule ([Snorkel docs](https://docs.snorkel.ai/docs/0.94/user-guide/data-dev/labeling-functions-lfs-overview/introduction-to-labeling-functions-lfs)). The analysis makes the "record of non-separating signals" in RESEARCH_QUESTION.md a concrete table without changing the labels. The research also changes how one class is read. A missing trailer is **never** evidence for the human label, so "likely human" must stay "history exists and no agent sign found."

**"Who published first"** is the hardest provenance question, and the dataset can only partly answer it. Copies have no history, so ordering within a cluster has to fall back through a chain of weaker sources:
- the representative's `first_commit_at`;
- registry commit dates from M3;
- `repos.created_at` and `discovered_at`;
- optionally, Software Heritage first-visit dates and World of Code blob-to-first-commit maps.

Every date in the output must carry its source. Author dates are author-controlled and can be rebased, so the app should show "earliest known evidence" rather than a verdict. Software Heritage maps a git blob SHA straight to `swh:1:cnt:<sha>`. Its `whereis` provenance endpoint, however, needs special permission and does not guarantee the earliest occurrence ([SWHID spec](https://docs.softwareheritage.org/devel/swh-model/persistent-identifiers); [swh-web provenance API](https://docs.softwareheritage.org/devel/swh-web/uri-scheme-api-provenance.html)).

## The Sprint 2 backlog: 95 core hours behind a two-day labelling gate

The ordering logic is simple. The 240-skill annotation (two raters, about 480 judgments) plus the 120 similarity pairs cannot be sped up by code. Every day the annotation sheets slip pushes T21, T23, T29 and the figures back by a day. The backlog therefore front-loads only what the sample needs: v1 labels, near-duplicate clusters, installer signals and a v2-lite label. Everything else runs in parallel while the raters work.

Effort figures are owner-agnostic person-hour estimates based on judgment, not measurement. Capacity assumes 3–4 contributors (the git log shows four authors) at about 5 focused hours a day for the roughly 8 working days to 10-28, which gives about 120–160 hours. **P0** is the core needed to answer RQ1 and must ship. **P1** is the provenance and app track, which should ship. **P2** is stretch or appendix. Cards prefixed N are new and do not exist in the CODING_PLAN.

| Order | Card | Work | Depends on | Est. h | Pri |
|---|---|---|---|---|---|
| 1 | N1 | Commit `.cursor/`, `config/`, `CODING_PLAN.md`, `.env.example` on a `sprint2/` branch. Create a venv. Add pyarrow, pyyaml, scipy, statsmodels, datasketch, httpx, pytest, pydantic, typer, rich. Fix the `.gitignore` additions and the download path | — | 3 | P0 |
| 2 | T00 | Package, config loader, CLI, Makefile, manifests | N1 | 5 | P0 |
| 3 | N2 | Verify `file_sha` = git blob SHA-1 on 1k rows. Document `content_sha_ok=2` and the 458,137 vs 458,548 gap | N1 | 1 | P0 |
| 4 | T01, T02 | Data loader, dev sample, structure features with SQL parity | T00 | 8 | P0 |
| 5 | T03 | Commit features: `ai_trailer_v1` (LIKE) **and** `ai_trailer_strict`. `classify_author` gains a **distribution-bot** class, plus ProgrammaticAccessBot | T01 | 5 | P0 |
| 6 | T04, T05 | Repo context, copy counts, v1 labels. Exact anchors 35,768 / 80,114 / 62,798 / 1,699,301 | T02, T03 | 6 | P0 |
| 7 | T14 | **Start the full MinHash run on day 2** in the background (multiprocess, memory-mapped signatures). Fallback: exact plus normalised-hash clusters | T01 | 8 (+ runtime) | P0 |
| 8 | N3 | Installer-metadata and front-matter provenance signals, scaffold fingerprint (M2, M8) | T01 | 5 | P0 |
| 9 | T13 | Wording W1–W10, with W9 from the fixed external lexicon (no training-split dependency) | T02 | 6 | P0 |
| 10 | T17 | Labels v2 with basis order **installer_declared > registry_exact > registry_near > cluster > copy_count**. Registry bases fill in when T10 lands (re-run is idempotent) | T05, T14, N3 | 5 | P0 |
| 11 | T19, T20 | 240-skill stratified sample (60 per label, near-threshold oversampling, ≤2 per repo, ≤1 per cluster) and blind sheets. **Out by end of 10-21** | T17 | 6 | P0 |
| 12 | Human | Two raters × 240 skills (~12 h each). Add one sheet column for "installed/copied vs authored" so the same pass also validates provenance | T20 | ~24 | P0 |
| 13 | T18 | Baselines B0–B6, GroupKFold by owner and cluster component. A learned W9 lexicon, if used, is fitted inside the training folds only | T17 | 6 | P0 |
| 14 | T06–T07 | Registry loader, GitHub client (ETag cache, rate-limit sleep, token never logged) | T00 | 8 | P1 |
| 15 | T08–T10 | Clone and mine 31 registry repos, distinct blobs, match to the dataset (anchors 6,719 / 216,440) | T06, N2 | 13 | P1 |
| 16 | T15 | Template similarity to the nearest registry version | T10, T14 | 4 | P1 |
| 17 | T21 | Cohen's kappa, per-rule precision with Wilson CIs, confusion matrix | annotation | 4 | P0 |
| 18 | T22 | Similarity validation pairs (code 3 h, reviewers ~6 h) | T14 | 3 (+6) | P0 |
| 19 | T23, T29 | Error analysis. Cutoff sweep (bulk 2–12 × copies 5–20), subgroups, ablation | T21, T18 | 10 | P0 |
| 20 | N4 | Provenance CLI MVP (next section) | T03, T05, T07, T14 | 34 | P1 |
| 21 | N6 | History enrichment sample (T25 widened to full per-file commits, PR linkage, committer split) | T07 | 8 | P1 |
| 22 | T24 | Adoption lag from registry matches (cheap once T10 exists) | T10 | 4 | P1 |
| 23 | T30–T32 | Figures 10–23, export tables, run_all and smoke test. The HTML explorer moves to P2 | all P0 | 13 | P0 |
| 24 | T11, T12, T16, T26–T28, N5, N7 | Change classifier, popular families, skill types, propagation patterns, RQ2 models, assistance probe, skills.sh crawl, diff-from-origin risk linter | various | 40+ | P2 |

The P0 code comes to about **95 hours plus about 30 hours of human judgment**. P1 adds about **71 hours**. P0 + P1 slightly exceeds a four-person capacity estimate, so the app MVP and N6 are the first things to trim.

The **critical path** runs through annotation:

N1 → T00 → T01 → T03 → T05 → T17 (with T14 and N3 done) → T19/T20 → annotation → T21 → T23/T29 → T30–T32.

Mapped to dates:

| Date | Critical-path work |
|---|---|
| 10-18 / 10-19 | N1, T00, N2 |
| 10-19 / 10-20 | T01–T05 |
| 10-19 | MinHash (T14) launches |
| 10-21 | T13, N3, T17-lite; sheets released |
| 10-22 to 10-24 | Annotation |
| 10-25 | T21, T22 |
| 10-26 | T23, T29 |
| 10-27 | Figures, export, smoke |
| 10-28 | Buffer and code freeze |

The plan has **at most one day of slack**. If the MinHash run is not finished by the evening of 10-20, draw the sample with the exact-plus-normalised-hash fallback rather than waiting.

## Six corrections to the existing CODING_PLAN

The cards are mostly sound. The research points to these specific changes.

**1. The T13/T18 circularity.** T13's W9 phrase lexicon is supposed to come from a T18 training split, while T18 consumes T13's features. Replace W9 with the published Kobak excess-vocabulary list plus a short team list frozen before labelling. If a learned lexicon is still wanted, fit it inside T18's cross-validation folds as a second pass. That removes the loop and the leakage at once.

**2. The trailer regression risk in T03/T05.** The notebook's test is `lower(message) LIKE '%co-authored-by:%<tool>%'`. It matches any later mention of the tool name after any co-author line, including a human co-author followed by a "Generated with Claude Code" footer. T03's spec, "only Co-authored-by trailers count", will therefore **not reproduce 80,114**. Port v1 literally, including the `bot_group = 'not a bot'` clause and the Skill-distribution bot group that the Cursor rules' summary omits. Then add the strict parser as a v2 feature, and report the delta between the two parsers as a finding ([project audit](../research_notes/GitSkills%20provenance%20sprint%202%20plan/project_audit.md)).

**3. The unused installer metadata.** Neither Sprint 1 nor the plan touches `_meta.json`, `.clawhub/origin.json` or front-matter `source`/`version`. These are deterministic statements that a skill was installed from a registry. Adding them as a top-precision templated basis (N3) is cheap, and it reaches skills that have no history, which is where 90.5% of the "insufficient" mass sits.

**4. The missing Python dependencies.** These block T00, so N1 has to run first.

**5. MinHash timing.** Pure-Python datasketch over about 1.84M texts takes hours, so T14 moves from Phase 2 to day 2.

**6. The scope-cut rule for the 10-22 go/no-go.** If annotation sheets are not out by end of 10-21, or if T10's registry match fails its anchors, then:
- T24–T28 become an appendix, or move to the write-up as future work;
- T16's skill-type taxonomy is dropped (32% unclassified in the pilot);
- T31's HTML explorer and the Streamlit front end are deferred;
- the provenance claim narrows to M1 + M2 + M4–M6 (exact copies, installer metadata, trailers, bulk, clusters), which are all computable without the registry track.

What must not be cut is T18's baselines and T29's ablation. Without them the "no separable signal" explanation cannot be tested. The plan's open questions also need owners this week: who annotates, whether an LLM third annotator is allowed (it requires an `ai-use-log.md`), and whether the instructor accepts RQ2 at all.

## A provenance CLI built from the same modules

The app should be a `provenance` subpackage of `skillsig`, not a separate codebase. It reuses T07's GitHub client, T02/T03's feature functions, T05/T17's rules and T14's index. The pipeline is linear and cacheable:

1. Parse the URL. Accept repo, `tree`, `blob` and `raw.githubusercontent` forms, resolve refs that contain slashes by trying the longest valid ref, and pin the commit SHA.
2. Discover skill files with one recursive tree call. Globs: `**/SKILL.md`, plus optionally AGENTS.md, CLAUDE.md and `.cursor/rules`.
3. Fetch per-file history with GraphQL `history(path:)`. Its `authors` field already merges git authors with Co-authored-by trailers, and several files can be aliased into one query ([GraphQL Commit](https://docs.github.com/en/graphql/reference/commits)). If there is no token or the repo is huge, fall back to a blobless clone with PyDriller's `filepath` filter ([PyDriller](https://pydriller.readthedocs.io/en/latest/repository.html)).
4. Compute features.
5. Match against a prebuilt corpus index: exact blob lookup in a slim DuckDB parquet, plus an LSH pickle re-ranked by Jaccard.
6. Emit a pydantic `Report` as JSON, Markdown or Rich tables.

GraphQL allows **5,000 points per hour** per user token, and requests over 10 seconds are killed ([GitHub GraphQL limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api)). By the design notes' rough estimate, a repo with fewer than 20 skills costs well under 50 points.

The report must avoid claiming more than the evidence shows. It should give:
- a categorical verdict using the same precedence as the research labels, worded as "Likely templated / copied", "Agent-assisted signals present", "No agent signals found (likely human)" or "Insufficient evidence";
- a coarse confidence level;
- a signal table listing **every check, including those that did not fire**;
- a table of copies ordered by earliest evidence date, each with its date source;
- fixed caveats: corpus snapshot July 2026, history for only about a quarter of representatives, trailers can be disabled, bulk-commit warning.

Once T21 exists, each rule should show its measured precision ("correct in X of Y annotated cases"). Detector studies show why text-only verdicts need hedging: even top detectors miss about **13% of style-imitating AI passages** ([Epoch AI](https://epoch.ai/data-insights/ai-detectors-false-negatives)). The skill text in scanned repos is untrusted input. During this research the GitSkills arXiv HTML page itself carried an embedded instruction-like block ([end goals notes](../research_notes/GitSkills%20provenance%20sprint%202%20plan/end_goals.md)), so the app must never pass fetched text to an LLM unguarded.

Testing has three layers:
- pure unit tests for the URL parser and the rules;
- pytest-recording / VCR.py cassettes with the `authorization` header filtered out ([pytest-recording](https://pypi.org/project/pytest-recording));
- a golden set: anthropics/skills (origin), one single-skill Claude-trailer commit (agent-assisted), one bulk install of 6 or more skills with a trailer (must **not** be labelled agent-assisted), one high-`copy_share` mirror, and one hand-edited personal skill.

Delivery is CLI first. A Streamlit front end comes only if time remains. Streamlit Community Cloud has a memory ceiling of about 2.7 GB, so a hosted version would need a reduced index, for example multi-copy families plus registry bodies ([Streamlit limits](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)). Hugging Face Gradio and Docker Spaces now need a paid plan to create ([HF Spaces](https://huggingface.co/docs/hub/en/spaces-overview)).

The full design is about 74 hours. An MVP that reuses the research modules (URL parser, discovery, history, corpus match, render, golden tests) is about **34 hours**. Two items should be measured in week 1 because the design depends on them: the size and load time of the full LSH index (roughly 1.9 GB of hash values alone, by estimate), and whether GraphQL `history(path:)` follows renames. Assume it does not.

## Data to acquire, and what provenance makes possible afterwards

New data should fill specific gaps in GitSkills: it has only first and last commits, no copy history, no PR linkage, and no usage counts.

**Priority 1 is a stratified GitHub API enrichment (N6).** A PAT gets 5,000 REST requests per hour, and code search is capped at **10 requests per minute**, which rules out rediscovery at scale ([REST limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api); [Search API](https://docs.github.com/en/rest/search/search)). Pull these for the 240 annotated skills plus a few thousand stratified others:
- the full per-file commit list;
- every trailer line;
- the author/committer split;
- the associated PR and its author type;
- topics, fork parent and contributor count.

This turns the trailer signal from first/last-commit-only into a timeline, for example an agent draft followed by human edits. It also lets the team estimate how many agent edits the dataset misses. `config/default.yaml` currently caps calls at 2,000, which needs raising for this. Hash logins and emails with HMAC at ingestion. GitHub's Acceptable Use Policies allow research use of public non-personal data on condition that publications are open access ([GitHub AUP](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies)).

**Priority 2 is the skills.sh API.** It exposes per-skill installs, an `isDuplicate` flag, a SHA-256 content hash, files and partner security audits, at 600 requests per minute ([skills.sh API](https://skills.sh/docs/api)). Installs are a far better adoption outcome than stars, and `isDuplicate` gives an independent copy label for validating M1/M6. Because its hash is SHA-256, the join to GitSkills has to recompute hashes from `content`, not reuse `file_sha`.

**Priority 3 is cheap edge data.** Claude plugin `marketplace.json` files declare upstream source repos for vendored skills, which gives explicit copy-to-origin edges ([Claude Code marketplaces](https://code.claude.com/docs/en/plugin-marketplaces)). AIDev's labelled agent PRs supply bot logins and trailer formats, although its 2025-08-01 cutoff predates SKILL.md ([AIDev](https://arxiv.org/html/2602.09185v1)).

**Stretch sources, for which access should be requested now** because approval may outlast the sprint:
- GH Archive on BigQuery for bot PR events and star timing ([gharchive](https://gharchive.org/));
- Software Heritage for first-visit timestamps;
- World of Code, whose blob-to-first-commit maps are the strongest cross-forge answer to "who published first" ([WoC workflow](https://arxiv.org/pdf/2010.16196)).

GHTorrent is dead and BigQuery `github_repos` predates skills. The only use for the latter is as a pre-LLM human markdown corpus for M11.

The point of provenance is what it enables. The security literature makes the case directly. Liu et al. confirmed **157 malicious skills, with 84.2% of vulnerabilities in natural-language SKILL.md text**. One actor produced 54.1% of them from a template, and one near-clone of a legitimate skill differed by only three injected lines ([Liu et al.](https://arxiv.org/html/2602.06547v1)). OWASP's skills guidance names provenance as a primary mitigation for supply-chain compromise ([OWASP AST02](https://owasp.org/www-project-agentic-skills-top-10/ast02)). The research supports five follow-on questions, labelled FQ to avoid clashing with the plan's RQ2:

| ID | Question | What it measures | Sprint 2 fit |
|---|---|---|---|
| FQ1 | Diffusion by class | Copies and time-to-first-copy by provenance class | Feasible |
| FQ2 | Attribution and licensing of copies | LICENSE sibling, origin mention, repo licence, including redistribution of anthropics/skills' source-available document skills ([anthropics/skills](https://github.com/anthropics/skills)), against the "at most a quarter attributed" Stack Overflow baseline ([Baltes & Diehl](https://arxiv.org/abs/1802.02938)) | Feasible |
| FQ3 | Security drift | Risk-pattern regexes (pipe-to-shell, credential paths, invisible Unicode) on lines that modified copies add relative to their origin | Feasible (light) |
| FQ4 | Staleness | Copies frozen at old origin versions, mirroring CISPA's outdated-snippet study ([CISPA](https://www.cispa.de/en/research/publications/4053-measuring-the-effects-of-stack-overflow-code-snippet-evolution-on-open-source-software-security)) | Needs registry versions |
| FQ5 | Homogenization | Lexical diversity by class and monthly cohort | Needs registry versions |

FQ1–FQ3 are computable from data in hand once M1–M3 exist. FQ4 is the plan's RQ2 by another name. FQ4 and FQ5 depend on registry versions, so they are stretch. The organizers' own suggested themes include "whether modified copies add command execution or network access" ([MSR 2027 Mining Challenge](https://2027.msrconf.org/track/msr-2027-mining-challenge)). The same machinery yields practical tools: an origin lookup, a diff-from-origin risk linter, an attribution checker, and trust badges.

## Conclusion

The research mainly changes **what counts as the bottleneck**. The 33 cards make Sprint 2 look like a coding problem. In practice, the measured reliability of the labels determines whether any downstream claim holds, and reviewers will expect it ([MSR 2027 Mining Challenge](https://2027.msrconf.org/track/msr-2027-mining-challenge)). That reliability depends on getting annotation sheets to two raters by 10-21. The first three days should therefore go only to the environment, the faithful v1 port, MinHash and installer signals. Registry lineage, the API enrichment and the app can follow, because they run in parallel with the annotation.

The cheapest new signal is installer metadata, which is already in the data. The most expensive, text detection, is the least trustworthy. A credible outcome for the paper is that commit and copy evidence separate the classes while wording does not. That is a publishable result, provided the baselines and ablation are built to show it. One provenance core, measured against hand labels and reused by both the paper and the CLI, gives the write-up a contribution that prior agent-attribution studies lack: precision measured for each rule.
