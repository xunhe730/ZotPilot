"""Tests for section heading classification."""

from zotpilot.models import CONFIDENCE_FALLBACK, SectionSpan
from zotpilot.pdf.section_classifier import (
    assign_section_with_confidence,
    categorize_heading,
)


class TestCategorizeHeading:
    def test_categorize_heading_results(self):
        """"Results" maps to results with weight 1.0."""
        assert categorize_heading("Results") == ("results", 1.0)

    def test_categorize_heading_methods(self):
        """"Materials and Methods" maps to methods with weight 0.85."""
        assert categorize_heading("Materials and Methods") == ("methods", 0.85)

    def test_categorize_heading_conclusion(self):
        """"Conclusion" maps to conclusion with weight 1.0."""
        assert categorize_heading("Conclusion") == ("conclusion", 1.0)

    def test_categorize_heading_abstract(self):
        """"Abstract" maps to abstract with weight 0.75."""
        assert categorize_heading("Abstract") == ("abstract", 0.75)

    def test_categorize_heading_references(self):
        """"References" maps to references with weight 0.1."""
        assert categorize_heading("References") == ("references", 0.1)

    def test_categorize_heading_unknown(self):
        """"Acknowledgements" maps to appendix with weight 0.3."""
        assert categorize_heading("Acknowledgements") == ("appendix", 0.3)

    def test_categorize_heading_summary(self):
        """"Summary" maps to conclusion with weight 1.0."""
        assert categorize_heading("Summary") == ("conclusion", 1.0)

    def test_categorize_heading_summary_statistics(self):
        """"Summary Statistics" maps to results with weight 1.0."""
        assert categorize_heading("Summary Statistics") == ("results", 1.0)

    def test_categorize_heading_no_match(self):
        """"Random Heading" returns (None, 0.0)."""
        assert categorize_heading("Random Heading") == (None, 0.0)


class TestAssignSectionWithConfidence:
    def test_assign_section_with_confidence(self):
        """Given section spans, correctly maps char positions."""
        spans = [
            SectionSpan(label="abstract", char_start=0, char_end=100,
                        heading_text="Abstract", confidence=0.75),
            SectionSpan(label="introduction", char_start=100, char_end=500,
                        heading_text="Introduction", confidence=0.5),
            SectionSpan(label="methods", char_start=500, char_end=1000,
                        heading_text="Methods", confidence=0.85),
        ]

        # Position inside abstract
        assert assign_section_with_confidence(50, spans) == ("abstract", 0.75)

        # Position at start of introduction
        assert assign_section_with_confidence(100, spans) == ("introduction", 0.5)

        # Position inside methods
        assert assign_section_with_confidence(750, spans) == ("methods", 0.85)

        # Position outside all spans returns unknown with fallback confidence
        assert assign_section_with_confidence(1500, spans) == ("unknown", CONFIDENCE_FALLBACK)

    def test_assign_section_empty_spans(self):
        """Empty span list returns unknown."""
        assert assign_section_with_confidence(0, []) == ("unknown", CONFIDENCE_FALLBACK)

    def test_assign_section_boundary(self):
        """char_end is exclusive (position at char_end is NOT in the span)."""
        spans = [
            SectionSpan(label="intro", char_start=0, char_end=100,
                        heading_text="Intro", confidence=1.0),
        ]
        # Position 99 is inside, 100 is outside
        assert assign_section_with_confidence(99, spans) == ("intro", 1.0)
        assert assign_section_with_confidence(100, spans) == ("unknown", CONFIDENCE_FALLBACK)
