# Registry READMEs report

Results of the README download, read from `src/data/skill_genealogy/registry_readmes.parquet` on 2026-11-07. How the step runs is in [registry READMEs](../code_flow/05_registry_readmes.md).

The download covers all **4,806** registries. For each one it fetched only the commit stage 3 mined, without file contents or history, then only the README file. The full run took 555 seconds. A fetch took 0.9 seconds at the median and 6.2 seconds at most (`nwjs/chromium.src`).

## What finished

| Status | Repos | What it means |
|---|---:|---|
| ok | 4,739 | A README was found |
| no_readme | 33 | The commit has no README in `.github/`, the root or `docs/` |
| gone | 34 | The repository was deleted or made private |
| empty, timeout, error | 0 | |

**4,769** READMEs were read from the commit stage 3 mined (`stage3_head`). **3** used the newest commit (`current_head`), because stage 3 has no commit for them: `ClickHouse/ClickHouse` (timeout), `Expensify/App` (error) and `libukai/awesome-agent-skills`.

## Where the READMEs are

| Place | READMEs |
|---|---:|
| repository root | 4,723 |
| `docs/` | 9 |
| `.github/` | 7 |

Almost all are `README.md` (4,713). The others are `README.rst` (9), `README.adoc` (6), `README.org` (4), `README.markdown` (4) and `README` (3).

## Size

The median README is 11,083 bytes. The 90th percentile is 35,167 bytes and the largest kept text is 189,126 bytes. **21** READMEs are larger than 200,000 bytes; they keep their row and path but not their text. 4,718 texts are stored.
