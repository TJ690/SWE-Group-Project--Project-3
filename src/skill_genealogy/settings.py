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
# Tree check (bug B01). A non-fork with at least this many stars and 1 to 4
# English skills in the dataset gets its SKILL.md files counted from the repo
# itself (stage 2b, check_registry_trees). Code search missed most skills of some
# popular upstreams, such as obra/superpowers. Same cutoff as the registry rule.
TREE_CHECK_MIN_STARS = 100
# Share of skills first seen in an older repo.
HUB_MIN_SHARE = 0.25
# Smaller repos are never hubs.
HUB_TEST_MIN_SKILLS = 5
# A hub also needs at least this many dated texts. One dated text is not enough.
HUB_MIN_DATED_SKILLS = 5
# Report when missing commit dates cover more than this share of registry texts.
NULL_DATE_REPORT_ABOVE = 0.10
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
# A23: LSH band used only to gather containment candidates. Near copies use
# NEAR_COPY_MIN_JACCARD. A copy can contain the older text and still have a
# Jaccard below 0.80, so those pairs are queried at 0.5. A copy that grew
# much larger than the source can fall below 0.5 and be missed.
REHOST_CONTAINMENT_LSH_THRESHOLD = 0.5
# A23 (G13): a near_contained registry link needs the older (source) text to
# keep at least this many shingles after boilerplate removal. A smaller rest is
# often only headings ("## Instructions", "## Safety", "## Resources") that
# unrelated skills share, so it is contained in many texts by chance. Full run
# 2026-11-09: contained links with a source under 20 shingles kept the skill's
# folder name in 26-48% of cases, from 20 to 200 shingles in 78-89%. A near
# link needs similar sizes on both sides and is not limited.
# A26 (G14) uses the same minimum for a registry text contained in a dataset
# text, for the same reason: the line check cannot catch it, because a copy
# that has those headings keeps every line of such a registry text.
REHOST_CONTAINED_MIN_SHINGLES = 20
# A25/A26 (G14): the LSH band layout that gathers dataset-copy candidates.
# datasketch's MinHashLSH picks 64 bands of 2 values for this threshold. A
# pair with Jaccard j shares a band with probability 1 - (1 - j^2)^64: 0.93 at
# 0.2, 0.997 at 0.3, 1.0 at 0.4. A registry text of 40 lines inside a copy of
# 100 lines (Jaccard about 0.4, containment 1.0) is found; with the 25 bands of
# 5 values of REHOST_CONTAINMENT_LSH_THRESHOLD it is found 1 time in 5. A copy
# with containment 0.8 is found reliably while it is at most about 5 times the
# registry text. Measured 2026-11-09 on 20,000 dataset texts against the full
# registry index: 581 band hits per text (25 x 5 bands: 97; one value: 2,917).
COPY_SEARCH_LSH_THRESHOLD = 0.1
# A26: a source that agrees on this many MinHash values or fewer (J <= 0.04) is
# chance, not evidence. It is not picked when a strong source is also eligible.
# A small registry text inside a large copy can reach containment 1.0 from one
# shared value and then win the oldest-date tie.
WEAK_MATCH_MAX_VALUES = 5
# A26: a source that agrees on at least this many MinHash values (J >= 0.16).
STRONG_MATCH_MIN_VALUES = 20
# A28b (G14): a copier registry with stage 3c confidence below this is weak.
# A text held only by weak copiers is flagged weak_copiers_only. Same cutoff
# as the stage 3c review sheet (KIND_REVIEW_BELOW).
WEAK_COPIER_BELOW = 0.5
# Line-diff check after a near match. A link below this share of registry
# body lines is dropped. 0.50 keeps a copy that still has half the source lines.
MIN_VERIFIED_LINE_SHARE = 0.50
# A27: pass a lineage only through s2_clusters rows at this threshold.
# The file also has 0.60 clusters. Those group looser neighbours and are not used.
CLUSTER_LINK_THRESHOLD = 0.80
# Stage 7 DuckDB joins. The memory cap keeps the English artifact join from
# loading file content. The temp cap is the spill folder under data_dir(dev).
DUCKDB_MEMORY_LIMIT = "8GB"
DUCKDB_TEMP_LIMIT = "40GB"
# Language labelling. One hash slice groups full skill texts. 2GB is too
# small for that group-by (DuckDB stops instead of spilling). 8GB fits one
# slice. Threads stay low so the hash table does not grow with every core.
LANGUAGE_DUCKDB_MEMORY_LIMIT = "8GB"
LANGUAGE_DUCKDB_THREADS = 2
# A22: a line shared by this many GitHub owners is template text.
# G12 keeps 50 unless the top 30 lines are real skill content (see PROGRESS).
BOILERPLATE_MIN_OWNERS = 50
# A22: lines with fewer whitespace-separated words are dropped before the owner
# count. A two-word heading such as "## usage" is not treated as boilerplate.
BOILERPLATE_MIN_WORDS = 3
# Same as rq2.substantive_body_change_pct.
SUBSTANTIVE_BODY_CHANGE_PCT = 5
# A13: a registry section is "kept" at or above this share of unchanged lines. 0 means removed.
SECTION_KEPT_MIN_SHARE = 0.9
# Same as rq2.grace_days.
STALE_AFTER_DAYS = 30
# A52: the copy-chain walk stops after this many nodes, counting the skill.
# A family tree can be deeper than one terminal line should print.
MAX_CHAIN_STEPS = 50
# Parent search cap.
TREE_MAX_EARLIER_MEMBERS = 2000
# A54: independent family spread. Below 2 owners the family is one_repo or one_owner.
# Spread "few_owners" below SOME, "some_owners" below WIDE, "wide" at WIDE or more owners.
SPREAD_SOME_MIN_OWNERS = 10
SPREAD_WIDE_MIN_OWNERS = 50
# A55: the staleness viewer diff keeps at most this many lines of one SKILL.md text.
# A cap keeps one huge text (the longest body is 355,781 characters) from filling a file.
DIFF_MAX_LINES = 3000
# Parallel clones.
DOWNLOAD_WORKERS = 8
# Per repository.
DOWNLOAD_TIMEOUT_SECONDS = 600
# Above this, one git log for all paths (no --follow).
MANY_PATHS_LIMIT = 200
# Most pathspecs in one GIT6 log. Git compares every changed entry with every
# pathspec, so 1,000 folders inside a directory of 100,000 skills is very slow.
# Above this many folders, GIT6 logs their parent folders instead.
GIT6_MAX_PATHSPECS = 1000
# Stage 3 merge and summary run in DuckDB, which spills to disk above this.
# The pandas merge of 5 million change rows peaked near 15 GB.
HISTORY_DUCKDB_MEMORY_LIMIT = "4GB"
# Stage 3 deleted skill folders (bug 2). A repo with more history-only folders
# than this is skipped: it rebuilt or moved its whole layout (a crawler such as
# majiayu000/claude-skill-registry has 300,000+), and its old paths are not
# deleted skills. Logging them all used more memory than the machine has.
DELETED_FOLDERS_MAX = 20_000
# Seconds between progress lines for a long step inside one repo.
PROGRESS_SECONDS = 30
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
# 99th percentile, 69 KB). See "Script size limit" in ARCHITECTURE_DECISIONS.md.
MAX_SCRIPT_BYTES = 200_000
# GIT8: blob ids per `git fetch`. 500 keeps one command line short.
BLOB_FETCH_BATCH = 500
# Stage 3b README download. GitHub shows the first README it finds in
# .github/, then the repo root, then docs/; within a folder these names are
# tried in this order (any letter case). Larger READMEs keep their row but no
# text, the same cap as a script.
README_FOLDERS = (".github", "", "docs")
README_FILE_NAMES = (
    "readme.md",
    "readme.markdown",
    "readme.mdx",
    "readme.rst",
    "readme.txt",
    "readme.org",
    "readme.adoc",
    "readme",
)
README_MAX_BYTES = 200_000
# Stage 3c registry kinds (source or copier). The date vote counts SKILL.md
# texts a registry shares with registries of other owners, and asks who had each
# text first. Its strength grows with the distance of that share from 0.5 and
# with the number of shared texts, up to KIND_FULL_EVIDENCE_TEXTS.
KIND_FULL_EVIDENCE_TEXTS = 20
# A registry that shares no text with another owner: nothing in it was copied
# from another registry. That is a source vote of this strength.
KIND_NO_SHARED_SOURCE_STRENGTH = 0.7
# A share of texts first seen elsewhere at or below this is a source vote of at
# least KIND_NO_SHARED_SOURCE_STRENGTH: others copy it, it copies little.
KIND_LOW_SHARE_SOURCE_MAX = 0.2
# The share above counts only shared texts, so a registry that wrote 600 skills
# and borrowed one shows 1 of 1 first seen elsewhere. When the texts first seen
# elsewhere are below this share of ALL the registry's own SKILL.md texts, the
# copier vote becomes a source vote of KIND_NO_SHARED_SOURCE_STRENGTH: it wrote
# nearly everything itself. Only below KIND_FULL_EVIDENCE_TEXTS shared texts: a
# crawler of repos outside the registries also has many unshared texts.
KIND_COPIED_SHARE_SOURCE_MAX = 0.1
# Below this confidence a registry goes to the review sheet.
KIND_REVIEW_BELOW = 0.5
# README phrases (lower case). A copier phrase says the repo holds skills of
# other authors; a source phrase says the skills are the team's own. A phrase
# counts only as whole words, only in a sentence that mentions a skill, and not
# right after "not", "no" or "never" ("not a mirror of" is not a copier). Words
# such as "a curated collection of skills" are not used: on the 2026-11-07 run
# original authors (MicrosoftDocs/Agent-Skills, GPTomics/bioSkills) used them
# for their own skills, so they describe packaging, not where skills came from.
# "copied from" is not used either: its 7 matches on that run described install
# steps, file trees or one borrowed plugin (microsoft/skills), and the date vote
# called all 7 sources.
README_COPIER_STRONG = (
    "aggregate catalog",
    "this mirror",
    "a mirror of",
    "mirrors of",
    "not necessarily the author",
    "not the author",
    "not the original author",
    "collected from",
    "aggregated from",
    "scraped from",
    "crawled from",
    "automatically collected",
    "auto-collected",
    "all credit goes to",
    "credit goes to the original",
)
README_SOURCE = (
    "our skills",
    "our own skills",
    "official skills",
    "skills built by",
    "built by our team",
    "written by our team",
    "maintained by our team",
)
README_STRONG_STRENGTH = 0.8
README_SOURCE_STRENGTH = 0.6
# Blobs per git cat-file --batch read. One read of every blob at once held
# several GB in memory for a repo with 500,000 texts.
BLOB_READ_BATCH = 2000
# Stages 4 and 5 read registry SKILL.md texts about this many blobs at a time.
# Without the copiers, stage 4 needs 933,117 SKILL.md blobs with 9.4 GB of text
# (2026-11-07), more than this machine's 16 GB holds as Python strings. 40,000
# blobs is about 0.4 GB (median text 5 KB, mean 10 KB).
LINEAGE_TEXT_BATCH = 40_000
# Stage 4 (G10 rule 0): registry kinds from stage 3c (registry_kinds.parquet)
# that become lineages. A copier is never a lineage, root or origin, so every
# later stage, which reads the stage 4 tables, leaves copiers out without a
# second filter. "unclear" (44 registries, no vote either way) is kept.
LINEAGE_REGISTRY_KINDS: tuple[str, ...] = ("source", "unclear")
# Stage 4b (G11): change rows of whole registries read and replayed per batch.
# The kept English lineages have 4.4M own and former-folder rows (2026-11-08);
# 400,000 rows is about 0.5 GB in pandas. A registry is never split, so one
# larger registry (github/gh-aw, 395,000 rows) is a batch of its own.
PACKAGE_ROW_BATCH = 400_000
# Stage 4b (G11): a skill whose folder is the repo root (skill_folder ".") would
# make the whole repository its package: 75 root lineages have 1.07M file rows
# (github/gh-aw alone 395,000). For them only these top-level folders count as
# package files. They are the optional skill folders of the Agent Skills format
# (scripts/, references/, assets/). Other skill folders keep every file.
ROOT_SKILL_PACKAGE_FOLDERS: tuple[str, ...] = ("scripts", "references", "assets")
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
