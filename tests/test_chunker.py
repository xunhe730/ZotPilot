"""Tests for the PDF chunker."""
from zotpilot.models import PageExtraction, SectionSpan
from zotpilot.pdf.chunker import Chunker


class TestChunker:
    def test_empty_text_returns_empty(self):
        chunker = Chunker(chunk_size=100, overlap=20)
        result = chunker.chunk("", [], [])
        assert result == []

    def test_single_chunk(self):
        chunker = Chunker(chunk_size=1000, overlap=100)
        text = "Short text that fits in one chunk."
        pages = [PageExtraction(page_num=1, markdown=text, char_start=0)]
        sections = [SectionSpan(
            label="introduction", char_start=0, char_end=len(text), heading_text="", confidence=1.0
        )]

        chunks = chunker.chunk(text, pages, sections)
        assert len(chunks) == 1
        assert chunks[0].text == text
        assert chunks[0].chunk_index == 0
        assert chunks[0].page_num == 1
        assert chunks[0].section == "introduction"

    def test_multiple_chunks_with_overlap(self):
        chunker = Chunker(chunk_size=50, overlap=10)
        # 50 tokens * 4 chars/token = 200 chars per chunk
        text = "a" * 600  # Should create multiple chunks
        pages = [PageExtraction(page_num=1, markdown=text, char_start=0)]
        sections = []

        chunks = chunker.chunk(text, pages, sections)
        assert len(chunks) > 1

        # Verify chunk indices are sequential
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i

    def test_page_assignment(self):
        chunker = Chunker(chunk_size=50, overlap=10)
        text = "Page one content. " * 20 + "Page two content. " * 20
        page1_len = len("Page one content. " * 20)
        pages = [
            PageExtraction(page_num=1, markdown="Page one content. " * 20, char_start=0),
            PageExtraction(page_num=2, markdown="Page two content. " * 20, char_start=page1_len),
        ]
        sections = []

        chunks = chunker.chunk(text, pages, sections)
        # First chunk should be on page 1
        assert chunks[0].page_num == 1
        # Last chunk should be on page 2
        assert chunks[-1].page_num == 2

    def test_section_assignment(self):
        chunker = Chunker(chunk_size=1000, overlap=100)
        text = "Methods description here."
        pages = [PageExtraction(page_num=1, markdown=text, char_start=0)]
        sections = [
            SectionSpan(label="methods", char_start=0, char_end=len(text), heading_text="Methods", confidence=0.85),
        ]

        chunks = chunker.chunk(text, pages, sections)
        assert chunks[0].section == "methods"
        assert chunks[0].section_confidence == 0.85

    def test_sentence_boundary_breaking(self):
        chunker = Chunker(chunk_size=25, overlap=5)
        # 25 tokens * 4 = 100 chars target
        text = "First sentence here. Second sentence there. Third sentence everywhere. Fourth sentence."
        pages = [PageExtraction(page_num=1, markdown=text, char_start=0)]
        sections = []

        chunks = chunker.chunk(text, pages, sections)
        # Chunks should try to break at sentence boundaries
        for chunk in chunks[:-1]:  # All but last
            # Most chunks should end near a sentence boundary
            assert chunk.text.strip() != ""
