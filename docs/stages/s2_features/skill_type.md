# 2.4 Skill type (`skill_type.py`)

**Command:** `python -m skillsig features --only skill_type` · **Tests:** `tests/s2_features/test_skill_type.py` · [Back to s2](README.md)

## What it does

Gives each distinct skill one topic from 14 types, and writes a summary of types by repository kind and language.

## How it works

Ordered regular expressions, **first match wins**, over the skill's `name` and `description`. If both are empty, the first 500 body characters (after front matter) are used instead.

| Order | Type | Example patterns |
|---|---|---|
| 1 | `meta` | skill-creator, skill-installer, find-skills, writing-skills, template-skill, skill authoring, creating skills, skill writer |
| 2 | `office_documents` | pdf, docx, xlsx, pptx, spreadsheet, powerpoint |
| 3 | `security` | security, vulnerability, pentest, OWASP, CVE-, XSS, threat model |
| 4 | `testing_debugging` | tests/testing, unit/integration/e2e tests, debug, pytest, jest, TDD (not "stress test", "A/B test") |
| 5 | `review_git` | code-review, review, commit, git, pull request, changelog, worktree |
| 6 | `frontend_design` | frontend, React, Vue, Svelte, Angular, CSS, Tailwind, Figma, UI, UX, design system, UI/web/visual design, landing page (not bare "design") |
| 7 | `planning_agent_workflow` | brainstorm, plan(s), planning, workflow, subagent, orchestrate, multi-agent, agentic, handoff, spec(s), ticket(s), agent session |
| 8 | `devops_cloud` | devops, Kubernetes/k8s, Docker, Terraform, CI/CD, AWS, Azure, GCP, cloud, deploy, infrastructure, Helm |
| 9 | `data_ml_research` | machine learning, ML, LLM, NLP, data science, pandas, Jupyter, research, PyTorch, TensorFlow, embedding, dataset, time series, statistics, analytics, forecast |
| 10 | `backend_api_languages` | API, backend, Python, Java, Go, Rust, TypeScript, JavaScript, Node.js, GraphQL, REST, Postgres, Redis, Django, Flask, FastAPI |
| 11 | `docs_writing_marketing` | documentation, README, copywriting, marketing, blog, technical writing, SEO, newsletter |
| 12 | `business_product` | product, PRD, roadmap, pricing, sales, finance, invoice, startup, go-to-market, customer(s), OKR(s) |
| 13 | `integrations_tools` | MCP, Slack, Notion, Jira, webhook(s), Zapier, integration(s), OAuth, GitHub Actions |
| 14 | `unclassified` | nothing matched |

`type_source` records which text was used (`front_matter`, `body`, `none`); `skill_types_all` keeps every matching type, in rule order, for analysis. `type_version` records the rule set (v1, task T46); the older v0 rules are kept in the code for comparison.

## Outputs

`src/data/derived/s2_types.parquet`: 1,877,981 rows, columns `file_sha, skill_type, skill_types_all, type_source, type_version`.

| Type | Skills | | Type | Skills |
|---|---|---|---|---|
| unclassified | 658,413 (35%) | | security | 81,871 |
| planning_agent_workflow | 224,164 | | devops_cloud | 65,102 |
| review_git | 185,155 | | integrations_tools | 44,672 |
| testing_debugging | 168,829 | | office_documents | 35,018 |
| data_ml_research | 118,932 | | docs_writing_marketing | 34,734 |
| backend_api_languages | 111,585 | | business_product | 32,373 |
| frontend_design | 109,805 | | meta | 7,328 |

The type came from name/description for 1,656,563 skills, from the body for 221,330, and from nothing for 88.

`results/s2_skill_type_by_project.csv`: 4,136 rows, `skill_type, repo_kind, language, files`, counting every file (copies included) per type, repository kind and repository language.

## Design decisions

- **Why keywords instead of a topic model?** Explainable, stable across reruns, and editable by a reviewer. The rules were tuned on the dev sample (v0 → v1): bare `test` and `design` were removed because they over-matched; planning (handoffs, specs, tickets), meta (skill authoring) and data words (time series, statistics, analytics) were widened.
- **Why name and description first?** They are the skill's own summary, and the text an agent reads to pick it. Bodies mention many topics in passing.
- **Why does order matter?** Specific types (`meta`: skills about writing skills; `office_documents`) are checked first and broad ones last. A skill-creator that mentions PDFs is still `meta`.
- **Why is it context only?** Type describes *what* a skill is about, not *who* wrote it. Keeping it out of the label avoids a label that is really a topic detector.

## Full data vs dev sample

Dev mode types only the sampled skills; the summary CSV still counts every copy of those skills. Each skill's type is the same as in a full run.

## Tests (20)

- Named cases: skill-creator → meta; a PDF toolkit → office; empty name and description → unclassified; a security review of tests → security; "test" beats a later review rule; meta beats a later office hit; Matt Pocock's skills (parametrised).
- Negatives: "stress test" is not testing but "unit tests" is; bare "design" is not frontend; "create a PDF" is not meta.
- Body fallback used only when front matter is empty, reading only the first 500 characters; all-matches list in rule order; v0 rules kept; rules follow the type order.
- A run counts files and skips when unchanged; dev keeps sampled skills and their copies; missing inputs name their commands.

## Limits

- 35% of skills match no rule.
- English keywords only.
- One type per skill; `skill_types_all` shows the overlap, but most analyses use the first.
