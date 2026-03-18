"""ChromaDB filter builders and post-retrieval text filters."""

VALID_CHUNK_TYPES = {"text", "figure", "table"}


def _build_chromadb_filters(
    year_min: int | None = None,
    year_max: int | None = None,
    chunk_types: list[str] | None = None,
) -> dict | None:
    """Build ChromaDB where clause for year range and chunk_type filters.

    IMPORTANT: ChromaDB only supports: $eq, $ne, $gt, $gte, $lt, $lte, $in, $nin
    It does NOT support substring/contains operations on metadata.
    Text-based filters (author, tag, collection) must use _apply_text_filters().

    Args:
        year_min: Minimum publication year
        year_max: Maximum publication year
        chunk_types: Filter to specific chunk types (text, figure, table)

    Returns:
        ChromaDB where clause dict, or None if no filters
    """
    conditions = []
    if year_min:
        conditions.append({"year": {"$gte": year_min}})
    if year_max:
        conditions.append({"year": {"$lte": year_max}})
    if chunk_types:
        if len(chunk_types) == 1:
            conditions.append({"chunk_type": {"$eq": chunk_types[0]}})
        else:
            conditions.append({"chunk_type": {"$in": chunk_types}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _meta_get(r, key: str, default: str = "") -> str:
    """Get a metadata field from StoredChunk (.metadata dict) or RetrievalResult (attrs)."""
    if hasattr(r, "metadata") and isinstance(r.metadata, dict):
        return r.metadata.get(key, default)
    return getattr(r, key, default)


def _apply_text_filters(
    results: list,
    author: str | None = None,
    tag: str | None = None,
    collection: str | None = None,
) -> list:
    """Apply substring-based filters in Python (post-retrieval).

    ChromaDB doesn't support substring matching, so we filter after retrieval.
    All matches are case-insensitive substrings.

    Works with both StoredChunk (metadata dict) and RetrievalResult (dataclass attrs).

    Args:
        results: List of StoredChunk or RetrievalResult objects
        author: Author name substring (case-insensitive)
        tag: Tag substring (case-insensitive)
        collection: Collection name substring (case-insensitive)

    Returns:
        Filtered list
    """
    if not author and not tag and not collection:
        return results

    author_lower = author.lower() if author else None
    tag_lower = tag.lower() if tag else None
    collection_lower = collection.lower() if collection else None

    filtered = []
    for r in results:
        if author_lower:
            authors = _meta_get(r, "authors", "").lower()
            if author_lower not in authors:
                continue

        if tag_lower:
            tags = _meta_get(r, "tags", "").lower()
            if tag_lower not in tags:
                continue

        if collection_lower:
            colls = _meta_get(r, "collections", "").lower()
            if collection_lower not in colls:
                continue

        filtered.append(r)

    return filtered


def _has_text_filters(author: str | None, tag: str | None, collection: str | None) -> bool:
    """Check if any text-based filters are active."""
    return bool(author or tag or collection)


def _apply_required_terms(results: list, terms: list[str]) -> list:
    """Filter results to only those containing all required terms as whole words.

    Case-insensitive. Checks the passage text (and full_context if available).
    """
    import re
    patterns = [re.compile(r'\b' + re.escape(t) + r'\b', re.IGNORECASE) for t in terms]

    filtered = []
    for r in results:
        text = getattr(r, 'text', '') or ''
        full_ctx = r.full_context() if hasattr(r, 'full_context') and callable(getattr(r, 'full_context', None)) else ''
        combined = text + ' ' + full_ctx
        if all(p.search(combined) for p in patterns):
            filtered.append(r)
    return filtered
