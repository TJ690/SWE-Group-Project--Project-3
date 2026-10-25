# Execution steps

Commands recorded here were run from the repo root and passed. Run later stages only after the earlier task in the same phase.

## Phase 0 — Scaffold and data

## T00 — Scaffold the package, config, CLI and Makefile

```bash
pip install -e . && python -m skillsig --help && pytest -q tests/test_cli.py tests/test_config.py tests/test_provenance.py
```

## T01 — Data loader, schema check and dev sample

```bash
pytest -q tests/s0_data && python -m skillsig data --dev-sample
```

### Outputs

src/data/derived/s0_devsample_ids.parquet

src/data/derived/s0_devsample_ids.parquet.manifest.json

## T02 — Structure features as pure functions, parity with the SQL build

```bash
pytest -q tests/s2_features/test_structure.py
```

```bash
python -m skillsig features --dev-sample
```

### Outputs

src/data/derived/s2_structure.parquet

src/data/derived/s2_structure.parquet.manifest.json

## T03 — Commit-history features: AI trailers, bot classes, batches, message classes

```bash
pytest -q tests/s2_features/test_commit.py
```

```bash
python -m skillsig features --only commit --dev-sample
```

### Outputs

src/data/derived/s2_commit.parquet

src/data/derived/s2_commit.parquet.manifest.json

## T04 — Repository-context features and project kind

```bash
pytest -q tests/s2_features/test_repo_context.py
```

```bash
python -m skillsig features --only repo_context --dev-sample
```

### Outputs

src/data/derived/s2_repo.parquet

src/data/derived/s2_repo.parquet.manifest.json

## T05 — Label rules v1 (port of Sprint 1) with exact regression

```bash
pytest -q tests/s3_labeling/test_rules_v1.py && pytest -q -m data tests/regression/test_rules_v1_counts.py
```

```bash
python -m skillsig label
```

### Outputs

src/data/derived/s3_labels_v1.parquet

src/data/derived/s3_labels_v1.parquet.manifest.json

## Phase 1 — Registry (versions, matching, changes)

## T06 — Registry sources loader

```bash
pytest -q tests/s1_registry/test_sources.py
```

### Outputs

No derived file. `load_sources` returns an in-memory list.

## T07 — GitHub client with cache and rate-limit handling

```bash
pytest -q tests/s1_registry/test_github_client.py
```

### Outputs

No derived file. The client writes its ETag cache at `src/data/cache/github.sqlite` when it runs. The acceptance tests use a temporary cache.

## T08 — Mine git history: SKILL.md versions and commit trailers

```bash
pytest -q tests/s1_registry/test_mine_git.py && python -m skillsig registry --only mine --dev-sample
```

### Outputs

src/data/derived/s1_versions_raw.parquet

src/data/derived/s1_versions_raw.parquet.manifest.json

src/data/derived/s1_commit_trailers.parquet

src/data/derived/s1_commit_trailers.parquet.manifest.json

## T09 — Registry versions: distinct blobs, sequence, idioms, snapshot flag

```bash
pytest -q tests/s1_registry/test_versions.py
```

```bash
python -m skillsig registry --only versions --dev-sample
```

### Outputs

src/data/derived/s1_registry_versions.parquet

src/data/derived/s1_registry_versions.parquet.manifest.json

## T10 — Match registry to the dataset: provenance, mirrors, lineages

```bash
pytest -q tests/s1_registry/test_match.py && pytest -q -m data tests/regression/test_registry_coverage.py
```

```bash
python -m skillsig registry --only match
```

### Outputs

src/data/derived/s1_registry_matches.parquet

src/data/derived/s1_registry_matches.parquet.manifest.json

src/data/derived/s1_lineages.parquet

src/data/derived/s1_lineages.parquet.manifest.json

## T11 — Version-to-version changes and the substantive/cosmetic + preserve/remove/introduce classifier

```bash
pytest -q tests/s1_registry/test_change_classifier.py
```

```bash
python -m skillsig registry --only changes --dev-sample
```

### Outputs

src/data/derived/s1_registry_changes.parquet

src/data/derived/s1_registry_changes.parquet.manifest.json

## T12 — Discover popular skill families and candidate origins

```bash
pytest -q tests/s1_registry/test_discover_popular.py
```

```bash
python -m skillsig registry --only discover_popular
```

### Outputs

results/s1_popular_candidates.csv

results/s1_popular_candidates.csv.manifest.json

## Phase 2 — Features and similarity

## T13 — Wording features W1-W10

```bash
pytest -q tests/s2_features/test_wording.py && python -m skillsig features --only wording --dev-sample
```

### Outputs

src/data/derived/s2_wording.parquet

src/data/derived/s2_wording.parquet.manifest.json
