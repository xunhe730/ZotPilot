# Contributing to ZotPilot

## Development Setup

```bash
git clone https://github.com/xdzhuang/ZotPilot.git
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

1. Create `src/zotpilot/embeddings/your_provider.py`
2. Implement `EmbedderProtocol` from `embeddings/base.py`:
   - `embed(texts, task_type)` → list of vectors
   - `embed_query(query)` → single vector
3. Add a `dimensions` attribute
4. Register in `embeddings/__init__.py` `create_embedder()` factory
5. Add config validation in `config.py`
6. Add tests in `tests/test_embedder.py`

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
