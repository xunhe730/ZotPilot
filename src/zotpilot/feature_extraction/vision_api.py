"""Vision API: API access layer for vision table extraction.

Provides TableVisionSpec, cost logging, and generic batch infrastructure.
Single-agent extraction logic is built on top of this layer.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import pymupdf

from .vision_extract import (
    AgentResponse,
    build_common_ctx,
    parse_agent_response,
    render_table_region,
    VISION_FIRST_SYSTEM,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TableVisionSpec:
    """Input spec for one table to extract via vision."""

    table_id: str
    pdf_path: Path
    page_num: int
    bbox: tuple[float, float, float, float]
    raw_text: str
    caption: str | None = None
    garbled: bool = False


@dataclass
class CostEntry:
    """One API call's cost record."""

    timestamp: str
    session_id: str
    table_id: str
    agent_role: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    cost_usd: float


# ---------------------------------------------------------------------------
# Pricing (dollars per million tokens)
# ---------------------------------------------------------------------------

_PRICING: dict[str, dict[str, float]] = {
    "claude-haiku-4-5-20251001": {
        "input": 1.00,
        "output": 5.00,
        "cache_write": 1.25,
        "cache_read": 0.10,
    },
}


def _compute_cost(usage: object, model: str) -> float:
    """Compute USD cost from an API response's usage object."""
    prices = _PRICING.get(model, _PRICING["claude-haiku-4-5-20251001"])
    input_t = getattr(usage, "input_tokens", 0) or 0
    output_t = getattr(usage, "output_tokens", 0) or 0
    cache_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (
        input_t * prices["input"]
        + output_t * prices["output"]
        + cache_w * prices["cache_write"]
        + cache_r * prices["cache_read"]
    ) / 1_000_000


# ---------------------------------------------------------------------------
# Cost logging
# ---------------------------------------------------------------------------


def _append_cost_entry(path: Path, entry: CostEntry) -> None:
    """Append a cost entry to the JSON log file."""
    entries: list[dict] = []
    if path.exists():
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            entries = []
    entries.append(asdict(entry))
    path.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# VisionAPI
# ---------------------------------------------------------------------------


class VisionAPI:
    """API access layer for vision table extraction with cost logging.

    Parameters
    ----------
    api_key:
        Anthropic API key.
    model:
        Model ID (default: claude-haiku-4-5-20251001).
    cost_log_path:
        Path to persistent JSON cost log file.
    cache:
        Enable prompt caching (system prompts cached across requests).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5-20251001",
        cost_log_path: Path | str = Path("vision_api_costs.json"),
        cache: bool = True,
    ) -> None:
        if anthropic is None:
            raise ImportError("anthropic package required: pip install anthropic")

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._cost_log_path = Path(cost_log_path)
        self._cache = cache
        self._session_id = datetime.now(timezone.utc).isoformat()
        self._session_cost = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def session_cost(self) -> float:
        """Total USD cost accumulated this session."""
        return self._session_cost

    # ------------------------------------------------------------------
    # Generic batch infrastructure
    # ------------------------------------------------------------------

    def _create_batch(self, requests: list[dict]) -> str:
        """Submit a batch and return the batch ID (synchronous API call)."""
        batch = self._client.messages.batches.create(requests=requests)
        logger.info("Submitted batch %s (%d requests)", batch.id, len(requests))
        return batch.id

    def _poll_batch(
        self,
        batch_id: str,
        expected_count: int,
        poll_interval: float = 30.0,
    ) -> dict[str, str]:
        """Poll a batch until done, return {custom_id: response_text}."""
        while True:
            time.sleep(poll_interval)
            status = self._client.messages.batches.retrieve(batch_id)
            if status.processing_status == "ended":
                break
            logger.debug("Batch %s status: %s", batch_id, status.processing_status)

        results: dict[str, str] = {}
        for result in self._client.messages.batches.results(batch_id):
            cid = result.custom_id
            rtype = getattr(result.result, "type", "unknown")
            if rtype != "succeeded":
                logger.error(
                    "Batch result %s: type=%s (not succeeded) — "
                    "this table will have no data for this stage",
                    cid, rtype,
                )
                continue
            try:
                text = result.result.message.content[0].text
                results[cid] = text

                usage = result.result.message.usage
                parts = cid.split("__")
                table_id = parts[0] if parts else cid
                role = parts[1] if len(parts) > 1 else "unknown"
                cost = _compute_cost(usage, self._model)
                self._session_cost += cost
                entry = CostEntry(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    session_id=self._session_id,
                    table_id=table_id,
                    agent_role=role,
                    model=self._model,
                    input_tokens=getattr(usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(usage, "output_tokens", 0) or 0,
                    cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                    cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                    cost_usd=cost,
                )
                _append_cost_entry(self._cost_log_path, entry)
            except (AttributeError, IndexError) as exc:
                logger.warning("Could not parse batch result %s: %s", cid, exc)

        if len(results) < expected_count:
            logger.error(
                "Batch %s: received %d/%d results — %d missing. "
                "Missing tables will have no data for this stage.",
                batch_id, len(results), expected_count,
                expected_count - len(results),
            )

        return results

    def _submit_and_poll(
        self,
        requests: list[dict],
        max_batch_bytes: int = 200_000_000,  # 200MB safety margin under 256MB limit
    ) -> dict[str, str]:
        """Submit request(s) as one or more batches, poll each, merge results.

        Splits into sub-batches if the serialized size would exceed
        ``max_batch_bytes`` (the Batch API limit is 256MB).
        """
        if not requests:
            return {}

        # Split requests into sub-batches that fit under the size limit
        batches: list[list[dict]] = []
        current_batch: list[dict] = []
        current_size = 0

        for req in requests:
            # Estimate serialized size (json length in bytes is a good proxy)
            req_size = len(json.dumps(req))
            if current_batch and current_size + req_size > max_batch_bytes:
                batches.append(current_batch)
                current_batch = []
                current_size = 0
            current_batch.append(req)
            current_size += req_size

        if current_batch:
            batches.append(current_batch)

        if len(batches) > 1:
            logger.info(
                "Splitting %d requests into %d sub-batches to stay under 256MB limit",
                len(requests), len(batches),
            )

        all_results: dict[str, str] = {}
        for i, batch_requests in enumerate(batches, 1):
            if len(batches) > 1:
                logger.info("Submitting sub-batch %d/%d (%d requests)", i, len(batches), len(batch_requests))
            batch_id = self._create_batch(batch_requests)
            results = self._poll_batch(batch_id, len(batch_requests))
            all_results.update(results)

        return all_results

    # ------------------------------------------------------------------
    # Table rendering
    # ------------------------------------------------------------------

    def _prepare_table(
        self, spec: TableVisionSpec,
    ) -> list[tuple[str, str]]:
        """Render PNG(s) for a table spec.

        Opens the PDF, renders the crop region (possibly as multiple
        overlapping strips for tall tables), and base64-encodes each image.

        Returns list of (base64_string, media_type) pairs.
        """
        doc = pymupdf.open(str(spec.pdf_path))
        try:
            page = doc[spec.page_num - 1]
            strips = render_table_region(page, spec.bbox)
            return [
                (base64.b64encode(png_bytes).decode("ascii"), media_type)
                for png_bytes, media_type in strips
            ]
        finally:
            doc.close()

    # ------------------------------------------------------------------
    # Request building
    # ------------------------------------------------------------------

    def _build_request(
        self,
        spec: TableVisionSpec,
        images: list[tuple[str, str]],
    ) -> dict:
        """Build one Anthropic batch request dict.

        Args:
            spec: Table vision spec (provides raw_text, caption, garbled, table_id).
            images: Pre-rendered images as (base64_string, media_type) pairs.

        Returns:
            Batch request dict with custom_id, params (model, max_tokens,
            system, messages).
        """
        ctx = build_common_ctx(spec.raw_text, spec.caption, spec.garbled)

        user_content: list[dict] = [{"type": "text", "text": ctx}]
        for b64, media_type in images:
            user_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64,
                },
            })

        system_blocks: list[dict] = [
            {"type": "text", "text": VISION_FIRST_SYSTEM},
        ]
        if self._cache:
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}

        return {
            "custom_id": f"{spec.table_id}__transcriber",
            "params": {
                "model": self._model,
                "max_tokens": 4096,
                "system": system_blocks,
                "messages": [{"role": "user", "content": user_content}],
            },
        }

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def extract_tables_batch(
        self,
        specs: list[TableVisionSpec],
    ) -> list[AgentResponse]:
        """Extract tables via the Anthropic Batch API.

        Renders each table as PNG(s), builds batch requests with
        VISION_FIRST_SYSTEM prompt, submits a single batch, polls
        until complete, and parses responses.

        Re-crop is NOT handled here. If any response has
        recrop_needed=True, the caller should compute a new crop
        and call this method again with updated specs.

        Args:
            specs: Table vision specs to extract.

        Returns:
            AgentResponse per spec, in the same order as input.
            Failed/missing batch results return AgentResponse with
            parse_success=False.
        """
        if not specs:
            return []

        requests: list[dict] = []
        for spec in specs:
            images = self._prepare_table(spec)
            requests.append(self._build_request(spec, images))

        results = self._submit_and_poll(requests)

        responses: list[AgentResponse] = []
        for spec in specs:
            raw_text = results.get(f"{spec.table_id}__transcriber")
            if raw_text is not None:
                responses.append(parse_agent_response(raw_text, "transcriber"))
            else:
                responses.append(AgentResponse(
                    headers=[], rows=[], footnotes="",
                    table_label=None, caption="",
                    is_incomplete=False, incomplete_reason="",
                    raw_shape=(0, 0), parse_success=False,
                    raw_response="",
                    recrop_needed=False, recrop_bbox_pct=None,
                ))
        return responses
