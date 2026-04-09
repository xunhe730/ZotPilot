"""Background execution for the batch-oriented research workflow."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..state import _get_zotero
from .batch import REINDEX_ELIGIBLE_REASONS, Batch, Item, LibraryMismatchError
from .batch_store import BatchStore

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="research-workflow")
_active_jobs: dict[str, str] = {}
_jobs_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Tool-layer callables — injected at startup by research_workflow.py so that
# the workflow core layer never imports from tools.* directly (P11).
# ---------------------------------------------------------------------------
_ingest_papers_impl: Callable[..., dict[str, Any]] | None = None
_get_ingest_status_impl: Callable[..., dict[str, Any]] | None = None
_index_library_impl: Callable[..., dict[str, Any]] | None = None


def register_tool_callables(
    *,
    ingest_papers_impl: Callable[..., dict[str, Any]],
    get_ingest_status_impl: Callable[..., dict[str, Any]],
    index_library_impl: Callable[..., dict[str, Any]],
) -> None:
    """Called once by the MCP adapter layer to inject tool implementations."""
    global _ingest_papers_impl, _get_ingest_status_impl, _index_library_impl
    _ingest_papers_impl = ingest_papers_impl
    _get_ingest_status_impl = get_ingest_status_impl
    _index_library_impl = index_library_impl


def _get_ingest_impl() -> Callable[..., dict[str, Any]]:
    if _ingest_papers_impl is None:
        raise RuntimeError(
            "ingest_papers_impl not registered. Call register_tool_callables() at startup."
        )
    return _ingest_papers_impl


def _get_status_impl() -> Callable[..., dict[str, Any]]:
    if _get_ingest_status_impl is None:
        raise RuntimeError(
            "get_ingest_status_impl not registered. Call register_tool_callables() at startup."
        )
    return _get_ingest_status_impl


def _get_index_impl() -> Callable[..., dict[str, Any]]:
    if _index_library_impl is None:
        raise RuntimeError(
            "index_library_impl not registered. Call register_tool_callables() at startup."
        )
    return _index_library_impl
def _ensure_library_binding(batch: Batch) -> None:
    current_library_id = str(_get_zotero().library_id)
    if current_library_id != batch.library_id:
        raise LibraryMismatchError(
            f"Active library {current_library_id!r} does not match batch library {batch.library_id!r}"
        )


def assert_library_binding(batch: Batch, current_library_id: str | None = None) -> None:
    """Public alias used by tool-layer guards."""
    if current_library_id is None:
        _ensure_library_binding(batch)
        return
    if str(current_library_id) != batch.library_id:
        raise LibraryMismatchError(
            f"Active library {current_library_id!r} does not match batch library {batch.library_id!r}"
        )


def _mark_job(batch_id: str, job_type: str) -> bool:
    with _jobs_lock:
        if batch_id in _active_jobs:
            return False
        _active_jobs[batch_id] = job_type
        return True


def _clear_job(batch_id: str) -> None:
    with _jobs_lock:
        _active_jobs.pop(batch_id, None)


def start_ingest_worker(store: BatchStore, batch_id: str) -> None:
    if _mark_job(batch_id, "ingest"):
        _executor.submit(_run_ingest_worker, store, batch_id)


def start_post_process_worker(store: BatchStore, batch_id: str) -> None:
    if _mark_job(batch_id, "post_process"):
        _executor.submit(_run_post_process_worker, store, batch_id)


def _translate_engine_item(item: Item, result: dict) -> Item:
    status = result.get("status")
    item_key = result.get("item_key")
    has_pdf = result.get("has_pdf")
    error = result.get("error") or ""
    if status in {"duplicate", "duplicate_in_batch"}:
        return item.with_updates(status="duplicate", zotero_item_key=item_key or item.zotero_item_key)
    if status == "saved":
        if has_pdf is False:
            degrade_reasons = tuple(dict.fromkeys((*item.degradation_reasons, "no_pdf")))
            return item.with_updates(
                status="degraded",
                pdf_present=False,
                metadata_complete=True,
                zotero_item_key=item_key or item.zotero_item_key,
                routing_method=result.get("ingest_method") or item.routing_method,
                degradation_reasons=degrade_reasons,
            )
        return item.with_updates(
            status="saved",
            pdf_present=True if has_pdf is True else item.pdf_present,
            metadata_complete=True,
            zotero_item_key=item_key or item.zotero_item_key,
            routing_method=result.get("ingest_method") or item.routing_method,
        )
    fail_reasons: list[str] = []
    if "anti_bot" in error:
        fail_reasons.append("anti_bot_blocked")
    elif "metadata" in error:
        fail_reasons.append("incomplete_metadata")
    elif "timeout" in error:
        fail_reasons.append("connector_timeout")
    return item.with_updates(
        status="failed",
        pdf_present=False if has_pdf is False else item.pdf_present,
        metadata_complete=False,
        zotero_item_key=item_key or item.zotero_item_key,
        routing_method=result.get("ingest_method") or item.routing_method,
        degradation_reasons=tuple(dict.fromkeys((*item.degradation_reasons, *fail_reasons))),
    )


def _run_ingest_worker(store: BatchStore, batch_id: str) -> None:
    try:
        batch = store.load(batch_id)
        if batch is None:
            return
        _ensure_library_binding(batch)
        batch.assert_phase("approved")
        batch = store.save(batch.transition_to("ingesting"))

        ingest_fn = _get_ingest_impl()
        status_fn = _get_status_impl()
        result = ingest_fn([dict(item.paper_payload) for item in batch.items])
        if not result.get("is_final") and result.get("batch_id"):
            engine_batch_id = result["batch_id"]
            batch = store.save(batch.mark_engine_batch(engine_batch_id))
            while True:
                status = status_fn(engine_batch_id)
                if status.get("is_final"):
                    result = status
                    break
                time.sleep(0.2)

        translated_items = []
        for item, engine_item in zip(batch.items, result.get("results", []), strict=False):
            translated_items.append(_translate_engine_item(item, engine_item))
        if len(translated_items) < len(batch.items):
            translated_items.extend(batch.items[len(translated_items):])

        batch = batch.with_items(translated_items).transition_to("post_ingest_verified")
        store.save(batch)
    except Exception:
        logger.exception("ingest worker failed for %s", batch_id)
        batch = store.load(batch_id)
        if batch is not None and batch.phase != "aborted":
            try:
                store.save(batch.transition_to("aborted"))
            except Exception:
                logger.exception("failed to abort batch %s after ingest worker error", batch_id)
    finally:
        _clear_job(batch_id)


def _index_item(item: Item) -> Item:
    if not item.zotero_item_key:
        return item.with_updates(
            status="degraded",
            degradation_reasons=tuple(dict.fromkeys((*item.degradation_reasons, "index_write_failed"))),
        )
    try:
        index_fn = _get_index_impl()
        result = index_fn(item_key=item.zotero_item_key, batch_size=0)
        has_more = result.get("has_more")
        while has_more:
            result = index_fn(item_key=item.zotero_item_key, batch_size=0)
            has_more = result.get("has_more")
        return item.with_updates(
            indexed=True,
            tagged=True,
            classified=True,
            status="saved" if item.status == "degraded" else item.status,
        )
    except Exception:
        logger.exception("index failed for %s", item.zotero_item_key)
        reasons = tuple(dict.fromkeys((*item.degradation_reasons, "embedding_api_unavailable")))
        return item.with_updates(
            status="degraded",
            indexed=False,
            degradation_reasons=reasons,
        )


def _run_post_process_worker(store: BatchStore, batch_id: str) -> None:
    try:
        batch = store.load(batch_id)
        if batch is None:
            return
        _ensure_library_binding(batch)
        if batch.phase == "taxonomy_authorized":
            batch = store.save(batch.transition_to("post_processing"))
        else:
            batch.assert_phase("post_ingest_approved")
            batch = store.save(batch.transition_to("post_processing"))

        processed = []
        for item in batch.items:
            if item.status in {"saved", "degraded"}:
                processed.append(_index_item(item))
            else:
                processed.append(item)
        batch = batch.with_items(processed).transition_to("post_process_verified")
        store.save(batch)
    except Exception:
        logger.exception("post-process worker failed for %s", batch_id)
        batch = store.load(batch_id)
        if batch is not None and batch.phase != "aborted":
            try:
                store.save(batch.transition_to("aborted"))
            except Exception:
                logger.exception("failed to abort batch %s after post-process error", batch_id)
    finally:
        _clear_job(batch_id)


def reindex_items(store: BatchStore, batch_id: str, item_keys: list[str]) -> tuple[Batch, list[str], list[str]]:
    batch = store.load(batch_id)
    if batch is None:
        raise KeyError(batch_id)
    _ensure_library_binding(batch)
    batch.assert_phase("done")

    item_key_set = set(item_keys)
    if not item_key_set:
        return batch, [], []

    items = []
    reindexed: list[str] = []
    still_degraded: list[str] = []
    for item in batch.items:
        if item.zotero_item_key not in item_key_set:
            items.append(item)
            continue
        if item.status != "degraded" or not any(r in REINDEX_ELIGIBLE_REASONS for r in item.degradation_reasons):
            raise ValueError(
                f"Item {item.zotero_item_key} is not reindex-eligible: {item.degradation_reasons!r}"
            )
        updated = _index_item(item)
        items.append(updated)
        if updated.status == "saved":
            reindexed.append(item.zotero_item_key or "")
        else:
            still_degraded.append(item.zotero_item_key or "")

    batch = store.save(batch.with_items(items))
    return batch, reindexed, still_degraded
