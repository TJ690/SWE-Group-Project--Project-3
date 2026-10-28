# s6. Report: the charts of the findings

**Code:** `src/skillsig/s6_report/` · **Tests:** `tests/s6_report` · **Run:** [EXECUTION_STEPS.md § s6](../../EXECUTION_STEPS.md#s6-report-stage)

```bash
python -m skillsig export
```

Draws all 17 charts in [docs/report/FINDINGS.md](../../report/FINDINGS.md) from the finished outputs of s0–s4, into `figures/report/`. It measures nothing new.

---

## The charts

The file prefix says where each chart sits in the report: `f0` for the primary evidence view, `f1`–`f5` for the findings, `a` and `b` for the appendices.

| File | Shows | Reads |
|---|---|---|
| `f0_evidence_dimensions.png` | Reuse evidence × disclosed agent involvement, counts and column shares | `results/s3_evidence_matrix.csv` |
| `f1_template_provenance.png` | Copy count vs. cluster rule on known registry copies | `results/s4_template_provenance.csv` |
| `f1_minhash_flow.png` | How MinHash and LSH build clusters | Config (shingle size, 128 numbers, bands) |
| `f1_cluster_sizes.png` | Skills by near-duplicate cluster size | `s2_clusters.parquet` |
| `f1_lsh_s_curve.png` | Chance of a direct match by true similarity | Config (band settings chosen by MinHashLSH) |
| `f1_scaffold_overlap.png` | Scaffold signals: already templated vs. new | `results/s4_scaffold_overlap.csv` |
| `f2_rules_vs_baselines.png` | v2.1 vs. one-signal shortcuts on the spot-check | `results/s4_spot_precision.csv` |
| `f2_disclosure_recall.png` | AI disclosures visible in first/last commits | `results/s4_disclosure_recall.csv` |
| `f3_spot_check.png` | Spot-check agreement per v2.1 label | `results/s4_spot_precision.csv` |
| `f3_label_confidence.png` | Confidence within each label | `results/s3_v2_v21_crosstab.csv` |
| `f4_text_methods_auc.png` | AUC per text-only method | `results/s3_text_methods.csv` |
| `f4_roc_curves.png` | ROC curves of the text-only methods | `s3_text_methods_oof.parquet` |
| `f5_stability.png` | Share changes per threshold setting | `results/s4_stability.csv` |
| `a_label_versions.png` | Label shares for v1, v2, v2.1 | `results/s3_v1_v2_crosstab.csv`, `s3_v2_v21_crosstab.csv` |
| `b_devsample_groups.png` | Dev sample vs. full data by group | `s0_devsample_ids.parquet`, `artifacts` |
| `b_repository_kinds.png` | Repositories and files by repository kind | `s2_repo.parquet` |
| `b_skill_types.png` | Skills by keyword type | `s2_types.parquet` |

The edit-robustness chart in Finding 1 is drawn by step 4.3 (`figures/12_edit_robustness.png`).

## How it works

`figures.py` has three layers:

1. **Data functions** turn stage outputs into what a chart shows: `label_versions`, `label_confidence`, `cluster_bins`, `lsh_match_probability`, `roc_points`, `stratum_label`.
2. **Plot functions** (`plot_*`) take that data and a path and write one PNG. They use the shared look in `style.py`.
3. **`FIGURES`** lists each chart with its inputs and a builder. `run` skips a chart whose inputs are missing (with a warning) or unchanged since its manifest, and draws the rest.

## Design decisions

- **A separate stage, not code inside each step:** the charts present results; they don't produce them. Keeping them apart means changing a chart never reruns a long step, and a step's tests don't depend on plotting.
- **Read finished tables, measure nothing:** every number comes from a file an earlier step wrote, so a chart can't disagree with the results it shows.
- **Titles state the finding with its number, computed from the data:** if a rerun changes a result, the title changes with it.
- **Skip missing inputs instead of failing:** the report can be drawn on a partial pipeline, for example before the human spot-check is scored.
- **Same palette as step 4.3's chart:** all report figures look like one set.

## Limits

- Code changes are not tracked by the manifests: after editing a chart, run with `--force`.
- The dev-sample chart scans the full `artifacts` table to count each group.

## Tests

`test_figures.py` (16): group names; label totals per version; confidence without insufficient evidence; cluster bands and the templated flag; the LSH formula (0.399 at 0.8 with 9 × 13); ROC AUC on toy scores; that the evidence chart keeps reuse and disclosure on separate axes; that each chart writes a PNG; and that `run` draws what it can, skips the rest, writes manifests and skips when up to date.
