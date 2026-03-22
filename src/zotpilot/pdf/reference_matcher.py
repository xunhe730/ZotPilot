"""Map tables/figures to the chunks that first reference them."""
from __future__ import annotations

import re
from bisect import bisect_right

from ..models import Chunk, ExtractedFigure, ExtractedTable


def match_references(
    full_markdown: str,
    chunks: list[Chunk],
    tables: list[ExtractedTable],
    figures: list[ExtractedFigure],
) -> dict[tuple[str, int], int]:
    """Map (element_type, caption_number) -> chunk_index of first reference.

    Scans full_markdown for patterns like "Table 1", "Fig. 3", "Figure 12".
    Parses caption numbers from ExtractedTable.caption and
    ExtractedFigure.caption. Returns mapping for matched items.

    Fallback for unreferenced items: page-based estimate — the chunk whose
    page_num matches the table/figure's page_num. If multiple, use the first.
    """
    if not chunks:
        return {}

    # Build sorted chunk start offsets for bisect
    chunk_starts = [c.char_start for c in chunks]

    # Scan full_markdown for all "Table N", "Fig. N", "Figure N" references
    table_ref_re = re.compile(r"(?:Table|Tab\.?)\s+(\d+)", re.IGNORECASE)
    fig_ref_re = re.compile(r"(?:Figure|Fig\.?)\s+(\d+)", re.IGNORECASE)

    # Find first occurrence char offset for each reference
    first_table_ref: dict[int, int] = {}  # caption_num -> char_offset
    for m in table_ref_re.finditer(full_markdown):
        num = int(m.group(1))
        if num not in first_table_ref:
            first_table_ref[num] = m.start()

    first_fig_ref: dict[int, int] = {}
    for m in fig_ref_re.finditer(full_markdown):
        num = int(m.group(1))
        if num not in first_fig_ref:
            first_fig_ref[num] = m.start()

    # Build page_num -> first chunk_index mapping for fallback
    page_to_chunk: dict[int, int] = {}
    for c in chunks:
        if c.page_num not in page_to_chunk:
            page_to_chunk[c.page_num] = c.chunk_index

    ref_map: dict[tuple[str, int], int] = {}

    # Map tables
    for table in tables:
        caption_num = _parse_caption_num(table.caption)
        if caption_num is None:
            continue
        if caption_num in first_table_ref:
            offset = first_table_ref[caption_num]
            idx = bisect_right(chunk_starts, offset) - 1
            ref_map[("table", caption_num)] = max(0, idx)
        elif table.page_num in page_to_chunk:
            ref_map[("table", caption_num)] = page_to_chunk[table.page_num]

    # Map figures
    for fig in figures:
        caption_num = _parse_caption_num(fig.caption)
        if caption_num is None:
            continue
        if caption_num in first_fig_ref:
            offset = first_fig_ref[caption_num]
            idx = bisect_right(chunk_starts, offset) - 1
            ref_map[("figure", caption_num)] = max(0, idx)
        elif fig.page_num in page_to_chunk:
            ref_map[("figure", caption_num)] = page_to_chunk[fig.page_num]

    return ref_map


def get_reference_context(
    full_markdown: str,
    chunks: list[Chunk],
    ref_map: dict[tuple[str, int], int],
    element_type: str,
    caption_num: int,
) -> str | None:
    """Return the text of the chunk containing the first reference.

    Used by Fix 5 to enrich figure/table embeddings.
    """
    chunk_index = ref_map.get((element_type, caption_num))
    if chunk_index is None:
        return None
    for c in chunks:
        if c.chunk_index == chunk_index:
            return c.text
    return None


def _parse_caption_num(caption: str | None) -> int | None:
    """Extract the first integer from a caption string."""
    if not caption:
        return None
    m = re.search(r"(\d+)", caption)
    return int(m.group(1)) if m else None
