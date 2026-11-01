"""Find template lines and build MinHash signatures for copy search.

What: stage 5, steps 1 and 2. Count lines shared by many owners, then
sign every English dataset text and every registry version after those
lines are removed.
Inputs: GitSkills artifacts, ``skill_language.parquet``,
``lineage_versions.parquet``, and registry history texts.
Outputs: ``boilerplate_lines.parquet`` and ``dataset_signatures/`` in the
full data folder (shared by dev and full), and ``registry_signatures.npz``
in the dev or full folder.
How to run: ``python -m skill_genealogy.find_copies [--dev] [--force]``.
``--only signatures`` runs this part. Rehost and copy links are later cards.
Dataset signatures are slow; run them in the background.
"""

from __future__ import annotations

import multiprocessing
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

import skill_genealogy.settings as settings
from skill_genealogy.file_helpers import (
    build_arg_parser,
    check_inputs_exist,
    connect_duckdb,
    print_sample_records,
    print_summary,
    read_parquet,
    write_parquet,
)
from skill_genealogy.text_helpers import (
    body_lines,
    has_comparable_text,
    make_minhash,
    make_word_shingles,
    minhash_to_array,
    normalize_text,
    remove_boilerplate_lines,
)

# One signature slice holds about a quarter of the distinct texts, same split
# as stage 2, so a group-by does not keep every skill string in memory.
TEXT_READ_PARTS = 4
# How many skill texts are exploded into lines at once. One query over every
# text spilled more than 100 GB because each line row kept the whole text.
LINE_BATCH_ROWS = 2_000
# Cap the spill so a bad group-by cannot fill the disk.
DUCKDB_TEMP_LIMIT = "40GB"
# One dataset .npz holds this many distinct English texts.
SIGNATURE_CHUNK_ROWS = 50_000
# DuckDB fetch_df_chunk counts vectors. One vector is about 2,048 rows.
DUCKDB_VECTOR_ROWS = 2048
# Lists shorter than this are signed in this process. Tests stay on that path.
POOL_MIN_TEXTS = 512
# How many tasks each worker takes at once from the pool.
POOL_CHUNKSIZE = 32
# Top template lines printed for the eye check.
TOP_LINE_COUNT = 30
# Kept lines nearest the owner cutoff, printed with the top list.
CUTOFF_LINE_COUNT = 5
# Empty-text examples stored in each .npz and printed in the summary.
EMPTY_EXAMPLE_COUNT = 2
# Preview length stored with an empty-text example.
EMPTY_PREVIEW_CHARS = 80
# Dataset part files. A partial write uses a different name so a crash cannot
# be loaded as a finished part.
PART_GLOB = "part_s??_c?????.npz"
# DuckDB spills a large group-by here instead of using all of the machine's RAM.
DUCKDB_MEMORY_LIMIT = "8GB"

_WORKER_BOILERPLATE: set[str] = set()

# Query Q5.1, see QUERIES.md
# Owners are read once, without the skill text. Lines are exploded from a
# small batch so a line row does not keep the whole text in memory.
FILE_OWNERS_SQL = """
SELECT DISTINCT
    artifacts.file_sha AS file_sha,
    split_part(artifacts.repo_full_name, '/', 1) AS owner
FROM artifacts
JOIN skill_language ON artifacts.file_sha = skill_language.file_sha
WHERE skill_language.is_english
  AND artifacts.file_sha IS NOT NULL
  AND artifacts.repo_full_name IS NOT NULL
  AND artifacts.repo_full_name <> ''
"""

# Query Q5.1, see QUERIES.md
# `batch_lines` is file_sha and the body-line key. The key is built in Python
# with body_lines and strip().lower(), the same key signature_for_text removes.
LINE_BATCH_SQL = """
SELECT DISTINCT file_owners.owner AS owner, batch_lines.line AS line
FROM batch_lines
JOIN file_owners ON batch_lines.file_sha = file_owners.file_sha
"""

# Query Q5.2, see QUERIES.md
# Same texts as ENGLISH_SLICE_TEXTS_SQL, without ORDER BY, so the owner pass
# can stream a slice instead of sorting every string first.
LINE_TEXT_SQL = """
SELECT artifacts.file_sha AS file_sha, any_value(artifacts.content) AS content
FROM artifacts
JOIN skill_language ON artifacts.file_sha = skill_language.file_sha
WHERE skill_language.is_english
  AND artifacts.file_sha IS NOT NULL
  AND artifacts.content IS NOT NULL
  AND hash(artifacts.file_sha) % {parts} = {part}
GROUP BY artifacts.file_sha
"""

# Query Q5.1, see QUERIES.md
LINE_OWNER_ROLLUP_SQL = """
SELECT line, count(DISTINCT owner)::BIGINT AS owners
FROM read_parquet({glob})
GROUP BY line
HAVING count(DISTINCT owner) >= {min_owners}
ORDER BY owners DESC, line
"""

# Query Q5.2, see QUERIES.md
ENGLISH_SLICE_COUNT_SQL = """
SELECT count(*)::BIGINT AS texts
FROM (
    SELECT artifacts.file_sha
    FROM artifacts
    JOIN skill_language ON artifacts.file_sha = skill_language.file_sha
    WHERE skill_language.is_english
      AND artifacts.file_sha IS NOT NULL
      AND artifacts.content IS NOT NULL
      AND hash(artifacts.file_sha) % {parts} = {part}
    GROUP BY artifacts.file_sha
)
"""

# Query Q5.2, see QUERIES.md
ENGLISH_SLICE_TEXTS_SQL = """
SELECT artifacts.file_sha AS file_sha, any_value(artifacts.content) AS content
FROM artifacts
JOIN skill_language ON artifacts.file_sha = skill_language.file_sha
WHERE skill_language.is_english
  AND artifacts.file_sha IS NOT NULL
  AND artifacts.content IS NOT NULL
  AND hash(artifacts.file_sha) % {parts} = {part}
GROUP BY artifacts.file_sha
ORDER BY artifacts.file_sha
"""

# Query Q5.3, see QUERIES.md
REGISTRY_TEXTS_SQL = """
SELECT lower(sha) AS sha, any_value(text) AS text
FROM read_parquet(?)
WHERE lower(sha) IN (SELECT sha FROM wanted)
GROUP BY lower(sha)
"""


def _quote_path(path: str) -> str:
    """Return a single-quoted SQL string with quotes escaped."""
    return "'" + path.replace("'", "''") + "'"


def _language_path() -> Path:
    """Return ``skill_language.parquet`` in the full data folder."""
    return settings.data_dir(False) / "skill_language.parquet"


def _boilerplate_path() -> Path:
    """Return the shared boilerplate file in the full data folder."""
    return settings.data_dir(False) / "boilerplate_lines.parquet"


def _dataset_dir() -> Path:
    """Return the shared dataset-signature folder in the full data folder."""
    return settings.data_dir(False) / "dataset_signatures"


def _registry_path(dev: bool) -> Path:
    """Return ``registry_signatures.npz`` for this mode."""
    return settings.data_dir(dev) / "registry_signatures.npz"


def _versions_path(dev: bool) -> Path:
    """Return ``lineage_versions.parquet`` for this mode."""
    return settings.data_dir(dev) / "lineage_versions.parquet"


def _texts_path(dev: bool) -> Path:
    """Return the registry history texts parquet for this mode."""
    return settings.data_dir(dev) / "registry_history" / "registry_history_texts.parquet"


def _register_view(con: duckdb.DuckDBPyConnection, name: str, path: str) -> None:
    """Point ``name`` at a parquet file or a glob."""
    quoted = _quote_path(path)
    con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet({quoted})")


def _prepare_duckdb(con: duckdb.DuckDBPyConnection, temp_dir: Path) -> None:
    """Turn off the progress bar and give the group-by a spill folder.

    ``preserve_insertion_order`` stays off so a group-by does not keep the
    input order. Queries that need an order say ``ORDER BY``.
    """
    temp_dir.mkdir(parents=True, exist_ok=True)
    con.execute("SET enable_progress_bar = false")
    con.execute("SET preserve_insertion_order = false")
    con.execute(f"SET memory_limit = '{DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET max_temp_directory_size = '{DUCKDB_TEMP_LIMIT}'")
    con.execute(f"SET temp_directory = {_quote_path(str(temp_dir))}")


def read_inputs(con: duckdb.DuckDBPyConnection, dev: bool) -> None:
    """Check the stage inputs and register the dataset views.

    Inputs: an open connection and ``dev``. Registry files follow ``dev``.
    Artifacts and ``skill_language.parquet`` are always the full dataset.
    Outputs: views named ``artifacts`` and ``skill_language``.
    Raises ``FileNotFoundError`` when a file is missing. The message names
    the command that builds it.
    """
    flag = " --dev" if dev else ""
    check_inputs_exist(
        {
            "artifacts": settings.ARTIFACTS_GLOB,
            "skill_language": _language_path(),
            "lineage_versions": _versions_path(dev),
            "registry_history_texts": _texts_path(dev),
        },
        {
            "artifacts": "python src/data/download_data.py",
            "skill_language": "python -m skill_genealogy.select_registries --force-languages",
            "lineage_versions": f"python -m skill_genealogy.build_registry_lineages{flag}",
            "registry_history_texts": f"python -m skill_genealogy.download_registry_history{flag}",
        },
    )
    _register_view(con, "artifacts", settings.ARTIFACTS_GLOB)
    _register_view(con, "skill_language", str(_language_path()))


def _owner_floor(con: duckdb.DuckDBPyConnection) -> int:
    """Return ``line_owner_min.min_owners``, or 1 when that table is absent.

    ``main`` creates the table so the full dataset does not load one-owner
    lines into pandas. A connection without it returns every line.
    """
    found = con.execute(
        """
        SELECT count(*) FROM information_schema.tables
        WHERE table_name = 'line_owner_min'
        """
    ).fetchone()
    if found is None or int(found[0]) == 0:
        return 1
    row = con.execute("SELECT min(min_owners) FROM line_owner_min").fetchone()
    if row is None or row[0] is None:
        return 1
    floor = int(row[0])
    if floor < 1:
        raise ValueError(f"line_owner_min.min_owners must be >= 1, got {floor}")
    return floor


def _empty_line_frame() -> pd.DataFrame:
    """An empty line/owners table with the columns the later steps expect."""
    return pd.DataFrame(
        {"line": pd.Series(dtype="object"), "owners": pd.Series(dtype="int64")}
    )


def _open_owner_writer(owners_path: Path) -> duckdb.DuckDBPyConnection:
    """Open a second connection that only sees the owner table.

    The text scan stays on the caller's connection. A second query on that
    same connection would cancel the scan.
    """
    writer = connect_duckdb()
    writer.execute("SET enable_progress_bar = false")
    quoted = _quote_path(str(owners_path))
    writer.execute(f"CREATE TABLE file_owners AS SELECT * FROM read_parquet({quoted})")
    return writer


def _body_line_keys(text: str) -> list[str]:
    """Return the line keys ``signature_for_text`` can remove.

    Each key is ``line.strip().lower()`` on a ``body_lines`` row, so a curly
    apostrophe and a straight one share one key. Lines with fewer than
    ``BOILERPLATE_MIN_WORDS`` words are left out.
    """
    minimum = settings.BOILERPLATE_MIN_WORDS
    keys: list[str] = []
    for line in body_lines(text):
        key = line.strip().lower()
        if len(key.split()) >= minimum:
            keys.append(key)
    return keys


def _batch_line_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per file and body-line key in this batch of texts."""
    rows: list[tuple[str, str]] = []
    shas = frame["file_sha"].tolist()
    contents = frame["content"].tolist()
    for file_sha, content in zip(shas, contents, strict=True):
        sha = "" if file_sha is None else str(file_sha)
        rows.extend((sha, key) for key in _body_line_keys(_as_text(content)))
    if not rows:
        empty = pd.Series(dtype="object")
        return pd.DataFrame({"file_sha": empty, "line": empty})
    return pd.DataFrame(rows, columns=["file_sha", "line"]).drop_duplicates()


def _write_line_batch(
    writer: duckdb.DuckDBPyConnection, frame: pd.DataFrame, dest: Path
) -> None:
    """Write distinct (owner, line) pairs for one small batch of texts."""
    lines = _batch_line_frame(frame)
    if lines.empty:
        return
    writer.register("batch_lines", lines)
    try:
        quoted = _quote_path(str(dest))
        writer.execute(
            f"COPY ({LINE_BATCH_SQL}) TO {quoted} (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        writer.unregister("batch_lines")


def _write_line_slice(
    reader: duckdb.DuckDBPyConnection,
    writer: duckdb.DuckDBPyConnection,
    part: int,
    folder: Path,
    batch_index: int,
) -> int:
    """Explode one hash slice into pair files. Return the next batch index."""
    print(f"line owners slice {part + 1} of {TEXT_READ_PARTS}", flush=True)
    sql = LINE_TEXT_SQL.format(parts=TEXT_READ_PARTS, part=part)
    result = reader.execute(sql)
    for frame in _iter_text_chunks(result, LINE_BATCH_ROWS):
        dest = folder / f"pairs_{batch_index:05d}.parquet"
        _write_line_batch(writer, frame, dest)
        print(
            f"line owners slice {part + 1} batch {batch_index}: {len(frame)} texts",
            flush=True,
        )
        batch_index += 1
    return batch_index


# Algorithm A22, see CODE_FLOW.md
def count_owners_per_line(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Count distinct GitHub owners for each line of an English skill.

    The owner is the text before the first ``/`` in ``repo_full_name``.
    The line key is ``line.strip().lower()`` on each ``body_lines`` row, the
    same key ``signature_for_text`` removes. Empty lines and lines with fewer
    than ``BOILERPLATE_MIN_WORDS`` words are dropped.
    One owner who has the line twice still counts as one. A text with
    ``is_english`` false is not counted.

    The connection must already have ``artifacts`` and ``skill_language``.
    When it also has a table ``line_owner_min(min_owners)``, lines below
    that count are left out. Without that table every remaining line is
    returned, highest owner count first.

    Inputs: an open DuckDB connection.
    Outputs: columns ``line`` and ``owners``.

    Example:
    >>> con = connect_duckdb()
    >>> _ = con.execute(
    ...     "CREATE TABLE artifacts (repo_full_name VARCHAR, file_sha VARCHAR, content VARCHAR)"
    ... )
    >>> _ = con.execute("INSERT INTO artifacts VALUES ('a/one', 's1', 'use this shared skill')")
    >>> _ = con.execute("INSERT INTO artifacts VALUES ('b/two', 's2', 'use this shared skill')")
    >>> _ = con.execute(
    ...     "CREATE TABLE skill_language (file_sha VARCHAR, is_english BOOLEAN)"
    ... )
    >>> _ = con.execute("INSERT INTO skill_language VALUES ('s1', true), ('s2', true)")
    >>> frame = count_owners_per_line(con)  # doctest: +ELLIPSIS
    line owners slice 1 of 4
    ...
    >>> int(frame.loc[frame["line"] == "use this shared skill", "owners"].iloc[0])
    2
    """
    con.execute("SET enable_progress_bar = false")
    with tempfile.TemporaryDirectory(prefix="skillsig_line_owners_") as folder:
        root = Path(folder)
        _collect_owner_pairs(con, root)
        return _rollup_line_owners(con, root, _owner_floor(con))


def _collect_owner_pairs(con: duckdb.DuckDBPyConnection, root: Path) -> None:
    """Write one pair file per batch of English texts under ``root``."""
    owners_path = root / "file_owners.parquet"
    quoted_owners = _quote_path(str(owners_path))
    con.execute(
        f"COPY ({FILE_OWNERS_SQL}) TO {quoted_owners} (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    writer = _open_owner_writer(owners_path)
    try:
        batch_index = 0
        for part in range(TEXT_READ_PARTS):
            batch_index = _write_line_slice(con, writer, part, root, batch_index)
    finally:
        writer.close()


def _open_rollup_connection(temp_dir: Path) -> duckdb.DuckDBPyConnection:
    """Open a connection whose buffer pool is not already full of skill text.

    The scan connection holds the artifact cache. Counting lines on that same
    pool ran out of the 8 GB limit after the walk.
    """
    rollup = connect_duckdb()
    rollup.execute("SET enable_progress_bar = false")
    rollup.execute("SET preserve_insertion_order = false")
    rollup.execute("SET threads = 2")
    rollup.execute(f"SET memory_limit = '{DUCKDB_MEMORY_LIMIT}'")
    rollup.execute(f"SET max_temp_directory_size = '{DUCKDB_TEMP_LIMIT}'")
    temp_dir.mkdir(parents=True, exist_ok=True)
    rollup.execute(f"SET temp_directory = {_quote_path(str(temp_dir))}")
    return rollup


def _rollup_line_owners(
    con: duckdb.DuckDBPyConnection, root: Path, minimum: int
) -> pd.DataFrame:
    """Count distinct owners per line and return the rows at or above ``minimum``."""
    written = list(root.glob("pairs_*.parquet"))
    if not written:
        return _empty_line_frame()
    con.execute("SET memory_limit = '1GB'")
    print("line owners rollup", flush=True)
    rollup = _open_rollup_connection(root / "rollup_temp")
    try:
        glob = _quote_path(str(root / "pairs_*.parquet"))
        sql = LINE_OWNER_ROLLUP_SQL.format(glob=glob, min_owners=minimum)
        frame = rollup.execute(sql).df()
    finally:
        rollup.close()
        con.execute(f"SET memory_limit = '{DUCKDB_MEMORY_LIMIT}'")
    if frame.empty:
        return _empty_line_frame()
    frame["line"] = frame["line"].astype(str)
    frame["owners"] = frame["owners"].astype("int64")
    return frame.sort_values(
        ["owners", "line"], ascending=[False, True], kind="mergesort"
    ).reset_index(drop=True)


def build_boilerplate_set(line_owners: pd.DataFrame, min_owners: int) -> set[str]:
    """Keep lines shared by at least ``min_owners`` owners.

    Inputs: the frame from ``count_owners_per_line``, and the cutoff.
    Outputs: the lowercase line strings. An empty frame returns an empty set.

    Example:
    >>> sample = pd.DataFrame({"line": ["use this skill", "only once here"], "owners": [50, 2]})
    >>> build_boilerplate_set(sample, 50) == {"use this skill"}
    True
    """
    if min_owners < 1:
        raise ValueError(f"min_owners must be >= 1, got {min_owners}")
    if line_owners.empty:
        return set()
    if "line" not in line_owners.columns or "owners" not in line_owners.columns:
        raise ValueError("line_owners needs columns line and owners")
    kept = line_owners.loc[line_owners["owners"] >= min_owners, "line"]
    return {str(line) for line in kept.tolist()}


def signature_for_text(text: str, boilerplate: set[str]) -> tuple[np.ndarray, int]:
    """Build one MinHash signature and the shingle count for a skill body.

    The text is normalised, front matter is removed, boilerplate lines are
    dropped, and the remaining words become 5-word shingles. The array is
    ``uint64`` of length 128. A body with no shingles left returns a zero
    array and a count of 0. That array is not a signature: callers must
    not store it. Two texts that differ only by a boilerplate line get the
    same array and the same count.

    Inputs: the raw skill text, and the lowercase boilerplate lines.
    Outputs: ``(hashvalues, shingle_count)``.

    Example:
    >>> lines = {"please install the tool"}
    >>> left_values, left_size = signature_for_text(
    ...     "alpha beta gamma delta epsilon\\nPlease install the tool", lines
    ... )
    >>> right_values, right_size = signature_for_text(
    ...     "alpha beta gamma delta epsilon", lines
    ... )
    >>> left_size == right_size == 1 and bool(np.array_equal(left_values, right_values))
    True
    >>> signature_for_text("Please install the tool", lines)[1]
    0
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    kept = remove_boilerplate_lines(body_lines(text), boilerplate)
    shingles = make_word_shingles("\n".join(kept))
    if not has_comparable_text(shingles):
        return np.zeros(settings.MINHASH_NUM_PERM, dtype=np.uint64), 0
    return minhash_to_array(make_minhash(shingles)), len(shingles)


def _as_text(value: object) -> str:
    """Return a skill string, or ``""`` when the cell is missing."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        return str(value)
    return ""


def _preview(text: str) -> str:
    """One short line of the normalised text, for an empty-signature example."""
    flat = normalize_text(text).replace("\n", " ")
    if len(flat) <= EMPTY_PREVIEW_CHARS:
        return flat
    return flat[:EMPTY_PREVIEW_CHARS]


def _init_sign_worker(lines: list[str]) -> None:
    """Store the boilerplate set in this worker process."""
    global _WORKER_BOILERPLATE
    _WORKER_BOILERPLATE = set(lines)


def _sign_text(text: str) -> tuple[np.ndarray, int]:
    """Sign one text with the boilerplate set stored by ``_init_sign_worker``."""
    return signature_for_text(text, _WORKER_BOILERPLATE)


def _worker_functions():
    """Return the pool worker callables from the importable module.

    ``python -m`` runs this file as ``__main__``. A spawned worker imports
    ``skill_genealogy.find_copies``, not ``__main__``, so the pool must call
    the functions on that module.
    """
    import skill_genealogy.find_copies as signing

    return signing._init_sign_worker, signing._sign_text


def _open_sign_pool(
    boilerplate: set[str], workers: int | None = None
) -> multiprocessing.pool.Pool:
    """Start workers that call ``signature_for_text`` with this boilerplate set."""
    init, _sign = _worker_functions()
    count = settings.DOWNLOAD_WORKERS if workers is None else workers
    return multiprocessing.Pool(
        processes=count,
        initializer=init,
        initargs=(list(boilerplate),),
    )


def _sign_many(
    texts: list[str],
    boilerplate: set[str],
    pool: multiprocessing.pool.Pool | None = None,
) -> list[tuple[np.ndarray, int]]:
    """Sign each text. A long list uses a process pool. Order is kept.

    The pool is the same idea as the language label step: each worker calls
    ``signature_for_text``. Fewer than ``POOL_MIN_TEXTS`` texts stay in this
    process, which is the path the tests use. Pass ``pool`` to reuse workers
    across chunks.
    """
    if len(texts) < POOL_MIN_TEXTS:
        return [signature_for_text(text, boilerplate) for text in texts]
    sign = _worker_functions()[1]
    if pool is not None:
        return pool.map(sign, texts, chunksize=POOL_CHUNKSIZE)
    workers = min(settings.DOWNLOAD_WORKERS, len(texts))
    with _open_sign_pool(boilerplate, workers) as owned:
        return owned.map(sign, texts, chunksize=POOL_CHUNKSIZE)


def _part_path(out_dir: Path, part: int, chunk: int) -> Path:
    """Path of one dataset signature part."""
    return out_dir / f"part_s{part:02d}_c{chunk:05d}.npz"


def _signature_paths(path: Path) -> list[Path]:
    """The one file, or every finished dataset part in a folder, in name order."""
    if path.is_dir():
        return sorted(path.glob(PART_GLOB))
    if path.is_file():
        return [path]
    return []


def _ids_from_array(values: np.ndarray) -> list[str]:
    """Turn a saved id array into a list of strings."""
    loaded = np.asarray(values)
    if loaded.ndim == 0:
        return [str(loaded)]
    return [str(item) for item in loaded.reshape(-1).tolist()]


def _write_signature_file(
    path: Path,
    ids: list[str],
    signatures: np.ndarray,
    sizes: np.ndarray,
    empty_count: int,
    empty_examples: list[str],
) -> None:
    """Write one signature ``.npz``. A crash leaves a hidden partial file.

    Arrays are ``ids``, ``signatures`` uint64 ``[n, 128]``, ``sizes`` int32,
    plus ``empty_count`` and up to two ``empty_examples``. The extra two
    arrays are not returned by ``load_signatures``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.tmp.npz")
    np.savez_compressed(
        temporary,
        ids=np.asarray(ids, dtype=str),
        signatures=np.asarray(signatures, dtype=np.uint64),
        sizes=np.asarray(sizes, dtype=np.int32),
        empty_count=np.asarray([empty_count], dtype=np.int64),
        empty_examples=np.asarray(empty_examples[:EMPTY_EXAMPLE_COUNT], dtype=str),
    )
    temporary.replace(path)


def _pack_signatures(
    ids: list[str],
    signed: list[tuple[np.ndarray, int]],
) -> tuple[list[str], np.ndarray, np.ndarray, int, list[str]]:
    """Split stored signatures from texts that have no shingles left.

    ``signed`` is ``(hashvalues, shingle_count)`` in the same order as ``ids``.
    A count of 0 is empty: it is counted, and its id is kept for the example
    list. The zero array is not stored.
    """
    kept_ids: list[str] = []
    kept_values: list[np.ndarray] = []
    kept_sizes: list[int] = []
    empty_count = 0
    empty_examples: list[str] = []
    for identifier, (values, size) in zip(ids, signed, strict=True):
        if size <= 0:
            empty_count += 1
            if len(empty_examples) < EMPTY_EXAMPLE_COUNT:
                empty_examples.append(identifier)
            continue
        kept_ids.append(identifier)
        kept_values.append(np.asarray(values, dtype=np.uint64))
        kept_sizes.append(size)
    if kept_values:
        matrix = np.vstack(kept_values).astype(np.uint64, copy=False)
    else:
        matrix = np.zeros((0, settings.MINHASH_NUM_PERM), dtype=np.uint64)
    sizes = np.asarray(kept_sizes, dtype=np.int32)
    return kept_ids, matrix, sizes, empty_count, empty_examples


def _vectors_per_chunk(chunk_rows: int) -> int:
    """How many DuckDB vectors cover about ``chunk_rows`` rows."""
    if chunk_rows < 1:
        raise ValueError(f"chunk_rows must be >= 1, got {chunk_rows}")
    return max(1, (chunk_rows + DUCKDB_VECTOR_ROWS - 1) // DUCKDB_VECTOR_ROWS)


def _iter_text_chunks(
    result: duckdb.DuckDBPyConnection, chunk_rows: int
) -> Iterator[pd.DataFrame]:
    """Yield frames of about ``chunk_rows`` from one ordered query."""
    vectors = _vectors_per_chunk(chunk_rows)
    pending: list[pd.DataFrame] = []
    pending_rows = 0
    while True:
        fetched = result.fetch_df_chunk(vectors)
        if fetched is None or len(fetched) == 0:
            break
        pending.append(fetched)
        pending_rows += len(fetched)
        while pending_rows >= chunk_rows:
            block = pd.concat(pending, ignore_index=True)
            yield block.iloc[:chunk_rows].reset_index(drop=True).copy()
            rest = block.iloc[chunk_rows:].reset_index(drop=True)
            pending = [rest] if len(rest) else []
            pending_rows = len(rest)
    if pending_rows:
        yield pd.concat(pending, ignore_index=True)


def _count_slice(con: duckdb.DuckDBPyConnection, part: int) -> int:
    """How many distinct English texts fall in this hash slice."""
    sql = ENGLISH_SLICE_COUNT_SQL.format(parts=TEXT_READ_PARTS, part=part)
    row = con.execute(sql).fetchone()
    if row is None:
        return 0
    return int(row[0])


def _sign_chunk(
    frame: pd.DataFrame,
    boilerplate: set[str],
    path: Path,
    pool: multiprocessing.pool.Pool | None = None,
) -> tuple[int, int]:
    """Sign one chunk and write its part file. Return signed and empty counts."""
    file_shas = [str(value) for value in frame["file_sha"].tolist()]
    texts = [_as_text(value) for value in frame["content"].tolist()]
    signed = _sign_many(texts, boilerplate, pool)
    kept_ids, matrix, sizes, empty_count, _stored = _pack_signatures(file_shas, signed)
    examples: list[str] = []
    for file_sha, text, (_values, size) in zip(file_shas, texts, signed, strict=True):
        if size > 0:
            continue
        examples.append(f"{file_sha} | {_preview(text)}")
        if len(examples) >= EMPTY_EXAMPLE_COUNT:
            break
    _write_signature_file(path, kept_ids, matrix, sizes, empty_count, examples)
    return len(kept_ids), empty_count


def _slice_is_complete(out_dir: Path, part: int, n_chunks: int) -> bool:
    """True when every chunk file for this slice is already on disk."""
    if n_chunks < 1:
        return True
    return all(_part_path(out_dir, part, index).is_file() for index in range(n_chunks))


def _sign_slice(
    con: duckdb.DuckDBPyConnection,
    boilerplate: set[str],
    out_dir: Path,
    part: int,
    pool_holder: list[multiprocessing.pool.Pool | None],
) -> None:
    """Sign one hash slice, skipping chunk files that already exist.

    The first large chunk starts the pool in ``pool_holder`` and later chunks
    reuse it.
    """
    n_texts = _count_slice(con, part)
    if n_texts == 0:
        print(f"dataset slice {part + 1}/{TEXT_READ_PARTS}: 0 texts", flush=True)
        return
    n_chunks = (n_texts + SIGNATURE_CHUNK_ROWS - 1) // SIGNATURE_CHUNK_ROWS
    if _slice_is_complete(out_dir, part, n_chunks):
        print(
            f"dataset slice {part + 1}/{TEXT_READ_PARTS}: skip {n_chunks} chunks",
            flush=True,
        )
        return
    print(
        f"dataset slice {part + 1}/{TEXT_READ_PARTS}: {n_texts} texts, {n_chunks} chunks",
        flush=True,
    )
    sql = ENGLISH_SLICE_TEXTS_SQL.format(parts=TEXT_READ_PARTS, part=part)
    written = _sign_slice_chunks(
        con.execute(sql), boilerplate, out_dir, part, pool_holder
    )
    if written != n_chunks:
        print(
            f"warning: slice {part + 1} wrote {written} chunks, count said {n_chunks}",
            flush=True,
        )


def _sign_slice_chunks(
    result: duckdb.DuckDBPyConnection,
    boilerplate: set[str],
    out_dir: Path,
    part: int,
    pool_holder: list[multiprocessing.pool.Pool | None],
) -> int:
    """Sign each chunk of one open text query. Return how many chunks were seen."""
    written = 0
    for frame in _iter_text_chunks(result, SIGNATURE_CHUNK_ROWS):
        path = _part_path(out_dir, part, written)
        if path.is_file():
            print(
                f"dataset slice {part + 1}/{TEXT_READ_PARTS} chunk {written}: skip",
                flush=True,
            )
            written += 1
            continue
        _sign_one_chunk(frame, boilerplate, path, part, written, pool_holder)
        written += 1
    return written


def _sign_one_chunk(
    frame: pd.DataFrame,
    boilerplate: set[str],
    path: Path,
    part: int,
    written: int,
    pool_holder: list[multiprocessing.pool.Pool | None],
) -> None:
    """Sign one chunk, starting the worker pool on the first large chunk."""
    started = time.perf_counter()
    if len(frame) >= POOL_MIN_TEXTS and pool_holder[0] is None:
        pool_holder[0] = _open_sign_pool(boilerplate)
    signed, empty = _sign_chunk(frame, boilerplate, path, pool_holder[0])
    elapsed = time.perf_counter() - started
    print(
        f"dataset slice {part + 1}/{TEXT_READ_PARTS} chunk {written}: "
        f"{len(frame)} texts, signed {signed}, empty {empty}, {elapsed:.1f}s",
        flush=True,
    )


def build_dataset_signatures(
    con: duckdb.DuckDBPyConnection, boilerplate: set[str], out_dir: Path
) -> int:
    """Sign every distinct English dataset text and return how many were stored.

    Texts are read in four hash slices and written in chunks of 50,000.
    A chunk file that already exists is skipped, so a stopped run can
    continue. ``--force`` deletes the files before this function is called.
    A text with no shingles after boilerplate removal is counted in the
    file and is not given a signature.

    Inputs: a connection with ``artifacts`` and ``skill_language``, the
    boilerplate set, and the output folder.
    Outputs: ``part_sXX_cYYYYY.npz`` files, and the number of stored signatures.
    """
    con.execute("SET enable_progress_bar = false")
    out_dir.mkdir(parents=True, exist_ok=True)
    pool_holder: list[multiprocessing.pool.Pool | None] = [None]
    try:
        for part in range(TEXT_READ_PARTS):
            _sign_slice(con, boilerplate, out_dir, part, pool_holder)
    finally:
        pool = pool_holder[0]
        if pool is not None:
            pool.close()
            pool.join()
    signed, _empty, _examples = _read_empty_report(out_dir)
    return signed


def _has_blob(sha: str) -> bool:
    """False for a blank sha and for git's all-zero add/delete sha."""
    if sha == "" or sha.lower() in {"none", "nan", "<na>"}:
        return False
    return set(sha) != {"0"}


def _clean(value: object) -> str:
    """String value, or ``""`` when the cell is missing."""
    if isinstance(value, str):
        return value
    return ""


def _load_blob_texts(dev: bool, blobs: list[str]) -> dict[str, str]:
    """Return raw text for the blob shas, keyed by lowercase sha.

    Query Q5.3. Only the shas in ``blobs`` are read.
    """
    wanted = sorted({blob.lower() for blob in blobs if _has_blob(blob)})
    if not wanted:
        return {}
    con = connect_duckdb()
    try:
        con.execute("CREATE TEMP TABLE wanted (sha VARCHAR)")
        con.register("wanted_frame", pd.DataFrame({"sha": wanted}))
        con.execute("INSERT INTO wanted SELECT sha FROM wanted_frame")
        rows = con.execute(REGISTRY_TEXTS_SQL, [str(_texts_path(dev))]).fetchall()
    finally:
        con.close()
    found: dict[str, str] = {}
    for sha, text in rows:
        found[str(sha)] = text if isinstance(text, str) else ""
    return found


def _read_registry_versions(dev: bool) -> tuple[pd.DataFrame, dict[str, str]]:
    """Load lineage version rows and the blob text of each one."""
    versions = read_parquet(_versions_path(dev))
    needed = ["lineage_id", "version_seq", "blob"]
    missing = [name for name in needed if name not in versions.columns]
    if missing:
        raise ValueError(f"lineage_versions is missing columns: {', '.join(missing)}")
    blobs = [_clean(value) for value in versions["blob"].tolist()]
    return versions, _load_blob_texts(dev, blobs)


def _text_for_blob(texts: dict[str, str], blob: object) -> str:
    """Return the raw text for a blob sha, or ``""`` when it is missing."""
    sha = _clean(blob)
    if not _has_blob(sha):
        return ""
    if sha in texts:
        return texts[sha]
    return texts.get(sha.lower(), "")


def build_registry_signatures(
    versions: pd.DataFrame,
    texts: dict[str, str],
    boilerplate: set[str],
    out_path: Path,
) -> int:
    """Sign every lineage version and return how many signatures were stored.

    The id is ``lineage_id#version_seq``. A version with no shingles left
    is not stored. The rows stay in the order of ``versions``.

    Inputs: version rows (``lineage_id``, ``version_seq``, ``blob``), a map
    of blob sha to raw text, the boilerplate set, and the output path.
    Outputs: one ``.npz`` file, and the number of stored signatures.
    """
    needed = ["lineage_id", "version_seq", "blob"]
    missing = [name for name in needed if name not in versions.columns]
    if missing:
        raise ValueError(f"versions is missing columns: {', '.join(missing)}")
    identifiers: list[str] = []
    bodies: list[str] = []
    for row in versions.itertuples(index=False):
        identifiers.append(f"{_clean(row.lineage_id)}#{int(row.version_seq)}")
        bodies.append(_text_for_blob(texts, row.blob))
    signed = _sign_many(bodies, boilerplate)
    examples = [
        f"{identifier} | {_preview(body)}"
        for identifier, body, (_values, size) in zip(
            identifiers, bodies, signed, strict=True
        )
        if size <= 0
    ]
    kept_ids, matrix, sizes, empty_count, _stored = _pack_signatures(identifiers, signed)
    _write_signature_file(
        out_path,
        kept_ids,
        matrix,
        sizes,
        empty_count,
        examples[:EMPTY_EXAMPLE_COUNT],
    )
    return len(kept_ids)


def _load_one(path: Path) -> tuple[list[str], np.ndarray, np.ndarray]:
    """Load ids, signatures and sizes from one ``.npz`` file."""
    with np.load(path, allow_pickle=False) as data:
        ids = _ids_from_array(data["ids"])
        signatures = np.asarray(data["signatures"], dtype=np.uint64)
        sizes = np.asarray(data["sizes"], dtype=np.int32).reshape(-1)
    if signatures.size == 0:
        signatures = np.zeros((0, settings.MINHASH_NUM_PERM), dtype=np.uint64)
    if signatures.ndim != 2 or signatures.shape[1] != settings.MINHASH_NUM_PERM:
        raise ValueError(
            f"{path} signatures must have shape (n, {settings.MINHASH_NUM_PERM}), "
            f"got {signatures.shape}"
        )
    if signatures.shape[0] != len(ids) or sizes.shape[0] != len(ids):
        raise ValueError(f"{path} id, signature and size counts do not match")
    return ids, signatures, sizes


def load_signatures(path_or_dir: Path) -> tuple[list[str], np.ndarray, np.ndarray]:
    """Load ids, signatures and shingle counts from one file or a part folder.

    A folder is every ``part_sXX_cYYYYY.npz`` in name order. A missing path
    raises ``FileNotFoundError``. A folder with no part files returns empty
    arrays of the right shape. The signature matrix is uint64 ``[n, 128]``
    and the sizes are int32.

    Inputs: a ``.npz`` file or a directory.
    Outputs: ``(ids, signatures, sizes)``.

    The full dataset directory is about 1.6 GB of hash values. Pass one
    part file when the whole folder does not need to be in memory.
    """
    if not path_or_dir.exists():
        raise FileNotFoundError(path_or_dir)
    files = _signature_paths(path_or_dir)
    empty_signatures = np.zeros((0, settings.MINHASH_NUM_PERM), dtype=np.uint64)
    empty_sizes = np.zeros((0,), dtype=np.int32)
    if not files:
        return [], empty_signatures, empty_sizes
    ids: list[str] = []
    blocks: list[np.ndarray] = []
    size_blocks: list[np.ndarray] = []
    for path in files:
        file_ids, signatures, sizes = _load_one(path)
        ids.extend(file_ids)
        blocks.append(signatures)
        size_blocks.append(sizes)
    if not ids:
        return [], empty_signatures, empty_sizes
    return ids, np.vstack(blocks), np.concatenate(size_blocks)


def _read_empty_report(path: Path) -> tuple[int, int, list[str]]:
    """Return stored signatures, empty texts, and up to two examples.

    A missing path counts as zero. Signature matrices are not loaded.
    """
    signed = 0
    empty = 0
    examples: list[str] = []
    for file in _signature_paths(path):
        with np.load(file, allow_pickle=False) as data:
            signed += int(np.asarray(data["ids"]).shape[0])
            if "empty_count" in data.files:
                empty += int(np.asarray(data["empty_count"]).reshape(-1)[0])
            if "empty_examples" in data.files and len(examples) < EMPTY_EXAMPLE_COUNT:
                stored = np.asarray(data["empty_examples"]).reshape(-1).tolist()
                for item in stored:
                    if len(examples) >= EMPTY_EXAMPLE_COUNT:
                        break
                    examples.append(str(item))
    return signed, empty, examples


def _signature_sample(path: Path) -> pd.DataFrame:
    """The first two stored signatures as a small frame for the summary."""
    columns = ["id", "shingle_count", "hash0"]
    rows: list[dict[str, object]] = []
    for file in _signature_paths(path):
        ids, signatures, sizes = _load_one(file)
        for index, identifier in enumerate(ids):
            rows.append(
                {
                    "id": identifier,
                    "shingle_count": int(sizes[index]),
                    "hash0": int(signatures[index, 0]),
                }
            )
            if len(rows) >= 2:
                return pd.DataFrame(rows, columns=columns)
    return pd.DataFrame(rows, columns=columns)


def save_outputs(line_owners: pd.DataFrame, path: Path) -> None:
    """Write boilerplate lines, highest owner count first.

    Inputs: the line/owners frame and the destination parquet path.
    Outputs: that parquet file, columns ``line`` and ``owners``.
    """
    if line_owners.empty:
        frame = _empty_line_frame()
    else:
        frame = line_owners.loc[:, ["line", "owners"]].copy()
        frame["line"] = frame["line"].astype(str)
        frame["owners"] = frame["owners"].astype("int64")
        frame = frame.sort_values(
            ["owners", "line"], ascending=[False, True], kind="mergesort"
        ).reset_index(drop=True)
    write_parquet(frame, path)


def _ranked_lines(frame: pd.DataFrame) -> pd.DataFrame:
    """Sort lines by owner count, highest first, then by the line text."""
    if frame.empty:
        return frame
    return frame.sort_values(
        ["owners", "line"], ascending=[False, True], kind="mergesort"
    ).reset_index(drop=True)


def _print_boilerplate(line_owners: pd.DataFrame) -> None:
    """Print the top template lines and the lines nearest the cutoff."""
    ranked = _ranked_lines(line_owners)
    print(f"top {TOP_LINE_COUNT} boilerplate lines", flush=True)
    for _, row in ranked.head(TOP_LINE_COUNT).iterrows():
        print(f"{int(row.owners):6d}  {row.line}", flush=True)
    print("lines nearest the owner cutoff", flush=True)
    if ranked.empty:
        print("(none)", flush=True)
    else:
        start = max(0, len(ranked) - CUTOFF_LINE_COUNT)
        for _, row in ranked.iloc[start:].iterrows():
            print(f"{int(row.owners):6d}  {row.line}", flush=True)
    print_sample_records(ranked, "boilerplate sample")


def _delete_signature_outputs(dev: bool) -> None:
    """Remove signature outputs so ``--force`` builds them again.

    Dataset signatures and boilerplate lines are shared by dev and full,
    so this deletes those shared files even when ``dev`` is true.
    """
    print(
        "force: rebuilding shared boilerplate lines and dataset signatures",
        flush=True,
    )
    boilerplate = _boilerplate_path()
    if boilerplate.is_file():
        boilerplate.unlink()
    registry = _registry_path(dev)
    if registry.is_file():
        registry.unlink()
    folder = _dataset_dir()
    if not folder.is_dir():
        return
    for path in folder.glob(PART_GLOB):
        path.unlink()
    for path in folder.glob(".part_s*.tmp.npz"):
        path.unlink()


def _load_or_count_lines(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Return the boilerplate table, counting it only when the file is missing."""
    path = _boilerplate_path()
    if path.is_file():
        print("skip: boilerplate_lines.parquet already exists", flush=True)
        return read_parquet(path)
    con.execute("CREATE OR REPLACE TEMP TABLE line_owner_min (min_owners INTEGER)")
    con.execute("INSERT INTO line_owner_min VALUES (?)", [settings.BOILERPLATE_MIN_OWNERS])
    frame = count_owners_per_line(con)
    save_outputs(frame, path)
    return frame


def _load_or_sign_registry(dev: bool, boilerplate: set[str]) -> int:
    """Return the number of registry signatures, signing only if the file is missing."""
    path = _registry_path(dev)
    if path.is_file():
        print("skip: registry_signatures.npz already exists", flush=True)
        signed, _empty, _examples = _read_empty_report(path)
        return signed
    versions, texts = _read_registry_versions(dev)
    print(f"registry versions to sign: {len(versions)}", flush=True)
    return build_registry_signatures(versions, texts, boilerplate, path)


def _print_examples(title: str, examples: list[str]) -> None:
    """Print up to two empty-text examples, or a line when there are none."""
    print(title, flush=True)
    if not examples:
        print("(none)", flush=True)
        return
    for example in examples[:EMPTY_EXAMPLE_COUNT]:
        print(example, flush=True)


def _print_run_summary(
    line_owners: pd.DataFrame,
    dataset_dir: Path,
    registry_path: Path,
    elapsed: float,
) -> None:
    """Print counts, empty-text examples, and two signature records."""
    dataset_signed, dataset_empty, dataset_examples = _read_empty_report(dataset_dir)
    registry_signed, registry_empty, registry_examples = _read_empty_report(registry_path)
    print_summary(
        "signature summary",
        {
            "boilerplate_lines": len(line_owners),
            "boilerplate_min_owners": settings.BOILERPLATE_MIN_OWNERS,
            "dataset_texts_signed": dataset_signed,
            "dataset_texts_empty": dataset_empty,
            "registry_versions_signed": registry_signed,
            "registry_versions_empty": registry_empty,
            "seconds": round(elapsed, 1),
        },
    )
    _print_examples("dataset texts empty after boilerplate removal", dataset_examples)
    _print_examples("registry versions empty after boilerplate removal", registry_examples)
    print_sample_records(_signature_sample(dataset_dir), "dataset signature sample")
    print_sample_records(_signature_sample(registry_path), "registry signature sample")


def main() -> None:
    """Build boilerplate lines and MinHash signatures.

    ``--dev`` writes registry signatures to the dev folder. Boilerplate
    lines and dataset signatures always go to the full data folder, because
    they do not depend on which registries were sampled. ``--only rehost``
    and ``--only copies`` do nothing until a later card adds those parts.
    ``--force`` deletes the shared dataset signatures as well as this mode's
    registry file, then builds them again.
    """
    parser = build_arg_parser(
        "Find template lines and build MinHash signatures for copy search."
    )
    parser.add_argument(
        "--only",
        choices=["signatures", "rehost", "copies"],
        help="run one part; the default runs every part this stage has built",
    )
    args = parser.parse_args()
    if args.only in {"rehost", "copies"}:
        print(f"skip: {args.only} is added by a later card", flush=True)
        return
    print(
        "boilerplate and dataset signatures go to the full data folder; "
        "registry signatures follow --dev",
        flush=True,
    )
    started = time.perf_counter()
    if args.force:
        _delete_signature_outputs(bool(args.dev))
    temp_dir = Path(tempfile.mkdtemp(prefix="skillsig_find_copies_"))
    con = connect_duckdb()
    try:
        _prepare_duckdb(con, temp_dir)
        read_inputs(con, bool(args.dev))
        # Step 1: Count owners per line (algorithm A22, query Q5.1)
        print("Step 1: count owners per line", flush=True)
        line_owners = _load_or_count_lines(con)
        boilerplate = build_boilerplate_set(line_owners, settings.BOILERPLATE_MIN_OWNERS)
        _print_boilerplate(line_owners)
        # Step 2: Build MinHash signatures
        print(
            f"Step 2: build MinHash signatures, boilerplate lines {len(boilerplate)}",
            flush=True,
        )
        build_dataset_signatures(con, boilerplate, _dataset_dir())
        _load_or_sign_registry(bool(args.dev), boilerplate)
    finally:
        con.close()
        shutil.rmtree(temp_dir, ignore_errors=True)
    _print_run_summary(
        line_owners,
        _dataset_dir(),
        _registry_path(bool(args.dev)),
        time.perf_counter() - started,
    )


if __name__ == "__main__":
    main()
