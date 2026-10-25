"""
STAGE: s1_registry
MODULE: changes
PURPOSE: Pair consecutive registry versions and record substantive or cosmetic changes.
INPUTS: src/data/derived/s1_registry_versions.parquet; blob text and bundle paths from clones
OUTPUTS: src/data/derived/s1_registry_changes.parquet
RQ LINK: RQ2 (version-to-version substantive change and introduced or removed signals)
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
from skillsig.s1_registry.change_classifier import ChangeMetrics, classify_change, diff_versions
from skillsig.s1_registry.sources import Source, clone_dir, load_sources

log = logging.getLogger(__name__)

_RERUN = "python -m skillsig registry --only changes"
_VERSIONS = "python -m skillsig registry --only versions"
_FETCH_CHUNK = 400
_SHA = re.compile(r"^[0-9a-f]{40,64}$")

_COLUMNS = (
    "lineage_id",
    "from_blob",
    "to_blob",
    "from_date",
    "to_date",
    "body_change_pct",
    "desc_similarity",
    "headings_added",
    "headings_removed",
    "files_delta",
    "change_type",
    "commands_added",
    "commands_removed",
    "network_added",
    "network_removed",
    "scripts_added",
    "scripts_removed",
    "steps_delta",
)
_NEEDED = (
    "lineage_id",
    "repo",
    "skill",
    "blob",
    "version_seq",
    "first_commit",
    "first_date",
)
_TEXT = ("lineage_id", "repo", "skill", "blob", "first_commit", "first_date")
_FLOATS = frozenset({"body_change_pct", "desc_similarity"})
_INTS = frozenset(
    {
        "headings_added",
        "headings_removed",
        "files_delta",
        "commands_added",
        "commands_removed",
        "network_added",
        "network_removed",
        "scripts_added",
        "scripts_removed",
        "steps_delta",
    }
)


def _arrow_type(name: str) -> pa.DataType:
    if name in _FLOATS:
        return pa.float64()
    if name in _INTS:
        return pa.int64()
    return pa.string()


_SCHEMA = pa.schema([(name, _arrow_type(name)) for name in _COLUMNS])

Tree = list[tuple[str, str]]


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write s1_registry_changes.parquet. Dev mode keeps every version pair."""
    output = _output_path(cfg)
    inputs = [_versions_path(cfg)]
    _require_versions(inputs[0])
    if not force and inputs_unchanged(output, cfg=cfg, inputs=inputs):
        log.info("registry changes up to date: %s", output)
        return StageResult(
            stage="s1_registry", outputs=[output], skipped=True, rows=_saved_rows(output)
        )
    if dev:
        log.info("registry changes keep every version pair; the dev sample is file-level")
    frame = _build(cfg, _read_table(inputs[0]))
    rows = _write_parquet(frame, output)
    write_manifest(output, cfg=cfg, inputs=inputs, rows=rows)
    log.info("wrote %s (%s rows)", output, rows)
    return StageResult(
        stage="s1_registry",
        outputs=[output],
        skipped=False,
        rows={"s1_registry_changes": rows},
    )


def _build(cfg: Config, versions: pd.DataFrame) -> pd.DataFrame:
    prepared = _prepare(versions)
    if prepared.empty:
        return _empty()
    sources = load_sources(cfg)
    by_repo = {source.repo: source for source in sources}
    rows: list[dict[str, object]] = []
    skipped = 0
    for repo, group in prepared.groupby("repo", sort=True):
        built, missed = _repo_changes(cfg, str(repo), group, by_repo)
        rows.extend(built)
        skipped += missed
    if skipped:
        log.warning("skipped %s version pairs with missing git objects", skipped)
    return _finish(rows)


def _repo_changes(
    cfg: Config, repo: str, frame: pd.DataFrame, by_repo: dict[str, Source]
) -> tuple[list[dict[str, object]], int]:
    pairs = _pairs(frame)
    clone = _clone_for(cfg, repo, by_repo)
    if clone is None or not pairs:
        return [], len(pairs) if clone is None else 0
    blobs = [item for pair in pairs for item in (pair["from_blob"], pair["to_blob"])]
    texts = _blob_texts(clone, blobs)
    commits = sorted(
        {pair["from_commit"] for pair in pairs} | {pair["to_commit"] for pair in pairs}
    )
    trees = {commit: _ls_tree(clone, commit) for commit in commits}
    rows: list[dict[str, object]] = []
    skipped = 0
    for pair in pairs:
        row = _measure(cfg, pair, texts, trees, repo)
        if row is None:
            skipped += 1
            continue
        rows.append(row)
    return rows, skipped


def _measure(
    cfg: Config,
    pair: dict[str, str],
    texts: dict[str, str | None],
    trees: dict[str, Tree | None],
    repo: str,
) -> dict[str, object] | None:
    old_text = texts.get(pair["from_blob"])
    new_text = texts.get(pair["to_blob"])
    old_files = _files_for(
        trees.get(pair["from_commit"]), pair["from_blob"], pair["from_skill"], repo
    )
    new_files = _files_for(trees.get(pair["to_commit"]), pair["to_blob"], pair["to_skill"], repo)
    if old_text is None or new_text is None or old_files is None or new_files is None:
        return None
    metrics = diff_versions(old_text, new_text, old_files, new_files)
    return _change_row(pair, metrics, classify_change(metrics, cfg))


def _change_row(
    pair: dict[str, str], metrics: ChangeMetrics, change_type: str
) -> dict[str, object]:
    return {
        "lineage_id": pair["lineage_id"],
        "from_blob": pair["from_blob"],
        "to_blob": pair["to_blob"],
        "from_date": pair["from_date"],
        "to_date": pair["to_date"],
        "body_change_pct": metrics.body_change_pct,
        "desc_similarity": metrics.desc_similarity,
        "headings_added": metrics.headings_added,
        "headings_removed": metrics.headings_removed,
        "files_delta": metrics.files_delta,
        "change_type": change_type,
        "commands_added": metrics.commands_added,
        "commands_removed": metrics.commands_removed,
        "network_added": metrics.network_added,
        "network_removed": metrics.network_removed,
        "scripts_added": metrics.scripts_added,
        "scripts_removed": metrics.scripts_removed,
        "steps_delta": metrics.steps_delta,
    }


def _pairs(frame: pd.DataFrame) -> list[dict[str, str]]:
    ordered = frame.sort_values(
        ["lineage_id", "version_seq", "first_date", "first_commit", "blob"],
        kind="mergesort",
    )
    pairs: list[dict[str, str]] = []
    for lineage_id, group in ordered.groupby("lineage_id", sort=True):
        rows = group.to_dict("records")
        for old, new in zip(rows, rows[1:], strict=False):
            pair = _pair(str(lineage_id), old, new)
            if pair is not None:
                pairs.append(pair)
    return pairs


def _pair(lineage_id: str, old: dict[str, object], new: dict[str, object]) -> dict[str, str] | None:
    from_blob, to_blob = str(old["blob"]), str(new["blob"])
    if from_blob == to_blob:
        return None
    return {
        "lineage_id": lineage_id,
        "from_skill": str(old["skill"]),
        "to_skill": str(new["skill"]),
        "from_blob": from_blob,
        "to_blob": to_blob,
        "from_date": str(old["first_date"]),
        "to_date": str(new["first_date"]),
        "from_commit": str(old["first_commit"]),
        "to_commit": str(new["first_commit"]),
    }


def _files_for(entries: Tree | None, blob: str, skill: str, repo: str) -> list[str] | None:
    if entries is None:
        return None
    return _files_under(entries, _skill_directory(entries, blob, skill, repo))


def _skill_directory(entries: Tree, blob: str, skill: str, repo: str) -> str | None:
    paths = [path for sha, path in entries if sha == blob]
    skill_paths = [path for path in paths if PurePosixPath(path).name.lower() == "skill.md"]
    chosen = _prefer_skill(skill_paths or paths, skill, repo)
    if chosen is None:
        return None
    parent = PurePosixPath(chosen).parent
    return "." if str(parent) == "." else parent.as_posix()


def _prefer_skill(paths: list[str], skill: str, repo: str) -> str | None:
    if not paths:
        return None
    repo_name = repo.rsplit("/", 1)[-1]

    def rank(path: str) -> tuple[int, str]:
        parent = PurePosixPath(path).parent
        name = repo_name if str(parent) == "." else parent.name
        return (0 if name == skill else 1, path)

    return sorted(paths, key=rank)[0]


def _files_under(entries: Tree, skill_dir: str | None) -> list[str] | None:
    if skill_dir is None:
        return None
    if skill_dir == ".":
        return [path for _sha, path in entries]
    prefix = skill_dir.rstrip("/") + "/"
    return [path for _sha, path in entries if path.startswith(prefix)]


def _prepare(frame: pd.DataFrame) -> pd.DataFrame:
    missing = [name for name in _NEEDED if name not in frame.columns]
    if missing:
        raise ValueError(f"registry versions missing columns: {missing}")
    out = frame.loc[:, list(_NEEDED)].copy()
    for name in _TEXT:
        out[name] = out[name].map(_cell_text)
    out["blob"] = out["blob"].str.lower()
    out["first_commit"] = out["first_commit"].str.lower()
    out["version_seq"] = out["version_seq"].map(_cell_int)
    mask = out["version_seq"].notna()
    for name in _TEXT:
        mask &= out[name].ne("")
    dropped = int((~mask).sum())
    if dropped:
        log.warning("dropped %s version rows with empty fields", dropped)
    ready = out.loc[mask].copy()
    ready["version_seq"] = ready["version_seq"].astype(int)
    return ready.reset_index(drop=True)


def _clone_for(cfg: Config, repo: str, by_repo: dict[str, Source]) -> Path | None:
    source = by_repo.get(repo)
    if source is None:
        path = _fallback_clone(cfg, repo)
    else:
        path = clone_dir(cfg, _origin(source, by_repo))
    if path is not None and (path / ".git").is_dir():
        return path
    log.warning("missing git clone %s for %s; version pairs skipped", path, repo)
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


def _blob_texts(clone: Path, blobs: list[str]) -> dict[str, str | None]:
    """Blob bytes from `git cat-file --batch` (same payload as `git cat-file -p`)."""
    wanted = list(dict.fromkeys(blob for blob in blobs if _SHA.fullmatch(blob)))
    if not wanted:
        return {}
    _ensure_blobs(clone, wanted)
    try:
        data = _git(clone, ["cat-file", "--batch"], ("\n".join(wanted) + "\n").encode())
    except RuntimeError as exc:
        log.warning("%s", exc)
        return dict.fromkeys(wanted)
    return _parse_texts(data, wanted)


def _ensure_blobs(clone: Path, blobs: list[str]) -> None:
    missing = _missing_blobs(clone, blobs)
    if not missing:
        return
    log.info("fetching %s of %s blobs in %s", len(missing), len(blobs), clone.name)
    for start in range(0, len(missing), _FETCH_CHUNK):
        _fetch_chunk(clone, missing[start : start + _FETCH_CHUNK])


def _missing_blobs(clone: Path, blobs: list[str]) -> list[str]:
    try:
        data = _git(clone, ["cat-file", "--batch-check"], ("\n".join(blobs) + "\n").encode())
    except RuntimeError as exc:
        log.warning("%s", exc)
        return list(blobs)
    missing: list[str] = []
    for line in data.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] == b"missing":
            missing.append(parts[0].decode("ascii", "replace"))
    return missing


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


def _ls_tree(clone: Path, commit: str) -> Tree | None:
    """Paths in `commit`, equivalent to `git ls-tree -r --name-only` plus blob ids."""
    if not _SHA.fullmatch(commit):
        return None
    try:
        data = _git(clone, ["ls-tree", "-r", "-z", commit])
    except RuntimeError as exc:
        log.warning("%s", exc)
        return None
    return _parse_tree(data)


def _parse_tree(data: bytes) -> Tree:
    rows: Tree = []
    for record in data.split(b"\0"):
        if not record:
            continue
        meta, sep, path = record.partition(b"\t")
        if not sep:
            continue
        parts = meta.split()
        if len(parts) < 3:
            continue
        rows.append((parts[2].decode("ascii", "replace"), path.decode("utf-8", "replace")))
    return rows


def _parse_texts(data: bytes, wanted: list[str]) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    index = 0
    length = len(data)
    while index < length:
        parsed = _take_object(data, index, length)
        if parsed is None:
            break
        sha, text, index = parsed
        found[sha] = text
    for sha in wanted:
        found.setdefault(sha, None)
    return found


def _take_object(data: bytes, index: int, length: int) -> tuple[str, str | None, int] | None:
    newline = data.find(b"\n", index)
    if newline < 0:
        return None
    parts = data[index:newline].split(b" ")
    index = newline + 1
    if len(parts) < 2:
        return None
    sha = parts[0].decode("ascii", "replace")
    if parts[1] == b"missing" or len(parts) < 3:
        return sha, None, index
    try:
        size = int(parts[2])
    except ValueError:
        return None
    content = data[index : index + size]
    index += size
    if index < length and data[index : index + 1] == b"\n":
        index += 1
    text = content.decode("utf-8-sig", "replace") if parts[1] == b"blob" else None
    return sha, text, index


def _git(clone: Path, args: list[str], payload: bytes | None = None) -> bytes:
    try:
        proc = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=clone,
            input=payload,
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


def _finish(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=list(_COLUMNS))
    if frame.empty:
        return frame
    ordered = frame.sort_values(
        ["lineage_id", "from_date", "to_date", "from_blob", "to_blob"],
        kind="mergesort",
    )
    return ordered.reset_index(drop=True)


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_COLUMNS))


def _output_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s1_registry_changes.parquet"


def _versions_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s1_registry_versions.parquet"


def _require_versions(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"missing {path}; run: {_VERSIONS}")


def _read_table(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path)


def _write_parquet(frame: pd.DataFrame, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = [pa.array(_column_values(frame, field.name), type=field.type) for field in _SCHEMA]
    table = pa.Table.from_arrays(arrays, schema=_SCHEMA)
    pq.write_table(table, path, compression="zstd")
    return int(table.num_rows)


def _column_values(frame: pd.DataFrame, name: str) -> list[object]:
    if frame.empty:
        return []
    values = frame[name].tolist()
    if name in _FLOATS:
        return [float(item) for item in values]
    if name in _INTS:
        return [int(item) for item in values]
    return [str(item) for item in values]


def _cell_text(value: object) -> str:
    if _missing(value):
        return ""
    return str(value).strip()


def _cell_int(value: object) -> int | None:
    if _missing(value) or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _missing(value: object) -> bool:
    if value is None or value is pd.NA:
        return True
    return isinstance(value, float) and value != value


def _saved_rows(output: Path) -> dict[str, int]:
    payload = json.loads(manifest_path(output).read_text(encoding="utf-8"))
    return {"s1_registry_changes": int(payload["rows"])}
