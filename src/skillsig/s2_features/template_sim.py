"""
STAGE: s2_features
MODULE: template_sim
PURPOSE: Score each skill against the nearest registry version with MinHash LSH.
INPUTS: s1_registry_versions.parquet, s1_registry_matches.parquet, clones, T14 shards
OUTPUTS: src/data/derived/s2_template.parquet
RQ LINK: RQ1 (registry near-duplicate signal for highly templated skills)
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from datasketch import MinHash, MinHashLSH

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_data.devsample import output_path as devsample_path
from skillsig.s1_registry.changes import _blob_texts
from skillsig.s1_registry.sources import Source, clone_dir, load_sources
from skillsig.s2_features.minhash_cluster import signature

log = logging.getLogger(__name__)

_VERSIONS = "python -m skillsig registry --only versions"
_MATCH = "python -m skillsig registry --only match"
_DEDUP = "python -m skillsig dedup"
_DEV_SAMPLE = "python -m skillsig data --dev-sample"
_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_SCHEME = "affine32"

_SCHEMA = pa.schema(
    [
        pa.field("file_sha", pa.string()),
        pa.field("match_kind", pa.string()),
        pa.field("nearest_blob", pa.string()),
        pa.field("nearest_lineage_id", pa.string()),
        pa.field("est_jaccard", pa.float64()),
    ]
)


class _RegistryIndex(MinHashLSH):
    """Registry LSH plus the signatures needed to estimate Jaccard."""

    def __init__(self, *, threshold: float, num_perm: int, seed: int) -> None:
        super().__init__(threshold=threshold, num_perm=num_perm)
        self.seed = seed
        self.signatures: dict[str, np.ndarray] = {}


def build_registry_index(cfg: Config) -> MinHashLSH:
    """MinHash LSH of registry version texts, keyed by blob SHA."""
    path = _versions_path(cfg)
    _require_file(path, _VERSIONS)
    index = _RegistryIndex(
        threshold=_index_threshold(cfg),
        num_perm=int(cfg.minhash.num_perm),
        seed=int(cfg.seed),
    )
    texts = _version_texts(cfg, _load_rows(path))
    for blob in sorted(texts):
        sig = signature(texts[blob], cfg)
        index.signatures[blob] = sig
        index.insert(blob, _as_minhash(sig, num_perm=index.h, seed=index.seed))
    log.info("registry index holds %s version signatures", len(index.signatures))
    return index


def nearest_registry(sig: np.ndarray, index: MinHashLSH) -> tuple[str | None, float]:
    """Nearest indexed blob and its estimated Jaccard, or (None, 0.0)."""
    stored, seed = _index_parts(index)
    if not stored:
        return None, 0.0
    array = _as_signature(sig, index.h)
    sketch = _as_minhash(array, num_perm=index.h, seed=seed)
    keys = sorted(key for key in (str(item) for item in index.query(sketch)) if key in stored)
    if not keys:
        return None, 0.0
    gallery = np.stack([np.asarray(stored[key], dtype=np.uint32) for key in keys])
    scores = (gallery == array).mean(axis=1)
    pick = int(scores.argmax())
    return keys[pick], float(scores[pick])


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write nearest-registry matches for shard signatures. Dev mode keeps the dev sample."""
    output = _output_path(cfg)
    _require_inputs(cfg, dev=dev)
    inputs = _inputs(cfg, dev=dev)
    if not force and inputs_unchanged(output, cfg=cfg, inputs=inputs):
        log.info("template similarity up to date: %s", output)
        return StageResult(
            stage="s2_features", outputs=[output], skipped=True, rows=_saved_rows(output)
        )
    if dev:
        log.info("template similarity scores dev-sample signatures")
    table = _build(cfg, dev=dev)
    _write_table(table, output)
    rows = int(table.num_rows)
    write_manifest(output, cfg=cfg, inputs=inputs, rows=rows)
    log.info("wrote %s (%s rows)", output, rows)
    return StageResult(
        stage="s2_features", outputs=[output], skipped=False, rows={"s2_template": rows}
    )


def _build(cfg: Config, *, dev: bool) -> pa.Table:
    index = build_registry_index(cfg)
    versions = _load_rows(_versions_path(cfg))
    matches = _load_rows(_matches_path(cfg))
    return _score_shards(
        cfg,
        index,
        lineages=_lineages(versions, matches),
        exact=_exact_blobs(matches),
        selected=_dev_ids(cfg) if dev else None,
        minimum=float(cfg.thresholds.templated_jaccard_min),
    )


def _index_threshold(cfg: Config) -> float:
    # Stay at or below the near-match cutoff so boundary pairs are still retrieved.
    return min(float(cfg.thresholds.templated_jaccard_min), float(cfg.minhash.lsh_threshold_low))


def _as_minhash(sig: np.ndarray, *, num_perm: int, seed: int) -> MinHash:
    return MinHash(
        num_perm=num_perm,
        seed=seed,
        hashvalues=np.asarray(sig, dtype=np.uint32),
        scheme=_SCHEME,
    )


def _index_parts(index: MinHashLSH) -> tuple[dict[str, np.ndarray], int]:
    stored = getattr(index, "signatures", None)
    seed = getattr(index, "seed", None)
    if not isinstance(stored, dict) or not isinstance(seed, int):
        raise TypeError("index must be the MinHashLSH returned by build_registry_index")
    return stored, seed


def _as_signature(sig: np.ndarray, num_perm: int) -> np.ndarray:
    array = np.asarray(sig, dtype=np.uint32)
    if array.shape != (num_perm,):
        raise ValueError(f"signature must have shape ({num_perm},), got {array.shape}")
    return array


def _version_texts(cfg: Config, rows: list[dict[str, object]]) -> dict[str, str]:
    by_repo = {source.repo: source for source in load_sources(cfg)}
    groups: dict[Path, list[str]] = {}
    for row in rows:
        blob = _sha(row.get("blob"))
        repo = _cell(row.get("repo"))
        if blob is None or repo is None:
            continue
        clone = _clone_for(cfg, repo, by_repo)
        if clone is not None:
            groups.setdefault(clone, []).append(blob)
    return _read_groups(groups)


def _read_groups(groups: dict[Path, list[str]]) -> dict[str, str]:
    found: dict[str, str] = {}
    missing = 0
    for clone in sorted(groups, key=lambda path: path.as_posix()):
        for blob, text in _blob_texts(clone, groups[clone]).items():
            if text is None:
                missing += 1
                continue
            found.setdefault(blob.lower(), text)
    if missing:
        log.warning("skipped %s registry blobs with no local text", missing)
    return found


def _clone_for(cfg: Config, repo: str, by_repo: dict[str, Source]) -> Path | None:
    source = by_repo.get(repo)
    if source is None:
        path = _fallback_clone(cfg, repo)
    else:
        path = clone_dir(cfg, _origin(source, by_repo))
    if path is not None and (path / ".git").is_dir():
        return path
    log.warning("missing git clone %s for %s; registry text skipped", path, repo)
    return None


def _origin(source: Source, by_repo: dict[str, Source]) -> Source:
    seen: set[str] = set()
    current = source
    while current.mirror_of and current.repo not in seen:
        seen.add(current.repo)
        nxt = by_repo.get(current.mirror_of)
        if nxt is None:
            break
        current = nxt
    return current


def _fallback_clone(cfg: Config, repo: str) -> Path | None:
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name:
        return None
    return resolve(cfg, cfg.paths.registry_repos) / f"{owner}__{name}"


def _lineages(
    versions: list[dict[str, object]], matches: list[dict[str, object]]
) -> dict[str, str]:
    chosen: dict[str, str] = {}
    ranked = sorted(_lineage_rows(versions), key=lambda item: (item[0], item[1], item[2], item[3]))
    for blob, _day, _seq, lineage in ranked:
        chosen.setdefault(blob, lineage)
    for blob, lineage in _origin_lineages(matches):
        chosen[blob] = lineage
    return chosen


def _lineage_rows(versions: list[dict[str, object]]) -> list[tuple[str, str, int, str]]:
    rows: list[tuple[str, str, int, str]] = []
    for row in versions:
        blob = _sha(row.get("blob"))
        lineage = _cell(row.get("lineage_id")) or _fallback_lineage(row)
        if blob is None or lineage is None:
            continue
        rows.append(
            (blob, _cell(row.get("first_date")) or "", _seq(row.get("version_seq")), lineage)
        )
    return rows


def _fallback_lineage(row: dict[str, object]) -> str | None:
    repo = _cell(row.get("repo"))
    skill = _cell(row.get("skill"))
    if repo is None or skill is None:
        return None
    return f"{repo}::{skill}"


def _origin_lineages(matches: list[dict[str, object]]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for row in matches:
        blob = _sha(row.get("blob"))
        repo = _cell(row.get("origin_repo"))
        skill = _cell(row.get("origin_skill"))
        if blob is not None and repo is not None and skill is not None:
            pairs.append((blob, f"{repo}::{skill}"))
    return pairs


def _exact_blobs(matches: list[dict[str, object]]) -> set[str]:
    return {blob for row in matches if (blob := _sha(row.get("blob"))) is not None}


def _score_shards(
    cfg: Config,
    index: MinHashLSH,
    *,
    lineages: dict[str, str],
    exact: set[str],
    selected: set[str] | None,
    minimum: float,
) -> pa.Table:
    parts: list[pa.Table] = []
    seen: set[str] = set()
    counts = {"exact": 0, "near": 0, "none": 0}
    for ids, sigs in _iter_shards(_shard_dir(cfg), cfg.minhash.num_perm):
        part = _score_shard(
            ids, sigs, index, lineages, exact, selected, seen, minimum, counts
        )
        if part is not None:
            parts.append(part)
    log.info(
        "template similarity exact=%s near=%s none=%s",
        counts["exact"],
        counts["near"],
        counts["none"],
    )
    if not parts:
        return _empty_table()
    table = pa.concat_tables(parts)
    return table.sort_by("file_sha") if table.num_rows else table


def _score_shard(
    ids: np.ndarray,
    sigs: np.ndarray,
    index: MinHashLSH,
    lineages: dict[str, str],
    exact: set[str],
    selected: set[str] | None,
    seen: set[str],
    minimum: float,
    counts: dict[str, int],
) -> pa.Table | None:
    file_shas: list[str] = []
    kinds: list[str] = []
    blobs: list[str | None] = []
    lineage_ids: list[str | None] = []
    scores: list[float] = []
    for pos, raw_id in enumerate(ids):
        file_sha = str(raw_id)
        if file_sha in seen or (selected is not None and file_sha not in selected):
            continue
        seen.add(file_sha)
        kind, blob, score = _classify(file_sha, sigs[pos], index, exact, minimum)
        file_shas.append(file_sha)
        kinds.append(kind)
        blobs.append(blob)
        lineage_ids.append(lineages.get(blob) if blob else None)
        scores.append(score)
        counts[kind] += 1
    if not file_shas:
        return None
    return _table(file_shas, kinds, blobs, lineage_ids, scores)


def _classify(
    file_sha: str,
    sig: np.ndarray,
    index: MinHashLSH,
    exact: set[str],
    minimum: float,
) -> tuple[str, str | None, float]:
    key = file_sha.lower()
    if key in exact:
        return "exact", key, 1.0
    blob, score = nearest_registry(sig, index)
    if blob is not None and score >= minimum:
        return "near", blob, float(score)
    if blob is None:
        return "none", None, 0.0
    return "none", blob, float(score)


def _iter_shards(directory: Path, num_perm: int):
    index = 0
    while (opened := _open_shard(directory, index, num_perm)) is not None:
        yield opened
        index += 1


def _open_shard(directory: Path, index: int, num_perm: int) -> tuple[np.ndarray, np.ndarray] | None:
    sig_path = directory / f"shard_{index:05d}.npy"
    ids_path = directory / f"shard_{index:05d}.ids.npy"
    if not sig_path.is_file() and not ids_path.is_file():
        return None
    if not sig_path.is_file() or not ids_path.is_file():
        raise ValueError(f"incomplete minhash shard {index} in {directory}; run: {_DEDUP}")
    sigs = np.load(sig_path, mmap_mode="r")
    ids = np.load(ids_path)
    if sigs.ndim != 2 or sigs.shape[1] != num_perm or sigs.shape[0] != ids.shape[0]:
        raise ValueError(f"{sig_path} does not match num_perm={num_perm}; run: {_DEDUP}")
    return ids, sigs


def _table(
    file_shas: list[str],
    kinds: list[str],
    blobs: list[str | None],
    lineage_ids: list[str | None],
    scores: list[float],
) -> pa.Table:
    return pa.table(
        {
            "file_sha": pa.array(file_shas, type=pa.string()),
            "match_kind": pa.array(kinds, type=pa.string()),
            "nearest_blob": pa.array(blobs, type=pa.string()),
            "nearest_lineage_id": pa.array(lineage_ids, type=pa.string()),
            "est_jaccard": pa.array(scores, type=pa.float64()),
        },
        schema=_SCHEMA,
    )


def _empty_table() -> pa.Table:
    columns = {field.name: pa.array([], type=field.type) for field in _SCHEMA}
    return pa.table(columns, schema=_SCHEMA)


def _output_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s2_template.parquet"


def _versions_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s1_registry_versions.parquet"


def _matches_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s1_registry_matches.parquet"


def _shard_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s2_minhash_shards"


def _require_inputs(cfg: Config, *, dev: bool) -> None:
    _require_file(_versions_path(cfg), _VERSIONS)
    _require_file(_matches_path(cfg), _MATCH)
    if dev:
        _require_file(devsample_path(cfg), _DEV_SAMPLE)
    directory = _shard_dir(cfg)
    if not (directory / "shard_00000.npy").is_file():
        raise FileNotFoundError(f"missing {directory}; run: {_DEDUP}")


def _require_file(path: Path, command: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"missing {path}; run: {command}")


def _inputs(cfg: Config, *, dev: bool) -> list[Path]:
    paths = [_versions_path(cfg), _matches_path(cfg), _shard_dir(cfg)]
    sources = cfg.root / "config" / "registry_sources.yaml"
    clones = resolve(cfg, cfg.paths.registry_repos)
    if sources.is_file():
        paths.append(sources)
    if clones.is_dir():
        paths.append(clones)
    if dev:
        paths.append(devsample_path(cfg))
    return paths


def _dev_ids(cfg: Config) -> set[str]:
    table = pq.read_table(devsample_path(cfg), columns=["file_sha"])
    return {str(item) for item in table.column("file_sha").to_pylist() if item is not None}


def _load_rows(path: Path) -> list[dict[str, object]]:
    return pq.read_table(path).to_pylist()


def _write_table(table: pa.Table, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    pq.write_table(table, temporary, compression="zstd")
    temporary.replace(output)


def _saved_rows(output: Path) -> dict[str, int]:
    payload = json.loads(manifest_path(output).read_text(encoding="utf-8"))
    return {"s2_template": int(payload["rows"])}


def _sha(value: object) -> str | None:
    text = _cell(value)
    if text is None:
        return None
    lowered = text.lower()
    return lowered if _SHA.fullmatch(lowered) else None


def _cell(value: object) -> str | None:
    if value is None or (isinstance(value, float) and value != value):
        return None
    text = str(value).strip()
    return text or None


def _seq(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0
