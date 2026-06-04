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

### Vendor preset catalog (`EMBEDDING_PRESETS`)

`providers.py` also holds `EMBEDDING_PRESETS` — a flat list of `VendorPreset`
entries that **only** pre-fill the interactive setup wizard for the
`openai-compatible` provider. They never appear at runtime, so they are
best-effort and drift-tolerant: a stale preset just means "the user overrides the
wrong default," not a crash (`Custom` is always a fallback).

- To add/update a vendor: append or edit a `VendorPreset(name, base_url,
  embedding_model, embedding_dimensions, key_url, requires_key)`. Set
  `requires_key=False` for keyless local endpoints (e.g. Ollama).
- Seed values were verified against vendor docs (SiliconFlow / Zhipu·GLM /
  Ollama / OpenAI) in 2026-06; re-verify against the vendor's current docs when
  updating, since dimensions and base_url roots change.
- **Do NOT add chat-only vendors** that have no embeddings API (e.g. DeepSeek).
  Qwen stays on the dedicated `dashscope` provider (native asymmetric retrieval);
  it is still reachable via the SiliconFlow preset by overriding the model.

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
