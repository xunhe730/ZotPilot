"""Page-1 overview label localization — annotation language via ``overview['lang']``.

``zh`` is the default and must stay byte-identical to prior releases; any other
declared language uses the neutral English label set.
"""
from zotpilot.pdf.annotator import build_overview_text


def test_zh_is_default_and_uses_chinese_labels():
    text = build_overview_text({
        "thesis": "核心论点一句话",
        "skeleton": {"question": "研究问题", "claim": "主要论点"},
        "strongest": "最有力",
        "weakest": "最薄弱",
    })
    assert "【核心论点】核心论点一句话" in text
    assert "问题：研究问题" in text
    assert "论点：主要论点" in text
    assert "最强：最有力" in text
    assert "最弱：最薄弱" in text


def test_explicit_zh_lang_equals_default():
    base = {"thesis": "T", "skeleton": {"evidence": "E"}, "strongest": "S"}
    assert build_overview_text({**base, "lang": "zh"}) == build_overview_text(base)


def test_en_lang_uses_english_labels():
    text = build_overview_text({
        "lang": "en",
        "thesis": "Core claim",
        "skeleton": {"question": "Q", "evidence": "E", "conclusion": "C"},
        "strongest": "S",
        "weakest": "W",
    })
    assert "Thesis: Core claim" in text
    assert "Question: Q" in text
    assert "Evidence: E" in text
    assert "Conclusion: C" in text
    assert "Strongest: S" in text
    assert "Weakest: W" in text
    for zh in ("核心论点", "问题", "证据", "结论", "最强", "最弱"):
        assert zh not in text


def test_unknown_lang_falls_back_to_english_labels():
    text = build_overview_text({"lang": "ja", "thesis": "X", "skeleton": {"claim": "Y"}})
    assert text.startswith("Thesis: X")
    assert "Claim: Y" in text


def test_empty_overview_returns_empty():
    assert build_overview_text({}) == ""
