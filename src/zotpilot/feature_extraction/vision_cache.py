"""Content-addressed cache for paid vision table-extraction results.

A re-run of indexing (e.g. resuming after a rate-limit abort) re-extracts every
PDF and re-derives the same ``TableVisionSpec`` crops deterministically. Without
this cache each re-run re-pays the vision Batch API for tables it already
transcribed. This caches one ``AgentResponse`` per spec keyed by the *content*
of the vision request (PDF bytes + page + crop bbox + caption + raw text + model
variant), so an unchanged PDF returns its transcriptions for free.

Only successful responses are cached — caching a parse failure would freeze a
transient error and prevent a retry on the next run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .vision_extract import AgentResponse

logger = logging.getLogger(__name__)

_PDF_HASH_BYTES = 65536  # first 64 KiB — enough to detect a replaced PDF


class VisionResultCache:
    """Per-spec ``AgentResponse`` cache stored as one JSON file per content key."""

    def __init__(self, cache_dir: Path | str) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._hash_memo: dict[str, str | None] = {}

    # -- keying -------------------------------------------------------------
    def _pdf_hash(self, pdf_path: Path | str) -> str | None:
        key = str(pdf_path)
        if key in self._hash_memo:
            return self._hash_memo[key]
        try:
            h = hashlib.sha256()
            with open(pdf_path, "rb") as f:
                h.update(f.read(_PDF_HASH_BYTES))
            digest: str | None = h.hexdigest()
        except OSError:
            digest = None  # unreadable PDF → uncacheable (always a miss)
        self._hash_memo[key] = digest
        return digest

    def content_key(self, spec, variant: str) -> str | None:
        """Stable content hash for a spec, or None if the PDF can't be hashed.

        ``variant`` folds in the model + prompt mode so a config change that would
        change the model's output also busts the cache.
        """
        pdf_h = self._pdf_hash(spec.pdf_path)
        if pdf_h is None:
            return None
        payload = json.dumps(
            {
                "pdf": pdf_h,
                "page": spec.page_num,
                "bbox": [round(float(x), 2) for x in spec.bbox],
                "garbled": bool(spec.garbled),
                "caption": spec.caption or "",
                "raw_text": spec.raw_text or "",
                "variant": variant,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # -- storage ------------------------------------------------------------
    def get(self, key: str | None) -> AgentResponse | None:
        if not key:
            return None
        fp = self._dir / f"{key}.json"
        if not fp.exists():
            return None
        try:
            return _from_dict(json.loads(fp.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as e:
            # A corrupt/schema-drifted entry must never crash or return a
            # half-built response — treat as a miss and let it be rewritten.
            logger.warning("Ignoring unusable vision cache entry %s: %s", fp, e)
            return None

    def put(self, key: str | None, response: AgentResponse) -> None:
        if not key:
            return
        fd, tmp_path = tempfile.mkstemp(dir=self._dir, suffix=".tmp", prefix="vc_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(_to_dict(response), f)
            os.replace(tmp_path, self._dir / f"{key}.json")
        except OSError as e:
            logger.warning("Failed to write vision cache entry %s: %s", key, e)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _to_dict(resp: AgentResponse) -> dict:
    d = asdict(resp)
    d["raw_shape"] = list(resp.raw_shape)  # tuple → JSON list
    return d


def _from_dict(d: dict) -> AgentResponse:
    data = dict(d)
    rs = data.get("raw_shape") or [0, 0]
    data["raw_shape"] = (int(rs[0]), int(rs[1]))
    return AgentResponse(**data)  # raises TypeError on schema drift → caught as miss
