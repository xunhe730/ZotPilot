# Changelog

## [0.2.1] - 2026-03-19

### Added
- `switch_library` tool — list available libraries (user + groups) or switch active library context. Resets all singletons on switch via `_reset_singletons()`
- `get_annotations` tool — read highlights and comments via Zotero Web API (requires ZOTERO_API_KEY). Uses new `ZoteroApiReader` (read-only pyzotero client, separate from `ZoteroWriter`)
- `_get_api_reader()` singleton in state.py for annotation reads
- `get_libraries()` method in ZoteroClient (SQLite, includes group libraries)
- Tool count: 30 → 32

## [0.2.0] - 2026-03-19

### Added
- **No-RAG mode**: `embedding_provider: "none"` disables semantic search while keeping all metadata-based tools functional (advanced_search, get_notes, list_tags, citations via SQLite DOI fallback, etc.)
- `_get_store_optional()` pattern in state.py for graceful degradation
- Citation tools fall back to SQLite for DOI lookup when vector store unavailable

### Changed
- `config.py` accepts `"none"` as valid embedding_provider, skips API key validation
- `embeddings/__init__.py` returns `None` for provider "none"
- `get_index_stats` returns `{mode: "no-rag", total_documents: 0}` in No-RAG mode
- `get_reranking_config` returns `{enabled: false, mode: "no-rag"}` in No-RAG mode
- `get_paper_details` and `get_library_overview` show `indexed: false` in No-RAG mode
- `search_papers`, `search_topic`, `get_passage_context` raise clear ToolError in No-RAG mode

## [0.1.5] - 2026-03-19

### Added
- `get_feeds` tool — list RSS feeds or get feed items (SQLite, no API key needed). Graceful degradation for old Zotero versions without feeds table
- Tool count: 29 → 30

## [0.1.4] - 2026-03-19

### Added
- `get_notes` tool — read and search notes by parent item or content keyword (SQLite, no API key needed)
- `create_note` tool — create child notes on Zotero items (requires ZOTERO_API_KEY)
- `advanced_search` tool — multi-condition metadata search by year/author/tag/collection/title/doi/publication. Works without indexing. Tag matching uses direct JOIN to prevent false positives ("ML" won't match "HTML")
- Tool count: 26 → 29

## [0.1.3] - 2026-03-19

### Changed
- All tool docstrings slimmed to 1-3 sentences (<500 chars each) — total docstring reduced from 17.5 KB to 2.1 KB (-88%)
- Parameter documentation migrated from docstrings to `Annotated[type, Field(description="...")]` for structured schema generation
- 5 batch tools merged into 2: `batch_tags(action="add|set|remove")` and `batch_collections(action="add|remove")` — tool count 29 → 26

### Removed
- `batch_add_tags`, `batch_set_tags`, `batch_remove_tags`, `batch_add_to_collection`, `batch_remove_from_collection` (replaced by `batch_tags` and `batch_collections`)

## [0.1.2] - 2026-03-19

### Added
- Query embedding cache in `VectorStore` (maxsize=512, FIFO eviction) — avoids repeated embedding API calls for identical queries
- 5 batch write tools: `batch_add_tags`, `batch_set_tags`, `batch_remove_tags`, `batch_add_to_collection`, `batch_remove_from_collection` (max 100 items, per-item error reporting)
- `zotpilot doctor` now validates `ZOTERO_USER_ID` is numeric (catches username vs ID confusion)
- SKILL.md updated with batch tool documentation and workflow chains

### Removed
- Built-in Chinese→English query translation (`translation.py` deleted) — bilingual search is now the Agent's responsibility, not the MCP server's
- `_contains_chinese` and `_translate_to_english` removed from search pipeline

### Changed
- `VectorStore.search()` uses cached embeddings via `_cached_embed_query()`
- `index_library()` clears query cache after indexing to ensure new documents are findable
- Search tool docstrings updated: query accepts any language, Agent should translate if needed

## [0.1.1] - 2026-03-19

### Fixed
- Ghost tool name: `index_documents()` references corrected to `index_library()`
- ReDoS: title_pattern now validates regex and rejects patterns over 200 chars
- API key exposure: setup wizard no longer prints full Gemini key to terminal
- Thread safety: all singleton initializers use double-checked locking
- `search_boolean` bypassed singleton — now uses `_get_zotero()` instead of raw `ZoteroClient()`
- `get_item()` no longer does a full table scan — dedicated SQL with WHERE clause
- Collection cache not invalidated after write ops — `create_collection`, `add_to_collection`, `remove_from_collection` now clear cache

### Changed
- Split `state.py` (432 lines, 7 responsibilities) into 4 modules: `state.py`, `filters.py`, `translation.py`, `result_utils.py`
- `_translate_to_english` now uses `_get_config()` (thread-safe) instead of reading global `_config` directly
- `VectorStore` metadata construction extracted into `_build_base_metadata()` helper
- `ZoteroClient.get_all_items_with_pdfs()` refactored to reuse `_row_to_item()` helper
- All search results now include both `doc_id` and `item_key` fields for LLM chain compatibility
- SKILL.md: installation flow now collects Zotero Web API credentials upfront, clarifies numeric User ID vs username

### Added
- Tests: `test_tools_search.py`, `test_indexer.py`, `test_journal_ranker.py` (26 new tests)
- Thread safety test for `_get_config()` concurrent access
- CI coverage gate (`--cov-fail-under=29`)

## [0.1.0] - 2026-03-16

### Added
- Initial release as ZotPilot (repackaged from deep-zotero)
- 26 MCP tools for search, indexing, citations, and library management
- Gemini and local embedding providers
- Section-aware reranking with journal quality weighting
- PDF extraction with table, figure, and OCR support
- Zotero auto-detection from profiles.ini
- Setup CLI wizard (`zotpilot setup`)
- Config migration from deep-zotero
- Claude Code skill for guided installation and usage
- GitHub Actions CI (ruff + mypy + pytest)
