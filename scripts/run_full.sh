#!/usr/bin/env bash
# Full forced rebuild of the skillsig pipeline on the full dataset: every table in results/
# and every report figure. Before annotation it draws the E6 spot-check sheets; once
# spot_A_done.xlsx exists it keeps the sample, refreshes the answer key and scores it instead.
#
#   caffeinate -i bash scripts/run_full.sh          # macOS: keep the Mac awake while it runs
#   DRY_RUN=1 bash scripts/run_full.sh              # print the steps only
#   FETCH=1 bash scripts/run_full.sh                # also re-fetch the 31 registry clones
#
# By default the registry clones are NOT re-fetched: a fetch pulls commits made after
# 2026-10-23 and changes the registry snapshot every label is built on.
# Small machines (<8 GB RAM): SKILLSIG_WORKERS=2 SKILLSIG_DUCKDB_MEMORY=1500MB.
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-$( [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3 )}"
STAMP="$(date +%Y%m%d_%H%M%S)"
mkdir -p logs
LOG="logs/run_full_${STAMP}.log"
DRY_RUN="${DRY_RUN:-0}"
FETCH="${FETCH:-0}"
START=$(date +%s)

say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

step() {  # step <stage> [module] [extra args...]
  local stage="$1" module="${2:-}"; shift || true; shift || true
  local cmd=("$PY" -m skillsig "$stage")
  [ -n "$module" ] && cmd+=(--only "$module")
  cmd+=("$@")
  say "START  ${stage}${module:+ --only $module} $*"
  if [ "$DRY_RUN" = "1" ]; then return 0; fi
  local t0=$(date +%s)
  if ! "${cmd[@]}" >>"$LOG" 2>&1; then
    say "FAILED ${stage}${module:+ --only $module} (see $LOG)"
    exit 1
  fi
  say "DONE   ${stage}${module:+ --only $module} in $(( $(date +%s) - t0 ))s"
}

say "skillsig full forced run; python=$PY; log=$LOG; fetch=$FETCH"

# 0. Data: views, schema check, dev sample.
step data "" --force

# 1. Registry (31 mined repos). mine_git re-fetches GitHub only when FETCH=1.
if [ "$FETCH" = "1" ]; then
  step registry mine_git --force
else
  SKILLSIG_NO_FETCH=1 step registry mine_git --force
fi
step registry versions --force
step registry match --force
step registry changes --force
step registry discover_popular --force

# 2. Features (full population).
step features structure --force
step features commit --force
step features repo_context --force
step features skill_type --force
step features wording --force

# 3. Near-duplicate clusters (about 30 min), then everything that reads them.
step dedup "" --force
step features template_sim --force
if [ "$DRY_RUN" != "1" ]; then
  # Specificity keeps resumable intermediate parts; remove them for a true rebuild.
  rm -rf src/data/derived/s2_specificity_identifiers src/data/derived/s2_specificity_buckets_c*
fi
step features specificity --force
step features scaffold --force

# 4. Labels, baselines, text-only experiments (E4, E5), independent evidence dimensions.
step label rules_v1 --force
step label rules_v2 --force
step label rules_v2_1 --force
step label baselines --force
step label style_model --force
step label text_methods --force
step label evidence --force

# 5. Evaluation E1, E2, E3, E5 and the scaffold check (E6 is scored after annotation).
step evaluate disclosure_recall --force
step evaluate template_provenance --force
step evaluate edit_robustness --force
step evaluate stability --force
step evaluate scaffold_check --force

# 6. Regression checks against known counts (v1 labels, registry union).
say "START  regression tests"
if [ "$DRY_RUN" != "1" ]; then
  if "$PY" -m pytest -q -p no:cacheprovider -m data tests/regression >>"$LOG" 2>&1; then
    say "DONE   regression tests"
  else
    say "WARN   regression tests failed (see $LOG); check before annotating"
  fi
fi

# 7. E6 spot-check. Before annotation: draw fresh sheets (an unannotated spot_A.xlsx is
#    moved aside first). After annotation (spot_A_done.xlsx exists): keep the same sample,
#    refresh the answer key from the new labels and score it, so no answers are lost.
ANN=results/annotation
if [ -f "$ANN/spot_A_done.xlsx" ]; then
  step sample spot_refresh
  step sample spot_check --force
else
  if [ "$DRY_RUN" != "1" ] && [ -f "$ANN/spot_A.xlsx" ]; then
    mkdir -p "$ANN/previous_${STAMP}"
    mv "$ANN"/spot_A.xlsx "$ANN"/spot_B.xlsx "$ANN"/trailer_check.csv "$ANN"/key.parquet \
       "$ANN"/trailer_key.parquet "$ANN"/codebook.md "$ANN/previous_${STAMP}/" 2>/dev/null || true
    say "MOVED  previous sheets to $ANN/previous_${STAMP}/"
  fi
  step sample
fi

# 8. Report figures (figures/report/), drawn from the tables above.
step export "" --force

say "ALL DONE in $(( ($(date +%s) - START) / 60 )) min. Tables: results/. Figures: figures/report/."
