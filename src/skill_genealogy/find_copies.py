"""Stage 5a: template lines and MinHash signatures.

What: count the body lines shared by many GitHub owners (template text such
as "## when to use"), then build a MinHash signature, with those lines removed,
for every distinct English dataset text and every registry lineage version.
Stage 5b (G13) and 5c (G14) compare the signatures; this stage only builds them.

Inputs (always the full data folder, also with ``--dev``): the GitSkills
artifacts (``settings.ARTIFACTS_GLOB``), ``skill_language.parquet`` (stage 2a)
and ``registry_history/registry_history_texts.parquet`` (stage 3). In the dev or
full data folder: ``lineage_versions.parquet`` (stage 4a).

Outputs:
- ``boilerplate_lines.parquet`` (full data folder, shared by dev and full):
  ``line, owners`` for lines with ``owners >= BOILERPLATE_MIN_OWNERS``.
- ``dataset_signatures/part_sXX_cYYYYY.npz`` (full data folder, shared): arrays
  ``ids`` (``file_sha``), ``signatures`` uint64 ``[n, 128]``, ``sizes`` int32.
- ``registry_signatures.npz`` (dev or full data folder): the same arrays, with
  ids ``lineage_id#version_seq``.

The dataset outputs do not depend on the registries, so ``--dev`` changes only
the registry file. Dataset signatures take more than an hour on full data: run
them in the background.

Run (no network):
    python -m skill_genealogy.find_copies --dev --only signatures
    python -m skill_genealogy.find_copies --only signatures --force
"""

from __future__ import annotations

import multiprocessing
import multiprocessing.pool
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from skill_genealogy import settings
from skill_genealogy.build_registry_lineages import history_dir, read_texts
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

# Distinct English texts are read in this many hash slices (same split as
# stage 2a), so one group-by never holds every skill string at once.
TEXT_READ_PARTS = 4
# Skill texts exploded into lines at once. One query over every text spilled
# more than 100 GB, because each line row kept its whole text.
LINE_BATCH_ROWS = 2_000
# DuckDB spills a large group-by to disk above this memory, up to this disk size.
DUCKDB_MEMORY_LIMIT = "8GB"
DUCKDB_TEMP_LIMIT = "40GB"
# One dataset .npz holds this many distinct English texts.
SIGNATURE_CHUNK_ROWS = 50_000
# DuckDB fetch_df_chunk counts vectors; one vector is 2,048 rows.
DUCKDB_VECTOR_ROWS = 2048
# Lists shorter than this are signed in this process (the path the tests use).
POOL_MIN_TEXTS = 512
# Texts each pool worker takes at once.
POOL_CHUNKSIZE = 32
# Template lines printed for the eye check, and kept lines nearest the cutoff.
TOP_LINE_COUNT = 30
CUTOFF_LINE_COUNT = 5
# Empty-text examples stored in each .npz and printed, and their preview length.
EMPTY_EXAMPLE_COUNT = 2
EMPTY_PREVIEW_CHARS = 80
# Finished dataset part files. A partial write has another name, so a crash
# cannot be loaded as a finished part.
PART_GLOB = "part_s??_c?????.npz"

# The boilerplate set of one pool worker process (set by _init_sign_worker).
_WORKER_BOILERPLATE: set[str] = set()

# Query Q5.1, see code_flow/09_boilerplate_signatures.md
# The owner of each English file_sha, read once without the skill text.
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

# Query Q5.1, see code_flow/09_boilerplate_signatures.md
# batch_lines is (file_sha, line key) of one batch of texts; the key is built in
# Python with the same rule signature_for_text uses to remove a line.
LINE_BATCH_SQL = """
SELECT DISTINCT file_owners.owner AS owner, batch_lines.line AS line
FROM batch_lines
JOIN file_owners ON batch_lines.file_sha = file_owners.file_sha
"""

# Query Q5.1, see code_flow/09_boilerplate_signatures.md
LINE_OWNER_ROLLUP_SQL = """
SELECT line, count(DISTINCT owner)::BIGINT AS owners
FROM read_parquet({glob})
GROUP BY line
HAVING count(DISTINCT owner) >= {min_owners}
"""

# Query Q5.2, see code_flow/09_boilerplate_signatures.md
# Distinct English texts of one hash slice. {order} is "ORDER BY file_sha" for
# signing (stable chunk files, so a stopped run can resume) and empty for the
# owner count (no sort of every string).
ENGLISH_SLICE_TEXTS_SQL = """
SELECT artifacts.file_sha AS file_sha, any_value(artifacts.content) AS content
FROM artifacts
JOIN skill_language ON artifacts.file_sha = skill_language.file_sha
WHERE skill_language.is_english
  AND artifacts.file_sha IS NOT NULL
  AND artifacts.content IS NOT NULL
  AND hash(artifacts.file_sha) % {parts} = {part}
GROUP BY artifacts.file_sha
{order}
"""

# Query Q5.2, see code_flow/09_boilerplate_signatures.md
ENGLISH_SLICE_COUNT_SQL = """
SELECT count(DISTINCT artifacts.file_sha)::BIGINT AS texts
FROM artifacts
JOIN skill_language ON artifacts.file_sha = skill_language.file_sha
WHERE skill_language.is_english
  AND artifacts.file_sha IS NOT NULL
  AND artifacts.content IS NOT NULL
  AND hash(artifacts.file_sha) % {parts} = {part}
"""

# The registry texts are read with query Q4.2 (build_registry_lineages.read_texts),
# one batch of LINEAGE_TEXT_BATCH blobs at a time.


# Paths


def boilerplate_path() -> Path:
    """``boilerplate_lines.parquet`` in the full data folder (shared by dev and full)."""
    return settings.data_dir(False) / "boilerplate_lines.parquet"


def dataset_signatures_dir() -> Path:
    """``dataset_signatures/`` in the full data folder (shared by dev and full)."""
    return settings.data_dir(False) / "dataset_signatures"


def registry_signatures_path(dev: bool) -> Path:
    """``registry_signatures.npz`` in the dev or full data folder."""
    return settings.data_dir(dev) / "registry_signatures.npz"


def _versions_path(dev: bool) -> Path:
    """``lineage_versions.parquet`` (stage 4a) in the dev or full data folder."""
    return settings.data_dir(dev) / "lineage_versions.parquet"


def _texts_path() -> Path:
    """The stage 3 texts table; there is no dev registry history."""
    return history_dir() / "registry_history_texts.parquet"


def _language_path() -> Path:
    """``skill_language.parquet`` (stage 2a) in the full data folder."""
    return settings.data_dir(False) / "skill_language.parquet"


def _quote_path(path: str) -> str:
    """A single-quoted SQL string literal with quotes escaped.

    >>> _quote_path("it's.parquet")
    "'it''s.parquet'"
    """
    return "'" + path.replace("'", "''") + "'"


# Step 0: read the inputs


def _prepare_duckdb(con: duckdb.DuckDBPyConnection, temp_dir: Path) -> None:
    """Give the connection a memory cap and a spill folder, and no progress bar.

    ``preserve_insertion_order`` is off so a group-by does not keep the input
    order; queries that need an order say ``ORDER BY``.
    """
    temp_dir.mkdir(parents=True, exist_ok=True)
    con.execute("SET enable_progress_bar = false")
    con.execute("SET preserve_insertion_order = false")
    con.execute(f"SET memory_limit = '{DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET max_temp_directory_size = '{DUCKDB_TEMP_LIMIT}'")
    con.execute(f"SET temp_directory = {_quote_path(str(temp_dir))}")


def read_inputs(con: duckdb.DuckDBPyConnection, dev: bool) -> None:
    """Check the four inputs and register the views ``artifacts`` and ``skill_language``.

    A missing file raises ``FileNotFoundError`` naming the command that builds it.
    """
    flag = " --dev" if dev else ""
    check_inputs_exist(
        {
            "artifacts": settings.ARTIFACTS_GLOB,
            "skill_language": _language_path(),
            "lineage_versions": _versions_path(dev),
            "registry_history_texts": _texts_path(),
        },
        {
            "artifacts": "python src/data/download_data.py",
            "skill_language": "python -m skill_genealogy.select_registries --force-languages",
            "lineage_versions": f"python -m skill_genealogy.build_registry_lineages{flag}",
            "registry_history_texts": "python -m skill_genealogy.download_registry_history",
        },
    )
    for name, path in (
        ("artifacts", settings.ARTIFACTS_GLOB),
        ("skill_language", str(_language_path())),
    ):
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet({_quote_path(path)})"
        )


# Step 1: count owners per line (Algorithm A22)


def _as_text(value: object) -> str:
    """A skill string, or ``""`` for a missing cell.

    >>> _as_text(None), _as_text(float("nan")), _as_text("x")
    ('', '', 'x')
    """
    return value if isinstance(value, str) else ""


def _body_line_keys(text: str) -> list[str]:
    """The line keys ``signature_for_text`` can remove, one per body line.

    The key is ``line.strip().lower()`` of a ``body_lines`` row (normalised
    text, front matter left out). Lines under ``BOILERPLATE_MIN_WORDS`` words
    are left out.

    >>> _body_line_keys("---\\nname: x\\n---\\n## Usage\\nRun The Tool now\\n")
    ['run the tool now']
    """
    keys: list[str] = []
    for line in body_lines(text):
        key = line.strip().lower()
        if len(key.split()) >= settings.BOILERPLATE_MIN_WORDS:
            keys.append(key)
    return keys


def _empty_line_frame() -> pd.DataFrame:
    """An empty ``line, owners`` table."""
    return pd.DataFrame({"line": pd.Series(dtype="object"), "owners": pd.Series(dtype="int64")})


def _ranked_lines(frame: pd.DataFrame) -> pd.DataFrame:
    """Lines sorted by owner count, highest first, then by the line text."""
    if frame.empty:
        return _empty_line_frame()
    ranked = frame.loc[:, ["line", "owners"]].copy()
    ranked["line"] = ranked["line"].astype(str)
    ranked["owners"] = ranked["owners"].astype("int64")
    return ranked.sort_values(
        ["owners", "line"], ascending=[False, True], kind="mergesort"
    ).reset_index(drop=True)


def _write_line_batch(writer: duckdb.DuckDBPyConnection, frame: pd.DataFrame, dest: Path) -> None:
    """Write the distinct ``(owner, line)`` pairs of one batch of texts to ``dest``."""
    rows = [
        (str(sha), key)
        for sha, content in zip(frame["file_sha"].tolist(), frame["content"].tolist(), strict=True)
        for key in _body_line_keys(_as_text(content))
    ]
    if not rows:
        return
    writer.register(
        "batch_lines", pd.DataFrame(rows, columns=["file_sha", "line"]).drop_duplicates()
    )
    try:
        writer.execute(
            f"COPY ({LINE_BATCH_SQL}) TO {_quote_path(str(dest))} "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        writer.unregister("batch_lines")


def _collect_owner_pairs(con: duckdb.DuckDBPyConnection, folder: Path) -> None:
    """Write one ``pairs_*.parquet`` file of ``(owner, line)`` per batch of English texts.

    The owners go to a second connection: a second query on ``con`` would
    cancel the open text scan.
    """
    owners_path = folder / "file_owners.parquet"
    con.execute(f"COPY ({FILE_OWNERS_SQL}) TO {_quote_path(str(owners_path))} (FORMAT PARQUET)")
    writer = connect_duckdb()
    writer.execute("SET enable_progress_bar = false")
    writer.execute(
        f"CREATE TABLE file_owners AS SELECT * FROM read_parquet({_quote_path(str(owners_path))})"
    )
    try:
        batch = 0
        for part in range(TEXT_READ_PARTS):
            print(f"line owners: slice {part + 1} of {TEXT_READ_PARTS}", flush=True)
            sql = ENGLISH_SLICE_TEXTS_SQL.format(parts=TEXT_READ_PARTS, part=part, order="")
            for frame in _iter_text_chunks(con.execute(sql), LINE_BATCH_ROWS):
                _write_line_batch(writer, frame, folder / f"pairs_{batch:05d}.parquet")
                batch += 1
                if batch % 50 == 0:
                    print(f"line owners: {batch} batches of {LINE_BATCH_ROWS} texts", flush=True)
    finally:
        writer.close()


def _rollup_line_owners(folder: Path, min_owners: int) -> pd.DataFrame:
    """Count distinct owners per line over the pair files; keep ``owners >= min_owners``.

    A fresh connection does the count: the scan connection's buffer pool is
    full of skill text, and counting there ran out of the 8 GB limit.
    """
    if not list(folder.glob("pairs_*.parquet")):
        return _empty_line_frame()
    print("line owners: rollup", flush=True)
    rollup = connect_duckdb()
    try:
        _prepare_duckdb(rollup, folder / "rollup_temp")
        rollup.execute("SET threads = 2")
        sql = LINE_OWNER_ROLLUP_SQL.format(
            glob=_quote_path(str(folder / "pairs_*.parquet")), min_owners=min_owners
        )
        return _ranked_lines(rollup.execute(sql).df())
    finally:
        rollup.close()


# Algorithm A22, see code_flow/09_boilerplate_signatures.md
def count_owners_per_line(con: duckdb.DuckDBPyConnection, min_owners: int = 1) -> pd.DataFrame:
    """Count the distinct GitHub owners of each body line of the English skill texts.

    The owner is the text before the first ``/`` of ``repo_full_name``. The
    line key is ``line.strip().lower()`` of each ``body_lines`` row, the key
    ``signature_for_text`` removes. Lines under ``BOILERPLATE_MIN_WORDS`` words
    are dropped. An owner who has a line twice counts once; a text whose
    ``is_english`` is false is not read. Lines under ``min_owners`` are left
    out (``main`` passes ``BOILERPLATE_MIN_OWNERS`` so millions of one-owner
    lines never reach pandas).

    Inputs: a connection with ``artifacts`` and ``skill_language``.
    Outputs: ``line, owners``, highest owner count first.

    >>> con = connect_duckdb()
    >>> _ = con.execute("CREATE TABLE artifacts "
    ...                 "(repo_full_name VARCHAR, file_sha VARCHAR, content VARCHAR)")
    >>> _ = con.execute("INSERT INTO artifacts VALUES ('a/x', 's1', 'use this shared skill'), "
    ...                 "('b/y', 's2', 'use this shared skill')")
    >>> _ = con.execute("CREATE TABLE skill_language AS SELECT * FROM "
    ...                 "(VALUES ('s1', true), ('s2', true)) t(file_sha, is_english)")
    >>> frame = count_owners_per_line(con)  # doctest: +ELLIPSIS
    line owners: ...
    >>> frame.to_dict("records")
    [{'line': 'use this shared skill', 'owners': 2}]
    """
    if min_owners < 1:
        raise ValueError(f"min_owners must be >= 1, got {min_owners}")
    con.execute("SET enable_progress_bar = false")
    with tempfile.TemporaryDirectory(prefix="genealogy_line_owners_") as name:
        folder = Path(name)
        _collect_owner_pairs(con, folder)
        return _rollup_line_owners(folder, min_owners)


def build_boilerplate_set(line_owners: pd.DataFrame, min_owners: int) -> set[str]:
    """The lines shared by at least ``min_owners`` owners.

    >>> sample = pd.DataFrame({"line": ["use this skill", "only once here"], "owners": [50, 2]})
    >>> build_boilerplate_set(sample, 50)
    {'use this skill'}
    """
    if min_owners < 1:
        raise ValueError(f"min_owners must be >= 1, got {min_owners}")
    if line_owners.empty:
        return set()
    kept = line_owners.loc[line_owners["owners"] >= min_owners, "line"]
    return {str(line) for line in kept.tolist()}


# Step 2: build MinHash signatures


def signature_for_text(text: str, boilerplate: set[str]) -> tuple[np.ndarray, int]:
    """One MinHash signature of a skill body without its boilerplate lines, and its shingle count.

    normalise -> body (front matter removed) -> drop boilerplate lines ->
    5-word shingles -> 128-value MinHash (``uint64``). Two texts that differ
    only by a boilerplate line get the same signature. A body with no shingle
    left returns a zero array and 0: that is not a signature, and callers
    must not store it.

    >>> lines = {"please install the tool"}
    >>> left = signature_for_text("alpha beta gamma delta epsilon\\nPlease install the tool", lines)
    >>> right = signature_for_text("alpha beta gamma delta epsilon", lines)
    >>> left[1] == right[1] == 1 and bool(np.array_equal(left[0], right[0]))
    True
    >>> signature_for_text("Please install the tool", lines)[1]
    0
    """
    kept = remove_boilerplate_lines(body_lines(_as_text(text)), boilerplate)
    shingles = make_word_shingles("\n".join(kept))
    if not has_comparable_text(shingles):
        return np.zeros(settings.MINHASH_NUM_PERM, dtype=np.uint64), 0
    return minhash_to_array(make_minhash(shingles)), len(shingles)


def _init_sign_worker(lines: list[str]) -> None:
    """Store the boilerplate set in this pool worker."""
    global _WORKER_BOILERPLATE
    _WORKER_BOILERPLATE = set(lines)


def _sign_text(text: str) -> tuple[np.ndarray, int]:
    """``signature_for_text`` with the worker's stored boilerplate set."""
    return signature_for_text(text, _WORKER_BOILERPLATE)


def _open_sign_pool(boilerplate: set[str], workers: int | None = None) -> multiprocessing.pool.Pool:
    """Start ``DOWNLOAD_WORKERS`` processes that sign with this boilerplate set.

    ``python -m`` runs this file as ``__main__``, but a spawned worker imports
    ``skill_genealogy.find_copies``, so the pool calls that module's functions.
    """
    import skill_genealogy.find_copies as module

    return multiprocessing.Pool(
        processes=workers or settings.DOWNLOAD_WORKERS,
        initializer=module._init_sign_worker,
        initargs=(sorted(boilerplate),),
    )


def _sign_many(
    texts: list[str], boilerplate: set[str], pool: multiprocessing.pool.Pool | None = None
) -> list[tuple[np.ndarray, int]]:
    """Sign each text, in order. Under ``POOL_MIN_TEXTS`` texts stay in this process."""
    if len(texts) < POOL_MIN_TEXTS:
        return [signature_for_text(text, boilerplate) for text in texts]
    import skill_genealogy.find_copies as module

    if pool is not None:
        return pool.map(module._sign_text, texts, chunksize=POOL_CHUNKSIZE)
    with _open_sign_pool(boilerplate, min(settings.DOWNLOAD_WORKERS, len(texts))) as owned:
        return owned.map(module._sign_text, texts, chunksize=POOL_CHUNKSIZE)


def _preview(text: str) -> str:
    """The first ``EMPTY_PREVIEW_CHARS`` characters of the normalised text, on one line."""
    return normalize_text(text).replace("\n", " ")[:EMPTY_PREVIEW_CHARS]


def _pack_signatures(
    ids: list[str], texts: list[str], signed: list[tuple[np.ndarray, int]]
) -> tuple[list[str], np.ndarray, np.ndarray, int, list[str]]:
    """Keep the texts that have a signature; count the empty ones.

    Returns kept ids, the ``uint64 [n, 128]`` matrix, int32 sizes, the empty
    count and up to ``EMPTY_EXAMPLE_COUNT`` examples ``"id | preview"``.
    """
    kept = [index for index, (_values, size) in enumerate(signed) if size > 0]
    empty = [index for index, (_values, size) in enumerate(signed) if size <= 0]
    if kept:
        matrix = np.vstack([signed[index][0] for index in kept]).astype(np.uint64, copy=False)
    else:
        matrix = np.zeros((0, settings.MINHASH_NUM_PERM), dtype=np.uint64)
    sizes = np.asarray([signed[index][1] for index in kept], dtype=np.int32)
    examples = [f"{ids[index]} | {_preview(texts[index])}" for index in empty[:EMPTY_EXAMPLE_COUNT]]
    return [ids[index] for index in kept], matrix, sizes, len(empty), examples


def _write_signature_file(
    path: Path,
    ids: list[str],
    signatures: np.ndarray,
    sizes: np.ndarray,
    empty_count: int,
    examples: list[str],
) -> None:
    """Write one ``.npz``: ``ids``, ``signatures``, ``sizes``, ``empty_count``, ``empty_examples``.

    The file is written under a hidden name and renamed, so a crash never
    leaves a file that looks finished.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.tmp.npz")
    np.savez_compressed(
        temporary,
        ids=np.asarray(ids, dtype=str),
        signatures=np.asarray(signatures, dtype=np.uint64).reshape(-1, settings.MINHASH_NUM_PERM),
        sizes=np.asarray(sizes, dtype=np.int32),
        empty_count=np.asarray([empty_count], dtype=np.int64),
        empty_examples=np.asarray(examples[:EMPTY_EXAMPLE_COUNT], dtype=str),
    )
    temporary.replace(path)


def _iter_text_chunks(result: duckdb.DuckDBPyConnection, chunk_rows: int) -> Iterator[pd.DataFrame]:
    """Yield frames of ``chunk_rows`` rows (the last may be shorter) from an open query."""
    vectors = max(1, -(-chunk_rows // DUCKDB_VECTOR_ROWS))
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
            yield block.iloc[:chunk_rows].reset_index(drop=True)
            rest = block.iloc[chunk_rows:].reset_index(drop=True)
            pending, pending_rows = ([rest] if len(rest) else []), len(rest)
    if pending_rows:
        yield pd.concat(pending, ignore_index=True)


def _part_path(out_dir: Path, part: int, chunk: int) -> Path:
    """``part_sXX_cYYYYY.npz``: slice ``part``, chunk ``chunk``."""
    return out_dir / f"part_s{part:02d}_c{chunk:05d}.npz"


def _sign_slice(
    con: duckdb.DuckDBPyConnection,
    boilerplate: set[str],
    out_dir: Path,
    part: int,
    pool_holder: list[multiprocessing.pool.Pool | None],
) -> None:
    """Sign one hash slice in chunks of ``SIGNATURE_CHUNK_ROWS``; skip chunks already on disk.

    The texts are sorted by ``file_sha``, so chunk ``n`` always holds the same
    texts and a stopped run can resume. The pool starts on the first large
    chunk and is reused by later ones.
    """
    row = con.execute(ENGLISH_SLICE_COUNT_SQL.format(parts=TEXT_READ_PARTS, part=part)).fetchone()
    n_texts = int(row[0]) if row else 0
    n_chunks = -(-n_texts // SIGNATURE_CHUNK_ROWS)
    label = f"dataset slice {part + 1}/{TEXT_READ_PARTS}"
    if all(_part_path(out_dir, part, index).is_file() for index in range(n_chunks)):
        print(f"{label}: {n_texts} texts, all {n_chunks} chunks exist, skip", flush=True)
        return
    print(f"{label}: {n_texts} texts, {n_chunks} chunks", flush=True)
    sql = ENGLISH_SLICE_TEXTS_SQL.format(
        parts=TEXT_READ_PARTS, part=part, order="ORDER BY artifacts.file_sha"
    )
    for chunk, frame in enumerate(_iter_text_chunks(con.execute(sql), SIGNATURE_CHUNK_ROWS)):
        path = _part_path(out_dir, part, chunk)
        if path.is_file():
            print(f"{label} chunk {chunk}: exists, skip", flush=True)
            continue
        started = time.perf_counter()
        if len(frame) >= POOL_MIN_TEXTS and pool_holder[0] is None:
            pool_holder[0] = _open_sign_pool(boilerplate)
        ids = [str(value) for value in frame["file_sha"].tolist()]
        texts = [_as_text(value) for value in frame["content"].tolist()]
        packed = _pack_signatures(ids, texts, _sign_many(texts, boilerplate, pool_holder[0]))
        _write_signature_file(path, *packed)
        print(
            f"{label} chunk {chunk}: {len(frame)} texts, signed {len(packed[0])}, "
            f"empty {packed[3]}, {time.perf_counter() - started:.1f}s",
            flush=True,
        )


def build_dataset_signatures(
    con: duckdb.DuckDBPyConnection, boilerplate: set[str], out_dir: Path
) -> int:
    """Sign every distinct English dataset text; return how many signatures are stored.

    Texts are read in ``TEXT_READ_PARTS`` hash slices and written in chunks of
    ``SIGNATURE_CHUNK_ROWS`` (one ``.npz`` each). A chunk file that exists is
    skipped, so a stopped run continues. A text with no shingle left after
    boilerplate removal is counted in its file, not stored.

    Inputs: a connection with ``artifacts`` and ``skill_language``, the
    boilerplate set, the output folder.
    Outputs: ``part_sXX_cYYYYY.npz`` files; the number of stored signatures.
    """
    con.execute("SET enable_progress_bar = false")
    out_dir.mkdir(parents=True, exist_ok=True)
    pool_holder: list[multiprocessing.pool.Pool | None] = [None]
    try:
        for part in range(TEXT_READ_PARTS):
            _sign_slice(con, boilerplate, out_dir, part, pool_holder)
    finally:
        if pool_holder[0] is not None:
            pool_holder[0].close()
            pool_holder[0].join()
    return read_signature_report(out_dir)[0]


def _version_ids_and_blobs(versions: pd.DataFrame) -> tuple[list[str], list[str]]:
    """``lineage_id#version_seq`` and the blob sha of each version row, in row order."""
    missing = [
        name for name in ("lineage_id", "version_seq", "blob") if name not in versions.columns
    ]
    if missing:
        raise ValueError(f"versions is missing columns: {', '.join(missing)}")
    ids = [
        f"{lineage}#{int(seq)}"
        for lineage, seq in zip(versions["lineage_id"], versions["version_seq"], strict=True)
    ]
    return ids, [_as_text(blob) for blob in versions["blob"].tolist()]


def build_registry_signatures(
    versions: pd.DataFrame, texts: dict[str, str], boilerplate: set[str], out_path: Path
) -> int:
    """Sign every lineage version; return how many signatures are stored.

    The id is ``lineage_id#version_seq`` and rows keep the order of
    ``versions``. A version whose blob has no text in ``texts``, or no shingle
    left, is counted as empty and not stored.

    Inputs: version rows (``lineage_id``, ``version_seq``, ``blob``), blob sha ->
    raw text, the boilerplate set, the output path.
    Outputs: one ``.npz`` file; the number of stored signatures.
    """
    ids, blobs = _version_ids_and_blobs(versions)
    bodies = [texts.get(blob, "") for blob in blobs]
    packed = _pack_signatures(ids, bodies, _sign_many(bodies, boilerplate))
    _write_signature_file(out_path, *packed)
    return len(packed[0])


def sign_registry_in_batches(
    versions: pd.DataFrame, texts_file: Path, boilerplate: set[str], out_path: Path, batch_rows: int
) -> int:
    """``build_registry_signatures`` with only ``batch_rows`` texts in memory at a time.

    The kept registry versions have about 9 GB of ``SKILL.md`` text, more than
    this machine holds as Python strings, so the texts of each batch are read
    with query Q4.2 and freed before the next. The file is the same as one
    ``build_registry_signatures`` call with every text.
    """
    ids, blobs = _version_ids_and_blobs(versions)
    kept_ids: list[str] = []
    blocks: list[np.ndarray] = []
    size_blocks: list[np.ndarray] = []
    empty_count = 0
    examples: list[str] = []
    pool = _open_sign_pool(boilerplate) if len(ids) >= POOL_MIN_TEXTS else None
    try:
        for start in range(0, len(ids), batch_rows):
            batch_blobs = blobs[start : start + batch_rows]
            texts = read_texts(texts_file, sorted(set(batch_blobs)))
            bodies = [texts.get(blob, "") for blob in batch_blobs]
            del texts
            batch_ids = ids[start : start + batch_rows]
            packed = _pack_signatures(batch_ids, bodies, _sign_many(bodies, boilerplate, pool))
            kept_ids.extend(packed[0])
            blocks.append(packed[1])
            size_blocks.append(packed[2])
            empty_count += packed[3]
            examples.extend(packed[4][: EMPTY_EXAMPLE_COUNT - len(examples)])
            print(f"registry signatures: {start + len(batch_ids)}/{len(ids)}", flush=True)
    finally:
        if pool is not None:
            pool.close()
            pool.join()
    matrix = (
        np.vstack(blocks) if blocks else np.zeros((0, settings.MINHASH_NUM_PERM), dtype=np.uint64)
    )
    sizes = np.concatenate(size_blocks) if size_blocks else np.zeros((0,), dtype=np.int32)
    _write_signature_file(out_path, kept_ids, matrix, sizes, empty_count, examples)
    return len(kept_ids)


def _signature_files(path: Path) -> list[Path]:
    """The one file, or every finished part file of a folder in name order."""
    if path.is_dir():
        return sorted(path.glob(PART_GLOB))
    return [path] if path.is_file() else []


def _load_one(path: Path) -> tuple[list[str], np.ndarray, np.ndarray]:
    """ids, signatures and sizes of one ``.npz``; raises ``ValueError`` on a bad shape."""
    with np.load(path, allow_pickle=False) as data:
        ids = [str(item) for item in np.asarray(data["ids"]).reshape(-1).tolist()]
        signatures = np.asarray(data["signatures"], dtype=np.uint64).reshape(
            -1, settings.MINHASH_NUM_PERM
        )
        sizes = np.asarray(data["sizes"], dtype=np.int32).reshape(-1)
    if signatures.shape[0] != len(ids) or sizes.shape[0] != len(ids):
        raise ValueError(f"{path}: id, signature and size counts do not match")
    return ids, signatures, sizes


def load_signatures(path_or_dir: Path) -> tuple[list[str], np.ndarray, np.ndarray]:
    """Load ``(ids, signatures uint64 [n, 128], sizes int32)`` from one ``.npz`` or a part folder.

    A folder is read part by part in name order. A folder without part files
    returns empty arrays; a missing path raises ``FileNotFoundError``. The
    full dataset folder is about 1.7 GB of hash values: pass one part file
    when the whole folder is not needed.
    """
    if not path_or_dir.exists():
        raise FileNotFoundError(path_or_dir)
    ids: list[str] = []
    blocks = [np.zeros((0, settings.MINHASH_NUM_PERM), dtype=np.uint64)]
    size_blocks = [np.zeros((0,), dtype=np.int32)]
    for path in _signature_files(path_or_dir):
        file_ids, signatures, sizes = _load_one(path)
        ids.extend(file_ids)
        blocks.append(signatures)
        size_blocks.append(sizes)
    return ids, np.vstack(blocks), np.concatenate(size_blocks)


def read_signature_report(path: Path) -> tuple[int, int, list[str]]:
    """Stored signatures, empty texts and up to two empty examples of a file or folder.

    Reads only the small arrays, not the signature matrices. A missing path counts 0.
    """
    signed, empty, examples = 0, 0, []
    for file in _signature_files(path):
        with np.load(file, allow_pickle=False) as data:
            signed += int(np.asarray(data["ids"]).shape[0])
            empty += int(np.asarray(data["empty_count"]).reshape(-1)[0])
            stored = [str(item) for item in np.asarray(data["empty_examples"]).reshape(-1).tolist()]
            examples.extend(stored[: EMPTY_EXAMPLE_COUNT - len(examples)])
    return signed, empty, examples


# Step 3: save outputs and print the summary


def save_outputs(line_owners: pd.DataFrame, path: Path) -> None:
    """Write ``line, owners`` to ``path``, highest owner count first."""
    write_parquet(_ranked_lines(line_owners), path)


def _delete_signature_outputs(dev: bool) -> None:
    """``--force``: delete the boilerplate list, the dataset parts and this mode's registry file.

    The first two are shared by dev and full, so a dev ``--force`` rebuilds them too.
    """
    print(
        "force: deleting boilerplate_lines, dataset_signatures and registry_signatures", flush=True
    )
    for path in (boilerplate_path(), registry_signatures_path(dev)):
        path.unlink(missing_ok=True)
    folder = dataset_signatures_dir()
    if folder.is_dir():
        for path in [*folder.glob(PART_GLOB), *folder.glob(".part_s*.tmp.npz")]:
            path.unlink()


def _load_or_count_lines(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """The boilerplate table: read it when it exists, else count and save it."""
    path = boilerplate_path()
    if path.is_file():
        print("skip: boilerplate_lines.parquet exists", flush=True)
        return read_parquet(path)
    frame = count_owners_per_line(con, settings.BOILERPLATE_MIN_OWNERS)
    save_outputs(frame, path)
    return frame


def _load_or_sign_registry(dev: bool, boilerplate: set[str]) -> None:
    """Sign the registry versions unless this mode's ``registry_signatures.npz`` exists."""
    path = registry_signatures_path(dev)
    if path.is_file():
        print("skip: registry_signatures.npz exists", flush=True)
        return
    versions = read_parquet(_versions_path(dev))
    print(f"registry versions to sign: {len(versions)}", flush=True)
    sign_registry_in_batches(
        versions, _texts_path(), boilerplate, path, settings.LINEAGE_TEXT_BATCH
    )


def _print_boilerplate(line_owners: pd.DataFrame) -> None:
    """Print the top template lines and the kept lines nearest the cutoff, for the eye check."""
    ranked = _ranked_lines(line_owners)
    print(f"top {TOP_LINE_COUNT} boilerplate lines (owners, line)", flush=True)
    for row in ranked.head(TOP_LINE_COUNT).itertuples(index=False):
        print(f"{row.owners:7d}  {row.line}", flush=True)
    print("kept lines nearest the owner cutoff", flush=True)
    for row in ranked.tail(CUTOFF_LINE_COUNT).itertuples(index=False):
        print(f"{row.owners:7d}  {row.line}", flush=True)


def _signature_sample(path: Path) -> pd.DataFrame:
    """The first two stored signatures of a file or folder: id, shingle count, first hash value."""
    rows: list[dict[str, object]] = []
    for file in _signature_files(path):
        ids, signatures, sizes = _load_one(file)
        for index in range(min(2 - len(rows), len(ids))):
            rows.append(
                {
                    "id": ids[index],
                    "shingle_count": int(sizes[index]),
                    "hash0": int(signatures[index, 0]),
                }
            )
        if len(rows) >= 2:
            break
    return pd.DataFrame(rows, columns=["id", "shingle_count", "hash0"])


def _print_run_summary(line_owners: pd.DataFrame, dev: bool, elapsed: float) -> None:
    """Print the counts, the empty-text examples and two signature records of each kind."""
    dataset = read_signature_report(dataset_signatures_dir())
    registry = read_signature_report(registry_signatures_path(dev))
    print_summary(
        "stage 5a signatures",
        {
            "boilerplate_min_owners": settings.BOILERPLATE_MIN_OWNERS,
            "boilerplate_lines": len(line_owners),
            "dataset_texts_signed": dataset[0],
            "dataset_texts_empty": dataset[1],
            "registry_versions_signed": registry[0],
            "registry_versions_empty": registry[1],
            "seconds": round(elapsed, 1),
        },
    )
    for title, examples in (("dataset", dataset[2]), ("registry", registry[2])):
        print(f"{title} texts empty after boilerplate removal:", flush=True)
        for example in examples or ["(none)"]:
            print(f"  {example}", flush=True)
    print_sample_records(_signature_sample(dataset_signatures_dir()), "dataset signature sample")
    print_sample_records(
        _signature_sample(registry_signatures_path(dev)), "registry signature sample"
    )


def run_signatures(dev: bool, force: bool) -> None:
    """Stage 5a: steps 1-3. Each output is skipped when it exists, unless ``force``."""
    started = time.perf_counter()
    print(
        "boilerplate and dataset signatures go to the full data folder; "
        "registry signatures follow --dev"
    )
    if force:
        _delete_signature_outputs(dev)
    temp_dir = Path(tempfile.mkdtemp(prefix="genealogy_find_copies_"))
    con = connect_duckdb()
    try:
        _prepare_duckdb(con, temp_dir)
        # Step 0: read the inputs
        read_inputs(con, dev)
        # Step 1: count owners per line and keep the boilerplate lines (Algorithm A22, query Q5.1)
        print("Step 1: count owners per line", flush=True)
        line_owners = _load_or_count_lines(con)
        boilerplate = build_boilerplate_set(line_owners, settings.BOILERPLATE_MIN_OWNERS)
        _print_boilerplate(line_owners)
        # Step 2: build MinHash signatures of the dataset texts (query Q5.2)
        # and of the registry versions (query Q4.2)
        print(
            f"Step 2: build MinHash signatures without {len(boilerplate)} boilerplate lines",
            flush=True,
        )
        build_dataset_signatures(con, boilerplate, dataset_signatures_dir())
        _load_or_sign_registry(dev, boilerplate)
    finally:
        con.close()
        shutil.rmtree(temp_dir, ignore_errors=True)
    # Step 3: summary
    _print_run_summary(line_owners, dev, time.perf_counter() - started)


def main() -> None:
    """Run stage 5a. ``--only rehost`` and ``--only copies`` (stages 5b, 5c) are not built yet."""
    parser = build_arg_parser("Stage 5a: template lines and MinHash signatures.")
    parser.add_argument(
        "--only",
        choices=["signatures", "rehost", "copies"],
        help="run one part of stage 5; the default runs every part built so far",
    )
    args = parser.parse_args()
    if args.only in ("rehost", "copies"):
        print(f"skip: --only {args.only} is stage 5b/5c (cards G13, G14), not built yet")
        return
    run_signatures(bool(args.dev), bool(args.force))


if __name__ == "__main__":
    main()
