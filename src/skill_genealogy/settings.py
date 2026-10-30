"""Paths and thresholds for every skill_genealogy stage.

What: folders, file globs, and the numeric limits the stages share.
Inputs: this file's location. The repo root is the nearest folder that
contains ``pyproject.toml``.
Outputs: none. ``data_dir`` and ``results_dir`` create their folder when
a stage asks for it.

Stages import these names. Nothing here reads the dataset.
"""

from __future__ import annotations

from pathlib import Path


def _find_repo_root() -> Path:
    """Return the nearest folder above this file that contains pyproject.toml.

    Example: this file lives in ``src/skill_genealogy/``, so the root is
    two folders up when that folder holds ``pyproject.toml``.
    """
    start = Path(__file__).resolve().parent
    for folder in (start, *start.parents):
        if (folder / "pyproject.toml").is_file():
            return folder
    raise FileNotFoundError("pyproject.toml not found above src/skill_genealogy/settings.py")


# Nearest folder that contains pyproject.toml.
REPO_ROOT: Path = _find_repo_root()
# Raw GitSkills tables: src/data/gitskills_data/data
RAW_DATA_DIR: Path = REPO_ROOT / "src" / "data" / "gitskills_data" / "data"
# RAW_DATA_DIR/artifacts/*.parquet
ARTIFACTS_GLOB: str = str(RAW_DATA_DIR / "artifacts" / "*.parquet")
# RAW_DATA_DIR/repos/*.parquet
REPOS_GLOB: str = str(RAW_DATA_DIR / "repos" / "*.parquet")
# RAW_DATA_DIR/mining_runs/*.parquet
MINING_RUNS_GLOB: str = str(RAW_DATA_DIR / "mining_runs" / "*.parquet")
# RAW_DATA_DIR/artifact_siblings/*.parquet
SIBLINGS_GLOB: str = str(RAW_DATA_DIR / "artifact_siblings" / "*.parquet")
# src/data/wild_history (read only)
WILD_HISTORY_DIR: Path = REPO_ROOT / "src" / "data" / "wild_history"
# src/data/derived/s2_minhash_shards (read only)
MINHASH_SHARDS_DIR: Path = REPO_ROOT / "src" / "data" / "derived" / "s2_minhash_shards"
# src/data/derived/s2_clusters.parquet (read only)
CLUSTERS_PATH: Path = REPO_ROOT / "src" / "data" / "derived" / "s2_clusters.parquet"

# Same seed as the main pipeline.
RANDOM_SEED = 20261015
# Registry rule (decided with the team).
MIN_REGISTRY_STARS = 100
# Registry rule.
MIN_REGISTRY_ENGLISH_SKILLS = 5
# Share of skills first seen in an older repo.
HUB_MIN_SHARE = 0.25
# Smaller repos are never hubs.
HUB_TEST_MIN_SKILLS = 5
# Dev sample size in registries.
DEV_REGISTRY_COUNT = 30
# Dev sample keeps at least this many hubs.
DEV_MIN_HUBS = 3
# Same as main pipeline minhash.shingle_words.
SHINGLE_WORDS = 5
# Same as main pipeline minhash.num_perm.
MINHASH_NUM_PERM = 128
# Same as thresholds.templated_jaccard_min.
NEAR_COPY_MIN_JACCARD = 0.80
# Broder 1997 containment, our choice.
NEAR_COPY_MIN_CONTAINMENT = 0.80
# Line-diff check after a near match.
MIN_VERIFIED_LINE_SHARE = 0.50
# A line in skills of this many owners is template text (tuned in G13).
BOILERPLATE_MIN_OWNERS = 50
# Same as rq2.substantive_body_change_pct.
SUBSTANTIVE_BODY_CHANGE_PCT = 5
# Same as rq2.grace_days.
STALE_AFTER_DAYS = 30
# Parent search cap.
TREE_MAX_EARLIER_MEMBERS = 2000
# Parallel clones.
DOWNLOAD_WORKERS = 8
# Per repository.
DOWNLOAD_TIMEOUT_SECONDS = 600
# Above this, one git log for all paths (no --follow).
MANY_PATHS_LIMIT = 200
# Script files.
SCRIPT_EXTENSIONS: tuple[str, ...] = (
    ".py",
    ".sh",
    ".bash",
    ".js",
    ".mjs",
    ".ts",
    ".ps1",
    ".rb",
)
# Larger scripts compared by sha only. Covers 99.8% of skill-folder scripts (about 3x the
# 99th percentile, 69 KB); see "Script size limit" in docs/skill_genealogy/ARCHITECTURE_DECISIONS.md.
MAX_SCRIPT_BYTES = 200_000
# Vendored folders.
SKIPPED_FOLDERS: tuple[str, ...] = (
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "dist/",
    "build/",
    ".git/",
)


def data_dir(dev: bool) -> Path:
    """Return the folder for stage tables, creating it if it is missing.

    Dev and full runs use different folders, so one cannot overwrite the other.
    Dev ends in ``skill_genealogy_dev``. Full ends in ``skill_genealogy``.
    Both sit under ``src/data/``.

    Inputs: ``dev`` — True for the 30-registry sample, False for the full run.
    Outputs: the folder path. The folder exists when this function returns.
    """
    folder_name = "skill_genealogy_dev" if dev else "skill_genealogy"
    folder = REPO_ROOT / "src" / "data" / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def results_dir(dev: bool) -> Path:
    """Return the folder for report tables, creating it if it is missing.

    Dev ends in ``results/skill_genealogy_dev``. Full ends in
    ``results/skill_genealogy``.

    Inputs: ``dev`` — True for the 30-registry sample, False for the full run.
    Outputs: the folder path. The folder exists when this function returns.
    """
    folder_name = "skill_genealogy_dev" if dev else "skill_genealogy"
    folder = REPO_ROOT / "results" / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    return folder
