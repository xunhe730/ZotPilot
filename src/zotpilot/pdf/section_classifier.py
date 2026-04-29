"""Lightweight section heading and reference-text classification.

Classifies heading text into academic paper section categories
using keyword matching only. No position-based guessing.
"""
from __future__ import annotations

import re

from ..models import CONFIDENCE_FALLBACK, SectionSpan

# Category keywords mapped to labels, ordered by weight (highest first).
# When multiple keywords match, highest-weighted category wins.
CATEGORY_KEYWORDS: list[tuple[str, list[str], float]] = [
    ("references", ["reference", "bibliography", "literature cited"], 0.1),
    ("results", ["result", "findings", "outcomes"], 1.0),
    ("conclusion", ["conclusion", "concluding"], 1.0),
    ("methods", ["method", "online methods", "materials", "experimental", "procedure",
                 "protocol", "design", "participants", "subjects"], 0.85),
    ("abstract", ["abstract"], 0.75),
    ("background", ["background", "literature review", "related work"], 0.7),
    ("discussion", ["discussion"], 0.65),
    ("introduction", ["introduction"], 0.5),
    ("appendix", ["appendix", "supplementa", "acknowledgment", "acknowledgement",
                  "grant", "funding", "disclosure", "conflict of interest",
                  "data availability", "code availability", "reporting summary",
                  "author contribution", "competing interest", "ethics declaration",
                  "additional information", "peer review", "publisher's note",
                  "rights and permissions"], 0.3),
]

# "summary" is special — matches conclusion unless combined with data words.
SUMMARY_EXCLUDES = ["statistics", "table", "data", "results summary"]

_PREAMBLE_EXACT = {
    "article",
    "review article",
    "check for updates",
    "nature",
    "nature methods",
    "nature biotechnology",
    "nature cell biology",
    "nature reviews genetics",
}

_REFERENCE_HEADING_RE = re.compile(
    r"^(references?|bibliography|literature cited|works cited)$",
    re.IGNORECASE,
)
_DOI_RE = re.compile(r"\b(?:doi:?\s*)?10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}[a-z]?\b")
_NUMBERED_REF_RE = re.compile(r"^\s*(?:\[\d+\]|\d+[\).])\s+\S+")
_JOURNAL_VOLUME_RE = re.compile(r"\b\d{1,4}\s*[,;:]\s*\d{1,6}(?:[-–]\d{1,6})?\b")


def categorize_heading(heading: str) -> tuple[str | None, float]:
    """Determine category from heading text using keyword matching.

    Returns (category, weight) or (None, 0) if no match.
    """
    heading_lower = heading.lower()

    if is_preamble_heading(heading):
        return ("preamble", 0.3)
    if "online content" in heading_lower or "correspondence and requests" in heading_lower:
        return ("appendix", 0.3)

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


def is_preamble_heading(heading: str) -> bool:
    """Return true for journal/title-page labels that should not become body sections."""
    clean = heading.strip().lower().strip("#*_ .:")
    if not clean:
        return False
    if clean in _PREAMBLE_EXACT:
        return True
    if clean.startswith(("https://doi.org/", "doi:", "received:", "accepted:", "published:")):
        return True
    if clean in {"open access", "main", "letters", "research article"}:
        return True
    return False


def is_reference_heading(heading: str) -> bool:
    """Return true for standalone bibliography headings."""
    clean = heading.strip().strip("#*_ .:")
    return bool(_REFERENCE_HEADING_RE.match(clean))


def is_reference_like_text(text: str) -> bool:
    """Detect bibliography-like chunks without rejecting normal cited prose."""
    text = (text or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and is_reference_heading(lines[0]):
        return True
    if len(text) < 250:
        return False

    head = text[:500].strip()
    first_line = head.splitlines()[0] if head else ""
    if is_reference_heading(first_line):
        return True

    if not lines:
        return False

    scored = 0
    numbered = 0
    doi_or_url = 0
    for line in lines:
        has_year = bool(_YEAR_RE.search(line))
        has_doi_or_url = bool(_DOI_RE.search(line) or _URL_RE.search(line))
        has_journal_pages = bool(_JOURNAL_VOLUME_RE.search(line))
        is_numbered = bool(_NUMBERED_REF_RE.search(line))
        authorish = bool(re.search(r"\b[A-Z][a-zA-Z'`-]+,\s+[A-Z]", line))

        if has_doi_or_url:
            doi_or_url += 1
        if is_numbered:
            numbered += 1
        if (has_year and (has_doi_or_url or has_journal_pages or authorish)) or is_numbered:
            scored += 1

    line_ratio = scored / max(len(lines), 1)
    return (len(lines) >= 4 and line_ratio >= 0.45) or numbered >= 3 or (doi_or_url >= 3 and line_ratio >= 0.3)


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
