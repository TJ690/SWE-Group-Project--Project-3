from __future__ import annotations

from pathlib import Path

import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s1_registry.sources import Source, clone_dir, load_sources


def test_seed_loads_31_mined_sources(cfg: Config) -> None:
    sources = load_sources(cfg)
    assert len(sources) == 31
    assert {s.status for s in sources} == {"mined"}
    assert "openai/plugins" not in {s.repo for s in sources}
    mirror = next(s for s in sources if s.repo == "flutter/agent-plugins")
    assert mirror.mirror_of == "flutter/skills"
    assert mirror.tier == "G"


def test_include_candidates_keeps_mined_and_candidate(cfg: Config) -> None:
    sources = load_sources(cfg, include_candidates=True)
    assert len(sources) == 32
    candidate = next(s for s in sources if s.repo == "openai/plugins")
    assert candidate.status == "candidate"
    noted = next(s for s in sources if s.repo == "openai/skills")
    assert noted.note is not None
    assert "deprecated" in noted.note


def test_clone_dir_uses_owner_double_underscore_repo(cfg: Config) -> None:
    source = next(s for s in load_sources(cfg) if s.repo == "anthropics/skills")
    expected = resolve(cfg, cfg.paths.registry_repos) / "anthropics__skills"
    assert clone_dir(cfg, source) == expected


def test_bad_tier_raises(tmp_cfg: Config) -> None:
    _write_sources(
        tmp_cfg.root,
        "sources:\n  - {repo: acme/skills, tier: C, group: community, status: mined}\n",
    )
    with pytest.raises(ValueError, match="tier"):
        load_sources(tmp_cfg)


def test_mirror_of_unknown_repo_raises(tmp_cfg: Config) -> None:
    _write_sources(
        tmp_cfg.root,
        """
sources:
  - {repo: acme/skills, tier: A, group: acme, status: mined, mirror_of: missing/repo}
""",
    )
    with pytest.raises(ValueError, match="mirror_of"):
        load_sources(tmp_cfg)


def test_mirror_of_self_raises(tmp_cfg: Config) -> None:
    _write_sources(
        tmp_cfg.root,
        """
sources:
  - {repo: acme/skills, tier: A, group: acme, status: mined, mirror_of: acme/skills}
""",
    )
    with pytest.raises(ValueError, match="another listed repo"):
        load_sources(tmp_cfg)


def test_repo_must_match_owner_name(tmp_cfg: Config) -> None:
    _write_sources(
        tmp_cfg.root,
        "sources:\n  - {repo: not-a-repo, tier: A, group: acme, status: mined}\n",
    )
    with pytest.raises(ValueError, match="owner/name"):
        load_sources(tmp_cfg)


def test_source_is_frozen() -> None:
    source = Source(
        repo="acme/skills",
        tier="A",
        group="acme",
        status="mined",
        mirror_of=None,
        note=None,
    )
    with pytest.raises(AttributeError):
        source.tier = "B"  # type: ignore[misc]


def _write_sources(root: Path, body: str) -> None:
    path = root / "config" / "registry_sources.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
