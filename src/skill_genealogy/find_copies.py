"""Stage 5a and 5b: template lines, MinHash signatures, registries copying each other.

Stage 5a (G12): count the body lines shared by many GitHub owners (template
text such as "## when to use"), then build a MinHash signature, with those
lines removed, for every distinct English dataset text and every registry
lineage version.

Stage 5b (G13): for each lineage, find whether its first version was copied
from an older version in another registry (same blob, near copy or contained
copy), follow that chain to the root lineage, and write the registry hub
table and the final repo roles. Registry kinds (source, copier, unclear) come
from stage 3c (G38); the share of re-hosted lineages is a printed cross-check.

Inputs (stage 5a, always the full data folder, also with ``--dev``): the
GitSkills artifacts (``settings.ARTIFACTS_GLOB``), ``skill_language.parquet``
(stage 2a) and ``registry_history/registry_history_texts.parquet`` (stage 3).
In the dev or full data folder: ``lineage_versions.parquet`` (stage 4a).
Stage 5b also reads ``lineages.parquet`` and ``registry_signatures.npz`` (dev
or full) and ``repo_roles.parquet`` (stage 2c) and ``registry_kinds.parquet``
(stage 3c) from the full data folder.

Outputs:
- ``boilerplate_lines.parquet`` (full data folder, shared by dev and full):
  ``line, owners`` for lines with ``owners >= BOILERPLATE_MIN_OWNERS``.
- ``dataset_signatures/part_sXX_cYYYYY.npz`` (full data folder, shared): arrays
  ``ids`` (``file_sha``), ``signatures`` uint64 ``[n, 128]``, ``sizes`` int32.
- ``registry_signatures.npz`` (dev or full data folder): the same arrays, with
  ids ``lineage_id#version_seq``.
- ``lineage_roots.parquet``, ``registry_hubs.parquet`` and
  ``repo_roles_final.parquet`` (stage 5b, dev or full data folder).

The dataset outputs do not depend on the registries, so ``--dev`` changes only
the registry file. Dataset signatures take more than an hour on full data: run
them in the background.

Run (no network):
    python -m skill_genealogy.find_copies --dev --only signatures
    python -m skill_genealogy.find_copies --only signatures --force
    python -m skill_genealogy.find_copies --dev --only rehost
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
from datasketch import MinHashLSH

from skill_genealogy import settings
from skill_genealogy.build_registry_lineages import history_dir, read_texts
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
    has_comparable_text,
    make_minhash,
    make_word_shingles,
    minhash_from_array,
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


# Stage 5b: registries copying each other (card G13)

# Columns of lineage_roots.parquet, in order.
ROOT_COLUMNS = (
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
# Columns of registry_hubs.parquet, in order. upstream_share is the G38 value
# that update_repo_roles copies into hub_share.
HUB_COLUMNS = (
    "repo",
    "registry_kind",
    "kind_confidence",
    "kind_basis",
    "upstream_share",
    "english_lineages",
    "rehosted_lineages",
    "dated_hub_share",
    "is_hub",
    "hub_reason",
    "preview_hub_share",
)
# Columns of one candidate source of a lineage (exact, near or contained).
_SOURCE_COLUMNS = (
    "lineage_id",
    "copied_from_lineage",
    "copied_from_seq",
    "rehost_link",
    "rehost_similarity",
    "source_release_date",
)
# A link found on the first version, best kind first.
LINK_RANK = {"exact": 0, "near": 1, "near_contained": 2}
# First versions looked up in the band index at once. About 640 band hits per
# first version on full data, so a block is a few million pairs.
REHOST_QUERY_BLOCK = 10_000
# Candidate pairs whose 128 hash values are compared at once (about 400 MB).
PAIR_SCORE_BLOCK = 200_000
# Registries printed in the "most re-hosted" list.
TOP_REHOST_REGISTRIES = 10

# Query Q5.3, see code_flow/10_registry_rehosts.md
# For each first version, the oldest version of another registry with the same
# blob, released strictly earlier. versions is lineage_versions joined to the
# lineage's repo.
EXACT_REHOST_SQL = """
SELECT
    query.lineage_id AS lineage_id,
    source.lineage_id AS copied_from_lineage,
    source.version_seq AS copied_from_seq,
    source.release_date AS source_release_date
FROM versions AS query
JOIN versions AS source
  ON source.blob = query.blob
 AND source.repo <> query.repo
 AND source.release_date < query.release_date
WHERE query.version_seq = 1
QUALIFY row_number() OVER (
    PARTITION BY query.lineage_id
    ORDER BY source.release_date, source.lineage_id, source.version_seq
) = 1
"""


def lineages_path(dev: bool) -> Path:
    """``lineages.parquet`` (stage 4a) in the dev or full data folder."""
    return settings.data_dir(dev) / "lineages.parquet"


def lineage_roots_path(dev: bool) -> Path:
    """``lineage_roots.parquet`` (stage 5b) in the dev or full data folder."""
    return settings.data_dir(dev) / "lineage_roots.parquet"


def registry_hubs_path(dev: bool) -> Path:
    """``registry_hubs.parquet`` (stage 5b) in the dev or full data folder."""
    return settings.data_dir(dev) / "registry_hubs.parquet"


def repo_roles_final_path(dev: bool) -> Path:
    """``repo_roles_final.parquet`` (stage 5b) in the dev or full data folder."""
    return settings.data_dir(dev) / "repo_roles_final.parquet"


def _repo_roles_path() -> Path:
    """``repo_roles.parquet`` (stage 2c) in the full data folder."""
    return settings.data_dir(False) / "repo_roles.parquet"


def _registry_kinds_path() -> Path:
    """``registry_kinds.parquet`` (stage 3c) in the full data folder."""
    return settings.data_dir(False) / "registry_kinds.parquet"


def _rehost_outputs(dev: bool) -> list[Path]:
    """The three stage 5b output files of this mode."""
    return [lineage_roots_path(dev), registry_hubs_path(dev), repo_roles_final_path(dev)]


# Step 4: read the stage 5b inputs


def read_rehost_inputs(dev: bool) -> dict[str, object]:
    """Read lineages, versions, registry signatures, stage-2 roles and G38 kinds.

    Lineages and versions follow ``dev``; roles and kinds always come from the
    full data folder. A missing file raises ``FileNotFoundError`` naming the
    command that builds it.
    """
    flag = " --dev" if dev else ""
    paths = {
        "lineages": lineages_path(dev),
        "lineage_versions": _versions_path(dev),
        "registry_signatures": registry_signatures_path(dev),
        "repo_roles": _repo_roles_path(),
        "registry_kinds": _registry_kinds_path(),
    }
    check_inputs_exist(
        paths,
        {
            "lineages": f"python -m skill_genealogy.build_registry_lineages{flag}",
            "lineage_versions": f"python -m skill_genealogy.build_registry_lineages{flag}",
            "registry_signatures": f"python -m skill_genealogy.find_copies{flag} --only signatures",
            "repo_roles": "python -m skill_genealogy.select_registries",
            "registry_kinds": "python -m skill_genealogy.label_registry_kinds",
        },
    )
    ids, signatures, sizes = load_signatures(paths["registry_signatures"])
    return {
        "lineages": read_parquet(paths["lineages"]),
        "versions": read_parquet(paths["lineage_versions"]),
        "signature_ids": ids,
        "signatures": signatures,
        "sizes": sizes,
        "repo_roles": read_parquet(paths["repo_roles"]),
        "registry_kinds": read_parquet(paths["registry_kinds"]),
    }


def version_table(versions: pd.DataFrame, lineages: pd.DataFrame) -> pd.DataFrame:
    """Each version with its registry ``repo``, its signature id and its date in UTC.

    ``version_id`` is ``lineage_id#version_seq``, the id of ``registry_signatures.npz``.
    A version whose lineage is not in ``lineages`` is dropped.
    """
    repos = lineages.loc[:, ["lineage_id", "repo"]].drop_duplicates("lineage_id")
    table = versions.loc[:, ["lineage_id", "version_seq", "blob", "release_date"]].merge(
        repos, on="lineage_id", how="inner"
    )
    table["version_seq"] = table["version_seq"].astype("int64")
    table["release_date"] = pd.to_datetime(table["release_date"], utc=True)
    table["version_id"] = table["lineage_id"].astype(str) + "#" + table["version_seq"].astype(str)
    return table.reset_index(drop=True)


# Step 5: exact re-hosts (query Q5.3)


def find_exact_sources(table: pd.DataFrame) -> pd.DataFrame:
    """For each first version, the oldest earlier version in another repo with the same blob.

    Inputs: ``version_table`` rows.
    Outputs: ``lineage_id, copied_from_lineage, copied_from_seq, rehost_link,
    rehost_similarity, source_release_date``; ``rehost_link`` is ``"exact"``
    and the similarity 1.0. Lineages without such a version have no row.
    """
    con = connect_duckdb()
    try:
        con.register(
            "versions", table.loc[:, ["lineage_id", "version_seq", "blob", "release_date", "repo"]]
        )
        found = con.execute(EXACT_REHOST_SQL).df()
    finally:
        con.close()
    found["rehost_link"] = "exact"
    found["rehost_similarity"] = 1.0
    found["source_release_date"] = pd.to_datetime(found["source_release_date"], utc=True)
    return found.loc[:, list(_SOURCE_COLUMNS)]


# Step 6: near and contained re-hosts (Algorithm A23)


def build_registry_lsh(
    registry_ids: list[str], signatures: np.ndarray, threshold: float
) -> MinHashLSH:
    """A datasketch ``MinHashLSH`` holding every registry signature under its id.

    ``MinHashLSH`` splits the 128 values into ``b`` bands of ``r`` values,
    chosen for ``threshold``. A query returns every id that equals it on at
    least one whole band, so pairs with Jaccard near or above the threshold
    come back almost always and pairs far below it rarely. This is the plain
    version used by the tests; the run uses ``build_band_index``, which gives
    the same candidates with much less memory.

    >>> rows = np.vstack([np.arange(128), np.arange(128), np.arange(128) + 500]).astype(np.uint64)
    >>> lsh = build_registry_lsh(["a#1", "b#1", "c#1"], rows, 0.5)
    >>> sorted(lsh.query(minhash_from_array(rows[0])))
    ['a#1', 'b#1']
    """
    lsh = MinHashLSH(threshold=float(threshold), num_perm=settings.MINHASH_NUM_PERM)
    for registry_id, values in zip(registry_ids, signatures, strict=True):
        lsh.insert(registry_id, minhash_from_array(values))
    return lsh


def build_band_index(
    signatures: np.ndarray, threshold: float
) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """The bands of ``MinHashLSH`` at ``threshold`` as sorted arrays, one tuple per band.

    Per band: ``group`` (the bucket of each row), ``order`` (rows sorted by
    bucket) and ``starts`` (where each bucket begins in ``order``). Two rows
    are candidates when one band's values are all equal, as in datasketch.
    A datasketch index of the 816,534 full registry versions needs several GB;
    these arrays need about 250 MB.

    >>> rows = np.vstack([np.arange(128), np.arange(128), np.arange(128) + 500]).astype(np.uint64)
    >>> pairs = band_candidate_pairs(build_band_index(rows, 0.5), np.array([0]))
    >>> pairs[1].tolist()
    [1]
    """
    shape = MinHashLSH(threshold=float(threshold), num_perm=settings.MINHASH_NUM_PERM)
    matrix = np.ascontiguousarray(signatures, dtype=np.uint64)
    bands = []
    for band in range(shape.b if len(matrix) else 0):
        block = np.ascontiguousarray(matrix[:, band * shape.r : (band + 1) * shape.r])
        keys = block.view(np.dtype((np.void, block.dtype.itemsize * shape.r))).reshape(-1)
        group = np.unique(keys, return_inverse=True)[1].reshape(-1).astype(np.int64)
        order = np.argsort(group, kind="stable")
        starts = np.searchsorted(group[order], np.arange(int(group.max()) + 2))
        bands.append((group, order, starts))
    return bands


def band_candidate_pairs(
    bands: list[tuple[np.ndarray, np.ndarray, np.ndarray]], query_rows: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Every ``(query row, other row)`` that shares at least one whole band, once.

    A row is never paired with itself. Pairs come back sorted by query row,
    then other row.
    """
    total = max((len(group) for group, _order, _starts in bands), default=0)
    keys = []
    for group, order, starts in bands:
        bucket = group[query_rows]
        low, counts = starts[bucket], starts[bucket + 1] - starts[bucket]
        offsets = np.arange(int(counts.sum())) - np.repeat(np.cumsum(counts) - counts, counts)
        others = order[np.repeat(low, counts) + offsets]
        keys.append(np.repeat(query_rows, counts).astype(np.int64) * total + others)
    if not keys:
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty.copy()
    unique = np.unique(np.concatenate(keys))
    query, other = unique // total, unique % total
    keep = query != other
    return query[keep], other[keep]


def containment_estimates(
    jaccard: np.ndarray, source_sizes: np.ndarray, copy_sizes: np.ndarray
) -> np.ndarray:
    """``estimate_containment`` for arrays: the share of each source text inside its copy.

    jaccard * (source + copy) / ((1 + jaccard) * source), clipped to 0..1;
    0 when the Jaccard or a size is 0.

    >>> containment_estimates(np.array([0.5, 0.0]), np.array([4, 4]), np.array([8, 8])).tolist()
    [1.0, 0.0]
    """
    jaccard = np.asarray(jaccard, dtype=np.float64)
    source = np.asarray(source_sizes, dtype=np.float64)
    copy = np.asarray(copy_sizes, dtype=np.float64)
    valid = (jaccard > 0) & (source > 0) & (copy > 0)
    raw = np.zeros_like(jaccard)
    raw[valid] = (
        jaccard[valid] * (source[valid] + copy[valid]) / ((1.0 + jaccard[valid]) * source[valid])
    )
    return np.clip(raw, 0.0, 1.0)


def score_pairs(
    signatures: np.ndarray, sizes: np.ndarray, query: np.ndarray, other: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Estimated Jaccard of each pair, and containment of ``other`` (older) in ``query``.

    Jaccard = share of the 128 MinHash values that are equal.
    """
    jaccard = np.zeros(len(query), dtype=np.float64)
    for start in range(0, len(query), PAIR_SCORE_BLOCK):
        stop = start + PAIR_SCORE_BLOCK
        agree = (signatures[query[start:stop]] == signatures[other[start:stop]]).sum(axis=1)
        jaccard[start:stop] = agree / settings.MINHASH_NUM_PERM
    return jaccard, containment_estimates(jaccard, sizes[other], sizes[query])


def link_kinds(
    jaccard: np.ndarray, containment: np.ndarray, source_shingles: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """The link of each scored pair (``near``, ``near_contained`` or ``""``) and its similarity.

    near: Jaccard >= NEAR_COPY_MIN_JACCARD, similarity = Jaccard. Otherwise
    near_contained: containment >= NEAR_COPY_MIN_CONTAINMENT and the source
    has at least REHOST_CONTAINED_MIN_SHINGLES shingles, similarity =
    containment. Otherwise no link.

    >>> jaccard, containment = np.array([0.9, 0.6, 0.6, 0.6]), np.array([0.9, 0.85, 0.5, 0.85])
    >>> link_kinds(jaccard, containment, np.array([5, 40, 40, 5]))[0].tolist()
    ['near', 'near_contained', '', '']
    """
    near = jaccard >= settings.NEAR_COPY_MIN_JACCARD
    big_enough = np.asarray(source_shingles) >= settings.REHOST_CONTAINED_MIN_SHINGLES
    contained = ~near & big_enough & (containment >= settings.NEAR_COPY_MIN_CONTAINMENT)
    kinds = np.where(near, "near", np.where(contained, "near_contained", ""))
    return kinds, np.where(near, jaccard, containment)


def _signature_rows(table: pd.DataFrame, signature_ids: list[str]) -> pd.DataFrame:
    """``table`` rows that have a signature, with ``row`` = position in the signature matrix."""
    position = pd.DataFrame({"version_id": signature_ids, "row": np.arange(len(signature_ids))})
    return (
        table.merge(position, on="version_id", how="inner")
        .sort_values("row")
        .reset_index(drop=True)
    )


def _older_other_repo(signed: pd.DataFrame, query: np.ndarray, other: np.ndarray) -> np.ndarray:
    """True where ``other`` is in another repo and released strictly before ``query``."""
    repo_code = pd.factorize(signed["repo"])[0]
    released = signed["release_date"].to_numpy(dtype="datetime64[ns]")
    by_row = np.full(int(signed["row"].max()) + 1 if len(signed) else 0, -1, dtype=np.int64)
    by_row[signed["row"].to_numpy()] = np.arange(len(signed))
    query_at, other_at = by_row[query], by_row[other]
    known = (query_at >= 0) & (other_at >= 0)
    keep = np.zeros(len(query), dtype=bool)
    keep[known] = (repo_code[query_at[known]] != repo_code[other_at[known]]) & (
        released[other_at[known]] < released[query_at[known]]
    )
    return keep


def find_near_sources(
    table: pd.DataFrame,
    signature_ids: list[str],
    signatures: np.ndarray,
    sizes: np.ndarray,
    skip_lineages: set[str],
) -> pd.DataFrame:
    """Near and contained candidates of every first version not in ``skip_lineages``.

    Candidates share an LSH band at ``REHOST_CONTAINMENT_LSH_THRESHOLD``
    (0.5), are in another repo and are released strictly earlier. A
    candidate is kept when ``link_kinds`` gives it a link. Every kept
    candidate is returned; ``choose_rehost_sources`` picks one per lineage.
    """
    signed = _signature_rows(table, signature_ids)
    first = signed.loc[(signed["version_seq"] == 1) & ~signed["lineage_id"].isin(skip_lineages)]
    bands = build_band_index(signatures, settings.REHOST_CONTAINMENT_LSH_THRESHOLD)
    found = []
    query_rows = first["row"].to_numpy()
    for start in range(0, len(query_rows), REHOST_QUERY_BLOCK):
        query, other = band_candidate_pairs(bands, query_rows[start : start + REHOST_QUERY_BLOCK])
        keep = _older_other_repo(signed, query, other)
        query, other = query[keep], other[keep]
        kinds, similarity = link_kinds(*score_pairs(signatures, sizes, query, other), sizes[other])
        linked = kinds != ""
        found.append(
            _source_rows(signed, query[linked], other[linked], kinds[linked], similarity[linked])
        )
        print(
            f"rehost near search: {min(start + REHOST_QUERY_BLOCK, len(query_rows))}"
            f"/{len(query_rows)} first versions",
            flush=True,
        )
    frames = [frame for frame in found if len(frame)]
    return pd.concat(frames, ignore_index=True) if frames else _empty_sources()


def _source_rows(
    signed: pd.DataFrame,
    query: np.ndarray,
    other: np.ndarray,
    kinds: np.ndarray,
    similarity: np.ndarray,
) -> pd.DataFrame:
    """Candidate source rows (``_SOURCE_COLUMNS``) for linked pairs of signature rows."""
    by_row = signed.set_index("row")
    queries = by_row.loc[query]
    sources = by_row.loc[other]
    return pd.DataFrame(
        {
            "lineage_id": queries["lineage_id"].to_numpy(),
            "copied_from_lineage": sources["lineage_id"].to_numpy(),
            "copied_from_seq": sources["version_seq"].to_numpy(),
            "rehost_link": kinds,
            "rehost_similarity": similarity,
            "source_release_date": sources["release_date"].to_numpy(),
        }
    )


def _empty_sources() -> pd.DataFrame:
    """An empty candidate-source table."""
    return pd.DataFrame(
        {
            "lineage_id": pd.Series(dtype="object"),
            "copied_from_lineage": pd.Series(dtype="object"),
            "copied_from_seq": pd.Series(dtype="int64"),
            "rehost_link": pd.Series(dtype="object"),
            "rehost_similarity": pd.Series(dtype="float64"),
            "source_release_date": pd.Series(dtype="datetime64[ns, UTC]"),
        }
    )


# Step 7: pick one source per lineage and follow the chain (Algorithms A23, A24)


# Algorithm A23, see code_flow/10_registry_rehosts.md
def find_rehost_source(
    lineage_id: str, first_version_id: str, candidates: pd.DataFrame
) -> dict | None:
    """The older version this lineage's first text was copied from, or None.

    ``candidates`` holds the first version (``version_id == first_version_id``)
    and other versions with ``version_id, lineage_id, repo, version_seq,
    release_date, blob, jaccard, containment, shingles`` (Jaccard and
    containment of each candidate against the first version, and the
    candidate's shingle count). A candidate counts only when
    its repo differs and it was released strictly earlier. Link: same blob
    -> ``exact``; Jaccard >= 0.80 -> ``near``; containment >= 0.80 and a
    source of at least 20 shingles -> ``near_contained``. Exact wins, then
    the highest similarity, then the oldest date, then ``lineage_id``. This
    is the plain version of steps 5-7; a test checks that the run's
    vectorised path picks the same source.

    >>> rows = pd.DataFrame([
    ...     {"version_id": "B#1", "lineage_id": "B", "repo": "b/r", "version_seq": 1,
    ...      "release_date": "2026-03-01", "blob": "x", "jaccard": 1.0,
    ...      "containment": 1.0, "shingles": 9},
    ...     {"version_id": "A#1", "lineage_id": "A", "repo": "a/r", "version_seq": 1,
    ...      "release_date": "2026-01-01", "blob": "x", "jaccard": 1.0,
    ...      "containment": 1.0, "shingles": 9}])
    >>> found = find_rehost_source("B", "B#1", rows)
    >>> found["copied_from_lineage"], found["rehost_link"]
    ('A', 'exact')
    """
    query = candidates.loc[candidates["version_id"] == first_version_id]
    if query.empty:
        return None
    query_row = query.iloc[0]
    query_at = pd.Timestamp(query_row["release_date"])
    scored = []
    for row in candidates.itertuples(index=False):
        if row.lineage_id == lineage_id or row.repo == query_row["repo"]:
            continue
        if not pd.Timestamp(row.release_date) < query_at:
            continue
        if row.blob == query_row["blob"]:
            link, similarity = "exact", 1.0
        else:
            kinds, values = link_kinds(
                np.array([row.jaccard]), np.array([row.containment]), np.array([row.shingles])
            )
            link, similarity = str(kinds[0]), float(values[0])
        if link:
            scored.append(
                (
                    LINK_RANK[link],
                    -similarity,
                    pd.Timestamp(row.release_date),
                    row.lineage_id,
                    int(row.version_seq),
                    link,
                    similarity,
                )
            )
    if not scored:
        return None
    best = min(scored)
    return {
        "lineage_id": lineage_id,
        "copied_from_lineage": best[3],
        "copied_from_seq": best[4],
        "rehost_link": best[5],
        "rehost_similarity": best[6],
    }


def choose_rehost_sources(candidates: pd.DataFrame) -> pd.DataFrame:
    """One source per lineage from all its candidates, by the A23 order.

    Exact first, then the highest similarity, then the oldest release date,
    then the source ``lineage_id`` and ``version_seq``.
    """
    if candidates.empty:
        return _empty_sources()
    ordered = candidates.assign(link_rank=candidates["rehost_link"].map(LINK_RANK))
    ordered = ordered.sort_values(
        [
            "lineage_id",
            "link_rank",
            "rehost_similarity",
            "source_release_date",
            "copied_from_lineage",
            "copied_from_seq",
        ],
        ascending=[True, True, False, True, True, True],
        kind="mergesort",
    )
    return (
        ordered.drop_duplicates("lineage_id").loc[:, list(_SOURCE_COLUMNS)].reset_index(drop=True)
    )


def _walk_to_root(
    start: str, parents: dict[str, str | None], first_dates: dict[str, pd.Timestamp]
) -> tuple[str, int, bool]:
    """``(root, steps, met a cycle)`` for one lineage.

    The walk follows ``parents`` until a lineage with no source. A lineage
    seen twice ends the walk; the root is then the cycle's oldest lineage
    (first date, then id), and the steps are those from ``start`` to it.
    """
    seen: dict[str, int] = {}
    current: str | None = start
    while current is not None and current not in seen:
        seen[current] = len(seen)
        current = parents.get(current)
    if current is None:
        return list(seen)[-1], len(seen) - 1, False
    cycle = [name for name, step in seen.items() if step >= seen[current]]
    root = min(
        cycle, key=lambda name: (first_dates.get(name, pd.Timestamp.max.tz_localize("UTC")), name)
    )
    return root, seen[root], True


# Algorithm A24, see code_flow/10_registry_rehosts.md
def follow_to_root(rehosts: pd.DataFrame) -> pd.DataFrame:
    """Follow ``copied_from_lineage`` to the root lineage of every lineage.

    Inputs: one row per lineage with ``lineage_id, copied_from_lineage,
    copied_from_seq, rehost_link, rehost_similarity, first_date`` (source
    columns null for an original). Outputs: ``ROOT_COLUMNS``, sorted by
    ``lineage_id``. ``is_original`` = no source; ``chain_length`` = steps
    to the root; a cycle sets ``chain_cycle`` and its oldest lineage is the root.

    >>> rows = pd.DataFrame({"lineage_id": ["A", "B", "C"],
    ...     "copied_from_lineage": [None, "A", "B"], "copied_from_seq": [None, 1, 1],
    ...     "rehost_link": [None, "exact", "near"], "rehost_similarity": [None, 1.0, 0.9],
    ...     "first_date": pd.to_datetime(["2026-01-01", "2026-03-01", "2026-04-01"], utc=True)})
    >>> follow_to_root(rows)[["lineage_id", "root_lineage", "chain_length"]].values.tolist()
    [['A', 'A', 0], ['B', 'A', 1], ['C', 'A', 2]]
    """
    sources = (
        rehosts["copied_from_lineage"]
        .astype(object)
        .where(rehosts["copied_from_lineage"].notna(), None)
    )
    parents = dict(zip(rehosts["lineage_id"], sources, strict=True))
    first_dates = dict(
        zip(rehosts["lineage_id"], pd.to_datetime(rehosts["first_date"], utc=True), strict=True)
    )
    walked = [
        _walk_to_root(lineage_id, parents, first_dates) for lineage_id in rehosts["lineage_id"]
    ]
    roots = rehosts.loc[
        :,
        [
            "lineage_id",
            "copied_from_lineage",
            "copied_from_seq",
            "rehost_link",
            "rehost_similarity",
        ],
    ].copy()
    roots["root_lineage"] = [item[0] for item in walked]
    roots["is_original"] = sources.isna().to_numpy()
    roots["chain_length"] = np.asarray([item[1] for item in walked], dtype=np.int64)
    roots["chain_cycle"] = np.asarray([item[2] for item in walked], dtype=bool)
    roots["copied_from_seq"] = roots["copied_from_seq"].astype("Int64")
    roots["rehost_similarity"] = roots["rehost_similarity"].astype("float64")
    return (
        roots.loc[:, list(ROOT_COLUMNS)]
        .sort_values("lineage_id", kind="mergesort")
        .reset_index(drop=True)
    )


def _lineage_sources(
    lineages: pd.DataFrame, table: pd.DataFrame, chosen: pd.DataFrame
) -> pd.DataFrame:
    """Every lineage with its chosen source (null for an original) and its first-version date."""
    first = table.loc[table["version_seq"] == 1, ["lineage_id", "release_date"]].rename(
        columns={"release_date": "first_date"}
    )
    rows = (
        lineages.loc[:, ["lineage_id"]].drop_duplicates().merge(first, on="lineage_id", how="left")
    )
    return rows.merge(chosen.drop(columns=["source_release_date"]), on="lineage_id", how="left")


def find_rehosted_lineages(dev: bool) -> pd.DataFrame:
    """Stage 5b steps 4-7: ``lineage_roots`` for every lineage of this mode. Nothing is written.

    The first version of each lineage is compared with every version of
    other registries released strictly earlier: same blob (Q5.3) first,
    else near or contained by MinHash (A23); then chains are followed to
    the root (A24).
    """
    inputs = read_rehost_inputs(dev)
    return _find_roots(inputs)


def _find_roots(inputs: dict[str, object]) -> pd.DataFrame:
    """Steps 5-7 on inputs already read."""
    lineages: pd.DataFrame = inputs["lineages"]
    table = version_table(inputs["versions"], lineages)
    print(f"Step 5: exact re-hosts among {lineages['lineage_id'].nunique()} lineages", flush=True)
    exact = find_exact_sources(table)
    print(f"exact re-hosts: {len(exact)}", flush=True)
    print("Step 6: near and contained re-hosts (Algorithm A23)", flush=True)
    near = find_near_sources(
        table,
        inputs["signature_ids"],
        inputs["signatures"],
        inputs["sizes"],
        set(exact["lineage_id"]),
    )
    print("Step 7: pick one source per lineage and follow chains (Algorithms A23, A24)", flush=True)
    chosen = choose_rehost_sources(
        pd.concat(
            [frame for frame in (exact, near) if len(frame)] or [_empty_sources()],
            ignore_index=True,
        )
    )
    return follow_to_root(_lineage_sources(lineages, table, chosen))


# Step 8: dated hub shares, registry kinds and roles (Algorithm A24b)


# Algorithm A24b, see code_flow/10_registry_rehosts.md
def compute_dated_hub_shares(lineages: pd.DataFrame, roots: pd.DataFrame) -> pd.DataFrame:
    """Per registry with lineages: English lineages, re-hosted ones and their share.

    ``dated_hub_share`` = rehosted / English lineages, null under
    ``HUB_TEST_MIN_SKILLS`` English lineages. A cross-check only: it sets
    no hub tag.

    >>> lineages = pd.DataFrame(
    ...     {"lineage_id": list("abcde"), "repo": ["r/x"] * 5, "is_english": [True] * 5}
    ... )
    >>> roots = pd.DataFrame(
    ...     {"lineage_id": list("abcde"), "is_original": [False, True, True, True, True]}
    ... )
    >>> compute_dated_hub_shares(lineages, roots).to_dict("records")
    [{'repo': 'r/x', 'english_lineages': 5, 'rehosted_lineages': 1, 'dated_hub_share': 0.2}]
    """
    work = lineages.loc[:, ["lineage_id", "repo", "is_english"]].merge(
        roots.loc[:, ["lineage_id", "is_original"]], on="lineage_id", how="left"
    )
    work["english"] = work["is_english"].fillna(False).astype(bool)
    work["rehosted"] = work["english"] & ~work["is_original"].fillna(True).astype(bool)
    counts = (
        work.groupby("repo", sort=True)
        .agg(english_lineages=("english", "sum"), rehosted_lineages=("rehosted", "sum"))
        .reset_index()
    )
    counts["english_lineages"] = counts["english_lineages"].astype("int64")
    counts["rehosted_lineages"] = counts["rehosted_lineages"].astype("int64")
    share = counts["rehosted_lineages"] / counts["english_lineages"].where(
        counts["english_lineages"] > 0
    )
    counts["dated_hub_share"] = share.where(
        counts["english_lineages"] >= settings.HUB_TEST_MIN_SKILLS
    ).astype("float64")
    return counts


def kind_reason(
    kind: str, upstream_share: object, shared_texts: object, readme_phrase: object
) -> str:
    """The G38 evidence in words: ``"copier: 95% of 49,661 shared texts first seen elsewhere"``.

    A README phrase is added as ``"; README: <phrase>"``.

    >>> kind_reason("source", 0.01, 384, None)
    'source: 1% of 384 shared texts first seen elsewhere'
    >>> kind_reason("unclear", float("nan"), 0, "aggregate catalog")
    'unclear: no text shared with another registry; README: aggregate catalog'
    """
    if pd.isna(upstream_share) or pd.isna(shared_texts) or int(shared_texts) == 0:
        reason = f"{kind}: no text shared with another registry"
    else:
        share, shared = float(upstream_share), int(shared_texts)
        reason = f"{kind}: {share:.0%} of {shared:,} shared texts first seen elsewhere"
    if isinstance(readme_phrase, str) and readme_phrase.strip():
        reason += f"; README: {readme_phrase.strip()}"
    return reason


def build_registry_hubs(
    kinds: pd.DataFrame, shares: pd.DataFrame, repo_roles: pd.DataFrame
) -> pd.DataFrame:
    """One row per registry in ``registry_kinds`` (copiers too), in ``HUB_COLUMNS``.

    ``is_hub`` = G38 kind is ``copier``. A registry without lineages (every
    copier, and sources whose skills are all non-English) has 0 English
    lineages and a null ``dated_hub_share``. ``preview_hub_share`` is the
    stage-2 ``hub_share``.
    """
    hubs = kinds.loc[
        :,
        [
            "repo",
            "registry_kind",
            "confidence",
            "kind_basis",
            "upstream_share",
            "shared_texts",
            "readme_phrase",
        ],
    ]
    hubs = hubs.rename(columns={"confidence": "kind_confidence"}).merge(
        shares, on="repo", how="left"
    )
    for column in ("english_lineages", "rehosted_lineages"):
        hubs[column] = hubs[column].fillna(0).astype("int64")
    hubs["dated_hub_share"] = hubs["dated_hub_share"].astype("float64")
    hubs["is_hub"] = hubs["registry_kind"] == "copier"
    hubs["hub_reason"] = [
        kind_reason(*values)
        for values in zip(
            hubs["registry_kind"],
            hubs["upstream_share"],
            hubs["shared_texts"],
            hubs["readme_phrase"],
            strict=True,
        )
    ]
    preview = repo_roles.loc[:, ["repo", "hub_share"]].rename(
        columns={"hub_share": "preview_hub_share"}
    )
    hubs = hubs.merge(preview.drop_duplicates("repo"), on="repo", how="left")
    return (
        hubs.loc[:, list(HUB_COLUMNS)].sort_values("repo", kind="mergesort").reset_index(drop=True)
    )


def update_repo_roles(repo_roles: pd.DataFrame, registry_hubs: pd.DataFrame) -> pd.DataFrame:
    """``repo_roles`` with registry rows taken from G38 and a ``hub_evidence`` column.

    A repo in ``registry_hubs`` gets ``is_hub``, ``hub_share`` = G38
    ``upstream_share``, ``repo_role`` = ``registry_copier`` for a copier and
    ``registry`` otherwise, and ``hub_evidence = "registry_kinds"``. Other
    rows keep their stage-2 values; ``hub_evidence`` is ``"dataset_dates"``
    when they have a ``hub_share``, else null. Same rows, same order.

    >>> roles = pd.DataFrame({"repo": ["a/reg", "b/app"], "is_hub": [False, False],
    ...     "hub_share": [0.1, None], "repo_role": ["registry", "repository"]})
    >>> hubs = pd.DataFrame({"repo": ["a/reg"], "registry_kind": ["copier"],
    ...     "is_hub": [True], "upstream_share": [0.95]})
    >>> update_repo_roles(roles, hubs)[["repo_role", "hub_evidence"]].values.tolist()
    [['registry_copier', 'registry_kinds'], ['repository', None]]
    """
    kinds = (
        registry_hubs.loc[:, ["repo", "registry_kind", "is_hub", "upstream_share"]]
        .drop_duplicates("repo")
        .set_index("repo")
    )
    result = repo_roles.reset_index(drop=True).copy()
    known = result["repo"].isin(kinds.index).to_numpy()
    matched = kinds.loc[result.loc[known, "repo"]]
    result["hub_share"] = result["hub_share"].astype("float64")
    result.loc[known, "is_hub"] = matched["is_hub"].astype(bool).to_numpy()
    result.loc[known, "hub_share"] = matched["upstream_share"].astype("float64").to_numpy()
    result.loc[known, "repo_role"] = np.where(
        matched["registry_kind"].to_numpy() == "copier", "registry_copier", "registry"
    )
    evidence = [
        "registry_kinds" if is_registry else ("dataset_dates" if has_share else None)
        for is_registry, has_share in zip(known, result["hub_share"].notna(), strict=True)
    ]
    result["hub_evidence"] = pd.Series(evidence, index=result.index, dtype=object)
    result["is_hub"] = result["is_hub"].astype(bool)
    return result


# Step 9: save the stage 5b outputs and print the summary


def save_rehost_outputs(
    roots: pd.DataFrame, hubs: pd.DataFrame, roles: pd.DataFrame, dev: bool
) -> None:
    """Write ``lineage_roots``, ``registry_hubs`` and ``repo_roles_final`` for this mode."""
    write_parquet(roots, lineage_roots_path(dev), ["lineage_id"])
    write_parquet(hubs, registry_hubs_path(dev), ["repo"])
    write_parquet(roles, repo_roles_final_path(dev), ["repo"])


def _registry_mix(lineages: pd.DataFrame, roots: pd.DataFrame) -> pd.DataFrame:
    """Per registry with lineages: lineages, original and re-hosted, most re-hosted first."""
    merged = lineages.loc[:, ["lineage_id", "repo"]].merge(
        roots.loc[:, ["lineage_id", "is_original"]], on="lineage_id"
    )
    counts = (
        merged.groupby("repo")
        .agg(lineages=("lineage_id", "size"), original=("is_original", "sum"))
        .reset_index()
    )
    counts["rehosted"] = counts["lineages"] - counts["original"]
    return counts.sort_values(
        ["rehosted", "repo"], ascending=[False, True], kind="mergesort"
    ).reset_index(drop=True)


def _print_rehost_summary(
    dev: bool,
    lineages: pd.DataFrame,
    roots: pd.DataFrame,
    hubs: pd.DataFrame,
    roles: pd.DataFrame,
    elapsed: float,
) -> None:
    """Print the stage 5b counts, per-registry lists, the cross-check and two samples per table."""
    links = roots["rehost_link"].value_counts()
    mix = _registry_mix(lineages, roots)
    print_summary(
        "stage 5b registries copying each other",
        {
            "lineages": len(roots),
            "original_lineages": int(roots["is_original"].sum()),
            "rehosted_lineages": int((~roots["is_original"]).sum()),
            **{f"link_{name}": int(links.get(name, 0)) for name in LINK_RANK},
            "max_chain_length": int(roots["chain_length"].max()) if len(roots) else 0,
            "chain_cycles": int(roots["chain_cycle"].sum()),
            "registries_with_lineages": len(mix),
            "registries_with_a_rehost": int((mix["rehosted"] > 0).sum()),
            "seconds": round(elapsed, 1),
        },
    )
    print("registries by G38 kind:", hubs["registry_kind"].value_counts().to_dict(), flush=True)
    print("repo roles:", roles["repo_role"].value_counts().to_dict(), flush=True)
    print(f"registries with the most re-hosted lineages (top {TOP_REHOST_REGISTRIES})", flush=True)
    for row in mix.head(TOP_REHOST_REGISTRIES).itertuples(index=False):
        print(f"  {row.repo}: re-hosted {row.rehosted} of {row.lineages}", flush=True)
    high = hubs.loc[
        (hubs["registry_kind"] != "copier") & (hubs["dated_hub_share"] >= settings.HUB_MIN_SHARE)
    ]
    print(
        f"cross-check: {len(high)} non-copier registries with "
        f"dated_hub_share >= {settings.HUB_MIN_SHARE}",
        flush=True,
    )
    for row in (
        high.sort_values("dated_hub_share", ascending=False)
        .head(TOP_REHOST_REGISTRIES)
        .itertuples(index=False)
    ):
        print(
            f"  {row.repo}: {row.dated_hub_share:.2f} "
            f"({row.rehosted_lineages}/{row.english_lineages}); {row.hub_reason}",
            flush=True,
        )
    if dev:
        print(
            "dev: only the dev registries have lineages, so a skill re-hosted from a registry "
            "outside the sample counts as original and dev dated_hub_share values are too low. "
            "Registry kinds come from the full G38 run, so dev and full agree on them.",
            flush=True,
        )
    rehosted = roots.loc[~roots["is_original"]]
    print_sample_records(
        pd.concat([rehosted.head(1), roots.loc[roots["is_original"]].head(1)]),
        "lineage root sample",
    )
    print_sample_records(
        pd.concat(
            [hubs.loc[hubs["english_lineages"] > 0].head(1), hubs.loc[hubs["is_hub"]].head(1)]
        ),
        "registry hub sample",
    )


def run_rehost(dev: bool, force: bool) -> None:
    """Stage 5b: steps 4-9. Skips when all three outputs exist, unless ``force``."""
    outputs = _rehost_outputs(dev)
    if force:
        print("force: deleting lineage_roots, registry_hubs and repo_roles_final", flush=True)
        for path in outputs:
            path.unlink(missing_ok=True)
    if outputs_exist(outputs):
        print("skip: stage 5b outputs exist (use --force to rebuild)", flush=True)
        return
    started = time.perf_counter()
    # Step 4: read the inputs
    print("Step 4: read lineages, versions, registry signatures, roles and kinds", flush=True)
    inputs = read_rehost_inputs(dev)
    # Steps 5-7: exact (Q5.3) and near (A23) sources, one per lineage, chains to the root (A24)
    roots = _find_roots(inputs)
    # Step 8: dated hub share cross-check (A24b), registry kinds from G38, final roles
    print("Step 8: dated hub shares, registry hubs and repo roles (Algorithm A24b)", flush=True)
    shares = compute_dated_hub_shares(inputs["lineages"], roots)
    hubs = build_registry_hubs(inputs["registry_kinds"], shares, inputs["repo_roles"])
    roles = update_repo_roles(inputs["repo_roles"], hubs)
    # Step 9: save and print the summary
    save_rehost_outputs(roots, hubs, roles, dev)
    _print_rehost_summary(
        dev, inputs["lineages"], roots, hubs, roles, time.perf_counter() - started
    )


def main() -> None:
    """Run stage 5a (signatures) and 5b (rehost), or one part with ``--only``.

    ``--only copies`` (stage 5c, card G14) is not built yet.
    """
    parser = build_arg_parser(
        "Stage 5: template lines, MinHash signatures, registries copying each other."
    )
    parser.add_argument(
        "--only",
        choices=["signatures", "rehost", "copies"],
        help="run one part of stage 5; the default runs every part built so far",
    )
    args = parser.parse_args()
    dev, force = bool(args.dev), bool(args.force)
    if args.only == "copies":
        print("skip: --only copies is stage 5c (card G14), not built yet")
        return
    if args.only in (None, "signatures"):
        run_signatures(dev, force)
    if args.only in (None, "rehost"):
        run_rehost(dev, force)


if __name__ == "__main__":
    main()
