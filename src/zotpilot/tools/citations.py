"""Citation graph tools: citing papers, references, citation counts."""
from typing import Annotated

from pydantic import Field

from ..state import mcp, _get_store, _get_config, ToolError
from ..config import Config


@mcp.tool()
def find_citing_papers(
    doc_id: Annotated[str, Field(description="Document ID (Zotero item key) from search results")],
    limit: Annotated[int, Field(description="Max citing papers to return", ge=1, le=100)] = 20,
) -> list[dict]:
    """Find papers that cite a given document. Requires DOI. Uses OpenAlex API."""
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
def find_references(
    doc_id: Annotated[str, Field(description="Document ID (Zotero item key) from search results")],
    limit: Annotated[int, Field(description="Max references to return", ge=1, le=100)] = 50,
) -> list[dict]:
    """Find papers referenced by a document (its bibliography). Requires DOI."""
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
def get_citation_count(
    doc_id: Annotated[str, Field(description="Document ID (Zotero item key) from search results")],
) -> dict:
    """Get citation and reference counts for a document. Requires DOI."""
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
