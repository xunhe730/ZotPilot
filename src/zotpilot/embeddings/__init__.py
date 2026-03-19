"""Embedding providers for ZotPilot."""
from .gemini import GeminiEmbedder, EmbeddingError
from .local import LocalEmbedder
from .dashscope import DashScopeEmbedder
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
    elif config.embedding_provider == "dashscope":
        logger.info(f"Using DashScope embeddings ({config.embedding_model}, {config.embedding_dimensions} dimensions)")
        return DashScopeEmbedder(
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
            api_key=config.dashscope_api_key,
            timeout=config.embedding_timeout,
            max_retries=config.embedding_max_retries,
        )
    elif config.embedding_provider == "none":
        logger.info("No-RAG mode: embedding disabled, semantic search unavailable")
        return None
    else:
        raise ValueError(
            f"Invalid embedding_provider: {config.embedding_provider}. "
            f"Must be 'gemini', 'dashscope', 'local', or 'none'"
        )


__all__ = ["create_embedder", "GeminiEmbedder", "DashScopeEmbedder", "LocalEmbedder", "EmbeddingError", "EmbedderProtocol"]
