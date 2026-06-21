"""Formula OCR provider registry and text-layer display-equation detection.

Phase A intentionally works at PyMuPDF text-block granularity: candidate crops
cover the whole block bbox and target display equations in PDFs with a text
layer. Inline math, image/vector-only equations, and full-page fallback remain
out of scope for this local-first pass.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import re
import secrets
import string
import time
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol, cast

import httpx
import pymupdf

from .. import providers
from ..models import ExtractedFormula

logger = logging.getLogger(__name__)

MATH_FONT_HINTS = (
    "cambria math",
    "cmex",
    "cmmi",
    "cmr",
    "cmsy",
    "latinmodernmath",
    "mathjax",
    "stix",
    "symbol",
    "xits math",
)
MATH_SYMBOL_RE = re.compile(r"[=+\-*/<>≤≥≈≠∑∏∫√∞∂∇α-ωΑ-Ω_{}^]|\\[A-Za-z]+")
MATH_RELATION_RE = re.compile(r"(?:=|≤|≥|≈|≠|<|>|\\(?:leq?|geq?|approx|neq|equiv)\b)")
WORD_RE = re.compile(r"[A-Za-z]{3,}")
EQUATION_NUMBER_PATTERN = r"\d+(?:(?:\.|-)\d+)*(?:[A-Za-z])?"
EQUATION_NUMBER_RE = re.compile(
    rf"(?:\bEq\.?\s*\(\s*(?P<eq>{EQUATION_NUMBER_PATTERN})\s*\)|"
    rf"[=+\-*/<>≤≥≈≠∑∏∫√∞∂∇_{{}}^][^()\n]{{0,180}}\(\s*(?P<tail>{EQUATION_NUMBER_PATTERN})\s*\)\s*$)",
    re.IGNORECASE,
)
TRAILING_EQUATION_NUMBER_RE = re.compile(
    rf"[\(（]\s*(?P<number>{EQUATION_NUMBER_PATTERN})\s*[\)）]\s*$",
    re.IGNORECASE,
)
NOISE_RE = re.compile(
    r"\b(?:abstract|keywords|introduction|conclusion|conclusions|references|"
    r"acknowledg(?:e)?ments?|figure|fig\.?|table|tab\.?|copyright|doi|"
    r"received|accepted|available\s+online|corresponding\s+author|"
    r"supplementary|publisher|license|creative\s+commons)\b|"
    r"(?:摘要|关键词|引言|前言|结论|参考文献|致谢|图\s*\d+|表\s*\d+|通讯作者|版权)",
    re.IGNORECASE,
)
SECTION_HEADING_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.?\s*)?"
    r"(?:abstract|keywords|introduction|background|methods?|materials?|results?|discussion|"
    r"conclusions?|references|acknowledgements?|appendix|前言|摘要|关键词|引言|绪论|"
    r"方法|材料|结果|讨论|结论|参考文献|致谢)\s*$",
    re.IGNORECASE,
)
AUTHOR_AFFILIATION_RE = re.compile(
    r"(?:@|(?:university|institute|college|school|department|laboratory|academy)\b|"
    r"(?:^|[,;])\s*[A-Z][a-z]+,\s*[A-Z]\.)",
    re.IGNORECASE,
)
CAPTION_OR_REFERENCE_RE = re.compile(
    r"^\s*(?:(?:fig(?:ure)?|tab(?:le)?)\.?\s*\d+|[图表]\s*\d+)"
    r"|^\s*(?:\[\d+\]|\d+\.)\s+[A-Z][A-Za-z .,'-]{8,}"
    r"|(?:\b(?:journal|vol\.?|volume|issue|pp\.?|pages?|et\s+al\.|"
    r"springer|elsevier|wiley|mdpi|science\s+direct|crossref)\b)",
    re.IGNORECASE,
)
INLINE_CITATION_RE = re.compile(
    r"(?:\[[0-9,\-\s]{1,24}\]|\b[A-Z][A-Za-z-]+\s+et\s+al\.|\([A-Z][A-Za-z-]+,?\s+\d{4}\))",
    re.IGNORECASE,
)
PROSE_CUE_RE = re.compile(
    r"\b(?:is|are|was|were|be|been|being|calculated|defined|shown|used|obtained|given|"
    r"according|model|models|result|results|specimen|specimens|sample|samples|paper|study)\b|"
    r"(?:计算|得到|表示|定义|根据|其中|式中|试样|样品|模型|结果|研究|本文|如图|表明)",
    re.IGNORECASE,
)
VARIABLE_GLOSS_RE = re.compile(r"\bwhere\b[^.。;；]{0,260}|其中[^.。;；]{0,260}|式中[^.。;；]{0,260}", re.IGNORECASE)
CHINESE_PROSE_CUE_RE = re.compile(
    r"(?:可以发现|发现|显著|强化效应|动态力学性能|断裂准则|修正|铝合金|材料|试验|实验|"
    r"模拟|数值|有限元|本文|研究|结果|表明|影响|性能|应变率|温度)"
)
TABLE_HEADER_CUE_RE = re.compile(
    r"(?:试样类型|应力三轴度|断裂应变|材料参数|编号|组别|类型|Lode\s*角|"
    r"strain\s+rate|stress\s+triaxiality|fracture\s+strain)",
    re.IGNORECASE,
)
LATEX_TAG_RE = re.compile(r"\\tag\s*\{\s*(?P<tag>[^{}]+?)\s*\}")
DISPLAY_MATH_RE = re.compile(r"\$\$(?P<latex>.+?)\$\$|\\\[(?P<bracket>.+?)\\\]", re.DOTALL)
FENCED_MATH_RE = re.compile(r"```(?:math|latex)\s*(?P<latex>.+?)```", re.DOTALL | re.IGNORECASE)
MARKDOWN_PAGE_RE = re.compile(r"<!--\s*page\s*(?P<page>\d+)\s*-->", re.IGNORECASE)
FORMULA_CACHE_NAMES = {"content_list.json", "content_list_v2.json", "middle.json", "manifest.json", "full.md"}
MAX_FORMULA_CACHE_ZIP_MEMBERS = 128
MAX_FORMULA_CACHE_ZIP_MEMBER_SIZE_BYTES = 100 * 1024 * 1024
MAX_FORMULA_CACHE_JSON_DEPTH = 256
MIN_CACHE_KEY_SUBSTRING_LENGTH = 8
MINERU_JSON_CACHE_NAMES = {"content_list.json", "content_list_v2.json", "middle.json", "manifest.json"}
PDF_EXTRACT_KIT_CACHE_NAMES = {"formula_detection.json", "formula_recognition.json", "results.json"}
MAX_RECORD_FORMULA_LABEL_CHARS = 96
SIMPLETEX_RETRIABLE_STATUS_CODES = {429, 500, 502, 503, 504}
SIMPLETEX_MIN_RETRY_DELAY = 0.25
SIMPLETEX_MAX_RETRY_DELAY = 30.0


@dataclass(frozen=True)
class FormulaCandidate:
    """A text-layer formula candidate detected from PyMuPDF spans."""
    page_num: int
    bbox: tuple[float, float, float, float]
    raw_text: str
    confidence: float
    font_names: tuple[str, ...] = ()
    span_flags: tuple[int, ...] = ()
    reference_context: str = ""
    variable_gloss: str = ""
    equation_number: str = ""
    equation_number_status: str = ""
    source: str = "text_layer"
    latex: str = ""


@dataclass(frozen=True)
class FormulaOCRResult:
    """Provider-normalized OCR output."""
    latex: str
    confidence: float | None = None


class FormulaOCRProvider(Protocol):
    """Base protocol for formula OCR providers."""
    name: str

    def recognize(self, image_bytes: bytes) -> FormulaOCRResult:
        """Recognize LaTeX from a PNG crop."""


class FormulaCandidateProvider(Protocol):
    """Base protocol for formula candidate providers."""
    name: str

    def extract_candidates(
        self,
        pdf_path: Path | str,
        *,
        item_key: str | None = None,
        cache_paths: tuple[Path | str, ...] | None = None,
        max_formulas_per_doc: int = 40,
        max_formulas_per_page: int = 6,
        min_confidence: float = 0.6,
    ) -> list[FormulaCandidate]:
        """Return formula candidates for one PDF."""


class LocalFormulaOCRProvider:
    """RapidLaTeXOCR-backed local provider."""
    name = "local"

    def __init__(self) -> None:
        try:
            from rapid_latex_ocr import LaTeXOCR
        except ImportError as e:
            raise RuntimeError(
                "Formula OCR local provider requires the optional dependency "
                "`zotpilot[formula]` (rapid-latex-ocr>=0.0.9)."
            ) from e
        self._engine = LaTeXOCR()

    def recognize(self, image_bytes: bytes) -> FormulaOCRResult:
        raw = self._engine(image_bytes)
        return _coerce_provider_result(raw)


class SimpleTexFormulaOCRProvider:
    """SimpleTex Open Platform formula OCR provider."""
    name = "simpletex"

    def __init__(
        self,
        *,
        token: str | None = None,
        app_id: str | None = None,
        app_secret: str | None = None,
        endpoint: str = "https://server.simpletex.net/api/latex_ocr",
        timeout: float = 30.0,
        min_interval: float = 0.55,
        max_retries: int = 2,
    ) -> None:
        if not token and not (app_id and app_secret):
            raise RuntimeError(
                "SimpleTex formula OCR requires formula_ocr_simpletex_token "
                "or formula_ocr_simpletex_app_id + formula_ocr_simpletex_app_secret."
            )
        self._token = token
        self._app_id = app_id
        self._app_secret = app_secret
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._last_request_at: float | None = None

    def recognize(self, image_bytes: bytes) -> FormulaOCRResult:
        data: dict[str, str] = {}
        files = {"file": ("formula.png", image_bytes, "image/png")}
        with httpx.Client(timeout=self._timeout) as client:
            response = self._post_with_retries(client, data=data, files=files)
        try:
            payload = response.json()
        except ValueError as e:
            raise RuntimeError("SimpleTex response is not valid JSON") from e
        return _coerce_simpletex_response(payload)

    def _headers(self, data: dict[str, str]) -> dict[str, str]:
        if self._token:
            return {"token": self._token}
        if not self._app_id or not self._app_secret:
            raise RuntimeError("SimpleTex APP authentication requires app_id and app_secret")
        return _simpletex_app_headers(data, self._app_id, self._app_secret)

    def _post_with_retries(
        self,
        client: httpx.Client,
        *,
        data: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
    ) -> httpx.Response:
        max_retries = max(self._max_retries, 0)
        attempts = max_retries + 1
        for attempt in range(attempts):
            self._throttle()
            headers = self._headers(data)
            try:
                response = client.post(self._endpoint, headers=headers, data=data, files=files)
            except httpx.RequestError as e:
                if attempt >= max_retries:
                    raise RuntimeError("SimpleTex formula OCR exhausted retries after request error") from e
                time.sleep(_simpletex_retry_delay(None, attempt, self._min_interval))
                continue
            status_code = response.status_code
            if (
                isinstance(status_code, int)
                and status_code in SIMPLETEX_RETRIABLE_STATUS_CODES
            ):
                if attempt >= max_retries:
                    raise RuntimeError(f"SimpleTex formula OCR exhausted retries after HTTP {status_code}")
                time.sleep(_simpletex_retry_delay(response, attempt, self._min_interval))
                continue
            response.raise_for_status()
            return response
        raise RuntimeError("SimpleTex formula OCR exhausted retries")

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        now = time.monotonic()
        if self._last_request_at is not None:
            wait_seconds = self._min_interval - (now - self._last_request_at)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
        self._last_request_at = time.monotonic()


class TextLayerFormulaCandidateProvider:
    """Detect display-equation candidates from the PDF text layer."""
    name = "text_layer"

    def extract_candidates(
        self,
        pdf_path: Path | str,
        *,
        item_key: str | None = None,
        cache_paths: tuple[Path | str, ...] | None = None,
        max_formulas_per_doc: int = 40,
        max_formulas_per_page: int = 6,
        min_confidence: float = 0.6,
    ) -> list[FormulaCandidate]:
        del item_key, cache_paths
        return _extract_text_layer_formula_candidates(
            pdf_path,
            max_formulas_per_doc=max_formulas_per_doc,
            max_formulas_per_page=max_formulas_per_page,
            min_confidence=min_confidence,
        )


class MinerUCacheFormulaCandidateProvider:
    """Read cached formula candidates from llm-for-zotero/MinerU outputs."""
    name = "mineru_cache"

    def __init__(self, cache_dirs: tuple[str, ...] = ()) -> None:
        self._cache_dirs = tuple(Path(path).expanduser() for path in cache_dirs if path)

    def extract_candidates(
        self,
        pdf_path: Path | str,
        *,
        item_key: str | None = None,
        cache_paths: tuple[Path | str, ...] | None = None,
        max_formulas_per_doc: int = 40,
        max_formulas_per_page: int = 6,
        min_confidence: float = 0.6,
    ) -> list[FormulaCandidate]:
        del max_formulas_per_page, min_confidence
        candidates: list[FormulaCandidate] = []
        for cache_path in _candidate_cache_paths(
            pdf_path,
            item_key=item_key,
            cache_dirs=self._cache_dirs,
            cache_paths=cache_paths,
        ):
            if cache_path.suffix.lower() == ".zip":
                candidates.extend(_parse_mineru_zip_candidates(cache_path))
            elif cache_path.name.lower() == "full.md":
                candidates.extend(_parse_mineru_markdown_candidates(cache_path))
            else:
                candidates.extend(_parse_mineru_json_candidates(cache_path))
        candidates = _dedupe_candidates(candidates)
        cached = [candidate for candidate in candidates if candidate.latex.strip()]
        needs_ocr = [candidate for candidate in candidates if not candidate.latex.strip()]
        ordered = cached + needs_ocr
        if max_formulas_per_doc > 0:
            ordered = ordered[:max_formulas_per_doc]
        return ordered


class AutoFormulaCandidateProvider:
    """Prefer cached structured candidates, then fall back to the text layer."""
    name = "auto"

    def __init__(self, cache_dirs: tuple[str, ...] = ()) -> None:
        self._cache_provider = MinerUCacheFormulaCandidateProvider(cache_dirs=cache_dirs)
        self._text_provider = TextLayerFormulaCandidateProvider()

    def extract_candidates(
        self,
        pdf_path: Path | str,
        *,
        item_key: str | None = None,
        cache_paths: tuple[Path | str, ...] | None = None,
        max_formulas_per_doc: int = 40,
        max_formulas_per_page: int = 6,
        min_confidence: float = 0.6,
    ) -> list[FormulaCandidate]:
        cached = self._cache_provider.extract_candidates(
            pdf_path,
            item_key=item_key,
            cache_paths=cache_paths,
            max_formulas_per_doc=max_formulas_per_doc,
            max_formulas_per_page=max_formulas_per_page,
            min_confidence=min_confidence,
        )
        if cached:
            return cached
        return self._text_provider.extract_candidates(
            pdf_path,
            item_key=item_key,
            cache_paths=cache_paths,
            max_formulas_per_doc=max_formulas_per_doc,
            max_formulas_per_page=max_formulas_per_page,
            min_confidence=min_confidence,
        )


class MinerUJsonFormulaCandidateProvider:
    """Read candidates from explicit structured MinerU JSON output paths."""
    name = "mineru_json"

    def __init__(self, cache_dirs: tuple[str, ...] = ()) -> None:
        self._cache_dirs = tuple(Path(path).expanduser() for path in cache_dirs if path)

    def extract_candidates(
        self,
        pdf_path: Path | str,
        *,
        item_key: str | None = None,
        cache_paths: tuple[Path | str, ...] | None = None,
        max_formulas_per_doc: int = 40,
        max_formulas_per_page: int = 6,
        min_confidence: float = 0.6,
    ) -> list[FormulaCandidate]:
        del max_formulas_per_page, min_confidence
        candidates: list[FormulaCandidate] = []
        for cache_path in _mineru_json_candidate_cache_paths(
            pdf_path,
            item_key=item_key,
            cache_dirs=self._cache_dirs,
            cache_paths=cache_paths,
        ):
            candidates.extend(_parse_mineru_json_candidates(cache_path, allowed_cache_path=_is_mineru_json_cache_path))
        candidates = _dedupe_candidates(candidates)
        cached = [candidate for candidate in candidates if candidate.latex.strip()]
        needs_ocr = [candidate for candidate in candidates if not candidate.latex.strip()]
        ordered = cached + needs_ocr
        if max_formulas_per_doc > 0:
            ordered = ordered[:max_formulas_per_doc]
        return ordered


class PdfExtractKitJsonFormulaCandidateProvider:
    """Read formula candidates from PDF-Extract-Kit-style JSON exports."""
    name = "pdf_extract_kit_json"

    def __init__(self, cache_dirs: tuple[str, ...] = ()) -> None:
        self._cache_dirs = tuple(Path(path).expanduser() for path in cache_dirs if path)

    def extract_candidates(
        self,
        pdf_path: Path | str,
        *,
        item_key: str | None = None,
        cache_paths: tuple[Path | str, ...] | None = None,
        max_formulas_per_doc: int = 40,
        max_formulas_per_page: int = 6,
        min_confidence: float = 0.6,
    ) -> list[FormulaCandidate]:
        del max_formulas_per_page, min_confidence
        candidates: list[FormulaCandidate] = []
        for cache_path in _pdf_extract_kit_candidate_cache_paths(
            pdf_path,
            item_key=item_key,
            cache_dirs=self._cache_dirs,
            cache_paths=cache_paths,
        ):
            candidates.extend(_parse_mineru_json_candidates(cache_path))
        candidates = _dedupe_candidates(candidates)
        cached = [candidate for candidate in candidates if candidate.latex.strip()]
        needs_ocr = [candidate for candidate in candidates if not candidate.latex.strip()]
        ordered = cached + needs_ocr
        if max_formulas_per_doc > 0:
            ordered = ordered[:max_formulas_per_doc]
        return ordered


FORMULA_OCR_PROVIDERS: dict[str, type[LocalFormulaOCRProvider] | type[SimpleTexFormulaOCRProvider]] = {
    "local": LocalFormulaOCRProvider,
    "simpletex": SimpleTexFormulaOCRProvider,
}
FORMULA_CANDIDATE_PROVIDERS: dict[
    str,
    type[TextLayerFormulaCandidateProvider]
    | type[MinerUCacheFormulaCandidateProvider]
    | type[MinerUJsonFormulaCandidateProvider]
    | type[PdfExtractKitJsonFormulaCandidateProvider]
    | type[AutoFormulaCandidateProvider],
] = {
    "text_layer": TextLayerFormulaCandidateProvider,
    "mineru_cache": MinerUCacheFormulaCandidateProvider,
    "mineru_json": MinerUJsonFormulaCandidateProvider,
    "pdf_extract_kit_json": PdfExtractKitJsonFormulaCandidateProvider,
    "auto": AutoFormulaCandidateProvider,
}


def ensure_formula_ocr_provider_dependency(name: str) -> None:
    """Check lightweight provider availability without constructing the OCR model."""
    if name not in FORMULA_OCR_PROVIDERS:
        valid = ", ".join(sorted(FORMULA_OCR_PROVIDERS))
        raise ValueError(f"Unknown formula OCR provider {name!r}. Valid providers: {valid}")
    if name == "local" and importlib.util.find_spec("rapid_latex_ocr") is None:
        raise RuntimeError(
            "Formula OCR local provider requires the optional dependency "
            "`zotpilot[formula]` (rapid-latex-ocr>=0.0.9)."
        )


def create_formula_ocr_provider(name: str, *, config: Any | None = None) -> FormulaOCRProvider:
    """Create a formula OCR provider from the registry."""
    try:
        provider_cls = FORMULA_OCR_PROVIDERS[name]
    except KeyError as e:
        valid = ", ".join(sorted(FORMULA_OCR_PROVIDERS))
        raise ValueError(f"Unknown formula OCR provider {name!r}. Valid providers: {valid}") from e
    if name == "simpletex":
        return SimpleTexFormulaOCRProvider(
            token=providers._resolve_secret(
                getattr(config, "formula_ocr_simpletex_token", None),
                "ZOTPILOT_SIMPLETEX_TOKEN",
                "SIMPLETEX_UAT",
                "SIMPLETEX_TOKEN",
            ),
            app_id=providers._resolve_secret(
                getattr(config, "formula_ocr_simpletex_app_id", None),
                "ZOTPILOT_SIMPLETEX_APP_ID",
                "SIMPLETEX_APP_ID",
            ),
            app_secret=providers._resolve_secret(
                getattr(config, "formula_ocr_simpletex_app_secret", None),
                "ZOTPILOT_SIMPLETEX_APP_SECRET",
                "SIMPLETEX_APP_SECRET",
            ),
            endpoint=getattr(config, "formula_ocr_simpletex_endpoint", "https://server.simpletex.net/api/latex_ocr"),
            timeout=float(getattr(config, "formula_ocr_simpletex_timeout", 30.0)),
            min_interval=float(getattr(config, "formula_ocr_simpletex_min_interval", 0.55)),
            max_retries=int(getattr(config, "formula_ocr_simpletex_max_retries", 2)),
        )
    return provider_cls()


def create_formula_candidate_provider(name: str, *, config: Any | None = None) -> FormulaCandidateProvider:
    """Create a formula candidate provider from the registry."""
    if name not in FORMULA_CANDIDATE_PROVIDERS:
        valid = ", ".join(sorted(FORMULA_CANDIDATE_PROVIDERS))
        raise ValueError(f"Unknown formula candidate provider {name!r}. Valid providers: {valid}")
    cache_dirs = _candidate_cache_dirs_from_config(config)
    if name == "text_layer":
        return TextLayerFormulaCandidateProvider()
    if name == "mineru_cache":
        return MinerUCacheFormulaCandidateProvider(cache_dirs=cache_dirs)
    if name == "mineru_json":
        return MinerUJsonFormulaCandidateProvider(cache_dirs=cache_dirs)
    if name == "pdf_extract_kit_json":
        return PdfExtractKitJsonFormulaCandidateProvider(cache_dirs=cache_dirs)
    return AutoFormulaCandidateProvider(cache_dirs=cache_dirs)


def is_high_quality_formula_latex(latex: str) -> bool:
    """Return True for LaTeX that looks useful enough to index."""
    cleaned = latex.strip()
    if len(cleaned) < 3 or len(cleaned) > 1500:
        return False
    if NOISE_RE.search(cleaned):
        return False
    if len(set(cleaned)) <= 2:
        return False
    symbol_hits = len(MATH_SYMBOL_RE.findall(cleaned))
    has_latex_command = "\\" in cleaned
    has_assignment = any(op in cleaned for op in ("=", "\\approx", "\\le", "\\ge"))
    has_math_variable = bool(re.search(r"[A-Za-z][_^]|\b[A-Za-z]\b", cleaned))
    return symbol_hits >= 2 or has_latex_command or (has_assignment and has_math_variable)


def formula_candidate_needs_ocr(candidate: FormulaCandidate) -> bool:
    """Return true when a candidate has no reusable cached LaTeX."""
    return not is_high_quality_formula_latex(candidate.latex.strip())


def count_formula_provider_calls(candidates: list[FormulaCandidate]) -> int:
    """Count OCR provider calls needed for a candidate list.

    Cached LaTeX candidates are intentionally zero-cost. They should not open
    the PDF, call SimpleTex/local OCR, or count against a daily call budget.
    """
    return sum(1 for candidate in candidates if formula_candidate_needs_ocr(candidate))


def _looks_like_formula_provider_quota_error(e: Exception) -> bool:
    message = str(e).lower()
    return any(
        marker in message
        for marker in (
            "402",
            "429",
            "balance",
            "insufficient",
            "limit",
            "quota",
            "rate",
            "too many requests",
        )
    )


def extract_formula_candidates(
    pdf_path: Path | str,
    *,
    candidate_provider: str | FormulaCandidateProvider = "text_layer",
    item_key: str | None = None,
    cache_paths: tuple[Path | str, ...] | None = None,
    cache_dirs: tuple[str, ...] = (),
    max_formulas_per_doc: int = 40,
    max_formulas_per_page: int = 6,
    min_confidence: float = 0.6,
) -> list[FormulaCandidate]:
    """Detect formula candidates from the configured candidate source."""
    if isinstance(candidate_provider, str):
        provider = create_formula_candidate_provider(
            candidate_provider,
            config=type("_CandidateConfig", (), {"formula_candidate_cache_dirs": cache_dirs})(),
        )
    else:
        provider = candidate_provider
    return provider.extract_candidates(
        pdf_path,
        item_key=item_key,
        cache_paths=cache_paths,
        max_formulas_per_doc=max_formulas_per_doc,
        max_formulas_per_page=max_formulas_per_page,
        min_confidence=min_confidence,
    )


def _extract_text_layer_formula_candidates(
    pdf_path: Path | str,
    *,
    max_formulas_per_doc: int = 40,
    max_formulas_per_page: int = 6,
    min_confidence: float = 0.6,
) -> list[FormulaCandidate]:
    """Detect block-level text-layer display-equation candidates from a PDF.

    Crops are rendered from whole PyMuPDF text blocks. If a publisher combines
    prose and a display equation in one block, nearby prose may be included in
    the OCR crop; line-level segmentation is left for a later phase.
    """
    candidates: list[FormulaCandidate] = []
    with pymupdf.open(str(pdf_path)) as doc:
        for page_index, page in enumerate(doc):
            page_num = page_index + 1
            page_text = _normalize_space(page.get_text("text") or "")
            page_candidates: list[FormulaCandidate] = []
            standalone_numbers: list[tuple[tuple[float, float, float, float], str]] = []
            text_dict = page.get_text("dict") or {}
            for block in text_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                extracted = _extract_block_signals(block)
                if extracted is None:
                    continue
                raw_text, bbox, font_names, span_flags = extracted
                standalone_number = _extract_standalone_equation_number(raw_text)
                if standalone_number:
                    standalone_numbers.append((bbox, standalone_number))
                    continue
                confidence = _candidate_confidence(raw_text, bbox, font_names, span_flags)
                if confidence < min_confidence:
                    continue
                equation_number = _extract_equation_number(raw_text)
                page_candidates.append(
                    FormulaCandidate(
                        page_num=page_num,
                        bbox=bbox,
                        raw_text=raw_text,
                        confidence=confidence,
                        font_names=tuple(sorted(font_names)),
                        span_flags=tuple(sorted(span_flags)),
                        reference_context=_extract_reference_context(page_text, raw_text),
                        variable_gloss=_extract_variable_gloss(page_text, raw_text),
                        equation_number=equation_number,
                        equation_number_status="provided" if equation_number else "missing",
                    )
                )
            page_candidates = _attach_standalone_equation_numbers(page_candidates, standalone_numbers)
            page_candidates = _merge_multiline_formula_candidates(page_candidates)
            page_candidates.sort(key=lambda c: c.confidence, reverse=True)
            page_candidates = _dedupe_candidates(page_candidates)
            if max_formulas_per_page > 0:
                page_candidates = page_candidates[:max_formulas_per_page]
            candidates.extend(page_candidates)
            if max_formulas_per_doc > 0 and len(candidates) >= max_formulas_per_doc:
                return candidates[:max_formulas_per_doc]
    return candidates


def recognize_formulas(
    pdf_path: Path | str,
    provider: FormulaOCRProvider | None,
    *,
    candidates: list[FormulaCandidate] | None = None,
    candidate_provider: str | FormulaCandidateProvider = "text_layer",
    item_key: str | None = None,
    cache_paths: tuple[Path | str, ...] | None = None,
    cache_dirs: tuple[str, ...] = (),
    max_formulas_per_doc: int = 40,
    max_formulas_per_page: int = 6,
    min_confidence: float = 0.6,
) -> list[ExtractedFormula]:
    """Detect text-layer formula candidates and OCR them with the provider."""
    formulas: list[ExtractedFormula] = []
    if candidates is None:
        candidates = extract_formula_candidates(
            pdf_path,
            candidate_provider=candidate_provider,
            item_key=item_key,
            cache_paths=cache_paths,
            cache_dirs=cache_dirs,
            max_formulas_per_doc=max_formulas_per_doc,
            max_formulas_per_page=max_formulas_per_page,
            min_confidence=min_confidence,
        )
    if not candidates:
        return []

    remaining_candidates: list[FormulaCandidate] = []
    for candidate in candidates:
        cached_formula = _formula_from_cached_latex(candidate, formula_index=len(formulas))
        if cached_formula is not None:
            formulas.append(cached_formula)
        else:
            remaining_candidates.append(candidate)
    if not remaining_candidates:
        return formulas
    if provider is None:
        return formulas

    with pymupdf.open(str(pdf_path)) as doc:
        for candidate in remaining_candidates:
            page = doc[candidate.page_num - 1]
            crop = _render_crop(page, candidate.bbox)
            try:
                result = provider.recognize(crop)
            except Exception as e:
                if _looks_like_formula_provider_quota_error(e):
                    raise
                logger.warning(
                    "Formula OCR provider %s failed on page %d: %s",
                    getattr(provider, "name", "unknown"),
                    candidate.page_num,
                    type(e).__name__,
                )
                continue
            latex = result.latex.strip()
            if not is_high_quality_formula_latex(latex):
                continue
            if result.confidence is not None and result.confidence < min_confidence:
                continue
            formulas.append(
                ExtractedFormula(
                    page_num=candidate.page_num,
                    formula_index=len(formulas),
                    bbox=candidate.bbox,
                    latex=latex,
                    confidence=result.confidence if result.confidence is not None else candidate.confidence,
                    raw_text=candidate.raw_text,
                    reference_context=candidate.reference_context,
                    equation_number=candidate.equation_number,
                    equation_number_status=candidate.equation_number_status,
                    variable_gloss=candidate.variable_gloss,
                    source=candidate.source,
                    provider=getattr(provider, "name", "unknown"),
                )
            )
    return formulas


def _formula_from_cached_latex(candidate: FormulaCandidate, *, formula_index: int) -> ExtractedFormula | None:
    latex = candidate.latex.strip()
    if not latex or not is_high_quality_formula_latex(latex):
        return None
    return ExtractedFormula(
        page_num=candidate.page_num,
        formula_index=formula_index,
        bbox=candidate.bbox,
        latex=latex,
        confidence=candidate.confidence,
        raw_text=candidate.raw_text,
        reference_context=candidate.reference_context,
        equation_number=candidate.equation_number,
        equation_number_status=candidate.equation_number_status,
        variable_gloss=candidate.variable_gloss,
        source=candidate.source,
        provider="cache",
    )


def _coerce_provider_result(raw: Any) -> FormulaOCRResult:
    if isinstance(raw, FormulaOCRResult):
        return raw
    if isinstance(raw, dict):
        latex = str(raw.get("latex") or raw.get("text") or raw.get("result") or "")
        confidence = raw.get("confidence")
        return FormulaOCRResult(latex=latex, confidence=float(confidence) if confidence is not None else None)
    if isinstance(raw, (tuple, list)) and raw:
        latex = str(raw[0])
        # RapidLaTeXOCR returns (latex, elapsed_seconds), not a confidence score.
        return FormulaOCRResult(latex=latex)
    return FormulaOCRResult(latex=str(raw))


def _coerce_simpletex_response(payload: Any) -> FormulaOCRResult:
    if not isinstance(payload, dict):
        raise RuntimeError("SimpleTex response is not a JSON object")
    if payload.get("status") is not True:
        error = payload.get("err_info") or payload.get("error") or payload.get("msg") or payload
        raise RuntimeError(f"SimpleTex formula OCR failed: {error}")
    result = payload.get("res")
    if not isinstance(result, dict):
        raise RuntimeError("SimpleTex response missing result payload")
    latex = str(result.get("latex") or result.get("text") or result.get("result") or "")
    if not latex.strip():
        raise RuntimeError("SimpleTex response missing LaTeX")
    confidence = result.get("conf")
    if confidence is None:
        confidence = result.get("confidence")
    return FormulaOCRResult(latex=latex, confidence=float(confidence) if confidence is not None else None)


def _simpletex_app_headers(data: dict[str, str], app_id: str, app_secret: str) -> dict[str, str]:
    headers = {
        "timestamp": str(int(time.time())),
        "random-str": _random_simpletex_nonce(),
        "app-id": app_id,
    }
    sign_data = {**data, **headers}
    sign_body = "&".join(f"{key}={sign_data[key]}" for key in sorted(sign_data))
    sign_body += f"&secret={app_secret}"
    headers["sign"] = hashlib.md5(sign_body.encode("utf-8"), usedforsecurity=False).hexdigest()
    return headers


def _simpletex_retry_delay(response: httpx.Response | None, attempt: int, min_interval: float) -> float:
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            try:
                retry_after_seconds = float(str(retry_after))
                return min(max(retry_after_seconds, 0.0), SIMPLETEX_MAX_RETRY_DELAY)
            except ValueError:
                pass
    base_delay = max(float(min_interval), SIMPLETEX_MIN_RETRY_DELAY)
    return min(float(base_delay * (2**attempt)), SIMPLETEX_MAX_RETRY_DELAY)


def _random_simpletex_nonce(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _extract_block_signals(
    block: dict[str, Any],
) -> tuple[str, tuple[float, float, float, float], set[str], set[int]] | None:
    pieces: list[str] = []
    fonts: set[str] = set()
    flags: set[int] = set()
    for line in block.get("lines", []):
        line_parts: list[str] = []
        for span in line.get("spans", []):
            text = str(span.get("text", ""))
            if text:
                line_parts.append(text)
            font = span.get("font")
            if font:
                fonts.add(str(font))
            flag = span.get("flags")
            if isinstance(flag, int):
                flags.add(flag)
        if line_parts:
            pieces.append(" ".join(line_parts))
    raw_text = _normalize_space(" ".join(pieces))
    if not raw_text:
        return None
    bbox_raw = block.get("bbox", (0.0, 0.0, 0.0, 0.0))
    bbox = tuple(float(x) for x in bbox_raw[:4])
    if len(bbox) != 4:
        return None
    return raw_text, bbox, fonts, flags


def _candidate_cache_dirs_from_config(config: Any | None) -> tuple[str, ...]:
    if config is None:
        return ()
    raw_dirs = getattr(config, "formula_candidate_cache_dirs", ()) or ()
    if isinstance(raw_dirs, str):
        return tuple(part.strip() for part in raw_dirs.split(";") if part.strip())
    return tuple(str(path) for path in raw_dirs if str(path).strip())


def _candidate_cache_paths(
    pdf_path: Path | str,
    *,
    item_key: str | None,
    cache_dirs: tuple[Path, ...],
    cache_paths: tuple[Path | str, ...] | None,
) -> list[Path]:
    keys = _cache_lookup_keys(pdf_path, item_key=item_key)
    found: list[Path] = []
    if cache_paths:
        for raw_path in cache_paths:
            path = Path(raw_path).expanduser()
            if path.exists() and path.is_file() and _is_formula_cache_path(path):
                found.append(path)

    for root in cache_dirs:
        if root.is_file() and _is_formula_cache_path(root):
            if item_key is None or root.name.lower() in FORMULA_CACHE_NAMES or _cache_path_matches_keys(root, keys):
                found.append(root)
            continue
        if not root.is_dir():
            continue
        candidate_dirs = [root]
        for key in keys:
            direct = root / key
            if direct.is_dir() and _path_is_within_root(direct, root):
                candidate_dirs.append(direct)
        for directory in candidate_dirs:
            found.extend(_cache_paths_in_directory(directory, keys, root=root))
        if not found:
            found.extend(_bounded_cache_scan(root, keys))
    return _unique_paths(found)


def _mineru_json_candidate_cache_paths(
    pdf_path: Path | str,
    *,
    item_key: str | None,
    cache_dirs: tuple[Path, ...],
    cache_paths: tuple[Path | str, ...] | None,
) -> list[Path]:
    roots = [Path(path).expanduser() for path in cache_paths or ()]
    roots.extend(cache_dirs)
    keys = _cache_lookup_keys(pdf_path, item_key=item_key)
    found: list[Path] = []
    for root in roots:
        if root.is_file():
            if _is_mineru_json_cache_path(root):
                found.append(root)
            continue
        if not root.is_dir():
            continue
        root_found = _mineru_json_paths_in_directory(root, keys)
        if not root_found:
            root_found = _bounded_mineru_json_scan(root, keys)
        found.extend(root_found)
    return _unique_paths(found)


def _pdf_extract_kit_candidate_cache_paths(
    pdf_path: Path | str,
    *,
    item_key: str | None,
    cache_dirs: tuple[Path, ...],
    cache_paths: tuple[Path | str, ...] | None,
) -> list[Path]:
    roots = [Path(path).expanduser() for path in cache_paths or ()]
    roots.extend(cache_dirs)
    keys = _cache_lookup_keys(pdf_path, item_key=item_key)
    found: list[Path] = []
    for root in roots:
        if root.is_file():
            if _is_pdf_extract_kit_cache_path(root):
                found.append(root)
            continue
        if not root.is_dir():
            continue
        root_found = _pdf_extract_kit_paths_in_directory(root, keys)
        if not root_found:
            root_found = _bounded_pdf_extract_kit_scan(root, keys)
        found.extend(root_found)
    return _unique_paths(found)


def _cache_lookup_keys(pdf_path: Path | str, *, item_key: str | None) -> set[str]:
    path = Path(pdf_path)
    keys = {path.stem.lower(), path.parent.name.lower()}
    if item_key:
        keys.add(item_key.lower())
    return {key for key in keys if key}


def _cache_paths_in_directory(directory: Path, keys: set[str], *, root: Path) -> list[Path]:
    found: list[Path] = []
    try:
        children = list(directory.iterdir())
    except OSError:
        return found
    for path in children:
        if _is_safe_formula_cache_file(path, root=root):
            found.append(path)
    if found:
        return found
    for child in children:
        if (
            not child.is_dir()
            or not _path_is_within_root(child, root)
            or (keys and child.name.lower() not in keys)
        ):
            continue
        try:
            for path in child.iterdir():
                if _is_safe_formula_cache_file(path, root=root):
                    found.append(path)
        except OSError:
            continue
    return found


def _mineru_json_paths_in_directory(directory: Path, keys: set[str]) -> list[Path]:
    found: list[Path] = []
    try:
        children = list(directory.iterdir())
    except OSError:
        return found
    for path in children:
        if path.is_file() and _is_mineru_json_cache_path(path):
            found.append(path)
    if found:
        return found
    for child in children:
        if not child.is_dir() or (keys and child.name.lower() not in keys):
            continue
        try:
            for path in child.iterdir():
                if path.is_file() and _is_mineru_json_cache_path(path):
                    found.append(path)
        except OSError:
            continue
    return found


def _pdf_extract_kit_paths_in_directory(directory: Path, keys: set[str]) -> list[Path]:
    found: list[Path] = []
    try:
        children = list(directory.iterdir())
    except OSError:
        return found
    for path in children:
        if path.is_file() and _is_pdf_extract_kit_cache_path(path):
            found.append(path)
    if found:
        return found
    for child in children:
        if not child.is_dir() or (keys and child.name.lower() not in keys):
            continue
        try:
            for path in child.iterdir():
                if path.is_file() and _is_pdf_extract_kit_cache_path(path):
                    found.append(path)
        except OSError:
            continue
    return found


def _bounded_cache_scan(root: Path, keys: set[str], *, max_entries: int = 20000) -> list[Path]:
    found: list[Path] = []
    visited = 0
    for path in root.rglob("*"):
        visited += 1
        if visited > max_entries:
            break
        if _is_safe_formula_cache_file(path, root=root) and _cache_path_matches_keys(path, keys):
            found.append(path)
    return found


def _bounded_mineru_json_scan(root: Path, keys: set[str], *, max_entries: int = 20000) -> list[Path]:
    found: list[Path] = []
    visited = 0
    for path in root.rglob("*"):
        visited += 1
        if visited > max_entries:
            break
        if path.is_file() and _is_mineru_json_cache_path(path) and _cache_path_matches_keys(path, keys):
            found.append(path)
    return found


def _bounded_pdf_extract_kit_scan(root: Path, keys: set[str], *, max_entries: int = 20000) -> list[Path]:
    found: list[Path] = []
    visited = 0
    for path in root.rglob("*"):
        visited += 1
        if visited > max_entries:
            break
        if path.is_file() and _is_pdf_extract_kit_cache_path(path) and _cache_path_matches_keys(path, keys):
            found.append(path)
    return found


def _cache_path_matches_keys(path: Path, keys: set[str]) -> bool:
    if not keys:
        return True
    lower_parts = {part.lower() for part in path.parts}
    if keys & lower_parts:
        return True
    lower_path = path.as_posix().lower()
    return any(
        len(key) >= MIN_CACHE_KEY_SUBSTRING_LENGTH and key in lower_path
        for key in keys
    )


def _is_mineru_json_cache_path(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in MINERU_JSON_CACHE_NAMES
        or name.endswith("_content_list.json")
        or name.endswith("_content_list_v2.json")
        or name.endswith(".content_list.json")
        or name.endswith(".content_list_v2.json")
    )


def _is_pdf_extract_kit_cache_path(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in PDF_EXTRACT_KIT_CACHE_NAMES
        or name.endswith("_formula_detection.json")
        or name.endswith(".formula_detection.json")
        or name.endswith("_formula_recognition.json")
        or name.endswith(".formula_recognition.json")
        or name.endswith("_results.json")
        or name.endswith(".results.json")
    )


def _is_formula_cache_path(path: Path) -> bool:
    name = path.name.lower()
    if name in FORMULA_CACHE_NAMES:
        return True
    if name.endswith(".zip"):
        return any(token in name for token in ("mineru", "formula", "cache"))
    return (
        name.endswith("_content_list.json")
        or name.endswith("_content_list_v2.json")
        or name.endswith(".content_list.json")
        or name.endswith(".content_list_v2.json")
        or name.endswith("_formula_detection.json")
        or name.endswith("_formula_recognition.json")
        or name.endswith(".formula_detection.json")
        or name.endswith(".formula_recognition.json")
        or name.endswith("_formula_results.json")
        or name.endswith(".formula_results.json")
    )


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _is_safe_formula_cache_file(path: Path, *, root: Path) -> bool:
    try:
        return path.is_file() and _is_formula_cache_path(path) and _path_is_within_root(path, root)
    except OSError:
        return False


def _path_is_within_root(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _parse_mineru_json_candidates(
    path: Path,
    *,
    allowed_cache_path: Callable[[Path], bool] = _is_formula_cache_path,
) -> list[FormulaCandidate]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        candidates = _parse_mineru_json_payload(payload, source=_source_from_cache_path(path))
    except (OSError, ValueError) as e:
        logger.warning("Failed to read formula candidate cache %s: %s", path, e)
        return []
    except RecursionError as e:
        logger.warning("Formula candidate cache %s is too deeply nested: %s", path, e)
        return []
    if path.name.lower() == "manifest.json":
        for referenced_path in _manifest_referenced_cache_paths(
            payload,
            base_dir=path.parent,
            allowed_cache_path=allowed_cache_path,
        ):
            if referenced_path == path:
                continue
            if referenced_path.suffix.lower() == ".md":
                candidates.extend(_parse_mineru_markdown_candidates(referenced_path))
            else:
                candidates.extend(_parse_mineru_json_candidates(referenced_path, allowed_cache_path=allowed_cache_path))
    return candidates


def _parse_mineru_zip_candidates(path: Path) -> list[FormulaCandidate]:
    candidates: list[FormulaCandidate] = []
    try:
        with zipfile.ZipFile(path) as archive:
            members = sorted(
                (
                    info for info in archive.infolist()
                    if _is_safe_zip_formula_cache_member(info)
                ),
                key=lambda info: info.filename,
            )
            if len(members) > MAX_FORMULA_CACHE_ZIP_MEMBERS:
                logger.warning(
                    "Skipping formula candidate archive %s: %d cache member(s) exceeds limit %d",
                    path,
                    len(members),
                    MAX_FORMULA_CACHE_ZIP_MEMBERS,
                )
                return []
            for info in _select_zip_formula_cache_members(members):
                name = info.filename
                if info.file_size > MAX_FORMULA_CACHE_ZIP_MEMBER_SIZE_BYTES:
                    logger.warning(
                        "Skipping formula cache member %s in %s: uncompressed size %d exceeds limit %d",
                        name,
                        path,
                        info.file_size,
                        MAX_FORMULA_CACHE_ZIP_MEMBER_SIZE_BYTES,
                    )
                    continue
                try:
                    text = archive.read(name).decode("utf-8")
                except (KeyError, UnicodeDecodeError, OSError) as e:
                    logger.warning("Failed to read formula cache member %s in %s: %s", name, path, e)
                    continue
                source = _source_from_cache_path(Path(name))
                if name.lower().endswith(".md"):
                    candidates.extend(_parse_mineru_markdown_text(text, source=source))
                    continue
                try:
                    payload = json.loads(text)
                    candidates.extend(_parse_mineru_json_payload(payload, source=source))
                except ValueError as e:
                    logger.warning("Failed to parse formula cache member %s in %s: %s", name, path, e)
                    continue
                except RecursionError as e:
                    logger.warning("Formula cache member %s in %s is too deeply nested: %s", name, path, e)
                    continue
    except (OSError, zipfile.BadZipFile) as e:
        logger.warning("Failed to read formula candidate archive %s: %s", path, e)
    return candidates


def _is_safe_zip_formula_cache_member(info: zipfile.ZipInfo) -> bool:
    name = info.filename
    if name.endswith("/"):
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        return False
    if path.parts and ":" in path.parts[0]:
        return False
    return _is_formula_cache_path(Path(name))


def _select_zip_formula_cache_members(members: list[zipfile.ZipInfo]) -> list[zipfile.ZipInfo]:
    if not members:
        return []
    preferred = ("content_list.json", "content_list_v2.json", "middle.json", "full.md")
    for cache_name in preferred:
        matches = [member for member in members if Path(member.filename).name.lower() == cache_name]
        if matches:
            return matches
    return members


def _parse_mineru_json_payload(payload: Any, *, source: str) -> list[FormulaCandidate]:
    candidates: list[FormulaCandidate] = []
    try:
        records = _iter_formula_records(payload)
    except RecursionError:
        return candidates
    for record in records:
        candidate = _candidate_from_formula_record(record, source=source)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _manifest_referenced_cache_paths(
    payload: Any,
    *,
    base_dir: Path,
    allowed_cache_path: Callable[[Path], bool] = _is_formula_cache_path,
) -> list[Path]:
    paths: list[Path] = []
    try:
        base_root = base_dir.resolve()
    except OSError:
        return paths
    stack = [payload]
    while stack and len(paths) < 64:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(value.values())
            continue
        if isinstance(value, list):
            stack.extend(value)
            continue
        if not isinstance(value, str):
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        if not _path_is_within_root(candidate, base_root):
            continue
        if candidate.exists() and candidate.is_file() and allowed_cache_path(candidate):
            paths.append(candidate)
    return _unique_paths(paths)


def _parse_mineru_markdown_candidates(path: Path) -> list[FormulaCandidate]:
    try:
        markdown = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to read formula markdown cache %s: %s", path, e)
        return []
    return _parse_mineru_markdown_text(markdown, source=_source_from_cache_path(path))


def _parse_mineru_markdown_text(markdown: str, *, source: str) -> list[FormulaCandidate]:
    candidates: list[FormulaCandidate] = []
    matches = list(DISPLAY_MATH_RE.finditer(markdown)) + list(FENCED_MATH_RE.finditer(markdown))
    matches.sort(key=lambda match: match.start())
    for index, match in enumerate(matches):
        latex = _clean_candidate_latex(match.groupdict().get("latex") or match.groupdict().get("bracket") or "")
        if not is_high_quality_formula_latex(latex):
            continue
        equation_number = _candidate_equation_number({"text": latex}, latex)
        candidates.append(
            FormulaCandidate(
                page_num=_page_num_before_offset(markdown, match.start()),
                bbox=(0.0, float(index), 0.0, float(index)),
                raw_text=latex,
                confidence=0.9,
                equation_number=equation_number,
                equation_number_status="provided" if equation_number else "missing",
                source=source,
                latex=latex,
            )
        )
    return candidates


def _iter_formula_records(
    payload: Any,
    *,
    inherited_page_num: int | None = None,
    depth: int = 0,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if depth > MAX_FORMULA_CACHE_JSON_DEPTH:
        return records
    if isinstance(payload, list):
        for item in payload:
            records.extend(
                _iter_formula_records(
                    item,
                    inherited_page_num=inherited_page_num,
                    depth=depth + 1,
                )
            )
        return records
    if not isinstance(payload, dict):
        return records

    page_num = _record_page_num_or_none(payload) or inherited_page_num
    if _record_looks_like_formula(payload):
        record = dict(payload)
        if page_num is not None and _record_page_num_or_none(record) is None:
            record["page_num"] = page_num
        records.append(record)
    for value in payload.values():
        if isinstance(value, (dict, list)):
            records.extend(
                _iter_formula_records(
                    value,
                    inherited_page_num=page_num,
                    depth=depth + 1,
                )
            )
    return records


def _record_looks_like_formula(record: dict[str, Any]) -> bool:
    labels = [
        str(record.get(key) or "").lower()
        for key in ("type", "category", "role", "block_type", "cls_name", "layout_type", "label", "det_label")
    ]
    if any("equation" in label or "formula" in label for label in labels):
        return True
    if str(record.get("text_format") or "").lower() in {"latex", "math", "equation"}:
        return True
    return bool(
        record.get("latex")
        or record.get("latex_styled")
        or record.get("formula_text")
        or record.get("rec_formula")
        or record.get("math_content")
    )


def _candidate_from_formula_record(record: dict[str, Any], *, source: str) -> FormulaCandidate | None:
    latex = _extract_record_latex(record)
    if latex and not is_high_quality_formula_latex(latex):
        return None
    bbox = _record_bbox(record)
    if not latex and bbox == (0.0, 0.0, 0.0, 0.0):
        return None
    raw_text = latex or _record_formula_label(record)
    if not raw_text:
        return None
    equation_number = _candidate_equation_number(record, latex)
    return FormulaCandidate(
        page_num=_record_page_num(record),
        bbox=bbox,
        raw_text=raw_text,
        confidence=_record_confidence(record),
        equation_number=equation_number,
        equation_number_status="provided" if equation_number else "missing",
        source=source,
        latex=latex,
    )


def _extract_record_latex(record: dict[str, Any]) -> str:
    for key in ("latex", "latex_styled", "rec_formula", "formula_text", "math_content", "text", "content"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return _clean_candidate_latex(value)
    return ""


def _record_formula_label(record: dict[str, Any]) -> str:
    for key in ("label", "cls_name", "det_label", "type", "category", "role"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            cleaned = _normalize_space(value)
            if len(cleaned) > MAX_RECORD_FORMULA_LABEL_CHARS:
                return ""
            return cleaned
    return "formula"


def _clean_candidate_latex(value: str) -> str:
    cleaned = value.strip()
    cleaned = re.sub(r"^```(?:math|latex)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    if cleaned.startswith("$$") and cleaned.endswith("$$"):
        cleaned = cleaned[2:-2]
    if cleaned.startswith(r"\[") and cleaned.endswith(r"\]"):
        cleaned = cleaned[2:-2]
    return cleaned.strip()


def _candidate_equation_number(record: dict[str, Any], latex: str) -> str:
    for key in ("equation_number", "eq_number", "eq_no", "formula_number", "number"):
        value = record.get(key)
        if value not in (None, ""):
            normalized = _normalize_equation_number(str(value))
            if normalized:
                return normalized
    tag_match = LATEX_TAG_RE.search(latex)
    if tag_match:
        return _normalize_equation_number(tag_match.group("tag"))
    return _extract_equation_number(latex)


def _normalize_equation_number(value: str) -> str:
    cleaned = value.strip()
    cleaned = cleaned.strip("[]()（）")
    if not cleaned:
        return ""
    return f"({cleaned})"


def _record_page_num(record: dict[str, Any]) -> int:
    return _record_page_num_or_none(record) or 1


def _record_page_num_or_none(record: dict[str, Any]) -> int | None:
    for key in ("page_num", "page", "page_no"):
        value = record.get(key)
        if isinstance(value, (int, float)):
            return max(int(value), 1)
        if isinstance(value, str) and value.strip().isdigit():
            return max(int(value), 1)
    for key in ("page_idx", "page_id", "page_index"):
        value = record.get(key)
        if isinstance(value, (int, float)):
            return max(int(value) + 1, 1)
        if isinstance(value, str) and value.strip().isdigit():
            return max(int(value) + 1, 1)
    return None


def _record_bbox(record: dict[str, Any]) -> tuple[float, float, float, float]:
    for key in ("bbox", "layout_bbox"):
        bbox = _coerce_bbox(record.get(key))
        if bbox is not None:
            return bbox
    for key in ("points", "dt_boxes"):
        bbox = _coerce_points_bbox(record.get(key))
        if bbox is not None:
            return bbox
    return (0.0, 0.0, 0.0, 0.0)


def _coerce_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            coerced = tuple(float(x) for x in value[:4])
        except (TypeError, ValueError):
            return None
        if len(coerced) == 4:
            return cast(tuple[float, float, float, float], coerced)
    return None


def _coerce_points_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or not value:
        return None
    points = value
    if len(points) == 1 and isinstance(points[0], (list, tuple)):
        points = points[0]
    xs: list[float] = []
    ys: list[float] = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            xs.append(float(point[0]))
            ys.append(float(point[1]))
        except (TypeError, ValueError):
            continue
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _record_confidence(record: dict[str, Any]) -> float:
    for key in ("confidence", "conf", "score", "confidence_score"):
        value = record.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
    return 0.9


def _page_num_before_offset(markdown: str, offset: int) -> int:
    page_num = 1
    for match in MARKDOWN_PAGE_RE.finditer(markdown[:offset]):
        try:
            page_num = max(int(match.group("page")), 1)
        except ValueError:
            continue
    return page_num


def _source_from_cache_path(path: Path) -> str:
    name = path.name.lower()
    if "formula_detection" in name or "formula_recognition" in name or name == "results.json":
        return "pdf_extract_kit_json"
    if name == "full.md":
        return "mineru_markdown"
    if "middle" in name:
        return "mineru_middle_json"
    if "content_list" in name:
        return "mineru_content_list"
    if name == "manifest.json":
        return "mineru_manifest"
    return "mineru_cache"


def _candidate_confidence(
    text: str,
    bbox: tuple[float, float, float, float],
    font_names: set[str],
    span_flags: set[int],
) -> float:
    if len(text) < 4 or len(text) > 900:
        return 0.0
    if _is_likely_non_formula_text(text):
        return 0.0

    x0, y0, x1, y1 = bbox
    width = max(x1 - x0, 0.0)
    height = max(y1 - y0, 0.0)
    if width < 8 or height < 5:
        return 0.0

    score = 0.0
    symbol_hits = len(MATH_SYMBOL_RE.findall(text))
    word_hits = len(WORD_RE.findall(text))
    symbol_density = symbol_hits / max(len(text), 1)

    if symbol_hits >= 2:
        score += 0.25
    if symbol_density >= 0.04:
        score += 0.20
    if _has_math_font(font_names):
        score += 0.22
    if _has_math_span_flags(span_flags):
        score += 0.10
    if EQUATION_NUMBER_RE.search(text):
        score += 0.08
    if width > 80 and height < 120:
        score += 0.08
    if re.search(r"\b[A-Za-z]\s*[=<>≈]\s*", text):
        score += 0.15
    if word_hits > 14 and symbol_density < 0.08:
        score -= 0.25

    return max(0.0, min(score, 1.0))


def _is_likely_non_formula_text(text: str) -> bool:
    """Reject common paper metadata/prose blocks before they reach OCR."""
    normalized = _normalize_space(text)
    if not normalized:
        return True
    symbol_hits = len(MATH_SYMBOL_RE.findall(normalized))
    word_hits = len(WORD_RE.findall(normalized))
    has_relation = bool(MATH_RELATION_RE.search(normalized))
    has_equation_number = bool(TRAILING_EQUATION_NUMBER_RE.search(normalized))
    math_signal = symbol_hits >= 2 or has_relation
    if SECTION_HEADING_RE.fullmatch(normalized):
        return True
    if NOISE_RE.search(normalized) and not math_signal:
        return True
    if _looks_like_chinese_prose_noise(normalized, has_relation=has_relation):
        return True
    if _looks_like_table_header_noise(normalized):
        return True
    if _looks_like_numeric_table_row_noise(normalized):
        return True
    if VARIABLE_GLOSS_RE.search(normalized) and not has_relation:
        return True
    if AUTHOR_AFFILIATION_RE.search(normalized) and not math_signal:
        return True
    if CAPTION_OR_REFERENCE_RE.search(normalized) and not math_signal:
        return True
    if INLINE_CITATION_RE.search(normalized) and not math_signal:
        return True
    if _looks_like_explicit_doi_or_url(normalized):
        return True
    if _looks_like_bare_doi(normalized) and not math_signal:
        return True
    if has_equation_number and not math_signal and word_hits >= 4:
        return True
    if word_hits >= 18 and symbol_hits < 3:
        return True
    if word_hits >= 8 and PROSE_CUE_RE.search(normalized) and symbol_hits < 2:
        return True
    return False


def _looks_like_chinese_prose_noise(text: str, *, has_relation: bool) -> bool:
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    if cjk_count < 6:
        return False
    if has_relation and not re.search(r"[。；，,;]", text):
        return False
    if re.search(r"[。；，,;]", text) and CHINESE_PROSE_CUE_RE.search(text):
        return True
    if not has_relation and CHINESE_PROSE_CUE_RE.search(text):
        return True
    return False


def _looks_like_table_header_noise(text: str) -> bool:
    if not TABLE_HEADER_CUE_RE.search(text):
        return False
    cue_hits = len(TABLE_HEADER_CUE_RE.findall(text))
    return cue_hits >= 2 or re.search(r"(?:η|θ|ε|Lode)", text) is not None


def _looks_like_numeric_table_row_noise(text: str) -> bool:
    if "\\" in text:
        return False
    tokens = re.findall(r"[A-Za-z]+|[-+]?\d+(?:\.\d+)?|[=<>≤≥∞≈≠+\-*/^]", text)
    if len(tokens) < 5:
        return False
    numeric_tokens = re.findall(r"[-+]?\d+(?:\.\d+)?", text)
    alpha_tokens = re.findall(r"[A-Za-z]+", text)
    operator_tokens = re.findall(r"[+*/^]", text)
    if len(numeric_tokens) < 3:
        return False
    numeric_ratio = len(numeric_tokens) / max(len(tokens), 1)
    return numeric_ratio >= 0.45 and len(alpha_tokens) <= 2 and not operator_tokens


def _looks_like_explicit_doi_or_url(text: str) -> bool:
    return bool(re.search(r"(?:https?://|www\.|doi\s*:)", text, re.IGNORECASE))


def _looks_like_bare_doi(text: str) -> bool:
    return bool(re.search(r"10\.\d{4,9}/", text, re.IGNORECASE))


def _has_math_font(font_names: set[str]) -> bool:
    for font in font_names:
        normalized = font.replace("-", " ").replace("_", " ").lower()
        if any(hint in normalized for hint in MATH_FONT_HINTS):
            return True
    return False


def _has_math_span_flags(span_flags: set[int]) -> bool:
    # PyMuPDF flags commonly include superscript/italic/bold bits. Treat them as
    # a small boost only; many publishers use ordinary fonts for equations.
    return any(flag & 0b10011 for flag in span_flags)


def _dedupe_candidates(candidates: list[FormulaCandidate]) -> list[FormulaCandidate]:
    kept: list[FormulaCandidate] = []
    for candidate in candidates:
        if all(_bbox_iou(candidate.bbox, existing.bbox) < 0.7 for existing in kept):
            kept.append(candidate)
    return kept


def _attach_standalone_equation_numbers(
    candidates: list[FormulaCandidate],
    number_blocks: list[tuple[tuple[float, float, float, float], str]],
) -> list[FormulaCandidate]:
    """Attach right/left margin equation-number blocks to same-line formulas."""
    if not candidates or not number_blocks:
        return candidates
    used_number_indexes: set[int] = set()
    attached: list[FormulaCandidate] = []
    for candidate in candidates:
        if candidate.equation_number:
            attached.append(candidate)
            continue
        best_index: int | None = None
        best_score: float | None = None
        for index, (number_bbox, _number) in enumerate(number_blocks):
            if index in used_number_indexes:
                continue
            score = _standalone_equation_number_match_score(candidate.bbox, number_bbox)
            if score is None:
                continue
            if best_score is None or score < best_score:
                best_index = index
                best_score = score
        if best_index is None:
            attached.append(candidate)
            continue
        used_number_indexes.add(best_index)
        _bbox, number = number_blocks[best_index]
        attached.append(
            replace(
                candidate,
                equation_number=number,
                equation_number_status="provided",
            )
        )
    return attached


def _merge_multiline_formula_candidates(candidates: list[FormulaCandidate]) -> list[FormulaCandidate]:
    """Merge adjacent formula-line candidates that belong to one display equation."""
    if len(candidates) < 2:
        return candidates
    ordered = sorted(candidates, key=lambda c: (c.page_num, c.bbox[1], c.bbox[0]))
    merged: list[FormulaCandidate] = []
    current = ordered[0]
    for candidate in ordered[1:]:
        if _should_merge_multiline_formula_candidates(current, candidate):
            current = _merge_formula_candidate_pair(current, candidate)
        else:
            merged.append(current)
            current = candidate
    merged.append(current)
    return merged


def _should_merge_multiline_formula_candidates(first: FormulaCandidate, second: FormulaCandidate) -> bool:
    if first.page_num != second.page_num:
        return False
    if first.equation_number and second.equation_number:
        return False
    first_x0, first_y0, first_x1, first_y1 = first.bbox
    second_x0, second_y0, second_x1, second_y1 = second.bbox
    vertical_gap = second_y0 - first_y1
    max_height = max(first_y1 - first_y0, second_y1 - second_y0, 1.0)
    if vertical_gap < -max_height * 0.25 or vertical_gap > max(10.0, max_height * 0.75):
        return False
    overlap = min(first_x1, second_x1) - max(first_x0, second_x0)
    min_width = max(min(first_x1 - first_x0, second_x1 - second_x0), 1.0)
    if overlap / min_width < 0.35:
        return False
    if _is_likely_non_formula_text(first.raw_text) or _is_likely_non_formula_text(second.raw_text):
        return False
    return True


def _merge_formula_candidate_pair(first: FormulaCandidate, second: FormulaCandidate) -> FormulaCandidate:
    equation_number = first.equation_number or second.equation_number
    equation_number_status = (
        "provided"
        if equation_number
        else first.equation_number_status or second.equation_number_status or "missing"
    )
    return replace(
        first,
        bbox=_union_bbox(first.bbox, second.bbox),
        raw_text=_normalize_space(f"{first.raw_text} {second.raw_text}"),
        confidence=max(first.confidence, second.confidence),
        font_names=tuple(sorted(set(first.font_names) | set(second.font_names))),
        span_flags=tuple(sorted(set(first.span_flags) | set(second.span_flags))),
        reference_context=first.reference_context or second.reference_context,
        variable_gloss=first.variable_gloss or second.variable_gloss,
        equation_number=equation_number,
        equation_number_status=equation_number_status,
    )


def _standalone_equation_number_match_score(
    candidate_bbox: tuple[float, float, float, float],
    number_bbox: tuple[float, float, float, float],
) -> float | None:
    cx0, cy0, cx1, cy1 = candidate_bbox
    nx0, ny0, nx1, ny1 = number_bbox
    candidate_height = max(cy1 - cy0, 1.0)
    number_height = max(ny1 - ny0, 1.0)
    candidate_mid_y = (cy0 + cy1) / 2
    number_mid_y = (ny0 + ny1) / 2
    vertical_gap = abs(candidate_mid_y - number_mid_y)
    if vertical_gap > max(10.0, min(32.0, max(candidate_height, number_height) * 1.25)):
        return None
    if nx0 >= cx1:
        horizontal_gap = nx0 - cx1
        side_penalty = 0.0
    elif nx1 <= cx0:
        horizontal_gap = cx0 - nx1
        side_penalty = 24.0
    else:
        return None
    if horizontal_gap > 300:
        return None
    return vertical_gap * 4 + horizontal_gap + side_penalty


def _union_bbox(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def _bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    iw = max(0.0, ix1 - ix0)
    ih = max(0.0, iy1 - iy0)
    intersection = iw * ih
    if intersection <= 0:
        return 0.0
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def _render_crop(page: pymupdf.Page, bbox: tuple[float, float, float, float]) -> bytes:
    rect = pymupdf.Rect(bbox)
    rect = rect + (-3, -3, 3, 3)
    rect = rect & page.rect
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=rect, alpha=False)
    return cast(bytes, pixmap.tobytes("png"))


def _extract_reference_context(page_text: str, raw_text: str, *, max_chars: int = 520) -> str:
    page_text = _normalize_space(page_text)
    raw_text = _normalize_space(raw_text)
    if not page_text:
        return ""
    idx = page_text.find(raw_text)
    if idx < 0 and len(raw_text) > 80:
        idx = page_text.find(raw_text[:80])
    if idx < 0:
        return page_text[:max_chars]
    start = max(0, idx - max_chars // 2)
    end = min(len(page_text), idx + len(raw_text) + max_chars // 2)
    snippet = page_text[start:end].strip()
    return snippet[:max_chars]


def _extract_variable_gloss(page_text: str, raw_text: str) -> str:
    context = _extract_reference_context(page_text, raw_text, max_chars=700)
    match = VARIABLE_GLOSS_RE.search(context)
    return _normalize_space(match.group(0)) if match else ""


def _extract_equation_number(raw_text: str) -> str:
    match = EQUATION_NUMBER_RE.search(raw_text)
    if match is None:
        match = TRAILING_EQUATION_NUMBER_RE.search(raw_text)
        if match is None:
            return ""
        before_number = raw_text[:match.start()]
        if not (MATH_RELATION_RE.search(before_number) or len(MATH_SYMBOL_RE.findall(before_number)) >= 2):
            return ""
        number = match.group("number")
    else:
        number = match.group("eq") or match.group("tail")
    return f"({number})" if number else ""


def _extract_standalone_equation_number(raw_text: str) -> str:
    normalized = _normalize_space(raw_text)
    match = re.fullmatch(
        rf"[\(（]\s*(?P<number>{EQUATION_NUMBER_PATTERN})\s*[\)）]",
        normalized,
        re.IGNORECASE,
    )
    return f"({match.group('number')})" if match else ""


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
