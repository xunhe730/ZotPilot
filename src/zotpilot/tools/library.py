"""Library browsing tools: collections, tags, paper details, overview."""
import logging
import sqlite3
from typing import Annotated, Literal

from pydantic import Field

from ..state import ToolError, _get_api_reader, _get_store_optional, _get_writer, _get_zotero, mcp
from ..zotero_client import _sqlite_uri
from .profiles import tool_tags

logger = logging.getLogger(__name__)


def _truncate_text(value: str | None, limit: int = 200, add_ellipsis: bool = False) -> str:
    if not value:
        return ""
    if len(value) <= limit:
        return value
    suffix = "..." if add_ellipsis else ""
    trim_limit = max(limit - len(suffix), 0)
    return f"{value[:trim_limit]}{suffix}"


def _invalidate_collection_cache():
    """No-op: collection queries are now direct SQL, no cache needed."""
    pass


@mcp.tool(tags=tool_tags("extended", "library"))
def browse_library(
    view: Annotated[
        Literal["overview", "tags", "collections", "papers", "collection_papers", "feeds"],
        Field(description="'overview' lists papers; 'tags' lists tag vocabulary; 'collections' lists folders; 'papers' lists papers in one collection; 'feeds' lists feeds or feed items"),  # noqa: E501
    ] = "overview",
    collection_key: Annotated[
        str | None,
        Field(description="Required when view='papers'. Collection key to inspect."),
    ] = None,
    library_id: Annotated[
        int | None,
        Field(description="Feed library ID when view='feeds'. None lists all feeds."),
    ] = None,
    limit: Annotated[int, Field(description="Max results per page or collection listing", ge=1)] = 100,
    offset: Annotated[int, Field(description="Starting index for overview pagination", ge=0)] = 0,
    verbosity: Annotated[
        Literal["minimal", "standard", "full"],
        Field(description="Metadata richness for overview rows and collection/feed results"),
    ] = "minimal",
) -> dict | list[dict]:
    """Browse lightweight library views without running a full library profile.

    Use view='overview' for paginated papers, view='tags' for the tag vocabulary,
    view='collections' for folder hierarchy, view='papers' for papers in one
    collection, or view='feeds' for RSS feeds and feed items.
    Returns the same structures as the underlying browse tools for compatibility.
    """
    if view == "overview":
        return _get_library_overview_impl(limit=limit, offset=offset, verbosity=verbosity)
    if view == "tags":
        return _list_tags_impl(limit=limit)
    if view == "collections":
        return _list_collections_impl()
    if view == "feeds":
        return _get_feeds_impl(library_id=library_id, limit=limit, verbosity=verbosity)
    if view in {"papers", "collection_papers"} and not collection_key:
        raise ToolError(
            "browse_library(view='papers') requires collection_key. "
            "Call browse_library(view='collections') first to choose a collection."
        )
    if not collection_key:
        raise ToolError(f"Unsupported browse_library view: {view}")
    return _get_collection_papers_impl(collection_key=collection_key, limit=limit)


def _list_collections_impl() -> list[dict]:
    return _get_zotero().get_all_collections()



def _get_collection_papers_impl(collection_key: str, limit: int = 100) -> list[dict]:
    return _get_zotero().get_collection_items(collection_key, limit)



def _list_tags_impl(limit: int = 200) -> list[dict]:
    tags = _get_zotero().get_all_tags()
    return tags[:limit]



@mcp.tool(tags=tool_tags("core", "context"))
def get_paper_details(
    doc_id: Annotated[str, Field(description="Document ID (Zotero item key) from search results")],
) -> dict:
    """Get complete metadata for a paper including abstract, tags, and index status."""
    zotero = _get_zotero()
    item = zotero.get_item(doc_id)
    if item is None:
        raise ToolError(f"Item not found: {doc_id}")

    abstract = zotero.get_item_abstract(doc_id)

    # Check if indexed in vector store
    try:
        store = _get_store_optional()
        if store is not None:
            meta = store.get_document_meta(doc_id)
            indexed = meta is not None
            quality_grade = meta.get("quality_grade", "") if meta else ""
        else:
            indexed = False
            quality_grade = ""
    except Exception:
        indexed = False
        quality_grade = ""

    return {
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
        "date_added": item.date_added,
    }


def _get_library_overview_impl(
    limit: int = 100,
    offset: int = 0,
    verbosity: Literal["minimal", "standard", "full"] = "minimal",
) -> dict:
    zotero = _get_zotero()
    all_items = zotero.get_all_items_with_pdfs()

    # Get indexed doc IDs for the "indexed" flag
    try:
        store = _get_store_optional()
        indexed_ids = store.get_indexed_doc_ids() if store is not None else set()
    except Exception:
        indexed_ids = set()

    page = all_items[offset:offset + limit]

    def _paper_row(item):
        row = {
            "doc_id": item.item_key,
            "title": item.title,
            "year": item.year,
            "indexed": item.item_key in indexed_ids,
        }
        if verbosity in {"standard", "full"}:
            row.update({
                "authors": item.authors,
                "publication": item.publication,
                "tags": item.tags,
                "collections": item.collections,
                "citation_key": item.citation_key,
            })
        return row

    return {
        "total": len(all_items),
        "offset": offset,
        "limit": limit,
        "papers": [_paper_row(item) for item in page],
    }



@mcp.tool(tags=tool_tags("extended", "library"))
def get_notes(
    item_key: Annotated[str | None, Field(description="Parent item key. None for all notes.")] = None,
    limit: Annotated[int, Field(description="Max notes to return", ge=1, le=200)] = 20,
    query: Annotated[str | None, Field(description="Search within note content (case-insensitive)")] = None,
    verbosity: Annotated[
        Literal["minimal", "standard", "full"],
        Field(description="Content richness for notes"),
    ] = "minimal",
) -> list[dict]:
    """Get or search notes. Filter by parent item and/or content keyword."""
    notes = _get_zotero().get_notes(item_key=item_key, query=query, limit=limit)
    try:
        writer = _get_writer()
        api_notes = writer.get_notes(item_key=item_key, query=query, limit=limit)
        if api_notes:
            # Merge SQLite and Web API views; Web API wins on key conflict.
            merged: dict[str, dict] = {n["key"]: n for n in notes}
            for n in api_notes:
                merged[n["key"]] = n
            notes = list(merged.values())[:limit]
    except ToolError:
        pass  # No API key — accept SQLite-only result
    if verbosity == "minimal":
        return [
            {
                "key": note.get("key"),
                "parent_key": note.get("parent_key"),
                "parent_title": note.get("parent_title"),
                "title": note.get("title"),
                "content": _truncate_text(note.get("content"), 200, add_ellipsis=True),
            }
            for note in notes
        ]
    return notes


def _get_feeds_impl(
    library_id: Annotated[int | None, Field(description="Feed library ID for items. None to list all feeds.")] = None,
    limit: Annotated[int, Field(description="Max feed items", ge=1, le=100)] = 20,
    verbosity: Annotated[
        Literal["minimal", "standard", "full"],
        Field(description="Metadata richness for feed items"),
    ] = "minimal",
) -> dict:
    """List RSS feeds or get items from a feed. Works without indexing."""
    zotero = _get_zotero()
    if library_id is None:
        feeds = zotero.get_feeds()
        return {"feeds": feeds, "total": len(feeds)}
    else:
        items = zotero.get_feed_items(library_id, limit=limit)
        if verbosity == "minimal":
            items = [
                {
                    "key": item.get("key"),
                    "title": item.get("title"),
                    "date_added": item.get("date_added"),
                    "read": item.get("read"),
                }
                for item in items
            ]
        elif verbosity == "standard":
            items = [
                {
                    "key": item.get("key"),
                    "title": item.get("title"),
                    "authors": item.get("authors"),
                    "url": item.get("url"),
                    "date_added": item.get("date_added"),
                    "read": item.get("read"),
                }
                for item in items
            ]
        return {"library_id": library_id, "items": items, "total": len(items)}


@mcp.tool(tags=tool_tags("extended", "library"))
def get_annotations(
    item_key: Annotated[str | None, Field(description="Item key. None for all annotations.")] = None,
    limit: Annotated[int, Field(description="Max annotations", ge=1, le=200)] = 50,
    verbosity: Annotated[
        Literal["minimal", "standard", "full"],
        Field(description="Content richness for annotations"),
    ] = "minimal",
) -> list[dict]:
    """Get highlights and comments. Requires ZOTERO_API_KEY."""
    annotations = _get_api_reader().get_annotations(item_key=item_key, limit=limit)
    if verbosity == "minimal":
        return [
            {
                **annotation,
                "text": _truncate_text(annotation.get("text"), 200, add_ellipsis=True),
                "comment": _truncate_text(annotation.get("comment"), 200, add_ellipsis=True),
            }
            for annotation in annotations
        ]
    return annotations


@mcp.tool(tags=tool_tags("extended", "library"))
def profile_library(
    include_profile: Annotated[bool, Field(description="Include full existing profile text")] = False,
) -> dict:
    """Analyze the Zotero library to generate a user profile for research context.

    Returns library statistics including year distribution, top tags, collections,
    and topic density from the vector index (if available).

    Also returns a summary of ~/.config/zotpilot/ZOTPILOT.md if it exists,
    so agents can see the existing user profile without needing filesystem access.

    Pure read operation — no side effects."""
    from pathlib import Path

    zotero = _get_zotero()

    # --- total items and year distribution: query SQLite directly so all items
    #     are counted, not just those that happen to have PDF attachments ---
    conn = sqlite3.connect(_sqlite_uri(zotero.db_path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        total_row = conn.execute("""
            SELECT COUNT(*) as cnt
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            WHERE i.libraryID = ?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
              AND it.typeName NOT IN ('note', 'attachment')
        """, (zotero.library_id,)).fetchone()
        total_items = total_row["cnt"] if total_row else 0

        year_rows = conn.execute("""
            SELECT CAST(substr(idv.value, 1, 4) AS TEXT) AS year, COUNT(*) AS cnt
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            JOIN itemData id ON i.itemID = id.itemID
            JOIN itemDataValues idv ON id.valueID = idv.valueID
            JOIN fields f ON id.fieldID = f.fieldID
            WHERE i.libraryID = ?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
              AND it.typeName NOT IN ('note', 'attachment')
              AND f.fieldName = 'date'
              AND length(idv.value) >= 4
              AND CAST(substr(idv.value, 1, 4) AS INTEGER) > 1000
            GROUP BY year
            ORDER BY year DESC
        """, (zotero.library_id,)).fetchall()
        year_distribution = {r["year"]: r["cnt"] for r in year_rows}

        col_rows = conn.execute("""
            SELECT c.key, c.collectionName, COUNT(ci.itemID) AS cnt
            FROM collections c
            JOIN collectionItems ci ON c.collectionID = ci.collectionID
            JOIN items i ON ci.itemID = i.itemID
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            WHERE c.libraryID = ?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
              AND it.typeName NOT IN ('note', 'attachment')
            GROUP BY c.collectionID, c.key, c.collectionName
            ORDER BY cnt DESC
        """, (zotero.library_id,)).fetchall()
        col_counts = [{"key": r["key"], "name": r["collectionName"], "count": r["cnt"]} for r in col_rows]

        journal_rows = conn.execute("""
            SELECT idv.value AS journal, COUNT(*) AS cnt
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            JOIN itemData id ON i.itemID = id.itemID
            JOIN itemDataValues idv ON id.valueID = idv.valueID
            JOIN fields f ON id.fieldID = f.fieldID
            WHERE i.libraryID = ?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
              AND it.typeName NOT IN ('note', 'attachment')
              AND f.fieldName = 'publicationTitle'
              AND idv.value != ''
            GROUP BY idv.value
            ORDER BY cnt DESC
            LIMIT 20
        """, (zotero.library_id,)).fetchall()
        top_journals = [{"name": r["journal"], "count": r["cnt"]} for r in journal_rows]
    finally:
        conn.close()

    # --- top tags (top 20) ---
    tags = zotero.get_all_tags()
    top_tags = [t["name"] for t in tags[:20]]

    # --- top collections (top 10 by item count) ---
    top_collections = col_counts[:10]

    # --- topic density from vector index ---
    store = _get_store_optional()
    if store is None:
        topic_density = {"indexed": False}
    else:
        try:
            doc_count = len(store.get_indexed_doc_ids())
            topic_density = {"indexed": True, "doc_count": doc_count}
        except Exception as e:
            logger.warning("Could not get index doc count: %s", e)
            topic_density = {"indexed": True, "doc_count": 0}

    # --- gaps analysis ---
    gaps: list[str] = []
    if year_distribution:
        min_year = min(int(y) for y in year_distribution)
        pre_2015_count = sum(v for k, v in year_distribution.items() if int(k) < 2015)
        if min_year >= 2015 or (total_items > 0 and pre_2015_count / total_items < 0.05):
            gaps.append("sparse coverage before 2015")
    survey_tags = {"review", "survey", "meta-analysis", "systematic review"}
    tagged_survey = sum(
        t["count"] for t in tags if t["name"].lower() in survey_tags
    )
    if total_items > 0 and tagged_survey / total_items < 0.05:
        gaps.append("few survey/review papers")

    # --- existing profile ---
    profile_path = Path("~/.config/zotpilot/ZOTPILOT.md").expanduser()
    existing_profile: str | None = None
    if profile_path.exists():
        try:
            existing_profile = profile_path.read_text(encoding="utf-8")
        except Exception:
            existing_profile = None

    result = {
        "total_items": total_items,
        "year_distribution": year_distribution,
        "top_tags": top_tags,
        "top_collections": top_collections,
        "top_journals": top_journals,
        "topic_density": topic_density,
        "gaps": gaps,
        "existing_profile_present": existing_profile is not None,
        "existing_profile_length": len(existing_profile) if existing_profile else 0,
        "existing_profile_snippet": _truncate_text(existing_profile, 200, add_ellipsis=True),
    }

    if include_profile:
        result["existing_profile"] = existing_profile

    return result
