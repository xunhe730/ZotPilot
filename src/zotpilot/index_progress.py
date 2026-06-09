"""Structured progress events for long-running indexing jobs."""
import json
import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

INDEX_PROGRESS_SCHEMA_VERSION = 1


class ProgressSink(Protocol):
    """Receives structured indexing progress events."""

    def emit(self, event_type: str, **payload: object) -> None:
        """Record one progress event."""


class JsonlProgressSink:
    """Append-only JSONL sink for index progress events."""

    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time):
        self.path = Path(path).expanduser()
        self._clock = clock

    def emit(self, event_type: str, **payload: object) -> None:
        event = make_progress_event(event_type, self._clock(), payload)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
                f.write("\n")
        except OSError as e:
            logger.warning("Failed to append index progress event to %s: %s", self.path, e)


def make_progress_event(event_type: str, timestamp: float, payload: Mapping[str, object]) -> dict[str, object]:
    """Build a JSON-serializable event with stable common fields."""
    event: dict[str, object] = {
        "schema_version": INDEX_PROGRESS_SCHEMA_VERSION,
        "event": event_type,
        "timestamp": timestamp,
    }
    for key, value in payload.items():
        event[key] = _json_safe(value)
    return event


def emit_progress(sink: ProgressSink | None, event_type: str, **payload: object) -> None:
    """Emit progress without letting observer failures affect indexing."""
    if sink is None:
        return
    try:
        sink.emit(event_type, **payload)
    except Exception as e:
        logger.warning("Index progress sink failed while emitting %s: %s", event_type, e)


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_json_safe(v) for v in value]
    return str(value)
