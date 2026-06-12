"""Configuration management."""
import hashlib
import json
import logging
import os
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import providers

logger = logging.getLogger(__name__)

ANTHROPIC_DEFAULT_VISION_MODEL = "claude-haiku-4-5-20251001"
DASHSCOPE_DEFAULT_VISION_MODEL = "qwen3-vl-flash"


def _default_config_dir() -> Path:
    """Platform-aware config directory."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", "~/AppData/Roaming")).expanduser()
    else:
        base = Path("~/.config").expanduser()
    return base / "zotpilot"


def _default_data_dir() -> Path:
    """Platform-aware data directory."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", "~/AppData/Local")).expanduser()
    else:
        base = Path("~/.local/share").expanduser()
    return base / "zotpilot"


def profile_path() -> Path:
    """Canonical path to the user's ZOTPILOT.md reading/research profile.

    Platform-aware: ``%APPDATA%/zotpilot/ZOTPILOT.md`` on Windows,
    ``~/.config/zotpilot/ZOTPILOT.md`` elsewhere. For backward compatibility,
    if the canonical path does not exist but a legacy ``~/.config/zotpilot``
    file does, the legacy path is returned. Used for BOTH reads and writes so
    the reader (profile_library, tutor._read_persona) and the writer
    (tutor.save_reading_persona) always resolve to the same file.
    """
    canonical = _default_config_dir() / "ZOTPILOT.md"
    if canonical.exists():
        return canonical
    legacy = Path("~/.config/zotpilot/ZOTPILOT.md").expanduser()
    if legacy.exists():
        return legacy
    return canonical


def index_data_dir(config: "Config") -> Path:
    """Directory that stores index-adjacent state files."""
    # Also normalize direct Config-like objects used by tests or API callers.
    return Path(config.chroma_db_path).expanduser().parent


def index_journal_path(config: "Config") -> Path:
    """Path to the crash-recovery index journal."""
    return index_data_dir(config) / "index_journal.json"


def index_lease_path(config: "Config") -> Path:
    """Path to the cross-process indexing lease."""
    return index_data_dir(config) / "index_lease.json"


def index_progress_path(config: "Config") -> Path:
    """Path to the append-only indexing progress stream."""
    return index_data_dir(config) / "index_progress.jsonl"


def _old_config_path() -> Path:
    """Legacy deep-zotero config path."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", "~/AppData/Roaming")).expanduser()
    else:
        base = Path("~/.config").expanduser()
    return base / "deep-zotero" / "config.json"


@dataclass
class Config:
    """Application configuration."""
    zotero_data_dir: Path
    chroma_db_path: Path
    embedding_model: str
    embedding_dimensions: int
    chunk_size: int
    chunk_overlap: int
    gemini_api_key: str | None
    dashscope_api_key: str | None
    # Custom Gemini base URL (for API proxies / restricted regions). None = SDK default.
    gemini_base_url: str | None
    # Embedding provider: "gemini", "dashscope", "local", or "none" (No-RAG mode)
    embedding_provider: str
    # DashScope embedding endpoint: "compatible" or "native"
    dashscope_embedding_endpoint: str
    # Embedding settings
    embedding_timeout: float
    embedding_max_retries: int
    # Reranking settings
    rerank_alpha: float
    rerank_section_weights: dict[str, float] | None
    rerank_journal_weights: dict[str, float] | None  # Use "unknown" for null quartile
    rerank_enabled: bool
    oversample_multiplier: int
    oversample_topic_factor: int  # Additional factor for search_topic
    stats_sample_limit: int
    # OCR settings (language passed through to pymupdf-layout)
    ocr_language: str
    # OpenAlex settings
    openalex_email: str | None  # Optional email for polite pool (10 req/sec vs 1 req/sec)
    # Vision extraction settings
    vision_enabled: bool
    vision_provider: str
    vision_model: str
    anthropic_api_key: str | None
    vision_max_tables_per_run: int | None
    vision_max_cost_usd: float | None
    # Long document filtering
    max_pages: int  # Maximum PDF pages to index (0 = no limit)
    preflight_enabled: bool
    # Zotero Web API (for write operations)
    zotero_api_key: str | None
    zotero_user_id: str | None
    zotero_library_type: str  # "user" or "group"
    # Semantic Scholar API key (optional, increases rate limit)
    semantic_scholar_api_key: str | None
    # OpenAI-compatible embedding provider (additive, optional; appended at end
    # so no non-default field follows -- keeps dataclass field ordering valid)
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    # Formula OCR settings (optional, local-first, excluded from index config hash)
    formula_ocr_enabled: bool = False
    formula_ocr_provider: str = "local"
    formula_ocr_max_formulas_per_doc: int = 40
    formula_ocr_max_formulas_per_page: int = 6
    formula_ocr_min_confidence: float = 0.6
    formula_ocr_simpletex_token: str | None = None
    formula_ocr_simpletex_app_id: str | None = None
    formula_ocr_simpletex_app_secret: str | None = None
    formula_ocr_simpletex_endpoint: str = "https://server.simpletex.net/api/latex_ocr"
    formula_ocr_simpletex_timeout: float = 30.0
    formula_ocr_simpletex_min_interval: float = 0.55
    formula_ocr_simpletex_max_retries: int = 2

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        """Load shared config from disk."""
        if path is not None:
            config_path = Path(path).expanduser()
        else:
            config_path = _default_config_dir() / "config.json"

            # Migration support: if new config doesn't exist but old one does, load from old
            if not config_path.exists():
                old_path = _old_config_path()
                if old_path.exists():
                    logger.info(
                        f"Migrating config from {old_path} to {config_path}. "
                        f"Please update your config path to {config_path}."
                    )
                    config_path = old_path

        data = {}
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)

        default_chroma = str(_default_data_dir() / "chroma")

        provider = data.get("embedding_provider", "gemini")
        # Provider-aware defaults for model and dimensions (single source of truth)
        default_model, default_dims = providers.EMBEDDING_MODEL_DEFAULTS.get(
            provider, ("gemini-embedding-001", 768)
        )

        vision_provider = data.get("vision_provider", "anthropic")
        default_vision_model = (
            DASHSCOPE_DEFAULT_VISION_MODEL
            if vision_provider == "dashscope"
            else ANTHROPIC_DEFAULT_VISION_MODEL
        )
        vision_model = data.get("vision_model", default_vision_model)
        if vision_provider == "dashscope" and vision_model == ANTHROPIC_DEFAULT_VISION_MODEL:
            vision_model = DASHSCOPE_DEFAULT_VISION_MODEL
        elif vision_provider == "anthropic" and vision_model == DASHSCOPE_DEFAULT_VISION_MODEL:
            vision_model = ANTHROPIC_DEFAULT_VISION_MODEL

        return cls(
            zotero_data_dir=Path(data.get("zotero_data_dir", "~/Zotero")).expanduser(),
            chroma_db_path=Path(data.get("chroma_db_path", default_chroma)).expanduser(),
            embedding_model=data.get("embedding_model", default_model),
            embedding_dimensions=data.get("embedding_dimensions", default_dims),
            chunk_size=data.get("chunk_size", 400),
            chunk_overlap=data.get("chunk_overlap", 100),
            gemini_api_key=data.get("gemini_api_key"),
            dashscope_api_key=data.get("dashscope_api_key"),
            gemini_base_url=data.get("gemini_base_url"),
            embedding_provider=data.get("embedding_provider", "gemini"),
            dashscope_embedding_endpoint=data.get("dashscope_embedding_endpoint", "compatible"),
            embedding_timeout=data.get("embedding_timeout", 120.0),
            embedding_max_retries=data.get("embedding_max_retries", 3),
            rerank_alpha=data.get("rerank_alpha", 0.7),
            rerank_section_weights=data.get("rerank_section_weights"),
            rerank_journal_weights=data.get("rerank_journal_weights"),
            rerank_enabled=data.get("rerank_enabled", True),
            oversample_multiplier=data.get("oversample_multiplier", 3),
            oversample_topic_factor=data.get("oversample_topic_factor", 5),
            stats_sample_limit=data.get("stats_sample_limit", 10000),
            ocr_language=data.get("ocr_language", "eng"),
            openalex_email=data.get("openalex_email"),
            vision_enabled=data.get("vision_enabled", True),
            vision_provider=vision_provider,
            vision_model=vision_model,
            anthropic_api_key=data.get("anthropic_api_key"),
            vision_max_tables_per_run=data.get("vision_max_tables_per_run"),
            vision_max_cost_usd=data.get("vision_max_cost_usd"),
            max_pages=data.get("max_pages", 40),
            preflight_enabled=data.get("preflight_enabled", True),
            zotero_api_key=data.get("zotero_api_key"),
            zotero_user_id=data.get("zotero_user_id"),
            zotero_library_type=data.get("zotero_library_type", "user"),
            semantic_scholar_api_key=data.get("semantic_scholar_api_key"),
            embedding_base_url=data.get("embedding_base_url", None),
            embedding_api_key=data.get("embedding_api_key", None),
            formula_ocr_enabled=data.get("formula_ocr_enabled", False),
            formula_ocr_provider=data.get("formula_ocr_provider", "local"),
            formula_ocr_max_formulas_per_doc=data.get("formula_ocr_max_formulas_per_doc", 40),
            formula_ocr_max_formulas_per_page=data.get("formula_ocr_max_formulas_per_page", 6),
            formula_ocr_min_confidence=data.get("formula_ocr_min_confidence", 0.6),
            formula_ocr_simpletex_token=data.get("formula_ocr_simpletex_token"),
            formula_ocr_simpletex_app_id=data.get("formula_ocr_simpletex_app_id"),
            formula_ocr_simpletex_app_secret=data.get("formula_ocr_simpletex_app_secret"),
            formula_ocr_simpletex_endpoint=data.get(
                "formula_ocr_simpletex_endpoint",
                "https://server.simpletex.net/api/latex_ocr",
            ),
            formula_ocr_simpletex_timeout=data.get("formula_ocr_simpletex_timeout", 30.0),
            formula_ocr_simpletex_min_interval=data.get("formula_ocr_simpletex_min_interval", 0.55),
            formula_ocr_simpletex_max_retries=data.get("formula_ocr_simpletex_max_retries", 2),
        )

    def save(self, path: Path | str | None = None) -> None:
        """Write the config to JSON using an atomic write pattern."""
        if path is not None:
            config_path = Path(path).expanduser()
        else:
            config_path = _default_config_dir() / "config.json"

        # Create parent dirs if missing
        config_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "zotero_data_dir": str(self.zotero_data_dir),
            "chroma_db_path": str(self.chroma_db_path),
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "embedding_provider": self.embedding_provider,
            "dashscope_embedding_endpoint": self.dashscope_embedding_endpoint,
            "embedding_timeout": self.embedding_timeout,
            "embedding_max_retries": self.embedding_max_retries,
            "rerank_alpha": self.rerank_alpha,
            "rerank_section_weights": self.rerank_section_weights,
            "rerank_journal_weights": self.rerank_journal_weights,
            "rerank_enabled": self.rerank_enabled,
            "oversample_multiplier": self.oversample_multiplier,
            "oversample_topic_factor": self.oversample_topic_factor,
            "stats_sample_limit": self.stats_sample_limit,
            "ocr_language": self.ocr_language,
            "openalex_email": self.openalex_email,
            "vision_enabled": self.vision_enabled,
            "vision_provider": self.vision_provider,
            "vision_model": self.vision_model,
            "gemini_api_key": self.gemini_api_key,
            "dashscope_api_key": self.dashscope_api_key,
            "gemini_base_url": self.gemini_base_url,
            "anthropic_api_key": self.anthropic_api_key,
            "vision_max_tables_per_run": self.vision_max_tables_per_run,
            "vision_max_cost_usd": self.vision_max_cost_usd,
            "max_pages": self.max_pages,
            "preflight_enabled": self.preflight_enabled,
            "zotero_api_key": self.zotero_api_key,
            "zotero_user_id": self.zotero_user_id,
            "zotero_library_type": self.zotero_library_type,
            "semantic_scholar_api_key": self.semantic_scholar_api_key,
            "embedding_base_url": self.embedding_base_url,
            "embedding_api_key": self.embedding_api_key,
            "formula_ocr_enabled": self.formula_ocr_enabled,
            "formula_ocr_provider": self.formula_ocr_provider,
            "formula_ocr_max_formulas_per_doc": self.formula_ocr_max_formulas_per_doc,
            "formula_ocr_max_formulas_per_page": self.formula_ocr_max_formulas_per_page,
            "formula_ocr_min_confidence": self.formula_ocr_min_confidence,
            "formula_ocr_simpletex_token": self.formula_ocr_simpletex_token,
            "formula_ocr_simpletex_app_id": self.formula_ocr_simpletex_app_id,
            "formula_ocr_simpletex_app_secret": self.formula_ocr_simpletex_app_secret,
            "formula_ocr_simpletex_endpoint": self.formula_ocr_simpletex_endpoint,
            "formula_ocr_simpletex_timeout": self.formula_ocr_simpletex_timeout,
            "formula_ocr_simpletex_min_interval": self.formula_ocr_simpletex_min_interval,
            "formula_ocr_simpletex_max_retries": self.formula_ocr_simpletex_max_retries,
        }
        data = {key: value for key, value in data.items() if value is not None}

        # Atomic write: temp file + rename
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=config_path.parent, suffix=".tmp", prefix="zotpilot_"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            # Set restrictive permissions on Unix before atomic rename
            if sys.platform != "win32":
                os.chmod(tmp_path, 0o600)

            os.replace(tmp_path, config_path)
            tmp_path = None  # Successfully replaced, no cleanup needed
        except OSError as e:
            # Clean up temp file on failure, original config untouched
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise RuntimeError(f"Failed to write config to {config_path}: {e}") from e

    def validate(self) -> list[str]:
        """Return list of validation errors, empty if valid."""
        errors = []
        if not self.zotero_data_dir.exists():
            errors.append(f"Zotero data dir not found: {self.zotero_data_dir}")
        if not (self.zotero_data_dir / "zotero.sqlite").exists():
            errors.append(f"Zotero database not found: {self.zotero_data_dir / 'zotero.sqlite'}")

        if self.embedding_provider == "gemini" and not self.gemini_api_key:
            errors.append("GEMINI_API_KEY not set (required for embedding_provider='gemini')")
        elif self.embedding_provider == "dashscope" and not self.dashscope_api_key:
            errors.append("DASHSCOPE_API_KEY not set (required for embedding_provider='dashscope')")
        elif self.embedding_provider == "openai-compatible":
            base_url = providers._resolve_secret(
                self.embedding_base_url, "ZOTPILOT_EMBEDDING_BASE_URL", "OPENAI_BASE_URL"
            )
            if not base_url:
                errors.append(
                    "embedding_base_url not set (required for embedding_provider='openai-compatible'); "
                    "set it or ZOTPILOT_EMBEDDING_BASE_URL / OPENAI_BASE_URL"
                )
            else:
                errors.extend(_validate_base_url(base_url))
            if not self.embedding_model:
                errors.append(
                    "embedding_model not set (required for embedding_provider='openai-compatible')"
                )
            if self.embedding_dimensions <= 0:
                errors.append(
                    "embedding_dimensions must be > 0 for embedding_provider='openai-compatible' "
                    "(set it explicitly; non-matryoshka servers ignore a requested dimension)"
                )
            key = providers._resolve_secret(
                self.embedding_api_key, "ZOTPILOT_EMBEDDING_API_KEY", "OPENAI_API_KEY"
            )
            if not key:
                logger.warning(
                    "No embedding_api_key set for embedding_provider='openai-compatible'. "
                    "This is fine for local endpoints (e.g. Ollama) but required for hosted vendors."
                )
        elif self.embedding_provider not in providers.EMBEDDING_PROVIDERS:
            valid = ", ".join(repr(p) for p in providers.EMBEDDING_PROVIDERS)
            errors.append(f"Invalid embedding_provider: {self.embedding_provider}. Must be one of: {valid}")  # noqa: E501
        if self.dashscope_embedding_endpoint not in ("compatible", "native"):
            errors.append("Invalid dashscope_embedding_endpoint: must be 'compatible' or 'native'")

        if self.vision_provider not in ("anthropic", "dashscope"):
            errors.append("Invalid vision_provider: must be 'anthropic' or 'dashscope'")
        elif self.vision_enabled and self.vision_provider == "dashscope" and not self.dashscope_api_key:
            errors.append("DASHSCOPE_API_KEY not set (required for vision_provider='dashscope')")
        if self.vision_provider == "dashscope" and self.vision_model.startswith("claude-"):
            errors.append("Invalid vision_model for vision_provider='dashscope'")
        elif self.vision_provider == "anthropic" and self.vision_model.startswith("qwen"):
            errors.append("Invalid vision_model for vision_provider='anthropic'")

        if self.gemini_base_url:
            parsed = urlparse(self.gemini_base_url)
            if parsed.scheme != "https":
                errors.append(
                    "gemini_base_url must use https:// — a plaintext endpoint would expose "
                    f"GEMINI_API_KEY in transit (got '{self.gemini_base_url}')"
                )
            elif not parsed.netloc:
                errors.append(f"gemini_base_url is malformed: {self.gemini_base_url}")

        from .feature_extraction.formula_ocr import FORMULA_OCR_PROVIDERS

        if self.formula_ocr_provider not in FORMULA_OCR_PROVIDERS:
            valid = ", ".join(repr(p) for p in FORMULA_OCR_PROVIDERS)
            errors.append(
                f"Invalid formula_ocr_provider: {self.formula_ocr_provider}. "
                f"Must be one of: {valid}"
            )
        if self.formula_ocr_max_formulas_per_doc < 0:
            errors.append("formula_ocr_max_formulas_per_doc must be >= 0")
        if self.formula_ocr_max_formulas_per_page < 0:
            errors.append("formula_ocr_max_formulas_per_page must be >= 0")
        if not 0.0 <= self.formula_ocr_min_confidence <= 1.0:
            errors.append("formula_ocr_min_confidence must be between 0.0 and 1.0")
        if self.formula_ocr_simpletex_timeout <= 0:
            errors.append("formula_ocr_simpletex_timeout must be > 0")
        if self.formula_ocr_simpletex_min_interval < 0:
            errors.append("formula_ocr_simpletex_min_interval must be >= 0")
        if self.formula_ocr_simpletex_max_retries < 0:
            errors.append("formula_ocr_simpletex_max_retries must be >= 0")
        if self.formula_ocr_provider == "simpletex":
            simpletex_token = providers._resolve_secret(
                self.formula_ocr_simpletex_token,
                "ZOTPILOT_SIMPLETEX_TOKEN",
                "SIMPLETEX_UAT",
                "SIMPLETEX_TOKEN",
            )
            simpletex_app_id = providers._resolve_secret(
                self.formula_ocr_simpletex_app_id,
                "ZOTPILOT_SIMPLETEX_APP_ID",
                "SIMPLETEX_APP_ID",
            )
            simpletex_app_secret = providers._resolve_secret(
                self.formula_ocr_simpletex_app_secret,
                "ZOTPILOT_SIMPLETEX_APP_SECRET",
                "SIMPLETEX_APP_SECRET",
            )
            if not simpletex_token and not (simpletex_app_id and simpletex_app_secret):
                errors.append(
                    "SimpleTex formula OCR requires formula_ocr_simpletex_token "
                    "or formula_ocr_simpletex_app_id + formula_ocr_simpletex_app_secret"
                )
            parsed = urlparse(self.formula_ocr_simpletex_endpoint)
            if parsed.scheme != "https" or not parsed.netloc:
                errors.append("formula_ocr_simpletex_endpoint must be a valid https:// URL")

        return errors


def _validate_base_url(url: str) -> list[str]:
    """Validate an openai-compatible base URL (H1): scheme + no embedded creds."""
    errors = []
    parsed = urllib.parse.urlsplit(url.rstrip("/"))
    if parsed.scheme not in ("http", "https"):
        errors.append(
            f"Invalid embedding_base_url scheme {parsed.scheme!r}: must be http or https"
        )
    if "@" in parsed.netloc:
        errors.append(
            "embedding_base_url must not contain embedded credentials (user:pass@host); "
            "pass the API key via embedding_api_key instead"
        )
    return errors


def _config_hash(config: "Config") -> str:
    """Hash of config values that affect indexed content.

    Changes to these values require re-indexing. The ``embedding_base_url`` is
    folded in CONDITIONALLY -- only for the openai-compatible provider -- so all
    existing providers hash byte-identically to prior releases (no forced
    reindex on upgrade). Relocated here from ``indexer.py`` so the lightweight
    CLI can import it without dragging in the indexer's heavy dependencies.
    """
    data = (
        f"{config.chunk_size}:"
        f"{config.chunk_overlap}:"
        f"{config.embedding_provider}:"
        f"{getattr(config, 'dashscope_embedding_endpoint', 'compatible')}:"
        f"{config.embedding_dimensions}:"
        f"{config.embedding_model}:"
        f"{config.ocr_language}:"
        f"{getattr(config, 'vision_enabled', True)}:"
        f"{getattr(config, 'vision_provider', 'anthropic')}:"
        f"{getattr(config, 'vision_model', '')}"
    )
    if config.embedding_provider == "openai-compatible":
        data += f":{getattr(config, 'embedding_base_url', '') or ''}"
    return hashlib.sha256(data.encode()).hexdigest()[:16]
