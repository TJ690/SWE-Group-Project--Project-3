"""
STAGE: s1_registry
MODULE: match
PURPOSE: Join registry blobs to dataset file_sha values and record origin, mirrors, and lineages.
INPUTS: s1_registry_versions.parquet, artifacts (repo_full_name, file_sha), registry source list
OUTPUTS: src/data/derived/s1_registry_matches.parquet, src/data/derived/s1_lineages.parquet
RQ LINK: RQ1 (exact registry copies and their origin) and RQ2 (lineage span and downstream copies)
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_intake.devsample import output_path as devsample_path
from skillsig.s0_intake.loader import snapshot_date
from skillsig.s1_registry.sources import Source, load_sources

log = logging.getLogger(__name__)

_VERSIONS = "python -m skillsig registry --only versions"
_DOWNLOAD = "python src/data/download_data.py"
_DEV_SAMPLE = "python -m skillsig data --dev-sample"
_SOURCES = Path("config/registry_sources.yaml")
_VERSION_TABLE = "_skillsig_match_versions"

_NEEDED = ("blob", "repo", "skill", "first_date")
_LONG = (
    "blob",
    "repo",
    "skill",
    "first_date",
    "last_date",
    "first_commit",
    "tier",
    "lineage_id",
    "files",
    "repos",
    "owners",
    "source_files",
    "holder_owners",
)
_MATCH = (
    "blob",
    "files",
    "repos",
    "owners",
    "origin_repo",
    "origin_skill",
    "origin_date",
    "tier",
    "redistributors",
)
_LINEAGE = (
    "lineage_id",
    "versions",
    "versions_seen",
    "copies",
    "owners",
    "first_date",
    "last_date",
    "span_days",
)
_MATCH_INTS = ("files", "repos", "owners")
_LINEAGE_INTS = ("versions", "versions_seen", "copies", "owners", "span_days")

_MATCH_SCHEMA = pa.schema(
    [
        ("blob", pa.string()),
        ("files", pa.int64()),
        ("repos", pa.int64()),
        ("owners", pa.int64()),
        ("origin_repo", pa.string()),
        ("origin_skill", pa.string()),
        ("origin_date", pa.string()),
        ("tier", pa.string()),
        ("redistributors", pa.list_(pa.string())),
    ]
)
_LINEAGE_SCHEMA = pa.schema(
    [
        ("lineage_id", pa.string()),
        ("versions", pa.int64()),
        ("versions_seen", pa.int64()),
        ("copies", pa.int64()),
        ("owners", pa.int64()),
        ("first_date", pa.string()),
        ("last_date", pa.string()),
        ("span_days", pa.int64()),
    ]
)


def match_exact(con: duckdb.DuckDBPyConnection, versions: pd.DataFrame) -> pd.DataFrame:
    """One row per source version whose blob equals an artifacts.file_sha.

    `files`, `repos`, and `owners` count dataset occurrences of that blob once,
    including the source repo. `source_files` counts occurrences in that repo only.
    """
    prepared = _prepare_versions(versions)
    if prepared.empty:
        return _empty_long()
    counts = _blob_counts(con, prepared)
    if counts.empty:
        return _empty_long()
    return _attach_counts(prepared, counts)


def assign_origin(matches: pd.DataFrame) -> pd.DataFrame:
    """One row per blob. Origin is the earliest source; other repos are redistributors.

    Ties break by first_commit, then repo, then skill. Another skill in the origin
    repo is not a redistributor. `tier` is the origin repo's tier.
    """
    if matches.empty:
        return _empty_matches()
    _require_columns(matches, ("blob", "repo", "skill", "first_date", *_MATCH_INTS), "matches")
    ordered = _origin_frame(matches).sort_values(
        ["blob", "first_date", "first_commit", "repo", "skill"], kind="mergesort"
    )
    origin = ordered.groupby("blob", sort=False).head(1)
    return _with_redistributors(ordered, origin)


def build_lineages(
    versions: pd.DataFrame,
    matches: pd.DataFrame,
    snapshot: date | datetime | str,
) -> pd.DataFrame:
    """One row per lineage_id.

    A version dated after `snapshot` is omitted when that lineage already has an
    earlier version. `copies` excludes occurrences in the lineage repo. `owners`
    counts distinct holders of the counted blobs, including the source repo.
    """
    prepared = _prepare_versions(versions)
    counted = _lineage_rows(prepared, _as_date(snapshot))
    if counted.empty:
        return _empty_lineages()
    summary = _summarize(counted)
    usage = _usage(counted, matches)
    return _finish_lineages(summary.merge(usage, on="lineage_id", how="left"))


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write registry matches and lineages. Dev mode keeps dev-sample blobs."""
    outputs = _output_paths(cfg)
    inputs = _inputs(cfg, dev=dev)
    _require_inputs(cfg, dev=dev)
    if not force and _current(cfg, outputs, inputs):
        log.info("registry match up to date: %s", outputs[0])
        return StageResult(
            stage="s1_registry", outputs=outputs, skipped=True, rows=_saved_rows(outputs)
        )
    if dev:
        log.info("registry match keeps dev-sample blobs and every copy of those blobs")
    written = _build(cfg, dev=dev)
    for path, count in zip(outputs, written, strict=True):
        write_manifest(path, cfg=cfg, inputs=inputs, rows=count)
    log.info("wrote %s (%s rows) and %s (%s rows)", outputs[0], written[0], outputs[1], written[1])
    return StageResult(
        stage="s1_registry",
        outputs=outputs,
        skipped=False,
        rows={"s1_registry_matches": written[0], "s1_lineages": written[1]},
    )


def _build(cfg: Config, *, dev: bool) -> tuple[int, int]:
    versions = _annotate(_read_versions(cfg), load_sources(cfg))
    snapshot = _snapshot(cfg)
    con = _connect_artifacts(cfg, dev=dev)
    try:
        matched = match_exact(con, versions)
    finally:
        con.close()
    outputs = _output_paths(cfg)
    matches = _write_frame(assign_origin(matched), outputs[0], _MATCH_SCHEMA)
    lineages = _write_frame(
        build_lineages(versions, matched, snapshot), outputs[1], _LINEAGE_SCHEMA
    )
    return matches, lineages


def _prepare_versions(versions: pd.DataFrame) -> pd.DataFrame:
    _require_columns(versions, _NEEDED, "versions")
    frame = versions.copy()
    for name in _NEEDED:
        frame[name] = frame[name].map(_day if name.endswith("date") else _text)
    keep = frame.loc[:, list(_NEEDED)].notna() & frame.loc[:, list(_NEEDED)].ne("")
    ready = frame.loc[keep.all(axis=1)].sort_values(
        ["first_date", "repo", "skill", "blob"], kind="mergesort"
    )
    return _with_defaults(ready.drop_duplicates(["repo", "skill", "blob"], keep="first"))


def _with_defaults(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["lineage_id"] = _lineage_ids(out)
    out["first_commit"] = _optional_text(out, "first_commit")
    out["last_date"] = _optional_text(out, "last_date")
    missing_last = out["last_date"].eq("")
    out.loc[missing_last, "last_date"] = out.loc[missing_last, "first_date"]
    out["tier"] = _optional_text(out, "tier")
    return out.reset_index(drop=True)


def _lineage_ids(frame: pd.DataFrame) -> pd.Series:
    fallback = frame["repo"].astype(str) + "::" + frame["skill"].astype(str)
    if "lineage_id" not in frame.columns:
        return fallback.astype("string")
    named = frame["lineage_id"].map(_text)
    blank = named.isna() | named.eq("")
    return named.mask(blank, fallback).astype("string")


def _optional_text(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame.columns:
        return pd.Series("", index=frame.index, dtype="string")
    mapped = frame[name].map(_day if name.endswith("date") else _text)
    return mapped.fillna("").astype("string")


def _blob_counts(con: duckdb.DuckDBPyConnection, versions: pd.DataFrame) -> pd.DataFrame:
    _require_artifacts(con)
    registered = False
    keys = versions.loc[:, ["blob", "repo"]].astype(str)
    try:
        con.register(_VERSION_TABLE, keys)
        registered = True
        totals = con.execute(_TOTALS_SQL).fetchdf()
        owners = con.execute(_OWNERS_SQL).fetchdf()
        source = con.execute(_SOURCE_SQL).fetchdf()
    finally:
        if registered:
            con.unregister(_VERSION_TABLE)
    if totals.empty:
        return totals
    counted = totals.merge(owners, on="blob", how="left")
    return counted.merge(source, on=["blob", "repo"], how="left")


def _attach_counts(versions: pd.DataFrame, counts: pd.DataFrame) -> pd.DataFrame:
    merged = versions.merge(counts, on=["blob", "repo"], how="inner")
    if merged.empty:
        return _empty_long()
    merged["source_files"] = merged["source_files"].fillna(0).astype("int64")
    merged["holder_owners"] = merged["holder_owners"].map(_as_owners)
    for name in ("files", "repos", "owners"):
        merged[name] = merged[name].astype("int64")
    ordered = merged.sort_values(["blob", "repo", "skill"], kind="mergesort")
    return ordered.loc[:, list(_LONG)].reset_index(drop=True)


def _origin_frame(matches: pd.DataFrame) -> pd.DataFrame:
    out = matches.copy()
    if "first_commit" not in out.columns:
        out["first_commit"] = ""
    else:
        out["first_commit"] = out["first_commit"].map(_text).fillna("")
    if "tier" not in out.columns:
        out["tier"] = ""
    return out


def _with_redistributors(ordered: pd.DataFrame, origin: pd.DataFrame) -> pd.DataFrame:
    chosen = origin.loc[:, ["blob", "repo"]].rename(columns={"repo": "origin_repo"})
    others = ordered.merge(chosen, on="blob", how="left")
    kept = others.loc[others["repo"].astype(str) != others["origin_repo"].astype(str)]
    lists = _repo_lists(kept)
    out = origin.rename(
        columns={"repo": "origin_repo", "skill": "origin_skill", "first_date": "origin_date"}
    )
    out["redistributors"] = out["blob"].map(lists).map(_string_list)
    out["tier"] = out["tier"].map(_text).fillna("")
    for name in _MATCH_INTS:
        out[name] = out[name].astype("int64")
    final = out.loc[:, list(_MATCH)].sort_values("blob", kind="mergesort")
    return final.reset_index(drop=True)


def _repo_names(repos: pd.Series) -> list[str]:
    return [str(repo) for repo in repos]


def _repo_lists(frame: pd.DataFrame) -> pd.Series:
    if frame.empty:
        return pd.Series(dtype=object)
    ordered = frame.loc[:, ["blob", "repo"]].drop_duplicates().sort_values(
        ["blob", "repo"], kind="mergesort"
    )
    return ordered.groupby("blob", sort=False)["repo"].agg(_repo_names)


def _lineage_rows(versions: pd.DataFrame, snapshot: date) -> pd.DataFrame:
    if versions.empty:
        return versions
    released = versions.loc[versions["first_date"] <= snapshot.isoformat()]
    covered = set(released["lineage_id"].astype(str))
    late = versions.loc[~versions["lineage_id"].astype(str).isin(covered)]
    if late.empty:
        return released.reset_index(drop=True)
    return pd.concat([released, late], ignore_index=True)


def _summarize(rows: pd.DataFrame) -> pd.DataFrame:
    summary = (
        rows.groupby("lineage_id", sort=False)
        .agg(
            versions=("blob", "nunique"),
            first_date=("first_date", "min"),
            last_date=("last_date", "max"),
        )
        .reset_index()
    )
    pairs = zip(summary["first_date"], summary["last_date"], strict=True)
    summary["span_days"] = [_span(first, last) for first, last in pairs]
    return summary


def _usage(rows: pd.DataFrame, matches: pd.DataFrame) -> pd.DataFrame:
    keys = rows.loc[:, ["lineage_id", "repo", "blob"]].drop_duplicates()
    linked = _link_matches(keys, matches)
    owners = _owner_counts(linked)
    usage = linked.groupby("lineage_id", sort=False).agg(
        versions_seen=("seen", "sum"), copies=("downstream", "sum")
    )
    usage["owners"] = [owners.get(str(key), 0) for key in usage.index]
    return usage.reset_index()


def _link_matches(keys: pd.DataFrame, matches: pd.DataFrame) -> pd.DataFrame:
    empty = _blank_link(keys)
    if matches.empty or "files" not in matches.columns:
        return empty
    _require_columns(matches, ("repo", "blob", "files", "source_files", "holder_owners"), "matches")
    slim = matches.drop_duplicates(["repo", "blob"], keep="first")
    columns = ["repo", "blob", "files", "source_files", "holder_owners"]
    linked = keys.merge(slim.loc[:, columns], how="left")
    linked["seen"] = linked["files"].notna()
    linked["downstream"] = _downstream(linked)
    return linked


def _blank_link(keys: pd.DataFrame) -> pd.DataFrame:
    out = keys.copy()
    out["files"] = pd.Series(pd.NA, index=out.index, dtype="Int64")
    out["source_files"] = pd.Series(0, index=out.index, dtype="int64")
    out["holder_owners"] = [[] for _ in range(len(out))]
    out["seen"] = False
    out["downstream"] = 0
    return out


def _downstream(linked: pd.DataFrame) -> pd.Series:
    files = linked["files"].fillna(0).astype("int64")
    source = linked["source_files"].fillna(0).astype("int64")
    return (files - source).clip(lower=0)


def _owner_counts(linked: pd.DataFrame) -> dict[str, int]:
    found: dict[str, set[str]] = {}
    seen = linked.loc[linked["seen"], ["lineage_id", "holder_owners"]]
    for lineage_id, owners in zip(seen["lineage_id"], seen["holder_owners"], strict=True):
        found.setdefault(str(lineage_id), set()).update(_as_owners(owners))
    return {key: len(value) for key, value in found.items()}


def _finish_lineages(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for name in ("versions_seen", "copies", "owners"):
        out[name] = out[name].fillna(0).astype("int64")
    out["versions"] = out["versions"].astype("int64")
    out["span_days"] = out["span_days"].astype("int64")
    ordered = out.loc[:, list(_LINEAGE)].sort_values("lineage_id", kind="mergesort")
    return ordered.reset_index(drop=True)


def _annotate(versions: pd.DataFrame, sources: list[Source]) -> pd.DataFrame:
    mirrors = {source.repo for source in sources if source.mirror_of}
    tiers = {source.repo: source.tier for source in sources}
    frame = versions.loc[~versions["repo"].map(_text).isin(mirrors)].copy()
    frame["tier"] = frame["repo"].map(_text).map(tiers).fillna("")
    names = {name for name in frame.loc[frame["tier"].eq(""), "repo"].map(_text) if name}
    unknown = sorted(names)
    if unknown:
        log.warning("registry version repos missing from sources: %s", ", ".join(unknown))
    return frame.reset_index(drop=True)


def _connect_artifacts(cfg: Config, *, dev: bool) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    try:
        glob = _sql_string(_artifacts_dir(cfg) / "*.parquet")
        if dev:
            sample = _sql_string(devsample_path(cfg))
            con.execute(
                f"""
                CREATE VIEW artifacts AS
                SELECT a.repo_full_name, a.file_sha
                FROM read_parquet({glob}) a
                SEMI JOIN read_parquet({sample}) s ON a.file_sha = s.file_sha
                """
            )
        else:
            con.execute(
                f"""
                CREATE VIEW artifacts AS
                SELECT repo_full_name, file_sha FROM read_parquet({glob})
                """
            )
    except Exception:
        con.close()
        raise
    return con


def _snapshot(cfg: Config) -> date:
    configured = cfg.rq2.snapshot_date.strip()
    if configured != "auto":
        return date.fromisoformat(configured)
    con = duckdb.connect()
    try:
        con.execute("SET TimeZone = 'UTC'")
        listed = _sql_string(_mining_runs_dir(cfg) / "*.parquet")
        con.execute(f"CREATE VIEW mining_runs AS SELECT * FROM read_parquet({listed})")
        return snapshot_date(con)
    finally:
        con.close()


def _current(cfg: Config, outputs: list[Path], inputs: list[Path]) -> bool:
    return all(inputs_unchanged(path, cfg=cfg, inputs=inputs) for path in outputs)


def _require_inputs(cfg: Config, *, dev: bool) -> None:
    versions = _versions_path(cfg)
    if not versions.is_file():
        raise FileNotFoundError(f"missing {versions}; run: {_VERSIONS}")
    directory = _artifacts_dir(cfg)
    if not directory.is_dir() or not any(directory.glob("*.parquet")):
        raise FileNotFoundError(f"missing {directory}; run: {_DOWNLOAD}")
    if dev and not devsample_path(cfg).is_file():
        raise FileNotFoundError(f"missing {devsample_path(cfg)}; run: {_DEV_SAMPLE}")
    if not _snapshot_auto(cfg):
        return
    runs = _mining_runs_dir(cfg)
    if not runs.is_dir() or not any(runs.glob("*.parquet")):
        raise FileNotFoundError(f"missing {runs}; run: {_DOWNLOAD}")


def _inputs(cfg: Config, *, dev: bool) -> list[Path]:
    paths = [_versions_path(cfg), _artifacts_dir(cfg), cfg.root / _SOURCES]
    if _snapshot_auto(cfg):
        paths.append(_mining_runs_dir(cfg))
    if dev:
        paths.append(devsample_path(cfg))
    return paths


def _output_paths(cfg: Config) -> list[Path]:
    derived = resolve(cfg, cfg.paths.derived)
    return [derived / "s1_registry_matches.parquet", derived / "s1_lineages.parquet"]


def _versions_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s1_registry_versions.parquet"


def _artifacts_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "artifacts"


def _mining_runs_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "mining_runs"


def _snapshot_auto(cfg: Config) -> bool:
    return cfg.rq2.snapshot_date.strip() == "auto"


def _read_versions(cfg: Config) -> pd.DataFrame:
    return pd.read_parquet(_versions_path(cfg))


def _write_frame(frame: pd.DataFrame, path: Path, schema: pa.Schema) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = [_column_array(frame, field) for field in schema]
    table = pa.Table.from_arrays(arrays, schema=schema)
    pq.write_table(table, path, compression="zstd")
    return int(table.num_rows)


def _column_array(frame: pd.DataFrame, field: pa.Field) -> pa.Array:
    if frame.empty:
        return pa.array([], type=field.type)
    if pa.types.is_list(field.type):
        return pa.array([_string_list(value) for value in frame[field.name]], type=field.type)
    if pa.types.is_integer(field.type):
        return pa.array([int(value) for value in frame[field.name]], type=field.type)
    return pa.array([_text(value) or "" for value in frame[field.name]], type=field.type)


def _require_artifacts(con: duckdb.DuckDBPyConnection) -> None:
    names = {name for (name,) in con.execute("SHOW TABLES").fetchall()}
    views = {name for (name,) in con.execute("SHOW VIEWS").fetchall()} if _has_views(con) else set()
    if "artifacts" not in names and "artifacts" not in views:
        raise ValueError("artifacts view is not registered; expected repo_full_name and file_sha")


def _has_views(con: duckdb.DuckDBPyConnection) -> bool:
    try:
        con.execute("SHOW VIEWS")
    except duckdb.Error:
        return False
    return True


def _require_columns(frame: pd.DataFrame, names: tuple[str, ...] | list[str], label: str) -> None:
    missing = [name for name in names if name not in frame.columns]
    if missing:
        raise ValueError(f"{label} missing columns: {missing}")


def _empty_long() -> pd.DataFrame:
    frame = pd.DataFrame(columns=list(_LONG))
    text = (
        "blob",
        "repo",
        "skill",
        "first_date",
        "last_date",
        "first_commit",
        "tier",
        "lineage_id",
    )
    for name in text:
        frame[name] = pd.Series(dtype="string")
    for name in ("files", "repos", "owners", "source_files"):
        frame[name] = pd.Series(dtype="int64")
    frame["holder_owners"] = pd.Series(dtype=object)
    return frame.loc[:, list(_LONG)]


def _empty_matches() -> pd.DataFrame:
    frame = pd.DataFrame(columns=list(_MATCH))
    for name in ("blob", "origin_repo", "origin_skill", "origin_date", "tier"):
        frame[name] = pd.Series(dtype="string")
    for name in _MATCH_INTS:
        frame[name] = pd.Series(dtype="int64")
    frame["redistributors"] = pd.Series(dtype=object)
    return frame.loc[:, list(_MATCH)]


def _empty_lineages() -> pd.DataFrame:
    frame = pd.DataFrame(columns=list(_LINEAGE))
    frame["lineage_id"] = pd.Series(dtype="string")
    frame["first_date"] = pd.Series(dtype="string")
    frame["last_date"] = pd.Series(dtype="string")
    for name in _LINEAGE_INTS:
        frame[name] = pd.Series(dtype="int64")
    return frame.loc[:, list(_LINEAGE)]


def _saved_rows(outputs: list[Path]) -> dict[str, int]:
    names = ("s1_registry_matches", "s1_lineages")
    rows: dict[str, int] = {}
    for name, path in zip(names, outputs, strict=True):
        payload = json.loads(manifest_path(path).read_text(encoding="utf-8"))
        rows[name] = int(payload["rows"])
    return rows


def _span(first: object, last: object) -> int:
    start = date.fromisoformat(str(first))
    end = date.fromisoformat(str(last))
    return (end - start).days


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_owners(value: object) -> list[str]:
    if value is None or value is pd.NA:
        return []
    if isinstance(value, float) and value != value:
        return []
    if isinstance(value, str):
        return [value] if value else []
    try:
        return sorted({str(item) for item in value if str(item)})  # type: ignore[arg-type]
    except TypeError:
        return []


def _string_list(value: object) -> list[str]:
    return _as_owners(value)


def _day(value: object) -> str:
    text = _text(value)
    if text is None:
        return ""
    return text[:10]


def _text(value: object) -> str | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and value != value:
        return None
    text = str(value).strip()
    return text or None


def _sql_string(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


_TOTALS_SQL = f"""
SELECT a.file_sha AS blob,
       v.repo AS repo,
       count(*)::BIGINT AS files,
       count(DISTINCT a.repo_full_name)::BIGINT AS repos,
       count(DISTINCT split_part(a.repo_full_name, '/', 1))::BIGINT AS owners
FROM artifacts a
JOIN (SELECT DISTINCT blob, repo FROM {_VERSION_TABLE}) v ON a.file_sha = v.blob
WHERE a.file_sha IS NOT NULL AND a.repo_full_name IS NOT NULL
GROUP BY a.file_sha, v.repo
"""

_OWNERS_SQL = f"""
SELECT blob, list(owner ORDER BY owner) AS holder_owners
FROM (
    SELECT DISTINCT a.file_sha AS blob, split_part(a.repo_full_name, '/', 1) AS owner
    FROM artifacts a
    SEMI JOIN (SELECT DISTINCT blob FROM {_VERSION_TABLE}) v ON a.file_sha = v.blob
    WHERE a.file_sha IS NOT NULL AND a.repo_full_name IS NOT NULL
)
GROUP BY blob
"""

_SOURCE_SQL = f"""
SELECT v.blob, v.repo, count(*)::BIGINT AS source_files
FROM (SELECT DISTINCT blob, repo FROM {_VERSION_TABLE}) v
JOIN artifacts a ON a.file_sha = v.blob AND a.repo_full_name = v.repo
WHERE a.file_sha IS NOT NULL
GROUP BY v.blob, v.repo
"""
