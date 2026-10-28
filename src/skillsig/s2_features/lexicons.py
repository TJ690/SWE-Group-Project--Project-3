"""
STAGE: s2_features
MODULE: lexicons
PURPOSE: Fold-safe marker-word lexicon (Fightin' Words log-odds, informative Dirichlet prior).
INPUTS: texts passed in by the caller (training folds only)
OUTPUTS: none (functions return lexicons and rates)
RQ LINK: RQ1 (phrasing signal: words over-used by skills whose commits disclosed an AI tool)

Monroe, Colaresi & Quinn (2008), "Fightin' Words", Political Analysis. Words are counted by
presence per document so one long file cannot dominate. The lexicon must be fitted inside each
training fold; a global list would leak test documents into the score.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence

import pandas as pd

VENDOR_WORDS = frozenset(
    """
    claude anthropic cursor copilot codex openai gemini devin kiro windsurf cline aider
    opencode gpt llm ai agent agents skill skills mcp chatgpt sonnet opus haiku
    """.split()
)
_FRONT = re.compile(r"^\s*---.*?\n---\s*\n?", re.DOTALL)
_FENCE = re.compile(r"```.*?(```|$)", re.DOTALL)
_INLINE = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"https?://\S+|www\.\S+")
_WORD = re.compile(r"[a-z]+")


def tokens(text: str) -> list[str]:
    """Lowercase alphabetic tokens; front matter, fenced code, inline code and URLs removed."""
    if not text:
        return []
    body = _FRONT.sub("", text, count=1)
    body = _FENCE.sub(" ", body)
    body = _INLINE.sub(" ", body)
    body = _URL.sub(" ", body)
    return _WORD.findall(body.lower())


def doc_sets(texts: Iterable[str]) -> list[frozenset[str]]:
    return [frozenset(tokens(text)) for text in texts]


def fightin_words(
    pos_docs: Sequence[str] | Sequence[frozenset[str]],
    neg_docs: Sequence[str] | Sequence[frozenset[str]],
    *,
    alpha0: float = 1000.0,
    min_docs: int = 20,
) -> pd.DataFrame:
    """-> word, z, delta, df_pos, df_neg (presence counts; prior from pooled frequencies)."""
    pos = _as_sets(pos_docs)
    neg = _as_sets(neg_docs)
    y_pos = Counter(w for doc in pos for w in doc)
    y_neg = Counter(w for doc in neg for w in doc)
    n_pos = sum(y_pos.values())
    n_neg = sum(y_neg.values())
    total = n_pos + n_neg
    rows = []
    for word in set(y_pos) | set(y_neg):
        yp, yn = y_pos[word], y_neg[word]
        if yp + yn < min_docs or total == 0:
            continue
        a = alpha0 * (yp + yn) / total
        delta = math.log((yp + a) / (n_pos + alpha0 - yp - a)) - math.log(
            (yn + a) / (n_neg + alpha0 - yn - a)
        )
        z = delta / math.sqrt(1 / (yp + a) + 1 / (yn + a))
        rows.append((word, z, delta, yp, yn))
    frame = pd.DataFrame(rows, columns=["word", "z", "delta", "df_pos", "df_neg"])
    return frame.sort_values(["z", "word"], ascending=[False, True], ignore_index=True)


def fit_lexicon(
    pos_docs: Sequence[str] | Sequence[frozenset[str]],
    neg_docs: Sequence[str] | Sequence[frozenset[str]],
    *,
    z_min: float = 3.0,
    max_words: int = 100,
    min_docs: int = 20,
) -> tuple[str, ...]:
    """Top over-represented style words: z > z_min, not a vendor word, length >= 3."""
    table = fightin_words(pos_docs, neg_docs, min_docs=min_docs)
    keep = table[
        (table["z"] > z_min) & ~table["word"].isin(VENDOR_WORDS) & (table["word"].str.len() >= 3)
    ]
    return tuple(keep["word"].head(max_words))


def marker_rate(text: str, lexicon: Sequence[str]) -> float:
    """Distinct lexicon words present per 1,000 tokens."""
    return rate_from_tokens(tokens(text), frozenset(lexicon))


def rate_from_tokens(toks: Sequence[str], lexicon: frozenset[str]) -> float:
    if not toks:
        return 0.0
    return 1000.0 * len(lexicon.intersection(toks)) / len(toks)


def _as_sets(docs: Sequence[str] | Sequence[frozenset[str]]) -> list[frozenset[str]]:
    return [doc if isinstance(doc, frozenset) else frozenset(tokens(doc)) for doc in docs]
