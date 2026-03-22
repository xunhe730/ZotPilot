"""Embedding protocol definition."""
from __future__ import annotations

from typing import Protocol


class EmbedderProtocol(Protocol):
    """Interface for text embedding."""

    def embed(self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]: ...
