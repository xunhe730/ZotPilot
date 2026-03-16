"""Citation graph tools: citing papers, references, citation counts."""
from ..state import mcp, _get_store, _get_config, ToolError
from ..config import Config


@mcp.tool()
def find_citing_papers(doc_id: str, limit: int = 20) -> list[dict]:
    """
    Find papers that cite a given document.

    Requires the document to have a DOI. Uses OpenAlex API for citation data.
    Rate-limited to 1 request/second (or 10/second if openalex_email configured).

    Args:
        doc_id: Document ID (Zotero item key) from search results
        limit: Maximum number of citing papers to return (1-100)

    Returns:
        List of citing papers with title, authors, year, DOI, and citation count
    """
    store = _get_store()
    meta = store.get_document_meta(doc_id)
    if not meta:
        raise ToolError(f"Document not found: {doc_id}")

    doi = meta.get("doi")
    if not doi:
        raise ToolError("Document has no DOI - citation lookup unavailable")

    from ..openalex_client import OpenAlexClient

    _config = _get_config()

    client = OpenAlexClient(email=_config.openalex_email)

    work = client.get_work_by_doi(doi)
    if not work:
        raise ToolError(f"Paper not found in OpenAlex: {doi}")

    citing = client.get_citing_works(work.openalex_id, limit)

    return [client.format_work(w) for w in citing]


@mcp.tool()
def find_references(doc_id: str, limit: int = 50) -> list[dict]:
    """
    Find papers that a document references (its bibliography).

    Requires the document to have a DOI. Uses OpenAlex API.
    Rate-limited to 1 request/second (or 10/second if openalex_email configured).

    Args:
        doc_id: Document ID (Zotero item key) from search results
        limit: Maximum number of references to return (1-100)

    Returns:
        List of referenced papers with title, authors, year, DOI, and citation count
    """
    store = _get_store()
    meta = store.get_document_meta(doc_id)
    if not meta:
        raise ToolError(f"Document not found: {doc_id}")

    doi = meta.get("doi")
    if not doi:
        raise ToolError("Document has no DOI - reference lookup unavailable")

    from ..openalex_client import OpenAlexClient

    _config = _get_config()

    client = OpenAlexClient(email=_config.openalex_email)

    work = client.get_work_by_doi(doi)
    if not work:
        raise ToolError(f"Paper not found in OpenAlex: {doi}")

    references = client.get_references(work.openalex_id, limit)

    return [client.format_work(w) for w in references]


@mcp.tool()
def get_citation_count(doc_id: str) -> dict:
    """
    Get citation count and reference count for a document.

    Requires the document to have a DOI. Uses OpenAlex API.

    Args:
        doc_id: Document ID (Zotero item key) from search results

    Returns:
        Dict with cited_by_count and reference_count
    """
    store = _get_store()
    meta = store.get_document_meta(doc_id)
    if not meta:
        raise ToolError(f"Document not found: {doc_id}")

    doi = meta.get("doi")
    if not doi:
        raise ToolError("Document has no DOI - citation lookup unavailable")

    from ..openalex_client import OpenAlexClient

    _config = _get_config()

    client = OpenAlexClient(email=_config.openalex_email)

    work = client.get_work_by_doi(doi)
    if not work:
        raise ToolError(f"Paper not found in OpenAlex: {doi}")

    return {
        "doc_id": doc_id,
        "doi": doi,
        "openalex_id": work.openalex_id,
        "cited_by_count": work.cited_by_count,
        "reference_count": len(work.references),
    }
