"""PDF processing package for ZotPilot.

Public API re-exported from sub-modules.
"""
from .extractor import (
    extract_document,
    resolve_pending_vision,
    PendingVisionWork,
    SYNTHETIC_CAPTION_PREFIX,
)

__all__ = [
    "extract_document",
    "resolve_pending_vision",
    "PendingVisionWork",
    "SYNTHETIC_CAPTION_PREFIX",
]
