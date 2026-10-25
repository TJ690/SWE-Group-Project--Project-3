from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from skillsig.config import Config, load_config
from skillsig.paths import repo_root

REPO_ROOT = repo_root(Path(__file__).parent)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    raw = REPO_ROOT / load_config(REPO_ROOT / "config/default.yaml").paths.raw
    if raw.is_dir():
        return
    skip = pytest.mark.skip(reason=f"dataset not found at {raw}; run: python -m skillsig data")
    for item in items:
        if "data" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolate_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKILLSIG_CONFIG", raising=False)


@pytest.fixture
def cfg() -> Config:
    return load_config(REPO_ROOT / "config/default.yaml")


@pytest.fixture
def tmp_cfg(cfg: Config, tmp_path: Path) -> Config:
    """Default config rooted at tmp_path, so relative paths and manifests stay in the sandbox."""
    return dataclasses.replace(cfg, root=tmp_path)
