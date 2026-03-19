"""Library browsing tools: collections, tags, paper details, overview."""
from typing import Annotated

from pydantic import Field

from ..state import mcp, _get_zotero, _get_store, _get_store_optional, _get_api_reader, ToolError


def _invalidate_collection_cache():
    """Reset the cached collection map so next call re-fetches from DB."""
    get_collection_papers._col_map = None


@mcp.tool()
def list_collections() -> list[dict]:
    """List all Zotero collections (folders) with keys and hierarchy."""
    return _get_zotero().get_all_collections()


@mcp.tool()
def get_collection_papers(
    collection_key: Annotated[str, Field(description="Collection key from list_collections")],
    limit: Annotated[int, Field(description="Max papers to return", ge=1)] = 100,
) -> list[dict]:
    """Get papers in a specific Zotero collection."""
    zotero = _get_zotero()
    all_items = zotero.get_all_items_with_pdfs()
    result = []
    for item in all_items:
        cols = [c.strip() for c in item.collections.split(";") if c.strip()] if item.collections else []
        # Match by collection key -- need to resolve collection names to keys
        # Use a join: get all collections and match by key
        if not hasattr(get_collection_papers, "_col_map"):
            get_collection_papers._col_map = None
        if get_collection_papers._col_map is None:
            collections = zotero.get_all_collections()
            get_collection_papers._col_map = {c["key"]: c["name"] for c in collections}
        col_map = get_collection_papers._col_map
        col_name = col_map.get(collection_key)
        if col_name and col_name in cols:
            result.append({
                "key": item.item_key,
                "title": item.title,
                "authors": item.authors,
                "year": item.year,
                "publication": item.publication,
                "doi": item.doi,
                "tags": item.tags,
                "citation_key": item.citation_key,
            })
        if len(result) >= limit:
            break
    return result


@mcp.tool()
def list_tags(
    limit: Annotated[int, Field(description="Max tags to return", ge=1)] = 200,
) -> list[dict]:
    """List all tags in the library sorted by usage count."""
    tags = _get_zotero().get_all_tags()
    return tags[:limit]


@mcp.tool()
def get_paper_details(
    item_key: Annotated[str, Field(description="Zotero item key")],
) -> dict:
    """Get complete metadata for a paper including abstract, tags, and index status."""
    zotero = _get_zotero()
    item = zotero.get_item(item_key)
    if item is None:
        raise ToolError(f"Item not found: {item_key}")

    abstract = zotero.get_item_abstract(item_key)

    # Check if indexed in vector store
    try:
        store = _get_store_optional()
        if store is not None:
            meta = store.get_document_meta(item_key)
            indexed = meta is not None
            quality_grade = meta.get("quality_grade", "") if meta else ""
        else:
            indexed = False
            quality_grade = ""
    except Exception:
        indexed = False
        quality_grade = ""

    return {
        "key": item.item_key,
        "doc_id": item.item_key,
        "title": item.title,
        "authors": item.authors,
        "year": item.year,
        "publication": item.publication,
        "doi": item.doi,
        "abstract": abstract,
        "tags": item.tags,
        "collections": item.collections,
        "citation_key": item.citation_key,
        "pdf_available": item.pdf_path is not None and item.pdf_path.exists(),
        "indexed": indexed,
        "quality_grade": quality_grade,
    }


@mcp.tool()
def get_library_overview(
    limit: Annotated[int, Field(description="Papers per page", ge=1)] = 100,
    offset: Annotated[int, Field(description="Starting index for pagination", ge=0)] = 0,
) -> dict:
    """Paginated overview of all papers in the library."""
    zotero = _get_zotero()
    all_items = zotero.get_all_items_with_pdfs()

    # Get indexed doc IDs for the "indexed" flag
    try:
        store = _get_store_optional()
        indexed_ids = store.get_indexed_doc_ids() if store is not None else set()
    except Exception:
        indexed_ids = set()

    page = all_items[offset:offset + limit]
    return {
        "total": len(all_items),
        "offset": offset,
        "limit": limit,
        "papers": [
            {
                "key": item.item_key,
                "title": item.title,
                "authors": item.authors,
                "year": item.year,
                "publication": item.publication,
                "tags": item.tags,
                "collections": item.collections,
                "citation_key": item.citation_key,
                "indexed": item.item_key in indexed_ids,
            }
            for item in page
        ],
    }


@mcp.tool()
def get_notes(
    item_key: Annotated[str | None, Field(description="Parent item key. None for all notes.")] = None,
    limit: Annotated[int, Field(description="Max notes to return", ge=1, le=200)] = 20,
    query: Annotated[str | None, Field(description="Search within note content (case-insensitive)")] = None,
) -> list[dict]:
    """Get or search notes. Filter by parent item and/or content keyword."""
    return _get_zotero().get_notes(item_key=item_key, query=query, limit=limit)


@mcp.tool()
def get_feeds(
    library_id: Annotated[int | None, Field(description="Feed library ID for items. None to list all feeds.")] = None,
    limit: Annotated[int, Field(description="Max feed items", ge=1, le=100)] = 20,
) -> dict:
    """List RSS feeds or get items from a feed. Works without indexing."""
    zotero = _get_zotero()
    if library_id is None:
        feeds = zotero.get_feeds()
        return {"feeds": feeds, "total": len(feeds)}
    else:
        items = zotero.get_feed_items(library_id, limit=limit)
        return {"library_id": library_id, "items": items, "total": len(items)}


@mcp.tool()
def get_annotations(
    item_key: Annotated[str | None, Field(description="Item key. None for all annotations.")] = None,
    limit: Annotated[int, Field(description="Max annotations", ge=1, le=200)] = 50,
) -> list[dict]:
    """Get highlights and comments. Requires ZOTERO_API_KEY."""
    return _get_api_reader().get_annotations(item_key=item_key, limit=limit)
