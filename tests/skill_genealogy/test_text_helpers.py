"""Tests for the shared text helpers.

Hand-made strings only. No dataset files.
"""

from __future__ import annotations

import pytest

from skill_genealogy.text_helpers import (
    _language_sample,
    _latin_letter_share,
    body_lines,
    detect_language,
    estimate_containment,
    estimate_jaccard,
    exact_containment,
    exact_jaccard,
    has_comparable_text,
    is_english,
    make_minhash,
    make_word_shingles,
    minhash_from_array,
    minhash_to_array,
    normalize_text,
    parse_front_matter,
    remove_boilerplate_lines,
    split_front_matter,
)

_REGISTRY = {"a", "b", "c", "d"}
_COPY = {"a", "b", "c", "d", "e", "f", "g", "h"}


def test_normalize_text_cleans_endings_quotes_spaces_and_bom() -> None:
    """CRLF, trailing spaces, curly quotes and a leading BOM become plain text."""
    raw = "\ufeff“Hello”  \r\nworld\u2014next  "
    assert normalize_text(raw) == '"Hello"\nworld-next'
    assert normalize_text(None) == ""


def test_normalize_text_collapses_blank_lines_and_nbsp() -> None:
    """Three blank lines become two, and a non-breaking space becomes a space."""
    assert normalize_text("a\n\n\n\nb") == "a\n\n\nb"
    assert normalize_text("a\u00a0b") == "a b"
    assert normalize_text("\n\n\n") == ""


def test_normalize_text_nfkc_and_extra_marks() -> None:
    """NFKC, zero-width characters, the minus sign and the ellipsis."""
    assert normalize_text("cafe\u0301") == normalize_text("caf\u00e9")
    assert normalize_text("\uff21\uff22\uff23") == "ABC"
    assert normalize_text("a\u200bb") == "ab"
    assert normalize_text("x\u2212y") == "x-y"
    assert normalize_text("wait\u2026") == "wait..."


def test_split_front_matter_with_without_and_unclosed() -> None:
    """A closed --- block splits; a missing or unclosed block stays with the text."""
    closed = "---\nname: demo\n---\nBody line"
    assert split_front_matter(closed) == ("name: demo", "Body line")
    plain = "Body only"
    assert split_front_matter(plain) == ("", plain)
    unclosed = "---\nname: demo\nbody without close"
    assert split_front_matter(unclosed) == ("", unclosed)


def test_parse_front_matter_and_body_lines() -> None:
    """YAML errors become {}, and body lines drop the front matter and blanks."""
    assert parse_front_matter("name: demo") == {"name": "demo"}
    assert parse_front_matter("name: [") == {}
    assert parse_front_matter("date: 2024-02-30") == {}
    assert parse_front_matter("- just\n- a list") == {}
    assert body_lines("---\nname: demo\n---\n\nUse this skill.\n") == ["Use this skill."]


def test_remove_boilerplate_lines_matches_lowercase() -> None:
    """A line is dropped when its stripped lowercase form is in the set."""
    lines = ["Use this.", "  Copyright  ", "Keep me"]
    assert remove_boilerplate_lines(lines, {"copyright"}) == ["Use this.", "Keep me"]


def test_make_word_shingles_width_short_and_empty() -> None:
    """Five-word windows, a short text, and an empty text."""
    assert make_word_shingles("a b c d e f", words=5) == {"a b c d e", "b c d e f"}
    assert make_word_shingles("A B C", words=5) == {"a b c"}
    assert make_word_shingles("") == set()
    assert make_word_shingles("   ") == set()


def test_jaccard_and_containment_on_worked_example() -> None:
    """Registry {a..d} inside copy {a..h} is Jaccard 0.5 and containment 1."""
    assert exact_jaccard(_REGISTRY, _COPY) == 0.5
    assert exact_containment(_REGISTRY, _COPY) == 1.0
    assert estimate_containment(0.5, 4, 8) == 1.0
    assert exact_jaccard(set(), set()) == 0.0
    assert exact_jaccard({"a"}, set()) == 0.0
    assert exact_jaccard(_REGISTRY, set()) == 0.0
    assert exact_containment(set(), _COPY) == 0.0
    assert exact_containment(_REGISTRY, set()) == 0.0
    assert estimate_containment(0.0, 4, 8) == 0.0
    assert estimate_containment(0.5, 4, 0) == 0.0


def test_estimate_containment_within_tolerance_on_two_hundred_shingles() -> None:
    """The size formula stays within 0.05 of exact containment on 200-shingle sets."""
    registry = {f"shingle {index}" for index in range(200)}
    copy = {f"shingle {index}" for index in range(150)} | {f"extra {index}" for index in range(50)}
    jaccard = exact_jaccard(registry, copy)
    exact = exact_containment(registry, copy)
    estimated = estimate_containment(jaccard, len(registry), len(copy))
    assert abs(estimated - exact) <= 0.05
    assert exact == 0.75


def test_empty_shingles_are_not_comparable() -> None:
    """An empty shingle set is not a near copy and gets no signature."""
    assert has_comparable_text(set()) is False
    assert has_comparable_text({"a b c d e"}) is True
    with pytest.raises(ValueError, match="no shingles: text has no comparable words"):
        make_minhash(set())


def test_minhash_identical_texts_and_array_round_trip() -> None:
    """Identical shingles estimate 1, and a saved array restores that estimate."""
    shingles = make_word_shingles("alpha beta gamma delta epsilon zeta eta")
    other = make_word_shingles("alpha beta gamma delta epsilon extra words here now")
    left = make_minhash(shingles)
    assert estimate_jaccard(left, make_minhash(shingles)) == 1.0
    restored = minhash_from_array(minhash_to_array(left))
    assert minhash_to_array(left).dtype.name == "uint64"
    assert estimate_jaccard(restored, make_minhash(other)) == estimate_jaccard(
        left, make_minhash(other)
    )


def test_detect_language_real_sentences() -> None:
    """Lingua labels real multi-line sentences, terse English, and short text."""
    german = (
        "Dieser Absatz beschreibt eine deutsche Fertigkeit zum Prüfen von Dateien.\n"
        "Er bleibt durchgehend auf Deutsch und nennt keine englischen Befehle."
    )
    french = (
        "Ce paragraphe décrit une compétence en français pour lire un fichier.\n"
        "Il explique les étapes sans utiliser de phrases anglaises."
    )
    spanish = (
        "Este párrafo describe una habilidad en español para revisar un archivo.\n"
        "Explica los pasos y el resultado esperado del informe."
    )
    chinese = (
        "这是一个中文技能，用来检查文件并写出简短报告。\n"
        "它说明每个步骤，并且不使用英文句子。"
    )
    japanese = (
        "これは日本語の技能です。ファイルを確認して短い報告を書きます。\n"
        "手順をかなで説明し、英語の文は使いません。"
    )
    russian = (
        "Этот абзац описывает навык на русском языке для проверки файла.\n"
        "Он объясняет шаги и ожидаемый результат отчёта."
    )
    steps = (
        "## Steps\n"
        "1. Run pdftotext input.pdf out.txt\n"
        "2. Check output encoding UTF-8\n"
        "3. Return summary JSON with fields: pages, words, errors"
    )
    bullets = (
        "- Parse YAML config\n"
        "- Validate schema fields\n"
        "- Emit warnings\n"
        "- Write report.md\n"
        "- Exit code 1 on failure"
    )
    fenced = "```\nThe skill is for you to use when it is needed and this is the tool.\n```"
    assert detect_language(german) == "de"
    assert detect_language(french) == "fr"
    assert detect_language(spanish) == "es"
    assert detect_language(chinese) == "zh"
    assert detect_language(japanese) == "ja"
    assert detect_language(russian) == "ru"
    assert detect_language(steps) == "en"
    assert detect_language(bullets) == "en"
    assert detect_language("ok") == "unknown"
    assert detect_language(fenced) == "unknown"
    assert is_english(steps) is True
    assert is_english("ok") is True
    assert is_english(german) is False
    assert is_english(chinese) is False


def test_detect_language_english_share() -> None:
    english_chinese = (
        "## Features\n"
        "- Read a local PDF and extract the text\n"
        "- Check the page count and the encoding\n"
        "- Write a short JSON summary for the user\n"
        "- Return the page count.\n"
        "\n"
        "## 中文说明\n"
        "这是一个中文段落用来说明技能的用途和步骤以及限制条件。\n"
    )
    english_korean = (
        "- Parse YAML config and validate schema fields then write report\n"
        "- Parse YAML config and validate schema fields then write report\n"
        "- Parse YAML config and validate schema fields then write report\n"
        "\n"
        "이 문서는 한국어로 작성된 기술 설명입니다.\n"
        "파일을 읽고 결과를 확인한 뒤 보고서를 작성합니다.\n"
        "사용자는 각 단계를 따라 진행합니다.\n"
        "입력 경로를 확인하고 출력 파일을 저장합니다.\n"
        "오류가 있으면 메시지를 표시하고 작업을 중단합니다.\n"
        "설정 파일을 불러오고 필요한 항목만 남깁니다.\n"
    )
    chinese_share = _latin_letter_share(_language_sample(english_chinese))
    korean_share = _latin_letter_share(_language_sample(english_korean))
    assert 0.78 <= chinese_share <= 0.82
    assert 0.56 <= korean_share <= 0.62
    assert detect_language(english_chinese) == "en"
    assert detect_language(english_korean) == "ko"


def test_detect_language_unknown_script_is_other() -> None:
    greek = (
        "Αυτή η παράγραφος περιγράφει μια δεξιότητα στα ελληνικά για έλεγχο αρχείου.\n"
        "Εξηγεί τα βήματα και το αναμενόμενο αποτέλεσμα της αναφοράς."
    )
    hebrew = (
        "פסקה זו מתארת מיומנות בעברית לבדיקת קובץ ולכתיבת דוח קצר.\n"
        "היא מסבירה את השלבים ואת התוצאה הצפויה של הדוח."
    )
    assert detect_language(greek) == "other"
    assert detect_language(hebrew) == "other"
    assert is_english(greek) is False
    assert is_english(hebrew) is False
