# The "So What" of Skill Provenance: End Goals and Follow-on RQs for GitSkills (MSR 2027 Mining Challenge)

Scope: once a SKILL.md can be labeled human-authored, agent-generated, or templated/copied, what is that label for? Research date: 2026-10-17.

Note for the report writer: the arXiv HTML page of the GitSkills paper (arxiv.org/html/2608.10906) contained an embedded text block shaped like instructions to the reader, plus a base64-encoded SKILL.md. The fetch tool ignored both. This is a small live example of the point below: skill text found in the wild has to be treated as untrusted input.

## 1. Security and supply chain: why provenance matters for trust and vetting

### Takeaway
Agent skills are an active attack surface. Most confirmed malicious skill content is natural-language instructions in SKILL.md rather than code. The largest malicious cluster found so far was a templated, copy-based campaign by one actor, and near-clones of legitimate skills with a few injected lines have also been found. Provenance (who wrote it, whether it was copied, and how it differs from its origin) is therefore a direct input to vetting. OWASP's skills guidance names provenance and signing as primary mitigations.

### Cited Findings
- **Liu et al. (arXiv 2602.06547, Feb 2026), the main study:**
  - They crawled 98,380 skills from skills.rest and skillsmp.com, statically flagged 4,287, and behaviorally confirmed 157 malicious skills containing 632 vulnerabilities (0.16% of all skills). — [Liu et al., arXiv 2602.06547](https://arxiv.org/html/2602.06547v1)
  - 84.2% of vulnerabilities sit in natural-language SKILL.md text rather than code. The top patterns are remote script execution (25.2%), behavior manipulation (18.8%) and credential harvesting (17.7%). — [Liu et al.](https://arxiv.org/html/2602.06547v1)
  - Provenance and templating evidence: one actor ("smp_170") accounts for 85 of the 157 malicious skills (54.1%). These skills use templated brand impersonation with 100% template consistency and 26 identical lines, across 85 brands. A near-clone of a legitimate skill (98.3% similarity) differs by only three injected lines that run a file-upload script. A "flow-nexus" family spans three publishers. — [Liu et al.](https://arxiv.org/html/2602.06547v1)
  - Skill name and file size do not predict maliciousness (Spearman r ≈ −0.005). 73.2% of malicious skills contain "shadow features" (undocumented behavior). Registries removed 93.6% of the reported skills within 30 days. — [Liu et al.](https://arxiv.org/html/2602.06547v1)
  - Their registry recommendations include submission-time fingerprinting, cross-skill similarity checks to catch "template factories", and comparing documentation against behavior. — [Liu et al.](https://arxiv.org/html/2602.06547v1)
- **Holzbauer et al. (arXiv 2603.16572), "Malicious Or Not", a counterpoint on scanners:**
  - They studied 238,180 unique skills from ClawHub, Skills.sh, SkillsDirectory and GitHub. Marketplace scanners flagged 36.20–41.93% of ClawHub skills, and only 33 of 27,111 skills (0.12%) were flagged by all five scanners.
  - Repository-aware analysis (codebase alignment plus repository metadata) cut the non-benign share to 0.52% (15 of 2,887 pairs). This is a reduction in flags, not a measured true-malicious rate.
  - Abandoned GitHub repositories that skills reference can be hijacked: 121 skills across 7 repos, the most-downloaded with 2,032 downloads. 354 skills appear in more than one repository.
  - [Holzbauer et al., arXiv 2603.16572](https://arxiv.org/html/2603.16572v1)
- **Snyk ToxicSkills (Feb 5, 2026):** audited 3,984 skills from ClawHub and skills.sh. 36.82% (1,467) had at least one security flaw and 13.4% (534) had at least one critical issue. Daily submissions rose from under 50 in mid-January to over 500 by early February. — [Snyk](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub)
- **Other papers:**
  - A 2026 paper frames SKILL.md as a registry-facing attack surface: how adversarial skills get admitted, ranked and selected before anything runs. — [arXiv 2605.11418](https://arxiv.org/pdf/2605.11418)
  - An attention-based detector distinguishes code-level from instruction-level attacks. — [arXiv 2606.23416](https://arxiv.org/pdf/2606.23416)
- **Precursor incidents (2025):**
  - Pillar Security's "Rules File Backdoor" (March 2025) showed hidden Unicode instructions in Cursor and Copilot rules files making the agent silently insert malicious script tags. Cursor said managing the risk was the user's responsibility. Pillar notes rules files are shared and adopted with little vetting. — [Pillar Security](https://pillar.security/blog/new-vulnerability-in-github-copilot-and-cursor-how-hackers-can-weaponize-code-agents); [The Hacker News](https://thehackernews.com/2025/03/new-rules-file-backdoor-attack-lets.html?hl=en)
  - Invariant Labs' MCP "tool poisoning" (April 2025) hid instructions in tool descriptions that the model sees and the user does not. A demo exfiltrated WhatsApp data through a second, malicious MCP server. — [Simon Willison](https://simonwillison.net/2025/Apr/9/mcp-prompt-injection/); [Docker blog](https://www.docker.com/blog/mcp-horror-stories-whatsapp-data-exfiltration-issue/)
- **OWASP Agentic Skills Top 10:**
  - AST01 Malicious Skills is mitigated by signing and registry scanning. AST02 Supply Chain Compromise is mitigated by provenance and transparency logs. Skill registries lack provenance controls that are standard in npm, PyPI and Cargo, and a compromised skill inherits the agent's full credentials. — [OWASP AST02](https://owasp.org/www-project-agentic-skills-top-10/ast02); [OWASP AST10 v0.5 PDF](https://owasp.org/www-project-agentic-skills-top-10/docs/OWASP-Agentic-Skills-Top10-v0.5.pdf)
  - The version is unclear: Adversa says v1.0 shipped on Aug 17, 2026, while the v0.5 draft is dated June 2026. — [Adversa](https://adversa.ai/blog/owasp-agentic-skills-top-10-explained)
- **GitSkills' own suggested RQs** include "whether modified copies add command execution or network access" and how often bundled executables are copied. This is an organizer-endorsed provenance-to-security link. — [MSR 2027 Mining Challenge](https://2027.msrconf.org/track/msr-2027-mining-challenge)

### Inferences
- The strongest "so what" is **diff-from-origin risk**. Once a copy is linked to its origin (the earliest instance in its content cluster, or an anthropics/skills file), the team can measure what modified copies add, such as new URLs, `curl | bash`, credential paths or hidden Unicode. That is the near-clone attack Liu et al. found by hand, done at scale on GitHub rather than registries.
- Provenance also maps onto an SLSA-like trust ladder for skills:
  - L0: unknown origin.
  - L1: traceable to an origin repo and commit.
  - L2: verbatim copy of a known, trusted origin, verifiable by hash.
  - L3: signed or published by an official vendor.
  - A badge based on the content-hash match to a vetted origin is implementable now with GitSkills' content grouping.
- Holzbauer et al. show that context and provenance reduce false positives. That supports framing provenance as a way to rank scanner alerts, not only as a detector of malice.

### Gaps
- No official SLSA spec or Sigstore profile for agent skills was found. The SLSA analogy is our own framing and is not a published standard.
- No study found measures malicious-pattern prevalence by authorship class (human vs. agent-written). This is open territory.
- Snyk's 76-payload and 91%-prompt-injection figures appear only in secondary sources and were not verified on Snyk's page.

## 2. Quality and maintenance: do agent-generated or copied skills differ?

### Takeaway
There is no direct study yet comparing quality or maintenance of skills by provenance class. Mature analogues exist and can be mirrored: copied Stack Overflow snippets go stale and spread vulnerabilities, AI-generated code shows elevated vulnerability rates, and duplication and churn in code rose after AI assistants spread. GitSkills ships the fields needed: content clusters, first and last commit authors, siblings and repo metadata.

### Cited Findings
- **Fields GitSkills provides:**
  - 3,797,117 SKILL.md occurrences, 1,877,981 distinct contents, about 50.5% verbatim copies.
  - 282,200 repos from 195,841 accounts.
  - 7,264,865 bundled sibling files.
  - 458,548 sampled commit histories with anonymized first- and last-commit authors.
  - Tables: `artifacts`, `repos`, `artifact_siblings`, `mining_runs`.
  - [MSR 2027 Mining Challenge](https://2027.msrconf.org/track/msr-2027-mining-challenge); [GitSkills arXiv 2608.10906](https://arxiv.org/html/2608.10906)
- **GitSkills limitations:** public repos and default branches only, files under 384 KB, author identities hashed and emails masked. — [GitSkills arXiv HTML](https://arxiv.org/html/2608.10906)
- **Organizer-suggested themes** include "metrics for instructions" (size, churn, age, clone coverage, readability), staleness relative to the tools described, and "whether copies propagate changes consistently, as code clones do." — [MSR 2027 Mining Challenge](https://2027.msrconf.org/track/msr-2027-mining-challenge)
- **Stack Overflow clone-propagation analogues:**
  - Baltes and Diehl found at most a quarter of copied SO snippets carry the attribution the license requires, and at most 1.8% of repositories using SO code comply with CC BY-SA 3.0. — [Baltes & Diehl, arXiv 1802.02938](https://arxiv.org/abs/1802.02938)
  - The CISPA study (IEEE S&P 2024) scanned 1.5M SO snippets against 11,479 GitHub projects. It found 2,405 outdated snippet versions in 2,109 projects, and 43 projects missing security fixes (forked about 1,085 times on average). — [CISPA](https://www.cispa.de/en/research/publications/4053-measuring-the-effects-of-stack-overflow-code-snippet-evolution-on-open-source-software-security)
  - A C++ study found 69 vulnerabilities in reused SO snippets across 2,589 GitHub files. — [arXiv 1910.01321](https://arxiv.org/pdf/1910.01321v1.pdf)
- **AI-generated code analogues:**
  - About 40% of 1,689 Copilot-generated programs were vulnerable (Pearce et al., IEEE S&P 2022). — [arXiv 2108.09293](https://arxiv.org/abs/2108.09293)
  - GitClear reports an 8x rise in duplicated 5+ line blocks during 2024, and churn rising from 3–4% to 5.5% in 2023. This is industry, not peer-reviewed, and correlational. — [LeadDev](https://leaddev.com/software-quality/how-ai-generated-code-accelerates-technical-debt); [Arc.dev](https://arc.dev/developer-blog/impact-of-ai-on-code/)
- **Agentic PR precedent (MSR 2026 challenge, AIDev, 932,791 agent-authored PRs):**
  - Accepted papers compared agents on acceptance rate, review volume and commit message quality, and studied test inclusion in agentic PRs.
  - This shows the challenge format rewards "agent vs. human artifact outcome" comparisons.
  - [AIDev arXiv 2602.09185](https://arxiv.org/pdf/2602.09185v1); [MSR 2026 task-level evaluation paper](https://2026.msrconf.org/details/msr-2026-mining-challenge/42/A-Task-Level-Evaluation-of-AI-Agents-in-Open-Source-Projects); [MSR 2026 tests in agentic PRs](https://2026.msrconf.org/details/msr-2026-mining-challenge/44/An-Empirical-Study-of-Tests-in-Agentic-Pull-Requests)
- **Ling et al. (40,285 marketplace Claude skills)** report heavy-tailed length, "strong ecosystem homogeneity and widespread intent-level redundancy", and non-trivial safety risks. The abstract gives no figures. — [arXiv 2602.08004](https://arxiv.org/abs/2602.08004)

### Inferences
- A direct mirror of CISPA is measurable in GitSkills: "stale copy" = a copy whose content differs from the current head of its origin cluster, where the origin later changed. The question is whether copies receive upstream fixes. Feasibility depends on how many origins have more than one version in the commit sample.
- Measurable quality and maintenance outcomes per provenance class:
  - Body length and front-matter completeness (name, description, license fields).
  - Description length and specificity.
  - Count of bundled scripts.
  - Number of commits, and days between first and last commit (a maintenance proxy).
  - Repo stars and forks.
  - Number of copies of the cluster (an adoption proxy).
  - Last-commit age (an abandonment proxy).

### Gaps
- No published study compares human-written vs. LLM-written natural-language agent instructions on quality or maintenance.
- Clone co-change literature (e.g., inconsistent changes to code clones) was not retrieved in this session. Cite it from the team's own SE coursework references if needed.
- The GitClear 2025 primary report was not retrieved.

## 3. Ecosystem dynamics: diffusion, template origins, homogenization, licensing

### Takeaway
GitSkills itself documents copy-driven diffusion, with about 50.5% verbatim copies and no registry or package manager. Anthropic's official anthropics/skills repo mixes Apache-2.0 skills with source-available document skills. That makes "copied without license or attribution" a concrete, checkable outcome, mirroring Baltes and Diehl's SO attribution study. Homogenization from LLM-written text is documented in general NLP and emerging for code, but untested for agent instructions.

### Cited Findings
- **How skills spread:**
  - Skills have "no compiler check, no central registry, and no package manager", so they spread by copying between repositories. The organizers suggest studying concentration of copying and transfer mechanisms (direct addition, catalogs, scaffolding). — [MSR 2027 Mining Challenge](https://2027.msrconf.org/track/msr-2027-mining-challenge)
  - Anthropic published the skill format in October 2025 as an open specification. — [GitSkills arXiv HTML](https://arxiv.org/html/2608.10906)
- **anthropics/skills licensing:**
  - The README says: "Many skills in this repo are open source (Apache 2.0)". The docx, pdf, pptx and xlsx skills "are source-available, not open source".
  - The repo provides a `template-skill` as a starting point. No license is stated for it in the README; check the per-folder LICENSE files.
  - [anthropics/skills](https://github.com/anthropics/skills)
- **Homogenization evidence:**
  - General NLP: co-writing with InstructGPT reduced lexical and key-point diversity, and an "artificial hivemind" effect appears across 23 models and 13 languages. — [arXiv 2510.04226](https://arxiv.org/pdf/2510.04226v1); [NeurIPS 2026 Multilingual Artificial Hivemind](https://neurips.cc/virtual/2026/164689)
  - Code: a preprint (Burtch) measures convergence of Kaggle submissions under AI assistants (e.g., seed 42). It is provisional. — [papers.cool 2607.13077](https://papers.cool/arxiv/2607.13077)
- **Downstream use of GitSkills-scale data:** about 888k skill specs embedded against O*NET tasks, an "Agentic Adoption Index". Embedding-based analysis of skill corpora is already accepted practice. — [Lee et al., arXiv 2608.20425](https://arxiv.org/abs/2608.20425)

### Inferences
- **Licensing RQ:** for copies of anthropics/skills content, especially the source-available docx/pdf/pptx/xlsx skills, measure the share of host repos that keep the LICENSE file (check `artifact_siblings` for a LICENSE sibling) or reference the origin. Redistributing source-available files in public repos is a concrete compliance signal, but the team should frame it as "potential" and not make legal claims.
- **Homogenization RQ:** compare within-class lexical and embedding diversity of distinct skill bodies across monthly cohorts and provenance classes. Expectation: agent-generated skills (e.g., created with skill-creator) cluster tighter. This needs a near-duplicate threshold separate from exact hashes.

### Gaps
- There is no ground truth for which skills were produced by Anthropic's skill-creator. Proxies include structural fingerprints of skill-creator output and Co-Authored-By trailers, if available in the commit sample. Whether GitSkills includes commit messages or trailers was not confirmed; the authors' docs mention only anonymized first and last commit authors.

## 4. Practical outputs and applications

### Takeaway
Provenance labels can feed tools: origin recommendation ("canonical source of this copy"), registry dedup, attribution and license linting, trust badges, and diff-from-origin risk alerts. The academic and OWASP literature explicitly asks for similarity checks, provenance and transparency logs.

### Cited Findings
- Liu et al. recommend cross-skill similarity checks at submission and documentation-behavior comparison. — [Liu et al.](https://arxiv.org/html/2602.06547v1)
- Holzbauer et al. recommend repository-aware analysis, reserving abandoned account names, and weekly rescans (about $24 per 3,000 skills). — [Holzbauer et al.](https://arxiv.org/html/2603.16572v1)
- OWASP AST02 calls for provenance and transparency logs, and says no single control suffices. — [OWASP AST02](https://owasp.org/www-project-agentic-skills-top-10/ast02)
- The CISPA authors recommend continuously monitoring origins for fixes rather than warning only at copy time. That is the "notify copies when origin is patched" application. — [CISPA](https://www.cispa.de/en/research/publications/4053-measuring-the-effects-of-stack-overflow-code-snippet-evolution-on-open-source-software-security)

### Inferences
Candidate one-sprint artifacts:
1. A `skill-origin` lookup: SHA-256 of a normalized SKILL.md returns the earliest repo, copy count, and whether it matches an anthropics/skills file.
2. A diff-from-origin risk linter that flags added URLs, shell pipes, secret paths and invisible Unicode.
3. An attribution and license checker for copied skills.
4. Policy recommendations for platforms (registries, GitHub, Anthropic): show origin badges, require a `license`/`source` front-matter field, and alert copies when the origin changes.

### Gaps
- No registry was found that publicly displays skill provenance badges today. Whether skills.sh or ClawHub do was not verified.

## 5. What an MSR mining-challenge paper looks like, and fitting the end goal

### Takeaway
MSR 2027 challenge papers are 4 pages plus 1 page of references in IEEE format, double-anonymous, due Dec 23, 2026 (abstract Dec 18). They must state the dataset version, include an ethics section, and are encouraged to provide a reproducible archive. Accepted MSR 2026 challenge papers were focused, 2–4 RQ empirical comparisons on the provided dataset, so one provenance RQ plus 2–3 "so what" RQs fits.

### Cited Findings
- **Format:** 4 pages plus 1 page of references, IEEE `\documentclass[10pt,conference]{IEEEtran}`, double-anonymous. — [MSR 2027 Mining Challenge](https://2027.msrconf.org/track/msr-2027-mining-challenge)
- **Required structure:** introduction and motivation; dataset version; approach and tools; results and implications; a separate ethics section before the conclusion. Cite GitSkills arXiv 2608.10906. — [MSR 2027 Mining Challenge](https://2027.msrconf.org/track/msr-2027-mining-challenge)
- **Dates:** abstract Dec 18, 2026; paper Dec 23, 2026; notification Jan 19, 2027. Submission via msr2027-challenge.hotcrp.com. — [MSR 2027 Mining Challenge](https://2027.msrconf.org/track/msr-2027-mining-challenge)
- **Review and awards:** there is no published rubric. The Best Mining Challenge Paper award requires code and data on preserved archives (Zenodo or Figshare, anonymized during review). Personally identifiable information is prohibited. — [MSR 2027 Mining Challenge](https://2027.msrconf.org/track/msr-2027-mining-challenge)
- **Second dataset:** the 2027 challenge has two datasets, GitSkills and SpecMine. SpecMine's suggested RQs explicitly include "distinguishing human-authored from agent-generated specs", which validates human-vs-agent provenance as an organizer-sanctioned framing. — [MSR 2027 Mining Challenge](https://2027.msrconf.org/track/msr-2027-mining-challenge)
- **Precedent:** the MSR 2026 challenge (AIDev) used the ACM sigconf template, also 4+1 pages. Example accepted papers: agent comparison on PR acceptance and review volume; tests in agentic PRs. — [MSR 2026 paper 42](https://2026.msrconf.org/details/msr-2026-mining-challenge/42/A-Task-Level-Evaluation-of-AI-Agents-in-Open-Source-Projects); [MSR 2026 paper 44](https://2026.msrconf.org/details/msr-2026-mining-challenge/44/An-Empirical-Study-of-Tests-in-Agentic-Pull-Requests)
- MSR 2027 is in Dublin, April 26–27, 2027, co-located with ICSE. — [UCL news (search snippet)](https://www.ucl.ac.uk/engineering/news/2026/sep/ucl-led-dataset-opens-new-research-how-ai-coding-agents-are-instructed); [MSR 2027](https://2027.msrconf.org/)

### Inferences
- A typical 4-page budget:
  - About 0.5 page intro and motivation (the security and attribution "so what").
  - About 0.75 page data and method: dataset version, provenance labeling, validation by manual annotation of a stratified sample with two raters and Cohen's κ (as Liu et al. did with κ = 0.89–0.91).
  - About 2 pages of RQ results with one figure or table each.
  - About 0.5 page implications for registries and tool builders, plus threats to validity.
  - A short ethics section: hashed authors, no PII, no attempt to de-anonymize.
- Reviewers will expect validation of the provenance classifier itself: precision and recall on a hand-labeled sample. Without that, downstream RQs are weak.

### Gaps
- Past MSR "best mining challenge paper" winners were not retrieved, and no review rubric is published.

## 6. Proposed RQs beyond detection, with analysis and data needs

### Takeaway
Five RQs that turn provenance labels into consequences. Each uses only GitSkills fields (plus optional GitHub API calls) and can be partially implemented in one sprint.

### Cited Findings (basis for each RQ)
- Copy, sibling and commit fields — [MSR 2027 Mining Challenge](https://2027.msrconf.org/track/msr-2027-mining-challenge)
- Near-clone injection and template factories — [Liu et al.](https://arxiv.org/html/2602.06547v1)
- Attribution baseline — [Baltes & Diehl](https://arxiv.org/abs/1802.02938)
- Stale-copy baseline — [CISPA](https://www.cispa.de/en/research/publications/4053-measuring-the-effects-of-stack-overflow-code-snippet-evolution-on-open-source-software-security)

### Inferences (proposed RQs)

**RQ1. Diffusion: do skills of different provenance classes spread differently?**
- Data: content-hash clusters; occurrence count per cluster; repo counts and owner counts; first-commit dates (sampled) for the origin and its copies.
- Analysis:
  - Compare copies per distinct content and time-to-first-copy across human, agent and template-origin classes, using Mann-Whitney U and Cliff's δ.
  - Check concentration of copying with a Gini coefficient or top-k share.
- Measurable outcome: median copies per skill by class; share of all occurrences traced to anthropics/skills origins.

**RQ2. Attribution and licensing: are copied skills attributed and license-compliant?**
- Data: copies whose content matches anthropics/skills (or another identified origin); `artifact_siblings` for LICENSE files; SKILL.md text for origin URLs or "adapted from" mentions; repo license metadata.
- Analysis:
  - Share of copies with (a) a LICENSE sibling, (b) an origin URL or mention, (c) a repo license compatible with Apache-2.0.
  - Separately, count public redistribution of the source-available docx/pdf/pptx/xlsx skills.
- Measurable outcome: attribution rate, to compare against Baltes and Diehl's "at most a quarter" for SO snippets.

**RQ3. Security drift: do modified copies and agent-generated skills carry more risk patterns than their origins?**
- Data: near-duplicate pairs (origin vs. modified copy) and the textual diff; bundled scripts.
- Analysis:
  - Regex and static indicators taken from Liu et al.'s categories: external URLs, `curl|wget ... | sh`, credential paths (`~/.ssh`, `.env`, AWS keys), "do not tell the user" or secrecy phrasing, invisible Unicode, permission-bypass flags.
  - Compare indicator rates by provenance class, and for the lines added in modified copies against their origins, using a chi-square test or Fisher's exact test with odds ratios.
- Measurable outcome: OR of a risk indicator given modified-copy vs. origin, and given agent vs. human.
- Validation: manual review of the top-N flagged diffs.

**RQ4. Maintenance and staleness: are copies and agent-generated skills maintained or abandoned?**
- Data: the commit sample (first and last commit dates, commit counts); repo pushed_at and stars; origin cluster versions.
- Analysis:
  - Survival or Kaplan-Meier analysis of "time since last change" by class.
  - Share of copies frozen at an older origin version (the CISPA-style stale-copy rate).
- Measurable outcome: stale-copy rate; median maintenance span per class.

**RQ5. Homogenization: is agent-generated skill text less diverse?**
- Data: distinct skill bodies sampled per class and per monthly cohort.
- Analysis:
  - Type-token ratio, compression ratio, and mean pairwise embedding similarity within class.
  - Trend over monthly cohorts.
- Measurable outcome: diversity index by class and month.

**Sprint-feasible subset:** RQ1, plus RQ2 restricted to anthropics/skills origins, plus a light RQ3 (regex indicators on origin-vs-copy diffs). All are computable from the SQLite or Parquet data with pandas and hashing, with no model training. RQ4 and RQ5 are stretch goals.

### Gaps
- How reliable agent-vs-human labels can be is not established anywhere. Commit metadata (bot authors, Co-Authored-By trailers) may or may not be present after GitSkills' anonymization, so the team must check the schema.
- Whether the 458,548 commit histories cover enough origin and copy pairs for RQ4 is unknown until the data is queried.
