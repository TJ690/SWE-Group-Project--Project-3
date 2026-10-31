"""Paths, thresholds and script-risk patterns for every skill_genealogy stage.

What: folders, file globs, numeric limits, and script-risk patterns.
Inputs: this file's location. The repo root is the nearest folder that
contains ``pyproject.toml``.
Outputs: none. ``data_dir`` and ``results_dir`` create their folder when
a stage asks for it.

Stages import these names. Nothing here reads the dataset.
"""

from __future__ import annotations

from pathlib import Path

from lingua import Language


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
# MinHash seed. Same value as RANDOM_SEED and the main pipeline plan.json.
MINHASH_SEED = RANDOM_SEED
# A8: fewer than this many letters is "unknown" (short skills count as English).
MIN_LANGUAGE_LETTERS = 20
# A8: Latin-script letter share required before a text can be labelled English.
MIN_ENGLISH_LETTER_SHARE = 0.75
# A8: lingua confidence of the top language; below this the text is "unknown".
LANGUAGE_MIN_CONFIDENCE = 0.50
# A8: characters of the body read for language detection (speed).
LANGUAGE_MAX_CHARS = 2000
# A8: languages lingua chooses from. A text outside this list is still non-English.
CANDIDATE_LANGUAGES: tuple[Language, ...] = (
    Language.ENGLISH,
    Language.CHINESE,
    Language.JAPANESE,
    Language.KOREAN,
    Language.RUSSIAN,
    Language.UKRAINIAN,
    Language.GERMAN,
    Language.FRENCH,
    Language.SPANISH,
    Language.PORTUGUESE,
    Language.ITALIAN,
    Language.DUTCH,
    Language.POLISH,
    Language.TURKISH,
    Language.VIETNAMESE,
    Language.INDONESIAN,
    Language.ARABIC,
    Language.PERSIAN,
    Language.HINDI,
    Language.THAI,
)
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
# A13: a registry section is "kept" at or above this share of unchanged lines. 0 means removed.
SECTION_KEPT_MIN_SHARE = 0.9
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
# GIT8: blob ids per `git fetch`. 500 keeps one command line short.
BLOB_FETCH_BATCH = 500
# merge_shards writes text rows in groups of this size. One huge copy broke a file before.
TEXT_ROW_GROUP = 1000
# No design cap for SKILL.md. This is only the integer passed to read_blob_texts.
# Git's own blob limit is about 2 GiB.
SKILL_MD_MAX_BYTES = 2_000_000_000
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
# A16: what a script can do. A match is a capability, not a malware verdict.
# Lists are the stage 9 examples (G04). Patterns are case-sensitive.
SCRIPT_RISK_PATTERNS: dict[str, tuple[str, ...]] = {
    "network": (
        r"\bcurl\b",
        r"\bwget\b",
        r"requests\.",
        r"fetch\(",
        r"https?://",
        r"urllib",
    ),
    "runs_commands": (
        r"subprocess",
        r"os\.system",
        r"\beval\(",
        r"\bexec\(",
        r"child_process",
    ),
    "deletes_files": (
        r"rm\s+-rf",
        r"shutil\.rmtree",
        r"\bunlink\(",
        r"os\.remove",
    ),
    "reads_secrets": (
        r"\.env\b",
        r"API_KEY",
        r"\btoken\b",
        r"~/\.ssh",
        r"credentials",
    ),
    "installs_packages": (
        r"\bpip3? install",
        r"npm (i|install)\b",
        r"brew install",
        r"curl[^\n|]*\|\s*(sh|bash)",
    ),
    "obfuscated": (
        r"base64\s+(-d|--decode)",
        r"[A-Za-z0-9+/]{200,}={0,2}",
    ),
}


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
