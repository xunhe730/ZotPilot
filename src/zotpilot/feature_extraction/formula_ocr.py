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
import unicodedata
import zipfile
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
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
MATH_SYMBOL_RE = re.compile(r"[=¼þ+*/<>≤≥≈≠∑∏∫√∞∂∇∆−α-ωΑ-Ω_{}^]|\\[A-Za-z]+")
PRIVATE_USE_MATH_GLYPH_RE = re.compile(r"[\uf000-\uf8ff]")
PRIVATE_USE_RELATION_RE = r"[\uf03d\uf0a3\uf0b3]"
MATH_LATEX_COMMAND_RE = re.compile(
    r"\\(?:alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|vartheta|iota|kappa|"
    r"lambda|mu|nu|xi|pi|rho|sigma|tau|upsilon|phi|varphi|chi|psi|omega|Gamma|Delta|"
    r"Theta|Lambda|Xi|Pi|Sigma|Upsilon|Phi|Psi|Omega|frac|dfrac|tfrac|sqrt|sum|prod|"
    r"int|oint|partial|nabla|dot|ddot|bar|hat|tilde|widetilde|vec|pmb|mathbf|boldsymbol|"
    r"mathrm|mathit|mathsf|mathtt|mathbb|mathcal|mathscr|mathfrak|bm|cal|prime|quad|"
    r"Leftrightarrow|Rightarrow|Leftarrow|leqslant|geqslant|"
    r"lim|log|ln|exp|sin|cos|tan|cdot|times|left|right|"
    r"begin\{(?:equation|aligned|align|array|cases|matrix|pmatrix|bmatrix)\})(?![A-Za-z])"
)
TEXT_FORMATTING_COMMAND_RE = re.compile(
    r"\\(?:text|textbf|textit|textrm|textsf|mathrm|mathbf|mathit|mathsf|mathtt|"
    r"mathbb|mathcal|mathscr|mathfrak|bm|pmb|operatorname)\s*\{([^{}]*)\}"
)
PROSE_TEXT_COMMAND_RE = re.compile(
    r"\\(?:text|textbf|textit|textrm|textsf)\s*\{([^{}]*)\}"
)
WORD_RE = re.compile(r"[A-Za-z]{3,}")
CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
EQUATION_NUMBER_PATTERN = r"(?:[A-Za-z][.:]\s*)?\d+(?:(?:\.|-)\d+)*(?:[A-Za-z])?"
PDF_EQUATION_NUMBER_PATTERN = r"(?:[A-Za-z][.:]\s*)?\d+(?:(?:[.:]|[-–—－−])\d+)*(?:[-–—－−])?(?:[A-Za-z])?"
EQUATION_NUMBER_RE = re.compile(
    rf"(?:\bEq\.?\s*\(\s*(?P<eq>{EQUATION_NUMBER_PATTERN})\s*\)|"
    rf"[=+\-*/<>≤≥≈≠∑∏∫√∞∂∇_{{}}^][^()\n]{{0,180}}\(\s*(?P<tail>{EQUATION_NUMBER_PATTERN})\s*\)\s*$)",
    re.IGNORECASE,
)
TRAILING_EQUATION_NUMBER_RE = re.compile(
    rf"[\(（ð]\s*(?P<number>{PDF_EQUATION_NUMBER_PATTERN})\s*[\)）Þ]\s*$",
    re.IGNORECASE,
)
PDF_EQUATION_NUMBER_TOKEN_RE = re.compile(
    rf"[\(（ð]\s*(?P<number>{PDF_EQUATION_NUMBER_PATTERN})\s*[\)）Þ]",
    re.IGNORECASE,
)
LATEX_TAG_RE = re.compile(
    rf"\\tag\s*\{{\s*(?:[\(（ð\)）Þ]\s*)?(?:Eq\.?\s*)?"
    rf"(?P<tag>{EQUATION_NUMBER_PATTERN})(?:\s*[\)）Þ,，.;:：]*)?\s*\}}",
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
PROSE_SENTENCE_RE = re.compile(
    r"(?:[A-Za-z]{3,}|[\u4e00-\u9fff]{4,}).{0,140}[.!?。；;:：,，]"
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
FIGURE_PANEL_PREFIX_LATEX_RE = re.compile(
    r"^\s*\(\s*(?:\\(?:mathrm|text|textrm)\s*\{\s*)?[a-h](?:\s*\})?\s*\)",
    re.IGNORECASE,
)
OCR_ARTIFACT_LATEX_RE = re.compile(
    r"\\(?:tt|amalg|rfloor|lfloor|natural|sharp|flat|clubsuit|spadesuit|heartsuit|diamondsuit)\b|\\#"
)
STRUCTURED_TABLE_UNIT_RE = re.compile(
    r"(?:G\s*p\s*a|M\s*P\s*a|k\s*N|m\s*m|s\s*\^\s*\{\s*-\s*1\s*\}|"
    r"\^\s*\{\s*\\circ\s*\}\s*C|°\s*C|D\s*e\s*f\.|"
    r"\bA\s*l\b|\bS\s*i\b|\bF\s*e\b|\bC\s*u\b|\bM\s*n\b|\bM\s*g\b|\bC\s*r\b|\bZ\s*n\b)",
    re.IGNORECASE,
)
VARIABLE_GLOSS_RE = re.compile(r"\bwhere\b[^.。;；]{0,260}|其中[^.。;；]{0,260}|式中[^.。;；]{0,260}", re.IGNORECASE)
FORMULA_JSON_CACHE_NAMES = {
    "content_list.json",
    "content_list_v2.json",
    "manifest.json",
    "middle.json",
    "formula_detection.json",
    "formula_recognition.json",
    "formula_results.json",
    "formulas.json",
    "result.json",
    "results.json",
    "predictions.json",
}
MINERU_JSON_CACHE_NAMES = {"content_list.json", "content_list_v2.json", "manifest.json", "middle.json"}
PDF_EXTRACT_KIT_CACHE_NAMES = {"formula_detection.json", "formula_recognition.json", "results.json"}
MAX_FORMULA_CACHE_ZIP_MEMBERS = 128
MAX_FORMULA_CACHE_ZIP_MEMBER_SIZE_BYTES = 100 * 1024 * 1024
MAX_FORMULA_CACHE_JSON_DEPTH = 256
MIN_CACHE_KEY_SUBSTRING_LENGTH = 8
MAX_RECORD_FORMULA_LABEL_CHARS = 96
SIMPLETEX_RETRIABLE_STATUS_CODES = {429, 500, 502, 503, 504}
SIMPLETEX_MIN_RETRY_DELAY = 0.25
SIMPLETEX_MAX_RETRY_DELAY = 30.0
SIMPLETEX_STOP_STATUS_CODES = {401, 402, 429}
SIMPLETEX_STOP_ERROR_HINTS = (
    "401",
    "402",
    "429",
    "balance",
    "insufficient",
    "limit",
    "quota",
    "rate",
    "余额",
    "次数",
    "额度",
    "限流",
)
COMPACT_FORMULA_VARIABLE_PREFIX_STOPWORDS = {
    "case",
    "data",
    "error",
    "fig",
    "figure",
    "model",
    "result",
    "step",
    "table",
    "where",
}
COMPACT_FORMULA_VARIABLE_PREFIX_ALLOWLIST = {
    "eeq",
    "peq",
    "seq",
}
PDF_TEXT_FALLBACK_MAX_PAGES = 80
PDF_NUMBERING_AUDIT_MAX_EXTRA_PAGES = 10
FORMULA_TEXT_MATCH_FILL_THRESHOLD = 0.42
FORMULA_TEXT_MATCH_ORDERABLE_PAGE_THRESHOLD = 0.50
FORMULA_TEXT_MATCH_MARGIN = 0.06
GREEK_TEXT_REPLACEMENTS = {
    "α": "alpha",
    "β": "beta",
    "γ": "gamma",
    "δ": "delta",
    "ε": "epsilon",
    "η": "eta",
    "θ": "theta",
    "λ": "lambda",
    "μ": "mu",
    "ν": "nu",
    "ξ": "xi",
    "π": "pi",
    "ρ": "rho",
    "σ": "sigma",
    "τ": "tau",
    "φ": "phi",
    "χ": "chi",
    "ψ": "psi",
    "ω": "omega",
    "Δ": "delta",
    "∆": "delta",
    "Θ": "theta",
    "Σ": "sigma",
}
LATEX_MATCH_TOKEN_STOPWORDS = {
    "begin",
    "end",
    "array",
    "aligned",
    "align",
    "equation",
    "left",
    "right",
    "big",
    "bigg",
    "bigl",
    "bigr",
    "displaystyle",
    "textstyle",
    "mathrm",
    "mathbf",
    "mathit",
    "pmb",
    "boldsymbol",
    "operatorname",
    "text",
    "quad",
    "qquad",
    "where",
    "and",
    "the",
    "for",
}
LATEX_MATCH_TOKEN_ALIASES = {
    "varepsilon": "epsilon",
    "leq": "<=",
    "leqslant": "<=",
    "le": "<=",
    "geq": ">=",
    "geqslant": ">=",
    "ge": ">=",
}
LATEX_MATCH_GREEK_WORDS = {
    "alpha",
    "beta",
    "gamma",
    "delta",
    "epsilon",
    "eta",
    "theta",
    "lambda",
    "mu",
    "nu",
    "xi",
    "pi",
    "rho",
    "sigma",
    "tau",
    "phi",
    "chi",
    "psi",
    "omega",
}


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
    bbox_coordinate_space: str = "pdf"
    latex: str = ""


@dataclass(frozen=True)
class _PdfEquationNumberRecord:
    number: str
    y_center: float
    x_right: float
    standalone: bool
    bbox: tuple[float, float, float, float]
    text: str
    page_width: float
    page_height: float


@dataclass(frozen=True)
class _PdfEquationNumberScanResult:
    records_by_page: dict[int, list[_PdfEquationNumberRecord]]
    truncated: bool = False


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
    """Base protocol for formula candidate detectors."""
    name: str

    def extract_candidates(
        self,
        pdf_path: Path | str,
        *,
        item_key: str | None = None,
        cache_paths: tuple[Path | str, ...] | None = None,
        max_formulas_per_doc: int = 40,
        max_formulas_per_page: int = 6,
        max_candidates_per_doc: int = 0,
        min_confidence: float = 0.6,
        pdf_fallback_max_pages: int | None = None,
    ) -> list[FormulaCandidate]:
        """Return candidate formula regions before OCR recognition."""


class TextLayerFormulaCandidateProvider:
    """PyMuPDF text-layer display-equation candidate detector."""
    name = "text_layer"

    def extract_candidates(
        self,
        pdf_path: Path | str,
        *,
        item_key: str | None = None,
        cache_paths: tuple[Path | str, ...] | None = None,
        max_formulas_per_doc: int = 40,
        max_formulas_per_page: int = 6,
        max_candidates_per_doc: int = 0,
        min_confidence: float = 0.6,
        pdf_fallback_max_pages: int | None = None,
    ) -> list[FormulaCandidate]:
        return _extract_text_layer_formula_candidates(
            pdf_path,
            max_formulas_per_doc=max_formulas_per_doc,
            max_formulas_per_page=max_formulas_per_page,
            max_candidates_per_doc=max_candidates_per_doc,
            min_confidence=min_confidence,
        )


class MinerUCacheFormulaCandidateProvider:
    """Read formula candidates from MinerU-style local JSON/Markdown caches.

    The adapter is intentionally dependency-free. It accepts cache directories
    produced by tools such as llm-for-zotero/MinerU, and extracts formula blocks
    from ``content_list.json``, ``manifest.json``, or block-level ``full.md``.
    """
    name = "mineru_cache"

    def __init__(
        self,
        cache_dirs: tuple[str, ...] = (),
        *,
        pdf_number_enrichment: bool = False,
        append_missing_pdf_candidates: bool = False,
    ) -> None:
        self._cache_dirs = tuple(Path(path).expanduser() for path in cache_dirs if path)
        self._cache_index_by_root: dict[Path, list[Path]] = {}
        self._pdf_number_enrichment = pdf_number_enrichment
        self._append_missing_pdf_candidates = append_missing_pdf_candidates

    def _is_cache_path(self, path: Path) -> bool:
        return _is_formula_cache_path(path)

    def extract_candidates(
        self,
        pdf_path: Path | str,
        *,
        item_key: str | None = None,
        cache_paths: tuple[Path | str, ...] | None = None,
        max_formulas_per_doc: int = 40,
        max_formulas_per_page: int = 6,
        max_candidates_per_doc: int = 0,
        min_confidence: float = 0.6,
        pdf_fallback_max_pages: int | None = None,
    ) -> list[FormulaCandidate]:
        candidates: list[FormulaCandidate] = []
        for cache_path in self._candidate_cache_paths(
            pdf_path,
            item_key=item_key,
            explicit_cache_paths=cache_paths,
        ):
            if cache_path.suffix.lower() == ".zip":
                candidates.extend(_parse_mineru_zip_candidates(cache_path))
            elif cache_path.suffix.lower() == ".md":
                candidates.extend(_parse_mineru_markdown_candidates(cache_path))
            else:
                candidates.extend(_parse_mineru_json_candidates(cache_path, allowed_cache_file=self._is_cache_path))
        candidates = [
            candidate for candidate in candidates
            if candidate.confidence >= min_confidence and _candidate_has_usable_payload(candidate)
        ]
        candidates.sort(key=lambda c: (c.page_num, c.bbox[1], c.bbox[0], -c.confidence))
        candidates = _dedupe_candidates(candidates)
        candidates = _split_multirow_independent_formula_candidates(candidates)
        candidates = _merge_split_formula_candidates(candidates)
        candidates = _limit_ocr_needed_candidates(
            candidates,
            max_formulas_per_page=max_formulas_per_page,
            max_formulas_per_doc=_ocr_doc_limit_for_candidate_discovery(
                max_formulas_per_doc=max_formulas_per_doc,
                max_candidates_per_doc=max_candidates_per_doc,
            ),
        )
        if max_candidates_per_doc > 0 and len(candidates) > max_candidates_per_doc:
            return _sort_formula_candidates_for_review(candidates[:max_candidates_per_doc])
        if candidates and not self._pdf_number_enrichment:
            candidates = _infer_single_leading_missing_equation_number(candidates)
            candidates = _infer_missing_equation_numbers_between_numbered(candidates)
            candidates = [
                candidate if candidate.equation_number_status else replace(
                    candidate,
                    equation_number_status="provided" if candidate.equation_number else "missing",
                )
                for candidate in candidates
            ]
            return _sort_formula_candidates_for_review(candidates)
        number_scan = _scan_pdf_equation_numbers_for_candidates(
            pdf_path,
            candidates,
            pdf_fallback_max_pages=pdf_fallback_max_pages,
        )
        complete_cached_before_pdf_enrichment = bool(candidates) and all(
            candidate.latex.strip() and candidate.equation_number
            for candidate in candidates
        )
        candidates = _enrich_candidate_equation_numbers_from_pdf(
            pdf_path,
            candidates,
            records_by_page=number_scan.records_by_page if number_scan is not None else None,
        )
        candidates = _merge_split_formula_candidates(candidates)
        candidates = _infer_single_leading_missing_equation_number(candidates)
        candidates = _infer_missing_equation_numbers_between_numbered(candidates)
        if candidates and not self._append_missing_pdf_candidates:
            candidates = _assign_equation_number_statuses_from_pdf(
                pdf_path,
                candidates,
                records_by_page=number_scan.records_by_page if number_scan is not None else None,
                scan_ok=number_scan is not None,
            )
            return _sort_formula_candidates_for_review(candidates)
        missing_cached_numbers = _missing_regular_equation_numbers(candidates)
        if complete_cached_before_pdf_enrichment and not missing_cached_numbers:
            candidates = _assign_equation_number_statuses_from_pdf(
                pdf_path,
                candidates,
                records_by_page=number_scan.records_by_page if number_scan is not None else None,
                scan_ok=number_scan is not None,
            )
            return _sort_formula_candidates_for_review(candidates)
        fallback_max_pages = _pdf_numbering_audit_max_pages(
            candidates,
            pdf_fallback_max_pages=pdf_fallback_max_pages,
        )
        fallback_max_records = (
            max_candidates_per_doc
            if not candidates and max_candidates_per_doc > 0
            else max_formulas_per_doc
            if not candidates and max_formulas_per_doc > 0
            else None
        )
        if number_scan is None:
            number_scan = _scan_pdf_equation_numbers_from_path(
                pdf_path,
                max_records=fallback_max_records,
                max_pages=fallback_max_pages,
            )
        candidates = _append_missing_pdf_numbered_formula_candidates(
            pdf_path,
            candidates,
            allow_empty=True,
            max_records=fallback_max_records,
            max_pages=fallback_max_pages,
            target_equation_numbers=missing_cached_numbers if complete_cached_before_pdf_enrichment else None,
            scan_result=number_scan,
        )
        candidates = _merge_number_only_pdf_candidates_with_latex_candidates(candidates)
        candidates = _dedupe_candidates(candidates)
        candidates = _infer_missing_equation_numbers_between_numbered(candidates)
        candidates = _merge_same_number_split_formula_candidates(candidates)
        candidates = _limit_ocr_needed_candidates(
            candidates,
            max_formulas_per_page=max_formulas_per_page,
            max_formulas_per_doc=_ocr_doc_limit_for_candidate_discovery(
                max_formulas_per_doc=max_formulas_per_doc,
                max_candidates_per_doc=max_candidates_per_doc,
            ),
        )
        if max_candidates_per_doc > 0 and len(candidates) > max_candidates_per_doc:
            return _sort_formula_candidates_for_review(candidates[:max_candidates_per_doc])
        candidates = _assign_equation_number_statuses_from_pdf(
            pdf_path,
            candidates,
            records_by_page=number_scan.records_by_page if number_scan is not None else None,
            scan_ok=number_scan is not None,
        )
        return _sort_formula_candidates_for_review(candidates)

    def _candidate_cache_paths(
        self,
        pdf_path: Path | str,
        *,
        item_key: str | None,
        explicit_cache_paths: tuple[Path | str, ...] | None = None,
    ) -> list[Path]:
        explicit_paths = [
            path for path in _explicit_formula_cache_paths(explicit_cache_paths)
            if self._is_cache_path(path)
        ]
        if explicit_paths:
            return explicit_paths
        pdf = Path(pdf_path)
        keys = {pdf.stem.lower(), pdf.name.lower()}
        if _looks_like_zotero_key(pdf.parent.name):
            keys.add(pdf.parent.name.lower())
        if item_key:
            keys.add(item_key.lower())
        found: list[Path] = []
        for root in self._cache_dirs:
            if root.is_file() and self._is_cache_path(root):
                if item_key is None or _cache_path_matches_keys(root, keys):
                    found.append(root)
                continue
            if not root.exists() or not root.is_dir():
                continue
            root_found: list[Path] = []
            if _looks_like_zotero_storage_root(root) and _is_direct_child_path(pdf.parent, root):
                root_found.extend(
                    _cache_paths_in_directory(
                        pdf.parent,
                        keys,
                        require_key_match=False,
                        cache_path_filter=self._is_cache_path,
                    )
                )
                if root_found:
                    found.extend(root_found)
                    continue
            direct_dirs = [root]
            if not _looks_like_zotero_storage_root(root):
                try:
                    direct_dirs.extend(
                        child for child in root.iterdir()
                        if child.is_dir() and child.name.lower() in keys
                    )
                except OSError as e:
                    logger.warning("Failed to inspect formula candidate cache dir %s: %s", root, e)
                    continue
            for directory in direct_dirs:
                if not directory.exists():
                    continue
                for path in directory.iterdir():
                    if self._is_cache_path(path):
                        is_root_level = directory == root
                        if not is_root_level or item_key is None or _cache_path_matches_keys(path, keys):
                            root_found.append(path)
            if not root_found or not any(_cache_path_matches_keys(path, keys) for path in root_found):
                if _looks_like_zotero_storage_root(root):
                    root_found.extend(_zotero_storage_cache_scan(root, keys, cache_path_filter=self._is_cache_path))
                else:
                    root_found.extend(self._indexed_cache_scan(root, keys))
            found.extend(root_found)
        return _unique_paths(found)

    def _indexed_cache_scan(self, root: Path, keys: set[str]) -> list[Path]:
        try:
            resolved_root = root.resolve()
        except OSError:
            resolved_root = root
        cached_paths = self._cache_index_by_root.get(resolved_root)
        if cached_paths is None:
            cached_paths = _bounded_cache_scan(root, set(), cache_path_filter=self._is_cache_path)
            self._cache_index_by_root[resolved_root] = cached_paths
        return [path for path in cached_paths if _cache_path_matches_keys(path, keys)]


class AutoFormulaCandidateProvider:
    """Prefer structured caches/PDF equation anchors before text-layer fallback."""

    name = "auto"
    _UNNUMBERED_TEXT_LAYER_NOISE_THRESHOLD = 50

    def __init__(
        self,
        cache_dirs: tuple[str, ...] = (),
        *,
        pdf_number_enrichment: bool = False,
        append_missing_pdf_candidates: bool = False,
    ) -> None:
        self._structured_provider = MinerUCacheFormulaCandidateProvider(
            cache_dirs=cache_dirs,
            pdf_number_enrichment=pdf_number_enrichment,
            append_missing_pdf_candidates=append_missing_pdf_candidates,
        )
        self._text_provider = TextLayerFormulaCandidateProvider()

    def extract_candidates(
        self,
        pdf_path: Path | str,
        *,
        item_key: str | None = None,
        cache_paths: tuple[Path | str, ...] | None = None,
        max_formulas_per_doc: int = 40,
        max_formulas_per_page: int = 6,
        max_candidates_per_doc: int = 0,
        min_confidence: float = 0.6,
        pdf_fallback_max_pages: int | None = None,
    ) -> list[FormulaCandidate]:
        structured_candidates = self._structured_provider.extract_candidates(
            pdf_path,
            item_key=item_key,
            cache_paths=cache_paths,
            max_formulas_per_doc=max_formulas_per_doc,
            max_formulas_per_page=max_formulas_per_page,
            max_candidates_per_doc=max_candidates_per_doc,
            min_confidence=min_confidence,
            pdf_fallback_max_pages=pdf_fallback_max_pages,
        )
        if _structured_candidates_complete_for_auto(structured_candidates):
            return structured_candidates
        text_candidates = self._text_provider.extract_candidates(
            pdf_path,
            item_key=item_key,
            cache_paths=cache_paths,
            max_formulas_per_doc=max_formulas_per_doc,
            max_formulas_per_page=max_formulas_per_page,
            max_candidates_per_doc=max_candidates_per_doc,
            min_confidence=min_confidence,
            pdf_fallback_max_pages=pdf_fallback_max_pages,
        )
        if not structured_candidates:
            if _looks_like_high_density_unnumbered_text_layer_noise(text_candidates):
                full_structured_candidates = self._structured_provider.extract_candidates(
                    pdf_path,
                    item_key=item_key,
                    cache_paths=cache_paths,
                    max_formulas_per_doc=max_formulas_per_doc,
                    max_formulas_per_page=max_formulas_per_page,
                    max_candidates_per_doc=max_candidates_per_doc,
                    min_confidence=min_confidence,
                    pdf_fallback_max_pages=0,
                )
                if full_structured_candidates:
                    return full_structured_candidates
                return [
                    candidate for candidate in text_candidates
                    if _text_layer_candidate_safe_for_auto_merge(candidate)
                ]
            return text_candidates
        text_candidates = [
            candidate for candidate in text_candidates
            if _text_layer_candidate_safe_for_auto_merge(candidate)
        ]
        if not text_candidates:
            return structured_candidates
        combined = _dedupe_candidates([*structured_candidates, *text_candidates])
        combined = _merge_number_only_pdf_candidates_with_latex_candidates(combined)
        combined = _infer_missing_equation_numbers_between_numbered(combined)
        combined = _merge_same_number_split_formula_candidates(combined)
        if max_candidates_per_doc > 0 and len(combined) > max_candidates_per_doc:
            combined = combined[:max_candidates_per_doc]
        return _sort_formula_candidates_for_review(combined)


def _looks_like_high_density_unnumbered_text_layer_noise(candidates: list[FormulaCandidate]) -> bool:
    if len(candidates) < AutoFormulaCandidateProvider._UNNUMBERED_TEXT_LAYER_NOISE_THRESHOLD:
        return False
    text_layer_count = sum(1 for candidate in candidates if candidate.source == "text_layer")
    if text_layer_count < len(candidates) * 0.9:
        return False
    numbered_count = sum(1 for candidate in candidates if candidate.equation_number)
    if numbered_count > max(3, int(len(candidates) * 0.05)):
        return False
    return True


class MinerUJsonFormulaCandidateProvider(MinerUCacheFormulaCandidateProvider):
    """Read candidates from explicit structured MinerU JSON paths."""
    name = "mineru_json"

    def _is_cache_path(self, path: Path) -> bool:
        return _is_mineru_json_cache_file(path)


class PdfExtractKitJsonFormulaCandidateProvider(MinerUCacheFormulaCandidateProvider):
    """Read formula candidates from PDF-Extract-Kit-style JSON exports."""
    name = "pdf_extract_kit_json"

    def _is_cache_path(self, path: Path) -> bool:
        return _is_pdf_extract_kit_cache_file(path)


def _structured_candidates_complete_for_auto(candidates: list[FormulaCandidate]) -> bool:
    if not candidates:
        return False
    return all(
        candidate.latex.strip()
        and "truncated" not in candidate.source
        for candidate in candidates
    )


def _text_layer_candidate_safe_for_auto_merge(candidate: FormulaCandidate) -> bool:
    if not candidate.equation_number:
        return False
    normalized = _normalize_space(candidate.raw_text)
    if not normalized:
        return False
    if _looks_like_equation_reference_prose_candidate(normalized, candidate.equation_number):
        return False
    return True


def _looks_like_equation_reference_prose_candidate(text: str, equation_number: str) -> bool:
    normalized = unicodedata.normalize("NFKC", _normalize_space(text or ""))
    number = _normalize_equation_number_token(equation_number).strip("()（）")
    if not normalized or not number:
        return False
    word_hits = len(WORD_RE.findall(normalized))
    cjk_hits = len(CJK_CHAR_RE.findall(normalized))
    number_pattern = re.escape(number).replace(r"\.", r"[.:]").replace(r"\-", r"[-–—－−]")
    english_reference = re.search(
        rf"\b(?:see|using|from|in|by|according\s+to|with|of)?\s*"
        rf"(?:eqs?\.?|equations?)\s*"
        rf"(?:[\(（][^\)）]+[\)）]\s*(?:,|and|or)?\s*)*"
        rf"[\(（]\s*{number_pattern}\s*[\)）]",
        normalized,
        re.IGNORECASE,
    )
    if english_reference and word_hits >= 8:
        return True
    cjk_reference = re.search(
        rf"(?:如|见|由|利用|采用|根据|通过|按照|结合|参见)?式\s*[\(（]\s*{number_pattern}\s*[\)）]",
        normalized,
    )
    return bool(cjk_reference and cjk_hits >= 8)


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
        self._attempt_budget_remaining: int | None = None
        self._attempts_used = 0

    @property
    def attempts_used(self) -> int:
        """Return actual HTTP attempts made by this provider instance."""
        return self._attempts_used

    def set_attempt_budget(self, budget: int | None) -> None:
        """Limit subsequent HTTP attempts for the current backfill run."""
        self._attempt_budget_remaining = None if budget is None else max(int(budget), 0)

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
            headers = self._headers(data)
            self._consume_attempt_budget()
            self._throttle()
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

    def _consume_attempt_budget(self) -> None:
        if self._attempt_budget_remaining is not None:
            if self._attempt_budget_remaining <= 0:
                raise RuntimeError("SimpleTex formula OCR daily call budget exhausted before request")
            self._attempt_budget_remaining -= 1
        self._attempts_used += 1

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
FORMULA_CANDIDATE_PROVIDERS: dict[
    str,
    type[TextLayerFormulaCandidateProvider]
    | type[AutoFormulaCandidateProvider]
    | type[MinerUCacheFormulaCandidateProvider]
    | type[MinerUJsonFormulaCandidateProvider]
    | type[PdfExtractKitJsonFormulaCandidateProvider],
] = {
    "auto": AutoFormulaCandidateProvider,
    "text_layer": TextLayerFormulaCandidateProvider,
    "mineru_cache": MinerUCacheFormulaCandidateProvider,
    "mineru_json": MinerUJsonFormulaCandidateProvider,
    "pdf_extract_kit_json": PdfExtractKitJsonFormulaCandidateProvider,
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
    """Create a formula candidate detector from the registry."""
    try:
        provider_cls = FORMULA_CANDIDATE_PROVIDERS[name]
    except KeyError as e:
        valid = ", ".join(sorted(FORMULA_CANDIDATE_PROVIDERS))
        raise ValueError(f"Unknown formula candidate provider {name!r}. Valid providers: {valid}") from e
    if name == "text_layer":
        return TextLayerFormulaCandidateProvider()
    pdf_number_enrichment = bool(
        getattr(config, "formula_candidate_cache_pdf_number_enrichment", False)
    )
    append_missing_pdf_candidates = bool(
        getattr(config, "formula_candidate_pdf_number_append_missing_candidates", False)
    )
    if name == "auto":
        return AutoFormulaCandidateProvider(
            cache_dirs=_candidate_cache_dirs_from_config(config),
            pdf_number_enrichment=pdf_number_enrichment,
            append_missing_pdf_candidates=append_missing_pdf_candidates,
        )
    cache_dirs = _candidate_cache_dirs_from_config(config)
    provider_cls = cast(
        type[MinerUCacheFormulaCandidateProvider],
        provider_cls,
    )
    return provider_cls(
        cache_dirs=cache_dirs,
        pdf_number_enrichment=pdf_number_enrichment,
        append_missing_pdf_candidates=append_missing_pdf_candidates,
    )


def count_formula_provider_calls(candidates: list[FormulaCandidate]) -> int:
    """Count candidates that still require OCR provider calls."""
    return sum(1 for candidate in candidates if not str(getattr(candidate, "latex", "")).strip())


def is_high_quality_formula_latex(latex: str) -> bool:
    """Return True for LaTeX that looks useful enough to index."""
    cleaned = _normalize_cached_formula_latex(latex.strip())
    if len(cleaned) < 3 or len(cleaned) > 1500:
        return False
    if NOISE_RE.search(cleaned):
        return False
    if _looks_like_broken_cached_latex(cleaned):
        return False
    if len(set(cleaned)) <= 2:
        return False
    visible_text = _latex_visible_text(cleaned)
    if _looks_like_figure_panel_measurement(cleaned, visible_text):
        return False
    if _looks_like_latex_prose_noise(cleaned, visible_text):
        return False
    has_tagged_formula_latex = bool(
        LATEX_TAG_RE.search(cleaned)
        and (_has_formula_relation(visible_text) or _has_formula_structure(visible_text))
    )
    if not has_tagged_formula_latex and _looks_like_non_formula_text(visible_text):
        return False
    symbol_hits = len(MATH_SYMBOL_RE.findall(visible_text))
    return (
        bool(MATH_LATEX_COMMAND_RE.search(cleaned))
        or bool(LATEX_TAG_RE.search(cleaned) and (_has_formula_relation(visible_text) or symbol_hits >= 2))
        or _has_formula_relation(visible_text)
        or (_has_formula_structure(visible_text) and symbol_hits >= 2)
    )


def extract_formula_candidates(
    pdf_path: Path | str,
    *,
    item_key: str | None = None,
    cache_paths: tuple[Path | str, ...] | None = None,
    max_formulas_per_doc: int = 40,
    max_formulas_per_page: int = 6,
    max_candidates_per_doc: int = 0,
    min_confidence: float = 0.6,
    candidate_provider: FormulaCandidateProvider | None = None,
    pdf_fallback_max_pages: int | None = None,
) -> list[FormulaCandidate]:
    """Detect formula candidates from a PDF without invoking OCR."""
    provider = candidate_provider or TextLayerFormulaCandidateProvider()
    return provider.extract_candidates(
        pdf_path,
        item_key=item_key,
        cache_paths=cache_paths,
        max_formulas_per_doc=max_formulas_per_doc,
        max_formulas_per_page=max_formulas_per_page,
        max_candidates_per_doc=max_candidates_per_doc,
        min_confidence=min_confidence,
        pdf_fallback_max_pages=pdf_fallback_max_pages,
    )


def _extract_text_layer_formula_candidates(
    pdf_path: Path | str,
    *,
    max_formulas_per_doc: int = 40,
    max_formulas_per_page: int = 6,
    max_candidates_per_doc: int = 0,
    min_confidence: float = 0.6,
) -> list[FormulaCandidate]:
    """Detect block-level text-layer display-equation candidates from a PDF.

    Crops are rendered from whole PyMuPDF text blocks. If a publisher combines
    prose and a display equation in one block, nearby prose may be included in
    the OCR crop; line-level segmentation is left for a later phase.
    """
    candidates: list[FormulaCandidate] = []
    doc_candidate_limit = (
        max_candidates_per_doc
        if max_candidates_per_doc > 0
        else max_formulas_per_doc
        if max_formulas_per_doc > 0
        else 0
    )
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
                if confidence <= 0.0 or confidence < min_confidence:
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
                        source="text_layer",
                    )
                )
            page_candidates.sort(key=lambda c: c.confidence, reverse=True)
            page_candidates = _dedupe_candidates(page_candidates)
            if max_formulas_per_page > 0:
                page_candidates = page_candidates[:max_formulas_per_page]
            candidates.extend(page_candidates)
            if doc_candidate_limit > 0 and len(candidates) >= doc_candidate_limit:
                return candidates[:doc_candidate_limit]
    return candidates


def recognize_formulas(
    pdf_path: Path | str,
    provider: FormulaOCRProvider | None,
    *,
    max_formulas_per_doc: int = 40,
    max_formulas_per_page: int = 6,
    min_confidence: float = 0.6,
    candidates: list[FormulaCandidate] | None = None,
    formula_index_offset: int = 0,
    formula_indices: list[int] | None = None,
) -> list[ExtractedFormula]:
    """Detect text-layer formula candidates and OCR them with the provider."""
    formulas: list[ExtractedFormula] = []
    formula_index_offset = max(int(formula_index_offset), 0)
    if candidates is None:
        candidates = extract_formula_candidates(
            pdf_path,
            max_formulas_per_doc=max_formulas_per_doc,
            max_formulas_per_page=max_formulas_per_page,
            min_confidence=min_confidence,
        )
    if not candidates:
        return []
    stable_formula_indices = (
        formula_indices
        if formula_indices is not None and len(formula_indices) == len(candidates)
        else None
    )

    def formula_index_for_position(candidate_index: int) -> int:
        if stable_formula_indices is not None:
            return max(int(stable_formula_indices[candidate_index]), 0)
        return formula_index_offset + candidate_index

    has_pending_ocr = any(not candidate.latex for candidate in candidates)
    if not has_pending_ocr:
        for candidate_index, candidate in enumerate(candidates):
            cached = _formula_from_cached_latex(
                candidate,
                formula_index=formula_index_for_position(candidate_index),
            )
            if cached is not None:
                formulas.append(cached)
        return _dedupe_recognized_formulas(formulas)

    with pymupdf.open(str(pdf_path)) as doc:
        for candidate_index, candidate in enumerate(candidates):
            formula_index = formula_index_for_position(candidate_index)
            if candidate.latex:
                cached = _formula_from_cached_latex(candidate, formula_index=formula_index)
                if cached is not None:
                    formulas.append(cached)
                continue
            if not _is_valid_bbox(candidate.bbox):
                continue
            if candidate.bbox_coordinate_space != "pdf":
                continue
            if candidate.page_num < 1 or candidate.page_num > len(doc):
                logger.warning(
                    "Skipping formula candidate with page %d outside PDF page range 1-%d",
                    candidate.page_num,
                    len(doc),
                )
                continue
            page = doc[candidate.page_num - 1]
            crop = _render_crop(page, candidate.bbox)
            if provider is None:
                raise RuntimeError("Formula OCR provider is required for candidates without LaTeX")
            try:
                result = provider.recognize(crop)
            except Exception as e:
                if _should_stop_formula_batch(e):
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
                    formula_index=formula_index,
                    bbox=candidate.bbox,
                    latex=latex,
                    confidence=result.confidence if result.confidence is not None else candidate.confidence,
                    raw_text=candidate.raw_text,
                    reference_context=candidate.reference_context,
                    equation_number=_candidate_effective_equation_number(candidate),
                    equation_number_status=candidate.equation_number_status,
                    variable_gloss=candidate.variable_gloss,
                    source=candidate.source,
                    provider=getattr(provider, "name", "unknown"),
                )
            )
    return _dedupe_recognized_formulas(formulas)


def _formula_from_cached_latex(candidate: FormulaCandidate, *, formula_index: int) -> ExtractedFormula | None:
    latex = _normalize_cached_formula_latex(candidate.latex.strip())
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
        equation_number=_candidate_effective_equation_number(candidate),
        equation_number_status=candidate.equation_number_status,
        variable_gloss=candidate.variable_gloss,
        source=candidate.source,
        provider=candidate.source,
    )


def _candidate_effective_equation_number(candidate: FormulaCandidate) -> str:
    if candidate.equation_number_status == "unnumbered":
        return ""
    return candidate.equation_number


def _normalize_cached_formula_latex(latex: str) -> str:
    """Repair common MinerU spacing artifacts in numeric constants."""
    normalized = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", latex)
    for _ in range(4):
        collapsed = re.sub(r"(?<![A-Za-z\\])(\d)\s+(?=\d)", r"\1", normalized)
        if collapsed == normalized:
            break
        normalized = collapsed
    return normalized


def _dedupe_recognized_formulas(formulas: list[ExtractedFormula]) -> list[ExtractedFormula]:
    """Drop exact/near-exact duplicate formulas while preserving stable indices."""
    seen: set[str] = set()
    deduped: list[ExtractedFormula] = []
    for formula in formulas:
        signature = _normalized_formula_latex_signature(formula.latex)
        if not signature:
            continue
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(
            ExtractedFormula(
                page_num=formula.page_num,
                formula_index=formula.formula_index,
                bbox=formula.bbox,
                latex=formula.latex,
                confidence=formula.confidence,
                raw_text=formula.raw_text,
                reference_context=formula.reference_context,
                equation_number=formula.equation_number,
                equation_number_status=formula.equation_number_status,
                variable_gloss=formula.variable_gloss,
                source=formula.source,
                provider=formula.provider,
            )
        )
    return deduped


def _normalized_formula_latex_signature(latex: str) -> str:
    signature = latex.strip().lower()
    signature = LATEX_TAG_RE.sub("", signature)
    for _ in range(4):
        replaced = TEXT_FORMATTING_COMMAND_RE.sub(lambda match: match.group(1), signature)
        if replaced == signature:
            break
        signature = replaced
    signature = signature.replace("\\ast", "*")
    signature = re.sub(r"\\(?:left|right)\s*", "", signature)
    signature = re.sub(r"\\(?:bigg|big|Bigg|Big)[lr]?\s*", "", signature)
    signature = re.sub(r"\\displaystyle|\\textstyle|\\phantom\s*\{[^{}]*\}", "", signature)
    signature = re.sub(r"[\s{}]", "", signature)
    return signature


def _looks_like_broken_cached_latex(latex: str) -> bool:
    artifact_hits = OCR_ARTIFACT_LATEX_RE.findall(latex)
    if len(artifact_hits) >= 2:
        return True
    if r"\tt" in latex and artifact_hits:
        return True
    brace_balance = latex.count("{") - latex.count("}")
    if abs(brace_balance) > 12:
        return True
    if abs(brace_balance) > 5 and not (_has_formula_relation(latex) or _has_formula_structure(latex)):
        return True
    return False


def _looks_like_figure_panel_measurement(latex: str, visible_text: str) -> bool:
    if not FIGURE_PANEL_PREFIX_LATEX_RE.search(latex):
        return False
    if len(visible_text) > 140:
        return False
    normalized_text = re.sub(
        r"\{\s*(=|≈|≤|≥|<|>|\\leq?|\\geq?|\\approx)\s*\}",
        r" \1 ",
        visible_text,
    )
    if not re.search(r"=\s*(?:[-+−]?\s*)?\d", normalized_text):
        return False
    relation_count = len(re.findall(r"=|≈|≤|≥|<|>|\\leq?|\\geq?|\\approx", normalized_text))
    if relation_count > 1:
        return False
    return True


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


def _should_stop_formula_batch(exc: Exception) -> bool:
    """Return True for provider errors where continuing would waste paid calls."""
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code in SIMPLETEX_STOP_STATUS_CODES:
            return True
    message = str(exc).lower()
    return any(hint in message for hint in SIMPLETEX_STOP_ERROR_HINTS)


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


def _explicit_formula_cache_paths(paths: tuple[Path | str, ...] | None) -> list[Path]:
    if not paths:
        return []
    found: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if path.exists() and path.is_file() and _is_formula_cache_path(path):
            found.append(path)
    return _unique_paths(found)


def _bounded_cache_scan(
    root: Path,
    keys: set[str],
    *,
    cache_path_filter: Callable[[Path], bool] | None = None,
    max_entries: int = 200000,
) -> list[Path]:
    if cache_path_filter is None:
        cache_path_filter = _is_formula_cache_path
    found: list[Path] = []
    visited = 0
    for path in root.rglob("*"):
        visited += 1
        if visited > max_entries:
            break
        if cache_path_filter(path) and (not keys or _cache_path_matches_keys(path, keys)):
            found.append(path)
    return found


def _looks_like_zotero_storage_root(root: Path) -> bool:
    return root.name.lower() == "storage"


def _is_direct_child_path(path: Path, root: Path) -> bool:
    try:
        return path.resolve().parent == root.resolve()
    except OSError:
        return path.parent == root


def _cache_paths_in_directory(
    directory: Path,
    keys: set[str],
    *,
    require_key_match: bool,
    cache_path_filter: Callable[[Path], bool] | None = None,
) -> list[Path]:
    if cache_path_filter is None:
        cache_path_filter = _is_formula_cache_path
    found: list[Path] = []
    try:
        child_paths = list(directory.iterdir())
    except OSError:
        return found
    for path in child_paths:
        if cache_path_filter(path) and (not require_key_match or _cache_path_matches_keys(path, keys)):
            found.append(path)
    return found


def _zotero_storage_cache_scan(
    root: Path,
    keys: set[str],
    *,
    cache_path_filter: Callable[[Path], bool] | None = None,
    max_child_dirs: int = 10000,
) -> list[Path]:
    """Scan a Zotero storage root without recursively walking the whole library."""
    if cache_path_filter is None:
        cache_path_filter = _is_formula_cache_path
    found: list[Path] = []
    visited_dirs = 0
    try:
        children = list(root.iterdir())
    except OSError as e:
        logger.warning("Failed to inspect Zotero storage cache root %s: %s", root, e)
        return found
    for child in children:
        visited_dirs += 1
        if visited_dirs > max_child_dirs:
            break
        try:
            child_paths = list(child.iterdir())
        except (NotADirectoryError, OSError):
            continue
        for path in child_paths:
            if cache_path_filter(path) and (not keys or _cache_path_matches_keys(path, keys)):
                found.append(path)
    return found


def _cache_path_matches_keys(path: Path, keys: set[str]) -> bool:
    lower_parts = {part.lower() for part in path.parts}
    lower_path = path.as_posix().lower()
    return bool(
        keys & lower_parts
        or any(len(key) >= MIN_CACHE_KEY_SUBSTRING_LENGTH and key in lower_path for key in keys)
    )


def _looks_like_zotero_key(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]{8}", value.strip()))


def _is_formula_cache_path(path: Path) -> bool:
    return _is_formula_cache_file(path) or _is_formula_archive_file(path)


def _is_formula_archive_file(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".zip") and any(token in name for token in ("mineru", "formula", "cache"))


def _is_mineru_json_cache_file(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in MINERU_JSON_CACHE_NAMES
        or name.endswith("_content_list.json")
        or name.endswith("_content_list_v2.json")
        or name.endswith(".content_list.json")
        or name.endswith(".content_list_v2.json")
    )


def _is_pdf_extract_kit_cache_file(path: Path) -> bool:
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


def _is_formula_cache_file(path: Path) -> bool:
    name = path.name.lower()
    if name == "full.md" or name in FORMULA_JSON_CACHE_NAMES:
        return True
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
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def _parse_mineru_json_candidates(
    path: Path,
    *,
    allowed_cache_file: Callable[[Path], bool] | None = None,
) -> list[FormulaCandidate]:
    if allowed_cache_file is None:
        allowed_cache_file = _is_formula_cache_file
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("Failed to read formula candidate cache %s: %s", path, e)
        return []
    if _json_depth_exceeds(payload, MAX_FORMULA_CACHE_JSON_DEPTH):
        logger.warning("Skipping deeply nested formula candidate cache %s", path)
        return []
    candidates = _parse_mineru_json_payload(payload, source=_source_from_cache_path(path))
    if path.name.lower() == "manifest.json":
        for referenced_path in _manifest_referenced_cache_paths(
            payload,
            base_dir=path.parent,
            allowed_cache_file=allowed_cache_file,
        ):
            if referenced_path.name.lower() == "manifest.json" or referenced_path.resolve() == path.resolve():
                continue
            if referenced_path.suffix.lower() == ".md":
                candidates.extend(_parse_mineru_markdown_candidates(referenced_path))
            else:
                candidates.extend(_parse_mineru_json_candidates(referenced_path, allowed_cache_file=allowed_cache_file))
    return candidates


def _parse_mineru_zip_candidates(path: Path) -> list[FormulaCandidate]:
    candidates: list[FormulaCandidate] = []
    try:
        with zipfile.ZipFile(path) as archive:
            cache_names = sorted(
                name for name in archive.namelist()
                if not name.endswith("/") and _is_formula_cache_file(Path(name))
            )
            if len(cache_names) > MAX_FORMULA_CACHE_ZIP_MEMBERS:
                logger.warning("Skipping formula cache archive with too many formula members: %s", path)
                return []
            for name in _select_zip_formula_cache_members(cache_names):
                source = _source_from_cache_path(Path(name))
                try:
                    info = archive.getinfo(name)
                    if info.file_size > MAX_FORMULA_CACHE_ZIP_MEMBER_SIZE_BYTES:
                        logger.warning("Skipping oversized formula cache member %s in %s", name, path)
                        continue
                    text = archive.read(name).decode("utf-8")
                except (KeyError, UnicodeDecodeError, OSError) as e:
                    logger.warning("Failed to read formula cache member %s in %s: %s", name, path, e)
                    continue
                if name.lower().endswith(".md"):
                    candidates.extend(_parse_mineru_markdown_text(text, source=source))
                    continue
                try:
                    payload = json.loads(text)
                except ValueError as e:
                    logger.warning("Failed to parse formula cache member %s in %s: %s", name, path, e)
                    continue
                if _json_depth_exceeds(payload, MAX_FORMULA_CACHE_JSON_DEPTH):
                    logger.warning("Skipping deeply nested formula cache member %s in %s", name, path)
                    continue
                candidates.extend(_parse_mineru_json_payload(payload, source=source))
    except (OSError, zipfile.BadZipFile) as e:
        logger.warning("Failed to read formula candidate archive %s: %s", path, e)
    return candidates


def _select_zip_formula_cache_members(names: list[str]) -> list[str]:
    lower_to_name = {Path(name).name.lower(): name for name in names}
    for preferred in ("content_list.json", "content_list_v2.json", "middle.json", "full.md"):
        if preferred in lower_to_name:
            return [lower_to_name[preferred]]
    return names


def _json_depth_exceeds(payload: Any, max_depth: int) -> bool:
    stack: list[tuple[Any, int]] = [(payload, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > max_depth:
            return True
        if isinstance(value, dict):
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
    return False


def _parse_mineru_json_payload(payload: Any, *, source: str) -> list[FormulaCandidate]:
    candidates: list[FormulaCandidate] = []
    for record in _iter_formula_records(payload):
        candidate = _candidate_from_formula_record(record, source=source)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _manifest_referenced_cache_paths(
    payload: Any,
    *,
    base_dir: Path,
    allowed_cache_file: Callable[[Path], bool] | None = None,
) -> list[Path]:
    if allowed_cache_file is None:
        allowed_cache_file = _is_formula_cache_file
    paths: list[Path] = []
    stack = [payload]
    while stack and len(paths) < 32:
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
        if candidate.exists() and candidate.is_file() and candidate.suffix.lower() != ".zip" and allowed_cache_file(candidate):
            paths.append(candidate.resolve())
    return _unique_paths(paths)


def _parse_mineru_markdown_candidates(path: Path) -> list[FormulaCandidate]:
    try:
        markdown = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to read formula markdown cache %s: %s", path, e)
        return []
    return _parse_mineru_markdown_text(markdown, source="mineru_markdown")


def _parse_mineru_markdown_text(markdown: str, *, source: str) -> list[FormulaCandidate]:
    candidates: list[FormulaCandidate] = []
    patterns = (
        r"\$\$(?P<latex>.+?)\$\$",
        r"\\\[(?P<latex>.+?)\\\]",
        r"```(?:math|latex)\s*(?P<latex>.+?)```",
    )
    matches: list[re.Match[str]] = []
    for pattern in patterns:
        matches.extend(re.finditer(pattern, markdown, re.DOTALL | re.IGNORECASE))
    matches.sort(key=lambda match: match.start())
    for index, match in enumerate(matches):
        latex = _clean_candidate_latex(match.group("latex"))
        if not is_high_quality_formula_latex(latex):
            continue
        candidates.append(
            FormulaCandidate(
                page_num=_page_num_before_offset(markdown, match.start()),
                bbox=(0.0, float(index), 0.0, float(index)),
                raw_text=latex,
                confidence=0.9,
                equation_number=_extract_equation_number(latex),
                source=source,
                bbox_coordinate_space="markdown",
                latex=latex,
            )
        )
    return candidates


def _iter_formula_records(payload: Any, *, inherited_page_num: int | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            records.extend(_iter_formula_records(item, inherited_page_num=inherited_page_num))
    elif isinstance(payload, dict):
        page_num = _record_page_num_or_none(payload) or inherited_page_num
        if _record_looks_like_formula(payload):
            record = dict(payload)
            if page_num is not None and _record_page_num_or_none(record) is None:
                record["page_num"] = page_num
            records.append(record)
        for value in payload.values():
            if isinstance(value, (dict, list)):
                records.extend(_iter_formula_records(value, inherited_page_num=page_num))
    return records


def _record_looks_like_formula(record: dict[str, Any]) -> bool:
    type_text = " ".join(
        str(record.get(key, "")).lower()
        for key in (
            "type",
            "category",
            "class",
            "class_name",
            "cls_name",
            "label",
            "name",
            "block_type",
            "layout_type",
            "det_label",
        )
    )
    if any(hint in type_text for hint in ("formula", "equation", "math")):
        return True
    format_text = " ".join(
        str(record.get(key, "")).lower()
        for key in ("text_format", "math_type", "format", "content_format")
    )
    if "latex" in format_text and is_high_quality_formula_latex(_record_latex(record)):
        return True
    latex = _record_explicit_latex(record)
    return bool(latex and is_high_quality_formula_latex(latex))


def _candidate_from_formula_record(record: dict[str, Any], *, source: str) -> FormulaCandidate | None:
    latex = _record_latex(record)
    bbox = _record_bbox(record)
    if not latex and not _is_valid_bbox(bbox):
        return None
    bbox_coordinate_space = _record_bbox_coordinate_space(record)
    confidence = _record_confidence(record)
    raw_text = latex or str(record.get("text") or record.get("content") or "") or _record_formula_label(record)
    raw_text = _normalize_space(raw_text)
    if not latex and len(raw_text) > MAX_RECORD_FORMULA_LABEL_CHARS:
        return None
    return FormulaCandidate(
        page_num=_record_page_num(record),
        bbox=bbox,
        raw_text=raw_text,
        confidence=confidence,
        equation_number=_record_equation_number(record, latex=latex, raw_text=raw_text),
        source=source,
        bbox_coordinate_space=bbox_coordinate_space,
        latex=latex,
    )


def _record_equation_number(record: dict[str, Any], *, latex: str, raw_text: str) -> str:
    for key in (
        "equation_number",
        "eq_number",
        "eq_no",
        "formula_number",
        "formula_no",
        "number",
        "tag",
    ):
        value = record.get(key)
        if value in (None, ""):
            continue
        number = str(value).strip()
        if (
            (number.startswith("(") and number.endswith(")"))
            or (number.startswith("（") and number.endswith("）"))
            or (number.startswith("[") and number.endswith("]"))
            or (number.startswith("［") and number.endswith("］"))
        ):
            number = number[1:-1].strip()
        formatted_number = _format_equation_number_token(number)
        if formatted_number:
            return formatted_number
    return _extract_equation_number(latex or raw_text)


def _record_formula_label(record: dict[str, Any]) -> str:
    for key in ("label", "cls_name", "class_name", "det_label", "type", "category", "layout_type"):
        value = record.get(key)
        if isinstance(value, str):
            label = _normalize_space(value)
            if label:
                return label
    return ""


def _record_latex(record: dict[str, Any]) -> str:
    content = record.get("content")
    if isinstance(content, dict):
        for key in (
            "math_content",
            "latex",
            "latex_styled",
            "latex_content",
            "formula",
            "rec_formula",
            "formula_text",
            "rec_text",
            "prediction",
            "pred",
            "text",
            "content",
            "html",
            "value",
        ):
            value = content.get(key)
            if isinstance(value, str):
                latex = _clean_candidate_latex(value)
                if latex:
                    return latex
    for key in (
        "latex",
        "latex_styled",
        "latex_content",
        "math_content",
        "formula",
        "rec_formula",
        "formula_text",
        "rec_text",
        "prediction",
        "pred",
        "text",
        "content",
        "html",
        "value",
    ):
        value = record.get(key)
        if isinstance(value, str):
            latex = _clean_candidate_latex(value)
            if latex:
                return latex
    return ""


def _record_explicit_latex(record: dict[str, Any]) -> str:
    content = record.get("content")
    if isinstance(content, dict):
        for key in (
            "math_content",
            "latex",
            "latex_styled",
            "latex_content",
            "formula",
            "rec_formula",
            "formula_text",
            "rec_text",
            "prediction",
            "pred",
        ):
            value = content.get(key)
            if isinstance(value, str):
                latex = _clean_candidate_latex(value)
                if latex:
                    return latex
    for key in (
        "latex",
        "latex_styled",
        "latex_content",
        "math_content",
        "formula",
        "rec_formula",
        "formula_text",
        "rec_text",
        "prediction",
        "pred",
    ):
        value = record.get(key)
        if isinstance(value, str):
            latex = _clean_candidate_latex(value)
            if latex:
                return latex
    return ""


def _clean_candidate_latex(value: str) -> str:
    latex = value.strip()
    latex = re.sub(r"^```(?:math|latex)?", "", latex).strip()
    latex = re.sub(r"```$", "", latex).strip()
    latex = re.sub(r"^\$\$|\$\$$", "", latex).strip()
    latex = re.sub(r"^\$|\$$", "", latex).strip()
    latex = re.sub(r"^\\\(|\\\)$", "", latex).strip()
    latex = re.sub(r"^\\\[|\\\]$", "", latex).strip()
    return latex


def _record_bbox(record: dict[str, Any]) -> tuple[float, float, float, float]:
    for key in ("bbox", "box", "position", "rect", "coordinates", "layout_bbox"):
        bbox = _coerce_bbox(record.get(key))
        if bbox is not None:
            return bbox
    for key in ("polygon", "poly", "points", "dt_boxes"):
        bbox = _bbox_from_polygon(record.get(key))
        if bbox is not None:
            return bbox
    return (0.0, 0.0, 0.0, 0.0)


def _record_bbox_coordinate_space(record: dict[str, Any]) -> str:
    for key in ("bbox_coordinate_space", "coordinate_space", "coord_space", "bbox_space"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_bbox_coordinate_space(value)
    bbox = record.get("bbox") or record.get("box") or record.get("position") or record.get("rect")
    if isinstance(bbox, dict):
        for key in ("coordinate_space", "coord_space", "space"):
            value = bbox.get(key)
            if isinstance(value, str) and value.strip():
                return _normalize_bbox_coordinate_space(value)
    return "unknown"


def _normalize_bbox_coordinate_space(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"pdf", "pdf_point", "pdf_points", "point", "points", "pymupdf", "fitz"}:
        return "pdf"
    if normalized in {"pixel", "pixels", "image", "image_pixel", "image_pixels"}:
        return "image"
    if normalized in {"relative", "normalized", "ratio", "percent", "percentage"}:
        return "relative"
    if normalized in {"markdown", "md"}:
        return "markdown"
    return normalized or "unknown"


def _coerce_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, dict):
        keys = ("x0", "y0", "x1", "y1")
        if all(key in value for key in keys):
            return (
                float(value["x0"]),
                float(value["y0"]),
                float(value["x1"]),
                float(value["y1"]),
            )
        keys = ("left", "top", "right", "bottom")
        if all(key in value for key in keys):
            return (
                float(value["left"]),
                float(value["top"]),
                float(value["right"]),
                float(value["bottom"]),
            )
        keys = ("x", "y", "width", "height")
        if all(key in value for key in keys):
            x0 = float(value["x"])
            y0 = float(value["y"])
            return (x0, y0, x0 + float(value["width"]), y0 + float(value["height"]))
        keys = ("x", "y", "w", "h")
        if all(key in value for key in keys):
            x0 = float(value["x"])
            y0 = float(value["y"])
            return (x0, y0, x0 + float(value["w"]), y0 + float(value["h"]))
        for key in ("points", "polygon", "poly", "dt_boxes"):
            bbox = _bbox_from_polygon(value.get(key))
            if bbox is not None:
                return bbox
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
        except (TypeError, ValueError):
            return None
    return None


def _bbox_from_polygon(value: Any) -> tuple[float, float, float, float] | None:
    points: list[tuple[float, float]] = []
    for point in _iter_polygon_points(value):
        try:
            points.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError):
            continue
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _iter_polygon_points(value: Any) -> list[tuple[Any, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    points: list[tuple[Any, Any]] = []
    for point in value:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            if isinstance(point[0], (int, float, str)) and isinstance(point[1], (int, float, str)):
                points.append((point[0], point[1]))
            else:
                points.extend(_iter_polygon_points(point))
    return points


def _record_confidence(record: dict[str, Any]) -> float:
    for key in ("confidence", "conf", "score", "prob", "confidence_score"):
        value = record.get(key)
        if value is not None:
            try:
                return max(0.0, min(float(value), 1.0))
            except (TypeError, ValueError):
                continue
    return 0.95


def _record_page_num(record: dict[str, Any]) -> int:
    page_num = _record_page_num_or_none(record)
    return page_num if page_num is not None else 1


def _record_page_num_or_none(record: dict[str, Any]) -> int | None:
    for key in ("page_num", "page_number", "page_no", "page"):
        value = record.get(key)
        if value is not None:
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                continue
    for key in ("page_idx", "page_index", "page_id"):
        value = record.get(key)
        if value is not None:
            try:
                return max(1, int(value) + 1)
            except (TypeError, ValueError):
                continue
    return None


def _page_num_before_offset(markdown: str, offset: int) -> int:
    prefix = markdown[:offset]
    page_markers = re.findall(r"(?:<!--\s*page\s+(\d+)\s*-->|^#{1,6}\s*page\s+(\d+)\b)", prefix, re.I | re.M)
    if not page_markers:
        return 1
    last = page_markers[-1]
    return int(last[0] or last[1])


def _source_from_cache_path(path: Path) -> str:
    name = path.name.lower()
    if name == "full.md":
        return "mineru_markdown"
    if name == "middle.json":
        return "mineru_middle_json"
    if name == "manifest.json":
        return "mineru_manifest"
    if any(token in name for token in ("formula_detection", "formula_recognition", "formula_results")):
        return "pdf_extract_kit_json"
    if name in {"formulas.json", "result.json", "results.json", "predictions.json"}:
        return "pdf_extract_kit_json"
    return "mineru_content_list"


def _candidate_has_usable_payload(candidate: FormulaCandidate) -> bool:
    latex = candidate.latex.strip()
    if latex:
        if _is_structured_cache_candidate(candidate):
            return _is_usable_structured_formula_latex(latex)
        return is_high_quality_formula_latex(latex)
    return _is_valid_bbox(candidate.bbox) and candidate.bbox_coordinate_space == "pdf"


def _is_structured_cache_candidate(candidate: FormulaCandidate) -> bool:
    return candidate.source.startswith(("mineru_", "pdf_extract_kit_"))


def _is_usable_structured_formula_latex(latex: str) -> bool:
    """Accept equation records from structured parsers without OCR-style overfiltering."""
    cleaned = latex.strip()
    if len(cleaned) < 2 or len(cleaned) > 2500:
        return False
    if len(set(cleaned)) <= 2:
        return False
    if _looks_like_broken_cached_latex(cleaned):
        return False
    visible_text = _latex_visible_text(cleaned)
    if _looks_like_figure_panel_measurement(cleaned, visible_text):
        return False
    normalized = _normalize_space(visible_text)
    if NOISE_RE.search(normalized) and not _has_formula_relation(normalized):
        return False
    if _looks_like_structured_cache_noise(cleaned, normalized):
        return False
    return (
        bool(MATH_LATEX_COMMAND_RE.search(cleaned))
        or bool(LATEX_TAG_RE.search(cleaned))
        or _has_formula_relation(normalized)
        or _has_formula_structure(normalized)
    )


def _looks_like_structured_cache_noise(latex: str, visible_text: str) -> bool:
    """Reject MinerU/PDF-Extract-Kit records that are labels, units, or glossaries."""
    if LATEX_TAG_RE.search(latex) or _has_formula_relation(visible_text):
        return False
    word_hits = len(WORD_RE.findall(visible_text))
    symbol_hits = len(MATH_SYMBOL_RE.findall(visible_text))
    unit_hits = len(STRUCTURED_TABLE_UNIT_RE.findall(latex))
    table_cell_hits = latex.count("&") + latex.count(r"\\")
    digit_hits = len(re.findall(r"\d", latex))
    has_array_table = bool(re.search(r"\\begin\s*\{\s*array\s*\}", latex))
    has_header_fraction = bool(re.search(r"\\frac\s*\{.{80,}\}\s*\{\s*1\s*\}", latex))
    if has_array_table and table_cell_hits >= 4 and (unit_hits >= 2 or digit_hits >= 12):
        return True
    if unit_hits >= 3 and (has_array_table or has_header_fraction):
        return True
    if r"\overbrace" in latex and unit_hits >= 1 and digit_hits >= 8 and latex.count(r"\quad") >= 3:
        return True
    if re.search(r"\[[^\]]{1,40}\]", visible_text) and word_hits <= 4 and symbol_hits <= 10:
        return True
    if r"\mathrm" in latex and (":" in visible_text or r"\colon" in latex) and word_hits >= 6:
        return True
    if word_hits >= 10 and symbol_hits / max(len(visible_text), 1) < 0.05:
        return True
    return False


def _is_valid_bbox(bbox: tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = bbox
    return x1 > x0 and y1 > y0


def _ocr_doc_limit_for_candidate_discovery(
    *,
    max_formulas_per_doc: int,
    max_candidates_per_doc: int,
) -> int:
    if max_candidates_per_doc > 0 and (
        max_formulas_per_doc <= 0 or max_candidates_per_doc > max_formulas_per_doc
    ):
        return 0
    return max_formulas_per_doc


def _limit_ocr_needed_candidates(
    candidates: list[FormulaCandidate],
    *,
    max_formulas_per_page: int,
    max_formulas_per_doc: int,
) -> list[FormulaCandidate]:
    cached_latex = [candidate for candidate in candidates if candidate.latex.strip()]
    needs_ocr = [candidate for candidate in candidates if not candidate.latex.strip()]
    if max_formulas_per_page > 0:
        needs_ocr = _limit_candidates_per_page_preserving_numbered(
            needs_ocr,
            max_formulas_per_page,
        )
    if max_formulas_per_doc > 0:
        needs_ocr = needs_ocr[:max_formulas_per_doc]
    limited = cached_latex + needs_ocr
    limited.sort(key=lambda c: (c.page_num, c.bbox[1], c.bbox[0], -c.confidence))
    return limited


def _normalize_pdf_fallback_max_pages(max_pages: int | None) -> int | None:
    """Return the effective PDF fallback page limit.

    ``None`` keeps the safe default. ``0`` means a caller explicitly opted in
    to a full-document scan for long theses, books, or high-density papers.
    """
    if max_pages is None:
        return PDF_TEXT_FALLBACK_MAX_PAGES
    value = int(max_pages)
    return value if value > 0 else None


def _pdf_numbering_audit_max_pages(
    candidates: list[FormulaCandidate],
    *,
    pdf_fallback_max_pages: int | None = None,
) -> int | None:
    """Scan far enough to audit numbering for existing cached candidates."""
    base_limit = _normalize_pdf_fallback_max_pages(pdf_fallback_max_pages)
    if base_limit is None:
        return None
    max_candidate_page = max((candidate.page_num for candidate in candidates), default=0)
    if max_candidate_page <= 0:
        return base_limit
    return max(base_limit, max_candidate_page + PDF_NUMBERING_AUDIT_MAX_EXTRA_PAGES)


def _scan_pdf_equation_numbers_for_candidates(
    pdf_path: Path | str,
    candidates: list[FormulaCandidate],
    *,
    pdf_fallback_max_pages: int | None = None,
) -> _PdfEquationNumberScanResult | None:
    if not candidates or all(candidate.equation_number for candidate in candidates):
        return None
    return _scan_pdf_equation_numbers_from_path(
        pdf_path,
        max_pages=_pdf_numbering_audit_max_pages(
            candidates,
            pdf_fallback_max_pages=pdf_fallback_max_pages,
        ),
    )


def _scan_pdf_equation_numbers_from_path(
    pdf_path: Path | str,
    *,
    max_records: int | None = None,
    max_pages: int | None = None,
) -> _PdfEquationNumberScanResult | None:
    path = Path(pdf_path)
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        logger.debug("Failed to open PDF for equation-number scan %s: %s", path, e)
        return None
    try:
        return _scan_pdf_equation_number_records_by_page(
            doc,
            max_records=max_records,
            max_pages=max_pages,
        )
    finally:
        doc.close()


def _enrich_candidate_equation_numbers_from_pdf(
    pdf_path: Path | str,
    candidates: list[FormulaCandidate],
    *,
    records_by_page: dict[int, list[_PdfEquationNumberRecord]] | None = None,
) -> list[FormulaCandidate]:
    if not candidates or all(candidate.equation_number for candidate in candidates):
        return candidates
    if records_by_page is None:
        scan = _scan_pdf_equation_numbers_from_path(pdf_path)
        records_by_page = scan.records_by_page if scan is not None else None
    if not records_by_page:
        return candidates

    enriched = _enrich_candidate_equation_numbers_from_pdf_page_order(candidates, records_by_page)
    enriched = _enrich_candidate_equation_numbers_from_pdf_text(enriched, records_by_page)
    candidates_by_page: dict[int, list[tuple[int, FormulaCandidate]]] = {}
    for index, candidate in enumerate(enriched):
        if candidate.equation_number:
            continue
        candidates_by_page.setdefault(candidate.page_num, []).append((index, candidate))

    for page_num, page_candidates in candidates_by_page.items():
        number_rows = [
            (record.number, record.y_center, record.x_right, record.standalone)
            for record in records_by_page.get(page_num, [])
        ]
        if not number_rows:
            continue
        ordered_candidates = [
            (page_candidates[local_index][0], candidate)
            for local_index, candidate in _candidate_items_in_reading_order(
                [candidate for _index, candidate in page_candidates]
            )
        ]
        ordered_number_records = _pdf_equation_records_in_reading_order(records_by_page.get(page_num, []))
        ordered_numbers = [
            (record.number, record.y_center, record.x_right, record.standalone)
            for record in ordered_number_records
        ]
        ordered_primary_numbers = [row for row in ordered_numbers if not row[3]]
        assigned_numbers = {
            candidate.equation_number
            for candidate in enriched
            if candidate.equation_number
        }
        available_primary_numbers = [
            row for row in ordered_primary_numbers
            if row[0] not in assigned_numbers
        ]
        available_numbers = [
            row for row in ordered_numbers
            if row[0] not in assigned_numbers
        ]
        can_order_match_by_position = all(
            candidate.bbox_coordinate_space == "pdf"
            for _candidate_index, candidate in ordered_candidates
        )
        if can_order_match_by_position and len(ordered_candidates) == len(available_primary_numbers):
            for (candidate_index, candidate), (number, _y_center, _x1, _standalone) in zip(
                ordered_candidates,
                available_primary_numbers,
            ):
                enriched[candidate_index] = replace(candidate, equation_number=number)
            continue
        if can_order_match_by_position and len(ordered_candidates) == len(available_numbers):
            for (candidate_index, candidate), (number, _y_center, _x1, _standalone) in zip(
                ordered_candidates,
                available_numbers,
            ):
                enriched[candidate_index] = replace(candidate, equation_number=number)
            continue
        used_numbers: set[int] = {
            index for index, (number, _y_center, _x1, _standalone) in enumerate(ordered_numbers)
            if number in assigned_numbers
        }
        for candidate_index, candidate in ordered_candidates:
            match_index = _nearest_equation_number_index(candidate, ordered_numbers, used_numbers)
            if match_index is None:
                continue
            used_numbers.add(match_index)
            enriched[candidate_index] = replace(candidate, equation_number=ordered_numbers[match_index][0])
    return enriched


def _enrich_candidate_equation_numbers_from_pdf_page_order(
    candidates: list[FormulaCandidate],
    records_by_page: dict[int, list[_PdfEquationNumberRecord]],
) -> list[FormulaCandidate]:
    """Assign equation numbers by page reading order when cache and PDF counts align."""
    enriched = list(candidates)
    existing_numbers = {
        candidate.equation_number
        for candidate in candidates
        if candidate.equation_number
    }
    candidates_by_page: dict[int, list[tuple[int, FormulaCandidate]]] = {}
    for index, candidate in enumerate(candidates):
        if (
            candidate.equation_number
            or not candidate.latex.strip()
            or not _is_structured_cache_candidate(candidate)
        ):
            continue
        candidates_by_page.setdefault(candidate.page_num, []).append((index, candidate))

    for page_num, page_candidates in candidates_by_page.items():
        if len(page_candidates) < 2:
            continue
        records = records_by_page.get(page_num, [])
        if not records:
            continue
        ordered_records = _pdf_equation_records_in_reading_order(records)
        ordered_primary_records = [
            record for record in ordered_records
            if not record.standalone and record.number not in existing_numbers
        ]
        if len(page_candidates) != len(ordered_primary_records):
            continue
        ordered_candidates = [
            (page_candidates[local_index][0], candidate)
            for local_index, candidate in _candidate_items_in_reading_order(
                [candidate for _index, candidate in page_candidates]
            )
        ]
        for (candidate_index, candidate), record in zip(ordered_candidates, ordered_primary_records):
            enriched[candidate_index] = replace(candidate, equation_number=record.number)
            existing_numbers.add(record.number)

    return enriched


def _enrich_candidate_equation_numbers_from_pdf_text(
    candidates: list[FormulaCandidate],
    records_by_page: dict[int, list[_PdfEquationNumberRecord]],
) -> list[FormulaCandidate]:
    """Pair structured cache formulas with PDF text-layer numbered formula records."""
    if not candidates:
        return candidates

    enriched = list(candidates)
    candidate_matches: list[tuple[float, float, int, int, str]] = []
    existing_numbers_by_page = {
        (candidate.page_num, candidate.equation_number)
        for candidate in candidates
        if candidate.equation_number
    }
    existing_numbers = {
        candidate.equation_number
        for candidate in candidates
        if candidate.equation_number
    }
    fillable_count_by_page: dict[int, int] = {}
    for candidate in candidates:
        if (
            not candidate.equation_number
            and candidate.latex.strip()
            and _is_structured_cache_candidate(candidate)
        ):
            fillable_count_by_page[candidate.page_num] = fillable_count_by_page.get(candidate.page_num, 0) + 1
    for candidate_index, candidate in enumerate(candidates):
        if (
            candidate.equation_number
            or not candidate.latex.strip()
            or not _is_structured_cache_candidate(candidate)
        ):
            continue
        records = records_by_page.get(candidate.page_num, [])
        if not records:
            continue
        scored_records = [
            (_formula_text_match_score(candidate.latex, record.text), record_index, record)
            for record_index, record in enumerate(records)
        ]
        scored_records = [row for row in scored_records if row[0] > 0]
        if not scored_records:
            continue
        scored_records.sort(reverse=True, key=lambda row: row[0])
        best_score, best_record_index, best_record = scored_records[0]
        second_score = scored_records[1][0] if len(scored_records) > 1 else 0.0
        if (candidate.page_num, best_record.number) in existing_numbers_by_page:
            continue
        if best_record.number in existing_numbers:
            continue
        available_record_count = sum(
            1 for record in records
            if record.number not in existing_numbers
            and (candidate.page_num, record.number) not in existing_numbers_by_page
        )
        page_can_be_order_matched = (
            fillable_count_by_page.get(candidate.page_num, 0) == available_record_count
            and available_record_count >= 2
        )
        threshold = (
            FORMULA_TEXT_MATCH_ORDERABLE_PAGE_THRESHOLD
            if page_can_be_order_matched
            else FORMULA_TEXT_MATCH_FILL_THRESHOLD
        )
        clear_low_score_cache_match = (
            candidate.bbox_coordinate_space != "pdf"
            and best_score >= 0.20
            and best_score - second_score >= FORMULA_TEXT_MATCH_MARGIN
        )
        if best_score < threshold and not clear_low_score_cache_match:
            continue
        if best_score - second_score < FORMULA_TEXT_MATCH_MARGIN:
            if best_score < 0.72 or _is_independent_formula_row_candidate(candidate):
                continue
        candidate_matches.append(
            (best_score, second_score, candidate_index, best_record_index, best_record.number)
        )

    if not candidate_matches:
        return candidates

    candidate_matches.sort(reverse=True, key=lambda row: row[0])
    assigned_candidates: set[int] = set()
    assigned_records_by_page: dict[tuple[int, int], int] = {}
    assigned_numbers = set(existing_numbers)
    for _score, _second_score, candidate_index, record_index, number in candidate_matches:
        candidate = candidates[candidate_index]
        page_record_key = (candidate.page_num, record_index)
        if candidate_index in assigned_candidates:
            continue
        if page_record_key in assigned_records_by_page:
            continue
        if number in assigned_numbers:
            continue
        enriched[candidate_index] = replace(candidate, equation_number=number)
        assigned_candidates.add(candidate_index)
        assigned_records_by_page[page_record_key] = candidate_index
        assigned_numbers.add(number)

    return enriched


def _formula_text_match_score(latex: str, pdf_text: str) -> float:
    left_tokens = _formula_match_tokens(latex)
    right_tokens = _formula_match_tokens(pdf_text)
    if not left_tokens or not right_tokens:
        return 0.0
    base_score = _formula_token_match_score(left_tokens, right_tokens)
    if len(right_tokens) <= len(left_tokens) + 4:
        return base_score
    window_size = min(len(right_tokens), max(len(left_tokens) + 4, 4))
    window_scores = [
        _formula_token_match_score(left_tokens, right_tokens[start:start + window_size])
        for start in range(0, len(right_tokens) - window_size + 1)
    ]
    return max([base_score, *window_scores])


def _formula_token_match_score(left_tokens: list[str], right_tokens: list[str]) -> float:
    left_compact = "".join(left_tokens)
    right_compact = "".join(right_tokens)
    sequence_score = SequenceMatcher(a=left_compact, b=right_compact).ratio()
    left_set = set(left_tokens)
    right_set = set(right_tokens)
    overlap_score = len(left_set & right_set) / max(len(left_set | right_set), 1)
    containment_score = len(left_set & right_set) / max(min(len(left_set), len(right_set)), 1)
    score = 0.45 * sequence_score + 0.35 * overlap_score + 0.20 * containment_score
    left_specific = _formula_specific_alpha_tokens(left_tokens)
    right_specific = _formula_specific_alpha_tokens(right_tokens)
    if left_specific and right_specific and left_specific.isdisjoint(right_specific):
        score *= 0.62
    return score


def _formula_match_tokens(text: str) -> list[str]:
    normalized = _latex_visible_text(text)
    for char, replacement in GREEK_TEXT_REPLACEMENTS.items():
        normalized = normalized.replace(char, f" {replacement} ")
    normalized = re.sub(r"\\([A-Za-z]+)", r" \1 ", normalized)
    normalized = _compact_letter_spaced_math_tokens(normalized)
    normalized = re.sub(r"\b([A-Za-z])\s*_\s*\{\s*([A-Za-z])\s*\}", r"\1\2", normalized)
    normalized = normalized.replace("−", "-").replace("–", "-")
    normalized = normalized.replace("⩽", "<=").replace("⩾", ">=")
    normalized = re.sub(r"[\uf000-\uf8ff]", " ", normalized)
    raw_tokens = re.findall(r"[A-Za-z]+|\d+|<=|>=|[=+\-*/<>≤≥≈≠]", normalized.lower())
    tokens: list[str] = []
    for token in raw_tokens:
        token = LATEX_MATCH_TOKEN_ALIASES.get(token, token)
        if token in LATEX_MATCH_TOKEN_STOPWORDS:
            continue
        if len(token) == 1 and token.isalpha() and token in {"l", "r", "c"}:
            continue
        tokens.append(token)
    return tokens


def _formula_specific_alpha_tokens(tokens: list[str]) -> set[str]:
    return {
        token
        for token in tokens
        if token.isalpha()
        and 2 <= len(token) <= 4
        and token not in LATEX_MATCH_GREEK_WORDS
        and token not in LATEX_MATCH_TOKEN_STOPWORDS
        and token not in {"math", "left", "right", "mid"}
    }


def _compact_letter_spaced_math_tokens(text: str) -> str:
    compacted = text
    for _ in range(3):
        updated = re.sub(r"\b([A-Z])\s+([A-Z])\b", lambda match: match.group(1) + match.group(2), compacted)
        if updated == compacted:
            break
        compacted = updated
    return compacted


def _assign_equation_number_statuses_from_pdf(
    pdf_path: Path | str,
    candidates: list[FormulaCandidate],
    *,
    records_by_page: dict[int, list[_PdfEquationNumberRecord]] | None = None,
    scan_ok: bool | None = None,
) -> list[FormulaCandidate]:
    if not candidates:
        return []
    if records_by_page is None and scan_ok is not True and all(candidate.equation_number for candidate in candidates):
        return [
            candidate if candidate.equation_number_status == "provided"
            else replace(candidate, equation_number_status="provided")
            for candidate in candidates
        ]

    if records_by_page is None:
        scan = _scan_pdf_equation_numbers_from_path(pdf_path)
        records_by_page = scan.records_by_page if scan is not None else {}
        scan_ok = scan is not None
    elif scan_ok is None:
        scan_ok = True

    has_any_pdf_number_anchor = any(records_by_page.values())
    assigned: list[FormulaCandidate] = []
    for candidate in candidates:
        page_records = records_by_page.get(candidate.page_num, [])
        if (
            scan_ok
            and candidate.equation_number
            and candidate.equation_number_status != "inferred"
            and page_records
            and candidate.equation_number not in {record.number for record in page_records}
        ):
            candidate = replace(candidate, equation_number="", equation_number_status="")
        status = candidate.equation_number_status
        if candidate.equation_number:
            status = "inferred" if status == "inferred" else "provided"
        elif (
            scan_ok
            and candidate.latex.strip()
            and (
                not has_any_pdf_number_anchor
                or not page_records
                or not _candidate_has_near_pdf_equation_number_record(
                    candidate,
                    page_records,
                )
            )
        ):
            status = "unnumbered"
        elif not status:
            status = "missing"
        if status == candidate.equation_number_status:
            assigned.append(candidate)
        else:
            assigned.append(replace(candidate, equation_number_status=status))
    return assigned


def _candidate_has_near_pdf_equation_number_record(
    candidate: FormulaCandidate,
    records: list[_PdfEquationNumberRecord],
) -> bool:
    if not records or not _is_valid_bbox(candidate.bbox):
        return False
    if candidate.bbox_coordinate_space == "unknown":
        return False
    y_center = (candidate.bbox[1] + candidate.bbox[3]) / 2.0
    y_values = _candidate_coordinate_values_to_pdf_space(candidate, y_center)
    return any(
        min(abs(candidate_y - record.y_center) for candidate_y in y_values) <= 90.0
        for record in records
    )


def _missing_regular_equation_numbers(candidates: list[FormulaCandidate]) -> set[str]:
    values: set[int] = set()
    for candidate in candidates:
        value = _regular_equation_number_value(candidate.equation_number)
        if value is not None:
            values.add(value)
    if len(values) < 3 or min(values) != 1:
        return set()
    expected = set(range(1, max(values) + 1))
    return {f"({value})" for value in sorted(expected - values)}


def _infer_single_leading_missing_equation_number(candidates: list[FormulaCandidate]) -> list[FormulaCandidate]:
    if len(candidates) < 3 or any(candidate.equation_number == "(1)" for candidate in candidates):
        return candidates
    numbered: list[tuple[int, FormulaCandidate]] = []
    ordered = _candidate_items_in_reading_order(candidates)
    for _index, candidate in ordered:
        value = _regular_equation_number_value(candidate.equation_number)
        if value is not None:
            numbered.append((value, candidate))
    if not numbered or min(value for value, _candidate in numbered) != 2:
        return candidates
    first_numbered = min(
        (candidate for _value, candidate in numbered),
        key=lambda candidate: (candidate.page_num, candidate.bbox[1], candidate.bbox[0]),
    )
    leading_unnumbered = [
        (index, candidate)
        for index, candidate in ordered
        if not candidate.equation_number
        and candidate.equation_number_status != "unnumbered"
        and (candidate.page_num, candidate.bbox[1], candidate.bbox[0])
        < (first_numbered.page_num, first_numbered.bbox[1], first_numbered.bbox[0])
    ]
    if len(leading_unnumbered) != 1:
        return candidates
    candidate_index, candidate = leading_unnumbered[0]
    visible_text = _latex_visible_text(candidate.latex)
    has_equation_body = _has_formula_relation(visible_text) or (
        "=" in visible_text and _has_formula_structure(visible_text)
    )
    if not candidate.latex.strip() or not has_equation_body:
        return candidates
    inferred = list(candidates)
    inferred[candidate_index] = replace(candidate, equation_number="(1)")
    return inferred


def _infer_missing_equation_numbers_between_numbered(candidates: list[FormulaCandidate]) -> list[FormulaCandidate]:
    if len(candidates) < 3:
        return candidates
    inferred = list(candidates)
    ordered = _candidate_items_in_reading_order(candidates)
    previous_sequence: tuple[str, int, int] | None = None
    pending: list[tuple[int, FormulaCandidate]] = []
    pending_blocked = False

    for index, candidate in ordered:
        sequence = _equation_number_sequence_value(candidate.equation_number)
        if sequence is None:
            if not candidate.equation_number:
                if _candidate_can_receive_inferred_equation_number(candidate):
                    pending.append((index, candidate))
                else:
                    pending_blocked = True
            continue

        if previous_sequence is not None and pending and not pending_blocked:
            missing_numbers = _missing_sequence_numbers_between(previous_sequence, sequence)
            if missing_numbers and len(missing_numbers) == len(pending):
                for (pending_index, pending_candidate), equation_number in zip(pending, missing_numbers):
                    inferred[pending_index] = replace(
                        pending_candidate,
                        equation_number=equation_number,
                        equation_number_status="inferred",
                    )

        previous_sequence = sequence
        pending = []
        pending_blocked = False

    return _repair_shifted_equation_numbers_between_anchors(inferred)


def _repair_shifted_equation_numbers_between_anchors(
    candidates: list[FormulaCandidate],
) -> list[FormulaCandidate]:
    ordered = _candidate_items_in_reading_order(candidates)
    if len(ordered) < 4:
        return candidates
    repaired = list(candidates)
    for start_pos, (start_index, start_candidate) in enumerate(ordered[:-2]):
        start_sequence = _equation_number_sequence_value(start_candidate.equation_number)
        if start_sequence is None:
            continue
        for end_pos in range(start_pos + 2, min(len(ordered), start_pos + 7)):
            end_index, end_candidate = ordered[end_pos]
            end_sequence = _equation_number_sequence_value(end_candidate.equation_number)
            if end_sequence is None:
                continue
            expected_numbers = _missing_sequence_numbers_between(start_sequence, end_sequence)
            between = ordered[start_pos + 1:end_pos]
            if not expected_numbers or len(expected_numbers) != len(between):
                continue
            between_indices = {candidate_index for candidate_index, _candidate in between}
            outside_numbers = {
                candidate.equation_number
                for candidate_index, candidate in ordered
                if candidate_index not in between_indices and candidate.equation_number
            }
            if any(equation_number in outside_numbers for equation_number in expected_numbers):
                continue
            if not _can_repair_equation_number_segment(between, expected_numbers):
                continue
            for (candidate_index, candidate), equation_number in zip(between, expected_numbers):
                if repaired[candidate_index].equation_number != equation_number:
                    repaired[candidate_index] = replace(
                        candidate,
                        equation_number=equation_number,
                        equation_number_status="inferred",
                    )
            return repaired
    return repaired


def _can_repair_equation_number_segment(
    segment: list[tuple[int, FormulaCandidate]],
    expected_numbers: list[str],
) -> bool:
    if not segment or not any(not candidate.equation_number for _index, candidate in segment):
        return False
    expected = set(expected_numbers)
    for _index, candidate in segment:
        if not _candidate_can_receive_inferred_equation_number(candidate):
            return False
        if candidate.equation_number and candidate.equation_number not in expected:
            return False
    existing_positions = [
        position
        for position, (_index, candidate) in enumerate(segment)
        if candidate.equation_number
    ]
    if not existing_positions:
        return False
    return any(
        segment[position][1].equation_number != expected_numbers[position]
        for position in existing_positions
    )


def _candidate_items_in_reading_order(candidates: list[FormulaCandidate]) -> list[tuple[int, FormulaCandidate]]:
    page_x0_values: dict[int, list[float]] = {}
    for candidate in candidates:
        page_x0_values.setdefault(candidate.page_num, []).append(candidate.bbox[0])
    column_thresholds: dict[int, float] = {}
    for page_num, x0_values in page_x0_values.items():
        if len(x0_values) < 2:
            continue
        min_x0 = min(x0_values)
        max_x0 = max(x0_values)
        if max_x0 - min_x0 < 240.0:
            continue
        threshold = (min_x0 + max_x0) / 2.0
        if any(value <= threshold for value in x0_values) and any(value > threshold for value in x0_values):
            column_thresholds[page_num] = threshold

    def sort_key(item: tuple[int, FormulaCandidate]) -> tuple[float, float, float, float]:
        _index, candidate = item
        threshold = column_thresholds.get(candidate.page_num)
        column = 1.0 if threshold is not None and candidate.bbox[0] > threshold else 0.0
        return (float(candidate.page_num), column, candidate.bbox[1], candidate.bbox[0])

    return sorted(enumerate(candidates), key=sort_key)


def _pdf_equation_records_in_reading_order(
    records: list[_PdfEquationNumberRecord],
) -> list[_PdfEquationNumberRecord]:
    if len(records) < 2:
        return list(records)
    x_right_values = [record.x_right for record in records]
    min_x = min(x_right_values)
    max_x = max(x_right_values)
    page_width = max((record.page_width for record in records), default=0.0)
    threshold: float | None = None
    if page_width > 0:
        has_left_column = any(value <= page_width * 0.58 for value in x_right_values)
        has_right_column = any(value >= page_width * 0.72 for value in x_right_values)
        if has_left_column and has_right_column:
            threshold = (min_x + max_x) / 2.0
    elif max_x - min_x >= 180.0:
        threshold = (min_x + max_x) / 2.0

    def sort_key(record: _PdfEquationNumberRecord) -> tuple[float, float, float]:
        column = 1.0 if threshold is not None and record.x_right > threshold else 0.0
        return (column, record.y_center, record.x_right)

    return sorted(records, key=sort_key)


def _candidate_can_receive_inferred_equation_number(candidate: FormulaCandidate) -> bool:
    if candidate.equation_number_status == "unnumbered":
        return False
    if not candidate.latex.strip():
        return False
    visible_text = _latex_visible_text(candidate.latex)
    return _has_formula_relation(visible_text) or ("=" in visible_text and _has_formula_structure(visible_text))


def _equation_number_sequence_value(equation_number: str) -> tuple[str, int, int] | None:
    match = re.fullmatch(r"\((?P<value>\d+)(?:[A-Za-z])?\)", equation_number or "")
    if match is not None:
        value = int(match.group("value"))
        return ("", 0, value) if 1 <= value <= 80 else None
    match = re.fullmatch(r"\((?P<prefix>\d+)(?P<sep>[.-])(?P<value>\d+)(?:[A-Za-z])?\)", equation_number or "")
    if match is None:
        return None
    prefix = int(match.group("prefix"))
    value = int(match.group("value"))
    if prefix <= 0 or value <= 0 or value > 200:
        return None
    sequence_kind = "hyphen" if match.group("sep") == "-" else "decimal"
    return (sequence_kind, prefix, value)


def _missing_sequence_numbers_between(
    previous: tuple[str, int, int],
    current: tuple[str, int, int],
) -> list[str]:
    if previous[:2] != current[:2] or current[2] <= previous[2] + 1:
        return []
    if previous[0] == "decimal":
        return [f"({previous[1]}.{value})" for value in range(previous[2] + 1, current[2])]
    if previous[0] == "hyphen":
        return [f"({previous[1]}-{value})" for value in range(previous[2] + 1, current[2])]
    return [f"({value})" for value in range(previous[2] + 1, current[2])]


def _regular_equation_number_value(equation_number: str) -> int | None:
    match = re.fullmatch(r"\((\d+)(?:[A-Za-z])?\)", equation_number or "")
    if match is None:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= 80 else None


def _append_missing_pdf_numbered_formula_candidates(
    pdf_path: Path | str,
    candidates: list[FormulaCandidate],
    *,
    allow_empty: bool = False,
    max_records: int | None = None,
    max_pages: int | None = None,
    target_equation_numbers: set[str] | None = None,
    scan_result: _PdfEquationNumberScanResult | None = None,
) -> list[FormulaCandidate]:
    """Add bbox-only OCR candidates for numbered formulas missed by cache parsers."""
    path = Path(pdf_path)
    if (not candidates and not allow_empty) or not path.exists():
        return candidates
    scan = scan_result
    if scan is None:
        scan = _scan_pdf_equation_numbers_from_path(
            path,
            max_records=max_records,
            max_pages=max_pages,
        )
    if scan is None:
        return candidates
    records_by_page = scan.records_by_page
    if not records_by_page:
        return candidates

    existing_numbers: set[str] = set()
    existing_numbers_by_page: dict[int, set[str]] = {}
    for candidate in candidates:
        if candidate.equation_number:
            existing_numbers.add(candidate.equation_number)
            existing_numbers_by_page.setdefault(candidate.page_num, set()).add(candidate.equation_number)

    appended = list(candidates)
    for page_num, records in records_by_page.items():
        page_existing_numbers = existing_numbers_by_page.get(page_num, set())
        for record in records:
            if target_equation_numbers is not None and record.number not in target_equation_numbers:
                continue
            if record.number in existing_numbers:
                continue
            if _looks_like_equation_reference_prose_candidate(record.text, record.number):
                continue
            bbox = _pdf_equation_record_candidate_bbox(record)
            if not _is_valid_bbox(bbox):
                continue
            appended.append(
                FormulaCandidate(
                    page_num=page_num,
                    bbox=bbox,
                    raw_text=record.text,
                    confidence=0.72,
                    equation_number=record.number,
                    source=(
                        "pdf_text_equation_number_truncated"
                        if scan.truncated
                        else "pdf_text_equation_number"
                    ),
                    bbox_coordinate_space="pdf",
                    latex="",
                )
            )
            existing_numbers.add(record.number)
            page_existing_numbers.add(record.number)
    appended.sort(key=lambda c: (c.page_num, c.bbox[1], c.bbox[0], -c.confidence))
    return appended


def _pdf_equation_record_candidate_bbox(record: _PdfEquationNumberRecord) -> tuple[float, float, float, float]:
    if record.page_width <= 0 or record.page_height <= 0:
        return record.bbox
    if not record.standalone:
        x0, y0, x1, y1 = record.bbox
        height = max(1.0, y1 - y0)
        width = max(1.0, x1 - x0)
        x_pad = min(18.0, max(6.0, width * 0.04))
        y_top_pad = min(8.0, max(4.0, height * 0.35))
        y_bottom_pad = min(14.0, max(8.0, height * 0.65))
        x_left = max(0.0, x0 - x_pad)
        has_relation = _has_formula_relation(record.text)
        short_right_tail = x0 >= record.page_width * 0.68 and width < record.page_width * 0.25
        if has_relation:
            y_bottom_pad = min(y_bottom_pad, 4.0)
        if short_right_tail:
            y_top_pad = min(y_top_pad, 2.0)
        if short_right_tail:
            x_left = max(0.0, record.page_width * 0.46)
        elif x0 >= record.page_width * 0.42 and width >= record.page_width * 0.25 and not has_relation:
            x_left = max(0.0, record.page_width * 0.03)
            y_top_pad = min(y_top_pad, 2.0)
            y_bottom_pad = min(y_bottom_pad, 4.0)
        return (
            x_left,
            max(0.0, y0 - y_top_pad),
            min(record.page_width, x1 + x_pad),
            min(record.page_height, y1 + y_bottom_pad),
        )
    x_right = min(record.page_width, record.x_right + 4.0)
    if _is_wide_standalone_equation_record(record) or record.x_right <= record.page_width * 0.55:
        x_left = max(0.0, record.page_width * 0.05)
    else:
        x_left = max(0.0, record.page_width * 0.52)
    y_top = max(0.0, record.y_center - 38.0)
    y_bottom = min(record.page_height, record.y_center + 24.0)
    return (x_left, y_top, x_right, y_bottom)


def _is_wide_standalone_equation_record(record: _PdfEquationNumberRecord) -> bool:
    if not record.standalone or record.page_width <= 0:
        return False
    x0, _y0, x1, _y1 = record.bbox
    return (x1 - x0) >= record.page_width * 0.25


def _pdf_equation_numbers_by_page(doc: pymupdf.Document) -> dict[int, list[tuple[str, float, float, bool]]]:
    records_by_page = _pdf_equation_number_records_by_page(doc)
    return {
        page_num: [
            (record.number, record.y_center, record.x_right, record.standalone)
            for record in records
        ]
        for page_num, records in records_by_page.items()
    }


def _pdf_equation_number_records_by_page(
    doc: pymupdf.Document,
    *,
    max_records: int | None = None,
    max_pages: int | None = None,
) -> dict[int, list[_PdfEquationNumberRecord]]:
    return _scan_pdf_equation_number_records_by_page(
        doc,
        max_records=max_records,
        max_pages=max_pages,
    ).records_by_page


def _scan_pdf_equation_number_records_by_page(
    doc: pymupdf.Document,
    *,
    max_records: int | None = None,
    max_pages: int | None = None,
) -> _PdfEquationNumberScanResult:
    records_by_page: dict[int, list[_PdfEquationNumberRecord]] = {}
    accepted_count = 0
    page_count = len(doc)
    page_limit = page_count
    if max_pages is not None and max_pages > 0:
        page_limit = min(page_count, max_pages)
    truncated = page_limit < page_count
    for page_index in range(page_limit):
        page = doc[page_index]
        records: list[_PdfEquationNumberRecord] = []
        block_entries: list[tuple[tuple[float, float, float, float], str]] = []
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        standalone_records: list[_PdfEquationNumberRecord] = []
        for block in page.get_text("blocks"):
            x0, y0, x1, y1, text, *_rest = block
            block_text = _normalize_space(str(text))
            block_bbox = (float(x0), float(y0), float(x1), float(y1))
            block_entries.append((block_bbox, block_text))
        line_entries = _pdf_text_line_entries(page)
        page_blocks = [*block_entries, *line_entries]
        for block_bbox, block_text in page_blocks:
            x0, y0, x1, y1 = block_bbox
            number = _extract_pdf_block_equation_number(block_text)
            standalone = False
            if not number:
                number = _extract_standalone_pdf_equation_number(
                    block_text,
                    x0=float(x0),
                    x1=float(x1),
                    page_width=page_width,
                )
                standalone = bool(number)
            if not number:
                number = _extract_wide_standalone_pdf_equation_number(block_text)
                standalone = bool(number)
            if not number:
                number = _extract_split_tail_pdf_equation_number(
                    block_text,
                    x1=float(x1),
                    page_width=page_width,
                )
                standalone = bool(number)
            if not number:
                number = _extract_leading_split_tail_pdf_equation_number(block_text)
                standalone = bool(number)
            if not number:
                number = _extract_embedded_pdf_equation_number(block_text)
            if not number:
                continue
            record = _PdfEquationNumberRecord(
                number=number,
                y_center=(float(y0) + float(y1)) / 2.0,
                x_right=float(x1),
                standalone=standalone,
                bbox=block_bbox,
                text=block_text,
                page_width=page_width,
                page_height=page_height,
            )
            if standalone:
                record = _merge_standalone_equation_record_with_formula_block(record, page_blocks)
                if not record.standalone:
                    record = _merge_inline_equation_record_with_formula_blocks(record, page_blocks)
            else:
                record = _merge_inline_equation_record_with_formula_blocks(record, page_blocks)
            if _looks_like_pdf_code_listing_record(record.text):
                continue
            if _looks_like_bibliographic_issue_number_record(record.text, record.number):
                continue
            if _replace_duplicate_pdf_equation_record_if_better(record, records, standalone_records):
                continue
            if _duplicates_pdf_equation_record(record, [*records, *standalone_records]):
                continue
            if standalone:
                if record.standalone:
                    standalone_records.append(record)
                else:
                    records.append(record)
            else:
                records.append(record)
        for split_record in _split_chapter_equation_number_records(
            page_blocks,
            page_width=page_width,
            page_height=page_height,
        ):
            records = [
                existing
                for existing in records
                if not _is_suffix_fragment_of_split_chapter_record(existing, split_record)
            ]
            standalone_records = [
                existing
                for existing in standalone_records
                if not _is_suffix_fragment_of_split_chapter_record(existing, split_record)
            ]
            if _duplicates_pdf_equation_record(split_record, [*records, *standalone_records]):
                continue
            records.append(split_record)
        anchors = [record.x_right for record in records]
        if page_width > 0:
            anchors.extend([page_width * 0.49, page_width * 0.91])
        for record in standalone_records:
            if page_height > 0 and (record.y_center < page_height * 0.08 or record.y_center > page_height * 0.94):
                continue
            anchor_tolerance = (
                42.0
                if _is_wide_standalone_equation_record(record) or not re.fullmatch(r"\(\d+\)", record.number)
                else 28.0
            )
            if anchors and min(abs(record.x_right - anchor) for anchor in anchors) > anchor_tolerance:
                continue
            records.append(record)
        records = _remove_repeated_regular_pdf_numbers_on_page(records)
        if records:
            if max_records is not None and max_records > 0:
                remaining = max_records - accepted_count
                if remaining <= 0:
                    truncated = page_index < page_count
                    break
                if len(records) > remaining:
                    truncated = True
                records = records[:remaining]
            records_by_page[page_index + 1] = records
            accepted_count += len(records)
            if max_records is not None and max_records > 0 and accepted_count >= max_records:
                if page_index < page_count - 1:
                    truncated = True
                break
    return _PdfEquationNumberScanResult(records_by_page=records_by_page, truncated=truncated)


def _pdf_text_line_entries(page: Any) -> list[tuple[tuple[float, float, float, float], str]]:
    try:
        text_dict = page.get_text("dict")
    except Exception:
        return []
    entries: list[tuple[tuple[float, float, float, float], str]] = []
    if not isinstance(text_dict, dict):
        return entries
    for block in text_dict.get("blocks", []):
        if not isinstance(block, dict):
            continue
        for line in block.get("lines", []):
            if not isinstance(line, dict):
                continue
            bbox = line.get("bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                continue
            spans = line.get("spans", [])
            if not isinstance(spans, list):
                continue
            text = "".join(str(span.get("text", "")) for span in spans if isinstance(span, dict))
            line_text = _normalize_space(text)
            if not line_text:
                continue
            entries.append(((float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])), line_text))
    return entries


def _remove_repeated_regular_pdf_numbers_on_page(
    records: list[_PdfEquationNumberRecord],
) -> list[_PdfEquationNumberRecord]:
    if len(records) < 2:
        return records
    keep_ids: set[int] = set()
    seen_regular_numbers: set[str] = set()
    for record in _pdf_equation_records_in_reading_order(records):
        if re.fullmatch(r"\(\d+\)", record.number or ""):
            if record.number in seen_regular_numbers:
                continue
            seen_regular_numbers.add(record.number)
        keep_ids.add(id(record))
    return [record for record in records if id(record) in keep_ids]


def _split_chapter_equation_number_records(
    page_blocks: list[tuple[tuple[float, float, float, float], str]],
    *,
    page_width: float,
    page_height: float,
) -> list[_PdfEquationNumberRecord]:
    entries: list[tuple[tuple[float, float, float, float], str, str]] = []
    for bbox, text in page_blocks:
        normalized = unicodedata.normalize("NFKC", _normalize_space(text or ""))
        if normalized:
            entries.append((bbox, text, normalized))
    records: list[_PdfEquationNumberRecord] = []
    for suffix_bbox, suffix_text, suffix_normalized in entries:
        suffix_match = re.fullmatch(r"(?P<tail>\d{1,2})\)", suffix_normalized)
        if suffix_match is None:
            continue
        if page_width > 0 and suffix_bbox[2] < page_width * 0.70:
            continue
        hyphen_entry = _find_split_chapter_hyphen_entry(suffix_bbox, entries)
        if hyphen_entry is None:
            continue
        hyphen_bbox, hyphen_text, _hyphen_normalized = hyphen_entry
        prefix_entry = _find_split_chapter_prefix_entry(hyphen_bbox, entries)
        if prefix_entry is None:
            continue
        prefix_bbox, prefix_text, prefix_normalized = prefix_entry
        prefix_match = re.search(r"[\(（]\s*(?P<head>\d{1,2})\s*$", prefix_normalized)
        if prefix_match is None:
            continue
        prefix_fragment = prefix_normalized[: prefix_match.start()]
        if not _looks_like_split_chapter_number_formula_prefix(prefix_fragment):
            continue
        number = _format_pdf_equation_number(f"{prefix_match.group('head')}-{suffix_match.group('tail')}")
        if not number:
            continue
        union_bbox = _bbox_union(prefix_bbox, hyphen_bbox, suffix_bbox)
        records.append(
            _PdfEquationNumberRecord(
                number=number,
                y_center=(union_bbox[1] + union_bbox[3]) / 2.0,
                x_right=union_bbox[2],
                standalone=False,
                bbox=union_bbox,
                text=_normalize_space(f"{prefix_text} {hyphen_text} {suffix_text}"),
                page_width=page_width,
                page_height=page_height,
            )
        )
    return records


def _find_split_chapter_hyphen_entry(
    suffix_bbox: tuple[float, float, float, float],
    entries: list[tuple[tuple[float, float, float, float], str, str]],
) -> tuple[tuple[float, float, float, float], str, str] | None:
    candidates: list[tuple[float, tuple[tuple[float, float, float, float], str, str]]] = []
    for bbox, text, normalized in entries:
        if bbox == suffix_bbox:
            continue
        if unicodedata.normalize("NFKC", normalized) != "-":
            continue
        x_gap = suffix_bbox[0] - bbox[2]
        if x_gap < -1.0 or x_gap > 10.0:
            continue
        if not _bboxes_share_text_line(bbox, suffix_bbox):
            continue
        candidates.append((abs(x_gap), (bbox, text, normalized)))
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: candidate[0])[1]


def _find_split_chapter_prefix_entry(
    hyphen_bbox: tuple[float, float, float, float],
    entries: list[tuple[tuple[float, float, float, float], str, str]],
) -> tuple[tuple[float, float, float, float], str, str] | None:
    candidates: list[tuple[float, tuple[tuple[float, float, float, float], str, str]]] = []
    for bbox, text, normalized in entries:
        if bbox == hyphen_bbox:
            continue
        if re.search(r"[\(（]\s*\d{1,2}\s*$", normalized) is None:
            continue
        x_gap = hyphen_bbox[0] - bbox[2]
        if x_gap < -2.0 or x_gap > 16.0:
            continue
        if not _bboxes_share_text_line(bbox, hyphen_bbox, tolerance=14.0):
            continue
        candidates.append((abs(x_gap), (bbox, text, normalized)))
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: candidate[0])[1]


def _looks_like_split_chapter_number_formula_prefix(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text or "")
    if not normalized:
        return False
    if CAPTION_OR_REFERENCE_RE.search(normalized) or SECTION_HEADING_RE.match(normalized):
        return False
    signal_count = (
        len(MATH_SYMBOL_RE.findall(normalized))
        + len(PRIVATE_USE_MATH_GLYPH_RE.findall(normalized))
        + len(re.findall(r"[\ue000-\uf8ff]", normalized))
        + _math_alnum_char_count(normalized)
    )
    return signal_count > 0 or _has_formula_relation(normalized) or _has_formula_structure(normalized)


def _is_suffix_fragment_of_split_chapter_record(
    record: _PdfEquationNumberRecord,
    split_record: _PdfEquationNumberRecord,
) -> bool:
    split_token = split_record.number.strip("()")
    if "-" not in split_token:
        return False
    suffix = split_token.rsplit("-", 1)[1]
    if record.number.strip("()") != suffix:
        return False
    if not _bboxes_share_text_line(record.bbox, split_record.bbox, tolerance=14.0):
        return False
    if abs(record.x_right - split_record.x_right) <= 4.0:
        return True
    return _bbox_intersection_area(record.bbox, split_record.bbox) > 0


def _bboxes_share_text_line(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    *,
    tolerance: float = 10.0,
) -> bool:
    first_center = (first[1] + first[3]) / 2.0
    second_center = (second[1] + second[3]) / 2.0
    y_overlap = min(first[3], second[3]) - max(first[1], second[1])
    return y_overlap >= -2.0 or abs(first_center - second_center) <= tolerance


def _bbox_union(
    *bboxes: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return (
        min(bbox[0] for bbox in bboxes),
        min(bbox[1] for bbox in bboxes),
        max(bbox[2] for bbox in bboxes),
        max(bbox[3] for bbox in bboxes),
    )


def _duplicates_pdf_equation_record(
    record: _PdfEquationNumberRecord,
    existing_records: list[_PdfEquationNumberRecord],
) -> bool:
    for existing in existing_records:
        if existing.number != record.number:
            continue
        if _bbox_intersection_area(record.bbox, existing.bbox) > 0:
            return True
        if abs(record.y_center - existing.y_center) <= 4.0:
            return True
    return False


def _replace_duplicate_pdf_equation_record_if_better(
    record: _PdfEquationNumberRecord,
    records: list[_PdfEquationNumberRecord],
    standalone_records: list[_PdfEquationNumberRecord],
) -> bool:
    """Keep the more complete bbox/text when split PDF records duplicate a number."""
    for index, existing in enumerate(records):
        if not _is_duplicate_pdf_equation_record(record, existing):
            continue
        if _pdf_equation_record_detail_score(record) > _pdf_equation_record_detail_score(existing):
            records[index] = record
        return True
    for index, existing in enumerate(standalone_records):
        if not _is_duplicate_pdf_equation_record(record, existing):
            continue
        if _pdf_equation_record_detail_score(record) > _pdf_equation_record_detail_score(existing):
            if record.standalone:
                standalone_records[index] = record
            else:
                del standalone_records[index]
                records.append(record)
        return True
    return False


def _is_duplicate_pdf_equation_record(
    record: _PdfEquationNumberRecord,
    existing: _PdfEquationNumberRecord,
) -> bool:
    if existing.number != record.number:
        return False
    if _bbox_intersection_area(record.bbox, existing.bbox) > 0:
        return True
    return abs(record.y_center - existing.y_center) <= 4.0


def _pdf_equation_record_detail_score(record: _PdfEquationNumberRecord) -> float:
    x0, y0, x1, y1 = record.bbox
    area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    text_score = min(float(len(record.text or "")), 1000.0)
    relation_bonus = 250.0 if _has_formula_relation(record.text) else 0.0
    merged_bonus = 120.0 if not record.standalone else 0.0
    return area + text_score + relation_bonus + merged_bonus


def _bbox_intersection_area(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    x_overlap = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    y_overlap = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    return x_overlap * y_overlap


def _merge_standalone_equation_record_with_formula_block(
    record: _PdfEquationNumberRecord,
    page_blocks: list[tuple[tuple[float, float, float, float], str]],
) -> _PdfEquationNumberRecord:
    if not record.standalone:
        return record
    rx0, ry0, rx1, ry1 = record.bbox
    leading_tail = _looks_like_leading_equation_number_tail(record.text)
    best: tuple[tuple[float, float, int], tuple[float, float, float, float], str] | None = None
    candidate_blocks = [
        *_same_line_fragmented_formula_blocks_for_number(record, page_blocks),
        *page_blocks,
    ]
    for bbox, text in candidate_blocks:
        if bbox == record.bbox:
            continue
        bx0, by0, bx1, by1 = bbox
        y_center = (by0 + by1) / 2.0
        y_distance = abs(y_center - record.y_center)
        y_overlap = min(by1, ry1) - max(by0, ry0)
        if leading_tail:
            y_gap = max(0.0, ry0 - by1)
            x_overlap = min(bx1, rx1) - max(bx0, rx0)
            if by0 >= ry1 or y_gap > 58.0:
                continue
            if x_overlap < -20.0:
                continue
        else:
            if bx0 >= rx0 or bx1 > rx0 + 18.0:
                continue
            if y_overlap < -8.0 and y_distance > 42.0:
                continue
        if not _looks_like_formula_block_for_standalone_number(text):
            continue
        x_gap = max(0.0, rx0 - bx1)
        signal_count = _standalone_formula_block_signal_count(text)
        score = (y_distance, x_gap, -signal_count)
        if best is None or score < best[0]:
            best = (score, bbox, text)
    if best is None:
        return record
    _score, bbox, text = best
    bx0, by0, bx1, by1 = bbox
    union_bbox = (
        min(bx0, rx0),
        min(by0, ry0),
        max(bx1, rx1),
        max(by1, ry1),
    )
    return replace(
        record,
        y_center=(union_bbox[1] + union_bbox[3]) / 2.0,
        x_right=union_bbox[2],
        standalone=False,
        bbox=union_bbox,
        text=_normalize_space(f"{text} {record.text}"),
    )


def _same_line_fragmented_formula_blocks_for_number(
    record: _PdfEquationNumberRecord,
    page_blocks: list[tuple[tuple[float, float, float, float], str]],
) -> list[tuple[tuple[float, float, float, float], str]]:
    if not record.standalone:
        return []
    rx0, _ry0, _rx1, _ry1 = record.bbox
    pieces: list[tuple[tuple[float, float, float, float], str]] = []
    for bbox, text in page_blocks:
        if bbox == record.bbox:
            continue
        bx0, _by0, bx1, _by1 = bbox
        normalized = _normalize_space(text)
        if not normalized or len(normalized) > 40:
            continue
        if bx0 >= rx0 or bx1 > rx0 + 18.0:
            continue
        if not _bboxes_share_text_line(record.bbox, bbox, tolerance=6.0):
            continue
        if CJK_CHAR_RE.search(normalized) and not (
            _has_formula_relation(normalized) or _has_formula_structure(normalized)
        ):
            continue
        pieces.append((bbox, normalized))
    if len(pieces) < 2:
        return []
    pieces.sort(key=lambda item: (item[0][0], item[0][1]))
    text = _normalize_space(" ".join(piece_text for _bbox, piece_text in pieces))
    if not _looks_like_formula_block_for_standalone_number(text):
        return []
    union_bbox = _bbox_union(*(bbox for bbox, _text in pieces))
    return [(union_bbox, text)]


def _merge_inline_equation_record_with_formula_blocks(
    record: _PdfEquationNumberRecord,
    page_blocks: list[tuple[tuple[float, float, float, float], str]],
) -> _PdfEquationNumberRecord:
    """Merge fragmented same-line formula blocks into a numbered tail record."""
    if record.standalone or record.page_width <= 0 or not page_blocks:
        return record
    rx0, ry0, rx1, ry1 = record.bbox
    record_height = max(1.0, ry1 - ry0)
    y_top = max(0.0, ry0 - max(18.0, record_height * 1.4))
    y_bottom = min(record.page_height, ry1 + max(12.0, record_height * 0.9))
    record_has_relation = _has_formula_relation(record.text)

    nearby_blocks: list[tuple[tuple[float, float, float, float], str]] = []
    left_math_signal = False
    left_other_number_signal = False
    for bbox, text in page_blocks:
        bx0, by0, bx1, by1 = bbox
        if bx1 < 0 or bx0 > min(record.page_width, rx1 + 12.0):
            continue
        if by1 < y_top or by0 > y_bottom:
            continue
        if record_has_relation and not _bboxes_share_text_line(record.bbox, bbox, tolerance=6.0):
            continue
        if bx0 < record.page_width * 0.45 and _contains_other_equation_number(text, record.number):
            left_other_number_signal = True
        if _contains_other_equation_number(text, record.number):
            continue
        if not _looks_like_inline_formula_merge_block(text):
            continue
        nearby_blocks.append((bbox, text))
        if bx0 < record.page_width * 0.45 and bx1 <= rx1 + 12.0:
            left_math_signal = True

    if not nearby_blocks:
        return record

    if rx0 >= record.page_width * 0.42 and (not left_math_signal or left_other_number_signal):
        column_left = record.page_width * 0.46
    else:
        column_left = record.page_width * 0.03
    column_right = min(record.page_width, rx1 + 12.0)
    selected: list[tuple[tuple[float, float, float, float], str]] = []
    for bbox, text in nearby_blocks:
        bx0, _by0, bx1, _by1 = bbox
        if bx1 < column_left or bx0 > column_right:
            continue
        selected.append((bbox, text))
    if len(selected) < 2:
        return record

    x0 = min(bbox[0] for bbox, _text in selected)
    y0 = min(bbox[1] for bbox, _text in selected)
    x1 = max(bbox[2] for bbox, _text in selected)
    y1 = max(bbox[3] for bbox, _text in selected)
    if x0 >= rx0 and y0 >= ry0 and x1 <= rx1 and y1 <= ry1:
        return record
    selected_text = " ".join(text for _bbox, text in sorted(selected, key=lambda item: (item[0][1], item[0][0])))
    union_bbox = (x0, y0, x1, y1)
    return replace(
        record,
        y_center=(union_bbox[1] + union_bbox[3]) / 2.0,
        x_right=union_bbox[2],
        bbox=union_bbox,
        text=_normalize_space(selected_text),
    )


def _contains_other_equation_number(text: str, current_number: str) -> bool:
    for match in PDF_EQUATION_NUMBER_TOKEN_RE.finditer(text or ""):
        number = _format_pdf_equation_number(match.group("number"))
        if number and number != current_number:
            return True
    return False


def _looks_like_inline_formula_merge_block(text: str) -> bool:
    normalized = _normalize_space(text)
    if not normalized:
        return False
    if CAPTION_OR_REFERENCE_RE.search(normalized) or SECTION_HEADING_RE.match(normalized):
        return False
    if re.fullmatch(r"\d+", normalized):
        return False
    word_hits = len(WORD_RE.findall(normalized))
    cjk_chars = len(CJK_CHAR_RE.findall(normalized))
    signal_count = _standalone_formula_block_signal_count(normalized)
    has_relation = _has_formula_relation(normalized)
    has_structure = _has_formula_structure(normalized)
    if cjk_chars >= 8 and _looks_like_cjk_formula_explanation_block(normalized):
        return False
    if cjk_chars >= 8 and not has_relation and signal_count < 6:
        return False
    if PROSE_CUE_RE.search(normalized) and not has_relation and word_hits >= 3:
        return False
    if PROSE_CUE_RE.search(normalized) and not has_relation and cjk_chars >= 4:
        return False
    if word_hits > 8 and signal_count < 4:
        return False
    if len(normalized) > 220 and signal_count < 6:
        return False
    return (
        has_relation
        or has_structure
        or signal_count > 0
        or bool(re.search(r"[()[\]{}⎛⎜⎝⎞⎟⎠√∫∑∏+\-−*/=<>≤≥≈]", normalized))
    )


def _looks_like_cjk_formula_explanation_block(text: str) -> bool:
    return bool(
        re.search(
            r"(?:式中|为|当前|应力|应变|温度|材料|参数|无量纲|参考|等效|模型|试验|曲线|图\d*)",
            text or "",
        )
    )


def _looks_like_formula_block_for_standalone_number(text: str) -> bool:
    normalized = _normalize_space(text)
    if not normalized:
        return False
    if CAPTION_OR_REFERENCE_RE.search(normalized) or SECTION_HEADING_RE.match(normalized):
        return False
    word_hits = len(WORD_RE.findall(normalized))
    signal_count = _standalone_formula_block_signal_count(normalized)
    if word_hits > 8 and signal_count < 4:
        return False
    if word_hits > 16:
        return False
    if PROSE_CUE_RE.search(normalized) and signal_count < 5:
        return False
    return (
        _has_formula_relation(normalized)
        or _has_formula_structure(normalized)
        or _looks_like_short_relation_formula(normalized)
        or signal_count >= 2
    )


def _looks_like_short_relation_formula(text: str) -> bool:
    raw = _normalize_space(text)
    if not raw or len(raw) > 90:
        return False
    if len(WORD_RE.findall(raw)) > 2:
        return False
    for candidate in (raw, unicodedata.normalize("NFKC", raw)):
        if re.search(
            r"(?:^|[\s({;,])"
            r"[A-Za-zΑ-Ωα-ω][A-Za-zΑ-Ωα-ω0-9_.'′]{0,4}"
            r"\s*(?:=|¼|≈|≤|≥|≠|<|>|:=)\s*"
            r"(?:[-+−]?\s*)?[A-Za-zΑ-Ωα-ω0-9]",
            candidate,
        ):
            return True
    return False


def _standalone_formula_block_signal_count(text: str) -> int:
    return (
        len(MATH_SYMBOL_RE.findall(text))
        + len(PRIVATE_USE_MATH_GLYPH_RE.findall(text))
        + _math_alnum_char_count(text)
        + len(re.findall(r"[Α-Ωα-ω]", text))
    )


def _extract_pdf_block_equation_number(text: str) -> str:
    match = TRAILING_EQUATION_NUMBER_RE.search(text)
    if match is None:
        return ""
    prefix = _normalize_space(text[:match.start()])
    if not prefix or len(prefix) > 650:
        return ""
    formatted_number = _format_pdf_equation_number(match.group("number"))
    if not formatted_number:
        return ""
    if _looks_like_reference_or_material_trailing_number(prefix, formatted_number):
        return ""
    if CAPTION_OR_REFERENCE_RE.search(prefix) or SECTION_HEADING_RE.match(prefix):
        return ""
    symbol_hits = len(MATH_SYMBOL_RE.findall(prefix))
    greek_hits = len(re.findall(r"[Α-Ωα-ω]", prefix))
    private_math_hits = len(PRIVATE_USE_MATH_GLYPH_RE.findall(prefix))
    math_alnum_hits = _math_alnum_char_count(prefix)
    word_hits = len(WORD_RE.findall(prefix))
    has_formula_signal = (
        _has_formula_relation(prefix)
        or _has_formula_structure(prefix)
        or private_math_hits > 0
        or math_alnum_hits > 0
        or bool(re.search(r"[=¼þ<>≤≥≈:+*/−]|[Α-Ωα-ω]|[∑∏∫√∞∂∇∆]", prefix))
        or _looks_like_low_fidelity_formula_fragment(prefix)
        or _looks_like_low_fidelity_numeric_formula_fragment(prefix, match.group("number"))
        or _looks_like_low_fidelity_absolute_value_fragment(prefix)
    )
    if not has_formula_signal:
        compact_prefix = re.sub(r"[\s,.;:，。；：\x00-\x1f]+", "", prefix)
        if not (
            _looks_like_split_equation_number_prefix(compact_prefix, word_hits)
            or _looks_like_spaced_formula_variable_prefix(prefix)
        ):
            return ""
    if word_hits > 20 and symbol_hits + greek_hits + private_math_hits + math_alnum_hits < 2:
        return ""
    return formatted_number


def _math_alnum_char_count(text: str) -> int:
    return sum(1 for char in text if 0x1D400 <= ord(char) <= 0x1D7FF)


def _looks_like_low_fidelity_formula_fragment(prefix: str) -> bool:
    normalized = _normalize_space(prefix)
    if not normalized or len(normalized) > 90:
        return False
    if CAPTION_OR_REFERENCE_RE.search(normalized) or SECTION_HEADING_RE.match(normalized):
        return False
    words = re.findall(r"[A-Za-z]{2,}", normalized)
    math_words = {"max", "min", "exp", "log", "ln", "tr", "sin", "cos", "tan"}
    if any(word.casefold() not in math_words for word in words):
        return False
    digit_hits = len(re.findall(r"\d", normalized))
    single_letter_hits = len(re.findall(r"(?<![A-Za-z])[A-Za-z](?![A-Za-z])", normalized))
    bracket_hits = len(re.findall(r"[()[\]{}]", normalized))
    if digit_hits < 3 or single_letter_hits < 2:
        return False
    return bracket_hits >= 2 or any(word.casefold() in math_words for word in words)


def _looks_like_low_fidelity_numeric_formula_fragment(prefix: str, number: str) -> bool:
    normalized_number = _normalize_equation_number_token(number)
    if "." in normalized_number:
        return False
    if not normalized_number.isdigit() or int(normalized_number) < 10:
        return False
    normalized = _normalize_space(prefix)
    if not normalized or len(normalized) > 32:
        return False
    if re.search(r"[A-Za-z\u4e00-\u9fffΑ-Ωα-ω]", normalized):
        return False
    tokens = re.findall(r"\d+", normalized)
    if not (2 <= len(tokens) <= 5):
        return False
    if not all(len(token) == 1 and 0 <= int(token) <= 9 for token in tokens):
        return False
    stripped = re.sub(r"[\d\s,.;:，。；：()+\-−*/]+", "", normalized)
    return stripped == ""


def _looks_like_low_fidelity_absolute_value_fragment(prefix: str) -> bool:
    normalized = _normalize_space(prefix)
    if not normalized or len(normalized) > 28:
        return False
    if len(WORD_RE.findall(normalized)) > 1:
        return False
    if re.search(r"[\u20d2\u20d3\u2758\u2223\u2016|]", normalized) is None:
        return False
    stripped = re.sub(r"[\u20d2\u20d3\u2758\u2223\u2016|A-Za-zΑ-Ωα-ω0-9_.'′\s+\-−*/(){}\[\]]+", "", normalized)
    return stripped == ""


def _extract_standalone_pdf_equation_number(
    text: str,
    *,
    x0: float,
    x1: float,
    page_width: float,
) -> str:
    text = text or ""
    block_width = max(0.0, x1 - x0)
    dangling_right_match = re.fullmatch(r"\s*(?P<number>\d+(?:\.\d+)*(?:[A-Za-z])?)\s*[\)）Þ]\s*", text)
    if dangling_right_match is not None:
        if block_width > max(28.0, page_width * 0.06):
            return ""
        if page_width > 0 and x1 < page_width * 0.82:
            return ""
        return _format_pdf_equation_number(dangling_right_match.group("number"))
    if not re.fullmatch(r"\s*[\(（ð].*[\)）Þ]\s*", text):
        return ""
    match = re.fullmatch(rf"[\s（(ð]*(?P<number>{PDF_EQUATION_NUMBER_PATTERN})[\s）)Þ]*", text)
    if match is None:
        return ""
    if block_width > max(46.0, page_width * 0.12):
        return ""
    # Formula numbers are often standalone blocks at the right edge of either
    # a single-column page or one column in a two-column article.
    if page_width > 0 and x1 < page_width * 0.42:
        return ""
    return _format_pdf_equation_number(match.group("number"))


def _extract_wide_standalone_pdf_equation_number(text: str) -> str:
    match = re.fullmatch(rf"\s*[\(（ð]\s*(?P<number>{PDF_EQUATION_NUMBER_PATTERN})\s*[\)）Þ]\s*", text or "")
    if match is None:
        return ""
    return _format_pdf_equation_number(match.group("number"))


def _extract_split_tail_pdf_equation_number(
    text: str,
    *,
    x1: float,
    page_width: float,
) -> str:
    match = TRAILING_EQUATION_NUMBER_RE.search(text or "")
    if match is None:
        return ""
    prefix = _normalize_space(text[:match.start()])
    if not prefix or len(prefix) > 80:
        return ""
    formatted_number = _format_pdf_equation_number(match.group("number"))
    if not formatted_number:
        return ""
    if _looks_like_formula_reference_tail(prefix):
        return ""
    if _looks_like_reference_or_material_trailing_number(prefix, formatted_number):
        return ""
    if CAPTION_OR_REFERENCE_RE.search(prefix) or SECTION_HEADING_RE.match(prefix):
        return ""
    if _has_formula_relation(prefix) or _has_formula_structure(prefix):
        return ""
    if len(WORD_RE.findall(prefix)) > 2:
        return ""
    if page_width > 0 and x1 < page_width * 0.40:
        return ""
    has_tail_signal = bool(
        re.search(r"\b(?:GPa|MPa|kPa|Pa|N|kN|mm|s)\b|%", prefix, re.IGNORECASE)
        or re.search(r"[\x00-\x1f()[\]{}（）⎛⎜⎝⎞⎟⎠]", prefix)
    )
    if not has_tail_signal:
        return ""
    return formatted_number


def _extract_leading_split_tail_pdf_equation_number(text: str) -> str:
    match = re.fullmatch(
        rf"\s*[\(（ð]\s*(?P<number>{PDF_EQUATION_NUMBER_PATTERN})\s*[\)）Þ]\s*"
        r"(?:with|where|,|，)\s*",
        text or "",
        flags=re.IGNORECASE,
    )
    if match is None:
        return ""
    return _format_pdf_equation_number(match.group("number"))


def _looks_like_leading_equation_number_tail(text: str) -> bool:
    return bool(_extract_leading_split_tail_pdf_equation_number(text))


def _extract_embedded_pdf_equation_number(text: str) -> str:
    normalized_text = _normalize_space(text or "")
    if _looks_like_pdf_code_listing_record(normalized_text):
        return ""
    if _looks_like_author_affiliation_number_block(normalized_text):
        return ""
    if _looks_like_figure_panel_dimension_number_block(normalized_text):
        return ""
    for match in PDF_EQUATION_NUMBER_TOKEN_RE.finditer(text or ""):
        prefix = _normalize_space(text[:match.start()])
        suffix = _normalize_space(text[match.end():])
        if not prefix or len(prefix) > 650:
            continue
        formatted_number = _format_pdf_equation_number(match.group("number"))
        if not formatted_number:
            continue
        if _looks_like_reference_or_material_trailing_number(prefix, formatted_number):
            continue
        if _looks_like_formula_reference_tail(prefix):
            continue
        if _is_embedded_prose_reference_number(text, match.start()):
            continue
        if _looks_like_reference_issue_number_context(prefix, suffix):
            continue
        if _looks_like_bibliographic_issue_number_context(prefix, suffix):
            continue
        if _looks_like_dimensional_abbreviation_context(prefix, suffix, match.group("number")):
            continue
        if _looks_like_cjk_prose_enumeration_context(prefix, suffix):
            continue
        if _looks_like_embedded_prose_citation_context(prefix, suffix):
            continue
        if CAPTION_OR_REFERENCE_RE.search(prefix) or SECTION_HEADING_RE.match(prefix):
            continue
        if re.search(r"(?:\b(?:eq\.?|equation)|式)\s*$", prefix, flags=re.IGNORECASE):
            continue
        compact_prefix = re.sub(r"[\s,.;:，。；：\x00-\x1f]+", "", prefix)
        if not (
            _has_formula_relation(prefix)
            or _has_formula_structure(prefix)
            or bool(MATH_SYMBOL_RE.search(prefix))
            or _math_alnum_char_count(prefix) > 0
            or _looks_like_split_equation_number_prefix(compact_prefix, len(WORD_RE.findall(prefix)))
            or _looks_like_spaced_formula_variable_prefix(prefix)
        ):
            continue
        prefix_has_formula_signal = (
            _has_formula_relation(prefix)
            or _has_formula_structure(prefix)
            or bool(MATH_SYMBOL_RE.search(prefix))
            or _math_alnum_char_count(prefix) > 0
            or _looks_like_split_equation_number_prefix(compact_prefix, len(WORD_RE.findall(prefix)))
            or _looks_like_spaced_formula_variable_prefix(prefix)
        )
        suffix_has_formula_signal = (
            _has_formula_relation(suffix)
            or _has_formula_structure(suffix)
            or bool(MATH_SYMBOL_RE.search(suffix))
            or _math_alnum_char_count(suffix) > 0
        )
        normalized_match_number = _normalize_equation_number_token(match.group("number"))
        match_number_value = int(normalized_match_number) if normalized_match_number.isdigit() else None
        if (
            match_number_value is not None
            and match_number_value <= 20
            and suffix_has_formula_signal
            and (
            _standalone_formula_block_signal_count(suffix) >= 2
            or _formula_relation_side_signal(suffix) >= 4
            )
        ):
            continue
        # Many PDFs keep the equation number inside the same text block and
        # immediately continue with prose such as "where ..." or "The ...".
        # Accept those when the prefix is already a strong math fragment.
        if suffix and not (prefix_has_formula_signal or suffix_has_formula_signal):
            continue
        return formatted_number
    return ""


def _looks_like_pdf_code_listing_record(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", _normalize_space(text or ""))
    if not normalized:
        return False
    if re.search(r"\b(?:props?|statev|dstran|stran|ddsdde)\s*\(\s*\d+\s*\)", normalized, re.IGNORECASE):
        return True
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\(\s*\d+\s*\)\s*=", normalized) and re.search(
        r"=\s*[^=]{0,80}\b[A-Za-z_][A-Za-z0-9_]*\s*\(\s*\d+\s*\)",
        normalized,
    ):
        return True
    if re.search(
        r"\b(?:subroutine|function|integer|double\s+precision|implicit\s+none|end\s+do)\b",
        normalized,
        re.IGNORECASE,
    ):
        return True
    if re.search(r"^\d+\s+[Cc]\s+\*{8,}", normalized):
        return True
    if normalized.count("*") >= 8 and re.search(r"\b[Cc]\b", normalized):
        return True
    return False


def _looks_like_formula_reference_tail(prefix: str) -> bool:
    normalized = unicodedata.normalize("NFKC", _normalize_space(prefix or ""))
    if not normalized:
        return False
    tail = normalized[-32:]
    if re.search(
        r"(?:如|见|由|利用|采用|根据|通过|按照|使用|结合)?式\s*$",
        tail,
    ):
        return True
    return bool(re.search(r"\b(?:eq\.?|equation)\s*$", tail, flags=re.IGNORECASE))


def _looks_like_author_affiliation_number_block(text: str) -> bool:
    normalized = _normalize_space(text)
    if not normalized:
        return False
    if _has_formula_relation(normalized) or _has_formula_structure(normalized):
        return False
    if len(PDF_EQUATION_NUMBER_TOKEN_RE.findall(normalized)) < 2:
        return False
    if re.search(r"[\(（ð]\s*\d+(?:[.:]\d+)*\s*[\)）Þ]\s*[a-z]\b", normalized) is None:
        return False
    word_hits = len(WORD_RE.findall(normalized))
    if word_hits < 4:
        return False
    person_name_hits = len(
        re.findall(
            r"\b[A-Z][A-Za-z'’-]+(?:\s+[A-Z]\.)?\s+[A-Z][A-Za-z'’-]+\b",
            normalized,
        )
    )
    return person_name_hits >= 2 or AUTHOR_AFFILIATION_RE.search(normalized) is not None


def _looks_like_figure_panel_dimension_number_block(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", _normalize_space(text or ""))
    if not normalized:
        return False
    panel_labels = re.findall(r"\(\s*\d{1,2}\s*\)", normalized)
    if len(panel_labels) < 2:
        return False
    dimension_assignments = re.findall(
        r"(?<![A-Za-z])(?:h|w|l|d|t|r|a|b|c)\s*={1,2}\s*"
        r"\d+(?:\.\d+)?\s*(?:mm|cm|um|μm|nm|m)\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if len(dimension_assignments) < 2:
        return False
    if re.search(r"\\[A-Za-z]+|[∑∏∫√∞∂∇]|[Α-Ωα-ω]", normalized):
        return False
    return True


def _looks_like_reference_issue_number_context(prefix: str, suffix: str) -> bool:
    if _has_formula_relation(prefix) or _has_formula_structure(prefix):
        return False
    normalized_prefix = _normalize_space(prefix)
    normalized_suffix = _normalize_space(suffix)
    if not re.search(r"(?:18|19|20)\d{2}\s*[,，]\s*$", normalized_prefix):
        return False
    if not re.match(r"^[:：]\s*\d+\s*[-–—~～]\s*\d+", normalized_suffix):
        return False
    return bool(
        re.search(r"(?:\[[A-Z]\]|［[A-Z]］|[Jj]\]\.?|[Mm]\]\.?|[Cc]\]\.?)", normalized_prefix)
        or CJK_CHAR_RE.search(normalized_prefix)
        or re.search(r"\b(?:journal|transactions|proceedings|magazine)\b", normalized_prefix, re.IGNORECASE)
    )


def _looks_like_bibliographic_issue_number_context(prefix: str, suffix: str) -> bool:
    normalized_prefix = _normalize_space(prefix)
    normalized_suffix = _normalize_space(suffix)
    if not re.search(r"\b\d{1,4}\s*$", normalized_prefix):
        return False
    if not re.match(r"^[,，]\s*\d+\s*[-–—]\s*\d+", normalized_suffix):
        return False
    if re.search(r"\((?:18|19|20)\d{2}\)", normalized_suffix):
        return True
    abbrev_hits = len(re.findall(r"\b[A-Z][A-Za-z]{1,12}\.", normalized_prefix))
    if (
        re.search(r"(?:18|19|20)\d{2}", normalized_prefix)
        and abbrev_hits >= 1
        and len(WORD_RE.findall(normalized_prefix)) >= 4
    ):
        return True
    if _has_formula_relation(prefix) or _has_formula_structure(prefix):
        return False
    return abbrev_hits >= 1


def _looks_like_bibliographic_issue_number_record(text: str, equation_number: str) -> bool:
    normalized = unicodedata.normalize("NFKC", _normalize_space(text or ""))
    normalized_number = _normalize_equation_number_token(equation_number).strip("()（）")
    if not normalized or not normalized_number:
        return False
    pattern = (
        rf"\b\d{{1,4}}\s*[\(（]\s*{re.escape(normalized_number)}\s*[\)）]"
        r"\s*[,，]\s*\d+\s*[-–—]\s*\d+"
    )
    match = re.search(pattern, normalized)
    if match is None:
        return False
    prefix = normalized[: match.start()]
    suffix = normalized[match.end() :]
    abbrev_hits = len(re.findall(r"\b[A-Z][A-Za-z]{1,14}\.", prefix))
    if re.search(r"\((?:18|19|20)\d{2}\)", suffix):
        return True
    if re.search(r"(?:18|19|20)\d{2}", prefix) and abbrev_hits >= 2 and len(WORD_RE.findall(prefix)) >= 4:
        return True
    if abbrev_hits >= 3 and re.search(r"\b(?:J|Journal|Proc|Proceedings|Trans|Transactions)\.?\b", prefix):
        return True
    return False


def _looks_like_reference_or_material_trailing_number(prefix: str, equation_number: str) -> bool:
    """Reject reference-list issue numbers and material grade suffixes misread as equations."""
    normalized = unicodedata.normalize("NFKC", _normalize_space(prefix or ""))
    if not normalized:
        return False
    abbreviation_hits = len(re.findall(r"\b[A-Z][A-Za-z]{1,14}\.", normalized))
    if (
        re.search(r"(?:18|19|20)\d{2}", normalized)
        and abbreviation_hits >= 2
        and len(WORD_RE.findall(normalized)) >= 4
        and re.search(r"\b\d{1,4}\s*$", normalized)
    ):
        return True
    math_signal_count = (
        len(MATH_SYMBOL_RE.findall(normalized))
        + len(PRIVATE_USE_MATH_GLYPH_RE.findall(normalized))
        + _math_alnum_char_count(normalized)
        + len(re.findall(r"[Α-Ωα-ω∑∏∫√∞∂∇∆]", normalized))
    )
    if (_has_formula_relation(normalized) or _has_formula_structure(normalized)) and math_signal_count >= 2:
        return False
    token = equation_number.strip("()")
    token_value = int(token) if token.isdigit() else None
    word_hits = len(WORD_RE.findall(normalized))
    starts_like_reference = re.match(r"^\s*(?:\[\d+\]|［\d+］|\d+\.)\s+", normalized) is not None
    if starts_like_reference and word_hits >= 3 and math_signal_count <= 2:
        return True
    bibliographic_keyword = re.search(
        r"\b(?:doi|https?|journal|proceedings|transactions|vol\.?|volume|issue|pp\.?|pages?)\b",
        normalized,
        re.IGNORECASE,
    )
    if bibliographic_keyword:
        if math_signal_count <= 2:
            return True
    if abbreviation_hits >= 2 and re.search(r"\b\d{1,4}\s*$", normalized) and math_signal_count <= 2:
        return True
    if re.search(r"(?:18|19|20)\d{2}\s*[,，]\s*\d{1,4}\s*$", normalized) and math_signal_count <= 2:
        return True
    if CJK_CHAR_RE.search(normalized) and math_signal_count <= 2:
        if re.search(r"(?:\[\d+\]|［\d+］|\b\d+\.|[Jj]\]|[Mm]\]|[Cc]\]|学报|期刊|工艺|力学|材料|出版社)", normalized):
            return True
    if re.search(r"\b(?:AA)?\d{4}-T\d[A-Z0-9]*$", normalized, re.IGNORECASE):
        return True
    if token_value is not None and token_value > 20 and re.search(
        r"\b(?:AA)?(?:20|60|70)\d{2}(?:-T\d[A-Z0-9]*)?$|"
        r"\b(?:Ti|Al|Mg|Weldox|Steel|Alloy)[-A-Za-z0-9]*$",
        normalized,
        re.IGNORECASE,
    ):
        return True
    if word_hits >= 8 and math_signal_count <= 2 and PROSE_SENTENCE_RE.search(normalized):
        return True
    return False


def _looks_like_dimensional_abbreviation_context(prefix: str, suffix: str, number: str) -> bool:
    normalized_number = _normalize_equation_number_token(number).upper()
    if normalized_number not in {"1D", "2D", "3D"}:
        return False
    combined = _normalize_space(f"{prefix} {suffix}")
    if _has_formula_relation(combined) or _has_formula_structure(combined):
        return False
    return bool(
        re.search(r"\b(?:one|two|three|[123])[-\s]?dimensional\b", combined, re.IGNORECASE)
        or re.search(
            r"\b(?:elasticity|plasticity|model|simulation|analysis|space|case|geometry)\b",
            combined,
            re.IGNORECASE,
        )
    )


def _looks_like_cjk_prose_enumeration_context(prefix: str, suffix: str) -> bool:
    normalized_prefix = _normalize_space(prefix)
    normalized_suffix = _normalize_space(suffix)
    if len(CJK_CHAR_RE.findall(normalized_prefix)) < 8:
        return False
    if len(CJK_CHAR_RE.findall(normalized_suffix[:24])) < 2:
        return False
    if re.match(r"^[\s,，;；:：.。]*[\u4e00-\u9fff]", normalized_suffix) is None:
        return False
    if re.search(r"[。.!！？?]\s*$", normalized_prefix):
        return True
    if re.search(r"(?:文献|研究|结果|误差|影响|参数|模型)", normalized_prefix[-80:]):
        math_signal_count = (
            len(MATH_SYMBOL_RE.findall(unicodedata.normalize("NFKC", normalized_prefix)))
            + _math_alnum_char_count(normalized_prefix)
            + len(PRIVATE_USE_MATH_GLYPH_RE.findall(normalized_prefix))
        )
        return math_signal_count <= 3
    return False


def _looks_like_embedded_prose_citation_context(prefix: str, suffix: str) -> bool:
    prefix_words = len(WORD_RE.findall(prefix))
    if prefix_words < 8:
        return False
    math_signal_count = (
        len(MATH_SYMBOL_RE.findall(prefix))
        + _math_alnum_char_count(prefix)
        + len(PRIVATE_USE_MATH_GLYPH_RE.findall(prefix))
    )
    if _has_formula_structure(prefix) and math_signal_count >= 4:
        return False
    prose_signal = (
        PROSE_SENTENCE_RE.search(prefix) is not None
        or PROSE_CUE_RE.search(prefix) is not None
        or INLINE_CITATION_RE.search(prefix) is not None
        or re.search(
            r"\b(?:which|where|given|because|respectively|therefore|thereby|"
            r"using|produced|reported|shown|called|corresponds?)\b",
            prefix,
            flags=re.IGNORECASE,
        )
        is not None
    )
    if not prose_signal:
        return False
    if math_signal_count <= max(3, prefix_words // 5):
        return True
    suffix_words = len(WORD_RE.findall(suffix))
    return suffix_words >= 4 and math_signal_count <= max(5, prefix_words // 4)


def _is_embedded_prose_reference_number(text: str, start: int) -> bool:
    if start <= 0:
        return False
    previous = text[start - 1]
    if previous.isspace() or previous in "=+-*/<>≤≥≈≠,;:，；：":
        return False
    if previous.isalnum():
        return True
    return bool(re.search(r"[A-Za-z\u4e00-\u9fff]{3,}$", text[:start]))


def _format_pdf_equation_number(number: str) -> str:
    number = _normalize_equation_number_token(number)
    if not re.fullmatch(EQUATION_NUMBER_PATTERN, number):
        return ""
    normalized = number
    if "." in normalized and normalized.split(".", 1)[0].isalpha():
        _prefix, normalized = normalized.split(".", 1)
    if normalized and normalized[-1].isalpha():
        normalized = normalized[:-1]
    parts = re.split(r"[.-]", normalized)
    if not all(part.isdigit() for part in parts):
        return ""
    if len(parts) == 1:
        value = int(parts[0])
        if value == 0 or value > 80:
            return ""
    elif int(parts[0]) == 0:
        return ""
    if "-" in normalized and len(parts) > 3:
        return ""
    if "-" in normalized:
        if "." in normalized:
            return ""
        hyphen_parts = normalized.split("-")
        if any(int(part) == 0 for part in hyphen_parts):
            return ""
        if int(hyphen_parts[0]) > 80:
            return ""
        if any(int(part) > 50 for part in hyphen_parts[1:]):
            return ""
    return _format_equation_number_token(number)


def _format_equation_number_token(number: str) -> str:
    normalized = _normalize_equation_number_token(number)
    if not normalized or not re.fullmatch(EQUATION_NUMBER_PATTERN, normalized):
        return ""
    return f"({normalized})"


def _normalize_equation_number_token(number: str) -> str:
    normalized = (number or "").strip().replace(":", ".")
    normalized = re.sub(r"[–—－−]+", "-", normalized)
    normalized = re.sub(r"\s+", "", normalized)
    return normalized.rstrip("-")


def _looks_like_split_equation_number_prefix(prefix: str, word_hits: int) -> bool:
    if not prefix:
        return True
    if len(prefix) > 18 or word_hits > 2:
        return False
    if re.fullmatch(r"[A-Za-z]{3,}", prefix) and not _looks_like_compact_formula_variable_prefix(prefix):
        return False
    bracket_chars = r"()[\]{}⎛⎜⎝⎞⎟⎠ቆቇ"
    if re.fullmatch(rf"[{re.escape(bracket_chars)}]+", prefix):
        return True
    if (
        any(char in bracket_chars for char in prefix)
        and re.fullmatch(rf"[{re.escape(bracket_chars)}0-9_*^¯˙∗+\-−/]+", prefix)
    ):
        return True
    if (
        any(char in bracket_chars for char in prefix)
        and any(char in prefix for char in "¯˙")
        and re.fullmatch(
            rf"[{re.escape(bracket_chars)}A-Za-zΑ-Ωα-ω0-9_*^¯˙∗+\-−/|]+",
            prefix,
        )
    ):
        return True
    letter_chars = r"A-Za-zＡ-Ｚａ-ｚΑ-Ωα-ω"
    if re.fullmatch(
        rf"[{re.escape(bracket_chars)}]*[{letter_chars}][{letter_chars}0-9０-９_*^¯˙∗+\-−/|]*"
        rf"(?:\([{letter_chars}0-9０-９_*^¯˙∗+\-−/|]+\))?[{re.escape(bracket_chars)}]*",
        prefix,
    ):
        return True
    digit_chars = r"0-9０-９"
    if re.fullmatch(rf"[{digit_chars}]+", prefix):
        if len(prefix) == 1:
            return True
        # Matrix brackets are often extracted as repeated glyph digits such as
        # "666664" or "777775" just before the equation number.
        return bool(re.fullmatch(r"[67]{2,}[45]", prefix))
    if re.fullmatch(
        rf"[{digit_chars}]+[{letter_chars}][{letter_chars}{digit_chars}_*^¯˙∗+\-−/|]*"
        rf"[{re.escape(bracket_chars)}]*",
        prefix,
    ):
        return True
    if not re.search(rf"[{letter_chars}_*^¯˙∗+\-−/|]", prefix):
        return False
    return bool(re.fullmatch(rf"[{letter_chars}{digit_chars}_*^¯˙∗+\-−/|]+", prefix))


def _looks_like_spaced_formula_variable_prefix(prefix: str) -> bool:
    normalized = (prefix or "").strip(" \t,.;:，。；：")
    groups = re.findall(r"[A-Za-zΑ-Ωα-ω]+", normalized)
    if not 2 <= len(groups) <= 4:
        return False
    if any(len(group) > 3 for group in groups):
        return False
    if any(group.casefold() in COMPACT_FORMULA_VARIABLE_PREFIX_STOPWORDS for group in groups):
        return False
    return bool(
        re.fullmatch(
            r"\s*[A-Za-zΑ-Ωα-ω]{1,3}(?:\s+[A-Za-zΑ-Ωα-ω]{1,3}){1,3}\s*",
            normalized,
        )
    )


def _looks_like_compact_formula_variable_prefix(prefix: str) -> bool:
    normalized = (prefix or "").strip().casefold()
    if normalized in COMPACT_FORMULA_VARIABLE_PREFIX_STOPWORDS:
        return False
    if normalized in COMPACT_FORMULA_VARIABLE_PREFIX_ALLOWLIST:
        return True
    if not re.fullmatch(r"[a-z]{3,8}", normalized):
        return False
    if re.search(r"(?:tion|ing|ment|ance|ence|ical|able|model|result)", normalized):
        return False
    return bool(re.search(r"[ijk].*[ijk]", normalized))


def _nearest_equation_number_index(
    candidate: FormulaCandidate,
    number_rows: list[tuple[str, float, float, bool]],
    used_indices: set[int],
) -> int | None:
    y_center = (candidate.bbox[1] + candidate.bbox[3]) / 2.0
    x_center = (candidate.bbox[0] + candidate.bbox[2]) / 2.0
    raw_candidates = _candidate_coordinate_values_to_pdf_space(candidate, y_center)
    x_candidates = _candidate_coordinate_values_to_pdf_space(candidate, x_center)
    for standalone in (False, True):
        match_index = _nearest_equation_number_index_for_kind(
            number_rows,
            used_indices,
            raw_candidates,
            x_candidates,
            standalone=standalone,
        )
        if match_index is not None:
            return match_index
    return None


def _nearest_equation_number_index_for_kind(
    number_rows: list[tuple[str, float, float, bool]],
    used_indices: set[int],
    y_candidates: list[float],
    x_candidates: list[float],
    *,
    standalone: bool,
) -> int | None:
    best_index: int | None = None
    best_score = float("inf")
    best_y_distance = float("inf")
    for index, (_number, number_y, number_x, is_standalone) in enumerate(number_rows):
        if index in used_indices or is_standalone != standalone:
            continue
        y_distance = min(abs(candidate_y - number_y) for candidate_y in y_candidates)
        x_distance = min(abs(candidate_x - number_x) for candidate_x in x_candidates)
        score = y_distance + min(x_distance, 260.0) * 0.06
        if score < best_score:
            best_score = score
            best_y_distance = y_distance
            best_index = index
    return best_index if best_y_distance <= 75.0 and best_score <= 90.0 else None


def _candidate_y_to_pdf_space(candidate: FormulaCandidate, y_value: float) -> float:
    if candidate.bbox_coordinate_space == "pdf":
        return y_value
    # MinerU content_list bboxes commonly use a 1000-pixel page height while
    # PyMuPDF text coordinates use PDF points. This heuristic is only used when
    # page-level counts differ, so exact numbering still prefers order matching.
    return y_value * 0.761


def _candidate_coordinate_values_to_pdf_space(candidate: FormulaCandidate, value: float) -> list[float]:
    if candidate.bbox_coordinate_space == "pdf":
        return [value]
    scaled = value * 0.761
    if candidate.bbox_coordinate_space in {"unknown", "image"}:
        return [value, scaled]
    return [scaled]


def _limit_candidates_per_page(
    candidates: list[FormulaCandidate],
    max_formulas_per_page: int,
) -> list[FormulaCandidate]:
    counts: dict[int, int] = {}
    limited: list[FormulaCandidate] = []
    for candidate in candidates:
        count = counts.get(candidate.page_num, 0)
        if count >= max_formulas_per_page:
            continue
        counts[candidate.page_num] = count + 1
        limited.append(candidate)
    return limited


def _limit_candidates_per_page_preserving_numbered(
    candidates: list[FormulaCandidate],
    max_formulas_per_page: int,
) -> list[FormulaCandidate]:
    counts: dict[int, int] = {}
    limited: list[FormulaCandidate] = []
    for candidate in candidates:
        if _equation_number_sequence_value(candidate.equation_number) is not None:
            limited.append(candidate)
            continue
        count = counts.get(candidate.page_num, 0)
        if count >= max_formulas_per_page:
            continue
        counts[candidate.page_num] = count + 1
        limited.append(candidate)
    return limited


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
    if _looks_like_non_formula_text(text):
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
        score += 0.18
    if width > 80 and height < 120:
        score += 0.08
    if _has_formula_relation(text):
        score += 0.15
    if word_hits > 14 and symbol_density < 0.08:
        score -= 0.25

    return max(0.0, min(score, 1.0))


def _latex_visible_text(latex: str) -> str:
    """Strip text-format wrappers so SimpleTex text-only output can be rejected."""
    visible = latex
    for _ in range(6):
        replaced = TEXT_FORMATTING_COMMAND_RE.sub(lambda match: match.group(1), visible)
        if replaced == visible:
            break
        visible = replaced
    visible = re.sub(r"\\(?:left|right)[()[\]{}|.]", " ", visible)
    visible = visible.replace("$", " ")
    return _normalize_space(visible)


def _looks_like_latex_prose_noise(latex: str, visible_text: str) -> bool:
    """Reject OCR outputs where prose is wrapped as LaTeX text blocks."""
    text_payloads = [
        _normalize_space(payload)
        for payload in PROSE_TEXT_COMMAND_RE.findall(latex)
        if _normalize_space(payload)
    ]
    if not text_payloads:
        return False

    prose_payload = _normalize_space(" ".join(text_payloads))
    if not prose_payload:
        return False
    if re.search(r"\bEq\.?\s*\d+\b|公式|式中", prose_payload, re.IGNORECASE):
        return True
    if NOISE_RE.search(prose_payload) or PROSE_CUE_RE.search(prose_payload):
        return True

    cjk_chars = len(CJK_CHAR_RE.findall(prose_payload))
    word_hits = len(WORD_RE.findall(prose_payload))
    payload_chars = len(re.sub(r"\s+", "", prose_payload))
    math_payload_symbols = len(MATH_SYMBOL_RE.findall(prose_payload))
    if cjk_chars >= 6 and math_payload_symbols < 3:
        return True
    if word_hits >= 8 and math_payload_symbols < 3:
        return True

    visible_chars = max(len(re.sub(r"\s+", "", visible_text)), 1)
    if payload_chars / visible_chars >= 0.45 and (cjk_chars >= 4 or word_hits >= 5):
        return True
    return False


def _looks_like_non_formula_text(text: str) -> bool:
    """Return True for non-formula PDF text blocks and publication metadata."""
    normalized = _normalize_space(text)
    if len(normalized) < 3:
        return True
    if NOISE_RE.search(normalized):
        return True
    if SECTION_HEADING_RE.match(normalized):
        return True
    if CAPTION_OR_REFERENCE_RE.search(normalized):
        return True
    if AUTHOR_AFFILIATION_RE.search(normalized):
        return True

    symbol_hits = len(MATH_SYMBOL_RE.findall(normalized))
    word_hits = len(WORD_RE.findall(normalized))
    cjk_chars = len(CJK_CHAR_RE.findall(normalized))
    symbol_density = symbol_hits / max(len(normalized), 1)
    has_relation = _has_formula_relation(normalized)
    has_structure = _has_formula_structure(normalized)
    has_math_command = bool(MATH_LATEX_COMMAND_RE.search(normalized))

    if has_math_command and has_relation and symbol_hits >= 4:
        return False
    if has_math_command and has_structure and symbol_hits >= 6 and word_hits <= 24:
        return False
    if PROSE_SENTENCE_RE.search(normalized):
        if word_hits > 8 or cjk_chars > 8:
            return True
        if not (has_math_command or has_relation):
            return True
    if has_relation and INLINE_CITATION_RE.search(normalized) and (word_hits >= 2 or cjk_chars >= 2):
        return True
    if has_relation and PROSE_CUE_RE.search(normalized):
        if word_hits >= 3 or cjk_chars >= 2:
            return True
    if has_math_command or has_relation:
        return False
    if has_structure and symbol_hits >= 2 and word_hits <= 12:
        return False
    if EQUATION_NUMBER_RE.search(normalized) and symbol_hits >= 2:
        return False
    if word_hits > 8:
        return True
    if word_hits > 4 and symbol_density < 0.04:
        return True
    return symbol_hits < 2


def _has_formula_relation(text: str) -> bool:
    normalized_text = unicodedata.normalize("NFKC", text or "")
    if _has_private_use_formula_relation(normalized_text):
        return True
    if re.search(
            r"(?:^|[\s({;,])(?:[\u0300-\u036f˙˚~ˆ^°\x00-\x1f]*\\?[A-Za-zΑ-Ωα-ω][A-Za-z0-9_{}^\\.'′]*|[)\]\d])"
            rf"\s*(?:=|¼|≈|≤|≥|≠|<|>|:=|\\leq?|\\geq?|\\approx|\\sim|{PRIVATE_USE_RELATION_RE})\s*"
            r"(?:[-+−]?\s*)?(?:\\?[A-Za-zΑ-Ωα-ω0-9]|[({]|\\)",
            normalized_text,
    ):
        return True
    return _has_complex_formula_relation(normalized_text)


def _has_private_use_formula_relation(text: str) -> bool:
    for match in re.finditer(PRIVATE_USE_RELATION_RE, text or ""):
        left = text[max(0, match.start() - 40):match.start()]
        right = text[match.end():match.end() + 40]
        if PRIVATE_USE_MATH_GLYPH_RE.search(left) and (
            PRIVATE_USE_MATH_GLYPH_RE.search(right)
            or re.search(r"[A-Za-zΑ-Ωα-ω0-9]", right)
        ):
            return True
    return False


def _has_complex_formula_relation(text: str) -> bool:
    for match in re.finditer(rf"(?:=|≈|≤|≥|≠|:=|\\leq?|\\geq?|\\approx|\\sim|{PRIVATE_USE_RELATION_RE})", text or ""):
        left = text[max(0, match.start() - 220):match.start()]
        right = text[match.end():match.end() + 220]
        if _formula_relation_side_signal(left) >= 4 and _formula_relation_side_signal(right) >= 4:
            return True
    return False


def _formula_relation_side_signal(text: str) -> int:
    normalized = text or ""
    command_hits = len(MATH_LATEX_COMMAND_RE.findall(normalized))
    greek_hits = len(re.findall(r"[Α-Ωα-ω]", normalized))
    structure_hits = len(re.findall(r"[_^{}]|[∑∏∫√∞∂∇]", normalized))
    math_alnum_hits = _math_alnum_char_count(normalized)
    single_var_hits = len(re.findall(r"(?<![A-Za-z\\])[A-Za-z](?![A-Za-z])", normalized))
    digit_hits = len(re.findall(r"\d", normalized))
    return (
        command_hits * 2
        + greek_hits
        + structure_hits
        + math_alnum_hits
        + min(single_var_hits, 4)
        + min(digit_hits, 2)
    )


def _has_formula_structure(text: str) -> bool:
    normalized_text = unicodedata.normalize("NFKC", text or "")
    return bool(
        re.search(r"(?:[_^{}]|[∑∏∫√∞∂∇]|[Α-Ωα-ω]|[A-Za-z]\s*/\s*[A-Za-z])", normalized_text)
    )


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
        duplicate_index = next(
            (index for index, existing in enumerate(kept) if _is_duplicate_candidate(candidate, existing)),
            None,
        )
        if duplicate_index is None:
            kept.append(candidate)
        elif _candidate_payload_score(candidate) > _candidate_payload_score(kept[duplicate_index]):
            kept[duplicate_index] = candidate
    return kept


def _candidate_payload_score(candidate: FormulaCandidate) -> tuple[int, int, float]:
    return (
        1 if candidate.latex.strip() else 0,
        1 if candidate.equation_number else 0,
        float(candidate.confidence or 0.0),
    )


def _merge_split_formula_candidates(candidates: list[FormulaCandidate]) -> list[FormulaCandidate]:
    """Merge vertically split display-equation records from layout parsers."""
    if len(candidates) < 2:
        return candidates
    ordered = sorted(candidates, key=lambda c: (c.page_num, c.bbox[1], c.bbox[0], -c.confidence))
    merged: list[FormulaCandidate] = []
    index = 0
    while index < len(ordered):
        current = ordered[index]
        index += 1
        while index < len(ordered) and _should_merge_split_formula_candidates(current, ordered[index]):
            current = _merge_two_formula_candidates(current, ordered[index])
            index += 1
        merged.append(current)
    return _merge_same_number_split_formula_candidates(merged)


def _merge_same_number_split_formula_candidates(candidates: list[FormulaCandidate]) -> list[FormulaCandidate]:
    merged: list[FormulaCandidate] = []
    for candidate in candidates:
        merge_index = next(
            (
                index for index, existing in enumerate(merged)
                if _should_merge_same_number_formula_candidates(existing, candidate)
            ),
            None,
        )
        if merge_index is None:
            merged.append(candidate)
        else:
            merged[merge_index] = _merge_two_formula_candidates(merged[merge_index], candidate)
    return merged


def _merge_number_only_pdf_candidates_with_latex_candidates(
    candidates: list[FormulaCandidate],
) -> list[FormulaCandidate]:
    """Move standalone PDF equation numbers onto nearby structured LaTeX formulas."""
    if len(candidates) < 2:
        return candidates
    merged = list(candidates)
    remove_indices: set[int] = set()
    assigned_formula_indices: set[int] = set()
    used_numbers = {
        candidate.equation_number
        for candidate in candidates
        if candidate.equation_number and not _is_number_only_pdf_candidate(candidate)
    }
    for number_index, number_candidate in enumerate(candidates):
        if not _is_number_only_pdf_candidate(number_candidate):
            continue
        if number_candidate.equation_number in used_numbers:
            continue
        best_index: int | None = None
        best_score = float("inf")
        best_y_distance = float("inf")
        for formula_index, formula_candidate in enumerate(candidates):
            if formula_index in assigned_formula_indices or formula_index == number_index:
                continue
            if formula_candidate.page_num != number_candidate.page_num:
                continue
            if formula_candidate.equation_number or not formula_candidate.latex.strip():
                continue
            if not _is_structured_cache_candidate(formula_candidate):
                continue
            if not _candidate_can_receive_inferred_equation_number(formula_candidate):
                continue
            score, y_distance = _number_only_pdf_to_latex_candidate_score(
                number_candidate,
                formula_candidate,
            )
            if score < best_score:
                best_score = score
                best_y_distance = y_distance
                best_index = formula_index
        if best_index is None or best_y_distance > 165.0 or best_score > 190.0:
            continue
        formula_candidate = merged[best_index]
        merged[best_index] = replace(
            formula_candidate,
            equation_number=number_candidate.equation_number,
            source=(
                formula_candidate.source
                if formula_candidate.source == number_candidate.source
                else f"{formula_candidate.source}+{number_candidate.source}"
            ),
        )
        remove_indices.add(number_index)
        assigned_formula_indices.add(best_index)
        used_numbers.add(number_candidate.equation_number)
    return [candidate for index, candidate in enumerate(merged) if index not in remove_indices]


def _split_multirow_independent_formula_candidates(candidates: list[FormulaCandidate]) -> list[FormulaCandidate]:
    """Split parser-collapsed array rows when each row is an independent equation."""
    split: list[FormulaCandidate] = []
    for candidate in candidates:
        rows = _independent_formula_rows_from_latex(candidate.latex)
        if len(rows) <= 1:
            split.append(candidate)
            continue
        x0, y0, x1, y1 = candidate.bbox
        row_height = max((y1 - y0) / len(rows), 1.0)
        for row_index, row in enumerate(rows):
            row_equation_number = _extract_equation_number(row) or (
                candidate.equation_number if row_index == 0 else ""
            )
            split.append(
                replace(
                    candidate,
                    bbox=(x0, y0 + row_height * row_index, x1, min(y1, y0 + row_height * (row_index + 1))),
                    raw_text=row,
                    equation_number=row_equation_number,
                    equation_number_status=(
                        candidate.equation_number_status if row_equation_number == candidate.equation_number else ""
                    ),
                    latex=row,
                    source=f"{candidate.source}_row",
                )
            )
    return split


def _independent_formula_rows_from_latex(latex: str) -> list[str]:
    cleaned = (latex or "").strip()
    if not cleaned:
        return []
    if not re.match(r"\\begin\s*\{\s*array\s*\}", cleaned) or not re.search(r"\\end\s*\{\s*array\s*\}", cleaned):
        return []
    inner = _outer_latex_array_body(cleaned)
    if not inner:
        return []
    raw_rows = [
        _normalize_independent_formula_row(part)
        for part in _split_top_level_latex_rows(inner)
    ]
    rows: list[str] = []
    for row in raw_rows:
        if not row:
            continue
        if rows and (
            _formula_row_likely_continuation(row)
            or not _formula_row_has_relation(row)
        ):
            rows[-1] = _normalize_space(f"{rows[-1]} {row}")
            continue
        rows.append(row)
    if len(rows) <= 1:
        return []
    if any(_formula_row_likely_continuation(row) for row in rows):
        return []
    if not all(_formula_row_has_relation(row) for row in rows):
        return []
    return rows


def _outer_latex_array_body(latex: str) -> str:
    begin_re = re.compile(r"\\begin\s*\{\s*array\s*\}\s*\{[^{}]*\}")
    end_re = re.compile(r"\\end\s*\{\s*array\s*\}")
    begin_match = begin_re.match(latex.strip())
    if begin_match is None:
        return ""
    depth = 0
    index = begin_match.end()
    body_start = index
    while index < len(latex):
        nested_begin = begin_re.match(latex, index)
        if nested_begin is not None:
            depth += 1
            index = nested_begin.end()
            continue
        end_match = end_re.match(latex, index)
        if end_match is not None:
            if depth == 0:
                return latex[body_start:index]
            depth -= 1
            index = end_match.end()
            continue
        index += 1
    return ""


def _split_top_level_latex_rows(latex: str) -> list[str]:
    return _split_top_level_latex_delimiter(latex, delimiter=r"\\")


def _split_top_level_alignment_cells(latex: str) -> list[str]:
    return _split_top_level_latex_delimiter(latex, delimiter="&")


def _split_top_level_latex_delimiter(latex: str, *, delimiter: str) -> list[str]:
    begin_re = re.compile(r"\\begin\s*\{\s*array\s*\}\s*\{[^{}]*\}")
    end_re = re.compile(r"\\end\s*\{\s*array\s*\}")
    parts: list[str] = []
    start = 0
    index = 0
    depth = 0
    while index < len(latex):
        nested_begin = begin_re.match(latex, index)
        if nested_begin is not None:
            depth += 1
            index = nested_begin.end()
            continue
        end_match = end_re.match(latex, index)
        if end_match is not None:
            depth = max(depth - 1, 0)
            index = end_match.end()
            continue
        if depth == 0 and latex.startswith(delimiter, index):
            parts.append(latex[start:index])
            index += len(delimiter)
            start = index
            continue
        index += 1
    parts.append(latex[start:])
    return parts


def _normalize_independent_formula_row(row: str) -> str:
    cells = [
        _strip_formula_row_wrappers(cell)
        for cell in _split_top_level_alignment_cells(row)
    ]
    cells = [_drop_style_only_formula_row(cell) for cell in cells]
    cells = [cell for cell in cells if cell]
    while cells and _formula_alignment_cell_likely_label(cells[-1]):
        cells.pop()
    if not cells:
        return ""
    return _normalize_space(" ".join(cells))


def _drop_style_only_formula_row(row: str) -> str:
    cleaned = _normalize_space(row)
    return "" if re.fullmatch(r"\\(?:displaystyle|textstyle|scriptstyle|scriptscriptstyle)", cleaned) else row


def _formula_alignment_cell_likely_label(cell: str) -> bool:
    if _extract_equation_number(cell):
        return True
    if _formula_row_has_relation(cell):
        return False
    visible_text = _normalize_space(_latex_visible_text(cell))
    if re.match(r"^(?:\+|-|−|=)", visible_text):
        return False
    if re.search(r"(?:\+|-|−|/|\^|\*|\\frac|\\sqrt|\\left|\\right)", visible_text):
        return False
    compact = re.sub(r"[^0-9A-Za-zΑ-Ωα-ω]", "", visible_text)
    return 0 < len(compact) <= 8


def _formula_row_likely_continuation(latex: str) -> bool:
    visible_text = _normalize_space(_latex_visible_text(latex))
    visible_text = re.sub(
        r"^\\(?:displaystyle|textstyle|scriptstyle|scriptscriptstyle)\s*",
        "",
        visible_text,
    )
    for _ in range(3):
        updated = re.sub(r"^(?:[{}]\s*)+", "", visible_text)
        updated = re.sub(r"^(?:\\(?:left|right)\s*\.\s*)+", "", updated)
        if updated == visible_text:
            break
        visible_text = updated
    return bool(re.match(r"^(?:=|\+|-|−|\\(?:left|right)\b)", visible_text))


def _formula_row_has_relation(latex: str) -> bool:
    visible_text = _latex_visible_text(latex)
    return _has_formula_relation(visible_text) or (
        "=" in visible_text and _has_formula_structure(visible_text)
    )


def _strip_formula_row_wrappers(row: str) -> str:
    stripped = _normalize_space(row)
    for _ in range(4):
        candidate = stripped.strip()
        if _outer_braces_wrap(candidate):
            stripped = _normalize_space(candidate[1:-1])
            continue
        break
    return stripped


def _outer_braces_wrap(text: str) -> bool:
    if not text.startswith("{") or not text.endswith("}"):
        return False
    depth = 0
    for index, char in enumerate(text):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and index != len(text) - 1:
                return False
        if depth < 0:
            return False
    return depth == 0


def _is_number_only_pdf_candidate(candidate: FormulaCandidate) -> bool:
    if not candidate.source.startswith("pdf_text_equation_number"):
        return False
    if candidate.latex.strip() or not candidate.equation_number:
        return False
    raw_text = _normalize_space(candidate.raw_text)
    number = re.escape(candidate.equation_number.strip("()"))
    return bool(re.fullmatch(rf"[\s()（）ðÞ]*{number}[\s()（）ðÞ]*", raw_text))


def _number_only_pdf_to_latex_candidate_score(
    number_candidate: FormulaCandidate,
    formula_candidate: FormulaCandidate,
) -> tuple[float, float]:
    number_y = (number_candidate.bbox[1] + number_candidate.bbox[3]) / 2.0
    number_x = (number_candidate.bbox[0] + number_candidate.bbox[2]) / 2.0
    formula_y = (formula_candidate.bbox[1] + formula_candidate.bbox[3]) / 2.0
    formula_x = (formula_candidate.bbox[0] + formula_candidate.bbox[2]) / 2.0
    y_values = _candidate_coordinate_values_to_pdf_space(formula_candidate, formula_y)
    x_values = _candidate_coordinate_values_to_pdf_space(formula_candidate, formula_x)
    y_distance = min(abs(value - number_y) for value in y_values)
    x_distance = min(abs(value - number_x) for value in x_values)
    score = y_distance + min(x_distance, 300.0) * 0.08
    return score, y_distance


def _should_merge_split_formula_candidates(first: FormulaCandidate, second: FormulaCandidate) -> bool:
    if first.page_num != second.page_num:
        return False
    if _is_independent_formula_row_candidate(first) or _is_independent_formula_row_candidate(second):
        return False
    if not first.latex.strip() or not second.latex.strip():
        return False
    if not (_is_valid_bbox(first.bbox) and _is_valid_bbox(second.bbox)):
        return False
    vertical_gap = second.bbox[1] - first.bbox[3]
    if vertical_gap < -8.0:
        return False
    first_width = max(0.0, first.bbox[2] - first.bbox[0])
    second_width = max(0.0, second.bbox[2] - second.bbox[0])
    overlap = max(0.0, min(first.bbox[2], second.bbox[2]) - max(first.bbox[0], second.bbox[0]))
    if min(first_width, second_width) <= 0 or overlap / min(first_width, second_width) < 0.42:
        return False
    if first.equation_number and second.equation_number:
        return _should_merge_same_number_formula_candidates(first, second)
    if (
        first.equation_number
        and not second.equation_number
        and vertical_gap <= 72.0
        and _latex_second_lhs_is_used_by_first_rhs(first.latex, second.latex)
    ):
        return True
    if (
        first.equation_number
        and not second.equation_number
        and vertical_gap <= 16.0
        and _latex_likely_indented_sibling_definition(second.latex)
    ):
        return True
    if (
        second.equation_number
        and not first.equation_number
        and vertical_gap <= 72.0
        and _latex_second_lhs_is_used_by_first_rhs(first.latex, second.latex)
    ):
        return True
    if vertical_gap > 28.0:
        return False
    if first.equation_number and not second.equation_number and _latex_likely_definition_continuation(second.latex):
        return True
    return _latex_likely_continues(first.latex) or _latex_likely_continuation(second.latex)


def _is_independent_formula_row_candidate(candidate: FormulaCandidate) -> bool:
    return candidate.source.endswith("_row")


def _should_merge_same_number_formula_candidates(first: FormulaCandidate, second: FormulaCandidate) -> bool:
    if (
        first.page_num != second.page_num
        or not first.equation_number
        or first.equation_number != second.equation_number
        or not first.latex.strip()
        or not second.latex.strip()
        or not (_is_valid_bbox(first.bbox) and _is_valid_bbox(second.bbox))
    ):
        return False
    first_width = max(0.0, first.bbox[2] - first.bbox[0])
    second_width = max(0.0, second.bbox[2] - second.bbox[0])
    overlap = max(0.0, min(first.bbox[2], second.bbox[2]) - max(first.bbox[0], second.bbox[0]))
    if min(first_width, second_width) <= 0 or overlap / min(first_width, second_width) < 0.42:
        return False
    vertical_gap = max(first.bbox[1], second.bbox[1]) - min(first.bbox[3], second.bbox[3])
    return vertical_gap <= 96.0


def _latex_likely_continues(latex: str) -> bool:
    cleaned = (latex or "").strip()
    if not cleaned:
        return False
    if cleaned.count("{") - cleaned.count("}") >= 2:
        return True
    return bool(re.search(r"(?:\\left|\+|-|=|,|;|\\frac\s*\{[^{}]*\}\s*)$", cleaned))


def _latex_likely_continuation(latex: str) -> bool:
    cleaned = (latex or "").strip()
    if not cleaned:
        return False
    head = cleaned[:120]
    if _has_formula_relation(head):
        return False
    return bool(re.match(r"^(?:\\left|\\right|\\big|\\Big|\\bigg|\\Bigg|\[|\]|\)|\}|,|;)", head))


def _latex_likely_definition_continuation(latex: str) -> bool:
    cleaned = (latex or "").strip()
    if not cleaned or LATEX_TAG_RE.search(cleaned):
        return False
    visible_text = _latex_visible_text(cleaned)
    if ":" not in visible_text and r"\colon" not in cleaned:
        return False
    return r"\mathrm" in cleaned and len(WORD_RE.findall(visible_text)) >= 6


def _latex_likely_indented_sibling_definition(latex: str) -> bool:
    cleaned = (latex or "").strip()
    if not re.match(r"^\\(?:q?quad|hspace)\b", cleaned):
        return False
    visible_text = _latex_visible_text(cleaned)
    return _has_formula_relation(visible_text) or ("=" in visible_text and _has_formula_structure(visible_text))


def _latex_second_lhs_is_used_by_first_rhs(first_latex: str, second_latex: str) -> bool:
    first_rhs = _latex_rhs(first_latex)
    second_lhs = _latex_lhs(second_latex)
    if not first_rhs or not second_lhs:
        return False
    lhs_tokens = _formula_match_tokens(second_lhs)
    rhs_tokens = _formula_match_tokens(first_rhs)
    if not lhs_tokens or not rhs_tokens or len(lhs_tokens) > 8:
        return False
    shared = sum(1 for token in lhs_tokens if token in rhs_tokens)
    if len(lhs_tokens) <= 2:
        return shared == len(lhs_tokens)
    return shared / len(lhs_tokens) >= 0.75


def _latex_lhs(latex: str) -> str:
    parts = (latex or "").split("=", 1)
    return parts[0] if len(parts) == 2 else ""


def _latex_rhs(latex: str) -> str:
    parts = (latex or "").split("=", 1)
    return parts[1] if len(parts) == 2 else ""


def _merge_two_formula_candidates(first: FormulaCandidate, second: FormulaCandidate) -> FormulaCandidate:
    bbox = (
        min(first.bbox[0], second.bbox[0]),
        min(first.bbox[1], second.bbox[1]),
        max(first.bbox[2], second.bbox[2]),
        max(first.bbox[3], second.bbox[3]),
    )
    confidences = [value for value in (first.confidence, second.confidence) if value is not None]
    source = first.source if first.source == second.source else f"{first.source}+{second.source}"
    return replace(
        first,
        bbox=bbox,
        raw_text="\n".join(part for part in (first.raw_text, second.raw_text) if part.strip()),
        confidence=min(confidences) if confidences else first.confidence,
        equation_number=first.equation_number or second.equation_number,
        source=source,
        latex="\n".join(part for part in (first.latex.strip(), second.latex.strip()) if part),
    )


def _sort_formula_candidates_for_review(candidates: list[FormulaCandidate]) -> list[FormulaCandidate]:
    return sorted(candidates, key=_formula_candidate_review_sort_key)


def _formula_candidate_review_sort_key(candidate: FormulaCandidate) -> tuple:
    number_key = _equation_number_review_sort_key(candidate.equation_number)
    if number_key is not None:
        return (0, *number_key, candidate.page_num, candidate.bbox[1], candidate.bbox[0])
    return (1, candidate.page_num, candidate.bbox[1], candidate.bbox[0])


def _equation_number_review_sort_key(equation_number: str) -> tuple | None:
    match = re.fullmatch(rf"\((?P<number>{EQUATION_NUMBER_PATTERN})\)", equation_number or "")
    if match is None:
        return None
    number = match.group("number")
    prefix = ""
    if "." in number and number.split(".", 1)[0].isalpha():
        prefix, number = number.split(".", 1)
    suffix = ""
    if number and number[-1].isalpha():
        suffix = number[-1]
        number = number[:-1]
    parts = tuple(int(part) for part in number.split(".") if part.isdigit())
    if not parts:
        return None
    return (1, prefix, parts, suffix) if prefix else (0, "", parts, suffix)


def _is_duplicate_candidate(candidate: FormulaCandidate, existing: FormulaCandidate) -> bool:
    candidate_latex = candidate.latex.strip()
    existing_latex = existing.latex.strip()
    if (
        candidate_latex
        and existing_latex
        and bool(candidate.equation_number) != bool(existing.equation_number)
        and _looks_like_near_duplicate_latex(candidate_latex, existing_latex)
    ):
        return True
    if candidate.page_num != existing.page_num:
        return False
    if candidate.equation_number and candidate.equation_number == existing.equation_number:
        return True
    if candidate.equation_number and existing.equation_number:
        return False
    if (
        candidate_latex
        and existing_latex
        and _normalize_latex_for_duplicate(candidate_latex) == _normalize_latex_for_duplicate(existing_latex)
    ):
        return True
    if _is_valid_bbox(candidate.bbox) and _is_valid_bbox(existing.bbox):
        return _bbox_iou(candidate.bbox, existing.bbox) >= 0.7
    return bool(candidate.raw_text and _normalize_space(candidate.raw_text) == _normalize_space(existing.raw_text))


def _normalize_latex_for_duplicate(latex: str) -> str:
    return re.sub(r"\s+", "", LATEX_TAG_RE.sub("", latex))


def _looks_like_near_duplicate_latex(first: str, second: str) -> bool:
    first_normalized = _normalize_latex_for_duplicate(first)
    second_normalized = _normalize_latex_for_duplicate(second)
    if len(first_normalized) < 60 or len(second_normalized) < 60:
        return False
    short, long = sorted((first_normalized, second_normalized), key=len)
    if short in long:
        return True
    return SequenceMatcher(a=first_normalized, b=second_normalized).ratio() >= 0.92


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
    tag_match = LATEX_TAG_RE.search(raw_text)
    if tag_match is not None:
        tag = tag_match.group("tag")
        return _format_equation_number_token(tag) if tag else ""
    match = EQUATION_NUMBER_RE.search(raw_text)
    if match is None:
        match = TRAILING_EQUATION_NUMBER_RE.search(raw_text)
        if match is None or _looks_like_non_formula_text(raw_text):
            return ""
        number = match.group("number")
        return _format_equation_number_token(number) if number else ""
    number = match.group("eq") or match.group("tail")
    if match.group("tail") and _looks_like_non_formula_text(raw_text):
        return ""
    return _format_equation_number_token(number) if number else ""


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
