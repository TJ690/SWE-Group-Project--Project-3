"""Find template lines, MinHash signatures, registry re-hosts, and copies.

What: stage 5. Count lines shared by many owners, sign every English
dataset text and every registry version, find lineages whose first
version was copied from an older lineage, then link dataset texts
outside the registries to a registry version.
Inputs: GitSkills artifacts and repos, ``skill_language.parquet``,
lineage tables, registry history texts, signatures, ``repo_roles_final``,
``s2_clusters.parquet``, and ``wild_history``.
Outputs: ``boilerplate_lines.parquet`` and ``dataset_signatures/`` in the
full data folder. In the dev or full folder: ``registry_signatures.npz``,
``lineage_roots.parquet``, ``registry_hubs.parquet``,
``repo_roles_final.parquet``, ``text_links.parquet``,
``copy_candidates.parquet``, and ``links_dropped.parquet``.
How to run: ``python -m skill_genealogy.find_copies [--dev] [--force]``.
``--only signatures`` runs steps 1 and 2. ``--only rehost`` runs step 3.
``--only copies`` runs steps 4–9. Dataset signatures are slow; run them
in the background.
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
from datasketch import MinHash, MinHashLSH

import skill_genealogy.settings as settings
from skill_genealogy.compare_texts import share_of_registry_lines_kept
from skill_genealogy.file_helpers import (
    build_arg_parser,
    check_inputs_exist,
    connect_duckdb,
    outputs_exist,
    print_sample_records,
    print_summary,
    read_parquet,
    write_parquet,
)
from skill_genealogy.text_helpers import (
    body_lines,
    estimate_containment,
    estimate_jaccard,
    has_comparable_text,
    make_minhash,
    make_word_shingles,
    minhash_from_array,
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


def _lineages_path(dev: bool) -> Path:
    """Return ``lineages.parquet`` for this mode."""
    return settings.data_dir(dev) / "lineages.parquet"


def _roots_path(dev: bool) -> Path:
    """Return ``lineage_roots.parquet`` for this mode."""
    return settings.data_dir(dev) / "lineage_roots.parquet"


def _registry_hubs_path(dev: bool) -> Path:
    """Return ``registry_hubs.parquet`` for this mode."""
    return settings.data_dir(dev) / "registry_hubs.parquet"


def _roles_final_path(dev: bool) -> Path:
    """Return ``repo_roles_final.parquet`` for this mode."""
    return settings.data_dir(dev) / "repo_roles_final.parquet"


def _repo_roles_path() -> Path:
    """Return the stage-2 role table in the full data folder.

    Dev and full rehost runs both read this file. Stage 2 writes one row
    per dataset repo, and the dated hub test only replaces registry rows.
    """
    return settings.data_dir(False) / "repo_roles.parquet"


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


_CANDIDATE_COLUMNS = (
    "version_id",
    "lineage_id",
    "repo",
    "version_seq",
    "release_date",
    "blob",
    "jaccard",
    "containment",
)
_REHOST_COLUMNS = (
    "lineage_id",
    "copied_from_lineage",
    "copied_from_seq",
    "rehost_link",
    "rehost_similarity",
    "release_date",
)
_ROOT_COLUMNS = (
    "lineage_id",
    "copied_from_lineage",
    "copied_from_seq",
    "rehost_link",
    "rehost_similarity",
    "root_lineage",
    "is_original",
    "chain_length",
    "chain_cycle",
)
_HUB_COLUMNS = (
    "repo",
    "english_lineages",
    "rehosted_lineages",
    "hub_share",
    "is_hub",
    "hub_reason",
)
_ROLE_FIELDS = ("repo", "is_registry", "is_hub", "hub_share", "repo_role")


def _require_columns(frame: pd.DataFrame, columns: tuple[str, ...], label: str) -> None:
    """Raise ``ValueError`` when ``frame`` is missing any of ``columns``."""
    missing = [name for name in columns if name not in frame.columns]
    if missing:
        raise ValueError(f"{label} is missing columns: {', '.join(missing)}")


def _version_id(lineage_id: str, version_seq: int) -> str:
    """Return the signature id ``lineage_id#version_seq``."""
    return f"{lineage_id}#{int(version_seq)}"


def _parse_time(value: object) -> pd.Timestamp:
    """Return a UTC timestamp, or NaT when the value is missing or invalid."""
    if value is None:
        return pd.NaT
    try:
        if pd.isna(value):
            return pd.NaT
    except TypeError:
        pass
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if isinstance(parsed, pd.Timestamp):
        return parsed
    return pd.NaT


def _as_float(value: object) -> float:
    """Return a float, or 0 when the cell is missing."""
    if value is None:
        return 0.0
    try:
        if pd.isna(value):
            return 0.0
    except TypeError:
        pass
    return float(value)


def _as_optional_int(value: object) -> int | None:
    """Return an int, or None when the cell is missing."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    return int(value)


def _parent_id(value: object) -> str | None:
    """Return a lineage id, or None when the cell has no source."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    text = str(value).strip()
    if text == "" or text.lower() in {"none", "nan", "<na>"}:
        return None
    return text


def _blob_key(value: object) -> str:
    """Return the lowercase blob sha, or ``""`` when it is not a real blob."""
    text = _clean(value).strip().lower()
    if not _has_blob(text):
        return ""
    return text


def _containment_of_source(jaccard: float, source_size: int, copy_size: int) -> float:
    """Estimate the share of the older text that sits inside the newer text.

    The source size is the registry size in ``estimate_containment``. A
    source of 4 shingles inside a copy of 8, at Jaccard 0.5, is fully
    contained.

    Example:
    >>> _containment_of_source(0.5, 4, 8)
    1.0
    """
    return estimate_containment(jaccard, source_size, copy_size)


def _query_lsh(index: MinHashLSH, sketch: MinHash) -> list[str]:
    """Return ids whose estimated Jaccard clears this index's threshold.

    datasketch checks bands, not every stored signature, so a true neighbour
    can be missing. Callers still score the ids that come back.
    """
    return [str(key) for key in index.query(sketch)]


def build_registry_lsh(
    registry_ids: list[str], signatures: np.ndarray, threshold: float
) -> MinHashLSH:
    """Index version signatures so a query can find near copies.

    datasketch ``MinHashLSH`` returns ids whose estimated Jaccard is about
    ``threshold`` or higher. ``registry_ids[i]`` belongs to ``signatures[i]``.
    Each row is a uint64 array of length 128. An empty id list returns an
    empty index.

    Example:
    >>> from skill_genealogy.text_helpers import make_minhash, minhash_to_array
    >>> values = minhash_to_array(make_minhash({"alpha beta", "beta gamma"}))
    >>> built = build_registry_lsh(["a#1", "b#1"], np.vstack([values, values]), 0.8)
    >>> sorted(built.query(minhash_from_array(values)))
    ['a#1', 'b#1']
    """
    if not 0.0 < float(threshold) <= 1.0:
        raise ValueError(f"threshold must be between 0 and 1, got {threshold}")
    matrix = np.asarray(signatures)
    if matrix.ndim != 2 or matrix.shape[1] != settings.MINHASH_NUM_PERM:
        width = settings.MINHASH_NUM_PERM
        raise ValueError(f"signatures must have shape (n, {width}), got {matrix.shape}")
    if matrix.shape[0] != len(registry_ids):
        raise ValueError(
            f"{len(registry_ids)} ids for {matrix.shape[0]} signatures"
        )
    seen: set[str] = set()
    index = MinHashLSH(threshold=float(threshold), num_perm=settings.MINHASH_NUM_PERM)
    for key, row in zip(registry_ids, matrix, strict=True):
        name = str(key)
        if name in seen:
            raise ValueError(f"duplicate signature id {name}")
        seen.add(name)
        index.insert(name, minhash_from_array(row))
    return index


def _link_for_row(query_blob: str, row: object) -> tuple[str, float] | None:
    """Return the link type and similarity for one ``itertuples`` row, or None."""
    blob = _blob_key(row.blob)
    if query_blob and blob and query_blob == blob:
        return "exact", 1.0
    jaccard = _as_float(row.jaccard)
    containment = _as_float(row.containment)
    if jaccard >= settings.NEAR_COPY_MIN_JACCARD:
        return "near", jaccard
    if containment >= settings.NEAR_COPY_MIN_CONTAINMENT:
        return "near_contained", containment
    return None


def _scored_sources(query: pd.Series, candidates: pd.DataFrame) -> list[dict[str, object]]:
    """Keep older versions in other repos that clear a link rule."""
    query_at = _parse_time(query["release_date"])
    if pd.isna(query_at):
        return []
    query_repo = str(query["repo"])
    query_lineage = str(query["lineage_id"])
    query_blob = _blob_key(query["blob"])
    query_version = str(query["version_id"])
    scored: list[dict[str, object]] = []
    for row in candidates.itertuples(index=False):
        if str(row.version_id) == query_version or str(row.lineage_id) == query_lineage:
            continue
        if str(row.repo) == query_repo:
            continue
        release_at = _parse_time(row.release_date)
        if pd.isna(release_at) or not release_at < query_at:
            continue
        link = _link_for_row(query_blob, row)
        if link is None:
            continue
        scored.append(
            {
                "lineage_id": str(row.lineage_id),
                "version_seq": int(row.version_seq),
                "release_at": release_at,
                "rehost_link": link[0],
                "similarity": link[1],
            }
        )
    return scored


def _best_source(scored: list[dict[str, object]]) -> dict[str, object]:
    """Pick exact first, then the highest similarity, then the oldest id."""

    def sort_key(item: dict[str, object]) -> tuple[object, ...]:
        exact_rank = 0 if item["rehost_link"] == "exact" else 1
        return (exact_rank, -float(item["similarity"]), item["release_at"], item["lineage_id"])

    return min(scored, key=sort_key)


# Algorithm A23, see CODE_FLOW.md
def find_rehost_source(
    lineage_id: str, first_version_id: str, candidates: pd.DataFrame
) -> dict | None:
    """Pick the older version this lineage's first text was copied from.

    ``candidates`` includes the query row (``version_id`` equals
    ``first_version_id``) and the versions that share its blob or passed
    the LSH check. A row counts only when its repo differs and its
    ``release_date`` is strictly earlier. An exact blob match wins.
    Otherwise the highest similarity wins: Jaccard for a near link,
    containment for a near-contained link. Ties go to the oldest date,
    then ``lineage_id``. None means this first version has no source.

    Example:
    >>> rows = pd.DataFrame([
    ...     {"version_id": "B#1", "lineage_id": "B", "repo": "b/repo",
    ...      "version_seq": 1, "release_date": "2026-03-01", "blob": "same",
    ...      "jaccard": 1.0, "containment": 1.0},
    ...     {"version_id": "A#1", "lineage_id": "A", "repo": "a/repo",
    ...      "version_seq": 1, "release_date": "2026-01-01", "blob": "same",
    ...      "jaccard": 1.0, "containment": 1.0},
    ... ])
    >>> found = find_rehost_source("B", "B#1", rows)
    >>> (found["copied_from_lineage"], found["rehost_link"])
    ('A', 'exact')
    """
    if candidates.empty:
        return None
    _require_columns(candidates, _CANDIDATE_COLUMNS, "candidates")
    matched = candidates.loc[candidates["version_id"].astype(str) == str(first_version_id)]
    if matched.empty:
        return None
    scored = [
        item
        for item in _scored_sources(matched.iloc[0], candidates)
        if str(item["lineage_id"]) != str(lineage_id)
    ]
    if not scored:
        return None
    best = _best_source(scored)
    return {
        "lineage_id": lineage_id,
        "copied_from_lineage": best["lineage_id"],
        "copied_from_seq": int(best["version_seq"]),
        "rehost_link": best["rehost_link"],
        "rehost_similarity": float(best["similarity"]),
    }


def _oldest_lineage(members: list[str], dates: dict[str, pd.Timestamp]) -> str:
    """Return the member with the oldest release date, then the smallest id.

    A missing date does not beat a real date.
    """

    def sort_key(lineage_id: str) -> tuple[object, ...]:
        stamp = dates.get(lineage_id, pd.NaT)
        if stamp is None or pd.isna(stamp):
            return (1, "", lineage_id)
        return (0, stamp.isoformat(), lineage_id)

    return min(members, key=sort_key)


def _chain_to_root(
    start: str,
    parents: dict[str, str | None],
    dates: dict[str, pd.Timestamp],
) -> tuple[str, int, bool]:
    """Return the root, the number of steps, and whether the walk met a cycle.

    A repeated id ends the walk. The root of a cycle is its oldest member.
    ``chain_length`` is the number of steps along the walk to that root.
    """
    seen: list[str] = []
    seen_at: dict[str, int] = {}
    current: str | None = start
    while current and current not in seen_at:
        seen_at[current] = len(seen)
        seen.append(current)
        current = parents.get(current)
    if current is None:
        return seen[-1], len(seen) - 1, False
    root = _oldest_lineage(seen[seen_at[current] :], dates)
    return root, seen_at.get(root, 0), True


def _empty_roots() -> pd.DataFrame:
    """An empty root table with the columns later stages expect."""
    frame = pd.DataFrame(columns=list(_ROOT_COLUMNS))
    frame["copied_from_seq"] = pd.Series(dtype="Int64")
    frame["rehost_similarity"] = pd.Series(dtype="float64")
    frame["chain_length"] = pd.Series(dtype="int64")
    frame["is_original"] = pd.Series(dtype="bool")
    frame["chain_cycle"] = pd.Series(dtype="bool")
    return frame


def _finish_roots(frame: pd.DataFrame) -> pd.DataFrame:
    """Cast root columns and sort by lineage id."""
    finished = frame.loc[:, list(_ROOT_COLUMNS)].copy()
    finished["copied_from_seq"] = finished["copied_from_seq"].astype("Int64")
    finished["rehost_similarity"] = pd.to_numeric(finished["rehost_similarity"], errors="coerce")
    finished["is_original"] = finished["is_original"].astype(bool)
    finished["chain_cycle"] = finished["chain_cycle"].astype(bool)
    finished["chain_length"] = finished["chain_length"].astype(int)
    finished["lineage_id"] = finished["lineage_id"].astype(str)
    finished["root_lineage"] = finished["root_lineage"].astype(str)
    return finished.sort_values("lineage_id", kind="mergesort").reset_index(drop=True)


# Algorithm A24, see CODE_FLOW.md
def follow_to_root(rehosts: pd.DataFrame) -> pd.DataFrame:
    """Follow ``copied_from_lineage`` until a lineage with no source.

    ``chain_length`` is the number of steps to ``root_lineage``.
    ``is_original`` is true only when this lineage has no source. A cycle
    stops the walk and sets ``chain_cycle``. The root of a cycle is the
    oldest lineage in it, then the smallest lineage id. That root still
    has a source, so ``is_original`` stays false and its chain length is 0.

    Inputs: one row per lineage, including originals. ``release_date`` is
    the first version's date and is used only to pick the oldest cycle member.
    Outputs: the lineage-root columns, sorted by ``lineage_id``.

    Example:
    >>> sample = pd.DataFrame({
    ...     "lineage_id": ["A", "B", "C"],
    ...     "copied_from_lineage": [None, "A", "B"],
    ...     "copied_from_seq": [None, 1, 1],
    ...     "rehost_link": [None, "exact", "near"],
    ...     "rehost_similarity": [None, 1.0, 0.9],
    ...     "release_date": ["2026-01-01", "2026-03-01", "2026-04-01"],
    ... })
    >>> rooted = follow_to_root(sample)
    >>> rooted.loc[rooted["lineage_id"] == "C", "root_lineage"].item()
    'A'
    >>> int(rooted.loc[rooted["lineage_id"] == "C", "chain_length"].item())
    2
    """
    if rehosts.empty:
        return _empty_roots()
    _require_columns(rehosts, _REHOST_COLUMNS, "rehosts")
    parents: dict[str, str | None] = {}
    dates: dict[str, pd.Timestamp] = {}
    fields: dict[str, dict[str, object]] = {}
    for row in rehosts.itertuples(index=False):
        lineage_id = str(row.lineage_id)
        if lineage_id in parents:
            continue
        parent = _parent_id(row.copied_from_lineage)
        parents[lineage_id] = parent
        dates[lineage_id] = _parse_time(row.release_date)
        fields[lineage_id] = {
            "copied_from_lineage": parent,
            "copied_from_seq": None if parent is None else _as_optional_int(row.copied_from_seq),
            "rehost_link": None if parent is None else row.rehost_link,
            "rehost_similarity": None if parent is None else row.rehost_similarity,
        }
    rows: list[dict[str, object]] = []
    for lineage_id, source in fields.items():
        root, steps, cycle = _chain_to_root(lineage_id, parents, dates)
        rows.append(
            {
                "lineage_id": lineage_id,
                "copied_from_lineage": source["copied_from_lineage"],
                "copied_from_seq": source["copied_from_seq"],
                "rehost_link": source["rehost_link"],
                "rehost_similarity": source["rehost_similarity"],
                "root_lineage": root,
                "is_original": source["copied_from_lineage"] is None,
                "chain_length": steps,
                "chain_cycle": cycle,
            }
        )
    return _finish_roots(pd.DataFrame(rows))


def _check_rehost_files(dev: bool) -> None:
    """Raise ``FileNotFoundError`` when a rehost input is missing."""
    flag = " --dev" if dev else ""
    check_inputs_exist(
        {
            "lineages": _lineages_path(dev),
            "lineage_versions": _versions_path(dev),
            "registry_signatures": _registry_path(dev),
        },
        {
            "lineages": f"python -m skill_genealogy.build_registry_lineages{flag}",
            "lineage_versions": f"python -m skill_genealogy.build_registry_lineages{flag}",
            "registry_signatures": f"python -m skill_genealogy.find_copies{flag} --only signatures",
        },
    )


def _version_table(versions: pd.DataFrame, lineages: pd.DataFrame) -> pd.DataFrame:
    """Join each version to its registry repo and signature id."""
    needed = ["lineage_id", "version_seq", "blob", "release_date"]
    _require_columns(versions, tuple(needed), "lineage_versions")
    _require_columns(lineages, ("lineage_id", "repo"), "lineages")
    repos = lineages.loc[:, ["lineage_id", "repo"]].drop_duplicates("lineage_id")
    merged = versions.loc[:, needed].merge(repos, on="lineage_id", how="left")
    merged["repo"] = merged["repo"].fillna("").astype(str)
    merged["lineage_id"] = merged["lineage_id"].astype(str)
    merged["version_seq"] = merged["version_seq"].astype(int)
    merged["version_id"] = [
        _version_id(lineage_id, version_seq)
        for lineage_id, version_seq in zip(
            merged["lineage_id"].tolist(), merged["version_seq"].tolist(), strict=True
        )
    ]
    return merged


def _sketches(ids: list[str], signatures: np.ndarray) -> dict[str, MinHash]:
    """Rebuild one MinHash per stored signature, keyed by version id."""
    return {
        str(key): minhash_from_array(signatures[index]) for index, key in enumerate(ids)
    }


def _blob_groups(records: dict[str, dict[str, object]]) -> dict[str, list[str]]:
    """Map a blob sha to the version ids that store it."""
    grouped: dict[str, list[str]] = {}
    for version_id, record in records.items():
        key = _blob_key(record["blob"])
        if key:
            grouped.setdefault(key, []).append(version_id)
    return grouped


def _rehost_fields(
    lineage_id: str, release_date: object, source: dict | None
) -> dict[str, object]:
    """One rehost row. ``source`` None means the lineage is original."""
    if source is None:
        return {
            "lineage_id": lineage_id,
            "copied_from_lineage": None,
            "copied_from_seq": None,
            "rehost_link": None,
            "rehost_similarity": None,
            "release_date": release_date,
        }
    return {
        "lineage_id": lineage_id,
        "copied_from_lineage": source["copied_from_lineage"],
        "copied_from_seq": int(source["copied_from_seq"]),
        "rehost_link": source["rehost_link"],
        "rehost_similarity": float(source["rehost_similarity"]),
        "release_date": release_date,
    }


def _scored_candidate(
    query_id: str,
    other_id: str,
    sketches: dict[str, MinHash],
    sizes: dict[str, int],
    record: dict[str, object],
) -> dict[str, object]:
    """Add Jaccard and containment of ``other_id`` inside the query text."""
    query_sketch = sketches.get(query_id)
    other_sketch = sketches.get(other_id)
    if query_sketch is None or other_sketch is None or other_id == query_id:
        jaccard, containment = 0.0, 0.0
    else:
        jaccard = estimate_jaccard(query_sketch, other_sketch)
        containment = _containment_of_source(
            jaccard, sizes.get(other_id, 0), sizes.get(query_id, 0)
        )
    return {
        "version_id": record["version_id"],
        "lineage_id": record["lineage_id"],
        "repo": record["repo"],
        "version_seq": int(record["version_seq"]),
        "release_date": record["release_date"],
        "blob": record["blob"],
        "jaccard": jaccard,
        "containment": containment,
    }


def _candidate_frame(record: dict[str, object], index: dict[str, object]) -> pd.DataFrame:
    """The query version plus exact blob mates and both LSH hit lists."""
    version_id = str(record["version_id"])
    found = {version_id}
    blob_key = _blob_key(record["blob"])
    blobs: dict[str, list[str]] = index["blobs"]
    if blob_key:
        found.update(blobs.get(blob_key, ()))
    sketches: dict[str, MinHash] = index["sketches"]
    sketch = sketches.get(version_id)
    if sketch is not None:
        found.update(_query_lsh(index["near"], sketch))
        found.update(_query_lsh(index["wide"], sketch))
    records: dict[str, dict[str, object]] = index["records"]
    rows = [
        _scored_candidate(version_id, other_id, sketches, index["sizes"], records[other_id])
        for other_id in found
        if other_id in records
    ]
    if not rows:
        return pd.DataFrame(columns=list(_CANDIDATE_COLUMNS))
    return pd.DataFrame(rows)


def _place_one(lineage_id: str, index: dict[str, object]) -> dict[str, object]:
    """Find a source for one lineage, or mark it original."""
    first: dict[str, dict[str, object]] = index["first"]
    record = first.get(lineage_id)
    if record is None:
        return _rehost_fields(lineage_id, None, None)
    source = find_rehost_source(
        lineage_id, str(record["version_id"]), _candidate_frame(record, index)
    )
    return _rehost_fields(lineage_id, record["release_date"], source)


def _search_index(
    lineages: pd.DataFrame,
    versions: pd.DataFrame,
    ids: list[str],
    signatures: np.ndarray,
    sizes: np.ndarray,
) -> dict[str, object]:
    """Build the blob map, sketches and two LSH indexes used for every lineage."""
    table = _version_table(versions, lineages)
    records = {
        str(row["version_id"]): row for row in table.to_dict(orient="records")
    }
    known = set(records)
    missing = sum(1 for key in ids if str(key) not in known)
    if missing:
        print(f"warning: {missing} signature ids are not lineage versions", flush=True)
    first = {
        str(row["lineage_id"]): row
        for row in records.values()
        if int(row["version_seq"]) == 1
    }
    sketches = _sketches(ids, signatures)
    size_of = {str(key): int(sizes[index]) for index, key in enumerate(ids)}
    return {
        "records": records,
        "first": first,
        "sketches": sketches,
        "sizes": size_of,
        "blobs": _blob_groups(records),
        "near": build_registry_lsh(ids, signatures, settings.NEAR_COPY_MIN_JACCARD),
        "wide": build_registry_lsh(
            ids, signatures, settings.REHOST_CONTAINMENT_LSH_THRESHOLD
        ),
    }


def _place_lineages(
    lineages: pd.DataFrame,
    versions: pd.DataFrame,
    ids: list[str],
    signatures: np.ndarray,
    sizes: np.ndarray,
) -> pd.DataFrame:
    """Return one rehost row per lineage, before the chain is followed."""
    index = _search_index(lineages, versions, ids, signatures, sizes)
    lineage_ids = lineages["lineage_id"].astype(str).drop_duplicates().tolist()
    placed = [_place_one(lineage_id, index) for lineage_id in lineage_ids]
    if not placed:
        return pd.DataFrame(columns=list(_REHOST_COLUMNS))
    return pd.DataFrame(placed)


def find_rehosted_lineages(dev: bool) -> pd.DataFrame:
    """Return the root row for every lineage in this mode.

    The first version is compared with versions of other lineages in other
    repos that were released earlier. Exact blob matches win. Otherwise the
    highest near or contained score wins. Chains are followed to the root.
    Nothing is written. A missing input raises ``FileNotFoundError`` and
    names the command that builds it.

    Inputs: ``dev`` — True reads the 30-registry sample.
    Outputs: the lineage-root columns, one row per lineage.
    """
    _check_rehost_files(dev)
    lineages = read_parquet(_lineages_path(dev))
    versions = read_parquet(_versions_path(dev))
    ids, signatures, sizes = load_signatures(_registry_path(dev))
    print(
        f"placing {lineages['lineage_id'].nunique()} lineages against {len(ids)} signatures",
        flush=True,
    )
    return follow_to_root(_place_lineages(lineages, versions, ids, signatures, sizes))


def _hub_decision(english: int, rehosted: int) -> tuple[float, bool, str]:
    """Return hub share, ``is_hub``, and the reason sentence.

    Fewer than ``HUB_TEST_MIN_SKILLS`` English lineages is not scored.
    The share is rehosted lineages divided by English lineages. A share of
    at least ``HUB_MIN_SHARE`` is a hub.

    Example:
    >>> share, is_hub, reason = _hub_decision(5, 2)
    >>> (share, is_hub, reason.endswith("-> hub"))
    (0.4, True, True)
    >>> small_share, small_hub, small_reason = _hub_decision(4, 4)
    >>> small_hub is False and small_reason.endswith("not scored") and small_share != small_share
    True
    """
    if english < settings.HUB_TEST_MIN_SKILLS:
        reason = (
            f"{english} English lineages is below {settings.HUB_TEST_MIN_SKILLS}, "
            "so the hub share is not scored"
        )
        return float("nan"), False, reason
    share = rehosted / english
    is_hub = share >= settings.HUB_MIN_SHARE
    ending = "hub" if is_hub else "not a hub"
    reason = f"{rehosted} of {english} skills first appeared in older registries -> {ending}"
    return share, is_hub, reason


def _empty_hubs() -> pd.DataFrame:
    """An empty hub table with the columns the role update expects."""
    frame = pd.DataFrame(columns=list(_HUB_COLUMNS))
    frame["english_lineages"] = pd.Series(dtype="int64")
    frame["rehosted_lineages"] = pd.Series(dtype="int64")
    frame["hub_share"] = pd.Series(dtype="float64")
    frame["is_hub"] = pd.Series(dtype="bool")
    return frame


# Algorithm A24b, see CODE_FLOW.md
def compute_dated_hub_shares(lineages: pd.DataFrame, roots: pd.DataFrame) -> pd.DataFrame:
    """Share of English lineages re-hosted from an older registry.

    ``hub_share`` is re-hosted English lineages divided by English lineages.
    ``is_hub`` is true when that share is at least ``HUB_MIN_SHARE``.
    Fewer than ``HUB_TEST_MIN_SKILLS`` English lineages is not scored:
    ``hub_share`` is null and ``is_hub`` is false. A non-English lineage
    is left out of both counts. One row per repo in ``lineages``.

    Example:
    >>> skills = pd.DataFrame({
    ...     "lineage_id": [f"s{i}" for i in range(5)],
    ...     "repo": ["acme/hub"] * 5,
    ...     "is_english": [True] * 5,
    ... })
    >>> roots = pd.DataFrame({
    ...     "lineage_id": [f"s{i}" for i in range(5)],
    ...     "is_original": [False, False, True, True, True],
    ... })
    >>> scored = compute_dated_hub_shares(skills, roots)
    >>> (float(scored.loc[0, "hub_share"]), bool(scored.loc[0, "is_hub"]))
    (0.4, True)
    """
    _require_columns(lineages, ("lineage_id", "repo", "is_english"), "lineages")
    _require_columns(roots, ("lineage_id", "is_original"), "roots")
    if lineages.empty:
        return _empty_hubs()
    work = lineages.loc[:, ["repo", "lineage_id", "is_english"]].copy()
    work["lineage_id"] = work["lineage_id"].astype(str)
    work["repo"] = work["repo"].astype(str)
    work = work.drop_duplicates("lineage_id")
    origin = roots.loc[:, ["lineage_id", "is_original"]].drop_duplicates("lineage_id")
    origin["lineage_id"] = origin["lineage_id"].astype(str)
    work = work.merge(origin, on="lineage_id", how="left")
    work["english"] = work["is_english"].fillna(False).astype(bool)
    original = work["is_original"].fillna(True).astype(bool)
    work["rehosted"] = work["english"] & ~original
    counts = (
        work.groupby("repo", sort=True)
        .agg(english_lineages=("english", "sum"), rehosted_lineages=("rehosted", "sum"))
        .reset_index()
    )
    decisions = [
        _hub_decision(int(english), int(rehosted))
        for english, rehosted in zip(
            counts["english_lineages"].tolist(),
            counts["rehosted_lineages"].tolist(),
            strict=True,
        )
    ]
    counts["english_lineages"] = counts["english_lineages"].astype(int)
    counts["rehosted_lineages"] = counts["rehosted_lineages"].astype(int)
    counts["hub_share"] = [item[0] for item in decisions]
    counts["is_hub"] = [item[1] for item in decisions]
    counts["hub_reason"] = [item[2] for item in decisions]
    return counts.loc[:, list(_HUB_COLUMNS)].reset_index(drop=True)


def _share_present(value: object) -> bool:
    """True when a hub share was computed, including a share of zero."""
    if value is None:
        return False
    try:
        return not bool(pd.isna(value))
    except TypeError:
        return True


def update_repo_roles(repo_roles: pd.DataFrame, registry_hubs: pd.DataFrame) -> pd.DataFrame:
    """Copy stage-2 roles and replace registry hubs from the dated test.

    A registry that appears in ``registry_hubs`` gets ``is_hub``,
    ``hub_share`` and ``repo_role`` from that table. ``repo_role`` is
    ``registry_hub`` when ``is_hub`` is true, otherwise ``registry``.
    ``hub_evidence`` is ``registry_history``. Other rows keep their stage-2
    values. Their ``hub_evidence`` is ``dataset_dates`` when ``hub_share``
    is present, and null when it is not. ``hub_tested`` is left as stage 2
    wrote it. The row count matches ``repo_roles``.

    Example:
    >>> roles = pd.DataFrame([{
    ...     "repo": "a/reg", "is_registry": True, "is_hub": False,
    ...     "hub_share": 0.0, "repo_role": "registry",
    ... }])
    >>> hubs = pd.DataFrame([{"repo": "a/reg", "is_hub": True, "hub_share": 0.3}])
    >>> updated = update_repo_roles(roles, hubs)
    >>> (updated.loc[0, "repo_role"], updated.loc[0, "hub_evidence"])
    ('registry_hub', 'registry_history')
    """
    _require_columns(repo_roles, _ROLE_FIELDS, "repo_roles")
    _require_columns(registry_hubs, ("repo", "is_hub", "hub_share"), "registry_hubs")
    result = repo_roles.reset_index(drop=True).copy()
    hubs = registry_hubs.loc[:, ["repo", "is_hub", "hub_share"]].drop_duplicates("repo")
    hubs = hubs.rename(columns={"is_hub": "dated_is_hub", "hub_share": "dated_hub_share"})
    merged = result.merge(hubs, on="repo", how="left")
    if len(merged) != len(result):
        raise ValueError("repo_roles repo values are not unique")
    is_registry = merged["is_registry"].fillna(False).astype(bool)
    dated = merged["dated_is_hub"].notna() & is_registry
    positions = np.flatnonzero(dated.to_numpy())
    result.loc[positions, "is_hub"] = (
        merged.loc[positions, "dated_is_hub"].astype(bool).to_numpy()
    )
    result.loc[positions, "hub_share"] = merged.loc[positions, "dated_hub_share"].to_numpy()
    roles = result["repo_role"].astype(object).to_numpy().copy()
    is_hub_now = result["is_hub"].astype(bool).tolist()
    for position in positions:
        roles[position] = "registry_hub" if is_hub_now[position] else "registry"
    result["repo_role"] = roles
    evidence = [
        "registry_history" if flag else ("dataset_dates" if _share_present(share) else None)
        for flag, share in zip(dated.tolist(), result["hub_share"].tolist(), strict=True)
    ]
    result["hub_evidence"] = evidence
    columns = [name for name in result.columns if name not in {"dated_is_hub", "dated_hub_share"}]
    return result.loc[:, columns]


def _with_preview(hubs: pd.DataFrame, repo_roles: pd.DataFrame) -> pd.DataFrame:
    """Add the stage-2 ``hub_share`` as ``preview_hub_share``."""
    _require_columns(repo_roles, ("repo", "hub_share"), "repo_roles")
    preview = repo_roles.loc[:, ["repo", "hub_share"]].drop_duplicates("repo")
    preview = preview.rename(columns={"hub_share": "preview_hub_share"})
    merged = hubs.merge(preview, on="repo", how="left")
    return merged.loc[:, [*_HUB_COLUMNS, "preview_hub_share"]]


def save_rehost_outputs(
    roots: pd.DataFrame, hubs: pd.DataFrame, roles: pd.DataFrame, dev: bool
) -> None:
    """Write the three rehost tables for this mode."""
    write_parquet(roots, _roots_path(dev), ["lineage_id"])
    write_parquet(hubs, _registry_hubs_path(dev), ["repo"])
    write_parquet(roles, _roles_final_path(dev), ["repo"])


def _print_registry_mix(lineages: pd.DataFrame, roots: pd.DataFrame) -> None:
    """Print original and re-hosted lineage counts, most re-hosted first."""
    merged = lineages.loc[:, ["lineage_id", "repo"]].copy()
    merged["lineage_id"] = merged["lineage_id"].astype(str)
    origin = roots.loc[:, ["lineage_id", "is_original"]].copy()
    origin["lineage_id"] = origin["lineage_id"].astype(str)
    merged = merged.merge(origin, on="lineage_id", how="left")
    merged["original"] = merged["is_original"].fillna(True).astype(bool)
    counts = merged.groupby("repo", sort=False).agg(
        lineages=("lineage_id", "size"), original=("original", "sum")
    )
    counts["rehosted"] = counts["lineages"] - counts["original"]
    counts = counts.sort_values(["rehosted", "repo"], ascending=[False, True], kind="mergesort")
    print("original vs re-hosted lineages", flush=True)
    for repo, row in counts.iterrows():
        print(
            f"{repo}: original {int(row.original)}, rehosted {int(row.rehosted)}",
            flush=True,
        )


def _print_hub_changes(hubs: pd.DataFrame, stage2: pd.DataFrame) -> None:
    """Print how many registry hub tags changed from the stage-2 preview."""
    preview = stage2.loc[:, ["repo", "is_hub", "repo_role"]].drop_duplicates("repo")
    merged = hubs.merge(preview, on="repo", how="left", suffixes=("", "_preview"))
    before = merged["is_hub_preview"].fillna(False).astype(bool)
    after = merged["is_hub"].astype(bool)
    print(
        f"registry hubs in this run: {int(after.sum())} (stage-2 preview {int(before.sum())})",
        flush=True,
    )
    changed = merged.loc[before.to_numpy() != after.to_numpy()]
    print("registries whose hub tag changed", flush=True)
    if changed.empty:
        print("(none)", flush=True)
        return
    for row in changed.itertuples(index=False):
        new_role = "registry_hub" if bool(row.is_hub) else "registry"
        print(f"{row.repo}: {row.repo_role} -> {new_role}", flush=True)


def _print_rehost_summary(
    dev: bool,
    roots: pd.DataFrame,
    hubs: pd.DataFrame,
    stage2: pd.DataFrame,
    lineages: pd.DataFrame,
    elapsed: float,
) -> None:
    """Print rehost counts, the hub comparison, and two sample records."""
    links = roots["rehost_link"].fillna("(original)").value_counts()
    print_summary(
        "rehost summary",
        {
            "lineages": len(roots),
            "original_lineages": int(roots["is_original"].sum()),
            "rehosted_lineages": int((~roots["is_original"]).sum()),
            "exact": int(links.get("exact", 0)),
            "near": int(links.get("near", 0)),
            "near_contained": int(links.get("near_contained", 0)),
            "chain_cycles": int(roots["chain_cycle"].sum()),
            "registries": len(hubs),
            "registry_hubs": int(hubs["is_hub"].sum()) if len(hubs) else 0,
            "seconds": round(elapsed, 1),
        },
    )
    _print_registry_mix(lineages, roots)
    _print_hub_changes(hubs, stage2)
    if dev:
        print(
            "dev sample: only the 30 downloaded registries can be a re-host source. "
            "A skill copied from a registry outside this sample is counted as original, "
            "so these hub shares are too low. DEV_MIN_HUBS cannot be guaranteed, "
            "because the sample was drawn in stage 2, before this dated test.",
            flush=True,
        )
    print_sample_records(roots, "lineage root sample")
    print_sample_records(hubs, "registry hub sample")


def _load_rehost_summary(dev: bool) -> None:
    """Print the summary from rehost files that are already on disk."""
    roots = read_parquet(_roots_path(dev))
    hubs = read_parquet(_registry_hubs_path(dev))
    lineages = read_parquet(_lineages_path(dev))
    stage2 = read_parquet(_repo_roles_path())
    _print_rehost_summary(dev, roots, hubs, stage2, lineages, 0.0)


def _run_rehost(dev: bool, force: bool) -> None:
    """Find re-hosted lineages and write the dated hub tables.

    Existing outputs are kept unless ``force`` is true. ``--force`` deletes
    only the three rehost files, not the signature files.
    """
    outputs = [_roots_path(dev), _registry_hubs_path(dev), _roles_final_path(dev)]
    if force:
        for path in outputs:
            if path.is_file():
                path.unlink()
    if outputs_exist(outputs):
        print("skip: rehost outputs already exist", flush=True)
        _load_rehost_summary(dev)
        return
    check_inputs_exist(
        {"repo_roles": _repo_roles_path()},
        {"repo_roles": "python -m skill_genealogy.select_registries"},
    )
    started = time.perf_counter()
    # Step 3: Registries copying from each other (algorithms A23, A24, A24b)
    print("Step 3: registries copying each other", flush=True)
    roots = find_rehosted_lineages(dev)
    lineages = read_parquet(_lineages_path(dev))
    stage2 = read_parquet(_repo_roles_path())
    hubs = _with_preview(compute_dated_hub_shares(lineages, roots), stage2)
    roles = update_repo_roles(stage2, hubs)
    save_rehost_outputs(roots, hubs, roles, dev)
    _print_rehost_summary(
        dev, roots, hubs, stage2, lineages, time.perf_counter() - started
    )


# Saved text-link columns. ``release_date`` is kept only until the cluster step.
_LINK_COLUMNS = (
    "file_sha",
    "lineage_id",
    "version_seq",
    "link",
    "jaccard",
    "containment",
    "similarity_verified",
)
_LINK_WORK_COLUMNS = (
    "file_sha",
    "lineage_id",
    "version_seq",
    "link",
    "jaccard",
    "containment",
    "release_date",
)
_DROPPED_COLUMNS = (
    "file_sha",
    "lineage_id",
    "link",
    "similarity_verified",
    "reason",
)
_COPY_COLUMNS = (
    "repo",
    "path",
    "file_sha",
    "lineage_id",
    "root_lineage",
    "version_seq",
    "link",
    "jaccard",
    "containment",
    "similarity_verified",
    "is_fork",
    "same_owner_as_registry",
    "has_wild_history",
)
# Line-diff texts fetched together. The artifact scan itself is one query.
LINE_CHECK_BATCH_ROWS = 2_000

# Query Q5.4, see QUERIES.md
# English texts that appear outside a registry repo, joined to a version blob.
# The oldest release wins. On the same date a non-hub wins.
EXACT_LINKS_SQL = """
WITH versions AS (
    SELECT
        lower(lineage_versions.blob) AS blob,
        lineage_versions.lineage_id AS lineage_id,
        lineage_versions.version_seq AS version_seq,
        lineage_versions.release_date AS release_date,
        lineages.repo AS registry_repo,
        coalesce(registry_hubs.is_hub, false) AS is_hub
    FROM lineage_versions
    JOIN lineages
      ON lineage_versions.lineage_id = lineages.lineage_id
    LEFT JOIN registry_hubs
      ON lineages.repo = registry_hubs.repo
    WHERE lineage_versions.blob IS NOT NULL
      AND lineage_versions.blob <> ''
      AND lpad(lower(lineage_versions.blob), 40, '0') <> repeat('0', 40)
),
ranked AS (
    SELECT
        copy_text_shas.file_sha AS file_sha,
        versions.lineage_id AS lineage_id,
        versions.version_seq AS version_seq,
        versions.release_date AS release_date,
        row_number() OVER (
            PARTITION BY copy_text_shas.file_sha
            ORDER BY
                versions.release_date ASC NULLS LAST,
                versions.is_hub ASC,
                versions.lineage_id ASC,
                versions.version_seq ASC
        ) AS pick
    FROM copy_text_shas
    JOIN skill_language
      ON copy_text_shas.file_sha = skill_language.file_sha
    JOIN versions
      ON lower(copy_text_shas.file_sha) = versions.blob
    WHERE skill_language.is_english
)
SELECT
    file_sha,
    lineage_id,
    version_seq,
    'exact' AS link,
    1.0 AS jaccard,
    1.0 AS containment,
    release_date
FROM ranked
WHERE pick = 1
"""

# Query Q5.4, see QUERIES.md
# Distinct English texts that have a file outside every registry repo.
COPY_TEXT_SHAS_SQL = """
SELECT DISTINCT artifacts.file_sha AS file_sha
FROM artifacts
ANTI JOIN registry_repo_list
  ON artifacts.repo_full_name = registry_repo_list.repo
JOIN skill_language
  ON artifacts.file_sha = skill_language.file_sha
WHERE skill_language.is_english
  AND artifacts.file_sha IS NOT NULL
  AND artifacts.file_sha <> ''
"""

# Query Q5.5, see QUERIES.md
# An unlinked text takes the lineage that already has the most texts in its
# 0.80 cluster. A tie goes to the lineage whose linked version is oldest.
CLUSTER_LINKS_SQL = """
WITH members AS (
    SELECT clusters.file_sha AS file_sha, clusters.cluster_id AS cluster_id
    FROM clusters
    WHERE abs(clusters.threshold - {threshold}) < 1e-6
),
linked_members AS (
    SELECT
        members.cluster_id AS cluster_id,
        linked_texts.lineage_id AS lineage_id,
        linked_texts.version_seq AS version_seq,
        linked_texts.release_date AS release_date,
        linked_texts.file_sha AS file_sha
    FROM members
    JOIN linked_texts ON members.file_sha = linked_texts.file_sha
),
lineage_counts AS (
    SELECT
        cluster_id,
        lineage_id,
        count(*) AS votes,
        min(release_date) AS oldest_release
    FROM linked_members
    GROUP BY cluster_id, lineage_id
),
best_lineage AS (
    SELECT cluster_id, lineage_id
    FROM (
        SELECT
            cluster_id,
            lineage_id,
            row_number() OVER (
                PARTITION BY cluster_id
                ORDER BY votes DESC, oldest_release ASC NULLS LAST, lineage_id ASC
            ) AS lineage_rank
        FROM lineage_counts
    )
    WHERE lineage_rank = 1
),
best_version AS (
    SELECT cluster_id, lineage_id, version_seq, release_date
    FROM (
        SELECT
            linked_members.cluster_id AS cluster_id,
            linked_members.lineage_id AS lineage_id,
            linked_members.version_seq AS version_seq,
            linked_members.release_date AS release_date,
            row_number() OVER (
                PARTITION BY linked_members.cluster_id, linked_members.lineage_id
                ORDER BY
                    linked_members.release_date ASC NULLS LAST,
                    linked_members.version_seq ASC
            ) AS version_rank
        FROM linked_members
        JOIN best_lineage
          ON linked_members.cluster_id = best_lineage.cluster_id
         AND linked_members.lineage_id = best_lineage.lineage_id
    )
    WHERE version_rank = 1
)
SELECT
    members.file_sha AS file_sha,
    best_version.lineage_id AS lineage_id,
    best_version.version_seq AS version_seq,
    'cluster' AS link,
    CAST(NULL AS DOUBLE) AS jaccard,
    CAST(NULL AS DOUBLE) AS containment,
    best_version.release_date AS release_date
FROM members
JOIN best_version ON members.cluster_id = best_version.cluster_id
JOIN skill_language ON members.file_sha = skill_language.file_sha
JOIN copy_text_shas ON members.file_sha = copy_text_shas.file_sha
LEFT JOIN linked_texts ON members.file_sha = linked_texts.file_sha
WHERE skill_language.is_english
  AND linked_texts.file_sha IS NULL
"""

# Query Q5.6, see QUERIES.md
# One row per file of a linked text that does not sit in a registry repo.
COPY_FILES_SQL = """
SELECT DISTINCT
    artifacts.repo_full_name AS repo,
    artifacts.path AS path,
    kept_links.file_sha AS file_sha,
    kept_links.lineage_id AS lineage_id,
    coalesce(lineage_roots.root_lineage, kept_links.lineage_id) AS root_lineage,
    kept_links.version_seq AS version_seq,
    kept_links.link AS link,
    kept_links.jaccard AS jaccard,
    kept_links.containment AS containment,
    kept_links.similarity_verified AS similarity_verified
FROM artifacts
JOIN kept_links ON artifacts.file_sha = kept_links.file_sha
LEFT JOIN lineage_roots ON kept_links.lineage_id = lineage_roots.lineage_id
ANTI JOIN registry_repo_list
  ON artifacts.repo_full_name = registry_repo_list.repo
WHERE artifacts.path IS NOT NULL
  AND artifacts.repo_full_name IS NOT NULL
"""

# Query Q5.7, see QUERIES.md
# One content string per linked text. The scan is filtered to those shas.
COPY_TEXTS_SQL = """
SELECT artifacts.file_sha AS file_sha, any_value(artifacts.content) AS content
FROM artifacts
JOIN needed_shas ON artifacts.file_sha = needed_shas.file_sha
GROUP BY artifacts.file_sha
"""

# Query Q5.8, see QUERIES.md
# Forks come from repos. The owner is the text before the first slash.
# Wild history is a distinct (repo, target_path).
FLAG_COPIES_SQL = """
SELECT
    copy_rows.repo AS repo,
    copy_rows.path AS path,
    copy_rows.file_sha AS file_sha,
    copy_rows.lineage_id AS lineage_id,
    copy_rows.root_lineage AS root_lineage,
    copy_rows.version_seq AS version_seq,
    copy_rows.link AS link,
    copy_rows.jaccard AS jaccard,
    copy_rows.containment AS containment,
    copy_rows.similarity_verified AS similarity_verified,
    coalesce(repo_forks.is_fork, 0) = 1 AS is_fork,
    split_part(copy_rows.repo, '/', 1) <> ''
        AND split_part(copy_rows.repo, '/', 1)
            = split_part(coalesce(lineage_repos.repo, ''), '/', 1)
        AS same_owner_as_registry,
    wild_paths.repo IS NOT NULL AS has_wild_history
FROM copy_rows
LEFT JOIN (
    SELECT full_name, max(is_fork) AS is_fork
    FROM repos
    GROUP BY full_name
) AS repo_forks ON copy_rows.repo = repo_forks.full_name
LEFT JOIN (
    SELECT lineage_id, any_value(repo) AS repo
    FROM lineages
    GROUP BY lineage_id
) AS lineage_repos ON copy_rows.lineage_id = lineage_repos.lineage_id
LEFT JOIN (
    SELECT DISTINCT repo, target_path FROM wild_versions
) AS wild_paths
  ON copy_rows.repo = wild_paths.repo
 AND copy_rows.path = wild_paths.target_path
"""


def _text_links_path(dev: bool) -> Path:
    """Return ``text_links.parquet`` for this mode."""
    return settings.data_dir(dev) / "text_links.parquet"


def _copies_path(dev: bool) -> Path:
    """Return ``copy_candidates.parquet`` for this mode."""
    return settings.data_dir(dev) / "copy_candidates.parquet"


def _dropped_path(dev: bool) -> Path:
    """Return ``links_dropped.parquet`` for this mode."""
    return settings.data_dir(dev) / "links_dropped.parquet"


def _wild_versions_path() -> Path:
    """Return ``wild_history/versions.parquet``. Dev and full both read it."""
    return settings.WILD_HISTORY_DIR / "versions.parquet"


def _relation_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    """True when ``name`` is a table or a view on this connection."""
    # fetchall, not fetchone: this DuckDB build keeps the result open after
    # one row, and the next statement then fails.
    rows = con.execute(
        """
        SELECT count(*) FROM information_schema.tables
        WHERE lower(table_name) = lower(?)
        """,
        [name],
    ).fetchall()
    return bool(rows) and int(rows[0][0]) > 0


def _require_relation(con: duckdb.DuckDBPyConnection, name: str) -> None:
    """Raise ``ValueError`` when a query's table was not registered."""
    if not _relation_exists(con, name):
        raise ValueError(f"missing table {name}")


def _empty_work_links() -> pd.DataFrame:
    """An empty link table, before the line check adds ``similarity_verified``."""
    frame = pd.DataFrame(columns=list(_LINK_WORK_COLUMNS))
    frame["version_seq"] = pd.Series(dtype="int64")
    frame["jaccard"] = pd.Series(dtype="float64")
    frame["containment"] = pd.Series(dtype="float64")
    return frame


def _empty_copies() -> pd.DataFrame:
    """An empty copy-file table with the columns stage 6 reads."""
    frame = pd.DataFrame(columns=list(_COPY_COLUMNS))
    frame["version_seq"] = pd.Series(dtype="int64")
    frame["jaccard"] = pd.Series(dtype="float64")
    frame["containment"] = pd.Series(dtype="float64")
    frame["similarity_verified"] = pd.Series(dtype="float64")
    frame["is_fork"] = pd.Series(dtype="bool")
    frame["same_owner_as_registry"] = pd.Series(dtype="bool")
    frame["has_wild_history"] = pd.Series(dtype="bool")
    return frame


def _empty_dropped() -> pd.DataFrame:
    """An empty dropped-link table."""
    frame = pd.DataFrame(columns=list(_DROPPED_COLUMNS))
    frame["similarity_verified"] = pd.Series(dtype="float64")
    return frame


def _work_links(rows: list[dict[str, object]]) -> pd.DataFrame:
    """Build the working link table from row dicts."""
    if not rows:
        return _empty_work_links()
    frame = pd.DataFrame(rows)
    for column in _LINK_WORK_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    frame = frame.loc[:, list(_LINK_WORK_COLUMNS)].copy()
    frame["file_sha"] = frame["file_sha"].astype(str)
    frame["lineage_id"] = frame["lineage_id"].astype(str)
    frame["version_seq"] = frame["version_seq"].astype(int)
    frame["link"] = frame["link"].astype(str)
    frame["jaccard"] = pd.to_numeric(frame["jaccard"], errors="coerce")
    frame["containment"] = pd.to_numeric(frame["containment"], errors="coerce")
    return frame.reset_index(drop=True)


def _concat_links(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Stack link tables. An empty list returns the empty working table."""
    parts = [frame for frame in frames if frame is not None and len(frame)]
    if not parts:
        return _empty_work_links()
    return pd.concat(parts, ignore_index=True)


def _date_text(value: object) -> str | None:
    """Return a release-date string, or None when the cell is missing."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    text = str(value).strip()
    if text == "" or text.lower() in {"none", "nat", "nan", "<na>"}:
        return None
    return text


def _hub_flag(value: object) -> bool:
    """True when the cell says this registry is a hub. Missing is false."""
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except TypeError:
        pass
    return bool(value)


def _choice_key(
    score: float,
    is_hub: bool,
    release_date: str | None,
    lineage_id: str,
    version_seq: int,
) -> tuple[object, ...]:
    """Lower is better: higher score, then a non-hub, then the oldest date.

    A missing date sorts after every real date, so it does not win an
    "oldest" tie. ``lineage_id`` and ``version_seq`` make the pick stable.

    Example:
    >>> _choice_key(0.9, False, "2026-06-01", "B", 1) < _choice_key(
    ...     0.9, True, "2026-01-01", "A", 1
    ... )
    True
    """
    stamp = "9999" if release_date is None else release_date
    return (-float(score), 1 if is_hub else 0, stamp, lineage_id, int(version_seq))


def _work_row(file_sha: str, chosen: dict[str, object], link: str) -> dict[str, object]:
    """One working link row from a chosen registry version."""
    return {
        "file_sha": file_sha,
        "lineage_id": chosen["lineage_id"],
        "version_seq": int(chosen["version_seq"]),
        "link": link,
        "jaccard": chosen["jaccard"],
        "containment": chosen["containment"],
        "release_date": chosen["release_date"],
    }


def _ensure_registry_repos(con: duckdb.DuckDBPyConnection) -> None:
    """Create ``registry_repo_list`` from lineage repos when the caller did not.

    The copy run replaces this with every ``is_registry`` repo before the
    exact query, so files in a registry outside this mode are still excluded.
    A test that already created the table is left as it is.
    """
    if _relation_exists(con, "registry_repo_list"):
        return
    _require_relation(con, "lineages")
    con.execute(
        """
        CREATE TEMP TABLE registry_repo_list AS
        SELECT DISTINCT repo
        FROM lineages
        WHERE repo IS NOT NULL AND repo <> ''
        """
    )


def _ensure_copy_text_shas(con: duckdb.DuckDBPyConnection) -> None:
    """Create the English texts that have a file outside a registry repo."""
    if _relation_exists(con, "copy_text_shas"):
        return
    _ensure_registry_repos(con)
    _require_relation(con, "artifacts")
    _require_relation(con, "skill_language")
    con.execute(f"CREATE TEMP TABLE copy_text_shas AS {COPY_TEXT_SHAS_SQL}")


def _ensure_exact_tables(con: duckdb.DuckDBPyConnection, dev: bool) -> None:
    """Register parquet views when the connection does not already have them.

    Tests create the tables themselves. A fresh connection reads the files
    for ``dev``. ``registry_repo_list`` is not replaced when it exists, so
    the copy run can load every registry before this is called.
    """
    flag = " --dev" if dev else ""
    if not _relation_exists(con, "artifacts"):
        check_inputs_exist(
            {"artifacts": settings.ARTIFACTS_GLOB, "skill_language": _language_path()},
            {
                "artifacts": "python src/data/download_data.py",
                "skill_language": "python -m skill_genealogy.select_registries --force-languages",
            },
        )
        _register_view(con, "artifacts", settings.ARTIFACTS_GLOB)
        _register_view(con, "skill_language", str(_language_path()))
    if not _relation_exists(con, "lineage_versions"):
        check_inputs_exist(
            {
                "lineage_versions": _versions_path(dev),
                "lineages": _lineages_path(dev),
                "registry_hubs": _registry_hubs_path(dev),
            },
            {
                "lineage_versions": f"python -m skill_genealogy.build_registry_lineages{flag}",
                "lineages": f"python -m skill_genealogy.build_registry_lineages{flag}",
                "registry_hubs": f"python -m skill_genealogy.find_copies{flag} --only rehost",
            },
        )
        _register_view(con, "lineage_versions", str(_versions_path(dev)))
        _register_view(con, "lineages", str(_lineages_path(dev)))
        _register_view(con, "registry_hubs", str(_registry_hubs_path(dev)))
    _ensure_copy_text_shas(con)


# Query Q5.4, see QUERIES.md
def find_exact_links(con: duckdb.DuckDBPyConnection, dev: bool) -> pd.DataFrame:
    """Link each English text outside the registries to one identical version.

    The dataset ``file_sha`` equals a lineage version ``blob``. When several
    lineages share that blob, the oldest ``release_date`` wins. On the same
    date a non-hub (``is_hub`` false) wins, then the smallest lineage id.
    Texts that appear only inside a registry repo are not links. A non-English
    text is not a link. ``dev`` selects the lineage files when the connection
    does not already hold the tables.

    Example:
    >>> con = connect_duckdb()
    >>> _ = con.execute(
    ...     "CREATE TABLE artifacts (repo_full_name VARCHAR, file_sha VARCHAR)"
    ... )
    >>> _ = con.execute(
    ...     "INSERT INTO artifacts VALUES ('a/reg', 'sha'), ('c/copy', 'sha')"
    ... )
    >>> _ = con.execute("CREATE TABLE skill_language (file_sha VARCHAR, is_english BOOLEAN)")
    >>> _ = con.execute("INSERT INTO skill_language VALUES ('sha', true)")
    >>> _ = con.execute(
    ...     "CREATE TABLE lineage_versions "
    ...     "(lineage_id VARCHAR, version_seq INTEGER, blob VARCHAR, release_date VARCHAR)"
    ... )
    >>> _ = con.execute(
    ...     "INSERT INTO lineage_versions VALUES ('Old', 1, 'sha', '2026-01-01'), "
    ...     "('New', 1, 'sha', '2026-05-01')"
    ... )
    >>> _ = con.execute("CREATE TABLE lineages (lineage_id VARCHAR, repo VARCHAR)")
    >>> _ = con.execute("INSERT INTO lineages VALUES ('Old', 'a/reg'), ('New', 'b/reg')")
    >>> _ = con.execute("CREATE TABLE registry_hubs (repo VARCHAR, is_hub BOOLEAN)")
    >>> _ = con.execute("INSERT INTO registry_hubs VALUES ('a/reg', true), ('b/reg', false)")
    >>> _ = con.execute("CREATE TABLE registry_repo_list (repo VARCHAR)")
    >>> _ = con.execute("INSERT INTO registry_repo_list VALUES ('a/reg'), ('b/reg')")
    >>> found = find_exact_links(con, True)
    >>> found.loc[0, "lineage_id"]
    'Old'
    """
    _ensure_exact_tables(con, dev)
    _require_relation(con, "registry_hubs")
    frame = con.execute(EXACT_LINKS_SQL).df()
    return _work_links(frame.to_dict(orient="records"))


def _unpack_dataset(dataset_sigs) -> tuple[list[str], np.ndarray, np.ndarray]:
    """Return ids, a uint64 matrix, and shingle counts from a signature tuple."""
    ids, matrix, sizes = dataset_sigs
    id_list = [str(item) for item in ids]
    values = np.asarray(matrix, dtype=np.uint64)
    if values.size == 0:
        values = np.zeros((0, settings.MINHASH_NUM_PERM), dtype=np.uint64)
    if values.ndim != 2 or values.shape[1] != settings.MINHASH_NUM_PERM:
        width = settings.MINHASH_NUM_PERM
        raise ValueError(f"signatures must have shape (n, {width}), got {values.shape}")
    counts = np.asarray(sizes, dtype=np.int64).reshape(-1)
    if len(id_list) != values.shape[0] or len(id_list) != counts.shape[0]:
        raise ValueError("dataset signature ids, rows and sizes do not match")
    return id_list, values, counts


def _bundle_index(registry_sigs: dict) -> dict[str, object]:
    """Align registry signatures with version rows. Cache the result on the dict.

    ``meta`` columns are ``version_id``, ``lineage_id``, ``version_seq``,
    ``release_date`` and ``is_hub``. A signature with no version row is left
    out of the containment matrix. Near hits on that id are skipped.
    """
    cached = registry_sigs.get("index")
    if isinstance(cached, dict):
        return cached
    _require_columns(
        registry_sigs["meta"],
        ("version_id", "lineage_id", "version_seq", "release_date", "is_hub"),
        "registry meta",
    )
    ids, matrix, sizes = _unpack_dataset(
        (registry_sigs["ids"], registry_sigs["signatures"], registry_sigs["sizes"])
    )
    meta = registry_sigs["meta"].drop_duplicates("version_id")
    by_row = {str(row.version_id): row for row in meta.itertuples(index=False)}
    records: list[dict[str, object]] = []
    keep: list[int] = []
    for position, version_id in enumerate(ids):
        row = by_row.get(version_id)
        if row is None:
            continue
        records.append(
            {
                "version_id": version_id,
                "lineage_id": str(row.lineage_id),
                "version_seq": int(row.version_seq),
                "release_date": _date_text(row.release_date),
                "is_hub": _hub_flag(row.is_hub),
                "repo": "" if getattr(row, "repo", None) is None else str(row.repo),
                "size": int(sizes[position]),
            }
        )
        keep.append(position)
    if keep:
        kept_matrix = np.ascontiguousarray(matrix[np.asarray(keep, dtype=int)])
    else:
        kept_matrix = np.zeros((0, settings.MINHASH_NUM_PERM), dtype=np.uint64)
    sketches = registry_sigs.get("sketches")
    if not isinstance(sketches, dict):
        sketches = {
            record["version_id"]: minhash_from_array(matrix[keep[index]])
            for index, record in enumerate(records)
        }
    packed = {
        "by_id": {str(record["version_id"]): record for record in records},
        "records": records,
        "matrix": kept_matrix,
        "sizes": np.asarray([int(record["size"]) for record in records], dtype=np.float64),
        "sketches": sketches,
    }
    registry_sigs["index"] = packed
    return packed


def _best_scored(
    options: list[tuple[dict[str, object], float, float]],
    score_at: int,
) -> dict[str, object] | None:
    """Pick the best ``(record, jaccard, containment)`` by one of the two scores.

    ``score_at`` 1 uses Jaccard. ``score_at`` 2 uses containment. Ties use
    ``_choice_key``: non-hub, then oldest release date.
    """
    best: dict[str, object] | None = None
    best_key: tuple[object, ...] | None = None
    for record, jaccard, containment in options:
        score = jaccard if score_at == 1 else containment
        key = _choice_key(
            score,
            bool(record["is_hub"]),
            _date_text(record["release_date"]),
            str(record["lineage_id"]),
            int(record["version_seq"]),
        )
        if best_key is None or key < best_key:
            best_key = key
            best = {
                **record,
                "jaccard": jaccard,
                "containment": containment,
            }
    return best


# Algorithm A25, see CODE_FLOW.md
def find_near_links(dataset_sigs, registry_lsh, registry_sigs) -> pd.DataFrame:
    """Link texts whose estimated Jaccard with a registry version is at least 0.80.

    ``dataset_sigs`` is ``(ids, signatures, sizes)`` from ``load_signatures``.
    ``registry_lsh`` is a MinHashLSH built at ``NEAR_COPY_MIN_JACCARD``. Each
    hit is scored with ``estimate_jaccard``. Hits under 0.80 are dropped, so
    an LSH false positive does not become a link. The best remaining hit wins.
    Ties: a non-hub, then the oldest ``release_date``.

    ``registry_sigs`` is a dict with ``ids``, ``signatures``, ``sizes`` and
    ``meta`` (version id, lineage, sequence, release date, is_hub).

    Example:
    >>> from skill_genealogy.text_helpers import make_minhash, minhash_to_array
    >>> values = minhash_to_array(make_minhash({"alpha beta", "beta gamma"}))
    >>> dataset = (["copy"], np.vstack([values]), np.array([2]))
    >>> meta = pd.DataFrame([{
    ...     "version_id": "A#1", "lineage_id": "A", "version_seq": 1,
    ...     "release_date": "2026-01-01", "is_hub": False,
    ... }])
    >>> bundle = {
    ...     "ids": ["A#1"], "signatures": np.vstack([values]),
    ...     "sizes": np.array([2]), "meta": meta,
    ... }
    >>> index = build_registry_lsh(["A#1"], np.vstack([values]), 0.8)
    >>> found = find_near_links(dataset, index, bundle)
    >>> (found.loc[0, "lineage_id"], found.loc[0, "link"])
    ('A', 'near')
    """
    ids, matrix, sizes = _unpack_dataset(dataset_sigs)
    if not ids:
        return _empty_work_links()
    packed = _bundle_index(registry_sigs)
    by_id: dict[str, dict[str, object]] = packed["by_id"]
    sketches: dict[str, MinHash] = packed["sketches"]
    rows: list[dict[str, object]] = []
    for position, file_sha in enumerate(ids):
        copy_size = int(sizes[position])
        if copy_size <= 0:
            continue
        sketch = minhash_from_array(matrix[position])
        options: list[tuple[dict[str, object], float, float]] = []
        for hit in _query_lsh(registry_lsh, sketch):
            record = by_id.get(hit)
            other = sketches.get(hit)
            if record is None or other is None:
                continue
            jaccard = estimate_jaccard(sketch, other)
            if jaccard < settings.NEAR_COPY_MIN_JACCARD:
                continue
            containment = estimate_containment(jaccard, int(record["size"]), copy_size)
            options.append((record, jaccard, containment))
        chosen = _best_scored(options, 1)
        if chosen is not None:
            rows.append(_work_row(file_sha, chosen, "near"))
    return _work_links(rows)


def _containment_array(
    jaccard: np.ndarray, registry_sizes: np.ndarray, copy_size: int
) -> np.ndarray:
    """Estimate containment of each registry text inside one copy.

    Same formula as ``estimate_containment``: Jaccard times the sum of the
    sizes, divided by ``(1 + Jaccard)`` times the registry size, clipped to
    0..1. A Jaccard of 0 or an empty size is 0.

    Example:
    >>> _containment_array(np.array([0.5, 0.0]), np.array([4.0, 4.0]), 8).tolist()
    [1.0, 0.0]
    """
    raw = np.zeros(jaccard.shape, dtype=np.float64)
    if copy_size <= 0:
        return raw
    valid = (registry_sizes > 0) & (jaccard > 0)
    if not bool(np.any(valid)):
        return raw
    jaccard_ok = jaccard[valid]
    size_ok = registry_sizes[valid]
    raw[valid] = jaccard_ok * (size_ok + copy_size) / ((1.0 + jaccard_ok) * size_ok)
    np.clip(raw, 0.0, 1.0, out=raw)
    return raw


def _contained_options(
    jaccard: np.ndarray,
    containment: np.ndarray,
    records: list[dict[str, object]],
) -> list[tuple[dict[str, object], float, float]]:
    """Registry rows with Jaccard under 0.80 and containment at least 0.80."""
    mask = (jaccard < settings.NEAR_COPY_MIN_JACCARD) & (
        containment >= settings.NEAR_COPY_MIN_CONTAINMENT
    )
    options: list[tuple[dict[str, object], float, float]] = []
    for index in np.flatnonzero(mask):
        position = int(index)
        options.append(
            (records[position], float(jaccard[position]), float(containment[position]))
        )
    return options


# Algorithm A26, see CODE_FLOW.md
def find_contained_links(dataset_sigs, registry_sigs) -> pd.DataFrame:
    """Link a text that kept a registry version and added its own lines.

    Containment is the share of the registry shingles found in the copy,
    estimated from Jaccard and the two shingle counts. A row is kept when
    that share is at least ``NEAR_COPY_MIN_CONTAINMENT`` and Jaccard is
    below ``NEAR_COPY_MIN_JACCARD``. The best containment wins. Ties: a
    non-hub, then the oldest release date.

    Every indexed registry version is scored. A 0.5 LSH misses the 40-line
    registry text with 60 added lines (Jaccard about 0.37, containment about
    0.95). LSH Ensemble missed that pair as well, so it is not used.

    Inputs: the same tuple and dict as ``find_near_links``, without an LSH.
    Outputs: working link rows with ``link`` ``near_contained``.

    Example:
    >>> registry = np.arange(128, dtype=np.uint64)
    >>> copy = registry.copy()
    >>> copy[40:] = np.arange(1000, 1088, dtype=np.uint64)
    >>> dataset = (["copy"], copy.reshape(1, -1), np.array([100]))
    >>> meta = pd.DataFrame([{
    ...     "version_id": "A#1", "lineage_id": "A", "version_seq": 1,
    ...     "release_date": "2026-01-01", "is_hub": False,
    ... }])
    >>> bundle = {
    ...     "ids": ["A#1"], "signatures": registry.reshape(1, -1),
    ...     "sizes": np.array([40]), "meta": meta,
    ... }
    >>> found = find_contained_links(dataset, bundle)
    >>> (found.loc[0, "link"], round(float(found.loc[0, "containment"]), 4))
    ('near_contained', 0.8333)
    """
    ids, matrix, sizes = _unpack_dataset(dataset_sigs)
    packed = _bundle_index(registry_sigs)
    records: list[dict[str, object]] = packed["records"]
    registry: np.ndarray = packed["matrix"]
    if not ids or not records:
        return _empty_work_links()
    registry_sizes: np.ndarray = packed["sizes"]
    rows: list[dict[str, object]] = []
    for position, file_sha in enumerate(ids):
        copy_size = int(sizes[position])
        if copy_size <= 0:
            continue
        agree = np.count_nonzero(registry == matrix[position], axis=1)
        jaccard = agree / settings.MINHASH_NUM_PERM
        containment = _containment_array(jaccard, registry_sizes, copy_size)
        chosen = _best_scored(_contained_options(jaccard, containment, records), 2)
        if chosen is not None:
            rows.append(_work_row(file_sha, chosen, "near_contained"))
    return _work_links(rows)


# Algorithm A27, see CODE_FLOW.md
def find_cluster_links(con: duckdb.DuckDBPyConnection, linked: pd.DataFrame) -> pd.DataFrame:
    """Give an unlinked text the lineage most common in its 0.80 cluster.

    ``linked`` rows are texts already linked by exact, near, or containment.
    They need ``file_sha``, ``lineage_id``, ``version_seq`` and
    ``release_date``. The connection must hold ``clusters`` (``file_sha``,
    ``cluster_id``, ``threshold``), ``skill_language``, and
    ``copy_text_shas``. A cluster at another threshold, such as 0.60, is
    ignored. The version is the oldest linked version of the winning lineage.
    Jaccard and containment are null: this step does not score the pair.

    Example:
    >>> con = connect_duckdb()
    >>> _ = con.execute(
    ...     "CREATE TABLE clusters (file_sha VARCHAR, cluster_id INTEGER, threshold DOUBLE)"
    ... )
    >>> _ = con.execute(
    ...     "INSERT INTO clusters VALUES ('a', 1, 0.8), ('b', 1, 0.8), ('u', 1, 0.8)"
    ... )
    >>> _ = con.execute("CREATE TABLE skill_language (file_sha VARCHAR, is_english BOOLEAN)")
    >>> _ = con.execute(
    ...     "INSERT INTO skill_language VALUES ('a', true), ('b', true), ('u', true)"
    ... )
    >>> _ = con.execute("CREATE TABLE copy_text_shas (file_sha VARCHAR)")
    >>> _ = con.execute("INSERT INTO copy_text_shas VALUES ('a'), ('b'), ('u')")
    >>> linked = pd.DataFrame([
    ...     {"file_sha": "a", "lineage_id": "A", "version_seq": 1, "release_date": "2026-03-01"},
    ...     {"file_sha": "b", "lineage_id": "A", "version_seq": 1, "release_date": "2026-03-01"},
    ... ])
    >>> found = find_cluster_links(con, linked)
    >>> found.loc[found["file_sha"] == "u", "lineage_id"].item()
    'A'
    """
    if linked.empty:
        return _empty_work_links()
    _require_columns(linked, ("file_sha", "lineage_id", "version_seq", "release_date"), "linked")
    _require_relation(con, "clusters")
    _require_relation(con, "skill_language")
    _require_relation(con, "copy_text_shas")
    work = linked.loc[:, ["file_sha", "lineage_id", "version_seq", "release_date"]].copy()
    work["file_sha"] = work["file_sha"].astype(str)
    work = work.drop_duplicates("file_sha")
    con.register("linked_texts", work)
    try:
        sql = CLUSTER_LINKS_SQL.format(threshold=float(settings.CLUSTER_LINK_THRESHOLD))
        frame = con.execute(sql).df()
    finally:
        con.unregister("linked_texts")
    return _work_links(frame.to_dict(orient="records"))


def _lookup_text(texts: dict[str, str], key: str) -> str | None:
    """Return a stored text, or None when the key was not loaded."""
    if key not in texts:
        return None
    return texts[key]


# Algorithm A28, see CODE_FLOW.md
def verify_links_with_line_diff(links: pd.DataFrame, texts_lookup) -> pd.DataFrame:
    """Set ``similarity_verified`` and drop a fuzzy link whose lines were rewritten.

    ``texts_lookup`` is a dict with ``copies`` (file_sha to text) and
    ``versions`` (``lineage_id#version_seq`` to text). An exact link is 1.0
    and is not diffed. A near, contained, or cluster link stores
    ``share_of_registry_lines_kept``. A share below
    ``MIN_VERIFIED_LINE_SHARE`` sets ``reason`` to ``line_share_below_min``.
    A missing text sets ``reason`` to ``missing_text`` and a share of 0.
    A kept row has ``reason`` null. The input rows are not modified.

    Example:
    >>> sample = pd.DataFrame([{
    ...     "file_sha": "c", "lineage_id": "A", "version_seq": 1, "link": "near",
    ...     "jaccard": 0.9, "containment": 0.9, "release_date": "2026-01-01",
    ... }])
    >>> checked = verify_links_with_line_diff(sample, {
    ...     "copies": {"c": "keep\\nchanged"},
    ...     "versions": {"A#1": "keep\\ndrop"},
    ... })
    >>> float(checked.loc[0, "similarity_verified"])
    0.5
    """
    if links.empty:
        result = links.copy()
        result["similarity_verified"] = pd.Series(dtype="float64")
        result["reason"] = pd.Series(dtype="object")
        return result
    _require_columns(links, ("file_sha", "lineage_id", "version_seq", "link"), "links")
    copies = {} if texts_lookup is None else texts_lookup.get("copies", {})
    versions = {} if texts_lookup is None else texts_lookup.get("versions", {})
    scores: list[float] = []
    reasons: list[str | None] = []
    for row in links.itertuples(index=False):
        if str(row.link) == "exact":
            scores.append(1.0)
            reasons.append(None)
            continue
        version_id = _version_id(str(row.lineage_id), int(row.version_seq))
        registry_text = _lookup_text(versions, version_id)
        copy_text = _lookup_text(copies, str(row.file_sha))
        if registry_text is None or copy_text is None:
            scores.append(0.0)
            reasons.append("missing_text")
            continue
        share = share_of_registry_lines_kept(registry_text, copy_text)
        scores.append(float(share))
        if share < settings.MIN_VERIFIED_LINE_SHARE:
            reasons.append("line_share_below_min")
        else:
            reasons.append(None)
    result = links.reset_index(drop=True).copy()
    result["similarity_verified"] = scores
    result["reason"] = pd.Series(reasons, dtype="object")
    return result


def _filter_outside(con: duckdb.DuckDBPyConnection, frame: pd.DataFrame) -> pd.DataFrame:
    """Keep rows whose ``file_sha`` is in ``copy_text_shas``. Types stay as they are."""
    if frame.empty:
        return frame
    asked = pd.DataFrame({"file_sha": frame["file_sha"].astype(str).drop_duplicates()})
    con.register("asked_shas", asked)
    try:
        found = con.execute(
            """
            SELECT asked_shas.file_sha
            FROM asked_shas
            WHERE asked_shas.file_sha IN (SELECT file_sha FROM copy_text_shas)
            """
        ).fetchall()
    finally:
        con.unregister("asked_shas")
    allowed = {str(row[0]) for row in found}
    mask = frame["file_sha"].astype(str).isin(allowed)
    return frame.loc[mask].reset_index(drop=True)


def _registry_meta(dev: bool) -> pd.DataFrame:
    """Version rows for the signature index, with the dated hub flag."""
    versions = read_parquet(_versions_path(dev))
    lineages = read_parquet(_lineages_path(dev))
    hubs = read_parquet(_registry_hubs_path(dev))
    table = _version_table(versions, lineages)
    flags = hubs.loc[:, ["repo", "is_hub"]].drop_duplicates("repo")
    merged = table.merge(flags, on="repo", how="left")
    merged["is_hub"] = merged["is_hub"].map(_hub_flag)
    columns = ["version_id", "lineage_id", "version_seq", "release_date", "is_hub", "repo"]
    return merged.loc[:, columns]


def _load_copy_bundle(dev: bool) -> dict:
    """Load registry signatures, version rows, and the 0.80 LSH."""
    ids, signatures, sizes = load_signatures(_registry_path(dev))
    meta = _registry_meta(dev)
    known = set(meta["version_id"].astype(str))
    missing = sum(1 for key in ids if str(key) not in known)
    if missing:
        print(f"warning: {missing} registry signatures have no version row", flush=True)
    near_lsh = build_registry_lsh(ids, signatures, settings.NEAR_COPY_MIN_JACCARD)
    print(
        f"registry index: {len(ids)} versions, "
        f"near LSH at {settings.NEAR_COPY_MIN_JACCARD}",
        flush=True,
    )
    return {
        "ids": ids,
        "signatures": signatures,
        "sizes": sizes,
        "meta": meta,
        "near_lsh": near_lsh,
    }


def _part_dataset(
    ids: list[str], matrix: np.ndarray, sizes: np.ndarray, positions: list[int]
):
    """Slice a signature part down to ``positions``."""
    if not positions:
        empty = np.zeros((0, settings.MINHASH_NUM_PERM), dtype=np.uint64)
        return [], empty, np.zeros((0,), dtype=np.int64)
    chosen = np.asarray(positions, dtype=int)
    return [ids[index] for index in positions], matrix[chosen], sizes[chosen]


def _search_signature_parts(
    exact: pd.DataFrame, bundle: dict
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Score every dataset part. Exact texts are not queried again."""
    exact_shas = set(exact["file_sha"].astype(str)) if len(exact) else set()
    files = _signature_paths(_dataset_dir())
    if not files:
        print("warning: dataset_signatures has no part files", flush=True)
        return _empty_work_links(), _empty_work_links()
    near_parts: list[pd.DataFrame] = []
    contained_parts: list[pd.DataFrame] = []
    linked = set(exact_shas)
    total = len(files)
    for number, path in enumerate(files, start=1):
        ids, matrix, sizes = load_signatures(path)
        positions = [index for index, sha in enumerate(ids) if sha not in linked]
        dataset = _part_dataset(ids, matrix, sizes, positions)
        near = find_near_links(dataset, bundle["near_lsh"], bundle)
        near_shas = set(near["file_sha"].astype(str)) if len(near) else set()
        rest = [index for index, sha in enumerate(dataset[0]) if sha not in near_shas]
        rest_dataset = _part_dataset(dataset[0], dataset[1], dataset[2], rest)
        contained = find_contained_links(rest_dataset, bundle)
        linked.update(near_shas)
        if len(contained):
            linked.update(contained["file_sha"].astype(str))
        near_parts.append(near)
        contained_parts.append(contained)
        print(
            f"signature part {number}/{total}: checked {len(positions)}, "
            f"near {len(near)}, contained {len(contained)}",
            flush=True,
        )
    return _concat_links(near_parts), _concat_links(contained_parts)


def _version_texts(dev: bool, links: pd.DataFrame) -> dict[str, str]:
    """Raw registry text for each linked version id."""
    if links.empty:
        return {}
    versions = read_parquet(_versions_path(dev))
    wanted = {
        _version_id(str(lineage), int(seq))
        for lineage, seq in zip(links["lineage_id"], links["version_seq"], strict=True)
    }
    versions = versions.copy()
    versions["version_id"] = [
        _version_id(str(lineage), int(seq))
        for lineage, seq in zip(versions["lineage_id"], versions["version_seq"], strict=True)
    ]
    chosen = versions.loc[versions["version_id"].isin(wanted)]
    texts = _load_blob_texts(dev, [_clean(value) for value in chosen["blob"].tolist()])
    found: dict[str, str] = {}
    for row in chosen.itertuples(index=False):
        found[str(row.version_id)] = _text_for_blob(texts, row.blob)
    return found


def _verify_from_artifacts(
    con: duckdb.DuckDBPyConnection,
    links: pd.DataFrame,
    version_texts: dict[str, str],
) -> pd.DataFrame:
    """Diff each non-exact link against its dataset text, in batches."""
    needed = pd.DataFrame({"file_sha": links["file_sha"].astype(str).drop_duplicates()})
    groups = {
        str(file_sha): frame
        for file_sha, frame in links.groupby(links["file_sha"].astype(str), sort=False)
    }
    con.register("needed_shas", needed)
    parts: list[pd.DataFrame] = []
    seen: set[str] = set()
    try:
        result = con.execute(COPY_TEXTS_SQL)
        for chunk in _iter_text_chunks(result, LINE_CHECK_BATCH_ROWS):
            copies: dict[str, str] = {}
            batch_frames: list[pd.DataFrame] = []
            for file_sha, content in zip(
                chunk["file_sha"].tolist(), chunk["content"].tolist(), strict=True
            ):
                sha = str(file_sha)
                copies[sha] = _as_text(content)
                group = groups.get(sha)
                if group is not None:
                    batch_frames.append(group)
                    seen.add(sha)
            if not batch_frames:
                continue
            batch = pd.concat(batch_frames, ignore_index=True)
            parts.append(
                verify_links_with_line_diff(
                    batch, {"copies": copies, "versions": version_texts}
                )
            )
    finally:
        con.unregister("needed_shas")
    missing = [groups[sha] for sha in groups if sha not in seen]
    if missing:
        parts.append(
            verify_links_with_line_diff(
                pd.concat(missing, ignore_index=True),
                {"copies": {}, "versions": version_texts},
            )
        )
    if not parts:
        return verify_links_with_line_diff(links, {"copies": {}, "versions": version_texts})
    return pd.concat(parts, ignore_index=True)


def _check_links(
    con: duckdb.DuckDBPyConnection, dev: bool, links: pd.DataFrame
) -> pd.DataFrame:
    """Verify fuzzy links. Exact links are marked 1.0 without reading the text."""
    if links.empty:
        return verify_links_with_line_diff(links, {"copies": {}, "versions": {}})
    exact_mask = links["link"].astype(str) == "exact"
    exact_checked = verify_links_with_line_diff(
        links.loc[exact_mask].copy(), {"copies": {}, "versions": {}}
    )
    others = links.loc[~exact_mask].copy()
    if others.empty:
        return exact_checked
    print(f"line check: {len(others)} fuzzy links", flush=True)
    checked = _verify_from_artifacts(con, others, _version_texts(dev, others))
    return pd.concat([exact_checked, checked], ignore_index=True)


def _split_checked(checked: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a verified frame into kept links and dropped links."""
    if checked.empty or "reason" not in checked.columns:
        return checked, _empty_dropped()
    dropped_mask = checked["reason"].notna()
    kept = checked.loc[~dropped_mask].reset_index(drop=True)
    dropped = checked.loc[dropped_mask, list(_DROPPED_COLUMNS)].reset_index(drop=True)
    if dropped.empty:
        return kept, _empty_dropped()
    return kept, dropped


def list_copy_files(con: duckdb.DuckDBPyConnection, links: pd.DataFrame) -> pd.DataFrame:
    """One row per file of a kept link that is not inside a registry repo.

    ``root_lineage`` comes from ``lineage_roots``. A lineage with no root row
    keeps its own id. The connection must hold ``artifacts``,
    ``lineage_roots`` and ``registry_repo_list``. Flags are not added here.
    """
    if links.empty:
        return _empty_copies().drop(
            columns=["is_fork", "same_owner_as_registry", "has_wild_history"]
        )
    columns = [
        "file_sha",
        "lineage_id",
        "version_seq",
        "link",
        "jaccard",
        "containment",
        "similarity_verified",
    ]
    _require_columns(links, tuple(columns), "links")
    _require_relation(con, "artifacts")
    _require_relation(con, "lineage_roots")
    _require_relation(con, "registry_repo_list")
    work = links.loc[:, columns].copy()
    work["file_sha"] = work["file_sha"].astype(str)
    con.register("kept_links", work)
    try:
        frame = con.execute(COPY_FILES_SQL).df()
    finally:
        con.unregister("kept_links")
    if frame.empty:
        return _empty_copies().drop(
            columns=["is_fork", "same_owner_as_registry", "has_wild_history"]
        )
    frame["version_seq"] = frame["version_seq"].astype(int)
    return frame.reset_index(drop=True)


def flag_forks_and_same_owner(
    copies: pd.DataFrame, con: duckdb.DuckDBPyConnection
) -> pd.DataFrame:
    """Add fork, same-owner, and wild-history flags. Nothing is dropped.

    ``is_fork`` is true when ``repos.is_fork`` is 1. ``same_owner_as_registry``
    is true when the text before the first ``/`` in the copy repo equals that
    of the lineage repo. ``has_wild_history`` is true when ``(repo, path)``
    is a ``(repo, target_path)`` in wild history. A missing repos row is not
    a fork. The connection must hold ``repos``, ``lineages`` and
    ``wild_versions``.

    Example:
    >>> con = connect_duckdb()
    >>> _ = con.execute("CREATE TABLE repos (full_name VARCHAR, is_fork BIGINT)")
    >>> _ = con.execute("INSERT INTO repos VALUES ('a/other', 0)")
    >>> _ = con.execute("CREATE TABLE lineages (lineage_id VARCHAR, repo VARCHAR)")
    >>> _ = con.execute("INSERT INTO lineages VALUES ('A', 'a/reg')")
    >>> _ = con.execute("CREATE TABLE wild_versions (repo VARCHAR, target_path VARCHAR)")
    >>> copies = pd.DataFrame([{
    ...     "repo": "a/other", "path": "SKILL.md", "file_sha": "sha",
    ...     "lineage_id": "A", "root_lineage": "A", "version_seq": 1,
    ...     "link": "exact", "jaccard": 1.0, "containment": 1.0,
    ...     "similarity_verified": 1.0,
    ... }])
    >>> flagged = flag_forks_and_same_owner(copies, con)
    >>> (bool(flagged.loc[0, "same_owner_as_registry"]), bool(flagged.loc[0, "is_fork"]))
    (True, False)
    """
    if copies.empty:
        return _empty_copies()
    flag_names = {"is_fork", "same_owner_as_registry", "has_wild_history"}
    needed = [name for name in _COPY_COLUMNS if name not in flag_names]
    _require_columns(copies, tuple(needed), "copies")
    _require_relation(con, "repos")
    _require_relation(con, "lineages")
    _require_relation(con, "wild_versions")
    con.register("copy_rows", copies.loc[:, needed].copy())
    try:
        frame = con.execute(FLAG_COPIES_SQL).df()
    finally:
        con.unregister("copy_rows")
    if frame.empty:
        return _empty_copies()
    frame["version_seq"] = frame["version_seq"].astype(int)
    frame["is_fork"] = frame["is_fork"].astype(bool)
    frame["same_owner_as_registry"] = frame["same_owner_as_registry"].astype(bool)
    frame["has_wild_history"] = frame["has_wild_history"].astype(bool)
    return frame.loc[:, list(_COPY_COLUMNS)].reset_index(drop=True)


def _public_text_links(links: pd.DataFrame) -> pd.DataFrame:
    """The saved text-link columns, one row per linked text."""
    if links.empty:
        frame = pd.DataFrame(columns=list(_LINK_COLUMNS))
        frame["version_seq"] = pd.Series(dtype="int64")
        frame["jaccard"] = pd.Series(dtype="float64")
        frame["containment"] = pd.Series(dtype="float64")
        frame["similarity_verified"] = pd.Series(dtype="float64")
        return frame
    frame = links.loc[:, list(_LINK_COLUMNS)].copy()
    frame["version_seq"] = frame["version_seq"].astype(int)
    return frame


def _register_copy_inputs(con: duckdb.DuckDBPyConnection, dev: bool) -> None:
    """Check copy inputs and register the views steps 4–9 read.

    ``registry_repo_list`` is every stage-2 registry, not only the lineages
    in this mode. A file in any of those repos is the registry's own file.
    """
    flag = " --dev" if dev else ""
    roles = _roles_final_path(dev)
    check_inputs_exist(
        {
            "artifacts": settings.ARTIFACTS_GLOB,
            "repos": settings.REPOS_GLOB,
            "skill_language": _language_path(),
            "lineage_versions": _versions_path(dev),
            "lineages": _lineages_path(dev),
            "lineage_roots": _roots_path(dev),
            "registry_hubs": _registry_hubs_path(dev),
            "repo_roles_final": roles,
            "registry_signatures": _registry_path(dev),
            "dataset_signatures": _dataset_dir(),
            "clusters": settings.CLUSTERS_PATH,
            "wild_history_versions": _wild_versions_path(),
            "registry_history_texts": _texts_path(dev),
        },
        {
            "artifacts": "python src/data/download_data.py",
            "repos": "python src/data/download_data.py",
            "skill_language": "python -m skill_genealogy.select_registries --force-languages",
            "lineage_versions": f"python -m skill_genealogy.build_registry_lineages{flag}",
            "lineages": f"python -m skill_genealogy.build_registry_lineages{flag}",
            "lineage_roots": f"python -m skill_genealogy.find_copies{flag} --only rehost",
            "registry_hubs": f"python -m skill_genealogy.find_copies{flag} --only rehost",
            "repo_roles_final": f"python -m skill_genealogy.find_copies{flag} --only rehost",
            "registry_signatures": f"python -m skill_genealogy.find_copies{flag} --only signatures",
            "dataset_signatures": "python -m skill_genealogy.find_copies --only signatures",
            "clusters": "main pipeline step 5a",
            "wild_history_versions": "main pipeline step 3f",
            "registry_history_texts": f"python -m skill_genealogy.download_registry_history{flag}",
        },
    )
    _register_view(con, "artifacts", settings.ARTIFACTS_GLOB)
    _register_view(con, "skill_language", str(_language_path()))
    _register_view(con, "lineage_versions", str(_versions_path(dev)))
    _register_view(con, "lineages", str(_lineages_path(dev)))
    _register_view(con, "registry_hubs", str(_registry_hubs_path(dev)))
    _register_view(con, "lineage_roots", str(_roots_path(dev)))
    _register_view(con, "repo_roles_final", str(roles))
    _register_view(con, "repos", settings.REPOS_GLOB)
    _register_view(con, "clusters", str(settings.CLUSTERS_PATH))
    _register_view(con, "wild_versions", str(_wild_versions_path()))
    con.execute(
        """
        CREATE TEMP TABLE registry_repo_list AS
        SELECT DISTINCT repo
        FROM repo_roles_final
        WHERE is_registry AND repo IS NOT NULL AND repo <> ''
        """
    )


def _save_copy_outputs(
    links: pd.DataFrame, copies: pd.DataFrame, dropped: pd.DataFrame, dev: bool
) -> None:
    """Write the three copy tables for this mode."""
    write_parquet(_public_text_links(links), _text_links_path(dev), ["file_sha"])
    saved_copies = copies if len(copies) else _empty_copies()
    write_parquet(saved_copies.loc[:, list(_COPY_COLUMNS)], _copies_path(dev), ["repo", "path"])
    saved_dropped = dropped if len(dropped) else _empty_dropped()
    write_parquet(saved_dropped.loc[:, list(_DROPPED_COLUMNS)], _dropped_path(dev), ["file_sha"])


def build_copy_candidates(dev: bool) -> pd.DataFrame:
    """Link dataset texts to registry versions and return one row per copy file.

    Steps 4–9. Exact blob, then near Jaccard, then containment, then the
    0.80 cluster, then the line-diff check. Files inside a registry repo are
    left out. Forks and same-owner copies are flagged, not dropped. The three
    parquet files are written. ``dev`` indexes only that mode's lineages.

    Outputs: ``copy_candidates.parquet``, and also ``text_links.parquet`` and
    ``links_dropped.parquet`` beside it.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="skillsig_copy_links_"))
    con = connect_duckdb()
    try:
        _prepare_duckdb(con, temp_dir)
        _register_copy_inputs(con, dev)
        # Step 4: Exact blob links (query Q5.4)
        print("Step 4: exact blob links", flush=True)
        print("listing English texts outside registry repos", flush=True)
        exact = find_exact_links(con, dev)
        print(f"exact texts: {len(exact)}", flush=True)
        # Step 5: Near links (algorithm A25)
        # Step 6: Contained links (algorithm A26)
        print("Step 5: near links", flush=True)
        print("Step 6: contained links", flush=True)
        bundle = _load_copy_bundle(dev)
        near, contained = _search_signature_parts(exact, bundle)
        near = _filter_outside(con, near)
        contained = _filter_outside(con, contained)
        print(f"near texts: {len(near)}, contained texts: {len(contained)}", flush=True)
        # Step 7: Cluster links (algorithm A27, query Q5.5)
        print("Step 7: cluster links", flush=True)
        linked = _concat_links([exact, near, contained])
        cluster = find_cluster_links(con, linked)
        print(f"cluster texts: {len(cluster)}", flush=True)
        # Step 8: Line-diff check (algorithm A28, query Q5.7)
        print("Step 8: line-diff check", flush=True)
        checked = _check_links(con, dev, _concat_links([linked, cluster]))
        kept, dropped = _split_checked(checked)
        print(f"kept texts: {len(kept)}, dropped: {len(dropped)}", flush=True)
        # Step 9: Copy files, forks, same owner (queries Q5.6, Q5.8)
        print("Step 9: copy files", flush=True)
        copies = flag_forks_and_same_owner(list_copy_files(con, kept), con)
        print(f"copy files: {len(copies)}", flush=True)
        _save_copy_outputs(kept, copies, dropped, dev)
        return copies
    finally:
        con.close()
        shutil.rmtree(temp_dir, ignore_errors=True)


def _count_column(frame: pd.DataFrame, column: str) -> dict[str, int]:
    """Value counts for one column. An empty frame returns an empty dict."""
    if frame.empty or column not in frame.columns:
        return {}
    return {str(key): int(value) for key, value in frame[column].value_counts().items()}


def _repos_without_history(copies: pd.DataFrame) -> int:
    """Distinct repos that still have a copy file missing from wild history.

    Stage 6 clones these repos. A repo whose every copy file is already in
    wild history is not counted.
    """
    if copies.empty:
        return 0
    missing = ~copies["has_wild_history"].astype(bool)
    return int(copies.loc[missing, "repo"].nunique())


def _print_copy_summary(dev: bool, elapsed: float) -> None:
    """Print link counts and two sample records from the saved files."""
    links = read_parquet(_text_links_path(dev))
    copies = read_parquet(_copies_path(dev))
    dropped = read_parquet(_dropped_path(dev))
    text_counts = _count_column(links, "link")
    file_counts = _count_column(copies, "link")
    reasons = _count_column(dropped, "reason")
    print_summary(
        "copy summary",
        {
            "linked_texts": len(links),
            "exact_texts": text_counts.get("exact", 0),
            "near_texts": text_counts.get("near", 0),
            "near_contained_texts": text_counts.get("near_contained", 0),
            "cluster_texts": text_counts.get("cluster", 0),
            "dropped_links": len(dropped),
            "dropped_line_share": reasons.get("line_share_below_min", 0),
            "dropped_missing_text": reasons.get("missing_text", 0),
            "copy_files": len(copies),
            "exact_files": file_counts.get("exact", 0),
            "near_files": file_counts.get("near", 0),
            "near_contained_files": file_counts.get("near_contained", 0),
            "cluster_files": file_counts.get("cluster", 0),
            "copy_repos": int(copies["repo"].nunique()) if len(copies) else 0,
            "fork_files": int(copies["is_fork"].sum()) if len(copies) else 0,
            "same_owner_files": int(copies["same_owner_as_registry"].sum()) if len(copies) else 0,
            "files_with_wild_history": int(copies["has_wild_history"].sum()) if len(copies) else 0,
            "repos_without_history": _repos_without_history(copies),
            "seconds": round(elapsed, 1),
        },
    )
    if dev:
        print(
            "dev sample: only lineages of the downloaded registries are indexed, "
            "so a copy of a registry outside this sample is not found.",
            flush=True,
        )
    print_sample_records(links, "text link sample")
    print_sample_records(copies, "copy file sample")


def _run_copies(dev: bool, force: bool) -> None:
    """Find dataset copies of this mode's registry lineages.

    Existing outputs are kept unless ``force`` is true. ``--force`` deletes
    only the three copy files, not the signatures or the rehost tables.
    """
    outputs = [_text_links_path(dev), _copies_path(dev), _dropped_path(dev)]
    if force:
        for path in outputs:
            if path.is_file():
                path.unlink()
    if outputs_exist(outputs):
        print("skip: copy outputs already exist", flush=True)
        _print_copy_summary(dev, 0.0)
        return
    started = time.perf_counter()
    # Step 10: dev indexes only this mode's lineages (the files above).
    build_copy_candidates(dev)
    # Step 11: summary
    _print_copy_summary(dev, time.perf_counter() - started)


def main() -> None:
    """Build signatures, re-host links, and dataset copy links.

    ``--dev`` writes registry files and copy tables to the dev folder.
    Boilerplate lines and dataset signatures always go to the full data
    folder. ``--only signatures`` runs steps 1 and 2. ``--only rehost``
    runs step 3. ``--only copies`` runs steps 4–9. ``--force`` rebuilds
    the part named by ``--only``. A signature rebuild also deletes the
    shared dataset signatures. A copies rebuild deletes only the three
    copy files.
    """
    parser = build_arg_parser(
        "Find template lines, MinHash signatures, re-host links, and dataset copies."
    )
    parser.add_argument(
        "--only",
        choices=["signatures", "rehost", "copies"],
        help="run one part; the default runs every part this stage has built",
    )
    args = parser.parse_args()
    if args.only == "copies":
        _run_copies(bool(args.dev), bool(args.force))
        return
    if args.only == "rehost":
        _run_rehost(bool(args.dev), bool(args.force))
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
    if args.only is None:
        _run_rehost(bool(args.dev), bool(args.force))
        _run_copies(bool(args.dev), bool(args.force))


if __name__ == "__main__":
    main()
