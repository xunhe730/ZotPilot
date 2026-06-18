"""Formula OCR provider registry and text-layer display-equation detection.

Phase A intentionally works at PyMuPDF text-block granularity: candidate crops
cover the whole block bbox and target display equations in PDFs with a text
layer. Inline math, image/vector-only equations, and full-page fallback remain
out of scope for this local-first pass.
"""
from __future__ import annotations

import hashlib
import importlib.util
import logging
import re
import secrets
import string
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

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
WORD_RE = re.compile(r"[A-Za-z]{3,}")
EQUATION_NUMBER_RE = re.compile(
    r"(?:\bEq\.?\s*\((?P<eq>\d+(?:\.\d+)?)\)|"
    r"[=+\-*/<>≤≥≈≠∑∏∫√∞∂∇_{}^][^()\n]{0,160}\((?P<tail>\d+(?:\.\d+)?)\)\s*$)",
    re.IGNORECASE,
)
NOISE_RE = re.compile(r"\b(?:abstract|references|figure|table|copyright|doi|keywords)\b", re.IGNORECASE)
VARIABLE_GLOSS_RE = re.compile(r"\bwhere\b[^.。;；]{0,260}|其中[^.。;；]{0,260}|式中[^.。;；]{0,260}", re.IGNORECASE)
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


FORMULA_OCR_PROVIDERS: dict[str, type[LocalFormulaOCRProvider] | type[SimpleTexFormulaOCRProvider]] = {
    "local": LocalFormulaOCRProvider,
    "simpletex": SimpleTexFormulaOCRProvider,
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


def extract_formula_candidates(
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
            text_dict = page.get_text("dict") or {}
            for block in text_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                extracted = _extract_block_signals(block)
                if extracted is None:
                    continue
                raw_text, bbox, font_names, span_flags = extracted
                confidence = _candidate_confidence(raw_text, bbox, font_names, span_flags)
                if confidence < min_confidence:
                    continue
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
                        equation_number=_extract_equation_number(raw_text),
                    )
                )
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
    provider: FormulaOCRProvider,
    *,
    max_formulas_per_doc: int = 40,
    max_formulas_per_page: int = 6,
    min_confidence: float = 0.6,
) -> list[ExtractedFormula]:
    """Detect text-layer formula candidates and OCR them with the provider."""
    formulas: list[ExtractedFormula] = []
    candidates = extract_formula_candidates(
        pdf_path,
        max_formulas_per_doc=max_formulas_per_doc,
        max_formulas_per_page=max_formulas_per_page,
        min_confidence=min_confidence,
    )
    if not candidates:
        return []

    with pymupdf.open(str(pdf_path)) as doc:
        for candidate in candidates:
            page = doc[candidate.page_num - 1]
            crop = _render_crop(page, candidate.bbox)
            try:
                result = provider.recognize(crop)
            except Exception as e:
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
                    variable_gloss=candidate.variable_gloss,
                    source="text_block",
                    provider=getattr(provider, "name", "unknown"),
                )
            )
    return formulas


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


def _candidate_confidence(
    text: str,
    bbox: tuple[float, float, float, float],
    font_names: set[str],
    span_flags: set[int],
) -> float:
    if len(text) < 4 or len(text) > 900:
        return 0.0
    if NOISE_RE.search(text):
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
        return ""
    number = match.group("eq") or match.group("tail")
    return f"({number})" if number else ""


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
