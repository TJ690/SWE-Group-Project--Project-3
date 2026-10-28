# 3.1 Labels v1 (`rules_v1.py`)

```bash
python -m skillsig label --only rules_v1
```

[Back to s3](README.md)

## What it does

Applies the Sprint 1 rules exactly as they were. The first rule that matches wins:

| Order | Label | Rule |
|---|---|---|
| 1 | `highly_templated` | 10 or more files in the dataset have the same text (`copies ≥ 10`) |
| 2 | `agent_assisted` | Has history, batch < 6, first author not a non-coding bot, **and** (a coding-agent bot made the first commit **or** the first commit has an AI trailer) |
| 3 | `likely_human` | Has history, batch = 1, first author is a user, no AI trailer on first or last commit, `copies = 1` |
| 4 | `insufficient_evidence` | Anything else, with reasons saying what was missing: `no_history`, `bulk_batch>=6`, `noncoding_bot`, `no_agent_signal`, … |

`copies` counts every file with the same `file_sha`. Batch size and the bot and trailer flags come from step 2.2.

## Output

`s3_labels_v1.parquet`: one row per distinct skill. Columns: `file_sha, label, copies, reasons`.

## Design decisions

- **Templated first:** if 10 files share the text, a trailer on *this* copy's commit only says who committed the copy, not who wrote the text.
- **Agent only for small batches:** a trailer on a commit that adds 40 skills records a simultaneous addition (often an install or import), not evidence that the agent wrote 40 skills.
- **Human is strict:** git has no positive "a person wrote this" marker, so every available signal must point the same way.
- **An exact port:** reproducing the Sprint 1 numbers proves the new pipeline is correct before any rule changes, and gives v2 a fixed reference. That's also why v1 has no confidence.

## Tests

- `test_rules_v1.py` (12): one fixture row per label; templated beats agent; bulk batch blocks a trailer; each human blocker; thresholds come from config; dev mode keeps sampled ids.
- Regression test on the real data (`-m data`): exactly 35,768 templated, 80,114 agent-assisted, 62,798 likely-human and 1,699,301 insufficient.
