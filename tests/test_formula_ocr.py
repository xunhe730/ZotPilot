import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from zotpilot.feature_extraction.formula_ocr import (
    PDF_TEXT_FALLBACK_MAX_PAGES,
    AutoFormulaCandidateProvider,
    FormulaCandidate,
    SimpleTexFormulaOCRProvider,
    TextLayerFormulaCandidateProvider,
    _assign_equation_number_statuses_from_pdf,
    _candidate_confidence,
    _coerce_provider_result,
    _coerce_simpletex_response,
    _dedupe_candidates,
    _enrich_candidate_equation_numbers_from_pdf,
    _enrich_candidate_equation_numbers_from_pdf_text,
    _extract_block_signals,
    _extract_embedded_pdf_equation_number,
    _extract_equation_number,
    _extract_pdf_block_equation_number,
    _extract_split_tail_pdf_equation_number,
    _extract_standalone_pdf_equation_number,
    _extract_wide_standalone_pdf_equation_number,
    _formula_text_match_score,
    _has_formula_relation,
    _infer_missing_equation_numbers_between_numbered,
    _is_usable_structured_formula_latex,
    _limit_ocr_needed_candidates,
    _looks_like_bibliographic_issue_number_record,
    _looks_like_equation_reference_prose_candidate,
    _looks_like_high_density_unnumbered_text_layer_noise,
    _merge_inline_equation_record_with_formula_blocks,
    _merge_number_only_pdf_candidates_with_latex_candidates,
    _merge_split_formula_candidates,
    _merge_standalone_equation_record_with_formula_block,
    _pdf_equation_record_candidate_bbox,
    _pdf_equation_records_in_reading_order,
    _PdfEquationNumberRecord,
    _remove_repeated_regular_pdf_numbers_on_page,
    _scan_pdf_equation_number_records_by_page,
    _simpletex_app_headers,
    _split_multirow_independent_formula_candidates,
    _zotero_storage_cache_scan,
    count_formula_provider_calls,
    create_formula_candidate_provider,
    create_formula_ocr_provider,
    is_high_quality_formula_latex,
    recognize_formulas,
)
from zotpilot.models import ExtractedFormula


def test_formula_latex_quality_filter_accepts_math_and_rejects_noise():
    assert is_high_quality_formula_latex(r"E = mc^2")
    assert is_high_quality_formula_latex(r"\frac{\partial L}{\partial x_i} = 0")
    assert is_high_quality_formula_latex(r"\mathrm{d}\sigma/\mathrm{d}\epsilon = E")
    assert is_high_quality_formula_latex(r"\mathsf { c } _ { \theta } ^ { s } = \frac { \sqrt { 3 } } { 2 }")
    assert _has_formula_relation(r"\hat { \varepsilon } , T , \eta = \frac { 1 } { 2 }")
    assert is_high_quality_formula_latex(
        r"x=\begin{cases}1,&\text{if }x>0\\0,&\text{otherwise}\end{cases}"
    )
    assert not is_high_quality_formula_latex("References")
    assert not is_high_quality_formula_latex("aaaaaa")
    assert not is_high_quality_formula_latex("the method is evaluated")
    assert not is_high_quality_formula_latex(r"\textbf{6061-T651 aluminum alloy mechanical behavior}")
    assert not is_high_quality_formula_latex(r"\text{0 前言}")
    assert not is_high_quality_formula_latex(
        r"\text{Abstract The mechanical behavior of Ti-5553 alloy is investigated in this work.}"
    )


def test_equation_reference_prose_filter_rejects_plural_eq_list_reference():
    text = "Eqs. (6.8), (6.9), and (6.12), whereas the effect of the prestrain is small."

    assert _looks_like_equation_reference_prose_candidate(text, "(6.9)")
    assert not is_high_quality_formula_latex(
        r"\text{Figure 2. True stress-strain curves at different temperatures.}"
    )
    assert not is_high_quality_formula_latex(
        r"\text{[12] Smith J. Constitutive modeling of titanium alloys. Journal of Materials.}"
    )
    assert not is_high_quality_formula_latex(
        r"\text{Conclusions The proposed model accurately describes the experimental results.}"
    )
    assert not is_high_quality_formula_latex(
        r"\text{The flow stress was calculated as $\sigma = F/A$ for each specimen.}"
    )
    assert not is_high_quality_formula_latex(r"\text{本文根据 $\sigma=F/A$ 计算每个试样的流动应力。}")
    assert not is_high_quality_formula_latex(r"( \mathrm { c } ) \nu _ { i } = 1 3 2 . 4 \mathrm { m } / \mathrm { s }")
    assert not is_high_quality_formula_latex(
        r"( \mathrm { c } ) \nu _ { \mathrm { i } } { = } 1 3 2 . 4 \mathrm { m } / \mathrm { s }"
    )
    assert not is_high_quality_formula_latex(
        r"\varepsilon _ { f } = [ D _ { 1 } + D _ { 2 } \tt e x p { D } \eta _ { \gamma } "
        r"\rvert ) \# ( D _ { 4 } \amalg _ { \varepsilon } ^ { * } \rfloor _ { e } \natural"
    )
    assert not is_high_quality_formula_latex(
        r"\begin{aligned}&\text{用J-C 模型拟合得到的载荷-位移曲线在材料颈缩之}\\"
        r"&\text{前与试验结果吻合很好,但是 J-C 模型不能有效预}\end{aligned}"
    )
    assert not is_high_quality_formula_latex(
        r"\begin{aligned}&\text{式中,}\eta\text{ 为应力三轴度; }A_{\mathrm{pl}},n_F,c_1,c_2"
        r"\text{ 为材料性能参数;}\\&\bar{\theta}=1-\frac{2}{\pi}\cos^{-1}(x)\quad(4)\end{aligned}"
    )
    assert not is_high_quality_formula_latex(
        r"\begin{aligned}&\text{Eq. 6:}\\&\eta=\frac{I_1}{3\sqrt{3J_2}},"
        r"&&\text{(4)}\end{aligned}"
    )


def test_cached_formula_latex_normalizes_spaced_numeric_constants(tmp_path):
    candidate = FormulaCandidate(
        page_num=1,
        bbox=(0, 0, 10, 10),
        raw_text="",
        confidence=0.95,
        equation_number="(2.1)",
        source="mineru_content_list",
        latex=r"V _ { b l } = 6 2 2 \cdot ( H / D ) ^ { 0 . 6 7 3 } - 3 3",
    )

    formulas = recognize_formulas(tmp_path / "missing.pdf", None, candidates=[candidate])

    assert len(formulas) == 1
    assert formulas[0].latex == r"V _ { b l } = 622 \cdot ( H / D ) ^ { 0.673 } - 33"


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


def test_simpletex_provider_attempt_budget_blocks_retry_before_second_http_call():
    rate_limited = MagicMock()
    rate_limited.status_code = 429
    rate_limited.headers = {"retry-after": "0"}
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value = rate_limited

    with patch("zotpilot.feature_extraction.formula_ocr.httpx.Client", return_value=client), \
         patch("zotpilot.feature_extraction.formula_ocr.time.sleep"):
        provider = SimpleTexFormulaOCRProvider(
            token="uat-token",
            endpoint="https://example.test/api",
            min_interval=0,
            max_retries=2,
        )
        provider.set_attempt_budget(1)
        with pytest.raises(RuntimeError, match="daily call budget exhausted"):
            provider.recognize(b"png-bytes")

    assert provider.attempts_used == 1
    assert client.post.call_count == 1


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


def test_formula_candidate_confidence_uses_equation_numbers_as_strong_signal():
    bbox = (10.0, 20.0, 520.0, 62.0)

    numbered_score = _candidate_confidence(
        r"\sigma_y = A + B\epsilon_p^n (1.10)",
        bbox,
        set(),
        set(),
    )
    unnumbered_score = _candidate_confidence(
        r"\sigma_y = A + B\epsilon_p^n",
        bbox,
        set(),
        set(),
    )

    assert numbered_score >= 0.75
    assert numbered_score > unnumbered_score


def test_extract_equation_number_accepts_spaced_tail_number():
    assert _extract_equation_number(r"\sigma = E\epsilon \qquad ( 7 )") == "(7)"
    assert _extract_equation_number(r"Eq. ( 1.10 )") == "(1.10)"
    assert _extract_equation_number(
        r"\sigma_{SZA} = [C_0 + C_1 \mathrm{exp}(-C_3T)] (1-c_1\eta) \qquad ( A.5 )"
    ) == "(A.5)"


def test_extract_pdf_block_equation_number_accepts_private_use_math_glyphs():
    assert _extract_pdf_block_equation_number("eq eq ( , , ) \uf073 \uf03d \uf02b (1)") == "(1)"
    assert _extract_pdf_block_equation_number("Vbl = 622 * (H/D)^0.673 - 33 ð2:1Þ") == "(2.1)"
    assert _extract_pdf_block_equation_number("\x04 \x05q \x06 \x07 ð5:1Þ") == "(5.1)"
    assert _extract_pdf_block_equation_number(r"\dot T = \chi \sigma : \dot \epsilon ð5:2Þ") == "(5.2)"
    assert _extract_pdf_block_equation_number(r"R_f = R(r) + 3G\Delta p ð23cÞ") == "(23c)"
    assert _extract_pdf_block_equation_number('" # !C2 \x02e f pl ¼ C3 ð5:5Þ') == "(5.5)"
    assert _extract_pdf_block_equation_number(") h(T) (2)") == "(2)"
    assert _extract_pdf_block_equation_number("⎠ (5)") == "(5)"
    assert _extract_pdf_block_equation_number("𝐴0 （4.12）") == "(4.12)"
    assert _extract_pdf_block_equation_number("𝐶3 （4.10）") == "(4.10)"
    assert _extract_pdf_block_equation_number("𝑙 （4.15）") == "(4.15)"
    assert _extract_pdf_block_equation_number("2R) （4.19）") == "(4.19)"
    assert _extract_pdf_block_equation_number("∆T (2.87)") == "(2.87)"
    assert _extract_pdf_block_equation_number(")2 (35a)") == "(35a)"
    assert _extract_pdf_block_equation_number(")2] (35b)") == "(35b)"
    assert _extract_pdf_block_equation_number("3 tr ( ¯b ed ) I (4)") == "(4)"
    assert _extract_pdf_block_equation_number("777775 ; ð9Þ") == "(9)"
    assert _extract_pdf_block_equation_number("u p f ð14Þ") == "(14)"
    assert _extract_pdf_block_equation_number("gjaj ð7Þ") == "(7)"
    assert _extract_pdf_block_equation_number("bak e þ bak\x111 e 2 ð9Þ") == "(9)"
    assert _extract_pdf_block_equation_number("dep ef ; ð17Þ") == "(17)"
    assert _extract_pdf_block_equation_number("seq ð12Þ") == "(12)"
    assert _extract_pdf_block_equation_number("2 max 1 1 1 3 d C ) C 1( C C (2)") == "(2)"
    assert _extract_pdf_block_equation_number("2 max 1 1 1 3 d C ) C 1( C C 1 (3)") == "(3)"
    assert _extract_pdf_block_equation_number("３ （６）") == "(６)"
    assert _extract_pdf_block_equation_number("Ｅ （３）") == "(３)"
    assert _extract_pdf_block_equation_number("1 2 3 (25)") == "(25)"
    assert _extract_pdf_block_equation_number("1 2 3 (32)") == "(32)"
    assert _extract_pdf_block_equation_number("9 σ2 xx = cʹʹ (6–1–)") == "(6-1)"
    assert _extract_pdf_block_equation_number("√ √ √ √ (12–3–)") == "(12-3)"
    assert _extract_pdf_block_equation_number(
        "Model 1 M=0.7, S=4, p=0, r=1 144 (120–160) 55 [37–66]"
    ) == ""
    assert _extract_pdf_block_equation_number("⃒⃒b (17)") == "(17)"
    assert _extract_pdf_block_equation_number(
        "Ｆ（ｖＲ）＝ｆ（ｖＲ）Ａ０＝ＹｐＡ０， ｖＲ＝ｆ －１（Ｙｐ） （２）"
    ) == "(２)"
    assert _extract_pdf_block_equation_number("４３（０５）：１３～１６．") == ""
    assert _extract_pdf_block_equation_number("Fig ð5:1Þ") == ""
    assert _extract_pdf_block_equation_number("利用式(2)对屈服强度进行拟合") == ""
    assert _extract_pdf_block_equation_number("model ð7Þ") == ""
    assert _extract_pdf_block_equation_number("result ð9Þ") == ""
    assert _extract_pdf_block_equation_number("where k ð9Þ") == ""


def test_extract_standalone_pdf_equation_number_accepts_pdf_encoded_section_numbers():
    assert _extract_standalone_pdf_equation_number("ð5:4Þ", x0=265.0, x1=284.0, page_width=595.0) == "(5.4)"
    assert _extract_standalone_pdf_equation_number("6)", x0=492.7, x1=503.6, page_width=544.2) == "(6)"
    assert _extract_standalone_pdf_equation_number("40", x0=265.0, x1=284.0, page_width=595.0) == ""
    assert _extract_standalone_pdf_equation_number("Fig", x0=265.0, x1=284.0, page_width=595.0) == ""


def test_extract_wide_standalone_pdf_equation_number_accepts_full_line_number_blocks():
    assert _extract_wide_standalone_pdf_equation_number("(66)") == "(66)"
    assert _extract_wide_standalone_pdf_equation_number("(6–1–)") == "(6-1)"
    assert _extract_wide_standalone_pdf_equation_number("(7.2-1)") == ""
    assert _extract_wide_standalone_pdf_equation_number("66") == ""


def test_extract_embedded_pdf_equation_number_accepts_mid_block_formula_numbers():
    assert _extract_embedded_pdf_equation_number(
        "𝜀𝜀̇𝑖𝑖𝑖𝑖= 𝜀𝜀̇𝑖𝑖𝑖𝑖 𝑒𝑒+ 𝜀𝜀̇𝑖𝑖𝑖𝑖 𝑝𝑝 (26) 𝜺𝜺̇ = 𝜺𝜺̇ 𝒆𝒆+ 𝜺𝜺̇ 𝒑𝒑"
    ) == "(26)"
    assert _extract_embedded_pdf_equation_number(
        "3 (𝜎𝜎1 + 𝜎𝜎2 + 𝜎𝜎3). (2) The r coordinate corresponds to the magnitude"
    ) == "(2)"
    assert _extract_embedded_pdf_equation_number("3 2ቇ, (5)") == "(5)"
    assert _extract_embedded_pdf_equation_number(
        "Combing equation (58) with equation 67 yields:"
    ) == ""
    assert _extract_embedded_pdf_equation_number(
        "Model 1 M=0.7, S=4, p=0, r=1 144 (120–160) 55 [37–66] "
        "0.107 0.155 0.06 M=0.6, S=5, p=0, r=1 152 [110–185] 41 [57–26]"
    ) == ""
    assert _extract_embedded_pdf_equation_number("the root(1) to be found is initially bracketed") == ""
    assert _extract_embedded_pdf_equation_number("data from one increment to the other(3).") == ""
    assert _extract_embedded_pdf_equation_number(
        "We added 2.5 wt % and 5.0 wt % Mo to Ti-5553 (which we refer to as "
        "Ti-5553+2.5Mo and Ti-5553+5Mo, respectively) by mechanical mixing "
        "and produced the Ti-5553 parts using refined L-PBF processing "
        "parameters for titanium alloys (30). Given the fact that the part "
        "dimension and size can affect the thermal history"
    ) == ""
    assert _extract_embedded_pdf_equation_number("ksð50Þ 3 ¼ rl m re ¼ 3 4") == ""
    assert _extract_embedded_pdf_equation_number(
        "ks(50) 1.5 2.24 2.27 2.29 2.25 Theoretical g 2.53 4.03 4.10"
    ) == ""
    assert _extract_embedded_pdf_equation_number(
        "元分析[J]. 玻璃钢/复合材料, 1997, (3): 3-7."
    ) == ""
    assert _extract_embedded_pdf_equation_number(
        "是文献［２２］在对高硬度（ＨＲＣ＝５５）３０ＣｒＭｎＳｉＮｉ２Ａ的Ｊ－Ｃ参数测定上有误差。"
        "（３）弹体硬度对侵彻转"
    ) == ""
    assert _extract_embedded_pdf_equation_number(
        "Shreyes N. Melkote (2)a,*, Wit Grzesik (2)b, Jose Outeiro (2)c, "
        "Joel Rech d, Volker Schulze (2)e, Helmi Attia (1)f, "
        "Pedro-J. Arrazola (2)g, Rachid M'Saoubi (1)h, Christopher Saldana a"
    ) == ""
    assert _extract_embedded_pdf_equation_number(
        "（1） h==6mm - （2） h=4.2mm. ⑶ h=2.8mm, （4） h=2.2mm. （5） h=1.5mm"
    ) == ""
    assert _extract_embedded_pdf_equation_number("Ceram. Soc. 73 (6), 1613–1619 (1990).") == ""
    assert _extract_embedded_pdf_equation_number(
        "Logan, R.W., Hosford, W.F., 1980. Upper-bound anisotropic yield locus "
        "calculations assuming <111>-pencil glide. Int. J. Mech. Sci. 22 (7), 419–430."
    ) == ""
    assert _extract_pdf_block_equation_number(
        "Logan, R.W., Hosford, W.F., 1980. Upper-bound anisotropic yield locus "
        "calculations assuming <111>-pencil glide. Int. J. Mech. Sci. 22 (7)"
    ) == ""
    assert _extract_embedded_pdf_equation_number(
        "Two-dimensional (2D) elasticity theory is adopted to develop"
    ) == ""


def test_high_quality_latex_accepts_tagged_formula_with_text_symbol():
    latex = (
        r"\sigma _ { \mathrm { X u } } = \sigma ( \varepsilon , \dot { \varepsilon } , T ) "
        r"( 1 - c _ { 1 } \eta ) ( 1 + c _ { 2 } | \overline { { \theta } } | )\tag{3}"
    )

    assert is_high_quality_formula_latex(latex)


def test_formula_candidate_confidence_rejects_non_formula_document_text():
    bbox = (10.0, 20.0, 520.0, 62.0)

    assert _candidate_confidence(
        "6061-T651 Aluminum Alloy Mechanical Behavior and Constitutive Model",
        bbox,
        set(),
        set(),
    ) == 0.0
    assert _candidate_confidence(
        "Wang, Y.; Li, X.; Department of Materials Science, Example University",
        bbox,
        set(),
        set(),
    ) == 0.0
    assert _candidate_confidence(
        "0 前言",
        bbox,
        {"Times-Italic"},
        {2},
    ) == 0.0


def test_text_layer_provider_does_not_reinclude_zero_confidence_metadata(tmp_path):
    import pymupdf

    pdf_path = tmp_path / "metadata-noise.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((120, 120), "HAL Id: jpa-00253379")
    page.insert_text((120, 150), "https://hal.science/jpa-00253379v1")
    page.insert_text((120, 180), "Submitted on 4 Feb 2008")
    page.insert_text((120, 230), "Ballistic failure processes in alumina")
    page.insert_text((120, 260), "D.G. Brandon, L. Baum and D. Sherman")
    page.insert_text((120, 290), "Department of Materials Engineering, Technion-Israel Institute of Technology")
    page.insert_text((120, 360), "This is a preliminary report in a new program of research.")
    page.insert_text((120, 430), "E = m c^2 (1)")
    doc.save(pdf_path)
    doc.close()

    provider = TextLayerFormulaCandidateProvider()
    candidates = provider.extract_candidates(pdf_path, min_confidence=0.0)

    assert [candidate.raw_text for candidate in candidates] == ["E = m c^2 (1)"]
    assert candidates[0].equation_number == "(1)"

    bbox = (10.0, 20.0, 520.0, 62.0)
    assert _candidate_confidence(
        "Abstract The tensile behavior of Ti-5553 alloy was studied at different strain rates.",
        bbox,
        {"CMMI10"},
        {2},
    ) == 0.0
    assert _candidate_confidence(
        "Keywords titanium alloy; ductile fracture; constitutive model; strain rate",
        bbox,
        {"Times-Italic"},
        {2},
    ) == 0.0
    assert _candidate_confidence(
        "Figure 3. Comparison between experimental and predicted true stress-strain curves.",
        bbox,
        {"CMMI10"},
        {2},
    ) == 0.0
    assert _candidate_confidence(
        "Table 2. Chemical composition and mechanical properties of tested specimens.",
        bbox,
        {"CMMI10"},
        {2},
    ) == 0.0
    assert _candidate_confidence(
        "图 4 断口形貌及韧窝分布特征。",
        bbox,
        {"CMMI10"},
        {2},
    ) == 0.0
    assert _candidate_confidence(
        "[18] Johnson A. Damage evolution in titanium alloys. Materials Science Journal.",
        bbox,
        {"CMMI10"},
        {2},
    ) == 0.0
    assert _candidate_confidence(
        "https://doi.org/10.1016/j.example.2024.100123",
        bbox,
        {"CMMI10"},
        {2},
    ) == 0.0
    assert _candidate_confidence(
        "Conclusions The model captures the strain-rate and temperature effects well.",
        bbox,
        {"CMMI10"},
        {2},
    ) == 0.0
    assert _candidate_confidence(
        "The flow stress was calculated as sigma = F / A for each tested specimen.",
        bbox,
        {"CMMI10"},
        {2},
    ) == 0.0
    assert _candidate_confidence(
        "本文根据 sigma = F / A 计算每个试样的流动应力。",
        bbox,
        {"CMMI10"},
        {2},
    ) == 0.0
    assert _candidate_confidence(
        "The value k = 3 was used.",
        bbox,
        {"CMMI10"},
        {2},
    ) == 0.0
    assert _candidate_confidence(
        "As shown in [12], sigma = F / A.",
        bbox,
        {"CMMI10"},
        {2},
    ) == 0.0
    assert _candidate_confidence(
        "sigma = F / A",
        bbox,
        set(),
        set(),
    ) >= 0.6


def test_text_layer_candidate_provider_delegates_to_default_extractor(tmp_path):
    provider = TextLayerFormulaCandidateProvider()

    with patch(
        "zotpilot.feature_extraction.formula_ocr._extract_text_layer_formula_candidates",
        return_value=[],
    ) as extract:
        candidates = provider.extract_candidates(tmp_path / "paper.pdf", max_candidates_per_doc=3)

    assert candidates == []
    assert extract.call_args.kwargs["max_candidates_per_doc"] == 3


def test_text_layer_candidate_provider_respects_batch_candidate_detection_limit(tmp_path):
    visited_pages: list[int] = []

    class FakePage:
        def __init__(self, page_index: int) -> None:
            self.page_index = page_index

        def get_text(self, mode: str = "text"):
            blocks = []
            texts = []
            for index in range(2):
                number = self.page_index * 2 + index + 1
                text = rf"sigma_{{{number}}} = epsilon_{{{number}}} + 1 ({number})"
                texts.append(text)
                blocks.append({
                    "type": 0,
                    "bbox": (40.0, 100.0 + index * 40, 360.0, 122.0 + index * 40),
                    "lines": [{
                        "spans": [{
                            "text": text,
                            "font": "CMMI10",
                            "flags": 1,
                        }],
                    }],
                })
            if mode == "dict":
                return {"blocks": blocks}
            return " ".join(texts)

    class FakeDoc:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def __iter__(self):
            for page_index in range(5):
                visited_pages.append(page_index)
                yield FakePage(page_index)

    provider = TextLayerFormulaCandidateProvider()

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=FakeDoc()):
        candidates = provider.extract_candidates(
            tmp_path / "paper.pdf",
            max_formulas_per_doc=0,
            max_formulas_per_page=2,
            max_candidates_per_doc=3,
        )

    assert len(candidates) == 3
    assert [candidate.equation_number for candidate in candidates] == ["(1)", "(2)", "(3)"]
    assert visited_pages == [0, 1]


def test_text_layer_candidate_provider_uses_audit_limit_before_ocr_doc_limit(tmp_path):
    visited_pages: list[int] = []

    class FakePage:
        def __init__(self, page_index: int) -> None:
            self.page_index = page_index

        def get_text(self, mode: str = "text"):
            number = self.page_index + 1
            text = rf"sigma_{{{number}}} = epsilon_{{{number}}} + 1 ({number})"
            if mode == "dict":
                return {
                    "blocks": [{
                        "type": 0,
                        "bbox": (40.0, 100.0, 360.0, 122.0),
                        "lines": [{
                            "spans": [{
                                "text": text,
                                "font": "CMMI10",
                                "flags": 1,
                            }],
                        }],
                    }]
                }
            return text

    class FakeDoc:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def __iter__(self):
            for page_index in range(6):
                visited_pages.append(page_index)
                yield FakePage(page_index)

    provider = TextLayerFormulaCandidateProvider()

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=FakeDoc()):
        candidates = provider.extract_candidates(
            tmp_path / "paper.pdf",
            max_formulas_per_doc=2,
            max_formulas_per_page=1,
            max_candidates_per_doc=4,
        )

    assert len(candidates) == 4
    assert [candidate.equation_number for candidate in candidates] == ["(1)", "(2)", "(3)", "(4)"]
    assert visited_pages == [0, 1, 2, 3]


def test_auto_candidate_provider_skips_text_layer_for_complete_structured_cache(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [10, 20, 200, 60],
                "text": r"E = mc^2",
                "equation_number": "(1)",
                "confidence": 0.95,
            }
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "auto",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "mineru-cache")),
    )

    with patch.object(
        TextLayerFormulaCandidateProvider,
        "extract_candidates",
        side_effect=AssertionError("text-layer fallback should not run"),
    ):
        candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].latex == r"E = mc^2"
    assert candidates[0].source == "mineru_content_list"


def test_auto_candidate_provider_merges_text_layer_when_structured_cache_is_incomplete(tmp_path):
    structured_candidate = FormulaCandidate(
        page_num=1,
        bbox=(10, 20, 200, 60),
        raw_text="",
        confidence=0.95,
        source="mineru_content_list",
    )
    text_candidate = FormulaCandidate(
        page_num=2,
        bbox=(10, 80, 200, 120),
        raw_text=r"\sigma = E\epsilon (2)",
        confidence=0.8,
        equation_number="(2)",
        source="text_layer",
    )
    text_noise = FormulaCandidate(
        page_num=2,
        bbox=(10, 140, 200, 180),
        raw_text="The result is compared with the experiment as discussed in Eq. (3) and Fig. 2.",
        confidence=0.8,
        equation_number="(3)",
        source="text_layer",
    )
    provider = create_formula_candidate_provider("auto", config=SimpleNamespace(formula_candidate_cache_dirs=""))

    with (
        patch(
            "zotpilot.feature_extraction.formula_ocr.MinerUCacheFormulaCandidateProvider.extract_candidates",
            return_value=[structured_candidate],
        ) as structured_extract,
        patch.object(
            TextLayerFormulaCandidateProvider,
            "extract_candidates",
            return_value=[text_candidate, text_noise],
        ) as text_extract,
    ):
        candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 2
    assert {candidate.source for candidate in candidates} == {"mineru_content_list", "text_layer"}
    assert {candidate.raw_text for candidate in candidates} == {structured_candidate.raw_text, text_candidate.raw_text}
    structured_extract.assert_called_once()
    text_extract.assert_called_once()


def test_auto_candidate_provider_does_not_open_pdf_for_unnumbered_cached_latex(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [10, 20, 200, 60],
                "text": r"E = mc^2",
                "confidence": 0.95,
            }
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "auto",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "mineru-cache")),
    )

    with (
        patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open") as pdf_open,
        patch.object(
            TextLayerFormulaCandidateProvider,
            "extract_candidates",
            side_effect=AssertionError("text-layer fallback should not run for cached LaTeX"),
        ),
    ):
        candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].latex == r"E = mc^2"
    assert candidates[0].equation_number == ""
    assert candidates[0].equation_number_status == "missing"
    pdf_open.assert_not_called()


def test_auto_candidate_provider_falls_back_to_text_layer_when_structured_provider_is_empty(tmp_path):
    text_candidate = FormulaCandidate(
        page_num=1,
        bbox=(10, 20, 200, 60),
        raw_text=r"\sigma = E\epsilon",
        confidence=0.8,
        source="text_layer",
    )
    provider = create_formula_candidate_provider("auto", config=SimpleNamespace(formula_candidate_cache_dirs=""))

    with (
        patch(
            "zotpilot.feature_extraction.formula_ocr.MinerUCacheFormulaCandidateProvider.extract_candidates",
            return_value=[],
        ) as structured_extract,
        patch.object(
            TextLayerFormulaCandidateProvider,
            "extract_candidates",
            return_value=[text_candidate],
        ) as text_extract,
    ):
        candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert candidates == [text_candidate]
    structured_extract.assert_called_once()
    text_extract.assert_called_once()


def test_auto_candidate_provider_retries_full_pdf_fallback_for_unnumbered_text_layer_noise(tmp_path):
    text_noise = [
        FormulaCandidate(
            page_num=(index // 6) + 1,
            bbox=(10, 20 + index, 200, 40 + index),
            raw_text=f"上海交通大学博士学位论文 {index}",
            confidence=0.0,
            source="text_layer",
        )
        for index in range(90)
    ]
    full_fallback_candidate = FormulaCandidate(
        page_num=118,
        bbox=(10, 20, 200, 60),
        raw_text=r"\sigma = E\epsilon (1)",
        confidence=0.72,
        equation_number="(1)",
        source="pdf_text_equation_number",
    )
    provider = AutoFormulaCandidateProvider(cache_dirs=())

    with (
        patch(
            "zotpilot.feature_extraction.formula_ocr.MinerUCacheFormulaCandidateProvider.extract_candidates",
            side_effect=[[], [full_fallback_candidate]],
        ) as structured_extract,
        patch.object(
            TextLayerFormulaCandidateProvider,
            "extract_candidates",
            return_value=text_noise,
        ) as text_extract,
    ):
        candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert _looks_like_high_density_unnumbered_text_layer_noise(text_noise)
    assert candidates == [full_fallback_candidate]
    assert structured_extract.call_count == 2
    assert structured_extract.call_args_list[1].kwargs["pdf_fallback_max_pages"] == 0
    text_extract.assert_called_once()


def test_mineru_cache_provider_reads_content_list_formula_latex(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 2,
                "bbox": [10, 20, 200, 60],
                "text": r"\sigma = E\epsilon",
                "confidence": 0.91,
            },
            {"type": "text", "text": "Introduction"},
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
            formula_candidate_pdf_number_append_missing_candidates=True,
        ),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].page_num == 3
    assert candidates[0].bbox == (10.0, 20.0, 200.0, 60.0)
    assert candidates[0].latex == r"\sigma = E\epsilon"
    assert candidates[0].source == "mineru_content_list"
    assert count_formula_provider_calls(candidates) == 0


def test_mineru_cache_provider_normalizes_bracketed_chapter_equation_number(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 2,
                "bbox": [10, 20, 200, 60],
                "text": r"\sigma = E\epsilon",
                "equation_number": "[3.12]",
                "confidence": 0.91,
            },
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
            formula_candidate_pdf_number_append_missing_candidates=True,
        ),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == ["(3.12)"]


def test_mineru_cache_provider_keeps_tagged_xu_formula(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    latex = (
        r"\sigma _ { \mathrm { X u } } = \sigma ( \varepsilon , \dot { \varepsilon } , T ) "
        r"( 1 - c _ { 1 } \eta ) ( 1 + c _ { 2 } | \overline { { \theta } } | )\tag{3}"
    )
    (cache_dir / "content_list.json").write_text(
        json.dumps([{"type": "equation", "page_idx": 7, "bbox": [510, 914, 712, 928], "text": f"$$\n{latex}\n$$"}]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
            formula_candidate_pdf_number_append_missing_candidates=True,
        ),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].equation_number == "(3)"
    assert candidates[0].page_num == 8
    assert candidates[0].latex == latex
    assert count_formula_provider_calls(candidates) == 0


def test_mineru_cache_provider_reads_explicit_equation_number_field(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "equation",
                "page_idx": 0,
                "bbox": [10, 20, 200, 60],
                "text": r"$$E = mc^2$$",
                "eq_number": "2.1",
            }
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
            formula_candidate_pdf_number_append_missing_candidates=True,
        ),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].equation_number == "(2.1)"


def test_mineru_cache_provider_recovers_equation_numbers_from_pdf_text(tmp_path):
    import pymupdf

    pdf_path = tmp_path / "paper.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((120, 180), "E = mc^2 (1)")
    page.insert_text((120, 280), r"sigma = E epsilon (2)")
    page.insert_text((120, 380), "sigma_eq , (3)")
    page.insert_text((520, 480), ", (4)")
    page = doc.new_page(width=600, height=800)
    page.insert_text((520, 180), "(5)")
    doc.save(pdf_path)
    doc.close()

    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [100, 170, 260, 195],
                "text": r"E = mc^2",
                "confidence": 0.9,
            },
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [100, 270, 320, 295],
                "text": r"\sigma = E\epsilon",
                "confidence": 0.9,
            },
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [100, 370, 320, 395],
                "text": r"\rho C_p",
                "confidence": 0.9,
            },
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [100, 470, 320, 495],
                "text": r"\frac{\sigma}{\epsilon}",
                "confidence": 0.9,
            },
            {
                "type": "interline_equation",
                "page_idx": 1,
                "bbox": [100, 170, 320, 195],
                "text": r"\Delta T = \int \sigma\,d\epsilon",
                "confidence": 0.9,
            },
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == ["(1)", "(2)", "(3)", "(4)", "(5)"]


def test_pdf_equation_records_reading_order_handles_two_column_pages():
    records = [
        _PdfEquationNumberRecord("(2)", 175.0, 284.0, False, (260.0, 168.0, 284.0, 184.0), "A (2)", 600.0, 800.0),
        _PdfEquationNumberRecord("(6)", 225.0, 544.0, False, (520.0, 218.0, 544.0, 234.0), "E (6)", 600.0, 800.0),
        _PdfEquationNumberRecord("(3)", 275.0, 284.0, False, (260.0, 268.0, 284.0, 284.0), "B (3)", 600.0, 800.0),
        _PdfEquationNumberRecord("(4)", 375.0, 284.0, False, (260.0, 368.0, 284.0, 384.0), "C (4)", 600.0, 800.0),
        _PdfEquationNumberRecord("(5)", 475.0, 284.0, False, (260.0, 468.0, 284.0, 484.0), "D (5)", 600.0, 800.0),
    ]

    ordered = _pdf_equation_records_in_reading_order(records)

    assert [record.number for record in ordered] == ["(2)", "(3)", "(4)", "(5)", "(6)"]


def test_standalone_equation_number_record_merges_nearby_formula_block():
    record = _PdfEquationNumberRecord(
        number="(1)",
        y_center=580.5,
        x_right=509.7,
        standalone=True,
        bbox=(493.0, 573.9, 509.7, 587.0),
        text="(1)",
        page_width=544.2,
        page_height=742.7,
    )
    blocks = [
        (
            (54.9, 540.2, 488.9, 553.3),
            "Eq. (1) is defined to specify the damage accumulation",
        ),
        (
            (77.2, 568.0, 370.7, 598.7),
            ", x x x x σ σ τ, , ε σ τ σ σ F p \uf0ee \uf0ed \uf0ec \uf0b3 "
            "\uf03d \uf03d \uf0a3 \uf0a3 \uf02d \uf02b \uf03d ﹤0 ， 0 0",
        ),
    ]

    merged = _merge_standalone_equation_record_with_formula_block(record, blocks)
    bbox = _pdf_equation_record_candidate_bbox(merged)

    assert not merged.standalone
    assert "σ σ τ" in merged.text
    assert merged.bbox[0] <= 77.2
    assert bbox[0] < 80.0
    assert bbox[2] >= 509.7


def test_inline_equation_number_record_merges_fragmented_formula_blocks():
    record = _PdfEquationNumberRecord(
        number="(2)",
        y_center=690.7,
        x_right=293.3,
        standalone=False,
        bbox=(128.4, 680.5, 293.3, 700.9),
        text=") h(T) (2)",
        page_width=595.0,
        page_height=842.0,
    )
    blocks = [
        ((37.6, 645.7, 291.0, 679.6), "The plastic deformation behavior is described by a model."),
        ((37.6, 691.0, 46.7, 699.9), "σeq"),
        ((47.2, 680.5, 53.1, 687.6), "("),
        ((53.1, 680.5, 107.8, 699.9), "εp, ˙εp, T ) = f ( εp"),
        ((108.3, 680.5, 127.9, 699.9), ") g ( ˙εp"),
        ((128.4, 680.5, 293.3, 700.9), ") h(T) (2)"),
        ((37.6, 712.9, 291.0, 736.8), "where σeq is the von Mises stress."),
    ]

    merged = _merge_inline_equation_record_with_formula_blocks(record, blocks)
    bbox = _pdf_equation_record_candidate_bbox(merged)

    assert merged.bbox[0] <= 37.6
    assert merged.bbox[2] >= 293.3
    assert "σeq" in merged.text
    assert "h(T) (2)" in merged.text
    assert bbox[0] < 40.0
    assert "plastic deformation" not in merged.text
    assert "von Mises" not in merged.text


def test_inline_equation_number_record_does_not_merge_other_numbered_blocks():
    record = _PdfEquationNumberRecord(
        number="(4)",
        y_center=707.3,
        x_right=562.3,
        standalone=False,
        bbox=(313.9, 699.0, 562.3, 715.6),
        text="fV = A + Q (1 - e^{-beta epsilon}) (4)",
        page_width=595.0,
        page_height=842.0,
    )
    blocks = [
        ((37.6, 680.5, 293.3, 700.9), "sigma = f(epsilon) g(epsilon) h(T) (2)"),
        ((306.6, 669.8, 562.4, 686.1), "f(epsilon) = alpha fL + (1-alpha) fV (3)"),
        ((306.6, 691.6, 373.9, 706.0), "{ fL = A + B epsilon^n"),
        ((313.9, 699.0, 562.3, 715.6), "fV = A + Q (1 - e^{-beta epsilon}) (4)"),
    ]

    merged = _merge_inline_equation_record_with_formula_blocks(record, blocks)

    assert "(2)" not in merged.text
    assert "(3)" not in merged.text
    assert "(4)" in merged.text
    assert "fL" in merged.text
    assert "fV" in merged.text


def test_inline_equation_number_record_does_not_merge_cjk_prose_context():
    record = _PdfEquationNumberRecord(
        number="(3)",
        y_center=727.2,
        x_right=542.4,
        standalone=False,
        bbox=(308.6, 720.0, 542.4, 734.5),
        text="sigma = alpha + beta epsilon (3)",
        page_width=595.0,
        page_height=842.0,
    )
    blocks = [
        ((55.3, 701.8, 287.1, 744.2), "用J-C 模型拟合得到的载荷-位移曲线在材料颈缩之前与试验结果吻合很好"),
        ((55.3, 748.3, 287.1, 775.7), "测材料缩颈之后的载荷-位移曲线。因此，对J-C 本构模型进行修正，如式(3)所示"),
        ((308.6, 720.0, 542.4, 734.5), "sigma = alpha + beta epsilon (3)"),
        ((308.3, 746.0, 541.9, 773.5), "式中，Q、β 分别与B、n 相似，分别为硬化系数和硬化指数。"),
    ]

    merged = _merge_inline_equation_record_with_formula_blocks(record, blocks)

    assert "sigma = alpha" in merged.text
    assert "载荷" not in merged.text
    assert "式中" not in merged.text


def test_inline_equation_number_record_does_not_merge_cjk_formula_explanation():
    record = _PdfEquationNumberRecord(
        number="(7)",
        y_center=640.8,
        x_right=542.6,
        standalone=False,
        bbox=(308.3, 633.7, 542.6, 647.9),
        text="epsilon_f = D_1 + D_2 exp(D_3 eta) (7)",
        page_width=595.0,
        page_height=842.0,
    )
    blocks = [
        ((55.9, 635.9, 292.4, 651.1), "eq eq 0 / \uf065 \uf065 \uf065 \uf03d 为无量纲化应变率；当前应变率；"),
        ((308.3, 633.7, 542.6, 647.9), "epsilon_f = D_1 + D_2 exp(D_3 eta) (7)"),
    ]

    merged = _merge_inline_equation_record_with_formula_blocks(record, blocks)

    assert "epsilon_f" in merged.text
    assert "无量纲" not in merged.text
    assert "当前应变率" not in merged.text


def test_standalone_then_inline_merge_expands_long_fragmented_formula():
    record = _PdfEquationNumberRecord(
        number="(9)",
        y_center=391.6,
        x_right=564.7,
        standalone=True,
        bbox=(547.2, 385.1, 564.7, 398.1),
        text="(9)",
        page_width=595.0,
        page_height=842.0,
    )
    blocks = [
        ((37.6, 366.5, 42.9, 375.4), "εf"),
        ((43.7, 358.3, 88.0, 375.1), "( ˙ε, T, η, θ ) ="),
        ((90.2, 353.6, 96.6, 360.8), "{"),
        ((109.1, 365.8, 123.7, 376.6), "cs θ +"),
        ((234.3, 353.6, 295.6, 370.0), ")]}−1 n{ ̅̅̅̅ 1 + c12"),
        ((415.7, 365.3, 448.8, 375.4), "⎣1 + D4ln"),
        ((448.8, 361.1, 461.9, 372.5), "⎝˙εp"),
        ((455.8, 372.0, 461.9, 380.9), "˙ε0"),
        ((469.4, 356.0, 500.0, 375.4), "⎦ [ 1 + D5"),
        ((500.5, 355.7, 529.8, 370.0), "( T −Tr"),
        ((506.4, 372.4, 532.0, 381.0), "Tm −Tr"),
        ((532.5, 356.0, 542.6, 363.1), ")]"),
        ((547.2, 385.1, 564.7, 398.1), "(9)"),
    ]

    merged_tail = _merge_standalone_equation_record_with_formula_block(record, blocks)
    merged = _merge_inline_equation_record_with_formula_blocks(merged_tail, blocks)

    assert not merged.standalone
    assert merged.bbox[0] <= 37.6
    assert merged.bbox[2] >= 564.7
    assert "εf" in merged.text
    assert "(9)" in merged.text


def test_mineru_cache_provider_merges_adjacent_formula_continuations(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [130, 394, 442, 431],
                "text": r"\varepsilon_f = \left\{ \frac{A_{pl}}{c_2}",
                "confidence": 0.92,
            },
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [144, 434, 425, 477],
                "text": r"\left[ \sqrt{\frac{1+c_1^2}{3}} + c_1\eta \right]",
                "confidence": 0.91,
            },
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].bbox == (130.0, 394.0, 442.0, 477.0)
    assert r"\varepsilon_f" in candidates[0].latex
    assert r"\sqrt{\frac{1+c_1^2}{3}}" in candidates[0].latex


def test_mineru_cache_pdf_number_enrichment_does_not_append_ocr_candidates_by_default(tmp_path):
    import pymupdf

    pdf_path = tmp_path / "paper.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((120, 180), "E = mc^2 (1)")
    page.insert_text((520, 280), "(2)")
    doc.save(pdf_path)
    doc.close()

    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [100, 170, 260, 195],
                "text": r"E = mc^2",
                "confidence": 0.9,
            },
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == ["(1)"]
    assert count_formula_provider_calls(candidates) == 0


def test_mineru_cache_provider_adds_pdf_numbered_bbox_candidates_for_cache_misses(tmp_path):
    import pymupdf

    pdf_path = tmp_path / "paper.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((120, 180), "E = mc^2 (1)")
    page.insert_text((520, 280), "(2)")
    doc.save(pdf_path)
    doc.close()

    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [100, 170, 260, 195],
                "text": r"E = mc^2",
                "confidence": 0.9,
            },
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
            formula_candidate_pdf_number_append_missing_candidates=True,
        ),
    )

    candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == ["(1)", "(2)"]
    assert candidates[1].latex == ""
    assert candidates[1].source == "pdf_text_equation_number"
    assert candidates[1].bbox_coordinate_space == "pdf"
    assert count_formula_provider_calls(candidates) == 1


def test_mineru_cache_provider_fills_pdf_numbered_sequence_gaps(tmp_path):
    import pymupdf

    pdf_path = tmp_path / "paper.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((120, 120), "a = b (1)")
    page.insert_text((120, 180), "c = d (2)")
    page.insert_text((120, 240), "e = f (3)")
    page.insert_text((120, 300), "g = h (4)")
    page.insert_text((120, 360), "i = j (5)")
    doc.save(pdf_path)
    doc.close()

    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {"type": "equation", "page_idx": 0, "bbox": [100, 110, 220, 135], "text": r"a = b\tag{1}"},
            {"type": "equation", "page_idx": 0, "bbox": [100, 170, 220, 195], "text": r"c = d\tag{2}"},
            {"type": "equation", "page_idx": 0, "bbox": [100, 350, 220, 375], "text": r"i = j\tag{5}"},
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
            formula_candidate_pdf_number_append_missing_candidates=True,
        ),
    )

    candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == ["(1)", "(2)", "(3)", "(4)", "(5)"]
    assert [candidate.source for candidate in candidates[2:4]] == [
        "pdf_text_equation_number",
        "pdf_text_equation_number",
    ]
    assert count_formula_provider_calls(candidates) == 2


def test_ocr_candidate_limit_preserves_dense_numbered_page_sequence():
    candidates = [
        FormulaCandidate(
            page_num=1,
            bbox=(50.0, 40.0 + index * 20.0, 260.0, 54.0 + index * 20.0),
            raw_text=f"x_{index + 1} = y_{index + 1} ({index + 1})",
            confidence=0.72,
            equation_number=f"({index + 1})",
            source="pdf_text_equation_number",
        )
        for index in range(7)
    ]

    limited = _limit_ocr_needed_candidates(
        candidates,
        max_formulas_per_page=6,
        max_formulas_per_doc=0,
    )

    assert [candidate.equation_number for candidate in limited] == [
        "(1)",
        "(2)",
        "(3)",
        "(4)",
        "(5)",
        "(6)",
        "(7)",
    ]


def test_ocr_candidate_limit_preserves_dense_hyphen_chapter_number_sequence():
    candidates = [
        FormulaCandidate(
            page_num=1,
            bbox=(50.0, 40.0 + index * 20.0, 260.0, 54.0 + index * 20.0),
            raw_text=f"x_{index + 1} = y_{index + 1} (3-{index + 1})",
            confidence=0.72,
            equation_number=f"(3-{index + 1})",
            source="pdf_text_equation_number",
        )
        for index in range(8)
    ]

    limited = _limit_ocr_needed_candidates(
        candidates,
        max_formulas_per_page=6,
        max_formulas_per_doc=0,
    )

    assert [candidate.equation_number for candidate in limited] == [
        "(3-1)",
        "(3-2)",
        "(3-3)",
        "(3-4)",
        "(3-5)",
        "(3-6)",
        "(3-7)",
        "(3-8)",
    ]


def test_split_tail_equation_number_rejects_cjk_formula_reference_text():
    text = "变，如式(9)所示。高速拉伸试样断裂应变通过式(10)"

    assert _extract_split_tail_pdf_equation_number(text, x1=540.0, page_width=595.0) == ""


def test_embedded_equation_number_rejects_internal_parenthetical_factor():
    text = "sigma = alpha + beta (1) [1 + C ln eps] (1 - T)"

    assert _extract_embedded_pdf_equation_number(text) == ""


def test_bibliographic_issue_number_record_rejects_merged_reference_line():
    text = (
        "Logan, R.W., Hosford, W.F., 1980. Upper-bound anisotropic yield locus "
        "calculations assuming <111>-pencil glide. Int. J. Mech. Sci. 22 (7), 419–430."
    )

    assert _looks_like_bibliographic_issue_number_record(text, "(7)")
    assert not _looks_like_bibliographic_issue_number_record(r"\sigma = E\epsilon (7)", "(7)")


def test_repeated_regular_pdf_numbers_keep_first_reading_order_record():
    true_record = _PdfEquationNumberRecord(
        number="(1)",
        y_center=560.0,
        x_right=290.0,
        standalone=False,
        bbox=(55.0, 550.0, 290.0, 570.0),
        text="epsilon_f = ... (1)",
        page_width=595.0,
        page_height=842.0,
    )
    false_internal = _PdfEquationNumberRecord(
        number="(1)",
        y_center=405.0,
        x_right=524.0,
        standalone=False,
        bbox=(309.0, 389.0, 524.0, 420.0),
        text="sigma = ... (1)(1)",
        page_width=595.0,
        page_height=842.0,
    )

    deduped = _remove_repeated_regular_pdf_numbers_on_page([false_internal, true_record])

    assert deduped == [true_record]


def test_unknown_cache_bbox_uses_clear_low_score_text_match_for_numbering():
    candidate = FormulaCandidate(
        page_num=1,
        bbox=(652.0, 353.0, 766.0, 377.0),
        raw_text=r"D = \sum \Delta \varepsilon_{eq} / \varepsilon_f",
        latex=r"D = \sum \Delta \varepsilon _ { \mathrm { e q } } / \varepsilon _ { \mathrm { f } }",
        confidence=0.91,
        source="mineru_content_list",
        bbox_coordinate_space="unknown",
    )
    records = [
        _PdfEquationNumberRecord(
            number="(4)",
            y_center=245.0,
            x_right=542.0,
            standalone=False,
            bbox=(314.0, 231.0, 542.0, 261.0),
            text="27 2 cos theta sigma sigma (4)",
            page_width=595.0,
            page_height=842.0,
        ),
        _PdfEquationNumberRecord(
            number="(5)",
            y_center=307.0,
            x_right=542.0,
            standalone=False,
            bbox=(308.0, 297.0, 542.0, 318.0),
            text="eq f / D epsilon epsilon = Delta sum (5)",
            page_width=595.0,
            page_height=842.0,
        ),
    ]

    enriched = _enrich_candidate_equation_numbers_from_pdf_text([candidate], {1: records})

    assert enriched[0].equation_number == "(5)"


def test_unknown_cache_page_order_matches_numbered_records_when_counts_align():
    candidates = [
        FormulaCandidate(
            page_num=31,
            bbox=(376.0, 367.0, 591.0, 388.0),
            raw_text="",
            confidence=0.95,
            latex=(
                r"\sigma _ { \mathrm { e q } } = f \big ( \varepsilon _ { \mathrm { e q } } \big ) "
                r"g \big ( \varepsilon _ { \mathrm { e q } } \big ) h ( T )"
            ),
            source="mineru_content_list",
            bbox_coordinate_space="unknown",
        ),
        FormulaCandidate(
            page_num=31,
            bbox=(218.0, 453.0, 747.0, 475.0),
            raw_text="",
            confidence=0.95,
            latex=(
                r"\sigma _ { \mathrm { e q } } = \big ( A + B \varepsilon _ { \mathrm { e q } } "
                r"{ } ^ { n } \big ) \big ( 1 + C l n \varepsilon _ { \mathrm { ~ e q } } "
                r"^ { \ast } \big )"
            ),
            source="mineru_content_list",
            bbox_coordinate_space="unknown",
        ),
        FormulaCandidate(
            page_num=31,
            bbox=(275.0, 604.0, 692.0, 642.0),
            raw_text="",
            confidence=0.95,
            latex=(
                r"\sigma _ { \mathrm { e q } } = \sqrt { \frac { 1 } { 2 } "
                r"[ ( \sigma _ { 1 } - \sigma _ { 2 } ) ^ { 2 } + "
                r"( \sigma _ { 2 } - \sigma _ { 3 } ) ^ { 2 } ] }"
            ),
            source="mineru_content_list_row",
            bbox_coordinate_space="unknown",
        ),
        FormulaCandidate(
            page_num=31,
            bbox=(275.0, 642.0, 692.0, 680.0),
            raw_text="",
            confidence=0.95,
            latex=(
                r"\varepsilon _ { \mathrm { e q } } = \sqrt { \frac { 2 } { 9 } "
                r"[ ( \varepsilon _ { 1 } - \varepsilon _ { 2 } ) ^ { 2 } + "
                r"( \varepsilon _ { 2 } - \varepsilon _ { 3 } ) ^ { 2 } ] }"
            ),
            source="mineru_content_list_row",
            bbox_coordinate_space="unknown",
        ),
        FormulaCandidate(
            page_num=31,
            bbox=(398.0, 808.0, 571.0, 829.0),
            raw_text="",
            confidence=0.95,
            latex=r"\xi _ { \mathrm { e q } } = f ( \eta , L , \xi _ { \mathrm { e q } } , T )",
            source="mineru_content_list",
            bbox_coordinate_space="unknown",
        ),
    ]
    records_by_page = {
        31: [
            _PdfEquationNumberRecord(
                "(4.1)", 319.0, 507.7, False, (453.0, 312.0, 507.7, 326.0), "fg h (4.1)", 595.0, 842.0,
            ),
            _PdfEquationNumberRecord(
                "(4.2)", 404.5, 507.7, False, (218.0, 397.0, 507.7, 412.0), "long jc (4.2)", 595.0, 842.0,
            ),
            _PdfEquationNumberRecord(
                "(4.3)",
                541.7,
                507.7,
                False,
                (333.0, 530.0, 507.7, 550.0),
                "sigma sqrt eps sqrt (4.3)",
                595.0,
                842.0,
            ),
            _PdfEquationNumberRecord(
                "(4.4)",
                541.7,
                507.7,
                False,
                (333.0, 555.0, 507.7, 575.0),
                "sigma sqrt eps sqrt (4.4)",
                595.0,
                842.0,
            ),
            _PdfEquationNumberRecord(
                "(4.5)",
                689.6,
                507.7,
                False,
                (398.0, 682.0, 507.7, 697.0),
                "xi eq f eta L T (4.5)",
                595.0,
                842.0,
            ),
        ]
    }

    enriched = _enrich_candidate_equation_numbers_from_pdf("ignored.pdf", candidates, records_by_page=records_by_page)

    assert [candidate.equation_number for candidate in enriched] == ["(4.1)", "(4.2)", "(4.3)", "(4.4)", "(4.5)"]


def test_standalone_number_merges_same_line_fragmented_spans_before_nearby_formula():
    record = _PdfEquationNumberRecord(
        number="(10)",
        y_center=138.5,
        x_right=289.6,
        standalone=True,
        bbox=(269.5, 132.7, 289.6, 144.4),
        text="(10)",
        page_width=595.0,
        page_height=842.0,
    )
    blocks = [
        ((194.8, 111.4, 204.5, 124.6), "\uf065\uf02a"),
        ((137.8, 132.1, 146.6, 144.4), "\uf065"),
        ((151.9, 132.1, 157.6, 144.4), "\uf03d"),
        ((159.6, 133.3, 170.7, 144.4), "ln("),
        ((171.6, 133.3, 178.0, 144.4), "A"),
        ((182.9, 133.3, 185.8, 144.4), "/"),
        ((188.7, 133.3, 195.0, 144.4), "A"),
        ((198.1, 133.3, 201.5, 144.4), ")"),
    ]

    merged = _merge_standalone_equation_record_with_formula_block(record, blocks)

    assert not merged.standalone
    assert merged.bbox[1] >= 132.0
    assert "ln(" in merged.text
    assert "\uf065\uf02a" not in merged.text


def test_mineru_cache_provider_does_not_append_cross_page_duplicate_pdf_numbers(tmp_path):
    import pymupdf

    pdf_path = tmp_path / "paper.pdf"
    doc = pymupdf.open()
    doc.new_page(width=600, height=800).insert_text((120, 120), "table values 0.90 -0.16 41 (4)")
    doc.save(pdf_path)
    doc.close()

    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {"type": "equation", "page_idx": 0, "bbox": [100, 110, 220, 135], "text": r"a = b\tag{4}"},
            {"type": "equation", "page_idx": 0, "bbox": [100, 170, 220, 195], "text": r"c = d"},
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates].count("(4)") == 1
    assert all(candidate.source != "pdf_text_equation_number" for candidate in candidates)


def test_mineru_cache_provider_infers_single_leading_missing_number(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "equation",
                "page_idx": 1,
                "bbox": [100, 90, 280, 120],
                "text": (
                    r"{ \overline { { \sigma } } } = K "
                    r"{ ( \varepsilon _ { 0 } + \overline { \varepsilon } ) } ^ { n }"
                ),
            },
            {"type": "equation", "page_idx": 1, "bbox": [100, 150, 280, 180], "text": r"\sigma=A+B\epsilon^n\tag{2}"},
            {"type": "equation", "page_idx": 1, "bbox": [100, 210, 280, 240], "text": r"\dot{\epsilon}=v/l\tag{3}"},
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == ["(1)", "(2)", "(3)"]
    assert all(candidate.source == "mineru_content_list" for candidate in candidates)
    assert count_formula_provider_calls(candidates) == 0


def test_mineru_cache_provider_infers_exact_numbering_gaps_between_neighbors(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {"type": "equation", "page_idx": 0, "bbox": [100, 90, 280, 120], "text": r"a=b\tag{1}"},
            {"type": "equation", "page_idx": 0, "bbox": [100, 150, 280, 180], "text": r"c=d"},
            {"type": "equation", "page_idx": 0, "bbox": [100, 210, 280, 240], "text": r"e=f\tag{3}"},
            {"type": "equation", "page_idx": 1, "bbox": [100, 90, 280, 120], "text": r"g=h\tag{4.18}"},
            {"type": "equation", "page_idx": 1, "bbox": [100, 150, 280, 180], "text": r"i=j"},
            {"type": "equation", "page_idx": 1, "bbox": [100, 210, 280, 240], "text": r"k=l"},
            {"type": "equation", "page_idx": 1, "bbox": [100, 270, 280, 300], "text": r"m=n"},
            {"type": "equation", "page_idx": 1, "bbox": [100, 330, 280, 360], "text": r"o=p\tag{4.22}"},
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == [
        "(1)",
        "(2)",
        "(3)",
        "(4.18)",
        "(4.19)",
        "(4.20)",
        "(4.21)",
        "(4.22)",
    ]


def test_inferred_gap_number_survives_pdf_status_assignment_when_pdf_scan_misses_anchor(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    candidates = [
        FormulaCandidate(
            page_num=16,
            bbox=(100, 100, 300, 130),
            raw_text="",
            confidence=0.95,
            latex=r"a=b",
            equation_number="(25)",
            source="mineru_content_list",
        ),
        FormulaCandidate(
            page_num=16,
            bbox=(100, 150, 300, 180),
            raw_text="",
            confidence=0.95,
            latex=r"c=d",
            source="mineru_content_list_row",
        ),
        FormulaCandidate(
            page_num=16,
            bbox=(100, 200, 300, 230),
            raw_text="",
            confidence=0.95,
            latex=r"e=f",
            equation_number="(27)",
            source="mineru_content_list_row",
        ),
    ]
    inferred = _infer_missing_equation_numbers_between_numbered(candidates)

    assert inferred[1].equation_number == "(26)"
    assert inferred[1].equation_number_status == "inferred"

    assigned = _assign_equation_number_statuses_from_pdf(
        pdf_path,
        inferred,
        records_by_page={
            16: [
                _PdfEquationNumberRecord(
                    number="(25)",
                    y_center=115.0,
                    x_right=500.0,
                    standalone=False,
                    bbox=(460, 100, 500, 130),
                    text="(25)",
                    page_width=600.0,
                    page_height=800.0,
                ),
                _PdfEquationNumberRecord(
                    number="(27)",
                    y_center=215.0,
                    x_right=500.0,
                    standalone=False,
                    bbox=(460, 200, 500, 230),
                    text="(27)",
                    page_width=600.0,
                    page_height=800.0,
                ),
            ]
        },
        scan_ok=True,
    )

    assert assigned[1].equation_number == "(26)"
    assert assigned[1].equation_number_status == "inferred"


def test_mineru_cache_provider_uses_column_order_for_gap_inference(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {"type": "equation", "page_idx": 0, "bbox": [100, 90, 280, 120], "text": r"a=b\tag{4}"},
            {"type": "equation", "page_idx": 1, "bbox": [100, 90, 280, 120], "text": r"c=d"},
            {"type": "equation", "page_idx": 2, "bbox": [510, 70, 760, 100], "text": r"g=h\tag{7}"},
            {"type": "equation", "page_idx": 2, "bbox": [60, 260, 220, 290], "text": r"e=f\tag{6}"},
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == ["(4)", "(5)", "(6)", "(7)"]


def test_mineru_cache_provider_merges_numbered_formula_definition_continuation(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "equation",
                "page_idx": 4,
                "bbox": [553, 386, 823, 417],
                "text": (
                    r"f ^ { * } ( f ) = \left\{ \begin{array} { c l }"
                    r"{ f ; } & { f \leq f _ { C } } \\"
                    r"{ f _ { c } + \kappa ( f - f _ { C } ) ; } & { f > f _ { C } }"
                    r"\end{array} \right.\tag{6),}"
                ),
                "confidence": 0.95,
            },
            {
                "type": "equation",
                "page_idx": 4,
                "bbox": [506, 439, 903, 532],
                "text": (
                    r"\begin{array} { l }"
                    r"{ \kappa = \frac { f _ { u } ^ { * } - f _ { c } } { f _ { f } - f _ { c } } } \\"
                    r"{ f _ { C } : \mathrm { c r i t i c a l ~ v o i d ~ v o l u m e ~ f r a c t i o n } }"
                    r"\end{array}"
                ),
                "confidence": 0.95,
            },
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].equation_number == "(6)"
    assert r"\kappa =" in candidates[0].latex


def test_pdf_equation_record_candidate_bbox_expands_inline_records():
    record = _PdfEquationNumberRecord(
        number="(13)",
        y_center=88.5,
        x_right=289.61,
        standalone=False,
        bbox=(55.32, 81.34, 289.61, 95.71),
        text="D1 + D2 exp(D3 eta) (13)",
        page_width=595.0,
        page_height=842.0,
    )

    bbox = _pdf_equation_record_candidate_bbox(record)

    assert bbox[0] < record.bbox[0]
    assert bbox[1] < record.bbox[1]
    assert bbox[2] > record.bbox[2]
    assert bbox[3] > record.bbox[3]
    assert (record.bbox[1] - bbox[1]) < (bbox[3] - record.bbox[3])
    assert bbox[0] >= 0.0
    assert bbox[1] >= 0.0
    assert bbox[2] <= record.page_width
    assert bbox[3] <= record.page_height


def test_pdf_equation_record_candidate_bbox_expands_right_half_formula_leftward():
    record = _PdfEquationNumberRecord(
        number="(25)",
        y_center=552.0,
        x_right=518.73,
        standalone=False,
        bbox=(332.58, 536.59, 518.73, 563.59),
        text=r"(A + Br_\alpha^n)(1 + \dot r_\delta^*)^C(1 - T_\gamma^{*m})^2, (25)",
        page_width=612.0,
        page_height=792.0,
    )

    bbox = _pdf_equation_record_candidate_bbox(record)

    assert bbox[0] == pytest.approx(18.36)
    assert bbox[2] > record.bbox[2]
    assert bbox[1] < record.bbox[1]
    assert bbox[3] > record.bbox[3]


def test_pdf_equation_record_candidate_bbox_does_not_cross_columns_when_relation_present():
    record = _PdfEquationNumberRecord(
        number="(4)",
        y_center=594.0,
        x_right=500.0,
        standalone=False,
        bbox=(284.6, 581.4, 500.0, 607.1),
        text=r"\eta = I_1 / (3\sqrt{3J_2}), (4)",
        page_width=547.0,
        page_height=792.0,
    )

    bbox = _pdf_equation_record_candidate_bbox(record)

    assert bbox[0] > 260.0
    assert bbox[0] < record.bbox[0]


def test_pdf_equation_record_candidate_bbox_expands_short_right_column_tail_to_column_start():
    record = _PdfEquationNumberRecord(
        number="(5)",
        y_center=624.0,
        x_right=500.0,
        standalone=False,
        bbox=(413.5, 619.1, 500.0, 629.5),
        text=", (5)",
        page_width=547.0,
        page_height=792.0,
    )

    bbox = _pdf_equation_record_candidate_bbox(record)

    assert bbox[0] == pytest.approx(251.62)
    assert bbox[2] > record.bbox[2]


def test_mineru_cache_provider_ignores_inline_math_in_text_records(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "text",
                "page_idx": 0,
                "text": "where $\\sigma$ is the flow stress and $\\epsilon$ is strain.",
            },
            {
                "type": "equation",
                "page_idx": 1,
                "text_format": "latex",
                "text": r"$$\sigma = E\epsilon$$",
            },
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert [candidate.latex for candidate in candidates] == [r"\sigma = E\epsilon"]
    assert count_formula_provider_calls(candidates) == 0


def test_mineru_cache_provider_reads_leading_numbered_formula_from_text_record(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "text",
                "page_idx": 3,
                "bbox": [514, 856, 909, 920],
                "text": (
                    r"$\sigma _ { \mathrm { e q } } = \alpha ( A + B \varepsilon _ { \mathrm { e q } } ^ { n } ) "
                    r"+ ( 1 - \alpha ) [ A + Q ( 1 - \exp ( - \beta \varepsilon _ { \mathrm { e q } } ) ) ]$ "
                    "(3)式中，Q 和 beta 为参数。"
                ),
            }
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].equation_number == "(3)"
    assert candidates[0].latex.startswith(r"\sigma _ { \mathrm { e q } } = \alpha")
    assert "式中" not in candidates[0].latex


def test_mineru_cache_provider_pairs_text_number_cue_with_same_column_formula(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    bad_latex = (
        r"\varepsilon _ { \mathrm { f } } = [ D _ { 1 } + D _ { 2 } "
        r"\tt e x p { D } \eta _ { \gamma } \rvert ) \# ( D _ { 4 } "
        r"\amalg _ { \varepsilon } ^ { * }"
    )
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "text",
                "page_idx": 8,
                "bbox": [124, 74, 341, 90],
                "text": "MJC 断裂准则如式(13)所示",
            },
            {
                "type": "equation",
                "page_idx": 8,
                "bbox": [650, 72, 769, 90],
                "text": r"$$\nu _ { \mathrm { r } } = a ( \nu_i ^ p - \nu_{bl} ^ p ) ^ { 1 / p }$$",
            },
            {
                "type": "equation",
                "page_idx": 8,
                "bbox": [109, 95, 442, 115],
                "text": f"$$\n{bad_latex}\n$$",
            },
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "mineru-cache")),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 2
    assert candidates[0].equation_number == "(13)"
    assert candidates[0].source == "mineru_content_list_low_quality"
    assert candidates[0].latex == bad_latex
    assert candidates[1].equation_number == ""
    assert candidates[1].latex.startswith(r"\nu")


def test_mineru_cache_provider_does_not_treat_citation_year_as_text_number_cue(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "text",
                "page_idx": 4,
                "bbox": [35, 558, 444, 576],
                "text": "The model proposed by Johnson and Cook (1983) is used here.",
            },
            {
                "type": "equation",
                "page_idx": 4,
                "bbox": [162, 590, 318, 612],
                "text": r"$$\sigma _ { \mathrm { e q } } = [ A + B p ^ n ] [ 1 - T ^ { * m } ]$$",
            },
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "mineru-cache")),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].equation_number == ""


def test_mineru_cache_provider_follows_manifest_references(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "manifest.json").write_text(
        json.dumps({
            "files": {
                "markdown": "full.md",
                "content_list": "content_list.json",
            }
        }),
        encoding="utf-8",
    )
    (cache_dir / "full.md").write_text("<!-- page 2 -->\n$$E = mc^2$$", encoding="utf-8")
    (cache_dir / "content_list.json").write_text(
        json.dumps([{"type": "interline_equation", "text": r"\sigma = E\epsilon", "page_idx": 2}]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert [candidate.latex for candidate in candidates] == [r"E = mc^2", r"\sigma = E\epsilon"]
    assert {candidate.source for candidate in candidates} == {"mineru_markdown", "mineru_content_list"}
    assert count_formula_provider_calls(candidates) == 0


def test_mineru_cache_provider_reads_llm_for_zotero_zip_cache(tmp_path):
    cache_zip = tmp_path / "LLM-for-Zotero-MinerU-cache-ESP3I5WX.zip"
    with zipfile.ZipFile(cache_zip, "w") as archive:
        archive.writestr("full.md", "<!-- page 1 -->\n$$E = mc^2$$")
        archive.writestr(
            "content_list.json",
            json.dumps([
                {
                    "type": "interline_equation",
                    "text": r"\sigma = E\epsilon",
                    "page_idx": 1,
                }
            ]),
        )
        archive.writestr(
            "paper_content_list_v2.json",
            json.dumps([{"type": "equation", "text": r"inline = noise", "page_idx": 0}]),
        )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(cache_zip)),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ESP3I5WX")

    assert [candidate.latex for candidate in candidates] == [r"\sigma = E\epsilon"]
    assert count_formula_provider_calls(candidates) == 0


def test_mineru_cache_provider_ignores_direct_zip_for_other_item_key(tmp_path):
    first_zip = tmp_path / "LLM-for-Zotero-MinerU-cache-ESP3I5WX.zip"
    second_zip = tmp_path / "LLM-for-Zotero-MinerU-cache-B8YIQE49.zip"
    for cache_zip, latex in (
        (first_zip, r"E = mc^2"),
        (second_zip, r"\sigma = E\epsilon"),
    ):
        with zipfile.ZipFile(cache_zip, "w") as archive:
            archive.writestr(
                "content_list.json",
                json.dumps([{"type": "interline_equation", "text": latex, "page_idx": 0}]),
            )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=f"{first_zip};{second_zip}"),
    )

    candidates = provider.extract_candidates(tmp_path / "B8YIQE49" / "paper.pdf", item_key="B8YIQE49")

    assert [candidate.latex for candidate in candidates] == [r"\sigma = E\epsilon"]


def test_mineru_cache_provider_uses_zip_full_md_when_no_content_list(tmp_path):
    cache_zip = tmp_path / "LLM-for-Zotero-MinerU-cache-ESP3I5WX.zip"
    with zipfile.ZipFile(cache_zip, "w") as archive:
        archive.writestr("full.md", "<!-- page 1 -->\n$$E = mc^2$$")
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(cache_zip)),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ESP3I5WX")

    assert [candidate.latex for candidate in candidates] == [r"E = mc^2"]
    assert candidates[0].source == "mineru_markdown"


def test_mineru_cache_provider_reads_middle_json_nested_formula(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "middle.json").write_text(
        json.dumps({
            "pdf_info": [
                {
                    "page_idx": 0,
                    "para_blocks": [
                        {
                            "layout_type": "interline_equation",
                            "content": {"math_content": r"f(x)=x^2"},
                        }
                    ],
                }
            ]
        }),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].page_num == 1
    assert candidates[0].source == "mineru_middle_json"
    assert candidates[0].latex == r"f(x)=x^2"


def test_pdf_extract_kit_provider_reads_formula_recognition_json(tmp_path):
    cache_dir = tmp_path / "pdf-extract-kit" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "formula_recognition.json").write_text(
        json.dumps([
            {
                "label": "formula",
                "page_index": 0,
                "points": [[10, 20], [200, 20], [200, 60], [10, 60]],
                "latex_styled": r"$$E = mc^2$$",
                "confidence_score": 0.88,
            },
            {"label": "text", "text": "Abstract"},
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "pdf_extract_kit_json",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "pdf-extract-kit")),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].page_num == 1
    assert candidates[0].bbox == (10.0, 20.0, 200.0, 60.0)
    assert candidates[0].latex == r"E = mc^2"
    assert candidates[0].source == "pdf_extract_kit_json"
    assert count_formula_provider_calls(candidates) == 0


def test_pdf_extract_kit_provider_reads_common_formula_field_variants(tmp_path):
    cache_dir = tmp_path / "pdf-extract-kit" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "results.json").write_text(
        json.dumps({
            "results": [
                {
                    "det_label": "formula",
                    "page_id": 0,
                    "dt_boxes": [[[10, 20], [200, 20], [200, 60], [10, 60]]],
                    "rec_formula": r"\epsilon_p = \epsilon - \sigma / E",
                    "confidence_score": 0.93,
                }
            ]
        }),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "pdf_extract_kit_json",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "pdf-extract-kit")),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].page_num == 1
    assert candidates[0].bbox == (10.0, 20.0, 200.0, 60.0)
    assert candidates[0].latex == r"\epsilon_p = \epsilon - \sigma / E"
    assert count_formula_provider_calls(candidates) == 0


def test_pdf_extract_kit_provider_accepts_pdf_space_detection_bbox(tmp_path):
    cache_dir = tmp_path / "pdf-extract-kit" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "formula_detection.json").write_text(
        json.dumps({
            "pages": [
                {
                    "page_idx": 1,
                    "detections": [
                        {
                            "cls_name": "formula",
                            "bbox": {
                                "left": 15,
                                "top": 25,
                                "right": 215,
                                "bottom": 75,
                                "coordinate_space": "pdf",
                            },
                            "score": 0.7,
                        },
                        {"cls_name": "text", "bbox": [1, 2, 3, 4], "score": 0.9},
                    ],
                }
            ]
        }),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "pdf_extract_kit_json",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "pdf-extract-kit")),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].page_num == 2
    assert candidates[0].bbox == (15.0, 25.0, 215.0, 75.0)
    assert candidates[0].bbox_coordinate_space == "pdf"
    assert count_formula_provider_calls(candidates) == 1


def test_mineru_cache_provider_scans_each_cache_root_independently(tmp_path):
    first_root = tmp_path / "first-cache"
    second_root = tmp_path / "second-cache"
    first_item = first_root / "OTHER"
    nested_item = second_root / "nested" / "ITEM123"
    first_item.mkdir(parents=True)
    nested_item.mkdir(parents=True)
    (first_item / "content_list.json").write_text(
        json.dumps([{"type": "interline_equation", "text": r"a=b", "page_idx": 0}]),
        encoding="utf-8",
    )
    (nested_item / "paper_content_list.json").write_text(
        json.dumps([{"type": "interline_equation", "text": r"E = mc^2", "page_idx": 1}]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=f"{first_root};{second_root}"),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert [candidate.latex for candidate in candidates] == [r"E = mc^2"]
    assert candidates[0].page_num == 2


def test_mineru_cache_provider_scans_nested_cache_when_root_file_is_not_item_specific(tmp_path):
    cache_root = tmp_path / "mineru-cache"
    nested_item = cache_root / "nested" / "ITEM123"
    nested_item.mkdir(parents=True)
    (cache_root / "content_list.json").write_text(
        json.dumps([{"type": "interline_equation", "text": r"a=b", "page_idx": 0}]),
        encoding="utf-8",
    )
    (nested_item / "paper_content_list.json").write_text(
        json.dumps([{"type": "interline_equation", "text": r"E = mc^2", "page_idx": 1}]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(cache_root)),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert [candidate.latex for candidate in candidates] == [r"E = mc^2"]
    assert candidates[0].page_num == 2


def test_mineru_cache_provider_scans_zotero_storage_one_level_for_sibling_cache(tmp_path):
    storage_root = tmp_path / "storage"
    pdf_dir = storage_root / "PDFKEY12"
    cache_dir = storage_root / "CACHE123"
    deep_cache_dir = storage_root / "NESTED12" / "deep"
    pdf_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    deep_cache_dir.mkdir(parents=True)
    pdf_path = pdf_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    (cache_dir / "mineru-cache-PDFKEY12_content_list.json").write_text(
        json.dumps([{"type": "interline_equation", "text": r"E = mc^2", "page_idx": 1}]),
        encoding="utf-8",
    )
    (deep_cache_dir / "ITEM123_content_list.json").write_text(
        json.dumps([{"type": "interline_equation", "text": r"a=b", "page_idx": 0}]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(storage_root)),
    )

    candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert [candidate.latex for candidate in candidates] == [r"E = mc^2"]
    assert candidates[0].page_num == 2


def test_mineru_cache_provider_prefers_pdf_storage_dir_without_scanning_root(tmp_path, monkeypatch):
    storage_root = tmp_path / "storage"
    pdf_dir = storage_root / "PDFKEY12"
    pdf_dir.mkdir(parents=True)
    pdf_path = pdf_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    cache_path = pdf_dir / "content_list.json"
    cache_path.write_text(
        json.dumps([{"type": "interline_equation", "text": r"E = mc^2", "page_idx": 1}]),
        encoding="utf-8",
    )
    original_iterdir = Path.iterdir

    def guarded_iterdir(path):
        if path == storage_root:
            raise AssertionError("storage root scan should be skipped when the PDF dir has formula cache")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(storage_root)),
    )

    paths = provider._candidate_cache_paths(pdf_path, item_key="ITEM123")

    assert paths == [cache_path]


def test_mineru_cache_provider_sibling_storage_scan_skips_root_child_is_dir_stats(tmp_path, monkeypatch):
    storage_root = tmp_path / "storage"
    pdf_dir = storage_root / "PDFKEY12"
    cache_dir = storage_root / "CACHE123"
    pdf_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    pdf_path = pdf_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    cache_path = cache_dir / "LLM-for-Zotero-MinerU-cache-PDFKEY12.zip"
    cache_path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    original_is_dir = Path.is_dir

    def guarded_is_dir(path):
        if path.parent == storage_root:
            raise AssertionError("storage child is_dir stat should be skipped during provider cache lookup")
        return original_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", guarded_is_dir)
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(storage_root)),
    )

    paths = provider._candidate_cache_paths(pdf_path, item_key="ITEM123")

    assert paths == [cache_path]


def test_mineru_cache_provider_uses_explicit_cache_paths_without_storage_scan(tmp_path, monkeypatch):
    storage_root = tmp_path / "storage"
    pdf_dir = storage_root / "PDFKEY12"
    cache_dir = storage_root / "CACHE123"
    pdf_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    pdf_path = pdf_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    cache_path = cache_dir / "LLM-for-Zotero-MinerU-cache-PDFKEY12.zip"
    with zipfile.ZipFile(cache_path, "w") as archive:
        archive.writestr(
            "content_list.json",
            json.dumps([{"type": "interline_equation", "text": r"E = mc^2", "page_idx": 0}]),
        )
    original_iterdir = Path.iterdir

    def guarded_iterdir(path):
        if path == storage_root:
            raise AssertionError("explicit cache paths should avoid scanning storage root")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(storage_root)),
    )

    candidates = provider.extract_candidates(
        pdf_path,
        item_key="ITEM123",
        cache_paths=(cache_path,),
    )

    assert [candidate.latex for candidate in candidates] == [r"E = mc^2"]


def test_mineru_cache_provider_reuses_pdf_number_scan_for_cached_formulas(tmp_path, monkeypatch):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {"type": "interline_equation", "text": r"E = mc^2", "page_idx": 0},
            {"type": "interline_equation", "text": r"\sigma = E\epsilon", "page_idx": 1},
        ]),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class FakeDoc:
        def close(self):
            pass

    calls = 0

    def fake_scan(_doc, *, max_records=None, max_pages=None):
        nonlocal calls
        calls += 1
        return SimpleNamespace(records_by_page={}, truncated=False)

    monkeypatch.setattr("zotpilot.feature_extraction.formula_ocr.pymupdf.open", lambda _path: FakeDoc())
    monkeypatch.setattr(
        "zotpilot.feature_extraction.formula_ocr._scan_pdf_equation_number_records_by_page",
        fake_scan,
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert [candidate.latex for candidate in candidates] == [r"E = mc^2", r"\sigma = E\epsilon"]
    assert calls == 1


def test_zotero_storage_cache_scan_avoids_child_is_dir_stat_calls(tmp_path, monkeypatch):
    storage_root = tmp_path / "storage"
    cache_dir = storage_root / "CACHE123"
    cache_dir.mkdir(parents=True)
    (storage_root / "regular-file.txt").write_text("not a directory", encoding="utf-8")
    cache_path = cache_dir / "LLM-for-Zotero-MinerU-cache-PDFKEY12.zip"
    cache_path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    original_is_dir = Path.is_dir

    def guarded_is_dir(path):
        if path.parent == storage_root:
            raise AssertionError("storage child is_dir stat should be skipped during cache scan")
        return original_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", guarded_is_dir)

    paths = _zotero_storage_cache_scan(storage_root, {"pdfkey12"})

    assert paths == [cache_path]


def test_mineru_cache_provider_requires_pdf_space_for_bbox_only_candidates(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [10, 20, 200, 60],
                "coordinate_space": "image_pixels",
                "confidence": 0.9,
            },
            {
                "type": "interline_equation",
                "page_idx": 1,
                "bbox": [15, 25, 215, 75],
                "coordinate_space": "pdf",
                "confidence": 0.9,
            },
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].page_num == 2
    assert candidates[0].bbox_coordinate_space == "pdf"
    assert count_formula_provider_calls(candidates) == 1


def test_mineru_cache_provider_keeps_structured_short_tensor_formulas(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    formulas = [
        (
            r"\pmb { d } = \pmb { d } ^ { \mathrm { e } } "
            r"+ \pmb { d } ^ { \mathrm { p } } + \pmb { d } ^ { \mathrm { t } }"
        ),
        r"\dot { T } = \frac { \chi } { \rho c _ { V } } \pmb { \sigma } : \dot { \pmb { \varepsilon } } _ { p l }",
        r"V _ { b l } = 6 2 2 \cdot ( H / D ) ^ { 0 . 6 7 3 } - 3 3",
    ]
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [10, 20 + index * 20, 300, 35 + index * 20],
                "text": formula,
                "confidence": 0.9,
            }
            for index, formula in enumerate(formulas)
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert [candidate.latex for candidate in candidates] == formulas
    assert count_formula_provider_calls(candidates) == 0


def test_mineru_cache_provider_does_not_open_pdf_for_complete_cached_latex(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [10, 20, 300, 35],
                "text": r"E = mc^2",
                "equation_number": "(1)",
                "confidence": 0.9,
            },
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [10, 50, 300, 65],
                "text": r"\sigma = E\epsilon",
                "equation_number": "(2)",
                "confidence": 0.9,
            },
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open") as pdf_open:
        candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == ["(1)", "(2)"]
    assert [candidate.equation_number_status for candidate in candidates] == ["provided", "provided"]
    assert count_formula_provider_calls(candidates) == 0
    pdf_open.assert_not_called()


def test_mineru_cache_provider_marks_cached_formulas_unnumbered_when_pdf_has_no_number_anchors(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [10, 20, 300, 35],
                "text": r"\eta = -p / q",
                "confidence": 0.9,
            },
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [10, 50, 300, 65],
                "text": r"q = \sqrt{3J_2}",
                "confidence": 0.9,
            },
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    class FakePage:
        rect = SimpleNamespace(width=612.0, height=792.0)

        def get_text(self, mode="text"):
            return [] if mode == "blocks" else ""

    class FakeDoc:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return FakePage()

        def close(self):
            pass

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=FakeDoc()):
        candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == ["", ""]
    assert [candidate.equation_number_status for candidate in candidates] == ["unnumbered", "unnumbered"]
    assert count_formula_provider_calls(candidates) == 0


def test_mineru_cache_provider_marks_page_without_number_anchors_unnumbered(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [10, 20, 300, 35],
                "text": r"E = mc^2",
                "equation_number": "(1)",
                "confidence": 0.9,
            },
            {
                "type": "interline_equation",
                "page_idx": 1,
                "bbox": [10, 50, 300, 65],
                "text": r"\eta = -p / q",
                "confidence": 0.9,
            },
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    class FakePage:
        def __init__(self, blocks):
            self.rect = SimpleNamespace(width=612.0, height=792.0)
            self._blocks = blocks

        def get_text(self, mode="text"):
            return self._blocks if mode == "blocks" else ""

    class FakeDoc:
        pages = [
            FakePage([(10, 20, 300, 35, "E = mc^2 (1)", 0, 0)]),
            FakePage([]),
        ]

        def __len__(self):
            return len(self.pages)

        def __getitem__(self, index):
            return self.pages[index]

        def close(self):
            pass

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=FakeDoc()):
        candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == ["(1)", ""]
    assert [candidate.equation_number_status for candidate in candidates] == ["provided", "unnumbered"]


def test_assign_equation_status_marks_far_same_page_formula_unnumbered(tmp_path):
    import pymupdf

    pdf_path = tmp_path / "paper.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((300, 520), r"\sigma = E \epsilon (2.88)")
    doc.save(pdf_path)
    doc.close()
    far_formula = FormulaCandidate(
        page_num=1,
        bbox=(220, 120, 760, 150),
        raw_text="",
        confidence=0.95,
        latex=r"2G = \frac{E}{1+\nu}",
        source="mineru_content_list",
    )
    near_formula = FormulaCandidate(
        page_num=1,
        bbox=(220, 500, 760, 530),
        raw_text="",
        confidence=0.95,
        latex=r"\sigma = E \epsilon",
        source="mineru_content_list",
    )

    assigned = _assign_equation_number_statuses_from_pdf(pdf_path, [far_formula, near_formula])

    assert [candidate.equation_number_status for candidate in assigned] == ["unnumbered", "missing"]


def test_assign_equation_status_does_not_use_unknown_cache_bbox_as_pdf_position(tmp_path):
    import pymupdf

    pdf_path = tmp_path / "paper.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((420, 500), r"\phi = 0 (1)")
    doc.save(pdf_path)
    doc.close()
    cache_formula = FormulaCandidate(
        page_num=1,
        bbox=(300, 585, 430, 625),
        bbox_coordinate_space="unknown",
        raw_text="",
        confidence=0.95,
        latex=r"f^{*}=\begin{cases} f, & f \le f_c \\ f_c + (f_F-f_c), & f > f_c \end{cases}",
        source="mineru_content_list",
    )

    assigned = _assign_equation_number_statuses_from_pdf(pdf_path, [cache_formula])

    assert assigned[0].equation_number_status == "unnumbered"


def test_mineru_cache_provider_does_not_cap_cached_latex_candidates(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [10, 20 + index * 20, 300, 35 + index * 20],
                "text": rf"x _ {{ {index} }} = y _ {{ {index} }} + 1",
                "confidence": 0.9,
            }
            for index in range(8)
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "mineru-cache")),
    )

    candidates = provider.extract_candidates(
        tmp_path / "paper.pdf",
        item_key="ITEM123",
        max_formulas_per_page=2,
        max_formulas_per_doc=3,
    )

    assert len(candidates) == 8
    assert count_formula_provider_calls(candidates) == 0


def test_mineru_cache_provider_caps_cached_latex_for_batch_detection(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [10, 20 + index * 20, 300, 35 + index * 20],
                "text": rf"x _ {{ {index} }} = y _ {{ {index} }} + 1",
                "confidence": 0.9,
            }
            for index in range(8)
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "mineru-cache")),
    )

    candidates = provider.extract_candidates(
        tmp_path / "paper.pdf",
        item_key="ITEM123",
        max_candidates_per_doc=3,
    )

    assert len(candidates) == 3
    assert count_formula_provider_calls(candidates) == 0


def test_mineru_cache_provider_still_caps_bbox_only_ocr_candidates(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    records = []
    for page_idx in range(3):
        for index in range(3):
            records.append({
                "type": "interline_equation",
                "page_idx": page_idx,
                "bbox": [10, 20 + index * 20, 300, 35 + index * 20],
                "coordinate_space": "pdf",
                "confidence": 0.9,
            })
    (cache_dir / "content_list.json").write_text(json.dumps(records), encoding="utf-8")
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "mineru-cache")),
    )

    candidates = provider.extract_candidates(
        tmp_path / "paper.pdf",
        item_key="ITEM123",
        max_formulas_per_page=2,
        max_formulas_per_doc=4,
    )

    assert len(candidates) == 4
    assert [candidate.page_num for candidate in candidates] == [1, 1, 2, 2]
    assert count_formula_provider_calls(candidates) == 4


def test_mineru_cache_provider_falls_back_to_pdf_numbered_formulas_without_cache(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def get_text(self, mode="text"):
            if mode == "blocks":
                return [
                    (80.0, 120.0, 520.0, 142.0, r"\sigma = E \epsilon (1)"),
                    (80.0, 180.0, 520.0, 202.0, "This is prose (2)"),
                ]
            return ""

    class FakeDoc:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return FakePage()

        def close(self):
            pass

    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "missing-cache")),
    )

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=FakeDoc()):
        candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].source == "pdf_text_equation_number"
    assert candidates[0].equation_number == "(1)"
    assert candidates[0].latex == ""
    assert count_formula_provider_calls(candidates) == 1


def test_mineru_cache_provider_ignores_reference_issue_numbers_without_cache(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def get_text(self, mode="text"):
            if mode == "blocks":
                return [
                    (80.0, 120.0, 520.0, 142.0, r"\sigma = E \epsilon (1)"),
                    (80.0, 180.0, 520.0, 202.0, "failure. Eur. J. Mech. A/Solids 27 (1)"),
                    (80.0, 240.0, 520.0, 262.0, "2024-T3(51)"),
                    (
                        80.0,
                        300.0,
                        520.0,
                        322.0,
                        "[40] 刘正,杨蒙蒙. 帽状试样局域变形行为及绝热剪切的数值模拟[J]. "
                        "热加工工艺.2016,45(01)",
                    ),
                ]
            return ""

    class FakeDoc:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return FakePage()

        def close(self):
            pass

    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "missing-cache")),
    )

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=FakeDoc()):
        candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == ["(1)"]
    assert candidates[0].source == "pdf_text_equation_number"
    assert count_formula_provider_calls(candidates) == 1


def test_pdf_equation_record_bbox_expands_midline_fragment_leftward():
    from zotpilot.feature_extraction.formula_ocr import (
        _pdf_equation_record_candidate_bbox,
        _PdfEquationNumberRecord,
    )

    record = _PdfEquationNumberRecord(
        number="(11)",
        y_center=346.5,
        x_right=528.0,
        standalone=False,
        bbox=(272.0, 323.0, 528.0, 369.0),
        text=r"\rho C_p , (11)",
        page_width=600.0,
        page_height=800.0,
    )

    bbox = _pdf_equation_record_candidate_bbox(record)

    assert bbox == (18.0, 321.0, 538.24, 373.0)


def test_pdf_equation_record_bbox_tightens_private_glyph_relation_line():
    record = _PdfEquationNumberRecord(
        number="(3)",
        y_center=436.37,
        x_right=518.85,
        standalone=False,
        bbox=(167.94, 421.59, 518.85, 451.15),
        text="˚\x01σ = C : de or ˚σ = (1 −D)C : de − ˙D (1 −D)σ, (3)",
        page_width=589.0,
        page_height=792.0,
    )

    assert _has_formula_relation(record.text)

    bbox = _pdf_equation_record_candidate_bbox(record)

    assert bbox[3] == pytest.approx(455.15)
    assert bbox[3] - record.bbox[3] <= 4.0


def test_pdf_equation_record_bbox_keeps_private_use_relation_in_column():
    record = _PdfEquationNumberRecord(
        number="(3)",
        y_center=727.25,
        x_right=542.4,
        standalone=False,
        bbox=(308.6, 720.0, 542.4, 734.5),
        text="eq eq eq n A B A Q \uf073 \uf061 \uf065 \uf061 \uf062\uf065 \uf03d \uf02b \uf02b \uf02d (3)",
        page_width=595.0,
        page_height=842.0,
    )

    assert _has_formula_relation(record.text)

    bbox = _pdf_equation_record_candidate_bbox(record)

    assert bbox[0] > 280.0
    assert bbox[0] < record.bbox[0]
    assert bbox[2] > record.bbox[2]


def test_mineru_cache_provider_merges_split_unit_tail_equation_number_without_cache(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def get_text(self, mode="text"):
            if mode == "blocks":
                return [
                    (33.7, 741.6, 75.0, 760.5, "tf ¼ Hv 0:07"),
                    (110.6, 735.7, 284.8, 755.0, "\x01 \x03 MPa ð17Þ"),
                ]
            return ""

    class FakeDoc:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return FakePage()

        def close(self):
            pass

    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "missing-cache")),
    )

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=FakeDoc()):
        candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].equation_number == "(17)"
    assert "tf ¼ Hv" in candidates[0].raw_text
    assert candidates[0].bbox[0] <= 33.7


def test_mineru_cache_provider_keeps_wide_standalone_number_as_full_row_crop(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def get_text(self, mode="text"):
            if mode == "blocks":
                return [
                    (240.1, 115.5, 274.0, 138.6, "("),
                    (267.9, 124.6, 280.7, 146.7, ")"),
                    (286.1, 124.3, 506.3, 138.6, "(5)"),
                ]
            return ""

    class FakeDoc:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return FakePage()

        def close(self):
            pass

    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "missing-cache")),
    )

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=FakeDoc()):
        candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == ["(5)"]
    assert candidates[0].bbox[0] <= 35.0
    assert candidates[0].bbox[2] >= 506.3


def test_mineru_cache_provider_merges_leading_number_tail_with_previous_formula_block(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def get_text(self, mode="text"):
            if mode == "blocks":
                return [
                    (56.4, 485.8, 173.3, 512.5, "sep • ðep • ; TÞ ¼ 1 þ CðTÞln ep •"),
                    (42.5, 521.7, 291.9, 541.6, "ð48Þ with"),
                ]
            return ""

    class FakeDoc:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return FakePage()

        def close(self):
            pass

    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "missing-cache")),
    )

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=FakeDoc()):
        candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == ["(48)"]
    assert "sep" in candidates[0].raw_text
    assert candidates[0].bbox[1] <= 485.8


def test_mineru_cache_provider_merges_line_level_standalone_number_from_long_paragraph(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def get_text(self, mode="text"):
            if mode == "blocks":
                return [
                    (
                        32.7,
                        58.7,
                        283.8,
                        197.1,
                        "long prose " * 80 + "term as s ¼ sa þs\x05 ð11Þ",
                    )
                ]
            if mode == "dict":
                return {
                    "blocks": [
                        {
                            "lines": [
                                {
                                    "bbox": (32.7, 172.7, 216.9, 182.7),
                                    "spans": [{"text": "represented with an athermal stress, sa, term as"}],
                                },
                                {
                                    "bbox": (32.7, 186.4, 73.4, 197.1),
                                    "spans": [{"text": "s ¼ sa þs\x05"}],
                                },
                                {
                                    "bbox": (268.6, 187.7, 283.8, 195.8),
                                    "spans": [{"text": "ð11Þ"}],
                                },
                            ]
                        }
                    ]
                }
            return ""

    class FakeDoc:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return FakePage()

        def close(self):
            pass

    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "missing-cache")),
    )

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=FakeDoc()):
        candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == ["(11)"]
    assert candidates[0].bbox[0] <= 32.7
    assert candidates[0].bbox[3] - candidates[0].bbox[1] < 60.0


def test_pdf_number_scan_repairs_split_chapter_number_spans():
    class FakePage:
        rect = SimpleNamespace(width=595.0, height=842.0)

        def get_text(self, mode="text"):
            if mode == "blocks":
                return []
            if mode == "dict":
                return {
                    "blocks": [
                        {
                            "lines": [
                                {
                                    "bbox": (327.0, 342.0, 459.0, 359.0),
                                    "spans": [
                                        {"text": "— —\ue5ce（２"},
                                    ],
                                },
                                {"bbox": (460.0, 349.0, 463.0, 351.0), "spans": [{"text": "－"}]},
                                {"bbox": (465.0, 346.0, 479.0, 353.0), "spans": [{"text": "１７）"}]},
                            ]
                        }
                    ]
                }
            return ""

    class FakeDoc:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return FakePage()

    scan = _scan_pdf_equation_number_records_by_page(FakeDoc())

    assert [record.number for record in scan.records_by_page[1]] == ["(2-17)"]


def test_pdf_number_scan_recovers_right_edge_number_missing_closing_parenthesis():
    class FakePage:
        rect = SimpleNamespace(width=595.0, height=842.0)

        def get_text(self, mode="text"):
            if mode == "blocks":
                return [
                    (497.26, 573.69, 522.26, 586.97, "(2.14"),
                    (375.43, 620.01, 522.14, 636.81, r"\sigma = E\epsilon (2.15)"),
                ]
            if mode == "dict":
                return {"blocks": []}
            return ""

    class FakeDoc:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return FakePage()

    scan = _scan_pdf_equation_number_records_by_page(FakeDoc())

    assert {record.number for record in scan.records_by_page[1]} == {"(2.14)", "(2.15)"}


def test_pdf_number_scan_skips_code_listing_props_calls():
    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def get_text(self, mode="text"):
            if mode == "blocks":
                return [
                    (60.0, 120.0, 480.0, 142.0, "144 C ******************************* = props(1)"),
                    (60.0, 144.0, 480.0, 166.0, "146 C ******************************* = props(2)"),
                    (80.0, 220.0, 520.0, 245.0, r"\sigma = E \varepsilon (3)"),
                ]
            if mode == "dict":
                return {"blocks": []}
            return ""

    class FakeDoc:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return FakePage()

    scan = _scan_pdf_equation_number_records_by_page(FakeDoc())

    assert [record.number for record in scan.records_by_page[1]] == ["(3)"]


def test_pdf_number_scan_skips_array_assignment_code_listing():
    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def get_text(self, mode="text"):
            if mode == "blocks":
                return [
                    (60.0, 120.0, 480.0, 142.0, "sigma(3)=E*eps(3)"),
                    (80.0, 220.0, 520.0, 245.0, r"\sigma = E \varepsilon (4)"),
                ]
            if mode == "dict":
                return {"blocks": []}
            return ""

    class FakeDoc:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return FakePage()

    scan = _scan_pdf_equation_number_records_by_page(FakeDoc())

    assert [record.number for record in scan.records_by_page[1]] == ["(4)"]


def test_infer_missing_equation_numbers_does_not_reuse_existing_number_outside_segment():
    candidates = [
        FormulaCandidate(1, (10, 100, 40, 120), r"a=b", 0.9, equation_number="(2.3)", latex=r"a=b"),
        FormulaCandidate(1, (10, 200, 40, 220), r"c=d", 0.9, equation_number="(2.2)", latex=r"c=d"),
        FormulaCandidate(2, (10, 100, 40, 120), r"e=f", 0.9, equation_number="(2.4)", latex=r"e=f"),
        FormulaCandidate(2, (10, 200, 40, 220), r"g=h", 0.9, equation_number="(2.5)", latex=r"g=h"),
        FormulaCandidate(2, (10, 300, 40, 320), r"i=j", 0.9, equation_number="(2.6)", latex=r"i=j"),
        FormulaCandidate(2, (10, 400, 40, 420), r"k=l", 0.9, latex=r"k=l"),
        FormulaCandidate(2, (10, 500, 40, 520), r"m=n", 0.9, equation_number="(2.7)", latex=r"m=n"),
    ]

    inferred = _infer_missing_equation_numbers_between_numbered(candidates)

    assert [candidate.equation_number for candidate in inferred].count("(2.3)") == 1
    assert inferred[2].equation_number == "(2.4)"


def test_infer_missing_equation_numbers_does_not_number_explicit_unnumbered_candidate():
    candidates = [
        FormulaCandidate(1, (10, 100, 40, 120), r"a=b", 0.9, equation_number="(2.85)", latex=r"a=b"),
        FormulaCandidate(
            1,
            (10, 140, 40, 160),
            r"c=d",
            0.9,
            latex=r"c=d",
            equation_number_status="unnumbered",
        ),
        FormulaCandidate(1, (10, 180, 40, 200), r"e=f", 0.9, equation_number="(2.87)", latex=r"e=f"),
    ]

    inferred = _infer_missing_equation_numbers_between_numbered(candidates)

    assert inferred[1].equation_number == ""
    assert inferred[1].equation_number_status == "unnumbered"


def test_mineru_cache_pdf_numbered_fallback_respects_doc_limit(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def __init__(self, page_index):
            self.page_index = page_index

        def get_text(self, mode="text"):
            if mode == "blocks":
                number = self.page_index + 1
                return [(80.0, 120.0, 520.0, 142.0, rf"x _ {{ {number} }} = y _ {{ {number} }} ({number})")]
            return ""

    class FakeDoc:
        visited_pages: list[int] = []

        def __len__(self):
            return 5

        def __getitem__(self, index):
            self.visited_pages.append(index)
            return FakePage(index)

        def close(self):
            pass

    doc = FakeDoc()
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "missing-cache")),
    )

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=doc):
        candidates = provider.extract_candidates(
            pdf_path,
            item_key="ITEM123",
            max_formulas_per_doc=2,
        )

    assert [candidate.equation_number for candidate in candidates] == ["(1)", "(2)"]
    assert {candidate.source for candidate in candidates} == {"pdf_text_equation_number_truncated"}
    assert doc.visited_pages == [0, 1]


def test_mineru_cache_pdf_numbered_fallback_respects_candidate_detection_limit(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def __init__(self, page_index):
            self.page_index = page_index

        def get_text(self, mode="text"):
            if mode == "blocks":
                number = self.page_index + 1
                return [(80.0, 120.0, 520.0, 142.0, rf"x _ {{ {number} }} = y _ {{ {number} }} ({number})")]
            return ""

    class FakeDoc:
        visited_pages: list[int] = []

        def __len__(self):
            return 5

        def __getitem__(self, index):
            self.visited_pages.append(index)
            return FakePage(index)

        def close(self):
            pass

    doc = FakeDoc()
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "missing-cache")),
    )

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=doc):
        candidates = provider.extract_candidates(
            pdf_path,
            item_key="ITEM123",
            max_candidates_per_doc=3,
        )

    assert [candidate.equation_number for candidate in candidates] == ["(1)", "(2)", "(3)"]
    assert {candidate.source for candidate in candidates} == {"pdf_text_equation_number_truncated"}
    assert doc.visited_pages == [0, 1, 2]


def test_mineru_cache_pdf_numbered_fallback_full_scan_opt_in_for_long_docs(tmp_path):
    pdf_path = tmp_path / "long-paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def __init__(self, page_index):
            self.page_index = page_index

        def get_text(self, mode="text"):
            if mode == "blocks" and self.page_index == 84:
                return [(80.0, 120.0, 520.0, 142.0, r"\sigma = E \varepsilon (1.1)")]
            return []

    class FakeDoc:
        def __init__(self):
            self.visited_pages: list[int] = []

        def __len__(self):
            return 100

        def __getitem__(self, index):
            self.visited_pages.append(index)
            return FakePage(index)

        def close(self):
            pass

    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "missing-cache")),
    )
    default_doc = FakeDoc()
    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=default_doc):
        default_candidates = provider.extract_candidates(
            pdf_path,
            item_key="ITEM123",
            max_formulas_per_doc=0,
        )

    assert default_candidates == []
    assert max(default_doc.visited_pages) == 79

    full_doc = FakeDoc()
    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=full_doc):
        full_scan_candidates = provider.extract_candidates(
            pdf_path,
            item_key="ITEM123",
            max_formulas_per_doc=0,
            pdf_fallback_max_pages=0,
        )

    assert [candidate.equation_number for candidate in full_scan_candidates] == ["(1.1)"]
    assert [candidate.source for candidate in full_scan_candidates] == ["pdf_text_equation_number"]
    assert max(full_doc.visited_pages) == 99


def test_assign_equation_number_statuses_clears_cache_number_not_supported_on_pdf_page(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    candidates = [
        FormulaCandidate(
            page_num=1,
            bbox=(10.0, 100.0, 120.0, 130.0),
            raw_text=r"E = mc^2",
            confidence=0.95,
            latex=r"E = mc^2",
            equation_number="(1)",
            source="mineru_content_list",
            bbox_coordinate_space="pdf",
        ),
        FormulaCandidate(
            page_num=1,
            bbox=(10.0, 500.0, 120.0, 530.0),
            raw_text=r"\sigma = E\varepsilon",
            confidence=0.95,
            latex=r"\sigma = E\varepsilon",
            equation_number="(2)",
            source="mineru_content_list",
            bbox_coordinate_space="pdf",
        ),
    ]
    records_by_page = {
        1: [
            _PdfEquationNumberRecord(
                number="(2)",
                y_center=515.0,
                x_right=500.0,
                standalone=False,
                bbox=(300.0, 500.0, 520.0, 530.0),
                text=r"\sigma = E\varepsilon (2)",
                page_width=600.0,
                page_height=800.0,
            )
        ]
    }

    assigned = _assign_equation_number_statuses_from_pdf(
        pdf_path,
        candidates,
        records_by_page=records_by_page,
        scan_ok=True,
    )

    assert assigned[0].equation_number == ""
    assert assigned[0].equation_number_status == "unnumbered"
    assert assigned[1].equation_number == "(2)"
    assert assigned[1].equation_number_status == "provided"


def test_mineru_cache_numbering_audit_scans_to_cached_candidate_pages(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 119,
                "bbox": [100, 200, 420, 230],
                "text": r"x = y + 1",
                "confidence": 0.9,
            }
        ]),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def __init__(self, page_index):
            self.page_index = page_index

        def get_text(self, mode="text"):
            if mode == "blocks" and self.page_index == 119:
                return [(90.0, 198.0, 520.0, 236.0, r"x = y + 1 (12)")]
            return []

    class FakeDoc:
        def __init__(self):
            self.visited_pages: list[int] = []

        def __len__(self):
            return 140

        def __getitem__(self, index):
            self.visited_pages.append(index)
            return FakePage(index)

        def close(self):
            pass

    doc = FakeDoc()
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=doc):
        candidates = provider.extract_candidates(
            pdf_path,
            item_key="ITEM123",
            max_formulas_per_doc=3,
        )

    assert [candidate.equation_number for candidate in candidates] == ["(12)"]
    assert max(doc.visited_pages) >= 119


def test_mineru_cache_provider_pairs_unnumbered_latex_with_pdf_number_records(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    q_latex = (
        r"q = \sqrt { 3 J _ { 2 } } = "
        r"\sqrt { \frac { 1 } { 2 } [ ( \sigma _ { 1 } - \sigma _ { 2 } ) ^ { 2 } ] }"
    )
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [40, 100, 280, 120],
                "text": r"p = - \frac { 1 } { 3 } ( \sigma _ { 1 } + \sigma _ { 2 } + \sigma _ { 3 } )",
                "equation_number": "(1)",
                "confidence": 0.9,
            },
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [40, 760, 280, 790],
                "text": q_latex,
                "confidence": 0.9,
            },
            {
                "type": "interline_equation",
                "page_idx": 0,
                "bbox": [300, 140, 560, 170],
                "text": r"\eta = \frac { - p } { q }",
                "equation_number": "(7)",
                "confidence": 0.9,
            },
        ]),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def get_text(self, mode="text"):
            if mode == "blocks":
                return [
                    (40.0, 100.0, 300.0, 120.0, r"p = - 1/3 (sigma1 + sigma2 + sigma3) (1)"),
                    (40.0, 340.0, 300.0, 360.0, r"q = sqrt(3 J2) = sqrt(1/2 [(sigma1-sigma2)^2]) (6)"),
                    (310.0, 140.0, 560.0, 160.0, r"eta = -p/q (7)"),
                ]
            return ""

    class FakeDoc:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return FakePage()

        def close(self):
            pass

    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=FakeDoc()):
        candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    numbers_by_latex = {candidate.latex: candidate.equation_number for candidate in candidates}
    assert numbers_by_latex[q_latex] == "(6)"
    assert numbers_by_latex[r"\eta = \frac { - p } { q }"] == "(7)"
    assert [candidate.equation_number for candidate in candidates].count("(6)") == 1


def test_mineru_cache_provider_merges_definition_continuation_after_pdf_number_enrichment(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    main_latex = r"\sigma _ { y } ( \xi ) = \sigma _ { y 0 } \cdot f ( \xi )"
    continuation_latex = (
        r"f ( \xi ) = ( 1 - \xi ^ { 2 } ) Y _ { S } + "
        r"\frac { ( 1 + \xi ) } { 2 } \xi ^ { 2 } Y _ { T }"
    )
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 2,
                "bbox": [150, 700, 260, 724],
                "text": main_latex,
                "confidence": 0.9,
            },
            {
                "type": "interline_equation",
                "page_idx": 2,
                "bbox": [58, 728, 360, 756],
                "text": continuation_latex,
                "confidence": 0.9,
            },
        ]),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def __init__(self, page_index):
            self.page_index = page_index

        def get_text(self, mode="text"):
            if mode == "blocks" and self.page_index == 2:
                return [(140.0, 696.0, 300.0, 726.0, r"sigma_y(xi)=sigma_y0 f(xi) (10)")]
            return []

    class FakeDoc:
        def __len__(self):
            return 3

        def __getitem__(self, index):
            return FakePage(index)

        def close(self):
            pass

    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=FakeDoc()):
        candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].equation_number == "(10)"
    assert continuation_latex in candidates[0].latex


def test_mineru_cache_provider_transfers_number_only_pdf_record_to_nearby_latex_candidate(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    latex = (
        r"P _ { m } = \left\{ \begin{array} { l l }"
        r"{ 44.1 \times \left( \frac { \sqrt[3]{W} } { R } \right) ^ { 1.5 } } & { 6 \leq r \leq 12 } \\"
        r"{ 52.4 \times \left( \frac { \sqrt[3]{W} } { R } \right) ^ { 1.13 } } & { r > 12 }"
        r"\end{array} \right."
    )
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {
                "type": "interline_equation",
                "page_idx": 19,
                "bbox": [510, 501, 759, 569],
                "text": latex,
                "confidence": 0.9,
            }
        ]),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def __init__(self, page_index):
            self.page_index = page_index

        def get_text(self, mode="text"):
            if mode == "blocks" and self.page_index == 19:
                return [(540.0, 418.0, 560.0, 431.0, "(15)")]
            return []

    class FakeDoc:
        def __len__(self):
            return 20

        def __getitem__(self, index):
            return FakePage(index)

        def close(self):
            pass

    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(
            formula_candidate_cache_dirs=str(tmp_path / "mineru-cache"),
            formula_candidate_cache_pdf_number_enrichment=True,
        ),
    )

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=FakeDoc()):
        candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].equation_number == "(15)"
    assert candidates[0].latex == latex


def test_mineru_cache_pdf_numbered_fallback_marks_page_budget_truncation(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class FakePage:
        rect = SimpleNamespace(width=600.0, height=800.0)

        def __init__(self, page_index):
            self.page_index = page_index

        def get_text(self, mode="text"):
            if mode == "blocks" and self.page_index == 0:
                return [(80.0, 120.0, 520.0, 142.0, r"x = y (1)")]
            return []

    class FakeDoc:
        def __init__(self):
            self.visited_pages: list[int] = []

        def __len__(self):
            return PDF_TEXT_FALLBACK_MAX_PAGES + 3

        def __getitem__(self, index):
            self.visited_pages.append(index)
            return FakePage(index)

        def close(self):
            pass

    doc = FakeDoc()
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "missing-cache")),
    )

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open", return_value=doc):
        candidates = provider.extract_candidates(pdf_path, item_key="ITEM123")

    assert [candidate.equation_number for candidate in candidates] == ["(1)"]
    assert candidates[0].source == "pdf_text_equation_number_truncated"
    assert doc.visited_pages == list(range(PDF_TEXT_FALLBACK_MAX_PAGES))


def test_mineru_cache_provider_reads_suffixed_content_list_and_nested_math(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "paper_content_list_v2.json").write_text(
        json.dumps({
            "pdf_info": [
                {
                    "page_idx": 4,
                    "blocks": [
                        {
                            "type": "equation_interline",
                            "bbox": [0.1, 0.2, 0.8, 0.3],
                            "content": {"math_content": r"\dot{\epsilon} = \dot{\epsilon}_0 e^{Q/RT}"},
                        }
                    ],
                }
            ]
        }),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "mineru-cache")),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].page_num == 5
    assert candidates[0].latex == r"\dot{\epsilon} = \dot{\epsilon}_0 e^{Q/RT}"


def test_mineru_cache_provider_keeps_same_bbox_on_different_pages(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "content_list.json").write_text(
        json.dumps([
            {"type": "equation", "page_idx": 0, "bbox": [10, 20, 200, 60], "text": r"E = mc^2"},
            {"type": "equation", "page_idx": 1, "bbox": [10, 20, 200, 60], "text": r"\sigma = E\epsilon"},
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "mineru-cache")),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 2
    assert {candidate.page_num for candidate in candidates} == {1, 2}


def test_mineru_cache_provider_reads_block_markdown_formulas(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "full.md").write_text(
        "<!-- page 4 -->\n\nSome text.\n\n$$\nE = mc^2\n$$\n",
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "mineru-cache")),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].page_num == 4
    assert candidates[0].latex == r"E = mc^2"
    assert candidates[0].source == "mineru_markdown"
    assert count_formula_provider_calls(candidates) == 0


def test_mineru_cache_provider_reads_bracket_and_fenced_math_markdown(tmp_path):
    cache_dir = tmp_path / "mineru-cache" / "ITEM123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "full.md").write_text(
        "<!-- page 2 -->\n\\[\\sigma = E\\epsilon\\]\n\n```math\nE = mc^2\n```\n",
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "mineru-cache")),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert [candidate.latex for candidate in candidates] == [r"\sigma = E\epsilon", r"E = mc^2"]
    assert {candidate.page_num for candidate in candidates} == {2}


def test_count_formula_provider_calls_counts_only_candidates_without_latex():
    candidates = [
        FormulaCandidate(page_num=1, bbox=(0, 0, 0, 0), raw_text=r"E=mc^2", confidence=0.9, latex=r"E=mc^2"),
        FormulaCandidate(page_num=1, bbox=(1, 2, 3, 4), raw_text="", confidence=0.8),
    ]

    assert count_formula_provider_calls(candidates) == 1


def test_recognize_formulas_accepts_cached_latex_without_provider(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    candidates = [
        FormulaCandidate(
            page_num=1,
            bbox=(0, 0, 0, 0),
            raw_text=r"E = mc^2",
            confidence=0.92,
            source="mineru_markdown",
            latex=r"E = mc^2",
        )
    ]

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open") as pdf_open:
        formulas = recognize_formulas(pdf_path, None, candidates=candidates)

    assert len(formulas) == 1
    assert formulas[0].latex == r"E = mc^2"
    assert formulas[0].provider == "mineru_markdown"
    pdf_open.assert_not_called()


def test_recognize_formulas_dedupes_and_filters_cached_latex(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    candidates = [
        FormulaCandidate(
            page_num=1,
            bbox=(0, 0, 0, 0),
            raw_text=r"\sigma _ { eq } = A + B \varepsilon ^ n",
            confidence=0.92,
            source="mineru_content_list",
            latex=r"\sigma _ { eq } = A + B \varepsilon ^ n",
        ),
        FormulaCandidate(
            page_num=1,
            bbox=(0, 1, 0, 1),
            raw_text=r"\sigma_{eq}=A+B\varepsilon^n",
            confidence=0.91,
            source="mineru_content_list",
            latex=r"\sigma_{eq}=A+B\varepsilon^n",
        ),
        FormulaCandidate(
            page_num=2,
            bbox=(0, 2, 0, 2),
            raw_text=r"( \mathrm { c } ) \nu _ { i } = 1 3 2 . 4 \mathrm { m } / \mathrm { s }",
            confidence=0.9,
            source="mineru_content_list",
            latex=r"( \mathrm { c } ) \nu _ { i } = 1 3 2 . 4 \mathrm { m } / \mathrm { s }",
        ),
    ]

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open") as pdf_open:
        formulas = recognize_formulas(pdf_path, None, candidates=candidates)

    assert [formula.formula_index for formula in formulas] == [0]
    assert formulas[0].latex == r"\sigma _ { eq } = A + B \varepsilon ^ n"
    pdf_open.assert_not_called()


def test_recognize_formulas_preserves_offset_after_filtered_cached_latex(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    candidates = [
        FormulaCandidate(
            page_num=10,
            bbox=(0, 0, 0, 0),
            raw_text="References",
            confidence=0.92,
            source="mineru_content_list",
            latex=r"\text{References}",
        ),
        FormulaCandidate(
            page_num=11,
            bbox=(0, 1, 0, 1),
            raw_text=r"E = mc^2",
            confidence=0.91,
            source="mineru_content_list",
            latex=r"E = mc^2",
            equation_number="(3.12)",
        ),
    ]

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open") as pdf_open:
        formulas = recognize_formulas(
            pdf_path,
            None,
            candidates=candidates,
            formula_index_offset=80,
        )

    assert [formula.formula_index for formula in formulas] == [81]
    assert formulas[0].equation_number == "(3.12)"
    pdf_open.assert_not_called()


def test_recognize_formulas_clears_stale_number_for_unnumbered_cached_latex(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    candidate = FormulaCandidate(
        page_num=67,
        bbox=(0, 1, 0, 1),
        raw_text=r"2G = \frac{E}{1+\nu}",
        confidence=0.91,
        source="mineru_content_list",
        latex=r"2G = \frac{E}{1+\nu}",
        equation_number="(2.86)",
        equation_number_status="unnumbered",
    )

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open") as pdf_open:
        formulas = recognize_formulas(pdf_path, None, candidates=[candidate])

    assert formulas[0].equation_number == ""
    assert formulas[0].equation_number_status == "unnumbered"
    pdf_open.assert_not_called()


def test_recognize_formulas_preserves_candidate_order_with_cached_and_ocr(tmp_path):
    import pymupdf

    pdf_path = tmp_path / "paper.pdf"
    doc = pymupdf.open()
    doc.new_page(width=300, height=300)
    doc.save(pdf_path)
    doc.close()
    provider = MagicMock()
    provider.name = "simpletex"
    provider.recognize.return_value = SimpleNamespace(latex=r"b = c", confidence=0.95)
    candidates = [
        FormulaCandidate(
            page_num=1,
            bbox=(10, 20, 120, 45),
            raw_text=r"a = b",
            confidence=0.9,
            latex=r"a = b",
            equation_number="(1)",
        ),
        FormulaCandidate(
            page_num=1,
            bbox=(10, 60, 120, 85),
            raw_text="",
            confidence=0.9,
            equation_number="(2)",
        ),
        FormulaCandidate(
            page_num=1,
            bbox=(10, 100, 120, 125),
            raw_text=r"c = d",
            confidence=0.9,
            latex=r"c = d",
            equation_number="(3)",
        ),
    ]

    formulas = recognize_formulas(pdf_path, provider, candidates=candidates)

    assert [formula.equation_number for formula in formulas] == ["(1)", "(2)", "(3)"]
    assert [formula.latex for formula in formulas] == [r"a = b", r"b = c", r"c = d"]


def test_recognize_formulas_skips_non_pdf_bbox_candidates(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    provider = MagicMock()
    provider.name = "simpletex"
    candidates = [
        FormulaCandidate(
            page_num=1,
            bbox=(10, 20, 200, 60),
            raw_text="",
            confidence=0.9,
            bbox_coordinate_space="image",
        )
    ]

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open") as pdf_open:
        pdf_open.return_value.__enter__.return_value = [MagicMock()]
        formulas = recognize_formulas(pdf_path, provider, candidates=candidates)

    assert formulas == []
    provider.recognize.assert_not_called()


def test_equation_number_detection_avoids_plain_step_numbers():
    assert _extract_equation_number(r"E = mc^2\tag{1}") == "(1)"
    assert _extract_equation_number(r"\varepsilon_c = -\ln(1-\varepsilon)\tag{A.5}") == "(A.5)"
    assert _extract_equation_number(r"p=-\frac{1}{3}\mathrm{tr}(\sigma)\tag{A. 1}") == "(A.1)"
    assert _extract_equation_number(r"\xi=\cos(3\Theta)\tag{A: 7}") == "(A.7)"
    assert _extract_equation_number(r"\Delta u_s(t)=u_1(t)-u_2(t)\tag{ð Eq 2Þ}") == "(2)"
    assert _extract_equation_number(r"c_0=\sqrt{E/\rho}\tag{ðEq 4Þ}") == "(4)"
    assert _extract_equation_number(r"\sigma=A+B\epsilon^n\tag{Eq. 1.10}") == "(1.10)"
    assert _extract_equation_number(r"\dot r=\dot\lambda\tag{ð7bÞ}") == "(7b)"
    assert _extract_equation_number(r"R_f=R(r)+3G\Delta p-\tilde q\tag{ð23aÞ}") == "(23a)"
    assert _extract_equation_number(r"\sigma_W=\sigma_S-(\sigma_S-\sigma_Y)e^{-m\varphi/n}\tag{1),}") == "(1)"
    assert _extract_equation_number(r"\eta = \sigma_m / \bar{\sigma}\tag{) 1}") == "(1)"
    assert _extract_equation_number("E = mc^2 (1)") == "(1)"
    assert _extract_equation_number(r"\sigma_y = A + B\epsilon_p^n (1.10)") == "(1.10)"
    assert _extract_equation_number(r"\dot{\epsilon} = \dot{\epsilon}_0 e^{Q/RT} (3.2.1)") == "(3.2.1)"
    assert _extract_equation_number("Eq. (3)") == "(3)"
    assert _extract_equation_number("Follow step (3)") == ""
    assert _extract_equation_number("(1) (2)") == ""
    assert _extract_equation_number("The model uses k = 3 (1)") == ""
    assert _extract_pdf_block_equation_number("E = mc^2 (123)") == ""
    assert _extract_pdf_block_equation_number(r"\epsilon = 0.05 (0.05)") == ""
    assert _extract_pdf_block_equation_number(r"\sigma_y = A + B\epsilon_p^n (2.102)") == "(2.102)"


def test_structured_cache_formula_filter_rejects_units_and_glossaries():
    assert not _is_usable_structured_formula_latex(r"| N _ { \mathrm { i } } | [ \mathrm { k N } ]")
    assert not _is_usable_structured_formula_latex(
        r"\begin{array} { l l l }"
        r"{ \sigma _ { V } { : } } & { \mathrm { v o n ~ M i s e s ~ e q u i v a l e n t ~ s t r e s s } } \\"
        r"{ \sigma _ { y } { : } } & { \mathrm { m a t r i x ~ m a t e r i a l ~ y i e l d ~ s t r e s s } }"
        r"\end{array}"
    )
    assert not _is_usable_structured_formula_latex(
        r"\begin{array} { r l }"
        r"{ \dot { \varepsilon } _ { k k } ^ { p l } \colon }"
        r"& { \mathrm { r a t e ~ o f ~ p l a s t i c ~ s t r a i n s ~ d u e ~ t o"
        r" ~ h y d r o s t a t i c ~ s t r e s s e s } } \\"
        r"{ \bar { \varepsilon } ^ { p l } \colon }"
        r"& { \mathrm { e q u i v a l e n t ~ p l a s t i c ~ s t r a i n } }"
        r"\end{array}"
    )
    assert not _is_usable_structured_formula_latex(
        r"\begin{array} { c c c c c }"
        r"{ { E \left( G p a \right) } } & { { \nu } } & { { A \left( M P a \right) } }"
        r"& { { B \left( M P a \right) } } & { { C } } \\"
        r"{ { \displaystyle \frac { 2 0 6 . 9 } { n } } } & { { 0 . 2 9 } }"
        r"& { { 5 0 4 } } & { { 3 7 0 } } & { { 0 . 0 2 5 } }"
        r"\end{array}"
    )
    assert not _is_usable_structured_formula_latex(
        r"\frac { \dot { \bar { \varepsilon } } _ { 0 } ~ ( s ^ { - 1 } )"
        r" ~ T _ { 0 } ( ^ { \circ } C ) ~ T _ { m } ( ^ { \circ } C )"
        r" ~ A ( M P a ) ~ B ( M P a ) ~ n } { 1 }"
    )
    assert not _is_usable_structured_formula_latex(
        r"\begin{array}{c} \begin{array} { r }"
        r"{ \overbrace { \mathrm { ~ D e f . ~ } ( m m ) } ^ { \mathrm { ~ G 1 ~ } }"
        r"\overbrace { 1 . 0 7 7 \quad 1 . 0 7 4 \quad 1 . 0 4 3 \quad 1 . 0 1 1 }"
        r"^ { \mathrm { ~ G 2 ~ } } } \end{array} \end{array}"
    )
    assert _is_usable_structured_formula_latex(
        r"f ^ { * } ( f ) = \left\{ \begin{array} { c l }"
        r"{ f ; } & { f \leq f _ { C } } \\"
        r"{ f _ { c } + \kappa ( f - f _ { C } ) ; } & { f > f _ { C } }"
        r"\end{array} \right.\tag{6),}"
    )


def test_formula_text_match_keeps_compact_subscript_tokens_distinct():
    rf_candidate = r"| x _ { i + 1 } - x _ { i } | \leqslant \varepsilon _ { R F }"
    nr_candidate = r"\mid x _ { i + 1 } - x _ { i } \mid \leqslant \varepsilon _ { N R }"
    pdf_rf = "This process is repeated until the last two roots satisfying: |xi+1 −xi| ⩽εRF (2.76)"
    pdf_nr = "| xi+1 −xi |⩽εNR (2.80)"

    assert _formula_text_match_score(rf_candidate, pdf_rf) > _formula_text_match_score(rf_candidate, pdf_nr)
    assert _formula_text_match_score(nr_candidate, pdf_nr) > _formula_text_match_score(nr_candidate, pdf_rf)


def test_pdf_text_number_enrichment_does_not_reuse_existing_document_number():
    candidates = [
        FormulaCandidate(
            page_num=1,
            bbox=(0, 0, 100, 20),
            raw_text="a=b",
            confidence=0.9,
            latex=r"a=b",
            equation_number="(1)",
            source="mineru_content_list",
        ),
        FormulaCandidate(
            page_num=2,
            bbox=(0, 40, 100, 60),
            raw_text="c=d",
            confidence=0.9,
            latex=r"c=d",
            source="mineru_content_list",
        ),
    ]
    records_by_page = {
        2: [
            _PdfEquationNumberRecord(
                number="(1)",
                y_center=50,
                x_right=100,
                standalone=False,
                bbox=(0, 40, 100, 60),
                text="c=d (1)",
                page_width=200,
                page_height=200,
            )
        ]
    }

    enriched = _enrich_candidate_equation_numbers_from_pdf_text(candidates, records_by_page)

    assert [candidate.equation_number for candidate in enriched] == ["(1)", ""]


def test_pdf_text_number_enrichment_ignores_weak_ambiguous_math_matches():
    candidates = [
        FormulaCandidate(
            page_num=3,
            bbox=(403, 535, 593, 561),
            raw_text="",
            confidence=0.95,
            latex=(
                r"\begin{array} { r } { \sigma _ { m } = \frac { 1 } { 3 } "
                r"( \sigma _ { 1 } + \sigma _ { 2 } + \sigma _ { 3 } ) . } \end{array}"
            ),
            source="mineru_content_list",
        ),
        FormulaCandidate(
            page_num=3,
            bbox=(400, 699, 596, 743),
            raw_text="",
            confidence=0.95,
            latex=r"\Theta = \frac { 1 } { 3 } \operatorname { arccos } \left( \frac { J _ { 3 } } { 2 } \right)",
            source="mineru_content_list",
        ),
    ]
    records_by_page = {
        3: [
            _PdfEquationNumberRecord(
                number="(4)",
                y_center=531.6,
                x_right=579.0,
                standalone=False,
                bbox=(36.0, 507.1, 579.0, 556.0),
                text=(
                    "√2 ඥ(𝜎𝜎1 −𝜎𝜎2)2 + (𝜎𝜎2 −𝜎𝜎3)2 "
                    "+ (𝜎𝜎3 −𝜎𝜎1)2 = ඥ3𝐽𝐽2., (4)"
                ),
                page_width=612.0,
                page_height=792.0,
            ),
            _PdfEquationNumberRecord(
                number="(5)",
                y_center=567.8,
                x_right=573.6,
                standalone=False,
                bbox=(349.8, 555.3, 573.6, 580.3),
                text="3 2ቇ, (5)",
                page_width=612.0,
                page_height=792.0,
            )
        ]
    }

    enriched = _enrich_candidate_equation_numbers_from_pdf_text(candidates, records_by_page)

    assert enriched[0].equation_number == ""


def test_infer_missing_equation_numbers_between_numbered_after_late_fallback():
    candidates = [
        FormulaCandidate(
            page_num=18,
            bbox=(510, 98, 650, 113),
            raw_text="",
            confidence=0.95,
            latex=r"\varepsilon_f = d_1 + d_2 exp(-d_3\eta)",
            equation_number="(14)",
        ),
        FormulaCandidate(
            page_num=18,
            bbox=(510, 604, 685, 634),
            raw_text="",
            confidence=0.95,
            latex=r"Error = \frac{1}{n}\sum_i^n\delta_i",
        ),
        FormulaCandidate(
            page_num=18,
            bbox=(512, 678, 630, 712),
            raw_text="",
            confidence=0.95,
            latex=r"\delta_i=\left|\frac{\varepsilon_f^{exp}-\varepsilon_f^{pre}}{\varepsilon_f^{exp}}\right|",
        ),
        FormulaCandidate(
            page_num=21,
            bbox=(58, 403, 139, 430),
            raw_text="",
            confidence=0.95,
            latex=r"D=\sum\frac{\Delta\varepsilon_{eq}}{\varepsilon_f}",
            equation_number="(17)",
        ),
    ]

    inferred = _infer_missing_equation_numbers_between_numbered(candidates)

    assert [candidate.equation_number for candidate in inferred] == ["(14)", "(15)", "(16)", "(17)"]


def test_infer_missing_equation_numbers_repairs_shifted_cache_number_between_anchors():
    candidates = [
        FormulaCandidate(
            page_num=6,
            bbox=(115, 179, 247, 195),
            raw_text="",
            confidence=0.95,
            latex=r"\sigma_{MC}=\bar{\sigma}_h+\mu\sigma_n",
            equation_number="(8)",
        ),
        FormulaCandidate(
            page_num=6,
            bbox=(115, 238, 393, 270),
            raw_text="",
            confidence=0.95,
            latex=r"\bar{\sigma}_h=\left(\frac{|\sigma_1-\sigma_2|^a}{2}\right)^{1/a}",
            equation_number="(10)",
        ),
        FormulaCandidate(
            page_num=6,
            bbox=(115, 345, 250, 362),
            raw_text="",
            confidence=0.95,
            latex=r"\sigma_{MC}=\bar{\sigma}_h+\mu\sigma_m",
        ),
        FormulaCandidate(
            page_num=6,
            bbox=(137, 823, 329, 850),
            raw_text="",
            confidence=0.95,
            latex=r"f(\sigma_j)=\tilde I_1+(J_2^{3/2}-cJ_3)^{1/3}",
            equation_number="(11)",
        ),
    ]

    inferred = _infer_missing_equation_numbers_between_numbered(candidates)

    assert [candidate.equation_number for candidate in inferred] == ["(8)", "(9)", "(10)", "(11)"]


def test_split_multirow_independent_formula_candidates_splits_array_rows():
    candidates = [
        FormulaCandidate(
            page_num=33,
            bbox=(453.0, 381.0, 517.0, 451.0),
            raw_text="",
            confidence=0.95,
            source="mineru_content_list",
            bbox_coordinate_space="unknown",
            latex=(
                r"\begin{array} { c } { \sigma = \frac { F } { A _ { 0 } } } \\ "
                r"{ \varepsilon = \frac { d l } { l _ { 0 } } } \end{array}"
            ),
        )
    ]

    split = _split_multirow_independent_formula_candidates(candidates)

    assert len(split) == 2
    assert split[0].latex == r"\sigma = \frac { F } { A _ { 0 } }"
    assert split[1].latex == r"\varepsilon = \frac { d l } { l _ { 0 } }"
    assert split[0].bbox[3] <= split[1].bbox[1]


def test_split_multirow_independent_formula_candidates_splits_bold_math_rows():
    candidates = [
        FormulaCandidate(
            page_num=2,
            bbox=(647.0, 546.0, 769.0, 582.0),
            raw_text="",
            confidence=0.95,
            source="mineru_content_list",
            bbox_coordinate_space="unknown",
            latex=(
                r"\begin{array} { r } "
                r"{ \pmb { \varepsilon } _ { T } = \ln ( 1 + \pmb { \varepsilon } _ { E } ) } \\ "
                r"{ \pmb { \sigma } _ { T } = \pmb { \sigma } _ { \varepsilon } "
                r"( 1 + \pmb { \varepsilon } _ { E } ) } \end{array}"
            ),
        )
    ]

    split = _split_multirow_independent_formula_candidates(candidates)

    assert len(split) == 2
    assert split[0].latex.startswith(r"\pmb { \varepsilon }")
    assert split[1].latex.startswith(r"\pmb { \sigma }")


def test_split_multirow_independent_formula_candidates_splits_displaystyle_rows():
    candidates = [
        FormulaCandidate(
            page_num=3,
            bbox=(515.0, 785.0, 762.0, 906.0),
            raw_text="",
            confidence=0.95,
            source="mineru_content_list",
            bbox_coordinate_space="unknown",
            latex=(
                r"\begin{array} { l } "
                r"{ \displaystyle \eta = \frac { I _ { 1 } } { 3 \sqrt { 3 J _ { 2 } } } , } \\ "
                r"{ \displaystyle \bar { \theta } = 1 - \frac { 2 } { \pi } \cos ^ { - 1 } ( x ) , } \\ "
                r"{ \displaystyle \bar { \theta } = 1 - \frac { 3 } { 2 } \pi \theta . } "
                r"\end{array}"
            ),
        )
    ]

    split = _split_multirow_independent_formula_candidates(candidates)

    assert len(split) == 3
    assert split[0].latex.startswith(r"\displaystyle \eta")
    assert split[1].latex.startswith(r"\displaystyle \bar")
    assert split[2].latex.startswith(r"\displaystyle \bar")


def test_split_multirow_independent_formula_candidates_ignores_style_only_rows():
    candidates = [
        FormulaCandidate(
            page_num=13,
            bbox=(58.0, 427.0, 174.0, 520.0),
            raw_text="",
            confidence=0.95,
            source="mineru_content_list",
            bbox_coordinate_space="unknown",
            equation_number="(13)",
            latex=(
                r"\begin{array} { l } "
                r"{ { \displaystyle \bar{\eta}_D = \frac{1}{D_f}\int \eta(D)dD } } \\ "
                r"{ { \displaystyle } } \\ "
                r"{ { \displaystyle \bar{\theta}_D = \frac{1}{D_f}\int \bar{\theta}(D)dD } } "
                r"\end{array}"
            ),
        ),
        FormulaCandidate(
            page_num=14,
            bbox=(60.0, 723.0, 425.0, 741.0),
            raw_text="",
            confidence=0.95,
            source="mineru_content_list",
            bbox_coordinate_space="unknown",
            equation_number="(15)",
            latex=r"\Delta W = W_E + W_P",
        ),
    ]

    split = _split_multirow_independent_formula_candidates(candidates)
    inferred = _infer_missing_equation_numbers_between_numbered(split)

    assert len(split) == 3
    assert split[0].equation_number == "(13)"
    assert split[1].equation_number == ""
    assert split[1].latex.startswith(r"\displaystyle \bar{\theta}")
    assert inferred[1].equation_number == "(14)"


def test_split_multirow_independent_formula_candidates_keeps_continuation_rows_together():
    candidates = [
        FormulaCandidate(
            page_num=17,
            bbox=(510.0, 827.0, 931.0, 887.0),
            raw_text="",
            confidence=0.95,
            source="mineru_content_list",
            latex=(
                r"\begin{array} { l } "
                r"{ \displaystyle \varepsilon _ { f } = D _ { 1 } e ^ { - D _ { 2 } \eta } } \\ "
                r"{ \displaystyle = D _ { 3 } e ^ { - D _ { 4 } \eta } + D _ { 5 } } "
                r"\end{array}"
            ),
        )
    ]

    split = _split_multirow_independent_formula_candidates(candidates)

    assert len(split) == 1
    assert split[0].latex == candidates[0].latex


def test_split_multirow_independent_formula_candidates_splits_aligned_top_level_rows():
    candidates = [
        FormulaCandidate(
            page_num=13,
            bbox=(60.0, 641.0, 477.0, 766.0),
            raw_text="",
            confidence=0.95,
            source="mineru_content_list",
            bbox_coordinate_space="unknown",
            equation_number="(7)",
            latex=(
                r"\begin{array} { r l r } "
                r"{ \varepsilon_f = A + B } & \\ { + C } & { \mathcal { O } } \\ "
                r"{ \alpha = \left\{ \begin{array} { l l } { 1 } & { \bar{\theta} \ge 0 } \\ "
                r"{ c_\theta } & { \bar{\theta} < 0 } \end{array} \right. } & { \mathfrak { C } } & { \alpha } "
                r"\end{array}"
            ),
        ),
        FormulaCandidate(
            page_num=13,
            bbox=(58.0, 809.0, 339.0, 844.0),
            raw_text="",
            confidence=0.95,
            source="mineru_content_list",
            bbox_coordinate_space="unknown",
            equation_number="(9)",
            latex=r"\bar{\theta}=1-\frac{2}{\pi}\cos^{-1}(x)",
        ),
    ]

    split = _split_multirow_independent_formula_candidates(candidates)
    inferred = _infer_missing_equation_numbers_between_numbered(split)

    assert len(split) == 3
    assert split[0].equation_number == "(7)"
    assert split[0].latex == r"\varepsilon_f = A + B + C"
    assert split[1].equation_number == ""
    assert r"\begin{array}" in split[1].latex
    assert inferred[1].equation_number == "(8)"
    assert inferred[1].equation_number_status == "inferred"


def test_split_multirow_independent_formula_candidates_merges_non_relation_middle_rows():
    candidates = [
        FormulaCandidate(
            page_num=5,
            bbox=(552.0, 72.0, 865.0, 226.0),
            raw_text="",
            confidence=0.95,
            source="mineru_content_list",
            bbox_coordinate_space="unknown",
            equation_number="(2)",
            latex=(
                r"\begin{array} { c } "
                r"{ \varepsilon_f = \left\{ A + B \right. } \\ "
                r"{ \left[ C + D \right] \times } \\ "
                r"{ (1 + E)(1 + F) } \\ "
                r"{ c_\theta^{\alpha\alpha} = \left\{ \frac{1}{c_\theta^\alpha} \right. } "
                r"\end{array}"
            ),
        ),
        FormulaCandidate(
            page_num=5,
            bbox=(526.0, 271.0, 877.0, 308.0),
            raw_text="",
            confidence=0.95,
            source="mineru_content_list",
            bbox_coordinate_space="unknown",
            equation_number="(4)",
            latex=r"\bar{\theta}=1-\frac{2}{\pi}\cos^{-1}(x)",
        ),
    ]

    split = _split_multirow_independent_formula_candidates(candidates)
    inferred = _infer_missing_equation_numbers_between_numbered(split)

    assert len(split) == 3
    assert split[0].equation_number == "(2)"
    assert "(1 + E)(1 + F)" in split[0].latex
    assert split[1].equation_number == ""
    assert r"c_\theta" in split[1].latex
    assert inferred[1].equation_number == "(3)"


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


def test_dedupe_candidates_keeps_overlapping_pdf_records_with_different_equation_numbers():
    candidates = [
        FormulaCandidate(
            page_num=1,
            bbox=(120, 400, 300, 460),
            raw_text="x = y (4) z = w (5)",
            confidence=0.72,
            equation_number="(4)",
        ),
        FormulaCandidate(
            page_num=1,
            bbox=(120, 400, 300, 460),
            raw_text="x = y (4) z = w (5)",
            confidence=0.72,
            equation_number="(5)",
        ),
    ]

    kept = _dedupe_candidates(candidates)

    assert [candidate.equation_number for candidate in kept] == ["(4)", "(5)"]


def test_merge_split_formula_candidates_merges_definition_continuation_used_by_main_formula():
    candidates = [
        FormulaCandidate(
            page_num=3,
            bbox=(150, 700, 260, 724),
            raw_text="",
            confidence=0.95,
            latex=r"\sigma _ { y } ( \xi ) = \sigma _ { y 0 } \cdot f ( \xi )",
            equation_number="(10)",
            source="mineru_content_list",
        ),
        FormulaCandidate(
            page_num=3,
            bbox=(58, 728, 360, 756),
            raw_text="",
            confidence=0.95,
            latex=(
                r"f ( \xi ) = ( 1 - \xi ^ { 2 } ) Y _ { S } + "
                r"\frac { ( 1 + \xi ) } { 2 } \xi ^ { 2 } Y _ { T }"
            ),
            source="mineru_content_list",
        ),
    ]

    merged = _merge_split_formula_candidates(candidates)

    assert len(merged) == 1
    assert merged[0].equation_number == "(10)"
    assert "f ( \\xi ) =" in merged[0].latex


def test_merge_split_formula_candidates_merges_definition_continuation_when_number_is_on_second_line():
    candidates = [
        FormulaCandidate(
            page_num=3,
            bbox=(360, 615, 420, 628),
            raw_text="",
            confidence=0.95,
            latex=r"H ( \xi ) = H _ { 0 } \cdot g ( \xi )",
            source="mineru_content_list",
        ),
        FormulaCandidate(
            page_num=3,
            bbox=(308, 629, 480, 653),
            raw_text="",
            confidence=0.95,
            latex=(
                r"g ( \xi ) = ( 1 - \xi ^ { 2 } ) H _ { S } + "
                r"\frac { ( 1 + \xi ) } { 2 } \xi ^ { 2 } H _ { T }"
            ),
            equation_number="(12)",
            source="mineru_content_list",
        ),
    ]

    merged = _merge_split_formula_candidates(candidates)

    assert len(merged) == 1
    assert merged[0].equation_number == "(12)"
    assert "H ( \\xi ) =" in merged[0].latex
    assert "g ( \\xi ) =" in merged[0].latex


def test_merge_split_formula_candidates_merges_qquad_sibling_definition_with_numbered_formula():
    candidates = [
        FormulaCandidate(
            page_num=2,
            bbox=(512, 484, 813, 523),
            raw_text="",
            confidence=0.95,
            latex=r"\dot { f } _ { G } = \left( 1 - f \right) tr \left( \dot { \varepsilon } _ { p } \right)",
            equation_number="(3)",
            source="mineru_content_list",
        ),
        FormulaCandidate(
            page_num=2,
            bbox=(512, 523, 813, 562),
            raw_text="",
            confidence=0.95,
            latex=(
                r"\qquad \dot { f } _ { N } = \displaystyle \frac { f _ { N } } "
                r"{ S _ { N } \sqrt { 2 \pi } } \exp \left[ - \frac { 1 } { 2 } "
                r"\left( \frac { \dot { \varepsilon } _ { p } - \varepsilon _ { N } } "
                r"{ S _ { N } } \right) ^ { 2 } \right] \dot { \varepsilon } _ { p }"
            ),
            source="mineru_content_list",
        ),
    ]

    merged = _merge_split_formula_candidates(candidates)

    assert len(merged) == 1
    assert merged[0].equation_number == "(3)"
    assert r"\dot { f } _ { G }" in merged[0].latex
    assert r"\dot { f } _ { N }" in merged[0].latex


def test_merge_split_formula_candidates_merges_adjacent_parts_with_same_number():
    candidates = [
        FormulaCandidate(
            page_num=96,
            bbox=(277, 625, 715, 651),
            raw_text="",
            confidence=0.95,
            latex=r"\left\{ \sigma_{\bar\varepsilon^p} = A + B\bar\varepsilon^{p^n}",
            equation_number="(3.17)",
            source="mineru_content_list",
        ),
        FormulaCandidate(
            page_num=96,
            bbox=(277, 677, 715, 703),
            raw_text="",
            confidence=0.95,
            latex=r"\sigma_{\dot{\bar\varepsilon}^p} = 1 + C(T)\ln(\dot\varepsilon/\dot\varepsilon_0) \right.",
            equation_number="(3.17)",
            source="mineru_content_list",
        ),
        FormulaCandidate(
            page_num=96,
            bbox=(277, 651, 715, 677),
            raw_text="",
            confidence=0.95,
            latex=r"\sigma_T(T)=1-m_1\left(\frac{T-T_0}{T_m-T_0}\right)^{m_2}",
            equation_number="(3.16)",
            source="mineru_content_list",
        ),
    ]

    merged = _merge_split_formula_candidates(candidates)

    assert len(merged) == 2
    assert [candidate.equation_number for candidate in merged] == ["(3.17)", "(3.16)"]
    assert "C(T)" in merged[0].latex


def test_merge_number_only_pdf_candidates_transfers_number_to_nearby_latex_candidate():
    latex = (
        r"P _ { \mathrm { m } } = \left\{ \begin{array} { l l }"
        r"{ 44.1 \times \left( \frac { \sqrt[3]{W} } { R } \right) ^ { 1.5 } } & { 6 \leq r \leq 12 }"
        r"\end{array} \right."
    )
    candidates = [
        FormulaCandidate(
            page_num=20,
            bbox=(309.5, 386.7, 564.0, 448.7),
            raw_text="(15)",
            confidence=0.72,
            equation_number="(15)",
            source="pdf_text_equation_number",
            bbox_coordinate_space="pdf",
        ),
        FormulaCandidate(
            page_num=20,
            bbox=(510, 501, 759, 569),
            raw_text=latex,
            confidence=0.95,
            latex=latex,
            source="mineru_content_list",
            bbox_coordinate_space="unknown",
        ),
    ]

    merged = _merge_number_only_pdf_candidates_with_latex_candidates(candidates)

    assert len(merged) == 1
    assert merged[0].equation_number == "(15)"
    assert merged[0].latex == latex


def test_dedupe_candidates_removes_same_page_equation_number_duplicates():
    candidates = [
        FormulaCandidate(
            page_num=2,
            bbox=(0, 0, 100, 40),
            raw_text="",
            confidence=0.9,
            latex=r"E=mc^2\tag{1}",
            equation_number="(1)",
        ),
        FormulaCandidate(
            page_num=2,
            bbox=(200, 0, 300, 40),
            raw_text="",
            confidence=0.9,
            latex=r"E=mc^2",
            equation_number="",
        ),
        FormulaCandidate(
            page_num=2,
            bbox=(400, 0, 500, 40),
            raw_text="",
            confidence=0.9,
            latex=r"a=b\tag{1}",
            equation_number="(1)",
        ),
        FormulaCandidate(
            page_num=3,
            bbox=(0, 0, 100, 40),
            raw_text="",
            confidence=0.9,
            latex=r"E=mc^2\tag{1}",
            equation_number="(1)",
        ),
    ]

    kept = _dedupe_candidates(candidates)

    assert [(candidate.page_num, candidate.latex) for candidate in kept] == [
        (2, r"E=mc^2\tag{1}"),
        (3, r"E=mc^2\tag{1}"),
    ]


def test_dedupe_candidates_prefers_numbered_near_duplicate_across_pages():
    unnumbered = (
        r"u _ { i , j } = \left\{ { \begin{array} { l l }"
        r"{ \nu _ { i , j } } & { { \mathrm { i f ~ } } r _ { j } \leq C _ { r } ,"
        r"\ \mathrm { o r } \ j = j _ { r a n d } } \\"
        r"{ \theta _ { i , j } } & { } \end{array} } \right."
    )
    numbered = (
        r"u _ { i , j } = \left\{ { \begin{array} { l l }"
        r"{ \nu _ { i , j } } & { { \mathrm { i f ~ } } r _ { j } \leq C _ { r } ,"
        r"\ { \mathrm { o r ~ } } j = j _ { r a n d } } \\"
        r"{ \theta _ { i , j } } & { } \end{array} } \right.\tag{19}"
    )
    candidates = [
        FormulaCandidate(
            page_num=7,
            bbox=(153, 808, 396, 840),
            raw_text=unnumbered,
            confidence=0.95,
            latex=unnumbered,
        ),
        FormulaCandidate(
            page_num=8,
            bbox=(72, 667, 313, 700),
            raw_text=numbered,
            confidence=0.95,
            equation_number="(19)",
            latex=numbered,
        ),
    ]

    kept = _dedupe_candidates(candidates)

    assert len(kept) == 1
    assert kept[0].equation_number == "(19)"


def test_dedupe_candidates_prefers_cached_latex_over_bbox_only_duplicate():
    candidates = [
        FormulaCandidate(
            page_num=1,
            bbox=(100, 100, 300, 140),
            raw_text="(2)",
            confidence=0.95,
            equation_number="(2)",
            source="pdf_text_equation_number",
            latex="",
        ),
        FormulaCandidate(
            page_num=1,
            bbox=(100, 100, 300, 140),
            raw_text=r"\sigma = E\epsilon",
            confidence=0.7,
            equation_number="(2)",
            source="mineru_content_list",
            latex=r"\sigma = E\epsilon",
        ),
    ]

    kept = _dedupe_candidates(candidates)

    assert len(kept) == 1
    assert kept[0].source == "mineru_content_list"
    assert kept[0].latex == r"\sigma = E\epsilon"


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

    assert text.splitlines()[0] == "Formula on page 3, index #2 (1)"
    assert "Context: Energy is defined" in text
    assert text.splitlines()[-1] == r"LaTeX: E = mc^2"
