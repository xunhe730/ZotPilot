"""Chunker interface shared by the char-based and token-aware backends."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Chunk, PageExtraction, SectionSpan


@runtime_checkable
class ChunkerProtocol(Protocol):
    def chunk(
        self,
        full_text: str,
        pages: list[PageExtraction],
        sections: list[SectionSpan],
    ) -> list[Chunk]: ...
