"""Fightin' Words lexicon helpers."""

from __future__ import annotations

from skillsig.s2_features.lexicons import (
    VENDOR_WORDS,
    fightin_words,
    fit_lexicon,
    marker_rate,
    tokens,
)

_BASE = "please read the file and run the tests then report the result"


def _docs(extra: str, n: int) -> list[str]:
    return [f"{_BASE} item{i % 3} {extra}" for i in range(n)]


def test_planted_word_has_highest_z() -> None:
    pos = _docs("comprehensive claude", 60)
    neg = _docs("", 60)
    table = fightin_words(pos, neg, min_docs=5)
    top = [w for w in table["word"] if w not in VENDOR_WORDS][0]
    assert top == "comprehensive"


def test_balanced_word_is_near_zero() -> None:
    table = fightin_words(_docs("", 50), _docs("", 50), min_docs=5).set_index("word")
    assert abs(table.loc["tests", "z"]) < 1


def test_vendor_words_never_in_lexicon() -> None:
    lexicon = fit_lexicon(_docs("comprehensive claude cursor", 60), _docs("", 60), min_docs=5)
    assert "comprehensive" in lexicon
    assert not VENDOR_WORDS & set(lexicon)


def test_tokens_strip_code_urls_front_matter() -> None:
    text = (
        "---\nname: secretword\n---\nUse `inlinecode` here.\n```bash\nfencedword\n```\n"
        "See https://example.com/urlword for more."
    )
    toks = tokens(text)
    for gone in ("secretword", "inlinecode", "fencedword", "urlword"):
        assert gone not in toks
    assert "use" in toks and "more" in toks


def test_marker_rate() -> None:
    assert marker_rate("", ("x",)) == 0.0
    assert marker_rate("alpha beta alpha gamma", ("alpha", "zeta")) == 250.0
