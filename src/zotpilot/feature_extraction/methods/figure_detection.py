"""Figure detection — detecting and rendering figures on PDF pages.

Consumes DetectedCaption objects from captions.py.
Called by _extract_figures_for_page() in pdf_processor.py.
"""
from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pymupdf

from ..captions import DetectedCaption

if TYPE_CHECKING:
    from ...models import PageExtraction, SectionSpan

logger = logging.getLogger(__name__)

_MERGE_GAP_PTS = 50  # merge boxes within 50 pts (bridges multi-panel figure gaps)
_DEFAULT_DPI = 150
_CAPTION_NUM_RE = re.compile(r"(\d+)")


def _min_figure_area(page_rect: pymupdf.Rect) -> float:
    """Adaptive minimum figure area derived from page dimensions.

    A figure must be large enough to convey meaningful visual information.
    Threshold is 1/200th of the page area — for US Letter (612x792) this
    yields ~2,424 sq pts (~49x49 pts), scaling naturally with page size.
    """
    return page_rect.get_area() / 200


# ---------------------------------------------------------------------------
# Vector graphics detection
# ---------------------------------------------------------------------------


def _detect_vector_figures(
    page: pymupdf.Page,
    page_rect: pymupdf.Rect,
) -> list[pymupdf.Rect]:
    """Detect figure regions from clustered vector graphics.

    Uses ``page.cluster_drawings()`` — PyMuPDF's native spatial clustering
    of drawing paths.  Filters out clusters that are too small to be figures
    or that span the full page (backgrounds / watermarks).

    This is a fallback for pages where the layout engine and
    ``get_image_info()`` found nothing but captions indicate figures exist.
    """
    try:
        clusters = page.cluster_drawings()
    except Exception:
        return []

    if not clusters:
        return []

    page_area = page_rect.get_area()
    min_area = _min_figure_area(page_rect)
    result: list[pymupdf.Rect] = []

    for c in clusters:
        r = pymupdf.Rect(c)
        if r.is_empty or r.is_infinite:
            continue
        if r.get_area() < min_area:
            continue
        if r.get_area() > page_area * 0.9:
            continue
        r = r & page_rect
        if r.is_empty:
            continue
        result.append(r)

    return result


# ---------------------------------------------------------------------------
# Box merging
# ---------------------------------------------------------------------------


def _merge_rects(rects: list[pymupdf.Rect]) -> list[pymupdf.Rect]:
    """Merge overlapping or nearby rectangles.

    Two rects merge if they overlap or are within _MERGE_GAP_PTS of each other,
    BUT only when they share meaningful horizontal overlap (>20% of the smaller
    rect's width). This prevents side-by-side figures in 2-column layouts from
    being incorrectly merged.
    """
    if not rects:
        return []

    rects = sorted(rects, key=lambda r: (r.y0, r.x0))
    merged: list[pymupdf.Rect] = [pymupdf.Rect(rects[0])]

    for rect in rects[1:]:
        last = merged[-1]

        x_overlap = min(last.x1, rect.x1) - max(last.x0, rect.x0)
        min_width = min(last.width, rect.width)
        if min_width > 0 and x_overlap / min_width < 0.2:
            merged.append(pymupdf.Rect(rect))
            continue

        expanded = pymupdf.Rect(
            last.x0 - _MERGE_GAP_PTS,
            last.y0 - _MERGE_GAP_PTS,
            last.x1 + _MERGE_GAP_PTS,
            last.y1 + _MERGE_GAP_PTS,
        )
        if expanded.intersects(rect):
            merged[-1] = last | rect
        else:
            merged.append(pymupdf.Rect(rect))

    return merged


# ---------------------------------------------------------------------------
# Side-by-side detection
# ---------------------------------------------------------------------------


def _has_side_by_side(objects: list[tuple[float, float, float, float]]) -> bool:
    """Detect whether any objects overlap vertically (side-by-side layout).

    Two objects are side-by-side when their y-ranges overlap by more than 30%
    of the shorter object's height.
    """
    for i in range(len(objects)):
        for j in range(i + 1, len(objects)):
            y0_i, y1_i = objects[i][1], objects[i][3]
            y0_j, y1_j = objects[j][1], objects[j][3]
            overlap = max(0, min(y1_i, y1_j) - max(y0_i, y0_j))
            min_height = min(y1_i - y0_i, y1_j - y0_j)
            if min_height > 0 and overlap / min_height > 0.3:
                return True
    return False


# ---------------------------------------------------------------------------
# Caption-to-figure matching
# ---------------------------------------------------------------------------


def _euclidean_match(
    objects: list[tuple[float, float, float, float]],
    captions_ordered: list[tuple[str, int, str]],
    all_captions: list[tuple[float, str]] | list[tuple[float, str, tuple]],
) -> list[str | None]:
    """Greedy Euclidean-distance assignment: each caption (in number order)
    matches the nearest unassigned object by bbox-center distance.
    """
    result: list[str | None] = [None] * len(objects)
    assigned: set[int] = set()

    obj_centers = [
        ((o[0] + o[2]) / 2, (o[1] + o[3]) / 2)
        for o in objects
    ]

    for _, cap_idx, cap_text in captions_ordered:
        cap = all_captions[cap_idx]
        if len(cap) >= 3 and cap[2]:
            cb = cap[2]
            cx = (cb[0] + cb[2]) / 2
            cy = (cb[1] + cb[3]) / 2
        else:
            cx = 0
            cy = cap[0]

        best_oi = -1
        best_dist = float("inf")
        for oi in range(len(objects)):
            if oi in assigned:
                continue
            ox, oy = obj_centers[oi]
            dist = math.hypot(ox - cx, oy - cy)
            if dist < best_dist:
                best_dist = dist
                best_oi = oi
        if best_oi >= 0:
            result[best_oi] = cap_text
            assigned.add(best_oi)

    return result


def _match_by_proximity(
    objects: list[tuple[float, float, float, float]],
    captions: list[tuple[float, str]] | list[tuple[float, str, tuple]],
) -> list[str | None]:
    """Match captions to objects by number ordering, with proximity fallback.

    Primary strategy: parse caption numbers, sort by number, match to objects
    sorted by y-position.

    When side-by-side objects are detected, switches to Euclidean distance
    matching.
    """
    if not objects:
        return []
    if not captions:
        return [None] * len(objects)

    side_by_side = _has_side_by_side(objects)

    numbered: list[tuple[str, int, str]] = []
    unnumbered: list[tuple[int, str]] = []
    for ci, cap in enumerate(captions):
        text = cap[1]
        m = _CAPTION_NUM_RE.search(text)
        if m:
            numbered.append((m.group(1), ci, text))
        else:
            unnumbered.append((ci, text))

    if numbered:
        def num_sort_key(item: tuple[str, int, str]) -> tuple[int, int | str]:
            num_str = item[0]
            try:
                return (0, int(num_str))
            except ValueError:
                return (1, num_str)

        numbered.sort(key=num_sort_key)

        if side_by_side:
            return _euclidean_match(objects, numbered, captions)

        obj_order = sorted(range(len(objects)), key=lambda i: objects[i][1])
        result: list[str | None] = [None] * len(objects)
        for i, oi in enumerate(obj_order):
            if i < len(numbered):
                result[oi] = numbered[i][2]
        return result

    if side_by_side:
        cap_order = sorted(range(len(captions)), key=lambda i: captions[i][0])
        pseudo = [(str(i), ci, captions[ci][1]) for i, ci in enumerate(cap_order)]
        return _euclidean_match(objects, pseudo, captions)

    obj_order = sorted(range(len(objects)), key=lambda i: objects[i][1])
    cap_order = sorted(range(len(captions)), key=lambda i: captions[i][0])
    result = [None] * len(objects)
    for i, oi in enumerate(obj_order):
        if i < len(cap_order):
            result[oi] = captions[cap_order[i]][1]
    return result


# ---------------------------------------------------------------------------
# Caption conversion helpers
# ---------------------------------------------------------------------------


def _detected_captions_to_tuples(
    captions: list[DetectedCaption],
) -> list[tuple[float, str, tuple]]:
    """Convert DetectedCaption objects to the (y_center, text, bbox) tuple format
    used by the internal matching functions.
    """
    return [(c.y_center, c.text, c.bbox) for c in captions]


# ---------------------------------------------------------------------------
# Box splitting
# ---------------------------------------------------------------------------


def _split_boxes_for_captions(
    rects: list[pymupdf.Rect],
    captions: list[tuple[float, str, tuple]],
) -> list[pymupdf.Rect]:
    """Split picture boxes when there are more captions than boxes.

    Phase 1: Split boxes that have captions INSIDE their y-range.
    Phase 2: When still outnumbered, use caption y-positions to create
             synthetic figure regions.
    """
    if len(captions) <= len(rects):
        return rects

    new_rects: list[pymupdf.Rect] = []

    for rect in sorted(rects, key=lambda r: r.y0):
        internal: list[tuple[int, float, str]] = []
        for ci, cap in enumerate(captions):
            cy = cap[0]
            cap_bbox = cap[2] if len(cap) >= 3 else None
            if rect.y0 < cy < rect.y1:
                if cap_bbox:
                    cap_x0, cap_x1 = cap_bbox[0], cap_bbox[2]
                    x_overlap = min(rect.x1, cap_x1) - max(rect.x0, cap_x0)
                    if x_overlap < 20:
                        continue
                internal.append((ci, cy, cap[1]))

        if not internal:
            new_rects.append(rect)
            continue

        internal.sort(key=lambda x: x[1])
        split_y = rect.y0
        for ci, cy, ctext in internal:
            sub = pymupdf.Rect(rect.x0, split_y, rect.x1, cy)
            if not sub.is_empty and abs(sub.y1 - sub.y0) > 100:
                new_rects.append(sub)
            split_y = cy + 40

        final = pymupdf.Rect(rect.x0, split_y, rect.x1, rect.y1)
        if not final.is_empty and abs(final.y1 - final.y0) > 100:
            new_rects.append(final)

    # Phase 2: synthetic rects for uncovered captions
    if len(captions) > len(new_rects) and new_rects:
        total_height = sum(r.y1 - r.y0 for r in new_rects)
        min_height = 250 * len(captions)
        if total_height >= min_height:
            x0 = min(r.x0 for r in new_rects)
            x1 = max(r.x1 for r in new_rects)
            for cap in sorted(captions, key=lambda c: c[0]):
                cy = cap[0]
                covered = any(r.y0 - 30 <= cy <= r.y1 + 30 for r in new_rects)
                if covered:
                    continue
                fig_top = max(cy - 200, 0)
                fig_bot = cy - 10
                if fig_bot > fig_top + 20:
                    new_rects.append(pymupdf.Rect(x0, fig_top, x1, fig_bot))

    return new_rects if new_rects else rects


# ---------------------------------------------------------------------------
# Public API: detect_figures
# ---------------------------------------------------------------------------


def detect_figures(
    page: pymupdf.Page,
    page_chunk: dict,
    captions: list[DetectedCaption],
    *,
    sections: list[SectionSpan] | None = None,
    pages: list[PageExtraction] | None = None,
) -> list[tuple[tuple[float, float, float, float], str | None]]:
    """Detect figure bboxes on a single page and match captions.

    Args:
        page: The pymupdf Page object.
        page_chunk: The page chunk dict from pymupdf4llm (contains page_boxes).
        captions: List of DetectedCaption objects (figure captions only).
        sections: Section spans for filtering references-section figures.
        pages: Page extractions for section lookup.

    Returns:
        List of (bbox, caption_text) tuples. bbox is (x0, y0, x1, y1).
        caption_text is None if no caption matched.
    """
    page_rect = page.rect
    min_area = _min_figure_area(page_rect)

    # Step 1: Collect picture and table boxes from page_chunk
    picture_rects: list[pymupdf.Rect] = []
    table_rects: list[pymupdf.Rect] = []

    for box in (page_chunk.get("page_boxes") or []):
        bbox = box.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = bbox
        area = abs(x1 - x0) * abs(y1 - y0)
        if area < min_area:
            continue
        rect = pymupdf.Rect(x0, y0, x1, y1)
        rect = rect & page_rect
        if rect.is_empty:
            continue
        if box.get("class") == "picture":
            picture_rects.append(rect)
        elif box.get("class") == "table":
            table_rects.append(rect)

    # Step 2: Use picture rects, fall back to table rects if none
    rects = picture_rects if picture_rects else table_rects

    # Step 3: Image-info fallback when no boxes from layout engine
    if not rects and captions:
        for img_info in page.get_image_info():
            bbox = img_info.get("bbox")
            if not bbox:
                continue
            rect = pymupdf.Rect(bbox)
            area = rect.get_area()
            if area < min_area:
                continue
            if area > page_rect.get_area() * 0.9:
                continue
            rects.append(rect)

    # Step 3b: Vector graphics fallback — drawings-based detection
    if not rects and captions:
        rects = _detect_vector_figures(page, page_rect)
        if rects:
            logger.debug(
                "Vector graphics fallback: %d region(s) from drawings on p%d",
                len(rects), page.number + 1,
            )

    if not rects:
        return []

    # Step 4: Merge overlapping boxes
    rects = _merge_rects(rects)

    # Step 5: Split boxes when captions outnumber boxes
    if captions:
        caption_tuples = _detected_captions_to_tuples(captions)
        rects = _split_boxes_for_captions(rects, caption_tuples)

    # Step 6: Match captions to boxes
    rects_sorted = sorted(rects, key=lambda r: r.y0)
    rect_bboxes = [(r.x0, r.y0, r.x1, r.y1) for r in rects_sorted]

    if captions:
        caption_tuples = _detected_captions_to_tuples(captions)
        matched = _match_by_proximity(rect_bboxes, caption_tuples)
    else:
        matched = [None] * len(rect_bboxes)

    return list(zip(rect_bboxes, matched))


# ---------------------------------------------------------------------------
# Public API: render_figure
# ---------------------------------------------------------------------------


def render_figure(
    doc: pymupdf.Document,
    page_num: int,
    bbox: tuple[float, float, float, float],
    images_dir: Path,
    fig_index: int,
    *,
    dpi: int = 150,
) -> Path | None:
    """Render a figure region to PNG.

    Args:
        doc: Open pymupdf.Document.
        page_num: 1-indexed page number.
        bbox: Figure bounding box (x0, y0, x1, y1).
        images_dir: Directory to save the PNG.
        fig_index: Figure index for filename.
        dpi: Rendering DPI.

    Returns:
        Output path or None on failure.
    """
    try:
        page = doc[page_num - 1]
        rect = pymupdf.Rect(bbox)
        pix = page.get_pixmap(clip=rect, dpi=dpi)
        fname = f"fig_p{page_num:03d}_{fig_index:02d}.png"
        images_dir.mkdir(parents=True, exist_ok=True)
        out_path = images_dir / fname
        pix.save(str(out_path))
        return out_path
    except Exception as e:
        logger.warning("Failed to render figure %d on page %d: %s",
                       fig_index, page_num, e)
        return None
