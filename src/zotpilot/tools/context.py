"""Context expansion tools."""
import re
from typing import Annotated

from pydantic import Field

from ..index_authority import current_library_pdf_doc_ids
from ..state import ToolError, _get_config, _get_store, _get_zotero, mcp
from .profiles import tool_tags


@mcp.tool(tags=tool_tags("core", "context"))
def get_passage_context(
    doc_id: Annotated[str, Field(description="Document ID from search results")],
    chunk_index: Annotated[int, Field(description="Chunk index from search results")],
    window: Annotated[int, Field(description="Chunks before/after to include", ge=1, le=5)] = 2,
    include_merged: Annotated[bool, Field(description="Return merged text instead of per-passage text")] = False,
    table_page: Annotated[int | None, Field(description="Page number of table (for table context)")] = None,
    table_index: Annotated[int | None, Field(description="Index of table on page")] = None,
) -> dict:
    """Expand context around a search result passage. Pass table_page+table_index for table context lookup."""
    window = max(1, min(window, 5))
    _config = _get_config()
    if _config.embedding_provider == "none":
        raise ToolError(
            "Passage context requires indexing. "
            "Configure an embedding provider and run index_library() first."
        )
    if doc_id not in current_library_pdf_doc_ids(_get_zotero()):
        raise ToolError(f"Document not found: {doc_id}")
    store = _get_store()

    # Handle table context lookup
    if table_page is not None and table_index is not None:
        return _get_table_reference_context(store, doc_id, table_page, table_index, window, include_merged)

    # Standard text chunk context
    chunks = store.get_adjacent_chunks(doc_id, chunk_index, window=window)

    if not chunks:
        raise ToolError(f"No chunks found for doc_id={doc_id}")

    # Get section and journal_quartile from center chunk
    center_chunk = next((c for c in chunks if c.metadata.get("chunk_index", -1) == chunk_index), chunks[0])

    passages = [
        {
            "chunk_index": c.metadata.get("chunk_index", -1),
            "page": c.metadata.get("page_num", 0),
            "section": c.metadata.get("section", "unknown"),
            "section_confidence": c.metadata.get("section_confidence", 1.0),
            "is_center": c.metadata.get("chunk_index", -1) == chunk_index,
            **({} if include_merged else {"text": c.text}),
        }
        for c in chunks
    ]

    return {
        "doc_id": doc_id,
        "doc_title": chunks[0].metadata.get("doc_title", "Unknown"),
        "citation_key": chunks[0].metadata.get("citation_key", ""),
        "section": center_chunk.metadata.get("section", "unknown"),
        "section_confidence": center_chunk.metadata.get("section_confidence", 1.0),
        "journal_quartile": center_chunk.metadata.get("journal_quartile") or None,
        "center_chunk_index": chunk_index,
        "window": window,
        "passages": passages,
        **({"merged_text": "\n\n".join(c.text for c in chunks)} if include_merged else {}),
    }


def _get_table_reference_context(
    store,
    doc_id: str,
    table_page: int,
    table_index: int,
    window: int,
    include_merged: bool,
) -> dict:
    """Find text that references a specific table and return with context."""
    # Get the specific table's metadata
    table_chunk_id = f"{doc_id}_table_{table_page:04d}_{table_index:02d}"
    table_results = store.collection.get(
        ids=[table_chunk_id],
        include=["metadatas"]
    )

    if not table_results["ids"]:
        raise ToolError(f"Table not found: page={table_page}, index={table_index}")

    table_meta = table_results["metadatas"][0]
    table_caption = table_meta.get("table_caption", "")

    # Get all text chunks for this document
    text_results = store.collection.get(
        where={
            "$and": [
                {"doc_id": {"$eq": doc_id}},
                {"chunk_type": {"$eq": "text"}},
            ]
        },
        include=["documents", "metadatas"]
    )

    if not text_results["ids"]:
        # No text chunks - return table metadata only
        return {
            "doc_id": doc_id,
            "doc_title": table_meta.get("doc_title", "Unknown"),
            "citation_key": table_meta.get("citation_key", ""),
            "note": "No text chunks found for this document",
            "table_caption": table_caption,
            "table_page": table_page,
            "table_index": table_index,
            "passages": [],
            "merged_text": "",
        }

    # Extract table number from caption (e.g., "Table 1: Results" -> "1")
    table_num_match = re.search(r"Table\s*(\d+|[IVXLCDM]+)", table_caption, re.IGNORECASE)
    if table_num_match:
        table_ref = table_num_match.group(0)  # "Table 1" or "Table I"
    else:
        # Fallback: search for any table reference near this page
        table_ref = "Table"

    # Search text chunks for reference to this table
    ref_pattern = re.compile(re.escape(table_ref), re.IGNORECASE)
    matching_chunk_idx = None

    for chunk_id, text, meta in zip(
        text_results["ids"], text_results["documents"], text_results["metadatas"]
    ):
        if ref_pattern.search(text):
            matching_chunk_idx = meta.get("chunk_index", -1)
            break

    if matching_chunk_idx is None:
        # No reference found - return table metadata with note
        return {
            "doc_id": doc_id,
            "doc_title": table_meta.get("doc_title", "Unknown"),
            "citation_key": table_meta.get("citation_key", ""),
            "note": "No text reference to this table found",
            "table_caption": table_caption,
            "table_page": table_page,
            "table_index": table_index,
            "passages": [],
            "merged_text": "",
        }

    # Found reference - get context around it
    context_chunks = store.get_adjacent_chunks(doc_id, matching_chunk_idx, window=window)
    center_chunk = next(
        (c for c in context_chunks if c.metadata.get("chunk_index", -1) == matching_chunk_idx),
        context_chunks[0] if context_chunks else None
    )

    if not center_chunk:
        raise ToolError(f"Could not retrieve context for chunk {matching_chunk_idx}")

    passages = [
        {
            "chunk_index": c.metadata.get("chunk_index", -1),
            "page": c.metadata.get("page_num", 0),
            "section": c.metadata.get("section", "unknown"),
            "section_confidence": c.metadata.get("section_confidence", 1.0),
            "is_center": c.metadata.get("chunk_index", -1) == matching_chunk_idx,
            **({} if include_merged else {"text": c.text}),
        }
        for c in context_chunks
    ]

    return {
        "doc_id": doc_id,
        "doc_title": center_chunk.metadata.get("doc_title", "Unknown"),
        "citation_key": center_chunk.metadata.get("citation_key", ""),
        "table_caption": table_caption,
        "table_page": table_page,
        "table_index": table_index,
        "reference_found_in_chunk": matching_chunk_idx,
        "section": center_chunk.metadata.get("section", "unknown"),
        "section_confidence": center_chunk.metadata.get("section_confidence", 1.0),
        "center_chunk_index": matching_chunk_idx,
        "window": window,
        "passages": passages,
        **({"merged_text": "\n\n".join(c.text for c in context_chunks)} if include_merged else {}),
    }
