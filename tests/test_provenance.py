from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from skillsig.config import Config
from skillsig.provenance import inputs_unchanged, manifest_path, write_manifest


def _setup(root: Path) -> tuple[Path, Path]:
    src = root / "input.txt"
    src.write_bytes(b"hello")
    out = root / "s2_features_test.parquet"
    out.write_bytes(b"output")
    return src, out


def test_manifest_contents(tmp_cfg: Config) -> None:
    src, out = _setup(tmp_cfg.root)
    path = write_manifest(out, cfg=tmp_cfg, inputs=[src], rows=7)
    assert path == manifest_path(out) == tmp_cfg.root / "s2_features_test.parquet.manifest.json"
    data = json.loads(path.read_text())
    assert data["rows"] == 7
    assert data["inputs"]["input.txt"].startswith("sha256:")
    assert data["config_hash"].startswith("sha256:")
    assert {"git_commit", "created_utc", "output"} <= data.keys()


def test_unchanged_until_input_byte_changes(tmp_cfg: Config) -> None:
    src, out = _setup(tmp_cfg.root)
    write_manifest(out, cfg=tmp_cfg, inputs=[src], rows=1)
    assert inputs_unchanged(out, cfg=tmp_cfg, inputs=[src])
    src.write_bytes(b"hellp")
    assert not inputs_unchanged(out, cfg=tmp_cfg, inputs=[src])


def test_config_change_invalidates(tmp_cfg: Config) -> None:
    src, out = _setup(tmp_cfg.root)
    write_manifest(out, cfg=tmp_cfg, inputs=[src], rows=1)
    other = dataclasses.replace(tmp_cfg, seed=tmp_cfg.seed + 1)
    assert not inputs_unchanged(out, cfg=other, inputs=[src])


def test_missing_output_or_manifest_is_stale(tmp_cfg: Config) -> None:
    src, out = _setup(tmp_cfg.root)
    assert not inputs_unchanged(out, cfg=tmp_cfg, inputs=[src])
    write_manifest(out, cfg=tmp_cfg, inputs=[src], rows=1)
    out.unlink()
    assert not inputs_unchanged(out, cfg=tmp_cfg, inputs=[src])


def test_directory_input_tracks_nested_files(tmp_cfg: Config) -> None:
    d = tmp_cfg.root / "raw"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "a.parquet").write_bytes(b"a")
    _, out = _setup(tmp_cfg.root)
    write_manifest(out, cfg=tmp_cfg, inputs=[d], rows=1)
    assert inputs_unchanged(out, cfg=tmp_cfg, inputs=[d])
    (d / "sub" / "a.parquet").write_bytes(b"b")
    assert not inputs_unchanged(out, cfg=tmp_cfg, inputs=[d])
