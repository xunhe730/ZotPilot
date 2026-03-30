"""Async ingestion batch state tracking data structures."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class IngestItemState:
    index: int
    url: str | None
    title: str | None = None
    status: Literal["pending", "saved", "duplicate", "failed"] = "pending"
    item_key: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        """Return dict representation, omitting None-valued optional fields.

        Always includes: index, url, status.
        Omits if None: title, item_key, error.
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
        return d


def _default_batch_id() -> str:
    return f"ing_{uuid.uuid4().hex[:12]}"


@dataclass
class BatchState:
    total: int
    collection_used: str | None
    tags: list[str] | None
    pending_items: list[IngestItemState]
    batch_id: str = field(default_factory=_default_batch_id)
    state: Literal[
        "queued", "running", "completed", "completed_with_errors", "failed", "cancelled"
    ] = "queued"
    is_final: bool = False
    created_at: float = field(default_factory=time.monotonic)
    finalized_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def update_item(
        self,
        index: int,
        *,
        status: Literal["pending", "saved", "duplicate", "failed"],
        item_key: str | None = None,
        title: str | None = None,
        error: str | None = None,
    ) -> None:
        """Thread-safe update of an item by index."""
        with self._lock:
            item = self.pending_items[index]
            item.status = status
            if item_key is not None:
                item.item_key = item_key
            if title is not None:
                item.title = title
            if error is not None:
                item.error = error

    def finalize(self) -> None:
        """Set is_final=True, determine state, set finalized_at."""
        with self._lock:
            saved = sum(
                1 for it in self.pending_items if it.status in ("saved", "duplicate")
            )
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
        saved = sum(1 for it in self.pending_items if it.status in ("saved", "duplicate"))
        failed = sum(1 for it in self.pending_items if it.status == "failed")
        pending = sum(1 for it in self.pending_items if it.status == "pending")
        return saved, failed, pending

    def summary(self) -> dict:
        """Return summary dict with aggregate counts."""
        with self._lock:
            saved, failed, pending_count = self._counts()
            return {
                "batch_id": self.batch_id,
                "state": self.state,
                "is_final": self.is_final,
                "total": self.total,
                "saved": saved,
                "failed": failed,
                "pending_count": pending_count,
                "collection_used": self.collection_used,
            }

    def full_status(self) -> dict:
        """Like summary() but adds results list."""
        with self._lock:
            saved, failed, pending_count = self._counts()
            return {
                "batch_id": self.batch_id,
                "state": self.state,
                "is_final": self.is_final,
                "total": self.total,
                "saved": saved,
                "failed": failed,
                "pending_count": pending_count,
                "collection_used": self.collection_used,
                "results": [it.to_dict() for it in self.pending_items],
            }


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
            if b.is_final
            and b.finalized_at is not None
            and (now - b.finalized_at) > self._completed_ttl_s
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
