"""JSON persistence for research sessions."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import _default_data_dir
from ..state import _get_zotero
from .research_session import ItemFingerprint, ResearchSession, SessionItem


def _default_sessions_dir() -> Path:
    return _default_data_dir() / "sessions"


def current_library_id() -> str:
    return str(_get_zotero().library_id)


def build_session_items(item_keys: list[str]) -> list[SessionItem]:
    zotero = _get_zotero()
    items: list[SessionItem] = []
    for item_key in item_keys:
        current = zotero.get_item(item_key)
        if current is None:
            continue
        items.append(
            SessionItem(
                item_key=item_key,
                title=current.title,
                fingerprint=ItemFingerprint(
                    title_prefix=(current.title or "")[:32],
                    date_added=current.date_added,
                ),
            )
        )
    return items


def validate_session_items(session: ResearchSession) -> list[dict]:
    zotero = _get_zotero()
    drifted: list[dict] = []
    for item in session.items:
        fingerprint = item.fingerprint
        if fingerprint is None:
            continue
        current = zotero.get_item(item.item_key)
        if current is None:
            drifted.append({"item_key": item.item_key, "reason": "deleted"})
            continue
        if fingerprint.date_added and current.date_added != fingerprint.date_added:
            drifted.append({"item_key": item.item_key, "reason": "replaced"})
            continue
        if fingerprint.title_prefix and not (current.title or "").startswith(fingerprint.title_prefix):
            drifted.append({"item_key": item.item_key, "reason": "title_changed"})
    return drifted


class SessionStore:
    """Persist research sessions under the ZotPilot data directory."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or _default_sessions_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.base_dir / f"{session_id}.json"

    def save(self, session: ResearchSession) -> ResearchSession:
        session.updated_at = session.updated_at or session.created_at
        self._path(session.session_id).write_text(
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return session

    def load(self, session_id: str) -> ResearchSession | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        session = ResearchSession.from_dict(data)
        drift_details = validate_session_items(session)
        if drift_details:
            session.drift_details = drift_details
            session.touch(status="resume_invalidated")
            self.save(session)
        return session

    def create(self, query: str, *, library_id: str | None = None, library_type: str = "user") -> ResearchSession:
        session = ResearchSession(query=query, library_id=library_id or current_library_id(), library_type=library_type)
        return self.save(session)

    def list_active(self, *, library_id: str | None = None, statuses: set[str] | None = None) -> list[ResearchSession]:
        library_id = library_id or current_library_id()
        statuses = statuses or {"running", "awaiting_user"}
        sessions: list[ResearchSession] = []
        for path in sorted(self.base_dir.glob("rs_*.json")):
            session = self.load(path.stem)
            if session is None:
                continue
            if session.library_id != str(library_id):
                continue
            if session.status not in statuses:
                continue
            sessions.append(session)
        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return sessions

    def get_active(self, *, library_id: str | None = None, statuses: set[str] | None = None) -> ResearchSession | None:
        sessions = self.list_active(library_id=library_id, statuses=statuses)
        return sessions[0] if sessions else None

    def update_items(
        self,
        session: ResearchSession,
        item_keys: list[str],
        *,
        phase: str,
        status: str,
    ) -> ResearchSession:
        session.items = build_session_items(item_keys)
        session.touch(phase=phase, status=status)
        return self.save(session)
