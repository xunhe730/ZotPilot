"""Admin tools: reranking config, vision costs."""
import json
import logging
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from ..reranker import VALID_QUARTILES, VALID_SECTIONS
from ..state import (
    ToolError,
    _clear_library_override,
    _get_config,
    _get_reranker,
    _get_retriever,
    _get_zotero,
    _set_library_override,
    mcp,
)

logger = logging.getLogger(__name__)


def get_reranking_config() -> dict:
    """Get current reranking weights and valid section/quartile names."""
    _config = _get_config()
    if _config.embedding_provider == "none":
        return {
            "enabled": False,
            "mode": "no-rag",
            "message": "Reranking unavailable in No-RAG mode. Configure an embedding provider to enable semantic search.",  # noqa: E501
        }
    _get_retriever()  # Ensure initialized
    reranker = _get_reranker()

    return {
        "enabled": _config.rerank_enabled,
        "alpha": reranker.alpha,
        "section_weights": reranker.default_section_weights,
        "journal_weights": {
            k if k is not None else "unknown": v
            for k, v in reranker.quartile_weights.items()
            if k != ""  # Skip the empty string duplicate
        },
        "valid_sections": sorted(VALID_SECTIONS),
        "valid_quartiles": sorted(VALID_QUARTILES),
        "oversample_multiplier": _config.oversample_multiplier,
    }


def get_vision_costs(
    last_n: Annotated[int, Field(description="Recent log entries to include in detail", ge=0)] = 10,
) -> dict:
    """Get vision API usage and cost summary from table extraction."""
    _config = _get_config()

    log_path = Path(_config.chroma_db_path).parent / "vision_costs.json"

    if not log_path.exists():
        return {
            "message": "Vision API has not been used yet -- no cost log found.",
            "log_path": log_path.name,
        }

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise ToolError(f"Failed to read vision cost log: {exc}")

    if not entries:
        return {
            "message": "Vision cost log exists but contains no entries.",
            "log_path": log_path.name,
        }

    # Aggregate totals
    total_cost = 0.0
    total_input = 0
    total_output = 0
    total_cache_write = 0
    total_cache_read = 0

    # Per-session tracking: session_id -> {first_ts, count, cost}
    session_map: dict[str, dict] = {}

    for entry in entries:
        total_cost += entry.get("cost_usd", 0.0)
        total_input += entry.get("input_tokens", 0)
        total_output += entry.get("output_tokens", 0)
        total_cache_write += entry.get("cache_write_tokens", 0)
        total_cache_read += entry.get("cache_read_tokens", 0)

        sid = entry.get("session_id", "unknown")
        ts = entry.get("timestamp", "")
        if sid not in session_map:
            session_map[sid] = {"session_id": sid, "first_timestamp": ts, "table_count": 0, "cost_usd": 0.0}
        session_map[sid]["table_count"] += 1
        session_map[sid]["cost_usd"] += entry.get("cost_usd", 0.0)
        # Keep earliest timestamp
        if ts and ts < session_map[sid]["first_timestamp"]:
            session_map[sid]["first_timestamp"] = ts

    total_tables = len(entries)
    avg_cost = total_cost / total_tables if total_tables else 0.0

    # Round session costs
    sessions = []
    for s in session_map.values():
        sessions.append({
            "session_id": s["session_id"],
            "first_timestamp": s["first_timestamp"],
            "table_count": s["table_count"],
            "cost_usd": round(s["cost_usd"], 6),
        })
    # Sort sessions by first timestamp ascending
    sessions.sort(key=lambda x: x["first_timestamp"])

    recent = entries[-last_n:] if last_n > 0 else []

    return {
        "total_cost_usd": round(total_cost, 6),
        "total_tables": total_tables,
        "avg_cost_per_table_usd": round(avg_cost, 6),
        "tokens": {
            "input": total_input,
            "output": total_output,
            "cache_write": total_cache_write,
            "cache_read": total_cache_read,
        },
        "sessions": sessions,
        "recent_entries": recent,
        "log_path": log_path.name,
    }


@mcp.tool()
def switch_library(
    library_id: Annotated[str | None, Field(description="Library/group ID. None to list available.")] = None,
    library_type: Annotated[Literal["user", "group", "default"], Field(description="'default' resets to user library")] = "group",  # noqa: E501
) -> dict:
    """List libraries or switch active library context.

    NOTE: Switching applies to metadata tools (tags, collections, notes, annotations,
    write operations) and the Zotero Web API reader. It does NOT apply to RAG search
    tools (search_papers, search_topic, search_tables, search_figures), passage context,
    or index stats — these always operate on the default user library because the vector
    store has no per-library isolation yet.
    """
    if library_id is None:
        # List available libraries
        zotero = _get_zotero()
        return {"libraries": zotero.get_libraries()}

    if library_type == "default":
        _clear_library_override()
        return {"switched": True, "library_type": "user", "message": "Reset to default user library"}

    _set_library_override(library_id, library_type)
    return {
        "switched": True,
        "library_id": library_id,
        "library_type": library_type,
        "message": (
            f"Switched to {library_type} library {library_id}. "
            f"Metadata/write tools now operate on this library. "
            f"Note: RAG search and indexing still use the default user library."
        ),
    }
