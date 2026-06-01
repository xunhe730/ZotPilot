"""ZotPilot deep-reading annotator (Phase 1).

Deterministic logic for the /ztp-tutor feature: candidate-generating placement,
held-page idempotent annotation writes, backup + atomic-swap orchestration.

See .omc/plans/ztp-tutor.md for the full design and invariants. The LLM/skill
layer only orchestrates; every byte that touches the user's PDF goes through
this module.
"""
from __future__ import annotations

import logging
import math
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pymupdf

from ..feature_extraction.postprocessors.cell_cleaning import _normalize_ligatures
from ..state import ToolError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ZOTPILOT_MARKER = "ZotPilot 导读"

DIMENSION_RGB: dict[str, tuple[float, float, float]] = {
    "thesis": (1.0, 0.83, 0.0),
    "concept": (1.0, 0.4, 0.4),
    "evidence": (0.18, 0.66, 0.9),
    "rebuttal": (0.37, 0.7, 0.21),
    "method": (0.64, 0.54, 0.9),
}

# Reverse ligature map for the ADDITIONAL candidate (RM-1).
# Apply longest-first (ffi/ffl before ff/fi/fl) to avoid partial replacements.
_REVERSE_LIGATURE_MAP: dict[str, str] = {
    "ffi": "ﬃ",
    "ffl": "ﬄ",
    "ff": "ﬀ",
    "fi": "ﬁ",
    "fl": "ﬂ",
}

# Token caps (M3) — boundary-enforced.
MAX_COMMENT_BYTES = 500
MAX_QUOTE_BYTES = 1000
MAX_ANNOTATION_COUNT = 200
MAX_OVERVIEW_BYTES = 2000

MIN_QUOTE_LEN = 12

# Geometry tolerances (§7.10 / §7.11).
_BBOX_DEDUP_TOL = 1.5
_REGION_ICON_SIZE = 16.0
_REGION_NEIGHBORHOOD = 24.0
_IOU_SKIP_THRESHOLD = 0.5

_VALID_DIMENSIONS = frozenset(DIMENSION_RGB.keys())
_VALID_KINDS = frozenset({"highlight", "region"})


# ---------------------------------------------------------------------------
# Exceptions and dataclasses
# ---------------------------------------------------------------------------


class ScannedPdfError(Exception):
    """Raised when a PDF has no extractable text layer."""


@dataclass(frozen=True)
class AnnotationSpec:
    quote: str
    dimension: str
    comment: str
    page_hint: int | None = None
    kind: Literal["highlight", "region"] = "highlight"
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    subtype: str | None = None


@dataclass(frozen=True)
class PlacementReport:
    placed: tuple[AnnotationSpec, ...]
    unplaced: tuple[tuple[str, str], ...]
    overview_placed: bool
    backup_path: str
    page_count: int
    file_size_before: int
    file_size_after: int
    verified: bool
    verification_details: dict
    coverage: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExistingAnnot:
    page_num: int  # 1-based
    kind: str
    rect: tuple[float, float, float, float]
    color: tuple[float, ...] | None
    content: str
    comment: str


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def map_dimension_to_rgb(dimension: str) -> tuple[float, float, float]:
    try:
        return DIMENSION_RGB[dimension]
    except KeyError:
        raise ToolError(
            f"unknown dimension {dimension!r}; expected one of {sorted(_VALID_DIMENSIONS)}"
        ) from None


_UNICODE_QUOTE_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-",
    " ": " ",
}
_WHITESPACE_RE = re.compile(r"\s+")
_HYPHEN_LINEBREAK_RE = re.compile(r"(\w)-\s*\n\s*(\w)")

# pymupdf4llm emits Markdown into page text (bold/code/headers/list markers).
# The LLM quotes that text verbatim, but page.search_for() queries the raw PDF
# text layer, which has none of these markers -> no_match. Strip them so a quote
# like "**Figure 3:** Velocity fields" matches "Figure 3: Velocity fields" in
# the PDF. Only DOUBLE emphasis (**, __) and backticks are removed; single _ / *
# are left intact to avoid corrupting snake_case identifiers common in papers.
_MD_EMPHASIS_RE = re.compile(r"\*\*|__|`")
_MD_LEADING_MARKER_RE = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)")


def normalize_quote_for_pdf(text: str) -> str:
    """One-way, idempotent normalization for placement matching.

    - Unicode normalize (NFKC)
    - Strip Markdown emphasis (**, __, `) and leading block markers (headers, lists)
    - Collapse Unicode quotes/dashes to ASCII
    - De-hyphenate line breaks: "meth-\nod" -> "method"
    - Apply ligature map (ﬁ→fi etc.)
    - Collapse whitespace
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    out = _HYPHEN_LINEBREAK_RE.sub(r"\1\2", out)
    out = _MD_LEADING_MARKER_RE.sub("", out)
    out = _MD_EMPHASIS_RE.sub("", out)
    for k, v in _UNICODE_QUOTE_MAP.items():
        out = out.replace(k, v)
    out = _normalize_ligatures(out)
    out = _WHITESPACE_RE.sub(" ", out).strip()
    return out


def re_ligate_quote(text: str) -> str:
    """Reverse the ligature map, longest substring first (RM-1).

    Produces an ADDITIONAL search candidate; never the sole query.
    """
    if not text:
        return ""
    out = text
    for plain in sorted(_REVERSE_LIGATURE_MAP.keys(), key=len, reverse=True):
        out = out.replace(plain, _REVERSE_LIGATURE_MAP[plain])
    return out


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def validate_annotation_specs(
    annotations: list[AnnotationSpec],
    overview: dict | None,
    *,
    page_count: int | None = None,
) -> None:
    """Boundary validation (M3 + §7.5)."""
    if not isinstance(annotations, list):
        raise ToolError("annotations must be a list")
    if len(annotations) > MAX_ANNOTATION_COUNT:
        raise ToolError(
            f"annotation count {len(annotations)} exceeds cap {MAX_ANNOTATION_COUNT}"
        )
    for i, spec in enumerate(annotations):
        if not isinstance(spec, AnnotationSpec):
            raise ToolError(f"annotations[{i}] is not an AnnotationSpec")
        if spec.dimension not in _VALID_DIMENSIONS:
            raise ToolError(
                f"annotations[{i}].dimension={spec.dimension!r} not in {sorted(_VALID_DIMENSIONS)}"
            )
        if spec.kind not in _VALID_KINDS:
            raise ToolError(f"annotations[{i}].kind={spec.kind!r} not in {sorted(_VALID_KINDS)}")
        if _utf8_len(spec.quote) > MAX_QUOTE_BYTES:
            raise ToolError(
                f"annotations[{i}] quote exceeds {MAX_QUOTE_BYTES} bytes "
                "(long quote suggests full-text echo)"
            )
        if _utf8_len(spec.comment) > MAX_COMMENT_BYTES:
            raise ToolError(
                f"annotations[{i}] comment exceeds {MAX_COMMENT_BYTES} bytes"
            )
        if spec.kind == "region":
            if spec.page is None or spec.bbox is None:
                raise ToolError(
                    f"annotations[{i}] kind='region' requires non-null page and bbox"
                )
            if page_count is not None and not (1 <= spec.page <= page_count):
                raise ToolError(
                    f"annotations[{i}].page={spec.page} out of range [1,{page_count}]"
                )
            bbox = spec.bbox
            if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
                raise ToolError(f"annotations[{i}].bbox must be 4 finite floats")
            x0, y0, x1, y1 = bbox
            if not (x1 > x0 and y1 > y0):
                raise ToolError(
                    f"annotations[{i}].bbox inverted/degenerate: {bbox}"
                )
    if overview is not None:
        if not isinstance(overview, dict):
            raise ToolError("overview must be a dict or None")
        text = build_overview_text(overview)
        if _utf8_len(text) > MAX_OVERVIEW_BYTES:
            raise ToolError(
                f"overview exceeds {MAX_OVERVIEW_BYTES} bytes (got {_utf8_len(text)})"
            )


def build_overview_text(overview: dict) -> str:
    """Compose CJK overview: thesis + 5-element skeleton + strongest/weakest."""
    if not overview:
        return ""
    thesis = str(overview.get("thesis", "")).strip()
    skel = overview.get("skeleton") or {}
    if not isinstance(skel, dict):
        skel = {}
    parts = [f"【核心论点】{thesis}"] if thesis else []
    label_keys = (
        ("question", "问题"),
        ("claim", "论点"),
        ("evidence", "证据"),
        ("rebuttal", "反驳"),
        ("conclusion", "结论"),
    )
    for key, label in label_keys:
        v = str(skel.get(key, "")).strip()
        if v:
            parts.append(f"{label}：{v}")
    strongest = str(overview.get("strongest", "")).strip()
    weakest = str(overview.get("weakest", "")).strip()
    if strongest:
        parts.append(f"最强：{strongest}")
    if weakest:
        parts.append(f"最弱：{weakest}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Preflight / backup / scanned guard
# ---------------------------------------------------------------------------


def preflight_write_access(pdf_path: Path) -> None:
    """RM-3: exists + parent.is_dir + W_OK file + W_OK parent.

    `os.path.ismount` only goes in the structured log line.
    """
    pdf_path = Path(pdf_path)
    parent = pdf_path.parent
    is_mount = False
    try:
        is_mount = os.path.ismount(parent)
    except Exception:
        is_mount = False
    if not pdf_path.exists():
        raise ToolError(f"preflight: PDF not found: {pdf_path}")
    if not parent.is_dir():
        raise ToolError(f"preflight: parent is not a directory: {parent}")
    if not os.access(pdf_path, os.W_OK):
        raise ToolError(f"preflight: file not writable: {pdf_path}")
    if not os.access(parent, os.W_OK):
        raise ToolError(f"preflight: parent dir not writable: {parent}")
    logger.info(
        "preflight ok pdf=%s parent=%s parent_ismount=%s",
        pdf_path,
        parent,
        is_mount,
    )


def backup_pdf(pdf_path: Path) -> Path:
    """copy2 → <pdf>.ztpbak, verify size; never clobber existing backup."""
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise ToolError(f"backup: source not found: {pdf_path}")
    bak = pdf_path.with_suffix(pdf_path.suffix + ".ztpbak")
    if bak.exists():
        logger.info("backup: preserving existing .ztpbak path=%s", bak)
        return bak
    shutil.copy2(pdf_path, bak)
    src_size = pdf_path.stat().st_size
    bak_size = bak.stat().st_size
    if bak_size == 0 or bak_size != src_size:
        raise ToolError(
            f"backup: size mismatch (src={src_size}, bak={bak_size})"
        )
    logger.info("backup ok path=%s bytes=%d", bak, bak_size)
    return bak


def has_text_layer(pdf_path: Path) -> bool:
    """Return True if at least one page yields non-whitespace text."""
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as e:
        raise ToolError(f"cannot open PDF: {pdf_path}: {e}") from e
    try:
        for i in range(doc.page_count):
            page = doc[i]
            txt = page.get_text("text") or ""
            if txt.strip():
                return True
        return False
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Existing-annotation scan (§7.11)
# ---------------------------------------------------------------------------


def read_existing_annotations(doc) -> list[ExistingAnnot]:
    """Scan all foreign annotations (non-ZotPilot-marked) page by page.

    Wraps per-annot access in try/except — degenerate annots are skipped,
    never abort the run (7F).
    """
    out: list[ExistingAnnot] = []
    page_count = doc.page_count
    for pno in range(page_count):
        try:
            page = doc[pno]
        except Exception:
            continue
        try:
            annots = list(page.annots() or [])
        except Exception:
            continue
        for a in annots:
            try:
                info = a.info or {}
                title = info.get("title", "") or ""
                if title.startswith(ZOTPILOT_MARKER):
                    continue
                rect = a.rect
                rect_t = (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
                kind_name = ""
                try:
                    kind_name = a.type[1] if a.type else ""
                except Exception:
                    kind_name = ""
                color = None
                try:
                    cols = a.colors or {}
                    stroke = cols.get("stroke")
                    if stroke is not None:
                        color = tuple(float(v) for v in stroke)
                except Exception:
                    color = None
                content = info.get("content", "") or ""
                comment = info.get("subject", "") or ""
                out.append(
                    ExistingAnnot(
                        page_num=pno + 1,
                        kind=str(kind_name).lower(),
                        rect=rect_t,
                        color=color,
                        content=content,
                        comment=comment,
                    )
                )
            except Exception:
                # 7F: degenerate annot — skip
                continue
    return out


# ---------------------------------------------------------------------------
# Clear (idempotency)
# ---------------------------------------------------------------------------


def clear_zotpilot_annotations(doc, *, marker: str = ZOTPILOT_MARKER) -> int:
    """Per held page, snapshot marker annots first, THEN delete (RC-2)."""
    removed = 0
    for pno in range(doc.page_count):
        page = doc[pno]
        try:
            annots = list(page.annots() or [])
        except Exception:
            continue
        targets = []
        for a in annots:
            try:
                title = (a.info or {}).get("title", "") or ""
                if title.startswith(marker):
                    targets.append(a)
            except Exception:
                continue
        for a in targets:
            try:
                page.delete_annot(a)
                removed += 1
            except Exception:
                continue
    return removed


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _quad_bbox(quad) -> tuple[float, float, float, float]:
    """Return enclosing bbox of a Quad object."""
    try:
        r = quad.rect
        return (float(r.x0), float(r.y0), float(r.x1), float(r.y1))
    except Exception:
        # fitz Quad always exposes .rect; fallback for raw tuples
        pass
    try:
        # quad has ul/ur/ll/lr Point
        xs = [quad.ul.x, quad.ur.x, quad.ll.x, quad.lr.x]
        ys = [quad.ul.y, quad.ur.y, quad.ll.y, quad.lr.y]
        return (min(xs), min(ys), max(xs), max(ys))
    except Exception:
        return (0.0, 0.0, 0.0, 0.0)


def _bbox_close(b1: tuple[float, float, float, float],
                b2: tuple[float, float, float, float],
                tol: float = _BBOX_DEDUP_TOL) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(b1, b2))


def _dedupe_quads(quads: list) -> list:
    """De-duplicate a list of Quads/rects by bbox tolerance."""
    out = []
    seen_bboxes: list[tuple[float, float, float, float]] = []
    for q in quads:
        bb = _quad_bbox(q)
        if any(_bbox_close(bb, sb) for sb in seen_bboxes):
            continue
        seen_bboxes.append(bb)
        out.append(q)
    return out


def _rect_iou(a: tuple[float, float, float, float],
              b: tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    iw = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    ih = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = iw * ih
    aa = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    bb = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = aa + bb - inter
    if union <= 0:
        return 0.0
    return inter / union


def _quads_union_bbox(quads: list) -> tuple[float, float, float, float]:
    bbs = [_quad_bbox(q) for q in quads]
    x0 = min(b[0] for b in bbs)
    y0 = min(b[1] for b in bbs)
    x1 = max(b[2] for b in bbs)
    y1 = max(b[3] for b in bbs)
    return (x0, y0, x1, y1)


def _point_near_rect(point: tuple[float, float],
                     rect: tuple[float, float, float, float],
                     dist: float) -> bool:
    px, py = point
    rx0, ry0, rx1, ry1 = rect
    dx = max(rx0 - px, 0.0, px - rx1)
    dy = max(ry0 - py, 0.0, py - ry1)
    return math.hypot(dx, dy) <= dist


# ---------------------------------------------------------------------------
# Word-index fallback (RL-2)
# ---------------------------------------------------------------------------


def _build_word_index(page) -> tuple[str, list[tuple[int, tuple[float, float, float, float]]]]:
    """Return (normalized_stream, per_char_table).

    per_char_table[i] = (word_no, original_bbox) for the i-th char of the stream.
    A trailing space after each word maps to (-1, none-bbox) sentinel meaning
    "separator". This lets de-hyphenated/merged tokens still recover the
    UNION of contributing bboxes.

    De-hyphenation across word entries (RL-2): when a word ends with '-' and
    the next word lies on a different row (or block-break), the trailing
    hyphen is dropped AND no separator space is emitted, so e.g. ("meth-",
    "od") in the raw stream becomes "method" in the normalized stream.
    """
    words = list(page.get_text("words") or [])
    norm_chars: list[str] = []
    char_to_word: list[tuple[int, tuple[float, float, float, float]] | None] = []
    for idx, w in enumerate(words):
        # (x0,y0,x1,y1,word,block,line,word_no)
        x0, y0, x1, y1, raw, _b, _l, wn = w
        bbox = (float(x0), float(y0), float(x1), float(y1))
        norm = normalize_quote_for_pdf(raw)
        if not norm:
            continue
        # Look ahead: if this word ends with '-' and the next word is on a
        # different row, drop the trailing '-' and skip the separator.
        next_w = words[idx + 1] if idx + 1 < len(words) else None
        joins_next = False
        if next_w is not None and norm.endswith("-"):
            ny0 = float(next_w[1])
            if abs(ny0 - float(y0)) > 2.0:
                joins_next = True
                norm = norm[:-1]
        for ch in norm:
            norm_chars.append(ch)
            char_to_word.append((int(wn), bbox))
        if not joins_next:
            norm_chars.append(" ")
            char_to_word.append(None)
    stream = "".join(norm_chars)
    return stream, [c if c is not None else (-1, (0.0, 0.0, 0.0, 0.0))
                    for c in char_to_word]


def _word_index_find(page, normalized_quote: str) -> list[tuple[float, float, float, float]] | None:
    """Return list of rects covering the quote span via the word index.

    On miss returns None. On hit returns one or more rects (one per
    line/segment) representing the UNION of contributing word bboxes
    grouped by y-row.
    """
    if not normalized_quote:
        return None
    stream, table = _build_word_index(page)
    if not stream:
        return None
    # Find first occurrence
    idx = stream.find(normalized_quote)
    if idx < 0:
        return None
    end = idx + len(normalized_quote)
    # Collect contributing word bboxes (skip separator sentinels with word_no=-1)
    seen: dict[int, tuple[float, float, float, float]] = {}
    order: list[int] = []
    for i in range(idx, end):
        wn, bb = table[i]
        if wn < 0:
            continue
        if wn not in seen:
            seen[wn] = bb
            order.append(wn)
    if not seen:
        return None
    # Group by row (y0 within 2pt tolerance)
    rows: list[list[tuple[float, float, float, float]]] = []
    for wn in order:
        bb = seen[wn]
        placed = False
        for row in rows:
            if abs(row[0][1] - bb[1]) < 2.0:
                row.append(bb)
                placed = True
                break
        if not placed:
            rows.append([bb])
    rects = []
    for row in rows:
        x0 = min(b[0] for b in row)
        y0 = min(b[1] for b in row)
        x1 = max(b[2] for b in row)
        y1 = max(b[3] for b in row)
        rects.append((x0, y0, x1, y1))
    return rects


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def _resolve_page_indices(doc, page_hint: int | None) -> list[int]:
    """Return 0-based page indices to scan: hinted page first, then others."""
    total = doc.page_count
    if page_hint is not None and 1 <= page_hint <= total:
        rest = [i for i in range(total) if i != page_hint - 1]
        return [page_hint - 1] + rest
    return list(range(total))


def _gather_candidates(page, spec: AnnotationSpec) -> list:
    """Plain-first + re-ligated additional candidate; union de-duped."""
    plain_quote = normalize_quote_for_pdf(spec.quote)
    quads_plain = []
    if plain_quote:
        try:
            quads_plain = list(page.search_for(plain_quote, quads=True) or [])
        except Exception:
            quads_plain = []
    quads_relig = []
    relig = re_ligate_quote(plain_quote)
    if relig and relig != plain_quote:
        try:
            quads_relig = list(page.search_for(relig, quads=True) or [])
        except Exception:
            quads_relig = []
    return _dedupe_quads(quads_plain + quads_relig)


def _foreign_highlights_on_page(
    existing: list[ExistingAnnot] | None, page_num_1: int
) -> list[tuple[float, float, float, float]]:
    if not existing:
        return []
    return [
        e.rect for e in existing
        if e.page_num == page_num_1 and "highlight" in e.kind
    ]


def _foreign_rects_on_page(
    existing: list[ExistingAnnot] | None, page_num_1: int
) -> list[tuple[float, float, float, float]]:
    if not existing:
        return []
    return [e.rect for e in existing if e.page_num == page_num_1]


def _place_single_annotation(
    doc,
    spec: AnnotationSpec,
    *,
    marker: str = ZOTPILOT_MARKER,
    existing: list[ExistingAnnot] | None = None,
) -> str | None:
    """Place one highlight. Returns None on success, reason string on failure."""
    if len(spec.quote) < MIN_QUOTE_LEN:
        return "too_short"

    page_indices = _resolve_page_indices(doc, spec.page_hint)
    accepted_page: int | None = None
    accepted_quads: list = []
    used_fallback = False
    # Phase 1: plain + re-ligated on each candidate page
    for pno in page_indices:
        page = doc[pno]
        cands = _gather_candidates(page, spec)
        if len(cands) > 1:
            return "ambiguous_multi_match"
        if len(cands) == 1:
            accepted_page = pno
            accepted_quads = cands
            break

    # Phase 2: word-index fallback
    if accepted_page is None:
        normalized_quote = normalize_quote_for_pdf(spec.quote)
        for pno in page_indices:
            page = doc[pno]
            rects = _word_index_find(page, normalized_quote)
            if rects:
                accepted_page = pno
                accepted_quads = [pymupdf.Rect(*r).quad for r in rects]
                used_fallback = True
                break

    if accepted_page is None:
        return "no_match"

    # IoU gate vs foreign highlights (§7.11)
    page_num_1 = accepted_page + 1
    foreign = _foreign_highlights_on_page(existing, page_num_1)
    if foreign:
        union_bb = _quads_union_bbox(accepted_quads)
        for fr in foreign:
            if _rect_iou(union_bb, fr) > _IOU_SKIP_THRESHOLD:
                return "user_already_annotated"

    # Held-page write (RC-2)
    page = doc[accepted_page]
    a = page.add_highlight_annot(accepted_quads)
    try:
        a.set_colors(stroke=map_dimension_to_rgb(spec.dimension))
    except Exception:
        pass
    info = a.info
    info["title"] = marker
    if spec.comment:
        info["content"] = spec.comment
    if spec.subtype:
        info["subject"] = spec.subtype
    a.set_info(info)
    a.update()
    if used_fallback:
        logger.debug("placement: word-index fallback used quote=%r", spec.quote[:32])
    return None


def _region_candidate_points(
    bbox: tuple[float, float, float, float]
) -> list[tuple[str, tuple[float, float]]]:
    x0, y0, x1, _y1 = bbox
    return [
        ("left_gutter", (max(0.0, x0 - _REGION_ICON_SIZE), y0)),
        ("top_up", (x0, max(0.0, y0 - _REGION_ICON_SIZE))),
        ("top_right", (x1, y0)),
        ("right_down", (x1, y0 + _REGION_ICON_SIZE)),
    ]


def _place_region_annotation(
    doc,
    page: int,
    bbox: tuple[float, float, float, float],
    comment: str,
    *,
    marker: str = ZOTPILOT_MARKER,
    subtype: str | None = None,
    existing: list[ExistingAnnot] | None = None,
) -> str | None:
    """Place sticky-note at offset point near figure/table bbox.

    Returns None on success, reason string on failure.
    1-based→0-based page conversion is here in ONE place: doc[page-1].
    """
    if page < 1 or page > doc.page_count:
        return "page_out_of_range"
    pg = doc[page - 1]
    candidates = _region_candidate_points(bbox)
    foreign_rects = _foreign_rects_on_page(existing, page)
    chosen: tuple[float, float] | None = None
    if not foreign_rects:
        chosen = candidates[0][1]
    else:
        for _label, point in candidates:
            if not any(
                _point_near_rect(point, fr, _REGION_NEIGHBORHOOD)
                for fr in foreign_rects
            ):
                chosen = point
                break
    if chosen is None:
        return "region_clustered"
    a = pg.add_text_annot(chosen, comment or "")
    info = a.info
    info["title"] = marker
    if comment:
        info["content"] = comment
    if subtype:
        info["subject"] = subtype
    a.set_info(info)
    a.update()
    return None


def _place_overview_note(doc, overview: dict, *, marker: str = ZOTPILOT_MARKER) -> bool:
    """Page-1 sticky-note overview via add_text_annot (CJK-safe). Returns True on success."""
    if not overview:
        return False
    text = build_overview_text(overview)
    if not text:
        return False
    pg = doc[0]
    a = pg.add_text_annot((24.0, 24.0), text)
    info = a.info
    info["title"] = marker
    info["content"] = text
    info["subject"] = "overview"
    a.set_info(info)
    a.update()
    return True


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_annotated_pdf(pdf_path: Path, expected_marker_count: int) -> dict:
    """Reopen the saved file and check marker count, warnings, is_repaired."""
    # Drain any prior warnings so we sample only this open.
    try:
        pymupdf.TOOLS.mupdf_warnings()
    except Exception:
        pass
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as e:
        return {
            "annot_count_match": False,
            "warnings_empty": False,
            "not_repaired": False,
            "verified": False,
            "error": str(e),
        }
    try:
        count = 0
        for i in range(doc.page_count):
            try:
                annots = list(doc[i].annots() or [])
            except Exception:
                continue
            for a in annots:
                try:
                    title = (a.info or {}).get("title", "") or ""
                    if title.startswith(ZOTPILOT_MARKER):
                        count += 1
                except Exception:
                    continue
        is_repaired = bool(getattr(doc, "is_repaired", False))
    finally:
        doc.close()
    warnings = ""
    try:
        warnings = pymupdf.TOOLS.mupdf_warnings() or ""
    except Exception:
        warnings = ""
    annot_count_match = count == expected_marker_count
    warnings_empty = warnings.strip() == ""
    not_repaired = not is_repaired
    return {
        "annot_count_match": annot_count_match,
        "warnings_empty": warnings_empty,
        "not_repaired": not_repaired,
        "verified": annot_count_match and warnings_empty and not_repaired,
        "marker_count": count,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _build_coverage(
    placed: list[AnnotationSpec],
    unplaced: list[tuple[str, str]],
    overview_placed: bool,
) -> dict:
    counts: dict[str, int] = {}
    for s in placed:
        key = s.subtype or s.kind
        counts[key] = counts.get(key, 0) + 1
    reasons: dict[str, int] = {}
    for _q, r in unplaced:
        reasons[r] = reasons.get(r, 0) + 1
    return {
        "placed_counts": counts,
        "unplaced_counts": reasons,
        "overview_placed": overview_placed,
        "respected": reasons.get("user_already_annotated", 0)
        + reasons.get("region_clustered", 0),
    }


def _label_for_spec(spec: AnnotationSpec) -> str:
    if spec.subtype:
        return f"{spec.subtype}:{spec.quote[:32]}"
    return spec.quote[:48]


def annotate_pdf_file(
    pdf_path: Path,
    annotations: list[AnnotationSpec],
    overview: dict | None,
    *,
    marker: str = ZOTPILOT_MARKER,
) -> PlacementReport:
    """Full write envelope per §4 Phase 1 (RC-1 distinct save target)."""
    pdf_path = Path(pdf_path)

    # Probe page_count for spec validation (read-only).
    try:
        probe = pymupdf.open(str(pdf_path))
        probe_page_count = probe.page_count
        probe.close()
    except Exception as e:
        raise ToolError(f"cannot open PDF: {pdf_path}: {e}") from e

    # 1. validate (M3 + §7.5)
    validate_annotation_specs(list(annotations), overview, page_count=probe_page_count)

    # 2. preflight (RM-3)
    preflight_write_access(pdf_path)

    # 3. scanned guard
    if not has_text_layer(pdf_path):
        raise ScannedPdfError(
            f"PDF has no text layer; OCR required before annotation: {pdf_path}"
        )

    file_size_before = pdf_path.stat().st_size

    # 4. backup (verified)
    bak_path = backup_pdf(pdf_path)

    tmp_path = pdf_path.with_suffix(pdf_path.suffix + ".ztptmp")
    out_path = pdf_path.with_suffix(pdf_path.suffix + ".ztpout")
    restore_path = pdf_path.with_suffix(pdf_path.suffix + ".ztptmp_restore")

    placed: list[AnnotationSpec] = []
    unplaced: list[tuple[str, str]] = []
    overview_placed = False
    verification: dict[str, Any] = {}
    file_size_after = file_size_before
    foreign_before = 0
    swapped = False

    try:
        # 5. copy2 → .ztptmp
        shutil.copy2(pdf_path, tmp_path)

        # 6. open work copy, scan foreign, clear, place, overview
        doc = pymupdf.open(str(tmp_path))
        try:
            existing = read_existing_annotations(doc)
            foreign_before = len(existing)
            clear_zotpilot_annotations(doc, marker=marker)
            for spec in annotations:
                if spec.kind == "region":
                    reason = _place_region_annotation(
                        doc,
                        spec.page or 0,
                        spec.bbox or (0.0, 0.0, 0.0, 0.0),
                        spec.comment,
                        marker=marker,
                        subtype=spec.subtype,
                        existing=existing,
                    )
                else:
                    reason = _place_single_annotation(
                        doc, spec, marker=marker, existing=existing
                    )
                if reason is None:
                    placed.append(spec)
                else:
                    unplaced.append((_label_for_spec(spec), reason))
            overview_placed = _place_overview_note(doc, overview or {}, marker=marker)

            # 7. save full to DISTINCT .ztpout (RC-1)
            doc.save(str(out_path), garbage=3, deflate=True)
        finally:
            doc.close()

        # 8. verify .ztpout
        expected = len(placed) + (1 if overview_placed else 0)
        verification = verify_annotated_pdf(out_path, expected)
        if not verification.get("verified"):
            raise ToolError(
                f"post-write verification failed: {verification}"
            )

        # cross-check foreign annot count is unchanged. MUST close this handle
        # before the os.replace below — on Windows a live handle on out_path
        # locks the file and blocks the rename (RC-3).
        chk_doc = pymupdf.open(str(out_path))
        try:
            foreign_after = len(read_existing_annotations(chk_doc))
        finally:
            chk_doc.close()
        if foreign_after != foreign_before:
            raise ToolError(
                f"foreign-annot count changed: before={foreign_before}, after={foreign_after}"
            )

        # 9. atomic swap — the ONLY point the original is modified (all-or-nothing).
        file_size_after = out_path.stat().st_size
        try:
            os.replace(str(out_path), str(pdf_path))
        except OSError as swap_exc:
            raise ToolError(
                f"could not replace the original PDF (is it open in Zotero or a "
                f"PDF viewer? close it and retry): {pdf_path}: {swap_exc}"
            ) from swap_exc
        swapped = True
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    except Exception as main_exc:
        # The original is untouched until the atomic os.replace above, so a
        # pre-swap failure needs the run-scoped pre-mutation snapshot (.ztptmp)
        # restored — NOT .ztpbak. .ztpbak is the user's pristine archive and is
        # never consumed; using a stale .ztpbak (from an earlier successful run)
        # as the rollback source would restore wrong content / lose user edits
        # (RL-3). If the swap already happened, the new content is committed —
        # do not undo it.
        try:
            if not swapped and tmp_path.exists():
                os.replace(str(tmp_path), str(pdf_path))
        except Exception as rb_exc:
            raise ToolError(
                f"rollback double-fault; backup preserved at {bak_path}: "
                f"original error={main_exc}; rollback error={rb_exc}"
            ) from rb_exc
        finally:
            for p in (tmp_path, out_path, restore_path):
                try:
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass
        if isinstance(main_exc, ScannedPdfError | ToolError):
            raise
        raise ToolError(f"annotate failed (rolled back): {main_exc}") from main_exc
    finally:
        # NEVER unlink(.ztpbak) here (RL-3 invariant)
        for p in (tmp_path, out_path, restore_path):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass

    coverage = _build_coverage(placed, unplaced, overview_placed)
    coverage["foreign_before"] = foreign_before
    return PlacementReport(
        placed=tuple(placed),
        unplaced=tuple(unplaced),
        overview_placed=overview_placed,
        backup_path=str(bak_path),
        page_count=probe_page_count,
        file_size_before=file_size_before,
        file_size_after=file_size_after,
        verified=bool(verification.get("verified")),
        verification_details=verification,
        coverage=coverage,
    )
