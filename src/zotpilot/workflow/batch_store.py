"""Persistent store for workflow batches."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from ..config import _default_data_dir
from .batch import TERMINAL_PHASES, Batch


def _default_batches_dir() -> Path:
    override = os.environ.get("ZOTPILOT_BATCHES_DIR")
    if override:
        return Path(override).expanduser()
    return _default_data_dir() / "batches"


class BatchStore:
    """Persist workflow batches under the ZotPilot data directory."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or _default_batches_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, batch_id: str) -> Path:
        return self.base_dir / f"{batch_id}.json"

    def save(self, batch: Batch) -> Batch:
        with self._lock:
            self._path(batch.batch_id).write_text(
                json.dumps(batch.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return batch

    def load(self, batch_id: str) -> Batch | None:
        path = self._path(batch_id)
        if not path.exists():
            return None
        return Batch.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_active(
        self,
        *,
        library_id: str | None = None,
        phases: set[str] | None = None,
    ) -> list[Batch]:
        batches: list[Batch] = []
        for path in sorted(self.base_dir.glob("ing_*.json")):
            try:
                batch = Batch.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if library_id is not None and batch.library_id != str(library_id):
                continue
            if batch.phase in TERMINAL_PHASES:
                continue
            if phases is not None and batch.phase not in phases:
                continue
            batches.append(batch)
        batches.sort(key=lambda item: item.last_transition_at, reverse=True)
        return batches

    def get_active(
        self,
        *,
        library_id: str | None = None,
        phases: set[str] | None = None,
    ) -> Batch | None:
        active = self.list_active(library_id=library_id, phases=phases)
        return active[0] if active else None
