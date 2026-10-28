# s1. Registry: where skills came from

**Code:** `src/skillsig/s1_registry/` · **Tests:** `tests/s1_registry` · **Run:** [EXECUTION_STEPS.md § s1](../../EXECUTION_STEPS.md#s1-registry-stage)

The dataset says where each `SKILL.md` **is**, not where it **came from**. Many skills are copies from a few well-known collections ("registries") such as `anthropics/skills`. This stage reads the full git history of 31 registries and links every version they ever published to the dataset files that are exact copies of it.

**Why it is cheap:** a git blob hash is the dataset's `file_sha`. Git names every file version `sha1("blob <size>\0" + bytes)`, and GitSkills stores the same hash. So matching is a plain equality join, with no text comparison.

---

## Steps

Run in this order, each with `python -m skillsig registry --only <module>`:

| Step | File | Does | Output | Rows |
|---|---|---|---|---|
| 1.1 | [mine_git.md](mine_git.md) | Reads every commit that touched a `SKILL.md` in each registry clone, plus AI co-author trailers | `s1_versions_raw`, `s1_commit_trailers` | 16,485 / 50,941 |
| 1.2 | [versions.md](versions.md) | One row per distinct version of each registry skill | `s1_registry_versions` | 15,254 |
| 1.3 | [match.md](match.md) | Finds dataset files that are exact copies; picks each file's origin registry; summarises each skill's history | `s1_registry_matches`, `s1_lineages` | 6,719 / 2,274 |
| 1.4 | [changes.md](changes.md) | Labels each version-to-version edit `substantive` or `cosmetic` (for RQ2) | `s1_registry_changes` | 12,877 |
| 1.5 | [discover_popular.md](discover_popular.md) | Lists the most copied skills that no registry explains | `results/s1_popular_candidates.csv` | 200 |

Tables go to `src/data/derived/` (not in git); the CSV goes to `results/` (in git). Each has a `.manifest.json` beside it.

```
registry_sources.yaml ─► 1.1 ─► 1.2 ─┬─► 1.3 ─► 1.5
                                     └─► 1.4
```

---

## The registry list

`config/registry_sources.yaml` has one line per registry:

```yaml
- {repo: anthropics/skills, tier: A, group: anthropic, status: mined}
- {repo: flutter/agent-plugins, tier: G, group: google, status: mined, mirror_of: flutter/skills}
```

| Field | Meaning |
|---|---|
| `tier` | **A** provider official · **B** well-known community · **G** Google/Gemini and GitHub/Copilot |
| `group` | Publisher (`anthropic`, `openai`, `community`, `google`, `gemini`, `github`), for breaking results down |
| `status` | Only `mined` entries are used; `candidate` marks one to add later |
| `mirror_of` | The repository republishes another one, so it is not cloned |

32 entries: 31 `mined` (2 tier A, 13 tier B, 16 tier G) and 1 `candidate` (`openai/plugins`). One mined entry is a mirror, so **30 repositories are cloned**, into `src/data/registry_repos/<owner>__<name>/`. A bad entry (unknown field, wrong tier, mirror of an unknown repository) stops the run.

**Why a hand-kept list?** Each entry claims "others copy from this repository". Step 1.5 suggests additions; a person decides.

---

## Who uses the outputs

| Output | Read by | For |
|---|---|---|
| `s1_registry_versions` | 2.7 template_sim | The registry texts each skill is compared with |
| `s1_registry_matches` | 2.7 template_sim, 2.3 repo_context | Exact registry copies; per-repository registry shares |
| `s1_versions_raw`, `s1_commit_trailers` | 4.1 disclosure recall | Every registry commit, to measure what first/last-commit history misses |
| `s1_lineages`, `s1_registry_changes` | Nothing yet | Analysis stage s5 (not built) |
| `s1_popular_candidates.csv` | A person | Choosing registries to add |

No label rule reads an s1 file directly. s1 reaches the labels through 2.7, which gives v2 its two strongest templated reasons (`registry_exact`, `registry_near`).

---

## Full data vs dev sample

Registry history is not part of the dataset, so 1.1, 1.2 and 1.4 are the same in both modes. With `--dev-sample`, 1.3 and 1.5 look only at the 50,000 sampled skills, so they find far fewer matches. Dev and full runs write the **same file names**, so a dev run overwrites the full outputs (see [s0](../s0_intake/README.md#dev-and-full-outputs-share-one-set-of-file-names)).

---

## Tests

```bash
python -m pytest -q tests/s1_registry
```

61 unit tests on small fake repositories; no dataset or network needed. Each step file lists its own. One regression test runs 1.3 on the real data (`python -m pytest -q -m data tests/regression/test_registry_coverage.py`); see [match.md](match.md).

Not covered: no real-data test for 1.1, 1.2, 1.4 or 1.5.
