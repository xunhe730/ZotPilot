"""
All dataclasses for the system. No dependencies on implementation modules.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .indexer import IndexResult

# Section detection confidence levels
CONFIDENCE_SCHEME_MATCH = 1.0   # Heading matched a known scheme pattern
CONFIDENCE_GAP_FILL = 0.7      # Inferred from gap-filling heuristic
CONFIDENCE_FALLBACK = 0.5      # Default when detection fails


# =============================================================================
# ZOTERO MODELS
# =============================================================================

@dataclass
class ZoteroItem:
    """A bibliographic item from Zotero with optional PDF attachment."""
    item_key: str
    title: str
    authors: str              # "Smith, J." or "Smith, J. et al."
    year: int | None
    pdf_path: Path | None     # Resolved filesystem path to PDF
    citation_key: str = ""    # BetterBibTeX citation key
    publication: str = ""     # Journal/conference name
    journal_quartile: str | None = None  # SCImago quartile (Q1/Q2/Q3/Q4)
    doi: str = ""             # Digital Object Identifier
    tags: str = ""            # Semicolon-separated Zotero tags
    collections: str = ""     # Semicolon-separated collection names


# =============================================================================
# PDF EXTRACTION MODELS
# =============================================================================

@dataclass
class PageExtraction:
    """Extraction results for a single PDF page."""
    page_num: int          # 1-indexed
    markdown: str          # Markdown text for this page
    char_start: int        # Offset in full document markdown
    tables_on_page: int = 0   # Count of tables detected on this page
    images_on_page: int = 0   # Count of images detected on this page


@dataclass
class DocumentExtraction:
    """Complete extraction results for a PDF."""
    pages: list[PageExtraction]
    full_markdown: str
    sections: list[SectionSpan]
    tables: list[ExtractedTable]
    figures: list[ExtractedFigure]
    stats: dict
    quality_grade: str
    completeness: ExtractionCompleteness | None = None
    vision_details: list[dict] | None = None
    pending_vision: object | None = field(default=None, repr=False)


@dataclass
class ExtractedFigure:
    """A figure extracted from a PDF."""
    page_num: int
    figure_index: int
    bbox: tuple[float, float, float, float]
    caption: str | None  # None for orphaned figures (no caption found)
    image_path: Path | None = None  # Path to saved PNG
    reference_context: str | None = None

    def to_searchable_text(self) -> str:
        """Return text for embedding."""
        if self.caption:
            text = self.caption
        else:
            text = f"Figure on page {self.page_num}"
        if self.reference_context:
            text += f"\n{self.reference_context}"
        return text


@dataclass
class ExtractionCompleteness:
    """Measures what was captured vs what exists in the document."""
    text_pages: int
    empty_pages: int
    ocr_pages: int
    figures_found: int
    figure_captions_found: int      # unique figure numbers from caption blocks on pages
    figures_missing: int            # captions_found - figures_found
    tables_found: int
    table_captions_found: int       # unique table numbers from caption blocks on pages
    tables_missing: int             # captions_found - tables_found
    figures_with_captions: int = 0  # extracted figures that have a caption assigned
    tables_with_captions: int = 0   # extracted tables that have a caption assigned
    sections_identified: int = 0
    unknown_sections: int = 0
    has_abstract: bool = False
    garbled_table_cells: int = 0
    interleaved_table_cells: int = 0
    encoding_artifact_captions: int = 0
    tables_1x1: int = 0
    duplicate_captions: int = 0
    figure_number_gaps: list[str] = field(default_factory=list)
    table_number_gaps: list[str] = field(default_factory=list)
    unmatched_figure_captions: list[str] = field(default_factory=list)  # caption numbers on pages not on any figure
    unmatched_table_captions: list[str] = field(default_factory=list)   # caption numbers on pages not on any table

    @property
    def grade(self) -> str:
        """Letter grade based on extraction completeness.

        F: no text, or 2+ fields completely missed
        D: any single field completely missed
        C: >20% of figures or tables lack captions
        B: some missing but <=20%
        A: nothing missing, has sections

        Fields: table captions, figure captions, figures, tables.
        A field is "completely missed" when we have evidence it should
        exist (objects found or captions found) but the other side is zero.
        """
        if self.text_pages == 0:
            return "F"

        # Count completely missed fields
        missed_fields = 0
        if self.tables_found > 0 and self.tables_with_captions == 0:
            missed_fields += 1  # table captions completely missed
        if self.figures_found > 0 and self.figures_with_captions == 0:
            missed_fields += 1  # figure captions completely missed
        if self.figure_captions_found > 0 and self.figures_found == 0:
            missed_fields += 1  # figures completely missed
        if self.table_captions_found > 0 and self.tables_found == 0:
            missed_fields += 1  # tables completely missed

        if missed_fields >= 2:
            return "F"
        if missed_fields >= 1:
            return "D"

        # Check for any missing items
        uncaptioned_figs = self.figures_found - self.figures_with_captions
        uncaptioned_tabs = self.tables_found - self.tables_with_captions
        any_missing = (
            self.figures_missing > 0
            or self.tables_missing > 0
            or uncaptioned_figs > 0
            or uncaptioned_tabs > 0
        )

        if not any_missing and self.sections_identified > 0:
            return "A"

        # >20% captions missing in any field -> C
        fig_caption_rate = (
            self.figures_with_captions / self.figures_found
            if self.figures_found > 0 else 1.0
        )
        tab_caption_rate = (
            self.tables_with_captions / self.tables_found
            if self.tables_found > 0 else 1.0
        )

        if fig_caption_rate < 0.8 or tab_caption_rate < 0.8:
            return "C"

        return "B"


# =============================================================================
# CHUNKING MODELS
# =============================================================================

@dataclass
class Chunk:
    """A text chunk from a document with position metadata."""
    text: str
    chunk_index: int          # Sequential index within document
    page_num: int             # Primary page (1-indexed)
    char_start: int           # Start offset in full document
    char_end: int             # End offset in full document
    section: str = "unknown"  # Document section (abstract, introduction, methods, etc.)
    section_confidence: float = 1.0  # Confidence of section assignment (0.0-1.0)


@dataclass
class SectionSpan:
    """A detected section span within a document."""
    label: str                # Section category label
    char_start: int           # Start offset in concatenated text
    char_end: int             # End offset in concatenated text
    heading_text: str         # The matched heading line, or "" for preamble/unknown
    confidence: float         # 0.0-1.0, use CONFIDENCE_* constants


# =============================================================================
# TABLE EXTRACTION MODELS
# =============================================================================

@dataclass
class ExtractedTable:
    """A table extracted from a PDF page."""
    page_num: int                              # 1-indexed
    table_index: int                           # Index within page (0-based)
    bbox: tuple[float, float, float, float]    # (x0, y0, x1, y1) bounding box
    headers: list[str]                         # Column headers (may be empty)
    rows: list[list[str]]                      # Data rows
    caption: str | None = ""                   # Detected caption text (None for orphans)
    caption_position: str = ""                 # "above" | "below" | ""
    footnotes: str = ""                        # Footnote text stripped from bottom rows
    reference_context: str | None = None
    artifact_type: str | None = None           # None=real data, else layout artifact tag
    extraction_strategy: str = ""               # which multi-strategy winner produced cell text

    @property
    def num_rows(self) -> int:
        """Number of data rows (excludes header)."""
        return len(self.rows)

    @property
    def num_cols(self) -> int:
        """Number of columns."""
        header_count = len(self.headers) if self.headers else 0
        row_count = max((len(r) for r in self.rows), default=0) if self.rows else 0
        return max(header_count, row_count)

    def to_markdown(self) -> str:
        """Convert table to markdown format for embedding."""
        lines = []

        # Add caption if present
        if self.caption:
            lines.append(f"**{self.caption}**\n")

        # Headers
        if self.headers:
            lines.append("| " + " | ".join(self.headers) + " |")
            lines.append("| " + " | ".join(["---"] * len(self.headers)) + " |")

        # Rows
        for row in self.rows:
            # Pad row to match header count if needed
            padded = row + [""] * (self.num_cols - len(row))
            lines.append("| " + " | ".join(padded[:self.num_cols]) + " |")

        # Footnotes
        if self.footnotes:
            lines.append("")
            lines.append(f"*{self.footnotes}*")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return {
            "page": self.page_num,
            "table_index": self.table_index,
            "headers": self.headers,
            "rows": self.rows,
            "caption": self.caption,
            "num_rows": self.num_rows,
            "num_cols": self.num_cols,
        }


# =============================================================================
# VECTOR STORE MODELS
# =============================================================================

@dataclass
class StoredChunk:
    """A chunk retrieved from the vector store."""
    id: str
    text: str
    metadata: dict
    score: float = 0.0        # Similarity score (0-1, higher = more similar)


# =============================================================================
# RETRIEVAL MODELS
# =============================================================================

@dataclass
class RetrievalResult:
    """A search result with expanded context."""
    chunk_id: str
    text: str
    score: float
    doc_id: str
    doc_title: str
    authors: str
    year: int | None
    page_num: int
    chunk_index: int
    citation_key: str = ""
    publication: str = ""
    section: str = "unknown"
    section_confidence: float = 1.0  # Confidence of section detection (0.0-1.0)
    tags: str = ""
    collections: str = ""
    journal_quartile: str | None = None
    composite_score: float | None = None  # Reranked score (similarity x section x journal)
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)

    def full_context(self) -> str:
        """Return chunk with surrounding context merged."""
        parts = self.context_before + [self.text] + self.context_after
        return "\n\n".join(parts)


@dataclass
class SearchResponse:
    """Complete search response."""
    query: str
    results: list[RetrievalResult]
    total_hits: int


# =============================================================================
# INDEXING REPORT MODELS
# =============================================================================

@dataclass
class IndexReport:
    """Complete indexing run report."""
    total_items: int
    indexed: int
    skipped: int
    failed: int
    empty: int
    already_indexed: int
    results: list[IndexResult]
    extraction_stats: dict  # OCR pages, text pages, etc.
    quality_distribution: dict[str, int]  # Grade -> count

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        return {
            "summary": {
                "total_items": self.total_items,
                "indexed": self.indexed,
                "skipped": self.skipped,
                "failed": self.failed,
                "empty": self.empty,
                "already_indexed": self.already_indexed,
            },
            "extraction_stats": self.extraction_stats,
            "quality_distribution": self.quality_distribution,
            "failures": [
                {"item_key": r.item_key, "title": r.title, "reason": r.reason,
                 "quality_grade": r.quality_grade}
                for r in self.results if r.status == "failed"
            ],
            "empty_documents": [
                {"item_key": r.item_key, "title": r.title, "reason": r.reason}
                for r in self.results if r.status == "empty"
            ],
            "indexed_documents": [
                {"item_key": r.item_key, "title": r.title,
                 "n_chunks": r.n_chunks, "n_tables": r.n_tables,
                 "quality_grade": r.quality_grade}
                for r in self.results if r.status == "indexed"
            ],
        }

    def to_markdown(self) -> str:
        """Generate markdown report."""
        lines = [
            "# Indexing Report",
            "",
            "## Summary",
            "",
            f"- **Total items processed:** {self.total_items}",
            f"- **Newly indexed:** {self.indexed}",
            f"- **Already in index:** {self.already_indexed}",
            f"- **Empty (no text):** {self.empty}",
            f"- **Skipped (unchanged):** {self.skipped}",
            f"- **Failed:** {self.failed}",
            "",
        ]

        if self.extraction_stats:
            lines.extend([
                "## Extraction Statistics",
                "",
                f"- Total pages: {self.extraction_stats.get('total_pages', 0)}",
                f"- Text pages: {self.extraction_stats.get('text_pages', 0)}",
                f"- OCR pages: {self.extraction_stats.get('ocr_pages', 0)}",
                f"- Empty pages: {self.extraction_stats.get('empty_pages', 0)}",
                "",
            ])

        if self.quality_distribution and any(self.quality_distribution.values()):
            lines.extend([
                "## Quality Distribution",
                "",
                "| Grade | Count |",
                "|-------|-------|",
            ])
            for grade in ["A", "B", "C", "D", "F"]:
                count = self.quality_distribution.get(grade, 0)
                lines.append(f"| {grade} | {count} |")
            lines.append("")

        failures = [r for r in self.results if r.status == "failed"]
        if failures:
            lines.extend([
                "## Failures",
                "",
                "| Item Key | Title | Error |",
                "|----------|-------|-------|",
            ])
            for r in failures:
                title = r.title[:40] + "..." if len(r.title) > 40 else r.title
                # Escape pipes in title and reason
                title = title.replace("|", "\\|")
                reason = r.reason.replace("|", "\\|") if r.reason else ""
                lines.append(f"| `{r.item_key}` | {title} | {reason} |")
            lines.append("")

        empty_docs = [r for r in self.results if r.status == "empty"]
        if empty_docs:
            lines.extend([
                "## Empty Documents (No Extractable Text)",
                "",
                "| Item Key | Title | Reason |",
                "|----------|-------|--------|",
            ])
            for r in empty_docs:
                title = r.title[:40] + "..." if len(r.title) > 40 else r.title
                title = title.replace("|", "\\|")
                reason = r.reason.replace("|", "\\|") if r.reason else ""
                lines.append(f"| `{r.item_key}` | {title} | {reason} |")
            lines.append("")

        return "\n".join(lines)
