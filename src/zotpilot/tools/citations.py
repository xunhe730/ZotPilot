"""Citation graph tools: citing papers, references, citation counts."""
from typing import Annotated, Literal

from pydantic import Field

from ..state import ToolError, _get_config, _get_store_optional, _get_zotero, mcp
from .profiles import tool_tags


def _get_doi(doc_id: str) -> str:
    """Get DOI for a document, trying vector store first, then SQLite."""
    store = _get_store_optional()
    if store is not None:
        meta = store.get_document_meta(doc_id)
        if not meta:
            raise ToolError(f"Document not found: {doc_id}")
        doi = meta.get("doi")
    else:
        # No-RAG mode: get DOI from Zotero SQLite
        item = _get_zotero().get_item(doc_id)
        if not item:
            raise ToolError(f"Document not found: {doc_id}")
        doi = item.doi
    if not doi:
        raise ToolError("Document has no DOI - citation lookup unavailable")
    return doi


def _get_openalex_work(doc_id: str):
    doi = _get_doi(doc_id)
    from ..openalex_client import OpenAlexClient

    _config = _get_config()
    client = OpenAlexClient(email=_config.openalex_email)
    work = client.get_work_by_doi(doi)
    if not work:
        raise ToolError(f"Paper not found in OpenAlex: {doi}")
    return doi, client, work

@mcp.tool(tags=tool_tags("extended", "citations"))
def get_citations(
    doc_id: Annotated[str, Field(description="Document ID (Zotero item key) from search results")],
    direction: Annotated[
        Literal["references", "citing", "count", "both"],
        Field(description="'references' returns bibliography; 'citing' returns citing works; 'count' returns counts only; 'both' returns counts plus both lists"),  # noqa: E501
    ] = "both",
    limit: Annotated[int, Field(description="Max citing/references works to return per direction", ge=1, le=100)] = 20,
) -> dict:
    """Get citation graph data for a document by DOI.

    Use direction to choose references, citing papers, counts only, or both lists together.
    Returns OpenAlex identifiers, counts, and any requested work lists.
    """
    doi, client, work = _get_openalex_work(doc_id)
    result = {
        "doc_id": doc_id,
        "doi": doi,
        "openalex_id": work.openalex_id,
        "cited_by_count": work.cited_by_count,
        "reference_count": len(work.references),
    }
    if direction in {"references", "both"}:
        references = client.get_references(work.openalex_id, limit)
        result["references"] = [client.format_work(w) for w in references]
    if direction in {"citing", "both"}:
        citing = client.get_citing_works(work.openalex_id, limit)
        result["citing"] = [client.format_work(w) for w in citing]
    return result


