"""Sentence Transformers embedding provider for domain-specific models."""
from __future__ import annotations

import importlib.util
import logging

from .base import EmbeddingError

logger = logging.getLogger(__name__)


class SentenceTransformerEmbedder:
    """Embedding via sentence-transformers (SPECTER2, PubMedBERT, etc.)."""

    def __init__(self, model_name: str, dimensions: int) -> None:
        if importlib.util.find_spec("sentence_transformers") is None:
            raise EmbeddingError(
                "sentence-transformers is not installed. "
                "Install it with: pip install 'zotpilot[biomedical]'"
            )
        self._model_name = model_name
        self.dimensions = dimensions
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.info("Loading sentence-transformer model: %s", self._model_name)
            self._model = SentenceTransformer(self._model_name)

    def embed(self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        if not texts:
            return []
        self._ensure_model()
        embeddings = self._model.encode(texts, show_progress_bar=False)
        return [e.tolist() for e in embeddings]

    def embed_query(self, query: str) -> list[float]:
        return self.embed([query])[0]
