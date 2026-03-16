"""Library browsing tools: collections, tags, paper details, overview."""
from ..state import mcp, _get_zotero, _get_store, ToolError


@mcp.tool()
def list_collections() -> list[dict]:
    """
    List all Zotero collections (folders) with their keys and hierarchy.

    Returns a list of collections, each with:
    - key: Zotero item key for the collection
    - name: collection display name
    - parent_key: key of parent collection, or null if top-level
    """
    return _get_zotero().get_all_collections()


@mcp.tool()
def get_collection_papers(collection_key: str, limit: int = 100) -> list[dict]:
    """
    Get all papers in a specific Zotero collection.

    Args:
        collection_key: The Zotero key of the collection (from list_collections)
        limit: Maximum number of papers to return (default 100)

    Returns a list of papers with key, title, authors, year, publication, doi, tags.
    """
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
def list_tags(limit: int = 200) -> list[dict]:
    """
    List all tags in the Zotero library with usage counts.

    Args:
        limit: Maximum number of tags to return, sorted by frequency (default 200)

    Returns a list of {name, count} dicts sorted by usage count descending.
    """
    tags = _get_zotero().get_all_tags()
    return tags[:limit]


@mcp.tool()
def get_paper_details(item_key: str) -> dict:
    """
    Get complete metadata for a paper by its Zotero item key.

    Args:
        item_key: The Zotero item key (e.g. "FRF9ACAJ")

    Returns full metadata including title, authors, year, publication, DOI,
    abstract, tags, collections, citation key, and whether it has been indexed
    for semantic search.
    """
    zotero = _get_zotero()
    item = zotero.get_item(item_key)
    if item is None:
        raise ToolError(f"Item not found: {item_key}")

    abstract = zotero.get_item_abstract(item_key)

    # Check if indexed in vector store
    try:
        store = _get_store()
        meta = store.get_document_meta(item_key)
        indexed = meta is not None
        quality_grade = meta.get("quality_grade", "") if meta else ""
    except Exception:
        indexed = False
        quality_grade = ""

    return {
        "key": item.item_key,
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
def get_library_overview(limit: int = 100, offset: int = 0) -> dict:
    """
    Get a paginated overview of all papers in the Zotero library.

    Args:
        limit: Number of papers per page (default 100)
        offset: Starting index for pagination (default 0)

    Returns:
    - total: total number of papers with PDFs
    - papers: list of {key, title, authors, year, publication, tags, indexed}
    - offset/limit for pagination
    """
    zotero = _get_zotero()
    all_items = zotero.get_all_items_with_pdfs()

    # Get indexed doc IDs for the "indexed" flag
    try:
        store = _get_store()
        indexed_ids = store.get_indexed_doc_ids()
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
