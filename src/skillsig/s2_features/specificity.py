"""
STAGE: s2_features
MODULE: specificity
PURPOSE: Share of a skill's identifiers (paths, flags, code names) that almost no other skill uses.
INPUTS: artifacts primary rows (readable filter), s2_clusters.parquet (primary threshold)
OUTPUTS: src/data/derived/s2_specificity.parquet, s2_specificity_identifiers/part-*.parquet
RQ LINK: RQ1 (positive evidence of project-specific writing; human / customised signal)

An identifier is rare when it appears in fewer than RARE_MAX_CLUSTERS near-duplicate clusters
across the whole readable population. Counting clusters rather than files stops a widely copied
skill from making its own identifiers look common. Document frequency always comes from the full
population; dev mode only limits which skills get a score. Text alone feeds this feature, never
labels, so computing it on every skill does not leak anything into evaluation.

Pass 1 reads each artifacts file in pyarrow batches and writes one (file_sha, identifier) part
per file; finished parts are reused, so an interrupted run resumes. Pass 2 joins clusters and
counts in DuckDB. --force recomputes the scores; parts are rebuilt only when missing.
"""

from __future__ import annotations

import gc
import json
import logging
import multiprocessing
import os
import re
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_intake.devsample import output_path as devsample_path

log = logging.getLogger(__name__)

RARE_MAX_CLUSTERS = 5  # rare = found in fewer than this many clusters
MIN_IDENTIFIERS = 3  # below this a skill gets no score (null)
MAX_IDENTIFIER_LEN = 80
N_BUCKETS = 16  # pass 2 counts identifiers in hash buckets to bound memory
MAX_TEXT_CHARS = 50_000  # identifiers come from the first 50k characters (minhash reads all)
_BATCH_ROWS = 2_000  # skill texts can be huge; small batches keep memory under ~2 GB

_FRONT = re.compile(r"^\s*---.*?\n---\s*\n?", re.DOTALL)
_URL = re.compile(r"https?://\S+|www\.\S+")
_INLINE = re.compile(r"`([^`\n]{2,80})`")
_PATH = re.compile(r"(?<![\w/.~-])(?:~/|\./|/)?(?:[\w.-]+/)+[\w.-]+")
_FILE = re.compile(
    r"\b[\w-]+\.(?:py|ts|tsx|js|jsx|mjs|md|json|ya?ml|toml|sh|go|rs|java|rb|sql|css|html|"
    r"ipynb|txt|csv|env|lock|cfg|ini|xml|kt|swift|php|c|h|cpp)\b"
)
_FLAG = re.compile(r"(?<![\w-])--[a-z][\w-]{1,40}")
_CAMEL = re.compile(r"\b(?:[a-z]+[A-Z]|[A-Z][a-z0-9]+[A-Z])[A-Za-z0-9]*\b")
_SNAKE = re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b")
_PAIR_SCHEMA = pa.schema([pa.field("file_sha", pa.string()), pa.field("identifier", pa.string())])
_DOWNLOAD = "python src/data/download_data.py"
_DEDUP = "python -m skillsig dedup"


def identifiers(text: str) -> frozenset[str]:
    """Distinct identifiers in a skill body: inline code, paths, file names, flags, code names."""
    if not text:
        return frozenset()
    body = _URL.sub(" ", _FRONT.sub("", text, count=1))
    found: set[str] = {span.strip() for span in _INLINE.findall(body)}
    for pattern in (_PATH, _FILE, _FLAG, _CAMEL, _SNAKE):
        found.update(pattern.findall(body))
    return frozenset(
        token.lower()
        for token in found
        if 2 <= len(token) <= MAX_IDENTIFIER_LEN and not token.isdigit()
    )


def score(idents: frozenset[str], df: dict[str, int]) -> dict[str, object]:
    """n_identifiers, n_rare and specificity (null below MIN_IDENTIFIERS).

    Reference definition for one skill; run() computes the same thing in SQL at scale.
    """
    n = len(idents)
    rare = sum(1 for token in idents if df.get(token, 1) < RARE_MAX_CLUSTERS)
    return {
        "n_identifiers": n,
        "n_rare": rare,
        "specificity": rare / n if n >= MIN_IDENTIFIERS else None,
    }


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write s2_specificity.parquet. DF is always computed over the full readable population."""
    derived = resolve(cfg, cfg.paths.derived)
    output = derived / "s2_specificity.parquet"
    pairs_path = derived / "s2_specificity_identifiers"
    inputs = _inputs(cfg, dev=dev)
    _require_inputs(cfg, dev=dev)
    if not force and inputs_unchanged(output, cfg=cfg, inputs=inputs):
        payload = json.loads(manifest_path(output).read_text(encoding="utf-8"))
        rows = {"s2_specificity": int(payload["rows"])}
        return StageResult("s2_features", [output], skipped=True, rows=rows)
    n_pairs = _write_pairs(cfg, pairs_path)
    rows = _write_scores(cfg, pairs_path, output, dev=dev)
    write_manifest(output, cfg=cfg, inputs=inputs, rows=rows)
    log.info("wrote %s (%s rows, %s identifier pairs)", output, rows, n_pairs)
    return StageResult("s2_features", [output], rows={"s2_specificity": rows})


def _readable_batches(path: Path, chunk_rows: int):
    """Yield lists of (file_sha, content) for readable primary skills in one artifacts file.

    Same filter as wording/minhash (T02 readable rule). pyarrow batches keep memory bounded.
    """
    columns = ["file_sha", "content", "dedup_primary", "body_chars"]
    for batch in pq.ParquetFile(path).iter_batches(batch_size=chunk_rows, columns=columns):
        content = pc.utf8_slice_codeunits(batch.column("content"), 0, MAX_TEXT_CHARS)
        data = batch.set_column(1, "content", content).to_pydict()
        rows = [
            (sha, text)
            for sha, text, primary, chars in zip(
                data["file_sha"],
                data["content"],
                data["dedup_primary"],
                data["body_chars"],
                strict=True,
            )
            if _readable(text, primary, chars)
        ]
        if rows:
            yield rows


def _readable(text: object, primary: object, chars: object) -> bool:
    if str(primary) not in {"1", "True", "true"} or not isinstance(text, str):
        return False
    if "\n" not in text and text.endswith("SKILL.md"):
        return False
    if text.startswith("Redirecting"):
        return False
    return chars is not None and int(chars) >= 200


def _extract(rows: list[tuple[str, str]]) -> list[tuple[str, list[str]]]:
    return [(sha, sorted(identifiers(text or ""))) for sha, text in rows]


def _write_pairs(cfg: Config, directory: Path) -> int:
    """Pass 1: one identifier file per artifacts file; finished parts are reused (resumable).

    Part names carry the text cap, so changing MAX_TEXT_CHARS builds fresh parts. To rebuild
    with the same settings, delete the s2_specificity_identifiers folder.
    """
    directory.mkdir(parents=True, exist_ok=True)
    workers = int(os.environ.get("SKILLSIG_WORKERS") or max(1, os.cpu_count() or 1))
    sources = sorted((resolve(cfg, cfg.paths.raw) / "artifacts").glob("*.parquet"))
    pairs = 0
    todo = [
        (index, source)
        for index, source in enumerate(sources)
        if not (directory / f"part-{index:05d}-c{MAX_TEXT_CHARS}.parquet").exists()
    ]
    if not todo:
        return sum(
            pq.ParquetFile(p).metadata.num_rows
            for p in directory.glob(f"part-*-c{MAX_TEXT_CHARS}.parquet")
        )
    with multiprocessing.get_context("spawn").Pool(processes=workers) as pool:
        for index, source in enumerate(sources):
            part = directory / f"part-{index:05d}-c{MAX_TEXT_CHARS}.parquet"
            if part.exists():
                pairs += pq.ParquetFile(part).metadata.num_rows
                continue
            pairs += _write_part(source, part, pool, workers, min(int(cfg.chunk_rows), _BATCH_ROWS))
            pa.default_memory_pool().release_unused()
            gc.collect()
            log.info(
                "specificity: %s/%s files, %s identifier pairs", index + 1, len(sources), pairs
            )
    return pairs


def _write_part(source: Path, part: Path, pool, workers: int, chunk_rows: int) -> int:
    temporary = part.with_name(part.name + ".tmp")
    count = 0
    with pq.ParquetWriter(temporary, _PAIR_SCHEMA, compression="zstd") as writer:
        for batch in _readable_batches(source, chunk_rows):
            size = max(1, len(batch) // workers + 1)
            chunks = [batch[i : i + size] for i in range(0, len(batch), size)]
            shas: list[str] = []
            idents: list[str] = []
            for result in pool.map(_extract, chunks):
                for sha, tokens in result:
                    shas += [sha] * len(tokens)
                    idents += tokens
            writer.write_table(
                pa.table({"file_sha": shas, "identifier": idents}, schema=_PAIR_SCHEMA)
            )
            count += len(idents)
    temporary.replace(part)
    return count


def _write_scores(cfg: Config, pairs: Path, output: Path, *, dev: bool) -> int:
    """Pass 2: cluster document frequency per identifier, then the per-skill share.

    Identifiers are split into N_BUCKETS by hash; each bucket is counted on its own and saved,
    so memory stays small and an interrupted run resumes. Buckets older than the newest part
    are rebuilt.
    """
    buckets = pairs.parent / f"s2_specificity_buckets_c{MAX_TEXT_CHARS}"
    buckets.mkdir(parents=True, exist_ok=True)
    newest_part = max(p.stat().st_mtime for p in pairs.glob(f"*-c{MAX_TEXT_CHARS}.parquet"))
    for bucket in range(N_BUCKETS):
        target = buckets / f"bucket-{bucket:02d}.parquet"
        if target.exists() and target.stat().st_mtime >= newest_part:
            continue
        _run_sql(_bucket_sql(cfg, pairs, target, bucket))
        log.info("specificity: bucket %s/%s counted", bucket + 1, N_BUCKETS)
    sample_filter = ""
    if dev:
        sample = _sql(devsample_path(cfg))
        sample_filter = f"WHERE file_sha IN (SELECT file_sha FROM read_parquet({sample}))"
    _run_sql(
        f"""
        COPY (
            WITH per_skill AS (
                SELECT file_sha, sum(n_identifiers)::INTEGER AS n_identifiers,
                       sum(n_rare)::INTEGER AS n_rare
                FROM read_parquet({_sql(buckets / "bucket-*.parquet")})
                {sample_filter}
                GROUP BY file_sha
            )
            SELECT file_sha, n_identifiers, n_rare,
                   CASE WHEN n_identifiers >= {MIN_IDENTIFIERS}
                        THEN n_rare::DOUBLE / n_identifiers END AS specificity
            FROM per_skill ORDER BY file_sha
        ) TO {_sql(output)} (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    con = duckdb.connect()
    try:
        return int(
            con.execute(f"SELECT count(*) FROM read_parquet({_sql(output)})").fetchall()[0][0]
        )
    finally:
        con.close()


def _bucket_sql(cfg: Config, pairs: Path, target: Path, bucket: int) -> str:
    clusters = _sql(resolve(cfg, cfg.paths.derived) / "s2_clusters.parquet")
    parts = _sql(pairs / f"*-c{MAX_TEXT_CHARS}.parquet")
    threshold = float(cfg.minhash.lsh_threshold)
    return f"""
        COPY (
            WITH cl AS (
                SELECT file_sha, any_value(cluster_id) AS cluster_id
                FROM read_parquet({clusters})
                WHERE abs(threshold - {threshold!r}) < 1e-9 GROUP BY file_sha
            ),
            hashed AS (
                SELECT p.file_sha, hash(p.identifier) AS h,
                       coalesce(hash(cl.cluster_id), hash('f:' || p.file_sha)) AS g
                FROM read_parquet({parts}) p LEFT JOIN cl USING (file_sha)
                WHERE hash(p.identifier) % {N_BUCKETS} = {bucket}
            ),
            df AS (
                SELECT h, count(*) AS clusters
                FROM (SELECT DISTINCT h, g FROM hashed) GROUP BY h
            )
            SELECT x.file_sha, count(*)::INTEGER AS n_identifiers,
                   count(*) FILTER (WHERE df.clusters < {RARE_MAX_CLUSTERS})::INTEGER AS n_rare
            FROM hashed x JOIN df USING (h)
            GROUP BY x.file_sha
        ) TO {_sql(target)} (FORMAT PARQUET, COMPRESSION ZSTD)
    """


def _run_sql(query: str) -> None:
    con = duckdb.connect()
    try:
        con.execute("SET preserve_insertion_order = false")
        limit = os.environ.get("SKILLSIG_DUCKDB_MEMORY")  # e.g. "1500MB" on small machines
        if limit:
            con.execute(f"SET memory_limit = '{limit}'")
        threads = os.environ.get("SKILLSIG_DUCKDB_THREADS")
        if threads:
            con.execute(f"SET threads = {int(threads)}")
        con.execute(query)
    finally:
        con.close()


def _inputs(cfg: Config, *, dev: bool) -> list[Path]:
    paths = [
        resolve(cfg, cfg.paths.raw) / "artifacts",
        resolve(cfg, cfg.paths.derived) / "s2_clusters.parquet",
    ]
    if dev:
        paths.append(devsample_path(cfg))
    return paths


def _require_inputs(cfg: Config, *, dev: bool) -> None:
    artifacts = resolve(cfg, cfg.paths.raw) / "artifacts"
    if not artifacts.is_dir() or not any(artifacts.glob("*.parquet")):
        raise FileNotFoundError(f"missing {artifacts}; run: {_DOWNLOAD}")
    clusters = resolve(cfg, cfg.paths.derived) / "s2_clusters.parquet"
    if not clusters.is_file():
        raise FileNotFoundError(f"missing {clusters}; run: {_DEDUP}")
    if dev and not devsample_path(cfg).is_file():
        command = "python -m skillsig data --dev-sample"
        raise FileNotFoundError(f"missing {devsample_path(cfg)}; run: {command}")


def _sql(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"
