"""Post-extraction recovery pass for orphan figures and tables.

Runs AFTER initial figure/table extraction, BEFORE completeness grading.
Audits results, detects orphans and floating captions, attempts targeted
pairing via y-proximity matching and numbering-gap search.

Does NOT create new figure/table objects — only fills in missing captions
on existing ones.
"""
from __future__ import annotations

import logging
import re

import pymupdf

from ..models import ExtractedFigure, ExtractedTable, SectionSpan, PageExtraction

logger = logging.getLogger(__name__)

# Caption number extraction — matches plain digits or appendix-style (A.1, S1)
_CAPTION_NUM_RE = re.compile(r"(\d+|[A-Z]\.\d+|S\d+)")

# Relaxed caption patterns for gap search (not block-start anchored)
_NUM_GROUP = r"(\d+|[IVXLCDM]+|[A-Z]\.\d+|S\d+)"
_FIG_REF_RE = re.compile(
    rf"(?:Figure|Fig\.?)\s+{_NUM_GROUP}\s*[.:(\u2014\u2013-]",
    re.IGNORECASE,
)
_TABLE_REF_RE = re.compile(
    rf"(?:Table|Tab\.)\s+{_NUM_GROUP}\s*[.:(\u2014\u2013-]",
    re.IGNORECASE,
)

# Body-text reference context (reject mid-paragraph references)
_BODY_REF_CONTEXT_RE = re.compile(
    r"(?:as\s+(?:shown|seen|described|illustrated|presented|depicted|listed|"
    r"reported|summarized|given|indicated)\s+in\s+|"
    r"(?:see|refer\s+to|shown\s+in|cf\.?)\s+)",
    re.IGNORECASE,
)

def _adaptive_max_y_distance(
    doc: pymupdf.Document,
    *,
    page_fraction: float = 0.15,
) -> float:
    """Compute fallback y-proximity threshold from page height.

    Used when fewer than 3 matched caption-item pairs are available
    for calibration.  Returns *page_fraction* of the median page height.
    """
    _ASSUMED_PAGE_HEIGHT = 792.0  # letter-size fallback
    if not doc or len(doc) == 0:
        return _ASSUMED_PAGE_HEIGHT * page_fraction
    heights = [doc[i].rect.height for i in range(min(len(doc), 5))]
    heights.sort()
    median_height = heights[len(heights) // 2]
    return median_height * page_fraction


def run_recovery(
    doc: pymupdf.Document,
    figures: list[ExtractedFigure],
    tables: list[ExtractedTable],
    page_chunks: list[dict],
    sections: list[SectionSpan] | None = None,
    pages: list[PageExtraction] | None = None,
) -> tuple[list[ExtractedFigure], list[ExtractedTable]]:
    """Run post-extraction recovery to fill orphan captions.

    Args:
        doc: Open pymupdf.Document.
        figures: Extracted figures (may have None captions).
        tables: Extracted tables (may have None captions).
        page_chunks: Output of pymupdf4llm.to_markdown(page_chunks=True).
        sections: Section spans (unused currently, reserved for future).
        pages: Page extractions (unused currently, reserved for future).

    Returns:
        Updated (figures, tables) with recovered captions filled in.
    """
    from ..feature_extraction.captions import find_all_captions

    max_y_dist = _adaptive_max_y_distance(doc)

    fig_recoveries = _recover_captions(
        doc, figures, find_all_captions,
        _FIG_REF_RE, kind="figure", max_y_distance=max_y_dist,
    )
    tab_recoveries = _recover_captions(
        doc, tables, find_all_captions,
        _TABLE_REF_RE, kind="table", max_y_distance=max_y_dist,
    )

    if fig_recoveries or tab_recoveries:
        logger.info(
            "Recovery pass: %d figure caption(s), %d table caption(s) recovered",
            fig_recoveries, tab_recoveries,
        )

    return figures, tables


def _recover_captions(
    doc: pymupdf.Document,
    items: list[ExtractedFigure] | list[ExtractedTable],
    caption_finder,
    ref_re: re.Pattern,
    kind: str,
    max_y_distance: float,
) -> int:
    """Core recovery logic for either figures or tables.

    Returns the number of captions recovered.
    """
    if not items:
        return 0

    include_figures = kind == "figure"
    include_tables = kind == "table"

    # --- Step 1: Audit ---
    # Map caption numbers (as strings) to item indices
    assigned_nums: dict[str, int] = {}
    orphan_indices: list[int] = []

    for idx, item in enumerate(items):
        if item.caption:
            m = _CAPTION_NUM_RE.search(item.caption)
            if m:
                assigned_nums[m.group(1)] = idx
        else:
            orphan_indices.append(idx)

    if not orphan_indices:
        return 0  # nothing to recover

    # Scan all pages for captions -> find floating (unassigned) ones
    # Also collect distances from assigned captions to their items for calibration
    floating_captions: list[dict] = []  # {num, y_center, text, page_num}
    matched_distances: list[float] = []
    for page_num_0, page in enumerate(doc):
        page_num = page_num_0 + 1
        detected = caption_finder(
            page,
            include_figures=include_figures,
            include_tables=include_tables,
        )
        for cap in detected:
            num_str = cap.number
            if not num_str:
                continue
            if num_str in assigned_nums:
                # Measure distance from this caption to its matched item
                item_idx = assigned_nums[num_str]
                item = items[item_idx]
                if item.page_num == page_num:
                    dist = abs(cap.y_center - item.bbox[3])
                    matched_distances.append(dist)
            else:
                floating_captions.append({
                    "num": num_str,
                    "y_center": cap.y_center,
                    "text": cap.text,
                    "page_num": page_num,
                    "bbox": cap.bbox,
                })

    # Calibrate threshold from matched caption-to-item distances
    if len(matched_distances) >= 3:
        matched_distances.sort()
        n_md = len(matched_distances)
        q1 = matched_distances[n_md // 4]
        q3 = matched_distances[3 * n_md // 4]
        iqr = q3 - q1
        effective_max_dist = q3 + 1.5 * iqr
        logger.debug(
            "Recovery %s: calibrated max_y_distance=%.0f from %d matched pairs "
            "(Q1=%.0f Q3=%.0f IQR=%.0f, passed default=%.0f)",
            kind, effective_max_dist, n_md, q1, q3, iqr, max_y_distance,
        )
    else:
        effective_max_dist = max_y_distance

    # --- Step 2: Y-Proximity Matching (orphans ↔ floating captions) ---
    recoveries = 0
    matched_floating: set[int] = set()  # indices into floating_captions

    # Sort orphans top-to-bottom within each page for greedy assignment
    orphan_indices_sorted = sorted(
        orphan_indices,
        key=lambda i: (items[i].page_num, items[i].bbox[1]),
    )

    for orphan_idx in orphan_indices_sorted:
        orphan = items[orphan_idx]
        orphan_page = orphan.page_num
        orphan_bottom = orphan.bbox[3]  # y1

        best_dist = effective_max_dist + 1
        best_fc_idx = -1

        for fc_idx, fc in enumerate(floating_captions):
            if fc_idx in matched_floating:
                continue
            if fc["page_num"] != orphan_page:
                continue
            dist = abs(fc["y_center"] - orphan_bottom)
            if dist < best_dist:
                best_dist = dist
                best_fc_idx = fc_idx

        if best_fc_idx >= 0 and best_dist <= effective_max_dist:
            items[orphan_idx].caption = floating_captions[best_fc_idx]["text"]
            num = floating_captions[best_fc_idx]["num"]
            assigned_nums[num] = orphan_idx
            matched_floating.add(best_fc_idx)
            recoveries += 1
            logger.debug(
                "Recovery: %s orphan p%d matched to floating caption %s (dist=%.0f)",
                kind, orphan_page, num, best_dist,
            )

    # Refresh orphan list after proximity matching
    orphan_indices = [i for i in orphan_indices if items[i].caption is None]
    if not orphan_indices:
        return recoveries

    # --- Step 3: Numbering Gap Search ---
    # Only works with integer-numbered captions (not appendix "A.1", "S1" etc.)
    if not assigned_nums:
        return recoveries

    int_nums: dict[int, int] = {}
    for k, v in assigned_nums.items():
        try:
            int_nums[int(k)] = v
        except ValueError:
            pass  # skip appendix-style numbers

    if not int_nums:
        return recoveries

    min_num = min(int_nums)
    max_num = max(int_nums)
    gap_nums = [n for n in range(min_num, max_num + 1)
                if str(n) not in assigned_nums]

    # Also check floating captions that weren't matched
    unmatched_floating_nums = {
        fc["num"] for fc_idx, fc in enumerate(floating_captions)
        if fc_idx not in matched_floating
    }
    gap_nums = [n for n in gap_nums if str(n) not in unmatched_floating_nums]

    for gap_num in gap_nums:
        # Determine page range: between pages of adjacent assigned captions
        lower_pages = [items[int_nums[n]].page_num
                       for n in int_nums if n < gap_num]
        upper_pages = [items[int_nums[n]].page_num
                       for n in int_nums if n > gap_num]

        page_lo = max(lower_pages) if lower_pages else 1
        page_hi = min(upper_pages) if upper_pages else len(doc)

        found_caption = _search_page_text_for_caption(
            doc, ref_re, gap_num, page_lo, page_hi, kind,
        )
        if not found_caption:
            continue

        # Try to match to an orphan on the found page
        caption_text, caption_page = found_caption
        best_orphan = None
        best_dist = float("inf")

        for orphan_idx in orphan_indices:
            orphan = items[orphan_idx]
            if orphan.page_num != caption_page:
                continue
            dist = orphan_idx  # prefer earlier orphan on same page
            if dist < best_dist:
                best_dist = dist
                best_orphan = orphan_idx

        if best_orphan is not None:
            items[best_orphan].caption = caption_text
            assigned_nums[str(gap_num)] = best_orphan
            orphan_indices.remove(best_orphan)
            recoveries += 1
            logger.debug(
                "Recovery: %s gap %d filled on p%d",
                kind, gap_num, caption_page,
            )

    return recoveries


def _search_page_text_for_caption(
    doc: pymupdf.Document,
    ref_re: re.Pattern,
    target_num: int,
    page_lo: int,
    page_hi: int,
    kind: str,
) -> tuple[str, int] | None:
    """Search page text for a specific caption number.

    Returns (caption_text, page_num) or None.
    Rejects body-text references like "as shown in Figure N".
    """
    for page_num_0 in range(page_lo - 1, min(page_hi, len(doc))):
        page = doc[page_num_0]
        page_num = page_num_0 + 1
        text_dict = page.get_text("dict")

        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            block_text = ""
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    block_text += span.get("text", "")
                block_text += " "
            block_text = block_text.strip()

            if not block_text:
                continue

            # Only check block start (first 400 chars) for caption pattern
            check_window = block_text[:400]
            m = ref_re.match(check_window)
            if not m:
                continue

            num = int(m.group(1))
            if num != target_num:
                continue

            # Reject body-text references by checking preceding context
            start_pos = m.start()
            preceding = block_text[max(0, start_pos - 60):start_pos]
            if _BODY_REF_CONTEXT_RE.search(preceding):
                continue

            return block_text, page_num

    return None
