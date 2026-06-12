from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from zotpilot.feature_extraction.formula_ocr import (
    FormulaCandidate,
    SimpleTexFormulaOCRProvider,
    _candidate_confidence,
    _coerce_provider_result,
    _coerce_simpletex_response,
    _dedupe_candidates,
    _extract_block_signals,
    _extract_equation_number,
    _simpletex_app_headers,
    create_formula_ocr_provider,
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


def test_simpletex_response_coerces_latex_and_confidence():
    result = _coerce_simpletex_response({
        "status": True,
        "res": {"latex": r"E = mc^2", "conf": 0.93},
        "request_id": "tr_1",
    })

    assert result.latex == r"E = mc^2"
    assert result.confidence == 0.93


def test_simpletex_response_errors_are_actionable():
    try:
        _coerce_simpletex_response({"status": False, "err_info": "quota exceeded"})
    except RuntimeError as exc:
        assert "quota exceeded" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


@pytest.mark.parametrize(
    "payload",
    [
        {"status": True, "res": {}},
        {"status": True, "res": {"latex": "", "conf": 0.7}},
    ],
)
def test_simpletex_response_rejects_missing_latex(payload):
    with pytest.raises(RuntimeError, match="missing LaTeX"):
        _coerce_simpletex_response(payload)


def test_simpletex_provider_posts_uat_token_header():
    response = MagicMock()
    response.json.return_value = {"status": True, "res": {"latex": r"a=b", "conf": 0.8}}
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value = response

    with patch("zotpilot.feature_extraction.formula_ocr.httpx.Client", return_value=client):
        provider = SimpleTexFormulaOCRProvider(token="uat-token", endpoint="https://example.test/api")
        result = provider.recognize(b"png-bytes")

    assert result.latex == "a=b"
    _url, kwargs = client.post.call_args
    assert kwargs["headers"] == {"token": "uat-token"}
    assert kwargs["files"]["file"][0] == "formula.png"
    assert kwargs["files"]["file"][1] == b"png-bytes"


def test_simpletex_provider_wraps_invalid_json_response():
    response = MagicMock()
    response.status_code = 200
    response.headers = {}
    response.json.side_effect = ValueError("not json")
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value = response

    with patch("zotpilot.feature_extraction.formula_ocr.httpx.Client", return_value=client):
        provider = SimpleTexFormulaOCRProvider(token="uat-token", endpoint="https://example.test/api")
        with pytest.raises(RuntimeError, match="not valid JSON"):
            provider.recognize(b"png-bytes")


def test_simpletex_provider_retries_rate_limit_response():
    rate_limited = MagicMock()
    rate_limited.status_code = 429
    rate_limited.headers = {"retry-after": "0"}
    success = MagicMock()
    success.status_code = 200
    success.headers = {}
    success.json.return_value = {"status": True, "res": {"latex": r"x=y", "conf": 0.9}}
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.side_effect = [rate_limited, success]

    with patch("zotpilot.feature_extraction.formula_ocr.httpx.Client", return_value=client), \
         patch("zotpilot.feature_extraction.formula_ocr.time.sleep") as sleep:
        provider = SimpleTexFormulaOCRProvider(
            token="uat-token",
            endpoint="https://example.test/api",
            min_interval=0,
            max_retries=1,
        )
        result = provider.recognize(b"png-bytes")

    assert result.latex == "x=y"
    assert client.post.call_count == 2
    sleep.assert_called_once_with(0.0)


def test_simpletex_app_auth_resigns_each_retry_attempt():
    rate_limited = MagicMock()
    rate_limited.status_code = 429
    rate_limited.headers = {"retry-after": "0"}
    success = MagicMock()
    success.status_code = 200
    success.headers = {}
    success.json.return_value = {"status": True, "res": {"latex": r"x=y", "conf": 0.9}}
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.side_effect = [rate_limited, success]

    with patch("zotpilot.feature_extraction.formula_ocr.httpx.Client", return_value=client), \
         patch("zotpilot.feature_extraction.formula_ocr.time.sleep"), \
         patch("zotpilot.feature_extraction.formula_ocr.time.time", side_effect=[1675550577, 1675550578]), \
         patch(
             "zotpilot.feature_extraction.formula_ocr._random_simpletex_nonce",
             side_effect=["nonce-one", "nonce-two"],
         ):
        provider = SimpleTexFormulaOCRProvider(
            app_id="app-id",
            app_secret="app-secret",
            endpoint="https://example.test/api",
            min_interval=0,
            max_retries=1,
        )
        result = provider.recognize(b"png-bytes")

    first_headers = client.post.call_args_list[0].kwargs["headers"]
    second_headers = client.post.call_args_list[1].kwargs["headers"]
    assert result.latex == "x=y"
    assert first_headers["timestamp"] == "1675550577"
    assert second_headers["timestamp"] == "1675550578"
    assert first_headers["random-str"] == "nonce-one"
    assert second_headers["random-str"] == "nonce-two"
    assert first_headers["sign"] != second_headers["sign"]


def test_simpletex_provider_retries_request_error_once():
    request = httpx.Request("POST", "https://example.test/api")
    success = MagicMock()
    success.status_code = 200
    success.headers = {}
    success.json.return_value = {"status": True, "res": {"latex": r"x=y", "conf": 0.9}}
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.side_effect = [httpx.ConnectError("temporary failure", request=request), success]

    with patch("zotpilot.feature_extraction.formula_ocr.httpx.Client", return_value=client), \
         patch("zotpilot.feature_extraction.formula_ocr.time.sleep") as sleep:
        provider = SimpleTexFormulaOCRProvider(
            token="uat-token",
            endpoint="https://example.test/api",
            min_interval=0,
            max_retries=1,
        )
        result = provider.recognize(b"png-bytes")

    assert result.latex == "x=y"
    assert client.post.call_count == 2
    sleep.assert_called_once_with(0.25)


def test_simpletex_provider_does_not_retry_non_retriable_auth_error():
    request = httpx.Request("POST", "https://example.test/api")
    auth_response = httpx.Response(401, request=request)
    response = MagicMock()
    response.status_code = 401
    response.headers = {}
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "unauthorized",
        request=request,
        response=auth_response,
    )
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value = response

    with patch("zotpilot.feature_extraction.formula_ocr.httpx.Client", return_value=client):
        provider = SimpleTexFormulaOCRProvider(
            token="uat-token",
            endpoint="https://example.test/api",
            max_retries=2,
        )
        with pytest.raises(httpx.HTTPStatusError):
            provider.recognize(b"png-bytes")

    assert client.post.call_count == 1


def test_simpletex_provider_raises_after_exhausting_retriable_statuses():
    first_failure = MagicMock()
    first_failure.status_code = 503
    first_failure.headers = {}
    second_failure = MagicMock()
    second_failure.status_code = 503
    second_failure.headers = {}
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.side_effect = [first_failure, second_failure]

    with patch("zotpilot.feature_extraction.formula_ocr.httpx.Client", return_value=client), \
         patch("zotpilot.feature_extraction.formula_ocr.time.sleep") as sleep:
        provider = SimpleTexFormulaOCRProvider(
            token="uat-token",
            endpoint="https://example.test/api",
            min_interval=0,
            max_retries=1,
        )
        with pytest.raises(RuntimeError, match="exhausted retries after HTTP 503"):
            provider.recognize(b"png-bytes")

    assert client.post.call_count == 2
    sleep.assert_called_once_with(0.25)


def test_simpletex_app_headers_match_documented_signature():
    with patch("zotpilot.feature_extraction.formula_ocr.time.time", return_value=1675550577), \
         patch("zotpilot.feature_extraction.formula_ocr._random_simpletex_nonce", return_value="mSkYSY28N4WkvidB"):
        headers = _simpletex_app_headers(
            {"use_batch": "True"},
            "19X4f10YM1Va894nvFl89ikY",
            "fu4Wfmna4153DFN12ctBsPqgVI3vvGGK",
        )

    assert headers == {
        "timestamp": "1675550577",
        "random-str": "mSkYSY28N4WkvidB",
        "app-id": "19X4f10YM1Va894nvFl89ikY",
        "sign": "5f271e1deccd95d467c7dd430ca2c8b1",
    }


def test_create_simpletex_provider_uses_config_settings():
    config = SimpleNamespace(
        formula_ocr_simpletex_token="uat-token",
        formula_ocr_simpletex_app_id=None,
        formula_ocr_simpletex_app_secret=None,
        formula_ocr_simpletex_endpoint="https://server.simpletex.net/api/latex_ocr_turbo",
        formula_ocr_simpletex_timeout=12.5,
        formula_ocr_simpletex_min_interval=0.25,
        formula_ocr_simpletex_max_retries=3,
    )

    provider = create_formula_ocr_provider("simpletex", config=config)

    assert isinstance(provider, SimpleTexFormulaOCRProvider)
    assert provider._endpoint == "https://server.simpletex.net/api/latex_ocr_turbo"
    assert provider._timeout == 12.5
    assert provider._min_interval == 0.25
    assert provider._max_retries == 3


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
