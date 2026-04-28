"""Indexing tools: index_library, get_index_stats."""

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Any

from pydantic import BeforeValidator, Field

from ..index_authority import (
    IndexJournal,
    IndexLease,
    LeaseContentionError,
    acquire_lease,
    authoritative_indexed_doc_ids,
    current_library_pdf_doc_ids,
    release_lease,
)
from ..reranker import VALID_QUARTILES, VALID_SECTIONS
from ..state import ToolError, _get_config, _get_reranker, _get_retriever, _get_store, _get_zotero, _index_lock, mcp
from .profiles import tool_tags

logger = logging.getLogger(__name__)


def _parse_json_string_list(value: Any) -> Any:
    """Accept list params even when an MCP client wraps them as JSON strings."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
        if isinstance(parsed, list):
            return parsed
    return value


def _collect_unindexed_papers(limit: int | None = None, offset: int = 0) -> tuple[list[dict], int]:
    """Return unindexed Zotero papers and their total count."""
    zotero = _get_zotero()
    current_doc_ids = current_library_pdf_doc_ids(zotero)
    indexed_set = authoritative_indexed_doc_ids(_get_store(), current_doc_ids)
    papers: list[dict] = []
    total = 0

    for item in zotero.get_all_items_with_pdfs():
        if item.item_key in indexed_set:
            continue
        total += 1
        if total <= offset:
            continue
        if limit is not None and len(papers) >= limit:
            continue
        papers.append(
            {
                "doc_id": item.item_key,
                "title": item.title or "(no title)",
                "year": item.year,
                "authors": item.authors,
            }
        )

    return papers, total


def _get_reranking_config_impl() -> dict:
    _config = _get_config()
    if _config.embedding_provider == "none":
        return {
            "enabled": False,
            "mode": "no-rag",
            "message": "Reranking unavailable in No-RAG mode. Configure an embedding provider to enable semantic search.",  # noqa: E501
        }
    _get_retriever()  # Ensure initialized
    reranker = _get_reranker()

    return {
        "enabled": _config.rerank_enabled,
        "alpha": reranker.alpha,
        "section_weights": reranker.default_section_weights,
        "journal_weights": {
            k if k is not None else "unknown": v for k, v in reranker.quartile_weights.items() if k != ""
        },
        "valid_sections": sorted(VALID_SECTIONS),
        "valid_quartiles": sorted(VALID_QUARTILES),
        "oversample_multiplier": _config.oversample_multiplier,
    }


def _get_vision_costs_impl(last_n: int = 10) -> dict:
    _config = _get_config()

    log_path = Path(_config.chroma_db_path).parent / "vision_costs.json"

    if not log_path.exists():
        return {
            "message": "Vision API has not been used yet -- no cost log found.",
            "log_path": log_path.name,
        }

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise ToolError(f"Failed to read vision cost log: {exc}")

    if not entries:
        return {
            "message": "Vision cost log exists but contains no entries.",
            "log_path": log_path.name,
        }

    total_cost = 0.0
    total_input = 0
    total_output = 0
    total_cache_write = 0
    total_cache_read = 0
    session_map: dict[str, dict] = {}

    for entry in entries:
        total_cost += entry.get("cost_usd", 0.0)
        total_input += entry.get("input_tokens", 0)
        total_output += entry.get("output_tokens", 0)
        total_cache_write += entry.get("cache_write_tokens", 0)
        total_cache_read += entry.get("cache_read_tokens", 0)

        sid = entry.get("session_id", "unknown")
        ts = entry.get("timestamp", "")
        if sid not in session_map:
            session_map[sid] = {"session_id": sid, "first_timestamp": ts, "table_count": 0, "cost_usd": 0.0}
        session_map[sid]["table_count"] += 1
        session_map[sid]["cost_usd"] += entry.get("cost_usd", 0.0)
        if ts and ts < session_map[sid]["first_timestamp"]:
            session_map[sid]["first_timestamp"] = ts

    total_tables = len(entries)
    avg_cost = total_cost / total_tables if total_tables else 0.0

    sessions = [
        {
            "session_id": s["session_id"],
            "first_timestamp": s["first_timestamp"],
            "table_count": s["table_count"],
            "cost_usd": round(s["cost_usd"], 6),
        }
        for s in session_map.values()
    ]
    sessions.sort(key=lambda x: x["first_timestamp"])

    recent = entries[-last_n:] if last_n > 0 else []

    return {
        "total_cost_usd": round(total_cost, 6),
        "total_tables": total_tables,
        "avg_cost_per_table_usd": round(avg_cost, 6),
        "tokens": {
            "input": total_input,
            "output": total_output,
            "cache_write": total_cache_write,
            "cache_read": total_cache_read,
        },
        "sessions": sessions,
        "recent_entries": recent,
        "log_path": log_path.name,
    }


@mcp.tool(tags=tool_tags("extended", "indexing"))
def index_library(
    force_reindex: Annotated[bool, Field(description="Delete and rebuild index for matching items")] = False,
    limit: Annotated[int | None, Field(description="Max items to index, None for all")] = None,
    item_key: Annotated[str | None, Field(description="Index only this specific item key")] = None,
    item_keys: Annotated[
        list[str] | None,
        BeforeValidator(_parse_json_string_list),
        Field(description="Index only these specific item keys"),
    ] = None,
    title_pattern: Annotated[str | None, Field(description="Regex to filter items by title (case-insensitive)")] = None,
    no_vision: Annotated[bool, Field(description="Disable vision-based table extraction")] = False,
    batch_size: Annotated[
        int,
        Field(
            description="Items per batch (default 2). Set 0 for all at once. "  # noqa: E501
        ),
    ] = 2,  # noqa: E501
    max_pages: Annotated[
        int | None, Field(description="Skip PDFs over N pages. None uses config default (40). 0=no limit.")
    ] = None,  # noqa: E501
    include_summary: Annotated[bool, Field(description="Include extended indexing summary fields")] = False,
    acknowledge_metadata_only: Annotated[
        bool, Field(description="Set True after presenting metadata_only_choice to the user and receiving consent.")
    ] = False,  # noqa: E501
    session_id: Annotated[
        str | None,
        Field(
            description=(
                "Optional research session id. When called from inside a "
                "ztp-research workflow, the SOP requires passing the active "
                "session_id so the post-ingest-review gate can fire. CLI / "
                "ad-hoc indexing should leave this None."
            )
        ),
    ] = None,
) -> dict:
    """Index Zotero PDFs into the vector store. Incremental by default; processes batch_size items per call. Repeat until has_more=false to index all.

    Concurrency: Only one indexing operation can run at a time. Concurrent calls
    will receive a ToolError: "Indexing in progress, please wait."

    Post-ingest gate: when called from a ztp-research workflow, pass session_id so the
    post-ingest-review checkpoint gate can fire. Indexing before checkpoint 2 approval writes
    unverified items into ChromaDB and burns embedding API quota (2026-04-08 incident).
    CLI / ad-hoc indexing: leave session_id=None to bypass the gate entirely.
    """  # noqa: E501
    if not _index_lock.acquire(blocking=False):
        raise ToolError("Indexing in progress, please wait.")
    try:
        from dataclasses import replace as dc_replace

        from ..indexer import Indexer

        # Direct Python callers can still bypass FastMCP/Pydantic dispatch, so
        # keep the same string->list coercion here for consistency.
        item_keys = _parse_json_string_list(item_keys)

        _config = _get_config()

        errors = _config.validate()
        if errors:
            raise ToolError(f"Config errors: {'; '.join(errors)}")

        config = _config

        # Set up journal/lease for this indexing run
        index_data_root = Path(config.chroma_db_path).parent
        journal_path = index_data_root / "index_journal.json"
        lease_path = index_data_root / "index_lease.json"
        journal = IndexJournal(journal_path)
        lease = IndexLease(lease_path)

        # Acquire mutual-exclusion lease
        try:
            acquire_lease(lease)
        except LeaseContentionError as e:
            raise ToolError(str(e))

        # Batch mode defaults to no_vision to avoid many small vision API calls
        if batch_size > 0 and not no_vision:
            config = dc_replace(config, vision_enabled=False)
        elif no_vision:
            config = dc_replace(config, vision_enabled=False)

        effective_max_pages = max_pages if max_pages is not None else config.max_pages

        indexer = Indexer(config)
        result = indexer.index_all(
            force_reindex=force_reindex,
            limit=limit,
            item_key=item_key,
            item_keys=item_keys,
            title_pattern=title_pattern,
            max_pages=effective_max_pages,
            batch_size=batch_size if batch_size > 0 else None,
            journal=journal,
        )

        # Clear query embedding cache so new documents are findable
        _get_store().clear_query_cache()

        # Serialize IndexResult objects
        serialized_results = []
        for r in result["results"]:
            serialized_results.append(
                {
                    "item_key": r.item_key,
                    "title": r.title,
                    "status": r.status,
                    "reason": r.reason,
                    "n_chunks": r.n_chunks,
                    "n_tables": r.n_tables,
                    "quality_grade": r.quality_grade,
                }
            )

        skipped_no_pdf_all: list[dict] = result.get("skipped_no_pdf", [])
        skipped_no_pdf_count = len(skipped_no_pdf_all)
        skipped_no_pdf_items = skipped_no_pdf_all[:50]
        has_more_skipped = skipped_no_pdf_count > 50

        response = {
            "results": serialized_results,
            "indexed": result["indexed"],
            "failed": result["failed"],
            "empty": result["empty"],
            "skipped": result["skipped"],
            "already_indexed": result["already_indexed"],
            "has_more": result.get("has_more", False),
            "vision_enabled": config.vision_enabled,
            "skipped_no_pdf_count": skipped_no_pdf_count,
            "skipped_no_pdf_items": skipped_no_pdf_items,
        }
        if has_more_skipped:
            response["skipped_no_pdf_has_more"] = True

        if skipped_no_pdf_count > 0:
            response["_notice_no_pdf"] = (
                f"\u26a0\ufe0f {skipped_no_pdf_count} paper(s) skipped during indexing (no PDF attachment). "
                "These are metadata-only entries. Log in via institutional VPN and re-ingest to add PDFs, "
                "or treat as reference-only."
            )

        if include_summary:
            response.update(
                {
                    "quality_distribution": result.get("quality_distribution"),
                    "extraction_stats": result.get("extraction_stats"),
                    "long_documents": result.get("long_documents", []),
                    "skipped_long": result.get("skipped_long", 0),
                    "total_to_index": result.get("total_to_index", 0),
                    "vision_pending_tables": result.get("vision_pending_tables", 0),
                    "vision_estimated_cost_usd": result.get("vision_estimated_cost_usd", 0.0),
                    "vision_budget_skipped": result.get("vision_budget_skipped", False),
                    "vision_skip_reason": result.get("vision_skip_reason"),
                }
            )

        return response
    finally:
        release_lease(lease)
        _index_lock.release()


@mcp.tool(tags=tool_tags("core", "indexing"))
def get_index_stats(
    limit: Annotated[int, Field(description="Papers per page for the unindexed paper list", ge=0, le=200)] = 50,
    offset: Annotated[int, Field(description="Starting index for the unindexed paper list", ge=0)] = 0,
    include_config: Annotated[bool, Field(description="Include reranking configuration details")] = False,
    include_vision_costs: Annotated[
        bool,
        Field(description="Include vision extraction usage and cost summary"),
    ] = False,
    last_n: Annotated[
        int,
        Field(description="Recent vision cost log entries to include when include_vision_costs=true", ge=0),
    ] = 10,
) -> dict:
    """Get index statistics and list of unindexed papers. Call first to check readiness."""
    _config = _get_config()
    if _config.embedding_provider == "none":
        result = {
            "total_documents": 0,
            "unindexed_count": 0,
            "unindexed_papers": [],
            "sample_unindexed": [],
            "offset": offset,
            "limit": limit,
            "mode": "no-rag",
            "message": "Indexing disabled in No-RAG mode. Use advanced_search, get_notes, list_tags, etc. for basic features. Configure an embedding provider to enable semantic search.",  # noqa: E501
        }
        if include_config:
            result["reranking_config"] = _get_reranking_config_impl()
        if include_vision_costs:
            result["vision_costs"] = _get_vision_costs_impl(last_n=last_n)
        return result
    _get_retriever()  # Ensure initialized
    store = _get_store()
    zotero = _get_zotero()
    current_doc_ids = current_library_pdf_doc_ids(zotero)
    _config = _get_config()
    doc_ids = authoritative_indexed_doc_ids(store, current_doc_ids)
    total_chunks = store.count_chunks_for_doc_ids(doc_ids)

    # Get section, journal, and chunk type coverage from a sample of chunks
    # (Getting all chunks would be expensive for large collections)
    sample = store.collection.get(limit=_config.stats_sample_limit, include=["metadatas"])

    section_counts: dict[str, int] = defaultdict(int)
    journal_doc_quartiles: dict[str, str] = {}  # doc_id -> quartile
    chunk_type_counts: dict[str, int] = defaultdict(int)

    if sample["metadatas"]:
        for meta in sample["metadatas"]:
            if meta.get("doc_id", "") not in doc_ids:
                continue
            section = meta.get("section", "unknown")
            section_counts[section] += 1

            chunk_type = meta.get("chunk_type", "text")
            chunk_type_counts[chunk_type] += 1

            doc_id = meta.get("doc_id", "")
            quartile = meta.get("journal_quartile", "")
            if doc_id and doc_id not in journal_doc_quartiles:
                journal_doc_quartiles[doc_id] = quartile

    # Count documents per quartile
    journal_counts: dict[str, int] = defaultdict(int)
    for quartile in journal_doc_quartiles.values():
        key = quartile if quartile else "unknown"
        journal_counts[key] += 1

    # Check for unindexed papers: items in Zotero with PDFs but not in ChromaDB
    unindexed_papers: list[dict] = []
    unindexed_count = 0
    try:
        unindexed_papers, unindexed_count = _collect_unindexed_papers(limit=limit, offset=offset)
    except Exception as e:
        logger.warning(f"Could not check for unindexed papers: {e}")

    result = {
        "total_documents": len(doc_ids),
        "total_chunks": total_chunks,
        "avg_chunks_per_doc": round(total_chunks / len(doc_ids), 1) if doc_ids else 0,
        "section_coverage": dict(section_counts),
        "journal_coverage": dict(journal_counts),
        "chunk_types": dict(chunk_type_counts),
        "unindexed_count": unindexed_count,
        "unindexed_papers": unindexed_papers,
        "sample_unindexed": unindexed_papers[:5],
        "offset": offset,
        "limit": limit,
    }

    if unindexed_count:
        sample_note = f" Showing {len(unindexed_papers)} result(s) from offset {offset}." if unindexed_papers else ""
        result["_notice"] = (
            f"\u26a0\ufe0f {unindexed_count} paper(s) in Zotero are not yet indexed."
            f"{sample_note} Call index_library() to update the RAG library."
        )

    if include_config:
        result["reranking_config"] = _get_reranking_config_impl()
    if include_vision_costs:
        result["vision_costs"] = _get_vision_costs_impl(last_n=last_n)

    return result
