# Running the Pipeline with Agent Skills

This guide shows how to use the project's agent skills as a runbook: instead of typing each `python -m skillsig` command yourself, you ask a coding agent (Claude Code or Codex) to run a step, and the skill tells it how. The skills follow the same steps as [docs/PIPELINE_RUNBOOK.md](docs/PIPELINE_RUNBOOK.md), with the same step ids.

The skills live in `.claude/skills/`. Codex reads the same folders through `.agents/skills/`, which links to them. The rules every skill follows are in [AGENTS.md](AGENTS.md).

## 1. What a skill does for you

When you name a step, the agent:

1. Reads that step's section of the runbook and checks that its input files exist. If one is missing, it names the step that builds it and stops.
2. Asks whether to run on the `dev` sample or the `full` data, if you did not say.
3. Runs the command. Long steps run in the background, with a log in `logs/`.
4. Checks the outputs with `scripts/output_status.py` and reports the row count, mode and build time, plus a short summary of the main result.
5. Links the step's design note in `docs/stages/`.

## 2. How to ask

**Claude Code:** type the slash command with a step and a mode.

```text
/skillsig-features 4b dev
/skillsig-label rules_v2_1 full
/skillsig-dedup full force
```

**Codex:** ask in plain words.

```text
run the commit features on the dev sample
rebuild the v2.1 labels on the full data
```

Each request has three parts:

| Part | Values | Meaning |
|---|---|---|
| Step | A runbook id (`4b`) or a module name (`commit`), or `all` | Which step to run. `all` runs the skill's steps in order and stops at the first failure. |
| Mode | `dev` or `full` | `dev` uses the 50,000-skill sample and is quick. `full` uses all 1,877,981 distinct skills and gives the reported numbers. |
| `force` | optional | Rebuild even if inputs and settings are unchanged. Use it after editing code, because code changes are not tracked. |

## 3. The skills in run order

| Order | Skill | Steps | Does | Example |
|---|---|---|---|---|
| 1 | `/skillsig-prepare` | 0, 1a–1d | Python setup, 13 GB dataset download, the four SQL tables | `/skillsig-prepare 1a` |
| 2 | `/skillsig-intake` | 2 | Schema check and the 50,000-skill dev sample | `/skillsig-intake` |
| 3 | `/skillsig-registry` | 3a–3e | History of the 31 registry collections and matches to the dataset | `/skillsig-registry match full` |
| 4 | `/skillsig-features` | 4a–4e | Structure, commits, repository, skill type, wording | `/skillsig-features 4b dev` |
| 5 | `/skillsig-dedup` | 5a | Near-duplicate clusters (MinHash), about 30 minutes on full data | `/skillsig-dedup full` |
| 6 | `/skillsig-features` | 5b–5d | Registry similarity, specificity, scaffolds (need 5a) | `/skillsig-features 5d full` |
| 7 | `/skillsig-label` | 6a–6h | Label rules v1, v2, v2.1, baselines, text models, evidence view | `/skillsig-label all full` |
| 8 | `/skillsig-evaluate` | 7a–7e | Validation checks against known answers | `/skillsig-evaluate all full` |
| 9 | `/skillsig-spotcheck` | 8–10, 8p, 9p | Blind review sheets and their scoring | `/skillsig-spotcheck 10 full` |
| 10 | `/skillsig-export` | 11 | Report figures in `figures/report/` | `/skillsig-export full` |
| any | `/pipeline` | all | The whole chain, a dry run, or status | `/pipeline status` |

Prepare (step 0, 1a–1d) and intake (step 2) always use the full data, so they take no mode.

## 4. Common tasks

### First time on a new machine

```text
/skillsig-prepare all
/skillsig-intake
/pipeline dev
```

The download is about 13 GB, and the agent confirms before starting it. `/pipeline dev` is a quick smoke run of every stage on the sample.

### Rebuild every table and figure for the report

```text
/pipeline plan
/pipeline full
```

`plan` lists the steps without running them. `full` runs `scripts/run_full.sh`, which takes hours. The agent confirms before starting and runs it in the background. Use `/pipeline status` to see how far it has got.

### After changing a label rule

```text
/skillsig-label rules_v2_1 full force
/skillsig-spotcheck 8r full
/skillsig-spotcheck 10 full
/skillsig-export full
```

Step 8r refreshes the review key for the new labels without drawing a new sample, so existing annotations are kept.

### After rebuilding the near-duplicate clusters

```text
/skillsig-dedup full force
/skillsig-features 5b full force
/skillsig-features 5c full force
/skillsig-features 5d full force
/skillsig-label all full force
```

Steps 5b–5d and every label step read the clusters, so they need `force`.

### Score new hand labels

When a reader saves a completed sheet in `results/annotation/` (for example `spot_B_done.xlsx` or `trailer_check_done.csv`):

```text
/skillsig-spotcheck 10 full
/skillsig-export full
```

Step 10 writes the agreement and precision tables. Filling in the sheets (step 9) is done by people, following [docs/ANNOTATION_GUIDELINES.md](docs/ANNOTATION_GUIDELINES.md). The agent explains the sheets but never fills them in.

### Check what exists

```text
/pipeline status
```

This lists missing outputs, outputs built in `dev` mode, and outputs built from an older commit.

## 5. Things to watch

- **Dev and full write to the same files.** A `dev` run replaces `full` outputs. Run `full` again before reporting numbers, and do not mix modes within a chain.
- **Completed annotations are never touched.** Files named `*_done.*` in `results/annotation/` are never overwritten, moved or deleted. That folder is ignored by git, so back up completed sheets yourself.
- **Answer keys stay private.** The agent does not open or show `key.parquet`, `trailer_key.parquet` or `template_review/key.parquet`, so readers stay blind.
- **The registry snapshot is frozen.** Registry mining runs with `SKILLSIG_NO_FETCH=1` unless you ask for a new snapshot, because fetching changes every label built on it.
- **Low memory.** For `dedup` on a machine with under 8 GB of RAM, ask the agent to set `SKILLSIG_WORKERS=2 SKILLSIG_DUCKDB_MEMORY=1500MB`.
- **Tests.** Ask the agent to run `python -m pytest -q`, or the full-data regression checks with `python -m pytest -q -m data tests/regression` after the label steps.

## 6. Without an agent

Every skill only runs commands you can type yourself. The equivalent manual commands are in the README's [Running the Pipeline](README.md#running-the-pipeline) section and, step by step, in [docs/PIPELINE_RUNBOOK.md](docs/PIPELINE_RUNBOOK.md).
