"""
STAGE: s1_registry
MODULE: sources
PURPOSE: Load and validate the registry source list from config/registry_sources.yaml.
INPUTS: config/registry_sources.yaml
OUTPUTS: none (in-memory list of Source)
RQ LINK: RQ1 (templated copies from registry repos) and RQ2 (registries tracked for updates)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from skillsig.config import Config
from skillsig.paths import resolve

_TIERS = frozenset({"A", "B", "G"})
_STATUSES = frozenset({"mined", "candidate"})
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_FIELDS = frozenset({"repo", "tier", "group", "status", "mirror_of", "note"})
_REQUIRED = ("repo", "tier", "group", "status")
_SOURCES_FILE = Path("config/registry_sources.yaml")


@dataclass(frozen=True)
class Source:
    repo: str
    tier: str
    group: str
    status: str
    mirror_of: str | None
    note: str | None


def load_sources(cfg: Config, *, include_candidates: bool = False) -> list[Source]:
    """Return seed sources. Candidates are omitted unless include_candidates is set."""
    path = cfg.root / _SOURCES_FILE
    sources = [_parse(row, path, i) for i, row in enumerate(_read_rows(path), start=1)]
    _validate_mirrors(sources, path)
    allowed = _STATUSES if include_candidates else frozenset({"mined"})
    return [source for source in sources if source.status in allowed]


def clone_dir(cfg: Config, source: Source) -> Path:
    """Local clone path `<registry_repos>/<owner>__<repo>`. Does not create the directory."""
    if not _REPO_RE.fullmatch(source.repo):
        raise ValueError(f"repo {source.repo!r} must match owner/name")
    owner, name = source.repo.split("/", 1)
    return resolve(cfg, cfg.paths.registry_repos) / f"{owner}__{name}"


def _read_rows(path: Path) -> list[object]:
    if not path.is_file():
        raise FileNotFoundError(f"registry sources not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise ValueError(f"{path}: top level must be a mapping with a sources list")
    return list(raw["sources"])


def _parse(row: object, path: Path, index: int) -> Source:
    data = _mapping(row, path, index)
    repo = _repo_field(data["repo"], path, index, "repo")
    tier = _text(data["tier"], path, index, "tier")
    if tier not in _TIERS:
        raise ValueError(f"{path}: source {index} tier {tier!r} must be one of A, B, G")
    status = _text(data["status"], path, index, "status")
    if status not in _STATUSES:
        raise ValueError(f"{path}: source {index} status {status!r} must be mined or candidate")
    mirror = data.get("mirror_of")
    return Source(
        repo=repo,
        tier=tier,
        group=_text(data["group"], path, index, "group"),
        status=status,
        mirror_of=None if mirror is None else _repo_field(mirror, path, index, "mirror_of"),
        note=None if data.get("note") is None else _text(data["note"], path, index, "note"),
    )


def _mapping(row: object, path: Path, index: int) -> dict[str, object]:
    if not isinstance(row, dict):
        raise ValueError(f"{path}: source {index} must be a mapping")
    unknown = sorted(set(row) - _FIELDS)
    missing = [name for name in _REQUIRED if name not in row]
    if unknown or missing:
        raise ValueError(f"{path}: source {index} unknown keys {unknown}, missing keys {missing}")
    return row


def _validate_mirrors(sources: list[Source], path: Path) -> None:
    known = {source.repo for source in sources}
    if len(known) != len(sources):
        raise ValueError(f"{path}: duplicate repos")
    for source in sources:
        target = source.mirror_of
        if target is not None and (target == source.repo or target not in known):
            raise ValueError(
                f"{path}: {source.repo} mirror_of {target!r} must name another listed repo"
            )


def _repo_field(value: object, path: Path, index: int, field: str) -> str:
    text = _text(value, path, index, field)
    if not _REPO_RE.fullmatch(text):
        raise ValueError(f"{path}: source {index} {field} {text!r} must match owner/name")
    return text


def _text(value: object, path: Path, index: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: source {index} {field} must be a non-empty string")
    return value
