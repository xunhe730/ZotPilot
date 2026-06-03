"""Ollama local embedding provider."""
import logging

import httpx

logger = logging.getLogger(__name__)

OLLAMA_DEFAULT_URL = "http://localhost:11434"


class OllamaEmbedder:
    """
    Local embeddings via Ollama (http://localhost:11434).
    Recommended model: nomic-embed-text (768 dims, no quota, offline).
    Other options: mxbai-embed-large (1024 dims), all-minilm (384 dims).
    """

    def __init__(
        self,
        model: str = "nomic-embed-text",
        base_url: str = OLLAMA_DEFAULT_URL,
        timeout: float = 120.0,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.dimensions = 768  # nomic-embed-text default; overridden by config

    def embed(self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        if not texts:
            return []
        resp = httpx.post(
            f"{self.base_url}/api/embed",
            json={"model": self.model, "input": texts},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]

    def embed_query(self, query: str) -> list[float]:
        return self.embed([query])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)
