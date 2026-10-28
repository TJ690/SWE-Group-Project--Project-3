# 1.5 Find popular skills outside the registry (`discover_popular.py`)

```bash
python -m skillsig registry --only discover_popular
```

[Back to s1](README.md)

## What it does

Ranks every distinct skill in the dataset by how widely it is copied, and flags the ones no mined registry explains. A person uses the list to decide which registries to add to `config/registry_sources.yaml`.

## How it works

One DuckDB query counts, per `file_sha`, distinct **owners**, then repositories, then files, and keeps the top 200. For each, the **candidate origin** is the holder with the earliest first commit (ties broken by stars, then name). `in_registry` says whether 1.3 already matched it. The Python helper `owner_repos(owner)` lists an owner's repositories, for checking candidates by hand.

## Output

`results/s1_popular_candidates.csv`: 200 rows. Columns: `file_sha, name, files, repos, owners, candidate_repo, candidate_stars, candidate_first_commit_at, in_registry`.

## Design decisions

- **Ranked by distinct owners:** one user copying a skill into 50 of their own repositories is not adoption.
- **A person decides, not the code:** the earliest holder is often an early copier, not the publisher. For example, 62 of the current top 200 are unexplained, and the suggested origin for the most popular of them (`fastapi`, 558 owners) is a 0-star repository.

## Limits

The YAML has a stale comment suggesting `python -m skillsig registry discover`; that command does not exist. Use `--only discover_popular` and the `owner_repos()` helper.

## Tests

`test_discover_popular.py` (11): owner spread outranks one owner's many copies; registry blobs are flagged; tie-breaking order; skip when unchanged; dev mode counts every copy of sampled skills.
