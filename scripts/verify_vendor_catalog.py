#!/usr/bin/env python3
"""Live drift check for ``VENDOR_CATALOG`` seeded ``(model, dimensions)``.

This is the MANDATORY pre-commit gate whenever a catalog model's ``dimensions``
is added or changed (see CONTRIBUTING). It POSTs one tiny embedding per curated
``(vendor, model)`` to the vendor's real ``/embeddings`` endpoint and asserts the
returned vector length equals the seeded ``dimensions`` -- the only
value-correctness check for the high-churn OpenAI-compatible rows. It is NOT run
in CI (it needs API keys + network).

Behavior:
  - Only OpenAI-compatible vendors with curated models are probed
    (gemini/dashscope/local are reported as not wire-probeable; ``custom`` has no
    curated models). Mirrors the runtime dimensions-drop-on-400 fallback in
    ``zotpilot.embeddings.openai_compat`` so fixed-dim models (e.g. SiliconFlow
    ``bge-m3``) are not false-flagged.
  - The API key is resolved from each vendor's ``key_env`` env vars (first
    non-empty). A vendor that ``requires_key`` but has none set is SKIPPED with a
    logged note -- never silently.
  - Exit code is non-zero ONLY if a real MISMATCH is found. Auth / unreachable /
    skipped do not fail the run (no keys or no network is a legitimate local
    condition).

Usage::

    ZOTPILOT_EMBEDDING_API_KEY=<key> python scripts/verify_vendor_catalog.py
"""
from __future__ import annotations

import os
import sys

import httpx

# Allow running straight from a checkout without installing.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zotpilot import providers  # noqa: E402


def _resolve_key(env_names: tuple[str, ...]) -> str | None:
    for name in env_names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _probe(base_url: str, api_key: str | None, model: str, dims: int) -> tuple[str, str]:
    """Return ``(state, detail)`` for one (model, dims) live probe.

    ``state`` ∈ {"pass", "mismatch", "auth", "unreachable", "error"}. Mirrors the
    runtime dimensions-drop-on-400 fallback: on HTTP 400 while ``dimensions`` was
    sent, retry once without it and classify on the native response length.
    """
    url = base_url.rstrip("/") + "/embeddings"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def _post(send_dimensions: bool) -> dict:
        payload: dict = {"model": model, "input": "ping", "encoding_format": "float"}
        if send_dimensions:
            payload["dimensions"] = dims
        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()

    try:
        try:
            data = _post(send_dimensions=True)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                data = _post(send_dimensions=False)
            else:
                raise
        vec = data["data"][0]["embedding"]
        returned = len(vec) if isinstance(vec, list) else None
        if returned != dims:
            return "mismatch", f"expected={dims} returned={returned}"
        return "pass", f"dims={dims}"
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in (401, 403):
            return "auth", f"HTTP {status} (bad/expired key)"
        return "error", f"HTTP {status}: {exc.response.text[:120]}"
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return "unreachable", f"cannot reach {base_url}: {exc}"
    except Exception as exc:  # noqa: BLE001 — report, do not crash the gate
        return "error", str(exc)


def main() -> int:
    passed = mismatched = errored = skipped = 0
    for vendor in providers.VENDOR_CATALOG:
        if vendor.provider != "openai-compatible" or not vendor.models:
            print(f"SKIP {vendor.key}: not OpenAI-wire-probeable / no curated models")
            skipped += 1
            continue
        api_key = _resolve_key(vendor.key_env)
        if vendor.requires_key and not api_key:
            print(
                f"SKIP {vendor.key}: no API key in env "
                f"({' / '.join(vendor.key_env) or 'none'})"
            )
            skipped += 1
            continue
        base_url = vendor.base_url or ""
        for model in vendor.models:
            state, detail = _probe(base_url, api_key, model.model, model.dimensions)
            tag = {
                "pass": "PASS",
                "mismatch": "MISMATCH",
                "auth": "SKIP(auth)",
                "unreachable": "SKIP(unreachable)",
                "error": "ERROR",
            }[state]
            print(f"{tag} {vendor.key}/{model.model} {detail}")
            if state == "pass":
                passed += 1
            elif state == "mismatch":
                mismatched += 1
            elif state == "error":
                errored += 1
            else:
                skipped += 1

    print(
        f"\nSummary: {passed} passed, {mismatched} mismatched, "
        f"{errored} errored, {skipped} skipped."
    )
    # Only a real dimension mismatch is a hard failure.
    return 1 if mismatched else 0


if __name__ == "__main__":
    raise SystemExit(main())
