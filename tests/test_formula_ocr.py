from zotpilot.feature_extraction.formula_ocr import (
    FormulaCandidate,
    _candidate_confidence,
    _coerce_provider_result,
    _dedupe_candidates,
    _extract_block_signals,
    _extract_equation_number,
    is_high_quality_formula_latex,
)
from zotpilot.models import ExtractedFormula


def test_formula_latex_quality_filter_accepts_math_and_rejects_noise():
    assert is_high_quality_formula_latex(r"E = mc^2")
    assert is_high_quality_formula_latex(r"\frac{\partial L}{\partial x_i} = 0")
    assert not is_high_quality_formula_latex("References")
    assert not is_high_quality_formula_latex("aaaaaa")
    assert not is_high_quality_formula_latex("the method is evaluated")


def test_rapid_latex_ocr_tuple_elapsed_time_is_not_confidence():
    result = _coerce_provider_result((r"E = mc^2", 0.42))

    assert result.latex == r"E = mc^2"
    assert result.confidence is None


def test_formula_candidate_confidence_uses_font_and_span_flags_as_boosts():
    plain_score = _candidate_confidence(
        "E = mc^2 (1)",
        (10.0, 20.0, 180.0, 42.0),
        set(),
        set(),
    )
    math_score = _candidate_confidence(
        "E = mc^2 (1)",
        (10.0, 20.0, 180.0, 42.0),
        {"CMMI10"},
        {2},
    )

    assert plain_score >= 0.6
    assert math_score > plain_score


def test_equation_number_detection_avoids_plain_step_numbers():
    assert _extract_equation_number("E = mc^2 (1)") == "(1)"
    assert _extract_equation_number("Eq. (3)") == "(3)"
    assert _extract_equation_number("Follow step (3)") == ""
    assert _extract_equation_number("(1) (2)") == ""


def test_extract_block_signals_collects_text_fonts_and_flags():
    block = {
        "type": 0,
        "bbox": (1, 2, 3, 4),
        "lines": [
            {
                "spans": [
                    {"text": "E = ", "font": "Times-Italic", "flags": 2},
                    {"text": "mc^2", "font": "CMMI10", "flags": 0},
                ]
            }
        ],
    }

    signals = _extract_block_signals(block)

    assert signals is not None
    text, bbox, fonts, flags = signals
    assert text == "E = mc^2"
    assert bbox == (1.0, 2.0, 3.0, 4.0)
    assert fonts == {"Times-Italic", "CMMI10"}
    assert flags == {0, 2}


def test_dedupe_candidates_keeps_best_overlapping_candidate():
    candidates = [
        FormulaCandidate(page_num=1, bbox=(0, 0, 100, 40), raw_text="a=b", confidence=0.9),
        FormulaCandidate(page_num=1, bbox=(2, 2, 98, 38), raw_text="a=b", confidence=0.7),
        FormulaCandidate(page_num=1, bbox=(200, 0, 300, 40), raw_text="c=d", confidence=0.8),
    ]

    kept = _dedupe_candidates(candidates)

    assert [c.raw_text for c in kept] == ["a=b", "c=d"]


def test_extracted_formula_searchable_text_leads_with_context_before_latex():
    formula = ExtractedFormula(
        page_num=3,
        formula_index=1,
        bbox=(0, 0, 10, 10),
        latex=r"E = mc^2",
        raw_text="E = mc^2 (1)",
        reference_context="Energy is defined by the following equation.",
        variable_gloss="where m is mass",
        equation_number="(1)",
    )

    text = formula.to_searchable_text()

    assert text.splitlines()[0] == "Formula on page 3 (1)"
    assert "Context: Energy is defined" in text
    assert text.splitlines()[-1] == r"LaTeX: E = mc^2"
