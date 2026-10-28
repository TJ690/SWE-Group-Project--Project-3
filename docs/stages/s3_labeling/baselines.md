# 3.4 Baselines (`baselines.py`)

```bash
python -m skillsig label --only baselines
```

[Back to s3](README.md)

## What it does

Builds five simple labels, each from a single signal, so the human spot-check (4.8) can show whether the full rule set does better than the obvious shortcuts.

| Baseline | Rule | Labels on the full data |
|---|---|---|
| `B0_majority` | Everything gets the most common v2 label | All insufficient |
| `B1_copy_count` | 10+ copies → templated | 35,768 templated |
| `B2_agent_folder` | File in a tool-specific folder (`.claude`, `.github`, `.cursor`, `.codex`, `.gemini`, `.kiro`, `.opencode`, `.antigravity`) → agent-assisted | 458,305 agent (24%) |
| `B3_trailer` | AI trailer on the first commit → agent-assisted | 138,462 agent |
| `B3b_trailer_no_bulk` | B3, but only for batches under 6 | 80,491 agent |

Everything a baseline doesn't label is `insufficient_evidence`.

The module also provides the **grouped cross-validation split** used by 3.5 and 3.6: skills that share an owner or a near-duplicate cluster, directly or through a chain, always land in the same fold.

## Outputs

- `s3_baseline_labels.parquet`: one row per distinct skill, one column per baseline.
- `results/s3_baseline_summary.csv`: label counts per baseline.

## Design decisions

- **Why baselines?** A complex rule set is only worth it if it beats one-line rules on the same annotated skills.
- **Why is the agent folder a baseline, not a rule?** It shows where a skill is installed, not who wrote it. It labels 24% of skills agent-assisted, far more than any disclosure, which is the point of measuring it.
- **Why group folds by owner and cluster?** One owner's skills, or near-copies of one text, share wording. Splitting them across training and test lets a model memorise them and look better than it is. Grouping reduces this through the owners and near-copies it can see; it does not rule out every leak.
- Agreement between a baseline and the rules is not evidence either is right; only the annotated sample can say that.

## Tests

`test_baselines.py` (10): each baseline's rule; B2 ignores shared and missing folders; B3 and B3b differ on bulk; owners sharing a cluster are grouped; the split never puts an owner or cluster on both sides.
