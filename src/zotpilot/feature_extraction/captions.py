"""Unified caption detection — single source of truth for finding table and figure captions."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pymupdf

    from ..models import PageExtraction, SectionSpan

# ---------------------------------------------------------------------------
# Caption number group — shared across all caption patterns
# ---------------------------------------------------------------------------

_NUM_GROUP = r"(\d+|[IVXLCDM]+|[A-Z]\.\d+|S\d+)"

# ---------------------------------------------------------------------------
# Figure caption patterns
# ---------------------------------------------------------------------------

_FIG_CAPTION_RE = re.compile(
    rf"^(?:Figure|Fig\.?)\s+{_NUM_GROUP}\s*[.:(\u2014\u2013-]",
    re.IGNORECASE,
)

_FIG_CAPTION_RE_RELAXED = re.compile(
    rf"^(?:Figure|Fig\.?)\s+{_NUM_GROUP}\s+\S",
    re.IGNORECASE,
)

_FIG_LABEL_ONLY_RE = re.compile(
    rf"^(?:Figure|Fig\.?)\s+{_NUM_GROUP}\s*$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Table caption patterns
# ---------------------------------------------------------------------------

_TABLE_CAPTION_RE = re.compile(
    rf"^(?:\*\*)?(?:Table|Tab\.)\s+{_NUM_GROUP}\s*[.:(\u2014\u2013-]",
    re.IGNORECASE,
)

_TABLE_CAPTION_RE_RELAXED = re.compile(
    rf"^(?:\*\*)?(?:Table|Tab\.)\s+{_NUM_GROUP}\s+\S",
    re.IGNORECASE,
)

_TABLE_LABEL_ONLY_RE = re.compile(
    rf"^(?:\*\*)?(?:Table|Tab\.?)\s+{_NUM_GROUP}\s*$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Supplementary prefix (stripped before caption matching)
# ---------------------------------------------------------------------------

_SUPP_PREFIX_RE = re.compile(r"^(?:supplementary|suppl?\.?)\s+", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Caption number extraction (for parsed number field)
# ---------------------------------------------------------------------------

_CAPTION_NUM_PARSE_RE = re.compile(
    rf"(?:Figure|Fig\.?|Table|Tab\.?)\s+{_NUM_GROUP}",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Font / style helpers
# ---------------------------------------------------------------------------


def _font_name_is_bold(name: str) -> bool:
    """Check if a font name indicates bold weight."""
    if name.endswith(".B") or name.endswith(".b"):
        return True
    lower = name.lower()
    return "bold" in lower or "-bd" in lower


def _block_is_bold(block: dict) -> bool:
    """Check if a block's text is primarily in a bold font.

    Detects bold via flags (bit 4) or font name patterns (.B, -Bold, etc.).
    Some PDFs encode bold only in the font name, not in flags.
    """
    total_chars = 0
    bold_chars = 0
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            n = len(span.get("text", "").strip())
            if n == 0:
                continue
            total_chars += n
            flags = span.get("flags", 0)
            font_name = span.get("font", "")
            if (flags & 16) or _font_name_is_bold(font_name):
                bold_chars += n
    return total_chars > 0 and bold_chars > total_chars * 0.5


def _block_has_label_font_change(block: dict, label_pattern: re.Pattern | None = None) -> bool:
    """Check if a block starts with a caption label in a distinct font.

    Many papers format captions as bold or italic "Figure N" followed by
    normal-weight description text, without any punctuation delimiter. The
    font change between the label and the body is the only signal
    distinguishing a caption from a body-text reference.

    Returns True when the first non-whitespace span uses a different font
    from the second non-whitespace span.
    """
    spans: list[dict] = []
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            if span.get("text", "").strip():
                spans.append(span)
    if len(spans) < 2:
        return False
    return spans[0].get("font") != spans[1].get("font")


def _block_label_on_own_line(block: dict, label_pattern: re.Pattern) -> bool:
    """Check if the block's first line is just a caption label (e.g. 'Table 1').

    Detects captions where label and description are separated by a newline
    rather than punctuation.
    """
    lines = block.get("lines", [])
    if len(lines) < 2:
        return False
    first_line = ""
    for span in lines[0].get("spans", []):
        first_line += span.get("text", "")
    first_line = first_line.strip()
    return bool(label_pattern.match(first_line))


# ---------------------------------------------------------------------------
# DetectedCaption dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectedCaption:
    """A caption detected on a PDF page."""

    text: str
    bbox: tuple[float, float, float, float]
    y_center: float
    caption_type: str  # "table" or "figure"
    number: str | None  # parsed caption number ("1", "A.1", "S2", "IV", or None)


def _parse_caption_number(text: str) -> str | None:
    """Extract the caption number from caption text."""
    m = _CAPTION_NUM_PARSE_RE.search(text)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Internal scanning helpers
# ---------------------------------------------------------------------------


def _text_from_line_onward(block: dict, start_line_idx: int) -> str:
    """Extract text from a specific line index onward in a block."""
    text = ""
    for line in block.get("lines", [])[start_line_idx:]:
        for span in line.get("spans", []):
            text += span.get("text", "")
        text += " "
    return text.strip()


def _scan_lines_for_caption(
    block: dict,
    prefix_re: re.Pattern,
    relaxed_re: re.Pattern | None,
    label_only_re: re.Pattern | None,
) -> str | None:
    """Scan individual lines of a block for a caption pattern.

    When PyMuPDF merges preceding text into the same block as a caption,
    the block-start regex fails. This scans each line and returns text
    from the first matching line onward.

    Returns caption text or None.
    """
    lines = block.get("lines", [])
    if len(lines) < 2:
        return None  # single-line block already tested at block level

    # Only scan first 5 lines -- captions merged with axis labels are
    # always near block start. Body text references buried deep in a
    # paragraph (line 20+) are not captions.
    max_scan = min(5, len(lines))
    for line_idx in range(1, max_scan):  # skip line 0 (already tested)
        line = lines[line_idx]
        line_text = ""
        for span in line.get("spans", []):
            line_text += span.get("text", "")
        line_text = line_text.strip()
        if not line_text:
            continue

        check_line = _SUPP_PREFIX_RE.sub("", line_text)
        if prefix_re.match(check_line):
            return _text_from_line_onward(block, line_idx)
        if label_only_re and label_only_re.match(check_line):
            return _text_from_line_onward(block, line_idx)
        if relaxed_re and relaxed_re.match(check_line):
            sub_block = {"lines": lines[line_idx:]}
            if _block_has_label_font_change(sub_block) or _block_is_bold(sub_block):
                return _text_from_line_onward(block, line_idx)

    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def find_all_captions(
    page: pymupdf.Page,
    *,
    include_figures: bool = True,
    include_tables: bool = True,
) -> list[DetectedCaption]:
    """Find all table and figure captions on a page.

    Scans page text blocks for caption patterns. Uses relaxed regex with
    font-change confirmation (3 structural signals: font change, label on
    own line, bold block).

    Args:
        page: A pymupdf Page object.
        include_figures: Whether to look for figure captions.
        include_tables: Whether to look for table captions.

    Returns:
        All detected captions sorted by y-position (top to bottom).
    """
    text_dict = page.get_text("dict")
    results: list[DetectedCaption] = []

    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        block_bbox = block.get("bbox", (0, 0, 0, 0))
        y_center = (block_bbox[1] + block_bbox[3]) / 2

        block_text = ""
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                block_text += span.get("text", "")
            block_text += " "
        block_text = block_text.strip()

        if not block_text:
            continue

        check_text = _SUPP_PREFIX_RE.sub("", block_text)

        # Try each caption type
        for caption_type, prefix_re, relaxed_re, label_only_re, include in [
            ("figure", _FIG_CAPTION_RE, _FIG_CAPTION_RE_RELAXED, _FIG_LABEL_ONLY_RE, include_figures),
            ("table", _TABLE_CAPTION_RE, _TABLE_CAPTION_RE_RELAXED, _TABLE_LABEL_ONLY_RE, include_tables),
        ]:
            if not include:
                continue

            matched = False
            caption_text = block_text

            if prefix_re.match(check_text):
                matched = True
            elif label_only_re and label_only_re.match(check_text):
                matched = True
            elif relaxed_re and relaxed_re.match(check_text):
                if (
                    _block_has_label_font_change(block)
                    or (label_only_re and _block_label_on_own_line(block, label_only_re))
                    or _block_is_bold(block)
                ):
                    matched = True

            if not matched:
                scanned = _scan_lines_for_caption(block, prefix_re, relaxed_re, label_only_re)
                if scanned:
                    caption_text = scanned
                    matched = True

            if matched:
                results.append(DetectedCaption(
                    text=caption_text,
                    bbox=tuple(block_bbox),
                    y_center=y_center,
                    caption_type=caption_type,
                    number=_parse_caption_number(caption_text),
                ))
                break  # a block can only be one caption type

    results.sort(key=lambda c: c.y_center)
    return results


# ---------------------------------------------------------------------------
# is_in_references utility
# ---------------------------------------------------------------------------


def is_in_references(
    page_num: int,
    sections: list[SectionSpan],
    pages: list[PageExtraction] | None = None,
) -> bool:
    """Check if a page falls within the references section.

    Args:
        page_num: 1-indexed page number.
        sections: List of SectionSpan objects.
        pages: List of PageExtraction objects. If provided, uses
            char_start from the matching page to determine section.
            If None, returns False (cannot determine without page data).

    Returns:
        True if the page is in the references section.
    """
    from ..section_classifier import assign_section

    if pages is not None:
        for p in pages:
            if p.page_num == page_num:
                label = assign_section(p.char_start, sections)
                return label == "references"
        return False

    return False
