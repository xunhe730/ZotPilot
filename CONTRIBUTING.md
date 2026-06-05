# Contributing to ZotPilot

## Development Setup

```bash
git clone https://github.com/xunhe730/ZotPilot.git
cd ZotPilot
uv sync --extra dev
```

## Code Style

- **Formatter/linter**: ruff (`uv run ruff check src/` and `uv run ruff format src/`)
- **Type checking**: mypy (`uv run mypy src/zotpilot/ --ignore-missing-imports`)
- **Line length**: 120 characters
- **Target**: Python 3.10+

## Running Tests

```bash
uv run pytest                          # Run all tests
uv run pytest --cov=zotpilot           # With coverage
uv run pytest tests/test_chunker.py    # Single test file
```

## Adding a New Embedding Provider

> **First check whether you need a new provider at all.** Any vendor that exposes
> an OpenAI-compatible `/embeddings` endpoint (SiliconFlow, Zhipu/GLM, Ollama,
> vLLM, most self-hosted servers) is reachable through the existing generic
> `openai-compatible` provider — it is a `base_url` + `model` + `dimensions`
> choice, not new code. Only add a dedicated provider for a genuinely
> non-OpenAI-compatible API (e.g. a native asymmetric-retrieval endpoint).

1. Create `src/zotpilot/embeddings/your_provider.py`
2. Implement `EmbedderProtocol` from `embeddings/base.py`:
   - `embed(texts, task_type)` → list of vectors
   - `embed_query(query)` → single vector
3. Add a `dimensions` attribute
4. Register the provider in **`src/zotpilot/providers.py`** — the single source of
   truth for the embedding allow-list. Add the name to `EMBEDDING_PROVIDERS` and a
   `(model, dimensions)` entry to `EMBEDDING_MODEL_DEFAULTS`. (The `validate()`
   allow-list, the factory error message, the CLI `--provider` choices, and the
   `config.load()` defaults all read from here — do **not** hand-edit those sites.)
5. Wire a branch into `embeddings/__init__.py` `create_embedder()` factory.
6. Add config validation in `config.py` (provider-specific required fields).
7. Add tests in `tests/test_embedder.py` and registry tests in `tests/test_provider_registry.py`.

### The vendor → model catalog (`VENDOR_CATALOG`)

`providers.py` holds `VENDOR_CATALOG` — the **single source of truth** for the
two-layer "vendor → model" setup UX. It is pure data and feeds ALL THREE setup
surfaces with no drift: the interactive wizard menus, the non-interactive
`--provider <vendor>` CLI, and the Agent skill (`ztp-setup`) via
`zotpilot setup --list-vendors --json`. Vendors map a setup-time choice onto the
runtime `embedding_provider` + `base_url`; the catalog **never appears at
runtime** and is drift-tolerant (a stale dim degrades to a setup-probe warning /
C1 error, never silent corruption).

**Principle-1 boundary (hard rule).** `VENDOR_CATALOG` / `resolve_setup_choice`
(and the `Vendor`/`VendorModel` types) are SETUP-LAYER only. They MUST NEVER be
imported by `config.py` or by any embedder in `embeddings/`. The runtime
authority stays `EMBEDDING_PROVIDERS` + `EMBEDDING_MODEL_DEFAULTS`. A test in
`tests/test_provider_registry.py` asserts `config.py` does not import the catalog
symbols — keep it green.

- **Adding/updating a model is ONE data edit.** Append or edit a
  `VendorModel(model, dimensions, note="", recommended=False)` inside the right
  `Vendor`'s `models=(...)`. No code change, and no test menu-index churn (the
  tests compute indices dynamically). Exactly one model per non-Custom vendor is
  `recommended=True`. Adding a model automatically flows to
  `setup --list-vendors` and the `ztp-setup` skill — **no skill edit needed**.
  Adding a whole vendor is one `Vendor(...)` tuple (a consistency test pins
  `vendor.provider ∈ EMBEDDING_PROVIDERS`).
- **MANDATORY drift gate on any `dimensions` edit.** Whenever you add or change a
  model's `dimensions`, you MUST run `python scripts/verify_vendor_catalog.py`
  with the relevant API keys in env BEFORE committing. It live-POSTs each
  `(model, dimensions)` (mirroring the runtime dimensions-drop-on-400 fallback)
  and fails on a length mismatch. It is the ONLY value-correctness check for the
  high-churn OpenAI-compatible rows and is **not in CI** (needs keys + network);
  keyless/unset vendors are skipped with a logged note.
- **Do NOT add chat-only vendors** that have no embeddings API (e.g. DeepSeek).
  Qwen3-Embedding is offered ONLY via SiliconFlow's OpenAI-compatible endpoint,
  never as a standalone dashscope-native row — the dedicated `dashscope` provider
  keeps Qwen's native asymmetric-retrieval path.

## Adding a New MCP Tool

1. Choose the appropriate `tools/*.py` module (or create a new one)
2. Import `mcp` and helpers from `state.py`
3. Decorate with `@mcp.tool()`
4. Add comprehensive docstring (this becomes the tool description in MCP)
5. If new module, add import in `tools/__init__.py`

## Pull Request Process

1. Create a feature branch
2. Write tests first (TDD preferred)
3. Ensure `uv run ruff check src/` passes
4. Ensure `uv run pytest` passes
5. Submit PR with description of changes
