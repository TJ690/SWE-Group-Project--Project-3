# 3.2 Labels v2 (`rules_v2.py`)

```bash
python -m skillsig label --only rules_v2
```

[Back to s3](README.md)

## What it does

Widens the templated rule with registry and cluster evidence, and gives every label a **confidence**. Skills that are not templated keep their v1 label.

## How it works

**Templated reasons**, checked in this order; the first that applies becomes `templated_basis`:

| Basis | Applies when |
|---|---|
| `registry_exact` | The skill is an exact copy of a registry version (step 2.7) |
| `registry_near` | A near copy (similarity ≥ 0.8) of a registry version |
| `cluster` | Its near-duplicate cluster at 0.8 has 10 or more skills (step 2.6) |
| `copy_count` | 10 or more files share its text (the v1 rule) |

If the skill sits in the same repository as the registry skill it matches, it is the registry's own file (`registry_origin`). The two **registry** reasons are then skipped, but `cluster` and `copy_count` can still apply.

**Confidence:**

| Label | High | Medium | Low |
|---|---|---|---|
| `highly_templated` | `registry_exact`, or 2+ reasons | One reason, clearly past its threshold | One reason, near its threshold (similarity 0.7–0.9; cluster size or copies 8–12) |
| `agent_assisted` | Coding-agent bot **and** first-commit trailer | One of the two | Batch size 4–8, or no usable history |
| `likely_human` | — | Has history | No usable history |
| `insufficient_evidence` | — | — | Always |

## Outputs

- `s3_labels_v2.parquet`: one row per distinct skill. Columns: `file_sha, label, templated_basis, confidence, reasons, v1_label`.
- `results/s3_v1_v2_crosstab.csv`: how many skills moved from each v1 label to each v2 label.

## Design decisions

- **Why add registry and cluster reasons?** v1 sees only byte-identical copies. Lightly edited copies, or families of near-identical variants, had `copies = 1` each and were left insufficient or even "human". v2 moved 263,936 v1-insufficient skills, 1,931 v1-human and 1,882 v1-agent skills to templated.
- **Why this order?** Strongest evidence first: a known source, then a known source with small edits, then shared content of unknown source, then the bluntest count.
- **Why lower confidence near a threshold?** A skill with 11 copies or similarity 0.81 would flip under a slightly different setting; low confidence marks these fragile labels. The bands are fixed in code, so changing a threshold in step 4.4 does not also move the band.

## Limits

- The step requires and fingerprints `s2_repo`, `s2_structure`, `s2_wording` and `s2_types` but does not read them, so rebuilding any of those reruns v2 for nothing.
- Lowering the similarity threshold below 0.8 has no effect unless 2.7 is rerun, because 2.7 decides `near` itself (see [2.7](../s2_features/template_sim.md)).

## Tests

`test_rules_v2.py` (16): exact registry copy is high confidence; bulk batch is not agent; registry origin is not templated by the registry reasons; confidence bands; wording and structure don't change the label; reasons never empty; deterministic; cluster reason uses the 0.8 threshold.
