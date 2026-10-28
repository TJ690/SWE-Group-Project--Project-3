"""
STAGE: s1_registry
MODULE: mine_git
PURPOSE: Mine SKILL.md versions and AI co-author trailers from registry git clones.
INPUTS: config/registry_sources.yaml, local clones under registry_repos
OUTPUTS: src/data/derived/s1_versions_raw.parquet, src/data/derived/s1_commit_trailers.parquet
RQ LINK: RQ1 (registry version history) and RQ2 (commit trailers and release timing)
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s1_registry.sources import Source, clone_dir, load_sources
from skillsig.s2_features.commit import AI_TOOLS, ai_tools_in_message

log = logging.getLogger(__name__)

_RERUN = "python -m skillsig registry --only mine"
_SOURCES = Path("config/registry_sources.yaml")

_VERSION_COLUMNS = [
    "repo",
    "commit",
    "commit_date",
    "subject",
    "path",
    "skill",
    "blob",
    "status",
    "release_label",
]
_TRAILER_COLUMNS = ["repo", "commit", "ai_tools", "has_ai_trailer"]

_VERSION_SCHEMA = pa.schema(
    [(name, pa.string()) for name in _VERSION_COLUMNS]
)
_TRAILER_SCHEMA = pa.schema(
    [
        ("repo", pa.string()),
        ("commit", pa.string()),
        ("ai_tools", pa.list_(pa.string())),
        ("has_ai_trailer", pa.bool_()),
    ]
)

_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_ZERO = re.compile(r"^0+$")
_COMMIT = re.compile(r"^@@([0-9a-f]{40,64})\|(\d{4}-\d{2}-\d{2})\|(.*)$")
_RAW = re.compile(r"^:[0-7]+ [0-7]+ [0-9a-f]+ ([0-9a-f]+) ([A-Z][0-9]*)\t(.*)$")
_RELEASE = re.compile(r"Release v?(\d+\.\d+\.\d+)")
_OCTAL = "01234567"
_SIMPLE_ESCAPES = {
    "a": b"\a",
    "b": b"\b",
    "f": b"\f",
    "n": b"\n",
    "r": b"\r",
    "t": b"\t",
    "v": b"\v",
    "\\": b"\\",
    '"': b'"',
}

_VERSION_LOG = (
    "log",
    "--no-renames",
    "--raw",
    "--no-abbrev",
    "--format=@@%H|%ad|%s",
    "--date=short",
    "--",
    "*SKILL.md",
)
_TRAILER_LOG = (
    "log",
    "--format=%H%x09%(trailers:key=Co-authored-by,valueonly,separator=%x3B)",
)


def clone_all(cfg: Config, sources: list[Source]) -> list[Path]:
    """Clone non-mirror sources (`--filter=blob:none --no-checkout`); fetch if present."""
    paths: list[Path] = []
    for source in sources:
        if source.mirror_of:
            log.info("skip clone of mirror %s (mirror_of %s)", source.repo, source.mirror_of)
            continue
        dest = clone_dir(cfg, source)
        _ensure_clone(dest, _remote_url(source), source.repo)
        paths.append(dest)
    return paths


def mine_repo(clone: Path, repo: str) -> pd.DataFrame:
    """One row per (commit, SKILL.md path) that still exists after the commit."""
    _require_clone(clone)
    return _versions_frame(_version_rows(_git(clone, *_VERSION_LOG), repo))


def mine_trailers(clone: Path, repo: str) -> pd.DataFrame:
    """One row per commit, with AI tools named on Co-authored-by trailers."""
    _require_clone(clone)
    return _trailers_frame(_trailer_rows(_git(clone, *_TRAILER_LOG), repo))


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write version and trailer parquets. Dev mode keeps every mined source."""
    sources = load_sources(cfg)
    outputs = _output_paths(cfg)
    inputs = _inputs(cfg)
    if not force and _current(cfg, outputs, inputs):
        log.info("git history mine up to date: %s", outputs[0])
        return StageResult(
            stage="s1_registry", outputs=outputs, skipped=True, rows=_saved_rows(outputs)
        )
    if dev:
        log.info("registry git mining keeps every mined source; the dev sample is file-level")
    versions, trailers = _collect(cfg, sources)
    counts = (
        _write_parquet(versions, outputs[0], _VERSION_SCHEMA),
        _write_parquet(trailers, outputs[1], _TRAILER_SCHEMA),
    )
    for path, count in zip(outputs, counts, strict=True):
        write_manifest(path, cfg=cfg, inputs=inputs, rows=count)
    log.info("wrote %s (%s rows) and %s (%s rows)", outputs[0], counts[0], outputs[1], counts[1])
    return StageResult(
        stage="s1_registry",
        outputs=outputs,
        skipped=False,
        rows={"s1_versions_raw": counts[0], "s1_commit_trailers": counts[1]},
    )


def _remote_url(source: Source) -> str:
    return f"https://github.com/{source.repo}.git"


def _ensure_clone(dest: Path, url: str, repo: str) -> None:
    if (dest / ".git").exists():
        if os.environ.get("SKILLSIG_NO_FETCH") == "1":
            log.info("keep existing clone of %s (SKILLSIG_NO_FETCH=1)", repo)
            return
        log.info("fetch %s", repo)
        _fetch(dest)
        return
    if dest.exists():
        raise FileNotFoundError(
            f"clone path exists and is not a git repo: {dest}; remove it and run: {_RERUN}"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("clone %s", repo)
    _git(dest.parent, "clone", "--filter=blob:none", "--no-checkout", url, str(dest))


def _fetch(dest: Path) -> None:
    """Fetch, then move the local branch to origin/HEAD so `git log` sees new commits."""
    _git(dest, "fetch", "--filter=blob:none", "--prune", "origin")
    _git(dest, "remote", "set-head", "origin", "-a")
    branch = _git(dest, "rev-parse", "--abbrev-ref", "HEAD").strip()
    ref = "HEAD" if branch == "HEAD" else f"refs/heads/{branch}"
    _git(dest, "update-ref", ref, "origin/HEAD")


def _require_clone(clone: Path) -> None:
    if not (clone / ".git").exists():
        raise FileNotFoundError(f"missing git clone {clone}; run: {_RERUN}")


def _version_rows(text: str, repo: str) -> list[dict[str, str | None]]:
    rows: list[dict[str, str | None]] = []
    current: tuple[str, str, str] | None = None
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("@@"):
            current = _parse_commit(line)
            continue
        if current is None or not line.startswith(":"):
            continue
        parsed = _parse_raw(line)
        if parsed is None:
            continue
        status, blob, path = parsed
        if status.startswith("D") or _ZERO.fullmatch(blob):
            continue
        commit, day, subject = current
        rows.append(_version_row(repo, commit, day, subject, path, blob, status))
    return rows


def _version_row(
    repo: str, commit: str, day: str, subject: str, path: str, blob: str, status: str
) -> dict[str, str | None]:
    return {
        "repo": repo,
        "commit": commit,
        "commit_date": day,
        "subject": subject,
        "path": path,
        "skill": _skill_name(path, repo),
        "blob": blob,
        "status": status,
        "release_label": _release_label(subject),
    }


def _parse_commit(line: str) -> tuple[str, str, str] | None:
    match = _COMMIT.match(line)
    if match is None:
        log.warning("skipping unparsed git log header: %s", line[:120])
        return None
    return match.group(1), match.group(2), match.group(3)


def _parse_raw(line: str) -> tuple[str, str, str] | None:
    match = _RAW.match(line)
    if match is None:
        log.warning("skipping unparsed raw line: %s", line[:160])
        return None
    blob, status, path = match.group(1), match.group(2), match.group(3)
    return status, blob, _unquote_path(path)


def _release_label(subject: str) -> str | None:
    match = _RELEASE.search(subject)
    return match.group(1) if match else None


def _skill_name(path: str, repo: str) -> str:
    parent = PurePosixPath(path).parent
    if str(parent) == ".":
        return repo.rsplit("/", 1)[-1]
    return parent.name


def _unquote_path(path: str) -> str:
    """Decode a git C-quoted path. Unquoted paths are returned unchanged."""
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        return path
    raw = path[1:-1]
    data = bytearray()
    index = 0
    while index < len(raw):
        if raw[index] != "\\":
            data.extend(raw[index].encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(raw):
            break
        data.extend(_escaped_byte(raw, index))
        index = _escape_end(raw, index)
    return bytes(data).decode("utf-8", "replace")


def _escaped_byte(raw: str, index: int) -> bytes:
    char = raw[index]
    if char in _SIMPLE_ESCAPES:
        return _SIMPLE_ESCAPES[char]
    if char in _OCTAL:
        end = _octal_end(raw, index)
        return bytes([int(raw[index:end], 8)])
    return char.encode("utf-8")


def _escape_end(raw: str, index: int) -> int:
    char = raw[index]
    if char in _SIMPLE_ESCAPES:
        return index + 1
    if char in _OCTAL:
        return _octal_end(raw, index)
    return index + 1


def _octal_end(raw: str, index: int) -> int:
    end = index
    while end < len(raw) and end < index + 3 and raw[end] in _OCTAL:
        end += 1
    return end


def _trailer_rows(text: str, repo: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line:
            continue
        sha, sep, raw = line.partition("\t")
        if sep != "\t" or _SHA.fullmatch(sha) is None:
            log.warning("skipping unparsed trailer line: %s", line[:160])
            continue
        tools = _ai_tools(raw)
        rows.append(
            {
                "repo": repo,
                "commit": sha,
                "ai_tools": tools,
                "has_ai_trailer": bool(tools),
            }
        )
    return rows


def _ai_tools(raw: str) -> list[str]:
    """Rebuild Co-authored-by lines so T03's trailer parser can see the tool names."""
    message = "\n".join(
        f"Co-authored-by: {part.strip()}" for part in raw.split(";") if part.strip()
    )
    found = ai_tools_in_message(message)
    return [name for name in AI_TOOLS if name in found]


def _versions_frame(rows: list[dict[str, str | None]]) -> pd.DataFrame:
    frame = pd.DataFrame.from_records(rows, columns=_VERSION_COLUMNS)
    for column in _VERSION_COLUMNS:
        frame[column] = frame[column].astype("string")
    return _sorted(frame, ["repo", "commit_date", "commit", "path"])


def _trailers_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "repo": pd.Series([str(row["repo"]) for row in rows], dtype="string"),
            "commit": pd.Series([str(row["commit"]) for row in rows], dtype="string"),
            "ai_tools": pd.Series(_tool_lists(rows), dtype="object"),
            "has_ai_trailer": pd.Series(
                [bool(row["has_ai_trailer"]) for row in rows], dtype="bool"
            ),
        }
    )
    return _sorted(frame, ["repo", "commit"])


def _sorted(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if frame.empty:
        return frame.reset_index(drop=True)
    return frame.sort_values(columns, kind="mergesort").reset_index(drop=True)


def _collect(cfg: Config, sources: list[Source]) -> tuple[pd.DataFrame, pd.DataFrame]:
    paths = clone_all(cfg, sources)
    origins = [source for source in sources if source.mirror_of is None]
    by_repo = dict(zip((source.repo for source in origins), paths, strict=True))
    mined_v, mined_t = _mine_origins(by_repo)
    version_parts = [mined_v[source.repo] for source in origins]
    trailer_parts = [mined_t[source.repo] for source in origins]
    for source in sources:
        if source.mirror_of is None:
            continue
        version_parts.append(_retarget(_lookup(mined_v, source), source.repo))
        trailer_parts.append(_retarget(_lookup(mined_t, source), source.repo))
    versions = _concat(
        version_parts, _versions_frame([]), ["repo", "commit_date", "commit", "path"]
    )
    trailers = _concat(trailer_parts, _trailers_frame([]), ["repo", "commit"])
    return versions, trailers


def _mine_origins(
    by_repo: dict[str, Path],
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    versions: dict[str, pd.DataFrame] = {}
    trailers: dict[str, pd.DataFrame] = {}
    for repo, clone in by_repo.items():
        log.info("mining %s", repo)
        versions[repo] = mine_repo(clone, repo)
        trailers[repo] = mine_trailers(clone, repo)
        log.info(
            "mined %s versions=%s trailers=%s", repo, len(versions[repo]), len(trailers[repo])
        )
    return versions, trailers


def _lookup(frames: dict[str, pd.DataFrame], source: Source) -> pd.DataFrame:
    origin = source.mirror_of or ""
    try:
        return frames[origin]
    except KeyError as exc:
        raise FileNotFoundError(
            f"mirror {source.repo} needs rows from {origin}, which was not mined; run: {_RERUN}"
        ) from exc


def _retarget(frame: pd.DataFrame, repo: str) -> pd.DataFrame:
    copied = frame.copy()
    copied["repo"] = repo
    return copied


def _concat(parts: list[pd.DataFrame], empty: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    frame = pd.concat(parts, ignore_index=True) if parts else empty
    if frame.empty:
        return empty
    return _sorted(frame, columns)


def _output_paths(cfg: Config) -> list[Path]:
    derived = resolve(cfg, cfg.paths.derived)
    return [derived / "s1_versions_raw.parquet", derived / "s1_commit_trailers.parquet"]


def _inputs(cfg: Config) -> list[Path]:
    return [cfg.root / _SOURCES]


def _current(cfg: Config, outputs: list[Path], inputs: list[Path]) -> bool:
    return all(inputs_unchanged(path, cfg=cfg, inputs=inputs) for path in outputs)


def _saved_rows(outputs: list[Path]) -> dict[str, int]:
    rows: dict[str, int] = {}
    for path in outputs:
        payload = json.loads(manifest_path(path).read_text(encoding="utf-8"))
        rows[path.stem] = int(payload["rows"])
    return rows


def _write_parquet(frame: pd.DataFrame, path: Path, schema: pa.Schema) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = [pa.array(_column_values(frame, field.name), type=field.type) for field in schema]
    table = pa.Table.from_arrays(arrays, schema=schema)
    pq.write_table(table, path, compression="zstd")
    return int(table.num_rows)


def _column_values(frame: pd.DataFrame, name: str) -> list[object]:
    if frame.empty:
        return []
    values = frame[name].tolist()
    if name == "ai_tools":
        return [list(item) for item in values]
    if name == "has_ai_trailer":
        return [bool(item) for item in values]
    return [_text_cell(item) for item in values]


def _text_cell(value: object) -> str | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and value != value:
        return None
    return str(value)


def _tool_lists(rows: list[dict[str, object]]) -> list[list[str]]:
    lists: list[list[str]] = []
    for row in rows:
        tools = row["ai_tools"]
        if isinstance(tools, list):
            lists.append([str(item) for item in tools])
        else:
            lists.append([])
    return lists


def _git(cwd: Path, *args: str) -> str:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    try:
        proc = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"git is not on PATH; install git and run: {_RERUN}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        if len(detail) > 500:
            detail = detail[-500:]
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {detail}") from exc
    return proc.stdout
