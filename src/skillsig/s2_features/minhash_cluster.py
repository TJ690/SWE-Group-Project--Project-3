"""
STAGE: s2_features
MODULE: minhash_cluster
PURPOSE: Cluster readable skills into near-duplicate families with MinHash LSH.
INPUTS: artifacts primary rows (file_sha, content) with the T02 readable filter
OUTPUTS: src/data/derived/s2_clusters.parquet and src/data/derived/s2_minhash_shards/*.npy
RQ LINK: RQ1 (near-duplicate clusters as a templated-family signal)
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from datasketch import MinHash, MinHashLSH

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_intake.devsample import output_path as devsample_path

log = logging.getLogger(__name__)

_DOWNLOAD = "python src/data/download_data.py"
_DEV_SAMPLE = "python -m skillsig data --dev-sample"
_FRONT_BODY = re.compile(r"^\s*---.*?\n---\s*\n?", re.DOTALL | re.ASCII)
_WS = re.compile(r"\s+")
_READABLE = """
dedup_primary = 1
AND content IS NOT NULL
AND NOT (strpos(content, chr(10)) = 0 AND content LIKE '%SKILL.md')
AND content NOT LIKE 'Redirecting%'
AND body_chars >= 200
"""
_SCORE_BLOCK = 64
_SCHEMA = pa.schema(
    [
        pa.field("file_sha", pa.string()),
        pa.field("cluster_id", pa.int64()),
        pa.field("cluster_size", pa.int64()),
        pa.field("threshold", pa.float64()),
        pa.field("nn_sim_est", pa.float64()),
    ]
)


def normalise(text: str, max_chars: int) -> str:
    """Strip front matter, lowercase, collapse whitespace, and cut to max_chars."""
    if max_chars < 0:
        raise ValueError(f"max_chars must be >= 0, got {max_chars}")
    body = _FRONT_BODY.sub("", text, count=1)
    collapsed = _WS.sub(" ", body.lower()).strip()
    return collapsed[:max_chars]


def signature(text: str, cfg: Config) -> np.ndarray:
    """MinHash signature, uint32[num_perm], over word shingles of the normalised body."""
    shingles = _shingles(
        normalise(text, cfg.minhash.body_max_chars), cfg.minhash.shingle_words
    )
    return _sketch(shingles, cfg.minhash.num_perm, cfg.seed)


def cluster_shard(ids: list[str], sigs: np.ndarray, threshold: float) -> list[tuple[str, int]]:
    """LSH clusters for one signature block. cluster_id is the smallest input index."""
    array = np.asarray(sigs)
    if array.ndim != 2:
        raise ValueError(f"signatures must have shape (n, num_perm), got {array.shape}")
    if len(ids) != array.shape[0]:
        raise ValueError(f"ids and signatures differ in length: {len(ids)} vs {array.shape[0]}")
    _check_threshold(threshold)
    if len(ids) == 0:
        return []
    labels, _sizes, _nn = _cluster([array], threshold)
    return [(str(file_id), int(label)) for file_id, label in zip(ids, labels, strict=True)]


def run(
    cfg: Config,
    *,
    dev: bool = False,
    force: bool = False,
    limit: int | None = None,
    sample_fraction: float | None = None,
) -> StageResult:
    """Write near-duplicate clusters at both MinHash thresholds. Dev mode uses the dev sample."""
    _check_run_args(limit, sample_fraction)
    output = _output_path(cfg)
    inputs = _inputs(cfg, dev=dev)
    _require_inputs(cfg, dev=dev)
    parameters = _parameters(dev=dev, limit=limit, sample_fraction=sample_fraction)
    if not force and _reusable(output, cfg=cfg, inputs=inputs, parameters=parameters):
        log.info("minhash clusters up to date: %s", output)
        return StageResult(
            stage="s2_features", outputs=[output], skipped=True, rows=_saved_rows(output)
        )
    directory = _shard_dir(cfg)
    plan = _plan(cfg, dev=dev, limit=limit, sample_fraction=sample_fraction)
    if force or not _same_plan(directory, plan):
        _clear_dir(directory)
    _write_json(directory / "plan.json", plan)
    shard_count = _fill_shards(
        cfg, directory, dev=dev, limit=limit, sample_fraction=sample_fraction
    )
    rows = _write_clusters(cfg, directory, shard_count, output)
    write_manifest(output, cfg=cfg, inputs=inputs, rows=rows)
    _remember(output, parameters)
    log.info("wrote %s (%s rows)", output, rows)
    return StageResult(
        stage="s2_features", outputs=[output], skipped=False, rows={"s2_clusters": rows}
    )


def _shingles(text: str, width: int) -> list[str]:
    if width < 1:
        raise ValueError(f"shingle width must be >= 1, got {width}")
    words = text.split()
    if len(words) < width:
        return [" ".join(words)] if words else []
    return [" ".join(words[index : index + width]) for index in range(len(words) - width + 1)]


def _sketch(shingles: list[str], num_perm: int, seed: int) -> np.ndarray:
    sketch = MinHash(
        num_perm=num_perm,
        seed=seed,
        permutations=_permutations(num_perm, seed),
        scheme="affine32",
    )
    if shingles:
        sketch.update_batch(shingle.encode("utf-8") for shingle in shingles)
    return np.asarray(sketch.hashvalues, dtype=np.uint32).copy()


@lru_cache(maxsize=4)
def _permutations(num_perm: int, seed: int) -> np.ndarray:
    return np.array(MinHash(num_perm=num_perm, seed=seed).permutations, copy=True)


@lru_cache(maxsize=8)
def _band_params(threshold: float, num_perm: int) -> tuple[int, int]:
    index = MinHashLSH(threshold=threshold, num_perm=num_perm)
    return int(index.b), int(index.r)


def _check_threshold(threshold: float) -> None:
    if threshold < 0.0 or threshold > 1.0:
        raise ValueError(f"threshold must be in [0, 1], got {threshold}")


def _check_run_args(limit: int | None, sample_fraction: float | None) -> None:
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if sample_fraction is not None and not 0 < sample_fraction <= 1:
        raise ValueError(f"sample_fraction must be in (0, 1], got {sample_fraction}")


def _cluster(
    maps: list[np.ndarray], threshold: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    count = sum(int(mapped.shape[0]) for mapped in maps)
    log.info("minhash clustering %s signatures at threshold %s", count, threshold)
    parent = np.arange(count, dtype=np.int64)
    nearest = np.full(count, np.nan, dtype=np.float64)
    _mark_exact(maps, nearest)
    bands, width = _band_params(threshold, int(maps[0].shape[1]))
    for band in range(bands):
        log.info("minhash band %s/%s at threshold %s", band + 1, bands, threshold)
        keys = _band_keys(maps, band * width, (band + 1) * width)
        _consume_band(maps, keys, parent, nearest)
    labels = _compress(parent)
    sizes = np.bincount(labels, minlength=count)[labels]
    return labels, sizes, nearest


def _mark_exact(maps: list[np.ndarray], nearest: np.ndarray) -> None:
    order, starts, ends = _group_spans(_signature_keys(maps))
    multi = (ends - starts) > 1
    for start, end in zip(starts[multi], ends[multi], strict=True):
        nearest[order[int(start) : int(end)]] = 1.0


def _signature_keys(maps: list[np.ndarray]) -> np.ndarray:
    parts = [_hash_pair(np.asarray(mapped)) for mapped in maps]
    return parts[0] if len(parts) == 1 else np.concatenate(parts)


def _hash_pair(sigs: np.ndarray) -> np.ndarray:
    high = np.zeros(sigs.shape[0], dtype=np.uint64)
    low = np.full(sigs.shape[0], np.uint64(0x9E3779B97F4A7C15), dtype=np.uint64)
    for col in range(sigs.shape[1]):
        column = sigs[:, col].astype(np.uint64, copy=False)
        high = high * np.uint64(0xBF58476D1CE4E5B9) + column + np.uint64(1)
        low = (low ^ column) * np.uint64(0x94D049BB133111EB) + np.uint64(col + 1)
    keys = np.empty(sigs.shape[0], dtype=[("a", "<u8"), ("b", "<u8")])
    keys["a"] = high
    keys["b"] = low
    return keys


def _consume_band(
    maps: list[np.ndarray], keys: np.ndarray, parent: np.ndarray, nearest: np.ndarray
) -> None:
    order, starts, ends = _group_spans(keys)
    multi = (ends - starts) > 1
    for start, end in zip(starts[multi], ends[multi], strict=True):
        indices = order[int(start) : int(end)]
        _union_many(parent, indices)
        _score_many(maps, indices, nearest)


def _group_spans(keys: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    order = np.argsort(keys, kind="mergesort")
    ordered = keys[order]
    if ordered.size == 0:
        empty = np.empty(0, dtype=np.int64)
        return order, empty, empty
    change = np.empty(ordered.size, dtype=bool)
    change[0] = True
    change[1:] = ordered[1:] != ordered[:-1]
    starts = np.flatnonzero(change)
    ends = np.empty_like(starts)
    ends[:-1] = starts[1:]
    ends[-1] = ordered.size
    return order, starts, ends


def _union_many(parent: np.ndarray, indices: np.ndarray) -> None:
    root = _find(parent, int(indices[0]))
    for index in indices[1:]:
        other = _find(parent, int(index))
        if other == root:
            continue
        if other < root:
            parent[root] = other
            root = other
        else:
            parent[other] = root


def _find(parent: np.ndarray, index: int) -> int:
    while parent[index] != index:
        parent[index] = parent[parent[index]]
        index = int(parent[index])
    return index


def _compress(parent: np.ndarray) -> np.ndarray:
    for _ in range(int(parent.shape[0]).bit_length() + 1):
        parent[:] = parent[parent]
    return parent


def _score_many(maps: list[np.ndarray], indices: np.ndarray, nearest: np.ndarray) -> None:
    if indices.size < 2:
        return
    pending = np.flatnonzero(~(nearest[indices] >= 1.0))
    if pending.size == 0:
        return
    rows = _take(maps, indices)
    _nearest(rows, pending, nearest, indices)


def _take(maps: list[np.ndarray], indices: np.ndarray) -> np.ndarray:
    if len(maps) == 1:
        return np.asarray(maps[0][indices])
    offsets = np.cumsum([0, *[int(mapped.shape[0]) for mapped in maps[:-1]]], dtype=np.int64)
    shard = np.searchsorted(offsets, indices, side="right") - 1
    out = np.empty((indices.shape[0], maps[0].shape[1]), dtype=np.uint32)
    for sid, mapped in enumerate(maps):
        pick = np.flatnonzero(shard == sid)
        if pick.size == 0:
            continue
        out[pick] = mapped[indices[pick] - offsets[sid]]
    return out


def _nearest(
    rows: np.ndarray, pending: np.ndarray, nearest: np.ndarray, global_index: np.ndarray
) -> None:
    best = np.full(pending.shape[0], -1.0, dtype=np.float64)
    queries = rows[pending]
    width = rows.shape[0]
    for start in range(0, width, _SCORE_BLOCK):
        _score_block(queries, rows[start : start + _SCORE_BLOCK], pending, start, best)
    chosen = global_index[pending]
    current = np.where(np.isnan(nearest[chosen]), -1.0, nearest[chosen])
    merged = np.maximum(current, best)
    nearest[chosen] = np.where(merged < 0.0, np.nan, merged)


def _score_block(
    queries: np.ndarray,
    gallery: np.ndarray,
    pending: np.ndarray,
    start: int,
    best: np.ndarray,
) -> None:
    positions = np.arange(start, start + gallery.shape[0])
    for q0 in range(0, queries.shape[0], _SCORE_BLOCK):
        query = queries[q0 : q0 + _SCORE_BLOCK]
        sims = (query[:, None, :] == gallery[None, :, :]).mean(axis=2)
        q_pos = pending[q0 : q0 + query.shape[0]]
        sims[q_pos[:, None] == positions[None, :]] = -1.0
        best[q0 : q0 + query.shape[0]] = np.maximum(
            best[q0 : q0 + query.shape[0]], sims.max(axis=1)
        )


def _band_keys(maps: list[np.ndarray], start: int, end: int) -> np.ndarray:
    bands = [np.ascontiguousarray(mapped[:, start:end]) for mapped in maps]
    band = bands[0] if len(bands) == 1 else np.concatenate(bands, axis=0)
    width = band.shape[1] * band.dtype.itemsize
    packed = np.ascontiguousarray(band).view(np.dtype((np.void, width)))
    return packed.reshape(band.shape[0])


def _output_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s2_clusters.parquet"


def _shard_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s2_minhash_shards"


def _inputs(cfg: Config, *, dev: bool) -> list[Path]:
    paths = [_artifacts_dir(cfg)]
    if dev:
        paths.append(devsample_path(cfg))
    return paths


def _artifacts_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "artifacts"


def _require_inputs(cfg: Config, *, dev: bool) -> None:
    directory = _artifacts_dir(cfg)
    if not directory.is_dir() or not any(directory.glob("*.parquet")):
        raise FileNotFoundError(f"missing {directory}; run: {_DOWNLOAD}")
    if not dev:
        return
    sample = devsample_path(cfg)
    if not sample.is_file():
        raise FileNotFoundError(f"missing {sample}; run: {_DEV_SAMPLE}")


def _parameters(
    *, dev: bool, limit: int | None, sample_fraction: float | None
) -> dict[str, object]:
    return {"dev": dev, "limit": limit, "sample_fraction": sample_fraction}


def _plan(
    cfg: Config, *, dev: bool, limit: int | None, sample_fraction: float | None
) -> dict[str, object]:
    return {
        "body_max_chars": cfg.minhash.body_max_chars,
        "chunk_rows": cfg.chunk_rows,
        "dev": dev,
        "input_stamp": _input_stamp(cfg, dev=dev),
        "limit": limit,
        "num_perm": cfg.minhash.num_perm,
        "sample_fraction": sample_fraction,
        "seed": cfg.seed,
        "shingle_words": cfg.minhash.shingle_words,
    }


def _input_stamp(cfg: Config, *, dev: bool) -> list[list[str | int]]:
    stamps: list[list[str | int]] = []
    for path in _inputs(cfg, dev=dev):
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for child in files:
            stat = child.stat()
            stamps.append([child.as_posix(), stat.st_size, stat.st_mtime_ns])
    return stamps


def _reusable(
    output: Path, *, cfg: Config, inputs: list[Path], parameters: dict[str, object]
) -> bool:
    if not inputs_unchanged(output, cfg=cfg, inputs=inputs):
        return False
    payload = json.loads(manifest_path(output).read_text(encoding="utf-8"))
    return payload.get("parameters") == parameters


def _same_plan(directory: Path, plan: dict[str, object]) -> bool:
    path = directory / "plan.json"
    if not path.is_file():
        return False
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return saved == plan


def _fill_shards(
    cfg: Config,
    directory: Path,
    *,
    dev: bool,
    limit: int | None,
    sample_fraction: float | None,
) -> int:
    done, seen = _complete_shards(directory, cfg.minhash.num_perm)
    if limit is not None and seen >= limit:
        log.info("minhash shards already hold the %s row limit", limit)
        return done
    remaining = None if limit is None else limit - seen
    sql, params = _query(
        cfg, dev=dev, sample_fraction=sample_fraction, limit=remaining, offset=seen
    )
    con = duckdb.connect()
    try:
        cursor = con.execute(sql, params)
        done = _consume_shards(cfg, directory, cursor, done)
    finally:
        con.close()
    return done


def _consume_shards(
    cfg: Config, directory: Path, cursor: duckdb.DuckDBPyConnection, index: int
) -> int:
    while batch := cursor.fetchmany(int(cfg.chunk_rows)):
        ids = [str(row[0]) for row in batch]
        texts = [row[1] if isinstance(row[1], str) else "" for row in batch]
        _save_shard(directory, index, ids, _signatures(texts, cfg))
        log.info("minhash shard %s (%s signatures)", index, len(ids))
        index += 1
    return index


def _signatures(texts: list[str], cfg: Config) -> np.ndarray:
    num_perm = cfg.minhash.num_perm
    out = np.empty((len(texts), num_perm), dtype=np.uint32)
    width = cfg.minhash.shingle_words
    max_chars = cfg.minhash.body_max_chars
    for row, text in enumerate(texts):
        out[row] = _sketch(_shingles(normalise(text, max_chars), width), num_perm, cfg.seed)
    return out


def _query(
    cfg: Config,
    *,
    dev: bool,
    sample_fraction: float | None,
    limit: int | None,
    offset: int,
) -> tuple[str, list[object]]:
    sql, params = _population_sql(cfg, dev=dev, sample_fraction=sample_fraction)
    if limit is None and offset == 0:
        return sql, params
    take = 2**62 if limit is None else limit
    params.extend([take, offset])
    return sql + "\nLIMIT ?\nOFFSET ?", params


def _population_sql(
    cfg: Config, *, dev: bool, sample_fraction: float | None
) -> tuple[str, list[object]]:
    glob = _sql_string(_artifacts_dir(cfg) / "*.parquet")
    where = _READABLE.strip()
    if dev:
        sample = _sql_string(devsample_path(cfg))
        where += f"\nAND file_sha IN (SELECT file_sha FROM read_parquet({sample}))"
    if sample_fraction is None:
        sql = f"""
            SELECT file_sha, content
            FROM read_parquet({glob})
            WHERE {where}
            ORDER BY file_sha
        """
        return sql, []
    sql = f"""
        WITH base AS (
            SELECT file_sha, content,
                   coalesce(cast(location_class AS VARCHAR), 'unknown') AS stratum
            FROM read_parquet({glob})
            WHERE {where}
        ),
        ranked AS (
            SELECT file_sha, content,
                   row_number() OVER (
                       PARTITION BY stratum
                       ORDER BY hash(file_sha || '-' || ?)
                   ) AS rn,
                   count(*) OVER (PARTITION BY stratum) AS stratum_n
            FROM base
        )
        SELECT file_sha, content
        FROM ranked
        WHERE rn <= greatest(1, cast(ceil(stratum_n * ?) AS BIGINT))
        ORDER BY file_sha
    """
    return sql, [str(cfg.seed), float(sample_fraction)]


def _complete_shards(directory: Path, num_perm: int) -> tuple[int, int]:
    count = 0
    rows = 0
    while _shard_ok(directory, count, num_perm):
        sigs = np.load(_sig_path(directory, count), mmap_mode="r")
        rows += int(sigs.shape[0])
        count += 1
    _delete_tail(directory, count)
    return count, rows


def _shard_ok(directory: Path, index: int, num_perm: int) -> bool:
    sig_path = _sig_path(directory, index)
    ids_path = _ids_path(directory, index)
    if not sig_path.is_file() or not ids_path.is_file():
        return False
    sigs = np.load(sig_path, mmap_mode="r")
    ids = np.load(ids_path, mmap_mode="r")
    return sigs.ndim == 2 and sigs.shape[1] == num_perm and sigs.shape[0] == ids.shape[0]


def _save_shard(directory: Path, index: int, ids: list[str], sigs: np.ndarray) -> None:
    _atomic_npy(_sig_path(directory, index), np.asarray(sigs, dtype=np.uint32))
    _atomic_npy(_ids_path(directory, index), np.asarray(ids, dtype="U64"))


def _atomic_npy(path: Path, array: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.tmp.npy")
    np.save(temporary, array)
    temporary.replace(path)


def _sig_path(directory: Path, index: int) -> Path:
    return directory / f"shard_{index:05d}.npy"


def _ids_path(directory: Path, index: int) -> Path:
    return directory / f"shard_{index:05d}.ids.npy"


def _delete_tail(directory: Path, start: int) -> None:
    if not directory.is_dir():
        return
    for child in directory.iterdir():
        if not child.is_file() or not child.name.startswith("shard_"):
            continue
        stem = child.name.removeprefix("shard_").split(".", 1)[0]
        if stem.isdigit() and int(stem) >= start:
            child.unlink()


def _clear_dir(directory: Path) -> None:
    if not directory.exists():
        return
    for child in directory.iterdir():
        if child.is_file():
            child.unlink()


def _write_clusters(cfg: Config, directory: Path, shard_count: int, output: Path) -> int:
    if shard_count == 0:
        _write_table(_empty_table(), output)
        return 0
    ids = _load_ids(directory, shard_count)
    maps = [_open_sigs(directory, index) for index in range(shard_count)]
    tables = [
        _threshold_table(ids, *_cluster(maps, threshold), threshold)
        for threshold in _thresholds(cfg)
    ]
    table = pa.concat_tables(tables) if len(tables) > 1 else tables[0]
    _write_table(table, output)
    return int(table.num_rows)


def _thresholds(cfg: Config) -> list[float]:
    return sorted({float(cfg.minhash.lsh_threshold), float(cfg.minhash.lsh_threshold_low)})


def _threshold_table(
    ids: list[str],
    labels: np.ndarray,
    sizes: np.ndarray,
    nearest: np.ndarray,
    threshold: float,
) -> pa.Table:
    return pa.table(
        {
            "file_sha": pa.array(ids, type=pa.string()),
            "cluster_id": pa.array(labels, type=pa.int64()),
            "cluster_size": pa.array(sizes, type=pa.int64()),
            "threshold": pa.array(np.full(len(ids), threshold, dtype=np.float64)),
            "nn_sim_est": pa.array(nearest, type=pa.float64(), mask=np.isnan(nearest)),
        },
        schema=_SCHEMA,
    )


def _empty_table() -> pa.Table:
    columns = {field.name: pa.array([], type=field.type) for field in _SCHEMA}
    return pa.table(columns, schema=_SCHEMA)


def _load_ids(directory: Path, shard_count: int) -> list[str]:
    ids: list[str] = []
    for index in range(shard_count):
        ids.extend(str(item) for item in np.load(_ids_path(directory, index)))
    return ids


def _open_sigs(directory: Path, index: int) -> np.ndarray:
    return np.load(_sig_path(directory, index), mmap_mode="r")


def _write_table(table: pa.Table, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    pq.write_table(table, temporary, compression="zstd")
    temporary.replace(output)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _remember(output: Path, parameters: dict[str, object]) -> None:
    path = manifest_path(output)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["parameters"] = parameters
    _write_json(path, payload)


def _saved_rows(output: Path) -> dict[str, int]:
    payload = json.loads(manifest_path(output).read_text(encoding="utf-8"))
    return {"s2_clusters": int(payload["rows"])}


def _sql_string(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"
