"""
STAGE: s2_features
MODULE: wording
PURPOSE: Compute wording features W1-W12 and description idiom for readable skills.
INPUTS: artifacts primary rows (file_sha, content, description) with the T02 readable filter
OUTPUTS: src/data/derived/s2_wording.parquet
RQ LINK: RQ1 (wording: imperatives, address, commands, grade, diversity, emphasis, burstiness)
"""

from __future__ import annotations

import json
import logging
import math
import multiprocessing
import os
import re
from multiprocessing.pool import Pool
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, inputs_unchanged, manifest_path, write_manifest
from skillsig.s0_intake.devsample import output_path as devsample_path
from skillsig.s1_registry.versions import description_idiom

log = logging.getLogger(__name__)

_DOWNLOAD = "python src/data/download_data.py"
_DEV_SAMPLE = "python -m skillsig data --dev-sample"

# Fixed instruction verbs (base form). Phrase hits stay empty until a training
# split supplies a lexicon (T18); the card names s2_features/lexicons.py for that
# store, which is outside this card's file list, so both lists live here.
_IMPERATIVE_VERBS = frozenset(
    """
    add analyse analyze apply audit automate avoid build call check choose
    click clone close collect commit compare configure consider convert copy
    create debug define delete deploy describe design document download draft
    edit enable ensure evaluate execute explain export extract fetch fill find
    fix follow format generate help identify implement import include install
    keep let list load make manage monitor move open optimise optimize parse
    plan prefer print provide publish push query read refactor remove render
    replace restart return review run save scaffold search select set show
    specify start stop summarise summarize test transform translate try turn
    update use validate verify visit wait watch write
    """.split()
)
PHRASE_LEXICON: tuple[str, ...] = ()

_MATTR_WINDOW = 500
_FEATURE_COLUMNS = (
    "w_imperative_ratio",
    "w_you_per_100w",
    "w_command_density",
    "w_fk_grade",
    "w_mattr500",
    "w_emphasis_per_k",
    "w_uniformity_bullet_cv",
    "w_uniformity_heading_entropy",
    "w_phrase_hits",
    "w_non_english",
    "w_sentence_cv",
    "w_prose_sentences",
    "w_emdash_per_1kw",
)
_SCHEMA = pa.schema(
    [
        pa.field("file_sha", pa.string()),
        *(pa.field(name, pa.float64()) for name in _FEATURE_COLUMNS),
        pa.field("desc_idiom", pa.string()),
    ]
)

# Same body strip as db/content_and_structure.sql and structure.py.
_FRONT_BODY = re.compile(r"^\s*---.*?\n---\s*\n?", re.DOTALL | re.ASCII)
_LIST_PREFIX = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_ALPHA = re.compile(r"[A-Za-z]+")
_TOKEN = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?")
_YOU = re.compile(r"^(?:you|your)\b")
_SENTENCE = re.compile(r"[.!?]+")
_EMPHASIS = re.compile(r"\b(?:MUST|NEVER|ALWAYS|IMPORTANT|CRITICAL|REQUIRED)\b")
_FENCE = re.compile(r"^[ \t]*```+([^`\n]*)$")
_PROMPT = re.compile(r"^[ \t]*\$[ \t]")
_HEADING = re.compile(r"^(#{1,6})[ \t]")
_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+(.*)$")
_SHELL_LANGS = frozenset(
    {
        "bash",
        "cmd",
        "console",
        "fish",
        "ksh",
        "powershell",
        "pwsh",
        "sh",
        "shell",
        "terminal",
        "zsh",
    }
)
_VOWELS = frozenset("aeiouy")
# W11: sentences end at . ! ? followed by space, or at a line break (bullets are fragments).
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_MIN_SENTENCES = 5


def wording_features(text: str) -> dict[str, float]:
    """W1-W12 for one skill body. An empty body is zeros, never NaN."""
    words = _words(text)
    chars = len(text)
    return {
        "w_imperative_ratio": _imperative_ratio(text),
        "w_you_per_100w": _you_per_100(words),
        "w_command_density": _per_1k(_command_lines(text), chars),
        "w_fk_grade": _fk_grade(text, words),
        "w_mattr500": _mattr(words),
        "w_emphasis_per_k": _per_1k(len(_EMPHASIS.findall(text)), chars),
        "w_uniformity_bullet_cv": _bullet_cv(text),
        "w_uniformity_heading_entropy": _heading_entropy(text),
        "w_phrase_hits": _phrase_hits(text),
        "w_non_english": _non_english(text),
        **_burstiness(text),
        "w_emdash_per_1kw": _emdash_rate(text),
    }


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Write s2_wording.parquet for readable primary skills. Dev mode keeps dev-sample ids."""
    output = _output_path(cfg)
    inputs = _inputs(cfg, dev=dev)
    _require_inputs(cfg, dev=dev)
    if not force and inputs_unchanged(output, cfg=cfg, inputs=inputs):
        log.info("wording features up to date: %s", output)
        return StageResult(
            stage="s2_features", outputs=[output], skipped=True, rows=_saved_rows(output)
        )
    rows = _write_wording(cfg, output, dev=dev)
    write_manifest(output, cfg=cfg, inputs=inputs, rows=rows)
    log.info("wrote %s (%s rows)", output, rows)
    return StageResult(
        stage="s2_features", outputs=[output], skipped=False, rows={"s2_wording": rows}
    )


def _words(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TOKEN.finditer(text)]


def _imperative_ratio(text: str) -> float:
    lines = 0
    hits = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        lines += 1
        if _line_is_imperative(line):
            hits += 1
    if lines == 0:
        return 0.0
    return hits / lines


def _line_is_imperative(line: str) -> bool:
    rest = _LIST_PREFIX.sub("", line, count=1)
    match = _ALPHA.search(rest)
    return match is not None and match.group(0).lower() in _IMPERATIVE_VERBS


def _you_per_100(words: list[str]) -> float:
    if not words:
        return 0.0
    hits = sum(1 for word in words if _YOU.match(word))
    return 100.0 * hits / len(words)


def _command_lines(text: str) -> int:
    """Non-empty shell-fence lines, plus `$ ` prompts that sit outside those fences."""
    count = 0
    in_shell = False
    for line in text.splitlines():
        fence = _FENCE.match(line)
        if fence:
            in_shell = False if in_shell else _is_shell(fence.group(1))
            continue
        if in_shell:
            if line.strip():
                count += 1
        elif _PROMPT.match(line):
            count += 1
    return count


def _is_shell(info: str) -> bool:
    token = info.strip().lower()
    if not token:
        return False
    return token.split(None, 1)[0].strip("{}:") in _SHELL_LANGS


def _per_1k(count: int, chars: int) -> float:
    if chars <= 0 or count <= 0:
        return 0.0
    return count * 1000.0 / chars


def _fk_grade(text: str, words: list[str]) -> float:
    """Flesch-Kincaid grade. 0.39*(words/sentences) + 11.8*(syllables/words) - 15.59."""
    n_words = len(words)
    if n_words == 0:
        return 0.0
    sentences = len(_SENTENCE.findall(text)) or 1
    syllables = sum(_syllables(word) for word in words)
    return 0.39 * (n_words / sentences) + 11.8 * (syllables / n_words) - 15.59


def _syllables(word: str) -> int:
    """Vowel-group count, with a trailing silent e dropped when another syllable remains."""
    groups = 0
    previous_vowel = False
    letters = 0
    for char in word:
        if char < "a" or char > "z":
            previous_vowel = False
            continue
        letters += 1
        vowel = char in _VOWELS
        if vowel and not previous_vowel:
            groups += 1
        previous_vowel = vowel
    if letters == 0:
        return 0
    if groups > 1 and word.endswith("e"):
        groups -= 1
    return groups or 1


def _mattr(words: list[str]) -> float:
    """Mean TTR of each 500-token window. Shorter texts use one window over the whole text."""
    total = len(words)
    if total == 0:
        return 0.0
    if total <= _MATTR_WINDOW:
        return len(set(words)) / total
    return _sliding_mattr(words)


def _sliding_mattr(words: list[str]) -> float:
    window: dict[str, int] = {}
    types = 0
    for token in words[:_MATTR_WINDOW]:
        types += _bump(window, token, 1)
    accumulated = types / _MATTR_WINDOW
    windows = 1
    for index in range(_MATTR_WINDOW, len(words)):
        types += _bump(window, words[index], 1)
        types += _bump(window, words[index - _MATTR_WINDOW], -1)
        accumulated += types / _MATTR_WINDOW
        windows += 1
    return accumulated / windows


def _bump(window: dict[str, int], token: str, delta: int) -> int:
    count = window.get(token, 0) + delta
    if count <= 0:
        window.pop(token, None)
        return -1
    window[token] = count
    if count == 1 and delta > 0:
        return 1
    return 0


def _bullet_cv(text: str) -> float:
    """Population coefficient of variation of bullet-body lengths. Under 2 bullets is 0."""
    lengths = _bullet_lengths(text)
    count = len(lengths)
    if count < 2:
        return 0.0
    mean = sum(lengths) / count
    if mean == 0.0:
        return 0.0
    variance = sum((length - mean) ** 2 for length in lengths) / count
    return math.sqrt(variance) / mean


def _bullet_lengths(text: str) -> list[int]:
    lengths: list[int] = []
    for line in text.splitlines():
        match = _BULLET.match(line)
        if match:
            lengths.append(len(match.group(1).strip()))
    return lengths


def _heading_entropy(text: str) -> float:
    """Shannon entropy (bits) of heading-level counts. No headings is 0."""
    counts: dict[int, int] = {}
    total = 0
    for line in text.splitlines():
        match = _HEADING.match(line)
        if not match:
            continue
        level = len(match.group(1))
        counts[level] = counts.get(level, 0) + 1
        total += 1
    if total == 0:
        return 0.0
    entropy = 0.0
    for count in counts.values():
        share = count / total
        entropy -= share * math.log2(share)
    return entropy


def _phrase_hits(text: str) -> float:
    if not PHRASE_LEXICON or not text:
        return 0.0
    lowered = text.lower()
    return float(sum(lowered.count(phrase.lower()) for phrase in PHRASE_LEXICON if phrase))


def _prose(text: str) -> str:
    """Body text with fenced code, tables, headings and list markers removed."""
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        stripped = line.strip()
        if in_fence or not stripped or stripped.startswith("|") or _HEADING.match(stripped):
            continue
        lines.append(_LIST_PREFIX.sub("", stripped))
    return "\n".join(lines)


def _burstiness(text: str) -> dict[str, float]:
    """W11 sentence-length coefficient of variation over prose; 0 below 5 sentences."""
    sentences = [s for s in _SENTENCE_SPLIT.split(_prose(text)) if _TOKEN.search(s)]
    lengths = [len(_TOKEN.findall(s)) for s in sentences]
    count = float(len(lengths))
    if len(lengths) < _MIN_SENTENCES:
        return {"w_sentence_cv": 0.0, "w_prose_sentences": count}
    mean = sum(lengths) / len(lengths)
    sd = math.sqrt(sum((n - mean) ** 2 for n in lengths) / len(lengths))
    return {"w_sentence_cv": sd / mean if mean else 0.0, "w_prose_sentences": count}


def _emdash_rate(text: str) -> float:
    """W12 em dashes (U+2014) per 1,000 words of prose; code is excluded."""
    prose = _prose(text)
    words = len(_TOKEN.findall(prose))
    return prose.count("\u2014") * 1000.0 / words if words else 0.0


def _non_english(text: str) -> float:
    if not text:
        return 0.0
    non_ascii = sum(ord(char) > 127 for char in text)
    if non_ascii / len(text) > 0.2:
        return 1.0
    return 0.0


def _output_path(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.derived) / "s2_wording.parquet"


def _artifacts_dir(cfg: Config) -> Path:
    return resolve(cfg, cfg.paths.raw) / "artifacts"


def _inputs(cfg: Config, *, dev: bool) -> list[Path]:
    paths = [_artifacts_dir(cfg)]
    if dev:
        paths.append(devsample_path(cfg))
    return paths


def _require_inputs(cfg: Config, *, dev: bool) -> None:
    directory = _artifacts_dir(cfg)
    if not directory.is_dir() or not any(directory.glob("*.parquet")):
        raise FileNotFoundError(f"missing {directory}; run: {_DOWNLOAD}")
    if not dev:
        return
    sample = devsample_path(cfg)
    if not sample.is_file():
        raise FileNotFoundError(f"missing {sample}; run: {_DEV_SAMPLE}")


def _write_wording(cfg: Config, output: Path, *, dev: bool) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    con = duckdb.connect()
    try:
        total = _stream(con, cfg, temporary, dev=dev)
        temporary.replace(output)
        return total
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    finally:
        con.close()


def _stream(con: duckdb.DuckDBPyConnection, cfg: Config, temporary: Path, *, dev: bool) -> int:
    cursor = con.execute(_read_sql(cfg, dev=dev))
    workers = _worker_count()
    log.info("wording features using %s worker processes", workers)
    writer = pq.ParquetWriter(temporary, _SCHEMA, compression="zstd")
    total = 0
    try:
        with multiprocessing.get_context("spawn").Pool(processes=workers) as pool:
            total = _consume(cursor, pool, writer, cfg.chunk_rows, workers)
        if total == 0:
            writer.write_table(_empty_table())
        return total
    finally:
        writer.close()


def _consume(
    cursor: duckdb.DuckDBPyConnection,
    pool: Pool,
    writer: pq.ParquetWriter,
    chunk_rows: int,
    workers: int,
) -> int:
    total = 0
    while batch := cursor.fetchmany(int(chunk_rows)):
        scored = _score_batch(pool, batch, workers)
        writer.write_table(pa.Table.from_pylist(scored, schema=_SCHEMA))
        total += len(scored)
        log.info("wording features scored %s rows", total)
    return total


def _score_batch(
    pool: Pool, rows: list[tuple[object, ...]], workers: int
) -> list[dict[str, object]]:
    parts = _parts(rows, workers)
    scored = pool.map(_featurize_rows, parts)
    return [row for part in scored for row in part]


def _parts(rows: list[tuple[object, ...]], workers: int) -> list[list[tuple[object, ...]]]:
    count = max(1, min(workers, len(rows)))
    size = math.ceil(len(rows) / count)
    return [rows[index : index + size] for index in range(0, len(rows), size)]


def _featurize_rows(rows: list[tuple[object, ...]]) -> list[dict[str, object]]:
    return [
        _featurize_row(file_sha, content, description) for file_sha, content, description in rows
    ]


def _featurize_row(file_sha: object, content: object, description: object) -> dict[str, object]:
    text = content if isinstance(content, str) else ""
    body = _FRONT_BODY.sub("", text, count=1)
    features = wording_features(body)
    desc = description if isinstance(description, str) else None
    return {"file_sha": str(file_sha), **features, "desc_idiom": description_idiom(desc)}


def _empty_table() -> pa.Table:
    columns = {field.name: pa.array([], type=field.type) for field in _SCHEMA}
    return pa.table(columns, schema=_SCHEMA)


def _read_sql(cfg: Config, *, dev: bool) -> str:
    glob = _sql_string(_artifacts_dir(cfg) / "*.parquet")
    sample_filter = ""
    if dev:
        sample = _sql_string(devsample_path(cfg))
        sample_filter = f"AND file_sha IN (SELECT file_sha FROM read_parquet({sample}))"
    return f"""
        SELECT file_sha, content, description
        FROM read_parquet({glob})
        WHERE dedup_primary = 1
          AND content IS NOT NULL
          AND NOT (strpos(content, chr(10)) = 0 AND content LIKE '%SKILL.md')
          AND content NOT LIKE 'Redirecting%'
          AND body_chars >= 200
          {sample_filter}
        ORDER BY file_sha
    """


def _worker_count() -> int:
    return max(1, os.cpu_count() or 1)


def _sql_string(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _saved_rows(output: Path) -> dict[str, int]:
    payload = json.loads(manifest_path(output).read_text(encoding="utf-8"))
    return {"s2_wording": int(payload["rows"])}
