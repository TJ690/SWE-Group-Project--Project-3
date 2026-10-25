from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from skillsig.config import Config, load_config

DEFAULT = Path(__file__).resolve().parents[1] / "config/default.yaml"


def test_default_thresholds(cfg: Config) -> None:
    assert cfg.thresholds.bulk_batch_min == 6
    assert cfg.thresholds.templated_copies_min == 10


def test_load_config_without_argument_uses_default() -> None:
    assert load_config().to_dict() == load_config(DEFAULT).to_dict()


def test_typed_sections(cfg: Config) -> None:
    assert cfg.seed == 20261015
    assert cfg.paths.derived == Path("src/data/derived")
    assert cfg.minhash.num_perm == 128
    assert cfg.rq2.grace_days_sensitivity == (0, 30, 90)
    assert cfg.sample.boundary_oversample is True


def test_config_is_frozen(cfg: Config) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.seed = 1  # type: ignore[misc]


def test_env_var_overrides_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    alt = tmp_path / "alt.yaml"
    alt.write_text(DEFAULT.read_text().replace("bulk_batch_min: 6", "bulk_batch_min: 9"))
    monkeypatch.setenv("SKILLSIG_CONFIG", str(alt))
    assert load_config().thresholds.bulk_batch_min == 9


def test_unknown_key_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(DEFAULT.read_text().replace("seed:", "sede:"))
    with pytest.raises(ValueError, match="sede"):
        load_config(bad)


def test_missing_file_named_in_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="nope.yaml"):
        load_config(tmp_path / "nope.yaml")
