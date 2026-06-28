import json
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from zotpilot.feature_extraction.formula_ocr import (
    FormulaCandidate,
    SimpleTexFormulaOCRProvider,
    _attach_standalone_equation_numbers,
    _candidate_confidence,
    _coerce_provider_result,
    _coerce_simpletex_response,
    _dedupe_candidates,
    _extract_block_signals,
    _extract_equation_number,
    _is_likely_non_formula_text,
    _merge_multiline_formula_candidates,
    _simpletex_app_headers,
    create_formula_candidate_provider,
    create_formula_ocr_provider,
    is_high_quality_formula_latex,
    recognize_formulas,
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


def test_formula_candidate_confidence_rejects_common_paper_prose_noise():
    noisy_blocks = [
        "Abstract",
        "Keywords: ductile fracture; stress triaxiality; finite element simulation",
        "John Smith, Department of Mechanical Engineering, Example University",
        "https://doi.org/10.1016/j.ijsolstr.2024.112345",
        "10.1016/j.ijsolstr.2024.112345",
        "Figure 3. Force-displacement curves for different specimens.",
        "The model parameters were calibrated according to Bai et al. [12].",
        "References",
        "6061-T651 铝合金动态力学性能及断裂准则修正 *",
        "800 mm/min ，应变率为 0.133 ～ 0.533 s  1 。可以发现，在低应变率下，"
        "6061-T651 铝合金材料无显著的应变率强化效应。",
        "式中，η 为应力三轴度；A pl，n F，c 1，c 2 为材料性能参数；θ 为 Lode 角。",
        "试样类型 应力三轴度 η Lode 角 θ 断裂应变 ε f",
        "R =∞ 0.612 1 0.459 5",
    ]

    for text in noisy_blocks:
        assert _is_likely_non_formula_text(text)
        assert _candidate_confidence(text, (10.0, 20.0, 260.0, 42.0), set(), set()) == 0.0

    formula_text = r"\sigma_{eq} = \sqrt{3J_2} (1)"
    assert not _is_likely_non_formula_text(formula_text)
    assert _candidate_confidence(
        formula_text,
        (10.0, 20.0, 260.0, 42.0),
        {"CMMI10"},
        {2},
    ) > 0.0

    decimal_division = "k = 10.5847/d"
    assert not _is_likely_non_formula_text(decimal_division)
    assert _candidate_confidence(
        decimal_division,
        (10.0, 20.0, 260.0, 42.0),
        set(),
        set(),
    ) > 0.0


def test_equation_number_detection_avoids_plain_step_numbers():
    assert _extract_equation_number("E = mc^2 (1)") == "(1)"
    assert _extract_equation_number("Eq. (3)") == "(3)"
    assert _extract_equation_number(
        r"\bar{\theta}=\frac{2\sigma_2-\sigma_1-\sigma_3}{\sigma_1-\sigma_3} (1.10)"
    ) == "(1.10)"
    assert _extract_equation_number(r"\varepsilon_f = D_1 + D_2 e^{D_3\eta} (2-1)") == "(2-1)"
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


def test_attach_standalone_equation_number_keeps_formula_crop_bbox():
    candidates = [
        FormulaCandidate(
            page_num=1,
            bbox=(100.0, 96.0, 300.0, 116.0),
            raw_text=r"E = mc^2",
            confidence=0.9,
            equation_number_status="missing",
        )
    ]
    number_blocks = [((500.0, 98.0, 520.0, 114.0), "(1)")]

    attached = _attach_standalone_equation_numbers(candidates, number_blocks)

    assert attached[0].equation_number == "(1)"
    assert attached[0].equation_number_status == "provided"
    assert attached[0].bbox == candidates[0].bbox


def test_merge_multiline_formula_candidates_preserves_trailing_equation_number():
    candidates = [
        FormulaCandidate(
            page_num=1,
            bbox=(100.0, 100.0, 430.0, 120.0),
            raw_text=r"\sigma = (A + B\varepsilon^n)",
            confidence=0.82,
            font_names=("CMMI10",),
            equation_number_status="missing",
        ),
        FormulaCandidate(
            page_num=1,
            bbox=(104.0, 123.0, 500.0, 143.0),
            raw_text=r"[1 + C\ln(\dot{\varepsilon}/\dot{\varepsilon}_0)] (1)",
            confidence=0.8,
            font_names=("CMMI10",),
            equation_number="(1)",
            equation_number_status="provided",
        ),
    ]

    merged = _merge_multiline_formula_candidates(candidates)

    assert len(merged) == 1
    assert merged[0].raw_text == (
        r"\sigma = (A + B\varepsilon^n) "
        r"[1 + C\ln(\dot{\varepsilon}/\dot{\varepsilon}_0)] (1)"
    )
    assert merged[0].bbox == (100.0, 100.0, 500.0, 143.0)
    assert merged[0].equation_number == "(1)"
    assert merged[0].equation_number_status == "provided"


def test_merge_multiline_formula_candidates_keeps_separate_numbered_equations():
    candidates = [
        FormulaCandidate(
            page_num=1,
            bbox=(100.0, 100.0, 430.0, 120.0),
            raw_text=r"a = b + c (3)",
            confidence=0.82,
            equation_number="(3)",
            equation_number_status="provided",
        ),
        FormulaCandidate(
            page_num=1,
            bbox=(104.0, 123.0, 430.0, 143.0),
            raw_text=r"d = e + f (4)",
            confidence=0.8,
            equation_number="(4)",
            equation_number_status="provided",
        ),
    ]

    merged = _merge_multiline_formula_candidates(candidates)

    assert [candidate.equation_number for candidate in merged] == ["(3)", "(4)"]


def test_dedupe_candidates_keeps_best_overlapping_candidate():
    candidates = [
        FormulaCandidate(page_num=1, bbox=(0, 0, 100, 40), raw_text="a=b", confidence=0.9),
        FormulaCandidate(page_num=1, bbox=(2, 2, 98, 38), raw_text="a=b", confidence=0.7),
        FormulaCandidate(page_num=1, bbox=(200, 0, 300, 40), raw_text="c=d", confidence=0.8),
    ]

    kept = _dedupe_candidates(candidates)

    assert [c.raw_text for c in kept] == ["a=b", "c=d"]


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
                "equation_number": "(3)",
                "confidence": 0.91,
            },
            {"type": "text", "text": "Introduction"},
        ]),
        encoding="utf-8",
    )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=str(tmp_path / "mineru-cache")),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", item_key="ITEM123")

    assert len(candidates) == 1
    assert candidates[0].page_num == 3
    assert candidates[0].bbox == (10.0, 20.0, 200.0, 60.0)
    assert candidates[0].latex == r"\sigma = E\epsilon"
    assert candidates[0].equation_number == "(3)"
    assert candidates[0].source == "mineru_content_list"


def test_mineru_cache_provider_follows_manifest_references(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    manifest = cache_dir / "manifest.json"
    content_list = cache_dir / "content_list.json"
    content_list.write_text(
        json.dumps([
            {
                "type": "equation",
                "page_idx": 0,
                "bbox": [1, 2, 3, 4],
                "text": r"$$E = mc^2\tag{1}$$",
            }
        ]),
        encoding="utf-8",
    )
    manifest.write_text(json.dumps({"content_list": "content_list.json"}), encoding="utf-8")
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=""),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", cache_paths=(manifest,))

    assert len(candidates) == 1
    assert candidates[0].latex == r"E = mc^2\tag{1}"
    assert candidates[0].equation_number == "(1)"
    assert candidates[0].source == "mineru_content_list"


def test_mineru_cache_provider_rejects_manifest_path_traversal(tmp_path):
    cache_dir = tmp_path / "cache"
    outside_dir = tmp_path / "outside"
    cache_dir.mkdir()
    outside_dir.mkdir()
    manifest = cache_dir / "manifest.json"
    outside_content = outside_dir / "content_list.json"
    outside_content.write_text(
        json.dumps([{"type": "equation", "text": r"E = mc^2"}]),
        encoding="utf-8",
    )
    manifest.write_text(json.dumps({"content_list": "../outside/content_list.json"}), encoding="utf-8")
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=""),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", cache_paths=(manifest,))

    assert candidates == []


def test_mineru_cache_provider_reads_llm_for_zotero_zip_cache(tmp_path):
    cache_zip = tmp_path / "LLM-for-Zotero-MinerU-cache-ATT001.zip"
    with zipfile.ZipFile(cache_zip, "w") as archive:
        archive.writestr(
            "cache/content_list.json",
            json.dumps([
                {
                    "type": "equation",
                    "page_idx": 1,
                    "bbox": [10, 20, 30, 40],
                    "text": r"\bar{\theta}=\frac{2\sigma_2-\sigma_1-\sigma_3}{\sigma_1-\sigma_3}",
                    "eq_number": "2.1",
                }
            ]),
        )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=""),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", cache_paths=(cache_zip,))

    assert len(candidates) == 1
    assert candidates[0].page_num == 2
    assert candidates[0].equation_number == "(2.1)"
    assert candidates[0].source == "mineru_content_list"


def test_mineru_cache_provider_skips_oversized_zip_members(tmp_path, monkeypatch):
    from zotpilot.feature_extraction import formula_ocr

    monkeypatch.setattr(formula_ocr, "MAX_FORMULA_CACHE_ZIP_MEMBER_SIZE_BYTES", 16, raising=False)
    cache_zip = tmp_path / "LLM-for-Zotero-MinerU-cache-ATT001.zip"
    with zipfile.ZipFile(cache_zip, "w") as archive:
        archive.writestr(
            "cache/content_list.json",
            json.dumps([{"type": "equation", "text": r"E = mc^2"}]),
        )
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=""),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", cache_paths=(cache_zip,))

    assert candidates == []


def test_mineru_cache_provider_skips_zip_with_too_many_formula_members(tmp_path, monkeypatch):
    from zotpilot.feature_extraction import formula_ocr

    monkeypatch.setattr(formula_ocr, "MAX_FORMULA_CACHE_ZIP_MEMBERS", 1, raising=False)
    cache_zip = tmp_path / "LLM-for-Zotero-MinerU-cache-ATT001.zip"
    with zipfile.ZipFile(cache_zip, "w") as archive:
        archive.writestr("a/content_list.json", json.dumps([{"type": "equation", "text": r"a=b"}]))
        archive.writestr("b/middle.json", json.dumps([{"type": "equation", "text": r"c=d"}]))
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=""),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", cache_paths=(cache_zip,))

    assert candidates == []


def test_mineru_cache_provider_preserves_same_basename_zip_members(tmp_path):
    cache_zip = tmp_path / "LLM-for-Zotero-MinerU-cache-ATT001.zip"
    with zipfile.ZipFile(cache_zip, "w") as archive:
        archive.writestr("paper-a/content_list.json", json.dumps([{"type": "equation", "text": r"a=b"}]))
        archive.writestr("paper-b/content_list.json", json.dumps([{"type": "equation", "text": r"c=d"}]))
    provider = create_formula_candidate_provider(
        "mineru_cache",
        config=SimpleNamespace(formula_candidate_cache_dirs=""),
    )

    candidates = provider.extract_candidates(tmp_path / "paper.pdf", cache_paths=(cache_zip,))

    assert [candidate.latex for candidate in candidates] == ["a=b", "c=d"]


def test_mineru_cache_key_match_does_not_use_short_substring_fallback(tmp_path):
    from zotpilot.feature_extraction.formula_ocr import _cache_path_matches_keys

    path = tmp_path / "unrelated" / "content_list.json"

    assert not _cache_path_matches_keys(path, {"re"})
    assert _cache_path_matches_keys(path, {"unrelated"})


def test_mineru_json_payload_depth_guard_prevents_recursion_error():
    from zotpilot.feature_extraction import formula_ocr

    payload: dict[str, object] = {}
    current = payload
    for _ in range(1200):
        next_node: dict[str, object] = {}
        current["children"] = [next_node]
        current = next_node
    current["type"] = "equation"
    current["text"] = r"E = mc^2"

    candidates = formula_ocr._parse_mineru_json_payload(payload, source="mineru_content_list")

    assert candidates == []


def test_bounded_cache_scan_skips_symlink_escape(tmp_path):
    from zotpilot.feature_extraction.formula_ocr import _bounded_cache_scan

    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    outside_cache = outside / "content_list.json"
    outside_cache.write_text(json.dumps([{"type": "equation", "text": r"E = mc^2"}]), encoding="utf-8")
    symlink_path = root / "ITEM123" / "content_list.json"
    symlink_path.parent.mkdir()
    try:
        symlink_path.symlink_to(outside_cache)
    except OSError as exc:
        pytest.skip(f"symlink creation is not available: {exc}")

    found = _bounded_cache_scan(root, {"ITEM123"})

    assert found == []


def test_cached_latex_does_not_open_pdf_or_call_ocr_provider(tmp_path):
    class CachedCandidateProvider:
        name = "cached-test"

        def extract_candidates(self, *_args, **_kwargs):
            return [
                FormulaCandidate(
                    page_num=1,
                    bbox=(0, 0, 0, 0),
                    raw_text=r"E = mc^2",
                    confidence=0.95,
                    equation_number="(1)",
                    source="mineru_content_list",
                    latex=r"E = mc^2",
                )
            ]

    ocr_provider = MagicMock()
    ocr_provider.name = "simpletex"

    with patch("zotpilot.feature_extraction.formula_ocr.pymupdf.open") as open_pdf:
        formulas = recognize_formulas(
            tmp_path / "missing.pdf",
            ocr_provider,
            candidate_provider=CachedCandidateProvider(),
        )

    assert len(formulas) == 1
    assert formulas[0].provider == "cache"
    assert formulas[0].equation_number == "(1)"
    open_pdf.assert_not_called()
    ocr_provider.recognize.assert_not_called()


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


def test_extracted_formula_searchable_text_labels_unnumbered_formulas():
    formula = ExtractedFormula(
        page_num=4,
        formula_index=0,
        bbox=(0, 0, 10, 10),
        latex=r"x+y",
        equation_number_status="unnumbered",
    )

    assert formula.to_searchable_text().splitlines()[0] == (
        "Formula on page 4, index #1 (unnumbered in source)"
    )
