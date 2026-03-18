# Changelog

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
- 24 MCP tools for search, indexing, citations, and library management
- Gemini and local embedding providers
- Section-aware reranking with journal quality weighting
- PDF extraction with table, figure, and OCR support
- Zotero auto-detection from profiles.ini
- Setup CLI wizard (`zotpilot setup`)
- Config migration from deep-zotero
- Claude Code skill for guided installation and usage
- GitHub Actions CI (ruff + mypy + pytest)
