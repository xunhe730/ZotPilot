"""Research session data structures."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_session_id() -> str:
    return f"rs_{uuid4().hex[:12]}"


@dataclass
class ItemFingerprint:
    """Minimal fingerprint for drift detection."""

    title_prefix: str
    date_added: str | None = None

    @classmethod
    def from_dict(cls, data: dict | None) -> "ItemFingerprint | None":
        if not data:
            return None
        return cls(
            title_prefix=str(data.get("title_prefix", "")),
            date_added=data.get("date_added"),
        )


@dataclass
class SessionItem:
    """Tracked Zotero item inside a research session."""

    item_key: str
    title: str | None = None
    note_count: int = 0
    fingerprint: ItemFingerprint | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "SessionItem":
        return cls(
            item_key=str(data["item_key"]),
            title=data.get("title"),
            note_count=int(data.get("note_count", 0)),
            fingerprint=ItemFingerprint.from_dict(data.get("fingerprint")),
        )


@dataclass
class ResearchSession:
    """Persisted state for a ztp-research workflow."""

    query: str
    library_id: str
    library_type: str = "user"
    session_id: str = field(default_factory=new_session_id)
    status: Literal["running", "awaiting_user", "completed", "cancelled", "resume_invalidated"] = "running"
    phase: str = "clarify_query"
    approved_checkpoints: list[str] = field(default_factory=list)
    items: list[SessionItem] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    drift_details: list[dict] = field(default_factory=list)

    def touch(self, *, phase: str | None = None, status: str | None = None) -> None:
        if phase is not None:
            self.phase = phase
        if status is not None:
            self.status = status
        self.updated_at = _utc_now()

    def approve(self, checkpoint: str) -> None:
        if checkpoint not in self.approved_checkpoints:
            self.approved_checkpoints.append(checkpoint)
        next_phase = "ingest" if checkpoint == "candidate-review" else "index"
        self.touch(phase=next_phase, status="running")

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["item_count"] = len(self.items)
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "ResearchSession":
        items = [SessionItem.from_dict(item) for item in data.get("items", [])]
        return cls(
            session_id=str(data["session_id"]),
            query=str(data["query"]),
            library_id=str(data["library_id"]),
            library_type=str(data.get("library_type", "user")),
            status=data.get("status", "running"),
            phase=data.get("phase", "clarify_query"),
            approved_checkpoints=list(data.get("approved_checkpoints", [])),
            items=items,
            created_at=data.get("created_at", _utc_now()),
            updated_at=data.get("updated_at", _utc_now()),
            drift_details=list(data.get("drift_details", [])),
        )
