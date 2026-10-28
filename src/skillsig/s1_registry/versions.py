"""
STAGE: s1_registry
MODULE: versions
PURPOSE: Collapse mined SKILL.md history into one ordered row per distinct blob.
INPUTS: s1_versions_raw.parquet, s1_commit_trailers.parquet, mining snapshot date
OUTPUTS: src/data/derived/s1_registry_versions.parquet
RQ LINK: RQ1 (version sequence and description idiom) and RQ2 (snapshot flag, intro trailer)
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_intake.loader import snapshot_date
from skillsig.s1_registry.sources import Source, clone_dir, load_sources
from skillsig.s2_features.structure import split_front_matter

log = logging.getLogger(__name__)

_MINE = "python -m skillsig registry --only mine"
_DOWNLOAD = "python src/data/download_data.py"
_RERUN = "python -m skillsig registry --only versions"

_COLUMNS = (
    "lineage_id",
    "repo",
    "skill",
    "blob",
    "version_seq",
    "first_commit",
    "first_date",
    "last_date",
    "n_commits",
    "subject",
    "release_label",
    "size_bytes",
    "ai_trailer_at_introduction",
    "desc_idiom",
    "post_snapshot",
    "renamed_group",
)
_NEEDED = (
    "repo",
    "commit",
    "commit_date",
    "subject",
    "skill",
    "blob",
    "release_label",
)
_KEY = ("repo", "skill", "blob", "commit", "commit_date")
_TEXT = (
    "lineage_id",
    "repo",
    "skill",
    "blob",
    "first_commit",
    "first_date",
    "last_date",
    "subject",
    "release_label",
    "desc_idiom",
    "renamed_group",
)
_STRING_COLUMNS = (
    "lineage_id",
    "repo",
    "skill",
    "blob",
    "first_commit",
    "first_date",
    "last_date",
    "subject",
)
_INTS = {"version_seq": False, "n_commits": False, "size_bytes": True}

_SCHEMA = pa.schema(
    [
        ("lineage_id", pa.string()),
        ("repo", pa.string()),
        ("skill", pa.string()),
        ("blob", pa.string()),
        ("version_seq", pa.int64()),
        ("first_commit", pa.string()),
        ("first_date", pa.string()),
        ("last_date", pa.string()),
        ("n_commits", pa.int64()),
        ("subject", pa.string()),
        ("release_label", pa.string()),
        ("size_bytes", pa.int64()),
        ("ai_trailer_at_introduction", pa.bool_()),
        ("desc_idiom", pa.string()),
        ("post_snapshot", pa.bool_()),
        ("renamed_group", pa.string()),
    ]
)

_SPACE = re.compile(r"\s+")
_WHENEVER = re.compile(r"^use this skill whenever\b")
_USE_WHEN = re.compile(
    r"^(?:always use this when|this skill should be used when|"
    r"use this skill when|use this when|use when)\b"
)
_GUIDE = re.compile(r"^(?:[a-z0-9][a-z0-9'+./-]*\s+){0,6}guides?\s+for\b")
_CAPABILITY_PHRASE = re.compile(
    r"^(?:use this skill to\b|this skill (?:helps|creates|generates|provides|enables|lets)\b)"
)
_WORD = re.compile(r"^[a-z]+")
_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_FETCH_CHUNK = 400
_CAPABILITY_VERBS = frozenset(
    {
        "add",
        "analyze",
        "analyse",
        "apply",
        "audit",
        "automate",
        "build",
        "builds",
        "configure",
        "convert",
        "create",
        "creates",
        "debug",
        "deploy",
        "design",
        "draft",
        "edit",
        "enable",
        "enables",
        "evaluate",
        "evaluates",
        "execute",
        "extract",
        "fix",
        "format",
        "generate",
        "generates",
        "help",
        "helps",
        "implement",
        "let",
        "lets",
        "manage",
        "monitor",
        "optimize",
        "optimise",
        "parse",
        "plan",
        "provide",
        "provides",
        "publish",
        "query",
        "read",
        "refactor",
        "render",
        "review",
        "run",
        "scaffold",
        "search",
        "set",
        "summarize",
        "summarise",
        "test",
        "transform",
        "translate",
        "turn",
        "update",
        "validate",
        "write",
        "writes",
    }
)


def description_idiom(desc: str | None) -> str:
    """Opening formula of a description: a trigger idiom, a capability summary, or other."""
    text = _opening(desc)
    if text is None:
        return "other"
    if _WHENEVER.match(text):
        return "use_this_skill_whenever"
    if _USE_WHEN.match(text):
        return "use_when"
    if _GUIDE.match(text):
        return "guide_for"
    if _capability(text):
        return "capability_summary"
    return "other"


def build_versions(raw: pd.DataFrame, trailers: pd.DataFrame, snapshot: date) -> pd.DataFrame:
    """One row per (repo, skill, blob), ordered by introduction date then commit."""
    prepared = _prepare(raw, trailers)
    if prepared.empty:
        return _empty_versions()
    collapsed = _collapse(prepared)
    sequenced = _assign_sequence(collapsed)
    flagged = _flag_snapshot(sequenced, snapshot)
    renamed = _assign_renames(flagged)
    return _finalize(renamed)


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write s1_registry_versions.parquet. Dev mode keeps every mined blob."""
    output = _output_path(cfg)
    inputs = _inputs(cfg)
    _require_inputs(cfg)
    if not force and inputs_unchanged(output, cfg=cfg, inputs=inputs):
        log.info("registry versions up to date: %s", output)
        return StageResult(
            stage="s1_registry", outputs=[output], skipped=True, rows=_saved_rows(output)
        )
    if dev:
        log.info("registry versions keep every mined blob; the dev sample is file-level")
    frame = build_versions(_read_table(inputs[0]), _read_table(inputs[1]), _snapshot(cfg))
    frame = _attach_blob_facts(cfg, frame)
    rows = _write_parquet(frame, output)
    write_manifest(output, cfg=cfg, inputs=inputs, rows=rows)
    log.info("wrote %s (%s rows)", output, rows)
    return StageResult(
        stage="s1_registry",
        outputs=[output],
        skipped=False,
        rows={"s1_registry_versions": rows},
    )


def _opening(desc: str | None) -> str | None:
    if not isinstance(desc, str):
        return None
    text = desc.strip().lower().lstrip("\"'`“”‘’*>")
    text = _SPACE.sub(" ", text).strip()
    return text or None


def _capability(text: str) -> bool:
    if _CAPABILITY_PHRASE.match(text):
        return True
    match = _WORD.match(text)
    return match is not None and match.group(0) in _CAPABILITY_VERBS


def _prepare(raw: pd.DataFrame, trailers: pd.DataFrame) -> pd.DataFrame:
    missing = [name for name in _NEEDED if name not in raw.columns]
    if missing:
        raise ValueError(f"versions raw missing columns: {missing}")
    frame = raw.loc[:, list(_NEEDED)].copy()
    for name in _NEEDED:
        frame[name] = frame[name].astype("string").str.strip()
    frame = _drop_incomplete(frame)
    frame.loc[frame["release_label"].eq(""), "release_label"] = pd.NA
    return frame.merge(_trailer_flags(trailers), on=["repo", "commit"], how="left").assign(
        has_ai_trailer=lambda df: df["has_ai_trailer"].eq(True)
    )


def _drop_incomplete(frame: pd.DataFrame) -> pd.DataFrame:
    present = frame.loc[:, list(_KEY)].notna() & frame.loc[:, list(_KEY)].ne("")
    keep = present.all(axis=1)
    dropped = int((~keep).sum())
    if dropped:
        log.warning("dropped %s version rows with null commit fields", dropped)
    return frame.loc[keep].reset_index(drop=True)


def _trailer_flags(trailers: pd.DataFrame) -> pd.DataFrame:
    columns = ["repo", "commit", "has_ai_trailer"]
    if trailers.empty or any(name not in trailers.columns for name in columns):
        return pd.DataFrame(columns=columns)
    flags = trailers.loc[:, columns].copy()
    flags["repo"] = flags["repo"].astype("string").str.strip()
    flags["commit"] = flags["commit"].astype("string").str.strip()
    flags["has_ai_trailer"] = flags["has_ai_trailer"].eq(True)
    return flags.groupby(["repo", "commit"], sort=False)["has_ai_trailer"].any().reset_index()


def _collapse(frame: pd.DataFrame) -> pd.DataFrame:
    ordered = frame.sort_values(
        ["repo", "skill", "blob", "commit_date", "commit"], kind="mergesort"
    )
    intro = ordered.groupby(["repo", "skill", "blob"], sort=False).head(1)
    stats = _group_stats(ordered)
    flags = _intro_flags(ordered, intro)
    named = intro.rename(columns={"commit": "first_commit", "commit_date": "first_date"})
    keep = ["repo", "skill", "blob", "first_commit", "first_date", "subject", "release_label"]
    return named.loc[:, keep].merge(stats, on=["repo", "skill", "blob"]).merge(
        flags, on=["repo", "skill", "blob"]
    )


def _group_stats(ordered: pd.DataFrame) -> pd.DataFrame:
    return (
        ordered.groupby(["repo", "skill", "blob"], sort=False)
        .agg(last_date=("commit_date", "max"), n_commits=("commit", "nunique"))
        .reset_index()
    )


def _intro_flags(ordered: pd.DataFrame, intro: pd.DataFrame) -> pd.DataFrame:
    first = intro.loc[:, ["repo", "skill", "blob", "commit_date"]].rename(
        columns={"commit_date": "first_date"}
    )
    day = ordered.merge(first, on=["repo", "skill", "blob"], how="left")
    day = day.loc[day["commit_date"] == day["first_date"]]
    return (
        day.groupby(["repo", "skill", "blob"], sort=False)["has_ai_trailer"]
        .any()
        .rename("ai_trailer_at_introduction")
        .reset_index()
    )


def _assign_sequence(frame: pd.DataFrame) -> pd.DataFrame:
    ordered = frame.sort_values(
        ["repo", "skill", "first_date", "first_commit", "blob"], kind="mergesort"
    ).reset_index(drop=True)
    ordered["version_seq"] = ordered.groupby(["repo", "skill"], sort=False).cumcount() + 1
    ordered["lineage_id"] = ordered["repo"] + "::" + ordered["skill"]
    return ordered


def _flag_snapshot(frame: pd.DataFrame, snapshot: date) -> pd.DataFrame:
    out = frame.copy()
    out["post_snapshot"] = out["first_date"].astype(str) > snapshot.isoformat()
    return out


def _assign_renames(frame: pd.DataFrame) -> pd.DataFrame:
    """Same blob under two skill names in one repo keeps both rows and a shared id."""
    out = frame.copy()
    counts = out.groupby(["repo", "blob"], sort=False)["skill"].transform("nunique")
    shared = out["repo"] + "::" + out["blob"]
    out["renamed_group"] = shared.where(counts.gt(1), pd.NA)
    return out


def _finalize(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["size_bytes"] = pd.Series(pd.NA, index=out.index, dtype="Int64")
    out["desc_idiom"] = pd.Series("other", index=out.index, dtype="string")
    out["version_seq"] = out["version_seq"].astype("int64")
    out["n_commits"] = out["n_commits"].astype("int64")
    out["ai_trailer_at_introduction"] = out["ai_trailer_at_introduction"].astype(bool)
    out["post_snapshot"] = out["post_snapshot"].astype(bool)
    for name in _STRING_COLUMNS:
        out[name] = out[name].astype("string")
    out["release_label"] = out["release_label"].astype("string")
    out["renamed_group"] = out["renamed_group"].astype("string")
    ordered = out.loc[:, list(_COLUMNS)].sort_values(
        ["lineage_id", "version_seq", "blob"], kind="mergesort"
    )
    return ordered.reset_index(drop=True)


def _empty_versions() -> pd.DataFrame:
    frame = pd.DataFrame(columns=list(_COLUMNS))
    for name in _TEXT:
        frame[name] = pd.Series(dtype="string")
    frame["version_seq"] = pd.Series(dtype="int64")
    frame["n_commits"] = pd.Series(dtype="int64")
    frame["size_bytes"] = pd.Series(dtype="Int64")
    frame["ai_trailer_at_introduction"] = pd.Series(dtype="bool")
    frame["post_snapshot"] = pd.Series(dtype="bool")
    return frame.loc[:, list(_COLUMNS)]


def _attach_blob_facts(cfg: Config, frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    facts = _repo_facts(cfg, frame)
    out = frame.copy()
    sizes: list[int | None] = []
    idioms: list[str] = []
    repos = out["repo"].astype(str)
    blobs = out["blob"].astype(str)
    for repo, blob in zip(repos, blobs, strict=True):
        size, idiom = facts.get((repo, blob), (None, "other"))
        sizes.append(size)
        idioms.append(idiom)
    out["size_bytes"] = pd.array(sizes, dtype="Int64")
    out["desc_idiom"] = pd.Series(idioms, dtype="string")
    return out


def _repo_facts(cfg: Config, frame: pd.DataFrame) -> dict[tuple[str, str], tuple[int | None, str]]:
    index = _clone_index(cfg, load_sources(cfg))
    groups: dict[Path, set[str]] = {}
    clones: dict[str, Path | None] = {}
    for repo in sorted(frame["repo"].astype(str).unique()):
        clone = _clone_for(cfg, repo, index)
        clones[repo] = clone
        if clone is None:
            continue
        blobs = frame.loc[frame["repo"].astype(str) == repo, "blob"].astype(str)
        groups.setdefault(clone, set()).update(blobs.tolist())
    parsed = {clone: _read_blobs(clone, sorted(blobs)) for clone, blobs in groups.items()}
    return _pair_facts(frame, clones, parsed)


def _pair_facts(
    frame: pd.DataFrame,
    clones: dict[str, Path | None],
    parsed: dict[Path, dict[str, tuple[int | None, str]]],
) -> dict[tuple[str, str], tuple[int | None, str]]:
    facts: dict[tuple[str, str], tuple[int | None, str]] = {}
    for repo, clone in clones.items():
        known = parsed.get(clone, {}) if clone is not None else {}
        blobs = frame.loc[frame["repo"].astype(str) == repo, "blob"].astype(str).unique()
        for blob in blobs:
            facts[(repo, blob)] = known.get(str(blob), (None, "other"))
    return facts


def _clone_index(cfg: Config, sources: list[Source]) -> dict[str, Path | None]:
    by_repo = {source.repo: source for source in sources}
    index: dict[str, Path | None] = {}
    for source in sources:
        path = clone_dir(cfg, _origin(source, by_repo))
        if (path / ".git").is_dir():
            index[source.repo] = path
            continue
        log.warning("missing git clone %s for %s; size and idiom left empty", path, source.repo)
        index[source.repo] = None
    return index


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


def _clone_for(cfg: Config, repo: str, index: dict[str, Path | None]) -> Path | None:
    if repo in index:
        return index[repo]
    owner, sep, name = repo.partition("/")
    if not sep:
        log.warning("missing git clone for %s; size and idiom left empty", repo)
        return None
    path = resolve(cfg, cfg.paths.registry_repos) / f"{owner}__{name}"
    if (path / ".git").is_dir():
        return path
    log.warning("missing git clone %s for %s; size and idiom left empty", path, repo)
    return None


def _read_blobs(clone: Path, blobs: list[str]) -> dict[str, tuple[int | None, str]]:
    """Batched `git cat-file -s` plus blob text (the same bytes as `git show`)."""
    wanted = [blob for blob in blobs if _SHA.fullmatch(blob)]
    if not wanted:
        return {}
    missing = _missing_blobs(clone, wanted)
    if missing:
        log.info("fetching %s of %s blobs in %s", len(missing), len(wanted), clone.name)
        _fetch_blobs(clone, missing)
    else:
        log.info("%s blobs already local in %s", len(wanted), clone.name)
    try:
        stdout = _cat_file_batch(clone, wanted)
    except RuntimeError as exc:
        log.warning("%s", exc)
        return {blob: (None, "other") for blob in wanted}
    return _parse_batch(stdout, set(wanted))


def _missing_blobs(clone: Path, blobs: list[str]) -> list[str]:
    """Local check only. Lazy fetch would download one blob per request."""
    try:
        proc = _git(clone, ["cat-file", "--batch-check"], "\n".join(blobs) + "\n")
    except RuntimeError as exc:
        log.warning("%s", exc)
        return list(blobs)
    missing: list[str] = []
    for line in proc.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] == b"missing":
            missing.append(parts[0].decode("ascii", "replace"))
    return missing


def _fetch_blobs(clone: Path, blobs: list[str]) -> None:
    for start in range(0, len(blobs), _FETCH_CHUNK):
        _fetch_chunk(clone, blobs[start : start + _FETCH_CHUNK])


def _fetch_chunk(clone: Path, blobs: list[str]) -> None:
    try:
        proc = subprocess.run(
            [
                "git",
                "-c",
                "fetch.negotiationAlgorithm=noop",
                "fetch",
                "origin",
                "--no-tags",
                "--no-write-fetch-head",
                "--recurse-submodules=no",
                "--stdin",
            ],
            cwd=clone,
            input=("\n".join(blobs) + "\n").encode(),
            capture_output=True,
            check=False,
            env=_git_env(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"git is not on PATH; install git and run: {_RERUN}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[-500:]
        log.warning("blob fetch failed in %s: %s", clone, detail)


def _cat_file_batch(clone: Path, blobs: list[str]) -> bytes:
    return _git(clone, ["cat-file", "--batch"], "\n".join(blobs) + "\n")


def _git(clone: Path, args: list[str], payload: str) -> bytes:
    try:
        proc = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=clone,
            input=payload.encode(),
            capture_output=True,
            check=False,
            env=_git_env(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"git is not on PATH; install git and run: {_RERUN}") from exc
    if proc.returncode != 0 and not proc.stdout:
        detail = proc.stderr.decode("utf-8", "replace").strip()[-500:]
        raise RuntimeError(f"git {' '.join(args)} failed in {clone}: {detail}")
    if proc.returncode != 0:
        log.warning("git %s exited %s in %s", " ".join(args), proc.returncode, clone)
    return proc.stdout


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"
    return env


def _parse_batch(data: bytes, wanted: set[str]) -> dict[str, tuple[int | None, str]]:
    found: dict[str, tuple[int | None, str]] = {}
    index = 0
    length = len(data)
    while index < length:
        newline = data.find(b"\n", index)
        if newline < 0:
            break
        header = _parse_header(data[index:newline])
        index = newline + 1
        if header is None:
            break
        sha, size = header
        if size is None:
            found[sha] = (None, "other")
            continue
        content = data[index : index + size]
        index += size
        if index < length and data[index : index + 1] == b"\n":
            index += 1
        found[sha] = (size, description_idiom(_front_description(content)))
    for sha in wanted:
        found.setdefault(sha, (None, "other"))
    return found


def _parse_header(header: bytes) -> tuple[str, int | None] | None:
    parts = header.split(b" ")
    if len(parts) < 2:
        return None
    sha = parts[0].decode("ascii", "replace")
    if parts[1] == b"missing":
        return sha, None
    if len(parts) < 3:
        return None
    try:
        return sha, int(parts[2])
    except ValueError:
        return None


def _front_description(content: bytes) -> str | None:
    loaded, _body, ok = split_front_matter(content.decode("utf-8-sig", "replace"))
    if not ok:
        return None
    description = loaded.get("description")
    if isinstance(description, str):
        return description
    if description is None:
        return None
    return str(description)


def _output_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s1_registry_versions.parquet"


def _version_paths(cfg: Config) -> tuple[Path, Path]:
    derived = resolve(cfg, cfg.paths.derived)
    return derived / "s1_versions_raw.parquet", derived / "s1_commit_trailers.parquet"


def _mining_runs_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "mining_runs"


def _snapshot_auto(cfg: Config) -> bool:
    return cfg.rq2.snapshot_date.strip() == "auto"


def _inputs(cfg: Config) -> list[Path]:
    paths = list(_version_paths(cfg))
    if _snapshot_auto(cfg):
        paths.append(_mining_runs_dir(cfg))
    return paths


def _require_inputs(cfg: Config) -> None:
    raw, trailers = _version_paths(cfg)
    if not raw.is_file() or not trailers.is_file():
        missing = raw if not raw.is_file() else trailers
        raise FileNotFoundError(f"missing {missing}; run: {_MINE}")
    if not _snapshot_auto(cfg):
        return
    directory = _mining_runs_dir(cfg)
    if not directory.is_dir() or not any(directory.glob("*.parquet")):
        raise FileNotFoundError(f"missing {directory}; run: {_DOWNLOAD}")


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


def _read_table(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path)


def _write_parquet(frame: pd.DataFrame, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = [
        pa.array(_column_values(frame, field.name), type=field.type) for field in _SCHEMA
    ]
    table = pa.Table.from_arrays(arrays, schema=_SCHEMA)
    pq.write_table(table, path, compression="zstd")
    return int(table.num_rows)


def _column_values(frame: pd.DataFrame, name: str) -> list[object]:
    if frame.empty:
        return []
    values = frame[name].tolist()
    if name in _INTS:
        return [_int_cell(item, null_ok=_INTS[name]) for item in values]
    if name in {"ai_trailer_at_introduction", "post_snapshot"}:
        return [bool(item) for item in values]
    return [_text_cell(item) for item in values]


def _int_cell(value: object, *, null_ok: bool) -> int | None:
    if _missing(value):
        return None if null_ok else 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return int(str(value))


def _missing(value: object) -> bool:
    if value is None or value is pd.NA:
        return True
    return isinstance(value, float) and value != value


def _text_cell(value: object) -> str | None:
    if _missing(value):
        return None
    return str(value)


def _saved_rows(output: Path) -> dict[str, int]:
    payload = json.loads(manifest_path(output).read_text(encoding="utf-8"))
    return {"s1_registry_versions": int(payload["rows"])}


def _sql_string(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"
