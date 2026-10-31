"""Label the language of every distinct skill text.

What: one language row per distinct ``file_sha`` (stage 2, step 1).
Later cards add the registry rule, the hub test and repo roles to this file.
Inputs: GitSkills ``artifacts`` parquet (``settings.ARTIFACTS_GLOB``).
Outputs: ``src/data/skill_genealogy/skill_language.parquet`` and
``language_check_sample.csv`` in that same full-data folder.
``--dev`` does not change the folder: the labels do not depend on registries,
and a dev run reuses them.
How to run: ``python -m skill_genealogy.select_registries [--force]``.
Score a filled check sample with ``--score-language-check``.
"""

from __future__ import annotations

import multiprocessing
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
)
from skill_genealogy.text_helpers import (
    build_language_detector,
    detect_language,
    normalize_text,
    split_front_matter,
)

# DuckDB hands rows to Python in vectors of this size. fetch_df_chunk counts vectors.
DUCKDB_VECTOR_ROWS = 2048
# Q2.1 reads this many hash slices. One slice holds about a quarter of the
# distinct strings (about 1 GB) so a 16 GB machine is not asked to group every
# text at once. hash(file_sha) % TEXT_READ_PARTS is 0 .. TEXT_READ_PARTS-1.
TEXT_READ_PARTS = 4
# Revision 2: 40 texts from each of en, unknown and non-English.
LANGUAGE_CHECK_PER_GROUP = 40
# First characters of the body stored in the hand-check CSV, on one line.
BODY_START_CHARS = 300
# The three groups in language_check_sample.csv, in file order.
LANGUAGE_CHECK_GROUPS: tuple[str, ...] = ("en", "unknown", "non_english")

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
REGISTER_ARTIFACTS_SQL = (
    "CREATE OR REPLACE VIEW artifacts AS SELECT * FROM read_parquet({path})"
)

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


def read_inputs(con: duckdb.DuckDBPyConnection) -> None:
    """Register the artifacts parquet. Step 1 reads no other table.

    A missing glob raises ``FileNotFoundError`` and names the download command.
    G08 will register ``repos`` in this same function.

    Inputs: an open DuckDB connection. The path comes from ``settings``.
    Outputs: a view named ``artifacts``.
    """
    check_inputs_exist(
        {"artifacts": settings.ARTIFACTS_GLOB},
        {"artifacts": DATASET_BUILDER},
    )
    _register_artifacts(con)


def _quiet_duckdb(con: duckdb.DuckDBPyConnection) -> None:
    """Turn off the progress bar and let the group-by drop input order.

    The label file is sorted later by ``file_sha``. Keeping input order makes
    the distinct-text group-by hold more memory.
    """
    con.execute("SET enable_progress_bar = false")
    con.execute("SET preserve_insertion_order = false")


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
    chunk_rows: int = 50_000,
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
    and ``chunk_rows`` (default 50,000).
    Outputs: frames with columns ``file_sha`` and ``content``. An empty
    table yields nothing.

    Example:
    >>> con = connect_duckdb()
    >>> _ = con.execute("CREATE TABLE artifacts AS SELECT 'a' AS file_sha, 'hi' AS content")
    >>> frames = list(read_distinct_texts_in_chunks(con, chunk_rows=10))
    reading text part 1 of 4
    reading text part 2 of 4
    reading text part 3 of 4
    reading text part 4 of 4
    >>> list(frames[0]["file_sha"])
    ['a']
    """
    _quiet_duckdb(con)
    pending: list[pd.DataFrame] = []
    pending_rows = 0
    for part in range(TEXT_READ_PARTS):
        print(f"reading text part {part + 1} of {TEXT_READ_PARTS}", flush=True)
        sql = DISTINCT_TEXTS_SQL.format(parts=TEXT_READ_PARTS, part=part)
        result = con.execute(sql)
        pending, pending_rows = yield from _stream_text_part(
            result, chunk_rows, pending, pending_rows
        )
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
            yield block.iloc[:chunk_rows].reset_index(drop=True).copy()
            rest = block.iloc[chunk_rows:].reset_index(drop=True)
            pending = [] if rest.empty else [rest]
            pending_rows = 0 if rest.empty else len(rest)
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
    The pool calls it once per chunk.

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
        body = split_front_matter(normalize_text(text))[1]
        language = detect_language(text)
        file_shas.append(str(file_sha))
        languages.append(language)
        english.append(language in {"en", "unknown"})
        sizes.append(len(body))
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


def _label_texts_chunk(texts: pd.DataFrame) -> pd.DataFrame:
    """Label one chunk. The pool calls this in a worker process."""
    return label_languages(texts)


def _label_chunks_in_pool(chunks: Iterator[pd.DataFrame]) -> Iterator[pd.DataFrame]:
    """Label chunks on ``DOWNLOAD_WORKERS`` processes.

    Each worker calls ``build_language_detector`` once (spawn, so the
    parent's detector is not copied). The chunk iterator is pulled on this
    process, so the DuckDB connection stays on one thread. At most
    ``DOWNLOAD_WORKERS`` chunks are waiting at once.

    Inputs: frames of ``file_sha`` and ``content``.
    Outputs: label frames, in the same order the chunks were read.
    """
    pending_chunks = iter(chunks)
    try:
        first = next(pending_chunks)
    except StopIteration:
        return
    context = multiprocessing.get_context("spawn")
    workers = settings.DOWNLOAD_WORKERS
    with context.Pool(processes=workers, initializer=_start_language_worker) as pool:
        waiting: deque[multiprocessing.pool.AsyncResult] = deque()
        waiting.append(pool.apply_async(_label_texts_chunk, (first,)))
        for chunk in pending_chunks:
            waiting.append(pool.apply_async(_label_texts_chunk, (chunk,)))
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

    Counts the texts (Q2.1), streams them in chunks of 50,000, and labels
    the chunks on a process pool. ``label_languages`` stays single-process
    for tests. The returned frame is the sorted file.

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


def main() -> None:
    """Label every distinct text, or score a filled language-check CSV.

    ``--score-language-check`` only reads the CSV. It does not relabel.
    ``--dev`` is accepted and ignored: both flags write the full data folder.
    ``--force`` rebuilds the parquet. It rebuilds the CSV only when the
    human columns are still empty, so a filled sheet is kept.

    Inputs: command-line flags and ``settings.ARTIFACTS_GLOB``.
    Outputs: the parquet, the CSV, and a printed summary. Nothing is returned.
    """
    parser = build_arg_parser("Label the language of every distinct skill text.")
    parser.add_argument(
        "--score-language-check",
        action="store_true",
        help="score a filled language_check_sample.csv and print accuracy",
    )
    args = parser.parse_args()
    if args.score_language_check:
        _print_score(score_language_check(_language_check_path()))
        return

    labels_path = _language_table_path()
    sample_path = _language_check_path()
    if outputs_exist([labels_path, sample_path]) and not args.force:
        print(f"skip: {labels_path} already exists (pass --force to label again)")
        _print_label_report(labels_path, {"output": str(labels_path)})
        return

    started = time.perf_counter()
    con = connect_duckdb()
    try:
        # Step 1: language label
        print("Step 1: language label", flush=True)
        print("step 1 always writes the full data folder", flush=True)
        read_inputs(con)
        rebuilt = args.force or not labels_path.is_file()
        if rebuilt:
            label_all_skill_languages(con, labels_path)
        if _should_write_sample(sample_path, force=args.force or rebuilt):
            draw_language_check_sample(con, labels_path, sample_path)
        summary = summarize_languages(con, labels_path)
    finally:
        con.close()
    summary["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    summary["output"] = str(labels_path)
    summary["language_check"] = str(sample_path)
    _print_label_report(labels_path, summary)


def _should_write_sample(path: Path, *, force: bool) -> bool:
    """True when the hand-check CSV should be written.

    A missing file is written. ``--force`` rewrites it only while the human
    columns are empty. A filled sheet is left as it is. When that sheet is
    kept after the parquet was rebuilt, the message says it was drawn from
    the previous labels.
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
