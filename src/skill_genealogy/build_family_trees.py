"""Stage 8: one tree per family (algorithms A36 and A37, query Q8.1).

What: stage 7 gave every English text one family. This stage turns each family
into a tree. A source family starts with its registry versions (generation 0,
chained by version, or by ``copied_from_seq`` on a re-host). Every other text
is a node whose parent is the most similar earlier node (MinHash share of
equal values), so its generation is its parent's plus one. An independent
family has no registry trunk: its root is the oldest dated text (the most
copied one when no text has a date) and the other texts hang below it.
Singletons and copier-only texts are leaves. A parent is the closest earlier
text, not a proof of who copied whom.

Inputs (dev or full data folder): ``families.parquet``, ``copy_files.parquet``
(stage 7), ``lineage_versions.parquet``, ``lineage_roots.parquet`` (stage 4a,
5b), ``registry_signatures.npz`` (5a). Full data folder (shared):
``dataset_signatures/``. GitSkills ``artifacts`` for the dates of texts that
are not copy files.

Outputs: ``family_tree.parquet`` (one row per node) and
``family_summary.parquet`` (one row per family).

Run: ``python -m skill_genealogy.build_family_trees [--dev] [--force]
[--skip-families-over N]``. Design note: docs/skill_genealogy/design/.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from skill_genealogy import settings
from skill_genealogy.file_helpers import (
    build_arg_parser,
    check_inputs_exist,
    connect_duckdb,
    outputs_exist,
    print_sample_records,
    print_summary,
    read_parquet,
    write_parquet,
)
from skill_genealogy.find_copies import (
    DUCKDB_MEMORY_LIMIT,
    DUCKDB_TEMP_LIMIT,
    _load_one,
    _quote_path,
    _signature_files,
    dataset_signatures_dir,
    lineage_roots_path,
    load_signatures,
    registry_signatures_path,
)

TREE_COLUMNS = [
    "family_id",
    "node_id",
    "node_kind",
    "blob",
    "repo",
    "first_date",
    "files",
    "parent_id",
    "parent_similarity",
    "generation",
    "approx",
]
# Kinds that are always one leaf per text (no comparison is made).
LEAF_KINDS = ("singleton", "copier_seen")
# A family reports progress every this many members (and the run at most every 30 seconds).
TICK_MEMBERS = 500

# Query Q8.1, see docs/skill_genealogy/tasks/G18.md
# The files of the wanted texts with their dataset first_commit_at, as UTC.
DATASET_FILES_SQL = """
SELECT
    artifacts.repo_full_name AS repo,
    artifacts.path AS path,
    artifacts.file_sha AS file_sha,
    min(TRY_CAST(artifacts.first_commit_at AS TIMESTAMPTZ)) AS first_commit_date
FROM artifacts
SEMI JOIN wanted_texts ON artifacts.file_sha = wanted_texts.file_sha
WHERE artifacts.repo_full_name IS NOT NULL AND artifacts.path IS NOT NULL
GROUP BY artifacts.repo_full_name, artifacts.path, artifacts.file_sha
"""


def family_tree_path(dev: bool) -> Path:
    """``family_tree.parquet`` in the dev or full data folder."""
    return settings.data_dir(dev) / "family_tree.parquet"


def family_summary_path(dev: bool) -> Path:
    """``family_summary.parquet`` in the dev or full data folder."""
    return settings.data_dir(dev) / "family_summary.parquet"


def output_paths(dev: bool) -> dict[str, Path]:
    """The two stage 8 output files of this mode."""
    return {"family_tree": family_tree_path(dev), "family_summary": family_summary_path(dev)}


class Progress:
    """Timestamped progress lines plus a status file that can be read at any time.

    Every ``stage`` call prints one line. ``trees`` prints a line at most every
    ``every`` seconds: families and texts done, the family being built, texts
    per second and an estimated time left. The same numbers are written to
    ``logs/build_family_trees_<dev|full>.progress.json`` (replaced in one step,
    so a reader never sees half a file). ``cat`` the file, or ``tail`` the log.
    """

    def __init__(self, dev: bool, every: float = 30.0, path: Path | None = None) -> None:
        self.start = time.perf_counter()
        self.every = every
        self.path = path or settings.REPO_ROOT / "logs" / (
            f"build_family_trees_{'dev' if dev else 'full'}.progress.json"
        )
        self.last = 0.0
        self.state: dict[str, object] = {"stage": "starting", "done": False}

    def _elapsed(self) -> float:
        return time.perf_counter() - self.start

    def _write(self) -> None:
        self.state["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self.state["elapsed_seconds"] = round(self._elapsed(), 1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.state, indent=2, default=str))
            temp.replace(self.path)
        except OSError:
            pass  # progress must never stop the build

    def stage(self, name: str, detail: str = "") -> None:
        """Start a named stage and print one line."""
        self.state.update({"stage": name, "detail": detail})
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp} +{self._elapsed():7.1f}s] {name}: {detail}".rstrip(": "), flush=True)
        self.last = time.perf_counter()
        self._write()

    def finish(self) -> None:
        """Mark the run finished."""
        self.state.update({"stage": "done", "done": True})
        self._write()

    def trees(
        self,
        families_done: int,
        families_total: int,
        texts_done: int,
        texts_total: int,
        current: str = "",
        current_done: int = 0,
        current_total: int = 0,
        force: bool = False,
    ) -> None:
        """Record tree progress; print a line when ``every`` seconds have passed."""
        seconds = max(self._elapsed() - float(self.state.get("trees_started", 0.0)), 1e-9)
        rate = texts_done / seconds
        left = (texts_total - texts_done) / rate if rate > 0 else float("nan")
        self.state.update(
            {
                "stage": "trees",
                "families_done": families_done,
                "families_total": families_total,
                "texts_done": texts_done,
                "texts_total": texts_total,
                "percent": round(100 * texts_done / max(texts_total, 1), 2),
                "current_family": current,
                "current_family_done": current_done,
                "current_family_total": current_total,
                "texts_per_second": round(rate, 1),
                "eta_seconds": None if rate <= 0 else round(left),
            }
        )
        now = time.perf_counter()
        if not force and now - self.last < self.every:
            return
        self.last = now
        stamp = datetime.now().strftime("%H:%M:%S")
        inside = f" · in {current} {current_done}/{current_total}" if current_total > 1000 else ""
        print(
            f"[{stamp} +{self._elapsed():7.1f}s] trees: families {families_done}/{families_total}"
            f" · texts {texts_done}/{texts_total} ({self.state['percent']}%)"
            f"{inside} · {rate:.0f} texts/s · ETA {left / 60:.1f} min",
            flush=True,
        )
        self._write()


# Node ids


def registry_node_id(lineage_id: str, version_seq: int) -> str:
    """``lineage_id#version_seq``, the registry signature id.

    >>> registry_node_id("a/b::skills/x", 2)
    'a/b::skills/x#2'
    """
    return f"{lineage_id}#{int(version_seq)}"


def copy_node_id(file_sha: str) -> str:
    """``text:`` plus the blob sha.

    >>> copy_node_id("abc")
    'text:abc'
    """
    return f"text:{file_sha}"


# Dates


def _utc_index(values: object) -> pd.DatetimeIndex:
    """UTC ``DatetimeIndex`` of ``values``; an index is positional in a DataFrame."""
    return pd.DatetimeIndex(pd.to_datetime(values, utc=True))


def _naive_utc(values: object) -> np.ndarray:
    """``datetime64[ns]`` array in UTC (NaT when missing), fast to compare and search."""
    return _utc_index(values).tz_convert(None).to_numpy("datetime64[ns]")


# Nodes of one family


def build_trunk_nodes(
    family_id: str, registry_versions: pd.DataFrame, roots: pd.DataFrame
) -> pd.DataFrame:
    """One generation-0 node per registry version of the family.

    ``registry_versions`` has ``lineage_id, version_seq, blob, release_date``.
    The parent is the previous version of the same lineage. The first version
    of a re-hosted lineage points at ``copied_from_lineage#copied_from_seq``
    when that version is in the family. ``roots`` has ``lineage_id``,
    ``copied_from_lineage`` and ``copied_from_seq``.
    """
    if registry_versions.empty:
        return pd.DataFrame(columns=TREE_COLUMNS)
    versions = registry_versions.sort_values(["lineage_id", "version_seq"], kind="mergesort")
    node_ids = [
        registry_node_id(lineage, seq)
        for lineage, seq in zip(versions["lineage_id"], versions["version_seq"], strict=True)
    ]
    known = set(node_ids)
    rehost = {}
    for row in roots.itertuples(index=False):
        if pd.notna(row.copied_from_lineage) and pd.notna(row.copied_from_seq):
            rehost[row.lineage_id] = registry_node_id(row.copied_from_lineage, row.copied_from_seq)
    parents = []
    previous_lineage, previous_id = None, ""
    for node_id, lineage in zip(node_ids, versions["lineage_id"], strict=True):
        if lineage != previous_lineage:
            source = rehost.get(lineage, "")
            parents.append(source if source in known else "")
        else:
            parents.append(previous_id)
        previous_lineage, previous_id = lineage, node_id
    return pd.DataFrame(
        {
            "family_id": family_id,
            "node_id": node_ids,
            "node_kind": "registry",
            "blob": versions["blob"].astype(str).to_numpy(),
            "repo": versions["lineage_id"].str.split("::").str[0].to_numpy(),
            "first_date": _utc_index(versions["release_date"]),
            "files": 0,
            "parent_id": parents,
            "parent_similarity": np.nan,
            "generation": 0,
            "approx": False,
        }
    )


def earliest_files(files: pd.DataFrame) -> pd.DataFrame:
    """One row per text from its files: the earliest file sets repo and date.

    ``files`` has ``repo, path, file_sha, first_commit_date``. A dated file
    wins over an undated one; a date tie takes the smaller repo, then path.
    ``files`` counts all files of the text. A text with no files is not in the
    result (the caller treats it as undated).
    """
    columns = ["file_sha", "repo", "first_date", "files"]
    if files.empty:
        return pd.DataFrame(columns=columns)
    table = files.assign(first_date=pd.to_datetime(files["first_commit_date"], utc=True))
    table["_undated"] = table["first_date"].isna()
    counts = table.groupby("file_sha", sort=False).size().rename("files")
    table = table.sort_values(
        ["file_sha", "_undated", "first_date", "repo", "path"], kind="mergesort"
    ).drop_duplicates("file_sha")
    table = table.join(counts, on="file_sha")
    return table.loc[:, columns].reset_index(drop=True)


def build_member_nodes(family_texts: pd.DataFrame, earliest: pd.DataFrame) -> pd.DataFrame:
    """One node per text of the family (registry copies of themselves left out).

    ``family_texts`` has ``file_sha`` and optionally ``registry_self``;
    ``earliest`` is ``earliest_files`` output (it may be indexed by
    ``file_sha``). A text without a file row is
    undated with no repo.
    """
    texts = family_texts
    if "registry_self" in texts.columns:
        texts = texts.loc[~texts["registry_self"].astype(bool)]
    # A lookup indexed by file_sha is used as it is (the run builds it once);
    # otherwise it is indexed here (small tables only).
    lookup = earliest if earliest.index.name == "file_sha" else earliest.set_index("file_sha")
    merged = lookup.reindex(texts["file_sha"].to_numpy()).rename_axis("file_sha").reset_index()
    return pd.DataFrame(
        {
            "node_id": [copy_node_id(sha) for sha in merged["file_sha"]],
            "node_kind": "copy",
            "blob": merged["file_sha"].astype(str).to_numpy(),
            "repo": merged["repo"].fillna("").astype(str).to_numpy(),
            "first_date": _utc_index(merged["first_date"]),
            "files": merged["files"].fillna(0).astype("int64").to_numpy(),
        }
    )


# Parent search (A37)


def score_candidates_simple(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Matching MinHash values divided by the width, one row at a time (the check)."""
    width = len(query)
    return np.asarray(
        [sum(int(a == b) for a, b in zip(row, query, strict=True)) / width for row in matrix],
        dtype=np.float64,
    )


def score_candidates(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Share of MinHash values equal to ``query`` in each row of ``matrix``."""
    if len(matrix) == 0:
        return np.zeros(0, dtype=np.float64)
    return (matrix == query).mean(axis=1)


def _best_candidate(
    blob: str,
    row: int,
    cand_blobs: np.ndarray,
    cand_rows: np.ndarray,
    matrix: np.ndarray,
) -> tuple[int, float]:
    """Position and score of the best candidate (the one rule for a run and for tests).

    The same blob scores 1 (the first such candidate). Otherwise the highest share of
    equal MinHash values wins and a tie keeps the first candidate. A text or candidate
    with no signature scores nothing; if nothing scores, position 0 and NaN.
    """
    same = np.flatnonzero(cand_blobs == blob)
    if len(same):
        return int(same[0]), 1.0
    if row < 0:
        return 0, float("nan")
    scores = np.full(len(cand_blobs), -1.0)
    has = cand_rows >= 0
    if has.any():
        scores[has] = score_candidates(matrix[row], matrix[cand_rows[has]])
    best = int(np.argmax(scores))
    if scores[best] < 0:
        return 0, float("nan")
    return best, float(scores[best])


def pick_parent_for_member(
    member: dict[str, object],
    earlier_nodes: pd.DataFrame,
    signatures: dict[str, object],
    max_earlier: int | None = None,
) -> tuple[str, float]:
    """Best earlier node for one member: ``(node_id, similarity)``.

    ``earlier_nodes`` has ``node_id``, ``blob`` and ``first_date`` in the
    order of preference (earliest first). The same blob scores 1. A tie keeps
    the earliest node. A missing signature on either side scores nothing; if
    nothing scores, the earliest node is kept with similarity NaN. No
    candidate gives ``("", nan)``. ``max_earlier`` keeps the last N rows.
    A run uses the same rule (``_best_candidate``) for every member.
    """
    nodes = earlier_nodes if max_earlier is None else earlier_nodes.tail(max_earlier)
    if nodes.empty:
        return "", float("nan")
    blob = str(member["blob"])
    position, score = _best_candidate(
        blob,
        signatures["row"].get(blob, -1),
        nodes["blob"].astype(str).to_numpy(),
        _signature_rows(nodes, signatures),
        signatures["matrix"],
    )
    return str(nodes["node_id"].iloc[position]), score


def _signature_rows(nodes: pd.DataFrame, signatures: dict[str, object]) -> np.ndarray:
    """Matrix row of each node (registry ids first, then blob), -1 when unsigned."""
    rows = signatures["row"]
    out = np.full(len(nodes), -1, dtype=np.int64)
    for position, (node_id, blob) in enumerate(zip(nodes["node_id"], nodes["blob"], strict=True)):
        out[position] = rows.get(node_id, rows.get(str(blob), -1))
    return out


def _root_position(members: pd.DataFrame) -> int:
    """Row of the root member: the oldest dated text, else the most copied one.

    Ties on date take the smaller node id.
    """
    dated = members["first_date"].notna().to_numpy()
    order = members.assign(_n=np.arange(len(members)))
    if dated.any():
        pool = order.loc[dated].sort_values(["first_date", "node_id"], kind="mergesort")
    else:
        pool = order.sort_values(["files", "node_id"], ascending=[False, True], kind="mergesort")
    return int(pool["_n"].iloc[0])


def build_one_family_tree(
    family_id: str,
    family_texts: pd.DataFrame,
    earliest: pd.DataFrame,
    registry_versions: pd.DataFrame,
    roots: pd.DataFrame,
    signatures: dict[str, object],
    max_earlier: int | None = None,
    tick: Callable[[int, int], None] | None = None,
) -> pd.DataFrame:
    """Trunk, members, parents, generations and ``approx`` of one family.

    ``max_earlier`` (default ``TREE_MAX_EARLIER_MEMBERS``) caps the earlier
    members a text is compared with: the most recent ones, plus every
    registry version released before it. A node that lost candidates has
    ``approx`` true. A text dated before every registry version (and every other
    text) attaches to the earliest registry version. Without a trunk the root
    member has no parent. ``tick(done, total)``
    is called every ``TICK_MEMBERS`` members so a very large family reports progress.
    """
    cap = settings.TREE_MAX_EARLIER_MEMBERS if max_earlier is None else max_earlier
    trunk = build_trunk_nodes(family_id, registry_versions, roots)
    members = build_member_nodes(family_texts, earliest)
    members["family_id"] = family_id
    root_pos = _root_position(members) if len(members) and trunk.empty else -1
    # Order: root first, then dated members by (date, id), then undated by id.
    order = members.assign(_n=np.arange(len(members)))
    order["_rank"] = np.where(
        order["_n"] == root_pos, 0, np.where(order["first_date"].notna(), 1, 2)
    )
    order = order.sort_values(["_rank", "first_date", "node_id"], kind="mergesort")
    members = members.loc[order["_n"].to_numpy()].reset_index(drop=True)
    root_pos = 0 if root_pos >= 0 else -1

    # Registry candidates in release order (stable: a tie keeps the lineage, then version
    # order), so "the earliest node wins" is the earliest release, not the first lineage id.
    trunk_dates = _naive_utc(trunk["first_date"])
    trunk_order = np.argsort(trunk_dates, kind="stable")
    trunk_dates = trunk_dates[trunk_order]
    trunk_ids = trunk["node_id"].to_numpy(dtype=object)[trunk_order]
    trunk_blobs = trunk["blob"].astype(str).to_numpy(dtype=object)[trunk_order]
    trunk_rows = (
        _signature_rows(trunk, signatures)[trunk_order] if len(trunk) else np.zeros(0, np.int64)
    )
    member_rows = _signature_rows(members, signatures)
    matrix = signatures["matrix"]
    member_dates = _naive_utc(members["first_date"])
    dated_pos = np.flatnonzero(~np.isnat(member_dates))
    dated_values = member_dates[dated_pos]

    n = len(members)
    parent_id = [""] * n
    similarity = np.full(n, np.nan)
    generation = np.zeros(n, dtype=np.int64)
    approx = np.zeros(n, dtype=bool)
    member_ids = members["node_id"].to_numpy(dtype=object)
    member_blobs = members["blob"].astype(str).to_numpy(dtype=object)

    for i in range(n):
        if tick is not None and i % TICK_MEMBERS == 0:
            tick(i, n)
        if i == root_pos:
            continue
        date = member_dates[i]
        if np.isnat(date):
            t_idx = np.arange(len(trunk))
            end = len(dated_pos)
        else:
            t_idx = np.flatnonzero(trunk_dates < date)
            end = int(np.searchsorted(dated_values, date, side="left"))
        approx[i] = end > cap
        window = dated_pos[max(0, end - cap) : end]
        window = window[window != i]
        if root_pos == 0 and 0 not in window and i != 0:
            window = np.concatenate([[0], window])
        if len(t_idx) == 0 and len(window) == 0 and len(trunk):
            # Dated before every registry version and before every other text: it hangs
            # on the earliest registry version (its parent is then later than the copy).
            t_idx = np.zeros(1, dtype=np.int64)
        cand_ids = np.concatenate([trunk_ids[t_idx], member_ids[window]])
        if len(cand_ids) == 0:
            continue
        best, score = _best_candidate(
            member_blobs[i],
            int(member_rows[i]),
            np.concatenate([trunk_blobs[t_idx], member_blobs[window]]),
            np.concatenate([trunk_rows[t_idx], member_rows[window]]),
            matrix,
        )
        parent_id[i] = str(cand_ids[best])
        similarity[i] = score
    # Generations: parents come earlier in the processing order, so one pass.
    gen_of = {node: 0 for node in trunk_ids}
    for i in range(n):
        parent = parent_id[i]
        generation[i] = gen_of.get(parent, -1) + 1 if parent else 0
        gen_of[member_ids[i]] = int(generation[i])
    members["parent_id"] = parent_id
    members["parent_similarity"] = similarity
    members["generation"] = generation
    members["approx"] = approx
    return pd.concat([trunk, members.loc[:, TREE_COLUMNS]], ignore_index=True)[TREE_COLUMNS]


def _build_leaf_nodes(texts: pd.DataFrame, earliest: pd.DataFrame) -> pd.DataFrame:
    """One root node per text for singleton and copier-only families, in one frame."""
    merged = texts.loc[:, ["file_sha", "family_id"]].merge(earliest, on="file_sha", how="left")
    return pd.DataFrame(
        {
            "family_id": merged["family_id"].to_numpy(),
            "node_id": ["text:" + sha for sha in merged["file_sha"]],
            "node_kind": "copy",
            "blob": merged["file_sha"].astype(str).to_numpy(),
            "repo": merged["repo"].fillna("").astype(str).to_numpy(),
            "first_date": _utc_index(merged["first_date"]),
            "files": merged["files"].fillna(0).astype("int64").to_numpy(),
            "parent_id": "",
            "parent_similarity": np.nan,
            "generation": 0,
            "approx": False,
        }
    )


# Summary


def summarize_families(
    tree: pd.DataFrame, families: pd.DataFrame, skipped: set[str] | None = None
) -> pd.DataFrame:
    """One row per family: size, depth, mean generation and root basis.

    ``families`` is the stage 7 table (one row per text). Mean generation uses
    copy nodes only. ``status`` is ``built``, ``skipped`` (over the size limit) or
    ``registry_only`` (no copy node: every text is a registry version). Those two
    have no nodes, so their ``root_basis`` is ``none``.
    """
    base = (
        families.groupby("family_id", sort=False)
        .agg(family_kind=("family_kind", "first"), texts=("file_sha", "size"))
        .reset_index()
    )
    # No Python lambdas: a group-by with one per family took over 20 minutes on 1.2M families.
    is_copy = (tree["node_kind"] == "copy").to_numpy()
    work = tree.assign(
        _copy=is_copy,
        _registry=~is_copy,
        _dated=is_copy & tree["first_date"].notna().to_numpy(),
        _gen_copy=np.where(is_copy, tree["generation"], 0),
        _approx=tree["approx"].astype(bool),
    )
    by_family = work.groupby("family_id", sort=False).agg(
        member_nodes=("_copy", "sum"),
        registry_nodes=("_registry", "sum"),
        dated_nodes=("_dated", "sum"),
        gen_sum=("_gen_copy", "sum"),
        depth=("generation", "max"),
        approx=("_approx", "any"),
    )
    by_family["mean_generation"] = by_family["gen_sum"] / by_family["member_nodes"].where(
        by_family["member_nodes"] > 0
    )
    named = tree.loc[is_copy & (tree["repo"] != ""), ["family_id", "repo"]].drop_duplicates()
    by_family["repos"] = named.groupby("family_id", sort=False).size()
    by_family = by_family.drop(columns="gen_sum")
    out = base.merge(by_family, on="family_id", how="left")
    for column in ("member_nodes", "repos", "dated_nodes", "registry_nodes", "depth"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    skipped = skipped or set()
    # skipped: left out by --skip-families-over. registry_only: every text is a registry
    # version itself (registry_self), so the family has no copy node to place.
    out["status"] = np.where(
        out["family_id"].isin(skipped),
        "skipped",
        np.where(out["depth"].notna(), "built", "registry_only"),
    )
    # No nodes (registry_only, skipped) means no root was chosen: "none".
    out["root_basis"] = np.where(
        out["status"] != "built",
        "none",
        np.where(
            out["registry_nodes"].fillna(0) > 0,
            "trunk",
            np.where(
                out["texts"] == 1,
                "single",
                np.where(out["dated_nodes"].fillna(0) > 0, "oldest_dated", "most_copied"),
            ),
        ),
    )
    for column in ("member_nodes", "repos", "dated_nodes", "registry_nodes", "depth"):
        out[column] = out[column].fillna(0).astype("int64")
    out["approx"] = out["approx"].fillna(False).astype(bool)
    return out.sort_values("family_id", kind="mergesort").reset_index(drop=True)


# Inputs


def read_inputs(dev: bool) -> dict[str, pd.DataFrame]:
    """Read families, copy files, versions and roots; a missing file names its builder."""
    folder = settings.data_dir(dev)
    flag = " --dev" if dev else ""
    check_inputs_exist(
        {
            "families": folder / "families.parquet",
            "copy_files": folder / "copy_files.parquet",
            "lineage_versions": folder / "lineage_versions.parquet",
            "lineage_roots": lineage_roots_path(dev),
            "registry_signatures": registry_signatures_path(dev),
            "dataset_signatures": dataset_signatures_dir(),
            "artifacts": settings.ARTIFACTS_GLOB,
        },
        {
            "families": f"python -m skill_genealogy.assign_families{flag}",
            "copy_files": f"python -m skill_genealogy.assign_families{flag}",
            "lineage_versions": f"python -m skill_genealogy.build_registry_lineages{flag}",
            "lineage_roots": f"python -m skill_genealogy.find_copies{flag}",
            "registry_signatures": f"python -m skill_genealogy.find_copies --only signatures{flag}",
            "dataset_signatures": "python -m skill_genealogy.find_copies --only signatures",
            "artifacts": "python src/data/download_data.py",
        },
    )
    return {
        "families": read_parquet(folder / "families.parquet"),
        "copy_files": read_parquet(folder / "copy_files.parquet"),
        "versions": read_parquet(folder / "lineage_versions.parquet"),
        "roots": read_parquet(lineage_roots_path(dev)),
    }


def read_member_dates(dev: bool, wanted: pd.DataFrame) -> pd.DataFrame:
    """Q8.1: files and dataset first-commit dates of the wanted texts (``file_sha`` column)."""
    con = connect_duckdb()
    try:
        temp = settings.data_dir(dev) / "duckdb_temp"
        temp.mkdir(parents=True, exist_ok=True)
        con.execute("SET enable_progress_bar = false")
        con.execute("SET preserve_insertion_order = false")
        con.execute(f"SET memory_limit = '{DUCKDB_MEMORY_LIMIT}'")
        con.execute(f"SET max_temp_directory_size = '{DUCKDB_TEMP_LIMIT}'")
        con.execute(f"SET temp_directory = {_quote_path(str(temp))}")
        glob = _quote_path(settings.ARTIFACTS_GLOB)
        con.execute(f"CREATE VIEW artifacts AS SELECT * FROM read_parquet({glob})")
        con.register("wanted_texts", wanted.loc[:, ["file_sha"]].drop_duplicates())
        return con.execute(DATASET_FILES_SQL).df()
    finally:
        con.close()


def load_tree_signatures(
    dev: bool, file_shas: set[str], progress: Progress | None = None
) -> dict[str, object]:
    """Registry signatures (by ``lineage#seq``) plus the wanted dataset rows (by blob sha).

    Outputs: ``{"row": id -> matrix row, "matrix": uint32 [n, 128]}``.
    """
    ids, signatures, _sizes = load_signatures(registry_signatures_path(dev))
    keys = list(ids)
    blocks = [signatures.astype(np.uint32)]
    wanted = pd.Index(sorted(file_shas))
    parts = _signature_files(dataset_signatures_dir())
    for number, path in enumerate(parts, start=1):
        if progress is not None and (number % 10 == 0 or number == len(parts)):
            progress.stage("signatures", f"part {number}/{len(parts)}")
        part_ids, part_signatures, _ = _load_one(path)
        keep = np.flatnonzero(pd.Index(part_ids).isin(wanted))
        keys.extend(part_ids[row] for row in keep)
        blocks.append(part_signatures[keep].astype(np.uint32))
    row: dict[str, int] = {}
    for position, key in enumerate(keys):
        row.setdefault(str(key), position)
    return {"row": row, "matrix": np.vstack(blocks)}


def save_outputs(tree: pd.DataFrame, summary: pd.DataFrame, dev: bool) -> None:
    """Write both parquet files, sorted so a rerun gives the same files.

    Each file is written under a temporary name and then moved into place, so a
    failed write never leaves a half file or removes the previous output.
    """
    pairs = [
        (tree, family_tree_path(dev), ["family_id", "generation", "first_date", "node_id"]),
        (summary, family_summary_path(dev), ["family_id"]),
    ]
    temps = []
    try:
        for frame, path, sort_by in pairs:
            temp = path.with_name(path.name + ".tmp")
            temps.append(temp)
            write_parquet(frame, temp, sort_by)
        for (_frame, path, _sort), temp in zip(pairs, temps, strict=True):
            temp.replace(path)
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)


# Build and run


def build_trees(
    dev: bool, skip_over: int | None = None, progress: Progress | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build every family tree; return ``(tree, summary)``."""
    progress = progress or Progress(dev)
    progress.stage("inputs", "reading stage 7 tables")
    tables = read_inputs(dev)
    families, copy_files = tables["families"], tables["copy_files"]
    versions, roots = tables["versions"], tables["roots"]
    # Q8.1: files of every text; copy files already carry history-based dates.
    copy_part = copy_files.loc[:, ["repo", "path", "file_sha", "first_commit_date"]]
    other = families.loc[~families["file_sha"].isin(set(copy_files["file_sha"])), ["file_sha"]]
    progress.stage(
        "dates", f"reading dataset first_commit_at of {len(other)} texts (one DuckDB scan)"
    )
    dataset_part = read_member_dates(dev, other)
    progress.stage("earliest", f"{len(dataset_part)} dataset files, {len(copy_part)} copy files")
    earliest = earliest_files(pd.concat([copy_part, dataset_part], ignore_index=True))
    # Indexed once: a per-family merge against 1.6M rows made the first run 18 texts/s.
    lookup = earliest.set_index("file_sha")

    sizes = families.groupby("family_id").size()
    skipped = set(sizes.index[sizes > skip_over]) if skip_over else set()
    # A skipped family gets no nodes at all, leaf kinds included (the summary says skipped).
    leaf = (
        families["family_kind"].isin(LEAF_KINDS)
        & ~families["registry_self"].astype(bool)
        & ~families["family_id"].isin(skipped)
    )
    wanted = families.loc[~leaf & ~families["registry_self"].astype(bool)]
    wanted = wanted.loc[~wanted["family_id"].isin(skipped)]
    progress.stage("signatures", f"loading for {wanted['file_sha'].nunique()} texts")
    signatures = load_tree_signatures(dev, set(wanted["file_sha"]), progress)
    progress.stage("signatures", f"{len(signatures['row'])} rows loaded")

    root_of = roots.set_index("lineage_id")["root_lineage"]
    versions = versions.assign(root_lineage=versions["lineage_id"].map(root_of))
    versions_by = {k: v for k, v in versions.groupby("root_lineage")}
    roots_by = {k: v for k, v in roots.groupby("root_lineage")}

    progress.stage("leaves", f"{int(leaf.sum())} singleton and copier-only texts")
    frames = [_build_leaf_nodes(families.loc[leaf], earliest)]
    empty_versions, empty_roots = versions.iloc[0:0], roots.iloc[0:0]
    groups = list(wanted.groupby("family_id", sort=True))
    texts_total, texts_done = len(wanted), 0
    progress.stage("trees", f"{len(groups)} families, {texts_total} texts")
    progress.state["trees_started"] = progress._elapsed()
    for number, (family_id, texts) in enumerate(groups, start=1):

        def tick(
            done: int,
            total: int,
            name: str = family_id,
            before: int = texts_done,
            index: int = number,
        ) -> None:
            progress.trees(index - 1, len(groups), before + done, texts_total, name, done, total)

        frames.append(
            build_one_family_tree(
                family_id,
                texts,
                lookup,
                versions_by.get(family_id, empty_versions),
                roots_by.get(family_id, empty_roots),
                signatures,
                tick=tick,
            )
        )
        texts_done += len(texts)
        progress.trees(number, len(groups), texts_done, texts_total)
    progress.trees(len(groups), len(groups), texts_done, texts_total, force=True)
    progress.stage("combine", f"{len(frames)} frames")
    tree = pd.concat(frames, ignore_index=True)[TREE_COLUMNS]
    progress.stage("summary", f"{len(tree)} nodes")
    return tree, summarize_families(tree, families, skipped)


def print_run_summary(tree: pd.DataFrame, summary: pd.DataFrame, seconds: float) -> None:
    """Print node and family counts, depth figures and two sample rows of each output."""
    built = summary.loc[summary["status"] == "built"]
    multi = built.loc[built["texts"] > 1]
    print_summary(
        "stage 8 family trees",
        {
            "seconds": round(seconds, 1),
            "tree nodes": len(tree),
            "families": len(summary),
            "families by kind": summary["family_kind"].value_counts().to_dict(),
            "skipped families": int((summary["status"] == "skipped").sum()),
            "approx families": int(summary["approx"].sum()),
            "median texts (size>1)": float(multi["texts"].median()) if len(multi) else 0,
            "depth median / p90 / max": (
                float(multi["depth"].median()) if len(multi) else 0,
                float(multi["depth"].quantile(0.9)) if len(multi) else 0,
                int(multi["depth"].max()) if len(multi) else 0,
            ),
            "dated copy nodes": int(
                tree.loc[tree["node_kind"] == "copy", "first_date"].notna().sum()
            ),
        },
    )
    print_sample_records(tree.loc[tree["parent_id"] != ""], "tree sample")
    print_sample_records(summary, "summary sample")


def run(dev: bool, force: bool, skip_over: int | None = None) -> None:
    """Skip when both outputs exist (unless ``force``), else build, save and print.

    ``force`` keeps the old files until the new build has finished, so a failed
    run leaves the previous outputs in place.
    """
    outputs = list(output_paths(dev).values())
    if outputs_exist(outputs) and not force:
        print("skip: stage 8 outputs exist (use --force to rebuild)", flush=True)
        return
    if force:
        print("force: the old outputs stay until the new ones are built", flush=True)
    started = time.perf_counter()
    progress = Progress(dev)
    tree, summary = build_trees(dev, skip_over, progress)
    progress.stage("save", "writing family_tree.parquet and family_summary.parquet")
    save_outputs(tree, summary, dev)
    progress.finish()
    print_run_summary(tree, summary, time.perf_counter() - started)


def main() -> None:
    """Run stage 8 from the command line (``--dev``, ``--force``, ``--skip-families-over N``)."""
    parser = build_arg_parser("Stage 8: one tree per family, parent = closest earlier text.")
    parser.add_argument(
        "--skip-families-over",
        type=int,
        default=None,
        help="leave out families with more than N texts (kept in the summary as skipped)",
    )
    args = parser.parse_args()
    run(bool(args.dev), bool(args.force), args.skip_families_over)


if __name__ == "__main__":
    main()
