"""Text steps shared by the skill_genealogy stages.

What: normalise skill text, split front matter, build word shingles,
MinHash signatures, Jaccard and containment, and lingua language labels.
Inputs: strings and shingle sets the caller already has. This module does
not read the dataset.
Outputs: strings, sets, MinHash objects and language codes. Nothing is written.
"""

from __future__ import annotations

import unicodedata
from typing import Literal

import numpy as np
import yaml
from datasketch import MinHash
from lingua import LanguageDetector, LanguageDetectorBuilder

from skill_genealogy.settings import (
    CANDIDATE_LANGUAGES,
    LANGUAGE_MAX_CHARS,
    LANGUAGE_MIN_CONFIDENCE,
    MIN_ENGLISH_LETTER_SHARE,
    MIN_LANGUAGE_LETTERS,
    MINHASH_NUM_PERM,
    MINHASH_SEED,
    SHINGLE_WORDS,
)

# Character cleanup runs before the trailing-space strip, so a non-breaking
# space at the end of a line is removed with the other trailing spaces.
_CHAR_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u2026": "...",
        "\u00a0": " ",
        "\u2009": " ",
        "\u202f": " ",
        "\u3000": " ",
    }
)
# Deleted anywhere in the text, including a byte-order mark in the middle.
_ZERO_WIDTH = str.maketrans("", "", "\u200b\u200c\u200d\u2060\ufeff")
# A run of blank lines longer than this is cut down to this many.
_BLANK_LINES_KEPT = 2
_LANGUAGE_DETECTOR: LanguageDetector | None = None

# datasketch 2 default, and the scheme the main pipeline stores.
MINHASH_SCHEME: Literal["affine32"] = "affine32"


def _collapse_blank_lines(lines: list[str]) -> list[str]:
    """Cut a run of 3 or more empty lines down to 2, then drop end blanks.

    Example:
    >>> _collapse_blank_lines(["a", "", "", "", "b", ""])
    ['a', '', '', 'b']
    """
    collapsed: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run <= _BLANK_LINES_KEPT:
                collapsed.append(line)
            continue
        blank_run = 0
        collapsed.append(line)
    while collapsed and collapsed[0] == "":
        collapsed.pop(0)
    while collapsed and collapsed[-1] == "":
        collapsed.pop()
    return collapsed


# Algorithm A1, see CODE_FLOW.md
def normalize_text(text: str | None) -> str:
    """Make Unicode, line endings, quotes and dashes the same, without lowercasing.

    None becomes an empty string. The first step is Unicode NFKC, which
    unifies composed and decomposed accents and full-width letters. Zero-width
    characters and a byte-order mark are then removed anywhere in the text.
    CRLF and CR become LF. Curly quotes become straight quotes, dashes and
    the minus sign become "-", an ellipsis becomes "...", and odd spaces
    become a normal space. Trailing spaces are removed on every line. Three
    or more blank lines in a row become two. Blank lines at the start and
    end are dropped. The original text stays available for display.

    Example:
    >>> normalize_text(None)
    ''
    >>> normalize_text("\\n\\n\\n")
    ''
    >>> normalize_text("\\ufeff\\u201cHi\\u201d  \\r\\n")
    '"Hi"'
    >>> normalize_text("cafe\\u0301") == normalize_text("caf\\u00e9")
    True
    >>> normalize_text("\\uff21\\uff22\\uff23")
    'ABC'
    >>> normalize_text("a\\u200bb")
    'ab'
    >>> normalize_text("x\\u2212y")
    'x-y'
    >>> normalize_text("wait\\u2026")
    'wait...'
    """
    if text is None:
        return ""
    unified = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    unix = unified.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = [line.translate(_CHAR_MAP).rstrip(" \t") for line in unix.split("\n")]
    return "\n".join(_collapse_blank_lines(cleaned))


# Algorithm A2, see CODE_FLOW.md
def split_front_matter(text: str) -> tuple[str, str]:
    """Split a leading YAML block from the body.

    Front matter counts only when the first line is ``---`` and a later line
    is ``---``. The returned pair is the text between those lines, then the
    body. Any other text returns an empty front matter and the original text.

    Example:
    >>> split_front_matter("---\\nname: demo\\n---\\nBody line")
    ('name: demo', 'Body line')
    >>> split_front_matter("Body only")
    ('', 'Body only')
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return ("", text)
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            front = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            return (front, body)
    return ("", text)


# Algorithm A2, see CODE_FLOW.md
def parse_front_matter(front_matter_text: str) -> dict[str, object]:
    """Parse front matter with YAML. A bad block becomes an empty dict.

    ``yaml.safe_load`` reads the text between the ``---`` lines. A scan
    error, a bad date, or a value that is not a dict of strings, returns ``{}``.

    Example:
    >>> parse_front_matter("name: demo")
    {'name': 'demo'}
    >>> parse_front_matter("name: [")
    {}
    >>> parse_front_matter("date: 2024-02-30")
    {}
    """
    if not front_matter_text.strip():
        return {}
    try:
        loaded = yaml.safe_load(front_matter_text)
    except (yaml.YAMLError, ValueError, TypeError, OverflowError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    if not all(isinstance(key, str) for key in loaded):
        return {}
    return dict(loaded)


# Algorithm A2, see CODE_FLOW.md
def body_lines(text: str) -> list[str]:
    """Return the normalised body as non-empty lines.

    The text is normalised, front matter is removed, and blank lines are
    dropped. Callers that score or diff a skill use this list.

    Example:
    >>> body_lines("---\\nname: demo\\n---\\n\\nUse this skill.\\n")
    ['Use this skill.']
    """
    _front, body = split_front_matter(normalize_text(text))
    return [line for line in body.split("\n") if line.strip() != ""]


# Algorithm A3, see CODE_FLOW.md
def remove_boilerplate_lines(lines: list[str], boilerplate: set[str]) -> list[str]:
    """Drop lines whose stripped lowercase text is in the boilerplate set.

    The set is built later (G13) from lines shared by many owners. Matching
    uses ``line.strip().lower()``, so entries in ``boilerplate`` are lowercase.
    The kept lines stay in their original spelling.

    Example:
    >>> remove_boilerplate_lines(["Use this.", "Copyright"], {"copyright"})
    ['Use this.']
    """
    return [line for line in lines if line.strip().lower() not in boilerplate]


# Algorithm A4, see CODE_FLOW.md
def make_word_shingles(text: str, words: int = SHINGLE_WORDS) -> set[str]:
    """Build the set of consecutive word groups used for near-copy checks.

    The text is lowercased and split on whitespace. Each shingle is ``words``
    tokens joined by one space. Callers pass the body, not the front matter.
    Fewer tokens than ``words`` become one shingle of every token. An empty
    body becomes an empty set.

    Example:
    >>> sorted(make_word_shingles("a b c d e f", words=5))
    ['a b c d e', 'b c d e f']
    >>> make_word_shingles("a b c", words=5)
    {'a b c'}
    >>> make_word_shingles("")
    set()
    """
    if words < 1:
        raise ValueError(f"words must be >= 1, got {words}")
    tokens = text.lower().split()
    if not tokens:
        return set()
    if len(tokens) < words:
        return {" ".join(tokens)}
    width = words
    return {" ".join(tokens[index : index + width]) for index in range(len(tokens) - width + 1)}


# Algorithm A5, see CODE_FLOW.md
def has_comparable_text(shingles: set[str]) -> bool:
    """True when the shingle set has at least one phrase to compare.

    An empty set is a body with no words left after boilerplate removal.
    Those texts get no signature and no similarity link. Exact text matches
    still link them.

    Example:
    >>> has_comparable_text(set())
    False
    >>> has_comparable_text({"a b c d e"})
    True
    """
    return bool(shingles)


# Algorithm A5, see CODE_FLOW.md
def make_minhash(shingles: set[str]) -> MinHash:
    """Build a MinHash sketch that estimates how much two shingle sets overlap.

    MinHash keeps the smallest hash of each shingle under many permutations.
    The share of permutations where two sketches agree estimates the Jaccard
    similarity of the two sets. Each shingle is updated as UTF-8 bytes.
    The sketch uses ``MINHASH_NUM_PERM`` permutations, seed ``MINHASH_SEED``
    (20261015, from the main pipeline ``plan.json``), and scheme affine32.
    An empty set raises ``ValueError`` because it is not evidence of copying.

    Example:
    >>> left = make_minhash({"alpha beta", "beta gamma"})
    >>> estimate_jaccard(left, make_minhash({"alpha beta", "beta gamma"}))
    1.0
    >>> try:
    ...     make_minhash(set())
    ... except ValueError as error:
    ...     str(error)
    'no shingles: text has no comparable words'
    """
    if not shingles:
        raise ValueError("no shingles: text has no comparable words")
    sketch = MinHash(num_perm=MINHASH_NUM_PERM, seed=MINHASH_SEED, scheme=MINHASH_SCHEME)
    for shingle in shingles:
        sketch.update(shingle.encode("utf-8"))
    return sketch


# Algorithm A5, see CODE_FLOW.md
def minhash_to_array(minhash: MinHash) -> np.ndarray:
    """Copy a sketch's hash values into a uint64 array for storage.

    affine32 stores 32-bit values. They are widened to uint64 so a saved
    column has one integer type. The numbers themselves do not change.

    Example:
    >>> values = minhash_to_array(make_minhash({"one two"}))
    >>> values.dtype == np.uint64
    True
    >>> len(values) == MINHASH_NUM_PERM
    True
    """
    return np.asarray(minhash.hashvalues, dtype=np.uint64).copy()


# Algorithm A5, see CODE_FLOW.md
def minhash_from_array(values: np.ndarray) -> MinHash:
    """Rebuild a sketch from hash values saved by ``minhash_to_array``.

    The seed and scheme must match the sketch that wrote the array, or a
    later Jaccard compare will refuse the pair.

    Example:
    >>> original = make_minhash({"one two", "two three"})
    >>> estimate_jaccard(original, minhash_from_array(minhash_to_array(original)))
    1.0
    """
    stored = np.asarray(values)
    if stored.ndim != 1 or stored.shape[0] != MINHASH_NUM_PERM:
        raise ValueError(
            f"MinHash array must have shape ({MINHASH_NUM_PERM},), got {stored.shape}"
        )
    return MinHash(
        num_perm=MINHASH_NUM_PERM,
        seed=MINHASH_SEED,
        hashvalues=stored,
        scheme=MINHASH_SCHEME,
    )


# Algorithm A6, see CODE_FLOW.md
def exact_jaccard(a: set[str], b: set[str]) -> float:
    """Share of the union that is in both sets.

    Jaccard = |a ∩ b| / |a ∪ b|. An empty set gives no evidence of copying,
    so if either set is empty the result is 0.0. Identical empty texts are
    found by exact text match, not by Jaccard.

    Example:
    >>> exact_jaccard({"a", "b", "c", "d"}, {"a", "b", "c", "d", "e", "f", "g", "h"})
    0.5
    >>> exact_jaccard(set(), set())
    0.0
    >>> exact_jaccard({"a"}, set())
    0.0
    """
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Algorithm A6, see CODE_FLOW.md
def estimate_jaccard(a: MinHash, b: MinHash) -> float:
    """Estimate Jaccard from the share of MinHash permutations that agree.

    This is the fast stand-in for ``exact_jaccard`` on large shingle sets.
    Identical sketches estimate 1.0.

    Example:
    >>> sketch = make_minhash({"alpha beta"})
    >>> estimate_jaccard(sketch, sketch)
    1.0
    """
    return float(a.jaccard(b))


# Algorithm A7, see CODE_FLOW.md
def exact_containment(registry_shingles: set[str], copy_shingles: set[str]) -> float:
    """Share of the registry shingles that also appear in the copy.

    containment = |registry ∩ copy| / |registry|. 1.0 means the copy still
    holds the whole registry text; the copy may add more. An empty registry
    or an empty copy is 0.0.

    Example:
    >>> exact_containment({"a", "b", "c", "d"}, {"a", "b", "c", "d", "e", "f", "g", "h"})
    1.0
    >>> exact_containment({"a"}, set())
    0.0
    """
    if not registry_shingles or not copy_shingles:
        return 0.0
    shared = len(registry_shingles & copy_shingles)
    return shared / len(registry_shingles)


# Algorithm A7, see CODE_FLOW.md
def estimate_containment(jaccard: float, registry_size: int, copy_size: int) -> float:
    """Estimate containment from a Jaccard score and the two set sizes.

    containment = shared shingles / shingles in the registry text.
    From Jaccard and the sizes that is
    jaccard * (registry_size + copy_size) / ((1 + jaccard) * registry_size),
    clipped to the range 0 to 1. A Jaccard of 0, an empty registry, or an
    empty copy gives 0.

    Registry {a, b, c, d} and copy {a, b, c, d, e, f, g, h} have Jaccard 0.5
    and containment 1.0.

    Example:
    >>> estimate_containment(0.5, 4, 8)
    1.0
    >>> estimate_containment(0.0, 4, 8)
    0.0
    >>> estimate_containment(0.5, 4, 0)
    0.0
    """
    if registry_size <= 0 or copy_size <= 0 or jaccard <= 0.0:
        return 0.0
    raw = jaccard * (registry_size + copy_size) / ((1.0 + jaccard) * registry_size)
    if raw < 0.0:
        return 0.0
    if raw > 1.0:
        return 1.0
    return float(raw)


def _drop_code_fences(text: str) -> str:
    """Remove fenced code blocks, including the fence lines.

    Example:
    >>> _drop_code_fences("Keep\\n```\\nsecret\\n```\\nthis")
    'Keep\\nthis'
    >>> _drop_code_fences("Keep\\n~~~\\nhidden\\n~~~\\nthis")
    'Keep\\nthis'
    """
    kept: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if not in_fence:
            kept.append(line)
    return "\n".join(kept)


# Algorithm A8, see CODE_FLOW.md
def build_language_detector() -> LanguageDetector:
    """Build the lingua detector once and reuse it for the rest of the process.

    Lingua scores a text against n-gram models and returns a confidence for
    each candidate language. The detector is limited to ``CANDIDATE_LANGUAGES``
    and the models are loaded at build time. A later call returns the same
    object.

    Example:
    >>> first = build_language_detector()
    >>> first is build_language_detector()
    True
    """
    global _LANGUAGE_DETECTOR
    if _LANGUAGE_DETECTOR is None:
        _LANGUAGE_DETECTOR = (
            LanguageDetectorBuilder.from_languages(*CANDIDATE_LANGUAGES)
            .with_preloaded_language_models()
            .build()
        )
    return _LANGUAGE_DETECTOR


def _language_sample(text: str) -> str:
    """Return the normalised body, without front matter or code fences, cut for speed.

    Example:
    >>> _language_sample("ok")
    'ok'
    """
    body = _drop_code_fences(split_front_matter(normalize_text(text))[1])
    return body[:LANGUAGE_MAX_CHARS]


def _is_latin_letter(character: str) -> bool:
    """True when the character is a letter whose Unicode name contains LATIN.

    Example:
    >>> _is_latin_letter("é")
    True
    >>> _is_latin_letter("中")
    False
    """
    return character.isalpha() and "LATIN" in unicodedata.name(character, "")


def _latin_letter_share(sample: str) -> float:
    """Share of the sample's letters that are Latin-script letters.

    A letter is Latin when ``"LATIN"`` is in its Unicode name. Digits and
    punctuation are ignored. A sample with no letters has share 0.

    Example:
    >>> _latin_letter_share("abcd中")
    0.8
    """
    letters = [character for character in sample if character.isalpha()]
    if not letters:
        return 0.0
    latin_letters = sum(_is_latin_letter(character) for character in letters)
    return latin_letters / len(letters)


def _latin_script_text(sample: str) -> str:
    """Replace every non-Latin letter with a space. Other characters stay.

    Example:
    >>> _latin_script_text("Go 中")
    'Go  '
    """
    parts: list[str] = []
    for character in sample:
        if character.isalpha() and not _is_latin_letter(character):
            parts.append(" ")
        else:
            parts.append(character)
    return "".join(parts)


def _top_language_code(ranked, *, block_english: bool = False) -> str:
    """ISO 639-1 code of lingua's top language, or ``unknown`` below the cutoff.

    When ``block_english`` is true and the top language is English, the best
    language that is not English is returned instead.

    Example:
    >>> from lingua import ConfidenceValue, Language
    >>> high = [ConfidenceValue(Language.ENGLISH, 0.8), ConfidenceValue(Language.KOREAN, 0.2)]
    >>> _top_language_code(high, block_english=True)
    'ko'
    >>> _top_language_code([ConfidenceValue(Language.ENGLISH, 0.2)])
    'unknown'
    """
    if not ranked or ranked[0].value < LANGUAGE_MIN_CONFIDENCE:
        return "unknown"
    top_code = ranked[0].language.iso_code_639_1.name.lower()
    if not block_english or top_code != "en":
        return top_code
    for item in ranked[1:]:
        code = item.language.iso_code_639_1.name.lower()
        if code != "en":
            return code
    return "unknown"


# Algorithm A8, see CODE_FLOW.md
def detect_language(text: str) -> str:
    """Label the normalised body with an ISO 639-1 code, ``unknown``, or ``other``.

    The sample is the normalised body with front matter and code fences
    removed, cut to ``LANGUAGE_MAX_CHARS``. Fewer than ``MIN_LANGUAGE_LETTERS``
    letters is ``unknown``. When at least ``MIN_ENGLISH_LETTER_SHARE`` of the
    letters are Latin-script letters, lingua reads that Latin part only, and a
    top confidence below ``LANGUAGE_MIN_CONFIDENCE`` is ``unknown``. Otherwise
    lingua reads the whole sample. A top label of English is replaced by the
    best language that is not English. If that result is ``unknown``, the
    label is ``other``, so a text under 75% Latin letters is never ``en`` or
    ``unknown``. A script lingua does not know, such as Greek or Hebrew, is
    ``other``.

    Example:
    >>> detect_language("The skill is for you to use when it is needed and this is the tool.")
    'en'
    >>> detect_language("ok")
    'unknown'
    """
    sample = _language_sample(text)
    if sum(character.isalpha() for character in sample) < MIN_LANGUAGE_LETTERS:
        return "unknown"
    detector = build_language_detector()
    if _latin_letter_share(sample) >= MIN_ENGLISH_LETTER_SHARE:
        ranked = detector.compute_language_confidence_values(_latin_script_text(sample))
        return _top_language_code(ranked)
    ranked = detector.compute_language_confidence_values(sample)
    code = _top_language_code(ranked, block_english=True)
    if code == "unknown":
        return "other"
    return code


# Algorithm A8, see CODE_FLOW.md
def is_english(text: str) -> bool:
    """True when the text is English, or too short or unclear to tell.

    ``unknown`` counts as English. That covers a text with fewer than 20
    letters and a text whose top language is below the confidence cutoff.
    G07 reports how many texts this includes. Other codes are False.

    Example:
    >>> is_english("ok")
    True
    """
    return detect_language(text) in {"en", "unknown"}
