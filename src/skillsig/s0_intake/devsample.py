"""
STAGE: s0_intake
MODULE: devsample
PURPOSE: Draw a seeded, stratified dev sample of skill file_sha ids.
INPUTS: artifacts view (file_sha, location_class, history_fetched, dedup_primary)
OUTPUTS: src/data/derived/s0_devsample_ids.parquet (file_sha, stratum)
RQ LINK: RQ1 / RQ2 (dev mode restricts later stages to these file_sha ids)
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from skillsig.config import Config
from skillsig.paths import resolve

log = logging.getLogger(__name__)

DEFAULT_DEV_SAMPLE = 50_000

# One row per file_sha. location_class and history_fetched come from the
# dedup_primary row (copies leave those fields empty). Copy-count is all rows.
_POPULATION_SQL = """
CREATE TEMP TABLE _pop AS
SELECT
    file_sha,
    coalesce(location_class, 'unknown')
        || '|history=' || CASE WHEN history_flag = 1 THEN '1' ELSE '0' END
        || '|copies=' || CASE
            WHEN copies >= 10 THEN '>=10'
            WHEN copies >= 2 THEN '2-9'
            ELSE '1'
        END AS stratum
FROM (
    SELECT
        file_sha,
        min(location_class) FILTER (WHERE dedup_primary = 1) AS location_class,
        max(CASE WHEN dedup_primary = 1 AND history_fetched = 1 THEN 1 ELSE 0 END)
            AS history_flag,
        count(*)::BIGINT AS copies
    FROM artifacts
    WHERE file_sha IS NOT NULL
    GROUP BY file_sha
    HAVING count(*) FILTER (WHERE dedup_primary = 1) > 0
)
"""


def output_path(cfg: Config) -> Path:
    """`<derived>/s0_devsample_ids.parquet` from Config.paths.derived."""
    return resolve(cfg, cfg.paths.derived) / "s0_devsample_ids.parquet"


def build_devsample(
    con: duckdb.DuckDBPyConnection, cfg: Config, n: int = DEFAULT_DEV_SAMPLE
) -> Path:
    """Stratified sample of n ids, seeded by cfg.seed. Always keeps dev_force_include."""
    if n < 1:
        raise ValueError(f"dev sample size must be >= 1, got {n}")
    _reset(con)
    con.execute(_POPULATION_SQL)
    _fill_sample(con, cfg, n)
    return _write_sample(con, output_path(cfg))


def _reset(con: duckdb.DuckDBPyConnection) -> None:
    for name in ("_sample", "_pop", "_force"):
        con.execute(f"DROP TABLE IF EXISTS {name}")


def _force_ids(cfg: Config) -> tuple[str, ...]:
    # T00 Config has no dev_force_include field; missing means an empty list.
    raw = getattr(cfg, "dev_force_include", ()) or ()
    if isinstance(raw, str):
        raw = (raw,)
    return tuple(dict.fromkeys(str(item) for item in raw))


def _fill_sample(con: duckdb.DuckDBPyConnection, cfg: Config, n: int) -> None:
    con.execute("CREATE TEMP TABLE _sample (file_sha VARCHAR, stratum VARCHAR)")
    _insert_forced(con, cfg)
    sizes = {
        stratum: int(count)
        for stratum, count in con.execute(
            "SELECT stratum, count(*) FROM _pop GROUP BY 1"
        ).fetchall()
    }
    forced = {
        stratum: int(count)
        for stratum, count in con.execute(
            "SELECT stratum, count(*) FROM _sample GROUP BY 1"
        ).fetchall()
    }
    seed = str(cfg.seed)
    for stratum, target in _allocate(sizes, n).items():
        need = target - forced.get(stratum, 0)
        if need <= 0:
            continue
        con.execute(
            """
            INSERT INTO _sample
            SELECT file_sha, stratum FROM _pop
            WHERE stratum = ?
              AND file_sha NOT IN (SELECT file_sha FROM _sample)
            ORDER BY hash(file_sha || ?), file_sha
            LIMIT ?
            """,
            [stratum, seed, need],
        )


def _insert_forced(con: duckdb.DuckDBPyConnection, cfg: Config) -> None:
    forced = _force_ids(cfg)
    if not forced:
        return
    con.execute("CREATE TEMP TABLE _force (file_sha VARCHAR)")
    con.executemany("INSERT INTO _force VALUES (?)", [(sha,) for sha in forced])
    con.execute(
        """
        INSERT INTO _sample
        SELECT f.file_sha, coalesce(p.stratum, 'absent')
        FROM _force f
        LEFT JOIN _pop p USING (file_sha)
        """
    )


def _allocate(sizes: dict[str, int], n: int) -> dict[str, int]:
    """How many ids to take from each stratum. Covers every stratum when n is large enough."""
    total = sum(sizes.values())
    if total <= n:
        return dict(sizes)
    strata = sorted(sizes)
    if n < len(strata):
        chosen = set(sorted(strata, key=lambda s: (-sizes[s], s))[:n])
        return {s: int(s in chosen) for s in strata}
    quota = {s: 1 for s in strata}
    _spread(quota, {s: sizes[s] - 1 for s in strata}, n - len(strata))
    if sum(quota.values()) != n:
        raise RuntimeError(f"dev sample allocation summed to {sum(quota.values())}, expected {n}")
    return quota


def _spread(quota: dict[str, int], room: dict[str, int], left: int) -> None:
    """Add `left` seats in proportion to remaining room (largest remainder, then caps)."""
    if left <= 0:
        return
    eligible = [s for s in sorted(room) if room[s] > 0]
    weight = sum(room[s] for s in eligible)
    if weight <= 0:
        return
    raw = {s: left * room[s] / weight for s in eligible}
    extra = {s: min(room[s], int(raw[s])) for s in eligible}
    used = sum(extra.values())
    order = sorted(eligible, key=lambda s: (-(raw[s] - int(raw[s])), s))
    for stratum in order:
        if used >= left:
            break
        if extra[stratum] < room[stratum]:
            extra[stratum] += 1
            used += 1
    if used < left:
        for stratum in sorted(eligible, key=lambda s: (-room[s], s)):
            take = min(room[stratum] - extra[stratum], left - used)
            extra[stratum] += take
            used += take
            if used >= left:
                break
    for stratum, taken in extra.items():
        quota[stratum] += taken


def _write_sample(con: duckdb.DuckDBPyConnection, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    dest = "'" + path.as_posix().replace("'", "''") + "'"
    con.execute(
        f"""
        COPY (
            SELECT file_sha, stratum FROM _sample ORDER BY file_sha
        ) TO {dest} (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    log.info("wrote dev sample %s", path)
    return path
