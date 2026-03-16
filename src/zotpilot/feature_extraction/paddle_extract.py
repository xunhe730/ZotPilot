"""Engine protocol, data models, and factory for PaddleOCR-based table extraction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .captions import DetectedCaption


@runtime_checkable
class PaddleEngine(Protocol):
    """Protocol that all PaddleOCR engine implementations must satisfy."""

    def extract_tables(self, pdf_path: Path) -> list[RawPaddleTable]:
        """Extract all tables from a PDF file.

        Args:
            pdf_path: Absolute path to the PDF file.

        Returns:
            List of raw table extractions in engine-native pixel coordinates.
        """
        ...


@dataclass
class RawPaddleTable:
    """Intermediate representation of a table as returned by a PaddleOCR engine.

    Bounding box coordinates are in engine-native pixel space and must be
    normalised to PDF points by the caller using page_size.
    """

    page_num: int
    bbox: tuple[float, float, float, float]
    page_size: tuple[int, int]
    headers: list[str]
    rows: list[list[str]]
    footnotes: str
    engine_name: str
    raw_output: str


@dataclass
class MatchedPaddleTable:
    """A PaddleOCR-detected table with an assigned caption (or orphan status).

    All fields from RawPaddleTable are reproduced here so downstream code
    has a single self-contained object without needing the original raw table.
    """

    page_num: int
    bbox: tuple[float, float, float, float]
    page_size: tuple[int, int]
    headers: list[str]
    rows: list[list[str]]
    footnotes: str
    engine_name: str
    raw_output: str
    caption: str | None
    caption_number: str | None
    is_orphan: bool


def match_tables_to_captions(
    raw_tables: list[RawPaddleTable],
    captions_by_page: dict[int, list[DetectedCaption]],
    page_rects: dict[int, tuple[float, float, float, float]],
) -> list[MatchedPaddleTable]:
    """Match PaddleOCR-detected table regions to captions by normalized vertical proximity.

    For each page, both coordinate systems are normalized to [0, 1] fractions of
    the page height. Each table is assigned to the closest caption whose center
    lies above the table's top edge. Assignment is greedy (top-to-bottom) so each
    caption is used at most once.

    Args:
        raw_tables: Tables returned by a PaddleEngine, in pixel coordinates.
        captions_by_page: Caption detections from find_all_captions(), keyed by
            1-indexed page number. Caption bboxes are in PDF points.
        page_rects: PDF page rects (x0, y0, x1, y1) in points, keyed by
            1-indexed page number. Callers must convert PyMuPDF Rect objects to
            plain tuples before passing.

    Returns:
        One MatchedPaddleTable per input RawPaddleTable, preserving input order
        within each page but with pages processed top-to-bottom.
    """
    # Group tables by page, preserving original index for output ordering.
    pages_with_tables: dict[int, list[tuple[int, RawPaddleTable]]] = {}
    for idx, table in enumerate(raw_tables):
        pages_with_tables.setdefault(table.page_num, []).append((idx, table))

    results: list[tuple[int, MatchedPaddleTable]] = []

    for page_num, indexed_tables in pages_with_tables.items():
        page_captions = list(captions_by_page.get(page_num, []))
        page_rect = page_rects.get(page_num)

        # Compute per-page PDF point height for caption normalization.
        if page_rect is not None:
            pt_y0, pt_y1 = page_rect[1], page_rect[3]
            pt_height = pt_y1 - pt_y0
        else:
            pt_y0, pt_height = 0.0, 1.0  # fallback: treat as already normalized

        def _caption_y_norm(cap: DetectedCaption) -> float:
            if pt_height == 0.0:
                return cap.y_center
            return (cap.y_center - pt_y0) / pt_height

        # Sort tables top-to-bottom by normalized y0 for greedy assignment.
        def _table_y0_norm(item: tuple[int, RawPaddleTable]) -> float:
            _, tbl = item
            px_height = tbl.page_size[1]
            if px_height == 0:
                return tbl.bbox[1]
            return tbl.bbox[1] / px_height

        sorted_tables = sorted(indexed_tables, key=_table_y0_norm)

        # Mutable pool of caption candidates (removed when matched).
        caption_pool: list[DetectedCaption] = list(page_captions)

        for orig_idx, table in sorted_tables:
            px_height = table.page_size[1]
            table_y0_norm = table.bbox[1] / px_height if px_height != 0 else table.bbox[1]

            # Find all captions whose center is at or above this table's top edge.
            candidates = [
                (cap, _caption_y_norm(cap))
                for cap in caption_pool
                if _caption_y_norm(cap) <= table_y0_norm
            ]

            if candidates:
                # Pick the caption closest to (but above) the table's top edge.
                best_cap, _ = max(candidates, key=lambda item: item[1])
                caption_pool.remove(best_cap)
                matched = MatchedPaddleTable(
                    page_num=table.page_num,
                    bbox=table.bbox,
                    page_size=table.page_size,
                    headers=table.headers,
                    rows=table.rows,
                    footnotes=table.footnotes,
                    engine_name=table.engine_name,
                    raw_output=table.raw_output,
                    caption=best_cap.text,
                    caption_number=best_cap.number,
                    is_orphan=False,
                )
            else:
                matched = MatchedPaddleTable(
                    page_num=table.page_num,
                    bbox=table.bbox,
                    page_size=table.page_size,
                    headers=table.headers,
                    rows=table.rows,
                    footnotes=table.footnotes,
                    engine_name=table.engine_name,
                    raw_output=table.raw_output,
                    caption=None,
                    caption_number=None,
                    is_orphan=True,
                )

            results.append((orig_idx, matched))

    # Restore original input order.
    results.sort(key=lambda item: item[0])
    return [matched for _, matched in results]


def get_engine(name: str) -> PaddleEngine:
    """Return an initialised engine instance for the given engine name.

    Model loading occurs inside each engine's ``__init__``, so the returned
    instance is ready to call ``extract_tables`` immediately.

    Args:
        name: Engine identifier. Supported values:
            ``"pp_structure_v3"`` — PP-StructureV3 (HTML output)
            ``"paddleocr_vl_1.5"`` — PaddleOCR-VL-1.5 (markdown output)

    Raises:
        ValueError: If *name* does not match any known engine.
    """
    if name == "pp_structure_v3":
        from .paddle_engines import PPStructureEngine
        return PPStructureEngine()
    if name == "paddleocr_vl_1.5":
        from .paddle_engines import PaddleOCRVLEngine
        return PaddleOCRVLEngine()
    raise ValueError(f"Unknown engine name: {name!r}")
