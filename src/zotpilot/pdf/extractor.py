"""PDF extraction via pymupdf4llm with pymupdf-layout.

pymupdf-layout MUST be imported before pymupdf4llm to activate
ML-based layout detection (tables, figures, headers, footers, OCR).
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pymupdf

try:
    import pymupdf.layout  # noqa: F401
except ImportError:
    _HAS_LAYOUT = False
else:
    _HAS_LAYOUT = True
import pymupdf4llm

from ..feature_extraction.captions import find_all_captions
from ..feature_extraction.postprocessors.cell_cleaning import clean_cells
from ..feature_extraction.vision_extract import compute_all_crops, compute_recrop_bbox
from ..models import (
    CONFIDENCE_GAP_FILL,
    CONFIDENCE_SCHEME_MATCH,
    DocumentExtraction,
    ExtractedFigure,
    ExtractedTable,
    PageExtraction,
    SectionSpan,
)
from .orphan_recovery import run_recovery
from .section_classifier import categorize_heading

if TYPE_CHECKING:
    from ..feature_extraction.vision_api import TableVisionSpec, VisionAPI

logger = logging.getLogger(__name__)

# Pattern for filtering page identifiers from section-header boxes (e.g. "R1356")
_PAGE_ID_RE = re.compile(r"^R?\d+$")

from ..feature_extraction.captions import (  # noqa: E402
    _TABLE_CAPTION_RE,
    _TABLE_CAPTION_RE_RELAXED,
    _TABLE_LABEL_ONLY_RE,
)

_CAP_PATTERNS = (_TABLE_CAPTION_RE, _TABLE_CAPTION_RE_RELAXED, _TABLE_LABEL_ONLY_RE)


# Prefix for synthetic captions assigned to orphan tables/figures
SYNTHETIC_CAPTION_PREFIX = "Uncaptioned "


def _should_run_full_document_ocr(
    *,
    total_chars: int,
    page_count: int,
    near_empty_pages: int,
    min_chars_per_page: int = 50,
    near_empty_page_ratio_threshold: float = 0.8,
) -> bool:
    """Return True only for genuinely scan-like low-text documents."""
    if page_count <= 0:
        return False
    low_text_overall = total_chars < min_chars_per_page * page_count
    mostly_near_empty = (near_empty_pages / page_count) >= near_empty_page_ratio_threshold
    return low_text_overall and mostly_near_empty


# ---------------------------------------------------------------------------
# Deferred vision work dataclasses
# ---------------------------------------------------------------------------

@dataclass
class _CropInfo:
    """Per-table crop metadata for deferred vision resolution."""
    page_num: int
    caption_text: str
    crop_bbox: tuple[float, float, float, float]


@dataclass
class PendingVisionWork:
    """Collected vision specs awaiting batch API submission.

    Stored on DocumentExtraction.pending_vision by extract_document()
    when a VisionAPI is provided.  Consumed by resolve_pending_vision().
    """
    specs: list          # list[TableVisionSpec]
    crop_infos: list     # list[_CropInfo]
    pdf_path: Path

# --- Layout-artifact table detection ---
# Spaced-out header words from Elsevier article-info boxes.
# Each word requires at least one internal whitespace gap (the spaced-letter
# formatting that Elsevier uses) so plain "article" / "abstract" in a normal
# header column does NOT match.
_ARTICLE_INFO_RE = re.compile(
    r"a\s+r\s*t\s*i\s*c\s*l\s*e|i\s+n\s*f\s*o|a\s+b\s*s\s*t\s*r\s*a\s*c\s*t",
    re.IGNORECASE,
)
# TOC-like cell: "N  Section Title  PageNum"  (e.g. "2 Review of methods 907")
_TOC_LINE_RE = re.compile(r"^\d+\s+[A-Z].*\d{2,}$")
# TOC entries packed into a single cell (after newline collapse):
# "page 1 Introduction 904 . 2 Review of methods 907 . 3 ..."
# Look for 3+ occurrences of "digit(s) Title-word ... digit(s)" separated by anything.
_TOC_PACKED_RE = re.compile(r"\d+\s+[A-Z][a-z]+.*?\d{2,}")
# Multi-column TOC row: number in col 0, title in col 1, page in col 2
_TOC_MULTICOLUMN_RE = re.compile(r"^\.?\d+\.?$")
# Figure reference inside a table cell
_FIG_REF_IN_CELL_RE = re.compile(
    r"(?:Figure|Fig\.?)\s+\d+\b.*(?:diagram|block|schematic|overview|flowchart)",
    re.IGNORECASE,
)


def _tag_figure_data_tables(
    tables: "list[ExtractedTable]",
    figures: "list[ExtractedFigure]",
    *,
    overlap_threshold: float = 0.5,
) -> None:
    """Tag orphan tables that overlap significantly with a figure.

    Tables without a real caption whose bounding box overlaps a figure
    by more than ``overlap_threshold`` (fraction of table area) are
    tagged as ``"figure_data_table"`` artifacts.

    Args:
        tables: Tables to check (modified in place).
        figures: Figures to compare against.
        overlap_threshold: Minimum overlap ratio (table area fraction)
            to trigger tagging.
    """
    for t in tables:
        if t.artifact_type:
            continue
        if t.caption and not t.caption.startswith(SYNTHETIC_CAPTION_PREFIX):
            continue
        t_rect = pymupdf.Rect(t.bbox)
        t_area = t_rect.get_area()
        if t_area <= 0:
            continue
        for f in figures:
            if f.page_num != t.page_num:
                continue
            f_rect = pymupdf.Rect(f.bbox)
            overlap = t_rect & f_rect
            if not overlap.is_empty:
                overlap_ratio = overlap.get_area() / t_area
                if overlap_ratio > overlap_threshold:
                    t.artifact_type = "figure_data_table"
                    logger.info(
                        "Tagged table on page %d as figure_data_table "
                        "(%.0f%% overlap with figure)",
                        t.page_num, overlap_ratio * 100,
                    )
                    break


def _classify_artifact(table: "ExtractedTable") -> str | None:
    """Classify a table as a layout artifact or real data.

    Returns an artifact-type tag string, or None for real data tables.

    Tags:
    - ``"article_info_box"``  — Elsevier article-info / abstract header box
    - ``"table_of_contents"`` — sequential section-number + page-number rows
    - ``"diagram_as_table"``  — block diagram / figure text mis-parsed as table
    """
    header_text = " ".join(table.headers).strip()
    cell_parts = " ".join(c for row in table.rows for c in row) if table.rows else ""
    all_text = (header_text + " " + cell_parts).strip()

    # A "real" caption is one matching "Table N" patterns — spurious captions
    # (author affiliations, dates) don't protect a table from artifact detection.
    has_real_caption = bool(
        table.caption and any(p.match(table.caption) for p in _CAP_PATTERNS)
    )

    # Pattern 1: Elsevier article-info box (tables without real "Table N" captions)
    if not has_real_caption:
        if _ARTICLE_INFO_RE.search(header_text):
            return "article_info_box"

    # Pattern 2a: Table of contents — one entry per cell
    all_cells = list(table.headers) + [c for row in table.rows for c in row]
    toc_hits = sum(1 for c in all_cells if c.strip() and _TOC_LINE_RE.match(c.strip()))
    total_rows = len(table.rows) + (1 if table.headers else 0)
    if toc_hits >= 3 and toc_hits >= total_rows * 0.4:
        return "table_of_contents"

    # Pattern 2b: TOC packed into a single cell (entries joined on one line)
    if not has_real_caption:
        for c in all_cells:
            if c and len(_TOC_PACKED_RE.findall(c)) >= 3:
                return "table_of_contents"

    # Pattern 2c: Multi-column TOC (number | title | page across columns)
    if not has_real_caption and total_rows >= 2:
        mc_hits = 0
        all_rows = []
        if table.headers and len(table.headers) >= 3:
            all_rows.append(table.headers)
        all_rows.extend(table.rows)
        for row in all_rows:
            if len(row) >= 3:
                col0 = row[0].strip()
                col2 = row[-1].strip()
                if _TOC_MULTICOLUMN_RE.match(col0) and _TOC_MULTICOLUMN_RE.match(col2):
                    mc_hits += 1
        if mc_hits >= 2 and mc_hits >= total_rows * 0.4:
            return "table_of_contents"

    # Pattern 3: Block diagram / figure parsed as table — if the cell text
    # contains a figure caption ("Figure N. block diagram ..."), the table
    # is a misidentified figure regardless of fill rate.
    if not has_real_caption:
        if _FIG_REF_IN_CELL_RE.search(all_text):
            return "diagram_as_table"

    return None


def _extract_figures_for_page(
    page: "pymupdf.Page",
    page_num: int,
    page_chunk: dict,
    write_images: bool,
    images_dir: "Path | None",
    doc: "pymupdf.Document",
    all_captions: list,
) -> "list[ExtractedFigure]":
    """Detect and optionally render figures on a page."""
    from ..feature_extraction.methods.figure_detection import detect_figures, render_figure

    figure_captions = [c for c in all_captions if c.caption_type == "figure"]
    figure_results = detect_figures(page, page_chunk, figure_captions) if page_chunk else []

    figures = []
    for fi, (fbbox, fcaption) in enumerate(figure_results):
        image_path = None
        if write_images and doc is not None and images_dir is not None:
            img = render_figure(doc, page_num, fbbox, Path(images_dir), fi)
            image_path = str(img) if img else None
        figures.append(ExtractedFigure(
            page_num=page_num,
            figure_index=fi,
            bbox=tuple(fbbox),
            caption=fcaption,
            image_path=Path(image_path) if image_path else None,
        ))
    return figures



def extract_document(
    pdf_path: Path | str,
    *,
    write_images: bool = False,
    images_dir: Path | str | None = None,
    ocr_language: str = "eng",
    vision_api: "VisionAPI | None" = None,
) -> DocumentExtraction:
    """Extract a PDF document using pymupdf4llm with layout detection."""
    pdf_path = Path(pdf_path)

    kwargs: dict = dict(
        page_chunks=True,
        write_images=False,
        header=False,
        footer=False,
        show_progress=False,
        ocr_language=ocr_language,
    )

    t0 = pymupdf.TOOLS.mupdf_warnings()
    _ = t0  # silence lint about access-only side effect
    import time

    markdown_started = time.perf_counter()
    page_chunks: list[dict] = pymupdf4llm.to_markdown(str(pdf_path), **kwargs)
    markdown_elapsed = time.perf_counter() - markdown_started

    # If too little text extracted, retry with PyMuPDF's built-in full-page OCR
    # (pymupdf4llm's should_ocr_page() skips "photo-like" pages in scanned PDFs)
    _OCR_MIN_CHARS_PER_PAGE = 50
    total_chars = sum(len(chunk.get("text", "").strip()) for chunk in page_chunks)
    native_text_lengths: list[int] = []
    native_scan_started = time.perf_counter()
    try:
        native_doc = pymupdf.open(str(pdf_path))
        for page in native_doc:
            native_text_lengths.append(len(page.get_text().strip()))
        native_doc.close()
    except Exception as e:
        logger.warning("Native text scan failed for %s: %s", pdf_path.name, e)
        native_text_lengths = []
    native_scan_elapsed = time.perf_counter() - native_scan_started

    near_empty_pages = sum(1 for n in native_text_lengths if n < 20)
    page_count = len(page_chunks)
    should_run_ocr = _should_run_full_document_ocr(
        total_chars=total_chars,
        page_count=page_count,
        near_empty_pages=near_empty_pages,
        min_chars_per_page=_OCR_MIN_CHARS_PER_PAGE,
    )
    logger.info(
        "extract_document[%s]: to_markdown=%.1fs native_scan=%.1fs chars=%d pages=%d "
        "near_empty_pages=%d ocr_fallback=%s",
        pdf_path.name,
        markdown_elapsed,
        native_scan_elapsed,
        total_chars,
        page_count,
        near_empty_pages,
        "yes" if should_run_ocr else "no",
    )
    if should_run_ocr:
        try:
            ocr_started = time.perf_counter()
            doc = pymupdf.open(str(pdf_path))
            ocr_texts: list[str] = []
            for page in doc:
                tp = page.get_textpage_ocr(language=ocr_language, dpi=300, full=True)
                ocr_texts.append(page.get_text(textpage=tp))
            doc.close()
            ocr_total = sum(len(t.strip()) for t in ocr_texts)
            logger.info(
                "extract_document[%s]: ocr_elapsed=%.1fs chars_before=%d chars_after=%d",
                pdf_path.name,
                time.perf_counter() - ocr_started,
                total_chars,
                ocr_total,
            )
            if ocr_total > total_chars:
                # OCR produced more text — rebuild chunks with new dicts
                page_chunks = [
                    {**chunk, "text": ocr_texts[i], "page_boxes": []}
                    if i < len(ocr_texts)
                    else chunk
                    for i, chunk in enumerate(page_chunks)
                ]
        except Exception as e:
            logger.warning(f"OCR fallback failed for {pdf_path.name}: {e}")

    # Build pages and full markdown
    pages: list[PageExtraction] = []
    md_parts: list[str] = []
    char_offset = 0

    for chunk in page_chunks:
        md = chunk.get("text", "")
        page_num = chunk.get("metadata", {}).get("page_number", 1)
        page_boxes = chunk.get("page_boxes", [])
        tables_on_page = sum(1 for b in page_boxes if b.get("class") == "table")
        images_on_page = sum(1 for b in page_boxes if b.get("class") == "picture")

        pages.append(PageExtraction(
            page_num=page_num,
            markdown=md,
            char_start=char_offset,
            tables_on_page=tables_on_page,
            images_on_page=images_on_page,
        ))
        md_parts.append(md)
        char_offset += len(md) + 1  # +1 for join newline

    full_markdown = "\n".join(md_parts)

    # --- Ligature normalization (all text) ---
    full_markdown = _normalize_ligatures(full_markdown)
    for p in pages:
        p.markdown = _normalize_ligatures(p.markdown)

    # Detect sections using toc_items or section-header page_boxes
    sections = _detect_sections(page_chunks, full_markdown, pages)

    # --- STRUCTURED EXTRACTION (use native PyMuPDF) ---
    doc = pymupdf.open(str(pdf_path))

    # --- Abstract detection ---
    # If no section is labelled "abstract", check first pages for abstract text
    has_abstract = any(s.label == "abstract" for s in sections)
    if not has_abstract and pages:
        abstract_span = _detect_abstract(pages, full_markdown, doc, sections)
        if abstract_span:
            sections = _insert_abstract(sections, abstract_span)

    figures: list[ExtractedFigure] = []
    fig_idx = 0

    # Per-page data collected for batch vision extraction
    # Each entry: (page_num, page, detected_caption, crop_bbox)
    _table_crops: list[tuple[int, "pymupdf.Page", object, tuple]] = []

    for chunk in page_chunks:
        pnum = chunk.get("metadata", {}).get("page_number", 1)
        page = doc[pnum - 1]

        page_label = None
        if sections and pages:
            from .section_classifier import assign_section
            for p in pages:
                if p.page_num == pnum:
                    page_label = assign_section(p.char_start, sections)
                    break
            if page_label in ("references", "appendix"):
                continue

        all_captions_on_page = find_all_captions(page)

        page_figs = _extract_figures_for_page(
            page, pnum, chunk, write_images, images_dir, doc,
            all_captions=all_captions_on_page,
        )
        for f in page_figs:
            f.figure_index = fig_idx
            figures.append(f)
            fig_idx += 1

        if vision_api is not None:
            for cap, crop_bbox in compute_all_crops(page, all_captions_on_page, caption_type="table"):
                _table_crops.append((pnum, page, cap, crop_bbox))

    # --- Vision spec collection (deferred — actual API call in resolve_pending_vision) ---
    tables: list[ExtractedTable] = []
    pending: PendingVisionWork | None = None

    if vision_api is not None and _table_crops:
        from ..feature_extraction.vision_api import TableVisionSpec

        specs: list[TableVisionSpec] = []
        crop_infos: list[_CropInfo] = []
        for seq_idx, (pnum, page, cap, crop_bbox) in enumerate(_table_crops):
            raw_text = page.get_text("text", clip=pymupdf.Rect(crop_bbox))
            specs.append(TableVisionSpec(
                table_id=f"p{pnum}_t{seq_idx}",
                pdf_path=pdf_path,
                page_num=pnum,
                bbox=crop_bbox,
                raw_text=raw_text,
                caption=cap.text,
                garbled=False,
            ))
            crop_infos.append(_CropInfo(
                page_num=pnum,
                caption_text=cap.text,
                crop_bbox=crop_bbox,
            ))
        pending = PendingVisionWork(specs=specs, crop_infos=crop_infos, pdf_path=pdf_path)

    # --- Figure post-processing (independent of tables) ---
    for f in figures:
        f.caption = _normalize_ligatures(f.caption)

    # Orphan recovery: match floating captions to captionless figures
    run_recovery(doc, figures, tables, page_chunks)

    figures = [f for f in figures if f.caption is not None]

    # Compute stats (needs open doc, but not tables)
    stats = _compute_stats(pages, page_chunks, doc)

    if pending is not None:
        # Vision requested: defer table construction, post-processing,
        # completeness, and synthetic captions to resolve_pending_vision().
        doc.close()
        return DocumentExtraction(
            pages=pages,
            full_markdown=full_markdown,
            sections=sections,
            tables=[],
            figures=figures,
            stats=stats,
            quality_grade="",
            completeness=None,
            vision_details=None,
            pending_vision=pending,
        )

    # No vision: compute completeness with empty tables and finalize.
    completeness = _compute_completeness(doc, pages, sections, tables, figures, stats)
    doc.close()

    for f in figures:
        if not f.caption:
            f.caption = f"{SYNTHETIC_CAPTION_PREFIX}figure on page {f.page_num}"

    return DocumentExtraction(
        pages=pages,
        full_markdown=full_markdown,
        sections=sections,
        tables=tables,
        figures=figures,
        stats=stats,
        quality_grade=completeness.grade,
        completeness=completeness,
        vision_details=None,
    )


# ---------------------------------------------------------------------------
# Batch vision resolution
# ---------------------------------------------------------------------------


def resolve_pending_vision(
    extractions: dict[str, DocumentExtraction],
    vision_api: "VisionAPI",
) -> None:
    """Batch all pending vision specs across documents into a single API call.

    Mutates each DocumentExtraction in-place: populates tables, vision_details,
    completeness, and quality_grade.

    Call this after extracting all documents with ``extract_document(...,
    vision_api=api)`` so that every table across every paper is submitted in
    one Anthropic Batch API request (plus one re-crop batch if needed, plus
    one full-page resend batch for incomplete/unparsable/empty responses).

    Args:
        extractions: Mapping of doc_key -> DocumentExtraction with
            ``pending_vision`` set by ``extract_document()``.
        vision_api: VisionAPI instance.
    """
    from ..feature_extraction.vision_api import TableVisionSpec

    # --- Collect all specs across all documents ---
    all_specs: list[TableVisionSpec] = []
    mapping: list[tuple[str, int]] = []  # (doc_key, local_index)

    for doc_key, ext in extractions.items():
        pending: PendingVisionWork | None = ext.pending_vision  # type: ignore[assignment]
        if pending is None or not pending.specs:
            continue
        for i, spec in enumerate(pending.specs):
            # Make table_id globally unique for the batch
            spec.table_id = f"{doc_key}__{spec.table_id}"
            all_specs.append(spec)
            mapping.append((doc_key, i))

    if not all_specs:
        # No tables anywhere — still finalize each document
        for _doc_key, ext in extractions.items():
            if ext.pending_vision is not None:
                _finalize_document_no_tables(ext)
        return

    import time as _time

    n_docs = len({dk for dk, _ in mapping})
    logger.info(
        "Vision wave 1/3: submitting %d tables across %d documents "
        "(est. 10-30min for batch processing)",
        len(all_specs), n_docs,
    )

    # --- Single batch call for all tables ---
    _w1_start = _time.perf_counter()
    all_responses = vision_api.extract_tables_batch(all_specs)
    _w1_elapsed = _time.perf_counter() - _w1_start
    n_parse_ok = sum(1 for r in all_responses if r.parse_success)
    logger.info(
        "Vision wave 1/3 complete: %d/%d parsed OK in %.1fmin",
        n_parse_ok, len(all_responses), _w1_elapsed / 60,
    )

    # Distribute responses back to per-document lists
    per_doc_responses: dict[str, list] = defaultdict(list)
    for (doc_key, _local_idx), resp in zip(mapping, all_responses):
        per_doc_responses[doc_key].append(resp)

    # --- Batch 2: recrop + full-page combined ---
    # After batch 1, two disjoint sets need a second attempt:
    #   Recrops:   model parsed OK but requests a tighter crop
    #   Full-page: parse failure, incomplete w/o recrop coords, or parsed-but-empty
    # These are disjoint (recrop requires parse_success=True) so they share one batch.
    b2_specs: list[TableVisionSpec] = []
    b2_mapping: list[tuple[str, int, str]] = []  # (doc_key, local_index, "recrop"|"fullpage")

    for doc_key, ext in extractions.items():
        pending = ext.pending_vision  # type: ignore[assignment]
        if pending is None or not pending.specs:
            continue
        responses = per_doc_responses.get(doc_key, [])
        if not responses:
            continue

        doc = pymupdf.open(str(pending.pdf_path))
        try:
            for local_idx, resp in enumerate(responses):
                crop_info = pending.crop_infos[local_idx]
                page = doc[crop_info.page_num - 1]

                # Recrop: parsed OK, model says crop needs adjustment
                if resp.recrop_needed and resp.recrop_bbox_pct is not None:
                    new_bbox = compute_recrop_bbox(
                        crop_info.crop_bbox, resp.recrop_bbox_pct,
                    )
                    new_raw_text = page.get_text(
                        "text", clip=pymupdf.Rect(new_bbox),
                    )
                    b2_specs.append(TableVisionSpec(
                        table_id=f"{doc_key}__recrop_p{crop_info.page_num}_t{local_idx}",
                        pdf_path=pending.pdf_path,
                        page_num=crop_info.page_num,
                        bbox=new_bbox,
                        raw_text=new_raw_text,
                        caption=crop_info.caption_text,
                        garbled=False,
                    ))
                    b2_mapping.append((doc_key, local_idx, "recrop"))
                    continue

                # Full-page triggers (disjoint from recrop):
                needs_fullpage = False
                if resp.is_incomplete and not resp.recrop_needed:
                    needs_fullpage = True
                elif resp.parse_success and not resp.headers and not resp.rows:
                    needs_fullpage = True
                elif not resp.parse_success:
                    needs_fullpage = True

                if needs_fullpage:
                    full_rect = page.rect
                    full_bbox = (full_rect.x0, full_rect.y0, full_rect.x1, full_rect.y1)
                    raw_text = page.get_text("text")
                    b2_specs.append(TableVisionSpec(
                        table_id=f"{doc_key}__fullpage_p{crop_info.page_num}_t{local_idx}",
                        pdf_path=pending.pdf_path,
                        page_num=crop_info.page_num,
                        bbox=full_bbox,
                        raw_text=raw_text,
                        caption=crop_info.caption_text,
                        garbled=False,
                    ))
                    b2_mapping.append((doc_key, local_idx, "fullpage"))
        finally:
            doc.close()

    per_doc_recrop: dict[str, dict[int, object]] = defaultdict(dict)
    per_doc_fullpage: dict[str, dict[int, object]] = defaultdict(dict)
    if b2_specs:
        n_recrop = sum(1 for _, _, k in b2_mapping if k == "recrop")
        n_fullpage = len(b2_specs) - n_recrop
        logger.info(
            "Vision wave 2/3: %d tables (%d recrop + %d full-page) "
            "(est. 10-30min for batch processing)",
            len(b2_specs), n_recrop, n_fullpage,
        )
        _w2_start = _time.perf_counter()
        b2_responses = vision_api.extract_tables_batch(b2_specs)
        _w2_elapsed = _time.perf_counter() - _w2_start
        logger.info(
            "Vision wave 2/3 complete in %.1fmin", _w2_elapsed / 60,
        )
        for (doc_key, local_idx, kind), resp in zip(b2_mapping, b2_responses):
            if kind == "recrop":
                per_doc_recrop[doc_key][local_idx] = resp
            else:
                per_doc_fullpage[doc_key][local_idx] = resp

    # --- Batch 3: failed-recrop follow-up (rare) ---
    # If any recrop came back broken (still incomplete, parse failure, or empty),
    # fall back to full-page for those tables only.
    b3_specs: list[TableVisionSpec] = []
    b3_mapping: list[tuple[str, int]] = []

    for doc_key, recrop_dict in per_doc_recrop.items():
        pending = extractions[doc_key].pending_vision  # type: ignore[assignment]
        if pending is None:
            continue
        need_followup: list[tuple[int, object]] = []
        for local_idx, rc_resp in recrop_dict.items():
            if rc_resp.parse_success and not rc_resp.is_incomplete and (rc_resp.headers or rc_resp.rows):
                continue
            need_followup.append((local_idx, rc_resp))

        if not need_followup:
            continue

        doc = pymupdf.open(str(pending.pdf_path))
        try:
            for local_idx, _rc_resp in need_followup:
                crop_info = pending.crop_infos[local_idx]
                page = doc[crop_info.page_num - 1]
                full_rect = page.rect
                full_bbox = (full_rect.x0, full_rect.y0, full_rect.x1, full_rect.y1)
                raw_text = page.get_text("text")
                b3_specs.append(TableVisionSpec(
                    table_id=f"{doc_key}__fullpage_p{crop_info.page_num}_t{local_idx}",
                    pdf_path=pending.pdf_path,
                    page_num=crop_info.page_num,
                    bbox=full_bbox,
                    raw_text=raw_text,
                    caption=crop_info.caption_text,
                    garbled=False,
                ))
                b3_mapping.append((doc_key, local_idx))
        finally:
            doc.close()

    if b3_specs:
        logger.info(
            "Vision wave 3/3 (failed-recrop follow-up): %d tables "
            "(est. 10-30min for batch processing)",
            len(b3_specs),
        )
        _w3_start = _time.perf_counter()
        b3_responses = vision_api.extract_tables_batch(b3_specs)
        n_recovered = 0
        for (doc_key, local_idx), resp in zip(b3_mapping, b3_responses):
            per_doc_fullpage[doc_key][local_idx] = resp
            if resp.parse_success and (resp.headers or resp.rows):
                n_recovered += 1
        _w3_elapsed = _time.perf_counter() - _w3_start
        logger.info(
            "Vision wave 3/3 complete in %.1fmin: recovered %d/%d failed recrops",
            _w3_elapsed / 60, n_recovered, len(b3_specs),
        )

    # --- Build tables and finalize each document ---
    for doc_key, ext in extractions.items():
        pending = ext.pending_vision  # type: ignore[assignment]
        if pending is None:
            continue
        if not pending.specs:
            _finalize_document_no_tables(ext)
            continue

        responses = per_doc_responses.get(doc_key, [])
        recrop_for_doc = per_doc_recrop.get(doc_key, {})
        fullpage_for_doc = per_doc_fullpage.get(doc_key, {})
        tables, vision_details = _build_tables_from_responses(
            responses, pending.crop_infos, recrop_for_doc,
            fullpage_responses=fullpage_for_doc,
        )

        # --- Post-process tables (needs doc re-opened) ---
        doc = pymupdf.open(str(pending.pdf_path))
        try:
            if tables:
                _assign_heading_captions(doc, tables)
                _assign_continuation_captions(tables)
                for t in tables:
                    t.caption = _normalize_ligatures(t.caption)
                for t in tables:
                    t.artifact_type = _classify_artifact(t)
                    if t.artifact_type:
                        logger.info(
                            "Tagged table on page %d as artifact: %s",
                            t.page_num, t.artifact_type,
                        )

                # Figure-table overlap detection
                _tag_figure_data_tables(tables, ext.figures)

                tables = [t for t in tables if not t.artifact_type]

            # Orphan recovery for figures (cross-page caption matching)
            # page_chunks not available here; pass empty list — recovery
            # still works via caption scanning on the open doc.
            run_recovery(doc, ext.figures, tables, [])

            completeness = _compute_completeness(
                doc, ext.pages, ext.sections, tables, ext.figures, ext.stats,
            )
        finally:
            doc.close()

        # Synthetic captions
        for t in tables:
            if not t.caption:
                t.caption = f"{SYNTHETIC_CAPTION_PREFIX}table on page {t.page_num}"
        for f in ext.figures:
            if not f.caption:
                f.caption = f"{SYNTHETIC_CAPTION_PREFIX}figure on page {f.page_num}"

        # Update extraction in-place
        ext.tables = tables
        ext.vision_details = vision_details if vision_details else None
        ext.completeness = completeness
        ext.quality_grade = completeness.grade
        ext.pending_vision = None


def _finalize_document_no_tables(ext: DocumentExtraction) -> None:
    """Finalize a document that had vision requested but no table specs."""
    pending: PendingVisionWork = ext.pending_vision  # type: ignore[assignment]
    doc = pymupdf.open(str(pending.pdf_path))
    try:
        completeness = _compute_completeness(
            doc, ext.pages, ext.sections, ext.tables, ext.figures, ext.stats,
        )
    finally:
        doc.close()

    for f in ext.figures:
        if not f.caption:
            f.caption = f"{SYNTHETIC_CAPTION_PREFIX}figure on page {f.page_num}"

    ext.completeness = completeness
    ext.quality_grade = completeness.grade
    ext.pending_vision = None


def _build_tables_from_responses(
    responses: list,
    crop_infos: list[_CropInfo],
    recrop_responses: dict[int, object],
    fullpage_responses: dict[int, object] | None = None,
) -> tuple[list[ExtractedTable], list[dict]]:
    """Convert vision API responses into ExtractedTable objects and detail dicts.

    Args:
        recrop_responses: Re-crop batch results keyed by local index.
        fullpage_responses: Full-page retry results keyed by local index.
            Populated when the effective response was degenerate (empty,
            incomplete without recrop coords, or parse failure).
    """
    tables: list[ExtractedTable] = []
    vision_details: list[dict] = []
    table_idx_per_page: dict[int, int] = {}
    _fullpage = fullpage_responses or {}

    for i, (orig_resp, crop_info) in enumerate(zip(responses, crop_infos)):
        resp = orig_resp  # may be overridden by recrop or fullpage
        recropped = False
        recrop_bbox_pct_used: list[float] | None = None
        fullpage_attempted = i in _fullpage

        if i in recrop_responses:
            recrop_resp = recrop_responses[i]
            # Only override if the recrop actually parsed — a failed recrop
            # should fall back to the (valid but incomplete) initial response.
            if not recrop_resp.is_incomplete and recrop_resp.parse_success:
                recrop_bbox_pct_used = orig_resp.recrop_bbox_pct
                resp = recrop_resp
                recropped = True

        # Full-page override: applied after recrop, only if the result has content
        fullpage_parse_success: bool | None = None
        if fullpage_attempted:
            fp_resp = _fullpage[i]
            fullpage_parse_success = fp_resp.parse_success
            if fp_resp.parse_success and (fp_resp.headers or fp_resp.rows):
                resp = fp_resp

        detail = {
            "text_layer_caption": crop_info.caption_text,
            "vision_caption": resp.caption,
            "page_num": crop_info.page_num,
            "crop_bbox": list(crop_info.crop_bbox),
            "recropped": recropped,
            "recrop_bbox_pct": recrop_bbox_pct_used,
            "parse_success": resp.parse_success,
            "is_incomplete": resp.is_incomplete,
            "incomplete_reason": resp.incomplete_reason,
            "recrop_needed": orig_resp.recrop_needed,
            "raw_response": resp.raw_response,
            "headers": resp.headers,
            "rows": resp.rows,
            "footnotes": resp.footnotes,
            "table_label": resp.table_label,
            "fullpage_attempted": fullpage_attempted,
            "fullpage_parse_success": fullpage_parse_success,
        }
        vision_details.append(detail)

        if not resp.parse_success:
            continue

        cleaned_headers, cleaned_rows = clean_cells(resp.headers, resp.rows)
        caption_text = resp.caption if resp.caption else crop_info.caption_text

        pnum = crop_info.page_num
        page_table_idx = table_idx_per_page.get(pnum, 0)
        table_idx_per_page[pnum] = page_table_idx + 1

        tables.append(ExtractedTable(
            page_num=pnum,
            table_index=page_table_idx,
            bbox=tuple(crop_info.crop_bbox),
            headers=cleaned_headers,
            rows=cleaned_rows,
            caption=caption_text,
            caption_position="above",
            footnotes=resp.footnotes,
            extraction_strategy="vision",
        ))

    return tables, vision_details


# ---------------------------------------------------------------------------
# Section detection
# ---------------------------------------------------------------------------

def _strip_md_formatting(text: str) -> str:
    """Strip markdown formatting characters (#, *, _, parens, leading numbers/dots)."""
    text = re.sub(r"^#+\s*", "", text)
    text = text.replace("**", "").replace("*", "").replace("_", "")
    # Remove leading section numbers like "1.", "2.1.", "3.2.1."
    text = re.sub(r"^\d+(\.\d+)*\.?\s*", "", text)
    # Remove surrounding parens and extra whitespace
    text = re.sub(r"\(\s*([a-z])\s*\)", "", text)
    return text.strip()


def _detect_sections(
    page_chunks: list[dict],
    full_markdown: str,
    pages: list[PageExtraction],
) -> list[SectionSpan]:
    """Detect sections using toc_items (preferred) or section-header page_boxes (fallback)."""
    total_len = len(full_markdown)
    if total_len == 0:
        return []

    # Strategy 1: Use toc_items if available
    toc_entries = []
    for chunk in page_chunks:
        for item in chunk.get("toc_items", []):
            toc_entries.append(item)

    if toc_entries:
        return _sections_from_toc(toc_entries, page_chunks, full_markdown, pages)

    # Strategy 2: Fall back to section-header page_boxes
    return _sections_from_header_boxes(page_chunks, full_markdown, pages)


def _sections_from_toc(
    toc_entries: list[list],
    page_chunks: list[dict],
    full_markdown: str,
    pages: list[PageExtraction],
) -> list[SectionSpan]:
    """Build sections from PDF table-of-contents entries matched to section-header boxes."""
    total_len = len(full_markdown)

    # Build page-indexed section-header box lookup
    header_boxes_by_page: dict[int, list[dict]] = {}
    for chunk in page_chunks:
        page_num = chunk.get("metadata", {}).get("page_number", 1)
        text = chunk.get("text", "")
        for box in chunk.get("page_boxes", []):
            if box.get("class") == "section-header":
                pos = box.get("pos")
                if pos and isinstance(pos, (list, tuple)) and len(pos) == 2:
                    box_text = text[pos[0]:pos[1]]
                    header_boxes_by_page.setdefault(page_num, []).append({
                        "text": box_text,
                        "pos": pos,
                        "page_num": page_num,
                    })

    # Match TOC entries to section-header boxes, get global char offsets
    # Only use level-1 and level-2 entries
    matched: list[tuple[int, int, str, str]] = []  # (global_offset, level, toc_title, heading_text)

    for entry in toc_entries:
        level, title, page = entry[0], entry[1], entry[2]
        if level > 3:
            continue
        # For level-3+, only include if the heading has a high-value keyword match
        if level == 3:
            clean = _strip_md_formatting(title)
            cat, weight = categorize_heading(clean)
            if not cat or weight < 0.85:
                continue

        toc_clean = _strip_md_formatting(title).lower().strip()
        if not toc_clean:
            continue

        # Find matching section-header box on the correct page (or adjacent pages,
        # since TOC page numbers can be off by 1 from layout engine detection)
        matched_box = None
        for search_page in [page, page + 1, page - 1]:
            boxes_on_page = header_boxes_by_page.get(search_page, [])
            for hbox in boxes_on_page:
                box_clean = _strip_md_formatting(hbox["text"]).lower().strip()
                if toc_clean in box_clean or box_clean in toc_clean:
                    matched_box = hbox
                    break
            if matched_box:
                break

        if matched_box is None:
            logger.debug("TOC entry %r (page %d) not matched to any section-header box", title, page)
            continue

        # Compute global char offset using the page the box was actually found on
        actual_page = matched_box["page_num"]
        page_obj = None
        for p in pages:
            if p.page_num == actual_page:
                page_obj = p
                break
        if page_obj is None:
            continue

        global_offset = page_obj.char_start + matched_box["pos"][0]
        matched.append((global_offset, level, title, matched_box["text"]))

    if not matched:
        return _sections_from_header_boxes(page_chunks, full_markdown, pages)

    # Sort by global offset
    matched.sort(key=lambda x: x[0])

    # Two-pass classification:
    # Pass 1: Keyword match or defer
    # Pass 2: L2+ entries inherit from keyword-matched L1 parent;
    #         everything else → unknown
    labels: list[str] = []
    confs: list[float] = []

    # Build parallel lists: classified labels and the matched entry info
    entries: list[tuple[int, int, str, str]] = matched  # (offset, level, toc_title, heading_text)

    # Pass 1: keyword classification
    for global_offset, level, toc_title, heading_text in entries:
        clean_title = _strip_md_formatting(toc_title)
        cat, weight = categorize_heading(clean_title)
        if cat:
            labels.append(cat)
            confs.append(CONFIDENCE_SCHEME_MATCH)
        else:
            labels.append("__deferred__")
            confs.append(CONFIDENCE_GAP_FILL)

    # Pass 2: L2+ entries inherit from their keyword-matched L1 parent.
    # This is structural inheritance (subsection belongs to parent), not
    # position guessing. L1 entries without keywords stay deferred → unknown.
    for i in range(len(entries)):
        if labels[i] != "__deferred__":
            continue
        if entries[i][1] >= 2:  # level 2 or deeper
            for j in range(i - 1, -1, -1):
                if entries[j][1] == 1 and labels[j] not in ("__deferred__", "unknown", "preamble"):
                    labels[i] = labels[j]
                    break

    # Remaining deferred → unknown
    for i in range(len(entries)):
        if labels[i] == "__deferred__":
            labels[i] = "unknown"

    # Build classified tuples
    classified: list[tuple[int, str, str, float]] = []
    for i in range(len(entries)):
        classified.append((entries[i][0], labels[i], entries[i][3], confs[i]))

    return _build_spans(classified, total_len)


def _sections_from_header_boxes(
    page_chunks: list[dict],
    full_markdown: str,
    pages: list[PageExtraction],
) -> list[SectionSpan]:
    """Build sections from section-header page_boxes (for PDFs without TOC)."""
    total_len = len(full_markdown)

    headers: list[tuple[int, str]] = []  # (global_offset, heading_text)

    for chunk in page_chunks:
        page_num = chunk.get("metadata", {}).get("page_number", 1)
        text = chunk.get("text", "")
        page_obj = None
        for p in pages:
            if p.page_num == page_num:
                page_obj = p
                break
        if page_obj is None:
            continue

        for box in chunk.get("page_boxes", []):
            if box.get("class") != "section-header":
                continue
            pos = box.get("pos")
            if not (pos and isinstance(pos, (list, tuple)) and len(pos) == 2):
                continue

            heading_text = text[pos[0]:pos[1]].strip()
            cleaned = _strip_md_formatting(heading_text).strip()

            # Filter page identifiers
            if _PAGE_ID_RE.match(cleaned):
                continue

            global_offset = page_obj.char_start + pos[0]
            headers.append((global_offset, heading_text))

    if not headers:
        return [SectionSpan(
            label="unknown",
            char_start=0,
            char_end=total_len,
            heading_text="",
            confidence=0.5,
        )]

    headers.sort(key=lambda x: x[0])

    # Classify
    classified: list[tuple[int, str, str, float]] = []
    for global_offset, heading_text in headers:
        clean = _strip_md_formatting(heading_text)
        cat, weight = categorize_heading(clean)
        if cat:
            classified.append((global_offset, cat, heading_text, CONFIDENCE_SCHEME_MATCH))
        else:
            classified.append((global_offset, "__deferred__", heading_text, CONFIDENCE_GAP_FILL))

    # Classify: keyword match or unknown (no TOC levels to inherit from)
    for i, (offset, label, heading_text, conf) in enumerate(classified):
        if label != "__deferred__":
            continue
        classified[i] = (offset, "unknown", heading_text, CONFIDENCE_GAP_FILL)

    return _build_spans(classified, total_len)


def _build_spans(
    classified: list[tuple[int, str, str, float]],
    total_len: int,
) -> list[SectionSpan]:
    """Build SectionSpan list from classified entries, covering the full document."""
    spans: list[SectionSpan] = []

    if classified[0][0] > 0:
        spans.append(SectionSpan(
            label="preamble",
            char_start=0,
            char_end=classified[0][0],
            heading_text="",
            confidence=CONFIDENCE_SCHEME_MATCH,
        ))

    for i, (offset, label, heading_text, conf) in enumerate(classified):
        char_end = classified[i + 1][0] if i + 1 < len(classified) else total_len
        spans.append(SectionSpan(
            label=label,
            char_start=offset,
            char_end=char_end,
            heading_text=heading_text,
            confidence=conf,
        ))

    return spans


def _detect_abstract(
    pages: list[PageExtraction],
    full_markdown: str,
    doc: pymupdf.Document,
    sections: list[SectionSpan],
) -> SectionSpan | None:
    """Detect abstract using three-tier approach.

    Tier 2: Already labelled via TOC — return None.
    Tier 1: Keyword match ('abstract') in first 3 pages.
    Tier 3: Font-based detection — differently-styled prose block.
    """
    import re

    # Tier 2: Already detected via TOC
    if any(s.label == "abstract" for s in sections):
        return None

    # Tier 1: Keyword detection in first 3 pages
    for page in pages[:3]:
        page_text = page.markdown
        lower = page_text.lower()
        match = re.search(
            r"(?:^|\n)\s*(?:#{1,3}\s*)?(?:\*\*)?abstract(?:\*\*)?\.?\s*[\n:]?",
            lower,
        )
        if match:
            abs_start = page.char_start + match.start()
            rest = page_text[match.end():]
            next_heading = re.search(r"\n\s*(?:#{1,3}\s|\*\*\d)", rest)
            if next_heading:
                abs_end = page.char_start + match.end() + next_heading.start()
            else:
                abs_end = page.char_start + len(page_text)
            return SectionSpan(
                label="abstract",
                char_start=abs_start,
                char_end=abs_end,
                heading_text="Abstract",
                confidence=CONFIDENCE_SCHEME_MATCH,
            )

    # Tier 3: Font-based detection (find differently-styled prose in first pages)
    if len(doc) < 4:
        return None

    # Compute body font from pages 3+
    font_counts: dict[tuple[str, float], int] = {}
    for page_idx in range(3, min(len(doc), 10)):
        page = doc[page_idx]
        text_dict = page.get_text("dict")
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    font_key = (span.get("font", ""), round(span.get("size", 0), 1))
                    char_count = len(span.get("text", ""))
                    font_counts[font_key] = font_counts.get(font_key, 0) + char_count

    if not font_counts:
        return None

    body_font = max(font_counts, key=font_counts.get)
    body_font_name, body_font_size = body_font

    # Scan first 3 pages for differently-styled prose blocks
    candidates: list[tuple[int, int, str]] = []  # (char_start, char_end, text)
    for page_idx in range(min(3, len(doc))):
        page = doc[page_idx]
        page_obj = pages[page_idx] if page_idx < len(pages) else None
        if page_obj is None:
            continue

        text_dict = page.get_text("dict")
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue

            # Get dominant font for this block
            block_font_counts: dict[tuple[str, float], int] = {}
            block_text = ""
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    font_key = (span.get("font", ""), round(span.get("size", 0), 1))
                    char_count = len(span.get("text", ""))
                    block_font_counts[font_key] = block_font_counts.get(font_key, 0) + char_count
                    block_text += span.get("text", "")
                block_text += " "
            block_text = block_text.strip()

            if not block_text or len(block_text) < 100:
                continue

            # Skip if it looks like affiliations/emails
            if re.search(r"@[\w.]+\.\w+", block_text):
                continue

            if not block_font_counts:
                continue

            block_font = max(block_font_counts, key=block_font_counts.get)
            # Different font = potential abstract
            if block_font != body_font and abs(block_font[1] - body_font_size) > 0.3:
                candidates.append((
                    page_obj.char_start,
                    page_obj.char_start + len(page_obj.markdown),
                    block_text,
                ))

    if len(candidates) == 1:
        return SectionSpan(
            label="abstract",
            char_start=candidates[0][0],
            char_end=candidates[0][1],
            heading_text="Abstract",
            confidence=CONFIDENCE_GAP_FILL,
        )

    return None


def _insert_abstract(
    sections: list[SectionSpan],
    abstract: SectionSpan,
) -> list[SectionSpan]:
    """Insert an abstract span into the sections list, adjusting boundaries."""
    result = []
    inserted = False
    for s in sections:
        if not inserted and s.char_start <= abstract.char_start < s.char_end:
            if abstract.char_start > s.char_start:
                result.append(SectionSpan(
                    label=s.label,
                    char_start=s.char_start,
                    char_end=abstract.char_start,
                    heading_text=s.heading_text,
                    confidence=s.confidence,
                ))
            abs_end = min(abstract.char_end, s.char_end)
            result.append(SectionSpan(
                label="abstract",
                char_start=abstract.char_start,
                char_end=abs_end,
                heading_text="Abstract",
                confidence=CONFIDENCE_SCHEME_MATCH,
            ))
            if abs_end < s.char_end:
                result.append(SectionSpan(
                    label=s.label,
                    char_start=abs_end,
                    char_end=s.char_end,
                    heading_text=s.heading_text,
                    confidence=s.confidence,
                ))
            inserted = True
        else:
            result.append(s)

    if not inserted:
        result.append(abstract)
        result.sort(key=lambda s: s.char_start)

    return result



def _assign_heading_captions(
    doc: pymupdf.Document,
    tables: list[ExtractedTable],
) -> None:
    """Assign captions to orphan tables from bold/italic headings above them.

    Some tables (e.g. "Abbreviations", glossary-style) have a heading above
    that is not formatted as "Table N" but is visually a title.  Scans
    ``page.get_text("dict")`` blocks in the zone above each orphan table
    for short bold or italic text and uses it as the caption.

    The scan zone is adaptive: computed from the page's actual median line
    spacing (median * 4 lines).
    """
    for t in tables:
        if t.caption and not t.caption.startswith(SYNTHETIC_CAPTION_PREFIX):
            continue  # already has a real caption

        page = doc[t.page_num - 1]
        table_top = t.bbox[1]

        # Adaptive scan zone: compute from page's median line spacing
        text_dict = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)
        blocks = text_dict["blocks"]

        line_spacings = []
        for block in blocks:
            if block.get("type") != 0:
                continue
            block_lines = block.get("lines", [])
            for li in range(1, len(block_lines)):
                spacing = block_lines[li]["bbox"][1] - block_lines[li - 1]["bbox"][3]
                if 0 < spacing < 50:
                    line_spacings.append(spacing)

        if line_spacings:
            line_spacings.sort()
            median_spacing = line_spacings[len(line_spacings) // 2]
            # Compute median line height too
            line_heights = []
            for block in blocks:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    h = line["bbox"][3] - line["bbox"][1]
                    if h > 0:
                        line_heights.append(h)
            if line_heights:
                line_heights.sort()
                median_height = line_heights[len(line_heights) // 2]
            else:
                median_height = 12
            scan_distance = (median_spacing + median_height) * 4
        else:
            scan_distance = 60

        scan_top = max(0, table_top - scan_distance)

        best_text = None
        best_y = -1.0

        for block in blocks:
            if block.get("type") != 0:  # text block only
                continue
            for line in block.get("lines", []):
                line_y = line["bbox"][3]  # bottom of line
                if line_y < scan_top or line_y > table_top:
                    continue

                spans = line.get("spans", [])
                if not spans:
                    continue

                text = "".join(s["text"] for s in spans).strip()
                if not text or len(text) > 120:
                    continue
                if len(text.split()) > 15:
                    continue

                # Check if bold or italic via font name patterns
                is_styled = False
                for s in spans:
                    font = s.get("font", "")
                    flags = s.get("flags", 0)
                    if any(p in font for p in (".B", "-Bold", "-bd", "Bold")):
                        is_styled = True
                        break
                    if flags & 2:  # italic flag
                        is_styled = True
                        break

                # Skip running heads / page headers (e.g. "Author Journal 2014, 18:650"
                # or "Sensors 2019, 19, 959")
                if re.search(r"\d{4},?\s*\d+[,:(]\s*\d+", text):
                    continue

                if is_styled and line_y > best_y:
                    best_text = text
                    best_y = line_y

        if best_text:
            # Strip markdown bold markers if present
            cleaned = best_text.strip("*").strip()
            t.caption = cleaned
            logger.debug(
                "Assigned heading caption to orphan table on page %d: '%s'",
                t.page_num, cleaned[:60],
            )


def _assign_continuation_captions(tables: list[ExtractedTable]) -> None:
    """Detect continuation tables and assign inherited captions.

    A table with no caption whose column headers match a captioned table
    on a nearby page (within 2 pages) is treated as a continuation.
    """
    for t in tables:
        if t.caption and not t.caption.startswith(SYNTHETIC_CAPTION_PREFIX):
            continue
        if not t.headers or len(t.headers) < 2:
            continue

        t_key = tuple(h.strip().lower() for h in t.headers if h.strip())
        if not t_key:
            continue

        # Search for a captioned table with matching headers
        for other in tables:
            if other is t:
                continue
            if not other.caption or other.caption.startswith(SYNTHETIC_CAPTION_PREFIX):
                continue
            if abs(other.page_num - t.page_num) > 2:
                continue

            o_key = tuple(h.strip().lower() for h in other.headers if h.strip())
            if t_key == o_key:
                t.caption = f"{other.caption} (continued)"
                logger.debug(
                    "Assigned continuation caption on page %d from page %d: '%s'",
                    t.page_num, other.page_num, t.caption[:60],
                )
                break




# ---------------------------------------------------------------------------
# Content quality detection
# ---------------------------------------------------------------------------

_MATH_GREEK_RE = re.compile(r"[\u0391-\u03C9\u2200-\u22FF=\u00b1\u00d7\u00f7\u00b2\u00b3\u2211\u220f\u222b\u2202\u2207]")  # noqa: E501

def _detect_garbled_spacing(text: str) -> tuple[bool, str]:
    """Flag text where average word length > 25 chars (missing word spaces).

    Skips cells containing Greek letters or math operators — these are
    legitimate technical content, not garbled extraction artifacts.
    Also excludes hyphenated words from the average computation, since
    compound technical terms (e.g. "sulfamethoxazole-trimethoprim") are
    legitimate long words.

    Returns (is_garbled, reason).
    """
    if not text or not text.strip():
        return False, ""
    if _MATH_GREEK_RE.search(text):
        return False, ""
    words = text.split()
    if not words:
        return False, ""
    # Exclude hyphenated words from average (they're compound terms, not garbled)
    non_hyphenated = [w for w in words if "-" not in w]
    if not non_hyphenated:
        return False, ""
    avg_len = sum(len(w) for w in non_hyphenated) / len(non_hyphenated)
    if avg_len > 25:
        return True, f"avg word length {avg_len:.0f} chars (likely merged words)"
    return False, ""


def _normalize_ligatures(text: str | None) -> str | None:
    """Replace common ligature codepoints with their ASCII equivalents."""
    if not text:
        return text
    from ..feature_extraction.postprocessors.cell_cleaning import _normalize_ligatures as _impl
    return _impl(text)


def _detect_interleaved_chars(
    text: str,
    *,
    threshold: float = 0.4,
) -> tuple[bool, str]:
    """Flag text where single-char alphabetic tokens exceed a threshold ratio.

    Only counts alphabetic single-char tokens.  Digits, punctuation,
    and decimal numbers (e.g. ".906", ",") are not interleaving signals.

    Min token count scales with cell size: max(5, len(text)//10).

    Args:
        text: Cell text to analyze.
        threshold: Ratio of single-alpha tokens above which text is
            flagged as interleaved.

    Returns (is_interleaved, reason).
    """
    if not text or not text.strip():
        return False, ""
    tokens = text.split()
    min_tokens = max(5, len(text) // 10)
    if len(tokens) < min_tokens:
        return False, ""
    single_chars = sum(1 for t in tokens if len(t) == 1 and t.isalpha())
    ratio = single_chars / len(tokens)
    if ratio > threshold:
        return True, f"{ratio:.0%} of tokens are single alpha chars (likely interleaved columns)"
    return False, ""


def _detect_encoding_artifacts(text: str) -> tuple[bool, list[str]]:
    """Detect ligature glyphs that indicate encoding problems.

    Returns (has_artifacts, list of found artifact strings).
    """
    # Common ligature codepoints that appear when PDF text extraction
    # fails to decompose ligatures
    _LIGATURES = [
        "\ufb00",  # ff
        "\ufb01",  # fi
        "\ufb02",  # fl
        "\ufb03",  # ffi
        "\ufb04",  # ffl
    ]
    if not text:
        return False, []
    found = [lig for lig in _LIGATURES if lig in text]
    return bool(found), found


def _check_content_readability(table: "ExtractedTable") -> dict:
    """Combine all quality checks into a per-table report.

    Returns dict with keys: garbled_cells, interleaved_cells,
    encoding_artifacts (bool), details (list[str]).
    """
    garbled = 0
    interleaved = 0
    has_encoding = False
    details: list[str] = []

    for ri, row in enumerate(table.rows):
        for ci, cell in enumerate(row):
            g, g_reason = _detect_garbled_spacing(cell)
            if g:
                garbled += 1
                details.append(f"row {ri} col {ci}: {g_reason}")
            i, i_reason = _detect_interleaved_chars(cell)
            if i:
                interleaved += 1
                details.append(f"row {ri} col {ci}: {i_reason}")

    if table.caption:
        enc, enc_list = _detect_encoding_artifacts(table.caption)
        if enc:
            has_encoding = True
            details.append(f"caption encoding artifacts: {enc_list}")

    return {
        "garbled_cells": garbled,
        "interleaved_cells": interleaved,
        "encoding_artifacts": has_encoding,
        "details": details,
    }


# ---------------------------------------------------------------------------
# Stats and quality grading
# ---------------------------------------------------------------------------

def _compute_stats(
    pages: list[PageExtraction], page_chunks: list[dict],
    doc: pymupdf.Document | None = None,
) -> dict:
    """Compute extraction statistics.

    If doc is provided, detects OCR pages by comparing native text
    (page.get_text()) with the markdown output. Pages where native
    text is empty but markdown has content were processed by OCR.
    """
    total_pages = len(pages)
    text_pages = 0
    empty_pages = 0
    ocr_pages = 0

    for i, page in enumerate(pages):
        md = page.markdown.strip()
        if md:
            text_pages += 1
            # Check if this page needed OCR
            if doc and i < len(doc):
                native_text = doc[i].get_text().strip()
                if len(native_text) < 20 and len(md) > 20:
                    ocr_pages += 1
        else:
            empty_pages += 1

    return {
        "total_pages": total_pages,
        "text_pages": text_pages,
        "ocr_pages": ocr_pages,
        "empty_pages": empty_pages,
    }


def _compute_completeness(
    doc: pymupdf.Document,
    pages: list[PageExtraction],
    sections: list[SectionSpan],
    tables: list[ExtractedTable],
    figures: list[ExtractedFigure],
    stats: dict,
) -> "ExtractionCompleteness":  # noqa: F821
    from ..feature_extraction.captions import find_all_captions
    from ..models import ExtractionCompleteness

    fig_nums: set[str] = set()
    tab_nums: set[str] = set()

    for page in doc:
        for cap in find_all_captions(page, include_figures=True, include_tables=True):
            if cap.number:
                if cap.caption_type == "figure":
                    fig_nums.add(cap.number)
                elif cap.caption_type == "table":
                    tab_nums.add(cap.number)

    # At this point, artifacts and false-positive figures have already been
    # removed by extract_document(). Work directly with the cleaned lists.
    tables_with_captions = sum(1 for t in tables if t.caption)

    # --- Content quality signals ---
    garbled_cells = 0
    interleaved_cells = 0
    encoding_artifact_captions = 0
    tables_1x1 = 0
    for t in tables:
        report = _check_content_readability(t)
        garbled_cells += report["garbled_cells"]
        interleaved_cells += report["interleaved_cells"]
        if report["encoding_artifacts"]:
            encoding_artifact_captions += 1
        if t.num_rows <= 1 and t.num_cols <= 1:
            tables_1x1 += 1

    # Duplicate captions: count caption texts that appear more than once.
    # Exclude "(continued)" captions — multi-page tables legitimately
    # produce multiple continuation captions with the same text.
    _CONTINUED_RE = re.compile(r"\(continued\)", re.IGNORECASE)
    all_captions: list[str] = []
    for f in figures:
        if f.caption and not _CONTINUED_RE.search(f.caption):
            all_captions.append(f.caption.strip())
    for t in tables:
        if t.caption and not _CONTINUED_RE.search(t.caption):
            all_captions.append(t.caption.strip())
    seen_captions: set[str] = set()
    duplicate_captions = 0
    for cap in all_captions:
        if cap in seen_captions:
            duplicate_captions += 1
        seen_captions.add(cap)

    # Caption number gaps: find missing integers in 1..max sequences
    def _find_gaps(nums: set[str]) -> list[str]:
        int_nums = set()
        for n in nums:
            try:
                int_nums.add(int(n))
            except ValueError:
                pass  # skip non-integer like "A.1", "S1"
        if not int_nums:
            return []
        full_range = set(range(1, max(int_nums) + 1))
        missing = sorted(full_range - int_nums)
        return [str(m) for m in missing]

    # Compute gaps from caption numbers found on pages
    figure_number_gaps = _find_gaps(fig_nums)
    table_number_gaps = _find_gaps(tab_nums)

    # Unmatched captions: caption numbers found on pages but not on any
    # extracted object's caption.  This is a set-level check (not just count).
    _cap_num_re = re.compile(r"(?:Table|Tab\.?|Figure|Fig\.?)\s+(\d+)", re.IGNORECASE)

    matched_fig_nums: set[str] = set()
    for f in figures:
        if f.caption:
            m = _cap_num_re.search(f.caption)
            if m:
                matched_fig_nums.add(m.group(1))

    matched_tab_nums: set[str] = set()
    for t in tables:
        if t.caption:
            m = _cap_num_re.search(t.caption)
            if m:
                matched_tab_nums.add(m.group(1))

    unmatched_fig = sorted(fig_nums - matched_fig_nums, key=lambda x: (len(x), x))
    unmatched_tab = sorted(tab_nums - matched_tab_nums, key=lambda x: (len(x), x))

    return ExtractionCompleteness(
        text_pages=stats.get("text_pages", 0),
        empty_pages=stats.get("empty_pages", 0),
        ocr_pages=stats.get("ocr_pages", 0),
        figures_found=len(figures),
        figure_captions_found=len(fig_nums),
        figures_missing=max(0, len(fig_nums) - len(figures)),
        tables_found=len(tables),
        table_captions_found=len(tab_nums),
        tables_missing=max(0, len(tab_nums) - len(tables)),
        figures_with_captions=len(figures),
        tables_with_captions=tables_with_captions,
        sections_identified=len([s for s in sections if s.label != "preamble"]),
        unknown_sections=len([s for s in sections if s.label == "unknown"]),
        has_abstract=any(s.label == "abstract" for s in sections),
        garbled_table_cells=garbled_cells,
        interleaved_table_cells=interleaved_cells,
        encoding_artifact_captions=encoding_artifact_captions,
        tables_1x1=tables_1x1,
        duplicate_captions=duplicate_captions,
        figure_number_gaps=figure_number_gaps,
        table_number_gaps=table_number_gaps,
        unmatched_figure_captions=unmatched_fig,
        unmatched_table_captions=unmatched_tab,
    )
