"""Local embedding provider using ChromaDB's default model."""
import logging

logger = logging.getLogger(__name__)


class LocalEmbedder:
    """
    Local embedding using ChromaDB's default function (all-MiniLM-L6-v2).

    Benefits:
    - No API key required
    - Works offline
    - ~90MB model, downloaded automatically on first use
    - 384 dimensions (vs Gemini's 768)

    Note: Uses symmetric embeddings (same for docs and queries).
    """

    def __init__(self):
        import chromadb.utils.embedding_functions as ef
        self._ef = ef.DefaultEmbeddingFunction()
        self.dimensions = 384  # all-MiniLM-L6-v2 output size

    def embed(self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        """Embed texts. task_type is ignored (symmetric model)."""
        if not texts:
            return []
        # ChromaDB's DefaultEmbeddingFunction returns numpy arrays with np.float32
        # Convert to native Python floats for ChromaDB compatibility
        return [[float(v) for v in e] for e in self._ef(texts)]

    def embed_query(self, query: str) -> list[float]:
        """Embed a search query."""
        return self.embed([query])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed documents for indexing."""
        return self.embed(texts)
