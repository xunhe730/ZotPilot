"""Tests for section heading classification."""

from zotpilot.models import CONFIDENCE_FALLBACK, SectionSpan
from zotpilot.pdf.extractor import _merge_inline_section_headings, _relabel_descriptive_body_sections
from zotpilot.pdf.section_classifier import (
    assign_section_with_confidence,
    categorize_heading,
    is_preamble_heading,
    is_reference_heading,
    is_reference_like_text,
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

    def test_categorize_nature_metadata_headings(self):
        assert categorize_heading("Data availability") == ("appendix", 0.3)
        assert categorize_heading("Code availability") == ("appendix", 0.3)
        assert categorize_heading("Author contributions") == ("appendix", 0.3)
        assert categorize_heading("Competing interests") == ("appendix", 0.3)
        assert categorize_heading("Online content") == ("appendix", 0.3)
        assert categorize_heading("Correspondence and requests for materials") == ("appendix", 0.3)

    def test_categorize_nature_preamble_headings(self):
        assert categorize_heading("Article") == ("preamble", 0.3)
        assert categorize_heading("Check for updates") == ("preamble", 0.3)
        assert is_preamble_heading("https://doi.org/10.1038/example")

    def test_reference_heading_variants(self):
        assert is_reference_heading("References")
        assert is_reference_heading("Bibliography")
        assert is_reference_heading("Literature cited")

    def test_reference_like_text_detects_bibliography(self):
        text = """
        References
        1. Smith, J. et al. A useful paper. Nature 520, 100-110 (2020). https://doi.org/10.1038/test1
        2. Chen, Y. et al. Another paper. Cell 180, 12-24 (2021). https://doi.org/10.1016/test2
        3. Wang, L. et al. More results. Science 370, 44-50 (2022). https://doi.org/10.1126/test3
        4. Lee, K. et al. Methods paper. Nat Methods 19, 88-99 (2023).
        """
        assert is_reference_like_text(text)

    def test_reference_like_text_ignores_normal_cited_prose(self):
        text = (
            "We evaluated perturbation response models across several datasets. "
            "The method improved calibration compared with prior work (Smith et al., 2020) "
            "and retained performance on held-out cell types. See https://doi.org/10.1038/example."
        )
        assert not is_reference_like_text(text)

    def test_short_reference_heading_text_detected(self):
        text = "References\n1. Smith, J. et al. Nature 520, 100-110 (2020)."
        assert is_reference_like_text(text)


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


class TestSectionSplitHeuristics:
    def test_inline_references_heading_splits_span(self):
        text = "## Results\nImportant findings.\n\n## References\n1. Smith, J. Nature 1, 2-3 (2020).\n"
        sections = [
            SectionSpan(label="results", char_start=0, char_end=len(text), heading_text="Results", confidence=1.0),
        ]

        split = _merge_inline_section_headings(sections, text)
        assert [s.label for s in split] == ["results", "references"]

    def test_descriptive_heading_after_abstract_becomes_results(self):
        sections = [
            SectionSpan(label="preamble", char_start=0, char_end=20, heading_text="Article", confidence=1.0),
            SectionSpan(label="abstract", char_start=20, char_end=100, heading_text="Abstract", confidence=1.0),
            SectionSpan(
                label="unknown",
                char_start=100,
                char_end=300,
                heading_text="Spatiotemporal atlas of brain ageing",
                confidence=0.7,
            ),
            SectionSpan(label="methods", char_start=300, char_end=500, heading_text="Methods", confidence=1.0),
        ]

        relabelled = _relabel_descriptive_body_sections(sections)
        assert relabelled[2].label == "results"

    def test_preamble_heading_after_abstract_stays_unknown(self):
        sections = [
            SectionSpan(label="abstract", char_start=0, char_end=100, heading_text="Abstract", confidence=1.0),
            SectionSpan(
                label="unknown",
                char_start=100,
                char_end=200,
                heading_text="Check for updates",
                confidence=0.7,
            ),
        ]

        relabelled = _relabel_descriptive_body_sections(sections)
        assert relabelled[1].label == "unknown"

    def test_nature_article_without_abstract_relabels_after_title_block(self):
        sections = [
            SectionSpan(label="preamble", char_start=0, char_end=10, heading_text="", confidence=1.0),
            SectionSpan(
                label="unknown",
                char_start=10,
                char_end=100,
                heading_text="Spatial transcriptomic clocks reveal cell proximity effects in brain ageing",
                confidence=0.7,
            ),
            SectionSpan(
                label="unknown",
                char_start=100,
                char_end=250,
                heading_text="Spatiotemporal atlas of brain ageing",
                confidence=0.7,
            ),
            SectionSpan(
                label="unknown",
                char_start=250,
                char_end=400,
                heading_text="Spatial ageing clocks",
                confidence=0.7,
            ),
            SectionSpan(label="methods", char_start=400, char_end=500, heading_text="Methods", confidence=1.0),
        ]

        relabelled = _relabel_descriptive_body_sections(sections)
        assert [s.label for s in relabelled] == ["preamble", "unknown", "results", "results", "methods"]
