"""Embedding providers for ZotPilot."""
from .gemini import GeminiEmbedder, EmbeddingError
from .local import LocalEmbedder
from .base import EmbedderProtocol


def create_embedder(config):
    """Create embedder based on config.embedding_provider."""
    import logging
    logger = logging.getLogger(__name__)

    if config.embedding_provider == "local":
        logger.info("Using local embeddings (all-MiniLM-L6-v2, 384 dimensions)")
        return LocalEmbedder()
    elif config.embedding_provider == "gemini":
        logger.info(f"Using Gemini embeddings ({config.embedding_model}, {config.embedding_dimensions} dimensions)")
        return GeminiEmbedder(
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
            api_key=config.gemini_api_key,
            timeout=config.embedding_timeout,
            max_retries=config.embedding_max_retries,
        )
    else:
        raise ValueError(
            f"Invalid embedding_provider: {config.embedding_provider}. "
            f"Must be 'gemini' or 'local'"
        )


__all__ = ["create_embedder", "GeminiEmbedder", "LocalEmbedder", "EmbeddingError", "EmbedderProtocol"]
