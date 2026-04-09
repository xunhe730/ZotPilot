"""Async ingestion batch state tracking data structures."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal, cast

logger = logging.getLogger(__name__)


class _UnsetType:
    """Sentinel type for distinguishing 'not provided' from None."""


_UNSET = _UnsetType()


@dataclass
class IngestItemState:
    index: int
    url: str | None
    title: str | None = None
    status: Literal["pending", "saved", "duplicate", "duplicate_in_batch", "failed"] = "pending"
    item_key: str | None = None
    error: str | None = None
    warning: str | None = None
    routing_status: str | None = None
    ingest_method: str | None = None  # "connector" | "api" | None
    has_pdf: bool | None = None
    route_selected: str | None = None
    save_method_used: str | None = None
    item_discovery_status: str | None = None
    pdf_verification_status: str | None = None
    reason_code: str | None = None
    verification_attempts: int | None = None
    verification_deadline_at: float | None = None
    suspected_duplicate_keys: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """Return dict representation, omitting None-valued optional fields.

        Always includes: index, url, status.
        Omits if None: title, item_key, error, warning.
        """
        d: dict = {
            "index": self.index,
            "url": self.url,
            "status": self.status,
        }
        if self.title is not None:
            d["title"] = self.title
        if self.item_key is not None:
            d["item_key"] = self.item_key
        if self.error is not None:
            d["error"] = self.error
        if self.warning is not None:
            d["warning"] = self.warning
        if self.routing_status is not None:
            d["routing_status"] = self.routing_status
        if self.ingest_method is not None:
            d["ingest_method"] = self.ingest_method
        if self.has_pdf is not None:
            d["has_pdf"] = self.has_pdf
        if self.route_selected is not None:
            d["route_selected"] = self.route_selected
        if self.save_method_used is not None:
            d["save_method_used"] = self.save_method_used
        if self.item_discovery_status is not None:
            d["item_discovery_status"] = self.item_discovery_status
        if self.pdf_verification_status is not None:
            d["pdf_verification_status"] = self.pdf_verification_status
        if self.reason_code is not None:
            d["reason_code"] = self.reason_code
        if self.verification_attempts is not None:
            d["verification_attempts"] = self.verification_attempts
        if self.verification_deadline_at is not None:
            d["verification_deadline_at"] = self.verification_deadline_at
        if self.suspected_duplicate_keys:
            d["suspected_duplicate_keys"] = list(self.suspected_duplicate_keys)
        return d


def _default_batch_id() -> str:
    return f"ing_{uuid.uuid4().hex[:12]}"



@dataclass
class BlockingDecision:
    """Structured decision that requires user resolution before proceeding.

    `item_keys` references items in the canonical `pdf_missing_items` list by key
    only — never duplicates titles, urls, or other payload fields.
    """

    decision_id: str
    batch_id: str | None
    item_keys: tuple[str, ...]
    description: str
    resolved: bool = False
    created_at: float = field(default_factory=time.monotonic)


def _build_suggested_next_steps() -> list[dict]:
    """Static ordered list of post-ingest steps. NO `tool` or `args` keys."""
    return [
        {"step_id": "index", "description": "Run full-text indexing on saved items.", "depends_on": []},
        {"step_id": "note", "description": "Generate per-paper summary notes.", "depends_on": ["index"]},
        {"step_id": "classify", "description": "Move items from INBOX into collections.", "depends_on": ["note"]},
        {"step_id": "tag", "description": "Apply tags from existing vocabulary.", "depends_on": ["classify"]},
        {"step_id": "verify", "description": "Confirm INBOX cleared and tags applied.", "depends_on": ["tag"]},
    ]


@dataclass
class BatchState:
    total: int
    collection_used: str | None
    pending_items: list[IngestItemState]
    session_id: str | None = None
    batch_id: str = field(default_factory=_default_batch_id)
    state: Literal["queued", "running", "completed", "completed_with_errors", "failed", "cancelled"] = "queued"
    is_final: bool = False
    created_at: float = field(default_factory=time.monotonic)
    finalized_at: float | None = None
    blocking_decisions: list["BlockingDecision"] = field(default_factory=list)
    suggested_next_steps: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def update_item(
        self,
        index: int,
        *,
        status: Literal["pending", "saved", "duplicate", "duplicate_in_batch", "failed"],
        item_key: str | None | _UnsetType = _UNSET,
        title: str | None | _UnsetType = _UNSET,
        error: str | None | _UnsetType = _UNSET,
        warning: str | None | _UnsetType = _UNSET,
        routing_status: str | None | _UnsetType = _UNSET,
        ingest_method: str | None | _UnsetType = _UNSET,
        has_pdf: bool | None | _UnsetType = _UNSET,
        route_selected: str | None | _UnsetType = _UNSET,
        save_method_used: str | None | _UnsetType = _UNSET,
        item_discovery_status: str | None | _UnsetType = _UNSET,
        pdf_verification_status: str | None | _UnsetType = _UNSET,
        reason_code: str | None | _UnsetType = _UNSET,
        verification_attempts: int | None | _UnsetType = _UNSET,
        verification_deadline_at: float | None | _UnsetType = _UNSET,
        suspected_duplicate_keys: tuple[str, ...] | list[str] | _UnsetType = _UNSET,
    ) -> None:
        """Thread-safe update of an item by index.

        Looks up the item by its .index attribute (not list position) so that
        gaps in the index sequence (caused by duplicates) don't cause IndexError.
        """
        with self._lock:
            item = next((it for it in self.pending_items if it.index == index), None)
            if item is None:
                logger.warning("update_item: no item with index=%d", index)
                return
            item.status = status
            if item_key is not _UNSET:
                item.item_key = cast("str | None", item_key)
            if title is not _UNSET:
                item.title = cast("str | None", title)
            if error is not _UNSET:
                item.error = cast("str | None", error)
            if warning is not _UNSET:
                item.warning = cast("str | None", warning)
            if routing_status is not _UNSET:
                item.routing_status = cast("str | None", routing_status)
            if ingest_method is not _UNSET:
                item.ingest_method = cast("str | None", ingest_method)
            if has_pdf is not _UNSET:
                item.has_pdf = cast("bool | None", has_pdf)
            if route_selected is not _UNSET:
                item.route_selected = cast("str | None", route_selected)
            if save_method_used is not _UNSET:
                item.save_method_used = cast("str | None", save_method_used)
            if item_discovery_status is not _UNSET:
                item.item_discovery_status = cast("str | None", item_discovery_status)
            if pdf_verification_status is not _UNSET:
                item.pdf_verification_status = cast("str | None", pdf_verification_status)
            if reason_code is not _UNSET:
                item.reason_code = cast("str | None", reason_code)
            if verification_attempts is not _UNSET:
                item.verification_attempts = cast("int | None", verification_attempts)
            if verification_deadline_at is not _UNSET:
                item.verification_deadline_at = cast("float | None", verification_deadline_at)
            if suspected_duplicate_keys is not _UNSET:
                item.suspected_duplicate_keys = tuple(
                    cast("tuple[str, ...] | list[str]", suspected_duplicate_keys)
                )

    def finalize(self) -> None:
        """Set is_final=True, determine state, set finalized_at."""
        with self._lock:
            saved = sum(1 for it in self.pending_items if it.status in ("saved", "duplicate", "duplicate_in_batch"))
            failed = sum(1 for it in self.pending_items if it.status == "failed")
            total_resolved = saved + failed

            if total_resolved == 0:
                self.state = "failed"
            elif failed == 0:
                self.state = "completed"
            elif saved == 0:
                self.state = "failed"
            else:
                self.state = "completed_with_errors"

            self.is_final = True
            self.finalized_at = time.monotonic()

    def _counts(self) -> tuple[int, int, int]:
        """Return (saved, failed, pending_count) without acquiring lock."""
        saved = sum(1 for it in self.pending_items if it.status in ("saved", "duplicate", "duplicate_in_batch"))
        failed = sum(1 for it in self.pending_items if it.status == "failed")
        pending = sum(1 for it in self.pending_items if it.status == "pending")
        return saved, failed, pending

    def summary(self) -> dict:
        """Return summary dict with aggregate counts."""
        with self._lock:
            saved, failed, pending_count = self._counts()
            return {
                "batch_id": self.batch_id,
                "session_id": self.session_id,
                "state": self.state,
                "is_final": self.is_final,
                "total": self.total,
                "saved": saved,
                "failed": failed,
                "pending_count": pending_count,
                "collection_used": self.collection_used,
            }

    def full_status(self) -> dict:
        """Like summary() but adds results list and _instruction."""
        with self._lock:
            saved, failed, pending_count = self._counts()
            saved_with_pdf = sum(
                1
                for it in self.pending_items
                if it.status == "saved"
                and (it.pdf_verification_status == "present" or it.has_pdf is True)
            )
            saved_metadata_only = sum(
                1
                for it in self.pending_items
                if it.status == "saved"
                and (it.pdf_verification_status == "missing" or it.has_pdf is False)
            )
            saved_pdf_pending = sum(
                1
                for it in self.pending_items
                if it.status == "saved" and it.pdf_verification_status == "pending"
            )
            pdf_missing_items = [
                {"item_key": it.item_key, "title": it.title, "url": it.url}
                for it in self.pending_items
                if it.status == "saved" and (it.pdf_verification_status == "missing" or it.has_pdf is False)
            ]
            pdf_pending_items = [
                {"item_key": it.item_key, "title": it.title, "url": it.url}
                for it in self.pending_items
                if it.status == "saved" and it.pdf_verification_status == "pending"
            ]
            status: dict = {
                "batch_id": self.batch_id,
                "session_id": self.session_id,
                "state": self.state,
                "is_final": self.is_final,
                "total": self.total,
                "saved": saved,
                "saved_with_pdf": saved_with_pdf,
                "saved_metadata_only": saved_metadata_only,
                "saved_pdf_pending": saved_pdf_pending,
                "failed": failed,
                "pending_count": pending_count,
                "collection_used": self.collection_used,
                "results": [it.to_dict() for it in self.pending_items],
            }
            if pdf_missing_items:
                status["pdf_missing_items"] = pdf_missing_items
            if pdf_pending_items:
                status["pdf_pending_items"] = pdf_pending_items

            # Emit metadata_only_choice as a structured BlockingDecision when finalized.
            # `pdf_missing_items` remains the canonical payload; this references by item_key only.
            if self.is_final and saved_metadata_only > 0 and not any(
                d.decision_id == "metadata_only_choice" and d.batch_id == self.batch_id
                for d in getattr(self, "blocking_decisions", [])
            ):
                getattr(self, "blocking_decisions", []).append(BlockingDecision(
                    decision_id="metadata_only_choice",
                    batch_id=self.batch_id,
                    item_keys=tuple(
                        it.item_key
                        for it in self.pending_items
                        if it.status == "saved"
                        and (it.pdf_verification_status == "missing" or it.has_pdf is False)
                        and it.item_key
                    ),
                    description=(
                        f"{saved_metadata_only} item(s) saved as metadata-only. "
                        "User must choose: retry on VPN, keep as metadata, or delete."
                    ),
                ))

            status["blocking_decisions"] = [
                {
                    "decision_id": d.decision_id,
                    "batch_id": d.batch_id,
                    "item_keys": list(d.item_keys),
                    "description": d.description,
                    "resolved": d.resolved,
                }
                for d in getattr(self, "blocking_decisions", [])
            ]
            status["suggested_next_steps"] = (
                _build_suggested_next_steps() if self.is_final and saved > 0 else []
            )

            return status


class BatchStore:
    """Thread-safe store for BatchState objects with TTL and max-size eviction."""

    def __init__(self, *, max_batches: int = 50, completed_ttl_s: float = 1800.0) -> None:
        self._max_batches = max_batches
        self._completed_ttl_s = completed_ttl_s
        self._batches: dict[str, BatchState] = {}
        self._lock = threading.Lock()

    def put(self, batch: BatchState) -> None:
        """Add or update a batch, triggering eviction if needed."""
        with self._lock:
            self._batches[batch.batch_id] = batch
            self._evict_unlocked()

    def get(self, batch_id: str) -> BatchState | None:
        """Return batch by ID, or None if not found."""
        with self._lock:
            return self._batches.get(batch_id)

    def clear(self) -> None:
        """Remove all batches."""
        with self._lock:
            self._batches.clear()

    def count(self) -> int:
        """Return number of stored batches."""
        with self._lock:
            return len(self._batches)

    def find_unresolved_metadata_only(
        self, session_id: str | None
    ) -> tuple["BlockingDecision", "BatchState"] | None:
        """Find an unresolved metadata_only_choice in the most recent matching batch.

        When session_id is provided, only batches from that session are checked.
        When session_id is None, all batches are searched (session-agnostic gate).

        Returns None when:
        - no matching batch has an unresolved metadata_only_choice
        - any lookup error occurs (fail-open with WARNING log)

        Reads BatchStore directly without caching. Concurrency: point-in-time
        snapshot, no lock held across the caller's downstream work.
        """
        try:
            with self._lock:
                if session_id is not None:
                    candidates = [
                        b for b in self._batches.values() if b.session_id == session_id
                    ]
                else:
                    candidates = list(self._batches.values())
            if not candidates:
                return None
            candidates.sort(key=lambda b: b.created_at, reverse=True)
            most_recent = candidates[0]
            for d in getattr(most_recent, "blocking_decisions", []):
                if d.decision_id == "metadata_only_choice" and not d.resolved:
                    return d, most_recent
            return None
        except Exception as exc:
            logger.warning(
                "metadata-only gate lookup failed: %s — failing open", exc
            )
            return None

    def evict_expired(self) -> None:
        """Evict finalized batches whose finalized_at is older than TTL."""
        with self._lock:
            self._evict_unlocked()

    def _evict_unlocked(self) -> None:
        """Eviction logic — caller must hold self._lock."""
        now = time.monotonic()

        # Step 1: TTL eviction — only finalized batches
        expired = [
            bid
            for bid, b in self._batches.items()
            if b.is_final and b.finalized_at is not None and (now - b.finalized_at) > self._completed_ttl_s
        ]
        for bid in expired:
            del self._batches[bid]

        # Step 2: Max-size eviction — evict oldest finalized batches first
        while len(self._batches) > self._max_batches:
            # Collect finalized batches sorted by finalized_at ascending
            finalized = sorted(
                [(bid, b) for bid, b in self._batches.items() if b.is_final],
                key=lambda x: x[1].finalized_at or 0.0,
            )
            if not finalized:
                # No finalized batches to evict; cannot reduce further
                break
            oldest_bid, _ = finalized[0]
            del self._batches[oldest_bid]
