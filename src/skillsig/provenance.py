"""
STAGE: package
MODULE: provenance
PURPOSE: Write `<output>.manifest.json` files and decide whether a stage output is up to date.
INPUTS: stage input files/directories, Config
OUTPUTS: <output>.manifest.json next to each stage output
RQ LINK: none (reproducibility of all RQ1/RQ2 outputs)
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skillsig.config import Config

log = logging.getLogger(__name__)

LARGE_FILE_BYTES = 1 << 30
_CHUNK = 1 << 20


@dataclass
class StageResult:
    stage: str
    outputs: list[Path] = field(default_factory=list)
    skipped: bool = False
    rows: dict[str, int] = field(default_factory=dict)


def manifest_path(output: Path) -> Path:
    return output.with_name(output.name + ".manifest.json")


def config_hash(cfg: Config) -> str:
    payload = json.dumps(cfg.to_dict(), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_fingerprint(path: Path) -> str:
    stat = path.stat()
    if stat.st_size > LARGE_FILE_BYTES:
        return f"size_mtime:{stat.st_size}:{stat.st_mtime_ns}"
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def fingerprint(path: Path) -> str:
    """sha256 of a file (size+mtime above 1 GB); for a directory, a hash over its files."""
    if path.is_file():
        return _file_fingerprint(path)
    if path.is_dir():
        digest = hashlib.sha256()
        for child in sorted(p for p in path.rglob("*") if p.is_file()):
            rel = child.relative_to(path).as_posix()
            digest.update(f"{rel}\0{_file_fingerprint(child)}\n".encode())
        return "dir_sha256:" + digest.hexdigest()
    raise FileNotFoundError(f"input not found: {path}")


def _display_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _input_fingerprints(cfg: Config, inputs: list[Path]) -> dict[str, str]:
    return {_display_path(p, cfg.root): fingerprint(p) for p in inputs}


def git_commit(root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def write_manifest(output: Path, *, cfg: Config, inputs: list[Path], rows: int) -> Path:
    manifest: dict[str, Any] = {
        "output": _display_path(output, cfg.root),
        "inputs": _input_fingerprints(cfg, inputs),
        "config_hash": config_hash(cfg),
        "git_commit": git_commit(cfg.root),
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "rows": int(rows),
    }
    target = manifest_path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log.debug("wrote manifest %s", target)
    return target


def inputs_unchanged(output: Path, *, cfg: Config, inputs: list[Path]) -> bool:
    """True when the output and its manifest exist and inputs + config hash match the manifest."""
    target = manifest_path(output)
    if not output.exists() or not target.is_file():
        return False
    try:
        manifest = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("unreadable manifest %s; treating output as stale", target)
        return False
    if manifest.get("config_hash") != config_hash(cfg):
        return False
    try:
        current = _input_fingerprints(cfg, inputs)
    except FileNotFoundError:
        return False
    return manifest.get("inputs") == current
