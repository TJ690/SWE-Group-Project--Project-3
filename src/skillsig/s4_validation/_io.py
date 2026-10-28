"""
STAGE: s4_validation
MODULE: _io
PURPOSE: Shared file helpers for the evaluation checks (paths, SQL quoting, manifest-aware writes).
INPUTS: none
OUTPUTS: none (helpers write what callers pass)
RQ LINK: none (infrastructure)
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import inputs_unchanged, manifest_path, write_manifest


def derived(cfg: Config, name: str) -> Path:
    return resolve(cfg, cfg.paths.derived) / name


def results(cfg: Config, name: str) -> Path:
    return resolve(cfg, cfg.paths.results) / name


def figures(cfg: Config, name: str) -> Path:
    return resolve(cfg, cfg.paths.figures) / name


def annotation(cfg: Config, name: str) -> Path:
    return resolve(cfg, cfg.paths.results) / "annotation" / name


def artifacts_glob(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "artifacts" / "*.parquet"


def artifacts_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "artifacts"


def sql(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def require(paths: list[tuple[Path, str]]) -> None:
    """Raise FileNotFoundError naming the command that builds the first missing input."""
    for path, command in paths:
        exists = (
            path.is_dir() and any(path.glob("*.parquet")) if path.suffix == "" else path.is_file()
        )
        if not exists:
            raise FileNotFoundError(f"missing {path}; run: {command}")


def up_to_date(outputs: list[Path], cfg: Config, inputs: list[Path]) -> bool:
    return all(inputs_unchanged(p, cfg=cfg, inputs=inputs) for p in outputs)


def saved_rows(outputs: list[Path]) -> dict[str, int]:
    return {
        p.name: int(json.loads(manifest_path(p).read_text(encoding="utf-8"))["rows"])
        for p in outputs
    }


def write_csv(frame: pd.DataFrame, path: Path, cfg: Config, inputs: list[Path]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    write_manifest(path, cfg=cfg, inputs=inputs, rows=len(frame))
    return len(frame)


def write_json(payload: dict[str, Any], path: Path, cfg: Config, inputs: list[Path]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_clean(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_manifest(path, cfg=cfg, inputs=inputs, rows=1)
    return 1


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):
        return _clean(value.item())
    return value
