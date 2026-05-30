"""Formula OCR support for PDF formula chunks."""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import string
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pymupdf

from ..models import ExtractedFormula

logger = logging.getLogger(__name__)

SIMPLETEX_STANDARD_ENDPOINT = "https://server.simpletex.cn/api/latex_ocr"
SIMPLETEX_TURBO_ENDPOINT = "https://server.simpletex.cn/api/latex_ocr_turbo"

_MATH_SYMBOL_RE = re.compile(r"[=≈≃≅≤≥<>+\-*/^_∑∫√σσεηθγτλμΔΩΦΨαβ{}()[\]|¼ðþ\x00-\x06]")
_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_EQUATION_NUMBER_RE = re.compile(r"\(\s*\d+(?:\.\d+)?\s*\)")
_NON_FORMULA_TEXT_RE = re.compile(
    r"journal\s*homepage|doi\.org|received|accepted|elsevier|copyright|"
    r"all\s+rights\s+reserved|table\s+\d+|fig(?:ure)?\.?\s*\d+|"
    r"通讯作者|电子邮件地址",
    re.IGNORECASE,
)
_SECTION_HEADING_RE = re.compile(r"^\s*\d+(?:\.\d+)+\.?\s+[A-Za-z]")
_LATEX_MATH_COMMAND_RE = re.compile(
    r"\\(?:frac|dfrac|tfrac|sqrt|sum|int|prod|lim|overline|bar|dot|"
    r"sin|cos|tan|sec|arccos|arcsin|ln|log|exp|"
    r"varepsilon|epsilon|sigma|theta|vartheta|eta|tau|xi|lambda|mu|alpha|beta|gamma|delta|Delta)\b"
)
_LATEX_TEXT_WRAPPER_RE = re.compile(r"^\\(?:text|mathrm|operatorname)\{(?P<body>.*)\}$", re.DOTALL)
_LATEX_NOISE_RE = re.compile(
    r"journalhomepage|internationaljournal|elsevier|doi\.org|received|accepted|"
    r"allrightsreserved|creativecommons|www\.|http|homepage|通讯作者|电子邮件地址",
    re.IGNORECASE,
)
_LATEX_TABLE_HEADER_RE = re.compile(r"(?:\\text\{)?(?:[A-Z][a-z]?\s*\[[^\]]+\]\s*){4,}", re.IGNORECASE)


@dataclass(frozen=True)
class FormulaOCRResult:
    """Single formula OCR result."""
    latex: str
    confidence: float | None = None
    request_id: str = ""


@dataclass(frozen=True)
class FormulaCandidate:
    """Rendered formula candidate before OCR."""
    page_num: int
    bbox: tuple[float, float, float, float]
    raw_text: str = ""


class SimpleTexFormulaOCR:
    """Small SimpleTex Open Platform client for formula image OCR."""

    def __init__(
        self,
        *,
        token: str | None = None,
        app_id: str | None = None,
        app_secret: str | None = None,
        endpoint: str = SIMPLETEX_STANDARD_ENDPOINT,
        timeout: float = 60.0,
        min_confidence: float = 0.0,
        request_interval_seconds: float = 0.55,
    ) -> None:
        self.token = token
        self.app_id = app_id
        self.app_secret = app_secret
        self.endpoint = endpoint
        self.timeout = timeout
        self.min_confidence = min_confidence
        self.request_interval_seconds = request_interval_seconds
        self._last_request_at = 0.0
        if not token and not (app_id and app_secret):
            raise ValueError("SimpleTex formula OCR requires token or app_id/app_secret")

    def recognize(self, image_bytes: bytes, *, filename: str = "formula.png") -> FormulaOCRResult | None:
        """Recognize a rendered formula crop and return LaTeX if available."""
        data: dict[str, str] = {}
        headers = self._auth_headers(data)
        files = {"file": (filename, image_bytes, "image/png")}
        self._wait_for_rate_limit()
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(self.endpoint, headers=headers, data=data, files=files)
            response.raise_for_status()
        self._last_request_at = time.monotonic()
        payload = response.json()
        if not payload.get("status"):
            logger.debug("SimpleTex OCR failed: %s", payload)
            return None
        result = payload.get("res") or {}
        latex = (result.get("latex") or "").strip()
        if not latex or latex == "[EMPTY]":
            return None
        confidence = result.get("conf")
        if confidence is not None and float(confidence) < self.min_confidence:
            return None
        return FormulaOCRResult(
            latex=latex,
            confidence=float(confidence) if confidence is not None else None,
            request_id=payload.get("request_id", ""),
        )

    def _auth_headers(self, data: dict[str, str]) -> dict[str, str]:
        if self.token:
            return {"token": self.token}
        assert self.app_id is not None and self.app_secret is not None
        timestamp = str(int(time.time()))
        alphabet = string.ascii_letters + string.digits
        random_str = "".join(secrets.choice(alphabet) for _ in range(16))
        header = {
            "timestamp": timestamp,
            "random-str": random_str,
            "app-id": self.app_id,
        }
        sorted_keys = sorted(list(data.keys()) + list(header.keys()))
        pre_sign = "&".join(
            f"{key}={header[key] if key in header else data[key]}"
            for key in sorted_keys
        )
        pre_sign += f"&secret={self.app_secret}"
        header["sign"] = hashlib.md5(pre_sign.encode("utf-8")).hexdigest()
        return header

    def _wait_for_rate_limit(self) -> None:
        if self.request_interval_seconds <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.request_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)


def extract_formula_candidates(
    doc: pymupdf.Document,
    *,
    max_candidates: int | None = None,
) -> list[FormulaCandidate]:
    """Find conservative formula-like regions suitable for image OCR."""
    candidates: list[FormulaCandidate] = []
    collection_limit = max_candidates * 5 if max_candidates and max_candidates > 0 else None
    for page_index, page in enumerate(doc):
        page_num = page_index + 1
        page_rect = page.rect
        text_dict = page.get_text("dict")
        for block in text_dict.get("blocks", []):
            rect = pymupdf.Rect(block.get("bbox", (0, 0, 0, 0)))
            if not _looks_like_formula_region(rect, page_rect):
                continue
            if block.get("type") == 0:
                text = _block_text(block)
                if _looks_like_formula_text(text):
                    candidates.append(FormulaCandidate(page_num=page_num, bbox=tuple(rect), raw_text=text))
            if collection_limit is not None and len(candidates) >= collection_limit:
                return _rank_candidates(_dedupe_candidates(candidates))[:max_candidates]
    ranked = _rank_candidates(_dedupe_candidates(candidates))
    return ranked[:max_candidates] if max_candidates and max_candidates > 0 else ranked


def recognize_formulas(
    doc: pymupdf.Document,
    *,
    formula_ocr: SimpleTexFormulaOCR,
    max_formulas: int | None = None,
    images_dir: Path | None = None,
) -> list[ExtractedFormula]:
    """Detect formula candidates, OCR them, and return searchable formulas."""
    formulas: list[ExtractedFormula] = []
    candidates = extract_formula_candidates(doc, max_candidates=max_formulas)
    formula_dir = images_dir / "formulas" if images_dir else None
    if formula_dir is not None:
        formula_dir.mkdir(parents=True, exist_ok=True)

    for idx, candidate in enumerate(candidates):
        try:
            page = doc[candidate.page_num - 1]
            png = _render_crop(page, candidate.bbox)
            image_path = None
            if formula_dir is not None:
                image_path = formula_dir / f"p{candidate.page_num:04d}_formula_{idx:03d}.png"
                image_path.write_bytes(png)
            result = formula_ocr.recognize(png, filename=f"p{candidate.page_num}_formula_{idx}.png")
        except Exception as exc:
            logger.debug("Formula OCR failed on page %d candidate %d: %s", candidate.page_num, idx, exc)
            continue
        if result is None:
            continue
        if not is_high_quality_formula_latex(result.latex, raw_text=candidate.raw_text):
            logger.debug(
                "Rejected low-quality formula OCR result on page %d candidate %d: %s",
                candidate.page_num,
                idx,
                result.latex,
            )
            continue
        formulas.append(ExtractedFormula(
            page_num=candidate.page_num,
            formula_index=len(formulas),
            bbox=candidate.bbox,
            latex=result.latex,
            confidence=result.confidence,
            image_path=image_path,
            source="simpletex",
            raw_text=candidate.raw_text,
        ))
        if max_formulas and max_formulas > 0 and len(formulas) >= max_formulas:
            break
    return formulas


def is_high_quality_formula_latex(latex: str, *, raw_text: str = "") -> bool:
    """Return True when OCR output is likely a real mathematical formula."""
    compact = _normalize_latex_for_filter(latex)
    raw_compact = _normalize_latex_for_filter(raw_text)
    if not compact or compact == "[empty]":
        return False
    if len(compact) < 4:
        return False
    if _LATEX_NOISE_RE.search(compact) or _LATEX_NOISE_RE.search(raw_compact):
        return False
    if _LATEX_TABLE_HEADER_RE.search(latex):
        return False

    text_match = _LATEX_TEXT_WRAPPER_RE.match(latex.strip())
    if text_match and not _has_math_structure(text_match.group("body")):
        return False

    text_words = len(_WORD_RE.findall(_strip_latex_commands(latex)))
    structural_score = _latex_math_score(latex, include_equation_number=False)
    math_score = _latex_math_score(latex)
    text_block_count = latex.count(r"\text{")
    if structural_score < 2:
        return False
    if text_block_count >= 3 and "=" not in latex and r"\frac" not in latex:
        return False
    if text_block_count >= 2 and structural_score <= 2 and "=" not in latex:
        return False
    if r"\text{table" in latex.lower():
        return False
    if r"\begin{array}" in latex and text_block_count >= 2 and "=" not in latex:
        return False
    if r"\begin{array}" in latex and len(latex) > 400 and "=" not in latex:
        return False
    if re.fullmatch(r"\\left\(\d+(?:\.\d+)?\\right\)\^\d+", compact):
        return False
    if r"\text{where" in latex.lower() and text_words > 5 and "=" not in latex:
        return False
    if math_score < 2:
        return False
    if text_words > 14 and math_score < 5:
        return False
    return True


def _normalize_latex_for_filter(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _strip_latex_commands(text: str) -> str:
    return re.sub(r"\\[A-Za-z]+", " ", text or "")


def _has_math_structure(text: str) -> bool:
    return _latex_math_score(text, include_equation_number=False) >= 2


def _latex_math_score(latex: str, *, include_equation_number: bool = True) -> int:
    score = 0
    score += 3 if "=" in latex or "¼" in latex else 0
    score += 2 * len(_LATEX_MATH_COMMAND_RE.findall(latex))
    score += min(latex.count("_") + latex.count("^"), 6)
    score += min(len(re.findall(r"[σσεηθτξλμΔαβγδϑ]", latex)), 6)
    if include_equation_number and _EQUATION_NUMBER_RE.search(latex):
        score += 2
    if "\\begin{array}" in latex or "\\begin{aligned}" in latex:
        score += 2
    return score


def _block_text(block: dict) -> str:
    parts: list[str] = []
    for line in block.get("lines", []):
        line_text = "".join(span.get("text", "") for span in line.get("spans", []))
        if line_text.strip():
            parts.append(line_text.strip())
    return "\n".join(parts)


def _looks_like_formula_text(text: str) -> bool:
    stripped = " ".join(text.split())
    if not stripped or len(stripped) > 260:
        return False
    if _NON_FORMULA_TEXT_RE.search(stripped) or _SECTION_HEADING_RE.search(stripped):
        return False
    math_hits = len(_MATH_SYMBOL_RE.findall(stripped))
    word_hits = len(_WORD_RE.findall(stripped))
    has_equation_number = bool(_EQUATION_NUMBER_RE.search(stripped))
    if math_hits < 3 and not (has_equation_number and math_hits >= 2):
        return False
    if word_hits > 8 and math_hits < 6:
        return False
    cjk_hits = sum(1 for ch in stripped if "\u4e00" <= ch <= "\u9fff")
    if cjk_hits > 24 and math_hits < 8:
        return False
    return True


def _looks_like_formula_region(rect: pymupdf.Rect, page_rect: pymupdf.Rect) -> bool:
    width = rect.width
    height = rect.height
    if width < 40 or height < 6:
        return False
    if width > page_rect.width * 0.92 or height > page_rect.height * 0.18:
        return False
    if rect.y0 < page_rect.height * 0.05 or rect.y1 > page_rect.height * 0.96:
        return False
    aspect = width / max(height, 1)
    return aspect >= 1.5 or height <= 45


def _dedupe_candidates(candidates: list[FormulaCandidate]) -> list[FormulaCandidate]:
    kept: list[FormulaCandidate] = []
    for candidate in candidates:
        rect = pymupdf.Rect(candidate.bbox)
        duplicate = False
        for existing in kept:
            if existing.page_num != candidate.page_num:
                continue
            other = pymupdf.Rect(existing.bbox)
            inter = rect & other
            if inter.is_empty:
                continue
            smaller = min(rect.get_area(), other.get_area())
            if smaller > 0 and inter.get_area() / smaller > 0.75:
                duplicate = True
                break
        if not duplicate:
            kept.append(candidate)
    return kept


def _rank_candidates(candidates: list[FormulaCandidate]) -> list[FormulaCandidate]:
    return sorted(candidates, key=_candidate_score, reverse=True)


def _candidate_score(candidate: FormulaCandidate) -> float:
    text = " ".join(candidate.raw_text.split())
    math_hits = len(_MATH_SYMBOL_RE.findall(text))
    word_hits = len(_WORD_RE.findall(text))
    score = float(math_hits * 2)
    if _EQUATION_NUMBER_RE.search(text):
        score += 8
    if "=" in text or "¼" in text:
        score += 5
    score -= min(word_hits, 20) * 0.8
    score -= max(len(text) - 160, 0) * 0.03
    return score


def _render_crop(page: pymupdf.Page, bbox: tuple[float, float, float, float]) -> bytes:
    rect = pymupdf.Rect(bbox)
    rect.x0 -= 4
    rect.y0 -= 4
    rect.x1 += 4
    rect.y1 += 4
    rect &= page.rect
    matrix = pymupdf.Matrix(3, 3)
    pix = page.get_pixmap(matrix=matrix, clip=rect, alpha=False)
    return pix.tobytes("png")
