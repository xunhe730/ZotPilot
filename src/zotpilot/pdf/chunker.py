"""Document chunking with overlap and page tracking."""
from ..models import Chunk, PageExtraction, SectionSpan
from .section_classifier import assign_section_with_confidence, is_reference_like_text


class Chunker:
    """Split documents into overlapping chunks."""

    def __init__(
        self,
        chunk_size: int = 400,
        overlap: int = 100,
    ):
        """
        Args:
            chunk_size: Target chunk size in tokens (estimated as chars/4)
            overlap: Overlap between chunks in tokens
        """
        self.chunk_chars = chunk_size * 4
        self.overlap_chars = overlap * 4

    def chunk(
        self,
        full_text: str,
        pages: list[PageExtraction],
        sections: list[SectionSpan],
    ) -> list[Chunk]:
        """
        Split text into overlapping chunks.

        Attempts to break at sentence boundaries when possible.
        Tracks which page each chunk primarily belongs to.
        Assigns document section labels to each chunk.
        """
        if not full_text:
            return []

        # Build page boundary index
        page_boundaries = [(p.char_start, p.page_num) for p in pages]

        chunks = []
        start = 0
        chunk_idx = 0

        while start < len(full_text):
            end = min(start + self.chunk_chars, len(full_text))

            # Try to break at sentence boundary in last 20% of chunk
            if end < len(full_text):
                search_start = start + int(self.chunk_chars * 0.8)
                best_break = end

                for punct in ['. ', '.\n', '? ', '?\n', '! ', '!\n']:
                    pos = full_text.rfind(punct, search_start, end)
                    if pos != -1:
                        best_break = pos + len(punct)
                        break

                end = best_break

            chunk_text = full_text[start:end].strip()

            if chunk_text:
                # Find page number for chunk start
                page_num = 1
                for offset, pnum in page_boundaries:
                    if offset <= start:
                        page_num = pnum
                    else:
                        break

                # Assign section label and confidence
                section, section_confidence = assign_section_with_confidence(start, sections)
                if section != "references" and is_reference_like_text(chunk_text):
                    section = "references"
                    section_confidence = 1.0

                chunks.append(Chunk(
                    text=chunk_text,
                    chunk_index=chunk_idx,
                    page_num=page_num,
                    char_start=start,
                    char_end=end,
                    section=section,
                    section_confidence=section_confidence,
                ))
                chunk_idx += 1

            # Move start with overlap, ensuring forward progress
            next_start = end - self.overlap_chars
            if next_start <= start:
                next_start = end
            start = next_start

        return chunks
