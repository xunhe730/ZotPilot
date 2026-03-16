"""Lightweight section heading classification.

Classifies heading text into academic paper section categories
using keyword matching only. No position-based guessing.
"""
from __future__ import annotations

from ..models import SectionSpan, CONFIDENCE_FALLBACK

# Category keywords mapped to labels, ordered by weight (highest first).
# When multiple keywords match, highest-weighted category wins.
CATEGORY_KEYWORDS: list[tuple[str, list[str], float]] = [
    ("results", ["result", "findings", "outcomes"], 1.0),
    ("conclusion", ["conclusion", "concluding"], 1.0),
    ("methods", ["method", "materials", "experimental", "procedure",
                 "protocol", "design", "participants", "subjects"], 0.85),
    ("abstract", ["abstract"], 0.75),
    ("background", ["background", "literature review", "related work"], 0.7),
    ("discussion", ["discussion"], 0.65),
    ("introduction", ["introduction"], 0.5),
    ("appendix", ["appendix", "supplementa", "acknowledgment", "acknowledgement",
                  "grant", "funding", "disclosure", "conflict of interest"], 0.3),
    ("references", ["reference", "bibliography"], 0.1),
]

# "summary" is special — matches conclusion unless combined with data words.
SUMMARY_EXCLUDES = ["statistics", "table", "data", "results summary"]


def categorize_heading(heading: str) -> tuple[str | None, float]:
    """Determine category from heading text using keyword matching.

    Returns (category, weight) or (None, 0) if no match.
    """
    heading_lower = heading.lower()

    for category, keywords, weight in CATEGORY_KEYWORDS:
        for keyword in keywords:
            if keyword in heading_lower:
                return (category, weight)

    # Special handling for "summary" (only if no other category matched above).
    # Exclusion-based: an unrecognised keyword just falls through to conclusion,
    # rather than requiring a specific keyword for the base functionality.
    if "summary" in heading_lower:
        for exclude in SUMMARY_EXCLUDES:
            if exclude in heading_lower:
                return ("results", 1.0)
        return ("conclusion", 1.0)

    return (None, 0.0)


def assign_section(char_start: int, spans: list[SectionSpan]) -> str:
    """Find the section label for a given character position."""
    label, _ = assign_section_with_confidence(char_start, spans)
    return label


def assign_section_with_confidence(
    char_start: int, spans: list[SectionSpan]
) -> tuple[str, float]:
    """Find section label and confidence for a character position."""
    for span in spans:
        if span.char_start <= char_start < span.char_end:
            return span.label, span.confidence
    return "unknown", CONFIDENCE_FALLBACK
