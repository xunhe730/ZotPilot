"""
Protocol definitions for all major components.
Allows parallel development against interfaces.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import (
    ZoteroItem,
    PageExtraction,
    DocumentExtraction,
    Chunk,
    StoredChunk,
    RetrievalResult,
    SectionSpan,
)


class ZoteroClientProtocol(Protocol):
    """Interface for Zotero database access."""

    def get_all_items_with_pdfs(self) -> list[ZoteroItem]:
        """Get all Zotero items that have PDF attachments."""
        ...

    def get_item(self, item_key: str) -> ZoteroItem | None:
        """Get a specific item by key."""
        ...


class PDFProcessorProtocol(Protocol):
    """Interface for PDF extraction via pymupdf-layout + pymupdf4llm."""

    def __call__(self, pdf_path: Path, **kwargs) -> DocumentExtraction:
        """Extract a PDF document, returning structured extraction results."""
        ...


class ChunkerProtocol(Protocol):
    """Interface for document chunking."""

    def chunk(
        self,
        full_text: str,
        pages: list[PageExtraction],
        sections: list[SectionSpan],
    ) -> list[Chunk]:
        """Split text into overlapping chunks."""
        ...


class EmbedderProtocol(Protocol):
    """Interface for text embedding."""

    def embed(self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        """Embed multiple texts."""
        ...

    def embed_query(self, query: str) -> list[float]:
        """Embed a search query (uses RETRIEVAL_QUERY task type)."""
        ...


class VectorStoreProtocol(Protocol):
    """Interface for vector storage and retrieval."""

    def add_chunks(self, doc_id: str, doc_meta: dict, chunks: list[Chunk]) -> None:
        """Add chunks for a document."""
        ...

    def search(self, query: str, top_k: int = 10, filters: dict | None = None) -> list[StoredChunk]:
        """Search for similar chunks."""
        ...

    def get_adjacent_chunks(self, doc_id: str, chunk_index: int, window: int = 2) -> list[StoredChunk]:
        """Get chunks adjacent to a given chunk."""
        ...

    def delete_document(self, doc_id: str) -> None:
        """Delete all chunks for a document."""
        ...

    def get_indexed_doc_ids(self) -> set[str]:
        """Get IDs of all indexed documents."""
        ...

    def count(self) -> int:
        """Count total chunks."""
        ...


class RetrieverProtocol(Protocol):
    """Interface for search with context expansion."""

    def search(
        self,
        query: str,
        top_k: int = 10,
        context_window: int = 1,
        filters: dict | None = None
    ) -> list[RetrievalResult]:
        """Search and expand context."""
        ...


class RerankerProtocol(Protocol):
    """Interface for result reranking."""

    def rerank(
        self,
        results: list[RetrievalResult],
        section_weights: dict[str, float] | None = None,
        journal_weights: dict[str, float] | None = None,
    ) -> list[RetrievalResult]:
        """Rerank results by composite score."""
        ...

    def score_result(
        self,
        result: RetrievalResult,
        section_weights: dict[str, float] | None = None,
        journal_weights: dict[str, float] | None = None,
    ) -> float:
        """Calculate composite score for a single result."""
        ...


class JournalRankerProtocol(Protocol):
    """Interface for journal quality lookup."""

    def lookup(self, publication: str) -> str | None:
        """Look up journal quartile (Q1/Q2/Q3/Q4 or None)."""
        ...

    @property
    def loaded(self) -> bool:
        """Check if lookup table is loaded."""
        ...
