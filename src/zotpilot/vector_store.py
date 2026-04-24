"""ChromaDB vector storage with chunk management."""
import logging
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import chromadb
from chromadb.config import Settings

from .interfaces import EmbedderProtocol
from .models import Chunk, StoredChunk

if TYPE_CHECKING:
    from .models import ExtractedTable

logger = logging.getLogger(__name__)


def _probe_chroma_db_access(db_path: Path) -> bool:
    """Probe whether an existing Chroma index can be opened safely.

    Run the probe in a subprocess so Rust-side segfaults do not take down the
    caller. Returns False on any crash or non-zero exit.
    """
    if not db_path.exists():
        return True
    if not any(db_path.iterdir()):
        return True

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import chromadb; "
                "from chromadb.config import Settings; "
                f"c=chromadb.PersistentClient(path={str(db_path)!r}, settings=Settings(anonymized_telemetry=False)); "
                "col=c.get_or_create_collection(name='chunks', metadata={'hnsw:space':'cosine'}); "
                "col.peek(limit=1)"
            ),
        ],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


def _quarantine_chroma_db(db_path: Path) -> Path | None:
    """Move a broken Chroma directory aside and return the backup path."""
    if not db_path.exists():
        return None
    suffix = time.strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.corrupt-{suffix}")
    shutil.move(str(db_path), str(backup))
    return backup


def _ref_chunk_index(ref_map: dict, element_type: str, item) -> int:
    """Look up chunk_index from ref_map using caption number."""
    caption = getattr(item, 'caption', None)
    if caption:
        m = re.search(r"(\d+)", caption)
        if m:
            return ref_map.get((element_type, int(m.group(1))), -1)
    return -1


class EmbeddingDimensionMismatchError(Exception):
    """Raised when embedder dimensions don't match existing index."""


class VectorStore:
    """
    ChromaDB-backed vector store for document chunks.

    Handles:
    - Adding chunks with metadata
    - Semantic search with filters
    - Adjacent chunk retrieval for context expansion
    - Document-level operations (delete, list)
    """

    def __init__(self, db_path: Path, embedder: EmbedderProtocol):
        self.db_path = Path(db_path)
        if not _probe_chroma_db_access(self.db_path):
            backup = _quarantine_chroma_db(self.db_path)
            logger.warning(
                "Chroma index at %s could not be opened safely; moved aside to %s and rebuilding a fresh index.",
                self.db_path,
                backup,
            )
        self.db_path.mkdir(parents=True, exist_ok=True)

        # Query embedding cache (FIFO eviction at maxsize)
        self._query_cache: dict[str, list[float]] = {}
        self._query_cache_maxsize = 512

        self.client = chromadb.PersistentClient(
            path=str(self.db_path),
            settings=Settings(anonymized_telemetry=False)
        )

        # Get embedder dimensions
        embedder_dims = getattr(embedder, 'dimensions', None)

        # Check if collection exists and has data
        try:
            existing = self.client.get_collection("chunks")
            # Do not call collection.count() during startup. In the current
            # Chroma/Rust stack that path can segfault on some existing local
            # indexes; reading collection metadata is enough to validate the
            # embedder dimension contract.
            if embedder_dims is not None:
                stored_dims = (existing.metadata or {}).get("embedding_dimensions")
                if stored_dims is not None and stored_dims != embedder_dims:
                    raise EmbeddingDimensionMismatchError(
                        f"Embedding dimension mismatch: index has {stored_dims} dimensions "
                        f"but current embedder uses {embedder_dims} dimensions. "
                        f"Delete the index and reindex with --force, or switch back to "
                        f"the original embedding provider.\n"
                        f"Index path: {db_path}"
                    )
        except (ValueError, chromadb.errors.NotFoundError):
            # Collection doesn't exist yet, that's fine
            pass

        # Create or get collection with dimension metadata
        metadata = {"hnsw:space": "cosine"}
        if embedder_dims is not None:
            metadata["embedding_dimensions"] = embedder_dims

        self.collection = self.client.get_or_create_collection(
            name="chunks",
            metadata=metadata
        )
        self.embedder = embedder

    @staticmethod
    def _build_base_metadata(doc_id: str, doc_meta: dict) -> dict:
        """Build the shared metadata fields for any chunk type."""
        return {
            "doc_id": doc_id,
            "doc_title": doc_meta.get("title", ""),
            "authors": doc_meta.get("authors", ""),
            "authors_lower": doc_meta.get("authors", "").lower(),
            "year": doc_meta.get("year") or 0,
            "citation_key": doc_meta.get("citation_key", ""),
            "publication": doc_meta.get("publication", ""),
            "doi": doc_meta.get("doi", ""),
            "tags": doc_meta.get("tags", ""),
            "tags_lower": doc_meta.get("tags", "").lower(),
            "collections": doc_meta.get("collections", ""),
            "journal_quartile": doc_meta.get("journal_quartile", ""),
            "pdf_hash": doc_meta.get("pdf_hash", ""),
            "quality_grade": doc_meta.get("quality_grade", ""),
        }

    def add_chunks(self, doc_id: str, doc_meta: dict, chunks: list[Chunk]) -> None:
        """
        Add all chunks for a document.

        Args:
            doc_id: Unique document identifier (Zotero item key)
            doc_meta: Document metadata (title, authors, year)
            chunks: List of Chunk objects to store
        """
        if not chunks:
            return

        ids = [f"{doc_id}_chunk_{c.chunk_index:04d}" for c in chunks]
        texts = [c.text for c in chunks]

        # Use RETRIEVAL_DOCUMENT task type
        embeddings = self.embedder.embed(texts, task_type="RETRIEVAL_DOCUMENT")

        metadatas = []
        for c in chunks:
            meta = self._build_base_metadata(doc_id, doc_meta)
            meta.update({
                "page_num": c.page_num,
                "chunk_index": c.chunk_index,
                "total_chunks": len(chunks),
                "char_start": c.char_start,
                "char_end": c.char_end,
                "section": c.section,
                "section_confidence": c.section_confidence,
                "chunk_type": "text",
            })
            metadatas.append(meta)

        self.collection.add(
            ids=ids,
            documents=texts,
            embeddings=embeddings,
            metadatas=metadatas
        )

    def add_tables(
        self,
        doc_id: str,
        doc_meta: dict,
        tables: list["ExtractedTable"],
        ref_map: dict[tuple[str, int], int] | None = None,
    ) -> None:
        """
        Add table chunks for a document.

        Tables are stored as separate chunks with markdown representation.
        Chunk IDs use format: {doc_id}_table_{page:04d}_{table_idx:02d}

        Args:
            doc_id: Unique document identifier (Zotero item key)
            doc_meta: Document metadata (title, authors, year, etc.)
            tables: List of ExtractedTable objects to store
        """
        if not tables:
            return

        ids = [
            f"{doc_id}_table_{t.page_num:04d}_{t.table_index:02d}"
            for t in tables
        ]
        texts = [t.to_markdown() for t in tables]

        # Use RETRIEVAL_DOCUMENT task type
        embeddings = self.embedder.embed(texts, task_type="RETRIEVAL_DOCUMENT")

        metadatas = []
        for t in tables:
            meta = self._build_base_metadata(doc_id, doc_meta)
            meta.update({
                "page_num": t.page_num,
                "chunk_index": _ref_chunk_index(ref_map, "table", t) if ref_map else -1,
                "chunk_type": "table",
                "table_index": t.table_index,
                "table_caption": t.caption or "",
                "table_num_rows": t.num_rows,
                "table_num_cols": t.num_cols,
                "reference_context": getattr(t, 'reference_context', '') or "",
                "section": "table",
                "section_confidence": 1.0,
            })
            metadatas.append(meta)

        self.collection.add(
            ids=ids,
            documents=texts,
            embeddings=embeddings,
            metadatas=metadatas
        )

    def add_figures(
        self,
        doc_id: str,
        doc_meta: dict,
        figures: list,
        ref_map: dict[tuple[str, int], int] | None = None,
    ) -> None:
        """Add figure chunks to the store.

        Figures are stored as separate chunks with caption as text.
        Chunk IDs use format: {doc_id}_fig_{page:03d}_{fig_idx:02d}

        Args:
            doc_id: Document ID (Zotero item key)
            doc_meta: Document-level metadata
            figures: List of ExtractedFigure objects
        """
        if not figures:
            return

        ids = []
        documents = []
        metadatas = []

        for fig in figures:
            chunk_id = f"{doc_id}_fig_{fig.page_num:03d}_{fig.figure_index:02d}"

            # Use caption for embedding, or fallback text for orphans
            text = fig.to_searchable_text()

            metadata = self._build_base_metadata(doc_id, doc_meta)
            metadata.update({
                "chunk_type": "figure",
                "page_num": fig.page_num,
                "chunk_index": _ref_chunk_index(ref_map, "figure", fig) if ref_map else -1,
                "figure_index": fig.figure_index,
                "caption": fig.caption or "",
                "image_path": str(fig.image_path) if fig.image_path else "",
                "reference_context": getattr(fig, 'reference_context', '') or "",
                "section": "figure",
                "section_confidence": 1.0,
            })

            ids.append(chunk_id)
            documents.append(text)
            metadatas.append(metadata)

        if ids:
            embeddings = self.embedder.embed(documents, task_type="RETRIEVAL_DOCUMENT")
            self.collection.add(
                ids=ids,
                documents=documents,
                embeddings=embeddings,
                metadatas=metadatas,
            )

    def _cached_embed_query(self, query: str) -> list[float]:
        """Embed a query, returning cached result if available."""
        if query in self._query_cache:
            return self._query_cache[query]
        embedding = self.embedder.embed_query(query)
        if len(self._query_cache) >= self._query_cache_maxsize:
            oldest = next(iter(self._query_cache))
            del self._query_cache[oldest]
        self._query_cache[query] = embedding
        return embedding

    def clear_query_cache(self):
        """Clear cached query embeddings (call after index updates)."""
        self._query_cache.clear()

    def search(
        self,
        query: str,
        top_k: int = 10,
        filters: dict | None = None
    ) -> list[StoredChunk]:
        """
        Search for similar chunks.

        Args:
            query: Search query text
            top_k: Number of results to return
            filters: Optional ChromaDB where clause

        Returns:
            List of StoredChunk objects sorted by similarity
        """
        # Use RETRIEVAL_QUERY task type for asymmetric search (with caching)
        query_embedding = self._cached_embed_query(query)

        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=filters,
            include=["documents", "metadatas", "distances"]
        )

        chunks = []
        if results['ids'] and results['ids'][0]:
            for i, chunk_id in enumerate(results['ids'][0]):
                metadata = results['metadatas'][0][i]
                if metadata is None:
                    continue
                chunks.append(StoredChunk(
                    id=chunk_id,
                    text=results['documents'][0][i],
                    metadata=metadata,
                    score=1 - results['distances'][0][i]  # Convert distance to similarity
                ))
        return chunks

    def get_adjacent_chunks(
        self,
        doc_id: str,
        chunk_index: int,
        window: int = 2
    ) -> list[StoredChunk]:
        """
        Get chunks adjacent to a given chunk for context expansion.

        Args:
            doc_id: Document ID
            chunk_index: Center chunk index
            window: Number of chunks before/after to include

        Returns:
            List of chunks sorted by chunk_index
        """
        results = self.collection.get(
            where={
                "$and": [
                    {"doc_id": {"$eq": doc_id}},
                    {"chunk_index": {"$gte": chunk_index - window}},
                    {"chunk_index": {"$lte": chunk_index + window}}
                ]
            },
            include=["documents", "metadatas"]
        )

        chunks = []
        if results['ids']:
            for i, chunk_id in enumerate(results['ids']):
                chunks.append(StoredChunk(
                    id=chunk_id,
                    text=results['documents'][i],
                    metadata=results['metadatas'][i]
                ))

        return sorted(chunks, key=lambda c: c.metadata['chunk_index'])

    def delete_document(self, doc_id: str) -> None:
        """Remove all chunks for a document."""
        self.collection.delete(where={"doc_id": {"$eq": doc_id}})

    def get_indexed_doc_ids(self) -> set[str]:
        """Get set of all indexed document IDs.

        Memory-efficient: extracts doc_id from chunk IDs without loading metadata.
        Handles text chunks ({doc_id}_chunk_{index:04d}),
        table chunks ({doc_id}_table_{page:04d}_{table_idx:02d}),
        and figure chunks ({doc_id}_fig_{page:03d}_{fig_idx:02d}).
        """
        results = self.collection.get(include=[])  # IDs only, no documents/metadata
        if not results['ids']:
            return set()

        doc_ids = set()
        for chunk_id in results['ids']:
            doc_id = self._doc_id_from_chunk_id(chunk_id)
            if doc_id:
                doc_ids.add(doc_id)
        return doc_ids

    @staticmethod
    def _doc_id_from_chunk_id(chunk_id: str) -> str | None:
        """Extract the logical document ID from a chunk/table/figure row ID."""
        if '_chunk_' in chunk_id:
            parts = chunk_id.rsplit('_chunk_', 1)
        elif '_table_' in chunk_id:
            parts = chunk_id.rsplit('_table_', 1)
        elif '_fig_' in chunk_id:
            parts = chunk_id.rsplit('_fig_', 1)
        else:
            return None
        return parts[0] if len(parts) == 2 else None

    def count(self) -> int:
        """Return total number of chunks."""
        results = self.collection.get(include=[])  # IDs only, avoids Rust count() path
        return len(results.get("ids") or [])

    def count_chunks_for_doc_ids(self, doc_ids: set[str]) -> int:
        """Count chunks belonging to the provided logical document IDs."""
        if not doc_ids:
            return 0
        results = self.collection.get(include=[])  # IDs only, no documents/metadata
        if not results["ids"]:
            return 0
        return sum(
            1
            for chunk_id in results["ids"]
            if self._doc_id_from_chunk_id(chunk_id) in doc_ids
        )

    def get_document_meta(self, doc_id: str) -> dict | None:
        """Get metadata for a document's first chunk.

        Useful for checking stored metadata (e.g., pdf_hash) without
        loading full document content.

        Args:
            doc_id: Document ID to look up

        Returns:
            Metadata dict from first chunk, or None if not found
        """
        results = self.collection.get(
            where={"doc_id": {"$eq": doc_id}},
            limit=1,
            include=["metadatas"]
        )
        if results["metadatas"]:
            return results["metadatas"][0]
        return None
