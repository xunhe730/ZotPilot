"""Ingest singletons, helpers, and MCP tools for the ingestion package."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Literal

from fastmcp.exceptions import ToolError
from pydantic import Field

from ...bridge import DEFAULT_PORT
from ...state import mcp, register_reset_callback
from .. import ingestion_bridge, ingestion_search
from ..ingest_state import BatchState, BatchStore, IngestItemState
from ..profiles import tool_tags
from ._shared import (
    _writer_lock,
    logger,
)

_batch_store = BatchStore(max_batches=50, completed_ttl_s=1800.0)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")


def _clear_batch_store() -> None:
    _batch_store.clear()


register_reset_callback(_clear_batch_store)


def _update_session_after_ingest(batch) -> None:
    pass  # Session update removed in US-001


# Inbox collection cache moved to _shared.py (US-002 iter-9 shrink)
from ._shared import (  # noqa: E402,F401  re-exported via package __init__
    _INBOX_COLLECTION_NAME,
    _clear_inbox_cache,
    _ensure_inbox_collection,
    _inbox_collection_key,
    _inbox_lock,
)


def _resolve_dois_concurrent(dois: list[str]) -> dict[str, str | None]:
    """Resolve multiple DOIs concurrently.

    Lazy-imports `resolve_doi_to_landing_url` from the package namespace so
    tests patching `zotpilot.tools.ingestion.resolve_doi_to_landing_url` take
    effect at call time.
    """
    if not dois:
        return {}
    from . import resolve_doi_to_landing_url as _pkg_resolver  # type: ignore[attr-defined]

    results: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=min(len(dois), 10)) as pool:
        futures = {pool.submit(_pkg_resolver, doi): doi for doi in dois}
        for future in futures:
            doi = futures[future]
            try:
                results[doi] = future.result()
            except Exception:
                results[doi] = None
    return results


def _apply_bridge_result_routing(
    result: dict,
    collection_key: str | None,
    tags: list[str] | None,
) -> dict:
    """Apply collection/tag routing after a bridge save result.

    Reads `_get_config` / `_get_writer` / `time.sleep` lazily from the package
    namespace so tests patching `zotpilot.tools.ingestion.*` see them.
    """
    from . import _get_config as _pkg_get_config  # type: ignore[attr-defined]
    from . import _get_writer as _pkg_get_writer  # type: ignore[attr-defined]
    from . import time as _pkg_time  # type: ignore[attr-defined]

    return ingestion_bridge.apply_bridge_result_routing(
        result,
        collection_key,
        tags,
        get_config=_pkg_get_config,
        get_writer=_pkg_get_writer,
        discover_saved_item_key_fn=lambda title, url, known_key, writer, window_s=(
            ingestion_bridge.ITEM_DISCOVERY_WINDOW_S
        ): ingestion_bridge.discover_saved_item_key(
            title, url, known_key, writer, window_s=window_s, logger=logger
        ),
        apply_collection_tag_routing_fn=lambda item_key, routed_collection_key, routed_tags, writer: (
            ingestion_bridge.apply_collection_tag_routing(
                item_key, routed_collection_key, routed_tags, writer, get_config=_pkg_get_config
            )
        ),
        writer_lock=_writer_lock,
        sleep_fn=_pkg_time.sleep,
        logger=logger,
    )


def ingest_papers(
    papers: Annotated[
        list[dict] | str,
        Field(
            description=(
                "JSON array of paper dicts, each with at least one of: doi, arxiv_id, landing_page_url. "
                "Typically from search_academic_databases results. Max 50 per call."
            )
        ),
    ],
    collection_key: Annotated[
        str | None,
        Field(description="Zotero collection key for all ingested papers. Defaults to INBOX."),
    ] = None,
) -> dict:
    """Start async batch ingestion of papers to Zotero via ZotPilot Connector.

    Returns immediately after validation and duplicate checking. Papers that need
    saving are processed in the background. Use get_ingest_status(batch_id) to
    track progress. When is_final is true, all papers have been processed."""
    # Lazy imports via package namespace so test patches on zotpilot.tools.ingestion.X are honoured.
    from . import BridgeServer as _pkg_BridgeServer  # type: ignore[attr-defined]
    from . import _coerce_json_list as _pkg_coerce_json_list  # type: ignore[attr-defined]
    from . import _ensure_inbox_collection as _pkg_ensure_inbox_collection  # type: ignore[attr-defined]
    from . import _get_config as _pkg_get_config  # type: ignore[attr-defined]
    from . import _get_writer as _pkg_get_writer  # type: ignore[attr-defined]
    from . import _is_pdf_or_doi_url as _pkg_is_pdf_or_doi_url  # type: ignore[attr-defined]
    from . import _lookup_local_item_key_by_doi as _pkg_lookup_doi  # type: ignore[attr-defined]
    from . import _resolve_dois_concurrent as _pkg_resolve_dois  # type: ignore[attr-defined]
    from . import _run_save_worker as _pkg_run_save_worker  # type: ignore[attr-defined]
    from . import _writer_lock as _pkg_writer_lock  # type: ignore[attr-defined]
    from . import classify_ingest_candidate as _pkg_classify  # type: ignore[attr-defined]
    from . import time as _pkg_time  # type: ignore[attr-defined]
    papers = _pkg_coerce_json_list(papers, "papers")
    if not isinstance(papers, list):
        raise ToolError("papers must be a JSON array of paper dicts")
    if len(papers) > 50:
        raise ToolError(f"Batch size {len(papers)} exceeds maximum of 50. Split into smaller batches.")

    if collection_key is None:
        collection_key = _pkg_ensure_inbox_collection()
    resolved_collection_key = collection_key

    results: list[dict] = []
    connector_candidates: list[dict] = []
    api_candidates: list[dict] = []
    saved = 0
    duplicates = 0
    failed = 0
    skipped_indices: set[int] = set()
    batch_seen_dois: dict[str, int] = {}

    for idx, paper in enumerate(papers):
        arxiv_id = paper.get("arxiv_id")
        landing_page_url = paper.get("landing_page_url")
        doi = paper.get("doi")

        normalized_doi = ingestion_search.normalize_doi(doi)
        arxiv_doi = ingestion_search.normalize_doi(f"10.48550/arxiv.{arxiv_id}") if arxiv_id else None
        if not normalized_doi:
            normalized_doi = arxiv_doi

        if normalized_doi and normalized_doi in batch_seen_dois:
            first_idx = batch_seen_dois[normalized_doi]
            logger.warning("Skipping batch item %d: duplicate DOI %s (first at %d)", idx, normalized_doi, first_idx)
            skipped_indices.add(idx)
            duplicates += 1
            results.append({
                "url": landing_page_url or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None),
                "status": "duplicate_in_batch",
                "title": paper.get("title"),
                "error": f"Duplicate of item {first_idx} in this batch",
            })
            continue
        if normalized_doi:
            batch_seen_dois[normalized_doi] = idx

        existing_item_key = _pkg_lookup_doi(normalized_doi) or (
            _pkg_lookup_doi(arxiv_doi) if arxiv_doi and arxiv_doi != normalized_doi else None
        )
        if existing_item_key:
            skipped_indices.add(idx)
            duplicates += 1
            if resolved_collection_key:
                try:
                    writer = _pkg_get_writer()
                    with _pkg_writer_lock:
                        writer.add_to_collection(existing_item_key, resolved_collection_key)
                except Exception as exc:
                    results.append({
                        "url": landing_page_url or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None),
                        "status": "duplicate",
                        "item_key": existing_item_key,
                        "title": paper.get("title"),
                        "warning": f"collection routing failed: {exc}",
                    })
                    continue
            results.append({
                "url": landing_page_url or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None),
                "status": "duplicate",
                "item_key": existing_item_key,
                "title": paper.get("title"),
            })
            continue

    # DOI resolution pass
    dois_to_resolve: list[str] = []
    seen_dois: set[str] = set()
    for idx, paper in enumerate(papers):
        if idx in skipped_indices:
            continue
        normalized_doi = ingestion_search.normalize_doi(paper.get("doi"))
        if (
            normalized_doi
            and not paper.get("arxiv_id")
            and (not paper.get("landing_page_url") or _pkg_is_pdf_or_doi_url(paper.get("landing_page_url")))
            and normalized_doi not in seen_dois
        ):
            seen_dois.add(normalized_doi)
            dois_to_resolve.append(normalized_doi)

    resolved_dois = _pkg_resolve_dois(dois_to_resolve)
    for idx, paper in enumerate(papers):
        if idx in skipped_indices:
            continue
        normalized_doi = ingestion_search.normalize_doi(paper.get("doi"))
        if normalized_doi in resolved_dois:
            paper["_resolved_landing_url"] = resolved_dois[normalized_doi]

    # Routing pass
    for idx, paper in enumerate(papers):
        if idx in skipped_indices:
            continue
        arxiv_id = paper.get("arxiv_id")
        landing_page_url = paper.get("landing_page_url")
        doi = paper.get("doi")

        normalized_doi = ingestion_search.normalize_doi(doi)
        arxiv_doi = ingestion_search.normalize_doi(f"10.48550/arxiv.{arxiv_id}") if arxiv_id else None
        if not normalized_doi:
            normalized_doi = arxiv_doi

        routing = _pkg_classify(paper, normalized_doi, arxiv_id, landing_page_url)

        if routing == "reject":
            failed += 1
            results.append({"url": landing_page_url or None, "status": "failed", "error": "no usable identifier"})
            continue

        if routing == "api":
            api_candidates.append({"paper": paper, "url": None, "_index": idx, "ingest_method": "api"})
            continue

        # connector
        if arxiv_id:
            url: str | None = f"https://arxiv.org/abs/{arxiv_id}"
        else:
            url = str(paper.get("_resolved_landing_url") or landing_page_url or "") or None
        connector_candidates.append({"paper": paper, "url": url, "_index": idx, "ingest_method": "connector"})

    # Bridge + extension availability check
    if connector_candidates:
        bridge_running = _pkg_BridgeServer.is_running(DEFAULT_PORT)
        if not bridge_running:
            try:
                _pkg_BridgeServer.auto_start(DEFAULT_PORT)
                bridge_running = True
            except Exception:
                bridge_running = False

        extension_connected = False
        last_seen_s: float | None = None
        if bridge_running:
            ext_status = ingestion_bridge.wait_for_extension(f"http://127.0.0.1:{DEFAULT_PORT}")
            extension_connected = bool(ext_status.get("extension_connected"))
            last_seen_s = ext_status.get("extension_last_seen_s")

        if not extension_connected:
            if not bridge_running:
                detail = "ZotPilot bridge could not be started. Run 'zotpilot bridge' manually or check the logs."
            elif last_seen_s is not None:
                detail = (
                    f"ZotPilot Connector last sent a heartbeat {last_seen_s:.0f}s ago "
                    "(stale). Make sure Chrome is open and the ZotPilot Connector "
                    "extension is enabled, then retry ingest_papers."
                )
            else:
                detail = (
                    "ZotPilot Connector has not connected to the bridge. "
                    "Make sure Chrome is open and the ZotPilot Connector extension "
                    "is installed and enabled, then retry ingest_papers."
                )
            for candidate in connector_candidates:
                failed += 1
                results.append({
                    "url": candidate["url"],
                    "paper_title": candidate["paper"].get("title"),
                    "status": "failed",
                    "error_code": "connector_offline",
                    "error": detail,
                })
            return {
                "batch_id": None,
                "is_final": True,
                "total": len(papers),
                "saved": saved,
                "duplicates": duplicates,
                "failed": failed,
                "results": results,
                "error_code": "connector_offline",
                "error": detail,
                "remediation": (
                    "1) Open Chrome. 2) Confirm the ZotPilot Connector extension "
                    "icon is active (click it if needed). 3) Wait ~10s for the "
                    "heartbeat to re-establish. 4) Retry ingest_papers with the same inputs."
                ),
            }

    # Preflight
    urls_to_save = [candidate["url"] for candidate in connector_candidates]
    if urls_to_save and _pkg_get_config().preflight_enabled:
        preflight_report = ingestion_bridge.preflight_urls(
            urls_to_save,
            sample_size=5,
            default_port=DEFAULT_PORT,
            bridge_server_cls=_pkg_BridgeServer,
            logger=logger,
            sleep_fn=_pkg_time.sleep,
            monotonic_fn=_pkg_time.monotonic,
        )
        if not preflight_report.get("all_clear", False):
            blocked_domains: set[str] = set()
            for blocked in preflight_report.get("blocked", []):
                failed += 1
                blocked_url = blocked.get("url") or ""
                blocked_domains.add(ingestion_bridge.extract_publisher_domain(blocked_url))
                results.append({
                    "url": blocked_url,
                    "status": "failed",
                    "error_code": blocked.get("error_code") or "anti_bot_detected",
                    "error": (
                        blocked.get("error")
                        or "Anti-bot protection detected. "
                        "Please complete browser verification in Chrome, then retry. "
                        "DO NOT retry with save_urls or DOI links — "
                        "you'll hit the same wall and produce a partial-success batch."
                    ),
                })
            for error in preflight_report.get("errors", []):
                failed += 1
                error_url = error.get("url") or ""
                blocked_domains.add(ingestion_bridge.extract_publisher_domain(error_url))
                results.append({
                    "url": error_url,
                    "status": "failed",
                    "error_code": error.get("error_code") or "preflight_failed",
                    "error": error.get("error") or "preflight failed",
                })

            remaining_candidates = [
                c for c in connector_candidates
                if ingestion_bridge.extract_publisher_domain(c["url"]) not in blocked_domains
            ]
            dropped_count = len(connector_candidates) - len(remaining_candidates)
            if dropped_count:
                logger.info(
                    "Preflight: dropped %d connector candidate(s) from %d blocked domain(s); "
                    "%d remain.",
                    dropped_count, len(blocked_domains), len(remaining_candidates),
                )
            connector_candidates = remaining_candidates

            if not connector_candidates and not api_candidates:
                from ..ingest_state import BlockingDecision
                preflight_decision = BlockingDecision(
                    decision_id="preflight_blocked",
                    batch_id=None,
                    item_keys=tuple(),
                    description=(
                        "Preflight detected anti-bot protection (CAPTCHA / Cloudflare / login). "
                        "User must complete browser verification in Chrome, then retry the SAME "
                        "ingest_papers call with identical inputs. "
                        "DO NOT retry with save_urls or DOI links — same wall, worse state."
                    ),
                )
                return {
                    "batch_id": None,
                    "state": "failed",
                    "is_final": True,
                    "total": len(papers),
                    "saved": saved,
                    "duplicates": duplicates,
                    "failed": failed,
                    "pending_count": 0,
                    "collection_used": resolved_collection_key,
                    "results": results,
                    "blocked": preflight_report.get("blocked", []),
                    "errors": preflight_report.get("errors", []),
                    "pending_items": [],
                    "blocking_decisions": [
                        {
                            "decision_id": preflight_decision.decision_id,
                            "batch_id": preflight_decision.batch_id,
                            "item_keys": list(preflight_decision.item_keys),
                            "description": preflight_decision.description,
                            "resolved": preflight_decision.resolved,
                        }
                    ],
                }

    # Build batch state
    pending_items = [
        IngestItemState(
            index=c["_index"],
            url=c["url"],
            title=c["paper"].get("title"),
            ingest_method=c.get("ingest_method"),
        )
        for c in connector_candidates + api_candidates
    ]
    batch = BatchState(
        total=len(papers),
        collection_used=resolved_collection_key,
        pending_items=pending_items,
        session_id=None,
    )

    if not connector_candidates and not api_candidates:
        batch.state = "completed" if failed == 0 else "completed_with_errors"
        batch.is_final = True
        batch.finalized_at = time.monotonic()
    else:
        _batch_store.put(batch)
        _executor.submit(_pkg_run_save_worker, batch, connector_candidates, api_candidates, resolved_collection_key)

    saved_with_pdf = sum(1 for it in pending_items if it.has_pdf is True)
    saved_metadata_only = sum(1 for it in pending_items if it.has_pdf is False)
    return {
        "batch_id": batch.batch_id,
        "is_final": batch.is_final,
        "total": len(papers),
        "saved": saved,
        "saved_with_pdf": saved_with_pdf,
        "saved_metadata_only": saved_metadata_only,
        "duplicates": duplicates,
        "failed": failed,
        "pending_count": len(connector_candidates) + len(api_candidates),
        "collection_used": resolved_collection_key,
        "results": results,
        "pending_items": [it.to_dict() for it in pending_items],
    }


def get_ingest_status(
    batch_id: Annotated[str, Field(description="Batch ID returned by ingest_papers")],
) -> dict:
    """Check progress of an async paper ingestion batch. When state is 'completed'
    or 'completed_with_errors', is_final will be true and results contain item_keys."""
    batch = _batch_store.get(batch_id)
    if batch is None:
        return {
            "batch_id": batch_id,
            "state": "not_found",
            "is_final": True,
            "error": (
                "Batch not found. It may have expired (TTL 30min after completion) "
                "or the server was restarted. Check Zotero directly."
            ),
        }
    _update_session_after_ingest(batch)
    return batch.full_status()


@mcp.tool(tags=tool_tags("core", "ingestion"))
def search_academic_databases(
    query: Annotated[str, Field(description="Search query for academic papers")],
    limit: Annotated[int, Field(ge=1, le=100, description="Number of results (1-100)")] = 20,
    year_min: Annotated[int | None, Field(description="Earliest publication year filter")] = None,
    year_max: Annotated[int | None, Field(description="Latest publication year filter")] = None,
    high_quality: Annotated[
        bool,
        Field(description="Filter retracted/non-articles/no-DOI; require cited_by_count>10"),
    ] = True,
    sort_by: Annotated[
        Literal["relevance", "citationCount", "publicationDate"],
        Field(description="Sort order: relevance (default), citationCount, or publicationDate"),
    ] = "relevance",
) -> list[dict]:
    """Search external academic databases for papers on a topic. Use this as the first step
    for any literature survey, research discovery, or "帮我调研 X" request — it finds papers
    NOT yet in the local library. Does NOT add to Zotero automatically; call ingest_papers
    with selected results to add them.

    Uses OpenAlex only.
    Supports "author:Name" prefix for author-scoped search and DOI strings for exact lookup."""
    # Lazy import so test patches on zotpilot.tools.ingestion._get_config are honoured.
    from . import _get_config as _pkg_get_config  # type: ignore[attr-defined]
    from . import httpx as _httpx
    return ingestion_search.search_academic_databases_impl(
        _pkg_get_config(),
        query,
        limit,
        year_min,
        year_max,
        sort_by,
        high_quality,
        httpx_module=_httpx,
        tool_error_cls=ToolError,
        logger=logger,
    )
