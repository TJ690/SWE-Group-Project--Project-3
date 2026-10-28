# 3.3 Labels v2.1 (`rules_v2_1.py`): the current label

```bash
python -m skillsig label --only rules_v2_1
```

[Back to s3](README.md)

## What it does

Starts from v2 and applies two corrections and one penalty, then adds flags that keep each kind of evidence visible on its own. v1 and v2 are not changed.

## How it works

Two terms first:

- **Disclosed:** the skill has history, and an AI trailer on the first or last commit (original or extended detector) or a coding-agent bot as author.
- **Revised:** the skill has 3 or more commits over 7 or more days (`human_min_commits`, `human_min_span_days`).

| Change | Rule | Effect on the full data |
|---|---|---|
| **O1: disclosed agent left as insufficient** | v2 said `insufficient_evidence`, the skill is disclosed, and the author is not a non-coding bot → `agent_assisted`, **low** confidence. The first reason says why v2 missed it: `trailer_in_bulk`, `trailer_last_only`, `trailer_ext_only` or `trailer_other` | 64,602 skills become agent-assisted |
| **O2: human needs revision** | `likely_human` is **medium** only when revised; otherwise **low** | 50,632 of 60,930 human labels are low |
| **Conflict** | Templated **and** disclosed → label stays templated, confidence drops one level, reason `conflict:templated+disclosed` | |

**Flags** (independent of the label): `flag_templated`, `flag_disclosed_agent`, `revised`, `conflict`, and `human_signal_count` (0–5: revised, batch of 1, one copy, user author, history without disclosure).

**Filters** (flags only; nothing is dropped): `filter_short` (body under 200 characters), `filter_case_variant` (file not named exactly `SKILL.md`), `filter_pre_format` (first commit before 2025-10-01, when the format launched).

## Outputs

- `s3_labels_v2_1.parquet`: one row per distinct skill. Columns: `file_sha, label, templated_basis, confidence, reasons, label_v2, label_v1`, the flags and the filters.
- `results/s3_v2_v21_crosstab.csv`: skills by v2 label, v2.1 label and v2.1 confidence.

## Design decisions

- **O1:** v2 left every disclosed agent inside a bulk commit as insufficient. A trailer is still a disclosure; the safest reading is "agent involved, weak evidence", with the reason code showing why.
- **O2:** v1's human rule mostly means "nothing suspicious in one commit". Repeated edits over a week are positive evidence that a person maintained the text.
- **Flag conflicts, don't choose:** an agent installing a registry skill is both; the label keeps the text evidence and the flag keeps the commit evidence.
- **Flags and filters, not exclusions:** later analyses can ask "all disclosed skills, whatever the label", or filter differently, without rerunning the rules.

## Tests

`test_rules_v2_1.py` (17): templated with a trailer stays templated and conflicts; bulk, last-only and extended-only trailers become low-confidence agent; a non-coding bot's trailer is ignored; human medium only when revised (one commit, or many commits within days, stay low); filters; reasons never empty; thresholds from config.
