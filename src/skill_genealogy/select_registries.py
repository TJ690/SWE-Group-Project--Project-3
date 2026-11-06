"""Choose registry repos and give every dataset repo a role.

What: stage 2. Step 1 labels the language of every distinct skill text.
Steps 2–6 keep repos with enough stars and English skills, test which of
those texts were first seen in an older repo, assign a role, and draw the
30-registry dev sample.
Inputs: GitSkills ``artifacts`` and ``repos`` parquet, plus
``skill_language.parquet`` once step 1 has written it.
Outputs: all under ``src/data/skill_genealogy/`` (full data folder).
``--dev`` does not change the folder. The registry list does not depend on
dev mode; later stages filter with ``dev_registries.parquet``.
How to run: ``python -m skill_genealogy.select_registries [--force]``.
``--force`` rebuilds steps 2–6 and keeps the language file.
``--force-languages`` rebuilds the language file, then steps 2–6.
Score a filled check sample with ``--score-language-check``.
"""

from __future__ import annotations

import gc
import multiprocessing
import random
import time
from collections import deque
from collections.abc import Iterator
from itertools import chain
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import skill_genealogy.settings as settings
from skill_genealogy.file_helpers import (
    build_arg_parser,
    check_inputs_exist,
    connect_duckdb,
    outputs_exist,
    print_sample_records,
    print_summary,
    write_parquet,
)
from skill_genealogy.text_helpers import (
    build_language_detector,
    classify_language_sample,
    language_sample_and_size,
    normalize_text,
    split_front_matter,
)

# DuckDB hands rows to Python in vectors of this size. fetch_df_chunk counts vectors.
DUCKDB_VECTOR_ROWS = 2048
# Q2.1 reads this many hash slices. hash(file_sha) % TEXT_READ_PARTS is
# 0 .. TEXT_READ_PARTS-1. Eight slices keep one group-by to about an eighth
# of the distinct texts, under LANGUAGE_DUCKDB_MEMORY_LIMIT.
TEXT_READ_PARTS = 8
# Rows of full skill text held in this process at once. Workers receive the
# short language sample, not these rows.
LABEL_CHUNK_ROWS = 10_000
# Revision 2: 40 texts from each of en, unknown and non-English.
LANGUAGE_CHECK_PER_GROUP = 40
# First characters of the body stored in the hand-check CSV, on one line.
BODY_START_CHARS = 300
# The three groups in language_check_sample.csv, in file order.
LANGUAGE_CHECK_GROUPS: tuple[str, ...] = ("en", "unknown", "non_english")
# Dev registries are split into these size groups, smallest skills first.
SIZE_GROUPS: tuple[str, ...] = ("small", "medium", "large")

_LABEL_COLUMNS: tuple[str, ...] = ("file_sha", "language", "is_english", "body_chars")
_CHECK_COLUMNS: tuple[str, ...] = (
    "file_sha",
    "language",
    "is_english",
    "body_chars",
    "body_start",
    "human_language",
    "human_is_english",
)
_LABEL_SCHEMA = pa.schema(
    [
        ("file_sha", pa.string()),
        ("language", pa.string()),
        ("is_english", pa.bool_()),
        ("body_chars", pa.int64()),
    ]
)
_TRUE_WORDS = {"true", "t", "yes", "y", "1"}
_FALSE_WORDS = {"false", "f", "no", "n", "0"}
_BLANK_WORDS = {"", "nan", "none", "<na>"}

DATASET_BUILDER = "python src/data/download_data.py"

# Query Q2.1, see QUERIES.md
REGISTER_ARTIFACTS_SQL = "CREATE OR REPLACE VIEW artifacts AS SELECT * FROM read_parquet({path})"

# Query Q2.1, see QUERIES.md
DISTINCT_TEXT_COUNT_SQL = """
SELECT count(*) AS texts
FROM (
    SELECT file_sha
    FROM artifacts
    WHERE file_sha IS NOT NULL
      AND content IS NOT NULL
    GROUP BY file_sha
)
"""

# Query Q2.1, see QUERIES.md
DISTINCT_TEXTS_SQL = """
SELECT file_sha, any_value(content) AS content
FROM artifacts
WHERE file_sha IS NOT NULL
  AND content IS NOT NULL
  AND hash(file_sha) % {parts} = {part}
GROUP BY file_sha
"""

# Query Q2.1, see QUERIES.md
SORT_LABELS_SQL = """
COPY (
    SELECT file_sha, language, is_english, body_chars
    FROM read_parquet({source})
    ORDER BY file_sha
) TO {dest} (FORMAT PARQUET, COMPRESSION ZSTD)
"""

# Query Q2.2, see QUERIES.md
LANGUAGE_SIDE_SQL = """
WITH labels AS (
    SELECT file_sha, is_english
    FROM read_parquet({labels})
)
SELECT
    labels.is_english AS is_english,
    count(DISTINCT labels.file_sha) AS texts,
    count(*) AS files,
    count(DISTINCT artifacts.repo_full_name) AS repos
FROM labels
JOIN artifacts ON artifacts.file_sha = labels.file_sha
GROUP BY labels.is_english
ORDER BY labels.is_english DESC
"""

# Query Q2.2, see QUERIES.md
LANGUAGE_COUNTS_SQL = """
SELECT
    language,
    count(*) AS texts,
    median(body_chars) AS median_body_chars
FROM read_parquet({labels})
GROUP BY language
ORDER BY texts DESC, language
"""

# Query Q2.3, see QUERIES.md
LANGUAGE_CHECK_SAMPLE_SQL = """
WITH labels AS (
    SELECT
        file_sha,
        language,
        CAST(is_english AS BOOLEAN) AS is_english,
        body_chars
    FROM read_parquet({labels})
),
en_rows AS (
    SELECT file_sha, language, is_english, body_chars, 'en' AS sample_group
    FROM labels
    WHERE language = 'en'
    ORDER BY hash(file_sha || '{seed}')
    LIMIT {per_group}
),
unknown_rows AS (
    SELECT file_sha, language, is_english, body_chars, 'unknown' AS sample_group
    FROM labels
    WHERE language = 'unknown'
    ORDER BY hash(file_sha || '{seed}')
    LIMIT {per_group}
),
other_ranked AS (
    SELECT
        file_sha,
        language,
        is_english,
        body_chars,
        row_number() OVER (
            PARTITION BY language
            ORDER BY hash(file_sha || '{seed}')
        ) AS lang_rank
    FROM labels
    WHERE language NOT IN ('en', 'unknown')
),
other_rows AS (
    SELECT
        file_sha,
        language,
        is_english,
        body_chars,
        'non_english' AS sample_group
    FROM other_ranked
    ORDER BY lang_rank, hash(file_sha || '{seed}')
    LIMIT {per_group}
),
picked AS (
    SELECT * FROM en_rows
    UNION ALL
    SELECT * FROM unknown_rows
    UNION ALL
    SELECT * FROM other_rows
),
joined AS (
    SELECT
        picked.file_sha AS file_sha,
        any_value(picked.language) AS language,
        any_value(picked.is_english) AS is_english,
        any_value(picked.body_chars) AS body_chars,
        any_value(picked.sample_group) AS sample_group,
        any_value(artifacts.content) AS content
    FROM picked
    JOIN artifacts ON artifacts.file_sha = picked.file_sha
    WHERE artifacts.content IS NOT NULL
    GROUP BY picked.file_sha
)
SELECT file_sha, language, is_english, body_chars, sample_group, content
FROM joined
ORDER BY
    CASE sample_group
        WHEN 'en' THEN 0
        WHEN 'unknown' THEN 1
        ELSE 2
    END,
    file_sha
"""

# Query Q2.4, see QUERIES.md
REGISTER_REPOS_SQL = "CREATE OR REPLACE VIEW repos AS SELECT * FROM read_parquet({path})"

# Query Q2.4, see QUERIES.md
REGISTER_LANGUAGE_SQL = (
    "CREATE OR REPLACE VIEW skill_language AS SELECT * FROM read_parquet({path})"
)

# Query Q2.4, see QUERIES.md
COUNT_ENGLISH_SKILLS_PER_REPO_SQL = """
WITH english AS (
    SELECT file_sha
    FROM skill_language
    WHERE is_english
),
per_repo AS (
    SELECT
        artifacts.repo_full_name AS repo,
        count(DISTINCT artifacts.file_sha) AS english_skills,
        count(*) AS files
    FROM artifacts
    JOIN english ON artifacts.file_sha = english.file_sha
    WHERE artifacts.file_sha IS NOT NULL
    GROUP BY artifacts.repo_full_name
)
SELECT
    repos.full_name AS repo,
    repos.owner AS owner,
    repos.stars AS stars,
    repos.is_fork AS is_fork,
    coalesce(per_repo.english_skills, 0)::BIGINT AS english_skills,
    coalesce(per_repo.files, 0)::BIGINT AS files
FROM repos
LEFT JOIN per_repo ON per_repo.repo = repos.full_name
ORDER BY repo
"""

# Query Q2.5, see QUERIES.md. Algorithm A18, see CODE_FLOW.md.
HUB_SHARES_SQL = """
WITH english AS (
    SELECT file_sha
    FROM skill_language
    WHERE is_english
),
skill_counts AS (
    SELECT
        artifacts.repo_full_name AS repo,
        count(DISTINCT artifacts.file_sha) AS english_skills
    FROM artifacts
    JOIN english ON artifacts.file_sha = english.file_sha
    WHERE artifacts.file_sha IS NOT NULL
    GROUP BY artifacts.repo_full_name
),
dated AS (
    SELECT
        artifacts.repo_full_name AS repo,
        artifacts.file_sha AS file_sha,
        min(TRY_CAST(artifacts.first_commit_at AS TIMESTAMP)) AS own_first
    FROM artifacts
    JOIN english ON artifacts.file_sha = english.file_sha
    WHERE artifacts.file_sha IS NOT NULL
      AND TRY_CAST(artifacts.first_commit_at AS TIMESTAMP) IS NOT NULL
    GROUP BY artifacts.repo_full_name, artifacts.file_sha
),
earliest AS (
    SELECT file_sha, min(own_first) AS earliest_at
    FROM dated
    GROUP BY file_sha
),
earliest_repo AS (
    SELECT
        dated.file_sha AS file_sha,
        min(dated.repo) AS earliest_repo,
        any_value(earliest.earliest_at) AS earliest_at
    FROM dated
    JOIN earliest
      ON dated.file_sha = earliest.file_sha
     AND dated.own_first = earliest.earliest_at
    GROUP BY dated.file_sha
),
marked AS (
    SELECT
        dated.repo AS repo,
        dated.file_sha AS file_sha,
        CASE
            WHEN earliest_repo.earliest_repo <> dated.repo
             AND earliest_repo.earliest_at < dated.own_first
            THEN 1
            ELSE 0
        END AS seen_elsewhere
    FROM dated
    JOIN earliest_repo ON dated.file_sha = earliest_repo.file_sha
),
shares AS (
    SELECT
        repo,
        count(*)::BIGINT AS skills_tested,
        sum(seen_elsewhere)::BIGINT AS first_seen_elsewhere,
        sum(seen_elsewhere) * 1.0 / count(*) AS hub_share
    FROM marked
    GROUP BY repo
)
SELECT
    shares.repo AS repo,
    shares.skills_tested AS skills_tested,
    shares.first_seen_elsewhere AS first_seen_elsewhere,
    shares.hub_share AS hub_share
FROM shares
{joins}
ORDER BY shares.repo
"""

_SKILL_COUNT_COLUMNS: tuple[str, ...] = (
    "repo",
    "owner",
    "stars",
    "is_fork",
    "english_skills",
    "files",
)
_HUB_COLUMNS: tuple[str, ...] = (
    "repo",
    "skills_tested",
    "first_seen_elsewhere",
    "hub_share",
)
_REGISTRY_COLUMNS: tuple[str, ...] = (
    "repo",
    "owner",
    "stars",
    "english_skills",
    "files",
    "hub_share",
    "is_hub",
    "hub_tested",
)
_ROLE_COLUMNS: tuple[str, ...] = (
    "repo",
    "owner",
    "stars",
    "is_fork",
    "english_skills",
    "is_registry",
    "is_hub",
    "hub_tested",
    "hub_share",
    "repo_role",
)
_DEV_COLUMNS: tuple[str, ...] = ("repo", "english_skills", "is_hub", "size_group")
_ROLE_NAMES: tuple[str, ...] = ("registry", "registry_hub", "hub", "repository")


def _quote_path(path: str) -> str:
    """Return a single-quoted SQL string with quotes escaped."""
    return "'" + path.replace("'", "''") + "'"


def _language_table_path() -> Path:
    """Return ``skill_language.parquet`` in the full data folder.

    Step 1 ignores ``--dev``. Dev and full runs share this file.
    """
    return settings.data_dir(False) / "skill_language.parquet"


def _language_check_path() -> Path:
    """Return ``language_check_sample.csv`` in the full data folder."""
    return settings.data_dir(False) / "language_check_sample.csv"


def _register_artifacts(con: duckdb.DuckDBPyConnection) -> None:
    """Point the ``artifacts`` view at ``ARTIFACTS_GLOB``."""
    sql = REGISTER_ARTIFACTS_SQL.format(path=_quote_path(settings.ARTIFACTS_GLOB))
    con.execute(sql)


def _register_repos(con: duckdb.DuckDBPyConnection) -> None:
    """Point the ``repos`` view at ``REPOS_GLOB``."""
    sql = REGISTER_REPOS_SQL.format(path=_quote_path(settings.REPOS_GLOB))
    con.execute(sql)


def _register_language(con: duckdb.DuckDBPyConnection) -> None:
    """Point the ``skill_language`` view at the step 1 parquet.

    Raises ``FileNotFoundError`` when the file is missing. The message names
    ``--force-languages``.
    """
    path = _language_table_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing skill_language.parquet: {path} "
            "(build with: python -m skill_genealogy.select_registries --force-languages)"
        )
    sql = REGISTER_LANGUAGE_SQL.format(path=_quote_path(str(path)))
    con.execute(sql)


def read_inputs(con: duckdb.DuckDBPyConnection) -> None:
    """Register the artifacts and repos parquet files.

    A missing glob raises ``FileNotFoundError`` and names the download command.
    ``skill_language`` is registered later, after step 1 has written it.
    Steps 2–6 read that view.

    Inputs: an open DuckDB connection. The paths come from ``settings``.
    Outputs: views named ``artifacts`` and ``repos``.
    """
    check_inputs_exist(
        {"artifacts": settings.ARTIFACTS_GLOB, "repos": settings.REPOS_GLOB},
        {"artifacts": DATASET_BUILDER, "repos": DATASET_BUILDER},
    )
    _register_artifacts(con)
    _register_repos(con)


def _quiet_duckdb(con: duckdb.DuckDBPyConnection) -> None:
    """Turn off the progress bar and let the group-by drop input order.

    The label file is sorted later by ``file_sha``. Keeping input order makes
    the distinct-text group-by hold more memory.
    """
    con.execute("SET enable_progress_bar = false")
    con.execute("SET preserve_insertion_order = false")


def _cap_text_scan_memory(con: duckdb.DuckDBPyConnection) -> None:
    """Bound the distinct-text group-by so it cannot take the whole machine.

    One slice needs more than 2GB, and this group-by stops when the cap is
    too low. The cap is one slice, not the whole table. Fewer threads keep
    the hash table from growing once per core. Spill files, if DuckDB uses
    them, go in ``duckdb_temp`` next to the language parquet. Other queries
    on this stage keep DuckDB's own limit.
    """
    temp = settings.data_dir(False) / "duckdb_temp"
    temp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET memory_limit = '{settings.LANGUAGE_DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET threads = {int(settings.LANGUAGE_DUCKDB_THREADS)}")
    con.execute(f"SET max_temp_directory_size = '{settings.DUCKDB_TEMP_LIMIT}'")
    con.execute("SET temp_directory = " + _quote_path(str(temp)))


def _vectors_per_chunk(chunk_rows: int) -> int:
    """How many DuckDB vectors cover about ``chunk_rows`` rows.

    One vector is 2,048 rows. 50,000 rows is 25 vectors (51,200 rows).
    The caller then slices that fetch into pieces of ``chunk_rows``.

    Example:
    >>> _vectors_per_chunk(50_000)
    25
    >>> _vectors_per_chunk(1)
    1
    """
    if chunk_rows < 1:
        raise ValueError("chunk_rows must be at least 1")
    return max(1, (chunk_rows + DUCKDB_VECTOR_ROWS - 1) // DUCKDB_VECTOR_ROWS)


def read_distinct_texts_in_chunks(
    con: duckdb.DuckDBPyConnection,
    chunk_rows: int = LABEL_CHUNK_ROWS,
) -> Iterator[pd.DataFrame]:
    """Yield distinct ``file_sha`` values and one non-null ``content`` each.

    Query Q2.1. ``any_value(content)`` keeps a single text per sha.
    The scan is split into ``TEXT_READ_PARTS`` hash slices so one group-by
    does not hold every string. DuckDB ``fetch_df_chunk`` returns whole
    vectors. A large result comes back in vectors of about 2,048 rows. A
    tiny file can come back one row at a time. Rows are packed until a
    frame has ``chunk_rows`` rows, including across slices, so the texts
    are never collected into one DataFrame.

    Inputs: a connection whose ``artifacts`` view is already registered,
    and ``chunk_rows`` (default 10,000).
    Outputs: frames with columns ``file_sha`` and ``content``. An empty
    table yields nothing.

    Example:
    >>> con = connect_duckdb()
    >>> _ = con.execute("CREATE TABLE artifacts AS SELECT 'a' AS file_sha, 'hi' AS content")
    >>> frames = list(read_distinct_texts_in_chunks(con, chunk_rows=10))
    reading text part 1 of 8
    reading text part 2 of 8
    reading text part 3 of 8
    reading text part 4 of 8
    reading text part 5 of 8
    reading text part 6 of 8
    reading text part 7 of 8
    reading text part 8 of 8
    >>> list(frames[0]["file_sha"])
    ['a']
    """
    _quiet_duckdb(con)
    _cap_text_scan_memory(con)
    pending: list[pd.DataFrame] = []
    pending_rows = 0
    for part in range(TEXT_READ_PARTS):
        print(f"reading text part {part + 1} of {TEXT_READ_PARTS}", flush=True)
        sql = DISTINCT_TEXTS_SQL.format(parts=TEXT_READ_PARTS, part=part)
        result = con.execute(sql)
        pending, pending_rows = yield from _stream_text_part(
            result, chunk_rows, pending, pending_rows
        )
        gc.collect()
    if pending_rows:
        yield pd.concat(pending, ignore_index=True)


def _stream_text_part(
    result: duckdb.DuckDBPyConnection,
    chunk_rows: int,
    pending: list[pd.DataFrame],
    pending_rows: int,
) -> Iterator[pd.DataFrame]:
    """Yield full chunks from one hash slice. Return the unpacked leftover.

    ``pending`` rows are kept until they fill ``chunk_rows``, so a slice
    boundary does not emit a short chunk. The return value is that leftover.
    """
    vectors = _vectors_per_chunk(chunk_rows)
    while True:
        fetched = result.fetch_df_chunk(vectors)
        if fetched is None or len(fetched) == 0:
            break
        pending.append(fetched)
        pending_rows += len(fetched)
        if len(pending) > 32:
            pending = [pd.concat(pending, ignore_index=True)]
        while pending_rows >= chunk_rows:
            block = pd.concat(pending, ignore_index=True)
            piece = block.iloc[:chunk_rows].reset_index(drop=True).copy()
            rest = block.iloc[chunk_rows:].reset_index(drop=True)
            del block
            pending = [] if rest.empty else [rest]
            pending_rows = 0 if rest.empty else len(rest)
            yield piece
    return pending, pending_rows


def _empty_label_frame() -> pd.DataFrame:
    """An empty label table with the four output columns and their dtypes."""
    return pd.DataFrame(
        {
            "file_sha": pd.Series(dtype="object"),
            "language": pd.Series(dtype="object"),
            "is_english": pd.Series(dtype="bool"),
            "body_chars": pd.Series(dtype="int64"),
        }
    )


def _as_text(value: object) -> str:
    """Return ``value`` when it is a string, otherwise an empty string."""
    if isinstance(value, str):
        return value
    return ""


# Algorithm A8, see CODE_FLOW.md
def label_languages(texts: pd.DataFrame) -> pd.DataFrame:
    """Label each row. One input row becomes one output row.

    The body is ``normalize_text`` then ``split_front_matter``. ``language``
    is ``detect_language`` on the original text (algorithm A8). ``is_english``
    is true for ``en`` and ``unknown``. ``body_chars`` is the length of the
    normalised body, so front matter is not counted. This function does not
    drop duplicate shas; the SQL query does that. It runs in one process.
    The pool uses ``_label_prepared_chunk`` so workers never receive the full text.

    Inputs: a frame with ``file_sha`` and ``content``.
    Outputs: ``file_sha``, ``language``, ``is_english``, ``body_chars``.

    Example:
    >>> sample = pd.DataFrame({"file_sha": ["a"], "content": ["ok"]})
    >>> row = label_languages(sample).iloc[0]
    >>> (row.language, bool(row.is_english), int(row.body_chars))
    ('unknown', True, 2)
    """
    if "file_sha" not in texts.columns or "content" not in texts.columns:
        raise ValueError("texts needs columns file_sha and content")
    if texts.empty:
        return _empty_label_frame()
    file_shas: list[str] = []
    languages: list[str] = []
    english: list[bool] = []
    sizes: list[int] = []
    pairs = zip(texts["file_sha"].tolist(), texts["content"].tolist(), strict=True)
    for file_sha, content in pairs:
        text = _as_text(content)
        sample, size = language_sample_and_size(text)
        language = classify_language_sample(sample)
        file_shas.append(str(file_sha))
        languages.append(language)
        english.append(language in {"en", "unknown"})
        sizes.append(size)
    return pd.DataFrame(
        {
            "file_sha": file_shas,
            "language": languages,
            "is_english": english,
            "body_chars": sizes,
        }
    )


def _start_language_worker() -> None:
    """Load the lingua detector once in this worker process."""
    build_language_detector()


def _prepare_language_chunk(texts: pd.DataFrame) -> pd.DataFrame:
    """Turn full skill texts into the short sample the workers label.

    ``sample`` is at most ``LANGUAGE_MAX_CHARS`` of the body. ``body_chars``
    is still the full normalised body length. The caller's frame, which holds
    the raw ``content``, can be dropped after this returns.
    """
    if texts.empty:
        return pd.DataFrame(
            {
                "file_sha": pd.Series(dtype="object"),
                "sample": pd.Series(dtype="object"),
                "body_chars": pd.Series(dtype="int64"),
            }
        )
    file_shas: list[str] = []
    samples: list[str] = []
    sizes: list[int] = []
    pairs = zip(texts["file_sha"].tolist(), texts["content"].tolist(), strict=True)
    for file_sha, content in pairs:
        sample, size = language_sample_and_size(_as_text(content))
        file_shas.append(str(file_sha))
        samples.append(sample)
        sizes.append(size)
    return pd.DataFrame(
        {"file_sha": file_shas, "sample": samples, "body_chars": sizes}
    )


def _label_prepared_chunk(prepared: pd.DataFrame) -> pd.DataFrame:
    """Label one prepared chunk. The pool calls this in a worker process.

    The frame has ``file_sha``, ``sample`` and ``body_chars``. The sample is
    what ``classify_language_sample`` scores, so the worker does not need the
    original skill text.
    """
    if prepared.empty:
        return _empty_label_frame()
    languages = [classify_language_sample(sample) for sample in prepared["sample"].tolist()]
    return pd.DataFrame(
        {
            "file_sha": prepared["file_sha"].astype(str).tolist(),
            "language": languages,
            "is_english": [language in {"en", "unknown"} for language in languages],
            "body_chars": [int(value) for value in prepared["body_chars"].tolist()],
        }
    )


def _label_chunks_in_pool(chunks: Iterator[pd.DataFrame]) -> Iterator[pd.DataFrame]:
    """Label chunks on ``DOWNLOAD_WORKERS`` processes.

    Each worker calls ``build_language_detector`` once (spawn, so the
    parent's detector is not copied). This process cuts each chunk down to
    the language sample before the pool sees it, then drops the full text.
    The chunk iterator stays on this process, so the DuckDB connection stays
    on one thread. At most ``DOWNLOAD_WORKERS`` prepared chunks are waiting.

    Inputs: frames of ``file_sha`` and ``content``.
    Outputs: label frames, in the same order the chunks were read.
    """
    pending_chunks = iter(chunks)
    try:
        first = _prepare_language_chunk(next(pending_chunks))
    except StopIteration:
        return
    context = multiprocessing.get_context("spawn")
    workers = settings.DOWNLOAD_WORKERS
    with context.Pool(processes=workers, initializer=_start_language_worker) as pool:
        waiting: deque[multiprocessing.pool.AsyncResult] = deque()
        waiting.append(pool.apply_async(_label_prepared_chunk, (first,)))
        for chunk in pending_chunks:
            prepared = _prepare_language_chunk(chunk)
            del chunk
            waiting.append(pool.apply_async(_label_prepared_chunk, (prepared,)))
            if len(waiting) >= workers:
                yield waiting.popleft().get()
        while waiting:
            yield waiting.popleft().get()


def _labels_table(frame: pd.DataFrame) -> pa.Table:
    """One label frame as an Arrow table with the fixed schema."""
    return pa.table(
        {
            "file_sha": frame["file_sha"].astype(str).tolist(),
            "language": frame["language"].astype(str).tolist(),
            "is_english": [bool(value) for value in frame["is_english"].tolist()],
            "body_chars": [int(value) for value in frame["body_chars"].tolist()],
        },
        schema=_LABEL_SCHEMA,
    )


def _empty_labels_table() -> pa.Table:
    """A zero-row Arrow table that still has the label schema."""
    return pa.table(
        {"file_sha": [], "language": [], "is_english": [], "body_chars": []},
        schema=_LABEL_SCHEMA,
    )


def _print_label_rate(done: int, started: float, total: int, *, first: bool) -> None:
    """Print texts per second. The first chunk also prints the estimated total."""
    elapsed = time.perf_counter() - started
    rate = done / elapsed if elapsed else 0.0
    print(f"texts_per_second: {rate:.1f}", flush=True)
    if first and total > 0 and rate > 0:
        print(f"estimated_total_seconds: {total / rate:.0f}", flush=True)
    print(f"labelled: {done} of {total}", flush=True)


def _count_distinct_texts(con: duckdb.DuckDBPyConnection) -> int:
    """Return how many distinct texts Q2.1 will label."""
    _quiet_duckdb(con)
    row = con.execute(DISTINCT_TEXT_COUNT_SQL).fetchone()
    if row is None:
        return 0
    return int(row[0])


def _write_unsorted_labels(
    chunks: Iterator[pd.DataFrame],
    unsorted_path: Path,
    total: int,
) -> None:
    """Label every chunk and append it to the unsorted parquet."""
    unsorted_path.parent.mkdir(parents=True, exist_ok=True)
    writer = pq.ParquetWriter(unsorted_path, _LABEL_SCHEMA, compression="zstd")
    wrote = 0
    first = True
    started = time.perf_counter()
    try:
        for labeled in _label_chunks_in_pool(chunks):
            if len(labeled) == 0:
                continue
            writer.write_table(_labels_table(labeled))
            wrote += len(labeled)
            _print_label_rate(wrote, started, total, first=first)
            first = False
        if wrote == 0:
            writer.write_table(_empty_labels_table())
    finally:
        writer.close()


def save_outputs(unsorted_path: Path, out_path: Path) -> None:
    """Sort label rows by ``file_sha`` and write the final parquet.

    Query Q2.1 ends with DuckDB ``COPY ... ORDER BY file_sha``. The sorted
    file is written beside ``out_path`` and then renamed over it, so a failed
    sort leaves the previous output in place. The caller deletes the unsorted file.

    Inputs: the unsorted parquet and the final path.
    Outputs: ``out_path``, zstd, columns ``file_sha``, ``language``,
    ``is_english``, ``body_chars``.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_path = out_path.with_name(out_path.stem + ".sorted.parquet")
    if sorted_path.exists():
        sorted_path.unlink()
    sql = SORT_LABELS_SQL.format(
        source=_quote_path(str(unsorted_path)),
        dest=_quote_path(str(sorted_path)),
    )
    con = connect_duckdb()
    try:
        _quiet_duckdb(con)
        con.execute(sql)
    finally:
        con.close()
    sorted_path.replace(out_path)


def label_all_skill_languages(
    con: duckdb.DuckDBPyConnection,
    out_path: Path,
) -> pd.DataFrame:
    """Label every distinct text and write ``out_path``, sorted by ``file_sha``.

    Counts the texts (Q2.1), streams them in chunks of 10,000, and labels
    the chunks on a process pool. Workers receive the language sample, not
    the full text. ``label_languages`` stays single-process for tests.
    The returned frame is the sorted file.

    Inputs: a connection with the ``artifacts`` view, and the output path.
    Outputs: the parquet file and the same rows as a DataFrame.
    """
    total = _count_distinct_texts(con)
    print(f"distinct_texts: {total}", flush=True)
    print("reading distinct texts", flush=True)
    started = time.perf_counter()
    chunks = read_distinct_texts_in_chunks(con)
    unsorted_path = out_path.with_name(out_path.stem + ".unsorted.parquet")
    try:
        first = next(chunks)
    except StopIteration:
        first = None
    print(f"first text part ready in {time.perf_counter() - started:.1f}s", flush=True)
    if first is None:
        stream: Iterator[pd.DataFrame] = iter(())
    else:
        stream = chain((first,), chunks)
    try:
        _write_unsorted_labels(stream, unsorted_path, total)
        save_outputs(unsorted_path, out_path)
    finally:
        if unsorted_path.is_file():
            unsorted_path.unlink()
    return pd.read_parquet(out_path)


def _side_counts(sides: pd.DataFrame, english: bool) -> tuple[int, int, int]:
    """Texts, artifact rows and repos for one side of ``is_english``."""
    if sides.empty:
        return (0, 0, 0)
    match = sides.loc[sides["is_english"].astype(bool) == english]
    if match.empty:
        return (0, 0, 0)
    row = match.iloc[0]
    return (int(row["texts"]), int(row["files"]), int(row["repos"]))


def _as_number(value: object) -> int | float | None:
    """Return a median as an int when it is whole, else one decimal, or None."""
    if value is None or pd.isna(value):
        return None
    number = float(value)
    if number.is_integer():
        return int(number)
    return round(number, 1)


def _top_languages(counts: pd.DataFrame) -> str:
    """The ten languages with the most texts, as ``code: count`` pairs."""
    parts: list[str] = []
    for _, row in counts.head(10).iterrows():
        parts.append(f"{row['language']}: {int(row['texts'])}")
    return ", ".join(parts)


def _unknown_stats(counts: pd.DataFrame) -> tuple[int, int | float | None]:
    """How many texts are ``unknown``, and the median body size of those texts."""
    unknown = counts.loc[counts["language"] == "unknown"]
    if unknown.empty:
        return (0, None)
    row = unknown.iloc[0]
    return (int(row["texts"]), _as_number(row["median_body_chars"]))


def summarize_languages(
    con: duckdb.DuckDBPyConnection,
    labels_path: Path,
) -> dict[str, object]:
    """Count English and non-English texts, files and repos.

    Query Q2.2. Texts are distinct ``file_sha`` values. Files are artifact
    rows. Repos are distinct ``repo_full_name`` values on those rows.
    A repo with both kinds of text is counted on both sides. ``unknown``
    is English (algorithm A8). The summary also reports how many texts are
    ``unknown`` and their median ``body_chars``. ``top_languages`` is the
    ten codes with the most texts.

    Inputs: a connection with the ``artifacts`` view, and the label parquet.
    Outputs: a dict of counts. Nothing is written.
    """
    _quiet_duckdb(con)
    quoted = _quote_path(str(labels_path))
    sides = con.execute(LANGUAGE_SIDE_SQL.format(labels=quoted)).fetchdf()
    counts = con.execute(LANGUAGE_COUNTS_SQL.format(labels=quoted)).fetchdf()
    english_texts, english_files, english_repos = _side_counts(sides, True)
    other_texts, other_files, other_repos = _side_counts(sides, False)
    unknown_texts, unknown_median = _unknown_stats(counts)
    return {
        "english_texts": english_texts,
        "non_english_texts": other_texts,
        "english_files": english_files,
        "non_english_files": other_files,
        "english_repos": english_repos,
        "non_english_repos": other_repos,
        "unknown_texts": unknown_texts,
        "unknown_median_body_chars": unknown_median,
        "top_languages": _top_languages(counts),
    }


def _body_start(content: object) -> str:
    """First 300 characters of the normalised body, with newlines as spaces.

    Example:
    >>> _body_start("---\\nname: demo\\n---\\nHello\\nthere")
    'Hello there'
    """
    body = split_front_matter(normalize_text(_as_text(content)))[1]
    one_line = body.replace("\r", " ").replace("\n", " ")
    return one_line[:BODY_START_CHARS]


def _sample_sql(labels_path: Path) -> str:
    """Q2.3 with the label path, the project seed and 40 rows per group."""
    return LANGUAGE_CHECK_SAMPLE_SQL.format(
        labels=_quote_path(str(labels_path)),
        seed=int(settings.RANDOM_SEED),
        per_group=int(LANGUAGE_CHECK_PER_GROUP),
    )


def _check_frame(picked: pd.DataFrame) -> pd.DataFrame:
    """Turn the sampled texts into the CSV columns. Human columns stay empty."""
    rows = {
        "file_sha": picked["file_sha"].astype(str),
        "language": picked["language"].astype(str),
        "is_english": [bool(value) for value in picked["is_english"].tolist()],
        "body_chars": [int(value) for value in picked["body_chars"].tolist()],
        "body_start": [_body_start(value) for value in picked["content"].tolist()],
        "human_language": "",
        "human_is_english": "",
    }
    return pd.DataFrame(rows, columns=list(_CHECK_COLUMNS))


def draw_language_check_sample(
    con: duckdb.DuckDBPyConnection,
    labels_path: Path,
    out_path: Path,
) -> pd.DataFrame:
    """Draw the 120-row hand-check CSV and write it.

    Query Q2.3. The seed is ``RANDOM_SEED``. Each group contributes 40
    texts, or every text in that group when the group is smaller.
    Non-English rows are mixed by language: the first hash-ordered text of
    each language, then the second, and so on, until 40 are taken.
    ``body_start`` is the first 300 characters of the normalised body on
    one line. ``human_language`` and ``human_is_english`` are empty.
    Two calls with the same labels and seed return the same rows.

    Inputs: a connection with ``artifacts``, the label parquet, the CSV path.
    Outputs: the CSV and the same rows.
    """
    picked = con.execute(_sample_sql(labels_path)).fetchdf()
    frame = _check_frame(picked)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out_path, index=False)
    return frame


def _group_name(language: str) -> str:
    """Map a language code to ``en``, ``unknown`` or ``non_english``.

    Example:
    >>> _group_name("zh")
    'non_english'
    >>> _group_name("unknown")
    'unknown'
    """
    if language == "en":
        return "en"
    if language == "unknown":
        return "unknown"
    return "non_english"


def _parse_flag(value: object, *, blank_ok: bool) -> bool | None:
    """Read a true/false cell. A blank cell is None when ``blank_ok`` is set.

    Example:
    >>> _parse_flag("true", blank_ok=True)
    True
    >>> _parse_flag("", blank_ok=True) is None
    True
    >>> _parse_flag("false", blank_ok=False)
    False
    """
    if value is None or pd.isna(value):
        text = ""
    else:
        text = str(value).strip().lower()
    if text in _BLANK_WORDS:
        if blank_ok:
            return None
        raise ValueError("is_english is blank")
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    raise ValueError(f"expected true or false, got {value!r}")


def _accuracy(correct: int, scored: int) -> float | None:
    """Return ``correct / scored`` to 4 decimal places, or None when nothing was scored."""
    if scored <= 0:
        return None
    return round(correct / scored, 4)


def _score_rows(frame: pd.DataFrame) -> dict[str, object]:
    """Compare ``is_english`` with ``human_is_english`` overall and per group."""
    totals = {group: [0, 0] for group in LANGUAGE_CHECK_GROUPS}
    correct = 0
    scored = 0
    skipped = 0
    disagreements: list[dict[str, object]] = []
    for _, row in frame.iterrows():
        human = _parse_flag(row["human_is_english"], blank_ok=True)
        if human is None:
            skipped += 1
            continue
        ours = _parse_flag(row["is_english"], blank_ok=False)
        group = _group_name(str(row["language"]))
        totals[group][0] += 1
        scored += 1
        if ours == human:
            totals[group][1] += 1
            correct += 1
            continue
        disagreements.append(
            {
                "file_sha": str(row["file_sha"]),
                "language": str(row["language"]),
                "is_english": ours,
                "human_language": str(row["human_language"]),
                "human_is_english": human,
            }
        )
    by_group = {
        group: {
            "scored": totals[group][0],
            "correct": totals[group][1],
            "accuracy": _accuracy(totals[group][1], totals[group][0]),
        }
        for group in LANGUAGE_CHECK_GROUPS
    }
    return {
        "scored": scored,
        "correct": correct,
        "skipped_blank": skipped,
        "accuracy": _accuracy(correct, scored),
        "by_group": by_group,
        "disagreements": disagreements,
    }


def score_language_check(csv_path: Path) -> dict[str, object]:
    """Score a filled language-check CSV.

    Accuracy is the share of rows whose ``human_is_english`` matches our
    ``is_english``. Blank human cells are skipped, not counted wrong.
    Groups are ``en``, ``unknown`` and ``non_english`` (every other code).
    Disagreements list ``file_sha``, both flags and ``human_language``.

    Inputs: the CSV path. The file must have the seven check columns.
    Outputs: a dict with ``scored``, ``correct``, ``accuracy``, ``by_group``
    and ``disagreements``. Raises ``FileNotFoundError`` when the file is missing
    and ``ValueError`` when a filled flag is not true or false.
    """
    if not csv_path.is_file():
        raise FileNotFoundError(
            f"Missing language check: {csv_path} "
            "(build with: python -m skill_genealogy.select_registries)"
        )
    frame = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    missing = [name for name in _CHECK_COLUMNS if name not in frame.columns]
    if missing:
        raise ValueError(f"missing columns: {', '.join(missing)}")
    return _score_rows(frame)


def _sample_has_human_labels(path: Path) -> bool:
    """True when a hand-check CSV has any filled human cell."""
    if not path.is_file():
        return False
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    for column in ("human_language", "human_is_english"):
        if column not in frame.columns:
            continue
        filled = frame[column].astype(str).str.strip() != ""
        if bool(filled.any()):
            return True
    return False


def _first_rows(path: Path, count: int = 2) -> pd.DataFrame:
    """Return the first ``count`` rows of a sorted label parquet."""
    parquet_file = pq.ParquetFile(path)
    if parquet_file.metadata is None or parquet_file.metadata.num_rows == 0:
        return _empty_label_frame()
    table = parquet_file.read_row_group(0).slice(0, count)
    return table.to_pandas()


def _print_score(score: dict[str, object]) -> None:
    """Print overall accuracy, one line per group, then each disagreement."""
    by_group = score["by_group"]
    if not isinstance(by_group, dict):
        raise TypeError(type(by_group))
    numbers: dict[str, object] = {
        "scored": score["scored"],
        "correct": score["correct"],
        "skipped_blank": score["skipped_blank"],
        "accuracy": score["accuracy"],
    }
    for group in LANGUAGE_CHECK_GROUPS:
        item = by_group[group]
        numbers[f"{group}_scored"] = item["scored"]
        numbers[f"{group}_correct"] = item["correct"]
        numbers[f"{group}_accuracy"] = item["accuracy"]
    print_summary("language check score", numbers)
    disagreements = score["disagreements"]
    if not isinstance(disagreements, list):
        raise TypeError(type(disagreements))
    print(f"disagreements: {len(disagreements)}")
    if disagreements:
        print_sample_records(pd.DataFrame(disagreements), "Disagreements", len(disagreements))


def _print_label_report(labels_path: Path, summary: dict[str, object]) -> None:
    """Print the language summary and two label rows."""
    print_summary("language labels", summary)
    print_sample_records(_first_rows(labels_path), "Sample language labels", 2)


def _selection_paths() -> list[Path]:
    """The three step 2–6 outputs, in the full data folder."""
    folder = settings.data_dir(False)
    return [
        folder / "registries.parquet",
        folder / "repo_roles.parquet",
        folder / "dev_registries.parquet",
    ]


def _require_columns(frame: pd.DataFrame, columns: tuple[str, ...], label: str) -> None:
    """Raise ``ValueError`` when ``frame`` is missing any of ``columns``."""
    missing = [name for name in columns if name not in frame.columns]
    if missing:
        raise ValueError(f"{label} needs columns: {', '.join(missing)}")


def _is_true_flag(value: object) -> bool:
    """True for a real true flag. Missing and false stay false.

    Example:
    >>> _is_true_flag(True)
    True
    >>> _is_true_flag(pd.NA)
    False
    >>> _is_true_flag(float("nan"))
    False
    """
    if value is None or value is pd.NA:
        return False
    try:
        if bool(pd.isna(value)):
            return False
    except (TypeError, ValueError):
        return False
    return bool(value)


def _registry_mask(frame: pd.DataFrame) -> pd.Series:
    """True for a non-fork with enough stars and enough English skills.

    A missing star count or fork flag does not pass. The cutoffs are
    ``MIN_REGISTRY_STARS`` and ``MIN_REGISTRY_ENGLISH_SKILLS``.
    """
    stars = pd.to_numeric(frame["stars"], errors="coerce")
    forks = pd.to_numeric(frame["is_fork"], errors="coerce")
    skills = pd.to_numeric(frame["english_skills"], errors="coerce").fillna(0)
    mask = (stars >= settings.MIN_REGISTRY_STARS) & forks.eq(0)
    mask = mask & (skills >= settings.MIN_REGISTRY_ENGLISH_SKILLS)
    return mask.fillna(False)


def count_english_skills_per_repo(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Count English skills and English files in every dataset repo.

    Query Q2.4. ``english_skills`` is the distinct English ``file_sha``
    values in that repo. ``files`` is the English artifact rows, so one
    text stored at two paths counts as two files. A repo with no English
    skill still appears, with both counts at 0. Rows are sorted by repo.

    Inputs: a connection with views ``artifacts``, ``repos`` and
    ``skill_language``.
    Outputs: ``repo``, ``owner``, ``stars``, ``is_fork``, ``english_skills``,
    ``files``.
    """
    _quiet_duckdb(con)
    frame = con.execute(COUNT_ENGLISH_SKILLS_PER_REPO_SQL).fetchdf()
    for name in ("english_skills", "files"):
        frame[name] = pd.to_numeric(frame[name], errors="coerce").fillna(0).astype("int64")
    return frame.loc[:, list(_SKILL_COUNT_COLUMNS)].reset_index(drop=True)


def select_registry_repos(skills_per_repo: pd.DataFrame) -> pd.DataFrame:
    """Keep repos that pass the registry rule, sorted by repo.

    The rule is ``stars >= MIN_REGISTRY_STARS``, ``is_fork = 0`` and
    ``english_skills >= MIN_REGISTRY_ENGLISH_SKILLS``. A missing star count
    or fork flag does not pass.

    Inputs: the frame from ``count_english_skills_per_repo``.
    Outputs: the same columns, one row per registry.

    Example:
    >>> repos = pd.DataFrame({
    ...     "repo": ["a/big", "b/fork", "c/small"],
    ...     "stars": [200, 1000, 50],
    ...     "is_fork": [0, 1, 0],
    ...     "english_skills": [10, 10, 10],
    ... })
    >>> list(select_registry_repos(repos)["repo"])
    ['a/big']
    """
    _require_columns(
        skills_per_repo,
        ("repo", "stars", "is_fork", "english_skills"),
        "skills_per_repo",
    )
    chosen = skills_per_repo.loc[_registry_mask(skills_per_repo)].copy()
    return chosen.sort_values("repo", kind="mergesort").reset_index(drop=True)


def _empty_hub_frame() -> pd.DataFrame:
    """A zero-row hub table with the four hub columns."""
    return pd.DataFrame(
        {
            "repo": pd.Series(dtype="object"),
            "skills_tested": pd.Series(dtype="int64"),
            "first_seen_elsewhere": pd.Series(dtype="int64"),
            "hub_share": pd.Series(dtype="float64"),
        }
    )


def _hub_joins(repos: list[str] | None) -> str:
    """The extra join for Q2.5: the skill cutoff, or a named repo list."""
    if repos is None:
        minimum = int(settings.HUB_TEST_MIN_SKILLS)
        return (
            "JOIN skill_counts ON shares.repo = skill_counts.repo\n"
            f"    AND skill_counts.english_skills >= {minimum}"
        )
    return "JOIN hub_repo_filter ON hub_repo_filter.repo = shares.repo"


def _hub_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Cast the hub query to ``repo``, tested texts, elsewhere texts, share."""
    if frame.empty:
        return _empty_hub_frame()
    out = frame.copy()
    out["repo"] = out["repo"].astype(str)
    out["skills_tested"] = out["skills_tested"].astype("int64")
    out["first_seen_elsewhere"] = out["first_seen_elsewhere"].astype("int64")
    out["hub_share"] = out["hub_share"].astype("float64")
    return out.loc[:, list(_HUB_COLUMNS)].reset_index(drop=True)


# Algorithm A18, see CODE_FLOW.md
def compute_hub_shares(
    con: duckdb.DuckDBPyConnection,
    repos: list[str] | None = None,
) -> pd.DataFrame:
    """Share of a repo's dated English texts that were first seen in an older repo.

    Query Q2.5. For each English text, the earliest ``first_commit_at`` wins.
    When two repos share that time, the smaller repo name wins. A text is
    first seen elsewhere when that winner is a different repo and its time
    is strictly earlier than this repo's own time. Texts with no usable
    date are left out. ``hub_share`` is elsewhere texts divided by tested
    texts. With ``repos`` omitted, only repos that have at least
    ``HUB_TEST_MIN_SKILLS`` English skills are scored. Passing ``repos``
    scores those names even when they are smaller.

    Inputs: a connection with views ``artifacts`` and ``skill_language``.
    ``repos`` is an optional list of ``owner/repo`` names.
    Outputs: ``repo``, ``skills_tested``, ``first_seen_elsewhere``, ``hub_share``.

    Example: three repos and four texts. A published t1 and t2 first.
    B holds all four. Two of B's texts are older in A, so B's share is 0.5.
    t4 has the same time in B and C, so that tie is not elsewhere for C.
    >>> artifacts = pd.DataFrame({
    ...     "repo_full_name": ["A", "B", "A", "B", "B", "C", "B", "C"],
    ...     "file_sha": ["t1", "t1", "t2", "t2", "t3", "t3", "t4", "t4"],
    ...     "first_commit_at": [
    ...         "2020-01-01", "2020-06-01", "2020-02-01", "2020-07-01",
    ...         "2020-03-01", "2020-08-01", "2020-04-01", "2020-04-01",
    ...     ],
    ... })
    >>> labels = pd.DataFrame({
    ...     "file_sha": ["t1", "t2", "t3", "t4"],
    ...     "is_english": [True, True, True, True],
    ... })
    >>> con = connect_duckdb()
    >>> _ = con.register("artifacts", artifacts)
    >>> _ = con.register("skill_language", labels)
    >>> shares = compute_hub_shares(con, repos=["A", "B", "C"]).set_index("repo")
    >>> int(shares.loc["B", "first_seen_elsewhere"])
    2
    >>> round(float(shares.loc["B", "hub_share"]), 1)
    0.5
    >>> float(shares.loc["A", "hub_share"])
    0.0
    >>> int(shares.loc["C", "first_seen_elsewhere"])
    1
    """
    if repos is not None and len(repos) == 0:
        return _empty_hub_frame()
    _quiet_duckdb(con)
    if repos is not None:
        names = sorted({str(repo) for repo in repos})
        con.register("hub_repo_filter", pd.DataFrame({"repo": names}))
    sql = HUB_SHARES_SQL.format(joins=_hub_joins(repos))
    return _hub_frame(con.execute(sql).fetchdf())


def _role_labels(is_registry: pd.Series, is_hub: pd.Series) -> pd.Series:
    """Map the two flags to ``registry``, ``registry_hub``, ``hub`` or ``repository``.

    Example:
    >>> flags = pd.Series([True, True, False])
    >>> hubs = pd.Series([False, True, True])
    >>> list(_role_labels(flags, hubs))
    ['registry', 'registry_hub', 'hub']
    """
    role = pd.Series("repository", index=is_registry.index, dtype="object")
    role = role.mask(is_hub, "hub")
    role = role.mask(is_registry, "registry")
    return role.mask(is_registry & is_hub, "registry_hub")


def _hub_tested(skills_tested: pd.Series) -> pd.Series:
    """True when at least ``HUB_MIN_DATED_SKILLS`` texts had a usable date.

    A missing count is false. This does not change ``hub_share``.

    Example:
    >>> list(_hub_tested(pd.Series([1, 5, None])))
    [False, True, False]
    """
    tested = pd.to_numeric(skills_tested, errors="coerce").fillna(0)
    return (tested >= settings.HUB_MIN_DATED_SKILLS).astype(bool)


def _as_hub_flag(hub_share: pd.Series, skills_tested: pd.Series) -> pd.Series:
    """True when the share is high enough and enough texts were dated.

    ``is_hub`` needs ``hub_share >= HUB_MIN_SHARE`` and
    ``skills_tested >= HUB_MIN_DATED_SKILLS``. A share of 1.0 on one dated
    text is not a hub. A missing share is false.

    Example:
    >>> share = pd.Series([1.0, 0.5, 0.5])
    >>> tested = pd.Series([1, 5, 4])
    >>> list(_as_hub_flag(share, tested))
    [False, True, False]
    """
    share = pd.to_numeric(hub_share, errors="coerce")
    high = (share >= settings.HUB_MIN_SHARE).fillna(False)
    return (high & _hub_tested(skills_tested)).astype(bool)


def assign_repo_roles(
    skills_per_repo: pd.DataFrame,
    hub_shares: pd.DataFrame,
) -> pd.DataFrame:
    """Give every repo one role, sorted by repo.

    ``registry`` passes the registry rule and is not a hub.
    ``registry_hub`` passes the rule and is a hub. ``hub`` is a hub and does
    not pass the rule. A hub needs ``hub_share >= HUB_MIN_SHARE`` and at
    least ``HUB_MIN_DATED_SKILLS`` dated texts. ``hub_share`` is kept as
    computed. ``hub_tested`` is true when that dated-text minimum is met.
    ``repository`` is everything else. A repo missing from ``hub_shares``
    is not a hub and was not tested.

    These hub columns are a preview from dataset dates. G13 recomputes the
    registry hub tag from downloaded commit dates.

    Inputs: skill counts for every repo, and the hub-test frame
    (``repo``, ``hub_share``, ``skills_tested``).
    Outputs: ``repo``, ``owner``, ``stars``, ``is_fork``, ``english_skills``,
    ``is_registry``, ``is_hub``, ``hub_tested``, ``hub_share``, ``repo_role``.
    """
    _require_columns(
        skills_per_repo,
        ("repo", "owner", "stars", "is_fork", "english_skills"),
        "skills_per_repo",
    )
    _require_columns(hub_shares, ("repo", "hub_share", "skills_tested"), "hub_shares")
    hubs = hub_shares.loc[:, ["repo", "hub_share", "skills_tested"]].drop_duplicates("repo")
    merged = skills_per_repo.merge(hubs, on="repo", how="left")
    merged["is_registry"] = _registry_mask(merged).astype(bool)
    merged["hub_tested"] = _hub_tested(merged["skills_tested"])
    merged["is_hub"] = _as_hub_flag(merged["hub_share"], merged["skills_tested"])
    merged["repo_role"] = _role_labels(merged["is_registry"], merged["is_hub"])
    chosen = merged.loc[:, list(_ROLE_COLUMNS)]
    return chosen.sort_values("repo", kind="mergesort").reset_index(drop=True)


def _registries_from_roles(roles: pd.DataFrame, skills: pd.DataFrame) -> pd.DataFrame:
    """Registry rows with file counts, hub share, ``is_hub`` and ``hub_tested``."""
    files = skills.loc[:, ["repo", "files"]]
    chosen = roles.loc[roles["is_registry"]].merge(files, on="repo", how="left")
    frame = chosen.loc[:, list(_REGISTRY_COLUMNS)]
    return frame.sort_values("repo", kind="mergesort").reset_index(drop=True)


def _tercile_labels(row_count: int) -> list[str]:
    """Return ``small``, ``medium``, ``large`` for rows already sorted by size.

    The three groups differ by at most one row. Extra rows go to the smaller
    groups. Fewer than three rows are all ``small``.

    Example:
    >>> _tercile_labels(0)
    []
    >>> _tercile_labels(2)
    ['small', 'small']
    >>> (_tercile_labels(4).count("small"), _tercile_labels(4).count("large"))
    (2, 1)
    """
    if row_count < 3:
        return ["small"] * row_count
    base, extra = divmod(row_count, 3)
    sizes = [base + (1 if index < extra else 0) for index in range(3)]
    labels: list[str] = []
    for size, name in zip(sizes, SIZE_GROUPS, strict=True):
        labels.extend([name] * size)
    return labels


def _swap_in_hubs(
    ordered: pd.DataFrame,
    chosen: set[int],
    rng: random.Random,
    min_hubs: int,
) -> set[int]:
    """Replace drawn non-hubs with hubs from the same size group until ``min_hubs``.

    Hubs are never removed. A group with no spare hub, or no drawn non-hub,
    is skipped. The same generator makes the swap deterministic.
    """
    hub_flags = ordered["is_hub"].map(_is_true_flag)
    need = min_hubs - int(hub_flags.loc[list(chosen)].sum())
    for name in SIZE_GROUPS:
        if need <= 0:
            break
        group = ordered.index[ordered["size_group"] == name]
        spare = [index for index in group if index not in chosen and bool(hub_flags.at[index])]
        held = [index for index in group if index in chosen and not bool(hub_flags.at[index])]
        rng.shuffle(spare)
        rng.shuffle(held)
        pairs = min(need, len(spare), len(held))
        for hub_index, non_index in zip(spare[:pairs], held[:pairs], strict=True):
            chosen.remove(non_index)
            chosen.add(hub_index)
        need -= pairs
    return chosen


def pick_dev_registries(
    registries: pd.DataFrame,
    count: int,
    min_hubs: int,
    seed: int,
) -> pd.DataFrame:
    """Draw ``count`` registries in three size groups, keeping at least ``min_hubs`` hubs.

    Rows are sorted by ``english_skills``, then repo name, and cut into three
    equal-count groups (``small``, ``medium``, ``large``). Each group
    contributes ``count // 3`` repos, and the first groups take any remainder.
    The draw uses ``random.Random(seed)``. When the draw has fewer than
    ``min_hubs`` hubs, non-hubs are swapped for hubs in the same group.
    If no such hub exists, the sample stays short of the minimum.
    Two calls with the same arguments return the same rows.

    Inputs: registry rows with ``repo``, ``english_skills`` and ``is_hub``.
    ``count`` is how many rows to draw. ``min_hubs`` is the hub floor.
    ``seed`` is the random seed.
    Outputs: ``repo``, ``english_skills``, ``is_hub``, ``size_group``,
    sorted by group then repo.
    """
    _require_columns(registries, ("repo", "english_skills", "is_hub"), "registries")
    if registries.empty or count <= 0:
        return pd.DataFrame(columns=list(_DEV_COLUMNS))
    ordered = registries.sort_values(["english_skills", "repo"], kind="mergesort")
    ordered = ordered.reset_index(drop=True)
    ordered["is_hub"] = ordered["is_hub"].map(_is_true_flag)
    ordered["size_group"] = _tercile_labels(len(ordered))
    per_group, remainder = divmod(count, 3)
    takes = [per_group + (1 if index < remainder else 0) for index in range(3)]
    rng = random.Random(seed)
    chosen: set[int] = set()
    for name, take in zip(SIZE_GROUPS, takes, strict=True):
        members = ordered.index[ordered["size_group"] == name].tolist()
        rng.shuffle(members)
        chosen.update(members[:take])
    chosen = _swap_in_hubs(ordered, chosen, rng, min_hubs)
    columns = ["repo", "english_skills", "is_hub", "size_group"]
    drawn = ordered.loc[sorted(chosen), columns].copy()
    return _sort_dev(drawn.reset_index(drop=True))


def _sort_dev(frame: pd.DataFrame) -> pd.DataFrame:
    """Sort dev registries by size group (small, medium, large), then repo."""
    order = {name: index for index, name in enumerate(SIZE_GROUPS)}
    ranked = frame.assign(_size_order=frame["size_group"].map(order))
    ranked = ranked.sort_values(["_size_order", "repo"], kind="mergesort")
    return ranked.drop(columns="_size_order").reset_index(drop=True)


def _undated_text_counts(
    skills: pd.DataFrame,
    hub_shares: pd.DataFrame,
    registry_repos: pd.Series,
) -> dict[str, object]:
    """Count English texts left out of the hub test because they have no date.

    A text is one ``(repo, file_sha)`` pair. ``english_skills`` counts every
    English text. ``skills_tested`` counts those with a usable
    ``first_commit_at``. The difference was left out. The registry share
    uses the same difference inside the chosen registries.
    """
    tested = hub_shares.loc[:, ["repo", "skills_tested"]]
    eligible = skills.loc[
        skills["english_skills"] >= settings.HUB_TEST_MIN_SKILLS, ["repo", "english_skills"]
    ]
    merged = eligible.merge(tested, on="repo", how="left")
    merged["skills_tested"] = merged["skills_tested"].fillna(0)
    texts = int(merged["english_skills"].sum())
    undated = texts - int(merged["skills_tested"].sum())
    registry_set = set(registry_repos.astype(str))
    registry = skills.loc[skills["repo"].astype(str).isin(registry_set), ["repo", "english_skills"]]
    registry = registry.merge(tested, on="repo", how="left")
    registry["skills_tested"] = registry["skills_tested"].fillna(0)
    registry_texts = int(registry["english_skills"].sum())
    registry_undated = registry_texts - int(registry["skills_tested"].sum())
    share: float | None = None
    if registry_texts > 0:
        share = round(registry_undated / registry_texts, 4)
    return {
        "hub_test_texts": texts,
        "undated_texts_left_out": undated,
        "registry_texts": registry_texts,
        "registry_undated_texts": registry_undated,
        "registry_undated_share": share,
    }


def save_registry_outputs(
    registries: pd.DataFrame,
    roles: pd.DataFrame,
    dev_registries: pd.DataFrame,
    folder: Path,
) -> None:
    """Write ``registries``, ``repo_roles`` and ``dev_registries`` parquet files.

    Registries and roles are sorted by repo. Dev registries stay in size-group
    order. All three use zstd compression.

    Inputs: the three frames and the output folder.
    Outputs: the three parquet files inside ``folder``.
    """
    registry_rows = registries.loc[:, list(_REGISTRY_COLUMNS)]
    role_rows = roles.loc[:, list(_ROLE_COLUMNS)]
    dev_rows = _sort_dev(dev_registries.loc[:, list(_DEV_COLUMNS)])
    write_parquet(registry_rows, folder / "registries.parquet", ["repo"])
    write_parquet(role_rows, folder / "repo_roles.parquet", ["repo"])
    write_parquet(dev_rows, folder / "dev_registries.parquet")


def _print_null_date_decision(stats: dict[str, object]) -> None:
    """Print the open-question decision when NULL dates cover more than 10%."""
    share = stats.get("registry_undated_share")
    if not isinstance(share, (int, float)) or share <= settings.NULL_DATE_REPORT_ABOVE:
        return
    print(
        "NULL first_commit_at covers "
        f"{stats['registry_undated_texts']} of {stats['registry_texts']} "
        f"English registry texts ({share:.1%}). "
        "Those texts stay out of this hub test. "
        "discovered_at is not used. "
        "Hub columns in this stage are a preview."
    )


def _print_selection(
    registries: pd.DataFrame,
    roles: pd.DataFrame,
    dev_registries: pd.DataFrame,
    stats: dict[str, object],
) -> None:
    """Print registry and role counts, the top 10 by stars, and two rows of each file."""
    role_counts = roles["repo_role"].value_counts()
    hub_count = int(roles["is_hub"].sum()) if len(roles) else 0
    numbers: dict[str, object] = {
        "registries": len(registries),
        "Hubs (preview, dataset dates)": hub_count,
        "registry_hubs": int(registries["is_hub"].sum()) if len(registries) else 0,
        "dev_registries": len(dev_registries),
        "dev_hubs": int(dev_registries["is_hub"].sum()) if len(dev_registries) else 0,
    }
    for name in _ROLE_NAMES:
        numbers[f"role_{name}"] = int(role_counts.get(name, 0))
    numbers.update(stats)
    print_summary("registry selection", numbers)
    _print_null_date_decision(stats)
    if numbers["dev_hubs"] < settings.DEV_MIN_HUBS:
        print(
            f"dev sample has {numbers['dev_hubs']} hubs; "
            f"the minimum is {settings.DEV_MIN_HUBS}. "
            "No further hub in the same size group was left to swap in."
        )
    top = registries.sort_values(["stars", "repo"], ascending=[False, True], kind="mergesort")
    top_columns = ["repo", "stars", "english_skills", "hub_share", "is_hub", "hub_tested"]
    print_sample_records(top.reindex(columns=top_columns), "Top 10 registries by stars", 10)
    print_sample_records(registries, "Sample registries", 2)
    print_sample_records(roles, "Sample repo roles", 2)
    print_sample_records(dev_registries, "Sample dev registries", 2)


def _load_selection() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Read the three saved selection tables."""
    registries_path, roles_path, dev_path = _selection_paths()
    return (
        pd.read_parquet(registries_path),
        pd.read_parquet(roles_path),
        pd.read_parquet(dev_path),
    )


def _run_language_step(
    con: duckdb.DuckDBPyConnection,
    labels_path: Path,
    sample_path: Path,
    *,
    rebuild: bool,
    started: float,
) -> None:
    """Step 1. Relabel only when ``rebuild`` is true.

    ``--force`` leaves this false, so the language file stays. A missing
    check sheet is still drawn. A filled sheet is kept when languages are rebuilt.
    """
    # Step 1: language label
    if rebuild:
        print("Step 1: language label", flush=True)
        print("step 1 always writes the full data folder", flush=True)
        label_all_skill_languages(con, labels_path)
        if _should_write_sample(sample_path, force=True):
            draw_language_check_sample(con, labels_path, sample_path)
        summary = summarize_languages(con, labels_path)
        summary["elapsed_seconds"] = round(time.perf_counter() - started, 1)
        summary["output"] = str(labels_path)
        summary["language_check"] = str(sample_path)
        _print_label_report(labels_path, summary)
        return
    print("Step 1: language label already exists (pass --force-languages to rebuild)")
    if _should_write_sample(sample_path, force=False):
        draw_language_check_sample(con, labels_path, sample_path)


def _run_selection_steps(
    con: duckdb.DuckDBPyConnection,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, object]]:
    """Steps 2–6. Return registries, roles, the dev list, and the date counts.

    The caller has already registered artifacts, repos and skill_language.
    """
    # Step 2: count English skills per repo
    print("Step 2: count English skills per repo", flush=True)
    skills = count_english_skills_per_repo(con)
    # Step 3: registry rule
    print("Step 3: registry rule", flush=True)
    chosen = select_registry_repos(skills)
    # Step 4: hub test
    print("Step 4: hub test", flush=True)
    hub_shares = compute_hub_shares(con)
    # Step 5: repo roles
    print("Step 5: repo roles", flush=True)
    roles = assign_repo_roles(skills, hub_shares)
    registries = _registries_from_roles(roles, skills)
    if set(chosen["repo"]) != set(registries["repo"]):
        raise RuntimeError("registry rule and repo roles chose different repos")
    # Step 6: dev registries
    print("Step 6: dev registries", flush=True)
    dev_registries = pick_dev_registries(
        registries,
        settings.DEV_REGISTRY_COUNT,
        settings.DEV_MIN_HUBS,
        settings.RANDOM_SEED,
    )
    save_registry_outputs(registries, roles, dev_registries, settings.data_dir(False))
    stats = _undated_text_counts(skills, hub_shares, registries["repo"])
    return registries, roles, dev_registries, stats


def main() -> None:
    """Label texts, choose registries, assign roles, and draw the dev sample.

    ``--score-language-check`` only reads the CSV. It does not relabel.
    ``--dev`` is accepted and ignored: every output stays in the full data
    folder. ``--force`` rebuilds steps 2–6 and keeps ``skill_language.parquet``.
    ``--force-languages`` rebuilds that file, then steps 2–6. A filled
    language-check sheet is kept either way.

    Inputs: command-line flags, the artifacts and repos globs, and the
    language parquet when it already exists.
    Outputs: the language files plus ``registries.parquet``,
    ``repo_roles.parquet`` and ``dev_registries.parquet``. Nothing is returned.
    """
    parser = build_arg_parser("Choose registries and assign a role to every repo.")
    parser.add_argument(
        "--score-language-check",
        action="store_true",
        help="score a filled language_check_sample.csv and print accuracy",
    )
    parser.add_argument(
        "--force-languages",
        action="store_true",
        help="rebuild skill_language.parquet. --force keeps that file.",
    )
    args = parser.parse_args()
    if args.score_language_check:
        _print_score(score_language_check(_language_check_path()))
        return

    labels_path = _language_table_path()
    sample_path = _language_check_path()
    selection_paths = _selection_paths()
    if not labels_path.is_file() and not args.force_languages:
        raise FileNotFoundError(
            f"Missing skill_language.parquet: {labels_path} "
            "(build with: python -m skill_genealogy.select_registries --force-languages)"
        )
    rebuild_languages = bool(args.force_languages) or not labels_path.is_file()
    rebuild_selection = rebuild_languages or bool(args.force) or not outputs_exist(selection_paths)
    if args.dev:
        print("dev: registry tables still go to the full data folder", flush=True)
    if not rebuild_languages and not rebuild_selection and sample_path.is_file():
        print(
            "skip: selection already exists "
            "(pass --force to rebuild steps 2-6, --force-languages to relabel)"
        )
        _print_label_report(labels_path, {"output": str(labels_path)})
        registries, roles, dev_registries = _load_selection()
        _print_selection(registries, roles, dev_registries, {})
        return

    started = time.perf_counter()
    con = connect_duckdb()
    try:
        read_inputs(con)
        _run_language_step(
            con,
            labels_path,
            sample_path,
            rebuild=rebuild_languages,
            started=started,
        )
        _register_language(con)
        if not rebuild_selection:
            print("skip: selection already exists (pass --force to rebuild steps 2-6)")
            registries, roles, dev_registries = _load_selection()
            stats: dict[str, object] = {}
        else:
            registries, roles, dev_registries, stats = _run_selection_steps(con)
    finally:
        con.close()

    # Step 7: print summary
    print("Step 7: print summary", flush=True)
    stats["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    _print_selection(registries, roles, dev_registries, stats)


def _should_write_sample(path: Path, *, force: bool) -> bool:
    """True when the hand-check CSV should be written.

    A missing file is written. A rebuild rewrites it only while the human
    columns are empty. A filled sheet is left as it is. When that sheet is
    kept after the parquet was rebuilt, the message says it was drawn from
    the previous labels. ``main`` passes force only for ``--force-languages``.
    """
    if not path.is_file():
        return True
    if not force:
        return False
    if _sample_has_human_labels(path):
        print(
            f"kept {path}: drawn from the previous labels; re-check before scoring",
            flush=True,
        )
        return False
    return True


if __name__ == "__main__":
    main()
