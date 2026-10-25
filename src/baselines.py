"""Single-signal baselines for the authorship-signal study (Sprint 2).

Each baseline looks at ONE signal and makes positive calls only. Anything it
cannot call is "insufficient_evidence", never "likely_human" by absence.
Label names and signal definitions follow DATA_DICTIONARY.md (sections 2 and 3).
The unit of analysis is a distinct skill: one row per file_sha with
dedup_primary = 1.
"""
import re

import numpy as np
import pandas as pd

HUMAN = "likely_human"
AGENT = "agent_assisted"
TEMPLATED = "highly_templated"
INSUFFICIENT = "insufficient_evidence"
LABELS = (HUMAN, AGENT, TEMPLATED, INSUFFICIENT)

COPY_THRESHOLD = 10  # files sharing a file_sha
BULK_CUTOFF = 6  # skills sharing a repository and first_commit_at
ADOPTION_DATE = pd.Timestamp("2025-10-01", tz="UTC")  # agent-skill format launch
NO_TOOL_FOLDER = {"No agent folder", "Other dot-folder"}

# DATA_DICTIONARY.md section 3: a Co-authored-by line naming one of these.
AI_TRAILER = re.compile(
    r"^\s*co-authored-by:.*(claude|anthropic|cursor|copilot|codex|openai|gemini)",
    re.IGNORECASE | re.MULTILINE,
)


def has_ai_trailer(message):
    """True if a commit message has an AI Co-authored-by trailer."""
    return isinstance(message, str) and AI_TRAILER.search(message) is not None


def _call(df, mask, label):
    return pd.Series(np.where(mask, label, INSUFFICIENT), index=df.index)


def copy_count(df):
    """Highly templated if the content occurs in COPY_THRESHOLD or more files."""
    return _call(df, df["copies"] >= COPY_THRESHOLD, TEMPLATED)


def agent_folder(df):
    """Agent-assisted if the file sits under a named agent-tool folder."""
    folder = df["agent_folder"]
    return _call(df, folder.notna() & ~folder.isin(NO_TOOL_FOLDER), AGENT)


def trailer(df, exclude_bulk=False):
    """Agent-assisted if the first commit carries an AI trailer.

    Needs commit history, so skills without it stay insufficient. With
    exclude_bulk, installs of BULK_CUTOFF or more skills are not called.
    """
    has_trailer = df["first_commit_message"].map(has_ai_trailer).astype(bool)
    hit = (df["history_fetched"] == 1) & has_trailer
    if exclude_bulk:
        hit &= ~df["is_bulk"].fillna(False).astype(bool)
    return _call(df, hit, AGENT)


def repo_date(df):
    """Likely human if the repository was created before the format existed."""
    created = pd.to_datetime(df["repo_created_at"], errors="coerce", utc=True)
    return _call(df, created < ADOPTION_DATE, HUMAN)


BASELINES = {
    "agent_folder": agent_folder,
    "copy_count": copy_count,
    "trailer": trailer,
    "trailer_excl_bulk": lambda df: trailer(df, exclude_bulk=True),
    "repo_date": repo_date,
}


def run_all(df):
    """Long table, one row per (skill, baseline): file_sha, baseline, label."""
    parts = [
        pd.DataFrame({"file_sha": df["file_sha"].values, "baseline": name, "label": fn(df).values})
        for name, fn in BASELINES.items()
    ]
    return pd.concat(parts, ignore_index=True)


def load_features(raw_dir="src/data/gitskills_data/data", gen_dir="src/data/generated_data"):
    """Build the per-skill input frame from the raw and generated Parquet files.

    Run from the repository root. Needs duckdb (in requirements.txt).
    """
    import duckdb

    sql = f"""
    WITH reps AS (
      SELECT repo_full_name, path, file_sha, history_fetched,
             first_commit_at, first_commit_message
      FROM read_parquet('{raw_dir}/artifacts/*.parquet')
      WHERE dedup_primary = 1
    ),
    batches AS (
      SELECT repo_full_name, first_commit_at, COUNT(*) AS batch_size
      FROM reps
      WHERE history_fetched = 1
      GROUP BY repo_full_name, first_commit_at
    ),
    copies AS (
      SELECT file_sha, COUNT(*) AS copies
      FROM read_parquet('{gen_dir}/file_agents.parquet')
      GROUP BY file_sha
    )
    SELECT r.file_sha, c.copies, f.agent_folder, r.history_fetched,
           r.first_commit_message,
           COALESCE(b.batch_size >= {BULK_CUTOFF}, FALSE) AS is_bulk,
           p.created_at AS repo_created_at
    FROM reps r
    LEFT JOIN copies c ON c.file_sha = r.file_sha
    LEFT JOIN read_parquet('{gen_dir}/file_agents.parquet') f
           ON f.repo_full_name = r.repo_full_name AND f.path = r.path
    LEFT JOIN batches b
           ON b.repo_full_name = r.repo_full_name AND b.first_commit_at = r.first_commit_at
    LEFT JOIN read_parquet('{raw_dir}/repos/*.parquet') p
           ON p.full_name = r.repo_full_name
    """
    return duckdb.connect().execute(sql).df()
