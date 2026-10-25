"""
STAGE: package
MODULE: config
PURPOSE: Load config/default.yaml into a frozen, attribute-access Config.
INPUTS: config/default.yaml (or the file named by $SKILLSIG_CONFIG)
OUTPUTS: none
RQ LINK: none (infrastructure; thresholds feed RQ1 labels and RQ2 lag variables)
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from skillsig.paths import repo_root

ENV_VAR = "SKILLSIG_CONFIG"
DEFAULT_CONFIG = Path("config/default.yaml")


@dataclass(frozen=True)
class PathsConfig:
    raw: Path
    generated: Path
    registry_repos: Path
    derived: Path
    cache: Path
    results: Path
    figures: Path


@dataclass(frozen=True)
class ThresholdsConfig:
    bulk_batch_min: int
    templated_copies_min: int
    templated_jaccard_min: float
    insufficient_conf_floor: str


@dataclass(frozen=True)
class MinhashConfig:
    num_perm: int
    shingle_words: int
    body_max_chars: int
    lsh_threshold: float
    lsh_threshold_low: float


@dataclass(frozen=True)
class Rq2Config:
    substantive_body_change_pct: float
    grace_days: int
    grace_days_sensitivity: tuple[int, ...]
    snapshot_date: str


@dataclass(frozen=True)
class SampleConfig:
    per_label: int
    boundary_oversample: bool
    similarity_pairs: int


@dataclass(frozen=True)
class GithubConfig:
    user_agent: str
    per_page: int
    max_calls: int
    live_sample_repos: int
    checkpoint_every: int


@dataclass(frozen=True)
class Config:
    seed: int
    chunk_rows: int
    paths: PathsConfig
    thresholds: ThresholdsConfig
    minhash: MinhashConfig
    rq2: Rq2Config
    sample: SampleConfig
    github: GithubConfig
    root: Path = field(default_factory=repo_root, compare=False)
    source: Path | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """Config values only (no root/source), JSON-friendly; the basis of the config hash."""
        data = dataclasses.asdict(self)
        data.pop("root")
        data.pop("source")
        return _jsonable(data)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return value.as_posix()
    return value


def _section(cls: type, data: Any, name: str) -> Any:
    if not isinstance(data, dict):
        raise ValueError(f"config section {name!r} must be a mapping")
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - names)
    missing = sorted(names - set(data))
    if unknown or missing:
        raise ValueError(f"config section {name!r}: unknown keys {unknown}, missing keys {missing}")
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        v = data[f.name]
        if f.type in ("Path", Path):
            v = Path(v)
        elif isinstance(v, list):
            v = tuple(v)
        kwargs[f.name] = v
    return cls(**kwargs)


def _config_path(path: str | Path | None, root: Path) -> Path:
    chosen = Path(path) if path is not None else Path(os.environ.get(ENV_VAR) or DEFAULT_CONFIG)
    return chosen if chosen.is_absolute() else root / chosen


def load_config(path: str | Path | None = None) -> Config:
    """Load a Config. Precedence: `path`, then $SKILLSIG_CONFIG, then config/default.yaml."""
    root = repo_root()
    cfg_path = _config_path(path, root)
    if not cfg_path.is_file():
        raise FileNotFoundError(f"config file not found: {cfg_path}")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    sections = {
        "paths": PathsConfig,
        "thresholds": ThresholdsConfig,
        "minhash": MinhashConfig,
        "rq2": Rq2Config,
        "sample": SampleConfig,
        "github": GithubConfig,
    }
    top = {"seed", "chunk_rows", *sections}
    unknown = sorted(set(raw) - top)
    missing = sorted(top - set(raw))
    if unknown or missing:
        raise ValueError(f"{cfg_path}: unknown keys {unknown}, missing keys {missing}")
    return Config(
        seed=int(raw["seed"]),
        chunk_rows=int(raw["chunk_rows"]),
        **{name: _section(cls, raw[name], name) for name, cls in sections.items()},
        root=root,
        source=cfg_path,
    )
