"""Token-aware chunker backed by LlamaIndex's SentenceSplitter + the model tokenizer."""
from __future__ import annotations

from ..models import Chunk, PageExtraction, SectionSpan
from .section_classifier import assign_section_with_confidence, is_reference_like_text


class LlamaIndexChunker:
    """Split text into chunks guaranteed to fit the embedding model's token window.

    Uses the model's own tokenizer (not a chars/4 estimate), so dense academic
    text cannot produce an over-budget chunk. A hard post-split cap truncates any
    residual outlier as a final safety net.
    """

    def __init__(
        self,
        chunk_size: int = 480,
        overlap: int = 100,
        model_tokenizer: str = "BAAI/bge-large-en-v1.5",
        hard_cap_tokens: int = 512,
    ):
        from llama_index.core.node_parser import SentenceSplitter
        from tokenizers import Tokenizer

        self._tokenizer = Tokenizer.from_pretrained(model_tokenizer)
        self.hard_cap_tokens = hard_cap_tokens

        self._splitter = SentenceSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            tokenizer=lambda t: self._tokenizer.encode(t).ids,
        )

    def _truncate(self, text: str) -> str:
        ids = self._tokenizer.encode(text).ids
        if len(ids) <= self.hard_cap_tokens:
            return text
        return self._tokenizer.decode(ids[: self.hard_cap_tokens])

    def chunk(
        self,
        full_text: str,
        pages: list[PageExtraction],
        sections: list[SectionSpan],
    ) -> list[Chunk]:
        if not full_text:
            return []

        page_boundaries = [(p.char_start, p.page_num) for p in pages]
        chunks: list[Chunk] = []
        cursor = 0
        for piece in self._splitter.split_text(full_text):
            piece = self._truncate(piece.strip())
            if not piece:
                continue
            # Locate char offset for page/char metadata mapping.
            # This is BEST-EFFORT / APPROXIMATE: SentenceSplitter may produce
            # overlapping pieces (chunk_overlap > 0) and _truncate can shorten a
            # piece, so `find` may match a slightly wrong position or fall back to
            # `cursor`. The approximation only affects page_num/char_start/char_end
            # metadata — it does NOT affect chunk content or the token-cap guarantee.
            start = full_text.find(piece[:64], cursor)
            if start < 0:
                start = cursor
            end = start + len(piece)
            cursor = end

            page_num = 1
            for offset, pnum in page_boundaries:
                if offset <= start:
                    page_num = pnum
                else:
                    break

            section, confidence = assign_section_with_confidence(start, sections)
            if section != "references" and is_reference_like_text(piece):
                section, confidence = "references", 1.0

            chunks.append(Chunk(
                text=piece, chunk_index=len(chunks), page_num=page_num,
                char_start=start, char_end=end,
                section=section, section_confidence=confidence,
            ))
        return chunks
