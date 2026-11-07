# Registry kinds

Labels each registry as a source (it writes its own skills) or a copier (it collects skills from other repositories). This is stage 3c. It does not use the network. [Registry history](04_registry_history.md) has to have run first. [Registry READMEs](05_registry_readmes.md) gives the second vote; without it, only the date vote is used. Card G38.

A later stage must not take a copier as the origin of a skill. `majiayu000/claude-skill-registry` holds half of all mined skill folders, and it is a crawler mirror.

## Scripts

Run these from the repo root.

```bash
bash scripts/skill_genealogy/dev_mode/06_registry_kinds.sh
```

Labels the 30 registries in `dev_registries.parquet`. The date vote still compares them with every registry. The table goes to `src/data/skill_genealogy_dev/registry_kinds.parquet`.

```bash
bash scripts/skill_genealogy/full_run/06_registry_kinds.sh
```

Labels every registry in `registries.parquet`. It takes a few seconds. The log is `logs/label_registry_kinds.log`.

```bash
bash scripts/skill_genealogy/tests/06_registry_kinds.sh
```

Runs the tests.

## Inputs

- `registries.parquet` (or `dev_registries.parquet` with `--dev`)
- `registry_history/registry_history_changes.parquet` and `registry_history_status.parquet` from stage 3
- `registry_readmes.parquet` from stage 3b. A missing file prints a note, and the README vote is empty.

A run skips when `registry_kinds.parquet` exists. `--force` rebuilds it and applies the labels in the review sheet.

## Steps

**Date vote.** Nobody can copy a text before it exists. For every `SKILL.md` blob in the stage 3 changes (not a delete), the step takes the first time each registry had it. A text is shared when registries of at least two owners have it. Registries of one owner count as one, so a sibling repo is not an upstream. For each shared text, the step asks whether a registry of another owner had it strictly earlier. `upstream_share` is the share of shared texts first seen elsewhere, and `top_upstream` names the three registries that had them first most often.

The vote is `copier` above 0.5 and `source` below. Its strength is `|upstream_share − 0.5| / 0.5`, times `min(1, shared_texts / 20)`, so 20 or more shared texts count fully. A registry that shares no text with another owner is a `source` vote of strength 0.7: nothing in it came from another registry. A share at or below 0.2 is also a `source` vote of at least 0.7. Exactly 0.5 is no vote.

The share counts only shared texts, so a registry that wrote 600 skills and borrowed one shows "1 of 1 first seen elsewhere". The step also counts `own_texts`, all distinct `SKILL.md` texts of the registry, and `copied_share` = texts first seen elsewhere / `own_texts`. When a registry has fewer than 20 shared texts and its `copied_share` is below 0.1, the copier vote becomes a `source` vote of 0.7: it borrowed a few texts and wrote the rest. With 20 or more shared texts the rule does not apply, because a crawler of repos outside the registries also has many texts no registry shares. `majiayu000/claude-skill-registry` has 49,661 shared texts and a `copied_share` of only 0.094, and it must stay a copier.

**README vote.** The README is split into sentences. Only sentences that mention a skill are read. A copier phrase such as "aggregate catalog", "this mirror", "not necessarily the author" or "collected from" is a `copier` vote of 0.8. A source phrase such as "our skills" or "official skills" is a `source` vote of 0.6. A phrase counts only as whole words, so "this mirrors" is not "this mirror", and not within three words after "not", "no" or "never", so "not a mirror of" does not count. Words such as "a curated collection of skills" are not used: original authors use them for their own skills, so they describe packaging, not where the skills came from. "Copied from" is not used either: it described install steps, file trees or one borrowed plugin.

**Combine.** When both votes agree, the confidence is `1 − (1 − date) × (1 − readme)`. One vote gives its strength. When they disagree, the stronger direction wins with confidence `|date − readme|`. No vote is `unclear` with 0. The confidence is rule-based: it ranks registries from doubtful to clear. It is not a measured probability.

**Review sheet.** Registries below 0.5 confidence go to `registry_kind_review.csv`, most skill folders first, with the evidence (`upstream_share`, `shared_texts`, `own_texts`, `copied_share`, `top_upstream`, `readme_phrase`, the first 500 characters of the README) and two empty columns, `your_label` and `note`. A person writes `source`, `copier` or `unclear` in `your_label` for the rows they check, from the top. On the next run with `--force`, those labels replace the computed kind, and `kind_basis` becomes `hand`. A sheet with any label or note is never overwritten.

**Checks.** The run prints counts by kind and basis, the date vote against the README vote, six registries checked by hand (`majiayu000/claude-skill-registry`, `lingxling/awesome-skills-cn` and `aiskillstore/marketplace` are copiers; `anthropics/skills`, `obra/superpowers` and `github/awesome-copilot` are sources), and how many registries with 20 or more shared texts reach a share of 0.7, 0.8 and 0.9.

## Outputs

`registry_kinds.parquet`, one row per registry:

- `repo`, `registry_kind` (`source`, `copier`, `unclear`), `confidence`, `needs_review`, `kind_basis` (`date`, `readme`, `date+readme`, `date_vs_readme`, `none`, `hand`)
- `skill_folders`, `shared_texts`, `elsewhere_first`, `upstream_share`, `own_texts`, `copied_share`, `top_upstream`
- `date_vote`, `date_strength`, `readme_vote`, `readme_strength`, `readme_phrase`, `hand_label`

`registry_kind_review.csv`, one row per registry below 0.5 confidence, most skill folders first.

## Limits

- A history rewrite (squash, force push) makes an original look younger than its copies.
- Only registries are compared. A text whose true origin is not a registry counts as first seen in whichever registry had it earliest.
- The README vote is literal. It misses READMEs that are not in English and copiers that do not say what they are.
- A small registry that crawled repos outside the registries, and shares fewer than 20 texts with registries, looks like a source under the `copied_share` rule.
- The cutoffs and phrase lists are named settings in `settings.py` (`KIND_*`, `README_COPIER_STRONG`, `README_SOURCE`).
