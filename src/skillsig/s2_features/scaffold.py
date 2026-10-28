"""
STAGE: s2_features
MODULE: scaffold
PURPOSE: Structural templating signals: placeholders, heading outlines, front-matter keys,
         folder layout, name-masked bodies, and the shared-line (boilerplate) fraction.
INPUTS: artifacts primary rows (T02 readable filter), all artifact rows (owners), artifact_siblings
OUTPUTS: src/data/derived/s2_scaffold.parquet, results/s2_scaffold_top_outlines.csv
RQ LINK: RQ1 (highly templated = shared scaffold with little customization, not only copies)

The copy, cluster and registry rules compare body text, so they miss skills generated from one
scaffold and then filled with new wording. Signals here compare structure instead:
- A placeholder residue (regex lexicon; generator markers from anthropics/skills
  skill-creator/scripts/init_skill.py at commit 1ed29a03dc852d30fa6ef2ca53a67dc2c2c2c563, the
  last version before the script was removed),
- B heading outline (canonicalised, name-masked, hashed) and B2 outline families (MinHash LSH
  candidates verified by heading-sequence edit distance, leader clustering per bucket),
- C ordered front-matter keys, D top-level folder layout,
- E name-masked body hash (Type-2 clone), F shared-line fraction (line document frequency,
  as in site-level template detection: Vieira et al. 2006; Kohlschuetter et al. 2010).
Spread is counted in distinct owners, so one author's house style is not a template.
`winnow` (Schleimer, Wilkerson & Aiken 2003) gives shared-passage evidence for the spot check.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_intake.devsample import output_path as devsample_path
from skillsig.s2_features.minhash_cluster import (
    _READABLE,
    _band_keys,
    _band_params,
    _find,
    _group_spans,
    _sketch,
)
from skillsig.s2_features.structure import split_front_matter

log = logging.getLogger(__name__)

BASES = ("generator", "placeholder", "masked_copy", "shared_lines", "outline")

GENERATORS: dict[str, tuple[str, ...]] = {
    "skill-creator": (
        "[todo: complete and informative explanation of what the skill does",
        "[todo: 1-2 sentences explaining what this skill enables]",
        "## structuring this skill",
        "[todo: replace with the first main section based on chosen structure]",
        'delete this entire "structuring this skill" section when done',
        "this skill includes example resource directories that demonstrate how to organize",
        "any unneeded directories can be deleted.",
    ),
}
# Markers of unfinished authoring only. Dev-sample review dropped {{var}}, <skill-name>,
# [your ...] and "replace with": in most hits they are runtime parameters the agent or user is
# meant to fill (e.g. {{gsd_args}}, skills/<skill-name>/SKILL.md), not an unfilled scaffold.
_PLACEHOLDERS = (
    re.compile(r"\[\s*(?:todo|tbd|fixme|placeholder)\b[^\]\n]*\](?!\()", re.IGNORECASE),
    re.compile(r"(?m)^[ \t]*(?:[-*+][ \t]+)?(?:todo|tbd|fixme)[ \t]*:", re.IGNORECASE),
    re.compile(r"<\s*placeholder\s*>", re.IGNORECASE),
    re.compile(
        r"\b(?:replace this\b|lorem ipsum|skill-name-here|your-skill-name|my-skill-name)",
        re.IGNORECASE,
    ),
)
_FENCE = re.compile(r"^[ \t]*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_NON_WORD = re.compile(r"[^a-z0-9<> ]+")
# Applied after _mask, so digits are already <n>.
_NUMBERING = re.compile(r"^(?:(?:step|phase|part)\s+)?<n>(?:\s+<n>)*(?:\s+|$)")
_MASK_TOKEN = re.compile(r"<\w+>")
_URL = re.compile(r"https?://\S+|www\.\S+")
_PATH = re.compile(r"(?:\.{1,2}/|~/)[\w./-]+")
_NUMBER = re.compile(r"\b\d+(?:\.\d+)*\b")
_WS = re.compile(r"\s+")
_WORD = re.compile(r"\w+")
_HAS_LETTER = re.compile(r"[a-z]")
_LINE_MARKUP = re.compile(r"^(?:#{1,6}|[-*+]|\d+[.)]|>|\|)\s*")
_GENERIC_FM = frozenset({"name", "description"})
_MIN_LINES = 3  # shared_lines needs at least this many distinct lines
_TOP_OUTLINES = 200
_PASSAGES = 3
_PASSAGE_CHARS = 200

_DOWNLOAD = "python src/data/download_data.py"
_DEV_SAMPLE = "python -m skillsig data --dev-sample"

_FEATURE_SCHEMA = pa.schema(
    [
        pa.field("file_sha", pa.string()),
        pa.field("owner", pa.string()),
        pa.field("placeholder_count", pa.int32()),
        pa.field("placeholder_generator", pa.string()),
        pa.field("outline_key", pa.string()),
        pa.field("outline_text", pa.string()),
        pa.field("outline_len", pa.int32()),
        pa.field("fm_key", pa.string()),
        pa.field("masked_sha", pa.string()),
        pa.field("line_count", pa.int32()),
    ]
)
_LINE_SCHEMA = pa.schema(
    [
        pa.field("file_sha", pa.string()),
        pa.field("owner", pa.string()),
        pa.field("line_hash", pa.int64()),
    ]
)
_OUTPUT_SCHEMA = pa.schema(
    [
        pa.field("file_sha", pa.string()),
        pa.field("owner", pa.string()),
        pa.field("placeholder_count", pa.int32()),
        pa.field("placeholder_generator", pa.string()),
        pa.field("outline_key", pa.string()),
        pa.field("outline_len", pa.int32()),
        pa.field("outline_family_id", pa.int64()),
        pa.field("outline_family_owners", pa.int64()),
        pa.field("fm_key", pa.string()),
        pa.field("fm_key_owners", pa.int64()),
        pa.field("layout_key", pa.string()),
        pa.field("layout_owners", pa.int64()),
        pa.field("masked_sha", pa.string()),
        pa.field("masked_owners", pa.int64()),
        pa.field("line_count", pa.int32()),
        pa.field("shared_line_frac", pa.float64()),
        pa.field("customization", pa.float64()),
        pa.field("scaffold_basis", pa.string()),
    ]
)


def placeholders(text: str) -> tuple[int, str | None]:
    """(generic placeholder hits outside code fences, first matching generator or None)."""
    lowered = text.lower()
    generator = next(
        (name for name, markers in GENERATORS.items() if any(m in lowered for m in markers)),
        None,
    )
    prose = "\n".join(_outside_fences(text.splitlines()))
    return sum(len(p.findall(prose)) for p in _PLACEHOLDERS), generator


def outline(body: str, name: str | None = None) -> list[str]:
    """Canonical heading list, e.g. ["2:overview", "2:when to use"]; the skill name -> <name>."""
    names = _name_patterns([name])
    items: list[str] = []
    for line in _outside_fences(body.splitlines()):
        match = _HEADING.match(line)
        if match is None:
            continue
        text = _canonical_heading(match.group(2), names)
        if text:
            items.append(f"{len(match.group(1))}:{text}")
    return items


def outline_key(items: Sequence[str]) -> str | None:
    return _digest("|".join(items)) if items else None


def fm_key(front: Mapping[str, Any]) -> str | None:
    """Ordered front-matter keys, e.g. "name|description|license"; None if none parsed."""
    keys = [str(key).strip().lower() for key in front]
    return "|".join(keys) if keys else None


def masked_sha(body: str, name: str | None = None, repo: str | None = None) -> str | None:
    """Hash of the body with names, repo, URLs, paths and numbers masked (Type-2 clone key)."""
    masked = _mask(body.lower(), _name_patterns([name, *_repo_parts(repo)]))
    collapsed = _WS.sub(" ", masked).strip()
    return _digest(collapsed) if collapsed else None


def line_hashes(
    body: str, cfg: Config, name: str | None = None, repo: str | None = None
) -> list[int]:
    """Distinct signed 64-bit hashes of normalised body lines >= scaffold.line_min_chars."""
    names = _name_patterns([name, *_repo_parts(repo)])
    minimum = int(cfg.scaffold.line_min_chars)
    seen: set[int] = set()
    for raw in body.splitlines():
        if _FENCE.match(raw):
            continue
        line = _LINE_MARKUP.sub("", _WS.sub(" ", raw.lower()).strip())
        line = _mask(line, names).strip()
        if len(line) < minimum or not _HAS_LETTER.search(line):
            continue
        seen.add(_hash64(line))
    return sorted(seen)


def layout_key(entries: Iterable[tuple[str, str]]) -> str | None:
    """Sorted top-level sibling names ("scripts/", "references/", "readme.md"); None if empty."""
    names = {_layout_name(entry, kind) for entry, kind in entries}
    names.discard("skill.md")
    return "|".join(sorted(names)) if names else None


def outline_distance(a: Sequence[str], b: Sequence[str]) -> int:
    """Levenshtein distance over heading sequences (each heading is one token)."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, start=1):
        current = [i]
        for j, right in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (left != right))
            )
        previous = current
    return previous[-1]


def outline_similarity(a: Sequence[str], b: Sequence[str]) -> float:
    longest = max(len(a), len(b))
    return 1.0 if longest == 0 else 1.0 - outline_distance(a, b) / longest


def outline_families(outlines: Sequence[Sequence[str]], cfg: Config) -> np.ndarray:
    """Family label (smallest member index) per outline.

    Candidates come from MinHash LSH over each outline's heading set. Within a bucket, leader
    clustering verifies members against the bucket's leaders by sequence edit distance and
    joins the first leader with similarity >= outline_sim_min. Buckets larger than
    outline_bucket_max are skipped. Outlines shorter than outline_len_min stay alone.
    """
    count = len(outlines)
    parent = np.arange(count, dtype=np.int64)
    eligible = [i for i, items in enumerate(outlines) if len(items) >= cfg.scaffold.outline_len_min]
    if len(eligible) < 2:
        return parent
    num_perm = int(cfg.minhash.num_perm)
    sigs = np.stack([_sketch(list(outlines[i]), num_perm, cfg.seed) for i in eligible])
    index = np.asarray(eligible, dtype=np.int64)
    bands, width = _band_params(float(cfg.scaffold.outline_lsh_threshold), num_perm)
    minimum = float(cfg.scaffold.outline_sim_min)
    largest = int(cfg.scaffold.outline_bucket_max)
    for band in range(bands):
        order, starts, ends = _group_spans(_band_keys([sigs], band * width, (band + 1) * width))
        for start, end in zip(starts, ends, strict=True):
            size = int(end - start)
            if size < 2 or size > largest:
                continue
            members = np.sort(index[order[int(start) : int(end)]])
            _leader_join(outlines, members, parent, minimum)
    return np.array([_find(parent, i) for i in range(count)], dtype=np.int64)


def winnow(tokens: Sequence[str], k: int, w: int) -> list[tuple[int, int]]:
    """Winnowing fingerprints (hash, position): min hash per window of w k-grams, rightmost tie.

    Any shared run of at least k + w - 1 tokens yields at least one shared fingerprint.
    """
    if k < 1 or w < 1:
        raise ValueError(f"k and w must be >= 1, got k={k}, w={w}")
    grams = [_hash64(" ".join(tokens[i : i + k])) for i in range(len(tokens) - k + 1)]
    if not grams:
        return []
    if len(grams) < w:
        best = min(range(len(grams)), key=lambda i: (grams[i], -i))
        return [(grams[best], best)]
    picked: list[tuple[int, int]] = []
    last = -1
    for start in range(len(grams) - w + 1):
        window = range(start, start + w)
        best = min(window, key=lambda i: (grams[i], -i))
        if best != last:
            picked.append((grams[best], best))
            last = best
    return picked


def shared_passages(text_a: str, text_b: str, cfg: Config) -> tuple[float, list[str]]:
    """(shared fingerprints / smaller fingerprint set, up to 3 longest shared passages of a)."""
    k, w = int(cfg.scaffold.winnow_k), int(cfg.scaffold.winnow_w)
    tokens_a = _WORD.findall(text_a.lower())
    tokens_b = _WORD.findall(text_b.lower())
    prints_a = winnow(tokens_a, k, w)
    hashes_a = {h for h, _ in prints_a}
    hashes_b = {h for h, _ in winnow(tokens_b, k, w)}
    if not hashes_a or not hashes_b:
        return 0.0, []
    common = hashes_a & hashes_b
    overlap = len(common) / min(len(hashes_a), len(hashes_b))
    spans: list[list[int]] = []
    for h, pos in sorted(prints_a, key=lambda item: item[1]):
        if h not in common:
            continue
        if spans and pos <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], pos + k)
        else:
            spans.append([pos, pos + k])
    spans.sort(key=lambda span: (span[0] - span[1], span[0]))
    passages = [" ".join(tokens_a[s:e])[:_PASSAGE_CHARS] for s, e in spans[:_PASSAGES]]
    return overlap, passages


def skill_signals(
    text: str, cfg: Config, *, repo: str | None = None
) -> tuple[dict[str, Any], list[int]]:
    """Per-skill columns of the feature table plus the skill's line hashes."""
    front, body, _valid = split_front_matter(text)
    name = front.get("name")
    name = name if isinstance(name, str) else None
    count, generator = placeholders(text)
    items = outline(body, name)
    lines = line_hashes(body, cfg, name, repo)
    return (
        {
            "placeholder_count": count,
            "placeholder_generator": generator,
            "outline_key": outline_key(items),
            "outline_text": "|".join(items) if items else None,
            "outline_len": len(items),
            "fm_key": fm_key(front),
            "masked_sha": masked_sha(body, name, repo),
            "line_count": len(lines),
        },
        lines,
    )


def scaffold_basis(feat: Mapping[str, Any], cfg: Config) -> str | None:
    """First match wins: generator, placeholder, masked_copy, shared_lines, outline."""
    sc = cfg.scaffold
    if _text(feat.get("placeholder_generator")):
        return "generator"
    if _num(feat.get("placeholder_count")) >= sc.placeholder_min:
        return "placeholder"
    if _num(feat.get("masked_owners")) >= cfg.thresholds.templated_copies_min:
        return "masked_copy"
    frac = feat.get("shared_line_frac")
    if (
        _num(feat.get("line_count")) >= _MIN_LINES
        and frac is not None
        and not pd.isna(frac)
        and float(frac) >= sc.shared_frac_min
    ):
        return "shared_lines"
    if _outline_rule(feat, cfg):
        return "outline"
    return None


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write s2_scaffold.parquet and the top-outline review table. Dev mode uses the dev sample."""
    outputs = _output_paths(cfg)
    inputs = _inputs(cfg, dev=dev)
    _require_inputs(cfg, dev=dev)
    if not force and all(inputs_unchanged(path, cfg=cfg, inputs=inputs) for path in outputs):
        log.info("scaffold features up to date: %s", outputs[0])
        return StageResult(
            stage="s2_features", outputs=outputs, skipped=True, rows=_saved_rows(outputs)
        )
    parts = _parts_dir(cfg)
    _clear_dir(parts)
    shards = _write_parts(cfg, parts, dev=dev)
    frame, top = _aggregate(cfg, parts, shards, dev=dev)
    _write_table(
        pa.Table.from_pandas(frame, schema=_OUTPUT_SCHEMA, preserve_index=False), outputs[0]
    )
    outputs[1].parent.mkdir(parents=True, exist_ok=True)
    top.to_csv(outputs[1], index=False)
    for path, count in zip(outputs, (len(frame), len(top)), strict=True):
        write_manifest(path, cfg=cfg, inputs=inputs, rows=count)
    counts = frame["scaffold_basis"].value_counts(dropna=False).to_dict()
    log.info("wrote %s (%s rows); bases %s", outputs[0], len(frame), counts)
    return StageResult(
        stage="s2_features",
        outputs=outputs,
        skipped=False,
        rows={"s2_scaffold": len(frame), "s2_scaffold_top_outlines": len(top)},
    )


def _outline_rule(feat: Mapping[str, Any], cfg: Config) -> bool:
    sc = cfg.scaffold
    if _num(feat.get("outline_len")) < sc.outline_len_min:
        return False
    if feat.get("outline_key") in sc.outline_stoplist:
        return False
    if _num(feat.get("outline_family_owners")) < sc.owners_min:
        return False
    keys = _text(feat.get("fm_key"))
    fm_support = (
        bool(set(keys.split("|")) - _GENERIC_FM)
        and _num(feat.get("fm_key_owners")) >= sc.owners_min
    )
    layout_support = (
        bool(_text(feat.get("layout_key"))) and _num(feat.get("layout_owners")) >= sc.owners_min
    )
    return fm_support or layout_support


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _leader_join(
    outlines: Sequence[Sequence[str]], members: np.ndarray, parent: np.ndarray, minimum: float
) -> None:
    leaders: list[int] = []
    for member in members.tolist():
        root = _find(parent, member)
        joined = False
        for leader in leaders:
            if _find(parent, leader) == root:
                joined = True
                break
            if outline_similarity(outlines[member], outlines[leader]) >= minimum:
                _union(parent, leader, member)
                joined = True
                break
        if not joined:
            leaders.append(member)


def _union(parent: np.ndarray, a: int, b: int) -> None:
    ra, rb = _find(parent, a), _find(parent, b)
    if ra != rb:
        parent[max(ra, rb)] = min(ra, rb)


def _outside_fences(lines: Iterable[str]) -> Iterable[str]:
    inside = False
    for line in lines:
        if _FENCE.match(line):
            inside = not inside
            continue
        if not inside:
            yield line


def _canonical_heading(text: str, names: list[re.Pattern[str]]) -> str:
    text = _mask(_LINK.sub(r"\1", text.lower()), names)
    text = _WS.sub(" ", _NON_WORD.sub(" ", text)).strip()
    text = _NUMBERING.sub("", text).strip()
    # A heading that is only a number or only masked tokens carries no structure.
    return text if _HAS_LETTER.search(_MASK_TOKEN.sub("", text)) or "<name>" in text else ""


def _name_patterns(values: Iterable[str | None]) -> list[re.Pattern[str]]:
    patterns = []
    for value in values:
        if not value:
            continue
        tokens = [t for t in re.split(r"[\s_-]+", value.lower().strip()) if t]
        if not tokens or len("".join(tokens)) < 3:
            continue
        patterns.append(re.compile(r"\b" + r"[\s_-]*".join(map(re.escape, tokens)) + r"\b"))
    return patterns


def _repo_parts(repo: str | None) -> list[str]:
    return [part for part in (repo or "").split("/") if part]


def _mask(text: str, names: list[re.Pattern[str]]) -> str:
    text = _URL.sub(" <url> ", text)
    text = _PATH.sub(" <path> ", text)
    for pattern in names:
        text = pattern.sub("<name>", text)
    return _NUMBER.sub("<n>", text)


def _layout_name(entry: str, kind: str) -> str:
    entry = entry.strip().lower()
    head, sep, _rest = entry.partition("/")
    return f"{head}/" if sep or kind == "dir" else head


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


def _hash64(text: str) -> int:
    raw = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(raw, "little", signed=True)


def _num(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if np.isnan(number) else number


def _write_parts(cfg: Config, parts: Path, *, dev: bool) -> int:
    """Pass 1: per-skill signals and line hashes, one parquet pair per chunk."""
    con = duckdb.connect()
    try:
        cursor = con.execute(_population_sql(cfg, dev=dev))
        index = 0
        while batch := cursor.fetchmany(int(cfg.chunk_rows)):
            features, lines = [], {"file_sha": [], "owner": [], "line_hash": []}
            for sha, text, repo in batch:
                owner = _owner(repo)
                row, hashes = skill_signals(text if isinstance(text, str) else "", cfg, repo=repo)
                features.append({"file_sha": str(sha), "owner": owner, **row})
                lines["file_sha"].extend([str(sha)] * len(hashes))
                lines["owner"].extend([owner] * len(hashes))
                lines["line_hash"].extend(hashes)
            pq.write_table(
                pa.Table.from_pylist(features, schema=_FEATURE_SCHEMA),
                parts / f"features_{index:05d}.parquet",
            )
            pq.write_table(
                pa.Table.from_pydict(lines, schema=_LINE_SCHEMA),
                parts / f"lines_{index:05d}.parquet",
            )
            log.info("scaffold part %s (%s skills)", index, len(features))
            index += 1
    finally:
        con.close()
    return index


def _owner(repo: Any) -> str:
    return str(repo or "").split("/", 1)[0].lower()


def _population_sql(cfg: Config, *, dev: bool) -> str:
    glob = _sql_string(_artifacts_dir(cfg) / "*.parquet")
    where = _READABLE.strip()
    if dev:
        where += f"\nAND {_dev_filter(cfg)}"
    return f"""
        SELECT file_sha, any_value(content) AS content, min(repo_full_name) AS repo_full_name
        FROM read_parquet({glob})
        WHERE {where}
        GROUP BY file_sha
        ORDER BY file_sha
    """


def _dev_filter(cfg: Config) -> str:
    return f"file_sha IN (SELECT file_sha FROM read_parquet({_sql_string(devsample_path(cfg))}))"


def _aggregate(
    cfg: Config, parts: Path, shards: int, *, dev: bool
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pass 2: owner counts per key, line document frequency, outline families; pass 3: flag."""
    if shards == 0:
        empty = pd.DataFrame({f.name: pd.Series(dtype=object) for f in _OUTPUT_SCHEMA})
        return empty, _empty_top()
    con = duckdb.connect()
    try:
        feats = _sql_string(parts / "features_*.parquet")
        lines = _sql_string(parts / "lines_*.parquet")
        con.execute(f"CREATE TEMP TABLE feats AS SELECT * FROM read_parquet({feats})")
        con.execute(_layout_sql(cfg, dev=dev))
        distinct = con.execute(
            """
            SELECT outline_key, any_value(outline_text) AS outline_text,
                   any_value(outline_len) AS outline_len,
                   count(DISTINCT owner) AS owners, count(*) AS skills,
                   min(file_sha) AS example_file_sha
            FROM feats WHERE outline_key IS NOT NULL
            GROUP BY outline_key ORDER BY outline_key
            """
        ).fetchdf()
        labels = outline_families([text.split("|") for text in distinct["outline_text"]], cfg)
        families = pd.DataFrame(
            {"outline_key": distinct["outline_key"], "outline_family_id": labels.astype("int64")}
        )
        con.register("families_df", families)
        con.execute("CREATE TEMP TABLE families AS SELECT * FROM families_df")
        line_min = int(cfg.scaffold.line_owners_min)
        frame = con.execute(
            f"""
            WITH df AS (
                SELECT line_hash, count(DISTINCT owner) AS owners
                FROM read_parquet({lines}) GROUP BY line_hash
            ),
            shared AS (
                SELECT l.file_sha,
                       avg(CASE WHEN df.owners >= {line_min} THEN 1.0 ELSE 0.0 END) AS frac
                FROM read_parquet({lines}) l JOIN df USING (line_hash)
                GROUP BY l.file_sha
            ),
            fam AS (
                SELECT fa.outline_family_id, count(DISTINCT f.owner) AS owners
                FROM feats f JOIN families fa USING (outline_key)
                GROUP BY fa.outline_family_id
            ),
            fmc AS (SELECT fm_key, count(DISTINCT owner) AS owners FROM feats
                    WHERE fm_key IS NOT NULL GROUP BY fm_key),
            msk AS (SELECT masked_sha, count(DISTINCT owner) AS owners FROM feats
                    WHERE masked_sha IS NOT NULL GROUP BY masked_sha),
            lay AS (
                SELECT f.file_sha, l.layout_key FROM feats f JOIN layouts l USING (file_sha)
            ),
            layc AS (
                SELECT lay.layout_key, count(DISTINCT f.owner) AS owners
                FROM lay JOIN feats f USING (file_sha) GROUP BY lay.layout_key
            )
            SELECT f.file_sha, f.owner, f.placeholder_count, f.placeholder_generator,
                   f.outline_key, f.outline_len, fa.outline_family_id,
                   fam.owners AS outline_family_owners,
                   f.fm_key, fmc.owners AS fm_key_owners,
                   lay.layout_key, layc.owners AS layout_owners,
                   f.masked_sha, msk.owners AS masked_owners,
                   f.line_count,
                   CASE WHEN f.line_count > 0 THEN coalesce(shared.frac, 0.0) END
                       AS shared_line_frac
            FROM feats f
            LEFT JOIN families fa USING (outline_key)
            LEFT JOIN fam ON fam.outline_family_id = fa.outline_family_id
            LEFT JOIN fmc USING (fm_key)
            LEFT JOIN msk USING (masked_sha)
            LEFT JOIN lay USING (file_sha)
            LEFT JOIN layc ON layc.layout_key = lay.layout_key
            LEFT JOIN shared USING (file_sha)
            ORDER BY f.file_sha
            """
        ).fetchdf()
    finally:
        con.close()
    frame["customization"] = 1.0 - frame["shared_line_frac"]
    frame["scaffold_basis"] = [
        scaffold_basis(feat, cfg) for feat in frame.to_dict(orient="records")
    ]
    fam_owners = (
        frame.loc[frame["outline_key"].notna(), ["outline_key", "outline_family_owners"]]
        .drop_duplicates("outline_key")
        .rename(columns={"outline_family_owners": "family_owners"})
    )
    return frame, _top_outlines(distinct, families, fam_owners)


def _layout_sql(cfg: Config, *, dev: bool) -> str:
    """Temp table layouts(file_sha, layout_key) over the representative row's folder."""
    artifacts = _sql_string(_artifacts_dir(cfg) / "*.parquet")
    siblings = _sql_string(_siblings_dir(cfg) / "*.parquet")
    keep = ""
    if dev:
        keep = f"AND {_dev_filter(cfg)}"
    return f"""
        CREATE TEMP TABLE layouts AS
        WITH rep AS (
            SELECT file_sha, min(repo_full_name) AS repo_full_name, min(path) AS path
            FROM read_parquet({artifacts})
            WHERE dedup_primary = 1 AND composition_fetched = 1 {keep}
              AND file_sha IN (SELECT file_sha FROM feats)
            GROUP BY file_sha
        ),
        names AS (
            SELECT rep.file_sha,
                   CASE WHEN strpos(s.entry_name, '/') > 0 OR s.entry_type = 'dir'
                        THEN lower(split_part(s.entry_name, '/', 1)) || '/'
                        ELSE lower(s.entry_name) END AS entry
            FROM rep JOIN read_parquet({siblings}) s
              ON s.repo_full_name = rep.repo_full_name AND s.artifact_path = rep.path
        )
        SELECT file_sha, array_to_string(list_sort(list_distinct(list(entry))), '|') AS layout_key
        FROM names WHERE entry <> 'skill.md'
        GROUP BY file_sha
    """


def _top_outlines(
    distinct: pd.DataFrame, families: pd.DataFrame, fam_owners: pd.DataFrame
) -> pd.DataFrame:
    if distinct.empty:
        return _empty_top()
    top = distinct.merge(families, on="outline_key").merge(fam_owners, on="outline_key")
    top = top.sort_values(["owners", "skills", "outline_key"], ascending=[False, False, True])
    columns = [
        "outline_key",
        "outline_text",
        "outline_len",
        "owners",
        "skills",
        "outline_family_id",
        "family_owners",
        "example_file_sha",
    ]
    return top.head(_TOP_OUTLINES)[columns].reset_index(drop=True)


def _empty_top() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "outline_key",
            "outline_text",
            "outline_len",
            "owners",
            "skills",
            "outline_family_id",
            "family_owners",
            "example_file_sha",
        ]
    )


def _output_paths(cfg: Config) -> list[Path]:
    return [
        resolve(cfg, cfg.paths.derived) / "s2_scaffold.parquet",
        resolve(cfg, cfg.paths.results) / "s2_scaffold_top_outlines.csv",
    ]


def _parts_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s2_scaffold_parts"


def _artifacts_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "artifacts"


def _siblings_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "artifact_siblings"


def _inputs(cfg: Config, *, dev: bool) -> list[Path]:
    paths = [_artifacts_dir(cfg), _siblings_dir(cfg)]
    if dev:
        paths.append(devsample_path(cfg))
    return paths


def _require_inputs(cfg: Config, *, dev: bool) -> None:
    for directory in (_artifacts_dir(cfg), _siblings_dir(cfg)):
        if not directory.is_dir() or not any(directory.glob("*.parquet")):
            raise FileNotFoundError(f"missing {directory}; run: {_DOWNLOAD}")
    if dev and not devsample_path(cfg).is_file():
        raise FileNotFoundError(f"missing {devsample_path(cfg)}; run: {_DEV_SAMPLE}")


def _clear_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for child in directory.iterdir():
        if child.is_file():
            child.unlink()


def _write_table(table: pa.Table, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    pq.write_table(table, temporary, compression="zstd")
    temporary.replace(output)


def _saved_rows(outputs: list[Path]) -> dict[str, int]:
    names = ("s2_scaffold", "s2_scaffold_top_outlines")
    return {
        name: int(json.loads(manifest_path(path).read_text(encoding="utf-8"))["rows"])
        for name, path in zip(names, outputs, strict=True)
    }


def _sql_string(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"
