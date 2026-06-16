"""Sentence Transformers embedding provider for domain-specific models."""
from __future__ import annotations

import importlib.util
import logging

from .base import EmbeddingError

logger = logging.getLogger(__name__)

_SPECTER2_BASE = "allenai/specter2_base"
_SPECTER2_ADAPTER = "allenai/specter2"


class SentenceTransformerEmbedder:
    """Embedding via sentence-transformers or adapters (SPECTER2, PubMedBERT, etc.)."""

    def __init__(self, model_name: str, dimensions: int) -> None:
        self._model_name = model_name
        self.dimensions = dimensions
        self._model = None
        self._use_adapters = model_name.startswith("allenai/specter2")

    def _ensure_model(self):
        if self._model is not None:
            return

        if self._use_adapters:
            self._load_specter2()
        else:
            self._load_sentence_transformer()

    def _load_specter2(self):
        if importlib.util.find_spec("adapters") is None:
            raise EmbeddingError(
                "adapters is not installed. "
                "Install it with: pip install adapters"
            )
        from adapters import AutoAdapterModel

        logger.info("Loading SPECTER2 base model: %s", _SPECTER2_BASE)
        model = AutoAdapterModel.from_pretrained(_SPECTER2_BASE)

        logger.info("Loading SPECTER2 adapter: %s", _SPECTER2_ADAPTER)
        model.load_adapter(_SPECTER2_ADAPTER, source="hf", set_active=True)

        self._model = model

    def _load_sentence_transformer(self):
        if importlib.util.find_spec("sentence_transformers") is None:
            raise EmbeddingError(
                "sentence-transformers is not installed. "
                "Install it with: pip install 'zotpilot[biomedical]'"
            )
        from sentence_transformers import SentenceTransformer

        logger.info("Loading sentence-transformer model: %s", self._model_name)
        self._model = SentenceTransformer(self._model_name)

    def embed(self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        if not texts:
            return []
        self._ensure_model()

        if self._use_adapters:
            return self._embed_specter2(texts)
        return self._embed_sentence_transformer(texts)

    def _embed_specter2(self, texts: list[str]) -> list[list[float]]:
        import torch
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(_SPECTER2_BASE)
        embeddings = []

        self._model.eval()
        with torch.no_grad():
            for text in texts:
                inputs = tokenizer(
                    text,
                    return_tensors="pt",
                    padding=True,
                    truncation=True,
                    max_length=512,
                )
                outputs = self._model(**inputs)
                cls_embedding = outputs.last_hidden_state[:, 0, :].squeeze().numpy()
                embeddings.append(cls_embedding.tolist())

        return embeddings

    def _embed_sentence_transformer(self, texts: list[str]) -> list[list[float]]:
        raw = self._model.encode(texts, show_progress_bar=False)
        return [e.tolist() for e in raw]

    def embed_query(self, query: str) -> list[float]:
        return self.embed([query])[0]
