"""Batch-based research workflow state."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from time import time
from typing import Any, Literal
from uuid import uuid4

Phase = Literal[
    "candidate",
    "candidates_confirmed",
    "preflighting",
    "preflight_blocked",
    "approved",
    "ingesting",
    "post_ingest_verified",
    "post_ingest_approved",
    "post_processing",
    "AwaitingTaxonomyAuthorization",
    "taxonomy_authorized",
    "post_ingest_skipped",
    "post_process_verified",
    "done",
    "aborted",
]

TERMINAL_PHASES: set[Phase] = {"done", "aborted"}
ACTIVE_PHASES: set[Phase] = {
    "candidate",
    "candidates_confirmed",
    "preflighting",
    "preflight_blocked",
    "approved",
    "ingesting",
    "post_ingest_verified",
    "post_ingest_approved",
    "post_processing",
    "AwaitingTaxonomyAuthorization",
    "taxonomy_authorized",
    "post_ingest_skipped",
    "post_process_verified",
}

_ALLOWED_TRANSITIONS: dict[Phase, set[Phase]] = {
    "candidate": {"candidates_confirmed", "aborted"},
    "candidates_confirmed": {"preflighting"},
    "preflighting": {"preflight_blocked", "approved", "aborted"},
    "preflight_blocked": {"preflighting"},
    "approved": {"ingesting", "aborted"},
    "ingesting": {"post_ingest_verified", "aborted"},
    "post_ingest_verified": {"post_ingest_approved", "aborted"},
    "post_ingest_approved": {"post_processing"},
    "post_processing": {"AwaitingTaxonomyAuthorization", "post_process_verified", "aborted"},
    "AwaitingTaxonomyAuthorization": {"taxonomy_authorized", "post_ingest_skipped", "aborted"},
    "taxonomy_authorized": {"post_processing"},
    "post_ingest_skipped": {"post_process_verified"},
    "post_process_verified": {"done", "aborted"},
    "done": set(),
    "aborted": set(),
}

REINDEX_ELIGIBLE_REASONS: frozenset[str] = frozenset({
    "embedding_api_unavailable",
    "embedding_api_rate_limit",
    "index_write_failed",
    "chromadb_transient_error",
})


class IllegalPhaseTransition(ValueError):
    """Raised when a workflow transition is not allowed."""


class InvalidPhaseError(ValueError):
    """Raised when a tool is called in the wrong phase."""


class LibraryMismatchError(RuntimeError):
    """Raised when the active Zotero library does not match the batch."""


class UnauthorizedTaxonomyChange(ValueError):
    """Raised when a tag or collection creation is not in the batch authorization list."""


def new_batch_id() -> str:
    return f"ing_{uuid4().hex[:12]}"


@dataclass(frozen=True)
class BlockingDecision:
    decision_id: str
    description: str
    item_keys: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "description": self.description,
            "item_keys": list(self.item_keys),
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BlockingDecision":
        return cls(
            decision_id=str(data["decision_id"]),
            description=str(data["description"]),
            item_keys=tuple(data.get("item_keys") or ()),
            payload=dict((data.get("payload") or data.get("metadata")) or {}),
        )


@dataclass(frozen=True)
class PreflightResult:
    round: int
    checked_at: float
    blocking_decisions: tuple[BlockingDecision, ...] = ()
    all_clear: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "checked_at": self.checked_at,
            "all_clear": self.all_clear,
            "blocking_decisions": [decision.to_dict() for decision in self.blocking_decisions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PreflightResult":
        return cls(
            round=int(data["round"]),
            checked_at=float(data["checked_at"]),
            all_clear=bool(data["all_clear"]),
            blocking_decisions=tuple(
                BlockingDecision.from_dict(item) for item in data.get("blocking_decisions") or ()
            ),
        )


@dataclass(frozen=True)
class Item:
    identifier: str
    doc_id: str
    source_url: str | None
    title: str | None = None
    paper_payload: dict[str, Any] = field(default_factory=dict)
    status: Literal["pending", "saved", "degraded", "failed", "duplicate"] = "pending"
    pdf_present: bool | None = None
    metadata_complete: bool | None = None
    indexed: bool = False
    noted: bool = False
    tagged: bool = False
    classified: bool = False
    zotero_item_key: str | None = None
    routing_method: Literal["connector", "api"] | None = None
    route_selected: str | None = None
    save_method_used: str | None = None
    item_discovery_status: str | None = None
    pdf_verification_status: str | None = None
    reason_code: str | None = None
    suspected_duplicate_keys: tuple[str, ...] = ()
    degradation_reasons: tuple[str, ...] = ()
    retry_attempts: int = 0

    def with_updates(self, **changes: Any) -> "Item":
        return replace(self, **changes)

    @property
    def payload(self) -> dict[str, Any]:
        return self.paper_payload

    def is_reindex_eligible(self) -> bool:
        return any(reason in REINDEX_ELIGIBLE_REASONS for reason in self.degradation_reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "doc_id": self.doc_id,
            "source_url": self.source_url,
            "title": self.title,
            "paper_payload": self.paper_payload,
            "status": self.status,
            "pdf_present": self.pdf_present,
            "metadata_complete": self.metadata_complete,
            "indexed": self.indexed,
            "noted": self.noted,
            "tagged": self.tagged,
            "classified": self.classified,
            "zotero_item_key": self.zotero_item_key,
            "routing_method": self.routing_method,
            "route_selected": self.route_selected,
            "save_method_used": self.save_method_used,
            "item_discovery_status": self.item_discovery_status,
            "pdf_verification_status": self.pdf_verification_status,
            "reason_code": self.reason_code,
            "suspected_duplicate_keys": list(self.suspected_duplicate_keys),
            "degradation_reasons": list(self.degradation_reasons),
            "retry_attempts": self.retry_attempts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Item":
        return cls(
            identifier=str(data["identifier"]),
            doc_id=str(data["doc_id"]),
            source_url=data.get("source_url"),
            title=data.get("title"),
            paper_payload=dict((data.get("paper_payload") or data.get("payload")) or {}),
            status=str(data.get("status", "pending")),  # type: ignore[arg-type]
            pdf_present=data.get("pdf_present"),
            metadata_complete=data.get("metadata_complete"),
            indexed=bool(data.get("indexed", False)),
            noted=bool(data.get("noted", False)),
            tagged=bool(data.get("tagged", False)),
            classified=bool(data.get("classified", False)),
            zotero_item_key=data.get("zotero_item_key"),
            routing_method=data.get("routing_method"),
            route_selected=data.get("route_selected"),
            save_method_used=data.get("save_method_used"),
            item_discovery_status=data.get("item_discovery_status"),
            pdf_verification_status=data.get("pdf_verification_status"),
            reason_code=data.get("reason_code"),
            suspected_duplicate_keys=tuple(data.get("suspected_duplicate_keys") or ()),
            degradation_reasons=tuple(data.get("degradation_reasons") or ()),
            retry_attempts=int(data.get("retry_attempts", 0)),
        )


@dataclass(frozen=True)
class Batch:
    batch_id: str
    library_id: str
    query: str
    phase: Phase
    items: tuple[Item, ...]
    preflight_result: PreflightResult | None
    authorized_new_tags: frozenset[str]
    authorized_new_collections: frozenset[str]
    created_at: float
    last_transition_at: float
    collection_key: str | None = None
    legacy_ingest_batch_id: str | None = None
    engine_index_map: dict[str, str] = field(default_factory=dict)
    pending_taxonomy_tags: tuple[str, ...] = ()
    pending_taxonomy_collections: tuple[str, ...] = ()
    final_report: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking_decisions(self) -> tuple[BlockingDecision, ...]:
        if self.preflight_result is None:
            return ()
        return self.preflight_result.blocking_decisions

    @property
    def engine_batch_id(self) -> str | None:
        return self.legacy_ingest_batch_id

    @classmethod
    def create(
        cls,
        *,
        library_id: str,
        query: str,
        phase: Phase,
        items: list[Item] | tuple[Item, ...],
    ) -> "Batch":
        now = time()
        return cls(
            batch_id=new_batch_id(),
            library_id=str(library_id),
            query=query,
            phase=phase,
            items=tuple(items),
            preflight_result=None,
            authorized_new_tags=frozenset(),
            authorized_new_collections=frozenset(),
            created_at=now,
            last_transition_at=now,
        )

    def assert_phase(self, expected: Phase | set[Phase]) -> None:
        if isinstance(expected, set):
            if self.phase not in expected:
                allowed = ", ".join(sorted(expected))
                raise InvalidPhaseError(f"Expected phase in {{{allowed}}}, got {self.phase!r}")
            return
        if self.phase != expected:
            raise InvalidPhaseError(f"Expected phase {expected!r}, got {self.phase!r}")

    def transition_to(self, target: Phase) -> "Batch":
        allowed = _ALLOWED_TRANSITIONS[self.phase]
        if target not in allowed:
            raise IllegalPhaseTransition(f"{self.phase!r} -> {target!r} is not allowed")
        return replace(self, phase=target, last_transition_at=time())

    def set_preflight_result(self, result: PreflightResult) -> "Batch":
        return replace(self, preflight_result=result)

    def with_preflight_result(self, result: PreflightResult) -> "Batch":
        return self.set_preflight_result(result)

    def with_items(self, items: list[Item] | tuple[Item, ...]) -> "Batch":
        return replace(self, items=tuple(items))

    def update_item(self, doc_id: str, **changes: Any) -> "Batch":
        updated = tuple(
            item.with_updates(**changes) if item.doc_id == doc_id else item for item in self.items
        )
        return replace(self, items=updated)

    def mark_engine_batch(self, engine_batch_id: str) -> "Batch":
        return replace(self, legacy_ingest_batch_id=engine_batch_id)

    def with_engine_batch_id(self, engine_batch_id: str) -> "Batch":
        return self.mark_engine_batch(engine_batch_id)

    def mark_approved(self) -> "Batch":
        if self.preflight_result is None or not self.preflight_result.all_clear:
            raise InvalidPhaseError("Batch cannot be approved without a successful preflight result")
        return self.transition_to("approved")

    def with_authorizations(
        self,
        *,
        new_tags: list[str] | None = None,
        new_collections: list[str] | None = None,
    ) -> "Batch":
        return replace(
            self,
            authorized_new_tags=self.authorized_new_tags | frozenset(new_tags or []),
            authorized_new_collections=self.authorized_new_collections | frozenset(new_collections or []),
            pending_taxonomy_tags=(),
            pending_taxonomy_collections=(),
        )

    def with_pending_taxonomy(
        self,
        *,
        tags: list[str] | tuple[str, ...] = (),
        collections: list[str] | tuple[str, ...] = (),
    ) -> "Batch":
        return replace(
            self,
            pending_taxonomy_tags=tuple(tags),
            pending_taxonomy_collections=tuple(collections),
        )

    def has_authorized_tags(self, proposed: list[str]) -> bool:
        return all(tag in self.authorized_new_tags for tag in proposed)

    def has_authorized_collections(self, proposed: list[str]) -> bool:
        return all(name in self.authorized_new_collections for name in proposed)

    def find_item(self, key: str) -> Item | None:
        for item in self.items:
            if item.doc_id == key or item.zotero_item_key == key or item.identifier == key:
                return item
        return None

    def reindex_eligible_item_keys(self) -> list[str]:
        return [
            item.zotero_item_key or item.doc_id
            for item in self.items
            if item.status == "degraded" and item.is_reindex_eligible()
        ]

    def next_action_payload(self) -> dict[str, Any] | None:
        if self.phase in TERMINAL_PHASES:
            return None
        if self.phase == "candidates_confirmed":
            return {
                "tool": "get_batch_status",
                "args_hint": {"batch_id": self.batch_id},
                "why": "Candidates confirmed; preflight is running. Poll for updated status.",
                "blocks_on": "worker",
            }
        if self.phase == "candidate":
            return {
                "tool": "confirm_candidates",
                "args_hint": {"batch_id": self.batch_id, "selected_ids": "<user choice>"},
                "why": "Review and confirm which candidates to ingest.",
                "blocks_on": "user",
            }
        if self.phase == "preflight_blocked":
            urls = [decision.payload.get("url") for decision in self.blocking_decisions if decision.payload.get("url")]
            return {
                "tool": "resolve_preflight",
                "args_hint": {"batch_id": self.batch_id, "urls": urls},
                "why": "Resolve browser verification and rerun preflight.",
                "blocks_on": "user",
            }
        if self.phase == "preflighting":
            return {
                "tool": "approve_ingest",
                "args_hint": {"batch_id": self.batch_id},
                "why": "Preflight is clear. User approval is required before ingest starts.",
                "blocks_on": "user",
            }
        if self.phase in {
            "approved",
            "ingesting",
            "post_processing",
            "taxonomy_authorized",
            "post_ingest_skipped",
            "post_ingest_approved",
        }:
            return {
                "tool": "get_batch_status",
                "args_hint": {"batch_id": self.batch_id},
                "why": "Worker is running. Poll for updated status.",
                "blocks_on": "worker",
            }
        if self.phase == "post_ingest_verified":
            return {
                "tool": "approve_post_ingest",
                "args_hint": {"batch_id": self.batch_id},
                "why": "Post-ingest verification is ready for review.",
                "blocks_on": "user",
            }
        if self.phase == "AwaitingTaxonomyAuthorization":
            return {
                "tool": "authorize_taxonomy_changes",
                "args_hint": {
                    "batch_id": self.batch_id,
                    "authorized_new_tags": list(self.pending_taxonomy_tags),
                    "authorized_new_collections": list(self.pending_taxonomy_collections),
                },
                "why": "New taxonomy proposals require explicit user authorization.",
                "blocks_on": "user",
            }
        if self.phase == "post_process_verified":
            return {
                "tool": "approve_post_process",
                "args_hint": {"batch_id": self.batch_id},
                "why": "Final verification is ready for acceptance.",
                "blocks_on": "user",
            }
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "library_id": self.library_id,
            "query": self.query,
            "phase": self.phase,
            "items": [item.to_dict() for item in self.items],
            "preflight_result": self.preflight_result.to_dict() if self.preflight_result else None,
            "authorized_new_tags": sorted(self.authorized_new_tags),
            "authorized_new_collections": sorted(self.authorized_new_collections),
            "created_at": self.created_at,
            "last_transition_at": self.last_transition_at,
            "collection_key": self.collection_key,
            "legacy_ingest_batch_id": self.legacy_ingest_batch_id,
            "engine_index_map": self.engine_index_map,
            "pending_taxonomy_tags": list(self.pending_taxonomy_tags),
            "pending_taxonomy_collections": list(self.pending_taxonomy_collections),
            "final_report": self.final_report,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Batch":
        preflight_raw = data.get("preflight_result")
        return cls(
            batch_id=str(data["batch_id"]),
            library_id=str(data["library_id"]),
            query=str(data.get("query", "")),
            phase=str(data["phase"]),  # type: ignore[arg-type]
            items=tuple(Item.from_dict(item) for item in data.get("items") or ()),
            preflight_result=PreflightResult.from_dict(preflight_raw) if preflight_raw else None,
            authorized_new_tags=frozenset(data.get("authorized_new_tags") or ()),
            authorized_new_collections=frozenset(data.get("authorized_new_collections") or ()),
            created_at=float(data["created_at"]),
            last_transition_at=float(data["last_transition_at"]),
            collection_key=data.get("collection_key"),
            legacy_ingest_batch_id=data.get("legacy_ingest_batch_id") or data.get("engine_batch_id"),
            engine_index_map=dict(data.get("engine_index_map") or {}),
            pending_taxonomy_tags=tuple(data.get("pending_taxonomy_tags") or ()),
            pending_taxonomy_collections=tuple(data.get("pending_taxonomy_collections") or ()),
            final_report=dict(data.get("final_report") or {}),
        )


def new_batch(*, library_id: str, query: str, phase: Phase, items: list[Item] | tuple[Item, ...]) -> Batch:
    return Batch.create(library_id=library_id, query=query, phase=phase, items=items)
