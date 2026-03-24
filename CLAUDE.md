# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable dev install)
uv pip install -e ".[dev]"

# Run MCP server directly
uv run zotpilot

# CLI subcommands
uv run zotpilot setup --non-interactive --provider gemini
uv run zotpilot index [--force] [--limit N] [--item-key KEY] [--max-pages N]
uv run zotpilot status [--json]
uv run zotpilot doctor [--full]
uv run zotpilot config set <key> <value>
uv run zotpilot register [--gemini-key KEY] [--zotero-api-key KEY] [--zotero-user-id ID]

# Tests
uv run pytest                          # all tests (coverage threshold: 29%)
uv run pytest tests/test_config.py    # single file
uv run pytest -k test_name            # single test by name

# Lint / type check
uv run ruff check src tests
uv run mypy src
```

## Architecture

ZotPilot exposes a **FastMCP server** with 32 tools for semantic search over a local Zotero library. The architecture has four main layers:

### 1. Entry points
- `cli.py` — argparse CLI with subcommands: `setup`, `index`, `status`, `doctor`, `config`, `register`. With no subcommand, runs the MCP server.
- `server.py` — thin shim: imports `state.mcp` and the `tools/` package (import side-effects register all tools), then calls `mcp.run()`.

### 2. MCP state and lazy singletons (`state.py`)
All shared objects (`VectorStore`, `Retriever`, `Reranker`, `ZoteroClient`, `ZoteroWriter`, `ZoteroApiReader`, `IdentifierResolver`) are lazy singletons protected by a single `threading.Lock`. Tools call `_get_retriever()`, `_get_zotero()`, etc. on each request. `switch_library` calls `_reset_singletons()` to tear them all down. A background thread monitors the parent process PID and calls `os._exit(0)` when it dies (prevents orphaned server processes).

### 3. Tool modules (`tools/`)
Eight modules, each imported by `tools/__init__.py` to trigger `@mcp.tool` decorator registration:

| Module | Responsibility |
|--------|----------------|
| `search.py` | `search_papers`, `search_topic`, `search_boolean`, `search_tables`, `search_figures` |
| `context.py` | `get_passage_context`, `get_paper_details` |
| `library.py` | `get_library_overview`, `advanced_search`, `get_notes`, `list_tags`, `list_collections`, etc. |
| `indexing.py` | `index_library`, `get_index_stats` |
| `citations.py` | `find_references`, `find_citing_papers`, `get_citation_count` |
| `write_ops.py` | `create_note`, `add_item_tags`, `set_item_tags`, `create_collection`, `add_to_collection`, etc. |
| `admin.py` | `switch_library`, `get_reranking_config`, `get_vision_costs` |
| `ingestion.py` | `search_academic_databases`, `add_paper_by_identifier`, `ingest_papers` |

Write operations (`write_ops.py`) require `ZOTERO_API_KEY` + `ZOTERO_USER_ID` env vars; they use `ZoteroWriter` (pyzotero Web API). Read-only tools use `ZoteroClient` (local SQLite).

### 4. RAG pipeline
```
PDF files
  └─ pdf/extractor.py          (PyMuPDF text extraction, OCR fallback)
  └─ feature_extraction/       (vision API for figures/tables, PaddleOCR optional)
  └─ pdf/chunker.py            (text → chunks with section classification)
  └─ pdf/section_classifier.py (labels chunks: Abstract, Methods, Results, etc.)
  └─ embeddings/               (base.py interface; gemini.py, dashscope.py, local.py impls)
  └─ vector_store.py           (ChromaDB wrapper; stores chunks with metadata)

Query path:
  retriever.py  →  vector_store.py  →  reranker.py  (RRF + section/journal weights)
```

### No-RAG mode
Setting `embedding_provider = "none"` in config disables the vector index. `_get_store_optional()` returns `None`; tools fall back to SQLite metadata search. `advanced_search`, notes, tags, and collections work without indexing.

## Configuration

Config file: `~/.config/zotpilot/config.json` (Unix) / `%APPDATA%\zotpilot\config.json` (Windows).
ChromaDB data: `~/.local/share/zotpilot/chroma/` (Unix).

API keys are always read from environment first, then config file. `Config.save()` never persists API keys to disk.

| Env var | Purpose |
|---------|---------|
| `GEMINI_API_KEY` | Embeddings (gemini provider) |
| `DASHSCOPE_API_KEY` | Embeddings (dashscope provider) |
| `ANTHROPIC_API_KEY` | Vision extraction (figures/tables) |
| `ZOTERO_API_KEY` | Write operations |
| `ZOTERO_USER_ID` | Numeric Zotero user ID |
| `S2_API_KEY` | Semantic Scholar (optional, higher rate limit) |

## Git Workflow

### Branch Strategy

- **`main`** — 生产分支，仅接受来自 `dev` 的 PR 合并，**禁止直接 push**
- **`dev`** — 日常开发分支，所有功能和修复都在此分支提交

### Rules

- **NEVER** `git push origin main` 直接推送 main
- 所有变更通过 PR 从 `dev` → `main` 合并
- 发版时：在 `dev` 完成 release checklist → 提 PR → 合并到 `main` → 在 `main` 打 tag

### Daily workflow

```bash
# 确保在 dev 分支
git checkout dev

# 功能开发完毕后推送
git push origin dev

# 需要发版时，提 PR
gh pr create --base main --head dev --title "release: vX.Y.Z"
```

## Version Management

Claude is responsible for version management on this project. When the user says "发版"、"release"、or similar, execute the full flow without asking for sub-confirmations:

### Release flow
1. **Commit** all staged changes with a conventional commit message (`feat:` / `fix:` / `docs:` etc.)
2. **Tag** `vX.Y.Z` — must match `pyproject.toml` version (CI validates this)
3. **Push** commit + tag: `git push && git push --tags`
4. CI (`release.yml`) auto-publishes to PyPI and creates the GitHub Release from CHANGELOG

### Version bump rules
- `patch` (0.x.**Z**): bug fixes, doc updates, test additions
- `minor` (0.**Y**.0): new user-facing features (new CLI subcommand, new MCP tool)
- `major` (**X**.0.0): breaking changes to MCP tool signatures or config format

### Per-release checklist
- [ ] `pyproject.toml` version bumped
- [ ] `src/zotpilot/__init__.py` `__version__` in sync
- [ ] `CHANGELOG.md` has a `## [X.Y.Z] - YYYY-MM-DD` entry at the top
- [ ] `README.md` reflects any new commands or features
- [ ] `uv run pytest -q` passes (coverage ≥ 29%)
- [ ] commit → tag → push

### CHANGELOG format
Follow the bilingual (中文 / English) format already established in CHANGELOG.md.
The CI `awk` extractor reads between the first two `## [` headers — keep that structure intact.

## Key design patterns

- **Singleton pattern with double-checked locking**: all expensive objects initialized once per server process, reset on `switch_library`.
- **No-RAG fallback**: `embedding_provider="none"` lets metadata-only tools work without ChromaDB or an embedding API.
- **Embedding provider abstraction**: `embeddings/base.py` defines `Embedder` interface; `embeddings/__init__.py:create_embedder(config)` returns the right impl.
- **Tool registration via import side-effects**: `server.py` does `from . import tools` which imports all 8 tool modules, each of which calls `@mcp.tool` on their functions.
- **Filters and result utils are re-exported from `state.py`** for backward compatibility (`filters.py`, `result_utils.py`).
