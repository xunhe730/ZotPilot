"""Batch-centric research workflow MCP tools."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Annotated, Any, Literal

from pydantic import Field

from ..state import _get_config, _get_zotero, mcp, register_reset_callback
from ..workflow import (
    Batch,
    BatchStore,
    BlockingDecision,
    IllegalPhaseTransition,
    InvalidPhaseError,
    Item,
    Phase,
    PreflightResult,
    new_batch,
)
from ..workflow.worker import (
    register_tool_callables,
    reindex_items,
    start_ingest_worker,
    start_post_process_worker,
)
from . import ingestion, ingestion_bridge, ingestion_search
from .indexing import index_library as _index_library
from .profiles import tool_tags

logger = logging.getLogger(__name__)
_batch_store = BatchStore()

# Register tool-layer callables into the workflow core so worker.py never
# needs to import from tools.* directly (P11 layer-dependency rule).
register_tool_callables(
    ingest_papers_impl=ingestion.ingest_papers_impl,
    get_ingest_status_impl=ingestion.get_ingest_status_impl,
    index_library_impl=_index_library,
)


def _clear_batch_store() -> None:
    for batch in _batch_store.list_active():
        # Keep persisted files; reset only the in-flight executor state.
        logger.debug("Active batch retained across reset: %s", batch.batch_id)


register_reset_callback(_clear_batch_store)


def _library_id() -> str:
    return str(_get_zotero().library_id)


def _normalize_identifier(value: str) -> str:
    normalized_doi = ingestion_search.normalize_doi(value)
    if normalized_doi:
        return normalized_doi
    return value.strip()


def _paper_from_identifier(identifier: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(identifier, dict):
        return dict(identifier)

    value = identifier.strip()
    if value.startswith(("http://", "https://")):
        return {"landing_page_url": value, "title": value}
    lowered = value.lower()
    if lowered.startswith("10.") or lowered.startswith("doi:"):
        return {"doi": value}
    if "/" not in value and " " not in value:
        return {"arxiv_id": value}
    return {"title": value, "landing_page_url": value}


def _item_from_paper(paper: dict[str, Any], index: int) -> Item:
    identifier = (
        paper.get("doi")
        or paper.get("arxiv_id")
        or paper.get("landing_page_url")
        or paper.get("oa_url")
        or paper.get("openalex_id")
        or f"candidate-{index}"
    )
    doc_id = (
        paper.get("openalex_id")
        or ingestion_search.normalize_doi(paper.get("doi"))
        or paper.get("arxiv_id")
        or paper.get("landing_page_url")
        or f"candidate-{index}"
    )
    source_url = paper.get("landing_page_url") or paper.get("oa_url") or ""
    return Item(
        identifier=str(identifier),
        doc_id=str(doc_id),
        source_url=str(source_url),
        title=paper.get("title"),
        paper_payload=dict(paper),
        route_selected="connector_primary" if source_url else "api_primary",
    )


def _batch_summary(batch: Batch) -> dict[str, int]:
    saved = sum(1 for item in batch.items if item.status == "saved")
    degraded = sum(1 for item in batch.items if item.status == "degraded")
    failed = sum(1 for item in batch.items if item.status == "failed")
    duplicate = sum(1 for item in batch.items if item.status == "duplicate")
    pdf_present_count = sum(
        1
        for item in batch.items
        if item.pdf_verification_status == "present" or item.pdf_present is True
    )
    pdf_pending_count = sum(1 for item in batch.items if item.pdf_verification_status == "pending")
    return {
        "total": len(batch.items),
        "saved": saved,
        "degraded": degraded,
        "failed": failed,
        "duplicate": duplicate,
        "pdf_present_count": pdf_present_count,
        "pdf_pending_count": pdf_pending_count,
    }


def _serialize_batch(batch: Batch) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "batch_id": batch.batch_id,
        "phase": batch.phase,
        "library_id": batch.library_id,
        "items": [item.to_dict() for item in batch.items],
        "summary": _batch_summary(batch),
        "blocking_decisions": [decision.to_dict() for decision in batch.blocking_decisions],
        "next_action": batch.next_action_payload(),
    }
    if batch.preflight_result is not None:
        payload["preflight_result"] = batch.preflight_result.to_dict()
    if batch.pending_taxonomy_tags or batch.pending_taxonomy_collections:
        payload["pending_taxonomy"] = {
            "new_tags": list(batch.pending_taxonomy_tags),
            "new_collections": list(batch.pending_taxonomy_collections),
        }
    if batch.final_report:
        payload["final_report"] = batch.final_report
    if batch.phase == "done":
        payload["reindex_eligible"] = batch.final_report.get("reindex_eligible", [])
    return payload


def _save(batch: Batch) -> Batch:
    return _batch_store.save(batch)


def _build_preflight_result(batch: Batch, *, round_number: int) -> tuple[Batch, dict[str, Any]]:
    items = list(batch.items)
    urls_to_save: list[str] = []
    url_to_doc_id: dict[str, str] = {}

    for idx, item in enumerate(items):
        paper = item.paper_payload
        normalized_doi = ingestion_search.normalize_doi(paper.get("doi"))
        arxiv_id = paper.get("arxiv_id")
        landing_page_url = paper.get("landing_page_url")
        routing = ingestion.classify_ingest_candidate(paper, normalized_doi, arxiv_id, landing_page_url)
        updated = item.with_updates(routing_method=None if routing == "reject" else routing)
        items[idx] = updated
        if routing != "connector":
            continue
        url = landing_page_url or paper.get("_resolved_landing_url") or item.source_url
        if arxiv_id:
            url = f"https://arxiv.org/abs/{arxiv_id}"
        if not url:
            continue
        urls_to_save.append(str(url))
        url_to_doc_id[str(url)] = updated.doc_id

    report: dict[str, Any] = {"all_clear": True, "blocked": [], "errors": []}
    if urls_to_save and _get_config().preflight_enabled:
        report = ingestion_bridge.preflight_urls(
            urls_to_save,
            sample_size=5,
            default_port=ingestion.DEFAULT_PORT,
            bridge_server_cls=ingestion.BridgeServer,
            logger=ingestion.logger,
            sleep_fn=ingestion.time.sleep,
            monotonic_fn=ingestion.time.monotonic,
        )

    decisions: list[BlockingDecision] = []
    for blocked in report.get("blocked", []):
        url = str(blocked.get("url") or "")
        doc_id = url_to_doc_id.get(url)
        decisions.append(
            BlockingDecision(
                decision_id="preflight_blocked",
                item_keys=tuple([doc_id] if doc_id else ()),
                description=(
                    "Preflight detected anti-bot protection. Complete browser verification "
                    "in Chrome, then call resolve_preflight."
                ),
                payload={"url": url, "kind": blocked.get("kind")},
            )
        )
    for error in report.get("errors", []):
        url = str(error.get("url") or "")
        doc_id = url_to_doc_id.get(url)
        decisions.append(
            BlockingDecision(
                decision_id="preflight_error",
                item_keys=tuple([doc_id] if doc_id else ()),
                description="Preflight failed for this URL. Resolve the publisher page, then retry.",
                payload={"url": url, "error": error.get("error")},
            )
        )

    result = PreflightResult(
        round=round_number,
        checked_at=ingestion.time.time(),
        blocking_decisions=tuple(decisions),
        all_clear=bool(report.get("all_clear", False)),
    )
    batch = batch.with_items(tuple(items)).with_preflight_result(result)
    batch = batch.transition_to("preflighting") if batch.phase == "candidates_confirmed" else batch
    if not result.all_clear:
        batch = batch.transition_to("preflight_blocked")
    return batch, report


def _apply_legacy_ingest_result(
    batch: Batch,
    response: dict[str, Any],
    *,
    phase: Phase | None = "post_ingest_verified",
) -> Batch:
    results = response.get("results", []) or []
    mapped_items = list(batch.items)
    doc_id_by_engine = dict(batch.engine_index_map)
    if not doc_id_by_engine:
        doc_id_by_engine = {str(index): item.doc_id for index, item in enumerate(batch.items)}

    for row in results:
        index = row.get("index")
        if index is None:
            continue
        doc_id = doc_id_by_engine.get(str(index))
        if not doc_id:
            continue
        item = batch.find_item(doc_id)
        if item is None:
            continue
        legacy_status = row.get("status")
        new_status = "pending"
        degradation_reasons = list(item.degradation_reasons)
        pdf_present = row.get("has_pdf")
        if legacy_status in {"duplicate", "duplicate_in_batch"}:
            new_status = "duplicate"
        elif legacy_status == "failed":
            new_status = "failed"
        elif legacy_status == "saved":
            if pdf_present is False:
                new_status = "degraded"
                degradation_reasons.append("no_pdf")
            else:
                new_status = "saved"
        refreshed_reason_code = row.get("reason_code") if "reason_code" in row else item.reason_code
        if row.get("pdf_verification_status") == "present" or pdf_present is True:
            refreshed_reason_code = None
        mapped = item.with_updates(
            status=new_status,  # type: ignore[arg-type]
            zotero_item_key=row.get("item_key"),
            pdf_present=pdf_present,
            pdf_verification_status=row.get("pdf_verification_status"),
            metadata_complete=row.get("item_key") is not None,
            routing_method=row.get("ingest_method") or item.routing_method,
            route_selected=row.get("route_selected") or item.route_selected,
            save_method_used=row.get("save_method_used") or item.save_method_used,
            item_discovery_status=row.get("item_discovery_status") or item.item_discovery_status,
            reason_code=refreshed_reason_code,
            suspected_duplicate_keys=tuple(row.get("suspected_duplicate_keys") or item.suspected_duplicate_keys),
            degradation_reasons=tuple(dict.fromkeys(degradation_reasons)),
        )
        mapped_items = [
            mapped if existing.doc_id == item.doc_id else existing
            for existing in mapped_items
        ]

    final_report = {
        "total": len(mapped_items),
        "saved": sum(1 for item in mapped_items if item.status == "saved"),
        "degraded": sum(1 for item in mapped_items if item.status == "degraded"),
        "failed": sum(1 for item in mapped_items if item.status == "failed"),
    }
    updated = replace(
        batch.with_items(tuple(mapped_items)),
        phase=phase or batch.phase,
        legacy_ingest_batch_id=batch.legacy_ingest_batch_id,
        engine_index_map=batch.engine_index_map,
        final_report=final_report,
    )
    return _save(updated)


def _build_post_process_report(batch: Batch) -> dict[str, Any]:
    item_reports: list[dict[str, Any]] = []
    full_success = 0
    partial = 0
    for item in batch.items:
        missing_steps: list[str] = []
        if item.status in {"saved", "degraded"}:
            if not item.indexed:
                missing_steps.append("index")
            if not item.noted:
                missing_steps.append("note")
            if not item.classified:
                missing_steps.append("classify")
            if not item.tagged:
                missing_steps.append("tag")
        if (
            item.status == "saved"
            and not missing_steps
            and (item.pdf_present is True or item.pdf_verification_status == "present")
        ):
            outcome = "full_success"
            full_success += 1
        elif item.status in {"saved", "degraded"}:
            outcome = "partial"
            partial += 1
        elif item.status == "duplicate":
            outcome = "skipped"
        else:
            outcome = "failure"
        item_reports.append({
            "doc_id": item.doc_id,
            "item_key": item.zotero_item_key,
            "title": item.title,
            "status": item.status,
            "pdf_present": item.pdf_present,
            "indexed": item.indexed,
            "noted": item.noted,
            "classified": item.classified,
            "tagged": item.tagged,
            "outcome": outcome,
            "missing_steps": missing_steps,
        })

    return {
        **_batch_summary(batch),
        "noted_count": sum(1 for item in batch.items if item.noted),
        "classified_count": sum(1 for item in batch.items if item.classified),
        "tagged_count": sum(1 for item in batch.items if item.tagged),
        "full_success_count": full_success,
        "partial_count": partial,
        "items": item_reports,
        "reindex_eligible": batch.reindex_eligible_item_keys(),
    }


def _tool_error(exc: Exception) -> Exception:
    from ..state import ToolError

    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, (IllegalPhaseTransition, InvalidPhaseError)):
        return ToolError(str(exc))
    return exc


@mcp.tool(tags=tool_tags("core", "research"))
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
) -> dict:
    results = ingestion.search_academic_databases_impl(
        query=query,
        limit=limit,
        year_min=year_min,
        year_max=year_max,
        high_quality=high_quality,
        sort_by=sort_by,
    )
    if not results:
        return {
            "batch_id": None,
            "candidates": [],
            "next_action": {
                "tool": "search_academic_databases",
                "args_hint": {"query": "<refine>"},
                "why": "No results. Try a broader or different query.",
                "blocks_on": "user",
            },
        }
    items = tuple(_item_from_paper(paper, index) for index, paper in enumerate(results))
    batch = new_batch(library_id=_library_id(), query=query, phase="candidate", items=items)
    _save(batch)
    return {
        "batch_id": batch.batch_id,
        "candidates": [item.to_dict() for item in batch.items],
        "next_action": batch.next_action_payload(),
    }


@mcp.tool(tags=tool_tags("core", "research"))
def ingest_by_identifiers(
    identifiers: Annotated[
        list[str | dict[str, Any]],
        Field(description="DOI / arXiv ID / URL / full paper dict list"),
    ],
) -> dict:
    papers = [_paper_from_identifier(identifier) for identifier in identifiers]
    items = tuple(_item_from_paper(paper, index) for index, paper in enumerate(papers))
    batch = new_batch(
        library_id=_library_id(),
        query="direct-ingest",
        phase="candidates_confirmed",
        items=items,
    )
    batch, report = _build_preflight_result(batch, round_number=1)
    _save(batch)
    return {
        "batch_id": batch.batch_id,
        "library_id": batch.library_id,
        "items": [item.to_dict() for item in batch.items],
        "preflight_result": batch.preflight_result.to_dict() if batch.preflight_result else None,
        "preflight_report": report,
        "next_action": batch.next_action_payload(),
    }


@mcp.tool(tags=tool_tags("core", "research"))
def confirm_candidates(
    batch_id: Annotated[str, Field(description="Batch returned by search_academic_databases")],
    selected_ids: Annotated[list[str], Field(description="Selected candidate doc_ids")],
) -> dict:
    batch = _batch_store.load(batch_id)
    if batch is None:
        from ..state import ToolError

        raise ToolError(f"Batch {batch_id!r} not found")
    batch.assert_phase("candidate")
    selected = [item for item in batch.items if item.doc_id in set(selected_ids)]
    if not selected:
        from ..state import ToolError

        raise ToolError("confirm_candidates requires at least one selected candidate")
    batch = batch.with_items(tuple(selected)).transition_to("candidates_confirmed")
    batch, report = _build_preflight_result(batch, round_number=1)
    _save(batch)
    return {
        "batch_id": batch.batch_id,
        "preflight_result": batch.preflight_result.to_dict() if batch.preflight_result else None,
        "preflight_report": report,
        "next_action": batch.next_action_payload(),
    }


@mcp.tool(tags=tool_tags("core", "research"))
def resolve_preflight(
    batch_id: Annotated[str, Field(description="Blocked batch waiting for browser verification")],
) -> dict:
    batch = _batch_store.load(batch_id)
    if batch is None:
        from ..state import ToolError

        raise ToolError(f"Batch {batch_id!r} not found")
    batch.assert_phase("preflight_blocked")
    reset = replace(batch, phase="preflighting")
    batch, report = _build_preflight_result(reset, round_number=2)
    _save(batch)
    return {
        "batch_id": batch.batch_id,
        "preflight_round_2_result": batch.preflight_result.to_dict() if batch.preflight_result else None,
        "preflight_report": report,
        "next_action": batch.next_action_payload(),
    }


@mcp.tool(tags=tool_tags("core", "research"))
def approve_ingest(
    batch_id: Annotated[str, Field(description="All-clear batch ready for ingest")],
) -> dict:
    batch = _batch_store.load(batch_id)
    if batch is None:
        from ..state import ToolError

        raise ToolError(f"Batch {batch_id!r} not found")
    batch.assert_phase("preflighting")
    if batch.preflight_result is None or not batch.preflight_result.all_clear:
        from ..state import ToolError

        raise ToolError("approve_ingest requires a preflight all-clear result")
    approved = _save(
        replace(
            batch.mark_approved(),
            engine_index_map={str(index): item.doc_id for index, item in enumerate(batch.items)},
        )
    )
    start_ingest_worker(_batch_store, approved.batch_id)
    return {
        "batch_id": approved.batch_id,
        "worker_started_at": approved.last_transition_at,
        "next_action": {
            "tool": "get_batch_status",
            "args_hint": {"batch_id": approved.batch_id},
            "why": "The ingest worker is running.",
            "blocks_on": "worker",
        },
    }


@mcp.tool(tags=tool_tags("core", "research"))
def get_batch_status(
    batch_id: Annotated[str, Field(description="Workflow batch id")],
) -> dict:
    batch = _batch_store.load(batch_id)
    if batch is None:
        from ..state import ToolError

        raise ToolError(f"Batch {batch_id!r} not found")
    if batch.legacy_ingest_batch_id and batch.phase not in {
        "candidate",
        "candidates_confirmed",
        "preflighting",
        "preflight_blocked",
        "approved",
        "post_process_verified",
        "done",
        "aborted",
    }:
        status = ingestion.get_ingest_status_impl(batch.legacy_ingest_batch_id)
        target_phase = (
            "post_ingest_verified"
            if batch.phase == "ingesting" and status.get("is_final") is True
            else batch.phase
        )
        batch = _apply_legacy_ingest_result(batch, status, phase=target_phase)
    return _serialize_batch(batch)


@mcp.tool(tags=tool_tags("core", "research"))
def approve_post_ingest(
    batch_id: Annotated[str, Field(description="Verified ingest batch")],
) -> dict:
    batch = _batch_store.load(batch_id)
    if batch is None:
        from ..state import ToolError

        raise ToolError(f"Batch {batch_id!r} not found")
    batch.assert_phase("post_ingest_verified")
    started = _save(batch.transition_to("post_ingest_approved"))
    start_post_process_worker(_batch_store, started.batch_id)
    return {
        "batch_id": started.batch_id,
        "next_action": {
            "tool": "get_batch_status",
            "args_hint": {"batch_id": started.batch_id},
            "why": "The post-process worker is running.",
            "blocks_on": "worker",
        },
    }


@mcp.tool(tags=tool_tags("core", "research"))
def authorize_taxonomy_changes(
    batch_id: Annotated[str, Field(description="Batch paused on taxonomy authorization")],
    authorized_new_tags: Annotated[list[str], Field(description="User-approved new tags")] = [],
    authorized_new_collections: Annotated[list[str], Field(description="User-approved new collections")] = [],
) -> dict:
    batch = _batch_store.load(batch_id)
    if batch is None:
        from ..state import ToolError

        raise ToolError(f"Batch {batch_id!r} not found")
    batch.assert_phase("AwaitingTaxonomyAuthorization")
    if not authorized_new_tags and not authorized_new_collections:
        skipped = replace(
            batch.transition_to("post_ingest_skipped"),
            pending_taxonomy_tags=(),
            pending_taxonomy_collections=(),
            final_report=_build_post_process_report(batch),
        ).transition_to("post_process_verified")
        _save(skipped)
        return _serialize_batch(skipped)
    authorized = batch.with_authorizations(
        new_tags=authorized_new_tags,
        new_collections=authorized_new_collections,
    ).transition_to("taxonomy_authorized")
    _save(authorized)
    start_post_process_worker(_batch_store, authorized.batch_id)
    return {
        "batch_id": authorized.batch_id,
        "next_action": {
            "tool": "get_batch_status",
            "args_hint": {"batch_id": authorized.batch_id},
            "why": "The post-process worker resumed after taxonomy authorization.",
            "blocks_on": "worker",
        },
    }


@mcp.tool(tags=tool_tags("core", "research"))
def approve_post_process(
    batch_id: Annotated[str, Field(description="Final verified batch")],
) -> dict:
    batch = _batch_store.load(batch_id)
    if batch is None:
        from ..state import ToolError

        raise ToolError(f"Batch {batch_id!r} not found")
    batch.assert_phase("post_process_verified")
    done = replace(
        batch.transition_to("done"),
        final_report=_build_post_process_report(batch),
    )
    _save(done)
    return {
        "batch_id": done.batch_id,
        "phase": "done",
        "final_report": done.final_report,
        "reindex_eligible": done.final_report.get("reindex_eligible", []),
        "next_action": None,
    }


@mcp.tool(tags=tool_tags("core", "research"))
def reindex_degraded(
    batch_id: Annotated[str, Field(description="Completed batch id")],
    item_keys: Annotated[list[str], Field(description="Degraded item_keys from the completed batch")],
    reason: Annotated[
        Literal["embedding_api_recovered", "manual_retry"],
        Field(description="Audit reason for the manual reindex"),
    ],
) -> dict:
    batch = _batch_store.load(batch_id)
    if batch is None:
        from ..state import ToolError

        raise ToolError(f"Batch {batch_id!r} not found")
    batch.assert_phase("done")
    try:
        updated_batch, reindexed, still_degraded = reindex_items(_batch_store, batch_id, item_keys)
    except (ValueError, KeyError) as exc:
        from ..state import ToolError

        raise ToolError(str(exc)) from exc
    updated_batch = _save(replace(updated_batch, final_report=_build_post_process_report(updated_batch)))
    return {
        "batch_id": updated_batch.batch_id,
        "reindexed": reindexed,
        "still_degraded": still_degraded,
        "reason": reason,
        "next_action": None,
    }
