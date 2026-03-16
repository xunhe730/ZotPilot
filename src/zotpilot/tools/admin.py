"""Admin tools: reranking config, vision costs."""
import json
import logging
from pathlib import Path

from ..state import mcp, _get_retriever, _get_reranker, _get_config, ToolError
from ..reranker import VALID_SECTIONS, VALID_QUARTILES

logger = logging.getLogger(__name__)


@mcp.tool()
def get_reranking_config() -> dict:
    """
    Get current reranking configuration.

    Returns section weights, journal quartile weights, alpha exponent,
    and valid section names for use with section_weights parameter.
    """
    _get_retriever()  # Ensure initialized
    reranker = _get_reranker()
    _config = _get_config()

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


@mcp.tool()
def get_vision_costs(last_n: int = 10) -> dict:
    """
    Get vision API batch usage and cost summary.

    Reads the vision cost log written during table extraction and returns
    a summary of token usage, costs, and per-session breakdowns.

    Args:
        last_n: Number of most recent log entries to include in detail (default 10)

    Returns:
        Dict with:
        - total_cost_usd: Total spend across all runs
        - total_tables: Total number of table extractions logged
        - avg_cost_per_table_usd: Mean cost per table
        - tokens: Breakdown of input, output, cache_write, cache_read totals
        - sessions: Per-session summary (session_id, first_timestamp, table_count, cost_usd)
        - recent_entries: Last N log entries in chronological order
        - log_path: Absolute path to the cost log file
    """
    _config = _get_config()

    log_path = Path(_config.chroma_db_path).parent / "vision_costs.json"

    if not log_path.exists():
        return {
            "message": "Vision API has not been used yet -- no cost log found.",
            "log_path": str(log_path),
        }

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise ToolError(f"Failed to read vision cost log: {exc}")

    if not entries:
        return {
            "message": "Vision cost log exists but contains no entries.",
            "log_path": str(log_path),
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
        "log_path": str(log_path),
    }
